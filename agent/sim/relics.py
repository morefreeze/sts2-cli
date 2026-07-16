#!/usr/bin/env python3
"""relics.py — Relic effect registry + trigger dispatcher.

The Python combat sim has carried `CombatState.relics: list[str]` since
Phase 2 but never acted on them. This module is the minimal mechanism
that lets relic effects fire on specific events without entangling the
combat loop with relic-specific code.

API:

    fire_relics(state, trigger, **payload)
        Iterate state.relics, look up each in RELIC_HANDLERS, and call
        any handler registered for `trigger`. Handlers mutate `state`
        in-place and may consume / return values via `payload`.

    register(relic_id, trigger, handler)
        Decorator-style registration so a relic file can attach itself
        without editing a central switch.

Trigger names mirror the C# RunSimulator events:
    - "combat_start"     — first event when entering a combat room
    - "combat_end"       — fires after the player wins (state.player_won())
    - "turn_start"       — top of each player turn (block reset etc done)
    - "turn_end"         — after end_turn enemy resolution
    - "damage_taken"     — player just took HP damage (payload: amount)
    - "card_played"      — every card played (payload: card_id, type)

Adding a relic = one Python function. Test coverage is in
agent/sim/test_relics.py (TBD).
"""
from __future__ import annotations

from typing import Callable

from agent.sim.combat_state import CombatState


# Registry: relic_id → {trigger → handler}
RELIC_HANDLERS: dict[str, dict[str, Callable]] = {}


def register(relic_id: str, trigger: str):
    """Decorator that attaches `func` to relic_id × trigger."""
    def deco(func: Callable):
        RELIC_HANDLERS.setdefault(relic_id, {})[trigger] = func
        return func
    return deco


def fire_relics(state: CombatState, trigger: str, **payload) -> None:
    """Fire every relic in state.relics that has a handler for `trigger`."""
    for rid in state.relics:
        handlers = RELIC_HANDLERS.get(rid) or RELIC_HANDLERS.get(rid.upper())
        if not handlers:
            continue
        h = handlers.get(trigger)
        if h is None:
            continue
        h(state, **payload)


# ─── Built-in relic handlers ──────────────────────────────────────────


@register("BURNING_BLOOD", "combat_end")
def _burning_blood_combat_end(state: CombatState) -> None:
    """Burning Blood — Ironclad starter. Heal 6 HP at the end of combat
    if the player won. The combat_end trigger only fires on victory so
    we don't need to re-check state.player_won() here."""
    heal = 6
    new_hp = min(state.hp + heal, state.max_hp)
    state.hp = new_hp


# ─── Energy-scaling family — Phase-4 verdict's "energy growth" lever ──
# Two trigger archetypes covering ~10 relics:
#   combat_start +N energy: Lantern, Very Hot Cocoa, Venerable Tea Set (post-rest)
#   turn_start   +1 energy: Philosophers Stone, Pumpkin Candle, Sozu, Spiked
#                            Gauntlets, Velvet Choker, Whispering Earring,
#                            Blessed Antler, Blood-Soaked Rose
# Downsides on Ancient relics are not modelled — Damaru's lever is the
# energy multiplier, which is what Phase-4 expected to move the needle.
# Conservative: only the energy effect is applied.


@register("LANTERN", "combat_start")
def _lantern_combat_start(state: CombatState) -> None:
    """Common relic: start each combat with +1 energy."""
    state.energy += 1


@register("VERY_HOT_COCOA", "combat_start")
def _very_hot_cocoa_combat_start(state: CombatState) -> None:
    """Ancient: start each combat with +4 energy."""
    state.energy += 4


def _per_turn_energy(state: CombatState) -> None:
    """Shared handler used by every per-turn +1-energy relic."""
    state.energy += 1


