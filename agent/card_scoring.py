"""
card_scoring.py — Heuristic card scoring for deck-building decisions.

Scores each card 0-10 based on damage/block/cost efficiency, special effects,
and synergy potential. Used by greedy_action() for card_reward, shop, and
card_select (removal) decisions.
"""

# Manual overrides for known high/low value cards.
# Cards not listed get scored by the generic formula.
OVERRIDES: dict[str, float] = {
    # === Ironclad top picks (9-10) ===
    "FEED": 9.5,            # kill an enemy → heal + max HP
    "DEMON_FORM": 9.5,      # +2 str/turn is game-winning
    "CORRUPTION": 9.0,      # skills cost 0, insane with dead branch
    "BRIMSTONE": 9.0,       # +2 str/turn, low downside for Ironclad
    "LIMIT_BREAK": 9.0,     # double strength
    "IMPERVIOUS": 9.0,      # 30 block
    "OFFERING": 8.5,        # draw + energy, life cost is fine
    "DARK_EMBRACE": 8.5,    # draw on exhaust synergy
    "FEEL_NO_PAIN": 8.5,    # block on exhaust
    "JUGGERNAUT": 8.5,      # each block = damage — game-winning in block-heavy decks
    "SENTINEL": 8.0,        # 3 energy if exhausted
    "SECOND_WIND": 8.0,     # exhaust + block
    "TRUE_GRIT": 7.5,       # exhaust 1, draw 2
    "WARCRY": 7.0,          # draw with no downside
    "SHRUG_IT_OFF": 8.0,    # 8 block + draw 1
    "SPOT_WEAKNESS": 6.5,   # +3 str if enemy attacking — strong conditional
    "BRUTALITY": 5.5,       # 0-cost draw at 1 HP/turn — aggressive cycle

    # === STS2 new cards — top picks (8-9) ===
    "HOWL_FROM_BEYOND": 9.0,   # AOE that auto-replays from exhaust pile each turn
    "APOTHEOSIS": 9.0,         # upgrade ALL cards (colorless) — enormous value
    "AGGRESSION": 8.5,         # power: free upgraded Attack from discard each turn
    "ADRENALINE": 8.0,         # 0-cost: gain 1 energy + draw 2 (no HP cost in STS2)
    "SHOCKWAVE": 7.5,          # AOE Weak + Vulnerable to all enemies — huge setup
    "FORGOTTEN_RITUAL": 8.0,   # gain energy when exhaust — capped (generic formula over-scores)
    "HELLRAISER": 8.0,         # auto-plays Strike on draw — free attacks all game
    "MOLTEN_FIST": 8.0,        # doubles target's Vulnerable — huge damage multiplier
    "DISMANTLE": 7.5,          # 13 dmg; hits twice if Vulnerable (26 for 1 energy)
    "BLADE_OF_INK": 6.0,       # this turn: +1 str per Attack — scales with attack-heavy turns
    "POMMEL_STRIKE": 7.5,      # 9 dmg + draw 1 (generic formula gives 8.5, slightly lower)
    "UNRELENTING": 7.5,        # 12 dmg + next Attack costs 0 — energy generation
    "BLUDGEON": 7.5,           # simple heavy single-target damage
    "SETUP_STRIKE": 7.0,       # 9 dmg + temporary strength this turn
    "GRAPPLE": 7.0,            # dmg + deals extra per block gained this turn
    "VICIOUS": 6.5,            # draw when you apply Vulnerable
    "INFERNAL_BLADE": 7.0,     # add free random Attack to hand
    "PERFECTED_STRIKE": 6.5,   # scales with Strike count (good early, less later)
    "CINDER": 5.5,             # dmg + forced exhaust top of draw (can trash good card)
    "BLOOD_WALL": 4.0,         # HP loss for block — net negative, avoid
    "BREAKTHROUGH": 6.0,       # AOE but costs HP — trade-off
    "EVIL_EYE": 6.0,           # block + extra block on exhaust — conditional
    "DOMINATE": 6.5,           # apply Vuln + gain Str per Vuln on enemy — strong offensive
    "TAUNT": 7.0,              # block + apply Vulnerable — efficient dual-purpose skill
    "JUGGLING": 5.5,           # power: add copy of 3rd Attack played each turn — free attacks
    "EXPECT_A_FIGHT": 5.0,     # gain 1 energy per Attack in hand — strong in attack-heavy decks
    "ASHEN_STRIKE": 5.0,       # damage scales with exhaust pile — strong with exhaust synergy
    "TREMBLE": 4.5,            # 1-cost Apply Vulnerable — combo piece with attack-heavy deck

    # === Strong cards (7-8) ===
    "CARNAGE": 7.5,         # 20 damage for free (ethereal)
    "RECKLESS_CHARGE": 7.0, # 7 dmg + wound, still good DPE
    "CLEAVE": 7.0,          # AOE 8 damage
    "UPPERCUT": 7.5,        # 13 dmg + vuln + weak
    "PUMMEL": 7.0,          # multi-hit with str scaling
    "BURNING_PACT": 7.0,   # draw 2 + exhaust
    "INFLAME": 8.0,         # +2 str (power, persistent)
    "METALLICIZE": 7.5,     # +3 block/turn (power)
    "BATTLE_TRANCE": 7.0,   # draw 3
    "BLOODLETTING": 7.0,    # free energy
    "RAGE": 7.0,            # block on attack plays
    "DROPKICK": 8.0,        # 5 dmg + draw + energy if vuln target
    "WHIRLWIND": 7.0,       # X-cost AOE
    "HEAVY_BLADE": 7.0,     # scales with str (3x multiplier)
    "SWORD_BOOMERANG": 7.0, # 3x3 random hits

    # === STS2 colorless decent picks (5-7) ===
    "DRAMATIC_ENTRANCE": 7.0,  # free AOE damage on combat start (Ethereal, costs 0)
    "METAMORPHOSIS": 6.5,   # add N free Attacks to draw pile — extra resources
    "BEAT_INTO_SHAPE": 6.5, # damage that scales with hits this turn — strong multi-hit
    "PANACHE": 6.0,         # power: AOE damage every 5 cards played
    "MASTER_OF_STRATEGY": 6.0, # draw 3, exhaust — card advantage
    "ANGER": 5.0,           # damage + copy in discard (scaling but clogs deck)
    "DARK_SHACKLES": 4.5,   # reduce enemy strength this turn — situational defensive
    "BLIGHT_STRIKE": 5.0,   # damage + apply Doom — Doom may not tick in headless (BUG-021)
    "DISCOVERY": 5.5,       # choose 1 of 3 random cards free — card quality upgrade
    "JACK_OF_ALL_TRADES": 5.0,  # add random colorless cards — inconsistent

    # === Decent cards (5-6) ===
    "ANGELIC_DEW": 5.5,
    "ARMAMENTS": 6.0,       # upgrade hand
    "BASH": 6.0,            # starter but vulnerable is good
    "BLOOD_FOR_BLOOD": 6.0,
    "BODY_SLAM": 6.5,       # scales with block
    "CLOTHESLINE": 5.5,     # 12 dmg + weak
    "COMBUST": 5.5,
    "ENTRENCH": 5.5,
    "FIEND_FIRE": 6.0,
    "FLEX": 5.0,
    "HAVOC": 5.5,
    "HEADBUTT": 5.5,        # 9 dmg + put card back on draw
    "HEMOKINESIS": 6.0,
    "IRON_WAVE": 5.5,       # 5 dmg + 5 block for 1
    "POWER_THROUGH": 6.0,  # 15 block, adds wounds
    "PUNCTURE": 5.0,
    "RAMPAGE": 5.5,
    "SEARING_BLOW": 5.0,   # needs upgrades
    "SEVER_SOUL": 5.5,
    "THUNDERCLAP": 7.0,    # AOE damage + Vulnerable to ALL enemies (strong setup)
    "TWIN_STRIKE": 6.0,    # 5x2 = 10 for 1
    "WILD_STRIKE": 5.5,    # 12 dmg, wound to draw

    # === Mediocre (3-4) ===
    "DEFEND": 3.0,          # basic, want to remove
    "DEFEND_IRONCLAD": 3.0,
    "DEFEND_SILENT": 3.0,
    "DEFEND_DEFECT": 3.0,
    "STRIKE": 2.0,          # basic, want to remove
    "STRIKE_IRONCLAD": 2.0,
    "STRIKE_SILENT": 2.0,
    "STRIKE_DEFECT": 2.0,
    "WOUND": 1.0,           # pure bad
    "DAZE": 1.0,
    "SLIMED": 1.0,
    "BURN": 0.5,
}

