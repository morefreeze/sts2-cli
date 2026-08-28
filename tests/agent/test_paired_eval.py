import json

import pytest

from agent.paired_eval import (
    PairingResult,
    compare,
    format_report,
    load_arm,
    main,
    pair_arms,
    paired_stats,
)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def result_row(seed, status="win", floor=10, combat_wins=5, **extra):
    row = {
        "seed": seed,
        "status": status,
        "floor": floor,
        "combat_wins": combat_wins,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# load_arm
# ---------------------------------------------------------------------------

def test_load_arm_keeps_last_occurrence_and_counts_duplicates(tmp_path):
    path = write_jsonl(tmp_path / "arm.jsonl", [
        result_row("s1", floor=10),
        result_row("s1", floor=99),  # duplicate seed, last one should win
        result_row("s2", floor=20),
    ])

    arm = load_arm(path)

    assert arm["by_seed"]["s1"]["floor"] == 99
    assert arm["by_seed"]["s2"]["floor"] == 20
    assert arm["diagnostics"]["duplicates_collapsed"] == 1
    assert arm["diagnostics"]["unique_seeds"] == 2


def test_load_arm_skips_malformed_line_and_counts_it(tmp_path):
    # Simulates the realistic failure mode on this machine: the eval process
    # gets killed mid-write and the final line of the JSONL is a truncated,
    # unparseable fragment. The good rows on either side of it must survive,
    # and the bad line must be counted (not silently dropped) so a truncated
    # input file is visibly truncated in the report.
    path = tmp_path / "arm.jsonl"
    lines = [
        json.dumps(result_row("s1", floor=10)),
        '{"seed": "s2", "status": "win", "floor": 12',  # truncated, no closing brace
        json.dumps(result_row("s3", floor=14)),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    arm = load_arm(path)

    assert set(arm["by_seed"]) == {"s1", "s3"}
    assert arm["diagnostics"]["malformed_lines"] == 1
    assert arm["diagnostics"]["unique_seeds"] == 2


def test_load_arm_classifies_valid_vs_invalid_seeds(tmp_path):
    path = write_jsonl(tmp_path / "arm.jsonl", [
        result_row("s1", status="win"),
        result_row("s2", status="dead"),
        result_row("s3", status="stuck"),
        result_row("s4", status="timeout"),
    ])

    arm = load_arm(path)

    assert arm["diagnostics"]["valid_seeds"] == 2
    assert arm["diagnostics"]["invalid_seeds"] == 2


# ---------------------------------------------------------------------------
# pair_arms
# ---------------------------------------------------------------------------

def test_pair_arms_clean_three_seed_pairing(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", floor=10),
        result_row("s2", floor=20),
        result_row("s3", floor=30),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", floor=15),
        result_row("s2", floor=25),
        result_row("s3", floor=35),
    ])

    result = pair_arms(load_arm(path_a), load_arm(path_b))

    assert isinstance(result, PairingResult)
    assert sorted(result.seeds) == ["s1", "s2", "s3"]
    assert result.only_in_a == 0
    assert result.only_in_b == 0
    assert result.invalid_in_a == 0
    assert result.invalid_in_b == 0


def test_pair_arms_drops_seed_invalid_in_one_arm(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", status="win"),
        result_row("s2", status="dead"),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", status="stuck"),  # valid in A, invalid in B
        result_row("s2", status="win"),
    ])

    result = pair_arms(load_arm(path_a), load_arm(path_b))

    assert result.seeds == ["s2"]
    assert result.invalid_in_b == 1
    assert result.invalid_in_a == 0
    assert result.only_in_a == 0
    assert result.only_in_b == 0


def test_pair_arms_counts_seed_only_present_in_one_arm(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1"),
        result_row("s2"),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1"),
    ])

    result = pair_arms(load_arm(path_a), load_arm(path_b))

    assert result.seeds == ["s1"]
    assert result.only_in_a == 1
    assert result.only_in_b == 0


def test_pair_arms_per_room_pairs_exclude_zero_fight_seeds(tmp_path):
    # s1: elite fights in both arms -> counts toward elite pairing.
    # s2: valid win/dead pair overall, but zero elite fights in arm B ->
    #     must still count toward the overall pair, but NOT the elite pairing.
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", total_elite_hp_loss=20, elite_combats=2),
        result_row("s2", total_elite_hp_loss=10, elite_combats=1),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", total_elite_hp_loss=10, elite_combats=1),
        result_row("s2", total_elite_hp_loss=0, elite_combats=0),
    ])

    result = pair_arms(load_arm(path_a), load_arm(path_b))

    assert sorted(result.seeds) == ["s1", "s2"]
    assert result.room_seeds["elite"] == ["s1"]
    assert len(result.room_seeds["elite"]) < len(result.seeds)


