from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
import hashlib
import importlib
import json
import math
import operator
from types import MappingProxyType
from typing import Any, Callable

import pytest

from agent.combat_env import CombatEnv


_BOSS_ROW_BY_ACT = {1: 16, 2: 16, 3: 15, 4: 14}


def _api():
    return importlib.import_module("agent.run_workbench.recorded_maps")


def _player(*, hp: int = 80, add_card: bool = False, add_relic: bool = False):
    deck = [{"id": "STRIKE"}]
    relics = [{"id": "BURNING_BLOOD"}]
    if add_card:
        deck.append({"id": "BASH"})
    if add_relic:
        relics.append({"id": "ANCHOR"})
    return {
        "hp": hp,
        "max_hp": 80,
        "gold": 99,
        "deck": deck,
        "relics": relics,
        "potions": [{"id": "HEALING_POTION"}],
    }


def _route_node(
    col: int,
    row: int,
    room_type: str,
    entry: dict[str, Any],
    exit_: dict[str, Any],
):
    return {
        "col": col,
        "row": row,
        "type": room_type,
        "entry_player": deepcopy(entry),
        "exit_player": deepcopy(exit_),
    }


def _valid_row(*, act: int = 1, ts: float = 10.0):
    before = _player()
    after = _player(hp=72, add_card=True, add_relic=True)
    return {
        "event": "map_snapshot",
        "act": act,
        "is_multiplayer": False,
        "ts": ts,
        "map": {
            "type": "map",
            "context": {"act": act},
            "rows": [
                [{
                    "col": 0,
                    "row": 0,
                    "type": "Ancient",
                    "children": [{"col": 0, "row": 1}, {"col": 1, "row": 1}],
                    "visited": True,
                    "current": False,
                }],
                [
                    {
                        "col": 0,
                        "row": 1,
                        "type": "MonsterRoom",
                        "children": [{"col": 1, "row": 2}],
                        "visited": True,
                        "current": False,
                    },
                    {
                        "col": 1,
                        "row": 1,
                        "type": "Merchant",
                        "children": [{"col": 1, "row": 2}],
                        "visited": False,
                        "current": False,
                    },
                ],
                [{
                    "col": 1,
                    "row": 2,
                    "type": "Elite",
                    "children": [{"col": 1, "row": 16}],
                    "visited": True,
                    "current": True,
                }],
            ],
            "boss": {"col": 1, "row": 16, "type": "BossRoom", "id": "BOSS.TEST"},
            "current_coord": {"col": 1, "row": 2},
        },
        "visited_nodes": [
            _route_node(0, 0, "Ancient", before, after),
            _route_node(0, 1, "Monster", after, after),
            _route_node(1, 2, "Elite", after, after),
        ],
    }


def _recorded_decision(
    *,
    kind: str = "event",
    selected_id: str = "TAKE",
    selected_label: str = "拿走",
    effect: str | None = "获得 25 金币。",
):
    return {
        "kind": kind,
        "selected_id": selected_id,
        "selected_label": selected_label,
        "options": [{
            "id": selected_id,
            "label": selected_label,
            "effect": effect,
            "selected": True,
        }],
        "evidence": "recorded",
    }


