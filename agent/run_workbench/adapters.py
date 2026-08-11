"""Normalize supported run artifacts into canonical workbench records."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Callable

from agent.run_metadata import record_run_id, safe_run_id

from .deltas import derive_snapshot_deltas, native_node_deltas
from .models import (
    Capabilities,
    COMPARISON_METADATA_FIELDS,
    Coverage,
    NodeOrigin,
    RunMetadata,
    RunOutcome,
    RunDelta,
    RunRecord,
    RunStatus,
    SourceKind,
    node_evidence_key,
)
from .sources import (
    SourceDescriptor,
    SourceFormatError,
    classify_records,
    read_json_records,
)


ReplayParser = Callable[[list[dict], str | None], dict]


@dataclass(frozen=True)
class AdaptedSource:
    """The normalized, non-throwing result of inspecting one artifact."""

    descriptor: SourceDescriptor
    runs: tuple[RunRecord, ...] = ()
    summary: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()


def adapt_path(
    path: Path,
    *,
    replay_parser: Callable[[list[dict], str | None], dict] | None = None,
) -> AdaptedSource:
    """Read and conservatively normalize one supported run artifact.

    The legacy replay parser is injected so this dependency-free package never
    imports the HTTP/viewer module that owns it.
    """
    path = Path(path)
    try:
        records = read_json_records(path)
    except SourceFormatError as error:
        descriptor = SourceDescriptor(SourceKind.UNKNOWN, 0, str(error))
        return AdaptedSource(descriptor=descriptor, errors=(str(error),))

    descriptor = classify_records(records, suffix=path.suffix)
    return adapt_records(
        path.name,
        records,
        descriptor=descriptor,
        replay_parser=replay_parser,
        source_path=path,
    )


def adapt_records(
    source_name: str,
    records: list[dict[str, Any]],
    *,
    descriptor: SourceDescriptor | None = None,
    replay_parser: Callable[[list[dict], str | None], dict] | None = None,
    source_path: Path | None = None,
) -> AdaptedSource:
    """Normalize already-decoded records without writing them to disk.

    ``source_path`` is an internal provenance hook for ``adapt_path``. Upload
    callers should omit it so their source name is the only recorded identity.
    """
    path = (
        Path(source_path)
        if source_path is not None
        else Path(Path(source_name).name)
    )
    resolved_descriptor = descriptor or classify_records(
        records, suffix=Path(source_name).suffix
    )
    if resolved_descriptor.kind is SourceKind.NATIVE_RUN:
        if not _has_native_run_structure(records[0]):
            return AdaptedSource(
                resolved_descriptor,
                errors=(
                    f"{path.name}: native run is missing players and map_point_history lists",
                ),
            )
        adapted = AdaptedSource(
            resolved_descriptor, runs=(_adapt_native(path, records[0]),)
        )
        return _validate_adapted_source(adapted)
    if resolved_descriptor.kind is SourceKind.REPLAY_JSONL:
        return _validate_adapted_source(
            _adapt_replay(path, resolved_descriptor, records, replay_parser)
        )
    if resolved_descriptor.kind is SourceKind.DECK_HISTORY:
        return _validate_adapted_source(
            _adapt_deck_history(path, resolved_descriptor, records)
        )
    if resolved_descriptor.kind is SourceKind.EVAL_RESULTS:
        return _validate_adapted_source(
            _adapt_eval_results(path, resolved_descriptor, records)
        )
    if resolved_descriptor.kind is SourceKind.SUMMARY:
        return AdaptedSource(
            resolved_descriptor,
            summary={"record_count": len(records), "records": deepcopy(records)},
        )
    return AdaptedSource(
        resolved_descriptor,
        errors=(f"{path.name}: {resolved_descriptor.message}",),
    )


def _adapt_native(path: Path, record: dict[str, Any]) -> RunRecord:
    players = record.get("players")
    player = players[0] if isinstance(players, list) and players and isinstance(players[0], dict) else {}
    nodes = _adapt_native_history(
        record.get("map_point_history"),
        source_id=str(path),
    )
    if nodes:
        final_player = _native_final_player_evidence(player)
        if final_player:
            # Final inventory is run-level evidence. Attach it once, to the
            # terminal canonical node, rather than duplicating the raw run on
            # every node.
            nodes[-1]["final_player"] = final_player
    acts = _dict_list(record.get("acts"))
    floors = _node_floors(nodes)
    status, victory, technical_kind = _status_from_record(record)
    max_floor = _first_int(record, "max_global_floor", "max_floor")
    if max_floor is None and floors:
        max_floor = max(floors)
    raw_modifiers = record.get("modifiers")
    modifiers = (
        tuple(raw_modifiers)
        if isinstance(raw_modifiers, list)
        and all(isinstance(modifier, str) for modifier in raw_modifiers)
        else ()
    )

    return RunRecord(
        run_id=record_run_id(record) or "",
        source_id=str(path),
        source_kind=SourceKind.NATIVE_RUN,
        metadata=RunMetadata(
            character=_first_text(player, "character", "name")
            or _first_text(record, "character"),
            seed=_first_text(record, "seed"),
            game_version=_first_text(record, "build_id", "game_version"),
            game_version_source=_metadata_version_source(record),
            checkpoint=_first_text(record, "checkpoint"),
            evaluation_mode=_first_text(record, "evaluation_mode"),
            scenario=_first_text(record, "scenario"),
            ascension=_first_int(record, "ascension"),
            modifiers=modifiers,
            is_multiplayer=(
                record.get("is_multiplayer")
                if type(record.get("is_multiplayer")) is bool
                else None
            ),
            started_at=_first_number(record, "started_at", "start_ts"),
            ended_at=_first_number(record, "ended_at", "end_ts", "ts"),
        ),
        outcome=RunOutcome(
            status=status,
            victory=victory,
            max_global_floor=max_floor,
            max_floor_label=_first_text(record, "max_floor_label"),
            technical_failure_kind=technical_kind,
        ),
        coverage=Coverage(
            complete_run=True,
            first_recorded_floor=min(floors) if floors else None,
            last_recorded_floor=max(floors) if floors else max_floor,
        ),
        capabilities=Capabilities(
            visited_route=True,
            node_rewards=True,
            final_inventory=True,
            turn_replay=False,
        ),
        acts=acts,
        nodes=nodes,
        comparison_conflicts=_comparison_conflicts_from_records([record]),
        _node_provenance_index=_single_source_node_provenance(
            nodes,
            source_kind=SourceKind.NATIVE_RUN,
            source_id=str(path),
        ),
    )


def _has_native_run_structure(record: dict[str, Any]) -> bool:
    return isinstance(record.get("players"), list) and isinstance(
        record.get("map_point_history"), list
    )


def _adapt_replay(
    path: Path,
    descriptor: SourceDescriptor,
    records: list[dict[str, Any]],
    replay_parser: ReplayParser | None,
) -> AdaptedSource:
    errors: list[str] = []
    parsed: dict[str, Any] = {}
    parser_succeeded = False
    parser_rejected = False
    if replay_parser is None:
        errors.append(f"{path.name}: no replay parser was provided")
    else:
        try:
            candidate = replay_parser(records, path.name)
            if isinstance(candidate, dict):
                parsed = candidate
                parser_succeeded = True
            else:
                parser_rejected = True
                errors.append(f"{path.name}: replay parser returned a non-object result")
        except Exception as error:  # adapter errors must not make the catalog unreadable
            parser_rejected = True
            errors.append(f"{path.name}: replay parser failed: {error}")

    summary = parsed.get("summary") if isinstance(parsed.get("summary"), dict) else {}
    nodes = _annotate_replay_nodes(
        _dict_list(parsed.get("rooms")),
        source_id=str(path),
    )
    observed_floors = _observed_replay_floors(records)
    observed_floors.extend(_node_floors(nodes))
    has_parser_coverage = parser_succeeded and all(
        key in summary
        for key in (
            "complete_run",
            "first_recorded_floor",
            "last_recorded_floor",
            "max_global_floor",
        )
    )
    status, victory, technical_kind = _status_from_records(records)
    has_terminal = any(_is_terminal_replay_entry(row) for row in records)
    if status is RunStatus.UNKNOWN and records:
        status = RunStatus.IN_PROGRESS

    starts = [_first_number(row, "ts") for row in records]
    timestamps = [value for value in starts if value is not None]
    start_action = _replay_start_action(records)
    ascension = _replay_ascension(summary, "ascension")
    if ascension is None:
        ascension = _replay_ascension(start_action, "ascension")
    modifiers = _replay_modifiers(summary)
    if modifiers is None:
        modifiers = _replay_modifiers(start_action)
    metadata = RunMetadata(
        character=_replay_text(summary, "character")
        or _replay_text(start_action, "character"),
        seed=_replay_text(summary, "seed")
        or _replay_text(start_action, "seed"),
        game_version=_replay_text(summary, "game_version", "build_id")
        or _replay_text(start_action, "game_version", "build_id"),
        game_version_source=_metadata_version_source(summary, start_action),
        checkpoint=_first_text(summary, "checkpoint")
        or _first_text(start_action, "checkpoint"),
        evaluation_mode=_first_text(summary, "evaluation_mode")
        or _first_text(start_action, "evaluation_mode"),
        scenario=_first_text(summary, "scenario")
        or _first_text(start_action, "scenario"),
        ascension=ascension,
        modifiers=modifiers or (),
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
    )
    run_id, identity_warnings = _replay_run_identity(records, summary)
    max_floor = _first_int(summary, "max_global_floor")
    if (
        max_floor is None
        and observed_floors
        and not has_parser_coverage
        and not parser_rejected
    ):
        max_floor = max(observed_floors)
    raw_actions = [
        deepcopy(row["data"])
        for row in records
        if row.get("type") == "action" and isinstance(row.get("data"), dict)
    ]
    raw_has_state = any(
        row.get("type") == "state" and isinstance(row.get("data"), dict)
        for row in records
    )
    raw_has_actions = bool(raw_actions)
    has_state = (
        summary["has_state_records"]
        if parser_succeeded and type(summary.get("has_state_records")) is bool
        else raw_has_state
    )
    has_actions = (
        summary["has_action_records"]
        if parser_succeeded and type(summary.get("has_action_records")) is bool
        else raw_has_actions
    )
    replay_by_node = {
        str(node["id"]): deepcopy(node)
        for node in nodes
        if node.get("id") is not None
    }
    if raw_actions:
        replay_by_node["__unassigned_actions__"] = {
            "_workbench_evidence_kind": "unassigned_replay_actions",
            "_workbench_provenance": [
                {"source_id": str(path), "source_kind": SourceKind.REPLAY_JSONL.value}
            ],
            "actions": raw_actions,
        }
    has_node_decisions = any(_node_has_decision_evidence(node) for node in nodes)
    usable_per_node_replay = any(
        node.get("id") is not None and _node_has_decision_evidence(node)
        for node in nodes
    )
    first_recorded_floor = (
        None
        if parser_rejected
        else (
            _first_int(summary, "first_recorded_floor")
            if has_parser_coverage
            else (min(observed_floors) if observed_floors else None)
        )
    )
    last_recorded_floor = (
        None
        if parser_rejected
        else (
            _first_int(summary, "last_recorded_floor")
            if has_parser_coverage
            else (max(observed_floors) if observed_floors else None)
        )
    )
    complete_run = (
        False
        if parser_rejected
        else (
            summary.get("complete_run") is True
            if has_parser_coverage
            else bool(
                observed_floors
                and min(observed_floors) == 1
                and has_terminal
            )
        )
    )
    run = RunRecord(
        run_id=run_id,
        source_id=str(path),
        source_kind=SourceKind.REPLAY_JSONL,
        metadata=metadata,
        outcome=RunOutcome(
            status=status,
            victory=victory,
            max_global_floor=max_floor,
            max_floor_label=_first_text(summary, "max_floor_label"),
            technical_failure_kind=technical_kind,
        ),
        coverage=Coverage(
            complete_run=complete_run,
            first_recorded_floor=first_recorded_floor,
            last_recorded_floor=last_recorded_floor,
        ),
        capabilities=Capabilities(
            visited_route=bool(observed_floors or nodes),
            decisions=bool((has_actions and raw_actions) or has_node_decisions),
            turn_replay=parser_succeeded
            and bool(
                (has_state and has_actions and raw_actions)
                or usable_per_node_replay
            ),
        ),
        nodes=nodes,
        replay_by_node=replay_by_node,
        warnings=identity_warnings,
        comparison_conflicts=_comparison_conflicts_from_records(
            [*records, start_action, summary]
        ),
        _node_provenance_index=_single_source_node_provenance(
            nodes,
            source_kind=SourceKind.REPLAY_JSONL,
            source_id=str(path),
        ),
    )
    return AdaptedSource(descriptor, runs=(run,), errors=tuple(errors))


def _adapt_deck_history(
    path: Path,
    descriptor: SourceDescriptor,
    records: list[dict[str, Any]],
) -> AdaptedSource:
    grouped: dict[str, list[dict[str, Any]]] = {}
    historical: list[tuple[int, dict[str, Any]]] = []
    errors: list[str] = []
    for index, row in enumerate(records, start=1):
        run_id = record_run_id(row)
        if not run_id:
            errors.append(f"{path.name}:{index}: deck-history record missing run_id")
            historical.append((index, row))
            continue
        grouped.setdefault(run_id, []).append(row)

    identified_runs = [
        _adapt_deck_run(path, run_id, rows)
        for run_id, rows in grouped.items()
    ]
    historical_runs = [
        _adapt_deck_run(
            path,
            "",
            [row],
            source_id=f"{path}:{index}",
            warnings=[f"{path.name}:{index}: deck-history record missing run_id"],
        )
        for index, row in historical
    ]
    return AdaptedSource(
        descriptor,
        runs=tuple(identified_runs + historical_runs),
        errors=tuple(errors),
    )


def _adapt_deck_run(
    path: Path,
    run_id: str,
    records: list[dict[str, Any]],
    *,
    source_id: str | None = None,
    warnings: list[str] | None = None,
) -> RunRecord:
    resolved_source_id = source_id or f"{path}:{run_id}"
    outcome_rows = [row for row in records if row.get("event") == "outcome"]
    outcome_row = outcome_rows[-1] if outcome_rows else {}
    status, victory, technical_kind = _status_from_record(outcome_row)
    timestamps = [
        timestamp
        for row in records
        if (timestamp := _first_number(row, "ts")) is not None
    ]
    floors = _deck_floors(records)
    max_floor = _first_int(outcome_row, "max_global_floor", "max_floor", "floor")
    if max_floor is None and floors:
        max_floor = max(floors)
    metadata = _metadata_from_records(
        records,
        started_at=min(timestamps) if timestamps else None,
        ended_at=_first_number(outcome_row, "ts")
        if outcome_row
        else (max(timestamps) if timestamps else None),
    )
    nodes = [
        _annotate_deck_history_evidence(row, resolved_source_id)
        for row in records
    ]
    return RunRecord(
        run_id=run_id,
        source_id=resolved_source_id,
        source_kind=SourceKind.DECK_HISTORY,
        metadata=metadata,
        outcome=RunOutcome(
            status=status,
            victory=victory,
            max_global_floor=max_floor,
            max_floor_label=_first_text(outcome_row, "max_floor_label"),
            technical_failure_kind=technical_kind,
        ),
        coverage=Coverage(
            complete_run=bool(outcome_rows),
            first_recorded_floor=min(floors) if floors else None,
            last_recorded_floor=max_floor,
        ),
        capabilities=Capabilities(
            visited_route=False,
            node_rewards=any(row.get("event") == "card_pick" for row in records),
            decisions=any(row.get("event") == "card_pick" for row in records),
        ),
        nodes=nodes,
        warnings=list(warnings or ()),
        comparison_conflicts=_comparison_conflicts_from_records(records),
        _node_provenance_index=_single_source_node_provenance(
            nodes,
            source_kind=SourceKind.DECK_HISTORY,
            source_id=resolved_source_id,
        ),
    )


def _adapt_eval_results(
    path: Path,
    descriptor: SourceDescriptor,
    records: list[dict[str, Any]],
) -> AdaptedSource:
    runs: list[RunRecord] = []
    for index, row in enumerate(records, start=1):
        if row.get("event") != "eval_result":
            continue
        status, victory, technical_kind = _status_from_record(row)
        floor = _first_int(row, "max_global_floor", "max_floor", "floor")
        timestamp = _first_number(row, "ts", "timestamp")
        runs.append(
            RunRecord(
                run_id=record_run_id(row) or "",
                source_id=f"{path}:{index}",
                source_kind=SourceKind.EVAL_RESULTS,
                metadata=_metadata_from_records(
                    [row],
                    started_at=_first_number(row, "started_at", "start_ts"),
                    ended_at=_first_number(row, "ended_at", "end_ts") or timestamp,
                ),
                outcome=RunOutcome(
                    status=status,
                    victory=victory,
                    max_global_floor=floor,
                    max_floor_label=_first_text(row, "max_floor_label"),
                    technical_failure_kind=technical_kind,
                ),
                coverage=Coverage(
                    complete_run=status not in {RunStatus.UNKNOWN, RunStatus.IN_PROGRESS},
                    last_recorded_floor=floor,
                ),
                comparison_conflicts=_comparison_conflicts_from_records([row]),
            )
        )
    return AdaptedSource(descriptor, runs=tuple(runs))


def _metadata_from_records(
    records: list[dict[str, Any]],
    *,
    started_at: float | None,
    ended_at: float | None,
) -> RunMetadata:
    return RunMetadata(
        character=_first_nonempty_record_value(records, "character"),
        seed=_first_nonempty_record_value(records, "seed"),
        game_version=_first_nonempty_record_value(records, "game_version")
        or _first_nonempty_record_value(records, "build_id"),
        game_version_source=_metadata_version_source(*records),
        checkpoint=_first_nonempty_record_value(records, "checkpoint"),
        evaluation_mode=_first_nonempty_record_value(records, "evaluation_mode"),
        scenario=_first_nonempty_record_value(records, "scenario"),
        ascension=_first_record_int(records, "ascension"),
        started_at=started_at,
        ended_at=ended_at,
    )


def _comparison_conflicts_from_records(
    records: list[dict[str, Any]],
) -> frozenset[str]:
    conflicts: set[str] = set()
    for attribute, keys in (
        ("character", ("character",)),
        ("seed", ("seed",)),
        ("game_version", ("game_version", "build_id")),
        ("evaluation_mode", ("evaluation_mode",)),
        ("scenario", ("scenario",)),
    ):
        observed = {
            value
            for record in records
            for key in keys
            if (value := _first_text(record, key)) is not None
        }
        if len(observed) > 1:
            conflicts.add(attribute)
    ascensions = {
        value
        for record in records
        if (value := _first_int(record, "ascension")) is not None
    }
    if len(ascensions) > 1:
        conflicts.add("ascension")
    return frozenset(conflicts & COMPARISON_METADATA_FIELDS)


def _metadata_version_source(*records: dict[str, Any]) -> str | None:
    for record in records:
        value = record.get("game_version_source")
        if type(value) is str and value.strip():
            return value.strip()
    return None


def _status_from_records(
    records: list[dict[str, Any]],
) -> tuple[RunStatus, bool | None, str | None]:
    for row in reversed(records):
        status, victory, technical_kind = _status_from_record(row)
        if status is not RunStatus.UNKNOWN:
            return status, victory, technical_kind
        data = row.get("data")
        if isinstance(data, dict):
            status, victory, technical_kind = _status_from_record(data)
            if status is not RunStatus.UNKNOWN:
                return status, victory, technical_kind
    return RunStatus.UNKNOWN, None, None


def _status_from_record(
    record: dict[str, Any],
) -> tuple[RunStatus, bool | None, str | None]:
    raw_status = _first_text(record, "status", "end_reason", "technical_failure_kind")
    aliases = {
        "won": "win",
        "victory": "win",
        "loss": "dead",
        "lost": "dead",
        "defeat": "dead",
        "reset-failure": "reset_failure",
    }
    normalized = aliases.get((raw_status or "").lower(), (raw_status or "").lower())
    try:
        status = RunStatus(normalized) if normalized else RunStatus.UNKNOWN
    except ValueError:
        status = RunStatus.UNKNOWN

    victory: bool | None = None
    for key in ("victory", "won", "run_won"):
        if isinstance(record.get(key), bool):
            victory = record[key]
            break
    if status is RunStatus.WIN:
        victory = True
    elif status is RunStatus.DEAD:
        victory = False
    elif status.is_technical:
        victory = False
    elif status is RunStatus.IN_PROGRESS:
        victory = None
    elif status is RunStatus.UNKNOWN and victory is not None:
        status = RunStatus.WIN if victory else RunStatus.DEAD
    technical_kind = status.value if status.is_technical else None
    return status, victory, technical_kind


def _is_terminal_replay_entry(row: dict[str, Any]) -> bool:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    event = row.get("event")
    if data.get("decision") == "game_over" or (
        isinstance(event, str) and event in {"outcome", "result", "eval_result"}
    ):
        return True
    status, _, _ = _status_from_record(row)
    if status is not RunStatus.UNKNOWN:
        return True
    nested_status, _, _ = _status_from_record(data)
    return nested_status is not RunStatus.UNKNOWN


def _observed_replay_floors(records: list[dict[str, Any]]) -> list[int]:
    floors: list[int] = []
    for row in records:
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        floor = _first_int(context, "floor")
        act = _first_int(context, "act")
        if floor is not None:
            floors.append((act - 1) * 17 + floor if act is not None and act > 0 else floor)
            continue
        floor = _first_int(data, "global_floor", "floor")
        if floor is not None:
            floors.append(floor)
    return floors


def _node_floors(nodes: list[dict[str, Any]]) -> list[int]:
    floors: list[int] = []
    for node in nodes:
        floor = _first_int(node, "global_floor", "floor")
        if floor is not None:
            floors.append(floor)
    return floors


def _deck_floors(records: list[dict[str, Any]]) -> list[int]:
    floors: list[int] = []
    for row in records:
        floor = _first_int(row, "floor_crossed", "floor")
        if floor is not None:
            floors.append(floor)
    return floors


def _replay_start_action(records: list[dict[str, Any]]) -> dict[str, Any]:
    for row in records:
        if row.get("type") != "action" or not isinstance(row.get("data"), dict):
            continue
        data = row["data"]
        if _first_text(data, "cmd", "decision") == "start_run":
            return data
    return {}


def _replay_run_identity(
    records: list[dict[str, Any]], summary: dict[str, Any]
) -> tuple[str, list[str]]:
    top_level_ids: list[str] = []
    nested_ids: list[str] = []
    for record in records:
        top_level = safe_run_id(record.get("run_id"))
        if top_level is not None:
            top_level_ids.append(top_level)
        data = record.get("data")
        if isinstance(data, dict):
            nested = safe_run_id(data.get("run_id"))
            if nested is not None:
                nested_ids.append(nested)
    summary_id = safe_run_id(summary.get("run_id"))
    resolved = (
        summary_id
        or (top_level_ids[0] if top_level_ids else None)
        or (nested_ids[0] if nested_ids else None)
        or ""
    )
    observed = sorted(
        {
            *top_level_ids,
            *nested_ids,
            *([summary_id] if summary_id is not None else []),
        }
    )
    warnings: list[str] = []
    if len(observed) > 1:
        warnings.append(
            "conflicting replay run_id values: "
            f"observed={', '.join(observed)}; using {resolved}"
        )
    return resolved, warnings


def _first_nonempty_record_value(records: list[dict[str, Any]], key: str) -> str | None:
    for record in records:
        value = _first_text(record, key)
        if value is not None:
            return value
    return None


def _first_record_int(records: list[dict[str, Any]], key: str) -> int | None:
    for record in records:
        value = _first_int(record, key)
        if value is not None:
            return value
    return None


def _first_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _replay_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _replay_ascension(record: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = record.get(key)
        if type(value) is int and 0 <= value <= 10:
            return value
    return None


def _replay_modifiers(record: dict[str, Any]) -> tuple[str, ...] | None:
    value = record.get("modifiers")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _first_int(record: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _first_nonnegative_int(record: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = record.get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _first_number(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, float) and math.isfinite(value):
            return float(value)
    return None


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _adapt_native_history(
    value: Any,
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    previous_node: dict[str, Any] | None = None
    act_node_indices: dict[int, int] = {}
    annotated: list[dict[str, Any]] = []
    for outer_index, entry in enumerate(value):
        if isinstance(entry, dict):
            entries = [(_native_act_index(entry), entry)]
        elif isinstance(entry, list):
            entries = [
                (outer_index, node)
                for node in entry
                if isinstance(node, dict)
            ]
        else:
            continue
        for act_index, node in entries:
            node_index = act_node_indices.get(act_index, 0)
            act_node_indices[act_index] = node_index + 1
            annotated.append(
                _annotate_native_node(
                    node,
                    previous_node,
                    act_index=act_index,
                    node_index=node_index,
                    source_id=source_id,
                )
            )
            previous_node = node
    return annotated


_DETAIL_EVIDENCE_LIMIT = 256


def _single_source_node_provenance(
    nodes: list[dict[str, Any]],
    *,
    source_kind: SourceKind,
    source_id: str,
) -> dict[str, tuple[NodeOrigin, ...]]:
    origin = NodeOrigin(source_kind=source_kind, source_id=source_id)
    return {
        node_evidence_key(nodes, index): (origin,)
        for index in range(len(nodes))
    }


def _native_final_player_evidence(player: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded inventory evidence needed by the final floor."""

    evidence: dict[str, Any] = {}
    for key in ("current_hp", "hp", "max_hp", "current_gold", "gold"):
        if key not in player:
            continue
        value = player[key]
        evidence[key] = value if _is_finite_number(value) else None
    for key in ("deck", "relic_items", "relics", "potion_items", "potions"):
        value = player.get(key)
        if isinstance(value, list):
            evidence[key] = [
                _sanitize_detail_evidence(item)
                for item in value[:_DETAIL_EVIDENCE_LIMIT]
            ]
    return evidence


