#!/usr/bin/env python3
"""enemy_intents.py — Act-1 enemy roster + intent loops from sts2-wiki.org.

Scrapes /zh/slay-the-spire-2-enemies to build data/enemies.json. Each row:

    {
      "id": "vantom",
      "zh_name": "万影",
      "category": "Boss" | "Monster" | "Elite",
      "act": "The Overgrowth" | "The Underdocks" | ...,
      "hp_normal": "173",        # raw text — may be "20 - 23" range
      "hp_ascended": "183",
      "moves_raw": "墨点 7 / 墨枪 6 / 肢解 27",
      "moves": [                  # parsed
          {"zh_name": "墨点",  "damage": 7},
          {"zh_name": "墨枪",  "damage": 6},
          {"zh_name": "肢解",  "damage": 27},
      ],
      "intent_loop": "墨点 → 墨枪 → 肢解 → 准备",
      "n_moves": 4,
      "appearances": "共1次遭遇，位于繁茂之地",
    }

The simulator uses {moves, intent_loop} to predict the next attack each
turn. Cards' "Apply N Vulnerable" then correctly amplifies the damage by
the standard 50% multiplier; "Apply N Weak" reduces enemy outgoing by 25%.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def scrape_enemies(out_path: str = "data/enemies.json") -> dict:
    url = "https://sts2-wiki.org/zh/slay-the-spire-2-enemies"
    print(f"GET {url}", file=sys.stderr)
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 sts2-rl-scrape/1.0"}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    enemies = []
    for article in soup.find_all("article"):
        h = article.find("h3")
        link = article.find("a")
        text = article.get_text("\n")
        if not h or not link:
            continue
        zh_name = h.get_text(strip=True)
        slug = link.get("href", "").rstrip("/").split("/")[-1]
        full_text = " ".join(article.stripped_strings)
        enemies.append(_parse_enemy(zh_name, slug, full_text))

    out = {"enemies": enemies, "n": len(enemies)}
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out


def _parse_enemy(zh_name: str, slug: str, text: str) -> dict:
    out: dict = {"id": slug, "zh_name": zh_name, "raw_text": text}
    # Category: Boss / Monster / Elite (1st tag in the tile)
    m = re.match(r"(Boss|Monster|Elite|Custom)", text)
    if m:
        out["category"] = m.group(1)
    # Act / region (between category and zh_name)
    m = re.search(r"(?:Boss|Monster|Elite|Custom)\s*(The\s+\w+(?:\s\w+)*?)\s*" + re.escape(zh_name), text)
    if m:
        out["act"] = m.group(1).strip()
    # Moves chunk: between zh_name and "HP"
    m = re.search(re.escape(zh_name) + r"\s*(.+?)\s*HP\s*([\d\s\-]+)\s*(?:\((\d+(?:\s*-\s*\d+)?)\))?", text)
    if m:
        out["moves_raw"] = m.group(1).strip()
        out["hp_normal"] = m.group(2).strip()
        if m.group(3):
            out["hp_ascended"] = m.group(3).strip()
        out["moves"] = _parse_moves(out["moves_raw"])
    # Intent loop: "循环：A → B → C" or similar
    m = re.search(r"(?:循环|交替|顺序)[：:]\s*(.+?)(?:View|\Z)", text)
    if m:
        out["intent_loop"] = m.group(1).strip().rstrip("。")
    # N moves: "Moves4" pattern
    m = re.search(r"Moves(\d+)", text)
    if m:
        out["n_moves"] = int(m.group(1))
    # Appearances: "共N次遭遇，位于X"
    m = re.search(r"(共\d+次遭遇[^Tags]*?)(?:Tags|\Z)", text)
    if m:
        out["appearances"] = m.group(1).strip()
    return out


def _parse_moves(text: str) -> list[dict]:
    """Split "墨点 7 / 墨枪 6 / 肢解 27" into structured move list."""
    moves = []
    for part in re.split(r"\s*/\s*", text):
        part = part.strip()
        if not part:
            continue
        # Move name + optional damage number trailing
        m = re.match(r"(.+?)\s+(\d+)$", part)
        if m:
            moves.append({"zh_name": m.group(1).strip(),
                          "damage": int(m.group(2))})
        else:
            # No damage (debuff move) — store name only
            moves.append({"zh_name": part})
    return moves


# ─── unit tests ────────────────────────────────────────────────────────────
TESTS_PARSE_MOVES = [
    ("墨点 7 / 墨枪 6 / 肢解 27",
     [{"zh_name": "墨点", "damage": 7},
      {"zh_name": "墨枪", "damage": 6},
      {"zh_name": "肢解", "damage": 27}]),
    ("加压 / 水柱 10",
     [{"zh_name": "加压"},
      {"zh_name": "水柱", "damage": 10}]),
    ("快斩 5 / 回旋镖 2 / 力量舞",
     [{"zh_name": "快斩", "damage": 5},
      {"zh_name": "回旋镖", "damage": 2},
      {"zh_name": "力量舞"}]),
]


if __name__ == "__main__":
    print("=== Unit tests ===")
    failed = 0
    for text, expected in TESTS_PARSE_MOVES:
        got = _parse_moves(text)
        if got == expected:
            print(f"  PASS: {text}")
        else:
            failed += 1
            print(f"  FAIL: {text}")
            print(f"    expected: {expected}")
            print(f"    got:      {got}")
    if failed:
        raise SystemExit(1)
    print()
    print("=== Scraping enemies ===")
    out = scrape_enemies()
    print(f"Scraped {out['n']} enemies")
    print()
    # Coverage stats
    has_moves = sum(1 for e in out["enemies"] if e.get("moves"))
    has_hp = sum(1 for e in out["enemies"] if e.get("hp_normal"))
    has_loop = sum(1 for e in out["enemies"] if e.get("intent_loop"))
    print(f"  with moves:       {has_moves}/{out['n']}")
    print(f"  with HP:          {has_hp}/{out['n']}")
    print(f"  with intent_loop: {has_loop}/{out['n']}")
    # Sample
    print()
    print("=== Sample (first 3) ===")
    for e in out["enemies"][:3]:
        print(f"  {e['zh_name']} ({e.get('category','?')} / {e.get('act','?')})")
        print(f"    HP {e.get('hp_normal','?')} ({e.get('hp_ascended','?')}) "
              f"moves={e.get('moves',[])}")
        print(f"    loop: {e.get('intent_loop','?')}")
