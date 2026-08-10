"""Source-neutral floor detail builders for canonical run records."""

from __future__ import annotations

from dataclasses import fields
import math
import re
from typing import Any, Iterable

from .deltas import NodeDeltas
from .models import (
    DeltaQuality,
    InventorySnapshot,
    NodeDetail,
    RunDelta,
    RunRecord,
    SourceKind,
)


# This matches the canonical visited-node bound used by the map service. A
# single floor must not smuggle an unbounded raw replay into a detail response.
DETAIL_COLLECTION_LIMIT = 256
_LABEL_PATTERN = re.compile(r"^A(?P<act>\d+)F(?P<floor>\d+)$", re.IGNORECASE)
_NATIVE_ID_PATTERN = re.compile(r"^a(?P<act_index>\d+):n\d+$")


class NodeNotFoundError(LookupError):
    """A stable public error that deliberately omits source paths."""

    def __init__(self, run_id: str, node_id: str) -> None:
        self.run_id = run_id
        self.node_id = node_id
        super().__init__(f"node '{node_id}' not found in run '{run_id}'")


def build_node_detail(record: RunRecord, node_id: str) -> NodeDetail:
    """Build one canonical floor detail without inspecting source filenames."""

    node = next(
        (
            item
            for item in record.nodes
            if isinstance(item, dict) and item.get("id") == node_id
        ),
        None,
    )
    if node is None:
        raise NodeNotFoundError(record.run_id, node_id)
    if record.source_kind is SourceKind.NATIVE_RUN:
        return native_node_detail(record, node)
    if record.capabilities.turn_replay:
        return replay_node_detail(record, node)
    return basic_node_detail(record, node)


def native_node_detail(record: RunRecord, node: dict[str, Any]) -> NodeDetail:
    index = _node_index(record, node)
    previous = record.nodes[index - 1] if index > 0 else None
    entry_sources: list[dict[str, Any]] = []
    explicit_entry = node.get("entry_player")
    if isinstance(explicit_entry, dict):
        entry_sources.append(explicit_entry)
    previous_stats = _native_player_stats(previous)
    if previous_stats:
        entry_sources.append(previous_stats)

    exit_sources: list[dict[str, Any]] = []
    current_stats = _native_player_stats(node)
    if current_stats:
        exit_sources.append(current_stats)
    explicit_exit = node.get("exit_player")
    if isinstance(explicit_exit, dict):
        exit_sources.append(explicit_exit)
    if index == len(record.nodes) - 1 and isinstance(node.get("final_player"), dict):
        exit_sources.append(node["final_player"])

    entry, entry_known = _inventory_snapshot(entry_sources)
    exit_snapshot, exit_known = _inventory_snapshot(exit_sources)
    return _detail(
        record,
        node,
        encounter=_native_encounter(node),
        entry=entry,
        exit=exit_snapshot,
        entry_known=entry_known,
        exit_known=exit_known,
        choices=_native_choices(node),
        actions=_dict_items(node.get("actions")),
        combat_rounds=(),
        turn_replay=False,
    )


def replay_node_detail(record: RunRecord, node: dict[str, Any]) -> NodeDetail:
    # Both snapshots and all round evidence are intentionally read from this
    # canonical room. No previous/next room is consulted for replay details.
    entry, entry_known = _inventory_snapshot(
        [node["start_player"]] if isinstance(node.get("start_player"), dict) else []
    )
    exit_snapshot, exit_known = _inventory_snapshot(
        [node["end_player"]] if isinstance(node.get("end_player"), dict) else []
    )
    combat = node.get("combat") if isinstance(node.get("combat"), dict) else {}
    return _detail(
        record,
        node,
        encounter=_replay_encounter(node),
        entry=entry,
        exit=exit_snapshot,
        entry_known=entry_known,
        exit_known=exit_known,
        choices=_dict_items(node.get("options"), node.get("choices")),
        actions=_dict_items(node.get("actions")),
        combat_rounds=_dict_items(combat.get("rounds")),
        turn_replay=True,
    )


def basic_node_detail(record: RunRecord, node: dict[str, Any]) -> NodeDetail:
    entry_sources = [
        value
        for key in ("entry_player", "start_player")
        if isinstance((value := node.get(key)), dict)
    ]
    exit_sources = [
        value
        for key in ("exit_player", "end_player", "player")
        if isinstance((value := node.get(key)), dict)
    ]
    entry, entry_known = _inventory_snapshot(entry_sources)
    exit_snapshot, exit_known = _inventory_snapshot(exit_sources)
    return _detail(
        record,
        node,
        encounter=_basic_encounter(node),
        entry=entry,
        exit=exit_snapshot,
        entry_known=entry_known,
        exit_known=exit_known,
        choices=_dict_items(node.get("choices"), node.get("options")),
        actions=_dict_items(node.get("actions")),
        combat_rounds=(),
        turn_replay=False,
    )