def _sanitize_detail_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth >= 16:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            key: _sanitize_detail_evidence(item, depth=depth + 1)
            for key, item in list(value.items())[:_DETAIL_EVIDENCE_LIMIT]
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_detail_evidence(item, depth=depth + 1)
            for item in value[:_DETAIL_EVIDENCE_LIMIT]
        ]
    return None


def _annotate_native_node(
    node: dict[str, Any],
    previous_node: dict[str, Any] | None,
    *,
    act_index: int,
    node_index: int,
    source_id: str,
) -> dict[str, Any]:
    enriched = deepcopy(node)
    enriched["id"] = f"a{act_index}:n{node_index}"
    enriched["_workbench_evidence_kind"] = "route_node"
    enriched["_workbench_provenance"] = [
        {
            "source_id": source_id,
            "source_kind": SourceKind.NATIVE_RUN.value,
        }
    ]
    enriched["deltas"] = native_node_deltas(node, previous_node).to_dict()
    _sanitize_delta_measurements(enriched)
    return enriched


def _native_act_index(node: dict[str, Any]) -> int:
    act_index = _first_int(node, "act_index")
    if act_index is not None and act_index >= 0:
        return act_index
    act = _first_int(node, "act")
    if act is not None:
        return max(0, act - 1)
    global_floor = _first_int(node, "global_floor")
    if global_floor is not None and global_floor > 0:
        return (global_floor - 1) // 17
    return 0


