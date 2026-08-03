import json

import pytest

from agent.run_workbench.metrics import compare_cohorts, summarize_cohort
from agent.run_workbench.models import (
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)


def _run(
    name: str,
    *,
    status: RunStatus = RunStatus.DEAD,
    floor: int | None = 7,
    character: str | None = "ironclad",
    version: str | None = "2026.08",
    mode: str | None = "evaluation",
    scenario: str | None = "standard",
    seed: str | None = "seed-1",
    started_at: float | None = None,
    ended_at: float | None = None,
    source_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=name,
        source_id=source_id or f"source-{name}",
        source_kind=SourceKind.NATIVE_RUN,
        metadata=RunMetadata(
            character=character,
            game_version=version,
            evaluation_mode=mode,
            scenario=scenario,
            seed=seed,
            started_at=started_at,
            ended_at=ended_at,
        ),
        outcome=RunOutcome(
            status=status,
            victory=status == RunStatus.WIN,
            max_global_floor=floor,
        ),
    )


def test_summary_excludes_technical_failures_from_gameplay_aggregates():
    summary = summarize_cohort(
        [
            _run("early", floor=7, seed="a"),
            _run("late", floor=21, seed="b"),
            _run("crash", status=RunStatus.CRASH, floor=1, seed="c"),
        ]
    )

    assert summary.all_n == 3
    assert summary.valid_n == 2
    assert summary.valid_floor_n == 2
    assert summary.floor_n == 2
    assert summary.technical_n == 1
    assert summary.excluded_n == 0
    assert summary.avg_global_floor == 14.0
    assert summary.median_global_floor == 14.0
    assert summary.max_global_floor == 21
    assert summary.act2_entry_n == 1
    assert summary.act2_entry_denominator == 2
    assert summary.act2_entry_rate == 0.5


def test_missing_floor_is_not_treated_as_zero_and_denominators_are_explicit():
    summary = summarize_cohort(
        [_run("missing", floor=None), _run("known", floor=12, seed="seed-2")]
    )

    assert summary.valid_n == 2
    assert summary.valid_floor_n == 1
    assert summary.floor_n == 1
    assert summary.avg_global_floor == 12.0
    assert summary.median_global_floor == 12.0
    assert summary.max_global_floor == 12
    assert summary.act2_entry_denominator == 1
    assert summary.act2_entry_rate == 0.0


def test_win_rate_uses_all_valid_gameplay_results():
    summary = summarize_cohort(
        [_run("win", status=RunStatus.WIN, floor=None), _run("dead", floor=3, seed="b")]
    )

    assert summary.win_n == 1
    assert summary.win_denominator == 2
    assert summary.win_rate == 0.5


def test_unknown_and_in_progress_are_reported_as_excluded():
    summary = summarize_cohort(
        [
            _run("unknown", status=RunStatus.UNKNOWN, floor=99),
            _run("progress", status=RunStatus.IN_PROGRESS, floor=10, seed="b"),
        ]
    )

    assert summary.valid_n == 0
    assert summary.technical_n == 0
    assert summary.excluded_n == 2
    assert summary.floor_n == 0
    assert summary.avg_global_floor is None


def test_include_technical_adds_only_floor_bearing_failures_to_distribution():
    records = [
        _run("dead", floor=21, ended_at=1),
        _run("crash", status=RunStatus.CRASH, floor=3, seed="b", ended_at=2),
        _run(
            "timeout",
            status=RunStatus.TIMEOUT,
            floor=None,
            seed="c",
            ended_at=3,
        ),
    ]

    summary = summarize_cohort(records, include_technical=True)

    assert summary.valid_n == 1
    assert summary.valid_floor_n == 1
    assert summary.technical_n == 2
    assert summary.technical_floor_n == 1
    assert summary.floor_n == 2
    assert summary.avg_global_floor == 12.0
    assert summary.median_global_floor == 12.0
    assert summary.max_global_floor == 21
    assert summary.win_denominator == 1
    assert summary.act2_entry_denominator == 1
    assert summary.act2_entry_rate == 1.0
    assert [(point.global_floor, point.count) for point in summary.histogram] == [(3, 1), (21, 1)]
    assert [point.run_id for point in summary.trend] == ["dead", "crash", "timeout"]
    assert [point.global_floor for point in summary.trend] == [21, 3, None]
    assert [point.cumulative_avg_global_floor for point in summary.trend] == [
        21.0,
        12.0,
        12.0,
    ]


