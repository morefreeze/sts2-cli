"""
combat_env.py — gymnasium.Env for STS2 combat training.

Also exports greedy_action(state) as a module-level function for use by
coordinator.py and _advance_to_combat().

Episode = one single combat (not a full game run).
Reward shaping: per-step damage/block/kill signals + end-of-combat bonus.

Design: simple 1:1 mapping — each env.step() = one game action (including
end_turn). No auto-skip. Policy and value networks are separated in train.py
to prevent value-loss gradient from corrupting policy on forced end_turn steps.
"""
import fcntl, hashlib, json, math, os, subprocess, random, time, select, sys, warnings
import gymnasium as gym
import numpy as np
from gymnasium.spaces import Box, Discrete
from agent.state_encoder import StateEncoder
from agent.strategy import Act1SafeStrategy, HpAwareMapStrategy, MapStrategy, rest_site_action
from agent.card_scoring import (score_card, score_card_in_deck, pick_best_card,
                                  pick_worst_card, deck_quality_score,
                                  is_act1_card_reward_eligible, _card_id_norm)
from agent.decision_advisor import DecisionAdvisor
from agent.run_decisions import append_run_decision, capture_run_decision

# Swappable map strategy — change globally via set_map_strategy()
_map_strategy: MapStrategy = HpAwareMapStrategy()
_decision_advisor = DecisionAdvisor()

_RUN_DECISION_REPLY_MAX_KEYS = 256
_RUN_DECISION_STATE_MAX_DEPTH = 8
_RUN_DECISION_STATE_MAX_NODES = 16_384
_RUN_DECISION_STATE_MAX_DICT_ITEMS = 512
_RUN_DECISION_STATE_MAX_LIST_ITEMS = 4_096
_RUN_DECISION_STATE_MAX_KEY_CHARS = 256
_RUN_DECISION_STATE_MAX_STRING_CHARS = 16_384
_RUN_DECISION_STATE_MAX_BYTES = 512 * 1024
_RUN_DECISION_FINGERPRINT_OMITTED = object()
_RUN_MAP_CAPTURE_METADATA_OMITTED = object()


class _RunMapTransitionError(ValueError):
    """A validated map refresh would make the retained route impossible."""


def _decision_advisor_enabled() -> bool:
    flag = os.environ.get("STS2_DECISION_ADVISOR", "").strip().lower()
    return flag in {"1", "true", "on", "yes"}


def _card_quality_gate_enabled() -> bool:
    flag = os.environ.get("STS2_CARD_QUALITY_GATE", "1").strip().lower()
    return flag in {"1", "true", "on", "yes"}


def set_map_strategy(strategy: MapStrategy):
    """Replace the global map strategy. Call before training or evaluation."""
    global _map_strategy
    _map_strategy = strategy

