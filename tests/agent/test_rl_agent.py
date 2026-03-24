import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def make_combat_state():
    return {
        "decision": "combat_play", "energy": 3, "round": 1,
        "hand": [{"index": 0, "id": {"en": "STRIKE"}, "cost": 1,
                  "can_play": True, "target_type": "AnyEnemy", "type": "Attack"}],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [{"hp": 30, "max_hp": 30, "block": 0,
                     "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}],
    }


def test_rl_agent_act_returns_cmd_dict():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([0]), None)  # action 0: play card 0 at enemy 0

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    state = make_combat_state()
    action = agent.act(state)
    assert isinstance(action, dict)
    assert action.get("cmd") == "action"
    assert action.get("action") in ("play_card", "end_turn")


def test_rl_agent_end_turn_action():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([40]), None)  # end_turn

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    action = agent.act(make_combat_state())
    assert action == {"cmd": "action", "action": "end_turn"}


def test_rl_agent_passes_action_mask_to_predict():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([40]), None)

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    agent.act(make_combat_state())
    call_kwargs = mock_model.predict.call_args[1]
    assert "action_masks" in call_kwargs
    assert call_kwargs["action_masks"].shape == (1, 41)
