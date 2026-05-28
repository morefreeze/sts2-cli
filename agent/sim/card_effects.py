#!/usr/bin/env python3
"""card_effects.py — Parse card text into structured effect primitives.

Input: a single card's text (e.g. "Deal 6 damage. Add a copy of this card into
your Discard Pile."). Output: a list of Effect dicts with a "kind" tag plus
typed parameters, suitable for the Phase 2 Python combat simulator.

Each card has both `normal_text` and `upgraded_text`; parse_card() returns
{normal: [effects], upgraded: [effects], unparsed: [residual strings]}.

Effect kinds covered (~15 primitives — enough for 80% of Ironclad cards):

  Direct combat:
    {"kind": "deal_damage", "amount": int}            — single-target damage
    {"kind": "deal_aoe", "amount": int}               — damage all enemies
    {"kind": "multi_hit", "amount": int, "times": "X" | int}
    {"kind": "gain_block", "amount": int}
    {"kind": "lose_hp", "amount": int}

  Status / power:
    {"kind": "apply_status", "status": str, "amount": int, "target": "enemy"|"all"}
    {"kind": "gain_status", "status": str, "amount": int,
        "scope": "permanent" | "this_turn" | "next_turn"}

  Card flow:
    {"kind": "draw", "amount": int}
    {"kind": "gain_energy", "amount": int}
    {"kind": "exhaust_self"}
    {"kind": "exhaust_target", "where": "draw_top" | "hand" | "deck"}
    {"kind": "add_copy", "where": "discard" | "hand" | "draw"}

  Keywords:
    {"kind": "innate"}
    {"kind": "retain"}
    {"kind": "ethereal"}

  Triggers (stored as wrapper around the body effects):
    {"kind": "on_turn_start", "effects": [...]}
    {"kind": "on_turn_end", "effects": [...]}
    {"kind": "on_lose_hp", "effects": [...]}
    {"kind": "on_exhaust", "effects": [...]}

  Conditionals:
    {"kind": "if", "condition": str, "then": [...], "else": [...]}

  Anything we can't parse goes to `unparsed` so v3 can fall back to real engine
  for those cards.
"""
import json
import re
from typing import Any

Effect = dict[str, Any]


# Status names normalised to their canonical form used by the simulator.
KNOWN_STATUSES = {
    "Vulnerable", "Weak", "Frail", "Strength", "Dexterity",
    "Thorns", "Block", "Metallicize", "Ritual", "Plated Armor",
    "Confusion", "Burn", "Poison",
}


def _trim_period(s: str) -> str:
    return s.strip().rstrip(".").strip()


