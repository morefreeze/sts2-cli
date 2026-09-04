#!/usr/bin/env python3
"""train_deck_predictor_hp.py — Card-level deck predictor targeting HP loss

Same idea as agent/train_deck_predictor_v2.py, but the learned target is
"HP lost per floor over the next K floors" instead of `run_max_floor`. HP
loss is a much denser, lower-variance signal per pick than "did this run
reach floor N" (measured: ~1/4 as many games needed for the same
statistical power), so it should let the predictor-driven card-pick bonus
converge on far less data.

LOWER predicted loss is BETTER. This is the opposite sense from v2's
run_max_floor target (higher is better) — see agent/card_scoring.py's
`HP_PREDICTOR_WEIGHT` wiring for the sign flip this implies downstream.

What's shared with train_deck_predictor_v2.py (imported, not duplicated):
    - card_db enrichment (`_enrich_card_db`)
    - the 269-dim feature layout: deck aggregates (`_deck_aggregate_features`),
      relic features (`_relic_features`, `_ENERGY_RELICS`), deck x relic /
      candidate x relic interaction features (`_interaction_features`),
      candidate features (`_candidate_features`), card/relic one-hot vocab
      (`_deck_card_counts`, `_cand_card_onehot`, `_relic_onehot`,
      `_scan_top_relics`), floor one-hot buckets (`_floor_onehot`,
      FLOOR_BUCKETS), and the COST_BINS/TYPES/RARITIES/BASE_FEATURE_NAMES/
      INTERACTION_FEATURE_NAMES constants.
    - HistGradientBoostingRegressor + sklearn Pipeline setup.
What's new here:
    - the label itself (HP-loss-per-floor instead of run_max_floor) and the
      two-pass run scan needed to compute it (`load_picks_and_outcomes`,
      `compute_hp_loss_labels`).
    - a run_id-grouped train/test holdout split for the performance report
      (v2 reports plain 5-fold `cross_val_score`, which is NOT grouped by
      run_id; since multiple rows come from the same run, an ungrouped split
      leaks. The task for this script explicitly calls for a grouped split,
      so this is intentionally not "modeled closely" on v2 there).

SKIP handling: v2 drops `picked == "SKIP"` picks entirely when building rows
(they carry no candidate-card outcome). This script does the same and does
NOT add an `is_skip` feature. Reasoning: the shared 269-dim feature layout
has no free slot for "no candidate was picked" — every one of its 33 base
features is either a deck-aggregate or a `cand_*` candidate feature assuming
a real candidate card. Adding an `is_skip` feature would grow that layout by
one dimension, which the spec for this task explicitly rules out ("Do not
reshape v2's feature layout"). SKIP *events* are still used as label
waypoints (their floor/hp values count as valid "later observations" when
computing another pick's forward-window label) — only the SKIP row itself is
excluded from the training matrix.

Label definition (see also agent/card_scoring.py and the design doc this
followed): for a pick at floor F in run R, look forward through R's
card_pick events (any `picked` value, including SKIP) for the last one at
floor <= F + K_FLOORS.
    - If found: label = (hp_at_F - hp_at_end) / max_hp / (floor_end - F),
      numerator clamped at >= 0.
    - If the run's outcome says it ended within the window
      (max_floor <= F + K_FLOORS), that ALWAYS overrides the branch above
      (checked first), because a forward observation right before death
      typically shows little/no HP change and would otherwise make dying
      look free: label = hp_at_F / max_hp / max(max_floor - F, 1).
    - If neither applies, the pick has no usable label and is dropped.

K_FLOORS defaults to 5, overridable via the K_FLOORS env var or --k-floors.

2026-08: card-id vocab + axis features (diverges from v2 here). v2's
269-dim layout's one-hot card-id vocab (the `_deck_card_counts`/
`_cand_card_onehot` slots above) was built from data/card_metadata.json,
which only covers the ~80 cards the agent happened to see played in
deck_history.jsonl -- 84% of the game's 507 cards reach the model with no
identity at all, just cost/type/rarity. This script instead sources its
card-id vocab from data/card_db_advisor.json (agent/build_advisor_ratings.py
output, 507 cards total), filtered to the IRONCLAD+SHARED cards
(deck_history.jsonl is Ironclad-only, so nothing else can ever appear --
164 cards, no padding needed). It also adds AXIS features: each advisor
card carries `data-card-axes` structured effect tags (92 distinct axes
among the IRONCLAD+SHARED cards); a 92-dim deck-side axis-count vector
(`_deck_axis_counts`) and a 92-dim candidate-side axis one-hot
(`_cand_axis_onehot`) are appended after the floor one-hot. Axes generalize
where the one-hot id vocab can't: a one-hot only "knows" a card if training
data happened to include it, but an axis describes what ANY card with that
tag does. The axis vocab is stored in the pickle (`axis_vocab`) so
inference (agent/card_scoring.py's `_hp_axis_features`) can't drift from
training's fixed order.

Pre-reqs (same as v2, plus the advisor card db):
    1. data/deck_history.jsonl has `card_pick` + `outcome` events
    2. data/card_metadata.json exists (run `agent/extract_card_db.py` first)
    3. data/card_db_advisor.json exists (run `agent/build_advisor_ratings.py`
       first -- needs a saved advisor.html snapshot or network access)

Usage:
    .venv/bin/python agent/train_deck_predictor_hp.py
    K_FLOORS=8 .venv/bin/python agent/train_deck_predictor_hp.py

Writes data/deck_predictor_hp.pkl. card_scoring.py only loads it when
STS2_PREDICTOR_TARGET=hp is set (default is the existing floor predictor;
unset behaviour is unchanged).
"""
import argparse, json, os, pickle, sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.train_deck_predictor_v2 import (  # noqa: E402  (see module docstring: shared feature code)
    BASE_FEATURE_NAMES,
    COST_BINS,
    FLOOR_BUCKETS,
    INTERACTION_FEATURE_NAMES,
    RARITIES,
    TYPES,
    _candidate_features,
    _cand_card_onehot,
    _deck_aggregate_features,
    _deck_card_counts,
    _enrich_card_db,
    _floor_onehot,
    _interaction_features,
    _norm_id,
    _relic_features,
    _relic_onehot,
    _scan_top_relics,
)