def _detail(
    record: RunRecord,
    node: dict[str, Any],
    *,
    encounter: dict[str, Any],
    entry: InventorySnapshot,
    exit: InventorySnapshot,
    entry_known: tuple[str, ...],
    exit_known: tuple[str, ...],
    choices: tuple[dict[str, Any], ...],
    actions: tuple[dict[str, Any], ...],
    combat_rounds: tuple[dict[str, Any], ...],
    turn_replay: bool,
) -> NodeDetail:
    act, floor, global_floor, label = _coordinates(node)
    coverage: dict[str, Any] = {
        "complete_run": record.coverage.complete_run,
        "first_recorded_floor": record.coverage.first_recorded_floor,
        "last_recorded_floor": record.coverage.last_recorded_floor,
        "turn_replay": turn_replay,
    }
    if not turn_replay:
        coverage["message"] = "此记录不包含逐回合操作"
    coverage["entry_inventory_fields"] = list(entry_known)
    coverage["exit_inventory_fields"] = list(exit_known)
    return NodeDetail(
        run_id=record.run_id,
        node_id=str(node.get("id")),
        act=act,  # type: ignore[arg-type] -- None is the honest unknown value.
        floor=floor,  # type: ignore[arg-type]
        global_floor=global_floor,  # type: ignore[arg-type]
        label=label,
        room_type=_room_type(node),
        status=_text(node.get("status")) or "unknown",
        encounter=encounter,
        entry=entry,
        exit=exit,
        deltas=_node_deltas(node.get("deltas")),
        choices=choices,
        actions=actions,
        combat_rounds=combat_rounds,
        coverage=coverage,
        facts=_dict_items(node.get("facts")),
        hypotheses=_dict_items(node.get("hypotheses")),
    )


def _node_index(record: RunRecord, selected: dict[str, Any]) -> int:
    for index, node in enumerate(record.nodes):
        if node is selected:
            return index
    selected_id = selected.get("id")
    for index, node in enumerate(record.nodes):
        if isinstance(node, dict) and node.get("id") == selected_id:
            return index
    return 0


