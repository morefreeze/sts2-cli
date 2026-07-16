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
import argparse, json, os, pickle, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Wiki text parser — used to enrich card_metadata stubs with stats so
# score_deck_dimensions and compute_deck_archetype see real damage/block/
# energy/draw values instead of zero (card_metadata only carries
# cost/rarity/type/occurrences).
_DAMAGE_RE = re.compile(r"deal\s+(\d+)\s+damage", re.I)
_BLOCK_RE = re.compile(r"gain\s+(\d+)\s+block", re.I)
_ENERGY_RE = re.compile(r"gain\s+(\d+)\s+energy", re.I)
_DRAW_N_RE = re.compile(r"draw\s+(\d+)\s+cards?", re.I)
_DRAW_A_RE = re.compile(r"draw\s+a\s+card", re.I)


def _enrich_card_db(card_metadata: dict, wiki_path: str = "data/ironclad_cards.json"):
    """Parse wiki normal_text to fill stats={damage,block,energy,cards} on
    each metadata record. Mutates and returns card_metadata."""
    if not os.path.exists(wiki_path):
        print(f"  (no wiki at {wiki_path} — stubs stay stats-less)")
        return card_metadata
    with open(wiki_path) as f:
        wiki = json.load(f).get("cards", [])
    n_enriched = 0
    for w in wiki:
        # Match wiki slug to runtime ids (e.g. "pommel-strike" → "POMMEL_STRIKE")
        slug = w.get("id", "")
        runtime_id = slug.upper().replace("-", "_")
        candidates = [runtime_id, runtime_id.replace("_IRONCLAD", ""),
                      slug.upper(), slug.upper().replace("-", "_")]
        text = w.get("normal_text") or ""
        stats = {}
        if (m := _DAMAGE_RE.search(text)) is not None: stats["damage"] = int(m.group(1))
        if (m := _BLOCK_RE.search(text)) is not None: stats["block"] = int(m.group(1))
        if (m := _ENERGY_RE.search(text)) is not None: stats["energy"] = int(m.group(1))
        if (m := _DRAW_N_RE.search(text)) is not None: stats["cards"] = int(m.group(1))
        elif _DRAW_A_RE.search(text): stats["cards"] = 1
        for cid in candidates:
            if cid in card_metadata:
                rec = card_metadata[cid]
                rec.setdefault("stats", stats)
                rec.setdefault("description", text)
                n_enriched += 1
                break
    print(f"  enriched {n_enriched}/{len(card_metadata)} card records with parsed stats")
    return card_metadata


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


# Energy-scaling relics — they raise base/per-turn energy by 1-4, which inflates
# the value of high-cost cards and changes the deck's effective archetype.
# Captured as a single binary feature: presence alters card-pick fitness.
_ENERGY_RELICS = {
    "LANTERN", "VERY_HOT_COCOA", "VENERABLE_TEA_SET",
    "PHILOSOPHERS_STONE", "PUMPKIN_CANDLE", "SOZU", "SPIKED_GAUNTLETS",
    "VELVET_CHOKER", "WHISPERING_EARRING", "BLESSED_ANTLER",
    "BLOOD_SOAKED_ROSE", "BREAD",
}


def _relic_features(relics):
    """Compute (relic_count, has_energy_relic, has_burning_blood). `relics` may
    be None for legacy rows (logged before relic tracking); treat as 0 in that
    case."""
    if not relics:
        return 0.0, 0.0, 0.0
    ids = {str(r).upper().replace("-", "_") for r in relics}
    has_energy = 1.0 if (ids & _ENERGY_RELICS) else 0.0
    has_bb = 1.0 if "BURNING_BLOOD" in ids else 0.0
    return float(len(relics)), has_energy, has_bb


def _interaction_features(deck_part_dict, cand_dict, has_energy, has_bb, relic_count):
    """12 explicit deck × relic and candidate × relic cross features."""
    return [
        has_energy * deck_part_dict["cost2_count"],
        has_energy * deck_part_dict["cost3plus_count"],
        has_energy * deck_part_dict["deck_size"],
        has_energy * deck_part_dict["n_power"],
        has_energy * deck_part_dict["dim_attack"],
        has_bb * deck_part_dict["hp_ratio"],
        has_bb * deck_part_dict["n_attack"],
        relic_count * deck_part_dict["cost3plus_count"],
        cand_dict["cost"] * has_energy,
        cand_dict["is_power"] * has_energy,
        cand_dict["cost"] * relic_count,
        cand_dict["is_attack"] * has_bb,
    ]


def _norm_id(s):
    return str(s).upper().replace("-", "_").replace(" ", "_")