def _route_bytes(route_nodes) -> bytes:
    return json.dumps(
        route_nodes,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _boss_route_row(*, boss_id: str = "BOSS.TEST"):
    row = _valid_row()
    row["map"]["rows"][2][0]["current"] = False
    row["map"]["boss"].update(id=boss_id, visited=True, current=True)
    row["map"]["current_coord"] = {"col": 1, "row": 16}
    previous = row["visited_nodes"][-1]["exit_player"]
    row["visited_nodes"].append(
        _route_node(1, 16, "Boss", previous, previous)
    )
    return row


def _linear_full_act_row(*, act: int = 1, boss_row: int | None = None):
    player = _player()
    boss_row = _BOSS_ROW_BY_ACT[act] if boss_row is None else boss_row
    ordinary = []
    visited_nodes = []
    for row_index in range(boss_row):
        room_type = "Ancient" if row_index == 0 else "Monster"
        ordinary.append([{
            "col": 0,
            "row": row_index,
            "type": room_type,
            "children": [{"col": 0, "row": row_index + 1}],
            "visited": True,
            "current": False,
        }])
        visited_nodes.append(
            _route_node(0, row_index, room_type, player, player)
        )
    visited_nodes.append(_route_node(0, boss_row, "Boss", player, player))
    return {
        "event": "map_snapshot",
        "act": act,
        "is_multiplayer": False,
        "ts": 10.0,
        "map": {
            "type": "map",
            "context": {"act": act},
            "rows": ordinary,
            "boss": {
                "col": 0,
                "row": boss_row,
                "type": "Boss",
                "id": "BOSS.LINEAR",
                "visited": True,
                "current": True,
            },
            "current_coord": {"col": 0, "row": boss_row},
        },
        "visited_nodes": visited_nodes,
    }


def _move_shop(row, *, col=None, map_row=None) -> None:
    shop = row["map"]["rows"][1][1]
    child = row["map"]["rows"][0][0]["children"][1]
    if col is not None:
        shop["col"] = col
        child["col"] = col
    if map_row is not None:
        shop["row"] = map_row
        child["row"] = map_row


def test_parse_recorded_map_row_builds_existing_map_and_delta_contracts() -> None:
    api = _api()
    row = _valid_row()
    expected_start = deepcopy(row["visited_nodes"][0]["entry_player"])
    expected_end = deepcopy(row["visited_nodes"][0]["exit_player"])

    parsed = api.parse_recorded_map_row(row)

    assert parsed.act_index == 0
    assert parsed.act_id == "RECORDED.ACT.1"
    assert parsed.act_map.act_id == parsed.act_id
    assert parsed.act_map.full_map is True
    assert parsed.act_map.visited_route is True
    assert parsed.act_map.fallback_reason is None
    assert [node.id for node in parsed.act_map.nodes] == [
        "recorded:0:0",
        "recorded:0:1",
        "recorded:1:1",
        "recorded:1:2",
        "recorded:1:16",
    ]
    assert [node.room_type for node in parsed.act_map.nodes] == [
        "Ancient", "Monster", "Shop", "Elite", "Boss",
    ]
    assert [edge.to_dict() for edge in parsed.act_map.edges] == [
        {"from": "recorded:0:0", "to": "recorded:0:1"},
        {"from": "recorded:0:0", "to": "recorded:1:1"},
        {"from": "recorded:0:1", "to": "recorded:1:2"},
        {"from": "recorded:1:1", "to": "recorded:1:2"},
        {"from": "recorded:1:2", "to": "recorded:1:16"},
    ]
    assert parsed.act_map.alignment.ok is True
    assert parsed.act_map.alignment.ambiguous is False
    assert parsed.act_map.alignment.path_node_ids == (
        "recorded:0:0", "recorded:0:1", "recorded:1:2",
    )
    assert [node.visited for node in parsed.act_map.nodes] == [True, True, False, True, False]
    assert [node.path_index for node in parsed.act_map.nodes] == [0, 1, None, 2, None]

    first = parsed.route_nodes[0]
    assert first == {
        "id": "a0:n0",
        "act": 1,
        "act_index": 0,
        "floor": 1,
        "global_floor": 1,
        "col": 0,
        "row": 0,
        "map_point_type": "Ancient",
        "room_type": "Ancient",
        "start_player": expected_start,
        "end_player": expected_end,
        "deltas": first["deltas"],
    }
    assert first["deltas"]["cards_gained"] == {
        "value": [{"id": "BASH"}], "quality": "derived",
    }
    assert first["deltas"]["relics_gained"] == {
        "value": [{"id": "ANCHOR"}], "quality": "derived",
    }
    assert [node["id"] for node in parsed.route_nodes] == ["a0:n0", "a0:n1", "a0:n2"]
    assert [node["global_floor"] for node in parsed.route_nodes] == [1, 2, 3]
    json.dumps(
        {"map": parsed.act_map.to_dict(), "route": parsed.route_nodes},
        allow_nan=False,
    )


def test_valid_decisions_roundtrip_detach_and_survive_dataclass_operations() -> None:
    api = _api()
    row = _valid_row()
    decision = _recorded_decision()
    row["visited_nodes"][1]["decisions"] = [decision]
    expected = deepcopy(row["visited_nodes"][1]["decisions"])

    parsed = api.parse_recorded_map_row(row)
    replaced = replace(parsed, act_id="RECORDED.ACT.REPLACED")
    copied = deepcopy(parsed)
    serialized = asdict(parsed)
    first_view = parsed.route_nodes
    second_view = parsed.route_nodes
    json_payload = json.dumps(
        parsed.route_nodes, ensure_ascii=False, allow_nan=False
    )

    assert parsed.route_nodes[1]["decisions"] == expected
    assert replaced.route_nodes[1]["decisions"] == expected
    assert copied.route_nodes[1]["decisions"] == expected
    assert serialized["route_nodes"][1]["decisions"] == expected
    assert first_view is not second_view
    assert first_view[1]["decisions"] is not second_view[1]["decisions"]
    assert type(parsed.route_nodes[1]["decisions"]) is list
    assert type(parsed.route_nodes[1]["decisions"][0]) is dict
    assert '"decisions"' in json_payload

    decision["selected_label"] = "已篡改"
    decision["options"][0]["effect"] = "已篡改"
    row["visited_nodes"][1]["decisions"].append(_recorded_decision())
    parsed.route_nodes[1]["decisions"][0]["options"][0]["effect"] = "输出篡改"
    serialized["route_nodes"][1]["decisions"].clear()
    copied.route_nodes[1]["decisions"].clear()
    replaced.route_nodes[1]["decisions"].clear()

    assert parsed.route_nodes[1]["decisions"] == expected
    assert copied.route_nodes[1]["decisions"] == expected
    assert replaced.route_nodes[1]["decisions"] == expected


def test_absent_decisions_preserves_exact_legacy_route_bytes() -> None:
    api = _api()

    payload = _route_bytes(api.parse_recorded_map_row(_valid_row()).route_nodes)

    assert b'"decisions"' not in payload
    assert len(payload) == 3797
    assert hashlib.sha256(payload).hexdigest() == (
        "5ab715489e2603eb4001028075e36ba3a2a0c18a9eb911a1cebd374e47f1a1cc"
    )


def test_boss_visited_node_decisions_roundtrip_with_model_identity() -> None:
    api = _api()
    row = _boss_route_row()
    row["visited_nodes"][-1]["decisions"] = [
        _recorded_decision(
            kind="relic",
            selected_id="BOSS_RELIC",
            selected_label="首领遗物",
            effect="获得一件首领遗物。",
        )
    ]

    parsed = api.parse_recorded_map_row(row)

    assert parsed.route_nodes[-1]["model_id"] == "BOSS.TEST"
    assert parsed.route_nodes[-1]["decisions"][0]["kind"] == "relic"
    assert parsed.route_nodes[-1]["decisions"][0]["selected_id"] == "BOSS_RELIC"


def test_unvisited_graph_decisions_are_not_authoritative_or_exposed() -> None:
    api = _api()
    row = _valid_row()
    row["map"]["rows"][1][1]["decisions"] = [_recorded_decision()]

    parsed = api.parse_recorded_map_row(row)

    assert all("decisions" not in node for node in parsed.route_nodes)
    assert all(not hasattr(node, "decisions") for node in parsed.act_map.nodes)


class _HostileDecisionString(str):
    def __eq__(self, _other):
        raise RuntimeError("DECISION_SECRET /private/inventory")


class _HostileDecisionDict(dict):
    def items(self):
        raise RuntimeError("DECISION_SECRET /private/inventory")


class _HostileDecisionMapping(Mapping):
    def __getitem__(self, _key):
        raise RuntimeError("DECISION_SECRET /private/inventory")

    def __iter__(self):
        raise RuntimeError("DECISION_SECRET /private/inventory")

    def __len__(self):
        raise RuntimeError("DECISION_SECRET /private/inventory")


class _HostileDecisionKey:
    def __hash__(self):
        return hash("id")

    def __eq__(self, _other):
        raise RuntimeError("DECISION_SECRET /private/inventory")


def _malformed_decisions(case: str):
    decision = _recorded_decision()
    if case == "invalid-kind":
        decision["kind"] = "combat"
        return [decision]
    if case == "zero-selected":
        decision["options"][0]["selected"] = False
        return [decision]
    if case == "two-selected":
        decision["options"].append({
            "id": "LEAVE",
            "label": "离开",
            "effect": None,
            "selected": True,
        })
        return [decision]
    if case == "33-options":
        decision["selected_id"] = "ID0"
        decision["selected_label"] = "选项0"
        decision["options"] = [
            {
                "id": f"ID{index}",
                "label": f"选项{index}",
                "effect": None,
                "selected": index == 0,
            }
            for index in range(33)
        ]
        return [decision]
    if case == "non-list":
        return {"not": "a list"}
    if case == "str-subclass":
        decision["selected_label"] = _HostileDecisionString("拿走")
        return [decision]
    if case == "lone-surrogate":
        decision["options"][0]["effect"] = "\ud800"
        return [decision]
    if case == "effect-513":
        decision["options"][0]["effect"] = "效" * 513
        return [decision]
    if case == "dict-subclass":
        return [_HostileDecisionDict(decision)]
    if case == "hostile-nested-mapping":
        decision["options"][0] = _HostileDecisionMapping()
        return [decision]
    if case == "hostile-nested-key":
        decision["options"][0] = {_HostileDecisionKey(): "TAKE"}
        return [decision]
    if case == "17-decisions":
        return [deepcopy(decision) for _index in range(17)]
    if case == "default-writer-32k-overflow":
        oversized = _recorded_decision(
            selected_id="牌" * 256,
            selected_label="选" * 256,
            effect="效" * 512,
        )
        decisions = [deepcopy(oversized) for _index in range(16)]
        encoded = json.dumps(
            decisions, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        assert len(encoded) > 32 * 1024
        return decisions
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "invalid-kind",
        "zero-selected",
        "two-selected",
        "33-options",
        "non-list",
        "str-subclass",
        "lone-surrogate",
        "effect-513",
        "dict-subclass",
        "hostile-nested-mapping",
        "hostile-nested-key",
        "17-decisions",
        "default-writer-32k-overflow",
    ],
)
def test_malformed_decisions_fail_closed_without_losing_other_valid_rows(
    case: str,
) -> None:
    api = _api()
    invalid = _valid_row(ts=2.0)
    invalid["visited_nodes"][1]["decisions"] = _malformed_decisions(case)

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(invalid)
    snapshots, errors = api.latest_recorded_acts(
        iter((_valid_row(ts=1.0), invalid))
    )

    assert 0 < len(str(raised.value)) <= 160
    assert set(snapshots) == {0}
    assert all("decisions" not in node for node in snapshots[0].route_nodes)
    assert len(errors) == 1
    assert 0 < len(errors[0]) <= 160
    if case in {
        "str-subclass",
        "dict-subclass",
        "hostile-nested-mapping",
        "hostile-nested-key",
    }:
        assert str(raised.value) == "invalid recorded map row"
        assert errors == ("row 1: invalid recorded map row",)
    combined = str(raised.value) + repr(errors)
    assert "DECISION_SECRET" not in combined
    assert "/private/inventory" not in combined


