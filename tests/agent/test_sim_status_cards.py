"""Burn-style status cards must cost the player HP in the sim.

`build_sim_state` drops cards `get_card_data` does not know, and the status
cards (BURN, DAZED, VOID, WOUND, SLIMED, INFECTION, FRANTIC_ESCAPE) are absent
from the sim DB because `build_card_db` iterates the advisor DB, which holds
only draftable cards. So the planner sees a hand of 3 real cards + 2 Burns as a
3-card hand and misses the end-of-turn damage entirely — it under-estimates
incoming damage on exactly the clogged decks a1 Defect accumulates.
"""
import random

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import end_turn
from agent.turn_planner import build_sim_state


def _combat_state(hand):
    return {
        "decision": "combat_play",
        "context": {"act": 1, "floor": 5, "room_type": "Monster"},
        "energy": 3,
        "player": {"hp": 50, "max_hp": 80, "block": 0, "deck": []},
        "hand": hand,
        "enemies": [{"index": 0, "name": {"en": "Mob"}, "hp": 30, "max_hp": 30,
                     "intents": []}],
    }


def test_burn_in_hand_is_counted_into_the_sim_state():
    state = _combat_state([
        {"id": "CARD.STRIKE_DEFECT", "name": {"en": "Strike"}},
        {"id": "CARD.BURN", "name": {"en": "Burn"}},
        {"id": "CARD.BURN", "name": {"en": "Burn"}},
    ])

    sim, _meta = build_sim_state(state)

    assert sim is not None
    assert sim.statuses.get("_burn_in_hand") == 2


def test_end_turn_applies_burn_damage_before_enemies_act():
    s = CombatState(hp=50, max_hp=80, energy=3, rng_seed=1)
    s.enemies = [Enemy(id="E", name="E", hp=30, max_hp=30,
                       intent={"type": "debuff", "damage": 0, "hits": 0})]
    s.statuses["_burn_in_hand"] = 2

    end_turn(s, random.Random(1))

    # 2 Burns x 2 damage, and block must not absorb it (Burn ignores block).
    assert s.hp == 46


def test_no_burn_means_no_extra_damage():
    s = CombatState(hp=50, max_hp=80, energy=3, rng_seed=1)
    s.enemies = [Enemy(id="E", name="E", hp=30, max_hp=30,
                       intent={"type": "debuff", "damage": 0, "hits": 0})]

    end_turn(s, random.Random(1))

    assert s.hp == 50
