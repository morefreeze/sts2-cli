# Advisor Card Ratings → Deck-Building Baseline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use the STS2 Card Advisor tier list + archetype axes as the deck-building score baseline for all 5 characters, replacing the Ironclad-only heuristic baseline at inference time.

**Architecture:** An offline script extracts the advisor catalog into two committed JSON files. `card_scoring.score_card` uses the per-card tier as its base for any rated card (context bonuses layer on top; manual `OVERRIDES` become bounded deltas), and `_card_tags` falls back to advisor axes so the tag/lock-in machinery works for the 4 non-Ironclad characters. No retraining.

**Tech Stack:** Python 3.11 (`.venv/bin/python`), pytest, stdlib `urllib`/`json`/`re`. No new dependencies.

**Reference spec:** `docs/superpowers/specs/2026-06-26-advisor-card-ratings-design.md`

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent/build_advisor_ratings.py` (create) | Pure `parse_cards(html)` parser + CLI that fetches/reads the advisor HTML and writes the two data files. |
| `data/advisor_card_ratings.json` (create, committed) | `{ NORM_ID: {tier, axes, character, anchor_score} }` |
| `data/advisor_card_tags.json` (create, committed) | `{ NORM_ID: [axes...] }` |
| `agent/card_scoring.py` (modify) | Load ratings/tags; tier-baseline branch in `score_card`; advisor fallback in `_card_tags`. |
| `python/play_full_run.py` (modify) | Extract `summarize()`; add per-character `avg_floor` to SUMMARY. |
| `tests/test_advisor_ratings.py` (create) | Unit tests for the parser, the loaders, the tier baseline, the tag fallback, and `summarize()`. |

**Notes for the implementer:**
- A downloaded snapshot already exists at `/tmp/sts2-cli/advisor.html` (≈4.3 MB). Use it for the generation step to avoid a network round-trip; the script also supports fetching the live URL.
- `_card_id_norm` (agent/card_scoring.py:1061) upper-cases the id and strips a leading `CARD.`. All data-file keys MUST be in that normalized form so lookups match.
- `score_card_in_deck` (agent/card_scoring.py:1096) calls `score_card()` for its base, so it inherits the tier baseline automatically — do **not** patch it separately.
- Always invoke Python as `.venv/bin/python` (system `python3` lacks deps).

---

## Task 1: Advisor HTML parser (pure function)

**Files:**
- Create: `agent/build_advisor_ratings.py`
- Test: `tests/test_advisor_ratings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_advisor_ratings.py
from agent.build_advisor_ratings import parse_cards

SAMPLE_HTML = '''
<script>
const ANCHOR_BY_BUILD = {"build A": [
{"id": "CARD.PHANTOM_BLADES", "title": "x", "character": "SILENT", "type": "Power",
 "cost": 1, "rarity": "Uncommon", "tier": "B", "axes": ["SCALING", "SHIV"],
 "anchor_score": 9.7, "signals": {"S1_multiplier": 3.0}},
{"id": "CARD.AGGRESSION", "character": "IRONCLAD", "type": "Power", "cost": 1,
 "rarity": "Rare", "tier": "B", "axes": ["SCALING", "RANDOM"], "anchor_score": 8.0},
{"id": "CARD.PHANTOM_BLADES", "character": "SILENT", "tier": "B", "axes": ["SCALING", "SHIV"],
 "anchor_score": 9.7}
]};
</script>
'''


def test_parse_cards_extracts_and_dedupes():
    cards = parse_cards(SAMPLE_HTML)
    # normalized keys (CARD. stripped, upper-cased), deduped by id
    assert set(cards) == {"PHANTOM_BLADES", "AGGRESSION"}
    pb = cards["PHANTOM_BLADES"]
    assert pb["tier"] == "B"
    assert pb["axes"] == ["SCALING", "SHIV"]
    assert pb["character"] == "SILENT"
    assert pb["anchor_score"] == 9.7


def test_parse_cards_skips_blank_tier():
    html = '<script>x = {"id": "CARD.FOO", "tier": "", "axes": []};</script>'
    assert parse_cards(html) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.build_advisor_ratings'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/build_advisor_ratings.py
