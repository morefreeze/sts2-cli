from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
import importlib
import json
import math
import operator
from typing import Any, Callable

import pytest


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
                    "children": [{"col": 1, "row": 3}],
                    "visited": True,
                    "current": True,
                }],
            ],
            "boss": {"col": 1, "row": 3, "type": "BossRoom", "id": "BOSS.TEST"},
            "current_coord": {"col": 1, "row": 2},
        },
        "visited_nodes": [
            _route_node(0, 0, "Ancient", before, after),
            _route_node(0, 1, "Monster", after, after),
            _route_node(1, 2, "Elite", after, after),
        ],
    }


def _boss_route_row(*, boss_id: str = "BOSS.TEST"):
    row = _valid_row()
    row["map"]["rows"][2][0]["current"] = False
    row["map"]["boss"].update(id=boss_id, visited=True, current=True)
    row["map"]["current_coord"] = {"col": 1, "row": 3}
    previous = row["visited_nodes"][-1]["exit_player"]
    row["visited_nodes"].append(
        _route_node(1, 3, "Boss", previous, previous)
    )
    return row


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
        "recorded:1:3",
    ]
    assert [node.room_type for node in parsed.act_map.nodes] == [
        "Ancient", "Monster", "Shop", "Elite", "Boss",
    ]
    assert [edge.to_dict() for edge in parsed.act_map.edges] == [
        {"from": "recorded:0:0", "to": "recorded:0:1"},
        {"from": "recorded:0:0", "to": "recorded:1:1"},
        {"from": "recorded:0:1", "to": "recorded:1:2"},
        {"from": "recorded:1:1", "to": "recorded:1:2"},
        {"from": "recorded:1:2", "to": "recorded:1:3"},
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
    ("incomplete-inventory", lambda r: r["visited_nodes"][0]["entry_player"].pop("gold")),
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
