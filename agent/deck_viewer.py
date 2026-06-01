#!/usr/bin/env python3
"""deck_viewer.py — Static HTML visualiser for sampled deck builds.

Pulls 100 distinct runs from data/deck_history.jsonl, picks one milestone
per run, joins it with the run's outcome (won / max_floor), computes both
the v1 deck_quality_score and a v2 per-deck mean (each card scored as if
it were the candidate among the rest of the deck), and renders a single
self-contained HTML file with one panel per deck.

Usage:
    .venv/bin/python agent/deck_viewer.py --out decks.html
"""
import argparse
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.card_scoring as _cs
from agent.card_scoring import (
    deck_quality_score,
    _load_predictor,
    _deck_features_v2,
    score_deck_dimensions,
    compute_deck_archetype,
)
# Trigger predictor load so the module-level _PREDICTOR_VERSION is populated.
_load_predictor()


def load_card_db() -> dict[str, dict]:
    """Map game-id (e.g. STRIKE_IRONCLAD) → wiki record (name, type, rarity)."""
    with open("data/ironclad_cards.json") as f:
        cards = json.load(f)["cards"]
    by_slug = {c["id"]: c for c in cards}

    # Build aliases for the runtime ID forms used in deck_history.
    db: dict[str, dict] = {}
    for c in cards:
        slug = c["id"]
        # canonical wiki slug (lowercase, dash-separated)
        db[slug] = c
        # uppercase snake_case used at runtime (strike-ironclad → STRIKE_IRONCLAD)
        db[slug.upper().replace("-", "_")] = c
        # without the _IRONCLAD class suffix
        bare = slug.replace("-ironclad", "")
        db[bare] = c
        db[bare.upper().replace("-", "_")] = c
    # Cover Strike/Defend without character suffix
    for canon in ("strike", "defend"):
        for variant in (f"{canon}-ironclad", canon):
            rec = by_slug.get(variant)
            if rec:
                db[canon.upper()] = rec
                db[f"{canon.upper()}_IRONCLAD"] = rec
    return db


def lookup_card(card_id: str, card_db: dict) -> dict:
    """Build a card dict the scoring pipeline expects."""
    upgraded = card_id.endswith("+")
    base_id = card_id.rstrip("+")
    rec = card_db.get(base_id)
    if not rec:
        # Strip _IRONCLAD suffix and retry
        for sfx in ("_IRONCLAD", "_SILENT", "_DEFECT", "_REGENT", "_NECROBINDER"):
            if base_id.endswith(sfx):
                rec = card_db.get(base_id[: -len(sfx)])
                if rec:
                    break
    if not rec:
        return {
            "card_id": card_id,
            "en_name": base_id.replace("_", " ").title(),
            "zh_name": base_id,
            "type": "?", "rarity": "?", "cost": "?",
            "upgraded": upgraded,
        }
    return {
        "card_id": card_id,
        "en_name": rec.get("en_name", base_id),
        "zh_name": rec.get("zh_name", base_id),
        "type": rec.get("type", "?"),
        "rarity": rec.get("rarity", "?"),
        "cost": rec.get("cost", "?"),
        "upgraded": upgraded,
    }


