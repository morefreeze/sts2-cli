#!/usr/bin/env python3
"""scrape_relic_db.py — Fetch relic details from sts2-wiki.org and write
data/relics.json.

Mirrors scrape_card_db.py. Reads /tmp/sts2-cli/relic_urls.json (a JSON
array of {"slug": "...", "url": "..."}), hits each page, extracts the
name / rarity / character restriction / effect text, and writes a single
data/relics.json keyed by uppercase runtime ID
(e.g. "BURNING_BLOOD") so card_scoring + the v2 predictor can look up
relics with the same naming convention used elsewhere.

Output schema per record:
    {
      "id":         "burning-blood",          # wiki slug
      "runtime_id": "BURNING_BLOOD",          # game-state key
      "en_name":    "Burning Blood",
      "zh_name":    "燃烧的血液",              # filled later if known
      "rarity":     "Starter",
      "character":  "Ironclad",
      "url":        "https://sts2-wiki.org/...",
      "effect_text":"At the end of combat, heal 6 HP.",
      "meta_raw":   "...full meta sentence from page..."
    }

Usage:
    .venv/bin/python agent/scrape_relic_db.py
        --urls /tmp/sts2-cli/relic_urls.json
        --out  data/relics.json
"""
import argparse
import json
import re
import sys
import time

import requests
from bs4 import BeautifulSoup


_RARITIES = {"Starter", "Common", "Uncommon", "Rare", "Shop", "Event",
             "Boss", "Ancient", "Special"}
_CHARACTERS = {"Ironclad", "Silent", "Defect", "Watcher", "Regent",
               "Necrobinder"}


def slug_to_runtime(slug: str) -> str:
    """Best-effort slug → uppercase runtime key (matches in-game id form)."""
    s = slug.upper().replace("-", "_")
    # Trailing _2 designates the upgraded / Tower variant; drop for matching
    if s.endswith("_2"):
        s = s[:-2]
    return s


_META_RE = re.compile(
    r"(?P<name>.+?)\s+is\s+(?:a|an|the)\s+(?P<rarity>\w+)\s+Slay the Spire 2 relic\.\s*"
    r"(?P<effect>.+?)(?:\s+Pool:\s*(?P<character>.+?))?\.\s*$",
    re.IGNORECASE,
)


def parse_relic(html: str, slug: str, url: str) -> dict:
    """Parse the page's <meta name="description"> tag — sts2-wiki.org is JS-
    rendered, so the visible HTML body is mostly an empty shell, but every
    page bakes a one-line summary into the description meta. That summary
    follows a strict template:

        "{Name} is a {rarity} Slay the Spire 2 relic.
         {effect text} Pool: {character}."

    which we crack with a single regex.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {"id": slug, "url": url, "runtime_id": slug_to_runtime(slug)}

    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""
    out["meta_raw"] = desc

    m = _META_RE.match(desc)
    if m:
        out["en_name"] = m.group("name").strip()
        rarity = m.group("rarity").strip().capitalize()
        out["rarity"] = rarity if rarity in _RARITIES else rarity
        eff = m.group("effect").strip()
        # Strip a trailing "Pool: …" if the regex grabbed it back
        eff = re.sub(r"\s*Pool:.*$", "", eff)
        out["effect_text"] = eff.rstrip(". ").rstrip()
        if m.group("character"):
            ch = m.group("character").strip()
            out["character"] = ch if ch in _CHARACTERS else ch
    else:
        # Fall back: take title as name, leave the rest blank
        title_tag = soup.find("title")
        if title_tag:
            t = title_tag.get_text(strip=True)
            mt = re.match(r"^(.+?)\s+Relic\s*\|", t)
            out["en_name"] = mt.group(1).strip() if mt else t

    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", default="/tmp/sts2-cli/relic_urls.json")
    p.add_argument("--out", default="data/relics.json")
    p.add_argument("--limit", type=int, default=None,
                   help="Optional cap for smoke testing.")
    p.add_argument("--sleep", type=float, default=0.4,
                   help="Seconds between requests (be kind to the wiki).")
    args = p.parse_args()

    with open(args.urls) as f:
        urls = json.load(f)
    if args.limit:
        urls = urls[: args.limit]

    print(f"Scraping {len(urls)} relics → {args.out}", file=sys.stderr)
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 sts2-rl-scrape/1.0"

    results = []
    fails = []
    for i, rec in enumerate(urls):
        slug = rec["slug"]
        url = rec["url"]
        try:
            r = session.get(url, timeout=20)
            r.raise_for_status()
            parsed = parse_relic(r.text, slug, url)
            results.append(parsed)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(urls)} — last: {parsed.get('en_name', slug)}",
                      file=sys.stderr)
        except Exception as e:
            fails.append((slug, str(e)))
            print(f"  ✗ {slug}: {e}", file=sys.stderr)
        time.sleep(args.sleep)

    out_path = args.out
    payload = {"relics": results, "n": len(results),
               "n_fail": len(fails), "fails": fails}
    with open(out_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Done. wrote {len(results)} relics ({len(fails)} failed) → {out_path}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