def _find_dotnet():
    """Return a command prefix list for .NET SDK. On Apple Silicon, prefers ARM64 dotnet."""
    import platform
    candidates = [
        os.path.expanduser("~/.dotnet-arm64/dotnet"),
        os.path.expanduser("~/.dotnet/dotnet"),
        "/usr/local/share/dotnet/dotnet",
        "dotnet",
    ]
    for p in candidates:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return [p]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    # On macOS ARM64, try arch -arm64 dotnet to load ARM64 managed assemblies
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["arch", "-arm64", "dotnet", "--version"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return ["arch", "-arm64", "dotnet"]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return ["dotnet"]

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")


# Hand-curated relic tier for boss-clear focus (Act 1 boss). Values are added
# on top of the keyword-derived score below. Picked to address the bottleneck
# "reach boss at HP≥75 + survive boss combat":
#   S (+6.0): directly raises boss-entry HP buffer or boss-fight throughput
#   A (+4.0): strong general value across Act 1 progression
#   F (-6.0): traps that look ok but hurt boss-clear (e.g. cost an action slot
#            with negligible boss-fight upside)
_RELIC_TIER_OVERRIDE = {
    # S-tier — clutch contributors to boss-clear
    "VAJRA": 6.0,                   # +1 Strength permanent
    "RED_SKULL": 6.0,               # +3 Strength when HP < 50% (boss usually triggers this)
    "ANCHOR": 6.0,                  # start each combat with +10 Block (HP buffer)
    "HORN_CLEAT": 6.0,              # turn 2 +14 Block (boss prep)
    "MEAT_ON_THE_BONE": 6.0,        # heal to 12 HP when low — saves boss-runs
    "CHAMPION_BELT": 6.0,           # vulnerable cards apply weak too — Bash combo
    "ORANGE_PELLETS": 6.0,          # cleanse all debuffs once per turn after 3 card types
    "ICE_CREAM": 6.0,               # energy carries over — massive boss-burst potential

    # A-tier — strong general progression
    "PHILOSOPHERS_STONE": 4.0,      # +1 energy/turn (energy relic family)
    "LANTERN": 4.0,                 # +1 starting energy per combat
    "DAMARU": 4.0,                  # similar energy buff
    "BAG_OF_PREPARATION": 4.0,      # 2 extra cards turn 1 (helps boss opener)
    "POCKETWATCH": 4.0,             # bonus draws on light-card turns
    "TUNGSTEN_ROD": 4.0,            # all damage taken reduced by 1
    "TORII": 4.0,                   # tiny attacks → 1 damage
    "BIRD_FACED_URN": 4.0,          # heal 2 HP when playing a Power
    "ART_OF_WAR": 4.0,              # +1 energy if no Attack played last turn
    "PAPER_PHROG": 4.0,             # Vulnerable becomes +75% instead of +50%
    "MERCURY_HOURGLASS": 4.0,       # 3 damage to all enemies start of turn
    "MAGIC_FLOWER": 4.0,            # healing +50%
    "CALIPERS": 4.0,                # block decay -15
    "VELVET_CHOKER": 4.0,           # +1 energy but cap at 6 cards played
    "BLOOD_VIAL": 4.0,              # heal 2 HP at start of combat
    "WHISPERING_EARRING": 4.0,      # double gold from kills

    # F-tier — looks ok but bad ROI for boss-focused builds
    "MARK_OF_PAIN": -6.0,           # adds Wounds, hard cost on small decks
    "RUNIC_PYRAMID": -6.0,          # discard skipped — Ironclad doesn't benefit
    "FUSION_HAMMER": -6.0,          # +1 energy but can't rest
    "DARKSTONE_PERIAPT": -6.0,      # Curse-trigger relic, edge case
    "ECTOPLASM": -6.0,              # +1 energy, no gold from combats
    "DEAD_BRANCH": -3.0,            # adds random cards — synergy-specific
    "TINY_HOUSE": -3.0,             # one-time event, low boss impact
}


def _score_shop_relic(relic: dict) -> float:
    """Score a shop relic for purchase desirability. Returns 0 if not worth buying.

    Combines keyword heuristic with hand-curated `_RELIC_TIER_OVERRIDE` for
    cases where Ironclad meta knowledge supersedes regex. The override is
    additive so the keyword backbone still picks up unseen relics correctly.
    """
    name_raw = relic.get("name") or {}
    name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
    desc_raw = relic.get("description") or {}
    desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
    text = name + " " + desc

    # Tier override applied first — uses runtime ID match
    rid = str(relic.get("id") or "").upper().replace("-", "_").replace(" ", "_")
    tier_bonus = _RELIC_TIER_OVERRIDE.get(rid, 0.0)

    # Hard pass on these
    if "curse" in text: return -5.0
    if "lose max" in text or "maximum hp" in text: return -3.0

    score = 3.0  # baseline: relics are generally useful
    # Big positive: per-turn stat gains
    if "each turn" in text and ("strength" in text or "vigor" in text): score += 4.0
    if "each turn" in text and ("block" in text or "dexterity" in text): score += 3.0
    if "each combat" in text and ("block" in text or "armor" in text): score += 3.0
    if "start of combat" in text and ("strength" in text or "dexterity" in text): score += 3.0
    # Strength bonus (Red Skull type: gives +str) — skip if relic causes str loss
    if "strength" in text and "lose" not in text: score += 2.0
    # Vulnerable multiplier enhancement (Paper Phrog: +25% vs vulnerable)
    if "vulnerable" in text and "enem" not in text: score += 2.0
    # Draw effects
    if "draw" in text and "card" in text: score += 2.0
    # Energy per combat start
    if "energy" in text and "start" in text: score += 3.0
    # Healing
    if "heal" in text or ("rest" in text and "hp" in text): score += 2.0
    # Max HP
    if "max hp" in text and ("gain" in text or "raise" in text or "increase" in text): score += 3.0
    # Gold generation
    if "gold" in text and ("gain" in text or "additional" in text or "drop" in text): score += 1.0
    # Exhaust synergy
    if "exhaust" in text: score += 1.5
    # Potion slots (more potions = more options)
    if "potion" in text and "slot" in text: score += 1.5
    # Card upgrade (powerful long-term)
    if "upgrade" in text and "card" in text: score += 2.0
    # Bad: adds wounds or curses
    if "wound" in text and "add" in text: score -= 2.0
    # Bad: enemies gain strength/buffs (e.g. Philosopher's Stone)
    if "enem" in text and "strength" in text: score -= 3.0
    # Bad: HP costs (e.g. Runic Dome, Sozu)
    if "hp" in text and "lose" in text and "start" in text: score -= 2.0
    return score + tier_bonus


def _score_shop_potion(potion: dict) -> float:
    """Score a shop potion for purchase desirability."""
    name_raw = potion.get("name") or {}
    name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
    desc_raw = potion.get("description") or {}
    desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
    text = name + " " + desc

    score = 3.0  # baseline: potions are generally useful
    if "strength" in text: score += 3.0       # Strength/Flex Potion — huge for bosses
    if "dexterity" in text: score += 2.0       # Speed Potion
    if "duplicate" in text: score += 3.0       # Duplication Potion
    if "draw" in text and "card" in text: score += 2.0   # Swift Potion
    if "energy" in text: score += 2.0          # Energy Potion
    if "block" in text: score += 1.5           # Block/Fortifier Potion
    if "vulnerable" in text: score += 2.0      # Weak/Vulnerable applier
    if "all enemies" in text: score += 1.5     # AOE damage
    if "exhaust" in text: score += 1.0         # Elixir
    if "artifact" in text: score += 2.0        # Ancient Potion
    if "heal" in text or "hp" in text: score += 2.0      # Health Potion — saves runs
    if "curse" in text: score -= 5.0           # Potion that adds curses
    return score


def _score_event_option(opt: dict) -> float:
    """Score an event option by keyword analysis. Higher = better."""
    import re as _re
    title = (opt.get("title") or "").lower()
    desc = (opt.get("description") or "").lower()
    # Strip rich-text markup ([gold], [red], [/gold]) to prevent false keyword matches
    # e.g. "[gold]ALL[/gold]" causing "lose all gold" to trigger.
    # Keep template vars like {RandomRelic} since their names carry useful semantics.
    raw = title + " " + desc
    text = _re.sub(r'\[/?[a-z0-9_]+\]', ' ', raw)   # markup tags only
    score = 0.0
    # Strong negatives — losing Max HP is permanent and devastating
    # "lose max hp" directly, OR "lose {N} max hp" (lose N max hp), but NOT
    # "lose {potion}. gain ... max hp" (which is gaining max hp after losing a potion)
    _lose_max_hp = ("lose max" in text or "maximum hp" in text or
                    bool(_re.search(r'lose\s+(?:\{[^}]+\}|\d+)\s+max\s*hp', text)))
    if _lose_max_hp:
        score -= 10.0
    if "curse" in text:
        score -= 8.0
    # "lose ALL gold" — losing all gold is very bad
    if "lose" in text and "all" in text and "gold" in text:
        score -= 5.0
    elif "lose" in text and "gold" in text:
        score -= 2.0
    if "torment" in title:
        score -= 5.0  # Neow's Torment adds a negative card
    if "take" in text and "damage" in text:
        score -= 2.0  # one-time HP loss is recoverable; was -3 (too harsh for HP→MaxHP trades)
    if "lose" in text and "hp" in text and "max" not in text:
        score -= 2.0  # "Lose N HP" phrasing (same cost as take damage)
    if "downgrade" in text:
        score -= 4.0  # downgrading cards is very bad
    # Negative: adds basic/weak cards to deck
    if "add" in text and ("additional strike" in text or "additional defend" in text):
        score -= 3.0
    # Strong positives
    if "rare" in text and ("card" in text or "obtain" in text or "random" in text):
        score += 8.0
    elif "uncommon" in text and "card" in text:
        score += 4.0  # uncommon card reward: decent, not as good as rare
    elif "card" in text and ("obtain" in text or "choose" in text) and "curse" not in text:
        score += 2.0  # generic card reward (common): better than nothing
    if "remove" in text and ("card" in text or "deck" in text):
        score += 6.0  # deck thinning = very valuable
    # Removing cards at HP cost (e.g. Precarious Shears): net value reduced vs free removal
    if "remove" in text and ("card" in text or "deck" in text) and "hp" in text and "curse" not in text:
        score -= 3.0
    if "relic" in text and "add" not in text:
        score += 5.0  # relics without downside
    elif "relic" in text:
        score += 2.0  # relics with some downside (e.g. also adds Strike)
    if "upgrade" in text:
        score += 4.0
    if "transform" in text:
        score += 3.0  # transform replaces bad starters with random cards
    # Use word-boundary search for "gain" to avoid "bargain", "again", "regain" false positives
    _gain = bool(_re.search(r'\bgain\b', text))
    if _gain and "gold" in text:
        score += 3.0
    if "max hp" in text and ("raise" in text or "increase" in text or _gain):
        score += 3.0  # gaining max HP is good
    if "potion" in text:
        score += 2.0
    if "heal" in text and "hp" in text:
        score += 2.0
    if "colorless" in text and "card" in text:
        score += 2.0  # colorless cards add utility
    # Permanent stat gains are very strong
    if "strength" in text and _gain and "enem" not in text and "lose" not in text:
        score += 4.0  # +str permanently is game-warping for Ironclad
    elif "strength" in text and _gain and "enem" in text:
        score -= 3.0  # enemies gaining strength is very bad
    if "dexterity" in text and _gain and "enem" not in text and "lose" not in text:
        score += 3.0  # +dex permanently is strong defense
    if "energy" in text and "each turn" in text and "lose" not in text:
        score += 5.0  # extra energy per turn = unlimited scaling
    elif "energy" in text and _gain and "lose" not in text:
        score += 1.5  # one-time energy gain: small but real value
    return score


def greedy_action(state: dict) -> dict:
    """Greedy heuristic for non-combat decisions. Used during training and by coordinator."""
    decision = state.get("decision", "")
    if _decision_advisor_enabled():
        advised = _decision_advisor.choose(state)
        if advised is not None:
            return advised

    if decision == "map_select":
        choices = state.get("choices", [])
        if choices:
            return _map_strategy.choose(state, choices)

    elif decision == "card_reward":
        cards = state.get("cards", [])
        if cards:
            deck = state.get("player", {}).get("deck") or []
            deck_size = state.get("player", {}).get("deck_size", len(deck) or 10)
            floor = state.get("floor") or state.get("context", {}).get("floor", 1)
            in_act2 = isinstance(floor, int) and floor >= 16
            if in_act2:
                threshold = 5.5 if deck_size < 18 else 6.0
            elif deck_size >= 18:
                threshold = 6.5
            else:
                threshold = 5.5
            # Provide game-state context for MC rollout (no-op when STS2_MC_ROLLOUT
            # is off — the v2 predictor path doesn't read it).
            from agent.card_scoring import set_mc_context as _set_mc_ctx
            player = state.get("player", {}) or {}
            # boss_id intentionally NOT passed (Jun 10): two n=30 evals with
            # boss-aware MC scored 3/30 reach each vs 16.7% baseline — the boss
            # eval distorts deck building (glass-cannon drift). Machinery stays
            # in rollout_recursive for gap-zone fine-tune evaluation use.
            _set_mc_ctx(
                hp=int(player.get("hp", 80) or 80),
                max_hp=int(player.get("max_hp", 80) or 80),
                floor=int(floor) if isinstance(floor, (int, float)) and floor > 0 else 5,
                relics=CombatEnv._state_relic_ids(state),
            )
            # Preserve reward indices while filtering late Act 1 deck dilution.
            # Ranking, score thresholds, and broken-card handling remain owned
            # by pick_best_card.
            indexed_cards = list(enumerate(cards))
            late_act1 = (
                not isinstance(floor, bool)
                and isinstance(floor, (int, float))
                and floor >= 12
            )
            if _card_quality_gate_enabled() and late_act1:
                act = state.get("act") or (state.get("context") or {}).get("act")
                indexed_cards = [
                    (original_index, card)
                    for original_index, card in indexed_cards
                    if is_act1_card_reward_eligible(card, deck, act)
                ]
                if not indexed_cards and state.get("can_skip") is False:
                    indexed_cards = list(enumerate(cards))
            eligible_cards = [card for _, card in indexed_cards]
            if not eligible_cards:
                return {"cmd": "action", "action": "skip_card_reward"}
            best_eligible = pick_best_card(
                eligible_cards, threshold=threshold, deck=deck
            )
            if best_eligible is not None:
                best = indexed_cards[best_eligible][0]
                return {"cmd": "action", "action": "select_card_reward",
                        "args": {"card_index": best}}
        return {"cmd": "action", "action": "skip_card_reward"}

    elif decision == "rest_site":
        return rest_site_action(state, state.get("options", []))

    elif decision == "event_choice":
        options = state.get("options", [])
        available = [o for o in options if not o.get("is_locked")]
        if available:
            best = max(available, key=_score_event_option)
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": best["index"]}}
        return {"cmd": "action", "action": "leave_room"}

    elif decision == "bundle_select":
        bundles = state.get("bundles", [])
        if len(bundles) >= 2:
            scores = [sum(score_card(c) for c in b.get("cards", [])) for b in bundles]
            best_idx = scores.index(max(scores))
        else:
            best_idx = 0
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": best_idx}}

    elif decision == "card_select":
        cards = state.get("cards", [])
        if cards:
            room_type = state.get("context", {}).get("room_type", "")
            max_sel = max(state.get("max_select", 1), 1)
            combat_rooms = ("RestSiteRoom", "Boss", "Monster", "Elite", "CombatRoom")
            if "rest" in room_type.lower():
                # SMITH upgrade: always single-card selection, pick best
                best = pick_best_card(cards, threshold=0.0)
                idx = best if best is not None else 0
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            elif room_type in ("Boss", "Monster", "Elite", "CombatRoom") or not room_type:
                # Mid-combat select (potion: pick best; boss mechanic: rare, pick best as heuristic)
                best = pick_best_card(cards, threshold=0.0)
                idx = best if best is not None else 0
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            else:
                # Distinguish "add N from external pool" vs "remove/transform from deck"
                # External event pools (discover/cheese) never contain Strikes/Defends.
                # Deck selections (remove/transform) always include the player's junk cards.
                has_junk = any(score_card(c) < 3.0 for c in cards)
                is_deck_selection = has_junk or len(cards) > 10
                if not is_deck_selection:
                    # External pool (no junk, small-ish): event "add N cards" — pick best N
                    scored_by = sorted(enumerate(cards), key=lambda x: score_card(x[1]), reverse=True)
                else:
                    # Deck selection (remove/transform): pick worst N
                    scored_by = sorted(enumerate(cards), key=lambda x: score_card(x[1]))
                selected = [str(scored_by[k][0]) for k in range(min(max_sel, len(scored_by)))]
                return {"cmd": "action", "action": "select_cards",
                        "args": {"indices": ",".join(selected)}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        held_potions = len(player.get("potions", []))
        floor = state.get("floor") or state.get("context", {}).get("floor", 0)
        removal_cost = state.get("card_removal_cost")
        pre_boss = isinstance(floor, int) and floor >= 11

        # Emergency: buy health potion first when HP is critically low (< 50%)
        # Jun 10 attempted "buy proactive at floor≥6 HP<85%" regressed → reverted.
        if hp_ratio < 0.50 and held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            for sp in shop_potions:
                sp_name = (sp.get("name") or {})
                sp_name = (sp_name.get("en", "") if isinstance(sp_name, dict) else str(sp_name)).lower()
                sp_desc = (sp.get("description") or {})
                sp_desc = (sp_desc.get("en", "") if isinstance(sp_desc, dict) else str(sp_desc)).lower()
                if ("heal" in sp_name + sp_desc or "restore" in sp_name + sp_desc) and "curse" not in sp_name + sp_desc:
                    return {"cmd": "action", "action": "buy_potion",
                            "args": {"potion_index": sp.get("index", 0)}}

        in_act2 = isinstance(floor, int) and floor >= 16
        deck = state.get("player", {}).get("deck", []) or []

        # === Basic-card dead-weight purge (Jun 13) ===
        # Three boss-entry decks all carried 3-5 un-removed Strike/Defend that
        # diluted core-card draw rate. Removal had been gated against buyable
        # card value, so a good shop card blocked removal. Strike/Defend are
        # dead weight at ANY deck size — purge them first, unconditionally,
        # until ≤2 remain.
        n_basic = sum(1 for c in deck
                      if _card_id_norm(c) in ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD"))
        if removal_cost and gold >= removal_cost and n_basic >= 3:
            return {"cmd": "action", "action": "remove_card"}

        # === Card removal — gated by quantified marginal value ===
        # User observation (Jun 10): removal payoff strong at deck ≤15, weak >20.
        # We now compare removal_value(deck) vs best buyable card score and
        # pick whichever marginal upgrade is larger. Earlier "always remove if
        # any basic/junk" was over-eager for bloated decks.
        from agent.card_scoring import removal_value as _rv
        rv = _rv(deck)
        # Compute peek of best buyable card for comparison BEFORE deciding remove
        _cards_peek = [c for c in state.get("cards", [])
                        if c.get("is_stocked") and c.get("cost", 999) <= gold]
        if _cards_peek:
            _best_score = max(score_card_in_deck(c, deck) for c in _cards_peek)
        else:
            _best_score = 0.0
        if removal_cost and gold >= removal_cost and rv > max(_best_score, 3.0):
            return {"cmd": "action", "action": "remove_card"}

        # Find best affordable card — deck-aware so synergy cards rank higher.
        cards_avail = [c for c in state.get("cards", [])
                       if c.get("is_stocked") and c.get("cost", 999) <= gold]
        best_card = max(cards_avail, key=lambda c: score_card_in_deck(c, deck)) if cards_avail else None
        best_score = score_card_in_deck(best_card, deck) if best_card else 0.0
        # Buy elite cards first (score ≥ 8.0 with synergy)
        if best_card and best_score >= 8.0:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Lower thresholds across the board (more aggressive shop usage)
        if in_act2:
            card_buy_threshold = 4.5
        elif pre_boss:
            card_buy_threshold = 5.0
        else:
            card_buy_threshold = 5.5
        if best_card and best_score >= card_buy_threshold:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Buy a relic — smaller buffer (25g vs 50g), lower threshold (4.0 vs 5.0).
        RELIC_GOLD_THRESHOLD = 25
        relics = [r for r in state.get("relics", [])
                  if r.get("is_stocked") and r.get("cost", 999) <= gold - RELIC_GOLD_THRESHOLD]
        if relics:
            best_relic = max(relics, key=_score_shop_relic)
            if _score_shop_relic(best_relic) >= 4.0:
                return {"cmd": "action", "action": "buy_relic",
                        "args": {"relic_index": best_relic.get("index", 0)}}
        # Buy a potion if we have empty slots and it's affordable
        if held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            if shop_potions:
                best_potion = max(shop_potions, key=_score_shop_potion)
                if _score_shop_potion(best_potion) >= 4.5:
                    return {"cmd": "action", "action": "buy_potion",
                            "args": {"potion_index": best_potion.get("index", 0)}}
        return {"cmd": "action", "action": "leave_room"}

    return {"cmd": "action", "action": "proceed"}


def _total_enemy_hp(state: dict) -> int:
    return sum(e.get("hp", 0) for e in state.get("enemies", []))


def _player_hp(state: dict) -> int:
    return state.get("player", {}).get("hp", 0)


def _enemy_power_amount(enemy: dict, power_name: str) -> float:
    """Return the amount of a named power on an enemy (0.0 if not present)."""
    for p in (enemy.get("powers") or []):
        pname = p.get("name", {})
        if isinstance(pname, dict):
            pname = pname.get("en", "")
        if str(pname).lower() == power_name.lower():
            return float(p.get("amount", 1))
    return 0.0


# Extra reward paid once per Boss-room combat win, on top of _combat_win_reward.
# Boss combats at floor 17/Act 1 / Act 2+ are the chokepoint where training stalled
# (0% win rate across 2.3M steps). +10.0 dominates the per-step hp_penalty/floor_bonus
# scale and pushes the policy to actually clear the fight, not just survive nearby.
BOSS_CLEAR_BONUS = 10.0

# One-shot reward when entering a Boss combat with HP > BOSS_ENTRY_HP_FLOOR,
# paid REGARDLESS of combat outcome. Decouples "deliver HP to boss" from
# "clear boss". HP-sweep diagnostic (May 25) showed boss-policy CAN win at
# hp≥80 (27%/53%/77% at 80/100/120), so the bottleneck is Acts 1-2 leakage
# leaving the player at hp 44-68 — squarely in the 0%-win dead zone. Reward
# every extra HP delivered above the dead zone so the policy explicitly
# learns "save HP for the boss room".
BOSS_ENTRY_HP_FLOOR = 50.0    # below this, no bonus (already in the 0% dead zone)
# Bumped 0.2 → 0.8 after three 300k boss-mix rounds (v1/v2/v3) all stalled at
# 0% boss-win. HP-sweep diagnostic (May 25) showed boss is winnable at hp≥80
# (27%/53%/77% at 80/100/120), so the bottleneck is HP delivery, not the boss
# fight itself. Boss-focused training kept teaching combat skill at the cost of
# eval regressing to baseline. Shifting the signal: reward HP arrived at the
# boss room, not boss kills. hp=100 → +40 (was +10), hp=80 → +24 (was +6).
BOSS_ENTRY_HP_WEIGHT = 0.8

# Optional dense boss-only shaping for boss snapshot fine-tuning.
# Off by default so existing full-run checkpoints/evals keep their reward scale.
BOSS_DENSE_DAMAGE_WEIGHT = 0.50


import os as _os_gl


class CombatEnv(gym.Env):
    """
    Gymnasium environment for STS2 combat.

    Each episode = one single combat encounter.
    Each step = one game action (play_card or end_turn).
    No auto-skip — forced end_turn steps are in the buffer. The policy and value
    networks must be SEPARATE (net_arch=dict(pi=..., vf=...)) to prevent
    value-loss gradient on forced steps from corrupting the policy head.
    """

    def __init__(self, cards_json: str = None, character: str = "Ironclad",
                 ascension: int = 0, seed: str = None, dry_run: bool = False,
                 seed_prefix: str = "t", max_floor: int = 0, extra_obs: bool = True,
                 relic_obs: bool = None,
                 replay_actions: list = None, native_save_path: str = None,
                 set_hp_after_load: int = None,
                 run_context: dict | None = None,
                 game_log: bool = False):
        super().__init__()
        if cards_json is None:
            cards_json = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
        self.enc = StateEncoder(cards_json)
        self.character = character
        # Replay logging (opt-in, --game-log). eval_rl only ever wrote per-run
        # summaries, so a stuck boss fight had no turn-by-turn record to inspect
        # in run_progress_viewer. GameLogger writes the format that viewer reads.
        self._game_logger = None
        self._game_log_enabled = bool(game_log)
        self.ascension = ascension
        self._seed = seed
        self._seed_prefix = seed_prefix
        self.dry_run = dry_run
        self.max_floor = max_floor  # 0 = unlimited

        # Extra features appended after enc.obs_size:
        #   [floor/17, entry_hp_ratio, e0_vuln, e0_weak, e1_vuln, e1_weak, e2_vuln, e2_weak]
        # extra_obs=False: legacy mode for checkpoints trained with 161-dim obs
        import os as _os_env
        relic_obs = _os_env.environ.get("STS2_RELIC_OBS") == "1" if relic_obs is None else relic_obs
        self._EXTRA_OBS = 8 if extra_obs else 0
        from agent.state_encoder import RELIC_VOCAB_SIZE
        self._RELIC_OBS = RELIC_VOCAB_SIZE if relic_obs else 0
        self.observation_space = Box(
            low=0.0, high=1.0,
            shape=(self.enc.obs_size + self._EXTRA_OBS + self._RELIC_OBS,), dtype=np.float32)
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0
        self._prev_enemy_hp = 0
        self._prev_player_hp = 0
        self._combat_start_enemy_hp = 1
        self._combat_start_player_max_hp = 1
        self._combat_entry_hp_ratio = 1.0  # HP ratio when combat started
        self._current_combat_room_type = ""  # captured at combat start; used by boss-clear bonus
        self._pending_boss_entry_reward = 0.0  # one-shot HP-at-boss-entry bonus, paid on first step
        self._current_floor = 1
        self._game_alive = False
        self._read_buf = b""
        self._pending_read_only_replies = 0
        self._combat_steps = 0
        self._dealt_damage_this_turn = False  # tracked for intent-block anti-stall gate
        self.max_combat_steps = 1000  # 200→1000 (May 9): boss/elite fights legitimately take
                                       # 300-600 steps; 200 cap was creating fake "to=89%" timeouts
                                       # mid-fight, which corrupted PPO advantage estimation.
        # Run-level deck-quality milestones (paid once per crossing per game).
        # Cleared when a fresh run starts (in reset's start_run branch).
        self._milestones_paid: set = set()
        # Deck-history JSONL — milestone snapshots + outcome rows for the learned
        # deck predictor (see agent/train_deck_predictor.py). Set DECK_HISTORY=
        # in environment to enable; empty disables recording.
        self._deck_history_path = os.environ.get("DECK_HISTORY_PATH",
            os.path.join(PROJECT_ROOT, "data", "deck_history.jsonl"))
        # Per-run state for the predictor: max floor seen, milestones captured
        self._run_max_floor = 1
        self._run_context = dict(run_context or {})
        self._capture_run_maps = self._run_context.get("capture_map") is True
        self._run_map_snapshots: dict[int, dict] = {}
        self._run_current_map_coord: tuple[int, int, int] | None = None
        self._run_current_map_room_identity: tuple[int, int] | None = None
        self._run_last_map_poll_state_id: int | None = None
        self._run_last_map_poll_state_ref: dict | None = None
        self._run_last_map_poll_state_fingerprint: str | None = None
        self._run_map_capture_poll_metadata = _RUN_MAP_CAPTURE_METADATA_OMITTED
        self._run_pending_map_capture: dict | None = None
        self._run_map_retry_state: dict | None = None
        self._run_seed = self._seed
        self._run_id = str(
            self._run_context.get("run_id")
            or f"r{int(time.time()*1000) % 10**9:09d}_{random.randint(0, 9999):04d}"
        )
        self._run_start_emitted = False
        self._run_started_at = time.time()
        self._run_outcome_emitted = False
        self._run_logging_errors: list[str] = []
        self._run_map_capture_failure_active = False
        self._run_milestone_records: list = []  # buffered rows until outcome known
        self._run_card_pick_records: list = []   # buffered card-reward decisions (per pick, see _buffer_card_pick)

        self._replay_actions = list(replay_actions) if replay_actions else []
        self._replay_pending = bool(self._replay_actions)
        # native_save_path can be a string (fixed save) or a list[str] (snapshot pool —
        # picked uniformly at random on each reset, so a vec-env spreads over the pool).
        if isinstance(native_save_path, (list, tuple)):
            self._save_pool = list(native_save_path)
            self._native_save_path = self._save_pool[0] if self._save_pool else None
        else:
            self._save_pool = None
            self._native_save_path = native_save_path
        # When set, send {"cmd": "set_player", "hp": N} right after load_save so the
        # subsequent combat starts at N HP. Used by boss_retry.py to sweep "how much
        # HP does the agent need to clear the boss".
        self._set_hp_after_load = (None if set_hp_after_load is None
                                   else int(set_hp_after_load))

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self._encode(self._current_state), {}

        # Snapshot pool: pick a random save per reset and force a fresh load
        # below (don't try to continue the previous run — we want fresh boss
        # variety each episode).
        if self._save_pool:
            self._native_save_path = random.choice(self._save_pool)
            self._game_alive = False

        # Try to advance to next combat in the current run
        if self._game_alive and self._current_state is not None:
            cur_floor = (self._current_state.get("floor")
                         or self._current_state.get("context", {}).get("floor", 0))
            if self.max_floor > 0 and isinstance(cur_floor, int) and cur_floor >= self.max_floor:
                # Curriculum: restart to keep fighting easy enemies
                self._emit_run_outcome(self._current_state, False, status="invalid")
                self._game_alive = False
                self._kill_proc()
            else:
                state = self._advance_to_combat(self._current_state)
                if state and state.get("decision") == "combat_play":
                    self._init_combat_tracking(state)
                    self._current_state = state
                    return self._encode(state), {}
                # Advance failed — game ended (natural game_over or crash).
                # Signal game_over via info instead of silently restarting:
                # eval_rl.py checks info["game_over"] to end the eval game correctly.
                terminal_status = (
                    "crash" if state is None else
                    "stuck" if state.get("decision") == "stuck" else
                    None if state.get("decision") == "game_over" else "invalid"
                )
                self._emit_run_outcome(
                    state or self._current_state,
                    bool(state and state.get("victory", False)),
                    status=terminal_status,
                )
                self._game_alive = False
                self._kill_proc()
                self._current_state = _dummy_combat_state()
                self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
                crashed = (state is None or state.get("decision") == "stuck")
                return self._encode(self._current_state), {
                    "game_over": True,
                    "crashed": crashed,
                    "stuck": bool(state and state.get("decision") == "stuck"),
                    "invalid": terminal_status == "invalid",
                    "victory": bool(state and state.get("victory", False)),
                }

        # Start a fresh game process + run
        run_seed = self._seed or f"{self._seed_prefix}_{self._run_counter}_{random.randint(0,99999)}"
        self._run_counter += 1
        self._milestones_paid.clear()  # new run — re-arm deck-quality milestones
        self._run_max_floor = 1
        self._run_id = str(
            self._run_context.get("run_id")
            or f"r{int(time.time()*1000) % 10**9:09d}_{random.randint(0, 9999):04d}"
        )
        self._run_seed = run_seed
        self._open_game_log(run_seed)
        self._run_start_emitted = False
        self._run_started_at = time.time()
        self._run_outcome_emitted = False
        self._run_milestone_records = []
        self._run_card_pick_records = []
        self._run_map_snapshots = {}
        self._run_current_map_coord = None
        self._run_current_map_room_identity = None
        self._clear_run_last_map_poll_state()
        self._run_map_capture_poll_metadata = _RUN_MAP_CAPTURE_METADATA_OMITTED
        self._run_pending_map_capture = None
        self._run_map_retry_state = None
        self._run_logging_errors = []
        self._run_map_capture_failure_active = False
        self._run_boss_id = None  # act boss from state.context.boss (set on first sight)
        self._kill_proc()
        self._emit_run_start()
        self._start_proc()
        if self._native_save_path:
            state = self._send({"cmd": "load_save",
                                "path": self._native_save_path, "lang": "en"})
            self._poll_run_map_state_once(state)
            if state is not None and state.get("type") != "error" and self._set_hp_after_load is not None:
                updated_state = self._send({"cmd": "set_player", "hp": self._set_hp_after_load})
                self._update_buffered_node_inventory(updated_state)
                if updated_state is not None and updated_state.get("type") != "error":
                    state = updated_state
        else:
            state = self._send({"cmd": "start_run", "character": self.character,
                                "seed": run_seed, "ascension": self.ascension})
            self._poll_run_map_state_once(state)
        if state is None or state.get("type") == "error":
            self._emit_run_outcome(state or {}, False, status="reset_failure")
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
            return self._encode(self._current_state), {
                "load_failed": bool(self._native_save_path),
                "reset_failure": True,
                "message": (state or {}).get("message", "") if state else "",
            }

        self._game_alive = True

        # Replay any saved actions (one-shot, on first reset only)
        if self._replay_pending:
            for cmd in self._replay_actions:
                state = self._send(cmd)
                self._poll_run_map_state_once(state)
                if state is None:
                    self._emit_run_outcome(self._current_state or {}, False, status="invalid")
                    self._game_alive = False
                    self._current_state = _dummy_combat_state()
                    self._init_combat_tracking(self._current_state)
                    return self._encode(self._current_state), {"replay_failed": True}
                if state.get("decision") == "game_over":
                    self._emit_run_outcome(
                        state, bool(state.get("victory", False)))
                    self._game_alive = False
                    self._current_state = _dummy_combat_state()
                    self._init_combat_tracking(self._current_state)
                    return self._encode(self._current_state), {
                        "game_over": True,
                        "victory": bool(state.get("victory", False)),
                        "from_replay": True,
                    }
            self._replay_pending = False

        state = self._advance_to_combat(state)
        if state is None or state.get("decision") != "combat_play":
            terminal_status = ("crash" if state is None else
                               "stuck" if state.get("decision") == "stuck" else
                               None if state.get("decision") == "game_over" else "invalid")
            self._emit_run_outcome(
                state or {}, bool(state and state.get("victory", False)),
                status=terminal_status,
            )
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
            terminal_info = {
                "game_over": bool(state and state.get("decision") == "game_over"),
                "victory": bool(state and state.get("victory", False)),
                "crashed": terminal_status == "crash",
                "stuck": terminal_status == "stuck",
                "invalid": terminal_status == "invalid",
            }
            return self._encode(self._current_state), terminal_info

        self._init_combat_tracking(state)
        self._current_state = state
        self._combat_steps = 0
        return self._encode(state), {}


    def _open_game_log(self, run_seed: str):
        """Start a fresh replay log for this run (no-op unless --game-log)."""
        self._close_game_log()
        if not self._game_log_enabled:
            return
        try:
            import sys as _sys
            _p = _os_gl.path.join(PROJECT_ROOT, "python")
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
            from game_log import GameLogger
            self._game_logger = GameLogger(self.character, run_seed, enabled=True)
        except Exception:
            self._game_logger = None      # logging must never break a run

    def _close_game_log(self):
        if self._game_logger is not None:
            try:
                self._game_logger.close()
            except Exception:
                pass
            self._game_logger = None

    def _dump_stuck(self, kind: str, state: dict):
        """Record where a stuck was declared. Stuck runs are dropped from the
        stats as technical failures, and they skew toward DEEP runs (Defect's
        were at A2F16/A3F15), so the exclusion silently biases results down."""
        import json as _json, os as _os
        path = _os.environ.get("STS2_STUCK_DUMP")
        if not path:
            return
        try:
            st = state or {}
            row = {
                "kind": kind,
                "act": (st.get("context") or {}).get("act"),
                "floor": (st.get("context") or {}).get("floor") or self._current_floor,
                "room_type": (st.get("context") or {}).get("room_type"),
                "decision": st.get("decision"),
                "round": st.get("round"),
                "energy": st.get("energy"),
                "hp": (st.get("player") or {}).get("hp"),
                "hand": [c.get("name") for c in (st.get("hand") or [])],
                "hand_cost": [c.get("cost") for c in (st.get("hand") or [])],
                "enemies": [{"name": e.get("name"), "hp": e.get("hp"),
                             "powers": e.get("powers")}
                            for e in (st.get("enemies") or [])],
                "player_powers": st.get("player_powers"),
            }
            with open(path, "a") as fh:
                fh.write(_json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass
    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size + self._EXTRA_OBS, dtype=np.float32), -2.0, True, False, {}

        # Detect dead process (e.g. potion crash in _greedy_use_potions during reset)
        if not self._game_alive:
            self._emit_run_outcome(self._current_state, False, status="crash")
            return (self._encode(self._current_state), -2.0, True, False,
                    {"crashed": True, "floor": self._current_floor})

        self._combat_steps += 1
        if self._combat_steps > self.max_combat_steps:
            # Combat too long — treat as defeat to avoid wasting time
            last_obs = self._encode(self._current_state)
            self._game_alive = False
            self._emit_run_outcome(self._current_state, False, status="timeout")
            self._kill_proc()
            return last_obs, -2.0, True, False, {"timeout": True}

        cmd = self.enc.decode(int(action), self._current_state)
        self._last_cmd = cmd
        # Capture pre-action state so end_turn intent-aware shaping can read
        # the block/intents the agent committed to before the enemy turn fires.
        pre_state = self._current_state
        self._retry_run_map_poll_before_gameplay()
        state = self._send(cmd)
        self._update_buffered_node_inventory(state)

        # Detect stuck: end_turn ignored by engine (round/HP unchanged)
        if (state and state.get("decision") == "combat_play"
                and cmd.get("action") == "end_turn"
                and state.get("round") == self._current_state.get("round")
                and state.get("player", {}).get("hp") == self._current_state.get("player", {}).get("hp")):
            # Try proceed to unstick
            for _ in range(5):
                state = self._send({"cmd": "action", "action": "proceed"})
                self._update_buffered_node_inventory(state)
                if state is None or state.get("decision") != "combat_play":
                    break
                if state.get("round") != self._current_state.get("round"):
                    break
            if state and state.get("decision") == "combat_play" and \
                    state.get("round") == self._current_state.get("round"):
                # Still stuck — kill this combat
                last_obs = self._encode(self._current_state)
                self._game_alive = False
                self._dump_stuck("end_turn_ignored", self._current_state)
                self._emit_run_outcome(self._current_state, False, status="stuck")
                self._kill_proc()
                return last_obs, -2.0, True, False, {"stuck": True}

        if state is None:
            self._game_alive = False
            self._emit_run_outcome(self._current_state, False, status="crash")
            last_obs = self._encode(self._current_state)
            cmd_str = json.dumps(getattr(self, "_last_cmd", None))
            floor = self._current_floor
            hand_size = len(self._current_state.get("hand", []))
            n_enemies = len(self._current_state.get("enemies", []))

            # Killing-blow crash: the last enemy died (from play_card or from poison/power
            # during end_turn), and C# crashed in DetectPostCombatState post-combat cleanup.
            # The combat WAS won. Give combat_win_reward instead of -2.0 crash penalty.
            cmd = getattr(self, "_last_cmd", {})
            action = cmd.get("action", "") if isinstance(cmd, dict) else ""
            is_killing_blow = (
                n_enemies == 1 and (
                    action == "play_card" or action == "end_turn"
                )
            )
            if is_killing_blow:
                # Combat won (last enemy killed), but C# crashed in post-combat cleanup.
                # Give full win reward — BUG-031 fix makes this very rare, so no exploitation risk.
                # _game_alive must be False (already set above) so reset() starts a fresh game.
                reward = self._combat_win_reward(self._current_state)
                print(f"\n[CRASH→WIN] floor={floor} reward={reward:.2f}",
                      file=sys.stderr, flush=True)
                return last_obs, reward, True, False, {
                    "floor": floor, "combat_won": True, "crashed": True,
                }

            print(f"\n[CRASH] floor={floor} cmd={cmd_str} hand={hand_size} enemies={n_enemies}",
                  file=sys.stderr, flush=True)
            return last_obs, -2.0, True, False, {"crashed": True, "floor": floor}

        # C# returns {"decision":"stuck"} when enemy turn deadlocks for 15s.
        # Kill the process immediately — returning a garbage combat_play state and
        # continuing would corrupt the C# process → cr=100% cascade.
        if state.get("decision") == "stuck":
            self._game_alive = False
            self._dump_stuck("engine_deadlock", self._current_state)
            self._emit_run_outcome(self._current_state, False, status="stuck")
            self._kill_proc()
            last_obs = self._encode(self._current_state)
            return last_obs, -2.0, True, False, {
                "crashed": True, "stuck": True, "floor": self._current_floor,
            }

        decision = state.get("decision", "")
        self._track_run_floor(state)
        reward = self._shaping_reward(state)

        # B5: pay HP-at-boss-entry bonus on the first step of a Boss combat.
        # Set by _init_combat_tracking when room_type=="Boss" and hp>floor;
        # cleared after paying so it only fires once per combat.
        if self._pending_boss_entry_reward > 0:
            reward += self._pending_boss_entry_reward
            self._pending_boss_entry_reward = 0.0

        # Track per-turn damage dealt (used by intent_block_reward gating below)
        if state is not None and cmd.get("action") == "play_card":
            cur_enemy_hp = _total_enemy_hp(state)
            prev_enemy_hp = _total_enemy_hp(pre_state) if pre_state else cur_enemy_hp
            if cur_enemy_hp < prev_enemy_hp:
                self._dealt_damage_this_turn = True
            # Q3b power-safe-turn bonus DISABLED 2026-05-19 — small positive
            # per-card reward accumulates over 1M steps into stall-favoring drift.

        # All end_turn reward shaping (Q3a intent-block, Q3c wasted-energy)
        # DISABLED 2026-05-19 — all attempts to nudge turn-level behavior caused
        # drift collapse. The base reward signal (hp_penalty + combat_win_reward
        # + step_penalty) is sufficient. Reset damage tracker for next turn.
        if cmd.get("action") == "end_turn":
            self._dealt_damage_this_turn = False

        # Use last known combat obs for terminal states (NOT zeros — zeros
        # confuse the value function because they're too similar to sparse
        # combat states, causing gradient pollution that collapses entropy)
        last_obs = self._encode(self._current_state)

        if decision == "game_over":
            self._game_alive = False
            r = reward + self._terminal_reward(state)
            if state.get("victory", False):
                # Boss kill: also award combat_win_reward so victory > regular combat win
                r += self._combat_win_reward(state)
            self._run_max_floor = max(self._run_max_floor, self._current_floor)
            self._emit_run_outcome(state, bool(state.get("victory", False)))
            return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                               "victory": state.get("victory", False)}

        if decision == "combat_play":
            state = self._combat_check_heal(state)  # reactive heal if HP critical mid-fight
            if state.get("decision") == "game_over":
                self._game_alive = False
                r = reward + self._terminal_reward(state)
                if state.get("victory", False):
                    r += self._combat_win_reward(state)
                self._run_max_floor = max(self._run_max_floor, self._current_floor)
                self._emit_run_outcome(state, bool(state.get("victory", False)))
                return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                                   "victory": state.get("victory", False)}
            self._current_state = state
            return self._encode(state), reward, False, False, {}

        # Mid-combat card_select (e.g. boss mechanics, card effects that trigger selection)
        # Auto-handle these without ending the episode — they appear in Boss/Monster/Elite rooms
        # while CombatManager is still active. Without this, Python incorrectly treats them as
        # "combat won" and then resume in the same boss fight as a "new" combat.
        if decision == "card_select":
            context = state.get("context", {})
            if context.get("room_type") in ("Boss", "Monster", "Elite"):
                for _ in range(10):
                    auto_cmd = greedy_action(state)
                    state = self._send(auto_cmd)
                    self._update_buffered_node_inventory(state)
                    if state is None:
                        self._game_alive = False
                        self._emit_run_outcome(self._current_state, False, status="crash")
                        return last_obs, -2.0, True, False, {"crashed": True}
                    if state.get("decision") in ("combat_play", "game_over"):
                        break
                    if state.get("decision") != "card_select":
                        break
                if state.get("decision") == "combat_play":
                    self._current_state = state
                    return self._encode(state), reward, False, False, {}
                if state.get("decision") == "game_over":
                    self._game_alive = False
                    r = reward + self._terminal_reward(state)
                    if state.get("victory", False):
                        r += self._combat_win_reward(state)
                    self._run_max_floor = max(self._run_max_floor, self._current_floor)
                    self._emit_run_outcome(state, bool(state.get("victory", False)))
                    return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                                       "victory": state.get("victory", False)}

        # Combat ended (transitioned to card_reward, map_select, etc.) — we won
        reward += self._combat_win_reward(state)
        self._current_state = state
        return last_obs, reward, True, False, {"floor": self._current_floor, "combat_won": True}

    def action_masks(self) -> np.ndarray:
        if self._current_state is None:
            return np.ones(41, dtype=bool)
        mask = self.enc.action_mask(self._current_state)
        if os.environ.get("STS2_VANTOM_SLIPPERY_MASK", "") in ("1", "true", "on"):
            try:
                from agent.turn_planner import apply_vantom_slippery_mask
                mask = apply_vantom_slippery_mask(self._current_state, mask)
            except Exception:
                pass
        if os.environ.get("STS2_BOSS_PLANNER_MASK", "") in ("1", "true", "on"):
            try:
                context = self._current_state.get("context") or {}
                room_type = str(context.get("room_type") or self._current_combat_room_type)
                room_type_l = room_type.lower()
                if "boss" in room_type_l or "elite" in room_type_l:
                    from agent.turn_planner import plan_action
                    planned = plan_action(self._current_state, mask)
                    if planned is not None and 0 <= int(planned) < len(mask) and bool(mask[int(planned)]):
                        forced = np.zeros_like(mask)
                        forced[int(planned)] = True
                        mask = forced
            except Exception:
                pass
        return mask

    def close(self):
        self._kill_proc()

    def set_max_floor(self, max_floor: int) -> None:
        self.max_floor = max_floor

    def set_hp_after_load(self, hp: int) -> None:
        """Update the post-load HP override at runtime. 0 or negative disables."""
        self._set_hp_after_load = (None if (hp is None or hp <= 0) else int(hp))

    def _encode(self, state: dict) -> np.ndarray:
        """Encode state + optional extra (8) + optional relic multi-hot."""
        base = self.enc.encode(state)
        if self._EXTRA_OBS == 0 and self._RELIC_OBS == 0:
            return base
        parts = [base]
        if self._EXTRA_OBS:
            floor_norm = min(self._current_floor / 17.0, 1.0)
            enemies = state.get("enemies", [])
            extra = [floor_norm, self._combat_entry_hp_ratio]
            for slot in range(3):
                e = enemies[slot] if slot < len(enemies) else {}
                extra.append(min(_enemy_power_amount(e, "Vulnerable") / 10.0, 1.0))
                extra.append(min(_enemy_power_amount(e, "Weak") / 10.0, 1.0))
            parts.append(np.array(extra, dtype=np.float32))
        if self._RELIC_OBS:
            from agent.state_encoder import encode_relics
            parts.append(encode_relics(CombatEnv._state_relic_ids(state)))
        return np.concatenate(parts)

    def _init_combat_tracking(self, state: dict):
        self._prev_enemy_hp = _total_enemy_hp(state)
        self._prev_player_hp = _player_hp(state)
        self._combat_start_enemy_hp = max(self._prev_enemy_hp, 1)
        self._combat_start_player_max_hp = max(state.get("player", {}).get("max_hp", 1), 1)
        floor = state.get("floor") or state.get("context", {}).get("floor", 1)
        self._current_floor = int(floor) if isinstance(floor, (int, float)) and floor > 0 else 1
        self._track_run_floor(state)
        hp = state.get("player", {}).get("hp", self._combat_start_player_max_hp)
        self._combat_entry_hp_ratio = hp / self._combat_start_player_max_hp
        self._dealt_damage_this_turn = False  # fresh combat starts with no damage logged
        # Capture room_type at combat start: by the time _combat_win_reward fires,
        # the state may have transitioned to card_reward and room_type is stale.
        room_type = (state.get("context") or {}).get("room_type", "")
        self._current_combat_room_type = str(room_type)
        # B5 plan (May 25): pay HP-at-boss-entry bonus on the FIRST step of a Boss
        # combat (paid regardless of outcome — see BOSS_ENTRY_HP_* constants).
        if self._current_combat_room_type == "Boss" and hp > BOSS_ENTRY_HP_FLOOR:
            self._pending_boss_entry_reward = (hp - BOSS_ENTRY_HP_FLOOR) * BOSS_ENTRY_HP_WEIGHT
        else:
            self._pending_boss_entry_reward = 0.0

    @staticmethod
    def _global_floor_from_state(state: dict | None, fallback: int = 1) -> int:
        """Return absolute run floor without changing act-local reward state."""
        if not isinstance(state, dict):
            return int(fallback)
        context = state.get("context") if isinstance(state.get("context"), dict) else {}
        global_floor = state.get("global_floor") or context.get("global_floor")
        if (isinstance(global_floor, (int, float)) and not isinstance(global_floor, bool)
                and global_floor > 0):
            return int(global_floor)
        act = context.get("act", 1)
        floor = state.get("floor") or context.get("floor")
        if (isinstance(act, (int, float)) and not isinstance(act, bool) and act > 0
                and isinstance(floor, (int, float)) and not isinstance(floor, bool)
                and floor > 0):
            return (int(act) - 1) * 17 + int(floor)
        return int(fallback)

    def _track_run_floor(self, state: dict | None) -> None:
        self._run_max_floor = max(
            int(self._run_max_floor),
            self._global_floor_from_state(state, fallback=self._run_max_floor),
        )

    def _shaping_reward(self, next_state: dict) -> float:
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        # Base damage reward = 0.15 × frac of starting enemy HP. At Act 1 boss
        # (floor 17+) we add an extra 0.10 so each fraction of boss HP burned
        # gives stronger signal — boss combats are long, every chunk matters,
        # and 0% win rate means policy needs more "got close" signal.
        # Option d (Jun 14) — HP-preservation reward reshape. The floor-bonus
        # lineage learned to RACE (spend HP for fast kills). To teach HP
        # preservation: lower dmg_reward (less race incentive) + heavier
        # hp_penalty (each HP lost hurts ~2.4×). Gated behind STS2_HP_REWARD=1
        # so existing checkpoints/evals are unaffected; only the d-retrain run
        # sets it. NEVER add explicit block_reward (block-forever collapse).
        import os as _os_r
        _hp_mode = _os_r.environ.get("STS2_HP_REWARD") == "1"
        _dmg_w = 0.10 if _hp_mode else 0.15
        _hp_w = -1.2 if _hp_mode else -0.50
        dmg_reward = _dmg_w * enemy_hp_lost / self._combat_start_enemy_hp
        if self._current_floor >= 17:
            dmg_reward += 0.10 * enemy_hp_lost / self._combat_start_enemy_hp
        if (_os_r.environ.get("STS2_BOSS_DENSE") == "1"
                and "boss" in str(self._current_combat_room_type).lower()):
            dmg_reward += BOSS_DENSE_DAMAGE_WEIGHT * enemy_hp_lost / self._combat_start_enemy_hp
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        hp_penalty = _hp_w * player_hp_lost / self._combat_start_player_max_hp

        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp

        # Step penalty: discourages stalling / timeout (200 steps → -0.60).
        # block_reward removed: it made pure-blocking per-step positive (0.019 > 0.003 step_penalty),
        # causing policy collapse into "block forever, never attack" → 60%+ timeout rate.
        # Blocking is still incentivized implicitly by hp_penalty (blocking prevents damage).
        step_penalty = -0.003
        return dmg_reward + hp_penalty + step_penalty

    def _intent_block_reward(self, pre_state: dict, dealt_damage_this_turn: bool) -> float:
        """Reward block matched to incoming attack damage at end_turn — but only
        if the agent ALSO dealt damage this turn (anti-stall) AND blocked at
        least 80% of incoming (anti-half-block-spam).

        History: prior versions collapsed at cwr<20%. Two new gates added (May 14):
          1. dealt_damage_this_turn — pure stall (all blocks, no attacks) gets 0.
          2. blocked/incoming >= 0.8 — only "good defensive turn" qualifies.
        Capped at 0.05 (half of prior 0.10) to keep magnitude small.
        """
        if pre_state is None or not dealt_damage_this_turn:
            return 0.0
        incoming = 0
        for e in pre_state.get("enemies", []) or []:
            if e.get("alive") is False:
                continue
            for it in (e.get("intents") or []):
                if (it.get("type") or "").lower() != "attack":
                    continue
                try:
                    dmg = int(it.get("damage", 0) or 0)
                    hits = int(it.get("hits", 1) or 1)
                except (TypeError, ValueError):
                    continue
                if dmg > 0 and hits > 0:
                    incoming += dmg * hits
        if incoming <= 0:
            return 0.0
        block = pre_state.get("player", {}).get("block", 0) or 0
        try:
            block = int(block)
        except (TypeError, ValueError):
            return 0.0
        # Require blocking at least 80% of incoming to qualify
        if block < incoming * 0.8:
            return 0.0
        blocked = min(block, incoming)
        max_hp = max(self._combat_start_player_max_hp, 1)
        return 0.05 * blocked / max_hp

    def _power_safe_turn_reward(self, card: dict, pre_state: dict) -> float:
        """+0.05 when playing a Power card on a 'safe' turn (no enemy attack
        intent). Encourages saving expensive setup for non-attack windows."""
        if pre_state is None or not isinstance(card, dict):
            return 0.0
        if (card.get("type") or "").lower() != "power":
            return 0.0
        for e in pre_state.get("enemies", []) or []:
            if e.get("alive") is False:
                continue
            for it in (e.get("intents") or []):
                if (it.get("type") or "").lower() == "attack":
                    return 0.0  # there's incoming damage; not safe
        return 0.05

    def _wasted_energy_penalty(self, pre_state: dict) -> float:
        """At end_turn, penalty if the player ended with ≥2 unspent energy AND
        had playable cards in hand. -0.02 per (small but adds up over a run).
        Acceptable to leave 1 energy (holding for next-turn setup); 2+ is waste."""
        if pre_state is None:
            return 0.0
        player = pre_state.get("player", {})
        energy = player.get("energy", 0) or 0
        try:
            energy = int(energy)
        except (TypeError, ValueError):
            return 0.0
        if energy < 2:
            return 0.0
        # Check for playable cards (cost <= energy, not unplayable status)
        hand = pre_state.get("hand", []) or []
        for c in hand:
            try:
                cost = int(c.get("cost", 99) or 99)
            except (TypeError, ValueError):
                continue
            cid = (c.get("id") or "")
            if isinstance(cid, dict):
                cid = cid.get("en", "")
            cid = str(cid).upper()
            if "WOUND" in cid or "BURN" in cid or "SLIMED" in cid or "DAZE" in cid:
                continue  # status cards can't be played voluntarily
            if cost <= energy:
                return -0.02  # had option, didn't use it
        return 0.0

    def _combat_win_reward(self, state: dict) -> float:
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        # hp_ratio is end_hp / start_hp_of_this_combat (not max_hp_of_run)
        # so a "no-damage win" returns ratio 1.0 even if combat started low-hp.
        hp_ratio = hp / max_hp
        # REVERTED 2026-05-19: Q1 0-damage bonus DISABLED. With max_combat_steps=1000,
        # the +2.0/+0.75/+0.50/+0.25 tiers made "block-then-kill" locally optimal —
        # agent drifted to stalling, hit cwr 82%→13%/to=84% collapse twice. Original
        # 8827k baseline (avg_floor=13.1) used only the quadratic curve below.
        # Option d: HP-preservation mode bumps the combat-win HP coefficient
        # 3.0→5.0 so finishing a fight at low HP is much less rewarding than
        # finishing healthy — discourages the race-to-low-HP habit.
        import os as _os_w
        _win_hp_coef = 5.0 if _os_w.environ.get("STS2_HP_REWARD") == "1" else 3.0
        reward = _win_hp_coef * hp_ratio * hp_ratio
        # Floor bonus: Act 1 (floor≤15) = 0.10/floor; Act 2+ gets +0.15/floor above 15.
        if self._current_floor <= 15:
            floor_bonus = (self._current_floor - 1) * 0.10
        else:
            floor_bonus = 1.4 + (self._current_floor - 15) * 0.15
        reward += floor_bonus
        # Deck-quality milestone bonus — paid ONCE per run when crossing each
        # of {5, 10, 15} for the first time, scaled by deck quality 0–10.
        # Encourages building a strong deck early; passive Strikes/Defends
        # → low deck_quality → small or zero bonus.
        reward += self._milestone_reward(state)
        if self._current_combat_room_type == "Boss":
            reward += BOSS_CLEAR_BONUS
        return reward

    @staticmethod
    def _state_relic_ids(state: dict) -> list[str]:
        """Pull the canonical runtime ID (uppercase snake-case) for each
        relic the player currently owns. Robust to a few legacy shapes:
        relics may live at the top level, on the player block, or carry
        either an "id" or "name" field."""
        relics_raw = state.get("relics")
        if not relics_raw:
            player = state.get("player") or {}
            relics_raw = player.get("relics", [])
        out = []
        for r in relics_raw or []:
            if isinstance(r, dict):
                rid = r.get("id") or r.get("name") or ""
            else:
                rid = str(r)
            rid = rid.upper().replace("-", "_").replace(" ", "_")
            if rid:
                out.append(rid)
        return out

    def _milestone_reward(self, state: dict) -> float:
        """One-shot reward when the run first crosses a milestone floor with a
        decent deck. Bounded ≤0.6 per milestone to avoid policy distortion.
        Also writes a deck snapshot row (buffered) for the predictor dataset."""
        floor = self._current_floor
        bonus = 0.0
        deck = state.get("player", {}).get("deck") or []
        for milestone in (5, 10, 15):
            if floor >= milestone and milestone not in self._milestones_paid:
                self._milestones_paid.add(milestone)
                q = deck_quality_score(deck)
                m_bonus = max(0.0, min((q - 4.5) * 0.15, 0.6))
                bonus += m_bonus
                # Buffer milestone record — final outcome appended in
                # _emit_run_outcome when the run ends.
                relic_ids = self._state_relic_ids(state)
                self._buffer_milestone_record(milestone, deck, q, relic_ids)
        return bonus

    def _buffer_milestone_record(self, milestone: int, deck: list, quality: float,
                                 relics: list[str] | None = None):
        """Save a deck snapshot for later outcome correlation."""
        if not self._deck_history_path:
            return
        try:
            from agent.card_scoring import (score_deck_dimensions,
                                            compute_deck_archetype, _card_id_norm)
        except ImportError:
            return
        try:
            dims = score_deck_dimensions(deck)
            arch = compute_deck_archetype(deck)
            cards = [_card_id_norm(c) for c in deck]
        except Exception:
            return
        self._run_milestone_records.append({
            "event": "milestone",
            "run_id": self._run_id,
            "floor_crossed": milestone,
            "deck_size": len(deck),
            "deck_quality": round(quality, 3),
            "dims": {k: round(v, 3) for k, v in dims.items()},
            "archetype": arch,
            "cards": cards,
            "relics": list(relics) if relics else [],
            "ts": time.time(),
        })

    def _buffer_card_pick(self, state: dict, cmd: dict):
        """Record one card_reward decision (deck_before + offered options + picked).
        Each per-run buffer flushes to disk in _emit_run_outcome along with
        milestones and the outcome row. Trains the deck predictor on actual
        decision counterfactuals (you saw these 3 options with this deck and
        picked X / SKIP — what was the future floor?), much denser signal than
        the 3 milestone snapshots per run."""
        # Capture act boss for per-boss outcome stats (context.boss exposed
        # at every decision point from floor 1).
        _b = (state.get("context") or {}).get("boss") or {}
        _bid = str(_b.get("id") or "").replace("_BOSS", "")
        if _bid and getattr(self, "_run_boss_id", None) is None:
            self._run_boss_id = _bid
        if not self._deck_history_path:
            return
        try:
            from agent.card_scoring import _card_id_norm
        except ImportError:
            return
        cards = state.get("cards") or []
        if not cards:
            return
        action = cmd.get("action", "")
        args = cmd.get("args", {}) or {}
        picked = None
        if action == "select_card_reward":
            idx = args.get("card_index", -1)
            if isinstance(idx, int) and 0 <= idx < len(cards):
                picked = _card_id_norm(cards[idx])
        elif action == "skip_card_reward":
            picked = "SKIP"
        else:
            return  # not a card_reward action — skip
        deck = state.get("player", {}).get("deck") or []
        floor = state.get("floor") or state.get("context", {}).get("floor", 1)
        opts = []
        for c in cards:
            opts.append({
                "id": _card_id_norm(c),
                "cost": c.get("cost"),
                "rarity": c.get("rarity"),
                "type": c.get("type"),
                "upgraded": c.get("upgraded", False),
            })
        self._run_card_pick_records.append({
            "event": "card_pick",
            "run_id": self._run_id,
            "floor": int(floor) if isinstance(floor, (int, float)) and floor > 0 else 1,
            "hp": state.get("player", {}).get("hp"),
            "max_hp": state.get("player", {}).get("max_hp"),
            "deck_before_ids": [_card_id_norm(c) for c in deck],
            "deck_size": len(deck),
            "relics": self._state_relic_ids(state),
            "options": opts,
            "picked": picked,
            "ts": time.time(),
        })

    def _run_metadata_row(self, event: str) -> dict:
        return {
            "event": event,
            "run_id": self._run_id,
            "seed": (
                self._run_context["seed"]
                if "seed" in self._run_context
                else self._run_seed
            ),
            "character": self.character,
            "ascension": self.ascension,
            "checkpoint": self._run_context.get("checkpoint"),
            "evaluation_mode": self._run_context.get("evaluation_mode"),
            "scenario": self._run_context.get("scenario"),
            "game_version": self._run_context.get("game_version"),
            "game_version_source": self._run_context.get("game_version_source"),
            "is_multiplayer": False,
        }

    def _report_run_logging_error(self, prefix: str, exc: Exception):
        error = f"{prefix} for {self._run_id}: {type(exc).__name__}: {exc}"
        if error not in self._run_logging_errors and len(self._run_logging_errors) < 64:
            self._run_logging_errors.append(error)
        try:
            warnings.warn(error, RuntimeWarning, stacklevel=2)
        except Warning:
            print(error, file=sys.stderr)

    @staticmethod
    def _bounded_player_snapshot(state: dict) -> dict:
        """Return a detached, JSON-safe inventory snapshot from state.player."""
        if type(state) is not dict or type(state.get("player")) is not dict:
            return {}
        player = state["player"]
        snapshot = {}
        for field in ("hp", "max_hp", "gold"):
            value = player.get(field)
            if type(value) not in (int, float):
                continue
            try:
                if math.isfinite(value):
                    snapshot[field] = value
            except (OverflowError, TypeError, ValueError):
                continue

        invalid = object()
        verbose_keys = {
            "description", "description_raw", "flavor", "flavor_text", "text",
        }

        def sanitize(value, depth, node_count):
            node_count[0] += 1
            if depth > 4 or node_count[0] > 4096:
                return invalid
            if value is None or type(value) is bool:
                return value
            if type(value) is int:
                return value if -(2**63) <= value < 2**63 else invalid
            if type(value) is float:
                return value if math.isfinite(value) else invalid
            if type(value) is str:
                return value if len(value) <= 256 else invalid
            if type(value) is list:
                if len(value) > 256:
                    return invalid
                result = []
                for item in value:
                    safe_item = sanitize(item, depth + 1, node_count)
                    if safe_item is invalid:
                        return invalid
                    result.append(safe_item)
                return result
            if type(value) is dict:
                if any(type(key) is not str for key in value):
                    return invalid
                retained = [
                    (key, item) for key, item in value.items()
                    if key.lower() not in verbose_keys
                ]
                if len(retained) > 32:
                    return invalid
                result = {}
                for key, item in retained:
                    if len(key) > 256:
                        return invalid
                    safe_item = sanitize(item, depth + 1, node_count)
                    if safe_item is invalid:
                        return invalid
                    result[key] = safe_item
                return result
            return invalid

        collection_fields = (
            ("deck", ("deck",)),
            ("relics", ("relics", "relic_items")),
            ("potions", ("potions", "potion_items")),
        )
        for output_field, aliases in collection_fields:
            safe_value = invalid
            for alias in aliases:
                if alias not in player or type(player[alias]) is not list:
                    continue
                candidate = sanitize(player[alias], 0, [0])
                if candidate is not invalid:
                    safe_value = candidate
                    break
            if safe_value is invalid:
                continue
            try:
                candidate_snapshot = {**snapshot, output_field: safe_value}
                payload = json.dumps(
                    candidate_snapshot, ensure_ascii=False, allow_nan=False,
                ).encode("utf-8")
                if len(payload) <= 64 * 1024:
                    snapshot[output_field] = safe_value
            except Exception:
                continue
        return snapshot

    def _buffered_inventory_checkpoint(self):
        if self._run_current_map_coord is None:
            return None
        act, col, row = self._run_current_map_coord
        snapshot = self._run_map_snapshots.get(act)
        if snapshot is None:
            return None
        node = snapshot.get("_coord_lookup", {}).get((col, row))
        if node is None:
            return None
        detached = json.loads(json.dumps(
            node, ensure_ascii=False, allow_nan=False
        ))
        return node, detached

    @staticmethod
    def _restore_buffered_inventory(checkpoint):
        if checkpoint is None:
            return
        node, detached = checkpoint
        node.clear()
        node.update(detached)

    def _update_buffered_node_inventory(self, state: dict):
        """Update the latest known coordinate without consulting the subprocess."""
        if self._run_current_map_coord is None:
            return
        if type(state) is not dict or type(state.get("player")) is not dict:
            return
        act, col, row = self._run_current_map_coord
        snapshot = self._run_map_snapshots.get(act)
        if snapshot is None:
            return
        node = snapshot.get("_coord_lookup", {}).get((col, row))
        if node is None:
            return
        player = self._bounded_player_snapshot(state)
        if "entry_player" not in node:
            node["entry_player"] = json.loads(json.dumps(
                player, ensure_ascii=False, allow_nan=False
            ))
        node["exit_player"] = player

    @staticmethod
    def _run_decision_state_fingerprint(state: object) -> str | None:
        """Return a bounded canonical fingerprint for one exact JSON state."""
        if type(state) is not dict:
            return None
        count = [0]

        def canonical(value: object, depth: int):
            count[0] += 1
            if (
                depth > _RUN_DECISION_STATE_MAX_DEPTH
                or count[0] > _RUN_DECISION_STATE_MAX_NODES
            ):
                raise ValueError("state fingerprint structure is too large")
            if value is None or type(value) is bool:
                return value
            if type(value) is int:
                if not -(2**63) <= value < 2**63:
                    raise ValueError("state fingerprint integer is out of range")
                return value
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError("state fingerprint number is not finite")
                return value
            if type(value) is str:
                if len(value) > _RUN_DECISION_STATE_MAX_STRING_CHARS:
                    raise ValueError("state fingerprint string is too large")
                value.encode("utf-8", errors="strict")
                return value
            if type(value) is list:
                if len(value) > _RUN_DECISION_STATE_MAX_LIST_ITEMS:
                    raise ValueError("state fingerprint list is too large")
                return ["list", [canonical(item, depth + 1) for item in value]]
            if type(value) is dict:
                if len(value) > _RUN_DECISION_STATE_MAX_DICT_ITEMS:
                    raise ValueError("state fingerprint object is too large")
                for key in value:
                    if (
                        type(key) is not str
                        or len(key) > _RUN_DECISION_STATE_MAX_KEY_CHARS
                    ):
                        raise ValueError("state fingerprint object key is invalid")
                    key.encode("utf-8", errors="strict")
                items = [
                    (key, canonical(item, depth + 1))
                    for key, item in value.items()
                ]
                items.sort(key=lambda pair: pair[0])
                return ["dict", items]
            raise ValueError("state fingerprint contains a non-JSON value")

        try:
            encoded = json.dumps(
                canonical(state, 0),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
            if len(encoded) > _RUN_DECISION_STATE_MAX_BYTES:
                return None
            return hashlib.sha256(encoded).hexdigest()
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return None

    def _clear_run_last_map_poll_state(self) -> None:
        self._run_last_map_poll_state_id = None
        self._run_last_map_poll_state_ref = None
        self._run_last_map_poll_state_fingerprint = None

    def _retain_run_last_map_poll_state(
        self,
        state: object,
        fingerprint: object = _RUN_DECISION_FINGERPRINT_OMITTED,
    ) -> bool:
        if type(state) is not dict:
            self._clear_run_last_map_poll_state()
            return False
        if fingerprint is _RUN_DECISION_FINGERPRINT_OMITTED:
            fingerprint = self._run_decision_state_fingerprint(state)
        if type(fingerprint) is not str or len(fingerprint) != 64:
            self._clear_run_last_map_poll_state()
            return False
        self._run_last_map_poll_state_id = id(state)
        self._run_last_map_poll_state_ref = state
        self._run_last_map_poll_state_fingerprint = fingerprint
        return True

    @staticmethod
    def _is_confirmed_run_decision_reply(reply: object) -> bool:
        if type(reply) is not dict or len(reply) > _RUN_DECISION_REPLY_MAX_KEYS:
            return False
        for key in reply:
            if type(key) is not str or len(key) > _RUN_DECISION_STATE_MAX_KEY_CHARS:
                return False
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return False
        reply_type = reply.get("type")
        return type(reply_type) is str and reply_type == "decision"

    @staticmethod
    def _bounded_run_room_identity(
        state: object,
    ) -> tuple[int, int] | None:
        """Return the exact act-local room identity used to guard combat reuse."""
        if type(state) is not dict:
            return None
        context = state.get("context")
        if type(context) is not dict:
            return None
        act = context.get("act")
        if type(act) is not int or not 1 <= act <= 4:
            return None
        state_has_floor = "floor" in state
        context_has_floor = "floor" in context
        if not state_has_floor and not context_has_floor:
            return None
        state_floor = state.get("floor") if state_has_floor else None
        context_floor = context.get("floor") if context_has_floor else None
        if state_has_floor and (
            type(state_floor) is not int or not 1 <= state_floor <= 17
        ):
            return None
        if context_has_floor and (
            type(context_floor) is not int or not 1 <= context_floor <= 17
        ):
            return None
        if state_has_floor and context_has_floor and state_floor != context_floor:
            return None
        floor = state_floor if state_has_floor else context_floor
        return act, floor

    @staticmethod
    def _validated_map_reply(
        reply: object,
    ) -> tuple[int, dict, tuple[int, int], dict, set[tuple[int, int]]]:
        def is_map_int(value):
            return type(value) is int and -(2**31) <= value < 2**31

        if type(reply) is not dict or reply.get("type") != "map":
            raise ValueError("get_map did not return type=map")
        context = reply.get("context")
        if type(context) is not dict:
            raise ValueError("map context must be an object")
        act = context.get("act")
        if type(act) is not int or not 1 <= act <= 4:
            raise ValueError("map context act must be an integer from 1 to 4")
        safe_context = {"act": act}
        act_name = context.get("act_name")
        if type(act_name) is str and len(act_name) <= 256:
            safe_context["act_name"] = act_name
        floor = context.get("floor")
        if type(floor) is int and -(2**31) <= floor < 2**31:
            safe_context["floor"] = floor
        room_type = context.get("room_type")
        if room_type is None and "room_type" in context:
            safe_context["room_type"] = None
        elif type(room_type) is str and len(room_type) <= 64:
            safe_context["room_type"] = room_type
        context_boss = context.get("boss")
        if type(context_boss) is dict:
            safe_context_boss = {}
            boss_id = context_boss.get("id")
            boss_name = context_boss.get("name")
            if type(boss_id) is str and len(boss_id) <= 64:
                safe_context_boss["id"] = boss_id
            if type(boss_name) is str and len(boss_name) <= 256:
                safe_context_boss["name"] = boss_name
            if safe_context_boss:
                safe_context["boss"] = safe_context_boss
        rows = reply.get("rows")
        if type(rows) is not list:
            raise ValueError("map rows must be a list")
        if len(rows) > 256:
            raise ValueError("map contains more than 256 row containers")

        nodes = {}
        current_nodes = []
        node_count = 0
        edge_count = 0
        safe_rows = []
        for row_nodes in rows:
            if type(row_nodes) is not list:
                raise ValueError("each map row must be a list")
            node_count += len(row_nodes)
            if node_count > 256:
                raise ValueError("map contains more than 256 nodes")
            safe_row = []
            for node in row_nodes:
                if type(node) is not dict:
                    raise ValueError("map node must be an object")
                col, row = node.get("col"), node.get("row")
                if not is_map_int(col) or not is_map_int(row):
                    raise ValueError("map node coordinates must be integers")
                coord = (col, row)
                if coord in nodes:
                    raise ValueError("map node coordinates must be unique")
                node_type = node.get("type")
                if type(node_type) is not str or len(node_type) > 64:
                    raise ValueError("map node type must be a string")
                visited = node.get("visited")
                current_marker = node.get("current")
                if type(visited) is not bool:
                    raise ValueError("map node visited marker must be a boolean")
                if type(current_marker) is not bool:
                    raise ValueError("map node current marker must be a boolean")
                children = node.get("children")
                if type(children) is not list:
                    raise ValueError("map node children must be a list")
                edge_count += len(children)
                if edge_count > 2048:
                    raise ValueError("map contains more than 2048 child edges")
                safe_children = []
                for child in children:
                    if type(child) is not dict:
                        raise ValueError("map child must be an object")
                    if (not is_map_int(child.get("col"))
                            or not is_map_int(child.get("row"))):
                        raise ValueError("map child coordinates must be integers")
                    safe_children.append({
                        "col": child["col"],
                        "row": child["row"],
                    })
                safe_node = {
                    "col": col,
                    "row": row,
                    "type": node_type,
                    "children": safe_children,
                    "visited": visited,
                    "current": current_marker,
                }
                nodes[coord] = safe_node
                safe_row.append(safe_node)
                if current_marker:
                    current_nodes.append(coord)
            safe_rows.append(safe_row)

        current = reply.get("current_coord")
        if type(current) is not dict:
            raise ValueError("map current_coord must be an object")
        col, row = current.get("col"), current.get("row")
        if not is_map_int(col) or not is_map_int(row):
            raise ValueError("map current_coord must contain integer coordinates")
        current_coord = (col, row)
        boss = reply.get("boss")
        if (type(boss) is not dict
                or not is_map_int(boss.get("col"))
                or not is_map_int(boss.get("row"))
                or type(boss.get("type")) is not str
                or len(boss["type"]) > 64):
            raise ValueError("map boss must contain integer coordinates and a type")
        boss_coord = (boss["col"], boss["row"])
        if boss_coord in nodes:
            raise ValueError("map boss coordinates overlap ordinary rows")
        boss_is_current = current_coord == boss_coord
        safe_boss = {
            "col": boss["col"],
            "row": boss["row"],
            "type": boss["type"],
            "visited": boss_is_current,
            "current": boss_is_current,
        }
        boss_id = boss.get("id")
        boss_name = boss.get("name")
        if type(boss_id) is str and len(boss_id) <= 64:
            safe_boss["id"] = boss_id
        if type(boss_name) is str and len(boss_name) <= 256:
            safe_boss["name"] = boss_name
        if boss_is_current:
            if current_nodes:
                raise ValueError(
                    "ordinary row current marker conflicts with boss current_coord"
                )
            current_node = safe_boss
        else:
            if current_coord not in nodes:
                raise ValueError("map current_coord is absent from rows and boss")
            if current_nodes != [current_coord]:
                raise ValueError(
                    "map current marker is inconsistent with current_coord"
                )
            current_node = nodes[current_coord]
        raw_map = {
            "type": "map",
            "context": safe_context,
            "rows": safe_rows,
            "boss": safe_boss,
            "current_coord": {"col": col, "row": row},
        }
        return (
            act,
            raw_map,
            current_coord,
            current_node,
            set(nodes) | {boss_coord},
        )

    def _ingest_run_map_reply(self, reply: object, state: dict) -> None:
        """Validate and ingest one already-received map reply without polling."""
        act, raw_map, (col, row), current_node, graph_coords = (
            self._validated_map_reply(reply)
        )
        state_room_identity = self._bounded_run_room_identity(state)
        map_floor = raw_map["context"].get("floor")
        map_room_identity = (
            (act, map_floor)
            if type(map_floor) is int and 1 <= map_floor <= 17
            else None
        )
        room_identity = (
            state_room_identity
            if state_room_identity == map_room_identity
            else None
        )
        captured_at = time.time()
        if (type(captured_at) not in (int, float)
                or not math.isfinite(captured_at)):
            raise ValueError("map capture timestamp must be finite")
        snapshot = self._run_map_snapshots.get(act)
        survivors = []
        if snapshot is not None:
            previous_map = snapshot["map"]
            previous_boss = previous_map["boss"]
            previous_boss_coord = (
                previous_boss["col"], previous_boss["row"]
            )
            previous_current = previous_map["current_coord"]
            if ((previous_current["col"], previous_current["row"])
                    == previous_boss_coord
                    and (col, row) != previous_boss_coord):
                raise _RunMapTransitionError(
                    "map refresh follows an already-current terminal boss"
                )
            survivors = [
                node for node in snapshot["visited_nodes"]
                if (node["col"], node["row"]) in graph_coords
            ]
        authoritative_visited = {
            (raw_node["col"], raw_node["row"])
            for raw_row in raw_map["rows"]
            for raw_node in raw_row
            if raw_node["visited"]
        }
        raw_boss = raw_map["boss"]
        if raw_boss["visited"]:
            authoritative_visited.add((raw_boss["col"], raw_boss["row"]))
        candidate_visited = {
            (visited["col"], visited["row"])
            for visited in survivors
        }
        candidate_visited.add((col, row))
        if candidate_visited != authoritative_visited:
            raise _RunMapTransitionError(
                "map authoritative visits do not match buffered inventories"
            )

        self._clear_run_last_map_poll_state()

        if snapshot is None:
            snapshot = {
                "map": raw_map,
                "visited_nodes": [],
                "_coord_lookup": {},
                "ts": captured_at,
            }
            self._run_map_snapshots[act] = snapshot
        else:
            snapshot["visited_nodes"] = survivors
            snapshot["_coord_lookup"] = {
                (node["col"], node["row"]): node for node in survivors
            }
            if (self._run_current_map_coord is not None
                    and self._run_current_map_coord[0] == act
                    and self._run_current_map_coord[1:] not in graph_coords):
                self._run_current_map_coord = None
                self._run_current_map_room_identity = None
            snapshot["map"] = raw_map
            snapshot["ts"] = captured_at

        coord_lookup = snapshot["_coord_lookup"]
        node = coord_lookup.get((col, row))
        if node is None:
            node = {
                "type": current_node["type"],
                "col": col,
                "row": row,
            }
            if "id" in current_node:
                node["id"] = current_node["id"]
            coord_lookup[(col, row)] = node
            snapshot["visited_nodes"].append(node)
        self._run_current_map_coord = (act, col, row)
        self._run_current_map_room_identity = room_identity
        self._update_buffered_node_inventory(state)
        self._run_map_capture_failure_active = False

    def _detached_run_map_poll_state(
        self,
        state: dict,
        state_id: int | None = None,
        inventory_checkpoint=None,
        *,
        state_ref: object = None,
        state_fingerprint: object = _RUN_DECISION_FINGERPRINT_OMITTED,
    ):
        if state_ref is None:
            state_ref = state
        if state_fingerprint is _RUN_DECISION_FINGERPRINT_OMITTED:
            state_fingerprint = self._run_decision_state_fingerprint(state_ref)
        detached_state = {"player": self._bounded_player_snapshot(state)}
        room_identity = self._bounded_run_room_identity(state)
        if room_identity is not None:
            detached_state["context"] = {
                "act": room_identity[0],
                "floor": room_identity[1],
            }
        retained = {
            "state_id": id(state) if state_id is None else state_id,
            "state_ref": state_ref,
            "state_fingerprint": state_fingerprint,
            "state": detached_state,
        }
        if inventory_checkpoint is not None:
            retained["inventory_checkpoint"] = inventory_checkpoint
        return retained

    def _capture_run_map_state(
        self, state: dict
    ) -> bool:
        """Capture evaluation map observability without affecting gameplay."""
        metadata = self._run_map_capture_poll_metadata
        if (
            metadata is _RUN_MAP_CAPTURE_METADATA_OMITTED
            or metadata[0] is not state
        ):
            poll_state_id = None
            poll_state_ref = None
            poll_state_fingerprint = _RUN_DECISION_FINGERPRINT_OMITTED
        else:
            _, poll_state_id, poll_state_ref, poll_state_fingerprint = metadata
        inventory_checkpoint = self._buffered_inventory_checkpoint()
        self._update_buffered_node_inventory(state)
        if not self._capture_run_maps:
            return True
        if self._pending_read_only_replies > 0:
            return False
        effective_state_id = id(state) if poll_state_id is None else poll_state_id
        try:
            reply = self._send_read_only({"cmd": "get_map"})
            if self._pending_read_only_replies > 0:
                self._run_pending_map_capture = self._detached_run_map_poll_state(
                    state,
                    effective_state_id,
                    inventory_checkpoint,
                    state_ref=poll_state_ref,
                    state_fingerprint=poll_state_fingerprint,
                )
            self._ingest_run_map_reply(reply, state)
            return True
        except _RunMapTransitionError as exc:
            self._restore_buffered_inventory(inventory_checkpoint)
            if not self._run_map_capture_failure_active:
                self._report_run_logging_error("map capture failed", exc)
                self._run_map_capture_failure_active = True
            return False
        except Exception as exc:
            if not self._run_map_capture_failure_active:
                self._report_run_logging_error("map capture failed", exc)
                self._run_map_capture_failure_active = True
            return False

    def _capture_run_map_state_with_poll_metadata(
        self,
        state: dict,
        *,
        poll_state_id: int | None,
        poll_state_ref: object,
        poll_state_fingerprint: object,
    ) -> bool:
        previous = self._run_map_capture_poll_metadata
        self._run_map_capture_poll_metadata = (
            state,
            poll_state_id,
            poll_state_ref,
            poll_state_fingerprint,
        )
        try:
            return self._capture_run_map_state(state)
        finally:
            self._run_map_capture_poll_metadata = previous

    def _poll_run_map_state_once(self, state: dict):
        self._update_buffered_node_inventory(state)
        if type(state) is not dict or not self._capture_run_maps:
            return
        state_id = id(state)
        state_fingerprint = self._run_decision_state_fingerprint(state)
        if (
            self._run_last_map_poll_state_ref is state
            and state_fingerprint is not None
            and self._run_last_map_poll_state_fingerprint == state_fingerprint
        ):
            return
        if (self._run_pending_map_capture is not None
                and self._run_pending_map_capture.get("state_ref") is state):
            return
        if (self._run_map_retry_state is not None
                and self._run_map_retry_state.get("state_ref") is state):
            return
        if self._capture_run_map_state_with_poll_metadata(
            state,
            poll_state_id=state_id,
            poll_state_ref=state,
            poll_state_fingerprint=state_fingerprint,
        ):
            self._retain_run_last_map_poll_state(state, state_fingerprint)
            self._run_map_retry_state = None
        elif self._pending_read_only_replies == 0:
            self._run_map_retry_state = self._detached_run_map_poll_state(
                state,
                state_id,
                state_ref=state,
                state_fingerprint=state_fingerprint,
            )

    def _retry_run_map_poll_before_gameplay(self):
        retained = self._run_map_retry_state
        if retained is None or self._pending_read_only_replies > 0:
            return
        self._run_map_retry_state = None
        if self._capture_run_map_state_with_poll_metadata(
            retained["state"],
            poll_state_id=retained["state_id"],
            poll_state_ref=retained.get("state_ref"),
            poll_state_fingerprint=retained.get(
                "state_fingerprint", _RUN_DECISION_FINGERPRINT_OMITTED
            ),
        ):
            self._retain_run_last_map_poll_state(
                retained.get("state_ref"),
                retained.get(
                    "state_fingerprint", _RUN_DECISION_FINGERPRINT_OMITTED
                ),
            )

    def _run_decision_target(
        self, state: object, decision: object
    ) -> tuple[int, int, int] | None:
        if decision is None or type(state) is not dict:
            return None
        target = self._run_current_map_coord
        if target is None:
            return None
        state_decision = state.get("decision")
        if type(state_decision) is not str:
            return None
        if state_decision == "combat_play":
            if (
                self._run_current_map_room_identity is None
                or self._bounded_run_room_identity(state)
                    != self._run_current_map_room_identity
            ):
                return None
        else:
            fingerprint = self._run_decision_state_fingerprint(state)
            if (
                self._run_last_map_poll_state_ref is not state
                or fingerprint is None
                or fingerprint != self._run_last_map_poll_state_fingerprint
            ):
                return None
        return target

    def _append_run_decision_to_node(
        self, target: tuple[int, int, int], decision: dict
    ) -> None:
        try:
            act, col, row = target
            snapshot = self._run_map_snapshots.get(act)
            if snapshot is None:
                raise ValueError("decision target act is absent")
            node = snapshot.get("_coord_lookup", {}).get((col, row))
            if node is None:
                raise ValueError("decision target coordinate is absent")
            node["decisions"] = append_run_decision(
                node.get("decisions", []), decision
            )
        except Exception as exc:
            self._report_run_logging_error("run decision logging failed", exc)

    def _send_with_run_decision(self, state: dict, command: dict):
        decision = capture_run_decision(state, command)
        self._retry_run_map_poll_before_gameplay()
        target = self._run_decision_target(state, decision)
        reply = self._send(command)
        if decision is None or not self._is_confirmed_run_decision_reply(reply):
            return reply
        if target is None:
            target = self._run_decision_target(state, decision)
        if target is not None:
            self._append_run_decision_to_node(target, decision)
        return reply

    def _serialized_run_map_snapshots(self) -> list[dict]:
        rows = []
        for act in sorted(self._run_map_snapshots):
            snapshot = self._run_map_snapshots[act]
            rows.append({
                **self._run_metadata_row("map_snapshot"),
                "act": act,
                "map": json.loads(json.dumps(
                    snapshot["map"], ensure_ascii=False, allow_nan=False
                )),
                "visited_nodes": json.loads(json.dumps(
                    snapshot["visited_nodes"], ensure_ascii=False, allow_nan=False
                )),
                "ts": snapshot["ts"],
            })
        return rows

    @staticmethod
    def _append_run_payload(path: str, payload: str) -> Exception | None:
        """Append one locked batch, rolling back any pre-commit partial write."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        encoded = payload.encode("utf-8")
        committed = False
        try:
            with open(path, "a+b", buffering=0) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.seek(0, os.SEEK_END)
                    original_offset = f.tell()
                    try:
                        written = f.write(encoded)
                        if written != len(encoded):
                            raise OSError(
                                f"short append: wrote {written} of {len(encoded)} bytes"
                            )
                        f.flush()
                        committed = True
                    except Exception as write_exc:
                        try:
                            f.truncate(original_offset)
                            f.flush()
                        except Exception as rollback_exc:
                            raise OSError(
                                f"append rollback failed: {rollback_exc}"
                            ) from write_exc
                        raise
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            if committed:
                return exc
            raise
        return None

    def _emit_run_start(self):
        if not self._deck_history_path or self._run_start_emitted:
            return
        run_start = {
            **self._run_metadata_row("run_start"),
            "ts": self._run_started_at,
        }
        try:
            payload = json.dumps(
                run_start, ensure_ascii=False, allow_nan=False
            ) + "\n"
            post_commit_error = self._append_run_payload(
                self._deck_history_path, payload
            )
            self._run_start_emitted = True
            if post_commit_error is not None:
                self._report_run_logging_error(
                    "run start logging failed", post_commit_error
                )
        except Exception as exc:
            self._report_run_logging_error("run start logging failed", exc)
            return

    def _emit_run_outcome(self, state: dict, victory: bool,
                          status: str | None = None):
        """Flush buffered milestone + card_pick records to disk with the final
        outcome appended. Called once per run from terminal paths (game_over,
        crash, etc.)."""
        self._update_buffered_node_inventory(state)
        self._track_run_floor(state)
        if self._run_outcome_emitted:
            return
        technical_statuses = {"crash", "timeout", "stuck", "reset_failure", "invalid"}
        final_status = status or ("win" if victory else "dead")
        if final_status not in technical_statuses | {"win", "dead"}:
            final_status = "invalid"
        technical_failure_kind = (final_status if final_status in technical_statuses
                                  else None)
        outcome = {
            **self._run_metadata_row("outcome"),
            "max_floor": int(self._run_max_floor),
            "won": bool(victory) and technical_failure_kind is None,
            "boss": getattr(self, "_run_boss_id", None),
            "status": final_status,
            "technical_failure_kind": technical_failure_kind,
            "ts": time.time(),
        }
        if self._deck_history_path:
            try:
                rows = []
                if not self._run_start_emitted:
                    rows.append({
                        **self._run_metadata_row("run_start"),
                        "ts": self._run_started_at,
                    })
                rows.extend(self._serialized_run_map_snapshots())
                rows.extend({**row, "is_multiplayer": False}
                            for row in self._run_milestone_records)
                rows.extend({**row, "is_multiplayer": False}
                            for row in self._run_card_pick_records)
                rows.append(outcome)
                payload = "".join(
                    json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                    for row in rows
                )
                post_commit_error = self._append_run_payload(
                    self._deck_history_path, payload
                )
                if post_commit_error is not None:
                    self._report_run_logging_error(
                        "run outcome logging failed", post_commit_error
                    )
            except Exception as exc:
                self._report_run_logging_error("run outcome logging failed", exc)
                return
            self._run_start_emitted = True
        # Mark successful/disabled logging before downstream updates so a
        # card-bandit failure cannot duplicate the JSON outcome.
        self._run_outcome_emitted = True
        # Bandit Q update: for every card_pick this run, update Q value with
        # the run's max_floor outcome. Background-update; save periodically.
        try:
            from agent.card_scoring import update_card_q, save_card_q
            for rec in self._run_card_pick_records:
                picked = rec.get("picked")
                if picked and picked != "SKIP":
                    update_card_q(picked, self._run_max_floor)
            # Periodic save (every 100 runs to limit disk churn)
            self._q_save_counter = getattr(self, "_q_save_counter", 0) + 1
            if self._q_save_counter % 100 == 0:
                save_card_q()
        except Exception:
            pass
        self._run_milestone_records = []
        self._run_card_pick_records = []

    def _terminal_reward(self, state: dict) -> float:
        if state.get("victory", False):
            return 2.0
        return -2.0

    def _combat_check_heal(self, state: dict) -> dict:
        """Reactively use heal potions during combat when HP drops critically low.

        Called on every combat_play state so we can heal mid-fight if HP tanks.
        Only uses heal/restore potions; all other potions are handled at combat start.
        """
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        room_type = (state.get("context") or {}).get("room_type", "")
        is_boss = "boss" in room_type.lower()
        # Floor-graduated 2026-05-19: Act 1 deaths often had unused heal potions at HP=20-30%
        # because old 0.40 threshold fires too late (one more enemy hit = dead). Act 1 has
        # no margin; better to burn the potion than die holding it.
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        if is_boss:
            heal_threshold = 0.50
        elif isinstance(floor, int) and floor <= 9:
            heal_threshold = 0.55  # Act 1: heal aggressively
        else:
            heal_threshold = 0.40
        if hp_ratio >= heal_threshold:
            return state  # healthy enough, no heal needed
        potions = player.get("potions", []) or []
        for p in potions:
            name_raw = p.get("name") or {}
            name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
            desc_raw = p.get("description") or {}
            desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
            text = name + " " + desc
            if ("heal" in text or "restore" in text) and "curse" not in text:
                pidx = p.get("index", 0)
                target_type = (p.get("target_type") or "").lower()
                args: dict = {"potion_index": pidx}
                if target_type == "anyenemy":
                    args["target_index"] = 0
                new_state = self._send_with_run_decision(
                    state,
                    {"cmd": "action", "action": "use_potion", "args": args},
                )
                self._update_buffered_node_inventory(new_state)
                if new_state is None:
                    self._game_alive = False
                    break
                state = new_state
                if state.get("decision") not in ("combat_play",):
                    break
                # Re-check HP after heal — might need another heal potion
                p2 = state.get("player", {})
                hp_ratio = p2.get("hp", 80) / max(p2.get("max_hp", 80), 1)
                if hp_ratio >= 0.35:
                    break
                potions = p2.get("potions", []) or []
        return state

    def _greedy_use_potions(self, state: dict) -> dict:
        """Auto-use potions before RL policy acts (RL action space has no potion actions).

        Strategy: use strength/flex/duplication at boss/elite fights; block if low HP.
        Fire/explosive potions target enemy 0 and are used at elite+ fights.
        """
        room_type = (state.get("context") or {}).get("room_type", "")
        is_boss  = "boss" in room_type.lower()
        is_elite = "elite" in room_type.lower()
        is_tough = is_boss or is_elite

        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        potions = player.get("potions", []) or []

        for p in potions:
            name_raw = p.get("name") or {}
            name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
            desc_raw = p.get("description") or {}
            desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
            text = name + " " + desc
            target_type = (p.get("target_type") or "").lower()
            pidx = p.get("index", 0)

            use = False
            target_index = None
            # Pre-compute incoming attack damage for threat-aware decisions
            incoming_dmg = sum(
                it.get("damage", 0) * (it.get("hits") or 1)
                for e in state.get("enemies", [])
                for it in (e.get("intents") or [])
                if it.get("type", "").lower() == "attack"
            )
            hp_cur = player.get("hp", 80)
            blk_cur = player.get("block", 0)
            # Damage that bypasses current block (what we'll actually take)
            unblocked_dmg = max(0, incoming_dmg - blk_cur)

            # Late-game (floor 10+): survivability matters more than saving potions
            is_late_game = self._current_floor >= 10
            is_act2 = self._current_floor >= 16

            if ("heal" in text or "restore" in text) and "curse" not in text:
                # Heal thresholds 2026-05-19: raised Act 1 monster from 0.30→0.50.
                # Jun 10 attempted 0.75/0.60 boost regressed -0.7 floor → reverted.
                if is_boss:
                    use = hp_ratio < 0.60
                elif is_act2:
                    use = hp_ratio < 0.50
                elif is_elite or is_late_game:
                    use = hp_ratio < 0.50
                else:
                    use = hp_ratio < 0.50
            elif "block" in text:
                # Block potion: always use at boss (30 block is always worth it vs boss attacks);
                # at elite/threatening: use when damaged or incoming is severe
                threatening = incoming_dmg > 0 and unblocked_dmg >= hp_cur * 0.45
                use = is_boss or threatening or (is_elite and hp_ratio < 0.60)
            elif not is_tough and not is_late_game:
                continue  # other potions: save for elite/boss (but use freely in late game)
            elif not is_tough:
                # Late-game monster fights: use offensive/utility potions when damaged
                if "strength" in text or "flex" in text or "energy" in text:
                    use = hp_ratio < 0.60  # use offensive boost if damaged
                elif "fire" in text or "explosive" in text or "attack" in text:
                    use = hp_ratio < 0.50
                    target_index = 0 if target_type == "anyenemy" else None
                elif "weak" in text or "fear" in text or "vulnerable" in text:
                    use = hp_ratio < 0.50
                    target_index = 0 if target_type == "anyenemy" else None
                else:
                    continue  # save specialty potions for boss
            elif "strength" in text or "flex" in text:
                use = True  # always use strength/flex at elite/boss
            elif "dexterity" in text:
                use = True  # dexterity potion at elite/boss
            elif "energy" in text and "channel" not in text:
                use = is_tough  # energy potion: useful burst at both elite and boss
                target_index = 0 if target_type == "anyenemy" else None
            elif "duplicat" in text:
                # duplicator/duplication: boss always; elite if damaged (doubles best card = big swing)
                use = is_boss or (is_elite and hp_ratio < 0.60)
            elif "blessing" in text or "forge" in text:
                use = is_tough  # upgrade hand at elite/boss
            elif "fire" in text or "explosive" in text:
                use = is_tough  # damage potions at elite and boss
                target_index = 0
            elif "attack" in text:
                # attack potion: always at elite/boss — killing faster = less damage taken
                use = is_tough
                target_index = 0 if target_type == "anyenemy" else None
            elif "weak" in text or "fear" in text or "vulnerable" in text:
                use = is_tough  # fear/weak potions apply debuffs — great at elite/boss
                target_index = 0 if target_type == "anyenemy" else None
            elif "power" in text or "ancient" in text:
                use = is_boss  # power/ancient potion: save for boss
            elif "speed" in text:
                use = is_tough  # speed potion: dex bonus at elite/boss
            elif is_boss:
                use = True  # boss fight: dump any remaining unmatched potion

            if not use:
                continue

            # Always send target_index: targeted potions need enemy 0, non-targeted ignore it.
            # Omitting it causes C# to crash with "target ID is null" for single-target potions.
            args: dict = {
                "potion_index": pidx,
                "target_index": target_index if target_index is not None else 0,
            }
            new_state = self._send_with_run_decision(
                state,
                {"cmd": "action", "action": "use_potion", "args": args},
            )
            self._update_buffered_node_inventory(new_state)
            if new_state is None:
                self._game_alive = False
                break
            # Resolve card_select (Attack Potion picks a card) or proceed
            for _ in range(10):
                if new_state.get("decision") == "card_select":
                    new_state = self._send(greedy_action(new_state))
                    self._update_buffered_node_inventory(new_state)
                elif new_state.get("decision") in ("combat_play", "game_over"):
                    break
                else:
                    break
                if new_state is None:
                    break
            if (new_state is None or new_state.get("decision") == "game_over"
                    or new_state.get("type") == "error"):
                return new_state or state
            state = new_state
            # Potion slots are re-indexed after each use; restart the scan on
            # the updated state so we never send stale potion_index values.
            return self._greedy_use_potions(state)

        return state

    def _advance_to_combat(self, state: dict) -> dict | None:
        for _ in range(200):
            if state is None:
                return None
            if state.get("decision") != "game_over":
                self._poll_run_map_state_once(state)
            self._track_run_floor(state)
            if state.get("decision") == "game_over":
                return state
            if state.get("decision") == "combat_play":
                state = self._greedy_use_potions(state)
                self._update_buffered_node_inventory(state)
                return state
            cmd = None
            # Full-map path planning: maximize expected HP at boss entry over the
            # whole act DAG instead of the one-step heuristic. Falls back to
            # greedy_action on any failure.
            #
            # On by default since 2026-08-21. HP at boss entry turned out to rank
            # boss win rate exactly (Defect entered at 69% and won 36% of boss
            # fights; the other four entered at 53-63% and lost all 48), and the
            # planner is what closes that gap. Measured over 100 fixed seeds per
            # character, Act 1 clears went 10/495 -> 21/412, and every character
            # cleared Act 1 at least once — previously only Defect ever did.
            # Set STS2_MAP_PLANNER=0 to fall back to the one-step heuristic.
            if (state.get("decision") == "map_select"
                    and os.environ.get("STS2_MAP_PLANNER", "1") != "0"):
                try:
                    from agent.map_planner import choose_map_node
                    cmd = choose_map_node(self, state)
                except Exception:
                    cmd = None
            if cmd is None:
                cmd = greedy_action(state)
            # Log every card_reward decision (deck_before + offered options + picked)
            # for the deck predictor's training set. Old milestone/outcome events
            # remain unchanged — this is a strictly-additive event stream.
            if state.get("decision") == "card_reward":
                self._buffer_card_pick(state, cmd)
            state = self._send_with_run_decision(state, cmd)
            self._update_buffered_node_inventory(state)
            if state is None:
                return None
        self._dump_stuck("advance_loop_exhausted", state)
        return {
            "decision": "stuck",
            "technical_failure_kind": "stuck",
            "player": {"hp": 0, "max_hp": 80},
        }

    def _start_proc(self):
        crash_log = os.path.join(PROJECT_ROOT, "crash_stderr.log")
        self._crash_log_f = open(crash_log, "a")
        self._proc = subprocess.Popen(
            DOTNET + ["run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._crash_log_f, cwd=PROJECT_ROOT,
            start_new_session=True,  # own process group — killed with os.killpg
        )
        self._read_buf = b""
        self._pending_read_only_replies = 0
        self._run_pending_map_capture = None
        self._run_map_retry_state = None
        self._run_current_map_coord = None
        self._run_current_map_room_identity = None
        self._clear_run_last_map_poll_state()
        self._run_map_capture_poll_metadata = _RUN_MAP_CAPTURE_METADATA_OMITTED
        ready = self._read_json(timeout_sec=15.0)
        if ready is None:
            # Game process failed to produce ready message — kill it now
            self._kill_proc()
            time.sleep(1.0)  # back-off before retry

    def _kill_proc(self):
        if self._proc is not None:
            try:
                self._proc.stdin.write(b'{"cmd":"quit"}\n')
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None
        self._game_alive = False
        self._read_buf = b""
        self._pending_read_only_replies = 0
        self._run_pending_map_capture = None
        self._run_map_retry_state = None
        self._run_current_map_coord = None
        self._run_current_map_room_identity = None
        self._clear_run_last_map_poll_state()
        self._run_map_capture_poll_metadata = _RUN_MAP_CAPTURE_METADATA_OMITTED

    def _read_json(self, timeout_sec: float = 5.0, *, kill_on_failure: bool = True,
                   return_frame_outcome: bool = False,
                   stop_on_malformed: bool = False):
        def result(outcome, payload=None):
            if return_frame_outcome:
                return outcome, payload
            return payload

        def consume_buffered_frame():
            while b"\n" in self._read_buf:
                line, self._read_buf = self._read_buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(b"{"):
                    if stop_on_malformed:
                        return result("malformed_consumed")
                    continue
                try:
                    return result("valid", json.loads(line))
                except json.JSONDecodeError:
                    if stop_on_malformed:
                        return result("malformed_consumed")
                    continue
                except Exception:
                    if stop_on_malformed:
                        return result("malformed_consumed")
                    if kill_on_failure:
                        self._kill_proc()
                    return result("no_complete_frame")
            return None

        if self._proc is None:
            return result("no_complete_frame")
        try:
            fileno = self._proc.stdout.fileno()
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                buffered = consume_buffered_frame()
                if buffered is not None:
                    return buffered
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fileno], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                chunk = os.read(fileno, 4096)
                if not chunk:
                    if kill_on_failure:
                        self._kill_proc()
                    return result("eof")
                self._read_buf += chunk
            buffered = consume_buffered_frame()
            if buffered is not None:
                return buffered
            if kill_on_failure:
                self._kill_proc()
            return result("no_complete_frame")
        except Exception:
            if kill_on_failure:
                self._kill_proc()
            return result("no_complete_frame")

    def _write_read_only_frame(self, frame: bytes, timeout_sec: float) -> bool:
        """Atomically deliver one small observation frame directly to the pipe."""
        if self._proc is None:
            return False
        fd = self._proc.stdin.fileno()
        pipe_buf = os.fpathconf(fd, "PC_PIPE_BUF")
        if len(frame) > pipe_buf:
            raise ValueError("read-only protocol frame exceeds PIPE_BUF")
        # Every ordinary _send() flushes its BufferedWriter before reading a
        # reply, and kills the process if that flush fails.  Therefore a live
        # process has no older Python-buffered bytes for this direct write to
        # overtake.  POSIX pipe writes <= PIPE_BUF are delivered atomically.
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _, writable, _ = select.select([], [fd], [], remaining)
            if not writable:
                return False
            try:
                written = os.write(fd, frame)
            except BlockingIOError:
                continue
            if written != len(frame):
                raise OSError(
                    f"short atomic pipe write: {written} of {len(frame)} bytes"
                )
            return True

    def _send_read_only(self, cmd: dict, *, timeout_sec: float = 5.0):
        """Send one observational command without terminating gameplay on failure.

        The C# command loop is synchronous and emits one reply per get_map
        command before reading the next command. If this bounded read times out,
        remember that one FIFO reply remains outstanding so the next gameplay
        send can drain it before returning its own response.
        """
        if type(cmd) is not dict or cmd.get("cmd") != "get_map":
            raise ValueError("read-only protocol path only accepts get_map")
        if self._proc is None or self._pending_read_only_replies > 0:
            return None
        try:
            frame = (json.dumps(cmd) + "\n").encode()
            if not self._write_read_only_frame(frame, timeout_sec):
                return None
            frame_outcome, reply = self._read_json(
                timeout_sec=timeout_sec,
                kill_on_failure=False,
                return_frame_outcome=True,
                stop_on_malformed=True,
            )
            if frame_outcome == "no_complete_frame":
                self._pending_read_only_replies += 1
            return reply
        except Exception:
            return None

    def _send(self, cmd: dict):
        if self._proc is None:
            return None
        try:
            self._retry_run_map_poll_before_gameplay()
            while self._pending_read_only_replies > 0:
                retained_capture = self._run_pending_map_capture
                frame_outcome, pending_reply = self._read_json(
                    timeout_sec=60.0,
                    return_frame_outcome=True,
                    stop_on_malformed=True,
                )
                if frame_outcome not in {"valid", "malformed_consumed"}:
                    return None
                self._pending_read_only_replies -= 1
                self._run_pending_map_capture = None
                if retained_capture is not None and frame_outcome == "valid":
                    try:
                        self._ingest_run_map_reply(
                            pending_reply, retained_capture["state"]
                        )
                        self._retain_run_last_map_poll_state(
                            retained_capture.get("state_ref"),
                            retained_capture.get(
                                "state_fingerprint",
                                _RUN_DECISION_FINGERPRINT_OMITTED,
                            ),
                        )
                        self._run_map_retry_state = None
                    except _RunMapTransitionError as exc:
                        self._restore_buffered_inventory(
                            retained_capture.get("inventory_checkpoint")
                        )
                        if not self._run_map_capture_failure_active:
                            self._report_run_logging_error(
                                "map capture failed", exc
                            )
                            self._run_map_capture_failure_active = True
                        self._run_map_retry_state = retained_capture
                    except Exception as exc:
                        if not self._run_map_capture_failure_active:
                            self._report_run_logging_error(
                                "map capture failed", exc
                            )
                            self._run_map_capture_failure_active = True
                        self._run_map_retry_state = retained_capture
                elif retained_capture is not None:
                    self._run_map_retry_state = retained_capture
            if self._proc is None:
                return None
            self._proc.stdin.write((json.dumps(cmd) + "\n").encode())
            self._proc.stdin.flush()
            if self._game_logger is not None:
                self._game_logger.log_action(cmd)
            # DoEndTurn takes ~3-15s; killing blow triggers DetectPostCombatState
            # which can take up to ~10s for reward generation. Use 60s to be safe.
            reply = self._read_json(timeout_sec=60.0)
            if self._game_logger is not None and reply is not None:
                self._game_logger.log_state(reply)
            return reply
        except Exception:
            self._kill_proc()
            return None


def _dummy_combat_state() -> dict:
    return {
        "decision": "combat_play", "energy": 3, "round": 1,
        "hand": [{"index": 0, "id": {"en": "STRIKE"}, "cost": 1,
                  "can_play": True, "target_type": "AnyEnemy", "type": "Attack",
                  "stats": {"damage": 6}}],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [{"hp": 30, "max_hp": 30, "block": 0,
                     "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}],
    }