def _native_player_stats(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    players = node.get("player_stats")
    if not isinstance(players, list) or not players or not isinstance(players[0], dict):
        return {}
    return players[0]


def _inventory_snapshot(
    sources: Iterable[dict[str, Any]],
) -> tuple[InventorySnapshot, tuple[str, ...]]:
    candidates = tuple(source for source in sources if isinstance(source, dict))
    values: dict[str, Any] = {"hp": None, "max_hp": None, "gold": None}
    known: list[str] = []
    scalar_keys = {
        "hp": ("hp", "current_hp"),
        "max_hp": ("max_hp",),
        "gold": ("gold", "current_gold"),
    }
    for field_name, keys in scalar_keys.items():
        observed = _first_inventory_int(candidates, keys)
        if observed is not _MISSING:
            values[field_name] = observed
            known.append(field_name)

    inventory_keys = {
        "deck": ("deck",),
        "relics": ("relic_items", "relics"),
        "potions": ("potion_items", "potions"),
    }
    for field_name, keys in inventory_keys.items():
        observed = _first_inventory_list(candidates, keys)
        if observed is _MISSING:
            values[field_name] = ()
            continue
        known.append(field_name)
        values[field_name] = _inventory_items(observed, field_name)

    return InventorySnapshot(**values), tuple(known)


_MISSING = object()


def _first_inventory_int(
    candidates: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> object:
    for source in candidates:
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            # An invalid field is unknown, but a later source may carry the
            # same inventory phase with a valid observation.
    return _MISSING


def _first_inventory_list(
    candidates: tuple[dict[str, Any], ...], keys: tuple[str, ...]
) -> object:
    for source in candidates:
        for key in keys:
            if key in source and isinstance(source[key], list):
                return source[key]
    return _MISSING


def _inventory_items(value: object, kind: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    result: list[dict[str, Any]] = []
    for item in value[:DETAIL_COLLECTION_LIMIT]:
        if isinstance(item, dict):
            result.append(_clean_dict(item))
        elif isinstance(item, str):
            key = "id" if kind == "deck" else "name"
            result.append({key: item})
    return tuple(result)


def _native_choices(node: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    groups: list[Any] = [node.get("choices"), node.get("options")]
    rooms = node.get("rooms")
    if isinstance(rooms, list):
        for room in rooms[:DETAIL_COLLECTION_LIMIT]:
            if isinstance(room, dict):
                groups.extend((room.get("choices"), room.get("options")))
    return _dict_items(*groups)


def _native_encounter(node: dict[str, Any]) -> dict[str, Any]:
    rooms = [
        room
        for room in (node.get("rooms") if isinstance(node.get("rooms"), list) else [])
        if isinstance(room, dict)
    ][:DETAIL_COLLECTION_LIMIT]
    sources = [node, *rooms]
    encounter: dict[str, Any] = {}
    model_id = _first_text(sources, "model_id", "room_model_id", "boss_id")
    if model_id is not None:
        encounter["model_id"] = model_id
    monster_ids = _first_list(sources, "monster_ids")
    if monster_ids is not None:
        encounter["monster_ids"] = _clean_value(monster_ids)
    event_name = _first_text(sources, "event_name", "event")
    if event_name is not None:
        encounter["event_name"] = event_name
    description = _first_text(sources, "description")
    if description is not None:
        encounter["description"] = description
    outcome = _first_dict(sources, "outcome", "result")
    if outcome is not None:
        encounter["outcome"] = _clean_dict(outcome)
    return encounter


def _replay_encounter(node: dict[str, Any]) -> dict[str, Any]:
    encounter = _basic_encounter(node)
    status = _text(node.get("status"))
    if status not in {None, "in_progress", "completed", "unknown"}:
        encounter.setdefault("outcome", {"status": status})
    return encounter


def _basic_encounter(node: dict[str, Any]) -> dict[str, Any]:
    encounter: dict[str, Any] = {}
    for target, keys in (
        ("model_id", ("model_id", "room_model_id", "boss_id")),
        ("event_name", ("event_name", "event")),
        ("description", ("description",)),
        ("boss", ("boss",)),
    ):
        value = next((_text(node.get(key)) for key in keys if _text(node.get(key))), None)
        if value is not None:
            encounter[target] = value
    outcome = node.get("outcome")
    if isinstance(outcome, dict):
        encounter["outcome"] = _clean_dict(outcome)
    return encounter


def _first_text(sources: list[dict[str, Any]], *keys: str) -> str | None:
    for source in sources:
        for key in keys:
            value = _text(source.get(key))
            if value is not None:
                return value
    return None


def _first_list(sources: list[dict[str, Any]], *keys: str) -> list[Any] | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, list):
                return value[:DETAIL_COLLECTION_LIMIT]
    return None


def _first_dict(sources: list[dict[str, Any]], *keys: str) -> dict[str, Any] | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, dict):
                return value
    return None


def _node_deltas(value: Any) -> NodeDeltas:
    if isinstance(value, NodeDeltas):
        return value
    payload = value if isinstance(value, dict) else {}
    resolved: dict[str, RunDelta] = {}
    for item in fields(NodeDeltas):
        raw = payload.get(item.name)
        if not isinstance(raw, dict):
            resolved[item.name] = RunDelta()
            continue
        try:
            quality = DeltaQuality(raw.get("quality"))
        except (TypeError, ValueError):
            quality = DeltaQuality.UNKNOWN
        raw_value = raw.get("value")
        cleaned = _clean_value(raw_value)
        if _contains_invalid_number(raw_value) or (
            cleaned is None and raw_value is not None
        ):
            resolved[item.name] = RunDelta()
        else:
            resolved[item.name] = RunDelta(value=cleaned, quality=quality)
    return NodeDeltas(**resolved)


def _contains_invalid_number(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_invalid_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_invalid_number(item) for item in value)
    return False


def _coordinates(
    node: dict[str, Any],
) -> tuple[int | None, int | None, int | None, str]:
    act = _int(node.get("act"))
    floor = _int(node.get("floor"))
    global_floor = _int(node.get("global_floor"))
    label = _text(node.get("label"))
    label_match = _LABEL_PATTERN.fullmatch(label) if label else None
    if label_match is not None:
        act = act if act is not None else int(label_match.group("act"))
        floor = floor if floor is not None else int(label_match.group("floor"))
    if act is None:
        act_index = _int(node.get("act_index"))
        if act_index is not None and act_index >= 0:
            act = act_index + 1
        else:
            node_id = _text(node.get("id")) or ""
            match = _NATIVE_ID_PATTERN.fullmatch(node_id)
            if match is not None:
                act = int(match.group("act_index")) + 1
    if global_floor is not None and global_floor > 0:
        if act is None:
            act = (global_floor - 1) // 17 + 1
        if floor is None:
            floor = (global_floor - 1) % 17 + 1
    if global_floor is None and act is not None and floor is not None:
        global_floor = (act - 1) * 17 + floor
    if label is None and act is not None and floor is not None:
        label = f"A{act}F{floor}"
    return act, floor, global_floor, label or "unknown"


def _room_type(node: dict[str, Any]) -> str:
    return (
        _text(node.get("room_type"))
        or _text(node.get("map_point_type"))
        or "unknown"
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict_items(*values: Any) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if len(result) >= DETAIL_COLLECTION_LIMIT:
                return tuple(result)
            if isinstance(item, dict):
                result.append(_clean_dict(item))
    return tuple(result)


def _clean_dict(value: dict[Any, Any], *, depth: int = 0) -> dict[str, Any]:
    if depth >= 16:
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:DETAIL_COLLECTION_LIMIT]:
        if isinstance(key, str):
            result[key] = _clean_value(item, depth=depth + 1)
    return result


def _clean_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 16:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return _clean_dict(value, depth=depth)
    if isinstance(value, (list, tuple)):
        return [
            _clean_value(item, depth=depth + 1)
            for item in value[:DETAIL_COLLECTION_LIMIT]
        ]
    return None


__all__ = [
    "DETAIL_COLLECTION_LIMIT",
    "NodeNotFoundError",
    "basic_node_detail",
    "build_node_detail",
    "native_node_detail",
    "replay_node_detail",
]
