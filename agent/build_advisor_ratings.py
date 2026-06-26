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