# 14-bucket floor one-hot: floor == 3, 4, ..., 16 (one bucket each)
FLOOR_BUCKETS = list(range(3, 17))


def _floor_onehot(floor):
    return [1.0 if floor == f else 0.0 for f in FLOOR_BUCKETS]


def _scan_top_relics(history_path, n=50):
    """One-pass scan to find top-N most common relic IDs in card_pick rows."""
    counts = defaultdict(int)
    with open(history_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") == "card_pick":
                for r in rec.get("relics") or []:
                    counts[_norm_id(r)] += 1
    return [r for r, _ in sorted(counts.items(), key=lambda x: -x[1])[:n]]


def _deck_card_counts(deck_ids, card_idx):
    """One feature per known card: count in deck."""
    out = [0.0] * len(card_idx)
    for cid in deck_ids:
        i = card_idx.get(_norm_id(cid))
        if i is not None:
            out[i] += 1.0
    return out


def _cand_card_onehot(opt, card_idx):
    out = [0.0] * len(card_idx)
    i = card_idx.get(_norm_id(opt.get("id", "")))
    if i is not None:
        out[i] = 1.0
    return out


def _relic_onehot(relics, relic_idx):
    out = [0.0] * len(relic_idx)
    for r in relics or []:
        i = relic_idx.get(_norm_id(r))
        if i is not None:
            out[i] = 1.0
    return out


def _candidate_features(opt):
    cost = opt.get("cost")
    cost_v = float(cost) if cost is not None else -1.0
    t = opt.get("type", "") or ""
    type_oh = [1.0 if t == tt else 0.0 for tt in TYPES]
    r = opt.get("rarity", "") or ""
    rar_oh = [1.0 if r == rr else 0.0 for rr in RARITIES]
    upg = 1.0 if opt.get("upgraded") else 0.0
    return [cost_v] + type_oh + rar_oh + [upg]


BASE_FEATURE_NAMES = [
    "deck_dim_attack", "deck_dim_defense", "deck_dim_energy", "deck_dim_draw",
    "arch_str_gain", "arch_str_user",
    "arch_exhaust_payload", "arch_exhaust_fuel", "arch_block_payload",
    "deck_cost_0", "deck_cost_1", "deck_cost_2", "deck_cost_3plus",
    "deck_n_attack", "deck_n_skill", "deck_n_power",
    "deck_n_common", "deck_n_uncommon", "deck_n_rare",
    "deck_size", "floor", "hp_ratio",
    "relic_count", "has_energy_relic", "has_burning_blood",
    "cand_cost",
    "cand_is_attack", "cand_is_skill", "cand_is_power",
    "cand_is_common", "cand_is_uncommon", "cand_is_rare",
    "cand_upgraded",
]
INTERACTION_FEATURE_NAMES = [
    "ix_energy_x_cost2", "ix_energy_x_cost3plus", "ix_energy_x_decksize",
    "ix_energy_x_npower", "ix_energy_x_dimattack",
    "ix_bb_x_hpratio", "ix_bb_x_nattack",
    "ix_reliccount_x_cost3plus",
    "ix_candcost_x_energy", "ix_candpower_x_energy",
    "ix_candcost_x_reliccount", "ix_candattack_x_bb",
]
# 4 dims + 5 arch + 10 deck_aggregates + 3 scalars + 3 relic + 8 candidate = 33 base
# + 12 interaction + 80 deck_card_count + 80 cand_card_oh + 50 relic_oh + 14 floor_oh = 269 total


def load_training_rows(history_path, card_db, picked_only=False, relics_only=False,
                       card_ids=None, top_relics=None):
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

    card_idx = {cid: i for i, cid in enumerate(card_ids or [])}
    relic_idx = {r: i for i, r in enumerate(top_relics or [])}
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
            if relics_only and not pick.get("relics"):
                continue
            n_used += 1
            deck_ids = pick.get("deck_before_ids") or []
            # Stub cards with metadata for the existing dim/archetype scorers.
            # Stats are required so card_dimensions reads non-zero damage/block/
            # energy/draw — _enrich_card_db has populated them above.
            deck_stubs = []
            for cid in deck_ids:
                m = card_db.get(cid, {})
                deck_stubs.append({"id": cid, "name": cid,
                                   "cost": m.get("cost"),
                                   "rarity": m.get("rarity"),
                                   "type": m.get("type"),
                                   "stats": m.get("stats") or {},
                                   "description": m.get("description") or ""})
            dims = score_deck_dimensions(deck_stubs)
            arch = compute_deck_archetype(deck_stubs)
            agg = _deck_aggregate_features(deck_ids, card_db)
            deck_size = len(deck_ids)
            floor = pick.get("floor", 0)
            mhp = max(pick.get("max_hp", 1) or 1, 1)
            hp_ratio = (pick.get("hp", 0) or 0) / mhp
            relic_count, has_energy_relic, has_bb = _relic_features(pick.get("relics"))
            deck_card_counts = _deck_card_counts(deck_ids, card_idx)
            relic_oh = _relic_onehot(pick.get("relics"), relic_idx)
            floor_oh = _floor_onehot(floor)
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
                relic_count,
                has_energy_relic,
                has_bb,
            ]
            # Snapshot key deck stats for interaction computation
            deck_stats = {
                "cost2_count": float(agg["cost_hist"][2]),
                "cost3plus_count": float(agg["cost_hist"][3]),
                "deck_size": float(deck_size),
                "n_power": float(agg["type_hist"][2]),
                "n_attack": float(agg["type_hist"][0]),
                "dim_attack": float(dims.get("attack", 0)),
                "hp_ratio": float(hp_ratio),
            }
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
                cand_oh = _cand_card_onehot(opt, card_idx)
                cand_stats = {
                    "cost": cand[0],          # cand_cost
                    "is_attack": cand[1],     # cand_is_attack
                    "is_power": cand[3],      # cand_is_power
                }
                ix = _interaction_features(deck_stats, cand_stats,
                                            has_energy_relic, has_bb, relic_count)
                X.append(deck_part + cand + ix + deck_card_counts + cand_oh
                          + relic_oh + floor_oh)
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
    p.add_argument("--relics-only", action="store_true",
                   help="Train only on picks with a non-empty relics field (newer rows); "
                        "causally clean for relic-aware feature signal")
    args = p.parse_args()

    if not os.path.exists(args.history):
        print(f"No history at {args.history}"); return 1
    if not os.path.exists(args.card_db):
        print(f"No card db at {args.card_db}; run agent/extract_card_db.py first"); return 1

    with open(args.card_db) as f:
        card_db = json.load(f)
    print(f"Loaded card_db: {len(card_db)} cards")
    _enrich_card_db(card_db)

    card_ids = sorted(card_db.keys())  # stable index for one-hot
    print(f"Scanning top-50 relics in history ...")
    top_relics = _scan_top_relics(args.history, n=50)
    print(f"  top relics: {top_relics[:5]}... (+{max(len(top_relics)-5,0)} more)")

    # Padded vocab for stable feature count: enforce exactly 80 cards + 50 relics
    card_ids = (card_ids + [f"__PAD_CARD_{i}" for i in range(80)])[:80]
    top_relics = (top_relics + [f"__PAD_RELIC_{i}" for i in range(50)])[:50]

    feature_names = (list(BASE_FEATURE_NAMES)
                     + list(INTERACTION_FEATURE_NAMES)
                     + [f"deck_count_{cid}" for cid in card_ids]
                     + [f"cand_oh_{cid}" for cid in card_ids]
                     + [f"relic_{rid}" for rid in top_relics]
                     + [f"floor_eq_{f}" for f in FLOOR_BUCKETS])

    X, y, n_used, n_skip = load_training_rows(args.history, card_db,
                                              picked_only=args.picked_only,
                                              relics_only=args.relics_only,
                                              card_ids=card_ids,
                                              top_relics=top_relics)
    mode = ("picked-only" if args.picked_only else "all-options") + (
        " | relics-only" if args.relics_only else "")
    print(f"Loaded {len(X)} rows from {n_used} picks ({n_skip} SKIP excluded) [{mode}]")
    if y:
        print(f"y stats: mean={sum(y)/len(y):.2f}, min={min(y)}, max={max(y)}")

    if len(X) < args.min_rows:
        print(f"Need {args.min_rows} rows, have {len(X)}. Skipping training.")
        return 1

    import numpy as np
    X = np.array(X); y = np.array(y)
    print(f"X shape: {X.shape}, expected (N, {len(feature_names)})")
    assert X.shape[1] == len(feature_names), f"feature-count mismatch: {X.shape[1]} vs {len(feature_names)}"

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
        ranked = sorted(zip(feature_names, perm.importances_mean), key=lambda x: -x[1])
        for name, imp in ranked[:15]:
            print(f"  {name:<30s} {imp:+.4f}")
    except Exception as e:
        print(f"(perm importance skipped: {e})")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump({"pipeline": pipe, "feature_names": feature_names,
                     "card_ids": card_ids, "top_relics": top_relics,
                     "floor_buckets": FLOOR_BUCKETS,
                     "n_train": len(X), "cv_r2_mean": float(cv_r2.mean()),
                     "card_db_size": len(card_db), "version": "v2-269"}, f)
    print(f"\nSaved → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
