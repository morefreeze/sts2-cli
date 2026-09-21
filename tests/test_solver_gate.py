"""Which characters route combat through plan_combat_turn.

The gate used to be a hard-coded `character == "Ironclad"` (Phase 1 scoped
itself to one character). Phase 2 makes it configurable so each character can
be smoke-tested without editing code, and so lifting the gate is a one-line
default change.
"""
import sys
import os

import pytest

# Same sys.path pattern tests/test_plan_combat_turn_resolution.py uses.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run


def test_default_is_ironclad_only():
    assert play_full_run.solver_characters({}) == {"Ironclad"}


def test_env_var_overrides_default():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "Defect"}) == {"Defect"}


def test_env_var_accepts_comma_separated_list_and_strips_space():
    assert play_full_run.solver_characters(
        {"STS2_SOLVER_CHARS": "Ironclad, Silent ,Defect"}
    ) == {"Ironclad", "Silent", "Defect"}


def test_all_keyword_enables_every_valid_character():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "all"}) == set(
        play_full_run.VALID_CHARACTERS
    )


def test_none_keyword_disables_the_solver_entirely():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "none"}) == set()


def test_empty_value_falls_back_to_the_default():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "  "}) == {"Ironclad"}


def test_unknown_character_raises_rather_than_silently_disabling():
    # A typo must not look like "solver quietly off for this character" --
    # that would make a whole regression run measure the wrong thing.
    with pytest.raises(ValueError, match="Ironcladd"):
        play_full_run.solver_characters({"STS2_SOLVER_CHARS": "Ironcladd"})


# --- summarize() solver-engagement rendering -------------------------------
#
# These guard against the exact failure this task exists to prevent: a 3-game
# Silent smoke test where all 502 plan_combat_turn calls errored with
# PredictionUnsupportedException, the solver produced zero plans, and
# summarize() still printed "Completed: 3/3" with nothing to flag that the
# run had secretly measured the fallback heuristic the whole time.

def _result(seed, solver_plans, solver_errors, victory=True, floor=20):
    return {
        "victory": victory,
        "seed": seed,
        "steps": 100,
        "act": 3,
        "floor": floor,
        "hp": 50,
        "max_hp": 70,
        "solver_plans": solver_plans,
        "solver_errors": solver_errors,
    }


def test_summarize_renders_no_banner_when_solver_engaged_every_run():
    results = [_result("s1", 40, 0), _result("s2", 35, 2)]
    out = play_full_run.summarize(results, 2, character="Silent", solver_chars={"Silent"})
    assert "SOLVER NEVER ENGAGED" not in out


def test_summarize_renders_banner_for_solver_enabled_character_with_zero_plans():
    results = [_result("s1", 0, 251, victory=False), _result("s2", 0, 251, victory=False)]
    out = play_full_run.summarize(results, 2, character="Silent", solver_chars={"Silent"})
    assert "!! SOLVER NEVER ENGAGED for Silent" in out


def test_summarize_no_banner_when_character_not_solver_enabled():
    # Zero engagement is expected and correct for the A/B's control arm --
    # a character deliberately excluded from solver_characters() should
    # never trip the banner just because it never called plan_combat_turn.
    results = [_result("s1", 0, 0, victory=False), _result("s2", 0, 0, victory=False)]
    out = play_full_run.summarize(results, 2, character="Silent", solver_chars={"Ironclad"})
    assert "SOLVER NEVER ENGAGED" not in out


def test_summarize_shows_per_run_engagement_numbers():
    results = [_result("s1", 178, 0)]
    out = play_full_run.summarize(results, 1, character="Ironclad", solver_chars={"Ironclad"})
    assert "solver=178/178" in out
