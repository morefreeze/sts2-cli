#!/usr/bin/env python3
"""BUG-054: setup.sh's IL patches must not stop any sts2.dll method from JIT-compiling.

setup.sh rewrites lib/sts2.dll with Mono.Cecil.  A patch can leave a method with IL the CLR rejects
(InvalidProgramException) -- and that only shows when the method is first JIT-compiled, mid-game, where
it kills whatever called it (the combat turn loop, for SandpitPower.UpdateCreaturePositions).  This
tool asks the CLR up front: it JIT-compiles every method of the PATCHED dll and of the UNPATCHED
dll.original with the very same GodotSharp.dll (tools/jit_probe.py's probe) and fails when the patched
one cannot compile a method the original can.  (Delegate.BeginInvoke/EndInvoke fail in both and are
therefore ignored; so is anything a stub gap breaks, because the original breaks on it too.)

    .venv/bin/python tools/check_il_patch.py             # lib/sts2.dll vs lib/sts2.dll.original (read-only)
    .venv/bin/python tools/check_il_patch.py --repatch   # run setup.sh's patcher on a COPY of the original, check that

--repatch extracts the C# patcher out of setup.sh, builds it in a temp dir and patches a temp copy, so
it verifies what setup.sh WOULD produce without touching lib/, src/**/bin or src/**/obj (safe next to
a running experiment).  Exit status: 0 = clean, 1 = the patched dll fails somewhere the original does
not, 2 = could not run.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_godot_stub_refs import BuildError, REPO, build_stubs, find_dotnet, find_real_godot  # noqa: E402
import jit_probe  # noqa: E402

SETUP_SH = os.path.join(REPO, "setup.sh")
_CSPROJ = re.compile(r"""cat > "\$PATCH_DIR/Patcher\.csproj" << 'PROJ'\n(.*?)\nPROJ\n""", re.S)
_PROGRAM = re.compile(r"""cat > "\$PATCH_DIR/Program\.cs" << 'CSHARP'\n(.*?)\nCSHARP\n""", re.S)


def extract_patcher(setup_text: str | None = None) -> tuple[str, str]:
    """(Patcher.csproj, Program.cs) exactly as setup.sh writes them to its temp dir."""
    if setup_text is None:
        with open(SETUP_SH, encoding="utf-8") as f:
            setup_text = f.read()
    csproj, program = _CSPROJ.search(setup_text), _PROGRAM.search(setup_text)
    if not csproj or not program:
        raise SystemExit("error: cannot find the Patcher.csproj / Program.cs heredocs in setup.sh")
    return csproj.group(1) + "\n", program.group(1) + "\n"


def build_patcher(dotnet: str, tmp: str, csproj: str, program: str, name: str = "patcher") -> str:
    """Compile a patcher into <tmp>/<name>_out; returns the path of Patcher.dll."""
    src = os.path.join(tmp, name + "_src")
    os.makedirs(src)
    with open(os.path.join(src, "Patcher.csproj"), "w", encoding="utf-8") as f:
        f.write(csproj)
    with open(os.path.join(src, "Program.cs"), "w", encoding="utf-8") as f:
        f.write(program)
    out = os.path.join(tmp, name + "_out")
    r = subprocess.run([dotnet, "build", os.path.join(src, "Patcher.csproj"), "-c", "Release", "-o", out, "--nologo",
                        "-v", "q", "--disable-build-servers", "-p:UseSharedCompilation=false"],
                       cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(r.stdout + r.stderr)
    return os.path.join(out, "Patcher.dll")


def lib_copy(tmp: str, name: str, game_lib: str, sts2_dll: str) -> str:
    """A scratch lib dir: every dll of `game_lib` except sts2.dll, plus `sts2_dll` as sts2.dll."""
    d = os.path.join(tmp, name)
    os.makedirs(d)
    for f in glob.glob(os.path.join(game_lib, "*.dll")):
        if os.path.basename(f) != "sts2.dll":
            shutil.copy(f, d)
    shutil.copy(sts2_dll, os.path.join(d, "sts2.dll"))
    return d


def patch_copy(dotnet: str, patcher_dll: str, tmp: str, name: str, game_lib: str, original: str) -> tuple[str, str]:
    """Run a patcher on a scratch copy of `original`; returns (lib dir holding the patched sts2.dll, patcher stdout)."""
    d = lib_copy(tmp, name, game_lib, original)
    r = subprocess.run([dotnet, patcher_dll, os.path.join(d, "sts2.dll")], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError("the patcher itself failed:\n" + r.stdout + r.stderr)
    return d, r.stdout


def jit_failures(dotnet: str, tmp: str, probe_out: str, name: str, lib_dir: str, godot_dll: str) -> dict[str, list[str]]:
    """{exception text: [Type::Method, ...]} for every method of lib_dir/sts2.dll the CLR cannot JIT-compile."""
    d = jit_probe.run_dir(tmp, "run_" + name, godot_dll, probe_out, lib_dir)
    out, err = jit_probe.run_probe(dotnet, d)
    summary, fails = jit_probe.parse(out)
    if not summary:
        raise BuildError("the probe produced no summary:\n" + out + err)
    return dict(fails)


def new_failures(patched: dict[str, list[str]], original: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(exception, method) pairs the patched dll fails on and the original does not."""
    base = {(k, w) for k, ws in original.items() for w in ws}
    return sorted((k, w) for k, ws in patched.items() for w in ws if (k, w) not in base)


