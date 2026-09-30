"""Phase 3a: STS2_OOC_POLICY -- out-of-combat decisions via agent.combat_env.greedy_action.

No game DLLs: play_full_run.EngineProcess is replaced by the scripted
_FakeEngine from test_map_route_determinism, and the greedy policy itself is
replaced through the play_full_run._load_greedy_action seam, so these tests pin
the harness's dispatch/fallback logic, not card_scoring's judgement.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine


def test_ooc_policy_defaults_to_naive():
    assert play_full_run.ooc_policy({}) == "naive"


def test_ooc_policy_blank_means_default():
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": "  "}) == "naive"


@pytest.mark.parametrize("raw,expected", [("greedy", "greedy"), ("NAIVE", "naive"), (" Greedy ", "greedy")])
def test_ooc_policy_accepts_known_values_case_insensitively(raw, expected):
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": raw}) == expected


def test_ooc_policy_rejects_unknown_values_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="STS2_OOC_POLICY"):
        play_full_run.ooc_policy({"STS2_OOC_POLICY": "smart"})


def test_greedy_decisions_exclude_map_and_combat():
    assert "map_select" not in play_full_run.OOC_GREEDY_DECISIONS
    assert "combat_play" not in play_full_run.OOC_GREEDY_DECISIONS
    assert play_full_run.OOC_GREEDY_DECISIONS == {
        "card_reward", "rest_site", "event_choice", "bundle_select", "card_select", "shop"}


@pytest.mark.parametrize("state,expected", [
    ({"decision": "card_reward"}, {"cmd": "action", "action": "skip_card_reward"}),
    ({"decision": "bundle_select"}, {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}),
    ({"decision": "card_select", "cards": [{"index": 0}]},
     {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}),
    ({"decision": "card_select", "cards": []}, {"cmd": "action", "action": "skip_select"}),
    ({"decision": "rest_site"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "event_choice"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "shop"}, {"cmd": "action", "action": "leave_room"}),
])
def test_fallback_command_per_decision(state, expected):
    assert play_full_run.ooc_fallback_command(state) == expected


def test_load_greedy_action_returns_the_real_combat_env_function():
    fn = play_full_run._load_greedy_action()
    assert callable(fn) and fn.__name__ == "greedy_action"
    assert fn.__module__ == "agent.combat_env"


def _game_over(act=1, floor=3):
    return {"type": "decision", "decision": "game_over", "victory": False,
            "act": act, "floor": floor,
            "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _run(monkeypatch, script, seed="s1", **kwargs):
    """play_run() against a scripted engine; returns (result, sent commands)."""
    created = []

    def _fake(argv, **kw):
        created.append(_FakeEngine(script))
        return created[-1]

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake)
    kwargs.setdefault("verbose", False)
    kwargs.setdefault("log", False)
    result = play_full_run.play_run(seed, character="Ironclad", **kwargs)
    return result, [json.loads(line) for line in created[0].sent]


def test_start_run_carries_the_requested_ascension(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()], ascension=1)
    start = sent[0]
    assert start["cmd"] == "start_run" and start["ascension"] == 1
    assert result["ascension"] == 1


def test_ascension_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert sent[0]["ascension"] == 0 and result["ascension"] == 0


def test_result_carries_policy_and_zero_counters_under_naive(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert result["ooc_policy"] == "naive"
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 0
    assert result["game_log"] is None  # log=False writes no game log


def test_keep_game_logs_copies_the_log_and_records_the_kept_path(monkeypatch, tmp_path):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    # game_log.LOG_DIR is read at call time by GameLogger and cleanup_old_logs,
    # so pointing it at tmp_path keeps the test out of the real logs/.
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr("game_log.LOG_DIR", str(logs_dir))
    keep = tmp_path / "kept"
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()], seed="p3a_7",
                     ascension=1, log=True, keep_log_dir=str(keep))
    kept = keep / "Ironclad_p3a_7.jsonl"
    assert result["game_log"] == str(kept)
    lines = [json.loads(l) for l in kept.read_text().splitlines()]
    meta = lines[0]
    assert meta["type"] == "run_meta"
    assert meta["seed"] == "p3a_7" and meta["character"] == "Ironclad"
    assert meta["ascension"] == 1 and meta["experiment"] == "ooc=naive"
    assert any(l.get("type") == "state" for l in lines[1:])


def test_eval_row_carries_phase3a_fields():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "p3a_1", "act": 1, "floor": 9, "steps": 50,
         "solver_plans": 3, "solver_errors": 0, "ascension": 1,
         "ooc_policy": "greedy", "ooc_greedy": 12, "ooc_fallbacks": 1,
         "game_log": "/x/Ironclad_p3a_1.jsonl"}, "Ironclad")
    assert row["ascension"] == 1 and row["ooc_policy"] == "greedy"
    assert row["ooc_greedy"] == 12 and row["ooc_fallbacks"] == 1
    assert row["game_log"] == "/x/Ironclad_p3a_1.jsonl"
    assert row["status"] == "dead" and row["floor"] == 9


def _decision(kind, **extra):
    d = {"type": "decision", "decision": kind,
         "context": {"act": 1, "floor": 5, "room_type": extra.pop("room_type", "")},
         "player": {"hp": 50, "max_hp": 80, "gold": extra.pop("gold", 100), "deck_size": 10}}
    d.update(extra)
    return d


def _greedy_env(monkeypatch, policy_fn):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: policy_fn)


def test_greedy_policy_answers_card_reward(monkeypatch):
    seen = []

    def policy(state):
        seen.append(state["decision"])
        return {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}}

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert seen == ["card_reward"]
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}} in sent
    assert result["ooc_policy"] == "greedy"
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 0


def test_naive_policy_never_loads_greedy(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)

    def boom():
        raise AssertionError("naive policy must not import greedy_action")

    monkeypatch.setattr(play_full_run, "_load_greedy_action", boom)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 0}} in sent
    assert result["ooc_greedy"] == 0


def test_refused_greedy_command_falls_back_and_is_counted(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "select_card_reward",
                                        "args": {"card_index": 1}})
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              {"type": "error", "message": "nope"},
              _game_over()]
    result, sent = _run(monkeypatch, script)
    actions = [c.get("action") for c in sent]
    assert actions[1:3] == ["select_card_reward", "skip_card_reward"]
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 1
    assert "error" not in result  # the fallback recovered; the run ended normally


def test_greedy_exception_falls_back_without_counting_a_greedy_decision(monkeypatch):
    def policy(state):
        raise RuntimeError("scoring blew up")

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"}, _decision("rest_site", options=[]), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1] == {"cmd": "action", "action": "leave_room"}
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 1


def test_map_and_combat_are_never_delegated(monkeypatch):
    def policy(state):
        raise AssertionError(f"greedy called for {state['decision']}")

    _greedy_env(monkeypatch, policy)
    monkeypatch.setenv("STS2_SOLVER_CHARS", "none")
    script = [{"type": "ready"},
              _decision("map_select", choices=[{"col": 0, "row": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1]["action"] == "select_map_node"
    assert result["ooc_greedy"] == 0


def test_shop_visit_is_capped(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    cap = play_full_run.SHOP_ACTION_CAP
    # gold changes every step so the global STUCK detector stays quiet
    shops = [_decision("shop", gold=1000 - i) for i in range(cap + 1)]
    script = [{"type": "ready"}, *shops, _game_over()]
    result, sent = _run(monkeypatch, script)
    actions = [c.get("action") for c in sent[1:]]
    assert actions == ["buy_card"] * cap + ["leave_room"]
    assert result["ooc_greedy"] == cap and result["ooc_fallbacks"] == 1


def test_shop_cap_resets_for_a_new_shop(monkeypatch):
    # Cap of 1: a second action in the SAME visit would be forced to leave_room,
    # so two buys with zero fallbacks proves the counter reset at the new floor.
    monkeypatch.setattr(play_full_run, "SHOP_ACTION_CAP", 1)
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    first = _decision("shop", gold=100)
    second = _decision("shop", gold=90)
    second["context"] = {"act": 1, "floor": 11, "room_type": ""}
    script = [{"type": "ready"}, first, second, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert [c.get("action") for c in sent[1:]] == ["buy_card", "buy_card"]
    assert result["ooc_greedy"] == 2 and result["ooc_fallbacks"] == 0


def test_event_page_stuck_guard_applies_under_greedy(monkeypatch):
    calls = []

    def policy(state):
        calls.append(1)
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}

    _greedy_env(monkeypatch, policy)
    page = _decision("event_choice",
                     options=[{"index": 0, "is_locked": False, "was_chosen": True}])
    script = [{"type": "ready"}, page, page, page, page, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert len(calls) == 3
    assert sent[-1] == {"cmd": "action", "action": "leave_room"}