# Bulk-register all per-turn +1-energy relics against the same handler.
for _rid in (
    "PHILOSOPHERS_STONE", "PUMPKIN_CANDLE", "SOZU", "SPIKED_GAUNTLETS",
    "VELVET_CHOKER", "WHISPERING_EARRING", "BLESSED_ANTLER",
    "BLOOD_SOAKED_ROSE", "BREAD",  # Bread: +1 from turn 2 onward; simplify to all turns
):
    register(_rid, "turn_start")(_per_turn_energy)


# ─── Combat-start +block / +buff family ───────────────────────────────


@register("ANCHOR", "combat_start")
def _anchor(state: CombatState) -> None:
    state.block += 10


@register("GORGET", "combat_start")
def _gorget(state: CombatState) -> None:
    # Plating ≈ block in our sim
    state.block += 4


@register("VAJRA", "combat_start")
def _vajra(state: CombatState) -> None:
    state.statuses["Strength"] = state.statuses.get("Strength", 0) + 1


@register("ODDLY_SMOOTH_STONE", "combat_start")
def _oddly_smooth_stone(state: CombatState) -> None:
    state.statuses["Dexterity"] = state.statuses.get("Dexterity", 0) + 1


@register("AKABEKO", "combat_start")
def _akabeko(state: CombatState) -> None:
    # Vigor — bonus damage on first attack. Approximate as +8 Strength_this_turn
    # (decays after turn 1 via state.end_turn for "_this_turn" suffix).
    state.statuses["Strength__this_turn"] = (
        state.statuses.get("Strength__this_turn", 0) + 8)


@register("BLOOD_VIAL", "combat_start")
def _blood_vial(state: CombatState) -> None:
    state.hp = min(state.hp + 2, state.max_hp)


@register("BAG_OF_MARBLES", "combat_start")
def _bag_of_marbles(state: CombatState) -> None:
    for e in state.enemies:
        if e.hp > 0:
            e.statuses["Vulnerable"] = e.statuses.get("Vulnerable", 0) + 1


@register("RED_MASK", "combat_start")
def _red_mask(state: CombatState) -> None:
    for e in state.enemies:
        if e.hp > 0:
            e.statuses["Weak"] = e.statuses.get("Weak", 0) + 1


@register("FESTIVE_POPPER", "combat_start")
def _festive_popper(state: CombatState) -> None:
    for e in state.enemies:
        if e.hp > 0:
            e.hp = max(0, e.hp - 9)


@register("BRONZE_SCALES", "combat_start")
def _bronze_scales(state: CombatState) -> None:
    state.statuses["Thorns"] = state.statuses.get("Thorns", 0) + 3


@register("BAG_OF_PREPARATION", "combat_start")
def _bag_of_preparation(state: CombatState) -> None:
    # +2 cards turn 1 — flag, the draw routine reads it
    state.statuses["_extra_draw_turn1"] = (
        state.statuses.get("_extra_draw_turn1", 0) + 2)


@register("PAPER_PHROG", "combat_start")
def _paper_phrog(state: CombatState) -> None:
    # Vulnerable damage 1.5x → 1.75x. Flag read by damage calc (if wired).
    state.statuses["_vuln_extra_mult"] = 0.25


@register("TUNGSTEN_ROD", "combat_start")
def _tungsten_rod(state: CombatState) -> None:
    state.statuses["_dmg_reduction"] = state.statuses.get("_dmg_reduction", 0) + 1


@register("BEATING_REMNANT", "combat_start")
def _beating_remnant(state: CombatState) -> None:
    state.statuses["_max_dmg_per_turn"] = 20


# ─── Per-turn buffs / debuffs ─────────────────────────────────────────


@register("BRIMSTONE", "turn_start")
def _brimstone(state: CombatState) -> None:
    state.statuses["Strength"] = state.statuses.get("Strength", 0) + 2
    for e in state.enemies:
        if e.hp > 0:
            e.statuses["Strength"] = e.statuses.get("Strength", 0) + 1


@register("MERCURY_HOURGLASS", "turn_start")
def _mercury_hourglass(state: CombatState) -> None:
    for e in state.enemies:
        if e.hp > 0:
            e.hp = max(0, e.hp - 3)


