from collections import Counter

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import _advance_enemy_intents, play_card
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


# --- build_sim_state / _advance_enemy_intents: intent_forecast (Task 2a) ---
#
# RunSimulator.cs now reports "intent_forecast": {"rounds": [...], "exact":
# bool, "unsupported": [...]} alongside "enemies" — a multi-turn lookahead
# resolved by the real C# MonsterMoveStateMachine + real seeded RNG, grouped
# by round with each entry carrying "enemy_index" so it aligns with the
# "enemies" array. build_sim_state reduces each round to the same
# {"type","damage","hits"} shape used for the current-turn intent, and
# _advance_enemy_intents (combat_step.py) consumes the queue one round per
# simulated end_turn(), falling back to the wiki-scraped state machine once
# it runs out (or was never populated).
#
# rounds[0] is a self-check, NOT a queued future move: RunSimulator.cs builds
# it from owner.Monster?.NextMove, identical by construction to the per-enemy
# "intents" field the state already carries — see combat_state.py:42 and
# RunSimulator.cs ~2581. Every fixture below therefore gives round 0 the same
# values as the enemy's "intents" (mirroring the real wire payload) and
# asserts that intent_forecast[0] is round 1's data, never round 0's.

def test_build_sim_state_attaches_intent_forecast_per_enemy():
    state = _base_combat_play_state(
        enemies=[
            {
                "name": "Cultist",
                "hp": 30, "max_hp": 30, "block": 0,
                "intents": [{"type": "buff"}],
            },
            {
                "name": "Jaw Worm",
                "hp": 20, "max_hp": 20, "block": 0,
                "intents": [{"type": "attack", "damage": 8, "hits": 1}],
            },
        ],
        intent_forecast={
            "rounds": [
                [
                    # Round 0 mirrors "intents" above — skipped, not queued.
                    {"enemy_index": 0, "move": "BUFF_MOVE", "type": "buff"},
                    {"enemy_index": 1, "move": "CHOMP_MOVE", "type": "attack",
                     "damage": 8, "hits": 1},
                ],
                [
                    {"enemy_index": 0, "move": "STAB_MOVE", "type": "attack",
                     "damage": 6, "hits": 1},
                    {"enemy_index": 1, "move": "THRASH_MOVE", "type": "attack",
                     "damage": 7, "hits": 2},
                ],
                [
                    {"enemy_index": 0, "move": "BUFF_MOVE", "type": "buff"},
                    {"enemy_index": 1, "move": "CHOMP_MOVE", "type": "attack",
                     "damage": 11, "hits": 1},
                ],
            ],
            "exact": True,
            "unsupported": [],
        },
    )

    sim, _ = build_sim_state(state)

    assert sim is not None
    cultist, jaw_worm = sim.enemies
    # [0] must be round 1's data, [1] round 2's — round 0 never appears.
    assert cultist.intent_forecast == [
        {"type": "attack", "damage": 6, "hits": 1},
        {"type": "debuff", "damage": 0, "hits": 0},
    ]
    assert jaw_worm.intent_forecast == [
        {"type": "attack", "damage": 7, "hits": 2},
        {"type": "attack", "damage": 11, "hits": 1},
    ]


def test_build_sim_state_intent_forecast_truncates_at_first_unsupported_round():
    # Enemy 0 has no entry in round 2 (e.g. RunSimulator.cs hit an unknown
    # branch type and stopped producing rounds for it beyond that point) —
    # the queue must stop there rather than treat the gap as "does nothing",
    # so _advance_enemy_intents falls back to the state machine once it's
    # empty. Round 0 mirrors "intents" and is skipped regardless.
    state = _base_combat_play_state(
        enemies=[
            {"name": "Boss", "hp": 100, "max_hp": 100, "block": 0,
             "intents": [{"type": "attack", "damage": 20, "hits": 1}]},
        ],
        intent_forecast={
            "rounds": [
                [{"enemy_index": 0, "move": "SLAM", "type": "attack",
                  "damage": 20, "hits": 1}],
                [{"enemy_index": 0, "move": "SLASH", "type": "attack",
                  "damage": 15, "hits": 1}],
                [],
            ],
            "exact": False,
            "unsupported": ["enemy 0 (BOSS): unsupported state type Foo"],
        },
    )

    sim, _ = build_sim_state(state)

    assert sim is not None
    assert sim.enemies[0].intent_forecast == [
        {"type": "attack", "damage": 15, "hits": 1},
    ]