def _annotate_replay_nodes(
    nodes: list[dict[str, Any]],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for node in nodes:
        start_snapshot = _replay_room_snapshot(node, phase="start")
        end_snapshot = _replay_room_snapshot(node, phase="end")
        enriched = deepcopy(node)
        enriched["_workbench_evidence_kind"] = "route_node"
        enriched["_workbench_provenance"] = [
            {
                "source_id": source_id,
                "source_kind": SourceKind.REPLAY_JSONL.value,
            }
        ]
        enriched["deltas"] = derive_snapshot_deltas(
            end_snapshot or {}, start_snapshot
        ).to_dict()
        _sanitize_delta_measurements(enriched)
        annotated.append(enriched)
    return annotated


def _replay_room_snapshot(
    node: dict[str, Any], *, phase: str
) -> dict[str, Any] | None:
    player = node.get(f"{phase}_player")
    player = player if isinstance(player, dict) else {}
    snapshot: dict[str, Any] = {}
    fields = (
        ("hp", ("hp", "current_hp"), (f"{phase}_hp", f"{phase}_current_hp"), "number"),
        ("max_hp", ("max_hp",), (f"{phase}_max_hp",), "number"),
        (
            "gold",
            ("gold", "current_gold"),
            (f"{phase}_gold", f"{phase}_current_gold"),
            "number",
        ),
        ("deck", ("deck",), (f"{phase}_deck",), "list"),
        ("relic_items", ("relic_items",), (f"{phase}_relic_items",), "list"),
        ("relics", ("relics",), (f"{phase}_relics",), "list"),
        ("potion_items", ("potion_items",), (f"{phase}_potion_items",), "list"),
        ("potions", ("potions",), (f"{phase}_potions",), "list"),
    )
    for target, player_keys, room_keys, kind in fields:
        value = _snapshot_field(player, player_keys, kind=kind)
        if value is _MISSING:
            value = _snapshot_field(node, room_keys, kind=kind)
        if value is not _MISSING:
            snapshot[target] = deepcopy(value)
    return snapshot or None


_MISSING = object()


def _snapshot_field(
    record: dict[str, Any], keys: tuple[str, ...], *, kind: str
) -> Any:
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if kind == "number" and _is_finite_number(value):
            return value
        if kind == "list" and isinstance(value, list):
            return value
    return _MISSING


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _sanitize_delta_measurements(node: dict[str, Any]) -> None:
    records = [node]
    for key in ("player_stats",):
        values = node.get(key)
        if isinstance(values, list):
            records.extend(value for value in values if isinstance(value, dict))
    for key in ("start_player", "end_player", "player"):
        value = node.get(key)
        if isinstance(value, dict):
            records.append(value)

    measurement_keys = {
        "hp",
        "current_hp",
        "max_hp",
        "gold",
        "current_gold",
        "damage_taken",
        "hp_healed",
        "start_hp",
        "end_hp",
        "start_max_hp",
        "end_max_hp",
        "start_gold",
        "end_gold",
        "start_current_hp",
        "end_current_hp",
        "start_current_gold",
        "end_current_gold",
    }
    delta_value_keys = {
        "cards_gained",
        "cards_removed",
        "cards_transformed",
        "cards_enchanted",
        "cards_upgraded",
        "upgraded_cards",
        "relic_choices",
        "relics_gained",
        "potion_choices",
        "potions_gained",
        "potions_used",
        "potions_discarded",
        "deck",
        "relic_items",
        "relics",
        "potion_items",
        "potions",
        "start_deck",
        "end_deck",
        "start_relic_items",
        "end_relic_items",
        "start_relics",
        "end_relics",
        "start_potion_items",
        "end_potion_items",
        "start_potions",
        "end_potions",
    }
    for record in records:
        for key in measurement_keys:
            value = record.get(key)
            if isinstance(value, float) and not math.isfinite(value):
                record[key] = None
        for key in delta_value_keys:
            if key in record and not _is_safe_delta_value(record[key]):
                record[key] = None


def _is_safe_delta_value(value: Any) -> bool:
    try:
        RunDelta(value=value).to_dict()
    except (TypeError, ValueError):
        return False
    return True


def _node_has_decision_evidence(node: dict[str, Any]) -> bool:
    return any(
        isinstance(node.get(key), list) and bool(node[key])
        for key in ("actions", "decisions", "options", "choices")
    )


def _annotate_deck_history_evidence(
    record: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    evidence = deepcopy(record)
    evidence["_workbench_evidence_kind"] = "deck_history_event"
    evidence["_workbench_provenance"] = [
        {"source_id": source_id, "source_kind": SourceKind.DECK_HISTORY.value}
    ]
    return evidence


def _validate_adapted_source(adapted: AdaptedSource) -> AdaptedSource:
    valid_runs: list[RunRecord] = []
    errors = list(adapted.errors)
    for run in adapted.runs:
        try:
            payload = run.to_dict()
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            errors.append(
                f"{run.source_id}: normalized run is not JSON-safe: {error}"
            )
            continue
        valid_runs.append(run)
    return AdaptedSource(
        descriptor=adapted.descriptor,
        runs=tuple(valid_runs),
        summary=adapted.summary,
        errors=tuple(errors),
    )
