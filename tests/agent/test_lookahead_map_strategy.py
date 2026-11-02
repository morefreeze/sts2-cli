"""LookaheadMapStrategy: whole-path map routing (agent/strategy.py), and the
STS2_MAP_POLICY switch in greedy_action that selects it.

Hermetic: hand-built maps in the engine's get_map shape, no game DLLs. The
corridor cases are the ones HpAwareMapStrategy (next node only) gets wrong: the
first rooms of both branches look the same, the difference is further on.
"""
import json
import random

import pytest

from agent import combat_env
from agent.greedy_trace import GreedyTrace
from agent.strategy import HpAwareMapStrategy, LookaheadMapStrategy


def _map(spec, pre_boss_rest=True):
    """spec: {(col, row): (type, [child (col, row) ...])} -> a get_map-shaped dict.
    A child that is not a key of spec stands for the boss. Like every real map,
    the rooms before the boss lead into a rest site on row 15 (pre_boss_rest);
    the boss itself is not a node of rows."""
    spec = dict(spec)
    if pre_boss_rest:
        spec = {k: (t, [(0, 15) if c not in spec else c for c in kids])
                for k, (t, kids) in spec.items()}
        spec[(0, 15)] = ("RestSite", [(9, 99)])
    n_rows = max(row for _, row in spec) + 1
    rows = [[] for _ in range(n_rows)]
    for (col, row), (room_type, children) in sorted(spec.items()):
        rows[row].append({"col": col, "row": row, "type": room_type,
                          "children": [{"col": c, "row": r} for c, r in children],
                          "visited": False, "current": False})
    return {"type": "map", "rows": rows, "boss": {"col": 3, "row": n_rows, "type": "Boss"}}


def _state(m, choices, hp=75, max_hp=75, act=1, gold=0, with_map=True):
    """A map_select decision offering `choices` ((col, row) pairs of m)."""
    types = {(n["col"], n["row"]): n["type"] for row in m["rows"] for n in row}
    state = {"type": "decision", "decision": "map_select", "act": act, "floor": 1,
             "context": {"act": act, "floor": 1},
             "choices": [{"col": c, "row": r, "type": types[(c, r)]} for c, r in choices],
             "player": {"hp": hp, "max_hp": max_hp, "gold": gold}}
    if with_map:
        state["map"] = m
    return state


def _picked(cmd):
    assert cmd["cmd"] == "action" and cmd["action"] == "select_map_node"
    return cmd["args"]["col"], cmd["args"]["row"]


def _pick(state, strategy=None, **kw):
    strategy = strategy or LookaheadMapStrategy()
    return _picked(strategy.choose(state, state["choices"], **kw))


def _hpaware_pick(state):
    return _picked(HpAwareMapStrategy().choose(state, state["choices"]))


