#!/usr/bin/env python3
"""train_deck_predictor_v2.py — Card-level deck predictor

Reads `card_pick` + `outcome` events from data/deck_history.jsonl, joins each
pick to its run's max_floor, and learns:
    (deck features + candidate-card features) → run_max_floor

Uses HistGradientBoostingRegressor on ~30 features. Trains 3 rows per
non-SKIP card_pick event (one per offered option, same outcome label).

Pre-reqs:
    1. data/deck_history.jsonl has `card_pick` events (logged by combat_env
       on every card_reward decision)
    2. data/card_metadata.json exists (run `agent/extract_card_db.py` first)

Usage:
    .venv/bin/python agent/extract_card_db.py
    .venv/bin/python agent/train_deck_predictor_v2.py

The v1 predictor (data/deck_predictor.pkl) keeps working from the same
deck_history.jsonl; v2 writes to data/deck_predictor_v2.pkl. card_scoring
loads whichever the calling code asks for.
"""
import argparse, json, os, pickle, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Histogram bins / one-hot vocab — must stay in sync between training and
# inference helpers. Cost is binned 0/1/2/3+; type and rarity are one-hot.
COST_BINS = [0, 1, 2, 3]  # cost >= 3 collapses into bin 3
TYPES = ["Attack", "Skill", "Power"]
RARITIES = ["Common", "Uncommon", "Rare"]


def _deck_aggregate_features(deck_ids, card_db):
    n_cost = [0, 0, 0, 0]
    n_type = [0, 0, 0]
    n_rarity = [0, 0, 0]
    n_known = 0
    for cid in deck_ids:
        meta = card_db.get(cid)
        if not meta:
            continue
        n_known += 1
        c = meta.get("cost")
        if c is not None:
            n_cost[min(int(c), 3)] += 1
        t = meta.get("type")
        if t in TYPES:
            n_type[TYPES.index(t)] += 1
        r = meta.get("rarity")
        if r in RARITIES:
            n_rarity[RARITIES.index(r)] += 1
    return {"cost_hist": n_cost, "type_hist": n_type,
            "rarity_hist": n_rarity, "n_known": n_known}


def _candidate_features(opt):
    cost = opt.get("cost")
    cost_v = float(cost) if cost is not None else -1.0
    t = opt.get("type", "") or ""
    type_oh = [1.0 if t == tt else 0.0 for tt in TYPES]
    r = opt.get("rarity", "") or ""
    rar_oh = [1.0 if r == rr else 0.0 for rr in RARITIES]
    upg = 1.0 if opt.get("upgraded") else 0.0
    return [cost_v] + type_oh + rar_oh + [upg]


FEATURE_NAMES = [
    "deck_dim_attack", "deck_dim_defense", "deck_dim_energy", "deck_dim_draw",
    "arch_str_gain", "arch_str_user",
    "arch_exhaust_payload", "arch_exhaust_fuel", "arch_block_payload",
    "deck_cost_0", "deck_cost_1", "deck_cost_2", "deck_cost_3plus",
    "deck_n_attack", "deck_n_skill", "deck_n_power",
    "deck_n_common", "deck_n_uncommon", "deck_n_rare",
    "deck_size", "floor", "hp_ratio",
    "cand_cost",
    "cand_is_attack", "cand_is_skill", "cand_is_power",
    "cand_is_common", "cand_is_uncommon", "cand_is_rare",
    "cand_upgraded",
]
# 4 dims + 5 arch + 10 deck_aggregates + 3 scalars + 8 candidate = 30


