"""LookaheadMapStrategy v2 (STS2_LOOKAHEAD_MODEL=v2): gold-aware whole-path routing.

Spec: docs/superpowers/specs/2026-11-04-lookahead-gold-aware-routing-design.md.
Hermetic: hand-built maps in the engine's get_map shape (the fixtures of
test_lookahead_map_strategy.py), no game DLLs.
"""
import json
import random

import pytest

from agent import combat_env
from agent.greedy_trace import GreedyTrace
from agent.route_planner import enumerate_paths
from agent.strategy import LookaheadMapStrategy, lookahead_model
from tests.agent.test_lookahead_map_strategy import (
    CORRIDOR, _ACT2_ELITE_OR_MONSTER, _map, _pick, _picked, _random_map, _state)

L = LookaheadMapStrategy
V1, V2 = L(model="v1"), L(model="v2")


def _path(*types, row0=3):
    return [{"col": 0, "row": row0 + i, "type": t} for i, t in enumerate(types)]


def _rule(trace):
    return next(r for r in trace.rules if r["key"] == "map.la_best_path")["values"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("STS2_LOOKAHEAD_MODEL", raising=False)
    monkeypatch.delenv("STS2_MAP_POLICY", raising=False)


# ---- STS2_LOOKAHEAD_MODEL ----

@pytest.mark.parametrize("raw, model", [(None, "v1"), ("", "v1"), ("  ", "v1"), ("v1", "v1"),
                                        ("V2", "v2"), (" v2 ", "v2")])
def test_model_resolution(monkeypatch, raw, model):
    if raw is not None:
        monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", raw)
    assert lookahead_model() == model
    assert L()._model() == model


@pytest.mark.parametrize("bad", ["v3", "2", "gold", "off"])
def test_a_bad_model_raises_naming_the_variable(monkeypatch, bad):
    monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", bad)
    with pytest.raises(ValueError, match="STS2_LOOKAHEAD_MODEL"):
        lookahead_model()
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3)
    with pytest.raises(ValueError, match="STS2_LOOKAHEAD_MODEL"):
        L().choose(state, state["choices"])
    # even where nothing is planned (a single offered node)
    lone = _state(CORRIDOR, [(0, 1)], hp=40, act=3)
    with pytest.raises(ValueError, match="STS2_LOOKAHEAD_MODEL"):
        L().choose(lone, lone["choices"])
    with pytest.raises(ValueError, match="STS2_LOOKAHEAD_MODEL"):
        combat_env.greedy_action(state, map_policy="lookahead")


def test_the_constructor_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", "v2")
    assert L(model="v1")._model() == "v1" and L()._model() == "v2"
    monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", "garbage")
    assert L(model="v2")._model() == "v2"       # a pinned model never reads the variable
    with pytest.raises(ValueError, match="STS2_LOOKAHEAD_MODEL"):
        L(model="v7")


def test_greedy_action_follows_the_environment_model(monkeypatch):
    # act 3, 1000 gold: two shops vs two treasures -- v1 takes the treasures, v2 the shops
    m = _SHOPS_OR_TREASURES
    state = {**_state(m, [(0, 1), (1, 1)], hp=75, max_hp=80, act=3, gold=1000), "ascension": 9}
    monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", "v1")
    assert _picked(combat_env.greedy_action(state, map_policy="lookahead")) == (1, 1)
    monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", "v2")
    assert _picked(combat_env.greedy_action(state, map_policy="lookahead")) == (0, 1)


# ---- v1 is untouched ----

def _fingerprint(strategy, state):
    t = GreedyTrace()
    cmd = strategy.choose(state, state["choices"], trace=t)
    return cmd, json.dumps(t.to_dict(), sort_keys=True)


