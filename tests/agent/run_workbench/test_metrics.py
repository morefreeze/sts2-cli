from dataclasses import replace
import gc
import json
import weakref

import pytest

from agent.run_workbench.metrics import (
    compare_cohorts,
    describe_comparison_readiness,
    summarize_cohort,
)
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
    ascension: object = 0,
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
            ascension=ascension,
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


def test_comparison_readiness_is_complete_and_order_independent():
    records = [
        _run("seed-a", seed="seed-a"),
        _run("seed-b", seed="seed-b", status=RunStatus.WIN),
    ]

    forward = describe_comparison_readiness(records)
    reverse = describe_comparison_readiness(reversed(records))

    assert forward.ready is True
    assert forward.seed_complete is True
    assert forward.seed_count == 2
    assert forward.comparison_signature == reverse.comparison_signature
    assert forward.comparison_signature is not None
    assert forward.missing_axes == ()
    assert forward.mixed_axes == ()
    assert forward.invalid_axes == ()
    with pytest.raises(AttributeError):
        setattr(forward, "ready", False)


@pytest.mark.parametrize(
    ("records", "missing_axes", "mixed_axes", "invalid_axes"),
    [
        ([_run("missing-version", version=None)], ("game_version",), (), ()),
        ([_run("missing-seed", seed=None)], ("seed",), (), ()),
        (
            [
                _run("version-a", version="2026.08", seed="seed-a"),
                _run("version-b", version="2026.09", seed="seed-b"),
            ],
            (),
            ("game_version",),
            (),
        ),
        ([_run("bool-ascension", ascension=True)], (), (), ("ascension",)),
        (
            [_run("in-progress", status=RunStatus.IN_PROGRESS)],
            (),
            (),
            ("valid_results",),
        ),
    ],
)
def test_comparison_readiness_rejects_missing_mixed_and_invalid_cohorts(
    records, missing_axes, mixed_axes, invalid_axes
):
    readiness = describe_comparison_readiness(records)

    assert readiness.ready is False
    assert readiness.comparison_signature is None
    assert readiness.missing_axes == missing_axes
    assert readiness.mixed_axes == mixed_axes
    assert readiness.invalid_axes == invalid_axes
    json.dumps(readiness.to_dict(), allow_nan=False)


def test_comparison_readiness_axis_diagnostics_have_stable_order():
    records = [
        _run(
            "first",
            character=None,
            version="2026.08",
            mode=7,
            ascension=True,
            seed=None,
        ),
        _run(
            "second",
            character=None,
            version="2026.09",
            mode=7,
            ascension=True,
            seed=None,
        ),
    ]

    readiness = describe_comparison_readiness(records)

    assert readiness.missing_axes == ("character", "seed")
    assert readiness.mixed_axes == ("game_version",)
    assert readiness.invalid_axes == ("evaluation_mode", "ascension")


def test_empty_comparison_readiness_reports_no_valid_results():
    readiness = describe_comparison_readiness([])

    assert readiness.ready is False
    assert readiness.missing_axes == ()
    assert readiness.mixed_axes == ()
    assert readiness.invalid_axes == ("valid_results",)
    assert readiness.seed_count == 0
    assert readiness.seed_complete is False
    assert readiness.comparison_signature is None


def test_comparison_signature_handles_unicode_long_values_and_hides_seed_list():
    long_version = "版本-🐉-" + "x" * 10_000
    record = _run("unicode", version=long_version, seed="private-seed-🌱")

    single = describe_comparison_readiness([record])
    duplicate = describe_comparison_readiness(
        [
            record,
            _run("duplicate", version=long_version, seed="private-seed-🌱"),
        ]
    )
    payload = single.to_dict()

    assert single.ready is True
    assert single.seed_count == 1
    assert single.comparison_signature == duplicate.comparison_signature
    assert "seeds" not in payload
    assert "private-seed" not in json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("field", ["version", "seed"])
def test_comparison_signature_handles_lone_surrogate_strings(field: str):
    lone_surrogate = json.loads('"\\ud800"')
    override = {field: lone_surrogate}

    first = describe_comparison_readiness([_run("first", **override)])
    second = describe_comparison_readiness([_run("second", **override)])

    assert first.ready is True
    assert first.comparison_signature is not None
    assert first.comparison_signature == second.comparison_signature
    json.dumps(first.to_dict(), allow_nan=False)