# Two entrances that look the same to a next-node scorer (both Monster); the
# left one is a corridor into an Elite.
CORRIDOR = _map({
    (0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
    (0, 2): ("Elite", [(0, 3)]),   (1, 2): ("RestSite", [(1, 3)]),
    (0, 3): ("RestSite", [(9, 9)]), (1, 3): ("Monster", [(9, 9)]),
})


# ---- the cases the policy exists for ----

def test_avoids_a_corridor_that_forces_an_elite_at_low_hp():
    # Act 3, 40/75 (the live Regent run): HpAware sees Monster vs Monster and
    # takes the first; only the whole path shows the Elite behind it.
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    assert _hpaware_pick(state) == (0, 1)
    assert _pick(state) == (1, 1)


def test_takes_the_elite_path_when_hp_is_high():
    # Act 1, full HP, the elite path listed second: HpAware's tie goes to the first.
    m = _map({
        (0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
        (0, 2): ("Monster", [(0, 3)]), (1, 2): ("Elite", [(1, 3)]),
        (0, 3): ("RestSite", [(9, 9)]), (1, 3): ("RestSite", [(9, 9)]),
    })
    state = _state(m, [(0, 1), (1, 1)], hp=80, max_hp=80, act=1)
    assert _hpaware_pick(state) == (0, 1)
    assert _pick(state) == (1, 1)


def test_prefers_the_path_with_a_rest_site_when_hp_is_low():
    # 30/80 in Act 2: three fights in a row vs a rest in the middle.
    m = _map({
        (0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
        (0, 2): ("Monster", [(0, 3)]), (1, 2): ("RestSite", [(1, 3)]),
        (0, 3): ("Monster", [(9, 9)]), (1, 3): ("Monster", [(9, 9)]),
    })
    state = _state(m, [(0, 1), (1, 1)], hp=30, max_hp=80, act=2)
    assert _hpaware_pick(state) == (0, 1)
    assert _pick(state) == (1, 1)


def test_a_rest_before_the_elite_makes_the_elite_path_worth_taking():
    # Same corridor, but the left entrance leads to a rest first: Act 1, 50/80.
    m = _map({
        (0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
        (0, 2): ("RestSite", [(0, 3)]), (1, 2): ("Monster", [(1, 3)]),
        (0, 3): ("Elite", [(0, 4)]), (1, 3): ("Monster", [(1, 4)]),
        (0, 4): ("RestSite", [(9, 9)]), (1, 4): ("RestSite", [(9, 9)]),
    })
    state = _state(m, [(1, 1), (0, 1)], hp=50, max_hp=80, act=1)
    assert _pick(state) == (0, 1)


def test_an_elite_reached_below_the_danger_line_earns_nothing_and_risks_death():
    s = LookaheadMapStrategy()
    path = [{"col": 0, "row": 5, "type": "Elite"}]
    healthy = s.evaluate_path(path, 70, 75, 0, 2)
    hurt = s.evaluate_path(path, 30, 75, 0, 2)
    assert healthy.parts["elite_reward"] > 0
    assert "elite_reward" not in hurt.parts            # 40% < the danger line
    assert hurt.parts["elite_danger"] < -50            # a real chance of dying
    assert healthy.value > hurt.value


def test_act_3_elites_are_avoided_even_at_full_hp_when_there_is_a_fight_instead():
    m = _map({
        (0, 1): ("Elite", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
        (0, 2): ("RestSite", [(9, 9)]), (1, 2): ("RestSite", [(9, 9)]),
    })
    state = _state(m, [(0, 1), (1, 1)], hp=75, act=3)
    assert _pick(state) == (1, 1)
    # ... while the same offer in Act 1 takes the elite
    assert _pick(_state(m, [(0, 1), (1, 1)], hp=75, act=1)) == (0, 1)


def test_boss_entry_hp_decides_between_otherwise_equal_routes():
    # The left route ends in a rest, the right one in a fight: a 35/75 hero should
    # not walk into the boss from a fight.
    m = _map({
        (0, 1): ("Unknown", [(0, 2)]), (1, 1): ("Unknown", [(1, 2)]),
        (0, 2): ("Monster", [(9, 9)]), (1, 2): ("RestSite", [(9, 9)]),
    }, pre_boss_rest=False)
    s = LookaheadMapStrategy()
    assert _pick(_state(m, [(0, 1), (1, 1)], hp=35, act=2), s) == (1, 1)


# ---- falling back to HpAware ----

FALLBACK_STATES = [
    dict(hp=75, max_hp=75, act=1), dict(hp=20, max_hp=75, act=1),
    dict(hp=40, max_hp=75, act=2), dict(hp=70, max_hp=75, act=3),
    dict(hp=75, max_hp=75, act=2, gold=200),
]


@pytest.mark.parametrize("kw", FALLBACK_STATES)
def test_without_a_map_it_is_hpaware_exactly(kw):
    choices = [(0, 1), (1, 1)]
    state = _state(CORRIDOR, choices, with_map=False, **kw)
    assert LookaheadMapStrategy().choose(state, state["choices"]) == \
        HpAwareMapStrategy().choose(state, state["choices"])
    # ... and the trace is HpAware's, preceded by the reason
    plain, lookahead = GreedyTrace(), GreedyTrace()
    HpAwareMapStrategy().choose(state, state["choices"], trace=plain)
    LookaheadMapStrategy().choose(state, state["choices"], trace=lookahead)
    assert lookahead.rules[0] == {"key": "map.la_fallback", "outcome": "fired",
                                  "values": {"reason": "no_map"}}
    assert lookahead.to_dict()["options"] == plain.to_dict()["options"]
    assert lookahead.rules[1:] == plain.rules


@pytest.mark.parametrize("map_json", [None, {}, {"type": "map", "rows": []}])
def test_an_empty_map_is_no_map(map_json):
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    state["map"] = map_json
    assert _pick(state) == _hpaware_pick(state)


def test_a_choice_that_is_not_on_the_map_falls_back_for_the_whole_decision():
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    state["choices"].append({"col": 5, "row": 1, "type": "Monster"})
    t = GreedyTrace()
    cmd = LookaheadMapStrategy().choose(state, state["choices"], trace=t)
    assert cmd == HpAwareMapStrategy().choose(state, state["choices"])
    assert t.rules[0]["values"] == {"reason": "choice_not_on_map"}


def test_too_many_paths_falls_back(monkeypatch):
    import agent.strategy as strategy

    def boom(*a, **kw):
        raise ValueError("more than 20000 paths")

    monkeypatch.setattr(strategy, "enumerate_paths", boom)
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    t = GreedyTrace()
    assert _picked(LookaheadMapStrategy().choose(state, state["choices"], trace=t)) == (0, 1)
    assert t.rules[0]["values"] == {"reason": "too_many_paths"}


def test_a_single_choice_is_taken_without_looking_ahead():
    state = _state(CORRIDOR, [(0, 1)], hp=40, act=3)
    t = GreedyTrace()
    assert _picked(LookaheadMapStrategy().choose(state, state["choices"], trace=t)) == (0, 1)
    assert [r["key"] for r in t.rules] == ["map.pick_min_priority"]


def test_the_map_can_be_passed_beside_the_state():
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3, with_map=False)
    s = LookaheadMapStrategy()
    assert _picked(s.choose(state, state["choices"], map_json=CORRIDOR)) == (1, 1)


# ---- ties, determinism ----

def test_equal_routes_go_to_hpaware_priority_then_node_order():
    s = LookaheadMapStrategy()
    # Monster and Unknown made worth exactly the same: HpAware ranks Unknown first
    s.UNKNOWN_COST = s.MONSTER_COST[0]
    s.UNKNOWN_REWARD = s.MONSTER_REWARD
    m = _map({(0, 1): ("Monster", [(0, 2)]), (1, 1): ("Unknown", [(0, 2)]),
              (0, 2): ("RestSite", [(9, 9)])})
    state = _state(m, [(0, 1), (1, 1)], hp=75, act=1)
    t = GreedyTrace()
    assert _pick(state, s, trace=t) == (1, 1)
    rule = next(r for r in t.rules if r["key"] == "map.la_best_path")
    assert rule["values"]["tied_best"] == [0, 1] and rule["values"]["decider"] == "tied"
    # two Monsters into the same room: node order
    m2 = _map({(0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(0, 2)]),
               (0, 2): ("RestSite", [(9, 9)])})
    assert _pick(_state(m2, [(0, 1), (1, 1)]), LookaheadMapStrategy()) == (0, 1)


def _random_map(rng):
    spec = {}
    types = ["Monster", "Monster", "Unknown", "Elite", "RestSite", "Shop"]
    for row in range(1, 9):
        for col in rng.sample(range(5), rng.randint(2, 4)):
            spec[(col, row)] = (rng.choice(types), [])
    for (col, row), (t, _) in list(spec.items()):
        nxt = [k for k in spec if k[1] == row + 1]
        kids = [k for k in nxt if abs(k[0] - col) <= 1] or nxt[:1]
        spec[(col, row)] = (t, kids if row < 8 else [(9, 9)])
    return _map(spec)


def test_the_choice_is_deterministic_and_independent_of_list_order():
    rng = random.Random(7)
    for _ in range(25):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        hp = rng.choice([20, 40, 60, 75])
        act = rng.choice([1, 2, 3])
        a = _state(m, first_row, hp=hp, act=act)
        b = _state(m, list(reversed(first_row)), hp=hp, act=act)
        ta, tb, ta2 = GreedyTrace(), GreedyTrace(), GreedyTrace()
        pa = _pick(a, trace=ta)
        assert _pick(a, trace=ta2) == pa and ta.to_dict() == ta2.to_dict()
        assert _pick(b, trace=tb) == pa
        # reversing the children lists changes nothing either
        for row in m["rows"]:
            for node in row:
                node["children"] = list(reversed(node["children"]))
        assert _pick(a) == pa


def test_choose_does_not_modify_the_state_or_the_map():
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    before = json.dumps(state, sort_keys=True)
    _pick(state, trace=GreedyTrace())
    assert json.dumps(state, sort_keys=True) == before


# ---- the simulation itself ----

def test_rest_heals_when_below_the_threshold_and_smiths_when_above():
    s = LookaheadMapStrategy()
    rest = [{"col": 0, "row": 3, "type": "RestSite"}]   # floor 4: heals under 75%
    low = s.evaluate_path(rest, 30, 80, 0, 1)
    high = s.evaluate_path(rest, 70, 80, 0, 1)
    assert low.hp_boss == pytest.approx(30 + 0.30 * 80)
    assert "room_reward" not in low.parts
    assert high.hp_boss == pytest.approx(70)            # no heal ...
    assert high.parts["room_reward"] == s.SMITH_REWARD  # ... an upgrade instead


def test_hp_never_exceeds_max_or_drops_below_one():
    s = LookaheadMapStrategy()
    full = s.evaluate_path([{"col": 0, "row": 15, "type": "RestSite"}], 60, 75, 0, 1)
    assert full.hp_boss == pytest.approx(75)
    fights = [{"col": 0, "row": r, "type": "Elite"} for r in range(2, 8)]
    dead = s.evaluate_path(fights, 20, 75, 0, 3)
    assert dead.hp_boss == 1.0 and 0 <= dead.p_boss < 0.01


def test_shops_pay_off_only_with_enough_gold():
    s = LookaheadMapStrategy()
    shop = [{"col": 0, "row": 4, "type": "Shop"}]
    assert s.evaluate_path(shop, 75, 75, 99, 1).parts.get("room_reward", 0) == 0
    assert s.evaluate_path(shop, 75, 75, 100, 1).parts["room_reward"] == s.SHOP_REWARD
    assert s.evaluate_path(shop, 75, 75, 75, 2).parts["room_reward"] == s.SHOP_REWARD   # act 2+: 75
    # gold picked up on the way counts
    walk = [{"col": 0, "row": 2, "type": "Elite"}] + shop
    assert s.evaluate_path(walk, 75, 75, 60, 1).parts["room_reward"] == \
        pytest.approx(s.SHOP_REWARD, rel=0.02)   # (less the elite's small chance of dying first)


def test_unknown_rooms_are_a_small_risk_not_a_fight():
    s = LookaheadMapStrategy()
    e = s.evaluate_path([{"col": 0, "row": 3, "type": "Unknown"}], 40, 75, 0, 2)
    assert e.hp_boss == pytest.approx(40 - s.UNKNOWN_COST)
    assert "fight_danger" not in e.parts and "elite_danger" not in e.parts


def test_value_is_the_sum_of_its_parts():
    s = LookaheadMapStrategy()
    path = [{"col": 0, "row": r, "type": t}
            for r, t in enumerate(["Monster", "Elite", "RestSite", "Shop", "Unknown", "Treasure"], 2)]
    e = s.evaluate_path(path, 55, 75, 120, 2)
    assert e.value == pytest.approx(sum(e.parts.values()))
    assert e.types == "ME R$?T".replace(" ", "")
    assert e.elites == 1 and e.rests == 1


# ---- the trace ----

def test_the_trace_scores_every_choice_and_explains_the_pick():
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    t = GreedyTrace()
    assert _pick(state, trace=t) == (1, 1)
    d = json.loads(json.dumps(t.to_dict()))            # JSON-serialisable
    assert [o["index"] for o in d["options"]] == [0, 1]
    for o in d["options"]:
        assert o["kind"] == "node" and o["status"] == "ok"
        # lower is better on the map panel: the score is the route's cost, the
        # sum of the (negated) parts
        assert o["score"] == pytest.approx(sum(c["value"] for c in o["components"]))
        assert {"col", "row", "type", "path", "hp_boss", "elites", "rests", "paths"} <= set(o["detail"])
    assert d["options"][1]["score"] < d["options"][0]["score"]
    assert d["options"][0]["detail"]["path"] == "MERR" and d["options"][0]["detail"]["elites"] == 1
    (rule,) = d["rules"]
    assert rule["key"] == "map.la_best_path" and rule["outcome"] == "fired"
    v = rule["values"]
    assert v["best_index"] == 1 and v["tied_best"] == [1] and v["n_choices"] == 2
    assert v["path"] == "MRMR" and v["elites"] == 0 and v["rests"] == 2
    assert v["decider"] == "elite_danger" and v["margin"] > 0
    assert v["act"] == 3 and v["hp_ratio"] == pytest.approx(40 / 75)


def test_tracing_does_not_change_the_pick():
    rng = random.Random(3)
    for _ in range(20):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        state = _state(m, first_row, hp=rng.choice([25, 50, 75]), act=rng.choice([1, 2, 3]))
        assert _pick(state) == _pick(state, trace=GreedyTrace())


def test_the_trace_keys_are_all_registered():
    from agent.greedy_trace import TRACE_KEYS
    assert set(LookaheadMapStrategy.PART_KEYS.values()) <= TRACE_KEYS
    assert {"map.la_best_path", "map.la_fallback"} <= TRACE_KEYS


# ---- greedy_action / STS2_MAP_POLICY ----

def _corridor_state():
    return _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)


def test_map_policy_defaults_to_hpaware_and_ignores_a_map(monkeypatch):
    monkeypatch.delenv("STS2_MAP_POLICY", raising=False)
    state = _corridor_state()
    assert combat_env._map_policy() == "hpaware"
    assert _picked(combat_env.greedy_action(state)) == (0, 1)
    assert combat_env.greedy_action(state) == combat_env._map_strategy.choose(state, state["choices"])
    for blank in ("", "  "):
        monkeypatch.setenv("STS2_MAP_POLICY", blank)
        assert combat_env._map_policy() == "hpaware"
        assert _picked(combat_env.greedy_action(state)) == (0, 1)


def test_map_policy_lookahead_uses_the_map(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", " Lookahead ")
    state = _corridor_state()
    assert _picked(combat_env.greedy_action(state)) == (1, 1)
    t = GreedyTrace()
    combat_env.greedy_action(state, trace=t)
    assert t.rules[0]["key"] == "map.la_best_path"


def test_map_policy_lookahead_without_a_map_is_hpaware(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", "lookahead")
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3, with_map=False)
    assert combat_env.greedy_action(state) == combat_env._map_strategy.choose(state, state["choices"])


def test_the_map_policy_argument_beats_the_environment(monkeypatch):
    state = _corridor_state()
    monkeypatch.setenv("STS2_MAP_POLICY", "hpaware")
    assert _picked(combat_env.greedy_action(state, map_policy="lookahead")) == (1, 1)
    monkeypatch.setenv("STS2_MAP_POLICY", "lookahead")
    assert _picked(combat_env.greedy_action(state, map_policy="hpaware")) == (0, 1)
    monkeypatch.setenv("STS2_MAP_POLICY", "not-a-policy")
    assert _picked(combat_env.greedy_action(state, map_policy="hpaware")) == (0, 1)


def test_an_unknown_map_policy_raises(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", "smart")
    with pytest.raises(ValueError, match="STS2_MAP_POLICY"):
        combat_env.greedy_action(_corridor_state())
    monkeypatch.delenv("STS2_MAP_POLICY")
    with pytest.raises(ValueError, match="STS2_MAP_POLICY"):
        combat_env.greedy_action(_corridor_state(), map_policy="smart")


def test_other_decisions_do_not_read_the_map_policy(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", "smart")      # would raise if read
    state = {"decision": "rest_site", "player": {"hp": 10, "max_hp": 80, "deck": []},
             "floor": 5, "options": [{"index": 0, "option_id": "HEAL", "is_enabled": True},
                                     {"index": 1, "option_id": "SMITH", "is_enabled": True}]}
    assert combat_env.greedy_action(state)["action"] == "choose_option"
