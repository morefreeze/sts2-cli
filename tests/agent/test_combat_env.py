import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import pytest
from agent.combat_env import CombatEnv, greedy_action

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def test_env_action_space_size():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.action_space.n == 41


def test_env_observation_space_shape():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.observation_space.shape == (130,)


def test_reward_combat_win():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._combat_start_player_max_hp = 80
    # Combat win with full HP: 1.0 * (80/80) = 1.0
    assert abs(env._combat_win_reward({"player": {"hp": 80, "max_hp": 80}}) - 1.0) < 1e-5
    # Combat win with half HP: 1.0 * (40/80) = 0.5
    assert abs(env._combat_win_reward({"player": {"hp": 40, "max_hp": 80}}) - 0.5) < 1e-5


def test_reward_terminal():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    assert env._terminal_reward({"victory": False}) == -0.5
    assert env._terminal_reward({"victory": True}) == 2.0


def test_shaping_reward_damage():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._prev_enemy_hp = 30
    env._prev_player_hp = 80
    env._combat_start_enemy_hp = 30
    env._combat_start_player_max_hp = 80
    # Deal 10 damage to enemy, take 0: +0.02 * 10/30 = +0.00667
    r = env._shaping_reward({"enemies": [{"hp": 20}], "player": {"hp": 80}})
    assert r > 0
    assert abs(r - 0.02 * 10 / 30) < 1e-5


def test_reset_returns_correct_obs_shape():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    obs, info = env.reset()
    assert obs.shape == (130,)


def test_step_dry_run_terminates():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env.reset()
    obs, reward, terminated, truncated, info = env.step(40)  # end_turn
    assert terminated  # dry_run always terminates


def test_greedy_action_map_select():
    state = {
        "decision": "map_select",
        "choices": [
            {"col": 1, "row": 3, "type": "rest"},
            {"col": 2, "row": 3, "type": "enemy"},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "select_map_node"
    # col and row must come from the same node (not independently sampled)
    col = action["args"]["col"]
    row = action["args"]["row"]
    valid_pairs = {(c["col"], c["row"]) for c in state["choices"]}
    assert (col, row) in valid_pairs


def test_greedy_action_card_reward():
    state = {
        "decision": "card_reward",
        "cards": [{"index": 0}]
    }
    action = greedy_action(state)
    assert action["action"] == "select_card_reward"


def test_greedy_action_rest_heal():
    state = {
        "decision": "rest_site",
        "options": [
            {"index": 0, "option_id": "SMITH", "is_enabled": True},
            {"index": 1, "option_id": "HEAL", "is_enabled": True},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "choose_option"
    assert action["args"]["option_index"] == 1  # HEAL preferred
