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


def test_default_is_every_character():
    # Phase 2 lifted the Ironclad-only gate after smoke-testing the other four
    # (docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md Task 2/3).
    assert play_full_run.solver_characters({}) == set(play_full_run.VALID_CHARACTERS)


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
    # Blank must mean "default", not "none" -- an empty env var is how a shell
    # exports an unset variable, and silently disabling the solver there would
    # make a whole batch measure the fallback heuristic.
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "  "}) == set(
        play_full_run.VALID_CHARACTERS
    )


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


def test_summarize_banner_distinguishes_zero_calls_from_all_calls_failing():
    # A batch that never reached a combat_play decision made ZERO calls; saying
    # "every call failed" there would be a false diagnostic, and a banner that
    # cries wolf inaccurately stops being read.
    never_called = play_full_run.summarize(
        [_result("s1", 0, 0, victory=False)], 1, character="Silent", solver_chars={"Silent"})
    assert "no plan_combat_turn call was ever made" in never_called
    assert "every plan_combat_turn call failed" not in never_called

    all_failed = play_full_run.summarize(
        [_result("s1", 0, 251, victory=False)], 1, character="Silent", solver_chars={"Silent"})
    assert "every plan_combat_turn call failed (251 errors)" in all_failed


def test_result_row_maps_a_win_to_status_win():
    row = play_full_run.result_to_eval_row(
        {"victory": True, "seed": "run_1", "floor": 51, "act": 3, "steps": 200}, "Ironclad")
    assert row["seed"] == "run_1"
    assert row["status"] == "win"
    assert row["floor"] == 51
    assert row["character"] == "Ironclad"


def test_result_row_maps_an_ordinary_loss_to_status_dead():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "run_2", "floor": 12, "act": 1, "steps": 90}, "Defect")
    assert row["status"] == "dead"


def test_result_row_maps_timeout_and_error_to_technical_statuses():
    # paired_eval only pairs {"win", "dead"}; everything else must land on a
    # name its _VALID_STATUSES check will drop, never on "dead" -- a failed run
    # counted as an ordinary death would silently bias the floor average.
    assert play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "timeout": True}, "Silent")["status"] == "timeout"
    assert play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "error": "engine_error: x"}, "Silent")["status"] == "crash"


def test_result_row_keeps_floor_none_rather_than_defaulting_to_zero():
    # A run that never reached a floor must not report floor 0 -- that would
    # read as "died on floor 0" in an average instead of "no data".
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "timeout": True}, "Regent")
    assert row["floor"] is None


def test_result_row_carries_solver_engagement_counters():
    # The paired A/B needs to be able to prove the ON arm actually engaged
    # the solver and the OFF arm did not, without re-deriving it from
    # STS2_SOLVER_CHARS after the fact.
    row = play_full_run.result_to_eval_row(
        {"victory": True, "seed": "s", "floor": 30, "solver_plans": 12, "solver_errors": 1},
        "Ironclad")
    assert row["solver_plans"] == 12
    assert row["solver_errors"] == 1
