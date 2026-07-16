#!/usr/bin/env python3
"""turn_planner.py — Exhaustive 1-turn lookahead planner for real combat.

Strategy (Jun 11): replace the PPO policy's combat decisions with search.
At every combat_play decision:
  1. Build an exact CombatState from the observed JSON (real hand, real
     enemy intents — the game TELEGRAPHS what each enemy does this turn).
  2. DFS-enumerate legal card sequences within the energy budget.
  3. For each sequence: clone → play cards in sim → run sim's end_turn()
     (replays enemy attacks hit-by-hit with block absorption, strength,
     weak/vulnerable — exactly the tested sim math).
  4. Score = HP after enemy turn (dominant) + damage/kill/energy tiebreaks.
  5. Emit the FIRST action of the best sequence as an env action int.
     Re-plan every step (handles draws/discard shifts organically).

Action encoding (state_encoder.py): int k → hand_slot=k//4, target=k%4
(0-2 = enemy index, 3 = untargeted), 40 = end_turn.

Planner falls back to None (→ caller uses model.predict) when the hand has
no sim-known cards or anything fails. BROKEN_CARDS are never played.
"""
from __future__ import annotations

import random
import re
import time

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import play_card, end_turn, get_card_data
from agent.card_scoring import BROKEN_CARDS, _card_id_norm

MAX_SEQUENCES = 500       # search budget per decision
DEADLINE_SEC = 0.8        # wall-clock budget per decision
NO_TARGET_SLOT = 3
END_TURN_ACTION = 40

# Status names as they appear in player_powers / enemy powers JSON → sim names
_STATUS_MAP = {
    "strength": "Strength", "dexterity": "Dexterity",
    "vulnerable": "Vulnerable", "weak": "Weak", "frail": "Frail",
    "thorns": "Thorns", "metallicize": "Metallicize",
    "plated armor": "PlatedArmor", "plating": "PlatedArmor",
    "barricade": "Barricade", "intangible": "Intangible",
    "ritual": "Ritual",
}


def _norm_status(name: str) -> str:
    n = (name or "").strip().lower()
    return _STATUS_MAP.get(n, name)


def _powers_to_statuses(powers) -> dict:
    out = {}
    for p in powers or []:
        name = p.get("name")
        if isinstance(name, dict):
            name = name.get("en", "")
        amt = p.get("amount", 0)
        if name and isinstance(amt, (int, float)) and amt != 0:
            out[_norm_status(str(name))] = int(amt)
    return out


def build_sim_state(state: dict) -> tuple[CombatState | None, list[dict]]:
    """Construct an exact CombatState from a combat_play JSON state.

    Returns (sim_state, hand_meta) where hand_meta[i] mirrors sim hand slot i:
    {"slot": json_hand_slot, "id": normalized_id, "target_type": str,
     "cost": resolved_int, "known": bool}.
    Unknown/broken cards are EXCLUDED from the sim hand (they'd corrupt
    search); their slots simply can't be chosen by the planner.
    """
    if not state or state.get("decision") != "combat_play":
        return None, []
    player = state.get("player") or {}
    s = CombatState(
        hp=int(player.get("hp", 0) or 0),
        max_hp=int(player.get("max_hp", 80) or 80),
        block=int(player.get("block", 0) or 0),
        energy=int(state.get("energy", 0) or 0),
        max_energy=int(state.get("max_energy", 3) or 3),
        floor=int((state.get("context") or {}).get("floor", 1) or 1),
        rng_seed=1234,
    )
    s.statuses = _powers_to_statuses(state.get("player_powers"))
    # Relics: names only in JSON; map a few key ones by name match
    relic_names = []
    for r in player.get("relics") or []:
        nm = r.get("name")
        if isinstance(nm, dict):
            nm = nm.get("en", "")
        if nm:
            relic_names.append(str(nm).upper().replace(" ", "_").replace("-", "_"))
    s.relics = relic_names

    # Enemies — in JSON order so target indices align
    for e in state.get("enemies") or []:
        intents = e.get("intents") or []
        atk = next((it for it in intents
                    if (it.get("type") or "").lower() == "attack"), None)
        if atk:
            intent = {"type": "attack",
                      "damage": int(atk.get("damage", 0) or 0),
                      "hits": int(atk.get("hits", 1) or 1)}
        else:
            intent = {"type": "debuff", "damage": 0, "hits": 0}
        name = e.get("name")
        if isinstance(name, dict):
            name = name.get("en", "")
        en = Enemy(
            id=str(name or "ENEMY").upper().replace(" ", "_"),
            name=str(name or ""),
            hp=int(e.get("hp", 1) or 1),
            max_hp=int(e.get("max_hp", 1) or 1),
            block=int(e.get("block", 0) or 0),
            intent=intent,
        )
        en.statuses = _powers_to_statuses(e.get("powers"))
        out_hp = en.hp
        if out_hp > 0:
            s.enemies.append(en)

    # Hand — only sim-known, playable, non-broken cards enter the sim hand.
    hand_meta: list[dict] = []
    for slot, c in enumerate(state.get("hand") or []):
        cid = _card_id_norm(c)
        known = (get_card_data(cid) is not None and cid not in BROKEN_CARDS
                 and bool(c.get("can_play", True)))
        cost_raw = c.get("cost")
        try:
            cost = int(cost_raw)
        except (TypeError, ValueError):
            cost = 1
        meta = {"slot": slot, "id": cid, "cost": cost,
                "target_type": str(c.get("target_type") or "").lower(),
                "known": known}
        hand_meta.append(meta)
        if known:
            s.hand.append(cid)
    # Draw pile: full deck composition is known (player.deck). Fill the sim
    # draw pile with deck − hand so draw effects (Pommel Strike, Shrug It Off)
    # pull real cards. Order is shuffled with a fixed seed — exact order is
    # unknowable but composition is exact.
    deck_ids = [_card_id_norm(c) for c in (player.get("deck") or [])]
    hand_ids = [m["id"] for m in hand_meta]
    pool = list(deck_ids)
    for hid in hand_ids:
        if hid in pool:
            pool.remove(hid)
    random.Random(99).shuffle(pool)
    s.draw_pile = pool
    return s, hand_meta


