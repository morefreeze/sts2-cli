"""Strict parsing for locally recorded authoritative map snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable, Mapping

from .deltas import derive_snapshot_deltas
from .models import ActMap, MapAlignment, MapEdge, MapNode


_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_GRAPH_NODES = 256
_MAX_GRAPH_EDGES = 2048
_MAX_GRAPH_STRING = 64
_MAX_PLAYER_BYTES = 64 * 1024
_MAX_ERRORS = 32
_MAX_ERROR_CHARS = 160
_MAX_INPUT_ROWS = 100_000
_MAX_ROUTE_NODES = 17
_MAX_MAP_COL = 6
_MAX_MAP_ROW = 16
_MAX_ROOT_KEYS = 32
_MAX_UNTRUSTED_DEPTH = 8
_MAX_UNTRUSTED_NODES = 1_000_000
_MAX_UNTRUSTED_LIST_ITEMS = 4096
_MAX_UNTRUSTED_OBJECT_KEYS = 512
_BOSS_ROW_BY_ACT = {1: 16, 2: 16, 3: 15, 4: 14}
_ROOT_KEYS = (
    "event", "act", "is_multiplayer", "ts", "map", "visited_nodes",
)

_ROOM_TYPES = {
    "ancient": "Ancient",
    "ancientroom": "Ancient",
    "neow": "Ancient",
    "neowroom": "Ancient",
    "monster": "Monster",
    "monsterroom": "Monster",
    "combat": "Monster",
    "combatroom": "Monster",
    "normalcombat": "Monster",
    "normalcombatroom": "Monster",
    "elite": "Elite",
    "eliteroom": "Elite",
    "boss": "Boss",
    "bossroom": "Boss",
    "shop": "Shop",
    "shoproom": "Shop",
    "merchant": "Shop",
    "merchantroom": "Shop",
    "rest": "RestSite",
    "restroom": "RestSite",
    "restsite": "RestSite",
    "restsiteroom": "RestSite",
    "campfire": "RestSite",
    "campfireroom": "RestSite",
    "treasure": "Treasure",
    "treasureroom": "Treasure",
    "chest": "Treasure",
    "chestroom": "Treasure",
    "unknown": "Unknown",
    "unknownroom": "Unknown",
    "event": "Unknown",
    "eventroom": "Unknown",
    "questionmark": "Unknown",
}
_PLAYER_FIELDS = frozenset({"hp", "max_hp", "gold", "deck", "relics", "potions"})
_PRODUCER_STRIPPED_KEYS = frozenset({
    "description", "description_raw", "flavor", "flavor_text", "text",
})


class RecordedMapError(ValueError):
    """A bounded validation error for one untrusted recorded map row."""


class _RecordedMapValidationError(ValueError):
    """An internal-only marker for intentional, safe validator failures."""


class _UntrustedRecordedMapError(ValueError):
    """An internal marker whose details are never exposed publicly."""


@dataclass(frozen=True)
class RecordedActSnapshot:
    act_index: int
    act_id: str
    act_map: ActMap
    route_nodes: tuple[dict[str, Any], ...]

    def __post_init__(self) -> None:
        route_nodes = object.__getattribute__(self, "route_nodes")
        if type(route_nodes) is not tuple:
            raise TypeError("route_nodes must be a tuple")
        encoded = json.dumps(
            route_nodes,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        encoded.encode("utf-8")
        object.__setattr__(self, "route_nodes", encoded)

    def __getattribute__(self, name: str) -> Any:
        if name == "route_nodes":
            encoded = object.__getattribute__(self, name)
            return tuple(json.loads(encoded))
        return object.__getattribute__(self, name)


@dataclass(frozen=True)
class _GraphNode:
    coord: tuple[int, int]
    room_type: str
    children: tuple[tuple[int, int], ...]
    visited: bool
    current: bool
    model_id: str | None = None


def _error(message: str) -> _RecordedMapValidationError:
    return _RecordedMapValidationError(message[:_MAX_ERROR_CHARS])


def _map_int(value: Any, *, label: str) -> int:
    if type(value) is not int or not _INT32_MIN <= value <= _INT32_MAX:
        raise _error(f"{label} must be an Int32 integer")
    return value


def _coordinate(value: Any, *, label: str) -> tuple[int, int]:
    if type(value) is not dict:
        raise _error(f"{label} must be an object")
    coord = (
        _map_int(value.get("col"), label=f"{label} col"),
        _map_int(value.get("row"), label=f"{label} row"),
    )
    if not 0 <= coord[0] <= _MAX_MAP_COL or not 0 <= coord[1] <= _MAX_MAP_ROW:
        raise _error(f"{label} is outside recorded map bounds")
    return coord


def _bounded_graph_string(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_GRAPH_STRING:
        raise _error(f"{label} must be a non-empty bounded string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _error(f"{label} must contain valid Unicode") from None
    return value


def _room_type(value: Any, *, label: str) -> str:
    raw = _bounded_graph_string(value, label=label)
    key = "".join(character for character in raw.casefold() if character.isalnum())
    return _ROOM_TYPES.get(key, "Unknown")


def _finite_timestamp(value: Any) -> int | float:
    if type(value) is int:
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise _error("recorded map timestamp must be finite")
    elif type(value) is float:
        if not math.isfinite(value):
            raise _error("recorded map timestamp must be finite")
    else:
        raise _error("recorded map timestamp must be finite")
    return value


def _json_value(value: Any, *, depth: int, count: list[int]) -> Any:
    count[0] += 1
    if depth > 4 or count[0] > 4096:
        raise _error("recorded player inventory exceeds structural limits")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise _error("recorded player inventory contains an invalid integer")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _error("recorded player inventory contains a non-finite number")
        return value
    if type(value) is str:
        if len(value) > 256:
            raise _error("recorded player inventory contains an oversized string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _error("recorded player inventory contains invalid Unicode") from None
        return value
    if type(value) is list:
        if len(value) > 256:
            raise _error("recorded player inventory contains an oversized list")
        return [_json_value(item, depth=depth + 1, count=count) for item in value]
    if type(value) is dict:
        if len(value) > 32:
            raise _error("recorded player inventory contains an invalid object")
        for key in value:
            if type(key) is not str:
                raise TypeError("recorded player inventory key is not a string")
            if (
                len(key) > 256
                or key.casefold() in _PRODUCER_STRIPPED_KEYS
            ):
                raise _error("recorded player inventory contains an invalid object")
        return {
            key: _json_value(item, depth=depth + 1, count=count)
            for key, item in value.items()
        }
    raise _error("recorded player inventory is not JSON-safe")


def _inventory_list(value: Any, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise _error(f"recorded player {label} must be a list")
    return _json_value(value, depth=0, count=[0])


def _player_snapshot(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise _error("recorded player snapshot has an invalid shape")
    if any(key not in _PLAYER_FIELDS for key in value):
        raise _error("recorded player snapshot has an invalid shape")
    detached: dict[str, Any] = {}
    for field in ("hp", "max_hp", "gold"):
        if field not in value:
            continue
        number = value[field]
        if type(number) is int:
            valid_number = _INT64_MIN <= number <= _INT64_MAX
        elif type(number) is float:
            valid_number = math.isfinite(number)
        else:
            valid_number = False
        if not valid_number:
            raise _error("recorded player snapshot contains an invalid number")
        detached[field] = number
    for field in ("deck", "relics", "potions"):
        if field in value:
            detached[field] = _inventory_list(value[field], label=field)
    try:
        encoded = json.dumps(
            detached, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        raise _error("recorded player snapshot is not JSON-safe") from None
    if len(encoded) > _MAX_PLAYER_BYTES:
        raise _error("recorded player snapshot exceeds the size limit")
    return detached


def _parse_graph(raw_map: Any, *, act: int) -> tuple[
    tuple[_GraphNode, ...],
    frozenset[tuple[int, int]],
    tuple[int, int],
    frozenset[tuple[tuple[int, int], tuple[int, int]]],
]:
    if type(raw_map) is not dict:
        raise _error("recorded map payload must have type map")
    map_type = raw_map.get("type")
    if type(map_type) is not str or map_type != "map":
        raise _error("recorded map payload must have type map")
    context = raw_map.get("context")
    if type(context) is not dict:
        raise _error("recorded map context must be an object")
    context_act = context.get("act")
    if type(context_act) is not int or context_act != act:
        raise _error("recorded map context act is inconsistent")
    rows = raw_map.get("rows")
    if type(rows) is not list:
        raise _error("recorded map rows must be a list")
    if len(rows) > _MAX_GRAPH_NODES:
        raise _error("recorded map exceeds the node limit")

    graph: list[_GraphNode] = []
    coordinates: set[tuple[int, int]] = set()
    current_markers: list[tuple[int, int]] = []
    ancient_coords: list[tuple[int, int]] = []
    boss_row = _BOSS_ROW_BY_ACT[act]
    edge_count = 0
    for row_container in rows:
        if type(row_container) is not list:
            raise _error("recorded map row container must be a list")
        for raw_node in row_container:
            if len(graph) + 1 >= _MAX_GRAPH_NODES:
                raise _error("recorded map exceeds the node limit")
            if type(raw_node) is not dict:
                raise _error("recorded map node must be an object")
            coord = _coordinate(raw_node, label="recorded map node coordinate")
            if coord[1] >= boss_row:
                raise _error("recorded map ordinary node must be below boss row")
            if coord in coordinates:
                raise _error("recorded map node coordinates must be unique")
            visited = raw_node.get("visited")
            current = raw_node.get("current")
            if type(visited) is not bool:
                raise _error("recorded map visited marker must be boolean")
            if type(current) is not bool:
                raise _error("recorded map current marker must be boolean")
            children = raw_node.get("children")
            if type(children) is not list:
                raise _error("recorded map children must be a list")
            edge_count += len(children)
            if edge_count > _MAX_GRAPH_EDGES:
                raise _error("recorded map exceeds the edge limit")
            child_coords = tuple(
                _coordinate(child, label="recorded map child coordinate")
                for child in children
            )
            room_type = _room_type(
                raw_node.get("type"), label="recorded map room type"
            )
            if room_type == "Boss":
                raise _error("recorded map ordinary node cannot be a boss")
            if room_type == "Ancient":
                ancient_coords.append(coord)
            graph.append(_GraphNode(
                coord=coord,
                room_type=room_type,
                children=child_coords,
                visited=visited,
                current=current,
            ))
            coordinates.add(coord)
            if current:
                current_markers.append(coord)

    boss = raw_map.get("boss")
    if type(boss) is not dict:
        raise _error("recorded map boss must be an object")
    boss_coord = _coordinate(boss, label="recorded map boss coordinate")
    if boss_coord[1] != boss_row:
        raise _error("recorded map boss row is inconsistent with its act")
    if boss_coord in coordinates:
        raise _error("recorded map boss coordinate must be unique")
    boss_id = boss.get("id")
    if boss_id is not None:
        boss_id = _bounded_graph_string(boss_id, label="recorded map boss id")
    if "children" in boss:
        boss_children = boss["children"]
        if type(boss_children) is not list or boss_children:
            raise _error("recorded map boss children must be an empty list")
    boss_visited = boss.get("visited", False)
    boss_current = boss.get("current", False)
    if type(boss_visited) is not bool:
        raise _error("recorded map boss visited marker must be boolean")
    if type(boss_current) is not bool:
        raise _error("recorded map boss current marker must be boolean")
    boss_room_type = _room_type(boss.get("type"), label="recorded map boss type")
    if boss_room_type != "Boss":
        raise _error("recorded map boss must have boss room type")
    graph.append(_GraphNode(
        coord=boss_coord,
        room_type=boss_room_type,
        children=(),
        visited=boss_visited,
        current=boss_current,
        model_id=boss_id,
    ))
    coordinates.add(boss_coord)
    if boss_current:
        current_markers.append(boss_coord)

    current_coord = _coordinate(
        raw_map.get("current_coord"), label="recorded map current coordinate"
    )
    if current_coord not in coordinates:
        raise _error("recorded map current coordinate is absent")
    if current_markers != [current_coord]:
        raise _error("recorded map current markers are inconsistent")

    edge_set: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for node in graph:
        for child in node.children:
            edge = (node.coord, child)
            if child not in coordinates:
                raise _error("recorded map edge points outside the graph")
            if child == node.coord:
                raise _error("recorded map self edges are not allowed")
            if child[1] <= node.coord[1]:
                raise _error("recorded map edges must move forward by row")
            if edge in edge_set:
                raise _error("recorded map edges must be unique")
            edge_set.add(edge)

    if len(ancient_coords) != 1 or ancient_coords[0][1] != 0:
        raise _error("recorded map must have one row-zero Ancient node")
    children_by_coord = {
        node.coord: node.children for node in graph
    }
    reachable = {ancient_coords[0]}
    frontier = [ancient_coords[0]]
    while frontier:
        parent = frontier.pop()
        for child in children_by_coord[parent]:
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)
    if reachable != coordinates:
        raise _error("recorded map contains nodes unreachable from Ancient")

    parents_by_coord: dict[tuple[int, int], list[tuple[int, int]]] = {
        coord: [] for coord in coordinates
    }
    for parent, child in edge_set:
        parents_by_coord[child].append(parent)
    reaches_boss = {boss_coord}
    frontier = [boss_coord]
    while frontier:
        child = frontier.pop()
        for parent in parents_by_coord[child]:
            if parent not in reaches_boss:
                reaches_boss.add(parent)
                frontier.append(parent)
    if reaches_boss != coordinates:
        raise _error("recorded map contains a route that cannot reach the boss")

    raw_visited = frozenset(node.coord for node in graph if node.visited)
    return tuple(graph), raw_visited, current_coord, frozenset(edge_set)


def _visited_type(value: dict[str, Any]) -> str:
    present = [key for key in ("type", "room_type") if key in value]
    if not present:
        raise _error("recorded route node must contain a room type")
    normalized = [
        _room_type(value[key], label="recorded route room type") for key in present
    ]
    if any(room_type != normalized[0] for room_type in normalized[1:]):
        raise _error("recorded route room types are inconsistent")
    return normalized[0]


def _snapshot_untrusted_value(
    value: Any, *, depth: int, count: list[int]
) -> Any:
    """Copy one bounded value without retaining attacker-controlled containers."""

    count[0] += 1
    if depth > _MAX_UNTRUSTED_DEPTH or count[0] > _MAX_UNTRUSTED_NODES:
        raise _UntrustedRecordedMapError
    if value is None or type(value) in (bool, int, float, str):
        return value
    if type(value) is list:
        if len(value) > _MAX_UNTRUSTED_LIST_ITEMS:
            raise _UntrustedRecordedMapError
        return [
            _snapshot_untrusted_value(item, depth=depth + 1, count=count)
            for item in value
        ]
    if type(value) is dict:
        if len(value) > _MAX_UNTRUSTED_OBJECT_KEYS:
            raise _UntrustedRecordedMapError
        detached = {}
        for key, item in value.items():
            count[0] += 1
            if count[0] > _MAX_UNTRUSTED_NODES or type(key) is not str:
                raise _UntrustedRecordedMapError
            detached[key] = _snapshot_untrusted_value(
                item, depth=depth + 1, count=count
            )
        return detached
    raise _UntrustedRecordedMapError


def _normalize_root_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    """Bound and detach all parser-owned input before schema validation."""

    try:
        if not isinstance(row, Mapping):
            raise TypeError("root is not a mapping")
        root_size = len(row)
        if root_size > _MAX_ROOT_KEYS:
            raise _UntrustedRecordedMapError
        iterator = iter(row)
        root_keys = []
        for _index in range(_MAX_ROOT_KEYS + 1):
            try:
                key = next(iterator)
            except StopIteration:
                break
            if type(key) is not str:
                raise _UntrustedRecordedMapError
            root_keys.append(key)
        else:
            raise _UntrustedRecordedMapError
        if len(root_keys) != root_size or len(set(root_keys)) != len(root_keys):
            raise _UntrustedRecordedMapError
        normalized = {}
        count = [0]
        for key in root_keys:
            if key in _ROOT_KEYS:
                normalized[key] = _snapshot_untrusted_value(
                    row[key], depth=0, count=count
                )
        return normalized
    except Exception:
        raise _UntrustedRecordedMapError from None


def _parse_recorded_row(
    row: dict[str, Any],
) -> tuple[RecordedActSnapshot, int | float]:
    event = row.get("event")
    if type(event) is not str or event != "map_snapshot":
        raise _error("recorded map row has an invalid event")
    act = row.get("act")
    if type(act) is not int or not 1 <= act <= 4:
        raise _error("recorded map act must be an integer from 1 to 4")
    if "is_multiplayer" in row and row.get("is_multiplayer") is not False:
        raise _error("recorded map row must be single-player")
    timestamp = _finite_timestamp(row.get("ts"))
    graph, raw_visited, current_coord, edge_set = _parse_graph(row.get("map"), act=act)
    by_coord = {node.coord: node for node in graph}

    raw_route = row.get("visited_nodes")
    if type(raw_route) is not list:
        raise _error("recorded visited route must be a list")
    if not raw_route:
        raise _error("recorded visited route must not be empty")
    if len(raw_route) > _MAX_ROUTE_NODES:
        raise _error("recorded visited route exceeds 17 floors")
    route_coordinates: list[tuple[int, int]] = []
    route_nodes: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    act_index = act - 1
    for path_index, raw_route_node in enumerate(raw_route):
        if type(raw_route_node) is not dict:
            raise _error("recorded route node must be an object")
        coord = _coordinate(raw_route_node, label="recorded route coordinate")
        if coord not in by_coord:
            raise _error("recorded route coordinate is absent from the graph")
        if coord in seen:
            raise _error("recorded route coordinates must be unique")
        seen.add(coord)
        room_type = _visited_type(raw_route_node)
        graph_node = by_coord[coord]
        if room_type != graph_node.room_type:
            raise _error("recorded route room type disagrees with the graph")
        entry = _player_snapshot(raw_route_node.get("entry_player"))
        exit_ = _player_snapshot(raw_route_node.get("exit_player"))
        route_node: dict[str, Any] = {
            "id": f"a{act_index}:n{path_index}",
            "act": act,
            "act_index": act_index,
            "floor": path_index + 1,
            "global_floor": act_index * 17 + path_index + 1,
            "col": coord[0],
            "row": coord[1],
            "map_point_type": room_type,
            "room_type": room_type,
            "start_player": entry,
            "end_player": exit_,
            "deltas": derive_snapshot_deltas(exit_, entry).to_dict(),
        }
        if graph_node.model_id is not None:
            route_node["model_id"] = graph_node.model_id
        route_nodes.append(route_node)
        route_coordinates.append(coord)

    route_set = frozenset(route_coordinates)
    if route_set != raw_visited:
        raise _error("recorded route does not match graph visited markers")
    if not route_coordinates or route_coordinates[-1] != current_coord:
        raise _error("recorded route does not end at the current coordinate")
    if by_coord[route_coordinates[0]].room_type != "Ancient":
        raise _error("recorded route must start at the Ancient node")
    for previous, current in zip(route_coordinates, route_coordinates[1:]):
        if (previous, current) not in edge_set:
            raise _error("recorded route is not connected in order")

    path_index_by_coord = {
        coord: index for index, coord in enumerate(route_coordinates)
    }
    map_nodes = tuple(
        MapNode(
            id=f"recorded:{node.coord[0]}:{node.coord[1]}",
            col=node.coord[0],
            row=node.coord[1],
            room_type=node.room_type,
            visited=node.coord in raw_visited,
            path_index=path_index_by_coord.get(node.coord),
        )
        for node in graph
    )
    map_edges = tuple(
        MapEdge(
            from_id=f"recorded:{node.coord[0]}:{node.coord[1]}",
            to_id=f"recorded:{child[0]}:{child[1]}",
        )
        for node in graph
        for child in node.children
    )
    path_ids = tuple(
        f"recorded:{coord[0]}:{coord[1]}" for coord in route_coordinates
    )
    act_id = f"RECORDED.ACT.{act}"
    snapshot = RecordedActSnapshot(
        act_index=act_index,
        act_id=act_id,
        act_map=ActMap(
            act_id=act_id,
            nodes=map_nodes,
            edges=map_edges,
            alignment=MapAlignment(
                ok=True,
                ambiguous=False,
                path_node_ids=path_ids,
            ),
            full_map=True,
            visited_route=bool(route_nodes),
            fallback_reason=None,
        ),
        route_nodes=tuple(route_nodes),
    )
    try:
        json.dumps(
            {"map": snapshot.act_map.to_dict(), "route": snapshot.route_nodes},
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        raise _error("recorded map output is not JSON-safe") from None
    return snapshot, timestamp


def _parse_public_row(
    row: Mapping[str, Any],
) -> tuple[RecordedActSnapshot, int | float]:
    """Translate only trusted internal validation failures to public detail."""

    try:
        normalized = _normalize_root_mapping(row)
    except Exception:
        raise RecordedMapError("invalid recorded map row") from None
    try:
        return _parse_recorded_row(normalized)
    except _RecordedMapValidationError as exc:
        raise RecordedMapError(str(exc)) from None
    except Exception:
        raise RecordedMapError("invalid recorded map row") from None


def parse_recorded_map_row(row: Mapping[str, Any]) -> RecordedActSnapshot:
    """Validate and detach one persisted ``map_snapshot`` row."""

    snapshot, _timestamp = _parse_public_row(row)
    return snapshot


def latest_recorded_acts(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[int, RecordedActSnapshot], tuple[str, ...]]:
    """Consume rows once and keep the latest valid snapshot for each act."""

    selected: dict[int, tuple[int | float, RecordedActSnapshot]] = {}
    errors: list[str] = []
    try:
        iterator = iter(rows)
    except Exception:
        return {}, ("invalid recorded map rows iterable",)

    exhausted = False
    for index in range(_MAX_INPUT_ROWS):
        try:
            row = next(iterator)
        except StopIteration:
            exhausted = True
            break
        except Exception:
            exhausted = True
            if len(errors) < _MAX_ERRORS:
                errors.append(
                    f"row {index}: invalid recorded map row iterator"[
                        :_MAX_ERROR_CHARS
                    ]
                )
            break
        try:
            snapshot, timestamp = _parse_public_row(row)
        except RecordedMapError as exc:
            if len(errors) < _MAX_ERRORS:
                errors.append(f"row {index}: {exc}"[:_MAX_ERROR_CHARS])
            continue
        except Exception:
            if len(errors) < _MAX_ERRORS:
                errors.append(
                    f"row {index}: invalid recorded map row"[:_MAX_ERROR_CHARS]
                )
            continue
        retained = selected.get(snapshot.act_index)
        if retained is None or timestamp >= retained[0]:
            selected[snapshot.act_index] = (timestamp, snapshot)
    if not exhausted:
        try:
            next(iterator)
        except StopIteration:
            pass
        except Exception:
            if len(errors) < _MAX_ERRORS:
                errors.append("invalid recorded map row iterator")
        else:
            if len(errors) < _MAX_ERRORS:
                errors.append("recorded map row scan limit reached")
    return (
        {
            act_index: selected[act_index][1]
            for act_index in sorted(selected)
        },
        tuple(errors),
    )
