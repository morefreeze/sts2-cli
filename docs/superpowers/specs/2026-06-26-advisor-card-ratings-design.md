# Design: Apply STS2 Card Advisor ratings to deck-building

**Date:** 2026-06-26
**Status:** Approved for planning
**Source data:** https://ing-gom.github.io/sts2-card-advisor/ (`ing-gom/sts2-card-advisor`)

## Problem

Our deck-building scorer (`agent/card_scoring.py::score_card`) is **Ironclad-centric**:
the `OVERRIDES` dict, the archetype tags (`data/ironclad_card_tags.json`), and the wiki
stats (`data/ironclad_cards.json`) all only cover Ironclad. The other four characters
(Silent, Defect, Regent, Necrobinder) get only the *generic* heuristic
(cost/type/damage/block/keyword parsing) with no tier priors and no archetype tags.

The advisor site publishes an expert/heuristic **tier list + archetype tagging** covering
**all 5 characters** with consistent fields. This is genuinely new information for 4 of 5
characters and a more principled baseline for the 5th.

## Scope & non-goals

- **In scope:** inference-time deck building only (card-reward / shop / smith picks via
  `card_scoring.py`). No retraining, no observation changes, no reward shaping.
- **Non-goal:** breaking the boss-reach ceiling. Our history shows inference-only deck
  scoring plateaus at ~13–17% boss-reach because the floor-15 wall is **HP attrition, not
  deck quality**. The realistic win here is **better deck building on the 4 weak
  characters**, plus a cleaner, uniform scoring foundation — not a higher Ironclad win rate.

## Source data (verified extractable)

The advisor HTML embeds card objects (inside `ANCHOR_BY_BUILD`) of the form:

```json
{"id":"CARD.PHANTOM_BLADES","character":"SILENT","type":"Power","cost":1,
 "rarity":"Uncommon","tier":"B","axes":["SCALING","SHIV","SHIV_AMPLIFIER"],
 "anchor_score":9.7}
```

Verified by extraction (brace-balanced JSON parse, dedupe by id):
- **132 unique rated cards**: REGENT 24, NECROBINDER 34, IRONCLAD 20, SILENT 25, DEFECT 22, SHARED 7.
- Tiers: S/A/B/C/D (38 A, 37 S, 29 B, 21 C, 6 D).
- **115 distinct `axes`** tags (SCALING, EXHAUST_TAG, ORB_PRODUCER, LIGHTNING_ORB, SHIV, BLOCK, AOE, …) — covers every character's archetypes.
- **ID join works**: advisor id with `CARD.` stripped (`AGGRESSION`, `BARRICADE`) matches
  our `data/card_metadata.json` keys exactly. `card_scoring.py` already normalizes ids by
  stripping the `CARD.` prefix.
- The site has an English layer (`name_loc.en` / `desc_loc.en`); ids are
  language-independent so localization is irrelevant to the join.

Cards **not** present in the advisor data (filler / unrated) cleanly fall back to the
existing heuristic — no gap to handle specially.

## Design decisions (locked)

1. **Integration point:** inference deck-building (lowest risk, no retrain).
2. **Blend policy:** *advisor tier = baseline everywhere*. Tier replaces the raw
   cost/type/damage heuristic as the per-card base for any rated card; context bonuses layer
   on top; manual `OVERRIDES` demoted to small ± deltas. (User accepted the Ironclad
   regression risk in exchange for a uniform, principled system.)
3. **Eval signal:** add a per-character `avg_floor` metric (fixed seeds, before/after) so we
   can measure the non-Ironclad upside, not just gate on Ironclad non-regression.

## Components

### 1. `agent/build_advisor_ratings.py` (new, offline)

- Input: advisor HTML — either fetched from the URL or a saved snapshot path (arg).
- Parse: scan for `{"id":"CARD....` and brace-balance to the matching `}`,
  `json.loads`, dedupe by id (keep first), retain `{tier, axes, character, anchor_score}`.
- Output: **`data/advisor_card_ratings.json`** = `{ NORM_ID: {tier, axes, character, anchor_score} }`
  where `NORM_ID` has the `CARD.` prefix stripped and is upper-cased to match
  `card_scoring._card_id_norm`.
- Committed to the repo so training/eval never touch the network. Re-run manually on advisor
  updates. Print a coverage summary (counts per character/tier) on run.