@pytest.mark.parametrize(
    ("act", "route_length", "boss_row"),
    [(1, 17, 16), (2, 17, 16), (3, 16, 15), (4, 15, 14)],
)
def test_act_geometry_preserves_canonical_global_floor_slots(
    act: int, route_length: int, boss_row: int
) -> None:
    api = _api()

    parsed = api.parse_recorded_map_row(_linear_full_act_row(act=act))

    assert len(parsed.route_nodes) == route_length
    assert parsed.route_nodes[0]["global_floor"] == (act - 1) * 17 + 1
    assert parsed.route_nodes[-1]["global_floor"] == (act - 1) * 17 + route_length
    assert parsed.route_nodes[-1]["row"] == boss_row
    assert parsed.route_nodes[-1]["model_id"] == "BOSS.LINEAR"


@pytest.mark.parametrize(
    ("act", "wrong_boss_row"),
    [(1, 15), (2, 15), (3, 16), (4, 15)],
)
def test_act_geometry_rejects_a_coherent_graph_with_the_wrong_boss_row(
    act: int, wrong_boss_row: int
) -> None:
    api = _api()

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(
            _linear_full_act_row(act=act, boss_row=wrong_boss_row)
        )


def test_eighteen_node_route_is_rejected_before_it_can_claim_alignment() -> None:
    api = _api()
    row = _linear_full_act_row()
    row["visited_nodes"].append(deepcopy(row["visited_nodes"][-1]))

    with pytest.raises(api.RecordedMapError, match="17"):
        api.parse_recorded_map_row(row)


def _break_authoritative_graph(row, case: str) -> None:
    if case == "int32-max-col":
        _move_shop(row, col=2**31 - 1)
    elif case == "col-seven":
        _move_shop(row, col=7)
    elif case == "negative-col":
        _move_shop(row, col=-1)
    elif case == "int32-max-row":
        _move_shop(row, map_row=2**31 - 1)
    elif case == "ordinary-row-sixteen":
        _move_shop(row, col=2, map_row=16)
    elif case == "ordinary-boss":
        row["map"]["rows"][2][0]["type"] = "Boss"
        row["visited_nodes"][2]["type"] = "Boss"
    elif case == "boss-wrong-row":
        row["map"]["boss"]["row"] = 15
        row["map"]["rows"][2][0]["children"][0]["row"] = 15
    elif case == "boss-shop":
        row["map"]["boss"]["type"] = "Shop"
    elif case == "back-edge":
        row["map"]["rows"][2][0]["children"].append({"col": 0, "row": 1})
    elif case == "no-ancient":
        row["map"]["rows"][0][0]["type"] = "Monster"
        row["visited_nodes"][0]["type"] = "Monster"
    elif case == "unreachable-node":
        row["map"]["rows"].append([{
            "col": 6,
            "row": 5,
            "type": "Monster",
            "children": [{"col": 1, "row": 16}],
            "visited": False,
            "current": False,
        }])
    elif case == "dead-end-node":
        row["map"]["rows"][1][1]["children"] = []
    else:  # pragma: no cover - guards the test helper itself
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "int32-max-col",
        "col-seven",
        "negative-col",
        "int32-max-row",
        "ordinary-row-sixteen",
        "ordinary-boss",
        "boss-wrong-row",
        "boss-shop",
        "back-edge",
        "no-ancient",
        "unreachable-node",
        "dead-end-node",
    ],
)
def test_invalid_graph_cannot_claim_full_map_or_alignment(case: str) -> None:
    api = _api()
    row = _valid_row()
    _break_authoritative_graph(row, case)

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(row)


def _mutate(mutator: Callable[[dict[str, Any]], None]):
    row = _valid_row()
    mutator(row)
    return row


