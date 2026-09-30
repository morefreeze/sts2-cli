#!/usr/bin/env python3
"""fit_route_model.py -- fit the Phase 3b route model from game logs.

    .venv/bin/python -m agent.fit_route_model --logs DIR_OR_GLOB [--logs ...] \\
        --ascension 1 --out agent/data/route_model_a1.json [--alpha 1.0] [--min-n 20]

Reads the JSONL game logs (python/game_log.py) of one ascension, fits the value
model (agent.route_model.fit_value_model) and the per-room transition cells
(fit_transition_model), saves them as a RouteModel JSON and prints a validation
report. See docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md.
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
from agent.route_model import RouteModel, fit_transition_model, fit_value_model


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


def print_report(games: list[dict], fit: dict, transitions: dict) -> None:
    by_char = Counter(g.get("character") or "?" for g in games)
    print("Games per character: " + ", ".join(f"{c} {n}" for c, n in sorted(by_char.items())))
    print(f"Value model: {fit['n_games']} games, {fit['n_samples']} snapshots, "
          f"{len(fit['cv']['full'])}-fold grouped CV")
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
    args = parser.parse_args(argv)

    paths = collect_logs(args.logs)
    games = load_games(paths)
    print(f"{len(paths)} log file(s), {len(games)} complete game(s) "
          f"({len(paths) - len(games)} excluded: no game_over or unreadable)")
    kept = [g for g in games if g.get("ascension") == args.ascension]
    print(f"Dropped {len(games) - len(kept)} game(s) with ascension != {args.ascension}")
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
    RouteModel(data).save(args.out)
    print_report(kept, fit, transitions)
    print(f"Saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
