#!/usr/bin/env python3
"""build_card_db.py — Generate sim card data from the SHIPPED game files.

The sim's existing DB (`data/ironclad_cards_parsed.json`) was scraped from a
wiki in May 2026 and holds 84 Ironclad cards and nothing else, so
`turn_planner` and `combat_simulator` are structurally inert for the other four
characters (`get_card_data` returns None and the DFS skips the card entirely).

This builds the same shape from data already in the repo, which is both more
complete and fresher than the wiki:

    localization_eng/cards.json  <ID>.description   (card text, with {Var} slots)
  x data/card_db_advisor.json    <ID>.vars          (the numbers for those slots)
  -> agent/sim/card_effects.parse_card_text         (existing parser)

Deliberately NOT a wiki re-scrape: the wiki copy is already stale against
v0.111 (it has Cinder as "Exhaust 1 card at random" where the game ships
"Exhaust the top card of your Draw Pile").

Usage:
    .venv/bin/python -m agent.sim.build_card_db --character DEFECT
    .venv/bin/python -m agent.sim.build_card_db --character ALL --out data/cards_parsed_all.json
"""
from __future__ import annotations

import argparse
import json
import os
import re

from agent.sim.card_effects import parse_card_text

# [gold]...[/gold] and [purple] are display markup; measured over all 507 cards
# these are the ONLY bracket tokens, so stripping them loses no meaning.
_MARKUP = re.compile(r"\[/?[a-zA-Z][^\]]*\]")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}")

# Placeholder names that never appear in `vars` because they are structural
# rather than per-card. Measured counts across all 507 cards in parentheses.
_STRUCTURAL = {
    "IfUpgraded": "",       # (29) upgrade-conditional clause; we render base text
    "energyPrefix": "",     # (20) energy icon
    "InCombat": "",         # (20) conditional phrasing
    "singleStarIcon": "",   # (6)
    "IsTargeting": "",      # (2)
}


def _split_top_level(body: str, sep: str = "|") -> list[str]:
    """Split on `sep`, ignoring separators inside nested {...}."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _find_close(text: str, open_idx: int) -> int:
    """Index of the `}` matching the `{` at open_idx, or -1."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def render(description: str, variables: dict) -> tuple[str | None, list[str]]:
    """Resolve the localization template grammar to plain card text.

    Grammar (all observed in localization_eng/cards.json):
        {Var:diff()}                     -> value
        {Var:energyIcons()}              -> value
        {Var:plural:one|many}            -> branch on value == 1; branches nest
        {IfUpgraded:show:A|B}            -> B, since we render the BASE card
        {InCombat:A|B}                   -> B, the out-of-combat form
        {Var}                            -> value

    Brace matching is explicit rather than regex-based: a `[^}]*` pattern stops
    at the first closing brace, which shreds the nested plural forms into
    strings like "Prevent the next 1 times}" that parse to nothing.
    """
    unresolved: list[str] = []

    def expand(text: str) -> str:
        out: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch != "{":
                out.append(ch)
                i += 1
                continue
            close = _find_close(text, i)
            if close < 0:                      # unbalanced — drop the brace
                i += 1
                continue
            inner = text[i + 1:close]
            out.append(resolve(inner))
            i = close + 1
        return "".join(out)

    def resolve(inner: str) -> str:
        name, _, rest = inner.partition(":")
        name = name.strip()
        if name in ("IfUpgraded", "InCombat"):
            # "show:A|B" or "A|B" — take the base (second) branch.
            body = rest.partition(":")[2] if rest.startswith("show:") else rest
            branches = _split_top_level(body)
            return expand(branches[-1]) if branches else ""
        if rest.startswith("plural:"):
            branches = _split_top_level(rest[len("plural:"):])
            value = variables.get(name)
            singular = isinstance(value, (int, float)) and int(value) == 1
            chosen = branches[0] if (singular or len(branches) == 1) else branches[-1]
            return expand(chosen)
        if name in variables and variables[name] is not None:
            return str(variables[name])
        if name in _STRUCTURAL:
            return _STRUCTURAL[name]
        unresolved.append(name)
        return ""

    text = expand(description)
    text = _MARKUP.sub("", text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,])", r"\1", text)   # "next time ." -> "next time."
    text = text.strip()
    return (text or None), unresolved


def build(characters: set[str], loc: dict, adv: dict) -> dict:
    cards: list[dict] = []
    stats = {"cards": 0, "unresolved": 0, "no_effects": 0}
    for cid, entry in sorted(adv.items()):
        character = entry.get("character")
        if characters and character not in characters:
            continue
        description = loc.get(f"{cid}.description")
        if description is None:
            continue
        variables = dict(entry.get("vars") or {})
        normal, unresolved = render(description, variables)
        if normal is None:
            continue
        # Exhaust/Innate are card PROPERTIES, absent from the description text —
        # the single most common parse mismatch when validating against the
        # known-good Ironclad entries. The advisor axes carry them.
        axes = entry.get("axes") or []
        if "EXHAUST_SELF" in axes and "exhaust" not in normal.lower():
            normal = f"{normal} Exhaust."
        upgraded = normal  # upg_* fields carry scalars, not text; base is the default
        effects, unparsed = parse_card_text(normal)
        stats["cards"] += 1
        if unresolved:
            stats["unresolved"] += 1
        if not effects:
            stats["no_effects"] += 1
        cards.append({
            "id": cid.lower().replace("_", "-"),
            "game_id": cid,
            "en_name": loc.get(f"{cid}.title") or cid,
            "character": (character or "").title(),
            "type": entry.get("type"),
            "rarity": entry.get("rarity"),
            "cost": str(entry.get("cost")) if entry.get("cost") is not None else "1",
            "normal_text": normal,
            "upgraded_text": upgraded,
            "parsed": {
                "normal": effects,
                "upgraded": effects,
                "unparsed_normal": unparsed,
                "unparsed_upgraded": unparsed,
            },
        })
    return {"cards": cards, "errors": [], "build_stats": stats}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", default="DEFECT",
                        help="Advisor character key, or ALL. SHARED is always included.")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    loc = json.load(open("localization_eng/cards.json"))
    adv = json.load(open("data/card_db_advisor.json"))

    if args.character.upper() == "ALL":
        wanted: set[str] = set()
    else:
        wanted = {args.character.upper(), "SHARED"}

    out = build(wanted, loc, adv)
    path = args.out or f"data/cards_parsed_{args.character.lower()}.json"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"wrote {path}: {out['build_stats']}")


if __name__ == "__main__":
    main()
