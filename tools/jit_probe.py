#!/usr/bin/env python3
"""JIT-compile every method of lib/sts2.dll against the stub GodotSharp.dll (no game code runs).

The second line of defence next to tools/audit_godot_stub_refs.py: the audit reads metadata, this
asks the CLR itself (RuntimeHelpers.PrepareMethod, the exact step that throws MissingMethodException
mid-game).  The pass criterion is "the stubs fail on exactly the methods the REAL GodotSharp.dll
fails on" -- today that is 166 methods: 164 Delegate.BeginInvoke/EndInvoke (never supported on
.NET Core) and 2 InvalidProgramException that come from the setup.sh IL patch of sts2.dll
(NRollingBoulderVfx.<PlayAnim>d__22 and SandpitPower.<UpdateCreaturePositions>d__18; the unpatched
sts2.dll.original compiles them).

    .venv/bin/python tools/jit_probe.py                      # stubs vs the real GodotSharp.dll (found via $STS2_GAME_DIR / Steam)
    .venv/bin/python tools/jit_probe.py --no-real            # stubs only: fail on anything but Begin/EndInvoke
    .venv/bin/python tools/jit_probe.py --dump 'MegaCrit.Sts2.Core.Models.Powers.RollingBoulderPower+<AfterPlayerTurnStart>d__8' MoveNext

Everything is built and run under a temp directory at lowest priority; nothing under src/**/bin or
src/**/obj is touched, so it is safe next to a running experiment.  Exit status 1 = the stubs fail
somewhere the real assembly does not.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_godot_stub_refs import BuildError, REPO, build_stubs, find_dotnet, find_real_godot  # noqa: E402

KNOWN_BENIGN = ("PlatformNotSupportedException",)   # Delegate.BeginInvoke / EndInvoke on .NET Core


def build_probe(dotnet, tmp):
    src = os.path.join(REPO, "tools", "jit_probe")
    work = os.path.join(tmp, "probe_src")
    shutil.copytree(src, work, ignore=shutil.ignore_patterns("bin", "obj"))
    out = os.path.join(tmp, "probe_out")
    r = subprocess.run([dotnet, "build", os.path.join(work, "Probe.csproj"), "-c", "Release", "-o", out, "--nologo",
                        "-v", "q", "--disable-build-servers", "-p:UseSharedCompilation=false"],
                       cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(r.stdout + r.stderr)
    return out


def run_dir(tmp, name, godot_dll, probe_out, game_lib):
    d = os.path.join(tmp, name)
    os.makedirs(d)
    for f in glob.glob(os.path.join(game_lib, "*.dll")):
        shutil.copy(f, d)
    shutil.copy(godot_dll, os.path.join(d, "GodotSharp.dll"))
    for f in glob.glob(os.path.join(probe_out, "Probe.*")):
        if not f.endswith(".pdb"):
            shutil.copy(f, d)
    return d


def run_probe(dotnet, d, extra=()):
    r = subprocess.run(["nice", "-n", "19", dotnet, os.path.join(d, "Probe.dll"), *extra],
                       capture_output=True, text=True)
    return r.stdout, r.stderr


def parse(stdout):
    summary, fails = "", defaultdict(list)
    for line in stdout.splitlines():
        if line.startswith("SUMMARY"):
            summary = line
        elif line.startswith("FAIL\t"):
            _, key, where = line.split("\t", 2)
            fails[key].append(where)
    return summary, fails


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--stubs-dll", help="already-built stub GodotSharp.dll (default: compile src/GodotStubs)")
    ap.add_argument("--real-godot", help="real GodotSharp.dll to use as the baseline")
    ap.add_argument("--no-real", action="store_true", help="no baseline: fail on anything but Delegate Begin/EndInvoke")
    ap.add_argument("--game-lib", default=os.path.join(REPO, "lib"), help="directory holding sts2.dll and its dependencies")
    ap.add_argument("--dump", nargs=2, metavar=("TYPE", "METHOD"), help="list the Godot members a method's IL references")
    args = ap.parse_args(argv)

    dotnet = find_dotnet()
    if not dotnet:
        print("error: no dotnet found (set $DOTNET)", file=sys.stderr)
        return 2
    if not os.path.isfile(os.path.join(args.game_lib, "sts2.dll")):
        print(f"error: {args.game_lib}/sts2.dll not found", file=sys.stderr)
        return 2
    tmp = tempfile.mkdtemp(prefix="jit_probe_")
    try:
        stubs = args.stubs_dll
        if not stubs:
            stubs, build_tmp = build_stubs()
            shutil.move(stubs, os.path.join(tmp, "GodotSharp.stubs.dll"))
            shutil.rmtree(build_tmp, ignore_errors=True)
            stubs = os.path.join(tmp, "GodotSharp.stubs.dll")
        probe_out = build_probe(dotnet, tmp)
        d_stub = run_dir(tmp, "run_stubs", stubs, probe_out, args.game_lib)
        if args.dump:
            out, _ = run_probe(dotnet, d_stub, ["--dump", *args.dump])
            print(out)
            return 0
        out, err = run_probe(dotnet, d_stub)
        summary, fails = parse(out)
        if not summary:
            print(out + err)
            print("error: the probe produced no summary", file=sys.stderr)
            return 2
        print("stubs :", summary)
        real_fails = None
        real = None if args.no_real else find_real_godot(args.real_godot)
        if real:
            d_real = run_dir(tmp, "run_real", real, probe_out, args.game_lib)
            rsum, real_fails = parse(run_probe(dotnet, d_real)[0])
            print("real  :", rsum)
        elif not args.no_real:
            print("(no real GodotSharp.dll found; comparing against the known-benign set only)")
        bad = 0
        for key, wheres in sorted(fails.items(), key=lambda kv: -len(kv[1])):
            expected = set(real_fails.get(key, [])) if real_fails is not None else (
                set(wheres) if key.startswith(KNOWN_BENIGN) else set())
            extra = [w for w in wheres if w not in expected]
            tag = "ok (real Godot fails identically)" if not extra and real_fails is not None else \
                  "ok (benign)" if not extra else "NEW FAILURE"
            print(f"{len(wheres):5}  {key}  [{tag}]")
            for w in (extra or wheres)[:5]:
                print(f"         e.g. {w}")
            bad += len(extra)
        print("RESULT:", "stubs fail where the reference does not: %d method(s)" % bad if bad else "no failures beyond the reference")
        return 1 if bad else 0
    except BuildError as e:
        sys.stderr.write(str(e) + "\nerror: build failed (see above)\n")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
