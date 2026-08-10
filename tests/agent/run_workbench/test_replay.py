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
    assert [
        round_info["actions"][-1]["label"]
        for round_info in combat["rounds"]
    ] == ["end_turn", "end_turn"]
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
    assert combat["rounds"][1]["end_reason"] == "combat_end"


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


@pytest.mark.parametrize(
    "context",
    [
        {"act": 1, "room_type": "Monster"},
        {"floor": 4, "room_type": "Monster"},
        {"act": True, "floor": 4, "room_type": "Monster"},
        {"act": 1, "floor": False, "room_type": "Monster"},
        {"act": "1", "floor": 4, "room_type": "Monster"},
        {"act": 1, "floor": "4", "room_type": "Monster"},
        {"act": float("nan"), "floor": 4, "room_type": "Monster"},
        {"act": 1, "floor": float("inf"), "room_type": "Monster"},
        {"act": 1, "floor": 4.5, "room_type": "Monster"},
    ],
    ids=[
        "missing-floor",
        "missing-act",
        "boolean-act",
        "boolean-floor",
        "string-act",
        "string-floor",
        "nan-act",
        "infinite-floor",
        "fractional-floor",
    ],
)
def test_parse_game_progress_does_not_invent_coverage_from_invalid_coordinates(
    context,
):
    entries = [
        {"type": "action", "data": {"cmd": "start_run"}},
        {
            "type": "state",
            "data": {
                "decision": "game_over",
                "context": context,
                "player": {},
            },
        },
    ]

    progress = parse_game_progress(entries)

    assert progress["summary"]["first_recorded_floor"] is None
    assert progress["summary"]["last_recorded_floor"] is None
    assert progress["summary"]["max_global_floor"] is None
    assert progress["summary"]["max_floor_label"] is None
    assert progress["summary"]["complete_run"] is False
    assert progress["summary"]["room_count"] == 1


def test_parse_game_progress_accepts_finite_integral_numeric_coordinates():
    entries = [
        {
            "type": "state",
            "data": {
                "decision": "combat_play",
                "context": {
                    "act": 2.0,
                    "floor": 4.0,
                    "room_type": "Monster",
                },
                "player": {},
            },
        }
    ]

    summary = parse_game_progress(entries)["summary"]

    assert summary["first_recorded_floor"] == 21
    assert summary["last_recorded_floor"] == 21
    assert summary["max_global_floor"] == 21
    assert summary["max_floor_label"] == "A2F4"


@pytest.mark.parametrize(
    "context",
    [
        {"act": 10**400, "floor": 1, "room_type": "Monster"},
        {"act": 1, "floor": 10**400, "room_type": "Monster"},
        {"act": -1, "floor": 1, "room_type": "Monster"},
        {"act": 1, "floor": -1, "room_type": "Monster"},
        {"act": 5, "floor": 1, "room_type": "Monster"},
        {"act": 1, "floor": 18, "room_type": "Monster"},
    ],
    ids=["huge-act", "huge-floor", "negative-act", "negative-floor", "act-5", "floor-18"],
)
def test_parse_game_progress_bounds_invalid_coordinates_for_evidence_and_display(
    context,
):
    entries = [
        {"type": "action", "data": {"cmd": "start_run"}},
        {
            "type": "state",
            "data": {
                "decision": "game_over",
                "context": context,
                "player": {},
            },
        },
    ]

    progress = parse_game_progress(entries)
    summary = progress["summary"]
    room = progress["rooms"][0]

    assert summary["first_recorded_floor"] is None
    assert summary["last_recorded_floor"] is None
    assert summary["max_global_floor"] is None
    assert summary["max_floor_label"] is None
    assert summary["complete_run"] is False
    assert 1 <= room["act"] <= 4
    assert 0 <= room["floor"] <= 17
    assert 0 <= room["global_floor"] <= 68
    assert len(room["id"]) < 128


def test_parse_game_progress_accepts_glory_floor_boundary():
    entries = [
        {"type": "action", "data": {"cmd": "start_run"}},
        _state(1, "game_over", 4, 17, "Boss", 1, victory=True),
    ]

    progress = parse_game_progress(entries)

    assert progress["summary"]["first_recorded_floor"] == 68
    assert progress["summary"]["last_recorded_floor"] == 68
    assert progress["summary"]["max_global_floor"] == 68
    assert progress["summary"]["max_floor_label"] == "A4F17"
    assert progress["summary"]["complete_run"] is False
    assert progress["rooms"][0]["label"] == "A4F17"