def test_v1_is_identical_with_and_without_the_variable_set(monkeypatch):
    rng = random.Random(21)
    for _ in range(40):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        state = _state(m, first_row, hp=rng.choice([20, 40, 60, 75]), act=rng.choice([1, 2, 3]),
                       gold=rng.choice([0, 60, 120, 500, 1000]))
        state["ascension"] = rng.choice([0, 3, 6, 9])
        state["player"]["name"] = rng.choice(["The Ironclad", "The Necrobinder", "The Silent"])
        monkeypatch.delenv("STS2_LOOKAHEAD_MODEL", raising=False)
        unset = _fingerprint(L(), state)
        for raw in ("", "v1", "V1"):
            monkeypatch.setenv("STS2_LOOKAHEAD_MODEL", raw)
            assert _fingerprint(L(), state) == unset
        assert _fingerprint(L(model="v1"), state) == unset
        # the v1 trace has none of the v2 keys, and no gold parts
        rule = json.loads(unset[1])["rules"][0]["values"]
        assert not {"candidates", "gold_spent", "gold_left", "shops", "character_mult",
                    "model"} & set(rule)
        for o in json.loads(unset[1])["options"]:
            assert not {"gold_spent", "gold_left", "shops"} & set(o["detail"])
            assert not {"map.la_shop", "map.la_gold_left"} & {c["key"] for c in o["components"]}


def test_v1_values_are_pinned():
    # numbers produced by the pre-v2 planner (git HEAD 5f47e0a) for these inputs
    path = _path("Monster", "Elite", "RestSite", "Shop", "Unknown", "Treasure", "Shop", row0=2)
    ev = V1.evaluate_path(path, 55, 75, 120, 2)
    assert ev.types == "MER$?T$" and ev.elites == 1 and ev.rests == 1
    assert (ev.gold_spent, ev.gold_left, ev.shops) == (0.0, 0.0, 0)
    assert ev.value == pytest.approx(-69.2180698075, rel=1e-12)
    assert ev.parts == pytest.approx({
        "room_reward": 30.897981825000002, "elite_reward": 4.826358810000001,
        "elite_danger": -26.91663, "fight_danger": -1.825, "boss_danger": -92.07207576,
        "hp_end": 15.8712953175}, rel=1e-12)
    assert list(ev.parts) == ["room_reward", "elite_reward", "elite_danger", "fight_danger",
                              "boss_danger", "hp_end"]
    assert ev.hp_boss == pytest.approx(57.0) and ev.p_boss == pytest.approx(0.928145925)
    ev9 = V1.evaluate_path(path, 55, 75, 120, 2, 9)
    assert ev9.value == pytest.approx(-204.06209222083714, rel=1e-12)
    # the shop is a plain 10 points and empties the purse, whatever the gold
    for gold in (100, 400, 1000):
        s = V1.evaluate_path(_path("Shop"), 75, 75, gold, 1)
        assert s.parts["room_reward"] == 10.0 and set(s.parts) <= set(L.V1_PART_KEYS)
    two = V1.evaluate_path(_path("Shop", "Shop"), 75, 75, 1000, 1)
    assert two.parts["room_reward"] == 10.0              # the second shop finds an empty purse
    # v1 ignores the character
    elite = _path("Elite")
    assert V1.evaluate_path(elite, 60, 80, 0, 2, 9, character="The Ironclad") == \
        V1.evaluate_path(elite, 60, 80, 0, 2, 9)
    assert L.COST_BANDS["a8+"][0][2] == 8.2 and L.COST_BANDS["a8+"][1][2] == 27.6


# ---- 1. gold along the route ----

# left: two shops; right: two treasures (no HP cost either side)
_SHOPS_OR_TREASURES = _map({
    (0, 1): ("Shop", [(0, 2)]), (1, 1): ("Treasure", [(1, 2)]),
    (0, 2): ("Shop", [(0, 3)]), (1, 2): ("Treasure", [(1, 3)]),
    (0, 3): ("RestSite", [(9, 9)]), (1, 3): ("RestSite", [(9, 9)]),
})
# the same, against two unknown rooms (an event costs a little HP and earns gold)
_SHOPS_OR_EVENTS = _map({
    (0, 1): ("Shop", [(0, 2)]), (1, 1): ("Unknown", [(1, 2)]),
    (0, 2): ("Shop", [(0, 3)]), (1, 2): ("Unknown", [(1, 3)]),
    (0, 3): ("RestSite", [(9, 9)]), (1, 3): ("RestSite", [(9, 9)]),
})


def test_with_1000_gold_a_two_shop_route_beats_a_shopless_one():
    for act in (1, 2, 3):
        state = {**_state(_SHOPS_OR_EVENTS, [(1, 1), (0, 1)], hp=75, max_hp=80, act=act,
                          gold=1000), "ascension": 9}
        assert _pick(state, V2) == (0, 1), act
    state = {**_state(_SHOPS_OR_TREASURES, [(1, 1), (0, 1)], hp=75, max_hp=80, act=3,
                      gold=1000), "ascension": 9}
    assert _pick(state, V1) == (1, 1)          # v1: a shop is worth 10 once, a relic 15 twice
    assert _pick(state, V2) == (0, 1)