def load_training_rows(history_path, card_db, picked_only=False):
    from agent.card_scoring import score_deck_dimensions, compute_deck_archetype
    picks_by_run = defaultdict(list)
    outcomes = {}
    with open(history_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ev = rec.get("event"); rid = rec.get("run_id")
            if not rid:
                continue
            if ev == "card_pick":
                picks_by_run[rid].append(rec)
            elif ev == "outcome":
                outcomes[rid] = rec.get("max_floor", 0)

    X, y = [], []
    n_used = n_skip = 0
    for rid, picks in picks_by_run.items():
        if rid not in outcomes:
            continue
        outcome = float(outcomes[rid])
        for pick in picks:
            if pick.get("picked") == "SKIP":
                n_skip += 1
                continue
            n_used += 1
            deck_ids = pick.get("deck_before_ids") or []
            # Stub cards with metadata for the existing dim/archetype scorers
            deck_stubs = []
            for cid in deck_ids:
                m = card_db.get(cid, {})
                deck_stubs.append({"id": cid, "name": cid,
                                   "cost": m.get("cost"),
                                   "rarity": m.get("rarity"),
                                   "type": m.get("type")})
            dims = score_deck_dimensions(deck_stubs)
            arch = compute_deck_archetype(deck_stubs)
            agg = _deck_aggregate_features(deck_ids, card_db)
            deck_size = len(deck_ids)
            floor = pick.get("floor", 0)
            mhp = max(pick.get("max_hp", 1) or 1, 1)
            hp_ratio = (pick.get("hp", 0) or 0) / mhp
            deck_part = [
                float(dims.get("attack", 0)),
                float(dims.get("defense", 0)),
                float(dims.get("energy", 0)),
                float(dims.get("draw", 0)),
                float(arch.get("str_gain", 0)),
                float(arch.get("str_user", 0)),
                float(arch.get("exhaust_payload", 0)),
                float(arch.get("exhaust_fuel", 0)),
                float(arch.get("block_payload", 0)),
                float(agg["cost_hist"][0]),
                float(agg["cost_hist"][1]),
                float(agg["cost_hist"][2]),
                float(agg["cost_hist"][3]),
                float(agg["type_hist"][0]),
                float(agg["type_hist"][1]),
                float(agg["type_hist"][2]),
                float(agg["rarity_hist"][0]),
                float(agg["rarity_hist"][1]),
                float(agg["rarity_hist"][2]),
                float(deck_size),
                float(floor),
                float(hp_ratio),
            ]
            options = pick.get("options") or []
            if picked_only:
                # Causally clean: only the picked option's features are paired
                # with the run's actual outcome. Counterfactual outcomes for
                # unpicked options are unknown.
                picked_id = pick.get("picked")
                opts_to_use = [o for o in options if o.get("id") == picked_id]
                if not opts_to_use:
                    continue
            else:
                # All 3 options share the same outcome label — 3× data but
                # candidate features get washed out (model learns "deck → outcome").
                opts_to_use = options
            for opt in opts_to_use:
                cand = _candidate_features(opt)
                X.append(deck_part + cand)
                y.append(outcome)
    return X, y, n_used, n_skip


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/deck_history.jsonl")
    p.add_argument("--card-db", default="data/card_metadata.json")
    p.add_argument("--out", default="data/deck_predictor_v2.pkl")
    p.add_argument("--min-rows", type=int, default=500)
    p.add_argument("--picked-only", action="store_true",
                   help="Train only on the picked option per event (1 row vs 3); causally cleaner")
    args = p.parse_args()

    if not os.path.exists(args.history):
        print(f"No history at {args.history}"); return 1
    if not os.path.exists(args.card_db):
        print(f"No card db at {args.card_db}; run agent/extract_card_db.py first"); return 1

    with open(args.card_db) as f:
        card_db = json.load(f)
    print(f"Loaded card_db: {len(card_db)} cards")

    X, y, n_used, n_skip = load_training_rows(args.history, card_db,
                                              picked_only=args.picked_only)
    mode = "picked-only" if args.picked_only else "all-options"
    print(f"Loaded {len(X)} rows from {n_used} picks ({n_skip} SKIP excluded) [{mode}]")
    if y:
        print(f"y stats: mean={sum(y)/len(y):.2f}, min={min(y)}, max={max(y)}")

    if len(X) < args.min_rows:
        print(f"Need {args.min_rows} rows, have {len(X)}. Skipping training.")
        return 1

    import numpy as np
    X = np.array(X); y = np.array(y)
    print(f"X shape: {X.shape}, expected (N, {len(FEATURE_NAMES)})")
    assert X.shape[1] == len(FEATURE_NAMES), "feature-count mismatch"

    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import cross_val_score

    pipe = Pipeline([("hgb", HistGradientBoostingRegressor(
        max_iter=500, max_depth=8, learning_rate=0.05,
        min_samples_leaf=30, random_state=0))])

    cv_r2 = cross_val_score(pipe, X, y, cv=5, scoring="r2", n_jobs=-1)
    print(f"5-fold CV R²: mean={cv_r2.mean():.3f}, std={cv_r2.std():.3f}")
    pipe.fit(X, y)

    try:
        from sklearn.inspection import permutation_importance
        print("\nTop 15 by permutation importance:")
        idx = np.random.RandomState(0).choice(len(X), min(5000, len(X)), replace=False)
        perm = permutation_importance(pipe, X[idx], y[idx], n_repeats=3,
                                      random_state=0, n_jobs=-1)
        ranked = sorted(zip(FEATURE_NAMES, perm.importances_mean), key=lambda x: -x[1])
        for name, imp in ranked[:15]:
            print(f"  {name:<22s} {imp:+.4f}")
    except Exception as e:
        print(f"(perm importance skipped: {e})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump({"pipeline": pipe, "feature_names": FEATURE_NAMES,
                     "n_train": len(X), "cv_r2_mean": float(cv_r2.mean()),
                     "card_db_size": len(card_db), "version": "v2"}, f)
    print(f"\nSaved → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