def sample_milestones(jsonl_path: str, n: int = 100, seed: int = 42) -> list[dict]:
    """One milestone per run, n distinct runs, ranked by floor then random."""
    by_run: dict[str, list[dict]] = defaultdict(list)
    outcomes: dict[str, dict] = {}
    with open(jsonl_path) as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = o.get("event")
            rid = o.get("run_id")
            if not rid:
                continue
            if ev == "milestone":
                by_run[rid].append(o)
            elif ev == "outcome":
                outcomes[rid] = o
    rng = random.Random(seed)
    runs = [rid for rid in by_run if rid in outcomes]
    rng.shuffle(runs)
    chosen = []
    for rid in runs[: n * 3]:  # over-sample then prune duplicates
        ms_list = by_run[rid]
        # prefer mid-late milestones to show real builds (not floor 1)
        ms_list = sorted(ms_list, key=lambda m: m.get("floor_crossed", 0))
        if not ms_list:
            continue
        pick = ms_list[-1] if len(ms_list) <= 2 else ms_list[len(ms_list) // 2 + 1]
        pick["_outcome"] = outcomes[rid]
        chosen.append(pick)
        if len(chosen) >= n:
            break
    return chosen


def archetype_label(arch: dict) -> str:
    """Human label for archetype dict."""
    if not arch:
        return "—"
    items = [(k, v) for k, v in arch.items() if k != "n" and v >= 2]
    items.sort(key=lambda x: -x[1])
    if not items:
        return "balanced"
    return " / ".join(f"{k}×{v}" for k, v in items[:2])


def deck_v2_predicted_floor(deck_full: list[dict], floor_at: int) -> float | None:
    """Predict run.max_floor for this deck. v2's deck-level features depend on
    floor_crossed too (it carries a strong signal), so we pass the floor at
    which the snapshot was taken."""
    pipe = _load_predictor()
    if pipe is None or _cs._PREDICTOR_VERSION != "v2" or len(deck_full) < 2:
        return None
    # Use each deck card as the "candidate" feature slot; the deck-level part of
    # the features is invariant, so the mean prediction is the model's expected
    # max_floor when reasoning about this composition.
    try:
        import numpy as _np
        feats = []
        for i, c in enumerate(deck_full):
            rest = deck_full[:i] + deck_full[i + 1:]
            feats.append(_deck_features_v2(rest, c, floor=floor_at))
        preds = pipe.predict(_np.array(feats))
        return float(preds.mean())
    except Exception as e:
        return None


def build_html(decks: list[dict], card_db: dict) -> str:
    """Render the deck panels."""
    rows = []
    for d in decks:
        cards_raw = d.get("cards", [])
        deck_full = [lookup_card(c, card_db) for c in cards_raw]
        v1_q = d.get("deck_quality", 0.0)
        v2_mean = deck_v2_predicted_floor(deck_full,
                                          floor_at=d.get("floor_crossed", 10))
        dims = d.get("dims", {})
        arch = d.get("archetype", {})
        outcome = d.get("_outcome", {})
        won = outcome.get("won", False)
        max_floor = outcome.get("max_floor", "?")
        row = {
            "run_id": d.get("run_id", "?"),
            "floor_at": d.get("floor_crossed", "?"),
            "deck_size": d.get("deck_size", len(deck_full)),
            "v1": v1_q,
            "v2": v2_mean,
            "won": won,
            "max_floor": max_floor,
            "dims": dims,
            "archetype_label": archetype_label(arch),
            "cards": deck_full,
        }
        rows.append(row)
    # Sort by max_floor desc, then deck_size
    rows.sort(key=lambda r: (-(r["max_floor"] if isinstance(r["max_floor"], int) else 0),
                              -r["deck_size"]))

    return _render(rows)


_CARD_TYPE_COLOR = {
    "Attack": "#d97057", "Skill": "#5b9aa6", "Power": "#b48b5d",
    "?": "#888",
}
_RARITY_COLOR = {
    "Basic": "#9e9e9e",
    "Common": "#bdbdbd",
    "Uncommon": "#6fa3d6",
    "Rare": "#d2a23a",
    "?": "#777",
}


def _render(rows: list[dict]) -> str:
    n = len(rows)
    won_count = sum(1 for r in rows if r["won"])
    avg_size = sum(r["deck_size"] for r in rows) / max(n, 1)
    avg_floor = sum(r["max_floor"] for r in rows
                    if isinstance(r["max_floor"], int)) / max(n, 1)

    panels = []
    for r in rows:
        won_badge = (
            '<span class="badge won">Won</span>' if r["won"]
            else '<span class="badge died">Died</span>'
        )
        v2_str = f"{r['v2']:.1f}" if r["v2"] is not None else "n/a"
        # Prediction error: predicted - actual max_floor (signed, smaller is better)
        if r["v2"] is not None and isinstance(r["max_floor"], int):
            err = r["v2"] - r["max_floor"]
            err_str = f"{err:+.1f}"
        else:
            err_str = "—"
        v1_str = f"{r['v1']:.2f}"

        # Card chips
        chips = []
        for c in r["cards"]:
            tcol = _CARD_TYPE_COLOR.get(c["type"], "#888")
            rcol = _RARITY_COLOR.get(c["rarity"], "#888")
            upg = "+" if c["upgraded"] else ""
            cost = c["cost"] if c["cost"] not in ("?", "X") else "·"
            chips.append(
                f'<span class="chip" style="border-left:3px solid {tcol};'
                f' background:linear-gradient(90deg,{rcol}22,#1a1a1a 60%);">'
                f'<span class="cost">{cost}</span>'
                f'<span class="cname">{c["zh_name"]}{upg}</span>'
                f'</span>'
            )

        # Dim bars
        dim_html = []
        for k in ("attack", "defense", "energy", "draw"):
            v = r["dims"].get(k, 0)
            pct = min(100, max(0, v * 100))
            dim_html.append(
                f'<div class="dim-row"><span class="dim-name">{k}</span>'
                f'<div class="dim-bar"><div class="dim-fill" '
                f'style="width:{pct:.0f}%;"></div></div>'
                f'<span class="dim-val">{v:.2f}</span></div>'
            )

        panels.append(f"""
        <div class="deck-panel">
          <div class="panel-head">
            <div class="floor-tag">F{r['max_floor']}</div>
            <div class="meta">
              <div class="title">{r['archetype_label']}</div>
              <div class="sub">@F{r['floor_at']} · size {r['deck_size']} · {r['run_id'][:14]}</div>
            </div>
            <div class="scores">
              {won_badge}
              <div class="score-row"><span class="score-label">v1 quality</span><span class="score-val">{v1_str}</span></div>
              <div class="score-row"><span class="score-label">v2 pred fl</span><span class="score-val v2val">{v2_str}</span></div>
              <div class="score-row"><span class="score-label">Δ pred-actual</span><span class="score-val">{err_str}</span></div>
            </div>
          </div>
          <div class="panel-body">
            <div class="cards">{"".join(chips)}</div>
            <div class="dims">{"".join(dim_html)}</div>
          </div>
        </div>
        """)

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>STS2 — Deck Builds Viewer</title>
<style>
  body {{
    background:#0b0d12; color:#e5e7eb; margin:0;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    line-height:1.4;
  }}
  header {{
    padding:24px 32px; border-bottom:1px solid #222;
    background:linear-gradient(180deg,#171a22,#0b0d12);
    position:sticky; top:0; z-index:5;
  }}
  header h1 {{ margin:0 0 4px; font-size:18px; font-weight:600; letter-spacing:.04em; }}
  header .stats {{ font-size:13px; color:#9ca3af; }}
  header .stats span {{ margin-right:18px; }}
  .legend {{ display:inline-flex; gap:10px; margin-left:24px; font-size:12px; }}
  .legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle;}}
  .grid {{
    display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr));
    gap:14px; padding:18px;
  }}
  .deck-panel {{
    background:#13161e; border:1px solid #222; border-radius:8px; padding:12px;
    display:flex; flex-direction:column; min-width:0;
  }}
  .panel-head {{
    display:flex; align-items:center; gap:12px; padding-bottom:10px;
    border-bottom:1px dashed #2a2d36;
  }}
  .floor-tag {{
    flex:0 0 auto; width:46px; height:46px; border-radius:8px;
    background:#1f2532; color:#e5e7eb; font-weight:600; font-size:14px;
    display:flex; align-items:center; justify-content:center;
  }}
  .meta {{ flex:1; min-width:0; }}
  .meta .title {{ font-weight:600; font-size:14px; color:#f3f4f6; }}
  .meta .sub {{ font-size:11px; color:#7b8290; margin-top:2px; }}
  .scores {{ flex:0 0 auto; text-align:right; font-size:12px; }}
  .score-row {{ display:flex; gap:6px; justify-content:flex-end; }}
  .score-label {{ color:#7b8290; }}
  .score-val {{ font-family: ui-monospace, "SF Mono", monospace; color:#e5e7eb; }}
  .v2val {{ color:#fcd34d; }}
  .badge {{
    display:inline-block; padding:1px 8px; border-radius:10px;
    font-size:10px; font-weight:600; letter-spacing:.06em;
    margin-bottom:4px;
  }}
  .badge.won {{ background:#15402a; color:#86efac; }}
  .badge.died {{ background:#3a1a1a; color:#fca5a5; }}
  .panel-body {{ display:flex; gap:14px; padding-top:10px; min-width:0; }}
  .cards {{
    flex:1; display:flex; flex-wrap:wrap; gap:4px; align-content:flex-start;
    min-width:0;
  }}
  .chip {{
    display:inline-flex; align-items:center; gap:5px;
    padding:2px 8px 2px 6px;
    border-radius:3px;
    font-size:11px;
    background:#1a1a1a;
    border:1px solid #25282f;
    color:#e5e7eb;
    max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }}
  .chip .cost {{
    width:14px; height:14px; border-radius:50%;
    background:#3b3f4a; color:#fff;
    display:inline-flex; align-items:center; justify-content:center;
    font-size:10px; flex:0 0 auto;
  }}
  .dims {{
    flex:0 0 140px; font-size:10px; color:#9ca3af;
  }}
  .dim-row {{ display:flex; align-items:center; gap:5px; margin-bottom:3px; }}
  .dim-name {{ width:50px; }}
  .dim-bar {{ flex:1; height:7px; background:#22262f; border-radius:2px; overflow:hidden;}}
  .dim-fill {{ height:100%; background:linear-gradient(90deg,#6fa3d6,#d2a23a); }}
  .dim-val {{ width:30px; text-align:right; font-family:ui-monospace,monospace;}}
</style>
</head>
<body>
  <header>
    <h1>STS2 · Deck Build Viewer</h1>
    <div class="stats">
      <span>{n} decks sampled</span>
      <span>Won: {won_count} ({100*won_count/max(n,1):.0f}%)</span>
      <span>avg deck size: {avg_size:.1f}</span>
      <span>avg final floor: {avg_floor:.1f}</span>
      <span class="legend">
        <span><i style="background:#d97057"></i>Attack</span>
        <span><i style="background:#5b9aa6"></i>Skill</span>
        <span><i style="background:#b48b5d"></i>Power</span>
        <span><i style="background:#d2a23a"></i>Rare</span>
        <span><i style="background:#6fa3d6"></i>Uncommon</span>
        <span><i style="background:#bdbdbd"></i>Common</span>
      </span>
    </div>
  </header>
  <div class="grid">
    {''.join(panels)}
  </div>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="decks.html",
                   help="output HTML path")
    p.add_argument("--n", type=int, default=100,
                   help="how many distinct runs to sample")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--input", default="data/deck_history.jsonl")
    args = p.parse_args()

    card_db = load_card_db()
    print(f"Card DB: {len({c['id'] for c in [v for v in card_db.values()]})} unique cards", file=sys.stderr)
    decks = sample_milestones(args.input, n=args.n, seed=args.seed)
    print(f"Sampled {len(decks)} decks from {args.input}", file=sys.stderr)
    html = build_html(decks, card_db)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