@register("RED_SKULL", "turn_start")
def _red_skull(state: CombatState) -> None:
    # +3 Strength while HP ≤ 50%. Apply per-turn so it auto-vanishes when healed.
    if state.hp <= state.max_hp // 2:
        state.statuses["Strength__this_turn"] = (
            state.statuses.get("Strength__this_turn", 0) + 3)


# ─── Turn-N specific (start-of-turn N effects) ────────────────────────


@register("CANDELABRA", "turn_start")
def _candelabra(state: CombatState) -> None:
    if state.turn == 2:
        state.energy += 2


@register("HORN_CLEAT", "turn_start")
def _horn_cleat(state: CombatState) -> None:
    if state.turn == 2:
        state.block += 14


@register("CAPTAINS_WHEEL", "turn_start")
def _captains_wheel(state: CombatState) -> None:
    if state.turn == 3:
        state.block += 18


@register("CHANDELIER", "turn_start")
def _chandelier(state: CombatState) -> None:
    if state.turn == 3:
        state.energy += 3


@register("STONE_CALENDAR", "turn_start")
def _stone_calendar(state: CombatState) -> None:
    # At end of turn 7, deal 52 to all. Approximate as start-of-turn 8 trigger.
    if state.turn == 8:
        for e in state.enemies:
            if e.hp > 0:
                e.hp = max(0, e.hp - 52)


# ─── Combat-end heals / safety net ────────────────────────────────────


@register("MEAT_ON_THE_BONE", "combat_end")
def _meat_on_the_bone(state: CombatState) -> None:
    if state.hp <= state.max_hp // 2:
        state.hp = min(state.hp + 12, state.max_hp)


# ─── Damage-taken triggers ────────────────────────────────────────────


@register("CENTENNIAL_PUZZLE", "damage_taken")
def _centennial_puzzle(state: CombatState, amount: int = 0) -> None:
    # First time lose HP each combat → draw 3 cards. Tracked via marker.
    if not state.statuses.get("_centennial_used"):
        state.statuses["_centennial_used"] = 1
        state.statuses["_extra_draw_now"] = state.statuses.get("_extra_draw_now", 0) + 3


@register("DEMON_TONGUE", "damage_taken")
def _demon_tongue(state: CombatState, amount: int = 0) -> None:
    # First time lose HP each player-turn → heal HP equal to amount. Approximate
    # "first time" as: only fires if state.lost_hp_this_turn was just set (we
    # already entered this branch only because hp dropped this turn).
    if not state.statuses.get("_demon_tongue_used_this_turn"):
        state.statuses["_demon_tongue_used_this_turn"] = 1
        state.hp = min(state.hp + amount, state.max_hp)


# ─── End-of-turn block / counter relics ───────────────────────────────


@register("ORICHALCUM", "turn_end")
def _orichalcum(state: CombatState) -> None:
    # If end turn with no block, gain 6
    if state.block == 0:
        state.block += 6


@register("CLOAK_CLASP", "turn_end")
def _cloak_clasp(state: CombatState) -> None:
    state.block += len(state.hand)


# ─── Misc impactful triggers ──────────────────────────────────────────


@register("ICE_CREAM", "turn_start")
def _ice_cream(state: CombatState) -> None:
    # Energy carries over. Hard to model perfectly in sim; approximate by
    # giving +1 starting energy on every turn after turn 1 (the carryover
    # is usually 1-2 energy in practice).
    if state.turn > 1:
        state.energy += 1


@register("PANTOGRAPH", "combat_start")
def _pantograph(state: CombatState) -> None:
    # Heal 25 HP at boss-combat start. Floor-aware: sim's combat_state has no
    # floor field directly; we encode boss combats via enemy count + HP profile.
    # Simpler: check if any enemy has hp ≥ 200 (boss-tier).
    if any(e.max_hp >= 200 for e in state.enemies):
        state.hp = min(state.hp + 25, state.max_hp)


@register("PERMAFROST", "combat_start")
def _permafrost(state: CombatState) -> None:
    # First Power played each combat → +7 block. Flag for card-play hook.
    state.statuses["_permafrost_ready"] = 1


