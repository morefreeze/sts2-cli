#!/usr/bin/env python3
"""boss_deck_viewer.py — HTML view of the best recent boss-entry decks.

Reads data/eval_decks.jsonl (written automatically by every eval_rl run):
  - deck records: boss-entry snapshot {seed, boss, hp_at_entry, deck_quality,
    cards:[{id,name,score}], relics, checkpoint, timestamp}
  - result records: {event:"result", seed, max_floor, run_won, boss_beaten}

Joins them by seed, takes the most recent --window records, ranks by
deck_quality, renders the top --n as a self-contained HTML page with
per-card score chips (hover = description) and combat outcome badges.

Usage:
    .venv/bin/python -m agent.boss_deck_viewer --out boss_decks.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.deck_viewer import load_card_db, lookup_card  # reuse card DB + bilingual lookup


def load_records(path: str):
    decks, results = [], {}
    if not os.path.exists(path):
        return decks, results
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "result":
                results[(r.get("seed"), r.get("game_index"))] = r
                results.setdefault((r.get("seed"), None), r)
            elif r.get("cards"):
                decks.append(r)
    return decks, results


def render(decks: list[dict], results: dict, card_db: dict,
           n: int, window: int) -> str:
    # newest first, window, then rank by deck_quality
    decks = sorted(decks, key=lambda d: d.get("timestamp") or "", reverse=True)
    recent = decks[:window]
    ranked = sorted(recent, key=lambda d: d.get("deck_quality") or 0,
                    reverse=True)[:n]

    panels = []
    for rank, d in enumerate(ranked, 1):
        seed = d.get("seed")
        res = (results.get((seed, d.get("game_index")))
               or results.get((seed, None)) or {})
        boss = d.get("boss") or res.get("boss") or "?"
        beaten = res.get("boss_beaten")
        if beaten is True:
            badge = '<span class="badge won">BOSS BEATEN</span>'
        elif beaten is False:
            badge = '<span class="badge died">DIED AT BOSS</span>'
        else:
            badge = '<span class="badge unk">RESULT UNKNOWN</span>'
        hp = d.get("hp_at_entry")
        mhp = d.get("max_hp")
        hp_str = f"{hp}/{mhp}" if hp is not None else "?"
        relics = [r for r in (d.get("relics") or []) if r]
        chips = []
        for c in sorted(d.get("cards") or [], key=lambda x: -(x.get("score") or 0)):
            cid = c.get("id") or "?"
            full = lookup_card(str(cid), card_db)
            desc = html.escape((full.get("description") or "")[:300])
            zh = full.get("zh_name") or ""
            sc = c.get("score")
            sc_str = f"{sc:.1f}" if isinstance(sc, (int, float)) else "?"
            hue = max(0, min(120, (sc or 0) * 12))  # 0=red → 10=green
            chips.append(
                f'<span class="chip" title="{html.escape(str(cid))} / {html.escape(zh)} · {desc}"'
                f' style="border-left:4px solid hsl({hue},70%,45%)">'
                f'{html.escape(str(c.get("name") or cid))}'
                f'<b>{sc_str}</b></span>')
        floor_str = res.get("max_floor", "?")
        panels.append(f"""
  <div class="panel">
    <div class="head">
      <span class="rank">#{rank}</span>
      <span class="quality">deck {d.get("deck_quality", "?")}</span>
      {badge}
      <span class="meta">boss <b>{html.escape(str(boss))}</b> · HP {hp_str}
        · floor {floor_str} · {html.escape(str(d.get("checkpoint") or ""))}
        · {html.escape(str(d.get("timestamp") or ""))}</span>
    </div>
    <div class="relics">{" ".join(f'<span class="relic">{html.escape(str(r))}</span>' for r in relics)}</div>
    <div class="cards">{"".join(chips)}</div>
  </div>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Best Boss-Entry Decks</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; background: #14161a;
       color: #e8e8e8; margin: 24px; }}
h1 {{ font-size: 20px; }} .sub {{ color: #9aa; font-size: 13px; margin-bottom: 18px; }}
.panel {{ background: #1d2026; border-radius: 10px; padding: 14px 16px; margin-bottom: 14px; }}
.head {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }}
.rank {{ font-size: 18px; font-weight: 700; color: #ffd75e; }}
.quality {{ font-weight: 600; color: #7ec8ff; }}
.meta {{ color: #9aa; font-size: 12px; }}
.badge {{ padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; }}
.badge.won {{ background: #1d4d2c; color: #7be38a; }}
.badge.died {{ background: #4d1d1d; color: #e37b7b; }}
.badge.unk {{ background: #3a3a44; color: #aaa; }}
.relics {{ margin: 6px 0; }}
.relic {{ background: #2a2438; color: #c9b6ff; border-radius: 5px;
          padding: 1px 7px; font-size: 11px; margin-right: 4px; }}
.cards {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.chip {{ background: #262a31; border-radius: 6px; padding: 3px 8px; font-size: 12px; }}
.chip b {{ color: #ffd75e; margin-left: 6px; font-size: 11px; }}
</style></head><body>
<h1>Best Boss-Entry Decks (top {n} of last {window})</h1>
<div class="sub">deck quality = deck_quality_score · per-card scores = score_card_in_deck
 · hover a card for its description · data: data/eval_decks.jsonl</div>
{"".join(panels)}
</body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="data/eval_decks.jsonl")
    p.add_argument("--out", default="boss_decks.html")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--window", type=int, default=60,
                   help="rank within the most recent N boss-entry records")
    args = p.parse_args()

    card_db = load_card_db()
    decks, results = load_records(args.input)
    print(f"{len(decks)} boss-entry decks, {sum(1 for k in results if k[1] is not None)} results",
          file=sys.stderr)
    html_text = render(decks, results, card_db, args.n, args.window)
    with open(args.out, "w") as f:
        f.write(html_text)
    print(f"Wrote {args.out} ({len(html_text):,} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