def _predict_after_enemy_turn(sim: CombatState, rng: random.Random) -> CombatState:
    """Clone + run the sim's end_turn (enemy attacks resolve exactly as the
    tested sim math: block soak per hit, strength, weak, vulnerable, thorns,
    relic triggers)."""
    c = sim.clone() if hasattr(sim, "clone") else None
    if c is None:
        import copy
        c = copy.deepcopy(sim)
    try:
        end_turn(c, rng)
    except Exception:
        pass
    return c


def _future_value(after: CombatState, est_remaining_turns: float) -> float:
    """Multi-turn value of persistent player state — fixes 1-turn myopia.

    Without this the planner NEVER plays Powers (Inflame = 0 damage this
    turn → always loses to Strike) and never ramps. Each point of:
      Strength      ≈ +2.0 dmg/turn (≈2 attacks/turn average)
      Dexterity     ≈ +1.5 block/turn
      Metallicize   ≈ +1.0 block/turn (direct)
      Thorns        ≈ +1.0 dmg/turn vs attackers
      Ritual        — enemy-side; not player
    Persistent powers registered via trigger wrappers (Demon Form etc.) are
    visible as statuses after play; on_turn_start powers add value too but
    statuses capture most of it.
    """
    v = 0.0
    v += after.statuses.get("Strength", 0) * 2.0 * est_remaining_turns
    v += after.statuses.get("Dexterity", 0) * 1.5 * est_remaining_turns
    v += after.statuses.get("Metallicize", 0) * 1.0 * est_remaining_turns
    v += after.statuses.get("Thorns", 0) * 1.0 * est_remaining_turns
    # Power-card engines registered as sim powers (Demon Form / FNP / Dark
    # Embrace wrappers live in state.powers) — flat per-engine value.
    v += len(after.powers) * 3.0 * min(est_remaining_turns, 3.0)
    return v


def _score(after: CombatState, before_seq: CombatState,
           dmg_dealt: int, kills: int, energy_left: int,
           unsupported_delta: int, est_remaining_turns: float = 3.0) -> float:
    """HP-centric: surviving HP after enemy turn dominates; small bonuses
    push damage/kills (anti-turtle, per block_reward collapse lesson);
    future-value term rewards ramping (Powers/Strength) by remaining length."""
    if after.hp <= 0:
        return -1000.0
    return (float(after.hp)
            + 0.10 * dmg_dealt
            + 2.0 * kills
            + 0.30 * energy_left
            - 2.0 * unsupported_delta
            + _future_value(after, est_remaining_turns))