def test_histogram_funnel_and_trend_are_immutable_and_deterministic():
    records = [
        _run("late", floor=35, ended_at=3, seed="c"),
        _run("fallback-b", floor=18, started_at=2, ended_at=None, seed="b"),
        _run("early", floor=17, ended_at=1, seed="a"),
        _run("fallback-a", floor=18, started_at=2, ended_at=None, seed="d"),
        _run("win", status=RunStatus.WIN, floor=51, ended_at=4, seed="e"),
    ]

    summary = summarize_cohort(records)

    assert [(point.global_floor, point.count) for point in summary.histogram] == [
        (17, 1),
        (18, 2),
        (35, 1),
        (51, 1),
    ]
    funnel = {point.key: point for point in summary.funnel}
    assert (funnel["floor_bearing"].count, funnel["floor_bearing"].denominator) == (5, 5)
    assert funnel["act1_boss_or_later"].count == 5
    assert funnel["act2_entry"].count == 4
    assert funnel["act2_boss_or_later"].count == 2
    assert funnel["act3_entry"].count == 2
    assert (funnel["completion"].count, funnel["completion"].denominator) == (1, 5)
    assert [point.run_id for point in summary.trend] == [
        "early",
        "fallback-a",
        "fallback-b",
        "late",
        "win",
    ]
    assert [point.cumulative_avg_global_floor for point in summary.trend] == [
        17.0,
        17.5,
        pytest.approx(17.666666666666668),
        22.0,
        27.8,
    ]
    with pytest.raises(AttributeError):
        summary.histogram[0].count = 2


def test_trend_uses_started_at_as_chronological_fallback_and_puts_missing_last():
    summary = summarize_cohort(
        [
            _run("ended", floor=4, ended_at=4, source_id="z"),
            _run("started", floor=2, started_at=2, source_id="z", seed="b"),
            _run("missing-b", floor=8, source_id="b", seed="c"),
            _run("missing-a", floor=6, source_id="a", seed="d"),
        ]
    )

    assert [point.timestamp for point in summary.trend] == [2.0, 4.0, None, None]
    assert [point.run_id for point in summary.trend] == [
        "started",
        "ended",
        "missing-a",
        "missing-b",
    ]


def test_summary_consumes_generator_once_and_is_json_safe():
    generated = (_run(f"run-{floor}", floor=floor, seed=str(floor)) for floor in [7, 18])

    summary = summarize_cohort(generated)
    payload = summary.to_dict()

    assert summary.valid_n == 2
    assert payload["histogram"] == [
        {"global_floor": 7, "count": 1},
        {"global_floor": 18, "count": 1},
    ]
    json.dumps(payload, allow_nan=False)


def test_empty_summary_has_no_fabricated_rates_or_floor_values():
    summary = summarize_cohort([])

    assert summary.all_n == summary.valid_n == summary.floor_n == 0
    assert summary.avg_global_floor is None
    assert summary.median_global_floor is None
    assert summary.max_global_floor is None
    assert summary.win_rate is None
    assert summary.act2_entry_rate is None
    assert summary.histogram == ()
    assert summary.trend == ()


@pytest.mark.parametrize(
    ("axis", "override", "expected_word"),
    [
        ("character", {"character": "silent"}, "character"),
        ("version", {"version": "2026.09"}, "version"),
        ("mode", {"mode": "training"}, "evaluation mode"),
        ("scenario", {"scenario": "boss"}, "scenario"),
    ],
)
def test_comparison_rejects_each_metadata_axis(axis, override, expected_word):
    current = [_run("current", **override)]
    baseline = [_run("baseline")]

    result = compare_cohorts(current, baseline)

    assert result.comparable is False
    assert any(expected_word in reason for reason in result.mismatch_reasons), axis
    assert result.avg_global_floor_delta is None


def test_comparison_rejects_internal_mixture_and_missing_axis_values():
    mixed = [_run("one", seed="a"), _run("two", character="silent", seed="b")]
    missing = [_run("baseline", character=None)]

    result = compare_cohorts(mixed, missing, require_paired_seeds=False)

    assert result.comparable is False
    assert any("current character" in reason and "mixed" in reason for reason in result.mismatch_reasons)
    assert any("baseline character" in reason and "missing" in reason for reason in result.mismatch_reasons)