"""Extract the STS2 Card Advisor tier list + archetype axes into committed JSON.

Source: https://ing-gom.github.io/sts2-card-advisor/ (ing-gom/sts2-card-advisor).
The advisor embeds card objects of the form
  {"id":"CARD.X","character":"...","type":"...","cost":N,"rarity":"...",
   "tier":"S|A|B|C|D","axes":[...],"anchor_score":N.N, ...}
inside its inline JS. We brace-balance every such object, json.loads it, dedupe
by normalized id, and keep the fields deck-building cares about.

Run `.venv/bin/python agent/build_advisor_ratings.py --help` for usage.
"""
import argparse
import json
import os
import re
import urllib.request

ADVISOR_URL = "https://ing-gom.github.io/sts2-card-advisor/"
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_KEEP = ("tier", "axes", "character", "anchor_score")


def _norm_id(raw: str) -> str:
    cid = str(raw).upper().strip()
    return cid[5:] if cid.startswith("CARD.") else cid


def parse_cards(html: str) -> dict:
    """Return { NORM_ID: {tier, axes, character, anchor_score} } from advisor HTML.

    Cards with a blank/missing tier are skipped. First occurrence of each id wins.
    """
    out: dict = {}
    for m in re.finditer(r'\{"id":\s*"CARD\.', html):
        i = m.start()
        depth = 0
        j = i
        while j < len(html):
            ch = html[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        try:
            obj = json.loads(html[i:j + 1])
        except ValueError:
            continue
        tier = obj.get("tier")
        if not tier:
            continue
        cid = _norm_id(obj.get("id", ""))
        if not cid or cid in out:
            continue
        out[cid] = {k: obj.get(k) for k in _KEEP}
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add agent/build_advisor_ratings.py tests/test_advisor_ratings.py
git commit -m "feat(advisor): brace-balanced card-catalog parser + tests"
```

---

## Task 2: CLI + generate committed data files

**Files:**
- Modify: `agent/build_advisor_ratings.py` (append CLI)
- Create (generated, committed): `data/advisor_card_ratings.json`, `data/advisor_card_tags.json`

- [ ] **Step 1: Add the CLI to the bottom of `agent/build_advisor_ratings.py`**

```python
def build_tags(ratings: dict) -> dict:
    """Return { NORM_ID: [axes...] } for cards that have any axes."""
    return {cid: r["axes"] for cid, r in ratings.items() if r.get("axes")}


def main():
    ap = argparse.ArgumentParser(description="Extract advisor card ratings to data/.")
    ap.add_argument("--html", help="Path to a saved advisor HTML snapshot. "
                                    "If omitted, fetch the live URL.")
    ap.add_argument("--url", default=ADVISOR_URL, help="Advisor URL (used when --html absent).")
    args = ap.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8") as f:
            html = f.read()
    else:
        with urllib.request.urlopen(args.url) as resp:
            html = resp.read().decode("utf-8")

    ratings = parse_cards(html)
    tags = build_tags(ratings)

    ratings_path = os.path.join(_DATA_DIR, "advisor_card_ratings.json")
    tags_path = os.path.join(_DATA_DIR, "advisor_card_tags.json")
    with open(ratings_path, "w", encoding="utf-8") as f:
        json.dump(ratings, f, ensure_ascii=False, indent=0, sort_keys=True)
    with open(tags_path, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=0, sort_keys=True)

    by_char: dict = {}
    by_tier: dict = {}
    for r in ratings.values():
        by_char[r["character"]] = by_char.get(r["character"], 0) + 1
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    print(f"wrote {len(ratings)} cards -> {ratings_path}")
    print(f"wrote {len(tags)} tag entries -> {tags_path}")
    print("by character:", dict(sorted(by_char.items())))
    print("by tier:", dict(sorted(by_tier.items())))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate the data files**

Run: `.venv/bin/python agent/build_advisor_ratings.py --html /tmp/sts2-cli/advisor.html`
Expected output (approximately):
```
wrote 132 cards -> .../data/advisor_card_ratings.json
wrote 132 tag entries -> .../data/advisor_card_tags.json
by character: {'DEFECT': 22, 'IRONCLAD': 20, 'NECROBINDER': 34, 'REGENT': 24, 'SHARED': 7, 'SILENT': 25}
by tier: {'A': 38, 'B': 29, 'C': 21, 'D': 6, 'S': 37}
```
(If `/tmp/sts2-cli/advisor.html` is gone, run without `--html` to fetch live; counts may drift slightly with advisor updates.)

- [ ] **Step 3: Spot-check the join key against our card DB**

Run:
```bash
.venv/bin/python -c "
import json
r = json.load(open('data/advisor_card_ratings.json'))
m = json.load(open('data/card_metadata.json'))
both = set(r) & set(m)
print('advisor cards:', len(r), '| overlap with card_metadata:', len(both))
print('sample overlap:', sorted(both)[:6])
"
```
Expected: overlap ≥ 15 (Ironclad cards in both), sample shows ids like `AGGRESSION`, `BARRICADE`.

- [ ] **Step 4: Commit**

```bash
git add agent/build_advisor_ratings.py data/advisor_card_ratings.json data/advisor_card_tags.json
git commit -m "feat(advisor): CLI + generated rating/tag data (132 cards, 5 chars)"
```

---

## Task 3: Loaders + `_TIER_BASE` + `_context_bonus` helper

**Files:**
- Modify: `agent/card_scoring.py` (add loaders near the other data loaders, ~line 34; add helper)
- Test: `tests/test_advisor_ratings.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_advisor_ratings.py
from agent import card_scoring


def test_advisor_ratings_loader_returns_dict():
    ratings = card_scoring._load_advisor_ratings()
    assert isinstance(ratings, dict)
    # generated in Task 2; a known Ironclad card should be present
    assert "AGGRESSION" in ratings
    assert ratings["AGGRESSION"]["tier"] in {"S", "A", "B", "C", "D"}


def test_tier_base_monotonic():
    tb = card_scoring._TIER_BASE
    assert tb["S"] > tb["A"] > tb["B"] > tb["C"] > tb["D"]


def test_context_bonus_rewards_draw_and_energy():
    # a skill that draws 2 and gives 1 energy → positive context bonus
    card = {"id": "CARD.TEST", "type": "skill", "cost": 1,
            "stats": {"cards": 2, "energy": 1}, "description": ""}
    assert card_scoring._context_bonus(card) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -k "advisor_ratings_loader or tier_base or context_bonus" -v`
Expected: FAIL with `AttributeError: module 'agent.card_scoring' has no attribute '_load_advisor_ratings'`

- [ ] **Step 3: Implement the loaders + constant + helper**

Add after the existing `_CARD_TAGS` block (agent/card_scoring.py, after line 34):

```python
# Advisor tier ratings + axes (data/advisor_card_ratings.json, data/advisor_card_tags.json),
# generated by agent/build_advisor_ratings.py from the STS2 Card Advisor. Used as the
# scoring BASELINE for any rated card (all 5 characters); unrated cards fall back to the
# heuristic below. Loaded lazily so a missing file degrades gracefully.
_ADVISOR_RATINGS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                      "..", "data", "advisor_card_ratings.json")
_ADVISOR_TAGS_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "..", "data", "advisor_card_tags.json")
_ADVISOR_RATINGS: dict | None = None
_ADVISOR_TAGS: dict | None = None

# Tier -> base score (0-10). Single source of truth for tuning the tier curve.
_TIER_BASE = {"S": 9.5, "A": 8.0, "B": 6.0, "C": 4.0, "D": 2.0}
# OVERRIDES become bounded deltas vs the tier base for rated cards (protects a few
# hand-tuned Ironclad picks without letting them dominate the principled baseline).
_OVERRIDE_DELTA_CAP = 2.0


def _load_advisor_ratings() -> dict:
    """Lazy-load data/advisor_card_ratings.json. {} if missing."""
    global _ADVISOR_RATINGS
    if _ADVISOR_RATINGS is None:
        try:
            with open(_ADVISOR_RATINGS_PATH) as _f:
                _ADVISOR_RATINGS = _json.load(_f)
        except (FileNotFoundError, ValueError):
            _ADVISOR_RATINGS = {}
    return _ADVISOR_RATINGS


def _load_advisor_tags() -> dict:
    """Lazy-load data/advisor_card_tags.json. {} if missing."""
    global _ADVISOR_TAGS
    if _ADVISOR_TAGS is None:
        try:
            with open(_ADVISOR_TAGS_PATH) as _f:
                _ADVISOR_TAGS = _json.load(_f)
        except (FileNotFoundError, ValueError):
            _ADVISOR_TAGS = {}
    return _ADVISOR_TAGS
```

Then add the `_context_bonus` helper just above `def score_card` (agent/card_scoring.py:224). It is the subset of bonuses that stay on top of the tier baseline — contextual value the flat tier doesn't capture:

```python
def _context_bonus(card: dict) -> float:
    """Context-dependent score additions kept on top of the advisor tier baseline.

    Deliberately a small, self-contained re-derivation (not a refactor of the
    heuristic body) so the unrated-card path in score_card stays byte-for-byte
    unchanged and free of regression risk. Covers: low-cost combo, draw, energy,
    keyword nudges, HP-loss penalty, 'damage equal to', and X-cost scaling.
    """
    import re as _re_cb
    raw_cost = card.get("cost", 1)
    if not isinstance(raw_cost, (int, float)):
        raw_cost = 1
    ctype = (card.get("type") or "").lower()
    stats = card.get("stats") or {}
    desc = str(card.get("description", "")).lower()

    bonus = 0.0
    if raw_cost == 0:
        bonus += LOW_COST_BONUS
    elif raw_cost == 1:
        bonus += ONE_COST_BONUS

    draw = stats.get("cards", stats.get("draw", 0)) or 0
    if draw > 0:
        bonus += draw * DRAW_VALUE
    energy = stats.get("energy", 0)
    if energy > 0:
        bonus += energy * ENERGY_VALUE

    if "vulnerable" in desc:
        bonus += VULN_WEAK_VALUE
    if "weak" in desc:
        bonus += VULN_WEAK_VALUE
    if "strength" in desc and ctype != "power":
        bonus += STRENGTH_VALUE * 0.5
    if "exhaust" in desc:
        bonus += EXHAUST_VALUE
    if "all enem" in desc:
        bonus += AOE_VALUE
    if "draw" in desc and draw == 0:
        bonus += DRAW_VALUE

    hp_loss_m = _re_cb.search(r"lose\s+(\d+)\s+hp", desc)
    if hp_loss_m:
        bonus -= 0.3 * int(hp_loss_m.group(1))
    if "damage equal to" in desc:
        bonus += 5.0
    if " x " in desc or "x times" in desc:
        x_dmg_m = _re_cb.search(r"(\d+)\s+damage", desc)
        bonus += min(int(x_dmg_m.group(1)) * 2 / 3, 5.0) if x_dmg_m else 2.0
    return bonus
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -k "advisor_ratings_loader or tier_base or context_bonus" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/card_scoring.py tests/test_advisor_ratings.py
git commit -m "feat(advisor): rating/tag loaders, _TIER_BASE, _context_bonus helper"
```

---

## Task 4: Tier-baseline branch in `score_card`

**Files:**
- Modify: `agent/card_scoring.py` (insert branch inside `score_card`, after id normalization at line 239, before the `if card_id in OVERRIDES:` block at line 243)
- Test: `tests/test_advisor_ratings.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_advisor_ratings.py
def test_rated_card_uses_tier_base(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS",
                        {"ZED": {"tier": "D", "axes": [], "character": "SILENT"}})
    # A D-tier card with big raw damage must NOT score high — tier dominates the base.
    card = {"id": "CARD.ZED", "type": "attack", "cost": 1,
            "stats": {"damage": 40}, "description": ""}
    score = card_scoring.score_card(card)
    assert score <= card_scoring._TIER_BASE["D"] + 0.01  # base 2.0, no context bonus


def test_override_becomes_bounded_delta(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS",
                        {"ZED": {"tier": "C", "axes": [], "character": "IRONCLAD"}})
    monkeypatch.setitem(card_scoring.OVERRIDES, "ZED", 10.0)  # huge absolute override
    card = {"id": "CARD.ZED", "type": "skill", "cost": 1, "stats": {}, "description": ""}
    score = card_scoring.score_card(card)
    # base C=4.0; delta capped at +2.0 → 6.0, NOT the raw 10.0
    assert abs(score - (card_scoring._TIER_BASE["C"] + card_scoring._OVERRIDE_DELTA_CAP)) < 0.01


def test_unrated_card_unchanged(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS", {})  # nothing rated
    card = {"id": "CARD.UNRATED", "type": "attack", "cost": 1,
            "stats": {"damage": 6}, "description": ""}
    # heuristic path: Strike-like 6 dmg/1 cost → ~5.0 from the dpe term
    assert card_scoring.score_card(card) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -k "tier_base or bounded_delta or unrated" -v`
Expected: FAIL — `test_rated_card_uses_tier_base` returns the high heuristic damage score (~10) because the branch doesn't exist yet.

- [ ] **Step 3: Insert the tier-baseline branch**

In `score_card`, immediately after the id-normalization block (the `if card_id.startswith("CARD."): card_id = card_id[5:]` at line 239) and BEFORE the existing `# Check manual override first` / `if card_id in OVERRIDES:` block, insert:

```python
    # === Advisor tier baseline (all 5 characters) ===
    # For any card the advisor rates, the tier IS the base value — it subsumes the
    # raw cost/type/damage/block heuristic and rarity. Context bonuses still layer on
    # top, OVERRIDES apply as a bounded delta, empirical bonus as elsewhere.
    _adv = _load_advisor_ratings().get(card_id)
    if _adv and _adv.get("tier") in _TIER_BASE:
        score = _TIER_BASE[_adv["tier"]]
        score += _context_bonus(card)
        if card_id in OVERRIDES:
            _delta = OVERRIDES[card_id] - _TIER_BASE[_adv["tier"]]
            _delta = max(-_OVERRIDE_DELTA_CAP, min(_OVERRIDE_DELTA_CAP, _delta))
            score += _delta
        if EMPIRICAL_BONUS and card_id in EMPIRICAL_BONUS:
            score += EMPIRICAL_BONUS[card_id] * EMPIRICAL_WEIGHT
        return max(0.0, min(score, 10.0))
```

(The existing `if card_id in OVERRIDES:` early-return below now only fires for cards that are overridden but NOT advisor-rated. Leave it unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: PASS (all tests, including the Task 1/3 ones)

- [ ] **Step 5: Commit**

```bash
git add agent/card_scoring.py tests/test_advisor_ratings.py
git commit -m "feat(advisor): tier baseline + bounded override delta in score_card"
```

---

## Task 5: Advisor axes feed `_card_tags` for all characters

> **Scope note:** This extends the **tag-aware scoring** layer (everything that consumes `_card_tags` / `_deck_tag_counts`) to the 4 non-Ironclad characters. It does **not** touch the archetype **lock-in** detector (`compute_deck_archetype`), which keys off hardcoded ID sets (`STRENGTH_GAIN_CARDS`, `EXHAUST_PAYLOAD_CARDS`, `BLOCK_PAYLOAD_CARDS`) and stays Ironclad-only. Extending lock-in to other characters via advisor axes (e.g. SHIV/ORB/SKELETON keystones) is a deliberate follow-up, out of scope here.

**Files:**
- Modify: `agent/card_scoring.py` (`_card_tags`, line 1285)
- Test: `tests/test_advisor_ratings.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_advisor_ratings.py
def test_card_tags_fall_back_to_advisor(monkeypatch):
    # Ironclad hand-tuned map has NO entry for this Silent card...
    monkeypatch.setattr(card_scoring, "_CARD_TAGS", {})
    monkeypatch.setattr(card_scoring, "_ADVISOR_TAGS",
                        {"PHANTOM_BLADES": ["SCALING", "SHIV"]})
    tags = card_scoring._card_tags({"id": "CARD.PHANTOM_BLADES"})
    assert "SHIV" in tags and "SCALING" in tags


def test_card_tags_prefers_handtuned(monkeypatch):
    # When both exist, hand-tuned Ironclad tags win (union with advisor).
    monkeypatch.setattr(card_scoring, "_CARD_TAGS", {"AGGRESSION": ["SCALING_PILLAR"]})
    monkeypatch.setattr(card_scoring, "_ADVISOR_TAGS", {"AGGRESSION": ["RANDOM"]})
    tags = card_scoring._card_tags({"id": "CARD.AGGRESSION"})
    assert "SCALING_PILLAR" in tags  # hand-tuned preserved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -k "card_tags" -v`
Expected: FAIL — `test_card_tags_fall_back_to_advisor` returns `[]` (no advisor fallback yet).

- [ ] **Step 3: Update `_card_tags`**

Replace the body of `_card_tags` (agent/card_scoring.py:1285) with a version that unions hand-tuned tags with advisor axes and falls back to advisor when hand-tuned has nothing:

```python
def _card_tags(card: dict) -> list[str]:
    cid = _card_id_norm(card)
    adv = _load_advisor_tags()

    def _resolve(key: str) -> list[str]:
        hand = _CARD_TAGS.get(key)
        axes = adv.get(key, [])
        if hand is None:
            return list(axes)  # non-Ironclad / untuned → advisor axes only
        # both present → hand-tuned union advisor (hand-tuned first, no dupes)
        return hand + [a for a in axes if a not in hand]

    tags = _resolve(cid)
    if tags:
        return tags
    # Starter cards: runtime IDs carry "_IRONCLAD" / "_SILENT" etc. suffix but the
    # tag maps use bare "STRIKE" / "DEFEND". Strip the suffix and retry once.
    for suffix in ("_IRONCLAD", "_SILENT", "_DEFECT", "_REGENT", "_NECROBINDER"):
        if cid.endswith(suffix):
            return _resolve(cid[:-len(suffix)])
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add agent/card_scoring.py tests/test_advisor_ratings.py
git commit -m "feat(advisor): _card_tags unions advisor axes for all 5 characters"
```

---

## Task 6: Per-character `avg_floor` in `play_full_run.py`

**Files:**
- Modify: `python/play_full_run.py` (extract `summarize()` from `main`, line 323-333)
- Test: `tests/test_advisor_ratings.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_advisor_ratings.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run


def test_summarize_reports_avg_floor():
    results = [
        {"victory": False, "seed": "run_1", "floor": 12, "act": 1, "steps": 50},
        {"victory": True,  "seed": "run_2", "floor": 17, "act": 3, "steps": 90},
        {"victory": False, "seed": "run_3", "floor": "?", "act": 1, "steps": 40},
    ]
    out = play_full_run.summarize(results, num_runs=3, character="Silent")
    assert "Wins: 1/3" in out
    assert "Completed: 3/3" in out
    # avg over numeric floors (12, 17) → 14.5
    assert "avg_floor=14.5" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -k "summarize" -v`
Expected: FAIL with `AttributeError: module 'play_full_run' has no attribute 'summarize'`

- [ ] **Step 3: Extract and extend `summarize`**

In `python/play_full_run.py`, add this function above `main()`:

```python
def summarize(results, num_runs, character="Ironclad"):
    """Build the SUMMARY text block, including avg_floor over numeric floors."""
    lines = ["\n" + "=" * 60, f"SUMMARY ({character})", "=" * 60]
    wins = sum(1 for r in results if r and r.get("victory"))
    completed = sum(1 for r in results if r and not r.get("timeout"))
    floors = []
    for i, r in enumerate(results):
        if r:
            status = "WIN" if r.get("victory") else ("TIMEOUT" if r.get("timeout") else "LOSS")
            lines.append(f"  Run {i+1}: {status} | seed={r.get('seed')} steps={r.get('steps')} "
                         f"act={r.get('act')} floor={r.get('floor')}")
            f = r.get("floor")
            if isinstance(f, (int, float)):
                floors.append(f)
    avg_floor = round(sum(floors) / len(floors), 1) if floors else 0.0
    lines.append(f"\nWins: {wins}/{num_runs}, Completed: {completed}/{num_runs}, "
                 f"avg_floor={avg_floor}")
    return "\n".join(lines)
```

Then replace the inline SUMMARY block at the end of `main()` (the lines from `print("\n" + "=" * 60)` through `print(f"\nWins: ...")`) with:

```python
    print(summarize(results, num_runs, character))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add python/play_full_run.py tests/test_advisor_ratings.py
git commit -m "feat(eval): per-character avg_floor in play_full_run summary"
```

---

## Task 7: Validation — regression + fixed-seed A/B

**Files:** none (verification only). Record results in the PR / commit message.

- [ ] **Step 1: Full unit-test pass**

Run: `.venv/bin/python -m pytest tests/test_advisor_ratings.py -v`
Expected: all PASS.

- [ ] **Step 2: Crash regression — 5 games × 5 characters (HARD GATE)**

> **What this validates:** `play_full_run.py` uses a RANDOM agent and does NOT import
> `card_scoring`, so this is purely the CLAUDE.md build/crash gate (the game runs end-to-end
> for all 5 characters). It does **not** measure deck quality. The `avg_floor=` field added
> in Task 6 is a reporting nicety here, NOT the deck-building signal.

Run:
```bash
for char in Ironclad Silent Defect Regent Necrobinder; do
  STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" \
    .venv/bin/python python/play_full_run.py 5 "$char" 2>&1 | grep -E "Wins|Completed|avg_floor"
done
```
Expected: every character prints `Completed: 5/5` (0 crashes/stuck).

- [ ] **Step 3: Deck-quality A/B — `eval_rl`, fixed seeds, git-toggled (the real signal)**

> **Tooling correction (discovered during execution):** the deck-building scorer is only
> exercised by `eval_rl.py` (via `greedy_action` → `score_card_in_deck`/`score_card` for
> card-reward/shop/smith picks), NOT by `play_full_run`. The A/B therefore runs through
> `eval_rl` with a FIXED model checkpoint, toggling only the `card_scoring` code via git:
> `before` = the checkpoint commit `2323eef` (no advisor); `after` = HEAD (advisor baseline).
>
> **Non-Ironclad measurement gap:** `eval_rl`'s combat policy is Ironclad-trained. Running
> `eval_rl --character Silent` exercises Silent's advisor card-picks but plays its combat
> with the Ironclad policy, so its avg_floor is dominated by the policy mismatch, not deck
> quality. With current infra, **only Ironclad has a valid gameplay A/B.** For the 4 weak
> characters, use the deck-quality proxy in Step 3b (no clean gameplay measure exists until
> they have their own trained policies).

3a — Ironclad gameplay A/B (run when machine load is low; ~40 games):
```bash
MODEL=checkpoints_best/ppo_ironclad_13308k_RETRAIN_23pct.zip
GD="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"

# AFTER (current HEAD, advisor active):
STS2_GAME_DIR="$GD" .venv/bin/python agent/eval_rl.py "$MODEL" \
  --character Ironclad --n-games 20 --fixed-seeds 2>&1 | grep -iE "avg_floor|boss|reach|win"

# BEFORE (toggle card_scoring + advisor data back to the checkpoint commit, same seeds):
git stash   # park any working-tree edits
git checkout 2323eef -- agent/card_scoring.py
rm -f data/advisor_card_ratings.json data/advisor_card_tags.json   # loaders degrade to {} → heuristic
STS2_GAME_DIR="$GD" .venv/bin/python agent/eval_rl.py "$MODEL" \
  --character Ironclad --n-games 20 --fixed-seeds 2>&1 | grep -iE "avg_floor|boss|reach|win"
# restore:
git checkout HEAD -- agent/card_scoring.py data/advisor_card_ratings.json data/advisor_card_tags.json
git stash pop || true
```
**Per the fixed-seed lesson, always `--fixed-seeds` on BOTH runs; never compare across seed sets.**

3b — Non-Ironclad deck-quality proxy (cheap, no games): score the decks the scorer builds
before vs after against the advisor tiers (mean tier value + S/A-tier share), per character,
from `data/eval_decks.jsonl` if present, or from a synthetic card-reward harness. Report the
delta as the non-Ironclad signal, with the caveat that it is partly circular (measured
against the injected source).

- [ ] **Step 4: Decision gate**

- Ironclad `avg_floor` / boss-reach AFTER must be **≥ BEFORE − noise** (no material
  regression). If Ironclad regresses, the first lever is the `_card_tags` union (switch to
  gap-fill-only so advisor axes don't perturb Ironclad's tuned tags), then `_TIER_BASE` /
  `_OVERRIDE_DELTA_CAP`. Re-run Step 3a.
- Non-Ironclad proxy (3b) should show a higher mean deck tier; none should regress.
- If the gate passes, the feature is ready to merge. If not, iterate on the knobs and re-run.

- [ ] **Step 5: Finalize**

Use the `superpowers:finishing-a-development-branch` skill to choose merge / PR / cleanup, recording the before/after avg_floor table in the PR body.

---

## Self-Review notes (for the implementer)

- **Spec coverage:** data pipeline (Tasks 1-2), tier baseline everywhere (Task 4), OVERRIDES→delta (Task 4), tag extension to 5 chars (Task 5), per-character avg_floor eval (Task 6), crash gate + Ironclad fixed-seed A/B (Task 7) — all present.
- **Unrated cards** are untouched (Task 4 branch returns early only for rated cards; the heuristic body is unchanged) — this is the regression-safety property.
- **`score_card_in_deck`** inherits the baseline via its `score_card()` call — no separate change.
- **Knobs:** `_TIER_BASE` and `_OVERRIDE_DELTA_CAP` are the only tunables; both are module constants.
