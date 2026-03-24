import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest, json
from unittest.mock import MagicMock, patch

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


@pytest.fixture
def agent():
    from agent.llm_agent import LLMAgent
    return LLMAgent(api_key="test-key", model="claude-sonnet-4-6", cards_json=CARDS_JSON)


def make_map_state():
    return {
        "decision": "map_select",
        "act": 1, "floor": 5,
        "player": {"hp": 65, "max_hp": 80, "gold": 50,
                   "relics": [{"name": {"en": "Burning Blood"}}],
                   "deck": [
                       {"id": {"en": "STRIKE"}, "type": "Attack"},
                       {"id": {"en": "DEFEND"}, "type": "Skill"},
                   ]},
        "choices": [
            {"col": 0, "row": 1, "type": "enemy"},
            {"col": 1, "row": 1, "type": "rest"},
        ],
    }


def test_deck_summary(agent):
    deck = [
        {"id": {"en": "STRIKE"}, "type": "Attack"},
        {"id": {"en": "STRIKE"}, "type": "Attack"},
        {"id": {"en": "DEFEND"}, "type": "Skill"},
        {"id": {"en": "CORRUPTION"}, "type": "Power"},
    ]
    summary = agent._deck_summary(deck)
    assert "2 attack" in summary.lower()
    assert "1 skill" in summary.lower()
    assert "1 power" in summary.lower()


def test_prune_state_removes_zh(agent):
    state = {
        "decision": "map_select",
        "name": {"en": "Elite", "zh": "精英"},
        "player": {"hp": 70, "max_hp": 80, "deck": [], "relics": [], "gold": 0},
    }
    pruned = agent._prune_state(state)
    assert "zh" not in json.dumps(pruned)


def test_act_returns_map_select_action(agent):
    state = make_map_state()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text='{"choice": 1, "reason": "take the rest site"}')]

    with patch.object(agent.client.messages, 'create', return_value=mock_resp):
        action = agent.act(state)

    assert action["action"] == "select_map_node"
    assert action["args"]["col"] == 1
    assert action["args"]["row"] == 1


def test_act_falls_back_to_index_0_on_bad_json(agent):
    state = make_map_state()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text='not valid json')]

    with patch.object(agent.client.messages, 'create', return_value=mock_resp):
        action = agent.act(state)

    assert action["args"]["col"] == 0  # fallback to choice 0
