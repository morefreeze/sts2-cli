"""Phase 3b Task 3: path enumeration and expected-value route choice.

Hermetic: hand-built maps and a hand-built RouteModel (value V = coef . state,
transition cells written per test).
"""
import pytest

from agent.route_model import DELTA_KEYS, RouteModel
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


def _cell(n=30, losses=(), death_hp=(), **delta):
    return {"n": n, "n_died": len(death_hp),
            "mean_delta": {k: float(delta.get(k, 0.0)) for k in DELTA_KEYS},
            "losses": list(losses), "death_hp": list(death_hp)}


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
    # at hp 50, 27 of 30 elite samples lost >= 50: a 90% death risk
    risky = _cell(losses=[10] * 3 + [60] * 27, n_relics=2)
    safe = _cell(hp=-5)
    model = _model({"*|Elite|all": risky, "*|Monster|all": safe}, {"hp": 0.1, "n_relics": 10.0})
    state = _state([(0, 1, "Elite"), (1, 1, "Monster")], hp=50)
    assert model.death_prob(1, "Elite", {"hp": 50, "max_hp": 80}) == pytest.approx(0.9)
    # survive-only value of the risky path is higher (25 vs 4.5) ...
    now = {"hp": 50, "max_hp": 80, "n_relics": 0, "act": 1, "floor": 1, "gf": 1,
           "deck_burst": 0.5, "deck_top8": 6.0}
    assert model.value(model.step(1, "Elite", now), "Defect") > model.value(
        model.step(1, "Monster", now), "Defect")
    # ... but the planner routes around the death risk
    pick = choose_node(m, state, "Defect", model)
    assert (pick["col"], pick["row"]) == (1, 1) and pick["p_alive"] == 1.0


def test_death_risk_lowers_p_alive_and_score_on_a_path():
    m = _map({(0, 1): ("Elite", [(3, 99)])})
    model = _model({"*|Elite|all": _cell(losses=[10] * 3 + [60] * 27)})
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