def test_pair_arms_treats_missing_room_fields_as_zero_fights(tmp_path):
    # Older rows may simply lack the hp-loss fields entirely.
    path_a = write_jsonl(tmp_path / "a.jsonl", [result_row("s1")])
    path_b = write_jsonl(tmp_path / "b.jsonl", [result_row("s1")])

    result = pair_arms(load_arm(path_a), load_arm(path_b))

    assert result.seeds == ["s1"]
    for room in ("monster", "elite", "boss"):
        assert result.room_seeds[room] == []


# ---------------------------------------------------------------------------
# paired_stats
# ---------------------------------------------------------------------------

def test_paired_stats_known_value_regression():
    # a = [10, 12, 14], b = [12, 13, 20] -> diffs = [2, 1, 6].
    #
    # Expected numbers below are LITERALS, independently hand/script-derived
    # from the fixture, not re-derived here via the same formula the
    # implementation uses (that would make this test circular: a shared
    # formula bug, e.g. dividing by sqrt(n-1) instead of sqrt(n) in `se`,
    # would pass a test that recomputes its expectation the same broken way).
    #
    # By hand: mean_diff = (2+1+6)/3 = 3.0
    #   sample variance (ddof=1) = ((2-3)^2 + (1-3)^2 + (6-3)^2) / (3-1)
    #                            = (1 + 4 + 9) / 2 = 7.0
    #   se = sqrt(7.0) / sqrt(3) = 2.6457513110645907 / 1.7320508075688772
    #      = 1.5275252316519468
    #   t  = mean_diff / se = 3.0 / 1.5275252316519468 = 1.9639610121239313
    #   p  = erfc(|t| / sqrt(2)) = erfc(1.388730105...) = 0.04953461343562678
    # Cross-checked with a standalone `python3 -c "..."` script that does not
    # import agent.paired_eval at all, and with the closed form
    # t/sqrt(2) = 3*sqrt(3/14) as an independent sanity check on `t`.
    a = [10, 12, 14]
    b = [12, 13, 20]

    stats = paired_stats(a, b)

    assert stats["n"] == 3
    assert stats["mean_a"] == pytest.approx(12.0)
    assert stats["mean_b"] == pytest.approx(15.0)
    assert stats["mean_diff"] == pytest.approx(3.0)
    assert stats["se"] == pytest.approx(1.5275252316519468)
    assert stats["t"] == pytest.approx(1.9639610121239313)
    assert stats["p"] == pytest.approx(0.04953461343562678)


def test_paired_stats_n_equals_one_returns_none_stats():
    stats = paired_stats([5], [8])

    assert stats["n"] == 1
    assert stats["mean_diff"] == pytest.approx(3.0)
    assert stats["se"] is None
    assert stats["t"] is None
    assert stats["p"] is None


def test_paired_stats_zero_variance_diffs_returns_none_stats():
    # Every pair has the same diff (+2) -> se is exactly zero.
    stats = paired_stats([1, 2, 3], [3, 4, 5])

    assert stats["n"] == 3
    assert stats["mean_diff"] == pytest.approx(2.0)
    assert stats["se"] == 0.0
    assert stats["t"] is None
    assert stats["p"] is None


def test_paired_stats_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        paired_stats([1, 2], [1, 2, 3])


def test_paired_stats_empty_input_does_not_raise():
    stats = paired_stats([], [])

    assert stats["n"] == 0
    assert stats["t"] is None
    assert stats["p"] is None


def test_paired_stats_rejects_nan_input():
    # A NaN silently riding through would produce a corrupt (and
    # RFC-8259-invalid) number in the JSON report. Must fail loudly instead.
    with pytest.raises(ValueError):
        paired_stats([1.0, float("nan"), 3.0], [2.0, 2.0, 4.0])


def test_paired_stats_rejects_inf_input():
    with pytest.raises(ValueError):
        paired_stats([1.0, 2.0, 3.0], [2.0, float("inf"), 4.0])


# ---------------------------------------------------------------------------
# compare (full pipeline)
# ---------------------------------------------------------------------------

