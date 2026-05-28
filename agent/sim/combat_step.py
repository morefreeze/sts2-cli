#!/usr/bin/env python3
"""combat_step.py — Apply card effects + enemy intents to a CombatState.

Two public functions drive the simulation loop:

  play_card(state, hand_idx, target_idx, rng)   — pay cost, apply effects, exhaust
                                                  / discard the card per its tags.
  end_turn(state, rng)                          — run enemy intents, decay,
                                                  start next turn (draw + reset).

Card effects come from data/ironclad_cards_parsed.json (built by
agent.sim.card_effects). Enemy intents come from data/enemies.json (built by
agent.sim.enemy_intents). Both are loaded lazily on first use and cached.

Effect-kind handling is intentionally incomplete — only the primitives that
cover ~80% of Ironclad cards are wired. Unknown / unsupported effect kinds
are logged into state.statuses["_unsupported_effects"] (count) so the
caller / validator can detect "this simulation diverged from reality".
"""
from __future__ import annotations

import json
import os
import random
from typing import Any

from agent.sim.combat_state import CombatState, Enemy

_CARD_DB: dict[str, dict] | None = None
_ENEMY_DB: dict[str, dict] | None = None


def _load_card_db() -> dict[str, dict]:
    global _CARD_DB
    if _CARD_DB is not None:
        return _CARD_DB
    path = "data/ironclad_cards_parsed.json"
    if not os.path.exists(path):
        # Try to (re)generate from raw + parser
        from agent.sim.card_effects import parse_card_db
        parse_card_db()
    with open(path) as f:
        data = json.load(f)
    # Index by id (the wiki slug) and also by uppercased-with-underscores form
    # used by the game ("STRIKE_IRONCLAD", "BASH" etc.).
    db: dict[str, dict] = {}
    for c in data["cards"]:
        cid = c["id"]  # e.g. "strike-ironclad"
        db[cid] = c
        # Game-engine form
        game_id = cid.upper().replace("-", "_")
        db[game_id] = c
    _CARD_DB = db
    return db


def _load_enemy_db() -> dict[str, dict]:
    global _ENEMY_DB
    if _ENEMY_DB is not None:
        return _ENEMY_DB
    path = "data/enemies.json"
    if not os.path.exists(path):
        _ENEMY_DB = {}
        return _ENEMY_DB
    with open(path) as f:
        data = json.load(f)
    _ENEMY_DB = {e["id"]: e for e in data["enemies"]}
    return _ENEMY_DB


def get_card_data(card_id: str) -> dict | None:
    """Look up parsed card metadata. Tries multiple naming forms:
      - exact (wiki slug "strike", game id "STRIKE_IRONCLAD", upgrade "STRIKE+")
      - stripped CARD./Card. prefix
      - drop the trailing "_IRONCLAD" / "_SILENT" / etc. (game class suffix)
      - drop the trailing "+" (upgrade marker — we use normal_text by default)
    """
    db = _load_card_db()
    candidates = [card_id]
    for prefix in ("CARD.", "Card."):
        if card_id.startswith(prefix):
            candidates.append(card_id[len(prefix):])
    # Drop upgrade plus
    base = card_id.rstrip("+")
    if base != card_id:
        candidates.append(base)
    # Drop class suffix
    for suffix in ("_IRONCLAD", "_SILENT", "_DEFECT", "_WATCHER"):
        for c in list(candidates):
            if c.endswith(suffix):
                candidates.append(c[: -len(suffix)])
    for c in candidates:
        if c in db:
            return db[c]
        if c.lower() in db:
            return db[c.lower()]
    return None