@pytest.mark.parametrize(
    ("override", "expected_axis"),
    [
        ({"character": " \t\n "}, "character"),
        ({"version": " \t\n "}, "game_version"),
        ({"mode": " \t\n "}, "evaluation_mode"),
        ({"scenario": " \t\n "}, "scenario"),
        ({"seed": " \t\n "}, "seed"),
    ],
)
def test_comparison_readiness_treats_whitespace_only_strings_as_missing(
    override, expected_axis
):
    readiness = describe_comparison_readiness([_run("blank", **override)])

    assert readiness.ready is False
    assert readiness.missing_axes == (expected_axis,)
    assert readiness.mixed_axes == ()
    assert readiness.invalid_axes == ()
    assert readiness.comparison_signature is None


def test_comparison_signature_uses_launch_trimmed_string_semantics():
    padded = describe_comparison_readiness(
        [
            _run(
                "padded",
                character=" ironclad ",
                version=" 2026.08 ",
                mode=" evaluation ",
                scenario=" standard ",
                seed=" seed-1 ",
            )
        ]
    )
    canonical = describe_comparison_readiness([_run("canonical")])

    assert padded.ready is True
    assert padded.comparison_signature == canonical.comparison_signature


@pytest.mark.parametrize(
    ("varying_field", "expected_axis"),
    [("version", "game_version"), ("seed", "seed")],
)
def test_comparison_readiness_rejects_bounded_distinct_overflow(
    monkeypatch: pytest.MonkeyPatch, varying_field: str, expected_axis: str
):
    monkeypatch.setattr("agent.run_workbench.metrics.COMPARISON_DISTINCT_LIMIT", 2)
    records = [
        _run(
            f"run-{index}",
            version=f"version-{index}" if varying_field == "version" else "2026.08",
            seed=f"seed-{index}" if varying_field == "seed" else "same-seed",
        )
        for index in range(3)
    ]

    readiness = describe_comparison_readiness(records)

    assert readiness.ready is False
    assert readiness.invalid_axes == (expected_axis,)
    assert readiness.comparison_signature is None
    json.dumps(readiness.to_dict(), allow_nan=False)