def test_compare_reports_pairing_diagnostics_and_metrics(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", status="win", floor=10, combat_wins=3,
                   total_elite_hp_loss=20, elite_combats=2),
        result_row("s2", status="dead", floor=12, combat_wins=4,
                   total_elite_hp_loss=10, elite_combats=1),
        result_row("s3", status="win", floor=14, combat_wins=5),
        result_row("only_a", status="win", floor=1, combat_wins=0),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", status="win", floor=11, combat_wins=3,
                   total_elite_hp_loss=8, elite_combats=2),
        result_row("s2", status="stuck", floor=0, combat_wins=0),
        result_row("s3", status="dead", floor=16, combat_wins=6),
        result_row("only_b", status="win", floor=1, combat_wins=0),
    ])

    result = compare(path_a, path_b, label_a="baseline", label_b="candidate")

    assert result["label_a"] == "baseline"
    assert result["label_b"] == "candidate"
    assert result["pairs"] == 2  # s1, s3 (s2 invalid in b; only_a/only_b unmatched)
    assert result["only_in_a"] == 1
    assert result["only_in_b"] == 1
    assert result["invalid_in_a"] == 0
    assert result["invalid_in_b"] == 1

    floor_stats = result["metrics"]["floor"]
    assert floor_stats["n"] == 2
    assert floor_stats["mean_a"] == pytest.approx((10 + 14) / 2)
    assert floor_stats["mean_b"] == pytest.approx((11 + 16) / 2)

    # Only s1 has elite_combats >= 1 in BOTH arms -> elite n == 1.
    elite_stats = result["metrics"]["elite_hp_loss_per_fight"]
    assert elite_stats["n"] == 1
    assert elite_stats["mean_a"] == pytest.approx(10.0)  # 20 / 2
    assert elite_stats["mean_b"] == pytest.approx(4.0)   # 8 / 2


def test_compare_win_metric_is_zero_one_indicator(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", status="win"),
        result_row("s2", status="dead"),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", status="dead"),
        result_row("s2", status="win"),
    ])

    result = compare(path_a, path_b)

    win_stats = result["metrics"]["win"]
    assert win_stats["n"] == 2
    assert win_stats["mean_a"] == pytest.approx(0.5)
    assert win_stats["mean_b"] == pytest.approx(0.5)
    assert win_stats["mean_diff"] == pytest.approx(0.0)


def test_compare_surfaces_unique_seed_and_duplicate_diagnostics(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", floor=10),
        result_row("s1", floor=11),  # duplicate, collapsed
        result_row("s2", floor=12),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", floor=13),
        result_row("s2", floor=14),
    ])

    result = compare(path_a, path_b)

    assert result["arm_a"]["unique_seeds"] == 2
    assert result["arm_a"]["duplicates_collapsed"] == 1
    assert result["arm_a"]["valid_seeds"] == 2
    assert result["arm_a"]["malformed_lines"] == 0
    assert result["arm_b"]["unique_seeds"] == 2
    assert result["arm_b"]["duplicates_collapsed"] == 0


def test_compare_with_no_shared_seeds_reports_zero_pairs(tmp_path):
    # The two arms never overlap at all (e.g. disjoint seed ranges) --
    # exercises the branch of compare()'s metric loop where every seed list
    # fed to paired_stats is empty.
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1"),
        result_row("s2"),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s3"),
        result_row("s4"),
    ])

    result = compare(path_a, path_b)

    assert result["pairs"] == 0
    assert result["only_in_a"] == 2
    assert result["only_in_b"] == 2
    assert result["invalid_in_a"] == 0
    assert result["invalid_in_b"] == 0
    for name in ("floor", "win", "combat_wins"):
        assert result["metrics"][name]["n"] == 0
        assert result["metrics"][name]["mean_a"] is None
    for room in ("monster", "elite", "boss"):
        assert result["room_pairs"][room] == 0

    # format_report must render this without crashing on the all-None stats.
    report = format_report(result)
    assert "n/a" in report


def test_compare_output_is_strict_json_roundtrippable(tmp_path):
    path_a = write_jsonl(tmp_path / "a.jsonl", [
        result_row("s1", status="win", floor=10, combat_wins=3,
                   total_elite_hp_loss=20, elite_combats=2),
        result_row("s2", status="dead", floor=12, combat_wins=4),
    ])
    path_b = write_jsonl(tmp_path / "b.jsonl", [
        result_row("s1", status="win", floor=11, combat_wins=3,
                   total_elite_hp_loss=8, elite_combats=2),
        result_row("s2", status="win", floor=14, combat_wins=5),
    ])

    result = compare(path_a, path_b)

    # allow_nan=False is the same guard main() uses for --json: this must
    # not raise, and the round trip must reproduce the same structure.
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert json.loads(encoded) == result