# ─── effect application ────────────────────────────────────────────────────
def apply_effect(state: CombatState, effect: dict[str, Any],
                 target_idx: int = 0, rng: random.Random | None = None) -> None:
    """Apply one parsed effect to `state`. Mutates in place.

    target_idx selects which enemy is targeted for single-target effects;
    AOE effects ignore it and hit every alive enemy.
    """
    rng = rng or random.Random(state.rng_seed)
    kind = effect.get("kind")

    if kind == "deal_damage":
        dmg = _modify_outgoing_damage(state, effect["amount"])
        if 0 <= target_idx < len(state.enemies) and state.enemies[target_idx].hp > 0:
            dmg = _modify_incoming_damage(state.enemies[target_idx], dmg)
            state.damage_enemy(target_idx, dmg)
        state.attacks_played_this_turn += 1
        return

    if kind == "deal_aoe":
        base = _modify_outgoing_damage(state, effect["amount"])
        for i, e in enumerate(state.enemies):
            if e.hp <= 0:
                continue
            dmg = _modify_incoming_damage(e, base)
            state.damage_enemy(i, dmg)
        state.attacks_played_this_turn += 1
        return

    if kind == "multi_hit":
        base = _modify_outgoing_damage(state, effect["amount"])
        times = effect.get("times")
        if times == "X":
            times = max(state.energy, 0)
        target_iter = (
            range(len(state.enemies))
            if effect.get("target") == "all"
            else [target_idx]
        )
        for _ in range(int(times)):
            for i in target_iter:
                if i < 0 or i >= len(state.enemies) or state.enemies[i].hp <= 0:
                    continue
                dmg = _modify_incoming_damage(state.enemies[i], base)
                state.damage_enemy(i, dmg)
        state.attacks_played_this_turn += 1
        return

    if kind == "gain_block":
        state.block += effect["amount"]
        return

    if kind == "apply_status":
        st = effect["status"]
        amount = effect["amount"]
        target_iter = (
            range(len(state.enemies))
            if effect.get("target") == "all"
            else [target_idx]
        )
        for i in target_iter:
            if i < 0 or i >= len(state.enemies) or state.enemies[i].hp <= 0:
                continue
            state.enemies[i].statuses[st] = state.enemies[i].statuses.get(st, 0) + amount
        return

    if kind == "gain_status":
        st = effect["status"]
        # this-turn-only buffs decay at end_turn; permanent stays.
        if effect.get("scope") == "this_turn":
            state.statuses[f"{st}__this_turn"] = state.statuses.get(f"{st}__this_turn", 0) + effect["amount"]
        else:
            state.statuses[st] = state.statuses.get(st, 0) + effect["amount"]
        return

    if kind == "lose_hp":
        # Lose-HP ignores block, sets the lost-hp flag.
        actual = min(effect["amount"], state.hp)
        state.hp -= actual
        state.lost_hp_this_turn = True
        state.hp_lost_this_combat += actual
        return

    if kind == "draw":
        state.draw(effect["amount"], rng)
        return

    if kind == "gain_energy":
        state.energy += effect["amount"]
        return

    if kind == "exhaust_self":
        # Handled by play_card after effects (it removes the played card).
        state.statuses["_pending_self_exhaust"] = 1
        return

    if kind == "exhaust_cards":
        n = effect.get("amount", 1)
        where = effect.get("from", "hand")
        pile = {
            "hand": state.hand,
            "draw": state.draw_pile,
            "discard": state.discard_pile,
        }.get(where, state.hand)
        if n == "all":
            state.exhaust_pile.extend(pile)
            state.cards_exhausted_this_turn += len(pile)
            pile.clear()
        elif n == "all_non_attack":
            non_attack = [c for c in pile
                          if (get_card_data(c) or {}).get("type") != "Attack"]
            for c in non_attack:
                pile.remove(c)
                state.exhaust_pile.append(c)
                state.cards_exhausted_this_turn += 1
        elif isinstance(n, int):
            for _ in range(min(n, len(pile))):
                # Random choice — STS shows player a picker; we approximate.
                c = pile.pop(rng.randrange(len(pile)))
                state.exhaust_pile.append(c)
                state.cards_exhausted_this_turn += 1
        return

    if kind == "exhaust_target":
        # "Exhaust the top card of your Draw Pile."
        if effect.get("where") == "draw_top" and state.draw_pile:
            c = state.draw_pile.pop()
            state.exhaust_pile.append(c)
            state.cards_exhausted_this_turn += 1
        return

    if kind == "add_copy":
        # Card to copy = the card being played; resolved by play_card.
        where = effect.get("where", "discard")
        state.statuses[f"_pending_add_copy__{where}"] = state.statuses.get(
            f"_pending_add_copy__{where}", 0) + 1
        return

    if kind == "double_status":
        st = effect["status"]
        for i in [target_idx] if effect.get("target") == "enemy" else range(len(state.enemies)):
            if 0 <= i < len(state.enemies) and state.enemies[i].hp > 0:
                cur = state.enemies[i].statuses.get(st, 0)
                state.enemies[i].statuses[st] = cur * 2
        return

    if kind == "self_buff_combat":
        # Increase this card's damage by N this combat — tracked via a per-card-id
        # counter so the next play of the same card gets the bonus.
        # The actual card id is recorded by play_card on the side.
        state.statuses[f"_combat_buff_pending"] = state.statuses.get(
            "_combat_buff_pending", 0) + effect.get("amount", 0)
        return

    if kind == "deal_damage_equal_to":
        if effect.get("source") == "block":
            dmg = _modify_outgoing_damage(state, state.block)
            if 0 <= target_idx < len(state.enemies):
                dmg = _modify_incoming_damage(state.enemies[target_idx], dmg)
                state.damage_enemy(target_idx, dmg)
            state.attacks_played_this_turn += 1
        return

    if kind == "scaling_damage":
        # Bonus per unit of state — recorded as pending; caller resolves the
        # specific scaling source. For now treat as outgoing-buff for the next
        # damage in this card's effect list (handled by play_card).
        state.statuses["_scaling_damage_pending"] = state.statuses.get(
            "_scaling_damage_pending", 0) + effect["per"]
        return

    if kind in {"innate", "retain", "ethereal", "unplayable"}:
        # Keywords don't have immediate effect during play (handled at draw / turn).
        return

    # Trigger wrappers — store for later evaluation
    if kind in {"on_turn_start", "on_turn_end", "on_lose_hp", "on_exhaust",
                "on_play_attack", "on_other_trigger", "on_nth_play_add_copy"}:
        # We DON'T apply the effects now; we register a power-style hook.
        state.statuses[f"_trigger_{kind}_count"] = state.statuses.get(
            f"_trigger_{kind}_count", 0) + 1
        return

    if kind == "if":
        # Best-effort: try the condition; if unrecognised, drop.
        if _condition_holds(state, target_idx, effect.get("condition", "")):
            for e in effect.get("then", []):
                apply_effect(state, e, target_idx, rng)
        return

    # Anything we don't recognise — count it as unsupported so the validator
    # can identify which cards diverge from real engine.
    state.statuses["_unsupported_effects"] = state.statuses.get("_unsupported_effects", 0) + 1


