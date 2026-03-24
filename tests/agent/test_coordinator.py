import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import pytest
from unittest.mock import MagicMock, patch

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def make_state(decision, **kwargs):
    base = {"type": "decision", "decision": decision,
            "act": 1, "floor": 1,
            "player": {"hp": 80, "max_hp": 80, "gold": 0, "deck": [], "relics": []}}
    base.update(kwargs)
    return base


def make_game_over(victory=True, hp=60, max_hp=80):
    return {"type": "decision", "decision": "game_over",
            "victory": victory, "act": 3, "floor": 50,
            "player": {"hp": hp, "max_hp": max_hp}}


def test_coordinator_routes_combat_to_rl():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    llm = MagicMock()
    coord = GameCoordinator(rl_agent=rl, llm_agent=llm)

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("combat_play", energy=3, round=1, hand=[], enemies=[]),
            make_game_over(victory=True),
        ]):
            result = coord.run_game("Ironclad", "test")

    rl.act.assert_called_once()
    llm.act.assert_not_called()
    assert result["victory"] is True


def test_coordinator_routes_map_select_to_llm():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    llm = MagicMock()
    llm.act.return_value = {"cmd": "action", "action": "select_map_node",
                            "args": {"col": 0, "row": 1}}
    coord = GameCoordinator(rl_agent=rl, llm_agent=llm)

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("map_select", choices=[{"col": 0, "row": 1, "type": "rest"}]),
            make_game_over(victory=False),
        ]):
            coord.run_game("Ironclad", "test")

    llm.act.assert_called_once()
    rl.act.assert_not_called()


def test_coordinator_uses_greedy_when_no_llm():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    coord = GameCoordinator(rl_agent=rl, llm_agent=None)  # no LLM

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("map_select", choices=[{"col": 0, "row": 1, "type": "rest"}]),
            make_game_over(victory=False),
        ]):
            result = coord.run_game("Ironclad", "test")

    # Should not crash; greedy_action handles map_select
    assert result is not None


def test_coordinator_game_over_result_structure():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    coord = GameCoordinator(rl_agent=rl, llm_agent=None)

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("combat_play", energy=3, round=1, hand=[], enemies=[]),
            make_game_over(victory=True, hp=60, max_hp=80),
        ]):
            result = coord.run_game("Ironclad", "test_seed")

    assert result["victory"] is True
    assert result["hp"] == 60
    assert result["max_hp"] == 80
    assert result["seed"] == "test_seed"