@register("VAMBRACE", "combat_start")
def _vambrace(state: CombatState) -> None:
    # First block gained per combat doubled. Flag for card-play hook.
    state.statuses["_vambrace_ready"] = 1


@register("LIZARD_TAIL", "combat_start")
def _lizard_tail(state: CombatState) -> None:
    # When HP would drop to 0, heal to 50% max instead (once per combat).
    # Sim hook checked in state.take_damage / state.alive().
    state.statuses["_lizard_tail_ready"] = 1


@register("STRAWBERRY", "combat_start")
def _strawberry(state: CombatState) -> None:
    # Pickup-only effect; if present it means we already have +7 max HP. Sim
    # state has max_hp set elsewhere; this relic is a no-op at sim time.
    pass


@register("PEAR", "combat_start")
def _pear(state: CombatState) -> None:
    pass  # Pickup: +10 max HP; reflected in state.max_hp already


# ─── Chain counters: "every N attacks/skills/powers" ─────────────────


import random as _rng_mod


def _bump_counter(state: CombatState, key: str) -> int:
    v = state.statuses.get(key, 0) + 1
    state.statuses[key] = v
    return v


@register("KUSARIGAMA", "on_attack_play")
def _kusarigama(state: CombatState) -> None:
    # Every 3 attacks in a single turn → 6 damage to random enemy
    if state.attacks_played_this_turn > 0 and state.attacks_played_this_turn % 3 == 0:
        alive = [e for e in state.enemies if e.hp > 0]
        if alive:
            _rng_mod.choice(alive).hp -= 6


@register("LETTER_OPENER", "on_skill_play")
def _letter_opener(state: CombatState) -> None:
    n = _bump_counter(state, "_skills_played_this_turn")
    if n > 0 and n % 3 == 0:
        for e in state.enemies:
            if e.hp > 0:
                e.hp = max(0, e.hp - 5)


@register("ORNAMENTAL_FAN", "on_attack_play")
def _ornamental_fan(state: CombatState) -> None:
    if state.attacks_played_this_turn > 0 and state.attacks_played_this_turn % 3 == 0:
        state.block += 4


@register("SHURIKEN", "on_attack_play")
def _shuriken(state: CombatState) -> None:
    if state.attacks_played_this_turn > 0 and state.attacks_played_this_turn % 3 == 0:
        state.statuses["Strength"] = state.statuses.get("Strength", 0) + 1


@register("KUNAI", "on_attack_play")
def _kunai(state: CombatState) -> None:
    if state.attacks_played_this_turn > 0 and state.attacks_played_this_turn % 3 == 0:
        state.statuses["Dexterity"] = state.statuses.get("Dexterity", 0) + 1


@register("NUNCHAKU", "on_attack_play")
def _nunchaku(state: CombatState) -> None:
    # Every 10 attacks → +1 energy. Cumulative across combats? Per combat in
    # game, but for sim we use per-combat counter.
    n = _bump_counter(state, "_total_attacks_combat")
    if n > 0 and n % 10 == 0:
        state.energy += 1


@register("INK_BOTTLE", "on_card_play")
def _ink_bottle(state: CombatState, card_id="", card_type="", cost=0) -> None:
    n = _bump_counter(state, "_total_cards_combat")
    if n > 0 and n % 10 == 0:
        state.statuses["_extra_draw_now"] = state.statuses.get("_extra_draw_now", 0) + 1


@register("PEN_NIB", "on_attack_play")
def _pen_nib(state: CombatState) -> None:
    # Every 10th attack deals double damage. Set a flag; damage application
    # would need to read this. Simplified: not wired into damage path yet, so
    # we approximate by extra damage to first alive enemy.
    n = _bump_counter(state, "_pen_nib_count")
    if n > 0 and n % 10 == 0:
        alive = next((e for e in state.enemies if e.hp > 0), None)
        if alive:
            alive.hp = max(0, alive.hp - 8)  # bonus dmg approximation


