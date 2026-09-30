"""Phase 3a: STS2_OOC_POLICY -- out-of-combat decisions via agent.combat_env.greedy_action.

No game DLLs: play_full_run.EngineProcess is replaced by the scripted
_FakeEngine from test_map_route_determinism, and the greedy policy itself is
replaced through the play_full_run._load_greedy_action seam, so these tests pin
the harness's dispatch/fallback logic, not card_scoring's judgement.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine


def test_ooc_policy_defaults_to_naive():
    assert play_full_run.ooc_policy({}) == "naive"


def test_ooc_policy_blank_means_default():
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": "  "}) == "naive"


@pytest.mark.parametrize("raw,expected", [("greedy", "greedy"), ("NAIVE", "naive"), (" Greedy ", "greedy")])
def test_ooc_policy_accepts_known_values_case_insensitively(raw, expected):
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": raw}) == expected


def test_ooc_policy_rejects_unknown_values_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="STS2_OOC_POLICY"):
        play_full_run.ooc_policy({"STS2_OOC_POLICY": "smart"})


def test_greedy_decisions_exclude_map_and_combat():
    assert "map_select" not in play_full_run.OOC_GREEDY_DECISIONS
    assert "combat_play" not in play_full_run.OOC_GREEDY_DECISIONS
    assert play_full_run.OOC_GREEDY_DECISIONS == {
        "card_reward", "rest_site", "event_choice", "bundle_select", "card_select", "shop"}


@pytest.mark.parametrize("state,expected", [
    ({"decision": "card_reward"}, {"cmd": "action", "action": "skip_card_reward"}),
    ({"decision": "bundle_select"}, {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}),
    ({"decision": "card_select", "cards": [{"index": 0}]},
     {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}),
    ({"decision": "card_select", "cards": []}, {"cmd": "action", "action": "skip_select"}),
    ({"decision": "rest_site"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "event_choice"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "shop"}, {"cmd": "action", "action": "leave_room"}),
])
def test_fallback_command_per_decision(state, expected):
    assert play_full_run.ooc_fallback_command(state) == expected


def test_load_greedy_action_returns_the_real_combat_env_function():
    fn = play_full_run._load_greedy_action()
    assert callable(fn) and fn.__name__ == "greedy_action"
    assert fn.__module__ == "agent.combat_env"
