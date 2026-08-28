from collections import Counter

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import play_card
from agent.turn_planner import (
    apply_vantom_slippery_mask,
    build_sim_state,
    intent_defense_override,
    plan_action,
    vantom_slippery_override,
)


def attack_card(cid, slot, *, damage=6, cost=1):
    return {
        "id": cid,
        "index": slot,
        "cost": cost,
        "type": "Attack",
        "can_play": True,
        "target_type": "AnyEnemy",
        "stats": {"damage": damage},
    }


def block_card(cid, slot, *, block=5, cost=1):
    return {
        "id": cid,
        "index": slot,
        "cost": cost,
        "type": "Skill",
        "can_play": True,
        "target_type": "Self",
        "stats": {"block": block},
    }


def test_defense_override_kills_attacking_enemy_before_blocking():
    state = {
        "decision": "combat_play",
        "energy": 3,
        "player": {"hp": 7, "max_hp": 80, "block": 0},
        "hand": [
            attack_card("CARD.STRIKE_IRONCLAD", 0, damage=6),
            block_card("CARD.DEFEND_IRONCLAD", 1, block=5),
        ],
        "enemies": [
            {
                "name": "Low HP attacker",
                "hp": 1,
                "max_hp": 20,
                "block": 0,
                "intents": [{"type": "attack", "damage": 12, "hits": 1}],
            },
            {
                "name": "Large attacker",
                "hp": 30,
                "max_hp": 30,
                "block": 0,
                "intents": [{"type": "attack", "damage": 10, "hits": 1}],
            },
        ],
    }

    assert intent_defense_override(state) == 0


def test_defense_override_blocks_attacks_that_are_lethal_below_danger_threshold():
    state = {
        "decision": "combat_play",
        "energy": 3,
        "player": {"hp": 9, "max_hp": 80, "block": 0},
        "hand": [
            attack_card("CARD.STRIKE_IRONCLAD", 0, damage=6),
            block_card("CARD.DEFEND_IRONCLAD", 1, block=5),
        ],
        "enemies": [
            {
                "name": "Small lethal attacker",
                "hp": 30,
                "max_hp": 30,
                "block": 0,
                "intents": [{"type": "attack", "damage": 10, "hits": 1}],
            }
        ],
    }

    assert intent_defense_override(state) == 7


def test_defense_override_blocks_attacks_that_leave_critical_hp():
    state = {
        "decision": "combat_play",
        "energy": 2,
        "player": {"hp": 9, "max_hp": 80, "block": 5},
        "hand": [
            attack_card("CARD.STRIKE_IRONCLAD", 0, damage=6),
            block_card("CARD.DEFEND_IRONCLAD", 1, block=5),
        ],
        "enemies": [
            {
                "name": "Attacker",
                "hp": 30,
                "max_hp": 30,
                "block": 0,
                "intents": [{"type": "attack", "damage": 10, "hits": 1}],
            }
        ],
    }

    assert intent_defense_override(state) == 7


def test_defense_override_is_more_conservative_in_boss_and_elite_rooms():
    base_state = {
        "decision": "combat_play",
        "energy": 2,
        "player": {"hp": 70, "max_hp": 80, "block": 0},
        "hand": [
            attack_card("CARD.STRIKE_IRONCLAD", 0, damage=6),
            block_card("CARD.DEFEND_IRONCLAD", 1, block=5),
        ],
        "enemies": [
            {
                "name": "Durable attacker",
                "hp": 30,
                "max_hp": 30,
                "block": 0,
                "intents": [{"type": "attack", "damage": 9, "hits": 1}],
            }
        ],
    }

    regular = {**base_state, "context": {"room_type": "Monster"}}
    boss = {**base_state, "context": {"room_type": "Boss"}}
    elite = {**base_state, "context": {"room_type": "Elite"}}

    assert intent_defense_override(regular) is None
    assert intent_defense_override(boss) == 7
    assert intent_defense_override(elite) == 7