# Type-based generic scoring weights
ATTACK_DPE_BASELINE = 6.0   # Strike: 6 dmg / 1 cost
BLOCK_BPE_BASELINE = 5.0    # Defend: 5 block / 1 cost
DRAW_VALUE = 1.5            # per card drawn
ENERGY_VALUE = 4.0          # per energy generated
VULN_WEAK_VALUE = 1.5       # vulnerable or weak bonus
STRENGTH_VALUE = 2.5        # per point of strength
DEXTERITY_VALUE = 2.0       # per point of dexterity
EXHAUST_VALUE = 1.0         # exhaust synergy bonus
AOE_VALUE = 1.5             # AOE bonus
POWER_BONUS = 1.5           # powers persist, extra value
RARITY_BONUS = {"Common": 0.0, "Uncommon": 0.5, "Rare": 1.0}

# Cards that are never worth picking (status/curse starters)
SKIP_IDS = {"STRIKE_R", "DEFEND_R", "STRIKE_B", "DEFEND_B", "STRIKE_G",
            "DEFEND_G", "WOUND", "DAZE", "SLIMED", "BURN", "DECAY",
            # STS2 naming variants
            "STRIKE_IRONCLAD", "DEFEND_IRONCLAD",
            "STRIKE_SILENT", "DEFEND_SILENT",
            "STRIKE_DEFECT", "DEFEND_DEFECT",
            # STS2 curses/status cards
            "DOUBT", "REGRET", "SHAME", "VOID", "NORMALITY"}