def _parse_sentence(s: str) -> tuple[list[Effect], list[str]]:
    """Parse a single sentence (period-delimited fragment) into effects.
    Returns (effects, unparsed_residual_strings)."""
    s = _trim_period(s)
    if not s:
        return [], []
    effects: list[Effect] = []
    unparsed: list[str] = []

    # Order matters — match more-specific patterns before generic.

    # --- Multi-hit attack: "Deal N damage X times." (X is var or int) -----
    m = re.match(r"Deal\s+(\d+)\s+damage\s+(?:to\s+ALL\s+enemies\s+)?(\d+|X)\s+times?", s, re.I)
    if m:
        amt = int(m.group(1))
        times_raw = m.group(2)
        times = "X" if times_raw == "X" else int(times_raw)
        aoe = "ALL enemies" in s
        effects.append({"kind": "multi_hit", "amount": amt, "times": times,
                        "target": "all" if aoe else "enemy"})
        return effects, unparsed

    # --- AOE damage: "Deal N damage to ALL enemies." -----------------------
    m = re.match(r"Deal\s+(\d+)\s+damage\s+to\s+ALL\s+enemies", s, re.I)
    if m:
        effects.append({"kind": "deal_aoe", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Single-target damage: "Deal N damage." ----------------------------
    m = re.match(r"Deal\s+(\d+)\s+damage\b", s, re.I)
    if m:
        effects.append({"kind": "deal_damage", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Block: "Gain N Block." / "gain N Block" (case-insens for trigger bodies)
    m = re.match(r"[Gg]ain\s+(\d+)\s+Block\b", s)
    if m:
        effects.append({"kind": "gain_block", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Apply status: "Apply N <Status>." ---------------------------------
    m = re.match(r"Apply\s+(\d+)\s+(\w+)", s)
    if m:
        st = m.group(2)
        aoe = "ALL enemies" in s
        effects.append({"kind": "apply_status", "status": st,
                        "amount": int(m.group(1)),
                        "target": "all" if aoe else "enemy"})
        return effects, unparsed

    # --- Gain status (self): "Gain N <Status>." [optional "this turn"] ----
    # Block / Energy / draw have their own patterns above; only match "status"
    # words here (Strength, Dexterity, Thorns, etc.).
    m = re.match(r"[Gg]ain\s+(\d+)\s+(\w+)(\s+this\s+turn)?", s)
    if m and m.group(2) not in {"Block", "Energy"}:
        scope = "this_turn" if m.group(3) else "permanent"
        effects.append({"kind": "gain_status", "status": m.group(2),
                        "amount": int(m.group(1)), "scope": scope})
        return effects, unparsed

    # --- Lose HP: "Lose N HP." ---------------------------------------------
    m = re.match(r"Lose\s+(\d+)\s+HP", s, re.I)
    if m:
        effects.append({"kind": "lose_hp", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Draw N cards ------------------------------------------------------
    m = re.match(r"Draw\s+(\d+)\s+cards?", s, re.I)
    if m:
        effects.append({"kind": "draw", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Gain N Energy -----------------------------------------------------
    m = re.match(r"Gain\s+(\d+)\s+Energy", s, re.I)
    if m:
        effects.append({"kind": "gain_energy", "amount": int(m.group(1))})
        return effects, unparsed

    # --- Add a copy of this card into <Pile> -------------------------------
    m = re.search(r"Add a copy of this card into your\s+(Discard|Hand|Draw)\s+Pile", s, re.I)
    if m:
        effects.append({"kind": "add_copy", "where": m.group(1).lower()})
        return effects, unparsed

    # --- Exhaust the top card of your Draw Pile ----------------------------
    if re.search(r"Exhaust\s+the\s+top\s+card", s, re.I):
        effects.append({"kind": "exhaust_target", "where": "draw_top"})
        return effects, unparsed

    # --- Plain "Exhaust." (self-exhaust on play) ---------------------------
    if s.strip().lower() == "exhaust":
        effects.append({"kind": "exhaust_self"})
        return effects, unparsed

    # --- "Exhaust N card(s) [in your Hand]" / "Exhaust your Hand" ----------
    m = re.match(r"[Ee]xhaust\s+(\d+|all)\s+cards?(?:\s+in\s+your\s+(\w+))?", s, re.I)
    if m:
        n_str = m.group(1).lower()
        where = (m.group(2) or "hand").lower()
        effects.append({
            "kind": "exhaust_cards",
            "amount": "all" if n_str == "all" else int(n_str),
            "from": where,
        })
        return effects, unparsed
    if re.match(r"[Ee]xhaust\s+your\s+(Hand|Discard|Draw)", s):
        m = re.match(r"[Ee]xhaust\s+your\s+(\w+)", s)
        effects.append({"kind": "exhaust_cards", "amount": "all",
                        "from": m.group(1).lower()})
        return effects, unparsed
    m = re.match(r"[Ee]xhaust\s+all\s+non-Attack\s+cards", s)
    if m:
        effects.append({"kind": "exhaust_cards", "amount": "all_non_attack",
                        "from": "hand"})
        return effects, unparsed

    # --- "hits twice" / "hits N times" (damage modifier following Deal X) --
    m = re.match(r"[Hh]its?\s+twice|[Hh]its?\s+(\d+)\s+times?", s)
    if m:
        n = 2 if "twice" in s.lower() else int(m.group(1))
        effects.append({"kind": "hits_modifier", "times": n})
        return effects, unparsed

    # --- "Hits an additional time for each <X>" — scaling hits -------------
    m = re.match(r"[Hh]its?\s+an?\s+additional\s+time\s+for\s+each\s+(.+)", s)
    if m:
        effects.append({"kind": "hits_scaling", "per": m.group(1).strip()})
        return effects, unparsed

    # --- "Put a card from your Discard Pile on top of your Draw Pile" -----
    m = re.match(r"Put a card from your\s+(\w+)\s+Pile\s+on top of your\s+(\w+)\s+Pile", s, re.I)
    if m:
        effects.append({"kind": "move_card", "from": m.group(1).lower(),
                        "to": m.group(2).lower(), "position": "top"})
        return effects, unparsed

    # --- "Upgrade a card in your Hand" / "Upgrade ALL cards in your Hand" --
    m = re.match(r"Upgrade\s+(a|ALL|all)\s+cards?\s+in\s+your\s+(\w+)", s, re.I)
    if m:
        effects.append({"kind": "upgrade_cards",
                        "amount": "all" if m.group(1).lower() == "all" else 1,
                        "from": m.group(2).lower()})
        return effects, unparsed

    # --- "Draw cards until you draw a non-Attack card" --------------------
    if re.match(r"Draw\s+cards?\s+until\s+you\s+draw", s, re.I):
        effects.append({"kind": "draw_until", "raw": s})
        return effects, unparsed

    # --- "Deals N additional damage for each <X>." — scaling damage --------
    m = re.match(r"Deals?\s+(\d+)\s+additional\s+damage\s+for\s+each\s+(.+)", s, re.I)
    if m:
        effects.append({"kind": "scaling_damage", "per": int(m.group(1)),
                        "per_what": m.group(2).strip()})
        return effects, unparsed

    # --- "Deal damage equal to your <X>." (Block-based finishers etc.) ----
    m = re.match(r"Deal\s+damage\s+equal\s+to\s+your\s+(\w+)", s, re.I)
    if m:
        effects.append({"kind": "deal_damage_equal_to",
                        "source": m.group(1).lower()})
        return effects, unparsed

    # --- "Play the top X (or N) cards of your Draw Pile." -----------------
    m = re.match(r"Play\s+the\s+top\s+(X|\d+|X\+1)\s+cards?\s+of\s+your\s+Draw\s+Pile", s, re.I)
    if m:
        n = m.group(1)
        effects.append({"kind": "play_top_n", "count": n})
        return effects, unparsed

    # --- "Skills cost 0/X Energy" — power-style cost mods ------------------
    m = re.match(r"(Attacks?|Skills?|Powers?)\s+cost\s+(\d+)", s, re.I)
    if m:
        effects.append({"kind": "cost_mod", "type": m.group(1),
                        "new_cost": int(m.group(2))})
        return effects, unparsed

    # --- "Can only be played if you have N or more cards in your X Pile" --
    m = re.match(r"Can only be played if\s+you\s+have\s+(\d+).*?in\s+your\s+(\w+)\s+Pile", s, re.I)
    if m:
        effects.append({"kind": "play_condition",
                        "min_cards_in": m.group(2).lower(),
                        "amount": int(m.group(1))})
        return effects, unparsed

    # --- "Add a copy of the third Attack you play each turn into Hand" ----
    # Treated as an on_play_attack trigger that adds a copy at the third trigger.
    m = re.match(r"Add a copy of the third\s+(Attack|Skill|Power)\s+you play each turn into your\s+(\w+)", s, re.I)
    if m:
        effects.append({"kind": "on_nth_play_add_copy",
                        "type": m.group(1), "n": 3,
                        "where": m.group(2).lower()})
        return effects, unparsed

    # --- Keywords ----------------------------------------------------------
    if s.strip().lower() in {"innate", "retain", "ethereal", "unplayable"}:
        effects.append({"kind": s.strip().lower()})
        return effects, unparsed

    # --- "Play the top card of your Draw Pile [and Exhaust it]." ----------
    if re.search(r"Play\s+the\s+top\s+card", s, re.I):
        effects.append({"kind": "play_top_card",
                        "then_exhaust": bool(re.search(r"Exhaust", s, re.I))})
        return effects, unparsed

    # --- Triggers (these wrap an inner effect list) ------------------------
    m = re.match(r"At the (start|end) of your turn,\s+(.+)", s, re.I)
    if m:
        when = f"on_turn_{m.group(1).lower()}"
        inner, ux = _parse_sentence(m.group(2))
        effects.append({"kind": when, "effects": inner})
        unparsed += ux
        return effects, unparsed

    m = re.match(r"Whenever\s+(?:you\s+)?(.+?),\s+(.+)", s, re.I)
    if m:
        trigger = m.group(1).strip().lower()
        if "lose hp" in trigger:
            kind = "on_lose_hp"
        elif "exhaust" in trigger:
            kind = "on_exhaust"
        elif "play" in trigger and "attack" in trigger:
            kind = "on_play_attack"
        else:
            kind = "on_other_trigger"
        inner, ux = _parse_sentence(m.group(2))
        effects.append({"kind": kind, "trigger_raw": trigger, "effects": inner})
        unparsed += ux
        return effects, unparsed

    # --- Conditional "If <cond>, <then>." (no else branch in STS card text)
    m = re.match(r"If\s+(.+?),\s+(.+)", s, re.I)
    if m:
        cond = m.group(1).strip()
        then_inner, ux = _parse_sentence(m.group(2))
        effects.append({"kind": "if", "condition": cond, "then": then_inner})
        unparsed += ux
        return effects, unparsed

    # --- "Increase this card's damage by N this combat." -------------------
    m = re.search(r"[Ii]ncrease this card'?s damage by\s+(\d+)\s+this combat", s)
    if m:
        effects.append({"kind": "self_buff_combat", "amount": int(m.group(1))})
        return effects, unparsed

    # --- "Double the enemy's <Status>." ------------------------------------
    m = re.search(r"Double the enemy'?s\s+(\w+)", s, re.I)
    if m:
        effects.append({"kind": "double_status", "status": m.group(1), "target": "enemy"})
        return effects, unparsed

    # --- "Cost changes from N to M" (upgrade-only meta) --------------------
    if "Cost changes from" in s:
        effects.append({"kind": "upgrade_changes_cost"})
        return effects, unparsed

    # --- Conjunctions ("and ..." in some sentences) — try split on " and " -
    if " and " in s:
        left, right = s.split(" and ", 1)
        l, lx = _parse_sentence(left)
        r, rx = _parse_sentence(right)
        if l or r:  # at least one half parsed
            effects.extend(l + r)
            unparsed.extend(lx + rx)
            return effects, unparsed

    unparsed.append(s)
    return effects, unparsed


def parse_card_text(text: str) -> tuple[list[Effect], list[str]]:
    """Split text into period-delimited sentences and parse each.
    Returns (all_effects, all_unparsed_sentences)."""
    if not text:
        return [], []
    sentences = [s.strip() for s in re.split(r"\.\s+(?=[A-Z])|\.$", text) if s.strip()]
    all_eff: list[Effect] = []
    all_unp: list[str] = []
    for s in sentences:
        e, u = _parse_sentence(s)
        all_eff.extend(e)
        all_unp.extend(u)
    return all_eff, all_unp


def parse_card(card: dict) -> dict:
    """Take a card dict (from data/ironclad_cards.json) and add 'parsed' with
    {normal: [effects], upgraded: [effects], unparsed_normal, unparsed_upgraded}."""
    n_eff, n_unp = parse_card_text(card.get("normal_text", ""))
    u_eff, u_unp = parse_card_text(card.get("upgraded_text", ""))
    return {
        **card,
        "parsed": {
            "normal": n_eff,
            "upgraded": u_eff,
            "unparsed_normal": n_unp,
            "unparsed_upgraded": u_unp,
        },
    }


def parse_card_db(path_in: str = "data/ironclad_cards.json",
                  path_out: str = "data/ironclad_cards_parsed.json") -> dict:
    with open(path_in) as f:
        data = json.load(f)
    parsed = [parse_card(c) for c in data["cards"]]
    out = {"cards": parsed, "errors": data.get("errors", [])}
    with open(path_out, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out


# ─── unit-tests (run as __main__) ────────────────────────────────────────────
TESTS = [
    # (text, expected_effect_kinds_in_order)
    ("Deal 6 damage.",                          ["deal_damage"]),
    ("Deal 10 damage. Apply 3 Vulnerable.",     ["deal_damage", "apply_status"]),
    ("Gain 8 Block. Draw 1 card.",              ["gain_block", "draw"]),
    ("Deal 5 damage to ALL enemies X times.",   ["multi_hit"]),
    ("Deal 8 damage. Exhaust.",                 ["deal_damage", "exhaust_self"]),
    ("Gain 2 Strength this turn.",              ["gain_status"]),
    ("Lose 1 HP. Whenever you lose HP on your turn, deal 6 damage to ALL enemies.",
                                                ["lose_hp", "on_lose_hp"]),
    ("At the start of your turn, lose 1 HP.",   ["on_turn_start"]),
    ("If the enemy is Vulnerable, hits twice.", ["if"]),
    ("Innate.",                                 ["innate"]),
    ("Add a copy of this card into your Discard Pile.",
                                                ["add_copy"]),
    ("Increase this card's damage by 5 this combat.",
                                                ["self_buff_combat"]),
    ("Whenever a card is Exhausted, gain 3 Block.",
                                                ["on_exhaust"]),
    ("Double the enemy's Vulnerable.",          ["double_status"]),
    ("Gain 1 Energy.",                          ["gain_energy"]),
]


def run_tests() -> tuple[int, int]:
    passed = failed = 0
    for text, expected in TESTS:
        eff, _ = parse_card_text(text)
        kinds = [e["kind"] for e in eff]
        if kinds == expected:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {text!r}")
            print(f"    expected: {expected}")
            print(f"    got:      {kinds}")
    return passed, failed


if __name__ == "__main__":
    print("=== Unit tests ===")
    p, f = run_tests()
    print(f"  {p}/{p+f} passed")
    if f:
        raise SystemExit(1)
    print()
    print("=== Parse full card DB ===")
    out = parse_card_db()
    cards = out["cards"]
    # Coverage stats
    total = len(cards)
    has_normal_effects = sum(1 for c in cards if c["parsed"]["normal"])
    has_upgraded_effects = sum(1 for c in cards if c["parsed"]["upgraded"])
    fully_parsed_normal = sum(1 for c in cards
                              if c["parsed"]["normal"] and not c["parsed"]["unparsed_normal"])
    fully_parsed_upgraded = sum(1 for c in cards
                                if c["parsed"]["upgraded"] and not c["parsed"]["unparsed_upgraded"])
    print(f"  {total} cards parsed")
    print(f"  Normal:   {has_normal_effects} have ≥1 parsed effect, "
          f"{fully_parsed_normal} fully parsed (no residual)")
    print(f"  Upgraded: {has_upgraded_effects} have ≥1 parsed effect, "
          f"{fully_parsed_upgraded} fully parsed (no residual)")
    # Sample unparsed sentences for inspection
    print()
    print("=== Sample unparsed (top 20 unique residuals) ===")
    from collections import Counter
    bag = Counter()
    for c in cards:
        for s in c["parsed"]["unparsed_normal"] + c["parsed"]["unparsed_upgraded"]:
            bag[s] += 1
    for s, n in bag.most_common(20):
        print(f"  ×{n}: {s[:120]}")