def test_seed_count_saturates_at_the_bounded_overflow_marker(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("agent.run_workbench.metrics.COMPARISON_DISTINCT_LIMIT", 2)
    readiness = describe_comparison_readiness(
        [_run(f"run-{index}", seed=f"seed-{index}") for index in range(5)]
    )

    assert readiness.ready is False
    assert readiness.seed_count == 3
    assert readiness.seed_complete is False
    assert readiness.invalid_axes == ("seed",)
    assert readiness.comparison_signature is None


@pytest.mark.parametrize(
    ("override", "expected_axis"),
    [({"version": 7}, "game_version"), ({"seed": 7}, "seed")],
)
def test_comparison_readiness_rejects_nonstring_version_and_seed(
    override, expected_axis
):
    readiness = describe_comparison_readiness([_run("invalid", **override)])

    assert readiness.ready is False
    assert readiness.invalid_axes == (expected_axis,)
    assert readiness.comparison_signature is None


@pytest.mark.parametrize(
    "invalid_ascension",
    [
        pytest.param(False, id="bool"),
        pytest.param(-1, id="negative"),
        pytest.param(11, id="above-max"),
        pytest.param(10**1000, id="huge"),
    ],
)
def test_comparison_readiness_requires_ascension_in_exact_game_range(
    invalid_ascension
):
    readiness = describe_comparison_readiness(
        [_run("invalid-ascension", ascension=invalid_ascension)]
    )

    assert readiness.ready is False
    assert readiness.invalid_axes == ("ascension",)
    assert readiness.comparison_signature is None


def test_comparison_readiness_accepts_maximum_game_ascension():
    readiness = describe_comparison_readiness([_run("ascension-10", ascension=10)])

    assert readiness.ready is True
    assert readiness.invalid_axes == ()


def test_invalid_ascension_never_calls_user_controlled_repr():
    class ExplosiveRepr:
        def __repr__(self):
            raise AssertionError("invalid ascension repr must not be called")

    readiness = describe_comparison_readiness(
        [_run("explosive-repr", ascension=ExplosiveRepr())]
    )

    assert readiness.ready is False
    assert readiness.invalid_axes == ("ascension",)


def test_ascension_distinct_tracking_is_bounded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent.run_workbench.metrics.COMPARISON_DISTINCT_LIMIT", 2)
    readiness = describe_comparison_readiness(
        [
            _run("ascension-0", ascension=0),
            _run("ascension-1", ascension=1),
            _run("ascension-2", ascension=2),
        ]
    )

    assert readiness.ready is False
    assert readiness.mixed_axes == ()
    assert readiness.invalid_axes == ("ascension",)
    assert readiness.comparison_signature is None


def test_comparison_readiness_consumes_input_iterable_once():
    class SinglePassRecords:
        def __init__(self):
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("records iterated more than once")
            yield _run("first", seed="seed-a")
            yield _run("second", seed="seed-b")

    records = SinglePassRecords()

    readiness = describe_comparison_readiness(records)

    assert readiness.ready is True
    assert records.iterations == 1


def test_comparison_readiness_uses_valid_results_and_ignores_technical_noise():
    readiness = describe_comparison_readiness(
        [
            _run("valid", seed="valid-seed"),
            _run(
                "progress",
                status=RunStatus.IN_PROGRESS,
                character=None,
                version=None,
                mode=7,
                scenario=True,
                ascension=True,
                seed=None,
            ),
        ]
    )

    assert readiness.ready is True
    assert readiness.seed_count == 1
    assert readiness.missing_axes == ()
    assert readiness.mixed_axes == ()
    assert readiness.invalid_axes == ()


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


@pytest.mark.parametrize(
    "invalid_floor",
    [
        True,
        False,
        0,
        -1,
        7.0,
        7.9,
        "7",
        float("nan"),
        float("inf"),
        float("-inf"),
        10**1000,
    ],
)
def test_invalid_floor_values_are_consistently_treated_as_missing(invalid_floor):
    record = _run("invalid-floor", floor=invalid_floor)

    summary = summarize_cohort([record])

    assert summary.valid_n == 1
    assert summary.valid_floor_n == 0
    assert summary.floor_n == 0
    assert summary.avg_global_floor is None
    assert summary.median_global_floor is None
    assert summary.max_global_floor is None
    assert summary.act2_entry_denominator == 0
    assert summary.histogram == ()
    assert summary.trend == ()
    assert summary.trend_unknown_time_n == 1
    json.dumps(summary.to_dict(), allow_nan=False)


def test_invalid_technical_floor_is_excluded_from_distribution_and_trend_floor():
    summary = summarize_cohort(
        [_run("crash", status=RunStatus.CRASH, floor=True)],
        include_technical=True,
    )

    assert summary.technical_n == 1
    assert summary.technical_floor_n == 0
    assert summary.floor_n == 0
    assert summary.histogram == ()
    assert summary.trend == ()
    assert summary.trend_unknown_time_n == 1


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


def test_trend_uses_started_at_as_chronological_fallback_and_excludes_missing_time():
    summary = summarize_cohort(
        [
            _run("ended", floor=4, ended_at=4, source_id="z"),
            _run("started", floor=2, started_at=2, source_id="z", seed="b"),
            _run("missing-b", floor=8, source_id="b", seed="c"),
            _run("missing-a", floor=6, source_id="a", seed="d"),
        ]
    )

    assert [point.timestamp for point in summary.trend] == [2.0, 4.0]
    assert [point.run_id for point in summary.trend] == [
        "started",
        "ended",
    ]
    assert summary.trend_eligible_n == 4
    assert summary.trend_timestamped_n == 2
    assert summary.trend_unknown_time_n == 2
    assert summary.trend_sampled_n == 2
    assert summary.trend_sampling_method == "all_timestamped"


def test_large_trend_is_bounded_deterministic_and_preserves_exact_aggregates(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("agent.run_workbench.metrics.TREND_SAMPLE_LIMIT", 16)
    records = [
        _run(
            f"run-{index:04d}",
            floor=index % 51 + 1,
            ended_at=float(index) if index % 5 else None,
            source_id=f"source-{index:04d}",
            seed=str(index),
        )
        for index in range(1_000)
    ]

    forward = summarize_cohort(records)
    reverse = summarize_cohort(reversed(records))

    assert forward.valid_n == 1_000
    assert forward.floor_n == 1_000
    assert forward.avg_global_floor == pytest.approx(
        sum(index % 51 + 1 for index in range(1_000)) / 1_000
    )
    assert forward.median_global_floor == 25.5
    assert forward.max_global_floor == 51
    assert forward.trend_eligible_n == 1_000
    assert forward.trend_timestamped_n == 800
    assert forward.trend_unknown_time_n == 200
    assert forward.trend_sampled_n == 16
    assert forward.trend_sample_limit == 16
    assert forward.trend_sampling_method == "deterministic_hash"
    assert len(forward.trend) == 16
    assert forward.trend == reverse.trend
    assert all(point.timestamp is not None for point in forward.trend)


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


def test_comparison_streams_without_retaining_every_run_record():
    def generated(prefix: str):
        old_refs: list[weakref.ReferenceType[RunRecord]] = []
        for index in range(40):
            if len(old_refs) >= 2:
                gc.collect()
                assert old_refs[-2]() is None
            record = _run(
                f"{prefix}-{index}",
                floor=index % 20 + 1,
                seed="paired-seed",
                ended_at=float(index + 1),
            )
            old_refs.append(weakref.ref(record))
            yield record

    result = compare_cohorts(generated("current"), generated("baseline"))

    assert result.comparable is True
    assert result.current.valid_n == 40
    assert result.baseline.valid_n == 40


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
    assert result.notes == (
        "cross-version comparison: current=2026.09, baseline=2026.08",
    )


def test_allow_cross_version_does_not_hide_missing_or_mixed_version_metadata():
    current = [_run("missing", version=None)]

    result = compare_cohorts(
        current,
        [_run("baseline")],
        allow_cross_version=True,
    )

    assert result.comparable is False
    assert any("current version" in reason and "missing" in reason for reason in result.mismatch_reasons)


@pytest.mark.parametrize(
    ("override", "axis"),
    [
        ({"character": ["ironclad"]}, "character"),
        ({"version": {"build": "2026.08"}}, "version"),
        ({"mode": 7}, "evaluation mode"),
        ({"scenario": True}, "scenario"),
    ],
)
def test_invalid_unhashable_or_nonstring_comparison_metadata_returns_reason(
    override, axis
):
    result = compare_cohorts([_run("current", **override)], [_run("baseline")])

    assert result.comparable is False
    assert any(
        f"current {axis}" in reason and "invalid" in reason
        for reason in result.mismatch_reasons
    )
    json.dumps(result.to_dict(), allow_nan=False)


def test_mixed_valid_and_invalid_metadata_is_reported_without_sorting_type_error():
    result = compare_cohorts(
        [
            _run("current-valid", character="ironclad", seed="a"),
            _run("current-invalid", character=7, seed="b"),
        ],
        [
            _run("baseline-a", seed="a"),
            _run("baseline-b", seed="b"),
        ],
    )

    assert result.comparable is False
    assert any(
        "current character" in reason and "mixed" in reason and "int" in reason
        for reason in result.mismatch_reasons
    )


@pytest.mark.parametrize("invalid_seed", [["seed"], {"seed": 1}, 7, True])
def test_invalid_seed_types_fail_strict_pairing_without_throwing(invalid_seed):
    result = compare_cohorts(
        [_run("current", seed=invalid_seed)],
        [_run("baseline", seed="seed")],
    )

    assert result.comparable is False
    assert result.paired is False
    assert any(
        "current seed set" in reason and "invalid" in reason
        for reason in result.mismatch_reasons
    )
    json.dumps(result.to_dict(), allow_nan=False)


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
            ascension=20,
            seed="other",
        ),
    ]

    result = compare_cohorts(current, [_run("baseline", seed="same")])

    assert result.comparable is True
    assert result.paired is True


