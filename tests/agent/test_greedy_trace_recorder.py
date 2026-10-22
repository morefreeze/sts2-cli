# tests/agent/test_greedy_trace_recorder.py
"""GreedyTrace: the recorder greedy_action writes its reasons into."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from agent import card_scoring
from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, TRACE_KEYS, GreedyTrace,
                                reset_greedy_context)


def test_option_and_rule_round_trip():
    t = GreedyTrace()
    t.option(2, kind="card", score=7.5,
             components=[("card.base", 6.0, "A"), ("card.archetype", 1.5, None)])
    t.option(0, kind="card", status="filtered")
    t.rule("card_reward.pick_best", "fired", best_index=2, threshold=5.5)
    d = t.to_dict()
    assert [o["index"] for o in d["options"]] == [0, 2]
    assert d["options"][1] == {
        "kind": "card", "index": 2, "score": 7.5, "status": "ok",
        "components": [{"key": "card.base", "value": 6.0, "detail": "A"},
                       {"key": "card.archetype", "value": 1.5}],
    }
    assert d["options"][0]["score"] is None and d["options"][0]["status"] == "filtered"
    assert d["rules"] == [{"key": "card_reward.pick_best", "outcome": "fired",
                           "values": {"best_index": 2, "threshold": 5.5}}]
    assert t.score_of("card", 2) == 7.5
    assert t.ok_scores("card") == [7.5]
    assert [r["key"] for r in t.fired()] == ["card_reward.pick_best"]


def test_unknown_key_raises():
    t = GreedyTrace()
    with pytest.raises(KeyError):
        t.rule("card_reward.typo", "fired")
    with pytest.raises(KeyError):
        t.option(0, kind="card", score=1.0, components=[("card.typo", 1.0, None)])


def test_bad_outcome_and_status_raise():
    t = GreedyTrace()
    with pytest.raises(ValueError):
        t.rule("card_reward.pick_best", "maybe")
    with pytest.raises(ValueError):
        t.option(0, kind="card", status="weird")


def test_behavior_env_covers_play_full_run_knobs():
    import play_full_run
    assert set(play_full_run.GREEDY_KNOBS) <= set(GREEDY_BEHAVIOR_ENV)
    assert {"STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL"} <= set(GREEDY_BEHAVIOR_ENV)


def test_reset_greedy_context_restores_module_default():
    card_scoring.set_mc_context(hp=12, max_hp=70, floor=14, relics=["X"])
    reset_greedy_context()
    assert card_scoring._MC_CONTEXT == {"hp": 80, "max_hp": 80, "floor": 5}


def test_event_keyword_keys_are_trace_keys():
    assert sum(1 for k in TRACE_KEYS if k.startswith("event.kw.")) == 28
