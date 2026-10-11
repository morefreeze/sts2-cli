"""BUG-052: src/GodotStubs must cover every Godot member lib/sts2.dll references.

A member the stubs lack is not a build error -- it is a MissingMethodException when the CALLING
method is JIT-compiled, mid-game (CombatManager / RollingBoulderPower -> GodotObject.Connect killed
the combat turn loop and the engine force-ended the run). tools/audit_godot_stub_refs.py finds those
holes statically; tools/jit_probe.py asks the CLR itself.

None of these tests start the engine or touch src/**/bin or src/**/obj: the stubs are compiled from
a COPY of the sources into a temp directory.
"""
import os
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import audit_godot_stub_refs as audit  # noqa: E402
import clr_metadata as clr  # noqa: E402
from godot_stub_emitter import Oracle  # noqa: E402

STS2 = os.path.join(REPO, "lib", "sts2.dll")
REAL_GODOT = audit.find_real_godot()

needs_game = pytest.mark.skipif(not os.path.isfile(STS2), reason="lib/sts2.dll not present (run setup.sh)")
needs_dotnet = pytest.mark.skipif(audit.find_dotnet() is None, reason="no dotnet SDK")


# --- the metadata reader's pure helpers ---------------------------------------------------------

def test_split_sig_separates_return_type_from_parameters():
    assert clr.split_sig("instance void(Godot.StringName,Godot.Callable,uint32)") == (
        "instance ", "void", "Godot.StringName,Godot.Callable,uint32", "")
    # generic return types contain no '(' but do contain commas and angle brackets
    assert clr.split_sig("System.Collections.Generic.Dictionary`2<Godot.Variant,Godot.Variant>(string)<1>") == (
        "", "System.Collections.Generic.Dictionary`2<Godot.Variant,Godot.Variant>", "string", "<1>")


def test_params_key_ignores_the_return_type():
    # what C# cannot overload on: the stub's `void Call(params Variant[])` blocks `Variant Call(...)`
    assert clr.params_key("instance void(Godot.Variant[])") == clr.params_key("instance Godot.Variant(Godot.Variant[])")
    assert clr.params_key("void(int32)") != clr.params_key("void(float32)")


def test_subst_vars_replaces_type_parameters_but_not_method_parameters():
    assert clr.subst_vars("instance void(!0)", ["Godot.Variant"]) == "instance void(Godot.Variant)"
    assert clr.subst_vars("!!0(!0,!1)<1>", ["int32", "string"]) == "!!0(int32,string)<1>"


def test_split_generic_handles_nested_arguments():
    assert clr.split_generic("A.B`2<x,C<y,z>>") == ("A.B`2", ["x", "C<y,z>"])
    assert clr.split_generic("A.B") == ("A.B", [])


# --- the game DLL --------------------------------------------------------------------------------

@needs_game
def test_reader_sees_the_godot_assembly_reference_and_a_realistic_amount_of_metadata():
    md = clr.Metadata(STS2)
    assert "GodotSharp" in {md.string(r[6]) for r in md.table(0x23)}
    assert len(md.table(0x02)) > 5000 and len(md.table(0x0A)) > 10000


@pytest.fixture(scope="module")
def stubs_dll():
    if audit.find_dotnet() is None:
        pytest.skip("no dotnet SDK")
    dll, tmp = audit.build_stubs()
    yield dll
    shutil.rmtree(tmp, ignore_errors=True)


def _describe(findings):
    return "\n".join(f"{f.category}: {f.type_name}::{f.member} {f.sig} {f.note}" for f in findings[:40])


@needs_game
@needs_dotnet
def test_stubs_define_every_godot_type_and_member_the_game_references(stubs_dll):
    findings, stats = audit.Auditor(stubs_dll).audit(STS2)
    assert stats["godot_memberrefs"] > 1000      # the audit really looked at something
    assert not findings, (
        f"{len(findings)} Godot references of lib/sts2.dll have no matching stub (each is a "
        "MissingMethodException waiting for the caller to be JIT-compiled). Run\n"
        "  .venv/bin/python tools/audit_godot_stub_refs.py\n"
        "to list them, and --emit-stubs FILE to generate no-op stubs.\n" + _describe(findings))


@needs_game
@needs_dotnet
@pytest.mark.skipif(REAL_GODOT is None, reason="the real GodotSharp.dll (Steam game dir / $STS2_GAME_DIR) is needed")
def test_stub_hierarchy_and_enum_numbering_match_real_godot(stubs_dll):
    findings, _ = audit.Auditor(stubs_dll).audit(STS2, Oracle(REAL_GODOT))
    assert not findings, _describe(findings)


@needs_game
@needs_dotnet
def test_removing_GodotObject_Connect_is_caught():
    """The BUG-052 regression itself: without Connect(StringName, Callable, uint) the audit must fail."""
    def drop_connect(work):
        path = os.path.join(work, "Core.cs")
        text = open(path).read()
        line = "public Error Connect(StringName signal, Callable callable, uint flags = 0) => Error.Ok;"
        assert line in text, "GodotObject.Connect moved or changed -- update this test"
        open(path, "w").write(text.replace(line, ""))

    dll, tmp = audit.build_stubs(drop_connect)
    try:
        findings, _ = audit.Auditor(dll).audit(STS2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    hits = [f for f in findings if f.type_name == "Godot.GodotObject" and f.member == "Connect"]
    assert hits and "Godot.StringName,Godot.Callable,uint32" in hits[0].sig


@needs_game
@pytest.mark.skipif(REAL_GODOT is None, reason="the real GodotSharp.dll (Steam game dir / $STS2_GAME_DIR) is needed")
def test_the_audit_itself_is_sound_against_the_real_assembly():
    """Audited against the REAL GodotSharp.dll the game must have zero missing references -- if not,
    the metadata reader or the member-resolution logic is wrong, not the stubs."""
    findings, stats = audit.Auditor(REAL_GODOT).audit(STS2)
    assert stats["godot_memberrefs"] > 1000
    assert not findings, _describe(findings)


@needs_game
@needs_dotnet
@pytest.mark.skipif(REAL_GODOT is None, reason="the real GodotSharp.dll (Steam game dir / $STS2_GAME_DIR) is needed")
def test_jit_probe_finds_no_method_the_stubs_cannot_compile_that_real_godot_can(capsys):
    """The CLR's own verdict (RuntimeHelpers.PrepareMethod on every method of sts2.dll; no game code runs)."""
    sys.path.insert(0, os.path.join(REPO, "tools"))
    import jit_probe
    rc = jit_probe.main(["--real-godot", REAL_GODOT])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "compiled=" in out
