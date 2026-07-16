from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import play_card
from agent.turn_planner import (
    apply_vantom_slippery_mask,
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
