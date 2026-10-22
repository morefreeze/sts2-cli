"""greedy_action(state, trace=...) records the evidence it decided with, and the
evidence agrees with the choice. Replays the corpus in game order."""
import copy
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


def _rest_state(*, floor, hp, max_hp=80, boss=None, heal_enabled=True,
                smith_enabled=True):
    """A real corpus rest_site state, edited: act-local `floor`, `hp`/`max_hp`,
    optionally another boss, and the HEAL / SMITH option disabled."""
    for _game, rows in corpus_games():
        for row in rows:
            if row["state"]["decision"] == "rest_site":
                state = copy.deepcopy(row["state"])
                state.pop("floor", None)
                state["context"]["floor"] = floor
                if boss is not None:
                    state["context"]["boss"] = boss
                state["player"]["hp"] = hp
                state["player"]["max_hp"] = max_hp
                for opt in state["options"]:
                    name = opt["option_id"].lower()
                    if "heal" in name:
                        opt["is_enabled"] = heal_enabled
                    elif "smith" in name:
                        opt["is_enabled"] = smith_enabled
                return state
    pytest.fail("no rest_site state in the corpus")


def _option_index(state, name):
    return next(o["index"] for o in state["options"] if name in o["option_id"].lower())


def _vantom_rest_state():
    """A corpus rest_site state moved to the Vantom mid-boss window: a Vantom
    boss, act-local floor 12, HP at 60%."""
    return _rest_state(floor=12, hp=48, boss={"id": "VANTOM_BOSS", "name": "Vantom"})


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
    assert vantom["values"]["is_vantom"] is True
    assert vantom["values"]["available"] is True
    assert vantom["values"]["floor"] == 12
    assert abs(vantom["values"]["hp_ratio"] - 0.6) < 0.02
    json.dumps(trace.to_dict(), allow_nan=False)


def test_rest_site_heal_rules_say_when_heal_is_unavailable():
    """10/80 HP at floor 3 is far below the critical threshold, but HEAL is
    disabled: the skipped rule must say so, or it reads as a rule that failed."""
    state = _rest_state(floor=3, hp=10, heal_enabled=False,
                       boss={"id": "CEREMONIAL_BEAST_BOSS", "name": "Ceremonial Beast"})
    smith_index = _option_index(state, "smith")
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": smith_index}}
    json.dumps(trace.to_dict(), allow_nan=False)

    by_key = {r["key"]: r for r in trace.rules}
    critical = by_key["rest.critical_heal"]
    assert critical["outcome"] == "skipped"
    assert critical["values"]["hp_ratio"] == 0.125
    assert critical["values"]["hp_ratio"] < critical["values"]["threshold"]
    assert critical["values"]["available"] is False
    # every heal rule that was checked says HEAL is unavailable (rest.heal_threshold
    # only appears when must_upgrade did not already decide)
    heal_rules = [r for r in trace.rules if r["key"] in HEAL_RULES]
    assert {r["key"] for r in heal_rules} >= {
        "rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal"}
    assert all(r["values"]["available"] is False for r in heal_rules)
    assert by_key["rest.vantom_mid_boss"]["values"]["is_vantom"] is False
    # the SMITH rules decide: must_upgrade if the deck has a target, else default_smith
    fired = trace.fired()
    assert len(fired) == 1 and trace.rules[-1] is fired[0]
    assert fired[0]["key"] in {"rest.must_upgrade", "rest.default_smith"}


def test_rest_site_must_upgrade_says_when_smith_is_unavailable():
    """Full HP, SMITH disabled: must_upgrade cannot fire whatever the deck holds."""
    state = _rest_state(floor=3, hp=80, smith_enabled=False)
    heal_index = _option_index(state, "heal")
    trace = GreedyTrace()
    traced = greedy_action(state, trace=trace)
    assert traced == greedy_action(state)
    assert traced == {"cmd": "action", "action": "choose_option",
                      "args": {"option_index": heal_index}}
    json.dumps(trace.to_dict(), allow_nan=False)

    by_key = {r["key"]: r for r in trace.rules}
    must = by_key["rest.must_upgrade"]
    assert must["outcome"] == "skipped"
    assert must["values"]["available"] is False
    assert must["values"]["deck_index"] is None
    # HEAL is available but full HP clears every heal rule; with no SMITH either,
    # the fallback takes HEAL
    assert all(by_key[k]["values"]["available"] is True for k in (
        "rest.critical_heal", "rest.pre_boss_heal", "rest.heal_threshold"))
    assert by_key["rest.default_smith"]["outcome"] == "skipped"
    assert trace.rules[-1]["key"] == "rest.fallback"
    assert trace.rules[-1]["outcome"] == "fired"
    assert len(trace.fired()) == 1
