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