@register("JOSS_PAPER", "on_exhaust")
def _joss_paper(state: CombatState) -> None:
    n = _bump_counter(state, "_joss_count")
    if n > 0 and n % 5 == 0:
        state.statuses["_extra_draw_now"] = state.statuses.get("_extra_draw_now", 0) + 1


@register("CHARONS_ASHES", "on_exhaust")
def _charons_ashes(state: CombatState) -> None:
    # Whenever you exhaust → 3 dmg to all enemies
    for e in state.enemies:
        if e.hp > 0:
            e.hp = max(0, e.hp - 3)


@register("PERMAFROST", "on_power_play")
def _permafrost_power(state: CombatState) -> None:
    # First Power each combat → +7 block (uses combat_start-set flag)
    if state.statuses.pop("_permafrost_ready", 0):
        state.block += 7


@register("GAME_PIECE", "on_power_play")
def _game_piece(state: CombatState) -> None:
    state.statuses["_extra_draw_now"] = state.statuses.get("_extra_draw_now", 0) + 1


@register("MUMMIFIED_HAND", "on_power_play")
def _mummified_hand(state: CombatState) -> None:
    # Random card in hand becomes free. Flag for play-resolution to consume.
    state.statuses["_next_card_free"] = 1


@register("BIRD_FACED_URN", "on_power_play")
def _bird_faced_urn(state: CombatState) -> None:
    # Heal 2 HP whenever you play a power
    state.hp = min(state.hp + 2, state.max_hp)


@register("INTIMIDATING_HELMET", "on_card_play")
def _intimidating_helmet(state: CombatState, card_id="", card_type="", cost=0) -> None:
    if cost >= 2:
        state.block += 4


@register("ART_OF_WAR", "turn_start")
def _art_of_war(state: CombatState) -> None:
    # +1 energy if no attacks last turn. Read flag from end-of-turn snapshot.
    if state.statuses.pop("_no_attacks_last_turn", 0):
        state.energy += 1


@register("ART_OF_WAR", "turn_end")
def _art_of_war_end(state: CombatState) -> None:
    if state.attacks_played_this_turn == 0:
        state.statuses["_no_attacks_last_turn"] = 1


@register("RIPPLE_BASIN", "turn_end")
def _ripple_basin(state: CombatState) -> None:
    if state.attacks_played_this_turn == 0:
        state.block += 4


@register("PARRYING_SHIELD", "turn_end")
def _parrying_shield(state: CombatState) -> None:
    if state.block >= 10:
        alive = [e for e in state.enemies if e.hp > 0]
        if alive:
            _rng_mod.choice(alive).hp -= 6


@register("STURDY_CLAMP", "turn_end")
def _sturdy_clamp(state: CombatState) -> None:
    # Up to 10 block persists. Mark a carryover; start_turn block-reset logic
    # zeros it normally, so we counteract by writing back min(block, 10) into
    # an attribute that start_turn protects.
    state.statuses["_carry_block"] = min(state.block, 10)


@register("STURDY_CLAMP", "turn_start")
def _sturdy_clamp_start(state: CombatState) -> None:
    carry = state.statuses.pop("_carry_block", 0)
    if carry > 0:
        state.block += carry


@register("CALIPERS", "turn_start")
def _calipers(state: CombatState) -> None:
    # Block decay -15 (instead of fully zero). Simulate by restoring up to 15
    # block at start of turn (if we have a snapshot of last-turn block).
    carry = state.statuses.pop("_calipers_carry", 0)
    if carry > 0:
        state.block += carry


@register("CALIPERS", "turn_end")
def _calipers_end(state: CombatState) -> None:
    state.statuses["_calipers_carry"] = max(0, state.block - 15)


@register("MAGIC_FLOWER", "combat_end")
def _magic_flower(state: CombatState) -> None:
    # Healing +50% — approximate by adding extra heal proportional to combat-end
    # heal-relics (Burning Blood handler already ran; bump by 3 if BB present).
    if "BURNING_BLOOD" in state.relics:
        state.hp = min(state.hp + 3, state.max_hp)
    if ("MEAT_ON_THE_BONE" in state.relics
            and state.hp < state.max_hp):
        state.hp = min(state.hp + 6, state.max_hp)


