"""Deterministically join canonical records with explicit run identities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields
from itertools import combinations
import json
from typing import Any, Iterable

from .models import Capabilities, Coverage, RunMetadata, RunOutcome, RunRecord, RunStatus, SourceKind


_SOURCE_PRIORITY = {
    SourceKind.NATIVE_RUN: 0,
    SourceKind.REPLAY_JSONL: 1,
    SourceKind.DECK_HISTORY: 2,
    SourceKind.EVAL_RESULTS: 3,
    SourceKind.SUMMARY: 4,
    SourceKind.UNKNOWN: 5,
}


def join_records(records: Iterable[RunRecord]) -> list[RunRecord]:
    """Join exact, non-empty run IDs and preserve ambiguous history separately."""
    ordered = sorted((deepcopy(record) for record in records), key=_record_key)
    identified: dict[str, list[RunRecord]] = {}
    historical: list[RunRecord] = []
    for record in ordered:
        if record.run_id:
            identified.setdefault(record.run_id, []).append(record)
        else:
            historical.append(record)

    merged = [_merge_group(group) for _, group in sorted(identified.items())]
    _warn_ambiguous_historical_records(historical, merged)
    merged.extend(historical)
    return sorted(merged, key=lambda record: (not bool(record.run_id), record.run_id, record.source_id))


def _merge_group(records: list[RunRecord]) -> RunRecord:
    records = sorted(records, key=_record_key)
    if len(records) == 1:
        return records[0]

    warnings = _unique(warning for record in records for warning in record.warnings)
    metadata, metadata_warnings = _merge_metadata(records)
    warnings.extend(metadata_warnings)
    outcome, outcome_warnings = _merge_outcome(records)
    warnings.extend(outcome_warnings)
    source_ids = sorted({record.source_id for record in records})
    source_kind = min((record.source_kind for record in records), key=lambda kind: _SOURCE_PRIORITY[kind])

    replay_by_node: dict[str, Any] = {}
    for record in records:
        for node_id, replay in sorted(record.replay_by_node.items()):
            if node_id in replay_by_node and replay_by_node[node_id] != replay:
                warnings.append(f"conflicting replay data for node {node_id!r}")
                continue
            replay_by_node[node_id] = deepcopy(replay)

    first_floors = [
        record.coverage.first_recorded_floor
        for record in records
        if record.coverage.first_recorded_floor is not None
    ]
    last_floors = [
        record.coverage.last_recorded_floor
        for record in records
        if record.coverage.last_recorded_floor is not None
    ]
    capabilities = Capabilities(
        **{
            field.name: any(getattr(record.capabilities, field.name) for record in records)
            for field in fields(Capabilities)
        }
    )
    return RunRecord(
        run_id=records[0].run_id,
        source_id=" | ".join(source_ids),
        source_kind=source_kind,
        metadata=metadata,
        outcome=outcome,
        coverage=Coverage(
            complete_run=any(record.coverage.complete_run for record in records),
            first_recorded_floor=min(first_floors) if first_floors else None,
            last_recorded_floor=max(last_floors) if last_floors else None,
        ),
        capabilities=capabilities,
        acts=[deepcopy(act) for record in records for act in record.acts],
        nodes=[deepcopy(node) for record in records for node in record.nodes],
        replay_by_node=replay_by_node,
        warnings=_unique(warnings),
    )


def _merge_metadata(records: list[RunRecord]) -> tuple[RunMetadata, list[str]]:
    values: dict[str, Any] = {}
    warnings: list[str] = []
    for field in fields(RunMetadata):
        candidates = [
            getattr(record.metadata, field.name)
            for record in records
            if _is_present(getattr(record.metadata, field.name))
        ]
        values[field.name] = candidates[0] if candidates else field.default
        distinct = _distinct(candidates)
        if len(distinct) > 1:
            warnings.append(
                f"conflicting metadata {field.name}: "
                + ", ".join(repr(value) for value in distinct)
                + f"; kept {values[field.name]!r}"
            )
    return RunMetadata(**values), warnings


def _merge_outcome(records: list[RunRecord]) -> tuple[RunOutcome, list[str]]:
    warnings: list[str] = []
    statuses = _distinct(
        record.outcome.status
        for record in records
        if record.outcome.status is not RunStatus.UNKNOWN
    )
    status = max(statuses, key=_status_key) if statuses else RunStatus.UNKNOWN
    if len(statuses) > 1:
        warnings.append(
            "conflicting outcome status: "
            + ", ".join(value.value for value in statuses)
            + f"; kept {status.value}"
        )

    victories = _distinct(
        record.outcome.victory
        for record in records
        if record.outcome.victory is not None
    )
    victory = victories[0] if victories else None
    if len(victories) > 1:
        warnings.append(f"conflicting outcome victory: {victories!r}; kept {victory!r}")

    floors = [
        record.outcome.max_global_floor
        for record in records
        if record.outcome.max_global_floor is not None
    ]
    max_floor = max(floors) if floors else None
    labels = [
        record.outcome.max_floor_label
        for record in records
        if record.outcome.max_floor_label is not None
    ]
    label = labels[0] if labels else None
    technical_kinds = [
        record.outcome.technical_failure_kind
        for record in records
        if record.outcome.technical_failure_kind is not None
    ]
    technical_kind = status.value if status.is_technical else (technical_kinds[0] if technical_kinds else None)
    return (
        RunOutcome(
            status=status,
            victory=victory,
            max_global_floor=max_floor,
            max_floor_label=label,
            technical_failure_kind=technical_kind,
        ),
        warnings,
    )


def _warn_ambiguous_historical_records(
    historical: list[RunRecord],
    identified: list[RunRecord],
) -> None:
    for left, right in combinations(historical, 2):
        if not _plausibly_overlap(left, right):
            continue
        _warn_ambiguous_pair(left, right)
    for anonymous in historical:
        for known in identified:
            if _plausibly_overlap(anonymous, known):
                _warn_ambiguous_pair(anonymous, known)


def _warn_ambiguous_pair(left: RunRecord, right: RunRecord) -> None:
    for record, counterpart in ((left, right), (right, left)):
        message = (
            "ambiguous historical identity: matching seed/timestamp with "
            f"{counterpart.source_id}; not merged"
        )
        if message not in record.warnings:
            record.warnings.append(message)


def _plausibly_overlap(left: RunRecord, right: RunRecord) -> bool:
    if not left.metadata.seed or left.metadata.seed != right.metadata.seed:
        return False
    left_range = _timestamp_range(left.metadata)
    right_range = _timestamp_range(right.metadata)
    if left_range is None or right_range is None:
        return False
    return left_range[0] <= right_range[1] and right_range[0] <= left_range[1]


def _timestamp_range(metadata: RunMetadata) -> tuple[float, float] | None:
    timestamps = [value for value in (metadata.started_at, metadata.ended_at) if value is not None]
    if not timestamps:
        return None
    return min(timestamps), max(timestamps)


def _record_key(record: RunRecord) -> tuple[str, str, str, str]:
    return (
        record.source_id,
        record.source_kind.value,
        record.run_id,
        json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _status_key(status: RunStatus) -> tuple[int, str]:
    if status.is_technical:
        return 3, status.value
    if status in {RunStatus.WIN, RunStatus.DEAD}:
        return 2, status.value
    if status is RunStatus.IN_PROGRESS:
        return 1, status.value
    return 0, status.value


def _is_present(value: Any) -> bool:
    return value is not None and value != ()


def _distinct(values: Iterable[Any]) -> list[Any]:
    distinct: list[Any] = []
    for value in values:
        if value not in distinct:
            distinct.append(value)
    return distinct


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