def test_build_sim_state_intent_forecast_absent_leaves_queue_empty():
    # Older logs/replays predate the "intent_forecast" field entirely.
    state = _base_combat_play_state(
        enemies=[
            {"name": "Slime", "hp": 10, "max_hp": 10, "block": 0,
             "intents": [{"type": "attack", "damage": 3, "hits": 1}]},
        ],
    )

    sim, _ = build_sim_state(state)

    assert sim is not None
    assert sim.enemies[0].intent_forecast == []


def test_advance_enemy_intents_after_build_sim_state_skips_current_round():
    # Integration regression: chains the REAL pipeline (build_sim_state ->
    # _advance_enemy_intents) instead of hand-constructing intent_forecast on
    # a bare Enemy like the unit tests above/below do. RunSimulator.cs's
    # round 0 is, by construction, identical to "intents" (the field already
    # in use as the enemy's current move) — see combat_state.py:42. Before
    # the fix, build_sim_state queued round 0 as forecast[0], so this first
    # _advance_enemy_intents() call re-installed the move the enemy had just
    # used (11 dmg x1) instead of advancing to round 1's real next move
    # (8 dmg x2). This must fail against the pre-fix code.
    state = _base_combat_play_state(
        enemies=[
            {
                "name": "Cultist",
                "hp": 30, "max_hp": 30, "block": 0,
                "intents": [{"type": "attack", "damage": 11, "hits": 1}],
            },
        ],
        intent_forecast={
            "rounds": [
                # Round 0: mirrors "intents" above exactly, as the real
                # wire payload always does.
                [{"enemy_index": 0, "move": "DARK_STRIKE", "type": "attack",
                  "damage": 11, "hits": 1}],
                # Round 1: the actual next move.
                [{"enemy_index": 0, "move": "RITUAL", "type": "attack",
                  "damage": 8, "hits": 2}],
            ],
            "exact": True,
            "unsupported": [],
        },
    )

    sim, _ = build_sim_state(state)
    assert sim is not None
    cultist = sim.enemies[0]
    # Sanity check the fixture: current intent is the just-used 11 dmg x1
    # move, matching round 0 by construction.
    assert cultist.intent == {"type": "attack", "damage": 11, "hits": 1}

    _advance_enemy_intents(sim)

    # Must advance to round 1's move (8 dmg x2), not repeat round 0 (11 x1).
    assert cultist.intent == {"type": "attack", "damage": 8, "hits": 2}


def test_advance_enemy_intents_consumes_forecast_before_state_machine():
    state = CombatState(hp=80, max_hp=80)
    e = Enemy(id="TOTALLY_UNKNOWN_MONSTER_XYZ", name="Mystery", hp=10, max_hp=10,
              intent={"type": "debuff", "damage": 0, "hits": 0})
    e.intent_forecast = [
        {"type": "attack", "damage": 9, "hits": 2},
        {"type": "attack", "damage": 4, "hits": 1},
    ]
    state.enemies = [e]

    _advance_enemy_intents(state)

    assert e.intent == {"type": "attack", "damage": 9, "hits": 2}
    assert e.intent_forecast == [{"type": "attack", "damage": 4, "hits": 1}]


def test_advance_enemy_intents_falls_back_once_forecast_exhausted():
    # An unknown monster id has no spire-codex/legacy data either, so once
    # the forecast queue empties, the existing fallback is a deterministic
    # no-op (leaves e.intent as whatever it last was) — proving the old
    # behavior still runs unchanged rather than crashing or looping forever.
    state = CombatState(hp=80, max_hp=80)
    e = Enemy(id="TOTALLY_UNKNOWN_MONSTER_XYZ", name="Mystery", hp=10, max_hp=10,
              intent={"type": "attack", "damage": 9, "hits": 2})
    e.intent_forecast = []
    state.enemies = [e]

    _advance_enemy_intents(state)

    assert e.intent == {"type": "attack", "damage": 9, "hits": 2}
    assert e.intent_forecast == []


def test_advance_enemy_intents_ignores_dead_enemies():
    state = CombatState(hp=80, max_hp=80)
    e = Enemy(id="DEAD_ENEMY", name="Dead", hp=0, max_hp=10,
              intent={"type": "debuff", "damage": 0, "hits": 0})
    e.intent_forecast = [{"type": "attack", "damage": 9, "hits": 2}]
    state.enemies = [e]

    _advance_enemy_intents(state)

    # Dead enemies are skipped entirely — forecast untouched.
    assert e.intent_forecast == [{"type": "attack", "damage": 9, "hits": 2}]
