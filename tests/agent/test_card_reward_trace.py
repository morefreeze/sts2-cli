"""Hand-built card_reward states for the rules the real-state corpus never (or
rarely) reaches, each checked against greedy_action's own branch conditions.
The base state is one real card_reward state from the corpus, edited."""
import copy
import json

import pytest

from agent import card_scoring
from agent.combat_env import greedy_action
from agent.greedy_trace import GreedyTrace
from tests.agent.advisor_corpus import corpus_rows, fresh_greedy_state

SKIP = {"cmd": "action", "action": "skip_card_reward"}


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def _base_state() -> dict:
    """A real Act 1, early-floor card_reward state with a skippable reward and a
    deck under 16 cards (so neither the 18-card threshold bump nor the late
    Act 1 quality gate applies)."""
    for row in corpus_rows():
        s = row["state"]
        if (row["game"] != "synthetic" and s["decision"] == "card_reward"
                and s.get("can_skip") and s["context"]["act"] == 1
                and len(s["cards"]) >= 2
                and len(s["player"]["deck"]) < 15):
            return copy.deepcopy(s)
    raise AssertionError("no suitable real card_reward state in the corpus")


def _run(state: dict):
    """(action, trace), after checking tracing changed neither the choice nor
    the JSON-safety of what the advisor will emit."""
    trace = GreedyTrace()
    action = greedy_action(copy.deepcopy(state), trace=trace)
    assert action == greedy_action(copy.deepcopy(state))
    json.dumps(trace.to_dict(), allow_nan=False)
    return action, trace


def _statuses(trace) -> list:
    return [o["status"] for o in trace.to_dict()["options"]]


def test_no_cards():
    s = _base_state()
    s["cards"] = []
    action, trace = _run(s)
    assert action == SKIP
    assert [r["key"] for r in trace.fired()] == ["card_reward.no_cards"]
    assert trace.to_dict()["options"] == []


def test_only_a_broken_card():
    broken = sorted(card_scoring.BROKEN_CARDS)[0]
    s = _base_state()
    s["cards"] = [dict(s["cards"][0], id="CARD." + broken, index=0)]
    action, trace = _run(s)
    assert action == SKIP
    assert _statuses(trace) == ["broken"]
    assert [r["key"] for r in trace.fired()] == ["card_reward.all_broken"]


def test_threshold_lift_makes_every_card_fall_short():
    s = _base_state()
    assert s["context"]["act"] == 1 and len(s["player"]["deck"]) < 18
    # combat_env's act-1, deck_size < 18 branch is `threshold = 5.5`
    # (STS2_CARD_THRESHOLD_LIFT is read on every call by _card_threshold_lift).
    act1_small_deck_threshold = 5.5
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STS2_CARD_THRESHOLD_LIFT", "9")
        action, trace = _run(s)
    assert action == SKIP
    [rule] = trace.fired()
    assert rule["key"] == "card_reward.below_threshold"
    assert rule["values"]["lift"] == 9.0
    assert rule["values"]["threshold"] == act1_small_deck_threshold + 9.0
    assert rule["values"]["best_score"] < rule["values"]["threshold"]
    assert set(_statuses(trace)) == {"ok"}


def _late_act1_dilute_state(*, can_skip: bool) -> dict:
    """Act 1, floor 13, a 16-card deck: is_act1_card_reward_eligible rejects
    every offered card (len(deck) >= 16), so the quality gate empties the list."""
    s = _base_state()
    deck = s["player"]["deck"]
    while len(deck) < 16:
        deck.append(copy.deepcopy(deck[len(deck) % 5]))
    s["player"]["deck_size"] = len(deck)
    s["context"]["act"] = 1
    s["context"]["floor"] = 13
    s.pop("act", None)
    s.pop("floor", None)
    s["can_skip"] = can_skip
    return s


def test_quality_gate_emptied_list_is_all_filtered_when_skippable():
    s = _late_act1_dilute_state(can_skip=True)
    action, trace = _run(s)
    assert action == SKIP
    [rule] = trace.fired()
    assert rule["key"] == "card_reward.all_filtered"
    assert rule["values"]["quality_gate"] is True
    assert rule["values"]["gate_fallback"] is False
    assert _statuses(trace) == ["filtered"] * len(s["cards"])


def test_quality_gate_fallback_when_the_reward_cannot_be_skipped():
    s = _late_act1_dilute_state(can_skip=False)
    action, trace = _run(s)
    assert action["action"] == "select_card_reward"
    assert "filtered" not in _statuses(trace)
    assert set(_statuses(trace)) == {"ok"}
    [rule] = trace.fired()
    assert rule["key"] == "card_reward.pick_best"
    assert rule["values"]["quality_gate"] is True
    assert rule["values"]["gate_fallback"] is True
    assert rule["values"]["best_index"] == action["args"]["card_index"]


def test_gate_fallback_is_false_when_the_gate_is_off():
    s = _base_state()
    action, trace = _run(s)
    for rule in trace.fired():
        assert rule["values"]["quality_gate"] is False
        assert rule["values"]["gate_fallback"] is False
