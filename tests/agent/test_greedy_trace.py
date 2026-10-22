"""greedy_action(state, trace=...) records the evidence it decided with, and the
evidence agrees with the choice. Replays the corpus in game order."""
import json

import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GreedyTrace, reset_greedy_context
from tests.agent.advisor_corpus import corpus_games, fresh_greedy_state


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def traced_corpus(decision=None):
    """(row, action, trace) for every corpus state of `decision` (None = all).
    Every state is replayed (traced) so greedy's hidden context stays in step."""
    for _game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            trace = GreedyTrace()
            action = greedy_action(row["state"], trace=trace)
            # the advisor emits the trace as JSON
            json.dumps(trace.to_dict(), allow_nan=False)
            if decision is None or row["state"]["decision"] == decision:
                yield row, action, trace


def _sums_match(trace):
    for opt in trace.to_dict()["options"]:
        if opt["status"] == "ok" and opt["components"]:
            total = sum(c["value"] for c in opt["components"])
            assert abs(total - opt["score"]) < 1e-9, opt


def test_trace_does_not_change_any_choice():
    for row, action, _trace in traced_corpus():
        assert action == row["expected"], (row["game"], row["seq"])


def test_card_reward_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("card_reward"):
        fired = trace.fired()
        assert len(fired) == 1, (row["game"], row["seq"], trace.rules)
        rule = fired[0]
        if action["action"] == "select_card_reward":
            idx = action["args"]["card_index"]
            assert rule["key"] == "card_reward.pick_best"
            assert trace.score_of("card", idx) == max(trace.ok_scores("card"))
            assert trace.score_of("card", idx) >= rule["values"]["threshold"]
        else:
            assert action["action"] == "skip_card_reward"
            assert rule["key"] in {"card_reward.below_threshold", "card_reward.all_filtered",
                                   "card_reward.all_broken", "card_reward.no_cards"}
            if rule["key"] == "card_reward.below_threshold":
                assert max(trace.ok_scores("card")) < rule["values"]["threshold"]
        _sums_match(trace)
        n += 1
    assert n > 0


def test_card_reward_components_are_the_nine_summed_terms():
    n = 0
    for _row, _action, trace in traced_corpus("card_reward"):
        for opt in trace.to_dict()["options"]:
            if opt["status"] == "ok":
                assert [c["key"] for c in opt["components"]] == [
                    "card.base", "card.set_bonus", "card.tag_synergy", "card.combat_type",
                    "card.boss_defense_gap", "card.boss_readiness", "card.quality_lift",
                    "card.archetype", "card.q_adjust"]
                n += 1
    assert n > 0


HEAL_RULES = {"rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
              "rest.heal_threshold"}
SMITH_RULES = {"rest.must_upgrade", "rest.default_smith"}


def test_rest_site_rules_stop_at_the_deciding_one():
    n = 0
    for row, action, trace in traced_corpus("rest_site"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        assert trace.rules[-1] is fired[0]
        assert all(r["outcome"] == "skipped" for r in trace.rules[:-1])
        key = fired[0]["key"]
        if action["action"] == "choose_option":
            idx = action["args"]["option_index"]
            opt = next(o for o in row["state"]["options"] if o["index"] == idx)
            if key in HEAL_RULES:
                assert "heal" in opt["option_id"].lower()
            elif key in SMITH_RULES:
                assert "smith" in opt["option_id"].lower()
            else:
                assert key == "rest.fallback"
        else:
            assert key == "rest.no_choice"
        n += 1
    assert n > 0


def _vantom_rest_state():
    """A real corpus rest_site state, moved to the Vantom mid-boss window: a
    Vantom boss, act-local floor 12, HP at 60%."""
    import copy
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "rest_site":
                state = copy.deepcopy(row["state"])
                state["context"]["boss"] = {"id": "VANTOM_BOSS", "name": "Vantom"}
                state["context"]["floor"] = 12
                state.pop("floor", None)
                state["player"]["hp"] = int(state["player"]["max_hp"] * 0.6)
                return state
    pytest.fail("no rest_site state in the corpus")


def test_rest_site_vantom_mid_boss_rule_is_env_gated():
    state = _vantom_rest_state()
    heal = next(o for o in state["options"] if "heal" in o["option_id"].lower())

    # gate closed (the shipped default): the rule is checked and skipped
    trace = GreedyTrace()
    greedy_action(state, trace=trace)
    vantom = next(r for r in trace.rules if r["key"] == "rest.vantom_mid_boss")
    assert vantom["outcome"] == "skipped"
    assert vantom["values"]["enabled"] is False

    # gate open: critical_heal (floor 12 -> 0.40) is skipped at 60% HP, and the
    # Vantom rule is the one that decides. STS2_VANTOM_REST_HEAL is read on every
    # call, so setting it only inside the context is enough; fresh_greedy_state
    # refuses to run with it set, hence it must not leak out.
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("STS2_VANTOM_REST_HEAL", "1")
        trace = GreedyTrace()
        traced = greedy_action(state, trace=trace)
        untraced = greedy_action(state)
    assert traced == untraced
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": heal["index"]}}
    keys = [(r["key"], r["outcome"]) for r in trace.rules]
    assert keys == [("rest.critical_heal", "skipped"), ("rest.vantom_mid_boss", "fired")]
    vantom = trace.rules[-1]
    assert vantom["values"]["enabled"] is True
    assert vantom["values"]["floor"] == 12
    assert abs(vantom["values"]["hp_ratio"] - 0.6) < 0.02
    json.dumps(trace.to_dict(), allow_nan=False)