def test_slippery_clamps_next_enemy_hp_loss_and_consumes_stacks():
    state = CombatState(hp=80, max_hp=80, energy=5, max_energy=5)
    state.enemies = [
        Enemy(id="VANTOM", name="Vantom", hp=50, max_hp=50,
              statuses={"Slippery": 2})
    ]
    state.hand = ["CINDER", "STRIKE_IRONCLAD", "CINDER"]

    assert play_card(state, 0, 0)
    assert state.enemies[0].hp == 49
    assert state.enemies[0].statuses["Slippery"] == 1

    assert play_card(state, 0, 0)
    assert state.enemies[0].hp == 48
    assert "Slippery" not in state.enemies[0].statuses

    assert play_card(state, 0, 0)
    assert state.enemies[0].hp == 31


def test_planner_strips_vantom_slippery_with_small_attack_before_big_attack():
    state = {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "player": {
            "hp": 80,
            "max_hp": 80,
            "block": 0,
            "deck": [
                attack_card("CARD.CINDER", 0, damage=17, cost=2),
                attack_card("CARD.STRIKE_IRONCLAD", 1, damage=6, cost=1),
            ],
        },
        "player_powers": None,
        "context": {"floor": 17, "room_type": "Boss"},
        "hand": [
            attack_card("CARD.CINDER", 0, damage=17, cost=2),
            attack_card("CARD.STRIKE_IRONCLAD", 1, damage=6, cost=1),
        ],
        "enemies": [
            {
                "name": "Vantom",
                "hp": 50,
                "max_hp": 50,
                "block": 0,
                "intents": [{"type": "buff"}],
                "powers": [
                    {
                        "name": "Slippery",
                        "description": "The next time this creature loses HP, it only loses 1 HP instead.",
                        "amount": 1,
                    }
                ],
            }
        ],
    }

    assert plan_action(state) == 4


def test_vantom_slippery_override_replaces_wasteful_attack_but_not_block():
    state = {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "player": {"hp": 80, "max_hp": 80, "block": 0},
        "hand": [
            attack_card("CARD.CINDER", 0, damage=17, cost=2),
            attack_card("CARD.STRIKE_IRONCLAD", 1, damage=6, cost=1),
            block_card("CARD.DEFEND_IRONCLAD", 2, block=5, cost=1),
        ],
        "enemies": [
            {
                "name": "Vantom",
                "hp": 173,
                "max_hp": 173,
                "block": 0,
                "intents": [{"type": "attack", "damage": 7, "hits": 1}],
                "powers": [{"name": "Slippery", "amount": 9}],
            }
        ],
    }

    assert vantom_slippery_override(state, 0) == 4
    assert vantom_slippery_override(state, 11) is None


def test_vantom_slippery_mask_blocks_wasteful_attack_only():
    state = {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "player": {"hp": 80, "max_hp": 80, "block": 0},
        "hand": [
            attack_card("CARD.CINDER", 0, damage=17, cost=2),
            attack_card("CARD.STRIKE_IRONCLAD", 1, damage=6, cost=1),
            block_card("CARD.DEFEND_IRONCLAD", 2, block=5, cost=1),
        ],
        "enemies": [
            {
                "name": "Vantom",
                "hp": 173,
                "max_hp": 173,
                "block": 0,
                "intents": [{"type": "attack", "damage": 7, "hits": 1}],
                "powers": [{"name": "Slippery", "amount": 9}],
            }
        ],
    }
    masks = [False] * 41
    masks[0] = True
    masks[4] = True
    masks[11] = True
    masks[40] = True

    adjusted = apply_vantom_slippery_mask(state, masks)

    assert not adjusted[0]
    assert adjusted[4]
    assert adjusted[11]
    assert adjusted[40]


# --- build_sim_state: real draw/discard pile order (Task 1) ----------------
#
# RunSimulator.cs now reports the ordered draw_pile/discard_pile alongside
# the pre-existing counts. Its order is top-first (index 0 = next card
# drawn) — confirmed by decompiling lib/sts2.dll (CardPile.MoveToTopInternal
# inserts at index 0; CardPileCmd's single-draw path reads
# `drawPile.Cards.FirstOrDefault()`) and empirically (ending a turn without
# playing anything redraws the reported pile in the same order). The
# Python sim's CombatState.draw() pops from the END of its list, so
# build_sim_state must store the real pile REVERSED for the sim to draw the
# correct card first — that's the behavior under test below, not just the
# JSON pass-through.