INVALID_ROWS = [
    ("wrong-event", lambda r: r.update(event="milestone")),
    ("act-zero", lambda r: r.update(act=0)),
    ("act-five", lambda r: r.update(act=5)),
    ("act-bool", lambda r: r.update(act=True)),
    ("multiplayer", lambda r: r.update(is_multiplayer=True)),
    ("multiplayer-nonbool", lambda r: r.update(is_multiplayer=0)),
    ("payload-non-map", lambda r: r.update(map=[])),
    ("payload-wrong-type", lambda r: r["map"].update(type="state")),
    ("context-act-mismatch", lambda r: r["map"]["context"].update(act=2)),
    ("too-many-nodes", lambda r: r["map"].update(rows=[[
        {"col": i, "row": 9, "type": "Monster", "children": [], "visited": False, "current": False}
        for i in range(257)
    ]])),
    ("too-many-edges", lambda r: r["map"]["rows"][0][0].update(
        children=[{"col": 0, "row": 1}] * 2049
    )),
    ("duplicate-coord", lambda r: r["map"]["rows"][1][1].update(col=0)),
    ("duplicate-edge", lambda r: r["map"]["rows"][0][0]["children"].append({"col": 0, "row": 1})),
    ("self-edge", lambda r: r["map"]["rows"][0][0]["children"].append({"col": 0, "row": 0})),
    ("dangling-edge", lambda r: r["map"]["rows"][0][0]["children"].append({"col": 99, "row": 99})),
    ("boss-collision", lambda r: r["map"]["boss"].update(col=1, row=2)),
    ("child-missing-coordinate", lambda r: r["map"]["rows"][0][0]["children"][0].pop("col")),
    ("node-bool-coordinate", lambda r: r["map"]["rows"][0][0].update(col=True)),
    ("node-float-coordinate", lambda r: r["map"]["rows"][0][0].update(col=0.0)),
    ("node-overflow-coordinate", lambda r: r["map"]["rows"][0][0].update(col=2**31)),
    ("child-bool-coordinate", lambda r: r["map"]["rows"][0][0]["children"][0].update(col=False)),
    ("boss-overflow-coordinate", lambda r: r["map"]["boss"].update(row=-(2**31) - 1)),
    ("current-float-coordinate", lambda r: r["map"]["current_coord"].update(row=2.0)),
    ("empty-room-type", lambda r: r["map"]["rows"][0][0].update(type="")),
    ("long-room-type", lambda r: r["map"]["rows"][0][0].update(type="x" * 65)),
    ("nonstring-room-type", lambda r: r["map"]["boss"].update(type=7)),
    ("empty-boss-id", lambda r: r["map"]["boss"].update(id="")),
    ("long-boss-id", lambda r: r["map"]["boss"].update(id="secret/" * 11)),
    ("nonstring-boss-id", lambda r: r["map"]["boss"].update(id=3)),
    ("rows-not-list", lambda r: r["map"].update(rows={})),
    ("row-container-not-list", lambda r: r["map"]["rows"].__setitem__(0, {})),
    ("visited-not-bool", lambda r: r["map"]["rows"][0][0].update(visited=1)),
    ("current-not-bool", lambda r: r["map"]["rows"][0][0].update(current=0)),
    ("boss-visited-not-bool", lambda r: r["map"]["boss"].update(visited=1)),
    ("boss-current-not-bool", lambda r: r["map"]["boss"].update(current=0)),
    ("multiple-current", lambda r: r["map"]["rows"][1][0].update(current=True)),
    ("no-current", lambda r: r["map"]["rows"][2][0].update(current=False)),
    ("inconsistent-current", lambda r: r["map"]["current_coord"].update(col=0, row=1)),
    ("visited-coordinate-absent", lambda r: r["visited_nodes"][1].update(col=8, row=8)),
    ("duplicate-visited-coordinate", lambda r: r["visited_nodes"][1].update(col=0, row=0)),
    ("visited-set-mismatch", lambda r: r["map"]["rows"][1][0].update(visited=False)),
    ("route-disconnected", lambda r: r["map"]["rows"][0][0]["children"].pop(0)),
    ("route-out-of-order", lambda r: r["visited_nodes"].__setitem__(
        slice(0, 2), r["visited_nodes"][1::-1]
    )),
    ("route-room-mismatch", lambda r: r["visited_nodes"][1].update(type="Shop")),
    ("visited-nodes-not-list", lambda r: r.update(visited_nodes={})),
    ("entry-missing", lambda r: r["visited_nodes"][0].pop("entry_player")),
    ("exit-missing", lambda r: r["visited_nodes"][0].pop("exit_player")),
    ("unknown-inventory-field", lambda r: r["visited_nodes"][0]["entry_player"].update(secret="x")),
    ("unsafe-inventory", lambda r: r["visited_nodes"][0]["entry_player"].update(deck=[object()])),
    ("producer-stripped-inventory-field", lambda r: r["visited_nodes"][0]["entry_player"]["deck"][0].update(
        description="not persisted by Task 1"
    )),
    ("nonfinite-inventory", lambda r: r["visited_nodes"][0]["entry_player"].update(hp=math.nan)),
    ("overflow-inventory-number", lambda r: r["visited_nodes"][0]["entry_player"].update(hp=10**10_000)),
    ("oversized-inventory-list", lambda r: r["visited_nodes"][0]["entry_player"].update(
        deck=[{"id": str(i)} for i in range(257)]
    )),
    ("oversized-inventory-structure", lambda r: r["visited_nodes"][0]["entry_player"].update(
        deck=[{"id": "x" * 256, "payload": "y" * 256} for _ in range(256)]
    )),
    ("invalid-ts-string", lambda r: r.update(ts="10")),
    ("invalid-ts-bool", lambda r: r.update(ts=True)),
    ("invalid-ts-nonfinite", lambda r: r.update(ts=math.inf)),
    ("invalid-ts-overflow", lambda r: r.update(ts=10**10_000)),
]


@pytest.mark.parametrize("_name,mutator", INVALID_ROWS, ids=[item[0] for item in INVALID_ROWS])
def test_parse_recorded_map_row_rejects_untrusted_invalid_rows(
    _name: str, mutator: Callable[[dict[str, Any]], None]
) -> None:
    api = _api()
    row = _mutate(mutator)

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(row)

    assert 0 < len(str(raised.value)) <= 160
    assert "secret/" not in str(raised.value)


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"hp": 70, "gold": 12},
        {"deck": [], "relics": [], "potions": []},
    ],
)
def test_player_snapshot_accepts_bounded_known_field_subsets(snapshot) -> None:
    api = _api()
    row = _valid_row()
    for node in row["visited_nodes"]:
        node["entry_player"] = deepcopy(snapshot)
        node["exit_player"] = deepcopy(snapshot)

    parsed = api.parse_recorded_map_row(row)

    assert all(node["start_player"] == snapshot for node in parsed.route_nodes)
    assert all(node["end_player"] == snapshot for node in parsed.route_nodes)


def test_identityless_task1_collections_are_retained_with_conservative_deltas() -> None:
    api = _api()
    row = _valid_row()
    before = {
        "hp": 70,
        "gold": 12,
        "deck": [{"name": "Mystery"}, "opaque", None],
        "relics": [{"name": "Mystery Relic"}],
        "potions": [None],
    }
    after = deepcopy(before)
    after["deck"].append({"name": "Another Mystery"})
    row["visited_nodes"][0]["entry_player"] = before
    row["visited_nodes"][0]["exit_player"] = after

    parsed = api.parse_recorded_map_row(row)

    first = parsed.route_nodes[0]
    assert first["start_player"]["deck"] == before["deck"]
    assert first["end_player"]["deck"] == after["deck"]
    assert first["deltas"]["cards_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert first["deltas"]["relics_gained"] == {
        "value": None,
        "quality": "unknown",
    }