def test_a_second_shop_is_worth_what_the_first_left():
    two = V2.evaluate_path(_path("Shop", "Shop"), 75, 75, 1000, 3)
    one = V2.evaluate_path(_path("Shop"), 75, 75, 1000, 3)
    assert one.shops == 1 and two.shops == 2
    assert two.gold_spent == pytest.approx(800.0) and two.gold_left == pytest.approx(200.0)
    assert two.parts["shop"] == pytest.approx(2 * V2.shop_value(400.0))
    assert two.parts["shop"] > one.parts["shop"]
    # the second visit sees 600 gold: 0.7 x 600 = 420 -> capped to 400
    # with 400 gold the first shop spends 280, the second sees 120 and spends 84
    poor = V2.evaluate_path(_path("Shop", "Shop"), 75, 75, 400, 3)
    assert poor.gold_spent == pytest.approx(280 + 84) and poor.shops == 2
    # ... and below the threshold the second shop is just a room
    broke = V2.evaluate_path(_path("Shop", "Shop"), 75, 75, 100, 3)
    assert broke.shops == 1 and broke.gold_spent == pytest.approx(70.0)   # 30 left < 75


def test_shop_value_curve():
    assert V2.shop_value(0) == 0.0
    assert V2.shop_value(150) == pytest.approx(12.0)
    assert V2.shop_value(400) == pytest.approx(22.0)
    assert V2.shop_value(70) == pytest.approx(5.6)
    assert V2.shop_value(210) == pytest.approx(12.0 + 0.04 * 60)


@pytest.mark.parametrize("gold, act, spend", [
    (1000, 1, 400.0),      # capped
    (572.0, 2, 400.0),     # 0.7 x 572 = 400.4
    (300, 1, 210.0),       # 70%
    (100, 1, 70.0),        # the act-1 threshold
    (75, 2, 52.5),         # the act-2+ threshold
    (75, 3, 52.5),
])
def test_shop_spend_is_70_percent_capped_at_400(gold, act, spend):
    ev = V2.evaluate_path(_path("Shop"), 75, 75, gold, act)
    assert ev.shops == 1
    assert ev.gold_spent == pytest.approx(spend)
    assert ev.gold_left == pytest.approx(gold - spend)
    assert ev.parts["shop"] == pytest.approx(V2.shop_value(spend))   # p = 1
    assert "room_reward" not in ev.parts       # a v2 shop is not a room_reward


@pytest.mark.parametrize("gold, act", [(99, 1), (74, 2), (74, 3), (0, 1)])
def test_a_shop_below_its_gold_threshold_is_not_visited(gold, act):
    ev = V2.evaluate_path(_path("Shop"), 75, 75, gold, act)
    assert ev.shops == 0 and ev.gold_spent == 0.0 and "shop" not in ev.parts


def test_a_shop_pays_with_the_survival_probability():
    # a low-HP act-3 elite first: the chance of dying in it discounts the shop after it
    risky = V2.evaluate_path(_path("Elite", "Shop"), 30, 80, 600, 3, 9)
    assert 0.0 < risky.p_boss < 0.95
    spend = min(0.7 * (600 + 30), 400.0)          # the elite's 30 gold is in the purse
    assert risky.gold_spent == pytest.approx(spend)
    assert risky.parts["shop"] == pytest.approx(risky.p_boss * V2.shop_value(spend))
    assert risky.parts["shop"] < V2.evaluate_path(_path("Shop"), 30, 80, 630, 3, 9).parts["shop"]


def test_leftover_gold_is_worth_3_percent_in_acts_1_and_2_and_nothing_in_act_3():
    rest = _path("RestSite", row0=3)
    for act in (1, 2):
        ev = V2.evaluate_path(rest, 80, 80, 500, act)        # full HP: smith, no heal
        assert ev.gold_left == pytest.approx(500.0)
        assert ev.parts["gold_left"] == pytest.approx(0.03 * 500)
    for act in (3, 4):
        ev = V2.evaluate_path(rest, 80, 80, 500, act)
        assert ev.gold_left == pytest.approx(500.0)
        assert "gold_left" not in ev.parts
    # it is discounted by the chance of surviving to the boss, like hp_end
    risky = V2.evaluate_path(_path("Elite", row0=3), 20, 80, 500, 2, 9)
    assert 0 < risky.p_boss < 1
    assert risky.parts["gold_left"] == pytest.approx(risky.p_boss * 0.03 * risky.gold_left)