def godot_for(args_godot: str | None, tmp: str) -> str:
    """The GodotSharp.dll both runs are linked against: explicit, else the game's real one, else the stubs."""
    real = find_real_godot(args_godot)
    if real:
        return real
    stubs, build_tmp = build_stubs()
    dst = os.path.join(tmp, "GodotSharp.stubs.dll")
    shutil.move(stubs, dst)
    shutil.rmtree(build_tmp, ignore_errors=True)
    return dst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--game-lib", default=os.path.join(REPO, "lib"), help="directory with sts2.dll(.original) and its dependencies")
    ap.add_argument("--patched", help="patched sts2.dll (default: <game-lib>/sts2.dll)")
    ap.add_argument("--original", help="unpatched sts2.dll (default: <game-lib>/sts2.dll.original)")
    ap.add_argument("--repatch", action="store_true", help="patch a COPY of the original with setup.sh's patcher and check that")
    ap.add_argument("--godot", help="GodotSharp.dll to link against (default: the game's real one, else the stubs)")
    args = ap.parse_args(argv)

    dotnet = find_dotnet()
    if not dotnet:
        print("error: no dotnet found (set $DOTNET)", file=sys.stderr)
        return 2
    original = args.original or os.path.join(args.game_lib, "sts2.dll.original")
    patched = args.patched or os.path.join(args.game_lib, "sts2.dll")
    if not os.path.isfile(original):
        print(f"error: {original} not found (setup.sh creates it)", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix="check_il_patch_")
    try:
        godot = godot_for(args.godot, tmp)
        probe_out = jit_probe.build_probe(dotnet, tmp)
        if args.repatch:
            csproj, program = extract_patcher()
            lib_dir, log = patch_copy(dotnet, build_patcher(dotnet, tmp, csproj, program), tmp, "repatched",
                                      args.game_lib, original)
            print("re-patched a copy of", original)
            print("\n".join(l for l in log.splitlines() if "Widened" in l or l.startswith("Applied")))
        else:
            if not os.path.isfile(patched):
                print(f"error: {patched} not found", file=sys.stderr)
                return 2
            lib_dir = lib_copy(tmp, "patched", args.game_lib, patched)
        base = jit_failures(dotnet, tmp, probe_out, "original", lib_copy(tmp, "original", args.game_lib, original), godot)
        patched_fails = jit_failures(dotnet, tmp, probe_out, "patched", lib_dir, godot)
        print("original sts2.dll cannot compile %d method(s) (ignored: the same with or without the patches)"
              % sum(len(v) for v in base.values()))
        print("patched  sts2.dll cannot compile %d method(s)" % sum(len(v) for v in patched_fails.values()))
        bad = new_failures(patched_fails, base)
        for key, where in bad:
            print(f"NEW FAILURE  {where}\n             {key}")
        print("RESULT:", f"the patches broke {len(bad)} method(s)" if bad else "the patches broke no method")
        return 1 if bad else 0
    except BuildError as e:
        sys.stderr.write(str(e) + "\nerror: could not run (see above)\n")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