def test_task1_partial_player_sanitizer_output_roundtrips() -> None:
    api = _api()
    partial = CombatEnv._bounded_player_snapshot({
        "player": {
            "hp": 67,
            "max_hp": True,
            "gold": 21,
            "deck": [{"name": "Identityless"}],
            "relics": [{"value": math.nan}],
            "potions": [object()],
        }
    })
    row = _valid_row()
    for node in row["visited_nodes"]:
        node["entry_player"] = deepcopy(partial)
        node["exit_player"] = deepcopy(partial)

    parsed = api.parse_recorded_map_row(row)

    assert partial == {"hp": 67, "gold": 21, "deck": [{"name": "Identityless"}]}
    assert parsed.route_nodes[0]["start_player"] == partial
    assert parsed.route_nodes[0]["deltas"]["cards_gained"]["quality"] == "unknown"


def _producer_full_act_reply(*, act: int, current_row: int) -> dict[str, Any]:
    """Exact Task 1 input shape, including marker-free boss metadata."""

    boss_row = _BOSS_ROW_BY_ACT[act]
    boss_current = current_row == boss_row
    rows = []
    for row_index in range(boss_row):
        rows.append([{
            "col": 0,
            "row": row_index,
            "type": "Ancient" if row_index == 0 else "Monster",
            "children": [{"col": 0, "row": row_index + 1}],
            "visited": row_index <= current_row,
            "current": not boss_current and row_index == current_row,
        }])
    return {
        "type": "map",
        "context": {"act": act},
        "rows": rows,
        "boss": {
            "col": 0,
            "row": boss_row,
            "type": "BossRoom",
            "id": "BOSS.PRODUCER",
            "name": "Producer Boss",
        },
        "current_coord": {"col": 0, "row": current_row},
    }


@pytest.mark.parametrize(
    ("act", "boss_row", "route_length"),
    [(1, 16, 17), (2, 16, 17), (3, 15, 16), (4, 14, 15)],
)
def test_task1_real_full_capture_roundtrips_act_geometry_boss_and_partial_players(
    act: int, boss_row: int, route_length: int
) -> None:
    api = _api()
    env = CombatEnv(dry_run=True, run_context={"capture_map": True})
    replies = iter(
        _producer_full_act_reply(act=act, current_row=row_index)
        for row_index in range(boss_row + 1)
    )
    env._send_read_only = lambda _command: next(replies)

    for row_index in range(boss_row + 1):
        assert env._capture_run_map_state({
            "player": {
                "hp": 80 - row_index,
                "gold": 20 + row_index,
                "deck": [{"name": "Identityless"}],
            },
        }) is True

    recorded_row = env._serialized_run_map_snapshots()[0]
    assert "visited" not in _producer_full_act_reply(
        act=act, current_row=boss_row
    )["boss"]
    assert recorded_row["map"]["boss"]["visited"] is True
    assert recorded_row["map"]["boss"]["current"] is True

    parsed = api.parse_recorded_map_row(recorded_row)

    assert len(parsed.route_nodes) == route_length
    assert parsed.route_nodes[-1]["row"] == boss_row
    assert parsed.route_nodes[-1]["model_id"] == "BOSS.PRODUCER"
    assert parsed.route_nodes[-1]["start_player"] == {
        "hp": 80 - boss_row,
        "gold": 20 + boss_row,
        "deck": [{"name": "Identityless"}],
    }
    assert parsed.route_nodes[-1]["deltas"]["cards_gained"]["quality"] == "unknown"


def test_parse_recorded_map_row_detaches_input_and_has_frozen_outer_contract() -> None:
    api = _api()
    row = _valid_row()
    parsed = api.parse_recorded_map_row(row)

    row["visited_nodes"][0]["entry_player"]["deck"][0]["id"] = "MUTATED"
    row["map"]["rows"][0][0]["children"].clear()

    assert parsed.route_nodes[0]["start_player"]["deck"][0]["id"] == "STRIKE"
    assert len(parsed.act_map.edges) == 5
    output_view = parsed.route_nodes
    output_view[1]["start_player"]["deck"][0]["id"] = "OUTPUT_MUTATION"
    assert parsed.route_nodes[1]["end_player"]["deck"][0]["id"] == "STRIKE"
    assert parsed.route_nodes[1]["start_player"]["deck"][0]["id"] == "STRIKE"
    with pytest.raises(FrozenInstanceError):
        parsed.act_index = 2


def test_route_nodes_field_returns_fresh_ordinary_defensive_views() -> None:
    api = _api()
    parsed = api.parse_recorded_map_row(_valid_row())
    pristine = api.parse_recorded_map_row(_valid_row())
    first_view = parsed.route_nodes
    second_view = parsed.route_nodes

    assert first_view is not second_view
    assert type(first_view[0]) is dict
    assert type(first_view[0]["start_player"]) is dict
    assert type(first_view[0]["start_player"]["deck"]) is list
    assert type(first_view[0]["start_player"]["deck"][0]) is dict

    dict_mutators = [
        lambda node: operator.setitem(node, "extra", True),
        lambda node: operator.delitem(node, "id"),
        lambda node: node.update({"extra": True}),
        lambda node: node.setdefault("extra", True),
        lambda node: node.pop("id"),
        lambda node: node.popitem(),
        lambda node: node.clear(),
        lambda node: operator.ior(node, {"extra": True}),
    ]
    list_mutators = [
        lambda deck: deck.append({"id": "NEW"}),
        lambda deck: deck.extend([{"id": "NEW"}]),
        lambda deck: deck.insert(0, {"id": "NEW"}),
        lambda deck: deck.pop(),
        lambda deck: deck.remove(deck[0]),
        lambda deck: deck.clear(),
        lambda deck: deck.sort(key=lambda item: item["id"]),
        lambda deck: deck.reverse(),
        lambda deck: operator.setitem(deck, 0, {"id": "NEW"}),
        lambda deck: operator.delitem(deck, 0),
        lambda deck: operator.iadd(deck, [{"id": "NEW"}]),
        lambda deck: operator.imul(deck, 2),
    ]
    for mutate in dict_mutators:
        mutate(parsed.route_nodes[0])
        assert parsed == pristine
    for mutate in list_mutators:
        mutate(parsed.route_nodes[0]["start_player"]["deck"])
        assert parsed == pristine

    assert parsed.route_nodes[0]["start_player"]["deck"][0]["id"] == "STRIKE"
    assert parsed.route_nodes[0]["deltas"]["cards_gained"]["value"][0]["id"] == "BASH"
    json.dumps(parsed.route_nodes, ensure_ascii=False, allow_nan=False)


def test_builtin_base_descriptors_cannot_reach_recorded_snapshot_storage() -> None:
    api = _api()
    parsed = api.parse_recorded_map_row(_valid_row())
    pristine = api.parse_recorded_map_row(_valid_row())
    route = parsed.route_nodes

    dict.__setitem__(route[0], "id", "ROOT_MUTATION")
    dict.update(route[0]["start_player"], {"hp": 1})
    list.append(route[0]["start_player"]["deck"], {"id": "APPENDED"})
    list.__setitem__(route[0]["start_player"]["deck"], 0, {"id": "REPLACED"})
    dict.__setitem__(route[0]["deltas"], "hp_before", {"value": 1})
    dict.update(route[0]["deltas"]["cards_gained"], {"quality": "exact"})
    list.append(route[0]["deltas"]["cards_gained"]["value"], {"id": "APPENDED"})
    dict.__setitem__(
        route[0]["deltas"]["cards_gained"]["value"][0],
        "id",
        "NESTED_MUTATION",
    )

    assert parsed == pristine
    assert parsed.route_nodes == pristine.route_nodes
    assert parsed.route_nodes[0]["id"] == "a0:n0"
    assert parsed.route_nodes[0]["start_player"]["hp"] == 80
    assert parsed.route_nodes[0]["start_player"]["deck"] == [{"id": "STRIKE"}]