def test_spending_beats_carrying():
    # same purse, same act: the route through a shop ends richer in value than the one that
    # walks past it (by the shop's 0.08/0.04 rate against the 0.03 carried)
    for act in (1, 2):
        walk = V2.evaluate_path(_path("RestSite", "RestSite"), 80, 80, 400, act)
        shop = V2.evaluate_path(_path("Shop", "RestSite"), 80, 80, 400, act)
        assert shop.value > walk.value


def test_v2_gold_gains_per_room_type():
    def left(types, act):
        return V2.evaluate_path(_path(*types), 80, 80, 0, act).gold_left
    # Monster 12, Elite 30, Treasure 40; Unknown 12 (acts 1-2) / 7 (act 3)
    assert left(["Monster"], 1) == 12.0
    assert left(["Elite"], 1) == 30.0
    assert left(["Treasure"], 1) == 40.0
    assert left(["Unknown"], 1) == 12.0 and left(["Unknown"], 2) == 12.0
    assert left(["Unknown"], 3) == 7.0
    assert left(["RestSite"], 2) == 0.0
    # v1's table is untouched
    assert V1.GOLD_GAIN == {"Monster": 16.0, "Elite": 44.0, "Treasure": 55.0}


def test_at_low_hp_an_act_3_shop_route_through_an_elite_still_loses():
    # shops behind an act-3 elite at 25-40/80 HP, 1000 gold: a safe shopless route wins
    m = _map({
        (0, 1): ("Monster", [(0, 2)]), (1, 1): ("Monster", [(1, 2)]),
        (0, 2): ("Elite", [(0, 3)]),   (1, 2): ("Monster", [(1, 3)]),
        (0, 3): ("Shop", [(0, 4)]),    (1, 3): ("RestSite", [(1, 4)]),
        (0, 4): ("Shop", [(9, 9)]),    (1, 4): ("Monster", [(9, 9)]),
    })
    for hp in (25, 30, 40):
        state = {**_state(m, [(0, 1), (1, 1)], hp=hp, max_hp=80, act=3, gold=1000),
                 "ascension": 9}
        t = GreedyTrace()
        assert _pick(state, V2, trace=t) == (1, 1), hp
        cands = _rule(t)["candidates"]
        shop_route = next(c for c in cands if c["shops"] == 2)
        assert shop_route["value"] < cands[0]["value"]     # death risk outweighs ~44 shop points


# ---- 2. costs ----

def test_v2_a8plus_act_3_costs():
    c = L.COST_BANDS_V2
    assert c["a8+"][0] == (3.8, 6.3, 10.7) and c["a8+"][1] == (14.9, 25.1, 34.3)
    assert c["a8+"][2] == L.COST_BANDS["a8+"][2]                     # the boss row
    assert c["base"] == L.COST_BANDS["base"] and c["a6-7"] == L.COST_BANDS["a6-7"]
    assert L.COST_BANDS["a8+"][0][2] == 8.2 and L.COST_BANDS["a8+"][1][2] == 27.6   # v1 kept
    mon = V2.evaluate_path(_path("Monster"), 60, 80, 0, 3, 9)
    assert mon.hp_boss == pytest.approx(60 - 10.7)
    elite = V2.evaluate_path(_path("Elite"), 60, 80, 0, 3, 9)
    assert elite.hp_boss == pytest.approx(60 - 34.3)
    # acts 1-2 and a6-7 unchanged between the models
    for act, asc in ((2, 9), (2, 6), (3, 6), (1, 9)):
        for kind in ("Monster", "Elite"):
            a = V1.evaluate_path(_path(kind), 60, 80, 0, act, asc).hp_boss
            b = V2.evaluate_path(_path(kind), 60, 80, 0, act, asc).hp_boss
            assert a == b, (act, asc, kind)


@pytest.mark.parametrize("name, mult", [
    ("IRONCLAD", 1.54), ("The Ironclad", 1.54), ("the silent", 0.93), ("DEFECT", 0.90),
    ("The Regent", 0.92), ("NECROBINDER", 0.79), ("The Necrobinder", 0.79),
    ("Mystery", 1.0), ("", 1.0), (None, 1.0), (7, 1.0)])