def test_ascension_mismatch_makes_cohorts_incompatible():
    result = compare_cohorts(
        [_run("current", ascension=10, seed="same")],
        [_run("baseline", ascension=0, seed="same")],
    )

    assert result.comparable is False
    assert any(
        reason == "ascension mismatch: current=10, baseline=0"
        for reason in result.mismatch_reasons
    )


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (None, "current ascension is missing"),
        (True, "current ascension is invalid"),
        (-1, "current ascension is invalid"),
        (1.0, "current ascension is invalid"),
    ],
)
def test_missing_or_invalid_ascension_has_deterministic_reason(current, expected):
    result = compare_cohorts(
        [_run("current", ascension=current, seed="same")],
        [_run("baseline", ascension=0, seed="same")],
    )

    assert result.comparable is False
    assert any(reason.startswith(expected) for reason in result.mismatch_reasons)


def test_mixed_ascension_has_deterministic_reason():
    result = compare_cohorts(
        [
            _run("current-a", ascension=0, seed="a"),
            _run("current-b", ascension=10, seed="b"),
        ],
        [
            _run("baseline-a", ascension=0, seed="a"),
            _run("baseline-b", ascension=0, seed="b"),
        ],
    )

    assert result.comparable is False
    assert any(
        reason == "current ascension is mixed: values=0, 10"
        for reason in result.mismatch_reasons
    )


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


@pytest.mark.parametrize(
    "invalid_timestamp", [float("nan"), float("inf"), float("-inf"), 10**1000]
)
def test_invalid_ended_timestamp_falls_back_to_finite_started_timestamp(
    invalid_timestamp,
):
    summary = summarize_cohort(
        [
            _run("fallback", ended_at=invalid_timestamp, started_at=2),
            _run("ended", ended_at=4, seed="b"),
        ]
    )

    assert [point.timestamp for point in summary.trend] == [2.0, 4.0]


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_public_to_dict_rejects_nonfinite_and_unsupported_values(nonfinite):
    summary = summarize_cohort([_run("valid")])

    with pytest.raises(ValueError, match="non-finite"):
        replace(summary, avg_global_floor=nonfinite).to_dict()
    with pytest.raises(TypeError, match="object"):
        replace(summary, histogram=(object(),)).to_dict()
