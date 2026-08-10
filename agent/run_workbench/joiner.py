"""Deterministically join canonical records with explicit run identities."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
from hashlib import sha256
from itertools import combinations, groupby
import json
from typing import Any, Iterable

from .models import (
    Capabilities,
    COMPARISON_METADATA_FIELDS,
    Coverage,
    NodeOrigin,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
    node_evidence_key,
)


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
    records = _deterministic_tie_records(records)
    if len(records) == 1:
        return _ensure_node_origins(records[0])

    warnings = _unique(warning for record in records for warning in record.warnings)
    metadata, metadata_warnings, comparison_conflicts = _merge_metadata(records)
    warnings.extend(metadata_warnings)
    outcome, outcome_warnings = _merge_outcome(records)
    warnings.extend(outcome_warnings)
    source_ids = sorted({record.source_id for record in records})
    source_kind = min((record.source_kind for record in records), key=lambda kind: _SOURCE_PRIORITY[kind])

    replay_by_node: dict[str, Any] = {}
    for record in _deterministic_evidence_records(records):
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
    merged_acts, act_warnings, _ = _merge_evidence(records, evidence_type="act")
    merged_nodes, node_warnings, node_origins = _merge_evidence(
        records, evidence_type="node"
    )
    warnings.extend(act_warnings)
    warnings.extend(node_warnings)
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
        acts=merged_acts,
        nodes=merged_nodes,
        replay_by_node=replay_by_node,
        warnings=_unique(warnings),
        comparison_conflicts=comparison_conflicts,
        _node_provenance_index={
            node_evidence_key(merged_nodes, index): origins
            for index, origins in enumerate(node_origins)
            if origins
        },
    )


def _ensure_node_origins(record: RunRecord) -> RunRecord:
    index = {
        node_evidence_key(record.nodes, item_index): origins
        for item_index in range(len(record.nodes))
        if (origins := _record_node_origins(record, item_index))
    }
    return replace(record, _node_provenance_index=index)


def _record_node_origins(
    record: RunRecord, index: int
) -> tuple[NodeOrigin, ...]:
    origins = record.node_origins(index)
    if origins:
        return origins
    if not isinstance(record.source_id, str) or not record.source_id:
        return ()
    return (NodeOrigin(record.source_kind, record.source_id),)


def _merge_typed_origins(
    *groups: tuple[NodeOrigin, ...],
) -> tuple[NodeOrigin, ...]:
    unique = {origin for group in groups for origin in group}
    return tuple(
        sorted(unique, key=lambda origin: (origin.source_kind.value, origin.source_id))
    )


def _merge_metadata(
    records: list[RunRecord],
) -> tuple[RunMetadata, list[str], frozenset[str]]:
    values: dict[str, Any] = {}
    warnings: list[str] = []
    comparison_conflicts = {
        conflict
        for record in records
        for conflict in record.comparison_conflicts
    }
    ordered_records = _deterministic_evidence_records(records)
    for field in fields(RunMetadata):
        candidates = [
            getattr(record.metadata, field.name)
            for record in ordered_records
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
            if field.name in COMPARISON_METADATA_FIELDS:
                comparison_conflicts.add(field.name)
    return RunMetadata(**values), warnings, frozenset(comparison_conflicts)


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
    if status is RunStatus.WIN:
        victory: bool | None = True
    elif status is RunStatus.DEAD or status.is_technical:
        victory = False
    elif len(victories) == 1:
        victory = victories[0]
    else:
        victory = None
    if victories and (len(victories) > 1 or any(value != victory for value in victories)):
        warnings.append(
            f"conflicting outcome victory: {victories!r}; kept {victory!r}"
        )

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
    technical_kind = status.value if status.is_technical else None
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


def _merge_evidence(
    records: list[RunRecord],
    *,
    evidence_type: str,
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[tuple[NodeOrigin, ...]],
]:
    merged: list[dict[str, Any]] = []
    by_identity: dict[str, int] = {}
    warnings: list[str] = []
    merged_origins: list[tuple[NodeOrigin, ...]] = []
    attribute = "acts" if evidence_type == "act" else "nodes"
    for record in _deterministic_evidence_records(records):
        for item_index, item in enumerate(getattr(record, attribute)):
            evidence = _annotate_evidence(item, record, evidence_type)
            origins = (
                _record_node_origins(record, item_index)
                if evidence_type == "node"
                else ()
            )
            identity = _stable_evidence_identity(evidence, evidence_type)
            if identity is None:
                merged.append(evidence)
                merged_origins.append(origins)
                continue
            existing_index = by_identity.get(identity)
            if existing_index is None:
                by_identity[identity] = len(merged)
                merged.append(evidence)
                merged_origins.append(origins)
                continue

            existing = merged[existing_index]
            merged_origins[existing_index] = _merge_typed_origins(
                merged_origins[existing_index], origins
            )
            if _evidence_payload(existing) == _evidence_payload(evidence):
                existing["_workbench_provenance"] = _merge_provenance(
                    existing.get("_workbench_provenance"),
                    evidence.get("_workbench_provenance"),
                )
                continue

            conflict = {
                "payload": _evidence_payload(evidence),
                "provenance": deepcopy(evidence["_workbench_provenance"]),
            }
            conflicts = existing.setdefault("_workbench_conflicting_evidence", [])
            if conflict not in conflicts:
                conflicts.append(conflict)
            existing["_workbench_provenance"] = _merge_provenance(
                existing.get("_workbench_provenance"),
                evidence.get("_workbench_provenance"),
            )
            warning = (
                f"conflicting {evidence_type} payload for {identity}; "
                f"kept {_primary_evidence_source(existing)}"
            )
            if warning not in warnings:
                warnings.append(warning)
    return merged, warnings, merged_origins


def _annotate_evidence(
    item: dict[str, Any],
    record: RunRecord,
    evidence_type: str,
) -> dict[str, Any]:
    evidence = deepcopy(item)
    evidence.setdefault(
        "_workbench_evidence_kind",
        "act" if evidence_type == "act" else "route_node",
    )
    evidence["_workbench_provenance"] = _merge_provenance(
        evidence.get("_workbench_provenance"),
        [
            {
                "source_id": record.source_id,
                "source_kind": record.source_kind.value,
            }
        ],
    )
    return evidence


def _stable_evidence_identity(
    evidence: dict[str, Any],
    evidence_type: str,
) -> str | None:
    keys = ("id", "act_id", "act") if evidence_type == "act" else ("id", "node_id")
    for key in keys:
        if evidence.get(key) is not None:
            return f"{key}={_stable_json(evidence[key])}"
    return None


def _evidence_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in evidence.items()
        if key
        not in {
            "_workbench_provenance",
            "_workbench_conflicting_evidence",
        }
    }


def _merge_provenance(*groups: Any) -> list[dict[str, str]]:
    provenance: dict[tuple[str, str], dict[str, str]] = {}
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            source_id = item.get("source_id")
            source_kind = item.get("source_kind")
            if not isinstance(source_id, str) or not isinstance(source_kind, str):
                continue
            provenance[(source_kind, source_id)] = {
                "source_id": source_id,
                "source_kind": source_kind,
            }
    return sorted(provenance.values(), key=_provenance_key)


def _provenance_key(item: dict[str, str]) -> tuple[int, str, str]:
    try:
        priority = _SOURCE_PRIORITY[SourceKind(item["source_kind"])]
    except (KeyError, ValueError):
        priority = len(_SOURCE_PRIORITY)
    return priority, item.get("source_id", ""), item.get("source_kind", "")


def _primary_evidence_source(evidence: dict[str, Any]) -> str:
    provenance = evidence.get("_workbench_provenance")
    if isinstance(provenance, list) and provenance:
        source_id = provenance[0].get("source_id")
        if isinstance(source_id, str):
            return f"evidence from {source_id}"
    return "deterministic evidence"


def _evidence_record_key(record: RunRecord) -> tuple[Any, ...]:
    return (_SOURCE_PRIORITY[record.source_kind],) + _record_key(record)


def _deterministic_evidence_records(
    records: Iterable[RunRecord],
) -> list[RunRecord]:
    lightweight_order = sorted(records, key=_evidence_record_key)
    ordered: list[RunRecord] = []
    for _, group in groupby(lightweight_order, key=_evidence_record_key):
        tied = list(group)
        if len(tied) > 1:
            tied.sort(key=_record_evidence_digest)
        ordered.extend(tied)
    return ordered


def _deterministic_tie_records(records: Iterable[RunRecord]) -> list[RunRecord]:
    lightweight_order = sorted(records, key=_record_key)
    ordered: list[RunRecord] = []
    for _, group in groupby(lightweight_order, key=_record_key):
        tied = list(group)
        if len(tied) > 1:
            tied.sort(key=_record_evidence_digest)
        ordered.extend(tied)
    return ordered


def _record_evidence_digest(record: RunRecord) -> bytes:
    digest = sha256()
    _update_structural_digest(digest, record.acts)
    _update_structural_digest(digest, record.nodes)
    _update_structural_digest(digest, record.replay_by_node)
    _update_structural_digest(digest, record.warnings)
    return digest.digest()


def _update_structural_digest(digest: Any, value: Any) -> None:
    if value is None:
        digest.update(b"n;")
        return
    if isinstance(value, bool):
        digest.update(b"b1;" if value else b"b0;")
        return
    if isinstance(value, int):
        digest.update(f"i{value};".encode("ascii"))
        return
    if isinstance(value, float):
        digest.update(f"f{value.hex()};".encode("ascii"))
        return
    if isinstance(value, str):
        digest.update(f"s{len(value)}:".encode("ascii"))
        for start in range(0, len(value), 8_192):
            digest.update(value[start : start + 8_192].encode("utf-8"))
        digest.update(b";")
        return
    if isinstance(value, (list, tuple)):
        digest.update(f"l{len(value)}:[".encode("ascii"))
        for item in value:
            _update_structural_digest(digest, item)
        digest.update(b"]")
        return
    if isinstance(value, dict):
        digest.update(f"d{len(value)}:{{".encode("ascii"))
        for key in sorted(value):
            _update_structural_digest(digest, key)
            _update_structural_digest(digest, value[key])
        digest.update(b"}")
        return
    digest.update(f"x{type(value).__name__}:{repr(value)};".encode("utf-8"))


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _record_key(record: RunRecord) -> tuple[str, ...]:
    """Order provenance without serializing acts, nodes, or replay evidence."""

    return (
        record.source_id,
        record.source_kind.value,
        record.run_id,
        *(
            _lightweight_value(getattr(record.metadata, field.name))
            for field in fields(RunMetadata)
        ),
        *(_lightweight_value(getattr(record.outcome, field.name)) for field in fields(RunOutcome)),
        *(_lightweight_value(getattr(record.coverage, field.name)) for field in fields(Coverage)),
        *(
            _lightweight_value(getattr(record.capabilities, field.name))
            for field in fields(Capabilities)
        ),
        str(len(record.acts)),
        str(len(record.nodes)),
        str(len(record.replay_by_node)),
    )


def _lightweight_value(value: Any) -> str:
    if value is None:
        return "0:"
    if isinstance(value, RunStatus):
        return f"1:{value.value}"
    if isinstance(value, SourceKind):
        return f"1:{value.value}"
    if type(value) in {bool, int, float, str}:
        return f"1:{type(value).__name__}:{value!r}"
    if isinstance(value, tuple):
        return "2:" + "\0".join(_lightweight_value(item) for item in value)
    return f"3:{type(value).__name__}:{repr(value)[:256]}"


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
