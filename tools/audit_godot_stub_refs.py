#!/usr/bin/env python3
"""Audit that src/GodotStubs (our hand-written GodotSharp.dll) covers everything the game uses.

Why this exists (BUG-052): ``lib/sts2.dll`` is compiled against the real GodotSharp.dll; at run
time our stub assembly (``AssemblyName`` = GodotSharp) is loaded in its place.  A member the game
calls but the stub lacks is not a build error -- it is a ``MissingMethodException`` thrown when the
CALLING method is JIT-compiled, i.e. mid-game, and when that method sits on the combat turn loop
the loop dies and the engine force-ends the run.  This tool finds those holes up front.

What it does (no dependencies beyond Python and a .NET SDK; the ECMA-335 reader is in
tools/clr_metadata.py):

  1. Reads every TypeRef / MemberRef of the game DLL(s) whose resolution scope is the GodotSharp
     assembly (generic instantiations through TypeSpec and nested types included).
  2. Compiles ``src/GodotStubs`` into a private temp directory -- a COPY of the sources, so nothing
     under ``src/**/bin`` or ``src/**/obj`` is touched and it is safe next to a running
     ``dotnet run --no-build`` experiment -- or takes ``--stubs-dll``.
  3. For each reference checks that the stub assembly has the type and a member with the same
     name, kind (field/method), parameter types and return type, looking through the stub's base
     classes (and the .NET core library for members the stub inherits from System.Object & co.).
     It also checks class-vs-valuetype and base-class/interface agreement, which the runtime
     enforces with a TypeLoadException.
  4. With the real GodotSharp.dll available (``--real-godot``, default: the Steam game directory)
     it also checks that every stub type sits below the same Godot ancestors the game relies on
     (a cast to ``AnimationMixer`` must succeed on an ``AnimationPlayer``), and ``--emit-stubs``
     renders C# no-op stubs for all holes (tools/godot_stub_emitter.py).

Exit status: 0 = zero missing, 1 = something missing, 2 = tool/usage error.

After a game update (new ``lib/sts2.dll``) or any edit to src/GodotStubs:

    .venv/bin/python tools/audit_godot_stub_refs.py                 # must say: TOTAL missing: 0
    .venv/bin/python tools/audit_godot_stub_refs.py --emit-stubs src/GodotStubs/AuditStubs2.cs
        # fills the new holes; review the `// MANUAL` lines (collisions with hand-written members)
    .venv/bin/python tools/audit_godot_stub_refs.py --check-oracle  # self-test: real GodotSharp = 0 missing

Limits: generic constraints are not compared; members resolved through System.* bases are checked
against the .NET core library only; method bodies are not scanned, so "referenced" means "present
in the game's metadata" (which is what makes the JIT fail), not "executed in headless play".
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clr_metadata import (Metadata, MetadataError, SigReader, generic_def,  # noqa: E402
                          index_types, split_generic, subst_vars)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GODOT_ASSEMBLY = "godotsharp"
STEAM_DATA = os.path.expanduser(
    "~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/"
    "Contents/Resources/data_sts2_macos_arm64")


class Finding:
    def __init__(self, category, type_name, member, sig, note=""):
        self.category, self.type_name, self.member, self.sig, self.note = category, type_name, member, sig, note

    def key(self):
        return (self.category, self.type_name, self.member, self.sig)

    def as_dict(self):
        return {"category": self.category, "type": self.type_name, "member": self.member,
                "signature": self.sig, "note": self.note}


class Auditor:
    def __init__(self, stub_dll, corelib=None):
        self.stub_md = Metadata(stub_dll)
        self.stubs = index_types(self.stub_md)
        self._corelib_path = corelib
        self._corelib = None
        asm = self.stub_md.table(0x20)
        self.stub_version = tuple(asm[0][1:5]) if asm else None

    # -- core library fallback (members the stub inherits from System.Object etc.) -------------
    def corelib(self):
        if self._corelib is None:
            path = self._corelib_path or find_corelib()
            self._corelib = index_types(Metadata(path)) if path else {}
        return self._corelib

    def stub_oracle(self, like):
        if getattr(self, "_stub_oracle", None) is None:
            self._stub_oracle = type(like)(self.stub_md.path)
        return self._stub_oracle

    def stub_chain(self, name):
        out, n = [], generic_def(name)
        while n and n in self.stubs and n not in out:
            out.append(n)
            b = self.stubs[n].base
            n = generic_def(b) if b else None
        return out

    def find_in_chain(self, type_name, member, sig, is_field, ctor):
        """-> (True|False|None, near-miss descriptions); None = chain left known territory.

        Walks base classes like the CLR does for a MemberRef, substituting a generic base's
        type arguments (``class Array : List<Variant>`` makes ``Array::Add(Variant)`` resolve to
        ``List<T>::Add(!0)``)."""
        near = []
        name, args = split_generic(type_name)
        args = []  # the referenced type itself is the generic definition: its !n stay as they are
        for _ in range(64):
            if not name:
                break
            ti = self.stubs.get(name) or self.corelib().get(name)
            if ti is None:
                return None, near
            table = ti.fields if is_field else ti.methods
            if member in table:
                cands = {subst_vars(c, args) for c in table[member]}
                if sig in cands:
                    return True, near
                near.extend(f"{ti.name}::{member} {c}" for c in sorted(cands))
            if ctor:  # constructors are not inherited
                return False, near
            if not ti.base:
                break
            name, args = split_generic(subst_vars(ti.base, args))
        return False, near

    def audit(self, game_dll, oracle=None):
        md = Metadata(game_dll)
        findings = {}
        stats = defaultdict(int)

        def add(f):
            findings.setdefault(f.key(), f)

        game_dir = os.path.dirname(os.path.abspath(game_dll))
        for i, r in enumerate(md.table(0x23), start=1):
            name = md.string(r[6])
            if "godot" not in name.lower():
                continue
            stats["godot_assembly_refs"] += 1
            if name.lower() != GODOT_ASSEMBLY:
                if not os.path.isfile(os.path.join(game_dir, name + ".dll")):
                    add(Finding("assembly", name, "", "", "second Godot-named assembly the game references "
                                "and the game directory does not contain; only GodotSharp is modelled"))
            elif self.stub_version and self.stub_version < tuple(r[:4]):
                add(Finding("assembly-version", name, "", "",
                            f"game wants {'.'.join(map(str, r[:4]))}, stub is "
                            f"{'.'.join(map(str, self.stub_version))} (a lower version fails to load)"))

        is_godot = {}

        def godot_typeref(i):
            v = is_godot.get(i)
            if v is None:
                a = md.typeref_assembly(i)
                v = bool(a) and a.lower() == GODOT_ASSEMBLY
                is_godot[i] = v
            return v

        # 1. every Godot TypeRef must exist in the stubs
        godot_names = set()
        for i in range(1, len(md.table(0x01)) + 1):
            if godot_typeref(i):
                n = md.typeref_name(i)
                godot_names.add(n)
                if n not in self.stubs:
                    add(Finding("type", n, "", "", "no such type in the stubs"))
        stats["godot_typerefs"] = len(godot_names)

        # 2. every Godot MemberRef must resolve
        for parent, name_i, sig_i in md.table(0x0A):
            tbl, idx = md.decode_coded("MemberRefParent", parent)
            if tbl == 0x01:
                if not godot_typeref(idx):
                    continue
                tname = md.typeref_name(idx)
            elif tbl == 0x1B:
                d = md.typespec_def(idx)
                if d is None or d not in godot_names:
                    continue
                tname = d
            else:
                continue
            member = md.string(name_i)
            kind, sig = SigReader(md, md.blob(sig_i)).signature()
            stats["godot_memberrefs"] += 1
            if tname not in self.stubs:
                add(Finding("member", tname, member, ("field " if kind == "field" else "") + sig,
                            "declaring type missing"))
                continue
            found, near = self.find_in_chain(tname, member, sig, kind == "field", member in (".ctor", ".cctor"))
            if found is None:
                stats["unverified_inherited"] += 1
            elif not found:
                add(Finding("member", tname, member, ("field " if kind == "field" else "") + sig,
                            ("present with other signature: " + "; ".join(near)) if near else ""))

        # 3. class/valuetype agreement in every signature blob that mentions a Godot type
        def scan_blob(blob, where):
            usage = []
            try:
                SigReader(md, blob, usage).signature()
            except (MetadataError, IndexError):
                return
            for marker, tname in usage:
                base = generic_def(tname)
                ti = self.stubs.get(base) if base in godot_names else None
                if ti is not None and marker != ("valuetype" if ti.is_value else "class"):
                    add(Finding("kind", base, "", "", f"game uses it as a {marker} (in a {where}); the stub "
                                f"declares a {'struct/enum' if ti.is_value else 'class/interface'} -> "
                                "TypeLoadException / bad IL at run time"))

        for t, col, label in ((0x0A, 2, "member ref"), (0x1B, 0, "type spec"), (0x2B, 1, "method spec"),
                              (0x11, 0, "local sig"), (0x04, 2, "field"), (0x06, 4, "method def"),
                              (0x17, 2, "property")):
            seen = set()
            for r in md.table(t):
                if r[col] not in seen:
                    seen.add(r[col])
                    scan_blob(md.blob(r[col]), label)

        # 4. base classes / interfaces of game types that live in the Godot assembly
        for i, r in enumerate(md.table(0x02), start=1):
            if not r[3]:
                continue
            base = generic_def(md.type_from_coded(r[3]))
            b = self.stubs.get(base) if base in godot_names else None
            if b is not None and (b.is_interface or b.is_value or b.is_sealed):
                add(Finding("base", base, md.typedef_name(i), "", "game type derives from it, stub declares it "
                            + ("an interface" if b.is_interface else "a value type" if b.is_value else "sealed")))
        for cls, iface in md.table(0x09):
            iname = generic_def(md.type_from_coded(iface))
            if iname in godot_names and iname in self.stubs and not self.stubs[iname].is_interface:
                add(Finding("base", iname, md.typedef_name(cls), "",
                            "game type implements it as an interface, stub declares a class/struct"))

        # 5. (needs the real GodotSharp) stub types must sit below the Godot ancestors the game uses
        if oracle is not None:
            for name in sorted(godot_names):
                if name not in self.stubs or name not in oracle.types:
                    continue
                if name.rsplit("/", 1)[-1] in ("SignalName", "MethodName", "PropertyName"):
                    continue  # constant holders, never cast; the member check covers constant lookup
                have = set(self.stub_chain(name))
                for anc in oracle.chain(name)[1:]:
                    if anc in godot_names and anc not in have:
                        add(Finding("hierarchy", name, "", "",
                                    f"real Godot: {' > '.join(oracle.chain(name))}; stub: "
                                    f"{' > '.join(self.stub_chain(name))} (lacks {anc}); give {name} the right "
                                    "base class, or a cast to it fails at run time"))
                        break

        # 6. (needs the real GodotSharp) enum members present in both must carry the same number: the game's IL
        #    holds the REAL numeric constants, so a stub enum numbered differently is silently wrong.
        if oracle is not None:
            stub_oracle = self.stub_oracle(oracle)
            for name in sorted(godot_names):
                real_ti, stub_ti = oracle.types.get(name), self.stubs.get(name)
                if not (real_ti and stub_ti and real_ti.is_enum and stub_ti.is_enum):
                    continue
                real_vals, stub_vals = dict(oracle.enum_members(name)), dict(stub_oracle.enum_members(name))
                bad = {k: (stub_vals[k], real_vals[k]) for k in stub_vals if k in real_vals and stub_vals[k] != real_vals[k]}
                su, ru = stub_oracle.enum_underlying(name), oracle.enum_underlying(name)
                if su != ru:  # the game pushes the real width (Godot 4 enums are mostly `long`) onto the stack
                    add(Finding("enum-value", name, "", "", f"underlying type is {su}, real Godot's is {ru}; "
                                f"declare it `enum X : {ru}`"))
                if bad:
                    sample = ", ".join(f"{k}: stub {a} != real {b}" for k, (a, b) in list(bad.items())[:4])
                    add(Finding("enum-value", name, "", "", f"{len(bad)} member(s) numbered differently ({sample}"
                                f"{', ...' if len(bad) > 4 else ''}); renumber the stub enum to real Godot's"))

        return sorted(findings.values(), key=lambda f: (f.category, f.type_name, f.member, f.sig)), dict(stats)


def find_dotnet():
    for c in (os.environ.get("DOTNET"), os.path.expanduser("~/.dotnet-arm64/dotnet"), shutil.which("dotnet")):
        if c and os.path.isfile(c):
            return c
    return None


def find_corelib():
    dn = find_dotnet()
    roots = [os.path.expanduser("~/.dotnet-arm64"), os.path.dirname(os.path.realpath(dn)) if dn else "",
             "/usr/local/share/dotnet", "/usr/share/dotnet", "/usr/lib/dotnet"]
    for root in roots:
        hits = sorted(glob.glob(os.path.join(root, "shared", "Microsoft.NETCore.App", "9.*",
                                             "System.Private.CoreLib.dll")))
        if hits:
            return hits[-1]
    return None


def find_real_godot(explicit=None):
    cands = [explicit, os.path.join(os.environ.get("STS2_GAME_DIR", ""), "GodotSharp.dll"),
             os.path.join(STEAM_DATA, "GodotSharp.dll")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def build_stubs(mutate=None):
    """Compile a COPY of src/GodotStubs into a fresh temp dir; returns (dll, tmpdir).

    `mutate(work_dir)` may edit the copied sources first (the tests use it to delete a member and
    check the audit notices)."""
    dotnet = find_dotnet()
    if not dotnet:
        raise SystemExit("error: no dotnet found (set $DOTNET or pass --stubs-dll)")
    src = os.path.join(REPO, "src", "GodotStubs")
    tmp = tempfile.mkdtemp(prefix="godot_stub_audit_")
    work = os.path.join(tmp, "src")
    os.makedirs(work)
    for f in os.listdir(src):  # sources + csproj only; never bin/ or obj/
        p = os.path.join(src, f)
        if os.path.isfile(p) and (f.endswith(".cs") or f.endswith(".csproj")):
            shutil.copy(p, work)
    if mutate:
        mutate(work)
    out = os.path.join(tmp, "out")
    cmd = [dotnet, "build", os.path.join(work, "GodotStubs.csproj"), "-c", "Release", "-o", out,
           "--nologo", "-v", "q", "--disable-build-servers", "-p:UseSharedCompilation=false"]
    r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise BuildError(r.stdout + r.stderr)
    return os.path.join(out, "GodotSharp.dll"), tmp


class BuildError(Exception):
    pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--game-dll", action="append", help="game assembly to audit (repeatable); default lib/sts2.dll")
    ap.add_argument("--stubs-dll", help="already-built stub GodotSharp.dll (default: compile src/GodotStubs in a temp dir)")
    ap.add_argument("--real-godot", help="the real GodotSharp.dll (default: $STS2_GAME_DIR or the Steam game dir)")
    ap.add_argument("--emit-stubs", metavar="FILE", help="write C# no-op stubs for the holes to FILE ('-' = stdout)")
    ap.add_argument("--check-oracle", action="store_true",
                    help="self-test: audit against the REAL GodotSharp.dll; must report zero missing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--keep-build", action="store_true", help="keep the temp build dir")
    args = ap.parse_args(argv)

    game_dlls = args.game_dll or [os.path.join(REPO, "lib", "sts2.dll")]
    for g in game_dlls:
        if not os.path.isfile(g):
            print(f"error: {g} not found", file=sys.stderr)
            return 2
    real_path = find_real_godot(args.real_godot)
    if (args.emit_stubs or args.check_oracle) and not real_path:
        print("error: the real GodotSharp.dll is needed (--real-godot, or set STS2_GAME_DIR)", file=sys.stderr)
        return 2

    tmp = None
    stub_dll = real_path if args.check_oracle else args.stubs_dll
    if not stub_dll:
        try:
            stub_dll, tmp = build_stubs()
        except BuildError as e:
            sys.stderr.write(str(e) + "\nerror: compiling src/GodotStubs failed (see above)\n")
            return 2
    try:
        oracle = None
        if real_path:
            from godot_stub_emitter import Oracle
            oracle = Oracle(real_path)
        auditor = Auditor(stub_dll)
        report, total, all_findings = {}, 0, []
        for g in game_dlls:
            findings, stats = auditor.audit(g, oracle)
            all_findings += findings
            report[g] = {"stats": stats, "findings": [f.as_dict() for f in findings]}
            total += len(findings)
            if args.json:
                continue
            print(f"== {g}: {stats.get('godot_typerefs', 0)} Godot type refs, "
                  f"{stats.get('godot_memberrefs', 0)} Godot member refs "
                  f"({stats.get('unverified_inherited', 0)} resolved outside the stubs/core library, unverified)")
            if oracle is None:
                print("   (no real GodotSharp.dll found: the base-class hierarchy check is skipped)")
            if not findings:
                print("   OK: every Godot type/member the game references exists in the stubs")
            for f in findings:
                where = f"{f.type_name}::{f.member}" if f.member else f.type_name
                print(f"   MISSING [{f.category}] {where}  {f.sig}".rstrip() + (f"   -- {f.note}" if f.note else ""))
        if args.json:
            print(json.dumps({"stubs_dll": stub_dll, "report": report, "missing_total": total}, indent=2))
        else:
            counts = defaultdict(int)
            for f in all_findings:
                counts[f.category] += 1
            print(f"TOTAL missing: {total}" + (f"  ({', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))})" if total else ""))
        if args.emit_stubs:
            from godot_stub_emitter import Emitter
            em = Emitter(auditor.stubs, oracle)
            em.add_holes(all_findings)
            text = em.render()
            if args.emit_stubs == "-":
                sys.stdout.write(text)
            else:
                with open(args.emit_stubs, "w") as fh:
                    fh.write(text)
            print(f"emitted {em.emitted_members} members, {len(em.gen)} new types -> {args.emit_stubs}; "
                  f"{len(em.manual)} MANUAL, {len(em.unresolved)} not in the real GodotSharp either", file=sys.stderr)
            for t, m, s, why in em.manual:
                print(f"  MANUAL {t}::{m} {s} -- {why}", file=sys.stderr)
            for f in em.unresolved:
                print(f"  UNRESOLVED {f.type_name}::{f.member} {f.sig}", file=sys.stderr)
        return 1 if total else 0
    finally:
        if tmp and not args.keep_build:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
