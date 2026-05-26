#!/usr/bin/env python3
"""extract_card_db.py — Build a card_id → metadata lookup from deck_history.jsonl.

Scans `card_pick` event rows (written by combat_env when the agent makes a
card_reward decision; each option carries its own cost/rarity/type/upgraded
from the live game state) and aggregates per card_id. The resulting
`data/card_metadata.json` lets the v2 predictor enrich `deck_before_ids`
with card-level features (cost histogram, type histogram, rarity counts)
without needing a separate game-data dump.

For each card_id we take the mode of (cost, rarity, type) observed across
all option rows. `upgrade_seen` records whether upgraded=True/False variants
have been observed (rare cards under-sample so this just signals coverage).

Usage:
    .venv/bin/python agent/extract_card_db.py
    # or:
    .venv/bin/python agent/extract_card_db.py --min-occurrences 5
"""
import argparse, json, os, sys
from collections import defaultdict, Counter


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="data/deck_history.jsonl")
    p.add_argument("--out", default="data/card_metadata.json")
    p.add_argument("--min-occurrences", type=int, default=2,
                   help="Skip cards with fewer option-occurrences than this (default 2)")
    args = p.parse_args()

    if not os.path.exists(args.history):
        print(f"No history at {args.history}")
        return 1

    by_id = defaultdict(lambda: {
        "cost_counter": Counter(),
        "rarity_counter": Counter(),
        "type_counter": Counter(),
        "upgraded_seen": Counter(),
        "occurrences": 0,
    })

    n_picks = 0
    with open(args.history) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("event") != "card_pick":
                continue
            n_picks += 1
            for opt in rec.get("options", []) or []:
                cid = opt.get("id")
                if not cid:
                    continue
                e = by_id[cid]
                e["occurrences"] += 1
                if opt.get("cost") is not None:
                    e["cost_counter"][opt["cost"]] += 1
                if opt.get("rarity"):
                    e["rarity_counter"][opt["rarity"]] += 1
                if opt.get("type"):
                    e["type_counter"][opt["type"]] += 1
                e["upgraded_seen"][bool(opt.get("upgraded", False))] += 1

    db = {}
    for cid, e in by_id.items():
        if e["occurrences"] < args.min_occurrences:
            continue
        db[cid] = {
            "cost": e["cost_counter"].most_common(1)[0][0] if e["cost_counter"] else None,
            "rarity": e["rarity_counter"].most_common(1)[0][0] if e["rarity_counter"] else None,
            "type": e["type_counter"].most_common(1)[0][0] if e["type_counter"] else None,
            "upgrade_seen": {str(k): v for k, v in e["upgraded_seen"].items()},
            "occurrences": e["occurrences"],
        }

    print(f"Scanned {n_picks} card_pick events from {args.history}")
    print(f"Unique cards (≥ {args.min_occurrences} occurrences): {len(db)}")
    if db:
        top = sorted(db.items(), key=lambda x: -x[1]["occurrences"])[:10]
        print(f"\nTop 10 most-seen cards:")
        for cid, meta in top:
            cost_s = str(meta["cost"]) if meta["cost"] is not None else "?"
            rar_s = str(meta["rarity"] or "?")
            type_s = str(meta["type"] or "?")
            print(f"  {cid:<35s} cost={cost_s:<3s} {rar_s:<10s} {type_s:<8s} n={meta['occurrences']}")
        # Quick stats
        rarities = Counter(m["rarity"] for m in db.values())
        types = Counter(m["type"] for m in db.values())
        print(f"\nRarity distribution: {dict(rarities)}")
        print(f"Type distribution:   {dict(types)}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(db, f, indent=2, sort_keys=True)
    print(f"\nWrote {args.out} ({os.path.getsize(args.out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