def test_character_multiplier_by_name(name, mult):
    assert L.character_multiplier(name) == mult


def test_the_character_is_read_from_the_decision():
    f = L.state_character
    assert f({"character": "IRONCLAD", "player": {"name": "The Silent"}}) == "IRONCLAD"
    assert f({"player": {"name": "The Silent"}}) == "The Silent"
    assert f({"character": None, "player": {"name": "The Regent"}}) == "The Regent"
    assert f({"player": {}}) is None and f({}) is None


def test_the_multiplier_scales_monster_and_elite_costs_not_the_boss():
    kw = dict(hp=70, max_hp=80, gold=0, act=2, ascension=9)
    base_e = V2.evaluate_path(_path("Elite"), kw["hp"], kw["max_hp"], 0, 2, 9)
    iron_e = V2.evaluate_path(_path("Elite"), kw["hp"], kw["max_hp"], 0, 2, 9, character="The Ironclad")
    necro_e = V2.evaluate_path(_path("Elite"), kw["hp"], kw["max_hp"], 0, 2, 9, character="The Necrobinder")
    assert iron_e.hp_boss == pytest.approx(70 - 25.1 * 1.54)
    assert necro_e.hp_boss == pytest.approx(70 - 25.1 * 0.79)
    # more risk for Ironclad, less for Necrobinder (elite_danger is a negative part)
    assert iron_e.parts["elite_danger"] < base_e.parts["elite_danger"] < \
        necro_e.parts.get("elite_danger", 0.0)
    assert iron_e.value < base_e.value < necro_e.value
    mon = V2.evaluate_path(_path("Monster"), 70, 80, 0, 2, 9, character="The Ironclad")
    assert mon.hp_boss == pytest.approx(70 - 6.3 * 1.54)
    # the boss is not scaled: no rooms, same boss_danger / hp_end
    iron_b = V2.evaluate_path([], 40, 80, 0, 2, 9, character="The Ironclad")
    base_b = V2.evaluate_path([], 40, 80, 0, 2, 9)
    assert iron_b == base_b


def test_the_multiplier_applies_only_in_the_a6_7_and_a8plus_tables():
    elite = _path("Elite")
    # act 1 and ascension <= 5 use the base table at every ascension: never scaled
    for act, asc in ((1, 9), (1, 7), (1, None), (2, 5), (2, 0), (2, None), (3, 3)):
        assert V2.evaluate_path(elite, 60, 80, 0, act, asc, character="The Ironclad") == \
            V2.evaluate_path(elite, 60, 80, 0, act, asc), (act, asc)
    # a6-7 and a8+ acts 2-3 are
    for act, asc in ((2, 6), (2, 7), (3, 7), (2, 8), (3, 10)):
        assert V2.evaluate_path(elite, 60, 80, 0, act, asc, character="The Ironclad").hp_boss < \
            V2.evaluate_path(elite, 60, 80, 0, act, asc).hp_boss, (act, asc)
    # an unknown character is not scaled
    assert V2.evaluate_path(elite, 60, 80, 0, 2, 9, character="Mystery") == \
        V2.evaluate_path(elite, 60, 80, 0, 2, 9)


def test_ironclad_avoids_an_elite_route_that_a_necrobinder_takes():
    # a7, act 2, 75/80 HP: the elite is affordable for the lightly-hit characters only
    def pick(name):
        state = {**_state(_ACT2_ELITE_OR_MONSTER, [(0, 1), (1, 1)], hp=75, max_hp=80, act=2),
                 "ascension": 7}
        if name:
            state["player"]["name"] = name
        return _pick(state, V2)
    assert pick("The Necrobinder") == (0, 1)        # elite
    assert pick(None) == (0, 1)
    assert pick("The Ironclad") == (1, 1)           # 16.6 HP of elite is too much
    # v1 has no character: all three agree
    state = {**_state(_ACT2_ELITE_OR_MONSTER, [(0, 1), (1, 1)], hp=75, max_hp=80, act=2),
             "ascension": 7}
    picks = set()
    for name in ("The Ironclad", "The Necrobinder", None):
        s = json.loads(json.dumps(state))
        if name:
            s["player"]["name"] = name
        picks.add(_pick(s, V1))
    assert len(picks) == 1