@register("STRIKE_DUMMY", "combat_start")
def _strike_dummy(state: CombatState) -> None:
    # +3 dmg per Strike-text card. Flag; damage path could read; for now
    # approximate as +1 Strength_this_combat (compounds for whole fight).
    state.statuses["Strength__this_turn"] = (
        state.statuses.get("Strength__this_turn", 0) + 1)


@register("MINIATURE_CANNON", "combat_start")
def _miniature_cannon(state: CombatState) -> None:
    # +3 dmg per upgraded attack — not modelled, approximate small bonus.
    state.statuses["Strength__this_turn"] = (
        state.statuses.get("Strength__this_turn", 0) + 1)


@register("BREAD_2", "turn_start")  # placeholder — BREAD already in energy family
def _placeholder(state: CombatState) -> None:
    pass


# Tuning Fork: every 10 Skills → +7 block (per combat)
@register("TUNING_FORK", "on_skill_play")
def _tuning_fork(state: CombatState) -> None:
    n = _bump_counter(state, "_tuning_skills")
    if n > 0 and n % 10 == 0:
        state.block += 7


@register("VEXING_PUZZLEBOX", "combat_start")
def _vexing_puzzlebox(state: CombatState) -> None:
    # Add random free card to hand — approximate as +1 extra-draw turn 1
    state.statuses["_extra_draw_turn1"] = (
        state.statuses.get("_extra_draw_turn1", 0) + 1)


@register("GREMLIN_HORN", "damage_taken")
def _gremlin_horn(state: CombatState, amount: int = 0) -> None:
    # Note: spec says "when enemy dies", not on player damage. Without an
    # enemy-death trigger we approximate per-combat: trigger via damage_taken
    # is wrong semantically — skip by registering a no-op equivalent here.
    pass


@register("RAINBOW_RING", "on_card_play")
def _rainbow_ring(state: CombatState, card_id="", card_type="", cost=0) -> None:
    # First time playing Attack + Skill + Power each turn → +1 Str/Dex
    key_map = {"Attack": "_rr_attack", "Skill": "_rr_skill", "Power": "_rr_power"}
    k = key_map.get(card_type)
    if k and not state.statuses.get(k):
        state.statuses[k] = 1
        # Check if all three played this turn
        if all(state.statuses.get(v) for v in key_map.values()):
            state.statuses["Strength"] = state.statuses.get("Strength", 0) + 1
            state.statuses["Dexterity"] = state.statuses.get("Dexterity", 0) + 1


@register("RAINBOW_RING", "turn_start")
def _rainbow_ring_reset(state: CombatState) -> None:
    for k in ("_rr_attack", "_rr_skill", "_rr_power"):
        state.statuses.pop(k, None)


@register("UNCEASING_TOP", "on_card_play")
def _unceasing_top(state: CombatState, card_id="", card_type="", cost=0) -> None:
    if not state.hand:
        state.statuses["_extra_draw_now"] = state.statuses.get("_extra_draw_now", 0) + 1


@register("RUINED_HELMET", "combat_start")
def _ruined_helmet(state: CombatState) -> None:
    state.statuses["_str_gain_first_doubled"] = 1


@register("BLACK_BLOOD", "combat_end")
def _black_blood(state: CombatState) -> None:
    # Replaces Burning Blood with heal 12 instead of 6 — approximated as +6 extra
    state.hp = min(state.hp + 6, state.max_hp)


@register("DEMON_FORM_INNATE", "combat_start")  # placeholder
def _placeholder2(state): pass


@register("UNSETTLING_LAMP", "combat_start")
def _unsettling_lamp(state: CombatState) -> None:
    state.statuses["_first_debuff_doubled"] = 1


@register("HORN_CLEAT_2", "combat_start")  # placeholder
def _placeholder3(state): pass


