"""Phase 3b Task 3: path enumeration and expected-value route choice.

Hermetic: hand-built maps and a hand-built RouteModel (value V = coef . state,
transition cells written per test).
"""
import random
import time

import pytest

from agent.route_model import ACT_FLOORS, CELL_LEVELS, DELTA_KEYS, RouteModel
from agent.route_planner import (
    choose_node, enumerate_paths, fixed_rows, node_key, score_path)


def _map(spec):
    """spec: {(col, row): (type, [child (col, row) ...])} -> a get_map-shaped dict.
    A child such as (3, 99) is not a node of `rows`: it stands for the boss."""
    n_rows = max(row for _, row in spec) + 1
    rows = [[] for _ in range(n_rows)]
    for (col, row), (room_type, children) in sorted(spec.items()):
        rows[row].append({"col": col, "row": row, "type": room_type,
                          "children": [{"col": c, "row": r} for c, r in children],
                          "visited": False, "current": False})
    return {"type": "map", "rows": rows, "boss": {"col": 3, "row": n_rows, "type": "Boss"}}


def _cell(n=30, survivors=(), death_hp=(), **delta):
    """survivors = (pre_hp, hp_lost) pairs; death_hp = pre-room hp of the deaths."""
    return {"n": n, "n_died": len(death_hp),
            "mean_delta": {k: float(delta.get(k, 0.0)) for k in DELTA_KEYS},
            "survivors": [list(p) for p in survivors], "death_hp": list(death_hp)}


def _model(cells, coef=None):
    """V = sum(coef[f] * f) over the given features (no intercept, no scaling)."""
    coef = coef or {"hp": 0.1}
    return RouteModel({
        "version": 1,
        "value": {"features": list(coef), "mean": [0.0] * len(coef), "scale": [1.0] * len(coef),
                  "coef": list(coef.values()), "intercept": 0.0},
        "transitions": {"split": 0.5, "min_n": 20, "cells": cells},
        "meta": {}, "validation": {}})


def _state(choices, hp=50, floor=1, act=1, n_relics=0):
    return {"type": "decision", "decision": "map_select",
            "context": {"act": act, "floor": floor},
            "choices": [{"col": c, "row": r, "type": t} for c, r, t in choices],
            "player": {"hp": hp, "max_hp": 80, "gold": 0, "deck": [],
                       "relics": [{"name": f"R{i}"} for i in range(n_relics)], "potions": []}}


def test_node_key_accepts_dicts_and_pairs():
    assert node_key({"col": 2, "row": 5}) == (2, 5)
    assert node_key([2, 5]) == (2, 5)
    assert node_key((2, 5)) == (2, 5)


def test_fixed_rows_finds_rows_where_every_node_shares_a_type():
    m = _map({(0, 1): ("Monster", [(0, 2), (1, 2)]), (1, 1): ("Elite", [(1, 2)]),
              (0, 2): ("Treasure", [(0, 3)]), (1, 2): ("Treasure", [(0, 3)]),
              (0, 3): ("RestSite", [(3, 99)])})
    fixed = fixed_rows(m)
    assert fixed[2] == "Treasure"
    assert 1 not in fixed          # Monster + Elite
    assert fixed[3] == "RestSite"  # a lone node is uniform too
    assert 0 not in fixed          # row 0 is empty in this map


def test_enumerate_paths_diamond_and_child_formats():
    m = _map({(1, 1): ("Monster", [(0, 2), (2, 2)]),
              (0, 2): ("Elite", [(1, 3)]), (2, 2): ("RestSite", [(1, 3)]),
              (1, 3): ("Shop", [(3, 99)])})
    paths = enumerate_paths(m, (1, 1))
    assert [[n["type"] for n in p] for p in paths] == [
        ["Monster", "Elite", "Shop"], ["Monster", "RestSite", "Shop"]]
    # children given as [col, row] lists work as well
    for row in m["rows"]:
        for node in row:
            node["children"] = [[c["col"], c["row"]] for c in node["children"]]
    assert len(enumerate_paths(m, (1, 1))) == 2
    assert enumerate_paths(m, (7, 7)) == []
    # a leaf start is a one-node path
    assert [[n["type"] for n in p] for p in enumerate_paths(m, (1, 3))] == [["Shop"]]