def test_a_character_key_wins_over_the_player_name():
    state = {**_state(_ACT2_ELITE_OR_MONSTER, [(0, 1), (1, 1)], hp=75, max_hp=80, act=2),
             "ascension": 7, "character": "IRONCLAD"}
    state["player"]["name"] = "The Necrobinder"
    assert _pick(state, V2) == (1, 1)


# ---- 3. candidates and the trace ----

def _all_values(m, choices, state, strategy=V2):
    out = []
    for c in choices:
        for p in enumerate_paths(m, c):
            ev = strategy.evaluate_path(p, state["player"]["hp"], state["player"]["max_hp"],
                                        state["player"]["gold"], state["act"], state.get("ascension"),
                                        "v2", strategy.state_character(state))
            out.append((ev.value, c, ev))
    return out


def test_candidates_are_the_three_best_routes_over_all_offered_nodes():
    rng = random.Random(5)
    checked = 0
    for _ in range(25):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        state = {**_state(m, first_row, hp=rng.choice([30, 50, 75]), max_hp=75,
                          act=rng.choice([1, 2, 3]), gold=rng.choice([0, 150, 1000])),
                 "ascension": 9}
        if len(first_row) < 2:
            continue
        t = GreedyTrace()
        _pick(state, V2, trace=t)
        values = _rule(t)
        cands = values["candidates"]
        everything = _all_values(m, first_row, state)
        want = sorted((v for v, _, _ in everything), reverse=True)[:3]
        assert len(cands) == min(3, len(everything))
        assert [c["value"] for c in cands] == [pytest.approx(w, abs=0.006) for w in want]
        assert [c["value"] for c in cands] == sorted((c["value"] for c in cands), reverse=True)
        for c in cands:
            assert set(c) == {"path", "first", "hp_boss_ratio", "gold_spent", "gold_left",
                              "elites", "shops", "value"}
            assert tuple(c["first"]) in set(first_row)
            assert c["path"][0] in "ME$?TR" and c["elites"] == c["path"].count("E")
        # the chosen route is as good as the best candidate (a tie may pick another)
        assert cands[0]["value"] == pytest.approx(
            max(v for v, _, _ in everything), abs=0.006)
        checked += 1
    assert checked >= 10


def test_candidates_are_capped_at_three_and_json_serialisable():
    m = _map({
        (0, 1): ("Monster", [(0, 2), (1, 2)]), (1, 1): ("Unknown", [(1, 2), (2, 2)]),
        (0, 2): ("Monster", [(0, 3)]), (1, 2): ("Shop", [(0, 3), (1, 3)]),
        (2, 2): ("Elite", [(1, 3)]),
        (0, 3): ("RestSite", [(9, 9)]), (1, 3): ("Monster", [(9, 9)]),
    })
    state = {**_state(m, [(0, 1), (1, 1)], hp=70, max_hp=80, act=2, gold=300), "ascension": 9}
    t = GreedyTrace()
    _pick(state, V2, trace=t)
    cands = _rule(t)["candidates"]
    assert len(cands) == 3
    assert len(json.dumps(t.to_dict())) > 0
    json.loads(json.dumps(t.to_dict()))


def test_the_v2_trace_carries_the_gold_fields_and_the_model():
    state = {**_state(_SHOPS_OR_EVENTS, [(1, 1), (0, 1)], hp=75, max_hp=80, act=3, gold=1000),
             "ascension": 9, "character": "IRONCLAD"}
    t = GreedyTrace()
    assert _pick(state, V2, trace=t) == (0, 1)
    v = _rule(t)
    assert v["model"] == "v2" and v["shops"] == 2
    assert v["gold_spent"] == pytest.approx(800.0) and v["gold_left"] == pytest.approx(200.0)
    assert v["character_mult"] == 1.54
    assert v["path"] == "$$RR" and v["candidates"][0]["first"] == [0, 1]
    assert v["candidates"][0]["shops"] == 2 and v["candidates"][0]["gold_spent"] == 800.0
    d = t.to_dict()
    details = {(o["detail"]["col"], o["detail"]["row"]): o["detail"] for o in d["options"]}
    assert details[(0, 1)]["shops"] == 2 and details[(0, 1)]["gold_spent"] == 800.0
    assert details[(1, 1)]["shops"] == 0 and details[(1, 1)]["gold_left"] > 1000.0
    # the options' components are the negated parts; the score is their sum
    for o in d["options"]:
        assert o["score"] == pytest.approx(sum(c["value"] for c in o["components"]))
    keys = {c["key"] for o in d["options"] for c in o["components"]}
    assert "map.la_shop" in keys
    # act 3: carried gold earns nothing, so no gold_left component anywhere
    assert "map.la_gold_left" not in keys