def plan_action(state: dict, masks=None, lethal_only: bool = False) -> int | None:
    """Search this turn's best card sequence; return env action int for the
    FIRST step (or 40=end_turn). None → caller should fall back to policy.

    lethal_only=True (hybrid mode): only return an action when the best
    sequence KILLS ALL enemies this turn — provably correct intervention;
    everything else stays with the PPO policy. Full-planner mode (False)
    underperformed the policy in n=30 evals (0/30 reach vs ~10%): 1-turn
    lookahead can't see multi-turn block/sequencing patterns the policy knows."""
    t0 = time.time()
    sim0, hand_meta = build_sim_state(state)
    if sim0 is None:
        return None
    known_metas = [m for m in hand_meta if m["known"]]
    if not known_metas:
        return None  # nothing we can reason about — let the policy act

    rng = random.Random(0xC0FFEE)
    enemy_hp0 = sum(e.hp for e in sim0.enemies)
    unsup0 = sim0.statuses.get("_unsupported_effects", 0)
    # Estimate remaining combat length: enemy HP pool / rough deck DPT (8).
    est_turns = max(1.0, min(6.0, enemy_hp0 / 8.0))

    best_score = None
    best_first: tuple[str, int] | None = None   # (card_id, sim_target_idx)
    lethal_first: tuple[str, int] | None = None  # first action of an all-kill sequence
    counter = {"n": 0}

    def evaluate(sim: CombatState, first: tuple[str, int] | None):
        nonlocal best_score, best_first, lethal_first
        all_dead = all(e.hp <= 0 for e in sim.enemies)
        if all_dead and first is not None and lethal_first is None:
            lethal_first = first
        after = _predict_after_enemy_turn(sim, rng)
        dmg = enemy_hp0 - sum(max(0, e.hp) for e in sim.enemies)
        kills = sum(1 for e in sim.enemies if e.hp <= 0)
        unsup = sim.statuses.get("_unsupported_effects", 0) - unsup0
        sc = _score(after, sim, dmg, kills, sim.energy, max(0, unsup),
                    est_remaining_turns=est_turns)
        if best_score is None or sc > best_score:
            best_score = sc
            best_first = first

    def dfs(sim: CombatState, first: tuple[str, int] | None):
        if counter["n"] >= MAX_SEQUENCES or time.time() - t0 > DEADLINE_SEC:
            return
        counter["n"] += 1
        evaluate(sim, first)  # ending the turn here is always an option
        seen: set[tuple[str, int]] = set()
        for idx, cid in enumerate(list(sim.hand)):
            data = get_card_data(cid)
            if data is None:
                continue
            cost_raw = data.get("cost", "1")
            try:
                cost = int(cost_raw)
            except (TypeError, ValueError):
                cost = sim.energy if cost_raw == "X" else 1
            if cost > sim.energy:
                continue
            alive = [i for i, e in enumerate(sim.enemies) if e.hp > 0]
            if not alive:
                continue
            is_attack = data.get("type") == "Attack"
            targets = alive if (is_attack and len(alive) > 1) else [alive[0]]
            for t in targets:
                key = (cid, t)
                if key in seen:
                    continue  # duplicate card id + same target → symmetric
                seen.add(key)
                child = sim.clone() if hasattr(sim, "clone") else None
                if child is None:
                    import copy
                    child = copy.deepcopy(sim)
                ok = False
                try:
                    ok = play_card(child, idx, t, rng)
                except Exception:
                    ok = False
                if not ok:
                    continue
                dfs(child, first or key)

    dfs(sim0, None)

    if lethal_only:
        if lethal_first is None:
            return None  # no provable kill — let the policy play
        best_first = lethal_first

    if best_first is None:
        return END_TURN_ACTION

    cid, sim_target = best_first
    # Map back to a JSON hand slot for this card id (first matching playable slot)
    slot = next((m["slot"] for m in hand_meta
                 if m["known"] and m["id"] == cid), None)
    if slot is None or slot >= 10:
        return None
    # Target slot: anyenemy cards use the enemy index; everything else 3.
    meta = next(m for m in hand_meta if m["slot"] == slot)
    if meta["target_type"] == "anyenemy":
        target_slot = min(max(sim_target, 0), 2)
    else:
        target_slot = NO_TARGET_SLOT
    action = slot * 4 + target_slot
    if masks is not None:
        try:
            if not bool(masks[action]):
                # try the untargeted variant, then any masked variant of slot
                alt = slot * 4 + NO_TARGET_SLOT
                if bool(masks[alt]):
                    return alt
                for ts in range(4):
                    if bool(masks[slot * 4 + ts]):
                        return slot * 4 + ts
                return None
        except Exception:
            return None
    return action


