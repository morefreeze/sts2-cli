"""Which characters route combat through plan_combat_turn.

The gate used to be a hard-coded `character == "Ironclad"` (Phase 1 scoped
itself to one character). Phase 2 makes it configurable so each character can
be smoke-tested without editing code, and so lifting the gate is a one-line
default change.
"""
import json
import sys
import os

import pytest

# Same sys.path pattern tests/test_plan_combat_turn_resolution.py uses.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine


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


# --- BUG-042: live CanPlay refusals of planned cards are re-planned ------------

REFUSAL = {"type": "error", "message": "Cannot play card Bodyguard: EnergyCostTooHigh"}
STILL_IN_HAND = {"type": "error", "message":
                 "Card could not be played (still in hand after action): Bodyguard [CARD.BODYGUARD]"}


def _combat(hand, energy=1, round_=1):
    return {"type": "decision", "decision": "combat_play", "round": round_,
            "energy": energy, "enemies": [{"index": 0, "hp": 30, "combat_id": 1}],
            "context": {"act": 2, "floor": 5, "room_type": "Monster"},
            "player": {"hp": 40, "max_hp": 80, "gold": 0},
            "hand": [{"index": i, "id": f"CARD.{cid}", "can_play": True, "cost": 1,
                      "target_type": "Self"} for i, cid in enumerate(hand)]}


def _plan(*actions):
    return {"type": "combat_plan", "actions": list(actions),
            "search": {"budget_ms": 120_000}}


def _play(card_id):
    return {"action": "play_card", "card_id": card_id, "card_occurrence": 0}


END = {"action": "end_turn"}
GAME_OVER = {"type": "decision", "decision": "game_over", "victory": False,
             "act": 2, "floor": 6,
             "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _run_solver(monkeypatch, script, seed="s1"):
    """play_run(Ironclad) with the solver on, against a scripted engine.
    Returns (result, sent commands)."""
    created = []

    def _fake(argv, **kw):
        created.append(_FakeEngine(script))
        return created[-1]

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake)
    monkeypatch.delenv("STS2_SOLVER_CHARS", raising=False)
    monkeypatch.delenv("STS2_SOLVER_BUDGET", raising=False)
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result = play_full_run.play_run(seed, character="Ironclad", verbose=False, log=False)
    return result, [json.loads(line) for line in created[0].sent]


def _actions(sent):
    return [c.get("action") or c.get("cmd") for c in sent]


def test_stale_refusal_replans_and_the_game_continues(monkeypatch):
    start = _combat(["STRIKE", "BODYGUARD"])
    after_strike = _combat(["BODYGUARD"], energy=0)
    script = [
        {"type": "ready"},
        start,
        _plan(_play("STRIKE"), _play("BODYGUARD"), END),
        after_strike,   # play_card STRIKE
        REFUSAL,        # play_card BODYGUARD: the live engine refuses it
        _plan(END),     # the harness must re-plan from the live state
        GAME_OVER,      # end_turn
    ]
    result, sent = _run_solver(monkeypatch, script)
    assert _actions(sent) == ["start_run", "plan_combat_turn", "play_card", "play_card",
                              "plan_combat_turn", "end_turn"]
    assert "error" not in result
    assert result["solver_stale_refusals"] == 1
    assert result["solver_turn_fallbacks"] == 0
    assert result["solver_plans"] == 2
    assert (result["act"], result["floor"]) == (2, 6)