def test_the_trace_reports_the_multiplier_actually_applied():
    def mult(act, asc, character="IRONCLAD"):
        state = {**_state(_ACT2_ELITE_OR_MONSTER, [(0, 1), (1, 1)], hp=75, max_hp=80, act=act),
                 "character": character}
        if asc is not None:
            state["ascension"] = asc
        t = GreedyTrace()
        _pick(state, V2, trace=t)
        return _rule(t)["character_mult"]
    assert mult(2, 9) == 1.54 and mult(2, 7) == 1.54
    assert mult(1, 9) == 1.0 and mult(2, 3) == 1.0 and mult(2, None) == 1.0
    assert mult(2, 9, "Mystery") == 1.0


def test_leftover_gold_appears_as_a_component_in_act_2():
    state = {**_state(_SHOPS_OR_EVENTS, [(1, 1), (0, 1)], hp=75, max_hp=80, act=2, gold=1000),
             "ascension": 9}
    t = GreedyTrace()
    _pick(state, V2, trace=t)
    keys = {c["key"] for o in t.to_dict()["options"] for c in o["components"]}
    assert {"map.la_shop", "map.la_gold_left"} <= keys
    assert {"map.la_shop", "map.la_gold_left"} <= set(L.PART_KEYS.values())


def test_tracing_does_not_change_the_v2_pick():
    rng = random.Random(9)
    for _ in range(25):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        state = {**_state(m, first_row, hp=rng.choice([25, 50, 75]), act=rng.choice([1, 2, 3]),
                          gold=rng.choice([0, 200, 1000])), "ascension": rng.choice([0, 7, 10])}
        assert _pick(state, V2) == _pick(state, V2, trace=GreedyTrace())


def test_v2_is_deterministic_and_independent_of_list_order():
    rng = random.Random(12)
    for _ in range(25):
        m = _random_map(rng)
        first_row = [(n["col"], n["row"]) for n in m["rows"][1]]
        kw = dict(hp=rng.choice([30, 60, 75]), act=rng.choice([1, 2, 3]),
                  gold=rng.choice([0, 200, 1000]))
        a = {**_state(m, first_row, **kw), "ascension": 9}
        b = {**_state(m, list(reversed(first_row)), **kw), "ascension": 9}
        ta, tb = GreedyTrace(), GreedyTrace()
        assert _pick(a, V2, trace=ta) == _pick(b, V2, trace=tb)
        assert ta.to_dict()["rules"][0]["values"]["candidates"] == \
            tb.to_dict()["rules"][0]["values"]["candidates"]


def test_v2_does_not_modify_the_state_or_the_map():
    state = {**_state(_SHOPS_OR_EVENTS, [(1, 1), (0, 1)], hp=75, max_hp=80, act=3, gold=1000),
             "ascension": 9}
    before = json.dumps(state, sort_keys=True)
    _pick(state, V2, trace=GreedyTrace())
    assert json.dumps(state, sort_keys=True) == before


def test_v2_without_a_map_is_still_hpaware():
    from agent.strategy import HpAwareMapStrategy
    state = _state(CORRIDOR, [(0, 1), (1, 1)], hp=40, act=3, with_map=False)
    assert V2.choose(state, state["choices"]) == HpAwareMapStrategy().choose(state, state["choices"])
    t = GreedyTrace()
    V2.choose(state, state["choices"], trace=t)
    assert t.rules[0] == {"key": "map.la_fallback", "outcome": "fired",
                          "values": {"reason": "no_map"}}


def test_the_decider_can_be_the_shop():
    # equal risk, the shop route wins by the shop part alone
    m = _map({
        (0, 1): ("Shop", [(0, 2)]), (1, 1): ("Unknown", [(1, 2)]),
        (0, 2): ("RestSite", [(9, 9)]), (1, 2): ("RestSite", [(9, 9)]),
    })
    state = {**_state(m, [(0, 1), (1, 1)], hp=60, max_hp=80, act=3, gold=800), "ascension": 9}
    t = GreedyTrace()
    assert _pick(state, V2, trace=t) == (0, 1)
    assert _rule(t)["decider"] == "shop"