# ---------------------------------------------------------------------------
# format_report / CLI
# ---------------------------------------------------------------------------

def test_format_report_marks_significant_metric_with_asterisk():
    result = {
        "label_a": "A", "label_b": "B",
        "path_a": "a.jsonl", "path_b": "b.jsonl",
        "arm_a": {"unique_seeds": 3, "valid_seeds": 3, "duplicates_collapsed": 0,
                  "malformed_lines": 0},
        "arm_b": {"unique_seeds": 3, "valid_seeds": 3, "duplicates_collapsed": 0,
                  "malformed_lines": 0},
        "pairs": 3, "only_in_a": 0, "only_in_b": 0,
        "invalid_in_a": 0, "invalid_in_b": 0,
        "room_pairs": {"monster": 0, "elite": 0, "boss": 0},
        "metrics": {
            "floor": {"mean_a": 17.5, "mean_b": 18.14, "mean_diff": 0.64,
                      "se": 0.1, "t": 6.4, "p": 0.0001, "n": 3},
            "win": {"mean_a": 0.5, "mean_b": 0.5, "mean_diff": 0.0,
                    "se": 0.2, "t": 0.0, "p": 0.9, "n": 3},
            "combat_wins": {"mean_a": None, "mean_b": None, "mean_diff": None,
                            "se": None, "t": None, "p": None, "n": 0},
        },
    }

    report = format_report(result)
    lines = {line.split()[0]: line for line in report.splitlines() if line}

    assert "*" in lines["floor"]
    assert "*" not in lines["win"]
    assert "n/a" in lines["combat_wins"]
    # Multiple-comparisons caveat must be visible in the rendered text, not
    # just in the docstring -- the reader of the printed table is who needs
    # to know the marker is per-metric and uncorrected.
    assert "uncorrected" in report


def test_main_json_flag_prints_valid_json_to_stdout(tmp_path, capsys):
    path_a = write_jsonl(tmp_path / "a.jsonl", [result_row("s1", floor=10)])
    path_b = write_jsonl(tmp_path / "b.jsonl", [result_row("s1", floor=12)])

    exit_code = main([str(path_a), str(path_b), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pairs"] == 1
    assert payload["metrics"]["floor"]["mean_diff"] == pytest.approx(2.0)


def test_main_text_mode_prints_readable_table(tmp_path, capsys):
    path_a = write_jsonl(tmp_path / "a.jsonl", [result_row("s1", floor=10)])
    path_b = write_jsonl(tmp_path / "b.jsonl", [result_row("s1", floor=12)])

    exit_code = main([str(path_a), str(path_b), "--label-a", "base", "--label-b", "cand"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "base" in out and "cand" in out
    assert "floor" in out


# ---------------------------------------------------------------------------
# Cross-check against agent.eval_rl (slow: imports torch/sb3_contrib)
# ---------------------------------------------------------------------------
#
# agent/paired_eval.py deliberately keeps its own copy of the valid-status
# set (_VALID_STATUSES) instead of importing agent.eval_rl, because eval_rl
# imports torch and sb3_contrib at module level and this module's near-zero
# import time is a load-bearing property (it is meant to be run often and
# interactively; see the "INTENTIONAL COPY" comment on _VALID_STATUSES).
# That means nothing else in this file will ever catch the two definitions
# drifting apart. This one test pays the heavy import cost on purpose so
# that drift is caught here instead of silently in production.

def test_valid_statuses_match_eval_rl_classification():
    from agent.eval_rl import _TECHNICAL_STATUSES, classify_eval_result
    from agent.paired_eval import _VALID_STATUSES

    observed_statuses = set()
    info_flags = (None, "reset_failure", "load_failed", "invalid",
                  "replay_failed", "stuck", "crashed")
    for timed_out in (False, True):
        for run_won in (False, True):
            for flag in info_flags:
                info = {flag: True} if flag else {}
                observed_statuses.add(classify_eval_result(
                    timed_out=timed_out, run_won=run_won, info=info,
                ))

    # Every status classify_eval_result can produce is either "technical"
    # (eval_rl's own _TECHNICAL_STATUSES) or a real policy outcome
    # (paired_eval's _VALID_STATUSES) -- and never both.
    assert _VALID_STATUSES == observed_statuses - _TECHNICAL_STATUSES
    assert _VALID_STATUSES.isdisjoint(_TECHNICAL_STATUSES)
