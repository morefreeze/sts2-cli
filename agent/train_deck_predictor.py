#!/usr/bin/env python3
"""train_deck_predictor.py — Learn a deck → max_floor regressor from game history.

Reads `data/deck_history.jsonl` which has rows like:
    {"event": "milestone", "run_id": ..., "floor_crossed": 5, "deck_quality": 4.5,
     "dims": {"attack":..., ...}, "archetype": {...}, "cards": [...]}
    {"event": "outcome", "run_id": ..., "max_floor": 14, "won": false}

Pairs each milestone snapshot with its run's final outcome, builds a feature
vector from deck stats, and fits a Ridge regressor to predict `max_floor`. The
saved model (`data/deck_predictor.pkl`) is loaded at import time by
`card_scoring.py` to nudge card_reward picks toward decks the model predicts
will reach further.

Usage:
    .venv/bin/python agent/train_deck_predictor.py [--history data/deck_history.jsonl]
                                                    [--out data/deck_predictor.pkl]
                                                    [--min-rows 200]
"""
import argparse, json, os, pickle, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _features_from_row(row: dict) -> list[float]:
    """Build a fixed-length feature vector from a milestone snapshot row.
    Features (in order):
      [floor_crossed, deck_size, deck_quality,
       attack_dim, defense_dim, energy_dim, draw_dim,
       arch_str_gain, arch_str_user, arch_exhaust_payload,
       arch_exhaust_fuel, arch_block_payload]
    """
    dims = row.get("dims", {})
    arch = row.get("archetype", {})
    return [
        float(row.get("floor_crossed", 0)),
        float(row.get("deck_size", 0)),
        float(row.get("deck_quality", 0)),
        float(dims.get("attack", 0)),
        float(dims.get("defense", 0)),
        float(dims.get("energy", 0)),
        float(dims.get("draw", 0)),
        float(arch.get("str_gain", 0)),
        float(arch.get("str_user", 0)),
        float(arch.get("exhaust_payload", 0)),
        float(arch.get("exhaust_fuel", 0)),
        float(arch.get("block_payload", 0)),
    ]


FEATURE_NAMES = [
    "floor_crossed", "deck_size", "deck_quality",
    "attack", "defense", "energy", "draw",
    "str_gain", "str_user", "exhaust_payload",
    "exhaust_fuel", "block_payload",
]


def load_pairs(history_path: str, target_kind: str = "lift"):
    """Read JSONL, pair milestone rows with the matching run's outcome row.
    Returns list of (features, target_value).

    target_kind:
      - "max_floor": absolute max_floor reached (legacy)
      - "lift": max_floor - floor_crossed (remaining floors from this snapshot;
                forces the model to learn *deck-driven future progress*, not the
                trivial "you crossed floor F so you reached at least F" baseline)
    """
    by_run: dict = defaultdict(lambda: {"milestones": [], "outcome": None})
    with open(history_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            rid = rec.get("run_id")
            if not rid:
                continue
            if rec.get("event") == "milestone":
                by_run[rid]["milestones"].append(rec)
            elif rec.get("event") == "outcome":
                by_run[rid]["outcome"] = rec
    pairs = []
    for rid, bundle in by_run.items():
        out = bundle["outcome"]
        if not out:
            continue
        max_floor = float(out.get("max_floor", 0))
        for ms in bundle["milestones"]:
            floor_crossed = float(ms.get("floor_crossed", 0))
            if target_kind == "lift":
                target = max(0.0, max_floor - floor_crossed)
            else:
                target = max_floor
            pairs.append((_features_from_row(ms), target))
    return pairs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/deck_history.jsonl")
    p.add_argument("--out", default="data/deck_predictor.pkl")
    p.add_argument("--min-rows", type=int, default=200,
                   help="Refuse to train if fewer than this many (milestone, outcome) pairs")
    p.add_argument("--target", default="lift", choices=["lift", "max_floor"],
                   help="lift = max_floor - floor_crossed (recommended); "
                        "max_floor = legacy absolute target")
    p.add_argument("--model", default="hgb", choices=["hgb", "ridge"],
                   help="hgb = HistGradientBoostingRegressor (non-linear, recommended); "
                        "ridge = linear baseline")
    args = p.parse_args()

    if not os.path.exists(args.history):
        print(f"No history file at {args.history}")
        return 1

    pairs = load_pairs(args.history, target_kind=args.target)
    print(f"Loaded {len(pairs)} (milestone, outcome) pairs from {args.history}")
    print(f"Target: {args.target} (mean={sum(p[1] for p in pairs)/len(pairs):.2f}, "
          f"min={min(p[1] for p in pairs):.1f}, max={max(p[1] for p in pairs):.1f})")
    if len(pairs) < args.min_rows:
        print(f"Need at least {args.min_rows} pairs to train; have {len(pairs)}. Skipping.")
        return 1

    import numpy as np
    X = np.array([p[0] for p in pairs])
    y = np.array([p[1] for p in pairs])

    try:
        from sklearn.linear_model import Ridge
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline
        from sklearn.model_selection import cross_val_score
    except ImportError:
        print("scikit-learn not installed — install with: .venv/bin/pip install scikit-learn")
        return 1

    if args.model == "hgb":
        # HistGradientBoosting handles 175k samples in <1s, captures non-linear
        # deck-composition effects Ridge can't (e.g., "10+ basic strikes is bad
        # even at high attack score" — interaction between deck_size and dims).
        pipe = Pipeline([("hgb", HistGradientBoostingRegressor(
            max_iter=300, max_depth=6, learning_rate=0.05,
            min_samples_leaf=50, random_state=0))])
    else:
        pipe = Pipeline([("scaler", StandardScaler()),
                         ("ridge", Ridge(alpha=1.0))])
    cv_r2 = cross_val_score(pipe, X, y, cv=5, scoring="r2")
    print(f"5-fold CV R²: mean={cv_r2.mean():.3f}, std={cv_r2.std():.3f}")
    pipe.fit(X, y)

    if args.model == "ridge":
        coefs = pipe.named_steps["ridge"].coef_
        print("\nFeature importance (standardized coefs):")
        for name, c in sorted(zip(FEATURE_NAMES, coefs), key=lambda x: -abs(x[1])):
            print(f"  {name:<20s} {c:+.3f}")
    else:
        try:
            from sklearn.inspection import permutation_importance
            print("\nPermutation importance (on a 10k sample, takes a few seconds)...")
            idx = np.random.RandomState(0).choice(len(X), min(10000, len(X)), replace=False)
            perm = permutation_importance(pipe, X[idx], y[idx], n_repeats=3,
                                          random_state=0, n_jobs=-1)
            for name, imp in sorted(zip(FEATURE_NAMES, perm.importances_mean),
                                    key=lambda x: -x[1]):
                print(f"  {name:<20s} {imp:+.3f}")
        except Exception as e:
            print(f"(perm importance skipped: {e})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump({"pipeline": pipe, "feature_names": FEATURE_NAMES,
                     "n_train": len(pairs), "cv_r2_mean": float(cv_r2.mean()),
                     "target": args.target, "model": args.model}, f)
    print(f"\nSaved predictor → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