# ─── Intent-aware defense override (Jun 13) ────────────────────────────────
# Narrow intervention discovered from boss replays: the policy NEVER blocks
# against telegraphed ATK intents (took 64 unblocked HP from Ceremonial Beast)
# yet wastes a Defend on a Debuff turn. Fix only the "should-block-didn't"
# error; leave the policy's (good) attack decisions alone. Never blocks when
# no attack is incoming → avoids the block_reward "block forever" collapse.

def _incoming_attack_damage(state: dict) -> int:
    """Total HP lost this turn from enemy attack intents. The engine's
    intent.damage is the resolved displayed value (already includes enemy
    Strength and player Vulnerable) — verified from replay traces where HP
    loss exactly matched the shown ATK number."""
    total = 0
    for e in state.get("enemies") or []:
        if (e.get("hp") or 0) <= 0:
            continue
        for it in e.get("intents") or []:
            if (it.get("type") or "").lower() == "attack":
                total += int(it.get("damage") or 0) * int(it.get("hits") or 1)
    return total


def _enemy_attack_damage(enemy: dict) -> int:
    total = 0
    for it in enemy.get("intents") or []:
        if (it.get("type") or "").lower() == "attack":
            total += int(it.get("damage") or 0) * int(it.get("hits") or 1)
    return total


def _card_cost(card: dict, energy: int) -> int:
    cost_raw = card.get("cost")
    try:
        return int(cost_raw)
    except (TypeError, ValueError):
        return energy if cost_raw == "X" else 1


def _card_damage_amount(card: dict) -> int:
    """Conservative single-hit damage estimate for kill-as-defense checks."""
    stats = card.get("stats") or {}
    dmg = int(stats.get("damage", 0) or 0)
    if dmg > 0:
        return dmg
    data = get_card_data(_card_id_norm(card))
    if data:
        for eff in data.get("parsed", {}).get("normal", []):
            kind = eff.get("kind")
            if kind in ("deal_damage", "deal_aoe"):
                dmg += int(eff.get("amount", 0) or 0)
            elif kind == "multi_hit":
                dmg += int(eff.get("amount", 0) or 0) * int(eff.get("hits", 1) or 1)
    return dmg


def _card_block_amount(card: dict) -> int:
    """Block a card grants. Reads JSON stats.block, falls back to sim parse."""
    stats = card.get("stats") or {}
    blk = int(stats.get("block", 0) or 0)
    if blk > 0:
        return blk
    data = get_card_data(_card_id_norm(card))
    if data:
        for eff in data.get("parsed", {}).get("normal", []):
            if eff.get("kind") == "gain_block":
                blk += int(eff.get("amount", 0) or 0)
    return blk


def _action_is_defense(state: dict, action_int: int) -> bool:
    """Is the chosen action playing a block-granting card?"""
    if action_int == END_TURN_ACTION:
        return False
    slot = action_int // 4
    hand = state.get("hand") or []
    if slot >= len(hand):
        return False
    return _card_block_amount(hand[slot]) > 0


def _masked_action_ok(masks, action: int) -> bool:
    if masks is None:
        return True
    try:
        return bool(masks[action])
    except Exception:
        return False


def _is_boss_or_elite_room(state: dict) -> bool:
    context = state.get("context") or {}
    room_type = str(
        state.get("room_type")
        or context.get("room_type")
        or context.get("room")
        or ""
    ).lower()
    return "boss" in room_type or "elite" in room_type


def _slippery_target(state: dict) -> tuple[int, int] | None:
    for idx, enemy in enumerate((state.get("enemies") or [])[:3]):
        if (enemy.get("hp") or 0) <= 0:
            continue
        for power in enemy.get("powers") or []:
            name = power.get("name")
            if isinstance(name, dict):
                name = name.get("en", "")
            if str(name or "").strip().lower() != "slippery":
                continue
            amount = int(power.get("amount", 0) or 0)
            if amount > 0:
                return idx, amount
    return None