def score_card(card: dict) -> float:
    """Score a card 0-10 based on its effectiveness.

    card: dict with keys: id, cost, type, rarity, stats, name, keywords
    """
    card_id = card.get("id", "")
    if isinstance(card_id, dict):
        card_id = card_id.get("en", str(card_id))
    card_id = card_id.upper().strip()
    # Game prefixes IDs with "CARD." — strip it for override lookups
    if card_id.startswith("CARD."):
        card_id = card_id[5:]

    # Check manual override first
    if card_id in OVERRIDES:
        return OVERRIDES[card_id]

    # Skip status/curse cards
    if card_id in SKIP_IDS:
        return 0.0

    cost = max(card.get("cost", 1), 1)  # treat 0-cost as 1 for ratio calc
    ctype = (card.get("type") or "").lower()
    rarity = card.get("rarity", "Common")
    stats = card.get("stats") or {}
    name = card.get("name", "")
    if isinstance(name, dict):
        name = name.get("en", "")

    score = 0.0

    # === Attack cards: score by damage per energy ===
    if ctype == "attack":
        damage = stats.get("damage", 0)
        if damage > 0:
            dpe = damage / cost
            score += min(dpe / ATTACK_DPE_BASELINE * 5.0, 7.0)

    # === Skill cards: score by block, draw, energy ===
    elif ctype == "skill":
        block = stats.get("block", 0)
        if block > 0:
            bpe = block / cost
            score += min(bpe / BLOCK_BPE_BASELINE * 4.0, 5.0)

    # === Power cards: base value + effect scoring ===
    elif ctype == "power":
        score += POWER_BONUS
        # Powers with str/dex are very strong
        if "strength" in str(stats).lower() or "str" in str(stats).lower():
            score += STRENGTH_VALUE
        if "dexterity" in str(stats).lower() or "dex" in str(stats).lower():
            score += DEXTERITY_VALUE

    # === Generic bonuses ===
    # Draw value
    draw = stats.get("draw", 0)
    if draw > 0:
        score += draw * DRAW_VALUE

    # Energy generation
    energy = stats.get("energy", 0)
    if energy > 0:
        score += energy * ENERGY_VALUE

    # Block on any card (not just skills)
    if ctype != "skill":
        block = stats.get("block", 0)
        if block > 0:
            score += min(block / cost * 0.5, 3.0)

    # Damage on non-attack cards
    if ctype != "attack":
        damage = stats.get("damage", 0)
        if damage > 0:
            score += min(damage / cost * 0.5, 3.0)

    # Check keywords/description for effects
    desc = str(card.get("description", "")).lower()
    if "vulnerable" in desc:
        score += VULN_WEAK_VALUE
    if "weak" in desc:
        score += VULN_WEAK_VALUE
    if "strength" in desc and ctype != "power":
        score += STRENGTH_VALUE * 0.5
    if "exhaust" in desc:
        score += EXHAUST_VALUE
    if "all enem" in desc:
        score += AOE_VALUE
    if "draw" in desc and draw == 0:
        score += DRAW_VALUE  # draw mentioned but not in stats

    # Rarity bonus
    score += RARITY_BONUS.get(rarity, 0.0)

    # Clamp to 0-10
    return max(0.0, min(score, 10.0))


def pick_best_card(cards: list[dict], threshold: float = 3.5) -> int | None:
    """Return index of the best card above threshold, or None to skip."""
    if not cards:
        return None
    scored = [(i, score_card(c)) for i, c in enumerate(cards)]
    scored.sort(key=lambda x: x[1], reverse=True)
    best_idx, best_score = scored[0]
    if best_score >= threshold:
        return best_idx
    return None


def pick_worst_card(cards: list[dict], threshold: float = 5.0) -> int | None:
    """Return index of the worst card below threshold for removal, or None."""
    if not cards:
        return None
    scored = [(i, score_card(c)) for i, c in enumerate(cards)]
    scored.sort(key=lambda x: x[1])
    worst_idx, worst_score = scored[0]
    if worst_score < threshold:
        return worst_idx
    return None
