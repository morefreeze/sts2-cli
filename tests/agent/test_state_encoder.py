import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
import pytest
from agent.state_encoder import StateEncoder

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


@pytest.fixture
def enc():
    return StateEncoder(CARDS_JSON)


def make_state(hand=None, energy=3, player_hp=80, player_max_hp=80,
               player_block=0, enemies=None):
    return {
        "decision": "combat_play",
        "energy": energy,
        "round": 1,
        "hand": hand or [],
        "player": {"hp": player_hp, "max_hp": player_max_hp, "block": player_block, "buffs": []},
        "enemies": enemies or [],
    }


def make_card(idx, card_id="STRIKE", cost=1, can_play=True, target_type="AnyEnemy"):
    return {"index": idx, "id": {"en": card_id}, "cost": cost,
            "can_play": can_play, "target_type": target_type, "type": "Attack"}


def make_enemy(hp=30, max_hp=30, block=0):
    return {"hp": hp, "max_hp": max_hp, "block": block,
            "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}


def test_encode_returns_correct_shape(enc):
    obs = enc.encode(make_state())
    assert obs.shape == (enc.obs_size,)
    assert obs.dtype == np.float32
    assert enc.obs_size == 130


def test_encode_energy_normalized(enc):
    obs = enc.encode(make_state(energy=3))
    assert abs(obs[0] - 1.0) < 1e-5


def test_encode_hp_normalized(enc):
    obs = enc.encode(make_state(player_hp=40, player_max_hp=80))
    player_start = 1 + 1 + 80  # after energy + turn + hand
    assert abs(obs[player_start] - 0.5) < 1e-5


def test_action_mask_empty_hand(enc):
    mask = enc.action_mask(make_state(hand=[], enemies=[make_enemy()]))
    assert mask.shape == (41,)
    assert mask[40] == True   # end_turn always valid
    assert not any(mask[:40])


def test_action_mask_playable_card_with_target(enc):
    state = make_state(
        hand=[make_card(0, can_play=True, target_type="AnyEnemy")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert mask[0 * 4 + 0] == True   # play card 0 at enemy 0
    assert mask[0 * 4 + 1] == False  # enemy 1 doesn't exist
    assert mask[0 * 4 + 3] == False  # no-target invalid for AnyEnemy


def test_action_mask_self_targeting_card(enc):
    state = make_state(
        hand=[make_card(0, can_play=True, target_type="Self")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert mask[0 * 4 + 3] == True   # no-target slot valid for Self
    assert mask[0 * 4 + 0] == False  # enemy target invalid for Self


def test_action_mask_unplayable_card(enc):
    state = make_state(
        hand=[make_card(0, can_play=False, target_type="AnyEnemy")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert not any(mask[:4])


def test_decode_end_turn(enc):
    action = enc.decode(40, make_state())
    assert action == {"cmd": "action", "action": "end_turn"}


def test_decode_play_card_with_target(enc):
    state = make_state(
        hand=[make_card(0, card_id="STRIKE"), make_card(1, card_id="DEFEND")],
        enemies=[make_enemy(), make_enemy()]
    )
    action = enc.decode(0 * 4 + 1, state)  # play card 0 targeting enemy 1
    assert action["action"] == "play_card"
    assert action["args"]["card_index"] == 0
    assert action["args"]["target_index"] == 1


def test_decode_play_card_no_target(enc):
    state = make_state(hand=[make_card(0, target_type="Self")], enemies=[])
    action = enc.decode(0 * 4 + 3, state)
    assert action["action"] == "play_card"
    assert "target_index" not in action["args"]
