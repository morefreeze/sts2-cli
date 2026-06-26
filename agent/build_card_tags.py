#!/usr/bin/env python3
"""build_card_tags.py — Generate archetype tags for Ironclad cards.

Hybrid approach (C):
  1. Regex-based extraction from data/ironclad_cards.json normal_text
  2. Hand-curated overrides for special cases (scaling pillars,
     hand-picked subset that the regex can't see)
  3. Output → data/ironclad_card_tags.json

Each card may have multiple tags (a card belongs to multiple streams).

Tag taxonomy (15 tags):
  Debuff      VULNERABLE, WEAK, STR_DOWN
  Strength    STRENGTH_GAIN, STRENGTH_USER
  Block       BLOCK_PAYLOAD, BLOCK_SCALING
  Engine      DRAW, EXHAUST_PAYLOAD, EXHAUST_FUEL, ENERGY_GAIN
  Output      BURST, CHEAP_ATTACK, VULNERABLE_PAYLOAD
  Growth      SCALING_PILLAR
"""
from __future__ import annotations
import json
import os
import re
import sys

CARD_JSON = "data/ironclad_cards.json"
OUT_PATH = "data/ironclad_card_tags.json"


# Regex patterns for automated tagging
_PATTERNS = {
    "VULNERABLE":           [r"\bvulnerable\b"],
    "WEAK":                 [r"\bweak\b"],
    "STR_DOWN":             [r"loses?\s+\d+\s*strength", r"strength.*this turn"],
    "STRENGTH_GAIN":        [r"gain\s+\d+\s*strength"],
    "BLOCK_PAYLOAD":        [r"gain\s+\d+\s*block"],
    "BLOCK_SCALING":        [r"damage\s+equal\s+to\s+your\s+block"],
    "DRAW":                 [r"draw\s+\d+\s*cards?", r"draw\s+a\s+card",
                              r"draw\s+cards\s+until"],
    "EXHAUST_PAYLOAD":      [r"whenever\s+(a\s+)?cards?\s+(is|are)?\s*exhaust",
                              r"whenever you exhaust",
                              r"for each card exhausted",
                              r"if you exhausted a card this turn"],
    "EXHAUST_FUEL":         [r"\bexhaust\b.*$"],   # self-exhaust (broad match below)
    "ENERGY_GAIN":          [r"gain\s+\d+\s*energy", r"costs?\s+1\s+less"],
    "VULNERABLE_PAYLOAD":   [r"vulnerable enemies",
                              r"for each vulnerable", r"if the enemy is vulnerable",
                              r"double the enemy.s vulnerable"],
}