def test_parse_game_progress_does_not_complete_on_unprocessed_terminal_state():
    entries = [
        {
            "type": "action",
            "data": {"cmd": "start_run", "run_id": "invalid-terminal"},
        },
        _state(1, "combat_play", 1, 1, "Monster", 70, round=1),
        {
            "type": "state",
            "data": {
                "decision": "game_over",
                "context": {},
                "player": {},
            },
        },
    ]

    progress = parse_game_progress(entries)

    assert progress["summary"]["first_recorded_floor"] == 1
    assert progress["summary"]["last_recorded_floor"] == 1
    assert progress["summary"]["complete_run"] is False
    assert progress["rooms"][0]["status"] == "completed"


@pytest.mark.parametrize("second_run_id", ["run-a", "run-b"])
def test_parse_game_progress_rejects_multiple_start_run_records(second_run_id):
    entries = [
        {"type": "action", "data": {"cmd": "start_run", "run_id": "run-a"}},
        _state(1, "combat_play", 1, 1, "Monster", 70, round=1),
        {
            "type": "action",
            "data": {"cmd": "start_run", "run_id": second_run_id},
        },
        _state(2, "game_over", 1, 2, "Monster", 0, victory=False),
    ]

    with pytest.raises(ValueError, match="^multiple start_run records$"):
        parse_game_progress(entries)


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([42], "entry must be an object"),
        (
            [{"type": "action", "data": "start_run"}],
            "action data must be an object",
        ),
        (
            [{"type": "state", "data": "combat_play"}],
            "state data must be an object",
        ),
        (
            [
                {
                    "type": "state",
                    "data": {"context": "A1F1", "player": {}},
                }
            ],
            "state context must be an object",
        ),
        (
            [
                {
                    "type": "state",
                    "data": {
                        "context": {"act": 1, "floor": 1},
                        "player": "healthy",
                    },
                }
            ],
            "state player must be an object",
        ),
        (
            [
                {
                    "type": "state",
                    "data": {
                        "context": {"act": 1, "floor": 1, "boss": "boss"},
                        "player": {},
                    },
                }
            ],
            "state context boss must be an object",
        ),
        (
            [
                _state(1, "combat_play", 1, 1, "Monster", 70, round=1),
                {
                    "type": "action",
                    "data": {"cmd": "action", "action": "end_turn", "args": "none"},
                },
            ],
            "action args must be an object",
        ),
    ],
    ids=["entry", "action", "state", "context", "player", "boss", "args"],
)
def test_parse_game_progress_rejects_non_object_nested_records(entries, message):
    with pytest.raises(ValueError, match=f"^{message}$"):
        parse_game_progress(entries)


def test_parse_game_progress_skips_non_object_items_in_mapping_lists():
    combat = _state(
        1,
        "combat_play",
        1,
        1,
        "Monster",
        70,
        round=1,
        options=[42, {"index": 0, "title": "Train"}],
        choices=["bad", {"col": 1, "row": 2, "type": "Monster"}],
        hand=[None, {"index": 0, "name": "Training Strike"}],
        enemies=[
            "bad",
            {
                "index": 0,
                "name": "Training Dummy",
                "hp": 12,
                "max_hp": 12,
                "intents": [42, {"type": "Attack", "damage": 3}],
                "powers": [None, {"name": "Test Power", "amount": 1}],
            },
        ],
    )
    combat["data"]["player"].update(
        {
            "deck": [42, {"id": "TEST_CARD", "name": "Training Strike"}],
            "relics": ["bad", {"id": "TEST_RELIC", "name": "Training Relic"}],
            "potions": [None, {"id": "TEST_POTION", "name": "Training Potion"}],
        }
    )
    reward = _state(
        2,
        "card_reward",
        1,
        1,
        "Monster",
        70,
        cards=["bad", {"index": 0, "id": "TEST_REWARD", "name": "Reward"}],
    )

    room = parse_game_progress([combat, reward])["rooms"][0]

    assert [option["label"] for option in room["options"]] == [
        "Train",
        "Monster",
        "Reward",
    ]
    assert [card["name"] for card in room["combat"]["turns"][0]["hand"]] == [
        "Training Strike"
    ]
    assert room["combat"]["turns"][0]["enemies"][0]["intents"] == [
        {"type": "Attack", "damage": 3}
    ]
    assert room["combat"]["turns"][0]["enemies"][0]["powers"] == [
        {"name": "Test Power", "amount": 1}
    ]
    assert room["start_player"]["relics"] == ["Training Relic"]
    assert room["start_player"]["potions"] == ["Training Potion"]
    assert [card["name"] for card in room["start_player"]["deck"]] == [
        "Training Strike"
    ]