### 2. `agent/card_scoring.py` changes

- **Lazy loader** `_load_advisor_ratings()` mirroring the existing `_load_card_db` /
  tag-loader pattern (module-level cache, returns `{}` if file missing → fully backwards
  compatible).
- **New knob** `_TIER_BASE = {"S":9.5,"A":8.0,"B":6.0,"C":4.0,"D":2.0}` (single source of
  truth for tuning).
- **`score_card()` control flow:**
  1. Keep the early returns (BROKEN_CARDS, SKIP_IDS) unchanged.
  2. If `card_id` in advisor ratings:
     - `score = _TIER_BASE[tier]`.
     - **Skip** the raw cost/type/damage/block scoring block (tier subsumes it).
     - **Keep** the context-dependent additions: low-cost combo bonus, draw/energy,
       X-cost, "damage equal to", HP-loss penalty, keyword nudges that represent
       *contextual* value, and (downstream, where deck context exists) pairwise synergy and
       tag/lock-in bonuses.
     - Apply `OVERRIDES[card_id]` as a **bounded delta** (e.g. clamp to ±2.0) on top, not as
       the base — protects clutch-relevant Ironclad picks from a tier-driven regression.
     - Apply existing `EMPIRICAL_BONUS` as today.
     - Clamp 0–10.
  3. Else (not rated): existing heuristic path, unchanged.
- **Re-tune `OVERRIDES`:** since they become deltas, their values must be reinterpreted as
  *relative* nudges, not absolute 0–10 scores. Convert the existing absolute overrides to
  deltas vs the card's tier base during implementation (e.g. an override of 9.0 on an A-tier
  card = +1.0 delta). Document the conversion in the file.

### 3. Tag-system extension

- `build_advisor_ratings.py` (or a sibling step) also emits **`data/advisor_card_tags.json`**
  = `{ NORM_ID: [axes...] }`.
- `card_scoring._card_tags()` merges advisor tags with `ironclad_card_tags.json` (advisor
  fills the 4 non-Ironclad characters; Ironclad keeps its hand-tuned tags, union with
  advisor axes). This is what lets the existing archetype / lock-in / synergy machinery
  function for all 5 characters.
- If the raw 115-axis vocabulary is too granular for the existing tag consumers, add a small
  `AXIS_ALIAS` map translating advisor axes → our existing tag names where they correspond
  (e.g. `BLOCK`→`BLOCK_SCALING` family). Start with a pass-through and only alias where a
  consumer needs it.

### 4. Evaluation

- **Hard gate (crash regression):** CLAUDE.md regression — 5 games × 5 characters via
  `play_full_run.py`, all `Completed: 5/5`, 0 crashes/stuck.
- **Ironclad non-regression:** `eval_rl` boss-reach / avg_floor, **fixed seeds**, before vs
  after (per the fixed-seed A/B lesson — random seeds are seed-confounded).
- **Non-Ironclad upside:** extend `play_full_run.py` SUMMARY to report **avg_floor per
  character** (the per-run `floor` is already in each result dict; seeds are already
  deterministic `run_{i+1}`). Run a larger fixed-seed batch (e.g. 20–30/char) before vs
  after for Silent/Defect/Regent/Necrobinder.

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Tier-baseline regresses tuned Ironclad picks (incl. clutch) | OVERRIDES kept as bounded deltas; Ironclad fixed-seed A/B gate before merge |
| Advisor tier is context-free | Synergy / tag / lock-in bonuses still supply contextual value on top |
| Boss-reach ceiling unmoved | Explicit non-goal; success = deck quality + non-Ironclad floor, not Ironclad win-rate |
| 115-axis vocabulary too granular for existing consumers | Pass-through first; add `AXIS_ALIAS` only where a consumer needs it |
| Advisor data drifts from game patches | Snapshot committed; `build_advisor_ratings.py` re-runnable; coverage summary printed |

## Success criteria

1. 5×5 crash regression stays green (hard gate).
2. Ironclad fixed-seed avg_floor / boss-reach **not worse** than baseline (within noise).
3. At least one non-Ironclad character shows a fixed-seed avg_floor improvement; none
   regress materially.
4. `data/advisor_card_ratings.json` + `data/advisor_card_tags.json` committed and
   reproducible from `build_advisor_ratings.py`.