DEFAULT_K_FLOORS = int(os.environ.get("K_FLOORS", "5"))


def load_picks_and_outcomes(history_path: str):
    """Stream data/deck_history.jsonl once. Returns (picks_by_run, outcomes):
    picks_by_run[rid] is that run's card_pick records (ANY `picked` value,
    including SKIP), sorted chronologically by `ts`; outcomes[rid] is the
    run's max_floor (last-seen value if a run somehow logged >1 outcome).

    Streams line by line — never slurps the (446 MB+) file."""
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
    for picks in picks_by_run.values():
        picks.sort(key=lambda r: (r.get("ts") or 0))
    return picks_by_run, outcomes


def compute_hp_loss_labels(picks_by_run: dict, outcomes: dict, k_floors: int):
    """Compute the HP-loss-per-floor label for every (run_id, pick_index)
    pair. Returns (labels, stats):
        labels: {(run_id, index_into_picks_by_run[run_id]): float}
        stats: counts of {"death_window", "normal", "skipped_no_window"}

    See module docstring for the label definition; the death-window branch
    is checked FIRST and overrides the forward-observation branch whenever
    both would otherwise apply (validated against the reference stats in
    the task spec: this ordering reproduces the given death-window mean of
    0.2281 to 4 decimal places on the full data/deck_history.jsonl)."""
    labels = {}
    stats = {"death_window": 0, "normal": 0, "skipped_no_window": 0}
    for rid, picks in picks_by_run.items():
        max_floor = outcomes.get(rid)
        n = len(picks)
        for i, pick in enumerate(picks):
            F = pick.get("floor", 0)
            mhp = max(pick.get("max_hp", 1) or 1, 1)
            hp_f = pick.get("hp", 0) or 0
            label = None
            is_death = False
            if max_floor is not None and max_floor <= F + k_floors:
                denom = max(max_floor - F, 1)
                numer = max(hp_f, 0)
                label = (numer / mhp) / denom
                is_death = True
            else:
                end_pick = None
                for j in range(i + 1, n):
                    fj = picks[j].get("floor", 0)
                    if fj <= F + k_floors:
                        end_pick = picks[j]
                if end_pick is not None:
                    floor_end = end_pick.get("floor", 0)
                    hp_end = end_pick.get("hp", 0) or 0
                    denom = floor_end - F
                    if denom > 0:
                        numer = max(hp_f - hp_end, 0)
                        label = (numer / mhp) / denom
            if label is None:
                stats["skipped_no_window"] += 1
                continue
            labels[(rid, i)] = label
            stats["death_window" if is_death else "normal"] += 1
    return labels, stats


