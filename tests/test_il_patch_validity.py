"""BUG-054: setup.sh's Cecil patches must leave every sts2.dll method JIT-compilable.

Cecil does not widen short-form branches.  Patch 4 inserts 13 bytes between a compiler-emitted
`brtrue.s` / `brfalse.s` and its target; when the displacement then exceeds 127 the writer truncates it
to a signed byte and the branch lands on the wrong instruction, so the CLR throws InvalidProgramException
the first time the method is compiled -- mid-game, inside NRollingBoulderVfx.<PlayAnim>d__22 and
SandpitPower.<UpdateCreaturePositions>d__18 (the latter killed the combat turn loop).

These tests extract the C# patcher out of setup.sh, build it in a temp dir, patch a COPY of
lib/sts2.dll.original and JIT-compile every method of the result with tools/check_il_patch.py.
They never touch lib/ (read-only), src/**/bin or src/**/obj, and never start the engine, so they are safe
next to a running experiment.  Set STS2_LIB_DIR to read the game DLLs from a copy of lib/ instead.
"""
import os
import re
import shutil
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import check_il_patch as chk  # noqa: E402
import jit_probe  # noqa: E402
from audit_godot_stub_refs import find_dotnet  # noqa: E402

LIB = os.environ.get("STS2_LIB_DIR", os.path.join(REPO, "lib"))
ORIGINAL = os.path.join(LIB, "sts2.dll.original")

needs_original = pytest.mark.skipif(not os.path.isfile(ORIGINAL), reason="lib/sts2.dll.original not present (run setup.sh)")
needs_dotnet = pytest.mark.skipif(find_dotnet() is None, reason="no dotnet SDK")

WIDEN_CALL = "int w = WidenOutOfRangeShortBranches(m.Body);"


# --- the patcher source (no dotnet needed) -------------------------------------------------------

def test_setup_sh_embeds_the_patcher_sources():
    csproj, program = chk.extract_patcher()
    assert "Mono.Cecil" in csproj
    assert "Applied {patches} patches" in program
    assert "module.Write(" in program


def test_the_widening_pass_runs_over_every_method_before_the_module_is_written():
    # Patch 4's inserted `br`/`brfalse` are long-form, but the compiler's branches around them are not.
    # The pass must see every method (a future patch can insert code anywhere) and run before the write.
    _, program = chk.extract_patcher()
    assert WIDEN_CALL in program
    call_at = program.index(WIDEN_CALL)
    assert call_at < program.index("module.Write(")
    assert "module.GetTypes()" in program[call_at - 300:call_at]   # a loop over the whole module, not one patch's methods


def test_every_short_branch_opcode_has_a_long_form():
    _, program = chk.extract_patcher()
    body = program[program.index("static OpCode? LongBranchForm"):program.index("static int WidenOutOfRangeShortBranches")]
    # all 14 ECMA-335 short branch opcodes (br.s, brfalse.s, brtrue.s, beq.s, bge.s, bgt.s, ble.s, blt.s,
    # bne.un.s, bge.un.s, bgt.un.s, ble.un.s, blt.un.s, leave.s)
    assert set(re.findall(r"OpCodes\.([A-Za-z_]+_S)\b", body)) == {
        "Br_S", "Brfalse_S", "Brtrue_S", "Beq_S", "Bge_S", "Bgt_S", "Ble_S", "Blt_S",
        "Bne_Un_S", "Bge_Un_S", "Bgt_Un_S", "Ble_Un_S", "Blt_Un_S", "Leave_S"}


# --- the real thing: patch a copy, ask the CLR ---------------------------------------------------

@pytest.fixture(scope="module")
def scratch():
    dotnet = find_dotnet()
    tmp = tempfile.mkdtemp(prefix="il_patch_validity_")
    try:
        godot = chk.godot_for(None, tmp)
        probe_out = jit_probe.build_probe(dotnet, tmp)
        base = chk.jit_failures(dotnet, tmp, probe_out, "original", chk.lib_copy(tmp, "original", LIB, ORIGINAL), godot)
        yield dotnet, tmp, godot, probe_out, base
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _patched_failures(scratch, name, program):
    dotnet, tmp, godot, probe_out, base = scratch
    csproj, _ = chk.extract_patcher()
    patcher = chk.build_patcher(dotnet, tmp, csproj, program, name)
    lib_dir, _ = chk.patch_copy(dotnet, patcher, tmp, name, LIB, ORIGINAL)
    return chk.new_failures(chk.jit_failures(dotnet, tmp, probe_out, name, lib_dir, godot), base)


@needs_original
@needs_dotnet
def test_patched_dll_compiles_every_method_the_original_compiles(scratch):
    _, program = chk.extract_patcher()
    bad = _patched_failures(scratch, "as_shipped", program)
    assert bad == [], "setup.sh's patches broke methods that compile fine unpatched: %s" % bad


@needs_original
@needs_dotnet
def test_the_check_notices_unwidened_short_branches(scratch):
    """Self-test of the check: with the widening pass switched off the BUG-054 failures must come back."""
    _, program = chk.extract_patcher()
    assert WIDEN_CALL in program
    bad = _patched_failures(scratch, "no_widening", program.replace(WIDEN_CALL, "int w = 0;"))
    if not bad:
        pytest.skip("this game version has no short branch near the signed-byte limit under patch 4; "
                    "the hazard is not exercised, so there is nothing for the check to notice")
    assert all(k.startswith("InvalidProgramException") for k, _ in bad), bad
