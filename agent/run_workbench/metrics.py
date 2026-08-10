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
from dataclasses import dataclass, field, fields, is_dataclass
from hashlib import sha256
import heapq
import json
import math
from typing import Any, Iterable

from .models import RunRecord, RunStatus


_GAMEPLAY_STATUSES = frozenset({RunStatus.WIN, RunStatus.DEAD})
TREND_SAMPLE_LIMIT = 512
COMPARISON_DISTINCT_LIMIT = 4_096
COMPARISON_TYPE_LABEL_LIMIT = 64


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
    trend_eligible_n: int
    trend_timestamped_n: int
    trend_unknown_time_n: int
    trend_sampled_n: int
    trend_sample_limit: int
    trend_sampling_method: str

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
class ComparisonReadiness:
    ready: bool
    missing_axes: tuple[str, ...]
    mixed_axes: tuple[str, ...]
    invalid_axes: tuple[str, ...]
    seed_count: int
    seed_complete: bool
    comparison_signature: str | None

    def to_dict(self) -> dict[str, Any]:
        return _to_json_value(self)


@dataclass(frozen=True, order=True)
class _TrendSeed:
    timestamp: float
    source_id: str
    run_id: str
    status: str
    global_floor: int | None


@dataclass(frozen=True)
class _StringDetails:
    values: frozenset[str]
    missing: bool
    invalid_types: tuple[str, ...]
    distinct_count: int
    overflow: bool = False
    invalid_types_overflow: bool = False

    @property
    def has_invalid_types(self) -> bool:
        return bool(self.invalid_types or self.invalid_types_overflow)


@dataclass(frozen=True)
class _AscensionDetails:
    values: frozenset[int]
    missing: bool
    invalid: bool
    overflow: bool = False


@dataclass
class _BoundedStrings:
    values: set[str] = field(default_factory=set)
    missing: bool = False
    invalid_types: set[str] = field(default_factory=set)
    distinct_count: int = 0
    overflow: bool = False
    invalid_types_overflow: bool = False

    def add(self, value: object) -> None:
        if type(value) is str:
            value = value.strip()
            if not value:
                self.missing = True
                return
            if not self.overflow:
                previous_count = len(self.values)
                self.values.add(value)
                if len(self.values) > previous_count:
                    self.distinct_count += 1
                if self.distinct_count > COMPARISON_DISTINCT_LIMIT:
                    self.values.clear()
                    self.distinct_count = COMPARISON_DISTINCT_LIMIT + 1
                    self.overflow = True
        elif value is None:
            self.missing = True
        else:
            if not self.invalid_types_overflow:
                label = _bounded_type_label(value)
                if (
                    label not in self.invalid_types
                    and len(self.invalid_types) >= COMPARISON_DISTINCT_LIMIT
                ):
                    self.invalid_types_overflow = True
                else:
                    self.invalid_types.add(label)

    def details(self) -> _StringDetails:
        return _StringDetails(
            values=frozenset(self.values),
            missing=self.missing,
            invalid_types=tuple(sorted(self.invalid_types)),
            distinct_count=self.distinct_count,
            overflow=self.overflow,
            invalid_types_overflow=self.invalid_types_overflow,
        )


def _bounded_type_label(value: object) -> str:
    name = type.__getattribute__(type(value), "__name__")
    if len(name) <= COMPARISON_TYPE_LABEL_LIMIT:
        return name
    digest = sha256(name.encode("utf-8", errors="backslashreplace")).hexdigest()[:12]
    marker = f"...#{digest}"
    prefix_length = COMPARISON_TYPE_LABEL_LIMIT - len(marker)
    return f"{name[:prefix_length]}{marker}"


@dataclass
class _BoundedAscensions:
    values: set[int] = field(default_factory=set)
    missing: bool = False
    invalid: bool = False
    overflow: bool = False

    def add(self, value: object) -> None:
        if value is None:
            self.missing = True
        elif type(value) is int and 0 <= value <= 10:
            if not self.overflow:
                self.values.add(value)
                if len(self.values) > COMPARISON_DISTINCT_LIMIT:
                    self.values.clear()
                    self.overflow = True
        else:
            self.invalid = True

    def details(self) -> _AscensionDetails:
        return _AscensionDetails(
            values=frozenset(self.values),
            missing=self.missing,
            invalid=self.invalid,
            overflow=self.overflow,
        )