def _deck_axis_counts(deck_ids, axis_by_id, axis_idx):
    """Sum of each axis's occurrence count over the deck (92 dims, fixed
    order given by axis_idx) — the deck-side half of the axis features.
    Unlike the one-hot card-id vocab (which only "knows" cards seen during
    training), axes describe what ANY card does, so this generalizes to
    off-vocab cards too, as long as they're in axis_by_id."""
    out = [0.0] * len(axis_idx)
    for cid in deck_ids:
        for a in axis_by_id.get(_norm_id(cid), ()):
            i = axis_idx.get(a)
            if i is not None:
                out[i] += 1.0
    return out


def _cand_axis_onehot(opt, axis_by_id, axis_idx):
    """Binary indicator of the candidate's axes (92 dims, same order as
    _deck_axis_counts) — the candidate-side half of the axis features."""
    out = [0.0] * len(axis_idx)
    cid = _norm_id(opt.get("id", ""))
    for a in axis_by_id.get(cid, ()):
        i = axis_idx.get(a)
        if i is not None:
            out[i] = 1.0
    return out


def load_training_rows(history_path, card_db, k_floors=DEFAULT_K_FLOORS,
                       picked_only=False, relics_only=False,
                       card_ids=None, top_relics=None,
                       axis_vocab=None, axis_by_id=None):
    """Builds (X, y, run_ids, n_used, n_skip_dropped) rows for the HP-loss
    target. `run_ids[k]` is the run_id that produced row X[k]/y[k] — needed
    by main() for the grouped train/test split.

    Row-building mirrors train_deck_predictor_v2.load_training_rows (same
    deck/candidate/interaction/vocab feature construction) except the label
    comes from compute_hp_loss_labels() instead of the run's max_floor, SKIP
    picks are dropped (see module docstring), and — new here — each row also
    carries axis features (see _deck_axis_counts/_cand_axis_onehot) appended
    after the floor one-hot, when axis_vocab/axis_by_id are given."""
    from agent.card_scoring import score_deck_dimensions, compute_deck_archetype

    picks_by_run, outcomes = load_picks_and_outcomes(history_path)
    labels, label_stats = compute_hp_loss_labels(picks_by_run, outcomes, k_floors)
    print(f"  label stats: {label_stats}")

    card_idx = {cid: i for i, cid in enumerate(card_ids or [])}
    relic_idx = {r: i for i, r in enumerate(top_relics or [])}
    axis_idx = {a: i for i, a in enumerate(axis_vocab or [])}
    axis_by_id = axis_by_id or {}
    X, y, run_ids = [], [], []
    n_used = n_skip_dropped = 0
    for rid, picks in picks_by_run.items():
        for i, pick in enumerate(picks):
            if pick.get("picked") == "SKIP":
                n_skip_dropped += 1
                continue
            label = labels.get((rid, i))
            if label is None:
                continue
            if relics_only and not pick.get("relics"):
                continue
            n_used += 1
            deck_ids = pick.get("deck_before_ids") or []
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
            deck_axis_cts = _deck_axis_counts(deck_ids, axis_by_id, axis_idx)
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
                picked_id = pick.get("picked")
                opts_to_use = [o for o in options if o.get("id") == picked_id]
                if not opts_to_use:
                    continue
            else:
                opts_to_use = options
            for opt in opts_to_use:
                cand = _candidate_features(opt)
                cand_oh = _cand_card_onehot(opt, card_idx)
                cand_axis_oh = _cand_axis_onehot(opt, axis_by_id, axis_idx)
                cand_stats = {
                    "cost": cand[0],
                    "is_attack": cand[1],
                    "is_power": cand[3],
                }
                ix = _interaction_features(deck_stats, cand_stats,
                                            has_energy_relic, has_bb, relic_count)
                X.append(deck_part + cand + ix + deck_card_counts + cand_oh
                          + relic_oh + floor_oh + deck_axis_cts + cand_axis_oh)
                y.append(label)
                run_ids.append(rid)
    return X, y, run_ids, n_used, n_skip_dropped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/deck_history.jsonl")
    p.add_argument("--card-db", default="data/card_metadata.json")
    p.add_argument("--advisor-card-db", default="data/card_db_advisor.json",
                   help="Full 507-card advisor DB (agent/build_advisor_ratings.py "
                        "output). Supplies the expanded IRONCLAD+SHARED card-id "
                        "vocab (replacing card_metadata.json's ~80-card one-hot "
                        "vocab) and the axis feature vocab.")
    p.add_argument("--out", default="data/deck_predictor_hp.pkl")
    p.add_argument("--min-rows", type=int, default=500)
    p.add_argument("--k-floors", type=int, default=DEFAULT_K_FLOORS,
                   help="Label window: HP lost per floor over the next K "
                        "floors. Default from K_FLOORS env var, else 5.")
    p.add_argument("--picked-only", action="store_true",
                   help="Train only on the picked option per event (1 row vs 3); causally cleaner")
    p.add_argument("--relics-only", action="store_true",
                   help="Train only on picks with a non-empty relics field (newer rows); "
                        "causally clean for relic-aware feature signal")
    p.add_argument("--test-frac", type=float, default=0.2,
                   help="Fraction of RUNS (not rows) held out for the MAE report")
    args = p.parse_args()

    if not os.path.exists(args.history):
        print(f"No history at {args.history}"); return 1
    if not os.path.exists(args.card_db):
        print(f"No card db at {args.card_db}; run agent/extract_card_db.py first"); return 1
    if not os.path.exists(args.advisor_card_db):
        print(f"No advisor card db at {args.advisor_card_db}; "
              f"run agent/build_advisor_ratings.py first"); return 1

    with open(args.card_db) as f:
        card_db = json.load(f)
    print(f"Loaded card_db: {len(card_db)} cards")
    _enrich_card_db(card_db)

    with open(args.advisor_card_db) as f:
        advisor_db = json.load(f)
    # Card-id one-hot vocab: the IRONCLAD+SHARED set, not card_metadata.json's
    # ~80-card vocab -- deck_history.jsonl is Ironclad-only, so these are the
    # only cards that can ever appear as a deck card or candidate. Unlike the
    # old vocab (built from whatever the agent happened to see played),
    # this is the full reachable catalogue, no padding needed.
    ironclad_shared = {cid: r for cid, r in advisor_db.items()
                       if r.get("character") in ("IRONCLAD", "SHARED")}
    card_ids = sorted(ironclad_shared)
    # Axis vocab: only the axes that IRONCLAD+SHARED cards can actually carry
    # (92 of the advisor's 176 total axes) -- axes belonging exclusively to
    # other characters' cards would be permanently-zero columns here.
    axis_vocab = sorted({a for r in ironclad_shared.values() for a in (r.get("axes") or [])})
    # Axis membership lookup by card id: sourced from the FULL 507-card
    # advisor db (not just IRONCLAD+SHARED) so any id that turns up in
    # deck_before_ids/options resolves -- in practice always an
    # IRONCLAD+SHARED id, per the vocab note above, but this stays correct
    # even if that invariant is ever loosened.
    axis_by_id = {cid: set(r.get("axes") or []) for cid, r in advisor_db.items()}
    print(f"Advisor card-id vocab (IRONCLAD+SHARED): {len(card_ids)} cards")
    print(f"Axis vocab (IRONCLAD+SHARED axes): {len(axis_vocab)} axes")

    print("Scanning top-50 relics in history ...")
    top_relics = _scan_top_relics(args.history, n=50)
    print(f"  top relics: {top_relics[:5]}... (+{max(len(top_relics)-5,0)} more)")
    top_relics = (top_relics + [f"__PAD_RELIC_{i}" for i in range(50)])[:50]

    feature_names = (list(BASE_FEATURE_NAMES)
                     + list(INTERACTION_FEATURE_NAMES)
                     + [f"deck_count_{cid}" for cid in card_ids]
                     + [f"cand_oh_{cid}" for cid in card_ids]
                     + [f"relic_{rid}" for rid in top_relics]
                     + [f"floor_eq_{f}" for f in FLOOR_BUCKETS]
                     + [f"deck_axis_{a}" for a in axis_vocab]
                     + [f"cand_axis_{a}" for a in axis_vocab])

    print(f"K_FLOORS = {args.k_floors}")
    X, y, run_ids, n_used, n_skip = load_training_rows(
        args.history, card_db, k_floors=args.k_floors,
        picked_only=args.picked_only, relics_only=args.relics_only,
        card_ids=card_ids, top_relics=top_relics,
        axis_vocab=axis_vocab, axis_by_id=axis_by_id)
    mode = ("picked-only" if args.picked_only else "all-options") + (
        " | relics-only" if args.relics_only else "")
    print(f"Loaded {len(X)} rows from {n_used} picks ({n_skip} SKIP dropped) [{mode}]")

    if len(X) < args.min_rows:
        print(f"Need {args.min_rows} rows, have {len(X)}. Skipping training.")
        return 1

    import numpy as np
    y_arr = np.array(y)
    s = np.sort(y_arr)

    def _pct(p):
        k = (len(s) - 1) * p
        f, c = int(k), min(int(k) + 1, len(s) - 1)
        return s[f] if f == c else s[f] * (c - k) + s[c] * (k - f)

    print(f"y stats: n={len(y_arr)} mean={y_arr.mean():.4f} sd={y_arr.std():.4f} "
          f"p10={_pct(0.10):.4f} median={_pct(0.5):.4f} p90={_pct(0.90):.4f}")
    print("  (reference from task spec: mean 0.1621, sd 0.1350, p10 0.0219, "
          "median 0.1375, p90 0.3207)")

    X = np.array(X)
    run_ids = np.array(run_ids)
    print(f"X shape: {X.shape}, expected (N, {len(feature_names)})")
    assert X.shape[1] == len(feature_names), f"feature-count mismatch: {X.shape[1]} vs {len(feature_names)}"

    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import GroupShuffleSplit
    from sklearn.metrics import mean_absolute_error

    def make_pipe():
        return Pipeline([("hgb", HistGradientBoostingRegressor(
            max_iter=500, max_depth=8, learning_rate=0.05,
            min_samples_leaf=30, random_state=0))])

    # Held-out MAE report: split by run_id (never split within a run — a
    # given run contributes rows at many floors, and rows from the same run
    # share correlated deck/relic/floor context, so a row-level split would
    # leak that context between train and test).
    gss = GroupShuffleSplit(n_splits=1, test_size=args.test_frac, random_state=0)
    train_idx, test_idx = next(gss.split(X, y_arr, groups=run_ids))
    n_train_runs = len(set(run_ids[train_idx]))
    n_test_runs = len(set(run_ids[test_idx]))
    print(f"Holdout split: {len(train_idx)} train rows / {n_train_runs} runs, "
          f"{len(test_idx)} test rows / {n_test_runs} runs")

    holdout_pipe = make_pipe()
    holdout_pipe.fit(X[train_idx], y_arr[train_idx])
    pred = holdout_pipe.predict(X[test_idx])
    mae = mean_absolute_error(y_arr[test_idx], pred)
    baseline_pred = np.full(len(test_idx), y_arr[train_idx].mean())
    baseline_mae = mean_absolute_error(y_arr[test_idx], baseline_pred)
    print(f"Holdout MAE: model={mae:.4f}  baseline(train-mean)={baseline_mae:.4f}  "
          f"improvement={100*(1 - mae/baseline_mae):.1f}%")
    if mae >= baseline_mae:
        print("!!! MODEL DOES NOT BEAT THE MEAN BASELINE ON HELD-OUT RUNS !!!")
        print("!!! Do not ship this checkpoint as-is.                    !!!")

    try:
        from sklearn.inspection import permutation_importance
        print("\nTop 15 by permutation importance (on holdout model/test rows):")
        idx = np.random.RandomState(0).choice(len(test_idx), min(5000, len(test_idx)), replace=False)
        perm = permutation_importance(holdout_pipe, X[test_idx][idx], y_arr[test_idx][idx],
                                      n_repeats=3, random_state=0, n_jobs=-1)
        ranked = sorted(zip(feature_names, perm.importances_mean), key=lambda x: -x[1])
        for name, imp in ranked[:15]:
            print(f"  {name:<30s} {imp:+.4f}")
    except Exception as e:
        print(f"(perm importance skipped: {e})")

    # Ship the model trained on ALL rows (the holdout split above is only for
    # the printed MAE report).
    final_pipe = make_pipe()
    final_pipe.fit(X, y_arr)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "wb") as f:
        pickle.dump({"pipeline": final_pipe, "feature_names": feature_names,
                     "card_ids": card_ids, "top_relics": top_relics,
                     "floor_buckets": FLOOR_BUCKETS, "axis_vocab": axis_vocab,
                     "n_train": len(X), "holdout_mae": float(mae),
                     "holdout_baseline_mae": float(baseline_mae),
                     "card_db_size": len(card_db),
                     "version": f"hp-axis-{len(feature_names)}",
                     "target": "hp_loss_per_floor", "K_FLOORS": args.k_floors}, f)
    print(f"\nSaved -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
