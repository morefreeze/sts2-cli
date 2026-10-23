"""HpAwareMapStrategy reads the act from `act`, not from the act-local floor.
context.floor / state.floor are act-local (RunState.ActFloor, 1-based per act)."""
from agent.greedy_trace import GreedyTrace
from agent.strategy import Act1SafeStrategy, HpAwareMapStrategy


def _state(act, floor, hp=60, max_hp=80, gold=0):
    return {"decision": "map_select", "act": act, "floor": floor,
            "context": {"act": act, "floor": floor},
            "player": {"hp": hp, "max_hp": max_hp, "gold": gold}}


def _components(state, choices):
    trace = GreedyTrace()
    HpAwareMapStrategy().choose(state, choices, trace=trace)
    return {o["index"]: [c["key"] for c in o["components"]]
            for o in trace.to_dict()["options"]}


ELITE_MONSTER = [{"col": 0, "row": 1, "type": "Elite"}, {"col": 1, "row": 1, "type": "Monster"}]


def test_act1_late_floors_use_act1_rules():
    comps = _components(_state(act=1, floor=16, hp=76), ELITE_MONSTER)[0]
    assert "map.elite_act2_healthy" not in comps and "map.elite_act2" not in comps
    assert "map.elite_pre_boss" in comps
    assert "map.elite_act2_boss_zone" not in comps


def test_act2_early_floor_uses_act2_rules():
    comps = _components(_state(act=2, floor=5, hp=56), ELITE_MONSTER)[0]
    assert "map.elite_act2" in comps


def test_act2_and_act3_boss_zone_from_floor_13():
    assert "map.elite_act2_boss_zone" in _components(_state(act=2, floor=13), ELITE_MONSTER)[0]
    assert "map.elite_act2_boss_zone" in _components(_state(act=3, floor=14), ELITE_MONSTER)[0]
    assert "map.elite_act2_boss_zone" not in _components(_state(act=2, floor=12), ELITE_MONSTER)[0]


def test_act1_floor_13_has_no_act2_boss_zone():
    assert "map.elite_act2_boss_zone" not in _components(_state(act=1, floor=13), ELITE_MONSTER)[0]


def test_shop_gold_threshold_follows_act():
    shop = [{"col": 0, "row": 1, "type": "Shop"}, {"col": 1, "row": 1, "type": "Monster"}]
    assert "map.shop_low_gold" not in _components(_state(act=3, floor=4, gold=80), shop)[0]
    assert "map.shop_low_gold" in _components(_state(act=1, floor=4, gold=80), shop)[0]


def test_missing_act_falls_back_to_act1_rules():
    state = _state(act=1, floor=5, hp=56)
    del state["act"]
    del state["context"]["act"]
    assert "map.elite_act2" not in _components(state, ELITE_MONSTER)[0]


def test_act_is_read_from_context_when_state_has_none():
    state = _state(act=2, floor=5, hp=56)
    del state["act"]
    assert "map.elite_act2" in _components(state, ELITE_MONSTER)[0]


def test_bool_act_is_not_an_act_number():
    state = _state(act=True, floor=5, hp=56)
    assert "map.elite_act2" not in _components(state, ELITE_MONSTER)[0]


def test_base_priority_detail_is_the_type_used_for_the_priority():
    # a node without a "type" is priced as "Unknown" (priority 4); the trace says so
    trace = GreedyTrace()
    HpAwareMapStrategy().choose(_state(act=1, floor=4), [{"col": 0, "row": 1}], trace=trace)
    base = trace.to_dict()["options"][0]["components"][0]
    assert base["key"] == "map.base_priority"
    assert base["value"] == HpAwareMapStrategy.PRIORITY["Unknown"]
    assert base["detail"] == "Unknown"


def _pick_rule(choices, state=None):
    trace = GreedyTrace()
    HpAwareMapStrategy().choose(state or _state(act=1, floor=4), choices, trace=trace)
    (rule,) = trace.fired()
    assert rule["key"] == "map.pick_min_priority"
    return rule["values"]


def test_pick_records_every_choice_tied_for_the_best_priority():
    rest = {"col": 1, "row": 1, "type": "RestSite"}
    monster = {"col": 0, "row": 1, "type": "Monster"}
    values = _pick_rule([monster, rest, dict(rest, col=2)])
    assert values["tied_best"] == [1, 2]
    assert values["best_index"] == 1          # the sort breaks ties by lowest index

    values = _pick_rule([monster, dict(monster, col=2)])
    assert values["tied_best"] == [0, 1] and values["best_index"] == 0

    values = _pick_rule([monster, rest])
    assert values["tied_best"] == [1] and values["best_index"] == 1


def test_pick_keeps_its_existing_keys():
    values = _pick_rule(ELITE_MONSTER)
    assert {"best_index", "hp_ratio", "gold", "floor", "in_act2"} <= set(values)


def test_act1_safe_accepts_a_trace_and_records_nothing():
    state = _state(act=1, floor=4)
    plain = Act1SafeStrategy().choose(state, ELITE_MONSTER)
    trace = GreedyTrace()
    traced = Act1SafeStrategy().choose(state, ELITE_MONSTER, trace=trace)
    assert traced == plain
    assert trace.to_dict() == {"options": [], "rules": []}


def test_act2_healthy_elite_ties_monster_and_node_order_decides():
    # Pins a known, unretuned tie: act 2+, HP >= 90%, Elite 6-1.0 = 5.0 == Monster 5.
    # The lowest index wins. Any retune of this must show up as a diff here.
    state = _state(act=2, floor=8, hp=76, max_hp=80)          # 95% HP
    elite = {"col": 0, "row": 1, "type": "Elite"}
    monster = {"col": 1, "row": 1, "type": "Monster"}

    values = _pick_rule([elite, monster], state)
    assert values["tied_best"] == [0, 1] and values["best_index"] == 0
    chosen = HpAwareMapStrategy().choose(state, [elite, monster])
    assert chosen["args"] == {"col": 0, "row": 1}              # Elite, index 0

    values = _pick_rule([monster, elite], state)
    assert values["tied_best"] == [0, 1] and values["best_index"] == 0
    chosen = HpAwareMapStrategy().choose(state, [monster, elite])
    assert chosen["args"] == {"col": 1, "row": 1}              # Monster, index 0


def test_state_act_wins_over_a_conflicting_context_act():
    state = _state(act=1, floor=5, hp=56)
    state["context"]["act"] = 2
    assert "map.elite_act2" not in _components(state, ELITE_MONSTER)[0]
    state = _state(act=2, floor=5, hp=56)
    state["context"]["act"] = 1
    assert "map.elite_act2" in _components(state, ELITE_MONSTER)[0]