def test_recorded_snapshot_preserves_exact_standard_dataclass_api() -> None:
    api = _api()
    parsed = api.parse_recorded_map_row(_valid_row())

    assert [item.name for item in fields(parsed)] == [
        "act_index", "act_id", "act_map", "route_nodes",
    ]
    positional = api.RecordedActSnapshot(
        parsed.act_index,
        parsed.act_id,
        parsed.act_map,
        parsed.route_nodes,
    )
    replaced = replace(parsed, act_id="RECORDED.ACT.REPLACED")
    serialized = asdict(parsed)
    deep_copied = deepcopy(parsed)

    assert positional == parsed
    assert replaced.act_id == "RECORDED.ACT.REPLACED"
    assert [item.name for item in fields(replaced)] == [
        "act_index", "act_id", "act_map", "route_nodes",
    ]
    assert set(serialized) == {"act_index", "act_id", "act_map", "route_nodes"}
    assert "_route_nodes_json" not in serialized
    assert deep_copied == parsed
    assert "route_nodes=" in repr(parsed)
    assert type(serialized["route_nodes"][0]) is dict
    operator.setitem(serialized["route_nodes"][0], "extra", True)
    operator.setitem(deep_copied.route_nodes[0], "extra", True)
    operator.setitem(replaced.route_nodes[0], "extra", True)
    assert parsed == positional
    assert deep_copied == parsed
    assert "extra" not in parsed.route_nodes[0]
    assert "extra" not in deep_copied.route_nodes[0]
    assert "extra" not in replaced.route_nodes[0]
    json.dumps(serialized, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize("route_state", ["absent", "empty"])
def test_parse_recorded_map_row_rejects_absent_or_empty_route(route_state: str) -> None:
    api = _api()
    row = _valid_row()
    if route_state == "absent":
        row.pop("visited_nodes")
    else:
        row["visited_nodes"] = []

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(row)


class _SpoofLiteral:
    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return False


@pytest.mark.parametrize("field", ["event", "map.type"])
def test_parse_recorded_map_row_requires_exact_string_discriminators(field: str) -> None:
    api = _api()
    row = _valid_row()
    if field == "event":
        row["event"] = _SpoofLiteral()
    else:
        row["map"]["type"] = _SpoofLiteral()

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(row)


class _BadGet(Mapping):
    def __getitem__(self, _key):
        raise RuntimeError("PRIVATE /local/path inventory")

    def __iter__(self):
        raise RuntimeError("PRIVATE /local/path inventory")

    def __len__(self):
        return 0

    def get(self, _key, _default=None):
        raise RuntimeError("PRIVATE /local/path inventory")

    def items(self):
        raise RuntimeError("PRIVATE /local/path inventory")


def test_public_parser_contains_hostile_root_mapping_exceptions() -> None:
    api = _api()

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(_BadGet())

    assert str(raised.value) == "invalid recorded map row"
    assert "PRIVATE" not in str(raised.value)


class _ForgedRecordedMapError(Mapping):
    def __init__(self, error_type):
        self._error = error_type("SECRET /private/inventory" + "x" * 300)

    def _raise(self):
        raise self._error

    def __getitem__(self, _key):
        self._raise()

    def __iter__(self):
        self._raise()

    def __len__(self):
        return 1

    def get(self, _key, _default=None):
        self._raise()

    def items(self):
        self._raise()


def test_public_parser_normalizes_forged_recorded_map_errors_from_hostile_mapping() -> None:
    api = _api()
    hostile = _ForgedRecordedMapError(api.RecordedMapError)

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(hostile)
    snapshots, errors = api.latest_recorded_acts(iter((hostile,)))

    assert str(raised.value) == "invalid recorded map row"
    assert snapshots == {}
    assert errors == ("row 0: invalid recorded map row",)
    assert "SECRET" not in str(raised.value) + repr(errors)
    assert "/private/inventory" not in str(raised.value) + repr(errors)


class _CollidingRootKey:
    def __init__(self, error_type):
        self._error = error_type("INJECTED /private/inventory" + "x" * 300)

    def __hash__(self):
        return hash("event")

    def __eq__(self, _other):
        raise self._error


def test_root_normalization_rejects_hostile_nonstring_keys_before_lookup() -> None:
    api = _api()
    hostile = {_CollidingRootKey(api.RecordedMapError): "map_snapshot"}

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(hostile)
    snapshots, errors = api.latest_recorded_acts(iter((hostile,)))

    assert str(raised.value) == "invalid recorded map row"
    assert snapshots == {}
    assert errors == ("row 0: invalid recorded map row",)
    assert "INJECTED" not in str(raised.value) + repr(errors)
    assert "/private/inventory" not in str(raised.value) + repr(errors)


class _CollidingNestedKey:
    def __init__(self, target: str, error_type):
        self._target = target
        self._error = error_type("NESTED_SECRET /private/inventory" + "x" * 300)

    def __hash__(self):
        return hash(self._target)

    def __eq__(self, _other):
        raise self._error


def _inject_hostile_nested_key(row, location: str, error_type) -> None:
    if location == "map":
        row["map"] = {_CollidingNestedKey("type", error_type): "map"}
    elif location == "context":
        row["map"]["context"] = {_CollidingNestedKey("act", error_type): 1}
    elif location == "current_coord":
        row["map"]["current_coord"] = {
            _CollidingNestedKey("col", error_type): 1,
            "row": 2,
        }
    elif location == "child":
        row["map"]["rows"][0][0]["children"][0] = {
            _CollidingNestedKey("col", error_type): 0,
            "row": 1,
        }
    elif location == "inventory_identity":
        row["visited_nodes"][0]["entry_player"]["deck"] = [{
            _CollidingNestedKey("id", error_type): "STRIKE",
        }]
    else:  # pragma: no cover - guards the test helper itself
        raise AssertionError(location)


@pytest.mark.parametrize(
    "location",
    ["map", "context", "current_coord", "child", "inventory_identity"],
)
def test_nested_forged_public_errors_are_always_generic(location: str) -> None:
    api = _api()
    row = _valid_row()
    _inject_hostile_nested_key(row, location, api.RecordedMapError)

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(row)
    snapshots, errors = api.latest_recorded_acts(iter((row,)))

    assert str(raised.value) == "invalid recorded map row"
    assert snapshots == {}
    assert errors == ("row 0: invalid recorded map row",)
    combined = str(raised.value) + repr(errors)
    assert "NESTED_SECRET" not in combined
    assert "/private/inventory" not in combined


def _private_collision_row(location: str, error_type) -> dict[Any, Any]:
    row = _valid_row()
    if location == "root":
        return {_CollidingNestedKey("event", error_type): "map_snapshot"}
    if location == "player":
        row["visited_nodes"][0]["entry_player"] = {
            _CollidingNestedKey("hp", error_type): 80,
        }
    elif location == "map":
        row["map"] = {_CollidingNestedKey("type", error_type): "map"}
    elif location == "context":
        row["map"]["context"] = {
            _CollidingNestedKey("act", error_type): 1,
        }
    elif location == "child":
        row["map"]["rows"][0][0]["children"][0] = {
            _CollidingNestedKey("col", error_type): 0,
            "row": 1,
        }
    elif location == "inventory":
        row["visited_nodes"][0]["entry_player"]["deck"] = [{
            _CollidingNestedKey("id", error_type): "STRIKE",
        }]
    else:  # pragma: no cover - guards the test helper itself
        raise AssertionError(location)
    return row


@pytest.mark.parametrize(
    "location", ["root", "player", "map", "context", "child", "inventory"]
)
def test_imported_private_validation_error_cannot_be_forged_by_input(
    location: str,
) -> None:
    api = _api()
    secret = "FORGED_PRIVATE /private/inventory" + "x" * 300
    hostile = _private_collision_row(
        location,
        lambda _message: api._RecordedMapValidationError(secret),
    )

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(hostile)
    snapshots, errors = api.latest_recorded_acts(iter((hostile,)))

    assert str(raised.value) == "invalid recorded map row"
    assert len(str(raised.value)) <= 160
    assert snapshots == {}
    assert errors == ("row 0: invalid recorded map row",)
    assert "FORGED_PRIVATE" not in str(raised.value) + repr(errors)
    assert "/private/inventory" not in str(raised.value) + repr(errors)


class _NonExactString(str):
    pass


def _non_exact_string_key_row(location: str) -> dict[Any, Any]:
    row = _valid_row()
    if location == "root":
        value = row.pop("event")
        row[_NonExactString("event")] = value
    elif location == "player":
        player = row["visited_nodes"][0]["entry_player"]
        value = player.pop("hp")
        player[_NonExactString("hp")] = value
    elif location == "map":
        raw_map = row["map"]
        value = raw_map.pop("type")
        raw_map[_NonExactString("type")] = value
    elif location == "context":
        context = row["map"]["context"]
        value = context.pop("act")
        context[_NonExactString("act")] = value
    elif location == "child":
        child = row["map"]["rows"][0][0]["children"][0]
        value = child.pop("col")
        child[_NonExactString("col")] = value
    elif location == "inventory":
        identity = row["visited_nodes"][0]["entry_player"]["deck"][0]
        value = identity.pop("id")
        identity[_NonExactString("id")] = value
    else:  # pragma: no cover - guards the test helper itself
        raise AssertionError(location)
    return row


@pytest.mark.parametrize(
    "location", ["root", "player", "map", "context", "child", "inventory"]
)
def test_all_untrusted_object_keys_must_be_exact_strings(location: str) -> None:
    api = _api()

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(_non_exact_string_key_row(location))

    assert str(raised.value) == "invalid recorded map row"


def test_parse_recorded_map_row_normalizes_known_variants_and_bounded_unknowns() -> None:
    api = _api()
    row = _valid_row()
    row["map"]["rows"][1][1]["type"] = "RestSiteRoom"
    row["map"]["rows"][2][0]["type"] = "NewFutureRoom"
    row["visited_nodes"][2]["type"] = "NewFutureRoom"

    parsed = api.parse_recorded_map_row(row)

    assert parsed.act_map.nodes[2].room_type == "RestSite"
    assert parsed.act_map.nodes[3].room_type == "Unknown"
    assert parsed.route_nodes[2]["room_type"] == "Unknown"


def test_parse_recorded_map_row_preserves_model_id_for_explicit_boss_visit() -> None:
    api = _api()
    parsed = api.parse_recorded_map_row(_boss_route_row())

    assert parsed.act_map.nodes[-1].visited is True
    assert parsed.act_map.nodes[-1].path_index == 3
    assert parsed.route_nodes[-1]["model_id"] == "BOSS.TEST"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda r: r["map"]["boss"].update(id="\ud800"),
        lambda r: r["map"]["boss"].update(type="\ud800"),
        lambda r: r["map"]["rows"][0][0].update(type="\ud800"),
        lambda r: r["visited_nodes"][0]["entry_player"]["deck"][0].update(id="\ud800"),
    ],
    ids=["boss-id", "boss-type", "room-type", "inventory-id"],
)
def test_parse_recorded_map_row_rejects_lone_surrogates(mutator) -> None:
    api = _api()
    row = _boss_route_row()
    mutator(row)

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(row)