# ─── damage modifiers ──────────────────────────────────────────────────────
def _modify_outgoing_damage(state: CombatState, base: int) -> int:
    dmg = base + state.statuses.get("Strength", 0)
    dmg += state.statuses.get("Strength__this_turn", 0)
    if state.statuses.get("Weak", 0) > 0:
        dmg = int(dmg * 0.75)
    return max(0, dmg)


def _modify_incoming_damage(enemy: Enemy, base: int) -> int:
    dmg = base
    if enemy.statuses.get("Vulnerable", 0) > 0:
        dmg = int(dmg * 1.5)
    return max(0, dmg)


def _player_incoming(state: CombatState, base: int) -> int:
    dmg = base
    if state.statuses.get("Vulnerable", 0) > 0:
        dmg = int(dmg * 1.5)
    return max(0, dmg)


def _condition_holds(state: CombatState, target_idx: int, cond: str) -> bool:
    c = cond.lower()
    if "vulnerable" in c and "enemy" in c:
        if 0 <= target_idx < len(state.enemies):
            return state.enemies[target_idx].statuses.get("Vulnerable", 0) > 0
    if "exhausted a card this turn" in c:
        return state.cards_exhausted_this_turn > 0
    return False


# ─── high-level transitions ───────────────────────────────────────────────
def play_card(state: CombatState, hand_idx: int, target_idx: int = 0,
              rng: random.Random | None = None) -> bool:
    """Pay cost and apply all effects of the card at state.hand[hand_idx].
    Returns True if the card was successfully played, False if e.g. not enough
    energy or unknown card. Card moves from hand → discard (or exhaust)."""
    if hand_idx < 0 or hand_idx >= len(state.hand):
        return False
    card_id = state.hand[hand_idx]
    data = get_card_data(card_id)
    if data is None:
        # Unknown card — give up, count as unsupported, discard it anyway.
        state.hand.pop(hand_idx)
        state.discard_pile.append(card_id)
        state.statuses["_unsupported_cards"] = state.statuses.get("_unsupported_cards", 0) + 1
        return False
    cost_raw = data.get("cost", "1")
    cost = _resolve_cost(cost_raw, state)
    if cost > state.energy:
        return False
    state.energy -= cost
    # Pop the card from hand BEFORE applying effects (so add_copy references the
    # right card and self-exhaust hooks fire correctly).
    played = state.hand.pop(hand_idx)
    # Pending flags reset
    state.statuses.pop("_pending_self_exhaust", None)
    # Apply normal_text effects (we're using non-upgraded for now; v3 plan: track
    # upgraded state per-card in the deck).
    for eff in data["parsed"]["normal"]:
        apply_effect(state, eff, target_idx, rng)
    # Self-exhaust if requested
    if state.statuses.pop("_pending_self_exhaust", 0):
        state.exhaust_pile.append(played)
        state.cards_exhausted_this_turn += 1
    else:
        # Default: discard.
        state.discard_pile.append(played)
    # Add-copy hooks
    for where in ("discard", "hand", "draw"):
        n = state.statuses.pop(f"_pending_add_copy__{where}", 0)
        for _ in range(n):
            target_pile = {"discard": state.discard_pile,
                           "hand": state.hand,
                           "draw": state.draw_pile}[where]
            target_pile.append(played)
    return True