def test_allow_cross_version_bypasses_only_cross_cohort_version_mismatch():
    result = compare_cohorts(
        [_run("current", version="2026.09")],
        [_run("baseline", version="2026.08")],
        allow_cross_version=True,
    )

    assert result.comparable is True
    assert result.paired is True
    assert not any("version" in reason for reason in result.mismatch_reasons)


def test_allow_cross_version_does_not_hide_missing_or_mixed_version_metadata():
    current = [_run("missing", version=None)]

    result = compare_cohorts(
        current,
        [_run("baseline")],
        allow_cross_version=True,
    )

    assert result.comparable is False
    assert any("current version" in reason and "missing" in reason for reason in result.mismatch_reasons)


def test_strict_pairing_rejects_different_seed_sets_and_missing_seeds():
    different = compare_cohorts(
        [_run("current", seed="current")], [_run("baseline", seed="baseline")]
    )
    missing = compare_cohorts(
        [_run("current", seed=None)], [_run("baseline", seed="baseline")]
    )

    assert different.comparable is False
    assert different.paired is False
    assert any("seed set" in reason for reason in different.mismatch_reasons)
    assert missing.comparable is False
    assert any("current seed set" in reason and "missing" in reason for reason in missing.mismatch_reasons)


def test_non_paired_comparison_is_allowed_but_visibly_labeled():
    result = compare_cohorts(
        [_run("current", floor=21, seed="current")],
        [_run("baseline", floor=7, seed="baseline")],
        require_paired_seeds=False,
    )

    assert result.comparable is True
    assert result.paired is False
    assert result.mismatch_reasons == ()
    assert any("non-paired" in note for note in result.notes)
    assert result.avg_global_floor_delta == 14.0


def test_identical_nonempty_seed_sets_are_paired_and_duplicates_do_not_matter():
    result = compare_cohorts(
        [_run("c1", seed="a"), _run("c2", seed="a")],
        [_run("b1", seed="a")],
        require_paired_seeds=False,
    )

    assert result.comparable is True
    assert result.paired is True
    assert result.notes == ()


def test_technical_noise_does_not_create_metadata_or_pairing_mismatches():
    current = [
        _run("valid", seed="same"),
        _run(
            "crash",
            status=RunStatus.CRASH,
            character="silent",
            version="other",
            mode="other",
            scenario="other",
            seed="other",
        ),
    ]

    result = compare_cohorts(current, [_run("baseline", seed="same")])

    assert result.comparable is True
    assert result.paired is True


def test_empty_valid_cohorts_fail_with_explicit_current_and_baseline_reasons():
    result = compare_cohorts(
        [_run("crash", status=RunStatus.CRASH)],
        [_run("progress", status=RunStatus.IN_PROGRESS)],
    )

    assert result.comparable is False
    assert result.mismatch_reasons == (
        "current cohort has no valid gameplay results",
        "baseline cohort has no valid gameplay results",
    )


def test_comparison_returns_summaries_and_all_requested_deltas():
    current = [_run("current", floor=21, seed="same", status=RunStatus.WIN)]
    baseline = [_run("baseline", floor=7, seed="same")]

    result = compare_cohorts(current, baseline)

    assert result.comparable is True
    assert result.current.avg_global_floor == 21.0
    assert result.baseline.avg_global_floor == 7.0
    assert result.avg_global_floor_delta == 14.0
    assert result.median_global_floor_delta == 14.0
    assert result.max_global_floor_delta == 14
    assert result.act2_entry_rate_delta == 1.0
    assert result.win_rate_delta == 1.0
    json.dumps(result.to_dict(), allow_nan=False)


def test_missing_floor_makes_floor_deltas_unavailable_without_breaking_comparison():
    result = compare_cohorts(
        [_run("current", floor=None, seed="same")],
        [_run("baseline", floor=7, seed="same")],
    )

    assert result.comparable is True
    assert result.avg_global_floor_delta is None
    assert result.median_global_floor_delta is None
    assert result.max_global_floor_delta is None
    assert result.act2_entry_rate_delta is None


def test_comparison_consumes_generators_without_mutating_records():
    current_record = _run("current", floor=18, seed="same")
    baseline_record = _run("baseline", floor=7, seed="same")

    result = compare_cohorts(
        (record for record in [current_record]),
        (record for record in [baseline_record]),
    )

    assert result.comparable is True
    assert current_record.outcome.max_global_floor == 18
    assert baseline_record.outcome.max_global_floor == 7
