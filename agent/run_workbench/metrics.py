"""Deterministic, dependency-free cohort metrics for the run workbench.

``valid_n`` always means completed gameplay outcomes (wins and deaths).  The
optional ``include_technical`` flag only expands the floor distribution and
histogram with floor-bearing technical failures, while the trend retains all
technical failures.  It never changes gameplay or win denominators.
``valid_floor_n`` is the gameplay-only floor denominator, while ``floor_n`` is
the number of points in the selected floor distribution.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields, is_dataclass
import math
from statistics import mean, median
from typing import Any, Iterable

from .models import RunRecord, RunStatus


_GAMEPLAY_STATUSES = frozenset({RunStatus.WIN, RunStatus.DEAD})


def _to_json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _to_json_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Metric serialization requires string dict keys")
        return {key: _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Metric serialization does not support non-finite floats")
        return value
    raise TypeError(
        f"Metric serialization does not support {type(value).__name__} values"
    )


@dataclass(frozen=True)
class HistogramPoint:
    global_floor: int
    count: int


@dataclass(frozen=True)
class FunnelPoint:
    key: str
    count: int
    denominator: int
    rate: float | None
    min_global_floor: int | None = None


@dataclass(frozen=True)
class TrendPoint:
    run_id: str
    source_id: str
    timestamp: float | None
    status: str
    global_floor: int | None
    cumulative_avg_global_floor: float | None


@dataclass(frozen=True)
class CohortSummary:
    """Aggregate metrics with distinct gameplay and floor denominators."""

    all_n: int
    valid_n: int
    valid_floor_n: int
    floor_n: int
    technical_n: int
    technical_floor_n: int
    excluded_n: int
    win_n: int
    win_denominator: int
    win_rate: float | None
    avg_global_floor: float | None
    median_global_floor: float | None
    max_global_floor: int | None
    act2_entry_n: int
    act2_entry_denominator: int
    act2_entry_rate: float | None
    include_technical: bool
    histogram: tuple[HistogramPoint, ...]
    funnel: tuple[FunnelPoint, ...]
    trend: tuple[TrendPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return _to_json_value(self)


@dataclass(frozen=True)
class ComparisonResult:
    current: CohortSummary
    baseline: CohortSummary
    comparable: bool
    paired: bool
    mismatch_reasons: tuple[str, ...]
    notes: tuple[str, ...]
    avg_global_floor_delta: float | None
    median_global_floor_delta: float | None
    max_global_floor_delta: int | None
    act2_entry_rate_delta: float | None
    win_rate_delta: float | None

    def to_dict(self) -> dict[str, Any]:
        return _to_json_value(self)


@dataclass(frozen=True)
class _MetricRecord:
    record: RunRecord
    floor: int | None


@dataclass(frozen=True)
class _StringDetails:
    values: frozenset[str]
    missing: bool
    invalid_types: tuple[str, ...]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _finite_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _normalize_floor(value: object) -> int | None:
    """Return a positive, finite, exactly integral floor or ``None``."""

    if type(value) is not int or value <= 0:
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return value


def _trend_sort_key(record: RunRecord) -> tuple[object, ...]:
    timestamp = _effective_timestamp(record)
    return (
        timestamp is None,
        timestamp if timestamp is not None else 0.0,
        record.source_id,
        record.run_id,
    )


def _effective_timestamp(record: RunRecord) -> float | None:
    ended_at = _finite_timestamp(record.metadata.ended_at)
    if ended_at is not None:
        return ended_at
    return _finite_timestamp(record.metadata.started_at)


def summarize_cohort(
    records: Iterable[RunRecord], *, include_technical: bool = False
) -> CohortSummary:
    """Summarize a cohort without mutating it or treating missing floors as zero."""

    materialized = tuple(
        _MetricRecord(
            record=record,
            floor=_normalize_floor(record.outcome.max_global_floor),
        )
        for record in records
    )
    valid = tuple(
        item
        for item in materialized
        if item.record.outcome.status in _GAMEPLAY_STATUSES
    )
    technical = tuple(
        item for item in materialized if item.record.outcome.status.is_technical
    )
    excluded_n = len(materialized) - len(valid) - len(technical)

    valid_with_floor = tuple(item for item in valid if item.floor is not None)
    technical_with_floor = tuple(item for item in technical if item.floor is not None)
    distribution_records = valid_with_floor
    if include_technical:
        distribution_records += technical_with_floor

    numeric_floors = tuple(
        item.floor for item in distribution_records if item.floor is not None
    )

    wins = sum(item.record.outcome.status == RunStatus.WIN for item in valid)
    valid_floors = tuple(
        item.floor for item in valid_with_floor if item.floor is not None
    )
    act2_entries = sum(floor >= 18 for floor in valid_floors)

    histogram = tuple(
        HistogramPoint(global_floor=floor, count=count)
        for floor, count in sorted(Counter(numeric_floors).items())
    )

    valid_floor_count = len(valid_with_floor)
    funnel = (
        FunnelPoint(
            "all_runs",
            len(materialized),
            len(materialized),
            _rate(len(materialized), len(materialized)),
        ),
        FunnelPoint(
            "floor_bearing",
            valid_floor_count,
            len(valid),
            _rate(valid_floor_count, len(valid)),
        ),
        _floor_funnel_point("act1_boss_or_later", 17, valid_floors),
        _floor_funnel_point("act2_entry", 18, valid_floors),
        _floor_funnel_point("act2_boss_or_later", 34, valid_floors),
        _floor_funnel_point("act3_entry", 35, valid_floors),
        FunnelPoint("completion", wins, len(valid), _rate(wins, len(valid))),
    )

    trend_records: tuple[_MetricRecord, ...] = valid
    if include_technical:
        trend_records += technical
    cumulative_floors: list[int] = []
    trend_points: list[TrendPoint] = []
    for item in sorted(
        trend_records, key=lambda candidate: _trend_sort_key(candidate.record)
    ):
        record = item.record
        floor = item.floor
        if floor is not None:
            cumulative_floors.append(floor)
        trend_points.append(
            TrendPoint(
                run_id=record.run_id,
                source_id=record.source_id,
                timestamp=_effective_timestamp(record),
                status=record.outcome.status.value,
                global_floor=floor,
                cumulative_avg_global_floor=(
                    float(mean(cumulative_floors)) if cumulative_floors else None
                ),
            )
        )

    return CohortSummary(
        all_n=len(materialized),
        valid_n=len(valid),
        valid_floor_n=valid_floor_count,
        floor_n=len(numeric_floors),
        technical_n=len(technical),
        technical_floor_n=len(technical_with_floor),
        excluded_n=excluded_n,
        win_n=wins,
        win_denominator=len(valid),
        win_rate=_rate(wins, len(valid)),
        avg_global_floor=float(mean(numeric_floors)) if numeric_floors else None,
        median_global_floor=float(median(numeric_floors)) if numeric_floors else None,
        max_global_floor=max(numeric_floors) if numeric_floors else None,
        act2_entry_n=act2_entries,
        act2_entry_denominator=valid_floor_count,
        act2_entry_rate=_rate(act2_entries, valid_floor_count),
        include_technical=include_technical,
        histogram=histogram,
        funnel=funnel,
        trend=tuple(trend_points),
    )


def _floor_funnel_point(
    key: str, threshold: int, floors: tuple[int, ...]
) -> FunnelPoint:
    count = sum(floor >= threshold for floor in floors)
    return FunnelPoint(
        key=key,
        count=count,
        denominator=len(floors),
        rate=_rate(count, len(floors)),
        min_global_floor=threshold,
    )


def _string_details(values: Iterable[object]) -> _StringDetails:
    valid_values: list[str] = []
    missing = False
    invalid_types: list[str] = []
    for value in values:
        if type(value) is str and value:
            valid_values.append(value)
        elif value is None or (type(value) is str and not value):
            missing = True
        else:
            invalid_types.append(type(value).__name__)
    return _StringDetails(
        values=frozenset(valid_values),
        missing=missing,
        invalid_types=tuple(sorted(set(invalid_types))),
    )


def _axis_value(
    records: tuple[RunRecord, ...], axis: str, label: str, cohort: str
) -> tuple[str | None, tuple[str, ...]]:
    details = _string_details(getattr(record.metadata, axis) for record in records)
    rendered_values = ", ".join(sorted(details.values))
    rendered_types = ", ".join(details.invalid_types)
    if details.invalid_types and not details.values and not details.missing:
        return None, (
            f"{cohort} {label} is invalid: expected nonempty string; "
            f"types={rendered_types}",
        )
    if not details.values and details.missing and not details.invalid_types:
        return None, (f"{cohort} {label} is missing",)
    if len(details.values) > 1 or details.missing or details.invalid_types:
        parts: list[str] = []
        if details.missing:
            parts.append("missing")
        if rendered_values:
            parts.append(f"values={rendered_values}")
        if rendered_types:
            parts.append(f"invalid types={rendered_types}")
        return None, (f"{cohort} {label} is mixed: {'; '.join(parts)}",)
    return next(iter(details.values)), ()


def _seed_details(records: tuple[RunRecord, ...]) -> _StringDetails:
    return _string_details(record.metadata.seed for record in records)


def _delta(current: float | int | None, baseline: float | int | None) -> Any:
    if current is None or baseline is None:
        return None
    return current - baseline


def compare_cohorts(
    current: Iterable[RunRecord],
    baseline: Iterable[RunRecord],
    *,
    allow_cross_version: bool = False,
    require_paired_seeds: bool = True,
) -> ComparisonResult:
    """Compare cohorts only when their gameplay-result metadata is compatible."""

    current_records = tuple(current)
    baseline_records = tuple(baseline)
    current_summary = summarize_cohort(current_records)
    baseline_summary = summarize_cohort(baseline_records)
    current_valid = tuple(
        record for record in current_records if record.outcome.status in _GAMEPLAY_STATUSES
    )
    baseline_valid = tuple(
        record for record in baseline_records if record.outcome.status in _GAMEPLAY_STATUSES
    )

    reasons: list[str] = []
    notes: list[str] = []
    if not current_valid:
        reasons.append("current cohort has no valid gameplay results")
    if not baseline_valid:
        reasons.append("baseline cohort has no valid gameplay results")

    paired = False
    if current_valid and baseline_valid:
        axes = (
            ("character", "character"),
            ("game_version", "version"),
            ("evaluation_mode", "evaluation mode"),
            ("scenario", "scenario"),
        )
        for attribute, label in axes:
            current_value, current_errors = _axis_value(
                current_valid, attribute, label, "current"
            )
            baseline_value, baseline_errors = _axis_value(
                baseline_valid, attribute, label, "baseline"
            )
            reasons.extend(current_errors)
            reasons.extend(baseline_errors)
            if (
                not current_errors
                and not baseline_errors
                and current_value != baseline_value
            ):
                if attribute == "game_version" and allow_cross_version:
                    notes.append(
                        "cross-version comparison: "
                        f"current={current_value}, baseline={baseline_value}"
                    )
                else:
                    reasons.append(
                        f"{label} mismatch: "
                        f"current={current_value}, baseline={baseline_value}"
                    )

        current_seed_details = _seed_details(current_valid)
        baseline_seed_details = _seed_details(baseline_valid)
        current_seeds = current_seed_details.values
        baseline_seeds = baseline_seed_details.values
        paired = (
            bool(current_seeds)
            and current_seeds == baseline_seeds
            and not current_seed_details.missing
            and not baseline_seed_details.missing
            and not current_seed_details.invalid_types
            and not baseline_seed_details.invalid_types
        )
        if require_paired_seeds:
            if current_seed_details.missing:
                reasons.append("current seed set has missing seed values")
            if current_seed_details.invalid_types:
                reasons.append(
                    "current seed set has invalid seed types: "
                    f"{', '.join(current_seed_details.invalid_types)}"
                )
            if baseline_seed_details.missing:
                reasons.append("baseline seed set has missing seed values")
            if baseline_seed_details.invalid_types:
                reasons.append(
                    "baseline seed set has invalid seed types: "
                    f"{', '.join(baseline_seed_details.invalid_types)}"
                )
            if current_seeds != baseline_seeds:
                reasons.append(
                    "fixed seed set mismatch: "
                    f"current={sorted(current_seeds)}, baseline={sorted(baseline_seeds)}"
                )
        elif not paired:
            notes.append(
                "non-paired comparison: fixed seed sets differ or contain "
                "missing/invalid values"
            )

    comparable = not reasons
    return ComparisonResult(
        current=current_summary,
        baseline=baseline_summary,
        comparable=comparable,
        paired=paired,
        mismatch_reasons=tuple(reasons),
        notes=tuple(notes),
        avg_global_floor_delta=(
            _delta(current_summary.avg_global_floor, baseline_summary.avg_global_floor)
            if comparable
            else None
        ),
        median_global_floor_delta=(
            _delta(current_summary.median_global_floor, baseline_summary.median_global_floor)
            if comparable
            else None
        ),
        max_global_floor_delta=(
            _delta(current_summary.max_global_floor, baseline_summary.max_global_floor)
            if comparable
            else None
        ),
        act2_entry_rate_delta=(
            _delta(current_summary.act2_entry_rate, baseline_summary.act2_entry_rate)
            if comparable
            else None
        ),
        win_rate_delta=(
            _delta(current_summary.win_rate, baseline_summary.win_rate)
            if comparable
            else None
        ),
    )