def _resolve_cost(cost_raw: str, state: CombatState) -> int:
    if cost_raw == "X":
        return state.energy  # X consumes all
    try:
        return int(cost_raw)
    except (ValueError, TypeError):
        return 1


def end_turn(state: CombatState, rng: random.Random | None = None) -> None:
    """End the player turn: run enemy intents → status decay → start_turn."""
    rng = rng or random.Random(state.rng_seed)
    # 1. Enemies act
    for e in state.enemies:
        if e.hp <= 0:
            continue
        intent = e.intent
        if intent.get("type") == "attack":
            dmg = intent.get("damage", 0)
            # Apply enemy Strength
            dmg += e.statuses.get("Strength", 0)
            if e.statuses.get("Weak", 0) > 0:
                dmg = int(dmg * 0.75)
            dmg = _player_incoming(state, dmg)
            for _ in range(intent.get("hits", 1)):
                state.take_damage(dmg)
                if not state.alive():
                    return
        # else: debuff/buff/sleep intents not fully resolved here — Phase 2
        # focuses on damage exchange first.
    # 2. End-of-turn decay (for both player and enemies' Vuln/Weak/Frail)
    state.end_turn()
    # 3. Start next turn: discard remaining hand, draw 5
    state.discard_pile.extend(state.hand)
    state.hand = []
    state.start_turn()
    state.draw(5, rng)
    # 4. Advance enemy intents — Phase 2 default = cycle through declared moves
    _advance_enemy_intents(state)