def test_enumerate_paths_orders_children_by_col_then_row_and_raises_past_the_cap():
    m = _map({(0, 1): ("Monster", [(2, 2), (0, 2), (1, 2)]),
              (0, 2): ("Elite", []), (1, 2): ("Shop", []), (2, 2): ("RestSite", [])})
    assert [p[1]["col"] for p in enumerate_paths(m, (0, 1))] == [0, 1, 2]
    # 2 nodes per row, each linked to both nodes of the next row: 2 ** 16 paths
    ladder = {(c, r): ("Monster", [(0, r + 1), (1, r + 1)] if r < 17 else [])
              for r in range(1, 18) for c in (0, 1)}
    with pytest.raises(ValueError, match="20000"):
        enumerate_paths(_map(ladder), (0, 1))


def test_rest_site_child_beats_a_monster_child_when_hp_is_what_the_value_prizes():
    m = _map({(0, 1): ("Monster", [(3, 99)]), (1, 1): ("RestSite", [(3, 99)])})
    model = _model({"*|Monster|all": _cell(hp=-10), "*|RestSite|all": _cell(hp=20)})
    pick = choose_node(m, _state([(0, 1, "Monster"), (1, 1, "RestSite")]), "Defect", model)
    assert (pick["col"], pick["row"]) == (1, 1)
    # gf 2 after one room + V(hp 70) = 7 for the rest site; 2 + 4 for the monster
    assert pick["score"] == pytest.approx(9.0) and pick["p_alive"] == 1.0
    assert pick["paths"] == 2


def test_elite_relics_are_traded_against_hp_by_the_value_model():
    m = _map({(0, 1): ("Monster", [(3, 99)]), (1, 1): ("Elite", [(3, 99)])})
    cells = {"*|Monster|all": _cell(hp=-5), "*|Elite|all": _cell(hp=-15, n_relics=1)}
    state = _state([(0, 1, "Monster"), (1, 1, "Elite")], hp=60)
    relics_prized = _model(cells, {"hp": 0.1, "n_relics": 3.0})
    relics_cheap = _model(cells, {"hp": 0.1, "n_relics": 0.5})
    assert choose_node(m, state, "Defect", relics_prized)["col"] == 1   # Elite
    assert choose_node(m, state, "Defect", relics_cheap)["col"] == 0    # Monster


def test_death_risk_dominates_a_higher_end_value():
    m = _map({(0, 1): ("Elite", [(3, 99)]), (1, 1): ("Monster", [(3, 99)])})
    # at hp 50, 27 of the 30 elite samples (all entered with 80) lost >= 50: a 90% death risk
    risky = _cell(survivors=[(80, 10)] * 3 + [(80, 60)] * 27, n_relics=2)
    safe = _cell(hp=-5)
    model = _model({"*|Elite|all": risky, "*|Monster|all": safe}, {"hp": 0.1, "n_relics": 10.0})
    state = _state([(0, 1, "Elite"), (1, 1, "Monster")], hp=50)
    assert model.death_prob(1, "Elite", {"hp": 50, "max_hp": 80}) == pytest.approx(0.9)
    # survive-only value of the risky path is higher (25 vs 4.5) ...
    now = {"hp": 50, "max_hp": 80, "n_relics": 0, "act": 1, "floor": 1, "gf": 1,
           "deck_burst": 0.5, "deck_top8": 6.0}
    assert model.value(model.step(1, "Elite", now, 1), "Defect") > model.value(
        model.step(1, "Monster", now, 1), "Defect")
    # ... but the planner routes around the death risk
    pick = choose_node(m, state, "Defect", model)
    assert (pick["col"], pick["row"]) == (1, 1) and pick["p_alive"] == 1.0


def test_death_risk_lowers_p_alive_and_score_on_a_path():
    m = _map({(0, 1): ("Elite", [(3, 99)])})
    model = _model({"*|Elite|all": _cell(survivors=[(80, 10)] * 3 + [(80, 60)] * 27)})
    path = enumerate_paths(m, (0, 1))[0]
    features = {"hp": 50, "max_hp": 80, "act": 1, "floor": 1, "gf": 1}
    scored = score_path(path, features, "Defect", model)
    # 0.9 * die at gf 1 + 1 = 2, 0.1 * (gf 2 + V(hp 50) = 5)
    assert scored["p_alive"] == pytest.approx(0.1)
    assert scored["score"] == pytest.approx(0.9 * 2 + 0.1 * (2 + 5.0))
    assert scored["end"]["gf"] == 2 and scored["checkpoints"] == {}