def test_parse_recorded_map_row_preserves_ordinary_unicode() -> None:
    api = _api()
    row = _boss_route_row(boss_id="首领.测试")
    row["visited_nodes"][0]["entry_player"]["deck"][0]["id"] = "打击"

    parsed = api.parse_recorded_map_row(row)

    assert parsed.route_nodes[-1]["model_id"] == "首领.测试"
    assert parsed.route_nodes[0]["start_player"]["deck"][0]["id"] == "打击"
    json.dumps(parsed.route_nodes, ensure_ascii=False, allow_nan=False).encode("utf-8")


@pytest.mark.parametrize(
    "children",
    [
        {},
        [{"col": 1, "row": 3}],
        [{"col": 0}],
        [{"col": 0, "row": 0}, {"col": 0, "row": 0}],
        [{"col": 99, "row": 99}],
    ],
    ids=["non-list", "self", "invalid", "duplicate", "dangling"],
)
def test_parse_recorded_map_row_rejects_boss_children(children) -> None:
    api = _api()
    row = _valid_row()
    row["map"]["boss"]["children"] = children

    with pytest.raises(api.RecordedMapError):
        api.parse_recorded_map_row(row)


def test_parse_recorded_map_row_accepts_absent_or_exact_empty_boss_children() -> None:
    api = _api()
    absent = api.parse_recorded_map_row(_valid_row())
    with_empty = _valid_row()
    with_empty["map"]["boss"]["children"] = []

    empty = api.parse_recorded_map_row(with_empty)

    assert absent.act_map == empty.act_map