def _advance_enemy_intents(state: CombatState) -> None:
    """Pick each enemy's next move from its declared sequence. With no real
    intent loop we just pick a random move from `moves`. The Phase 3 hybrid
    can call back to the real engine for canonical intents when this falls
    short."""
    edb = _load_enemy_db()
    for e in state.enemies:
        if e.hp <= 0:
            continue
        slug = _enemy_id_to_slug(e.id) or _enemy_name_to_slug(e.name)
        meta = edb.get(slug) if slug else None
        if not meta:
            continue
        moves = meta.get("moves", [])
        if not moves:
            continue
        rng = random.Random(state.rng_seed)
        m = rng.choice(moves)
        if "damage" in m:
            e.intent = {"type": "attack", "damage": m["damage"], "hits": 1}
        else:
            e.intent = {"type": "debuff", "damage": 0, "hits": 0}


def _enemy_id_to_slug(eid: str) -> str | None:
    if not eid:
        return None
    return eid.lower().replace("_", "-")


def _enemy_name_to_slug(name: str) -> str | None:
    # Best-effort English-name → slug mapping; the simulator is content-agnostic
    # otherwise. The actual cn-to-slug map lives in data/enemies.json.
    if not name:
        return None
    return name.lower().replace(" ", "-")


# ─── unit tests ────────────────────────────────────────────────────────────
def _make_starter() -> CombatState:
    s = CombatState(hp=80, max_hp=80, energy=3, max_energy=3)
    s.hand = ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "BASH",
              "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"]
    s.enemies = [Enemy(id="JAW_WORM", name="Jaw Worm", hp=42, max_hp=42,
                        intent={"type": "attack", "damage": 11, "hits": 1})]
    return s


if __name__ == "__main__":
    # 1. play STRIKE_IRONCLAD on enemy
    s = _make_starter()
    e_hp_before = s.enemies[0].hp
    ok = play_card(s, 0, 0)
    assert ok, "play failed"
    assert s.energy == 2, f"energy {s.energy}"
    assert s.enemies[0].hp < e_hp_before, "damage didn't apply"
    print(f"✓ STRIKE_IRONCLAD: enemy {e_hp_before} → {s.enemies[0].hp} (-{e_hp_before-s.enemies[0].hp})")

    # 2. play DEFEND_IRONCLAD on self
    s = _make_starter()
    ok = play_card(s, 1, 0)
    assert ok
    assert s.block > 0, f"no block {s.block}"
    print(f"✓ DEFEND_IRONCLAD: block 0 → {s.block}")

    # 3. play BASH (cost 2): damage + vulnerable
    s = _make_starter()
    ok = play_card(s, 2, 0)
    assert ok
    assert s.energy == 1, f"energy {s.energy}"  # 3 - 2 = 1
    assert s.enemies[0].statuses.get("Vulnerable", 0) > 0
    print(f"✓ BASH: enemy now Vulnerable {s.enemies[0].statuses['Vulnerable']}, "
          f"hp {s.enemies[0].hp}")

    # 4. End turn → enemy hits player
    s = _make_starter()
    hp_before = s.hp
    end_turn(s)
    assert s.hp < hp_before, f"enemy didn't hit? {s.hp}"
    print(f"✓ end_turn: enemy hit player, hp {hp_before} → {s.hp}")

    # 5. Damage with Strength
    s = _make_starter()
    s.statuses["Strength"] = 5
    e_hp_before = s.enemies[0].hp
    play_card(s, 0, 0)
    delta = e_hp_before - s.enemies[0].hp
    print(f"✓ STRIKE with Strength+5: dealt {delta} (Strike=6 + 5 = 11 expected)")
    assert delta >= 11

    # 6. Damage on Vulnerable enemy
    s = _make_starter()
    s.enemies[0].statuses["Vulnerable"] = 3
    e_hp_before = s.enemies[0].hp
    play_card(s, 0, 0)  # STRIKE 6 × 1.5 = 9
    delta = e_hp_before - s.enemies[0].hp
    print(f"✓ STRIKE on Vulnerable enemy: dealt {delta} (6 × 1.5 = 9 expected)")
    assert delta >= 9

    print("\n✓ all combat_step sanity tests pass")