def test_score_path_records_checkpoints_only_for_requested_rows():
    m = _map({(0, 1): ("Monster", [(0, 2)]), (0, 2): ("RestSite", [(0, 3)]),
              (0, 3): ("Monster", [(3, 99)])})
    model = _model({"*|Monster|all": _cell(hp=-10), "*|RestSite|all": _cell(hp=20)})
    path = enumerate_paths(m, (0, 1))[0]
    features = {"hp": 50, "max_hp": 80, "act": 1, "floor": 1, "gf": 1}
    scored = score_path(path, features, "Defect", model, frozenset({2}))
    assert set(scored["checkpoints"]) == {2}
    assert scored["checkpoints"][2]["hp"] == pytest.approx(60.0)   # 50 - 10 + 20
    assert scored["end"]["hp"] == pytest.approx(50.0)


def test_tie_goes_to_the_lower_col_whatever_the_choice_order():
    m = _map({(2, 1): ("Monster", [(3, 99)]), (5, 1): ("Monster", [(3, 99)])})
    model = _model({"*|Monster|all": _cell(hp=-5)})
    pick = choose_node(m, _state([(5, 1, "Monster"), (2, 1, "Monster")]), "Defect", model)
    assert (pick["col"], pick["row"]) == (2, 1)


def test_choose_node_returns_checkpoints_for_the_fixed_rows_the_chosen_path_crosses():
    m = _map({(0, 1): ("Monster", [(0, 2), (1, 2)]), (1, 1): ("RestSite", [(1, 2)]),
              (0, 2): ("Treasure", [(0, 3)]), (1, 2): ("Treasure", [(0, 3)]),
              (0, 3): ("RestSite", [(3, 99)])})
    model = _model({"*|Monster|all": _cell(hp=-10, gold=5), "*|RestSite|all": _cell(hp=20),
                    "*|Treasure|all": _cell(n_relics=1, deck_burst=0.1, deck_top8=0.5)})
    pick = choose_node(m, _state([(0, 1, "Monster"), (1, 1, "RestSite")]), "Defect", model)
    assert (pick["col"], pick["row"]) == (1, 1)  # rest first, both paths end resting
    assert set(pick["checkpoints"]) == {"2", "3"}  # row 1 mixes types: not a checkpoint row
    cp2, cp3 = pick["checkpoints"]["2"], pick["checkpoints"]["3"]
    assert set(cp2) == {"hp", "max_hp", "deck_burst", "deck_top8", "n_relics", "n_potions",
                      "gold"}
    assert cp2["hp"] == pytest.approx(70.0) and cp2["n_relics"] == pytest.approx(1.0)
    assert cp2["deck_burst"] == pytest.approx(0.1) and cp2["deck_top8"] == pytest.approx(0.5)
    assert cp3["hp"] == pytest.approx(80.0)  # 70 + 20, clamped to max_hp 80
    assert isinstance(pick["score"], float) and pick["p_alive"] == 1.0


def test_boss_choice_is_scored_as_the_empty_path():
    m = _map({(0, 1): ("Monster", [(3, 99)])})
    model = _model({})
    pick = choose_node(m, _state([(3, 16, "Boss")], hp=50, floor=16), "Defect", model)
    assert (pick["col"], pick["row"]) == (3, 16)
    assert pick["score"] == pytest.approx(16 + 5.0)   # gf + V
    assert pick["paths"] == 1 and pick["checkpoints"] == {}


def test_choices_missing_from_the_map_are_ignored_and_none_scoreable_gives_none():
    m = _map({(0, 1): ("Monster", [(3, 99)])})
    model = _model({"*|Monster|all": _cell(hp=-5)})
    pick = choose_node(m, _state([(4, 1, "Monster"), (0, 1, "Monster")]), "Defect", model)
    assert (pick["col"], pick["row"]) == (0, 1)
    assert choose_node(m, _state([(4, 1, "Monster")]), "Defect", model) is None
    assert choose_node(m, _state([]), "Defect", model) is None


def test_death_term_uses_the_floor_of_the_room_that_kills():
    # Act 2 opens at floor 0 (gf 17). The row-0 Ancient is floor 1 of the act: dying in it ends
    # the run at gf 18, whatever the current state's floor.
    m = _map({(3, 0): ("Ancient", [(3, 99)])})
    model = _model({"*|Ancient|all": _cell(death_hp=[70] * 30)})   # every Ancient entered at <= 70 died
    features = {"hp": 50, "max_hp": 80, "act": 2, "floor": 0, "gf": 17}
    scored = score_path(enumerate_paths(m, (3, 0))[0], features, "Defect", model)
    assert scored["p_alive"] == 0.0 and scored["score"] == pytest.approx(ACT_FLOORS + 1)