# Hand-curated overrides — final say wins. Lists are ADDED to regex-detected tags.
# Set value to a list with a leading "-" to also remove a tag.
_HAND_OVERRIDES: dict[str, list[str]] = {
    # Scaling pillars — late-game cornerstone cards (per user's example)
    "body-slam":       ["SCALING_PILLAR"],
    "rampage":         ["SCALING_PILLAR"],
    "demon-form":      ["SCALING_PILLAR"],
    "inflame":         ["SCALING_PILLAR"],
    "feed":            ["SCALING_PILLAR"],
    "pyre":            ["SCALING_PILLAR"],
    "whirlwind":       ["SCALING_PILLAR"],
    "bludgeon":        ["SCALING_PILLAR"],
    "inferno":         ["SCALING_PILLAR"],
    "barricade":       ["SCALING_PILLAR", "BLOCK_SCALING"],
    "juggernaut":      ["SCALING_PILLAR"],
    "rupture":         ["SCALING_PILLAR", "STRENGTH_GAIN"],
    "hellraiser":      ["SCALING_PILLAR"],
    "perfected-strike": ["SCALING_PILLAR", "STRENGTH_USER"],
    "stampede":        ["SCALING_PILLAR"],
    "juggling":        ["SCALING_PILLAR"],

    # Multi-hit attacks — strength scaling beneficiaries
    "twin-strike":     ["STRENGTH_USER", "CHEAP_ATTACK"],
    "pommel-strike":   ["STRENGTH_USER", "CHEAP_ATTACK", "DRAW"],
    "sword-boomerang": ["STRENGTH_USER", "CHEAP_ATTACK"],
    "thrash":          ["STRENGTH_USER"],
    "thunderclap":     ["STRENGTH_USER", "VULNERABLE"],
    "tear-asunder":    ["STRENGTH_USER"],
    "dismantle":       ["VULNERABLE_PAYLOAD"],
    "wild-strike":     ["STRENGTH_USER"] if False else [],  # not in list
    "iron-wave":       ["BLOCK_PAYLOAD", "CHEAP_ATTACK"],
    "breakthrough":    ["CHEAP_ATTACK"],   # multi-hit AoE

    # Bursts
    "bludgeon":        ["BURST"],
    "uppercut":        ["BURST", "WEAK", "VULNERABLE"],
    "mangle":          ["BURST", "STR_DOWN"],
    "break":           ["BURST", "VULNERABLE"],
    "howl-from-beyond": ["BURST"],
    "hemokinesis":     ["BURST"],
    "cinder":          ["BURST", "EXHAUST_PAYLOAD"],

    # Exhaust ecosystem
    "feel-no-pain":    ["EXHAUST_PAYLOAD", "BLOCK_PAYLOAD"],
    "dark-embrace":    ["EXHAUST_PAYLOAD", "DRAW"],
    "corruption":      ["EXHAUST_PAYLOAD"],
    "evil-eye":        ["EXHAUST_PAYLOAD", "BLOCK_PAYLOAD"],
    "forgotten-ritual": ["EXHAUST_PAYLOAD", "ENERGY_GAIN"],
    "burning-pact":    ["EXHAUST_FUEL", "DRAW"],
    "havoc":           ["EXHAUST_FUEL"],
    "stoke":           ["EXHAUST_FUEL", "DRAW"],
    "second-wind":     ["EXHAUST_FUEL", "BLOCK_PAYLOAD"],
    "infernal-blade":  ["EXHAUST_FUEL"],
    "brand":           ["STRENGTH_GAIN", "EXHAUST_FUEL"],
    "fiend-fire":      ["EXHAUST_FUEL", "BURST"],
    "pacts-end":       ["EXHAUST_PAYLOAD"],   # benefits from filled Exhaust pile
    "ashen-strike":    ["EXHAUST_PAYLOAD"],
    "anger":           ["CHEAP_ATTACK"],

    # Block scaling / synergy
    "rage":            ["BLOCK_PAYLOAD"],
    "juggernaut":      ["SCALING_PILLAR", "BLOCK_PAYLOAD"],
    "grapple":         ["BLOCK_PAYLOAD"],
    "crimson-mantle":  ["BLOCK_PAYLOAD"],
    "unmovable":       ["BLOCK_PAYLOAD"],
    "stone-armor":     ["BLOCK_PAYLOAD"],
    "blood-wall":      ["BLOCK_PAYLOAD"],
    "colossus":        ["BLOCK_PAYLOAD"],
    "true-grit":       ["BLOCK_PAYLOAD", "EXHAUST_FUEL"],
    "impervious":      ["BLOCK_PAYLOAD", "EXHAUST_FUEL"],

    # Vulnerable spread
    "bash":            ["VULNERABLE", "BURST"],
    "tremble":         ["VULNERABLE"],
    "taunt":           ["VULNERABLE", "BLOCK_PAYLOAD"],
    "thunderclap":     ["VULNERABLE", "STRENGTH_USER"],
    "molten-fist":     ["VULNERABLE_PAYLOAD"],
    "bully":           ["VULNERABLE_PAYLOAD", "CHEAP_ATTACK"],
    "dominate":        ["VULNERABLE_PAYLOAD", "STRENGTH_GAIN"],
    "cruelty":         ["VULNERABLE_PAYLOAD"],
    "vicious":         ["VULNERABLE_PAYLOAD", "DRAW"],

    # Energy boost
    "bloodletting":    ["ENERGY_GAIN"],
    "offering":        ["ENERGY_GAIN", "DRAW", "EXHAUST_FUEL"],
    "pyre":            ["ENERGY_GAIN", "SCALING_PILLAR"],
    "expect-a-fight":  ["ENERGY_GAIN"],

    # Draw engines
    "battle-trance":   ["DRAW"],
    "pillage":         ["DRAW"],
    "drum-of-battle":  ["DRAW"],
    "shrug-it-off":    ["BLOCK_PAYLOAD", "DRAW"],
    "spite":           ["DRAW", "CHEAP_ATTACK"],

    # Misc
    "bash":            ["VULNERABLE", "BURST"],
    "strike":          ["CHEAP_ATTACK"],
    "defend":          ["BLOCK_PAYLOAD"],
    "armaments":       ["BLOCK_PAYLOAD"],
    "headbutt":        ["DRAW"],
    "setup-strike":    ["STRENGTH_GAIN"],
    "one-two-punch":   ["STRENGTH_USER"],
    "primal-force":    ["SCALING_PILLAR"],
    "stomp":           [],
    "cascade":         ["ENERGY_GAIN"],
    "unrelenting":     ["ENERGY_GAIN", "BURST"],
    "flame-barrier":   ["BLOCK_PAYLOAD"],
    "tank":            [],     # single-player only — Tank never appears
    "aggression":      ["DRAW", "STRENGTH_USER", "UPGRADE_PRIORITY_HIGH"],

    # === Combat-type optimality tags — what type of fight this card wins
    # MOB_OPTIMAL: low-cost fast-attack, finishes 3-HP-pack mobs in <5 turns
    # ELITE_OPTIMAL: vulnerable + concentrated single-target burst
    # BOSS_OPTIMAL: scaling pillars + sustain + long-fight payoff
    "strike":          ["CHEAP_ATTACK", "MOB_OPTIMAL"],
    "twin-strike":     ["STRENGTH_USER", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "sword-boomerang": ["STRENGTH_USER", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "iron-wave":       ["BLOCK_PAYLOAD", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "anger":           ["CHEAP_ATTACK", "MOB_OPTIMAL"],
    "wild-strike":     ["CHEAP_ATTACK", "MOB_OPTIMAL"],
    "thunderclap":     ["VULNERABLE", "STRENGTH_USER", "MOB_OPTIMAL"],   # AoE 4 dmg+vuln
    "cleave":          ["MOB_OPTIMAL"],
    "havoc":           ["EXHAUST_FUEL", "UPGRADE_PRIORITY_HIGH", "MOB_OPTIMAL"],
    "pommel-strike":   ["STRENGTH_USER", "CHEAP_ATTACK", "DRAW", "MOB_OPTIMAL"],
    "bully":           ["VULNERABLE_PAYLOAD", "CHEAP_ATTACK", "MOB_OPTIMAL"],

    "bash":            ["VULNERABLE", "BURST", "ELITE_OPTIMAL"],
    "uppercut":        ["BURST", "WEAK", "VULNERABLE", "ELITE_OPTIMAL"],
    "bludgeon":        ["BURST", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "ELITE_OPTIMAL", "BOSS_OPTIMAL"],
    "mangle":          ["BURST", "STR_DOWN", "ELITE_OPTIMAL"],
    "break":           ["BURST", "VULNERABLE", "ELITE_OPTIMAL"],
    "molten-fist":     ["VULNERABLE_PAYLOAD", "ELITE_OPTIMAL"],
    "tear-asunder":    ["STRENGTH_USER", "ELITE_OPTIMAL"],
    "hemokinesis":     ["BURST", "ELITE_OPTIMAL"],
    "perfected-strike": ["SCALING_PILLAR", "STRENGTH_USER", "ELITE_OPTIMAL", "BOSS_OPTIMAL"],

    "demon-form":      ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "inflame":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "body-slam":       ["BLOCK_SCALING", "CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "barricade":       ["BLOCK_SCALING", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "feed":            ["CHEAP_ATTACK", "EXHAUST_FUEL", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "whirlwind":       ["CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "rupture":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "juggernaut":      ["SCALING_PILLAR", "BLOCK_PAYLOAD", "BOSS_OPTIMAL"],
    "metallicize":     ["BLOCK_PAYLOAD", "BOSS_OPTIMAL"],
    "corruption":      ["EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "dark-embrace":    ["DRAW", "EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "rampage":         ["SCALING_PILLAR", "BOSS_OPTIMAL"],

    # === UPGRADE_PRIORITY_HIGH: qualitative jump on +. Auto-detected:
    # cost reduction, innate gained, exhaust removed. Plus handpicks where
    # numeric scaling acceleration matters disproportionately.
    "barricade":       ["BLOCK_SCALING", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],   # 3→2 cost
    "body-slam":       ["BLOCK_SCALING", "CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],  # 1→0 cost
    "corruption":      ["EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH"],   # 3→2 cost
    "dark-embrace":    ["DRAW", "EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH"],  # 2→1 cost
    "expect-a-fight":  ["ENERGY_GAIN", "UPGRADE_PRIORITY_HIGH"],                       # 2→1 cost
    "havoc":           ["EXHAUST_FUEL", "UPGRADE_PRIORITY_HIGH"],                       # 1→0 cost
    "hellraiser":      ["SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],                     # 2→1 cost
    "infernal-blade":  ["EXHAUST_FUEL", "UPGRADE_PRIORITY_HIGH"],                       # 1→0 cost
    "juggling":        ["SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],                     # gains innate
    "stampede":        ["SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],                     # 2→1 cost
    "stoke":           ["EXHAUST_FUEL", "DRAW", "UPGRADE_PRIORITY_HIGH"],               # 1→0 cost
    "unmovable":       ["BLOCK_PAYLOAD", "UPGRADE_PRIORITY_HIGH"],                      # 2→1 cost

    # Scaling/ramp acceleration — upgrade lets the engine hit faster
    "demon-form":      ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH"],    # 2→3 STR/turn
    "inflame":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH"],    # 2→3 STR
    "rupture":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH"],
    "feed":            ["CHEAP_ATTACK", "EXHAUST_FUEL", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],   # +max HP from kills
    "whirlwind":       ["CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],     # +1 damage per hit
    "bludgeon":        ["BURST", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH"],            # huge dmg leap
    "spot-weakness":   ["STRENGTH_GAIN"] if False else [],  # not in list

    # ── Final-override layer: combat-type tags added LAST so they win ──
    "strike":          ["CHEAP_ATTACK", "MOB_OPTIMAL"],
    "twin-strike":     ["STRENGTH_USER", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "sword-boomerang": ["STRENGTH_USER", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "iron-wave":       ["BLOCK_PAYLOAD", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "anger":           ["CHEAP_ATTACK", "MOB_OPTIMAL"],
    "thunderclap":     ["VULNERABLE", "STRENGTH_USER", "MOB_OPTIMAL"],
    "pommel-strike":   ["STRENGTH_USER", "CHEAP_ATTACK", "DRAW", "MOB_OPTIMAL"],
    "bully":           ["VULNERABLE_PAYLOAD", "CHEAP_ATTACK", "MOB_OPTIMAL"],
    "havoc":           ["EXHAUST_FUEL", "UPGRADE_PRIORITY_HIGH", "MOB_OPTIMAL"],

    "bash":            ["VULNERABLE", "BURST", "ELITE_OPTIMAL"],
    "uppercut":        ["BURST", "WEAK", "VULNERABLE", "ELITE_OPTIMAL"],
    "mangle":          ["BURST", "STR_DOWN", "ELITE_OPTIMAL"],
    "break":           ["BURST", "VULNERABLE", "ELITE_OPTIMAL"],
    "molten-fist":     ["VULNERABLE_PAYLOAD", "ELITE_OPTIMAL"],
    "tear-asunder":    ["STRENGTH_USER", "ELITE_OPTIMAL"],
    "hemokinesis":     ["BURST", "ELITE_OPTIMAL"],

    "bludgeon":        ["BURST", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "ELITE_OPTIMAL", "BOSS_OPTIMAL"],
    "perfected-strike": ["SCALING_PILLAR", "STRENGTH_USER", "ELITE_OPTIMAL", "BOSS_OPTIMAL"],
    "demon-form":      ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "inflame":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "body-slam":       ["BLOCK_SCALING", "CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "barricade":       ["BLOCK_SCALING", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "feed":            ["CHEAP_ATTACK", "EXHAUST_FUEL", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "whirlwind":       ["CHEAP_ATTACK", "SCALING_PILLAR", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "rupture":         ["SCALING_PILLAR", "STRENGTH_GAIN", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "juggernaut":      ["SCALING_PILLAR", "BLOCK_PAYLOAD", "BOSS_OPTIMAL"],
    "corruption":      ["EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "dark-embrace":    ["DRAW", "EXHAUST_FUEL", "EXHAUST_PAYLOAD", "UPGRADE_PRIORITY_HIGH", "BOSS_OPTIMAL"],
    "rampage":         ["SCALING_PILLAR", "BOSS_OPTIMAL"],
}


def _detect(text: str) -> set[str]:
    """Apply regex patterns to a card's normal_text; return set of matched tags."""
    if not text:
        return set()
    text = text.lower()
    out = set()
    for tag, patterns in _PATTERNS.items():
        for p in patterns:
            if re.search(p, text):
                out.add(tag); break

    # Exhaust-fuel is "self-exhausts" (card text ends in "Exhaust." or has it
    # standalone) — but exclude EXHAUST_PAYLOAD which is about WHENEVER
    # something exhausts. Use a stricter pattern.
    if re.search(r"\bexhaust\b\.?$", text.rstrip()) and "whenever" not in text:
        out.add("EXHAUST_FUEL")

    # Block scaling supersedes block_payload
    if "BLOCK_SCALING" in out:
        out.discard("BLOCK_PAYLOAD")
    return out


def _apply_overrides(tags: set[str], override: list[str]) -> set[str]:
    if not override:
        return tags
    for t in override:
        if t.startswith("-"):
            tags.discard(t[1:])
        else:
            tags.add(t)
    return tags


def _norm_id(slug: str) -> str:
    return slug.upper().replace("-", "_")


def main():
    if not os.path.exists(CARD_JSON):
        print(f"missing {CARD_JSON}", file=sys.stderr); return 1
    with open(CARD_JSON) as f:
        data = json.load(f)
    cards = data.get("cards", [])
    out: dict[str, dict] = {}
    summary = {"by_tag": {}, "n_tagged": 0, "n_untagged": 0}
    for c in cards:
        slug = c.get("id") or ""
        rid = _norm_id(slug)
        text = c.get("normal_text") or ""
        type_ = c.get("type") or ""
        cost = c.get("cost")
        try:
            cost_i = int(cost) if cost is not None and cost not in ("?", "X") else None
        except Exception:
            cost_i = None

        tags = _detect(text)
        # Implicit tags from cost/type — cheap attack
        if (cost_i is not None and cost_i <= 1 and type_ == "Attack"
                and "BURST" not in tags):
            tags.add("CHEAP_ATTACK")
        # Apply hand-curated override (additive — does not clear regex tags
        # unless prefixed with "-")
        if slug in _HAND_OVERRIDES:
            tags = _apply_overrides(tags, _HAND_OVERRIDES[slug])
        # Final cleanup: block_scaling implies remove block_payload
        if "BLOCK_SCALING" in tags:
            tags.discard("BLOCK_PAYLOAD")
        out[rid] = sorted(tags)
        for t in tags:
            summary["by_tag"][t] = summary["by_tag"].get(t, 0) + 1
        if tags:
            summary["n_tagged"] += 1
        else:
            summary["n_untagged"] += 1

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump({"summary": summary, "card_tags": out}, f, indent=2)
    print(f"Wrote {OUT_PATH}  ({len(out)} cards, {summary['n_tagged']} tagged, "
          f"{summary['n_untagged']} no-tag)")
    print(f"Tag distribution: " + ", ".join(
        f"{t}={n}" for t, n in sorted(summary['by_tag'].items(),
                                       key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