def test_four_refusals_in_one_turn_hand_the_rest_of_the_turn_to_the_heuristic(
        monkeypatch, capsys):
    hand = _combat(["BODYGUARD"])
    script = [{"type": "ready"}, hand]
    for _ in range(4):
        script += [_plan(_play("BODYGUARD"), END), REFUSAL]
    script += [
        _combat([], energy=0),   # heuristic play_card (index 0) succeeds
        GAME_OVER,               # heuristic end_turn: empty hand, nothing to play
    ]
    result, sent = _run_solver(monkeypatch, script)
    acts = _actions(sent)
    assert acts.count("plan_combat_turn") == 4, "the 5th plan must not be requested"
    assert acts[-2:] == ["play_card", "end_turn"]
    assert "error" not in result
    assert result["solver_stale_refusals"] == 4
    assert result["solver_turn_fallbacks"] == 1   # one turn, however many heuristic steps
    out = capsys.readouterr().out
    assert out.count("plan_combat_turn refused 4 times this turn -- "
                     "heuristic for the rest of the turn") == 1


def test_three_refusals_still_allow_another_plan(monkeypatch):
    # The guard is "more than 3": the 4th plan of the turn is still requested.
    hand = _combat(["BODYGUARD"])
    script = [{"type": "ready"}, hand]
    for _ in range(3):
        script += [_plan(_play("BODYGUARD"), END), REFUSAL]
    script += [_plan(END), GAME_OVER]
    result, sent = _run_solver(monkeypatch, script)
    assert _actions(sent).count("plan_combat_turn") == 4
    assert result["solver_stale_refusals"] == 3 and result["solver_turn_fallbacks"] == 0


def test_the_refusal_budget_resets_on_the_next_turn(monkeypatch):
    turn1 = _combat(["BODYGUARD"], round_=1)
    turn2 = _combat(["BODYGUARD"], round_=2)
    script = [{"type": "ready"}, turn1]
    for _ in range(3):
        script += [_plan(_play("BODYGUARD"), END), REFUSAL]
    script += [_plan(END), turn2]                     # turn 1 ends after 3 refusals
    for _ in range(3):
        script += [_plan(_play("BODYGUARD"), END), REFUSAL]
    script += [_plan(END), GAME_OVER]
    result, sent = _run_solver(monkeypatch, script)
    assert result["solver_stale_refusals"] == 6 and result["solver_turn_fallbacks"] == 0
    assert _actions(sent).count("plan_combat_turn") == 8


def test_plan_execution_failure_reports_act_and_floor_from_the_last_decision(monkeypatch):
    # BUG-042's second defect: the failing reply is an error dict with no
    # context/player, so the row used to read act=None floor=None.
    script = [{"type": "ready"}, _combat(["BODYGUARD"]),
              _plan(_play("BODYGUARD"), END), STILL_IN_HAND]
    result, _ = _run_solver(monkeypatch, script)
    assert result["error"] == "plan_combat_turn_execution_failed"
    assert (result["act"], result["floor"]) == (2, 5)
    assert (result["hp"], result["max_hp"]) == (40, 80)
    assert result["solver_stale_refusals"] == 0


def test_results_stamp_the_new_counters_even_without_combat(monkeypatch):
    result, _ = _run_solver(monkeypatch, [{"type": "ready"}, GAME_OVER])
    assert result["solver_stale_refusals"] == 0 and result["solver_turn_fallbacks"] == 0


def test_eval_row_carries_the_stale_refusal_counters():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "act": 1, "floor": 9,
         "solver_stale_refusals": 5, "solver_turn_fallbacks": 2}, "Ironclad")
    assert row["solver_stale_refusals"] == 5 and row["solver_turn_fallbacks"] == 2


def test_summarize_reports_stale_refusals_and_turn_fallbacks():
    results = [dict(_result("s1", 40, 0), solver_stale_refusals=3, solver_turn_fallbacks=1),
               dict(_result("s2", 35, 2), solver_stale_refusals=4, solver_turn_fallbacks=0),
               _result("s3", 10, 0)]   # a row from before the counters existed counts as 0
    out = play_full_run.summarize(results, 3, character="Silent", solver_chars={"Silent"})
    # The start of the line is what tests and greps match; the new counts follow it.
    assert ("Solver engagement: 85/87 plan_combat_turn calls returned a usable plan, "
            "7 stale-plan refusals re-planned, 1 turns fell back to the heuristic") in out
