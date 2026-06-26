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
        # NOTE: brace counting is string-NAIVE — it does not skip "{"/"}" that appear
        # inside string values. Safe for this dataset (advisor fields have no literal
        # braces in strings); if one ever did, json.loads below would raise and that
        # single card would be skipped (fail-safe, not a crash). Re-runnable offline.
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