def test_parse_game_progress_preserves_sparse_empty_player_payload():
    entry = {
        "type": "state",
        "data": {
            "decision": "combat_play",
            "context": {"act": 1, "floor": 1, "room_type": "Monster"},
            "player": {},
        },
    }

    room = parse_game_progress([entry])["rooms"][0]

    assert room["start_player"] == {}
    assert room["end_player"] == {}


def test_parse_game_progress_preserves_complete_nonempty_player_payload():
    entry = {
        "type": "state",
        "data": {
            "decision": "combat_play",
            "context": {"act": 1, "floor": 1, "room_type": "Monster"},
            "player": {
                "name": "Test Warrior",
                "hp": 50,
                "max_hp": 60,
                "block": 4,
                "gold": 25,
                "deck_size": 1,
                "relics": [{"id": "TEST_RELIC", "name": "Test Relic"}],
                "potions": [{"id": "TEST_POTION", "name": "Test Potion"}],
                "deck": [
                    {
                        "id": "TEST_CARD",
                        "name": "Test Card",
                        "type": "Attack",
                        "upgraded": True,
                    }
                ],
            },
        },
    }
    expected = {
        "name": "Test Warrior",
        "hp": 50,
        "max_hp": 60,
        "block": 4,
        "gold": 25,
        "deck_size": 1,
        "relics": ["Test Relic"],
        "relic_items": [{"id": "TEST_RELIC", "name": "Test Relic"}],
        "potions": ["Test Potion"],
        "potion_items": [{"id": "TEST_POTION", "name": "Test Potion"}],
        "deck": [
            {
                "id": "TEST_CARD",
                "name": "Test Card",
                "type": "Attack",
                "upgraded": True,
            }
        ],
    }

    room = parse_game_progress([entry])["rooms"][0]

    assert room["start_player"] == expected
    assert room["end_player"] == expected


def test_parse_game_progress_normalizes_nonfinite_numbers_and_metadata():
    state = _state(
        1,
        "combat_play",
        1,
        1,
        "Monster",
        float("nan"),
        round=float("inf"),
        energy=float("nan"),
        enemies=[
            {
                "index": 0,
                "name": "Training Dummy",
                "hp": float("nan"),
                "max_hp": float("inf"),
            }
        ],
    )
    state["data"]["player"].update(
        {
            "name": None,
            "max_hp": float("inf"),
            "block": True,
            "gold": float("nan"),
            "deck_size": False,
        }
    )
    entries = [
        {
            "type": "action",
            "data": {
                "cmd": "start_run",
                "run_id": float("nan"),
                "character": {},
                "seed": [],
                "build_id": float("inf"),
                "ascension": float("nan"),
                "modifiers": ["TEST_MODIFIER", 42],
            },
        },
        state,
    ]

    progress = parse_game_progress(entries)

    assert progress["summary"]["run_id"] is None
    assert progress["summary"]["character"] is None
    assert progress["summary"]["seed"] is None
    assert progress["summary"]["game_version"] is None
    assert progress["summary"]["ascension"] is None
    assert progress["summary"]["modifiers"] is None
    assert progress["rooms"][0]["start_hp"] is None
    assert progress["rooms"][0]["max_hp"] is None
    assert progress["rooms"][0]["start_player"]["block"] is None
    assert progress["rooms"][0]["combat"]["turns"][0]["enemies"][0]["hp"] is None
    json.dumps(progress, allow_nan=False)


def test_parse_game_progress_rejects_boolean_timestamp_and_step_numbers():
    state = _state(1, "combat_play", 1, 1, "Monster", 70, round=1)
    state["ts"] = True
    action = _action(2, "end_turn")
    action["step"] = False
    action["ts"] = False

    progress = parse_game_progress([state, action])

    assert progress["summary"]["started_at"] is None
    assert progress["summary"]["ended_at"] is None
    assert progress["rooms"][0]["actions"][0]["step"] is None


@pytest.mark.parametrize(
    "ascension",
    [True, 3.0, -1, 11, float("nan"), "3"],
    ids=["boolean", "float", "negative", "too-high", "nan", "string"],
)
def test_parse_game_progress_requires_bounded_integer_ascension(ascension):
    entries = [
        {
            "type": "action",
            "data": {"cmd": "start_run", "ascension": ascension},
        },
        _state(1, "combat_play", 1, 1, "Monster", 70, round=1),
    ]

    assert parse_game_progress(entries)["summary"]["ascension"] is None


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
