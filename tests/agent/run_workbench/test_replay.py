import json
from pathlib import Path

import pytest

from agent.run_workbench.replay import format_room_label, parse_game_progress


FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_workbench"


def _read_fixture(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _state(step, decision, act, floor, room_type, hp, **extra):
    data = {
        "type": "decision",
        "decision": decision,
        "context": {
            "act": act,
            "act_name": "Test Act",
            "floor": floor,
            "room_type": room_type,
            "boss": {"id": "BOSS", "name": "Boss"},
        },
        "player": {
            "name": "The Ironclad",
            "hp": hp,
            "max_hp": 70,
            "gold": 99,
            "deck_size": 10,
            "deck": [{"name": "Strike", "type": "Attack", "upgraded": False}],
            "relics": [{"name": "Burning Blood"}],
            "potions": [],
        },
    }
    data.update(extra)
    return {"step": step, "ts": f"2026-07-10T00:00:{step:02d}", "type": "state", "data": data}


def _action(step, action, **args):
    return {
        "step": step,
        "ts": f"2026-07-10T00:00:{step:02d}",
        "type": "action",
        "data": {"cmd": "action", "action": action, "args": args},
    }


def test_format_room_label_uses_act_relative_floor():
    assert format_room_label({"act": 1, "floor": 7}) == "A1F7"
    assert format_room_label({"act": 2, "floor": 5}) == "A2F5"


def test_parse_game_progress_groups_rooms_options_actions_and_hp_loss():
    entries = [
        {"step": 0, "ts": "2026-07-10T00:00:00", "type": "action", "data": {"cmd": "start_run", "character": "Ironclad", "seed": "seed-1"}},
        _state(
            1,
            "event_choice",
            1,
            1,
            "Event",
            70,
            event_name="Neow",
            options=[
                {"index": 0, "title": "Gold", "description": "Gain gold"},
                {"index": 1, "title": "Max HP", "description": "Gain max HP"},
            ],
        ),
        _action(1, "choose_option", option_index=1),
        _state(
            2,
            "map_select",
            1,
            1,
            "Map",
            70,
            choices=[
                {"col": 0, "row": 1, "type": "Monster"},
                {"col": 1, "row": 1, "type": "Event"},
            ],
        ),
        _action(2, "select_map_node", col=0, row=1),
        _state(
            3,
            "combat_play",
            1,
            2,
            "Monster",
            70,
            round=1,
            energy=3,
            hand=[{"index": 0, "name": "Strike", "can_play": True}],
            enemies=[{"index": 0, "name": "Fuzzy", "hp": 20, "max_hp": 20, "intents": [{"type": "Attack", "damage": 5}]}],
        ),
        _action(3, "end_turn"),
        _state(
            4,
            "card_reward",
            1,
            2,
            "Monster",
            63,
            cards=[
                {"index": 0, "id": "CARD.POMMEL_STRIKE", "name": "Pommel Strike", "type": "Attack", "rarity": "Common"},
                {"index": 1, "id": "CARD.SHRUG_IT_OFF", "name": "Shrug It Off", "type": "Skill", "rarity": "Common"},
            ],
        ),
        _action(4, "select_card_reward", card_index=0),
        _state(5, "map_select", 1, 2, "Map", 63, choices=[{"col": 2, "row": 2, "type": "Elite"}]),
        _action(5, "select_map_node", col=2, row=2),
        _state(6, "combat_play", 2, 5, "Elite", 63, round=1, enemies=[{"index": 0, "name": "Elite", "hp": 80, "max_hp": 80}]),
        _state(7, "game_over", 2, 5, "Elite", 0, victory=False),
    ]

    progress = parse_game_progress(entries, source_name="sample.jsonl")

    assert progress["summary"]["character"] == "Ironclad"
    assert progress["summary"]["seed"] == "seed-1"
    assert progress["summary"]["max_floor_label"] == "A2F5"
    assert progress["summary"]["total_hp_loss"] == 70

    rooms = progress["rooms"]
    assert [(room["label"], room["room_type"]) for room in rooms] == [
        ("A1F1", "Event"),
        ("A1F2", "Monster"),
        ("A2F5", "Elite"),
    ]
    assert rooms[0]["hp_loss"] == 0
    assert rooms[1]["start_hp"] == 70
    assert rooms[1]["end_hp"] == 63
    assert rooms[1]["hp_loss"] == 7
    assert rooms[2]["status"] == "dead"
    assert rooms[2]["hp_loss"] == 63

    event_options = [item for item in rooms[0]["options"] if item["kind"] == "event_option"]
    assert [item["label"] for item in event_options] == ["Gold", "Max HP"]
    assert [item["selected"] for item in event_options] == [False, True]

    map_options = [item for item in rooms[0]["options"] if item["kind"] == "map_choice"]
    assert [item["label"] for item in map_options] == ["Monster", "Event"]
    assert [item["selected"] for item in map_options] == [True, False]

    reward_options = [item for item in rooms[1]["options"] if item["kind"] == "card_reward"]
    assert [item["label"] for item in reward_options] == ["Pommel Strike", "Shrug It Off"]
    assert [item["item_id"] for item in reward_options] == ["CARD.POMMEL_STRIKE", "CARD.SHRUG_IT_OFF"]
    assert [item["selected"] for item in reward_options] == [True, False]

    assert any(action["label"] == "select_card_reward card_index=0" for action in rooms[1]["actions"])
    assert rooms[1]["combat"]["turns"][0]["enemy_names"] == ["Fuzzy"]


def test_parse_game_progress_builds_round_based_combat_replay():
    entries = [
        {"step": 0, "ts": "2026-07-10T00:00:00", "type": "action", "data": {"cmd": "start_run", "character": "Ironclad", "seed": "seed-1"}},
        _state(
            1,
            "combat_play",
            1,
            2,
            "Monster",
            70,
            round=1,
            energy=3,
            hand=[
                {"index": 0, "id": "CARD.STRIKE_IRONCLAD", "name": "Strike", "cost": 1, "type": "Attack", "can_play": True},
                {"index": 1, "id": "CARD.DEFEND_IRONCLAD", "name": "Defend", "cost": 1, "type": "Skill", "can_play": True},
            ],
            enemies=[
                {"index": 0, "name": "Jaw Worm", "hp": 20, "max_hp": 20, "block": 0, "intents": [{"type": "Attack", "damage": 6}]}
            ],
        ),
        _action(1, "play_card", card_index=0, target_index=0),
        _state(
            2,
            "combat_play",
            1,
            2,
            "Monster",
            70,
            round=1,
            energy=2,
            hand=[
                {"index": 0, "id": "CARD.DEFEND_IRONCLAD", "name": "Defend", "cost": 1, "type": "Skill", "can_play": True},
            ],
            enemies=[
                {"index": 0, "name": "Jaw Worm", "hp": 14, "max_hp": 20, "block": 0, "intents": [{"type": "Attack", "damage": 6}]}
            ],
        ),
        _action(2, "end_turn"),
        _state(
            3,
            "combat_play",
            1,
            2,
            "Monster",
            64,
            round=2,
            energy=3,
            hand=[
                {"index": 0, "id": "CARD.DEFEND_IRONCLAD", "name": "Defend", "cost": 1, "type": "Skill", "can_play": True},
                {"index": 1, "id": "CARD.STRIKE_IRONCLAD", "name": "Strike", "cost": 1, "type": "Attack", "can_play": True},
            ],
            enemies=[
                {"index": 0, "name": "Jaw Worm", "hp": 14, "max_hp": 20, "block": 0, "intents": [{"type": "Buff"}]}
            ],
        ),
    ]

    room = parse_game_progress(entries)["rooms"][0]
    replay = room["combat"]["rounds"][0]

    assert replay["round"] == 1
    assert replay["start_state"]["hp"] == 70
    assert [card["name"] for card in replay["start_state"]["hand"]] == ["Strike", "Defend"]
    assert replay["start_state"]["enemies"][0]["intents"] == [{"type": "Attack", "damage": 6}]
    assert replay["actions"][0]["card"]["name"] == "Strike"
    assert replay["actions"][0]["target"]["name"] == "Jaw Worm"
    assert replay["actions"][1]["label"] == "end_turn"
    assert replay["end_state"]["round"] == 2
    assert replay["end_state"]["hp"] == 64
    assert [card["name"] for card in replay["end_state"]["hand"]] == ["Defend", "Strike"]
    assert replay["hp_loss"] == 6
def test_parse_game_progress_extracts_a2f4_metadata_and_coverage():
    entries = _read_fixture("replay_a2f4_excerpt.jsonl")

    progress = parse_game_progress(
        entries, source_name="replay_a2f4_excerpt.jsonl"
    )

    summary = progress["summary"]
    assert summary["run_id"] == "fixture-a2f4"
    assert summary["game_version"] == "v0.103.2"
    assert summary["ascension"] == 0
    assert summary["modifiers"] == ["TEST_MODIFIER"]
    assert summary["first_recorded_floor"] == 1
    assert summary["last_recorded_floor"] == 21
    assert summary["has_state_records"] is True
    assert summary["has_action_records"] is True
    assert summary["complete_run"] is True

    combat = progress["rooms"][-1]["combat"]
    assert len(combat["rounds"]) == 2
    assert combat["rounds"][0]["hp_loss"] == 6
    assert combat["rounds"][0]["start_state"]["potions"][0]["name"] == (
        "Training Potion"
    )
    assert combat["rounds"][1]["actions"][0]["card"]["name"] == (
        "Training Strike"
    )
    assert combat["rounds"][1]["actions"][0]["target"]["name"] == (
        "Training Beast"
    )


def test_parse_game_progress_uses_optional_state_context_metadata():
    entry = _state(1, "combat_play", 2, 4, "Monster", 64, round=1)
    entry["data"]["context"].update(
        {
            "run_id": "state-context-run",
            "character": "CONTEXT_WARRIOR",
            "seed": "context-seed",
            "build_id": "context-build",
            "ascension": 3,
            "modifiers": ["CONTEXT_MODIFIER"],
        }
    )

    summary = parse_game_progress([entry])["summary"]

    assert summary["run_id"] == "state-context-run"
    assert summary["character"] == "CONTEXT_WARRIOR"
    assert summary["seed"] == "context-seed"
    assert summary["game_version"] == "context-build"
    assert summary["ascension"] == 3
    assert summary["modifiers"] == ["CONTEXT_MODIFIER"]
    assert summary["complete_run"] is False


def test_parse_game_progress_rejects_action_only_records_explicitly():
    entries = [
        {
            "step": 0,
            "type": "action",
            "data": {"cmd": "start_run", "run_id": "action-only"},
        },
        {
            "step": 1,
            "type": "action",
            "data": {"cmd": "action", "action": "end_turn", "args": {}},
        },
    ]

    with pytest.raises(ValueError, match="^no state records$"):
        parse_game_progress(entries, source_name="action-only.jsonl")


def test_legacy_viewer_reexports_the_package_parser():
    from agent.run_progress_viewer import (
        format_room_label as legacy_format_room_label,
    )
    from agent.run_progress_viewer import (
        parse_game_progress as legacy_parse_game_progress,
    )

    assert legacy_format_room_label is format_room_label
    assert legacy_parse_game_progress is parse_game_progress