@register("CENTENNIAL_PUZZLE", "combat_start")
def _centennial_puzzle_reset(state: CombatState) -> None:
    state.statuses.pop("_centennial_used", None)


@register("DEMON_TONGUE", "turn_start")
def _demon_tongue_reset(state: CombatState) -> None:
    state.statuses.pop("_demon_tongue_used_this_turn", None)


# ─── Card-add triggers (out of sim scope — no-op stubs for completeness) ─


for _stub in (
    "FROZEN_EGG", "MOLTEN_EGG", "TOXIC_EGG",       # Upgrade-on-add — out of sim
    "BOOK_OF_FIVE_RINGS",                           # Heal every 5 cards added
    "ETERNAL_FEATHER", "POTION_BELT",
    "MEAL_TICKET", "REGAL_PILLOW",                  # Rest-site heals
    "OLD_COIN", "BOWLER_HAT", "LUCKY_FYSH",         # Gold relics
    "AMETHYST_AUBERGINE", "BLACK_BLOOD",
    "PRAYER_WHEEL", "WHITE_STAR",                   # Extra rewards
    "WAR_PAINT", "WHETSTONE",                        # One-shot upgrades
    "TINY_MAILBOX", "GIRYA", "SHOVEL",              # Rest-site interactions
    "PLANISPHERE", "JUZU_BRACELET",                 # ? room related
    "VEXING_PUZZLEBOX",                              # Random card add
    "BELLOWS", "STONE_CRACKER",                     # Upgrade-related
    "GAMBLING_CHIP", "LAVA_LAMP",                   # Reward / one-shot
):
    @register(_stub, "combat_start")
    def _noop(state: CombatState) -> None:
        pass


# ─── Sanity test ──────────────────────────────────────────────────────


def _sanity():
    s = CombatState(hp=40, max_hp=80, energy=3)
    s.relics = ["BURNING_BLOOD"]
    fire_relics(s, "combat_end")
    assert s.hp == 46, f"Burning Blood didn't heal: hp={s.hp}"

    # Trigger that no handler cares about → no-op
    fire_relics(s, "turn_start")
    assert s.hp == 46

    # Cap at max_hp
    s.hp = 78
    fire_relics(s, "combat_end")
    assert s.hp == 80, f"Burning Blood overshot max_hp: hp={s.hp}"

    # Unknown relic → no error
    s.relics = ["NONSENSE"]
    fire_relics(s, "combat_end")

    # Lantern: +1 energy at combat start
    s = CombatState(hp=70, max_hp=80, energy=3)
    s.relics = ["LANTERN"]
    fire_relics(s, "combat_start")
    assert s.energy == 4, f"Lantern didn't add energy: {s.energy}"

    # Very Hot Cocoa: +4 energy at combat start
    s = CombatState(hp=70, max_hp=80, energy=3)
    s.relics = ["VERY_HOT_COCOA"]
    fire_relics(s, "combat_start")
    assert s.energy == 7, f"Cocoa +4 wrong: {s.energy}"

    # Philosophers Stone: +1 energy per turn
    s = CombatState(hp=70, max_hp=80, energy=3)
    s.relics = ["PHILOSOPHERS_STONE"]
    fire_relics(s, "turn_start")
    assert s.energy == 4, f"Phil Stone turn_start: {s.energy}"
    fire_relics(s, "turn_start")
    assert s.energy == 5, f"Phil Stone repeat: {s.energy}"

    # Two relics stack
    s = CombatState(hp=70, max_hp=80, energy=3)
    s.relics = ["LANTERN", "PHILOSOPHERS_STONE"]
    fire_relics(s, "combat_start")
    assert s.energy == 4, f"Lantern alone: {s.energy}"
    fire_relics(s, "turn_start")
    assert s.energy == 5, f"Phil Stone after Lantern: {s.energy}"

    print("✓ relics sanity tests pass (BB + Lantern + Cocoa + Phil Stone)")


if __name__ == "__main__":
    _sanity()