def _base_combat_play_state(**overrides):
    state = {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "player": {"hp": 50, "max_hp": 80, "block": 0, "deck": []},
        "hand": [],
        "enemies": [],
    }
    state.update(overrides)
    return state


def test_build_sim_state_uses_real_draw_pile_order_when_present():
    state = _base_combat_play_state(draw_pile=[
        {"id": "CARD.STRIKE_IRONCLAD"},
        {"id": "CARD.DEFEND_IRONCLAD"},
        {"id": "CARD.BASH"},
    ])

    sim, _ = build_sim_state(state)

    assert sim is not None
    # Behavioral check, not just a mirror of the reversal: the FIRST card the
    # sim actually draws must be the real game's top card (STRIKE_IRONCLAD,
    # reported at index 0), even though the sim's own list stores it last.
    assert sim.draw(1) == ["STRIKE_IRONCLAD"]
    assert sim.draw(2) == ["DEFEND_IRONCLAD", "BASH"]


def test_build_sim_state_falls_back_to_shuffled_deck_composition_when_draw_pile_absent():
    deck = ([{"id": "CARD.STRIKE_IRONCLAD"}] * 5
            + [{"id": "CARD.DEFEND_IRONCLAD"}] * 4
            + [{"id": "CARD.BASH"}])
    state = _base_combat_play_state(
        player={"hp": 50, "max_hp": 80, "block": 0, "deck": deck},
        hand=[
            attack_card("CARD.STRIKE_IRONCLAD", 0),
            block_card("CARD.DEFEND_IRONCLAD", 1),
        ],
        # No "draw_pile" key at all — simulates a log/replay recorded before
        # this field existed. Must fall back to the old deck-minus-hand
        # shuffle rather than crash or leave the pile empty.
    )

    sim, hand_meta = build_sim_state(state)

    assert sim is not None
    hand_ids = [m["id"] for m in hand_meta]
    assert hand_ids == ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"]
    # Order is an arbitrary fixed-seed shuffle (unchanged legacy behavior) —
    # only composition (deck - hand) is a guaranteed property.
    assert Counter(sim.draw_pile) == Counter(
        {"STRIKE_IRONCLAD": 4, "DEFEND_IRONCLAD": 3, "BASH": 1}
    )


def test_build_sim_state_empty_real_draw_pile_stays_empty():
    # A real, observed draw_pile of [] means the pile is GENUINELY empty
    # right now (everything is in hand/discard) — this must NOT be treated
    # like a missing field and fall back to the deck-composition guess,
    # which would incorrectly resurrect cards that are actually elsewhere.
    state = _base_combat_play_state(
        player={"hp": 50, "max_hp": 80, "block": 0,
                "deck": [{"id": "CARD.STRIKE_IRONCLAD"}] * 5},
        draw_pile=[],
    )

    sim, _ = build_sim_state(state)

    assert sim is not None
    assert sim.draw_pile == []
    assert sim.draw(3) == []  # nothing to draw, no crash


def test_build_sim_state_sets_discard_pile_when_present_without_reversing():
    # Discard-pile order carries no meaning in the sim (CombatState.draw()
    # always rng.shuffle()s it before treating it as the new draw pile), so
    # unlike draw_pile this is a straight pass-through — asserted explicitly
    # so a future copy-paste of the draw_pile reversal doesn't sneak in here.
    state = _base_combat_play_state(discard_pile=[
        {"id": "CARD.STRIKE_IRONCLAD"}, {"id": "CARD.BASH"},
    ])

    sim, _ = build_sim_state(state)

    assert sim is not None
    assert sim.discard_pile == ["STRIKE_IRONCLAD", "BASH"]
