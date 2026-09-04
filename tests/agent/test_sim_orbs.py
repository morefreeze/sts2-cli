"""Orbs — Defect's core mechanic, previously absent from the sim entirely.

The sim modelled attacks, block and statuses, i.e. Ironclad's game. Every
Defect deck revolves around channelling orbs and evoking them, so a sim without
orbs cannot represent a Defect deck at all — which is why search (turn_planner,
combat_simulator rollouts) could never compete with the trained policy on
Defect no matter how the card text was parsed.
"""
import random

from agent.sim.combat_state import CombatState, Enemy


def _state(**kw) -> CombatState:
    s = CombatState(hp=60, max_hp=80, energy=3, rng_seed=1)
    s.enemies = [Enemy(id="E", name="E", hp=50, max_hp=50,
                       intent={"type": "attack", "damage": 5, "hits": 1})]
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_channel_adds_an_orb():
    s = _state()

    s.channel("Frost")

    assert s.orbs == ["Frost"]


def test_channel_beyond_capacity_evokes_the_oldest():
    """Slots are finite: channelling into a full ring evokes the leftmost."""
    s = _state(orb_slots=3)
    for _ in range(3):
        s.channel("Frost")

    s.channel("Lightning")

    assert len(s.orbs) == 3
    assert s.orbs[-1] == "Lightning"
    assert s.orbs[0] == "Frost"


def test_frost_passive_fires_from_end_turn():
    """end_turn must tick orb passives, or channelled orbs do nothing at all.

    Asserted on the explicit counter rather than on resulting block: block is
    consumed by the enemy attack inside end_turn, so a "block >= before" style
    check passes trivially at block 0 and tests nothing.
    """
    from agent.sim.combat_step import end_turn
    s = _state()
    s.channel("Frost")

    end_turn(s, random.Random(1))

    assert s.statuses.get("_orb_block_last_turn", 0) == 2


def test_evoke_frost_grants_block_and_removes_the_orb():
    s = _state()
    s.channel("Frost")

    gained = s.evoke(random.Random(1))

    assert s.orbs == []
    assert gained is not None
    assert s.block >= 5


def test_evoke_lightning_damages_an_enemy():
    s = _state()
    s.channel("Lightning")
    hp_before = s.enemies[0].hp

    s.evoke(random.Random(1))

    assert s.enemies[0].hp < hp_before


def test_evoke_on_empty_ring_is_a_no_op():
    s = _state()

    assert s.evoke(random.Random(1)) is None
    assert s.orbs == []


def test_parser_emits_channel_effect():
    from agent.sim.card_effects import parse_card_text

    effects, _ = parse_card_text("Channel 1 Frost.")

    assert [e["kind"] for e in effects] == ["channel"]
    assert effects[0]["orb"] == "Frost"
    assert effects[0]["amount"] == 1


def test_parser_emits_evoke_effect():
    from agent.sim.card_effects import parse_card_text

    effects, _ = parse_card_text("Evoke your rightmost Orb.")

    assert [e["kind"] for e in effects] == ["evoke"]


def test_channel_effect_applies_to_state():
    from agent.sim.combat_step import apply_effect

    s = _state()
    apply_effect(s, {"kind": "channel", "orb": "Lightning", "amount": 2},
                 0, random.Random(1))

    assert s.orbs == ["Lightning", "Lightning"]
