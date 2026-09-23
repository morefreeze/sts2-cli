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
        {"victory": True, "seed": "run_1", "floor": 17, "act": 3, "steps": 200}, "Ironclad")
    assert row["seed"] == "run_1"
    assert row["status"] == "win"
    assert row["floor"] == 51   # act 3 floor 17 -> global 51
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


def test_result_row_floor_is_global_not_act_local():
    # The engine's game_over "floor" is ACT-LOCAL (resets to 1 each act), but
    # agent/paired_eval.py's floor metric is the GLOBAL run floor -- the same
    # (act - 1) * 17 + floor that agent/eval_rl.py writes (eval_rl.py:109,876).
    # Emitting the act-local value made a death at act 2 floor 16 score 16,
    # below an act-1 death at floor 17: the Phase 2 A/B's first verdict read
    # Defect as -0.24 floors while 24 of its 40 solver runs had reached act 2+.
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "act": 2, "floor": 16, "steps": 150}, "Defect")
    assert row["floor"] == 33
    assert row["act_floor"] == 16


def test_result_row_global_floor_matches_eval_rl_convention():
    from agent.eval_rl import global_floor_from_state
    for act, floor in [(1, 1), (1, 17), (2, 1), (2, 16), (3, 9)]:
        row = play_full_run.result_to_eval_row(
            {"victory": False, "seed": "s", "act": act, "floor": floor}, "Ironclad")
        assert row["floor"] == global_floor_from_state(
            {"floor": floor, "context": {"act": act}})


def test_summarize_avg_floor_uses_global_floor():
    # avg_floor averaged act-local floors, so a run that died in act 2 pulled
    # the average DOWN relative to one that died deep in act 1.
    results = [
        {"victory": False, "seed": "a", "act": 1, "floor": 10},
        {"victory": False, "seed": "b", "act": 2, "floor": 4},
    ]
    out = play_full_run.summarize(results, 2, character="Defect", solver_chars={"Defect"})
    assert "avg_floor=15.5" in out   # (10 + 21) / 2, not (10 + 4) / 2


# --- solver time budget tiers (Phase 2b-1) ------------------------------------

def test_solver_budget_defaults_to_the_validated_120s():
    assert play_full_run.solver_budget_seconds({}) == 120


def test_blank_solver_budget_means_default():
    assert play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": "  "}) == 120


@pytest.mark.parametrize("tier", [30, 60, 120, 180, 300])
def test_solver_budget_accepts_each_tier(tier):
    assert play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": str(tier)}) == tier


@pytest.mark.parametrize("bad", ["45", "0", "-30", "abc", "30s", "120.0", "+30"])
def test_solver_budget_rejects_anything_off_the_tier_list(bad):
    # Raise before any game starts: a typo that silently ran the default would
    # label a 120 s run as some other tier in a multi-hour A/B.
    with pytest.raises(ValueError, match="STS2_SOLVER_BUDGET"):
        play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": bad})


def test_solver_call_watchdog_sits_well_above_every_budget():
    # The budget is soft (checked between node expansions); the worst overrun in
    # 1,409 measured solves was 0.4 s. The watchdog must never fire on a
    # legitimately slow search, only on a BUG-040 hang.
    for tier in play_full_run.SOLVER_BUDGET_TIERS_S:
        assert play_full_run.solver_call_timeout_s(tier) >= tier + 60


def test_result_row_records_the_budget_tier(monkeypatch):
    monkeypatch.setenv("STS2_SOLVER_BUDGET", "30")
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "act": 1, "floor": 5}, "Silent")
    assert row["solver_budget_s"] == 30


# --- play_run watchdog integration (Phase 2b-1) --------------------------------

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _fake_engine(monkeypatch, tmp_path, *extra):
    pidfile = tmp_path / "engine.pid"
    monkeypatch.setattr(play_full_run, "engine_argv",
                        lambda: [sys.executable, FAKE_ENGINE, "--pidfile", str(pidfile), *extra])
    monkeypatch.delenv("STS2_SOLVER_CHARS", raising=False)
    monkeypatch.delenv("STS2_SOLVER_BUDGET", raising=False)


def test_play_run_turns_a_solver_hang_into_a_hang_result(monkeypatch, tmp_path):
    _fake_engine(monkeypatch, tmp_path, "--hang-on", "plan_combat_turn")
    monkeypatch.setattr(play_full_run, "solver_call_timeout_s", lambda budget_s: 0.5)
    result = play_full_run.play_run("seed_x", "Ironclad", verbose=False, log=False)
    assert result["hang"] is True
    assert result["error"].startswith("engine_hang")
    # act/floor/hp come from the last decision seen before the hang -- the
    # error path must not report None/None like BUG-042's rows do.
    assert (result["act"], result["floor"], result["hp"]) == (1, 8, 28)
    assert play_full_run.result_to_eval_row(result, "Ironclad")["status"] == "stuck"


def test_play_run_fails_loudly_if_engine_and_harness_disagree_on_the_budget(monkeypatch, tmp_path):
    # Harness resolves 120 s (unset); the fake engine claims 30 s.
    _fake_engine(monkeypatch, tmp_path, "--plan-budget-ms", "30000")
    result = play_full_run.play_run("seed_x", "Ironclad", verbose=False, log=False)
    assert "solver budget" in result["error"]
    assert not result.get("hang")


def test_play_run_fails_loudly_if_the_engine_reports_no_budget_at_all(monkeypatch, tmp_path):
    # An engine binary built before the budget tiers existed reports no
    # `search` dict and silently runs EVERY tier at 120 s -- a tier A/B would
    # then compare 120 s with 120 s and report "no difference". `dotnet run
    # --no-build` runs whatever is in bin/, so a stale build is a real hazard.
    _fake_engine(monkeypatch, tmp_path, "--no-search")
    result = play_full_run.play_run("seed_x", "Ironclad", verbose=False, log=False)
    assert "stale build" in result["error"]


def test_summarize_labels_a_hang_as_hang_and_not_completed():
    out = play_full_run.summarize(
        [{"victory": False, "seed": "s", "act": 1, "floor": 8, "steps": 40,
          "error": "engine_hang: no reply within 180s", "hang": True,
          "solver_plans": 3, "solver_errors": 0}],
        1, character="Ironclad", solver_chars={"Ironclad"})
    assert "Run 1: HANG" in out
    assert "Completed: 0/1" in out
