"""The in-game mod attaches ``decision["map"]`` (src/Sts2Advisor/DecisionCapture.cs
FullMap) in the shape of the headless engine's get_map reply (RunSimulator.GetFullMap):
this pins that a map written the way the mod writes it is accepted by the route
planner's helpers and drives LookaheadMapStrategy through the advisor's own
entry point (STS2_MAP_POLICY=lookahead -> greedy_action -> advisor_server.advise).

The map below is hand-written from the C# (keys, types, null/false cases), then
round-tripped through JSON like the HTTP request. Mod shape:

  {"type": "map", "context": {act, floor, ...}, "rows": [[node, ...], ...],
   "boss": {"col", "row", "type": "Boss", "id"?}, "current_coord": {"col","row"} | null}
  node = {"col", "row", "type": PointType.ToString(), "children": [{"col","row"}, ...] | null,
          "visited": bool, "current": bool}
Rows are the rows that have at least one node (earlier, already-travelled rows
included); the boss is not a node of rows and is a child of the last row's nodes.
"""
import json

import pytest

from agent import advisor_server
from agent.combat_env import greedy_action
from agent.greedy_trace import reset_greedy_context
from agent.route_planner import enumerate_paths, fixed_rows, node_index, node_key
from agent.strategy import LookaheadMapStrategy


def _node(col, row, kind, children, visited=False, current=False):
    return {"col": col, "row": row, "type": kind,
            "children": None if children is None else [{"col": c, "row": r} for c, r in children],
            "visited": visited, "current": current}


BOSS = (3, 16)


def _mod_map():
    """Player stands on (2, 1) (row 1, visited); next offered: (1, 2) and (3, 2).
    The left one is a corridor into an Elite; the right one rests first. Row 15's
    only node leads to the boss, whose coordinate is not in rows."""
    rows = [
        [_node(3, 0, "Ancient", [(2, 1)], visited=True)],
        [_node(2, 1, "Monster", [(1, 2), (3, 2)], visited=True, current=True)],
        [_node(1, 2, "Monster", [(1, 3)]), _node(3, 2, "Monster", [(3, 3)])],
        [_node(1, 3, "Elite", [(1, 15)]), _node(3, 3, "RestSite", [(1, 15)])],
        [_node(1, 15, "RestSite", [BOSS])],
    ]
    return {"type": "map", "context": {"act": 3, "floor": 1, "room_type": "Monster"},
            "rows": rows, "boss": {"col": BOSS[0], "row": BOSS[1], "type": "Boss", "id": "KAISER_CRAB_BOSS"},
            "current_coord": {"col": 2, "row": 1}}


def _decision(hp=40, max_hp=75, act=3, with_map=True):
    d = {"type": "decision", "decision": "map_select",
         "context": {"act": act, "floor": 1, "room_type": "Monster"},
         "choices": [{"col": 1, "row": 2, "type": "Monster"}, {"col": 3, "row": 2, "type": "Monster"}],
         "player": {"hp": hp, "max_hp": max_hp, "gold": 30, "deck": [], "relics": [], "potions": [],
                    "potion_slots": 3},
         "act": act, "act_name": "Glory", "floor": 1}
    if with_map:
        d["map"] = _mod_map()
    return json.loads(json.dumps(d))   # what the sidecar receives


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("STS2_MAP_POLICY", raising=False)
    reset_greedy_context()
    yield
    reset_greedy_context()


def test_route_planner_helpers_accept_the_mod_map():
    m = _decision()["map"]
    index = node_index(m)
    assert set(index) == {(3, 0), (2, 1), (1, 2), (3, 2), (1, 3), (3, 3), (1, 15)}
    # the boss is not a node of rows; paths end at the last row's node
    paths = enumerate_paths(m, (1, 2))
    assert [[node_key(n) for n in p] for p in paths] == [[(1, 2), (1, 3), (1, 15)]]
    assert [n["type"] for n in paths[0]] == ["Monster", "Elite", "RestSite"]
    assert fixed_rows(m) == {0: "Ancient", 1: "Monster", 2: "Monster", 15: "RestSite"}
    # a node with null children (the C# writes null when Children is null) ends a path
    m["rows"][4][0]["children"] = None
    assert [len(p) for p in enumerate_paths(m, (3, 2))] == [3]


def test_lookahead_strategy_plans_the_whole_path_on_the_mod_map():
    d = _decision()
    cmd = LookaheadMapStrategy().choose(d, d["choices"])
    # 40/75 HP in act 3: the left corridor ends in an Elite; the right rests first
    assert cmd["args"] == {"col": 3, "row": 2}


def test_advisor_reply_shows_the_lookahead_rules_with_the_env_policy(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", "lookahead")
    reply = advisor_server.advise(_decision(), greedy_action, [])
    assert reply["action"]["args"] == {"col": 3, "row": 2}
    keys = [r["key"] for r in reply["rules"]]
    assert "map.la_best_path" in keys and "map.la_fallback" not in keys
    assert reply["warnings"] == []
    json.dumps(reply, allow_nan=False)


def test_hpaware_default_ignores_the_map_and_keeps_its_rules():
    reply = advisor_server.advise(_decision(), greedy_action, [])
    keys = [r["key"] for r in reply["rules"]]
    assert "map.pick_min_priority" in keys
    assert not any(k.startswith("map.la_") for k in keys)
    assert reply["action"]["args"] == {"col": 1, "row": 2}   # tie on the next node: list order


def test_lookahead_without_a_map_falls_back_and_says_so(monkeypatch):
    monkeypatch.setenv("STS2_MAP_POLICY", "lookahead")
    reply = advisor_server.advise(_decision(with_map=False), greedy_action, [])
    keys = [r["key"] for r in reply["rules"]]
    assert "map.la_fallback" in keys and "map.la_best_path" not in keys


def test_configured_lookahead_is_not_a_scary_knob_warning():
    assert advisor_server.knob_warnings({"STS2_MAP_POLICY": "lookahead"}) == []