def test_latest_recorded_acts_consumes_once_and_selects_latest_valid_per_act() -> None:
    api = _api()
    first = _valid_row(act=1, ts=1.0)
    later = _valid_row(act=1, ts=3.0)
    later["visited_nodes"][0]["exit_player"]["hp"] = 61
    act_two = _valid_row(act=2, ts=2.0)
    consumed = []

    def rows():
        for row in (first, act_two, later):
            consumed.append(row["ts"])
            yield row

    snapshots, errors = api.latest_recorded_acts(rows())

    assert consumed == [1.0, 2.0, 3.0]
    assert set(snapshots) == {0, 1}
    assert snapshots[0].route_nodes[0]["end_player"]["hp"] == 61
    assert snapshots[1].act_id == "RECORDED.ACT.2"
    assert errors == ()


def test_latest_recorded_acts_keeps_valid_snapshot_after_invalid_later_row() -> None:
    api = _api()
    valid = _valid_row(ts=1.0)
    invalid = _valid_row(ts=2.0)
    invalid["map"]["rows"][0][0]["type"] = ""

    snapshots, errors = api.latest_recorded_acts(iter((valid, invalid)))

    assert snapshots[0].route_nodes[0]["start_player"]["hp"] == 80
    assert len(errors) == 1


def test_latest_recorded_acts_preserves_integer_timestamp_order_above_float_precision() -> None:
    api = _api()
    later_timestamp = _valid_row(ts=2**53 + 1)
    later_timestamp["visited_nodes"][0]["exit_player"]["hp"] = 71
    earlier_timestamp = _valid_row(ts=2**53)
    earlier_timestamp["visited_nodes"][0]["exit_player"]["hp"] = 62

    snapshots, errors = api.latest_recorded_acts(
        iter((later_timestamp, earlier_timestamp))
    )

    assert errors == ()
    assert snapshots[0].route_nodes[0]["end_player"]["hp"] == 71


def test_latest_recorded_acts_uses_last_row_for_exact_timestamp_ties() -> None:
    api = _api()
    first = _valid_row(ts=100)
    second = _valid_row(ts=100)
    second["visited_nodes"][0]["exit_player"]["hp"] = 61

    snapshots, errors = api.latest_recorded_acts(iter((first, second)))

    assert errors == ()
    assert snapshots[0].route_nodes[0]["end_player"]["hp"] == 61


def test_latest_recorded_acts_bounds_deterministic_errors_and_never_throws() -> None:
    api = _api()
    malformed = [None, 1, {}, {"event": "not-a-map"}, _BadGet()] * 100

    first = api.latest_recorded_acts(iter(malformed))
    second = api.latest_recorded_acts(iter(deepcopy(malformed)))

    assert first == second
    assert first[0] == {}
    assert 0 < len(first[1]) <= 32
    assert all(0 < len(error) <= 160 for error in first[1])
    assert all("None" not in error and "not-a-map" not in error for error in first[1])
    assert all("PRIVATE" not in error and "/local/path" not in error for error in first[1])


class _KnownGetOnlyMapping(Mapping):
    def __init__(self, row):
        self._row = row
        self.get_calls = 0
        self.iter_calls = 0
        self.items_calls = 0

    def get(self, key, default=None):
        self.get_calls += 1
        return self._row.get(key, default)

    def __getitem__(self, _key):
        raise RuntimeError("SECRET full mapping access")

    def __iter__(self):
        self.iter_calls += 1
        raise RuntimeError("SECRET full mapping iteration")

    def __len__(self):
        return 200_000

    def items(self):
        self.items_calls += 1
        raise RuntimeError("SECRET full mapping items")


def test_oversized_root_is_rejected_before_any_iteration_or_lookup() -> None:
    api = _api()
    hostile = _KnownGetOnlyMapping(_valid_row())

    with pytest.raises(api.RecordedMapError) as raised:
        api.parse_recorded_map_row(hostile)

    assert str(raised.value) == "invalid recorded map row"
    assert hostile.get_calls == 0
    assert hostile.iter_calls == 0
    assert hostile.items_calls == 0


def test_bounded_read_only_root_mapping_remains_supported() -> None:
    api = _api()

    parsed = api.parse_recorded_map_row(MappingProxyType(_valid_row()))

    assert parsed.act_id == "RECORDED.ACT.1"


class _CountingIrrelevantRows:
    def __init__(self, total: int):
        self.total = total
        self.consumed = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.consumed >= self.total:
            raise StopIteration
        self.consumed += 1
        return {"event": "irrelevant"}


def test_latest_recorded_acts_has_a_hard_scan_limit() -> None:
    api = _api()
    rows = _CountingIrrelevantRows(200_000)

    snapshots, errors = api.latest_recorded_acts(rows)

    assert snapshots == {}
    assert errors
    assert rows.consumed < 200_000


class _ExplodingRows:
    def __init__(self, first):
        self.first = first
        self.calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.calls += 1
        if self.calls == 1:
            return self.first
        raise RuntimeError("SECRET /private/iterator")


def test_latest_recorded_acts_preserves_selection_when_next_raises() -> None:
    api = _api()

    snapshots, errors = api.latest_recorded_acts(_ExplodingRows(_valid_row()))

    assert set(snapshots) == {0}
    assert len(errors) == 1
    assert "SECRET" not in errors[0]
    assert "/private/iterator" not in errors[0]


class _BadRowsIterable:
    def __iter__(self):
        raise RuntimeError("SECRET /private/iter")


def test_latest_recorded_acts_contains_iter_failure() -> None:
    api = _api()

    snapshots, errors = api.latest_recorded_acts(_BadRowsIterable())

    assert snapshots == {}
    assert len(errors) == 1
    assert "SECRET" not in errors[0]
    assert "/private/iter" not in errors[0]


class _RowsPastCap:
    def __init__(self, row, *, explode_at_sentinel: bool = False):
        self.row = row
        self.explode_at_sentinel = explode_at_sentinel
        self.calls = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.calls += 1
        if self.explode_at_sentinel and self.calls == 4:
            raise RuntimeError("SECRET /private/cap-sentinel")
        return self.row


def test_latest_recorded_acts_checks_one_sentinel_after_scan_cap(monkeypatch) -> None:
    api = _api()
    monkeypatch.setattr(api, "_MAX_INPUT_ROWS", 3)
    rows = _RowsPastCap(_valid_row())

    snapshots, errors = api.latest_recorded_acts(rows)

    assert set(snapshots) == {0}
    assert rows.calls == 4
    assert errors == ("recorded map row scan limit reached",)


def test_latest_recorded_acts_contains_exception_from_cap_sentinel(
    monkeypatch,
) -> None:
    api = _api()
    monkeypatch.setattr(api, "_MAX_INPUT_ROWS", 3)
    rows = _RowsPastCap(_valid_row(), explode_at_sentinel=True)

    snapshots, errors = api.latest_recorded_acts(rows)

    assert set(snapshots) == {0}
    assert rows.calls == 4
    assert errors == ("invalid recorded map row iterator",)
    assert "SECRET" not in repr(errors)
    assert "/private/cap-sentinel" not in repr(errors)