def _card_hit_count(card: dict) -> int:
    stats = card.get("stats") or {}
    for key in ("hits", "hit_count", "times"):
        try:
            hits = int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            hits = 0
        if hits > 0:
            return hits

    text = str(card.get("description") or "")
    data = get_card_data(_card_id_norm(card))
    if data:
        text = f"{text}\n{data.get('normal_text') or ''}"
        for eff in data.get("parsed", {}).get("normal", []):
            if eff.get("kind") == "multi_hit":
                try:
                    hits = int(eff.get("times", 0) or 0)
                except (TypeError, ValueError):
                    hits = 0
                if hits > 0:
                    return hits

    low = text.lower()
    if "twice" in low:
        return 2
    m = re.search(r"\b(\d+)\s+times\b", low)
    if m:
        return max(1, int(m.group(1)))
    return 1


def _attack_action_for_target(card: dict, slot: int, target_idx: int) -> int | None:
    target_type = str(card.get("target_type") or "").lower()
    if "all" in target_type and "enem" in target_type:
        return slot * 4 + NO_TARGET_SLOT
    if "anyenemy" in target_type or "enem" in target_type:
        return slot * 4 + target_idx
    return None


def _slippery_strip_options(state: dict, masks=None) -> list[tuple[tuple, int]]:
    target = _slippery_target(state)
    if target is None:
        return []
    target_idx, slippery = target
    energy = int(state.get("energy", 0) or 0)
    options: list[tuple[tuple, int]] = []
    for slot, card in enumerate((state.get("hand") or [])[:10]):
        if not card.get("can_play", True):
            continue
        if str(card.get("type") or "").lower() != "attack":
            continue
        cost = _card_cost(card, energy)
        if cost > energy:
            continue
        action = _attack_action_for_target(card, slot, target_idx)
        if action is None or not _masked_action_ok(masks, action):
            continue
        damage = _card_damage_amount(card)
        hits = _card_hit_count(card)
        if damage <= 0 or hits <= 0:
            continue
        strips = min(slippery, hits)
        waste = max(0, damage - 1) * strips
        score = (strips, -waste, -cost, -slot)
        options.append((score, action))
    options.sort(reverse=True)
    return options


def vantom_slippery_override(state: dict, policy_action: int,
                             masks=None) -> int | None:
    """Replace a wasteful attack into Vantom's Slippery with a better stripper.

    Returns None when the policy chose defense/end_turn, when no Slippery enemy
    is present, or when the chosen attack is already the best Slippery-strip
    action. The caller can use this as a narrow safety layer around raw policy
    actions.
    """
    if not state or state.get("decision") != "combat_play":
        return None
    if policy_action == END_TURN_ACTION:
        return None
    hand = state.get("hand") or []
    slot = int(policy_action) // 4
    if slot >= len(hand):
        return None
    chosen = hand[slot]
    if str(chosen.get("type") or "").lower() != "attack":
        return None

    options = _slippery_strip_options(state, masks)
    if not options:
        return None
    best_score, best_action = options[0]
    current_score = next((score for score, action in options
                          if action == int(policy_action)), None)
    if current_score is None:
        return best_action
    if best_action != int(policy_action) and best_score > current_score:
        return best_action
    return None


def apply_vantom_slippery_mask(state: dict, masks):
    """Mask wasteful Slippery attacks while leaving defense/end_turn legal."""
    if masks is None or not state or state.get("decision") != "combat_play":
        return masks
    options = _slippery_strip_options(state, masks)
    if len(options) <= 1:
        return masks
    adjusted = masks.copy() if hasattr(masks, "copy") else list(masks)
    best_score, best_action = options[0]
    for score, action in options[1:]:
        if action != best_action and score < best_score:
            adjusted[action] = False
    adjusted[best_action] = True
    return adjusted


def _kill_attacker_override(state: dict, masks, energy: int) -> int | None:
    """Return an attack that removes incoming damage by killing an attacker."""
    candidates = []
    enemies = state.get("enemies") or []
    for target_idx, enemy in enumerate(enemies[:3]):
        if (enemy.get("hp") or 0) <= 0:
            continue
        prevented = _enemy_attack_damage(enemy)
        if prevented <= 0:
            continue
        lethal_needed = int(enemy.get("hp", 0) or 0) + int(enemy.get("block", 0) or 0)
        for slot, card in enumerate((state.get("hand") or [])[:10]):
            if not card.get("can_play", True):
                continue
            cost = _card_cost(card, energy)
            if cost > energy:
                continue
            damage = _card_damage_amount(card)
            if damage < lethal_needed:
                continue
            target_type = str(card.get("target_type") or "").lower()
            if "all" in target_type and "enem" in target_type:
                action = slot * 4 + NO_TARGET_SLOT
            elif "anyenemy" in target_type or "enem" in target_type:
                action = slot * 4 + target_idx
            else:
                continue
            if _masked_action_ok(masks, action):
                candidates.append((prevented, damage, -cost, -slot, action))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][-1]