@dataclass
class _ComparisonAccumulator:
    valid_n: int = 0
    axes: dict[str, _BoundedStrings] = field(
        default_factory=lambda: {
            "character": _BoundedStrings(),
            "game_version": _BoundedStrings(),
            "evaluation_mode": _BoundedStrings(),
            "scenario": _BoundedStrings(),
        }
    )
    seeds: _BoundedStrings = field(default_factory=_BoundedStrings)
    ascensions: _BoundedAscensions = field(default_factory=_BoundedAscensions)

    def observe(self, record: RunRecord) -> None:
        if record.outcome.status not in _GAMEPLAY_STATUSES:
            return
        self.valid_n += 1
        for attribute, details in self.axes.items():
            details.add(getattr(record.metadata, attribute))
        self.seeds.add(record.metadata.seed)
        self.ascensions.add(record.metadata.ascension)

    def ascension_details(self) -> _AscensionDetails:
        return self.ascensions.details()


def describe_comparison_readiness(
    records: Iterable[RunRecord],
) -> ComparisonReadiness:
    """Describe whether valid cohort results have a stable comparison key."""

    accumulator = _ComparisonAccumulator()
    for record in records:
        accumulator.observe(record)

    missing_axes: list[str] = []
    mixed_axes: list[str] = []
    invalid_axes: list[str] = []
    resolved: dict[str, str | int] = {}

    for axis in (
        "character",
        "game_version",
        "evaluation_mode",
        "scenario",
    ):
        details = accumulator.axes[axis].details()
        if details.missing:
            missing_axes.append(axis)
        if (
            len(details.values) > 1
            or (details.missing and details.values)
            or (details.has_invalid_types and details.values)
            or details.overflow
        ):
            mixed_axes.append(axis)
        if details.has_invalid_types or details.overflow:
            invalid_axes.append(axis)
        if (
            len(details.values) == 1
            and not details.missing
            and not details.has_invalid_types
            and not details.overflow
        ):
            resolved[axis] = next(iter(details.values))

    ascension = accumulator.ascension_details()
    if ascension.missing:
        missing_axes.append("ascension")
    if (
        len(ascension.values) > 1
        or (ascension.missing and ascension.values)
        or (ascension.invalid and ascension.values)
        or ascension.overflow
    ):
        mixed_axes.append("ascension")
    if ascension.invalid or ascension.overflow:
        invalid_axes.append("ascension")
    if (
        len(ascension.values) == 1
        and not ascension.missing
        and not ascension.invalid
        and not ascension.overflow
    ):
        resolved["ascension"] = next(iter(ascension.values))

    seeds = accumulator.seeds.details()
    if seeds.missing:
        missing_axes.append("seed")
    if seeds.has_invalid_types or seeds.overflow:
        invalid_axes.append("seed")
    seed_complete = bool(seeds.values) and not (
        seeds.missing or seeds.has_invalid_types or seeds.overflow
    )

    if not accumulator.valid_n:
        invalid_axes.append("valid_results")

    ready = (
        accumulator.valid_n > 0
        and not missing_axes
        and not mixed_axes
        and not invalid_axes
        and seed_complete
        and len(resolved) == 5
    )
    comparison_signature = None
    if ready:
        signature_payload = {**resolved, "seeds": sorted(seeds.values)}
        comparison_signature = sha256(
            json.dumps(
                signature_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    return ComparisonReadiness(
        ready=ready,
        missing_axes=tuple(missing_axes),
        mixed_axes=tuple(mixed_axes),
        invalid_axes=tuple(invalid_axes),
        seed_count=seeds.distinct_count,
        seed_complete=seed_complete,
        comparison_signature=comparison_signature,
    )


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

    all_n = 0
    valid_n = 0
    valid_floor_n = 0
    technical_n = 0
    technical_floor_n = 0
    excluded_n = 0
    wins = 0
    act2_entries = 0
    valid_floor_counts: Counter[int] = Counter()
    distribution_counts: Counter[int] = Counter()
    trend_eligible_n = 0
    trend_timestamped_n = 0
    trend_unknown_time_n = 0
    trend_heap: list[tuple[int, _TrendSeed]] = []

    for record in records:
        all_n += 1
        status = record.outcome.status
        floor = _normalize_floor(record.outcome.max_global_floor)
        is_valid = status in _GAMEPLAY_STATUSES
        is_technical = status.is_technical
        if is_valid:
            valid_n += 1
            wins += status is RunStatus.WIN
            if floor is not None:
                valid_floor_n += 1
                valid_floor_counts[floor] += 1
                distribution_counts[floor] += 1
                act2_entries += floor >= 18
        elif is_technical:
            technical_n += 1
            if floor is not None:
                technical_floor_n += 1
                if include_technical:
                    distribution_counts[floor] += 1
        else:
            excluded_n += 1

        if is_valid or (include_technical and is_technical):
            trend_eligible_n += 1
            timestamp = _effective_timestamp(record)
            if timestamp is None:
                trend_unknown_time_n += 1
            else:
                trend_timestamped_n += 1
                seed = _TrendSeed(
                    timestamp=timestamp,
                    source_id=record.source_id,
                    run_id=record.run_id,
                    status=status.value,
                    global_floor=floor,
                )
                priority = _trend_priority(seed)
                item = (-priority, seed)
                if len(trend_heap) < TREND_SAMPLE_LIMIT:
                    heapq.heappush(trend_heap, item)
                elif priority < -trend_heap[0][0]:
                    heapq.heapreplace(trend_heap, item)

    histogram = tuple(
        HistogramPoint(global_floor=floor, count=count)
        for floor, count in sorted(distribution_counts.items())
    )

    funnel = (
        FunnelPoint(
            "all_runs",
            all_n,
            all_n,
            _rate(all_n, all_n),
        ),
        FunnelPoint(
            "floor_bearing",
            valid_floor_n,
            valid_n,
            _rate(valid_floor_n, valid_n),
        ),
        _floor_funnel_point_from_counts("act1_boss_or_later", 17, valid_floor_counts),
        _floor_funnel_point_from_counts("act2_entry", 18, valid_floor_counts),
        _floor_funnel_point_from_counts("act2_boss_or_later", 34, valid_floor_counts),
        _floor_funnel_point_from_counts("act3_entry", 35, valid_floor_counts),
        FunnelPoint("completion", wins, valid_n, _rate(wins, valid_n)),
    )

    selected_trend = sorted(seed for _, seed in trend_heap)
    cumulative_floor_sum = 0
    cumulative_floor_n = 0
    trend_points: list[TrendPoint] = []
    for seed in selected_trend:
        if seed.global_floor is not None:
            cumulative_floor_sum += seed.global_floor
            cumulative_floor_n += 1
        trend_points.append(
            TrendPoint(
                run_id=seed.run_id,
                source_id=seed.source_id,
                timestamp=seed.timestamp,
                status=seed.status,
                global_floor=seed.global_floor,
                cumulative_avg_global_floor=(
                    cumulative_floor_sum / cumulative_floor_n
                    if cumulative_floor_n
                    else None
                ),
            )
        )

    floor_n = sum(distribution_counts.values())
    floor_sum = sum(floor * count for floor, count in distribution_counts.items())

    return CohortSummary(
        all_n=all_n,
        valid_n=valid_n,
        valid_floor_n=valid_floor_n,
        floor_n=floor_n,
        technical_n=technical_n,
        technical_floor_n=technical_floor_n,
        excluded_n=excluded_n,
        win_n=wins,
        win_denominator=valid_n,
        win_rate=_rate(wins, valid_n),
        avg_global_floor=floor_sum / floor_n if floor_n else None,
        median_global_floor=_counter_median(distribution_counts),
        max_global_floor=max(distribution_counts) if distribution_counts else None,
        act2_entry_n=act2_entries,
        act2_entry_denominator=valid_floor_n,
        act2_entry_rate=_rate(act2_entries, valid_floor_n),
        include_technical=include_technical,
        histogram=histogram,
        funnel=funnel,
        trend=tuple(trend_points),
        trend_eligible_n=trend_eligible_n,
        trend_timestamped_n=trend_timestamped_n,
        trend_unknown_time_n=trend_unknown_time_n,
        trend_sampled_n=len(trend_points),
        trend_sample_limit=TREND_SAMPLE_LIMIT,
        trend_sampling_method=(
            "all_timestamped"
            if trend_timestamped_n <= TREND_SAMPLE_LIMIT
            else "deterministic_hash"
        ),
    )


def _floor_funnel_point_from_counts(
    key: str, threshold: int, floors: Counter[int]
) -> FunnelPoint:
    denominator = sum(floors.values())
    count = sum(amount for floor, amount in floors.items() if floor >= threshold)
    return FunnelPoint(
        key=key,
        count=count,
        denominator=denominator,
        rate=_rate(count, denominator),
        min_global_floor=threshold,
    )


def _counter_median(counts: Counter[int]) -> float | None:
    total = sum(counts.values())
    if total == 0:
        return None
    left_rank = (total - 1) // 2
    right_rank = total // 2
    cumulative = 0
    left: int | None = None
    for floor, count in sorted(counts.items()):
        previous = cumulative
        cumulative += count
        if left is None and previous <= left_rank < cumulative:
            left = floor
        if previous <= right_rank < cumulative:
            return (left + floor) / 2 if left is not None else float(floor)
    raise AssertionError("floor histogram count mismatch")


def _trend_priority(seed: _TrendSeed) -> int:
    payload = "\0".join(
        (
            repr(seed.timestamp),
            seed.source_id,
            seed.run_id,
            seed.status,
            repr(seed.global_floor),
        )
    )
    return int.from_bytes(sha256(payload.encode("utf-8")).digest(), "big")


def _string_details(values: Iterable[object]) -> _StringDetails:
    details = _BoundedStrings()
    for value in values:
        details.add(value)
    return details.details()


def _axis_value(
    records: tuple[RunRecord, ...], axis: str, label: str, cohort: str
) -> tuple[str | None, tuple[str, ...]]:
    details = _string_details(getattr(record.metadata, axis) for record in records)
    return _axis_value_from_details(details, label, cohort)


def _axis_value_from_details(
    details: _StringDetails, label: str, cohort: str
) -> tuple[str | None, tuple[str, ...]]:
    rendered_values = ", ".join(sorted(details.values))
    rendered_types = _invalid_type_summary(details)
    if details.overflow:
        return None, (
            f"{cohort} {label} has more than "
            f"{COMPARISON_DISTINCT_LIMIT} distinct values",
        )
    if details.has_invalid_types and not details.values and not details.missing:
        return None, (
            f"{cohort} {label} is invalid: expected nonempty string; "
            f"types={rendered_types}",
        )
    if not details.values and details.missing and not details.has_invalid_types:
        return None, (f"{cohort} {label} is missing",)
    if len(details.values) > 1 or details.missing or details.has_invalid_types:
        parts: list[str] = []
        if details.missing:
            parts.append("missing")
        if rendered_values:
            parts.append(f"values={rendered_values}")
        if rendered_types:
            parts.append(f"invalid types={rendered_types}")
        return None, (f"{cohort} {label} is mixed: {'; '.join(parts)}",)
    return next(iter(details.values)), ()


def _invalid_type_summary(details: _StringDetails) -> str:
    rendered = ", ".join(details.invalid_types)
    if not details.invalid_types_overflow:
        return rendered
    suffix = "additional type labels omitted"
    return f"{rendered}; {suffix}" if rendered else suffix


def _ascension_details(records: tuple[RunRecord, ...]) -> _AscensionDetails:
    details = _BoundedAscensions()
    for record in records:
        details.add(record.metadata.ascension)
    return details.details()


def _ascension_axis_value(
    records: tuple[RunRecord, ...], cohort: str
) -> tuple[int | None, tuple[str, ...]]:
    details = _ascension_details(records)
    return _ascension_axis_value_from_details(details, cohort)


def _ascension_axis_value_from_details(
    details: _AscensionDetails, cohort: str
) -> tuple[int | None, tuple[str, ...]]:
    rendered_values = ", ".join(str(value) for value in sorted(details.values))
    if details.overflow:
        return None, (
            f"{cohort} ascension has more than "
            f"{COMPARISON_DISTINCT_LIMIT} distinct values",
        )
    if details.invalid and not details.values and not details.missing:
        return None, (
            f"{cohort} ascension is invalid: expected int in range 0..10",
        )
    if not details.values and details.missing and not details.invalid:
        return None, (f"{cohort} ascension is missing",)
    if len(details.values) > 1 or details.missing or details.invalid:
        parts: list[str] = []
        if details.missing:
            parts.append("missing")
        if rendered_values:
            parts.append(f"values={rendered_values}")
        if details.invalid:
            parts.append("invalid")
        return None, (f"{cohort} ascension is mixed: {'; '.join(parts)}",)
    return next(iter(details.values)), ()


def _seed_details(records: tuple[RunRecord, ...]) -> _StringDetails:
    return _string_details(record.metadata.seed for record in records)


def _observed_records(
    records: Iterable[RunRecord], accumulator: _ComparisonAccumulator
) -> Iterable[RunRecord]:
    for record in records:
        accumulator.observe(record)
        yield record


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

    current_details = _ComparisonAccumulator()
    baseline_details = _ComparisonAccumulator()
    current_summary = summarize_cohort(_observed_records(current, current_details))
    baseline_summary = summarize_cohort(_observed_records(baseline, baseline_details))

    reasons: list[str] = []
    notes: list[str] = []
    if not current_details.valid_n:
        reasons.append("current cohort has no valid gameplay results")
    if not baseline_details.valid_n:
        reasons.append("baseline cohort has no valid gameplay results")

    paired = False
    if current_details.valid_n and baseline_details.valid_n:
        axes = (
            ("character", "character"),
            ("game_version", "version"),
            ("evaluation_mode", "evaluation mode"),
            ("scenario", "scenario"),
        )
        for attribute, label in axes:
            current_value, current_errors = _axis_value_from_details(
                current_details.axes[attribute].details(), label, "current"
            )
            baseline_value, baseline_errors = _axis_value_from_details(
                baseline_details.axes[attribute].details(), label, "baseline"
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

        current_ascension, current_ascension_errors = _ascension_axis_value_from_details(
            current_details.ascension_details(), "current"
        )
        baseline_ascension, baseline_ascension_errors = _ascension_axis_value_from_details(
            baseline_details.ascension_details(), "baseline"
        )
        reasons.extend(current_ascension_errors)
        reasons.extend(baseline_ascension_errors)
        if (
            not current_ascension_errors
            and not baseline_ascension_errors
            and current_ascension != baseline_ascension
        ):
            reasons.append(
                "ascension mismatch: "
                f"current={current_ascension}, baseline={baseline_ascension}"
            )
        elif bool(current_ascension_errors) != bool(baseline_ascension_errors):
            reasons.append(
                "ascension mismatch: one cohort has invalid or incomplete metadata"
            )

        current_seed_details = current_details.seeds.details()
        baseline_seed_details = baseline_details.seeds.details()
        current_seeds = current_seed_details.values
        baseline_seeds = baseline_seed_details.values
        paired = (
            bool(current_seeds)
            and current_seeds == baseline_seeds
            and not current_seed_details.missing
            and not baseline_seed_details.missing
            and not current_seed_details.has_invalid_types
            and not baseline_seed_details.has_invalid_types
            and not current_seed_details.overflow
            and not baseline_seed_details.overflow
        )
        if require_paired_seeds:
            if current_seed_details.overflow:
                reasons.append(
                    "current seed set exceeds bounded comparison limit"
                )
            if baseline_seed_details.overflow:
                reasons.append(
                    "baseline seed set exceeds bounded comparison limit"
                )
            if current_seed_details.missing:
                reasons.append("current seed set has missing seed values")
            if current_seed_details.has_invalid_types:
                reasons.append(
                    "current seed set has invalid seed types: "
                    f"{_invalid_type_summary(current_seed_details)}"
                )
            if baseline_seed_details.missing:
                reasons.append("baseline seed set has missing seed values")
            if baseline_seed_details.has_invalid_types:
                reasons.append(
                    "baseline seed set has invalid seed types: "
                    f"{_invalid_type_summary(baseline_seed_details)}"
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