def test_an_act_2_ancient_room_neither_helps_nor_hurts_a_route_through_it():
    # Act 2 map with the real shape: row 0 is the Ancient, rows 1.. the map proper. The Ancient
    # links to both row-1 Monsters, and those Monsters are selectable straight from the start.
    spec = {(3, 0): ("Ancient", [(1, 1), (4, 1)]),
            (1, 1): ("Monster", [(1, 2)]), (4, 1): ("Monster", [(4, 2)]),
            (1, 2): ("RestSite", [(3, 99)]), (4, 2): ("RestSite", [(3, 99)])}
    m = _map(spec)
    assert len(m["rows"][0]) == 1 and m["rows"][0][0]["type"] == "Ancient"
    model = _model({"*|Ancient|all": _cell(), "*|Monster|all": _cell(), "*|RestSite|all": _cell()})
    state = _state([(3, 0, "Ancient"), (1, 1, "Monster"), (4, 1, "Monster")], hp=50, floor=0, act=2)
    features = {"hp": 50, "max_hp": 80, "act": 2, "floor": 0, "gf": ACT_FLOORS}
    via_ancient = score_path(enumerate_paths(m, (3, 0))[0], features, "Defect", model)
    direct = score_path(enumerate_paths(m, (1, 1))[0], features, "Defect", model)
    # zero-delta cells: both paths reach row 2 and end on the same floor with the same state.
    # (Counting rooms instead of reading the row put the longer Ancient path one floor ahead.)
    assert via_ancient["end"]["gf"] == direct["end"]["gf"] == ACT_FLOORS + 3
    assert via_ancient["score"] == pytest.approx(direct["score"])
    pick = choose_node(m, state, "Defect", model)
    assert (pick["col"], pick["row"]) == (1, 1)   # equal scores: lower col wins, not the Ancient


def test_score_path_and_choose_node_report_which_cell_levels_the_path_used():
    m = _map({(0, 1): ("Monster", [(0, 2)]), (0, 2): ("Shop", [(0, 3)]),
              (0, 3): ("Ancient", [(3, 99)])})
    model = _model({"1|Monster|hi": _cell(n=25), "*|Shop|all": _cell(n=60)})
    features = {"hp": 50, "max_hp": 80, "act": 1, "floor": 1, "gf": 1, "deck_burst": 0.9}
    scored = score_path(enumerate_paths(m, (0, 1))[0], features, "Defect", model)
    # Monster: exact act cell; Shop: only the all-acts cell exists; Ancient: nothing -> zero
    assert scored["cell_levels"] == {"act": 1, "pooled": 1, "type": 0, "zero": 1}
    pick = choose_node(m, _state([(0, 1, "Monster")], floor=1), "Defect", model)
    # the state's empty deck has deck_burst 0 -> the "lo" Monster bucket, which has no cell:
    # Monster and Ancient resolve to zero, only the Shop finds its all-acts cell
    assert pick["cell_levels"] == {"act": 0, "pooled": 1, "type": 0, "zero": 2}
    assert set(pick["cell_levels"]) == set(CELL_LEVELS)


def test_choose_node_on_a_real_size_map_with_big_cells_is_fast():
    # ~15 rows deep; rows 1-9 are two nodes wide and fully linked (2 ** 8 = 256 paths from each
    # start, 512 in all), rows 10-15 a single chain; every cell holds 3000 survivor samples.
    spec = {}
    for row in range(1, 10):
        for col in (0, 1):
            spec[(col, row)] = (("Monster", "Elite", "Unknown", "RestSite")[(row + col) % 4],
                                [(0, row + 1), (1, row + 1)] if row < 9 else [(0, 10)])
    for row in range(10, 16):
        spec[(0, row)] = (("Shop", "Monster", "Unknown")[row % 3], [(0, row + 1)] if row < 15 else [(3, 99)])
    m = _map(spec)
    rng = random.Random(0)
    cells = {f"*|{t}|all": _cell(n=3000, n_died=60, hp=-3.0,
                                 survivors=[(rng.randint(10, 80), rng.randint(0, 25))
                                            for _ in range(2940)],
                                 death_hp=[rng.randint(5, 50) for _ in range(60)])
             for t in ("Monster", "Elite", "Unknown", "RestSite", "Shop")}
    model = _model(cells)
    state = _state([(0, 1, "Monster"), (1, 1, "Elite")], hp=60)
    started = time.perf_counter()
    pick = choose_node(m, state, "Defect", model)
    elapsed = time.perf_counter() - started
    assert pick["paths"] == 512
    assert elapsed < 1.0, f"choose_node took {elapsed:.2f}s on a 512-path map"
