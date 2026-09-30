#!/usr/bin/env python3
"""fit_route_model.py -- fit the Phase 3b route model from game logs.

    .venv/bin/python -m agent.fit_route_model --logs DIR_OR_GLOB [--logs ...] \\
        --ascension 1 --out agent/data/route_model_a1.json [--alpha 1.0] [--min-n 20] \\
        [--allow-value-logs]

Reads the JSONL game logs (python/game_log.py) of one ascension, fits the value
model (agent.route_model.fit_value_model) and the per-room transition cells
(fit_transition_model), saves them as a RouteModel JSON and prints a validation
report. Games played under STS2_ROUTE_POLICY=value are skipped unless
--allow-value-logs: their routes depend on the state, so a refit on them would
train on the planner's own choices. See
docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import math
import os
import statistics
import sys
from collections import Counter

from agent.route_data import load_games
from agent.route_model import (
    RouteModel, cell_bucket, cell_keys, fit_transition_model, fit_value_model)

# Pre-room HP bins of the calibration table: label, lower bound (inclusive), upper (exclusive).
CALIBRATION_BINS = (("<25", -math.inf, 25), ("25-35", 25, 35), ("35-50", 35, 50),
                    ("50-65", 50, 65), (">=65", 65, math.inf))
CALIBRATION_ACTS = (1, 2)
CALIBRATION_ROOMS = ("Monster", "Elite")


def collect_logs(specs: list[str]) -> list[str]:
    """``*.jsonl`` files under each spec: a directory contributes every jsonl
    inside it (recursively), anything else is expanded as a glob. Order-stable,
    duplicates dropped."""
    paths: list[str] = []
    for spec in specs:
        if os.path.isdir(spec):
            found = glob.glob(os.path.join(spec, "**", "*.jsonl"), recursive=True)
        else:
            found = [p for p in glob.glob(spec, recursive=True) if os.path.isfile(p)]
        paths.extend(sorted(found))
    return list(dict.fromkeys(paths))


def _mean_se(values: list[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return mean, se


def is_value_log(game: dict) -> bool:
    """True for a game played under STS2_ROUTE_POLICY=value: its run_meta
    experiment tag says so, or its log holds a get_map reply."""
    return "route=value" in (game.get("experiment") or "") or bool(game.get("has_map_state"))


def dedupe_games(games: list[dict]) -> list[dict]:
    """Keep the first game of each (character, seed, ascension). The same run can
    sit under two --logs specs; a game without a seed is never merged."""
    seen: set = set()
    kept = []
    for game in games:
        seed = game.get("seed")
        key = (game.get("character"), seed, game.get("ascension"))
        if seed is not None:
            if key in seen:
                continue
            seen.add(key)
        kept.append(game)
    return kept


def calibration_rows(games: list[dict], model: RouteModel) -> list[dict]:
    """Empirical death rate vs the model's mean death_prob, per Act 1/2 Monster
    and Elite cell (exact act + bucket) and pre-room HP bin, over the same
    samples. In-sample by construction: it shows drift of the estimator, not
    generalisation."""
    split = model.data["transitions"]["split"]
    acc: dict = {}
    for game in games:
        for t in game.get("transitions", []):
            if t["room_type"] not in CALIBRATION_ROOMS or t["act"] not in CALIBRATION_ACTS:
                continue
            pre = t["pre"]
            key = f"{t['act']}|{t['room_type']}|{cell_bucket(t['room_type'], pre, split)}"
            label = next(name for name, lo, hi in CALIBRATION_BINS if lo <= pre["hp"] < hi)
            row = acc.setdefault((key, label), {"n": 0, "died": 0, "q": 0.0})
            row["n"] += 1
            row["died"] += 1 if t["died"] else 0
            row["q"] += model.death_prob(t["act"], t["room_type"], pre)
    order = {name: i for i, (name, _, _) in enumerate(CALIBRATION_BINS)}
    return [{"cell": key, "hp_bin": label, "n": r["n"], "died": r["died"],
             "rate": r["died"] / r["n"], "model_q": r["q"] / r["n"]}
            for (key, label), r in sorted(acc.items(), key=lambda kv: (kv[0][0], order[kv[0][1]]))]


def sparse_cells(transitions: dict) -> list[dict]:
    """Every cell with fewer than min_n samples, and the cell a lookup that lands
    on it falls back to ("zero" when the whole chain is sparse)."""
    min_n = transitions["min_n"]
    cells = transitions["cells"]
    rows = []
    for key, cell in cells.items():
        if cell["n"] >= min_n:
            continue
        act, room_type, bucket = key.split("|")
        chain = cell_keys(act, room_type, bucket)
        later = chain[[k for _, k in chain].index(key) + 1:]
        target = next((k for _, k in later if k in cells and cells[k]["n"] >= min_n), "zero")
        rows.append({"cell": key, "n": cell["n"], "falls_back_to": target})
    return rows


def print_report(games: list[dict], fit: dict, transitions: dict,
                 model: RouteModel | None = None) -> None:
    by_char = Counter(g.get("character") or "?" for g in games)
    print("Games per character: " + ", ".join(f"{c} {n}" for c, n in sorted(by_char.items())))
    print(f"Value model: {fit['n_games']} games, {fit['n_samples']} snapshots, "
          f"{len(fit['cv']['full'])}-fold CV grouped by seed ({fit['n_groups']} seeds)")
    for name in ("full", "hp_only"):
        mean, se = _mean_se(fit["cv"][name])
        print(f"  CV MAE {name:8s} {mean:.3f} +/- {se:.3f} floors")
    print(f"  chosen: {fit['chosen']}")
    print(f"Coefficients (floors per 1 SD), intercept {fit['model']['intercept']:.3f}:")
    for feature, coef in zip(fit["model"]["features"], fit["model"]["coef"]):
        print(f"  {feature:16s} {coef:+8.3f}")
    print(f"Transition cells (Act 1-2; Monster/Elite split at deck_burst "
          f"{transitions['split']:.3f}, min_n {transitions['min_n']}):")
    print(f"  {'cell':22s} {'n':>5s} {'died':>5s} {'d_hp':>7s} {'d_relics':>9s} "
          f"{'d_burst':>8s} {'d_top8':>7s}")
    for key, cell in transitions["cells"].items():
        if key.split("|")[0] in ("1", "2"):
            md = cell["mean_delta"]
            print(f"  {key:22s} {cell['n']:5d} {cell['n_died']:5d} {md['hp']:+7.1f} "
                  f"{md['n_relics']:+9.2f} {md['deck_burst']:+8.3f} {md['deck_top8']:+7.3f}")
    sparse = sparse_cells(transitions)
    print(f"Sparse cells (n < min_n {transitions['min_n']}): {len(sparse)}")
    for row in sparse:
        print(f"  {row['cell']:22s} n={row['n']:<4d} -> falls back to {row['falls_back_to']}")
    if model is not None:
        print("Calibration, Act 1-2 Monster/Elite cells (death rate vs mean model q, "
              "by pre-room HP):")
        print(f"  {'cell':22s} {'hp':>6s} {'n':>5s} {'died':>5s} {'rate':>7s} {'model_q':>8s}")
        for row in calibration_rows(games, model):
            print(f"  {row['cell']:22s} {row['hp_bin']:>6s} {row['n']:5d} {row['died']:5d} "
                  f"{row['rate']:7.3f} {row['model_q']:8.3f}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fit the Phase 3b route model from game logs.")
    parser.add_argument("--logs", action="append", required=True, metavar="DIR_OR_GLOB",
                        help="directory of *.jsonl game logs, or a glob; repeatable")
    parser.add_argument("--ascension", type=int, required=True,
                        help="keep only games whose run_meta ascension equals this")
    parser.add_argument("--out", required=True, help="where to write the route model JSON")
    parser.add_argument("--alpha", type=float, default=1.0, help="ridge alpha (default 1.0)")
    parser.add_argument("--min-n", type=int, default=20,
                        help="minimum samples for a transition cell before it falls back "
                             "to a pooled one (default 20)")
    parser.add_argument("--allow-value-logs", action="store_true",
                        help="also train on games played under STS2_ROUTE_POLICY=value "
                             "(skipped by default: their routes depend on the state)")
    args = parser.parse_args(argv)

    paths = collect_logs(args.logs)
    games = load_games(paths)
    print(f"{len(paths)} log file(s), {len(games)} complete game(s) "
          f"({len(paths) - len(games)} excluded: no game_over or unreadable)")
    kept = [g for g in games if g.get("ascension") == args.ascension]
    print(f"Dropped {len(games) - len(kept)} game(s) with ascension != {args.ascension}")
    if not args.allow_value_logs:
        n = len(kept)
        kept = [g for g in kept if not is_value_log(g)]
        print(f"Skipped {n - len(kept)} game(s) played under the value route policy "
              f"(pass --allow-value-logs to train on them)")
    n = len(kept)
    kept = dedupe_games(kept)
    print(f"Dropped {n - len(kept)} duplicate game(s) (same character, seed, ascension)")
    if len(kept) < 2:
        parser.error(f"need at least 2 games at ascension {args.ascension}, got {len(kept)}")

    fit = fit_value_model(kept, alpha=args.alpha)
    transitions = fit_transition_model(kept, min_n=args.min_n)
    data = {
        "version": 1,
        "value": fit["model"],
        "transitions": transitions,
        "meta": {
            "n_games": len(kept),
            "n_transitions": sum(len(g["transitions"]) for g in kept),
            "characters": dict(sorted(Counter(g.get("character") or "?" for g in kept).items())),
            "ascension": args.ascension,
            "sources": list(args.logs),
            "fitted_at": datetime.date.today().isoformat(),
        },
        "validation": {"cv": fit["cv"], "chosen": fit["chosen"]},
    }
    model = RouteModel(data)
    model.save(args.out)
    print_report(kept, fit, transitions, model)
    print(f"Saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
