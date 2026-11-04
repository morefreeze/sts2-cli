"""agent/advice_debug.py and the `debug` key of advisor_server.advise()."""
import copy
import json

import pytest

from agent import advice_debug, advisor_server
from agent.combat_env import greedy_action
from tests.agent.advisor_corpus import corpus_rows, fresh_greedy_state


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def _first(decision):
    return copy.deepcopy(next(r["state"] for r in corpus_rows()
                              if r["state"]["decision"] == decision))


def _event_state(titles, locked=()):
    return {"decision": "event_choice",
            "options": [{"index": i, "title": t, "description": None, "is_locked": i in locked}
                        for i, t in enumerate(titles)]}


def _ev_option(index, score=0.0, components=()):
    return {"kind": "option", "index": index, "score": score, "status": "ok",
            "components": [{"key": k, "value": v} for k, v in components]}


def test_event_with_one_unscored_option_names_it():
    state = _event_state(["Known", "Mystery"])
    evidence = {"options": [_ev_option(0, 3.0, [("event.kw.gain_max_hp", 3.0)]), _ev_option(1)],
                "rules": []}
    assert advice_debug.debug_info(state, evidence) == {
        "event_unscored": ["Mystery"], "event_all_zero": False}


def test_event_where_every_option_is_zero():
    state = _event_state(["A", "B", "Locked"], locked={2})
    evidence = {"options": [_ev_option(0), _ev_option(1),
                            {"kind": "option", "index": 2, "score": None, "status": "locked",
                             "components": []}], "rules": []}
    assert advice_debug.debug_info(state, evidence) == {
        "event_unscored": ["A", "B"], "event_all_zero": True}


def test_event_fully_scored_has_no_debug():
    state = _event_state(["A"])
    evidence = {"options": [_ev_option(0, 1.5, [("event.kw.energy_once", 1.5)])], "rules": []}
    assert advice_debug.debug_info(state, evidence) is None


def test_event_advice_carries_debug_on_a_real_state():
    state = _first("event_choice")
    for option in state["options"]:
        option["title"] = "Zzzz unknown"
        option["description"] = "nothing greedy knows"
    reply = advisor_server.advise(state, greedy_action, [])
    assert reply["action"] is not None
    assert reply["debug"]["event_all_zero"] is True
    assert reply["debug"]["event_unscored"] == ["Zzzz unknown"] * len(state["options"])


def test_card_reward_block_has_margin_and_deck_metrics():
    state = _first("card_reward")
    reply = advisor_server.advise(state, greedy_action, [])
    block = reply["debug"]["card_reward"]
    values = next(r["values"] for r in reply["rules"]
                  if r["key"] in ("card_reward.pick_best", "card_reward.below_threshold"))
    ok = [o for o in reply["options"] if o["status"] == "ok"]
    best = max(ok, key=lambda o: o["score"])
    assert block["deck_size"] == len(state["player"]["deck"])
    assert block["best_index"] == best["index"]
    assert block["best_score"] == pytest.approx(best["score"], abs=1e-4)
    assert block["threshold"] == pytest.approx(values["threshold"], abs=1e-4)
    assert block["margin"] == pytest.approx(block["best_score"] - block["threshold"], abs=1e-4)
    assert {"overall", "burst_5turn", "block_per_turn"} <= set(block["metrics_before"])
    assert set(block["metrics_after_best"]) == set(block["metrics_before"])
    json.dumps(reply, allow_nan=False)


def test_card_reward_debug_does_not_touch_the_state():
    state = _first("card_reward")
    before = copy.deepcopy(state)
    advisor_server.advise(state, greedy_action, [])
    assert state == before


@pytest.mark.parametrize("decision", ["rest_site", "shop", "map_select", "card_select",
                                      "bundle_select"])
def test_other_advised_decisions_have_no_debug(decision):
    state = _first(decision)
    reply = advisor_server.advise(state, greedy_action, [])
    assert reply["action"] is not None
    assert "debug" not in reply


def test_debug_info_none_for_other_decisions():
    assert advice_debug.debug_info({"decision": "combat_play"}, {}) is None


def test_debug_raising_keeps_the_advice(monkeypatch):
    def boom(state, evidence):
        raise KeyError("x")
    monkeypatch.setattr(advisor_server, "debug_info", boom)
    state = _first("card_reward")
    reply = advisor_server.advise(state, greedy_action, ["w"])
    assert reply["action"] is not None and reply["options"]
    assert "debug" not in reply
    assert reply["warnings"] == ["w", "debug_raised: KeyError"]


def test_unserializable_debug_is_dropped_not_the_advice(monkeypatch):
    monkeypatch.setattr(advisor_server, "debug_info",
                        lambda state, evidence: {"margin": float("nan")})
    reply = advisor_server.advise(_first("card_reward"), greedy_action, [])
    assert reply["action"] is not None and reply["options"]
    assert "debug" not in reply
    assert "debug_not_serializable" in reply["warnings"]
    json.dumps(reply, allow_nan=False)