def intent_defense_override(state: dict, masks=None,
                             danger_frac: float = 0.18) -> int | None:
    """If the enemy telegraphs an attack dealing significant UNBLOCKED damage
    and the policy isn't defending, return the action for the most efficient
    block card. None = no intervention.

    Caller must already know the policy action is NOT a defense (check via
    _action_is_defense) — this function assumes intervention is wanted."""
    player = state.get("player") or {}
    hp = int(player.get("hp", 0) or 0)
    max_hp = int(player.get("max_hp", 80) or 80)
    block = int(player.get("block", 0) or 0)
    energy = int(state.get("energy", 0) or 0)
    incoming = _incoming_attack_damage(state)
    if incoming <= 0:
        return None  # no attack this turn — never block (anti-collapse)
    unblocked = max(0, incoming - block)
    if _is_boss_or_elite_room(state):
        danger = max(6.0, 0.10 * max_hp)
    else:
        danger = max(12.0, danger_frac * max_hp)
    critical_hp = max(5.0, 0.08 * max_hp)
    if unblocked < danger and unblocked < hp and (hp - unblocked) > critical_hp:
        return None  # hit is survivable as-is; let the policy attack
    kill_action = _kill_attacker_override(state, masks, energy)
    if kill_action is not None:
        return kill_action
    # Find playable block cards; prefer highest block.
    opts = []
    for slot, c in enumerate(state.get("hand") or []):
        if not c.get("can_play", True):
            continue
        cost = _card_cost(c, energy)
        if cost > energy:
            continue
        blk = _card_block_amount(c)
        if blk > 0:
            opts.append((blk, slot, cost))
    if not opts:
        return None
    opts.sort(reverse=True)
    _, slot, _ = opts[0]
    action = slot * 4 + NO_TARGET_SLOT
    if masks is not None:
        try:
            if not bool(masks[action]):
                return None
        except Exception:
            return None
    return action


# ─── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Synthetic combat_play state: 3 Strikes + 1 Defend, 1 enemy attacking 12.
    fake = {
        "decision": "combat_play",
        "energy": 3, "max_energy": 3,
        "player": {"hp": 30, "max_hp": 80, "block": 0, "relics": []},
        "player_powers": None,
        "context": {"floor": 5},
        "hand": [
            {"index": 0, "id": "CARD.STRIKE_IRONCLAD", "cost": 1, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
            {"index": 1, "id": "CARD.STRIKE_IRONCLAD", "cost": 1, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
            {"index": 2, "id": "CARD.DEFEND_IRONCLAD", "cost": 1, "type": "Skill",
             "can_play": True, "target_type": "Self"},
            {"index": 3, "id": "CARD.BASH", "cost": 2, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
        ],
        "enemies": [
            {"name": "JAW WORM", "hp": 11, "max_hp": 44, "block": 0,
             "intents": [{"type": "attack", "damage": 12, "hits": 1}],
             "powers": None},
        ],
    }
    t0 = time.time()
    a = plan_action(fake)
    dt = time.time() - t0
    print(f"action={a} ({dt*1000:.0f}ms)")
    # Enemy at 11 HP: two strikes (6+6) kill it → no incoming damage.
    # Expect: play a Strike (slot 0 or 1, target 0) → action 0 or 4.
    assert a in (0, 4), f"expected strike→enemy0 (0/4), got {a}"
    print("✓ planner kills the 11-HP enemy instead of blocking")

    # Variant: enemy 40 HP (can't kill) — blocking is better.
    fake["enemies"][0]["hp"] = 40
    a2 = plan_action(fake)
    print(f"action={a2}")
    # Best play: defend (12 incoming, 5 block) + strikes with rest.
    # Defend = slot 2 untargeted → 2*4+3 = 11
    print("✓ planner output for unkillable enemy:", a2)
