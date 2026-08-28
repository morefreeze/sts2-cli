#!/usr/bin/env python3
"""paired_eval.py — Paired A/B comparison of two `agent/eval_rl.py` result logs.

`agent/eval_rl.py --results-log <path>` appends one JSON row per attempt via
`append_eval_result_row`; `summarize_eval_results` aggregates a SINGLE arm.
Neither answers the question this project actually needs to answer before
accepting a planner/policy change: "is arm B better than arm A on the exact
same seeds?" An unpaired comparison (e.g. two independent win-rate numbers)
is dominated by seed-to-seed variance in map layout and RNG — the paired
seed-by-seed difference is what isolates the effect of the change. This
module is the measurement gate: it re-derives the shared seed set, drops
everything that cannot be fairly compared, and reports a paired t-test on
what is left.

Usage:
    python -m agent.paired_eval baseline.jsonl candidate.jsonl \
        --label-a baseline --label-b candidate
    python -m agent.paired_eval a.jsonl b.jsonl --json

The project's standing bar for accepting a behavior change is a paired
comparison over 240 fixed seeds (`--fixed-seeds --seed-offset N` in
eval_rl.py); a prior accepted result read "elite HP/fight 29.1 -> 24.2
(paired -3.44 HP, se 1.03, p=0.0010)". This module's default text report is
shaped to make that exact claim easy to read off.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np

# Statuses that represent an actual policy outcome (the run was played to a
# real end state, win or loss). Everything else — crash, timeout, stuck,
# reset_failure, invalid — is a technical failure of the harness or engine,
# not a signal about the policy, and must never be averaged into a metric.
# Mirrors `_TECHNICAL_STATUSES` / the `valid` filter in
# `agent.eval_rl.summarize_eval_results`.
_VALID_STATUSES = {"win", "dead"}

# Per-room HP-loss room names, lower-cased to match the JSONL field prefixes
# (`total_<room>_hp_loss`, `<room>_combats`) written by
# `CombatEnv._combat_hp_loss_summary`.
_HP_LOSS_ROOMS = ("monster", "elite", "boss")


def load_arm(path) -> dict:
    """Load one evaluation arm's JSONL result log.

    Returns ``{"by_seed": {seed: row}, "diagnostics": {...}}`` — top-level
    keys are strings and values are dicts, matching ``dict[str, dict]``.

    A results log is an append-only stream: `eval_rl.py` may be re-run over
    the same seed set (a retry batch, a resumed eval), so the same seed can
    appear more than once. The LAST occurrence wins, on the theory that a
    later row reflects the most recent run of that seed against this
    checkpoint/config — callers that want something else should pre-filter
    the file. Every row is kept regardless of its `status` ("valid-or-invalid
    row"); validity is only decided at pairing time, because a seed that is
    invalid in this arm can still be meaningful to report as dropped rather
    than silently missing.
    """
    rows: dict[str, dict] = {}
    total_lines = 0
    duplicates = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            seed = row.get("seed")
            if seed is None:
                # Can't pair a row that carries no pairing key; ignore it
                # rather than crash the whole comparison on one bad line.
                continue
            seed = str(seed)
            total_lines += 1
            if seed in rows:
                duplicates += 1
            rows[seed] = row
    valid_seeds = sum(1 for row in rows.values() if row.get("status") in _VALID_STATUSES)
    return {
        "by_seed": rows,
        "diagnostics": {
            "total_lines": total_lines,
            "unique_seeds": len(rows),
            "duplicates_collapsed": duplicates,
            "valid_seeds": valid_seeds,
            "invalid_seeds": len(rows) - valid_seeds,
        },
    }


@dataclass
class PairingResult:
    """The seed accounting behind one A/B comparison.

    `seeds` is the base pairing used for run-level metrics (floor, win,
    combat_wins): every seed present in both arms AND valid (win/dead) in
    both arms. `room_seeds[room]` is a further-filtered subset — a seed only
    belongs there if BOTH arms also fought at least one combat of that room
    type, since `total_<room>_hp_loss / <room>_combats` is undefined (and
    meaningless to compare) for a run that never saw that room.
    """

    seeds: list[str]
    room_seeds: dict[str, list[str]]
    only_in_a: int
    only_in_b: int
    invalid_in_a: int
    invalid_in_b: int
    unique_seeds_a: int
    unique_seeds_b: int
    duplicates_a: int
    duplicates_b: int = field(default=0)


def pair_arms(arm_a: dict, arm_b: dict) -> PairingResult:
    """Compute the shared, valid seed set between two loaded arms.

    Drop accounting is deliberately split into two disjoint reasons:

    - `only_in_a` / `only_in_b`: the seed is entirely absent from the other
      arm (never ran, or ran under a different seed set). There is nothing
      to compare it against regardless of its own status.
    - `invalid_in_a` / `invalid_in_b`: the seed IS present in both arms, but
      one side's row is a technical failure (stuck/timeout/crash/etc.), not
      a policy outcome. These are the pairs that "almost" counted.

    A seed invalid in both arms is counted in both `invalid_in_*` buckets —
    that is intentional double bookkeeping for a diagnostic report, not a
    pairing decision; it is never added to `seeds` either way.
    """
    rows_a = arm_a["by_seed"]
    rows_b = arm_b["by_seed"]
    seeds_a = set(rows_a)
    seeds_b = set(rows_b)
    common = seeds_a & seeds_b

    paired: list[str] = []
    invalid_in_a = 0
    invalid_in_b = 0
    for seed in common:
        a_valid = rows_a[seed].get("status") in _VALID_STATUSES
        b_valid = rows_b[seed].get("status") in _VALID_STATUSES
        if not a_valid:
            invalid_in_a += 1
        if not b_valid:
            invalid_in_b += 1
        if a_valid and b_valid:
            paired.append(seed)
    paired.sort()

    room_seeds: dict[str, list[str]] = {}
    for room in _HP_LOSS_ROOMS:
        key = f"{room}_combats"
        room_seeds[room] = sorted(
            seed for seed in paired
            if int(rows_a[seed].get(key, 0) or 0) >= 1
            and int(rows_b[seed].get(key, 0) or 0) >= 1
        )

    return PairingResult(
        seeds=paired,
        room_seeds=room_seeds,
        only_in_a=len(seeds_a - seeds_b),
        only_in_b=len(seeds_b - seeds_a),
        invalid_in_a=invalid_in_a,
        invalid_in_b=invalid_in_b,
        unique_seeds_a=len(seeds_a),
        unique_seeds_b=len(seeds_b),
        duplicates_a=arm_a["diagnostics"]["duplicates_collapsed"],
        duplicates_b=arm_b["diagnostics"]["duplicates_collapsed"],
    )


def paired_stats(a_values, b_values) -> dict:
    """Manual paired t-test: is arm B different from arm A on shared seeds?

    diffs = b - a; mean_diff = mean(diffs); se = std(diffs, ddof=1) / sqrt(n);
    t = mean_diff / se. `p` is the two-sided normal approximation
    `erfc(|t| / sqrt(2))` rather than the Student t-distribution CDF, because
    this project has no scipy dependency and does not want to add one solely
    for a CDF lookup. That approximation is accurate for large n — the
    project's standard eval size is 240 seeds, where t and the normal
    quantile are indistinguishable in practice — but it is slightly
    ANTI-CONSERVATIVE (understates p, i.e. overstates significance) for
    small n. Do not trust a `p` from this function as a hard accept/reject
    threshold for n well under ~30; at that size use it as a rough signal
    and lean on the effect size (`mean_diff`, `se`) instead.

    `a_values` and `b_values` must be the same length and already ordered by
    the same seed sequence (i.e. index i in both is the same seed) — callers
    are `compare()`, which builds both lists from `PairingResult.seeds` (or
    `.room_seeds[room]`) in that same order.
    """
    a = list(a_values)
    b = list(b_values)
    if len(a) != len(b):
        raise ValueError(
            "paired_stats requires equal-length sequences (a and b must be "
            "the same seeds, in the same order)"
        )
    n = len(a)
    if n == 0:
        return {"mean_a": None, "mean_b": None, "mean_diff": None,
                "se": None, "t": None, "p": None, "n": 0}

    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    mean_a = float(np.mean(a_arr))
    mean_b = float(np.mean(b_arr))
    diffs = b_arr - a_arr
    mean_diff = float(np.mean(diffs))

    if n < 2:
        # A single pair has no within-pair variance to estimate a standard
        # error from — report the point estimate, not a fabricated
        # significance test.
        return {"mean_a": mean_a, "mean_b": mean_b, "mean_diff": mean_diff,
                "se": None, "t": None, "p": None, "n": n}

    se = float(np.std(diffs, ddof=1) / math.sqrt(n))
    if se == 0:
        # Every pair moved by exactly the same amount: t is mathematically
        # infinite. That is a real (if unusual) result, but reporting t=inf,
        # p=0.0 reads as a bug, not a finding — surface it as "no variance to
        # test" instead of dividing by zero.
        return {"mean_a": mean_a, "mean_b": mean_b, "mean_diff": mean_diff,
                "se": 0.0, "t": None, "p": None, "n": n}

    t = mean_diff / se
    p = math.erfc(abs(t) / math.sqrt(2))
    return {"mean_a": mean_a, "mean_b": mean_b, "mean_diff": mean_diff,
            "se": se, "t": t, "p": p, "n": n}


def _floor_value(row: dict) -> float:
    return float(row.get("floor", 0) or 0)


def _win_value(row: dict) -> float:
    return 1.0 if row.get("status") == "win" else 0.0


def _combat_wins_value(row: dict) -> float:
    return float(row.get("combat_wins", 0) or 0)


def _room_hp_per_fight(row: dict, room: str) -> float:
    total = float(row.get(f"total_{room}_hp_loss", 0) or 0)
    fights = float(row.get(f"{room}_combats", 0) or 0)
    # room_seeds already guarantees fights >= 1 in both arms before this is
    # ever called; the guard here is just so a stray 0 can't crash a report.
    return total / fights if fights else 0.0


# Metric name -> (values-from-row extractor, seed list to use). The seed
# list is resolved against the PairingResult at compare() time; room metrics
# use the narrower per-room pairing, everything else uses the full pairing.
_RUN_LEVEL_METRICS = {
    "floor": _floor_value,
    "win": _win_value,
    "combat_wins": _combat_wins_value,
}


def compare(path_a, path_b, *, label_a: str = "A", label_b: str = "B") -> dict:
    """Run the full paired comparison between two eval_rl.py result logs.

    This is the single entry point the CLI and `--json` output both funnel
    through, and the shape returned here IS the `--json` output — keep the
    two in sync rather than building a second representation for print().
    """
    arm_a = load_arm(path_a)
    arm_b = load_arm(path_b)
    pairing = pair_arms(arm_a, arm_b)
    rows_a = arm_a["by_seed"]
    rows_b = arm_b["by_seed"]

    metrics: dict[str, dict] = {}
    for name, extractor in _RUN_LEVEL_METRICS.items():
        metrics[name] = paired_stats(
            [extractor(rows_a[seed]) for seed in pairing.seeds],
            [extractor(rows_b[seed]) for seed in pairing.seeds],
        )
    for room in _HP_LOSS_ROOMS:
        seeds = pairing.room_seeds[room]
        metrics[f"{room}_hp_loss_per_fight"] = paired_stats(
            [_room_hp_per_fight(rows_a[seed], room) for seed in seeds],
            [_room_hp_per_fight(rows_b[seed], room) for seed in seeds],
        )

    return {
        "label_a": label_a,
        "label_b": label_b,
        "path_a": os.fspath(path_a),
        "path_b": os.fspath(path_b),
        "arm_a": {
            "unique_seeds": arm_a["diagnostics"]["unique_seeds"],
            "valid_seeds": arm_a["diagnostics"]["valid_seeds"],
            "duplicates_collapsed": arm_a["diagnostics"]["duplicates_collapsed"],
        },
        "arm_b": {
            "unique_seeds": arm_b["diagnostics"]["unique_seeds"],
            "valid_seeds": arm_b["diagnostics"]["valid_seeds"],
            "duplicates_collapsed": arm_b["diagnostics"]["duplicates_collapsed"],
        },
        "pairs": len(pairing.seeds),
        "only_in_a": pairing.only_in_a,
        "only_in_b": pairing.only_in_b,
        "invalid_in_a": pairing.invalid_in_a,
        "invalid_in_b": pairing.invalid_in_b,
        "room_pairs": {room: len(pairing.room_seeds[room]) for room in _HP_LOSS_ROOMS},
        "metrics": metrics,
    }


_METRIC_LABELS = {
    "floor": "floor",
    "win": "win",
    "combat_wins": "combat_wins",
    "monster_hp_loss_per_fight": "monster_hp_loss_per_fight",
    "elite_hp_loss_per_fight": "elite_hp_loss_per_fight",
    "boss_hp_loss_per_fight": "boss_hp_loss_per_fight",
}


def _fmt(value, digits=3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def format_report(result: dict) -> str:
    """Render `compare()`'s output as the readable text table the CLI prints.

    A `*` after `p` flags p < 0.05 — the eyeball-scan signal for "this
    metric moved". It is not a claim of correctness at small n; see the
    normal-approximation caveat on `paired_stats`.
    """
    lines = []
    lines.append(f"Paired eval: {result['label_a']} (A) vs {result['label_b']} (B)")
    lines.append(
        f"  arm A: {result['arm_a']['unique_seeds']} seeds "
        f"({result['arm_a']['valid_seeds']} valid, "
        f"{result['arm_a']['duplicates_collapsed']} duplicates collapsed)  "
        f"[{result['path_a']}]"
    )
    lines.append(
        f"  arm B: {result['arm_b']['unique_seeds']} seeds "
        f"({result['arm_b']['valid_seeds']} valid, "
        f"{result['arm_b']['duplicates_collapsed']} duplicates collapsed)  "
        f"[{result['path_b']}]"
    )
    lines.append(
        f"  pairs used: {result['pairs']}  "
        f"(only_in_a={result['only_in_a']} only_in_b={result['only_in_b']} "
        f"invalid_in_a={result['invalid_in_a']} invalid_in_b={result['invalid_in_b']})"
    )
    room_pairs = result["room_pairs"]
    lines.append(
        "  per-room pairs: "
        + ", ".join(f"{room}={room_pairs[room]}" for room in _HP_LOSS_ROOMS)
    )
    lines.append("")

    header = f"{'metric':<28}{'n':>5}  {'mean_a':>10}  {'mean_b':>10}  {'diff':>10}  {'se':>8}  {'t':>7}  {'p':>8}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, label in _METRIC_LABELS.items():
        stats = result["metrics"].get(name)
        if stats is None:
            continue
        sig = "*" if (stats["p"] is not None and stats["p"] < 0.05) else " "
        lines.append(
            f"{label:<28}{stats['n']:>5}  "
            f"{_fmt(stats['mean_a']):>10}  {_fmt(stats['mean_b']):>10}  "
            f"{_fmt(stats['mean_diff']):>10}  {_fmt(stats['se']):>8}  "
            f"{_fmt(stats['t'], 2):>7}  {_fmt(stats['p'], 4):>7}{sig}"
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Paired A/B comparison of two agent/eval_rl.py --results-log files "
                    "over their shared fixed-seed set.",
    )
    parser.add_argument("path_a", help="Arm A (baseline) results JSONL")
    parser.add_argument("path_b", help="Arm B (candidate) results JSONL")
    parser.add_argument("--label-a", default="A", help="Display name for arm A")
    parser.add_argument("--label-b", default="B", help="Display name for arm B")
    parser.add_argument("--json", action="store_true",
                        help="Emit the full comparison as one JSON object instead of a text table")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    result = compare(args.path_a, args.path_b, label_a=args.label_a, label_b=args.label_b)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
