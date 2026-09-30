"""Phase 3b Task 1: per-room transitions and state features from game logs.

Hermetic: the logs are tiny synthetic JSONL rows in the shape python/game_log.py
writes (run_meta line, then {"type": "action"|"state", "data": ...}).
"""
import json

import pytest

from agent.route_data import FEATURE_KEYS, extract_transitions, load_games, state_features

CARD_A = {"id": "A", "type": "Attack", "upgraded": True}
CARD_B = {"id": "B", "type": "Skill", "upgraded": False}


def _player(hp, deck=None, relics=None, potions=None, max_hp=80, gold=99):
    return {"hp": hp, "max_hp": max_hp, "gold": gold,
            "deck": [CARD_A, CARD_B] if deck is None else deck,
            "relics": [] if relics is None else relics,
            "potions": [] if potions is None else potions}


def _ms(act, floor, hp, choices, deck=None, relics=None, potions=None):
    """A map_select decision. choices: [(col, row, type)]."""
    return {"type": "decision", "decision": "map_select",
            "context": {"act": act, "floor": floor, "room_type": "Map"},
            "choices": [{"col": c, "row": r, "type": t} for c, r, t in choices],
            "player": _player(hp, deck, relics, potions)}


def _over(act, floor, victory, hp=0):
    # The real game_over reply carries no deck / relics, only deck_size.
    return {"type": "decision", "decision": "game_over", "victory": victory,
            "context": {"act": act, "floor": floor}, "act": act, "floor": floor,
            "player": {"hp": hp, "max_hp": 80, "gold": 50, "deck_size": 12, "potions": []}}


def _state(data):
    return {"step": 0, "type": "state", "data": data}


def _act(col, row):
    return {"step": 0, "type": "action",
            "data": {"cmd": "action", "action": "select_map_node", "args": {"col": col, "row": row}}}


META = {"type": "run_meta", "seed": "s1", "character": "Defect", "ascension": 1}


def test_state_features_counts_and_global_floor():
    state = _ms(2, 3, 55, [(0, 3, "Monster")], relics=[{"name": "R1"}, {"name": "R2"}],
                potions=[{"id": "P"}, None])
    f = state_features(state)
    assert f["deck_size"] == 2
    assert f["n_upgraded"] == 1
    assert f["n_relics"] == 2
    assert f["n_potions"] == 1
    assert (f["act"], f["floor"], f["gf"]) == (2, 3, 20)
    assert f["hp"] == 55 and f["max_hp"] == 80 and f["gold"] == 99
    assert 0.0 <= f["deck_strength"] <= 1.0
    for key in FEATURE_KEYS:
        assert key in f


def test_state_features_defaults_for_a_bare_state():
    f = state_features({})
    assert (f["act"], f["floor"], f["gf"]) == (1, 0, 0)
    assert f["hp"] == 0 and f["deck_size"] == 0 and f["n_relics"] == 0 and f["n_potions"] == 0


def test_state_features_game_over_uses_deck_size_and_top_level_act_floor():
    f = state_features({"decision": "game_over", "act": 2, "floor": 4,
                        "player": {"hp": 0, "max_hp": 70, "deck_size": 13}})
    assert f["deck_size"] == 13 and f["hp"] == 0 and f["gf"] == 21


def test_two_moves_then_death():
    rows = [META,
            _state(_ms(1, 1, 80, [(1, 1, "Monster"), (2, 1, "Elite")])),
            _act(1, 1),
            _state({"type": "decision", "decision": "combat_play"}),
            _state(_ms(1, 2, 70, [(1, 2, "Elite")])),
            _act(1, 2),
            _state(_over(1, 3, False))]
    game = extract_transitions(rows)
    assert game["character"] == "Defect" and game["ascension"] == 1 and game["seed"] == "s1"
    assert game["won"] is False
    assert game["final_gf"] == 3
    first, second = game["transitions"]
    assert (first["room_type"], first["pre"]["hp"], first["post"]["hp"], first["died"]) == \
        ("Monster", 80, 70, False)
    assert (second["room_type"], second["pre"]["hp"], second["post"]["hp"], second["died"]) == \
        ("Elite", 70, 0, True)
    assert first["act"] == 1
    assert [s["choice_type"] for s in game["snapshots"]] == ["Monster", "Elite"]
    assert [s["features"]["hp"] for s in game["snapshots"]] == [80, 70]


def test_meta_fields_are_none_without_run_meta():
    game = extract_transitions([_state(_over(1, 1, False))])
    assert game["character"] is None and game["ascension"] is None and game["seed"] is None
    assert game["transitions"] == [] and game["snapshots"] == []


def test_boss_move_crossing_an_act_keeps_a_boss_transition():
    rows = [META,
            _state(_ms(1, 16, 60, [(3, 16, "Boss")])),
            _act(3, 16),
            _state({"type": "decision", "decision": "combat_play"}),
            _state({"type": "decision", "decision": "card_reward"}),
            _state({"type": "decision", "decision": "event_choice",
                    "context": {"act": 2, "floor": 0}}),
            _state(_ms(2, 1, 75, [(0, 1, "Monster")])),
            _state(_over(2, 2, False))]
    game = extract_transitions(rows)
    boss = game["transitions"][0]
    assert boss["room_type"] == "Boss"
    assert boss["act"] == 1
    assert boss["pre"]["act"] == 1 and boss["post"]["act"] == 2
    assert boss["post"]["hp"] == 75 and boss["died"] is False


def test_victory_game_over_after_the_last_boss_is_not_a_death():
    rows = [META,
            _state(_ms(3, 16, 60, [(3, 16, "Boss")])),
            _act(3, 16),
            _state(_over(3, 17, True, hp=12))]
    game = extract_transitions(rows)
    assert game["won"] is True and game["final_gf"] == 2 * 17 + 17
    t = game["transitions"][0]
    assert t["room_type"] == "Boss" and t["died"] is False and t["post"]["hp"] == 12


def test_log_without_game_over_is_excluded(tmp_path):
    rows = [META, _state(_ms(1, 1, 80, [(1, 1, "Monster")])), _act(1, 1)]
    assert extract_transitions(rows) is None
    path = tmp_path / "trunc.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert load_games([str(path)]) == []


def test_non_decision_states_are_ignored():
    rows = [META,
            _state(_ms(1, 1, 80, [(1, 1, "Monster")])),
            _state({"type": "map", "rows": []}),
            _state({"type": "combat_plan", "actions": []}),
            _state({"type": "error", "message": "boom"}),
            _act(1, 1),
            _state({"type": "combat_plan", "actions": []}),
            _state({"type": "error", "message": "boom"}),
            _state(_ms(1, 2, 72, [(1, 2, "Monster")])),
            _state(_over(1, 3, False))]
    game = extract_transitions(rows)
    assert len(game["transitions"]) == 1
    t = game["transitions"][0]
    assert t["pre"]["hp"] == 80 and t["post"]["hp"] == 72 and t["died"] is False
    assert game["final_gf"] == 3


def test_repeated_map_select_replaces_the_pre_state():
    rows = [META,
            _state(_ms(1, 1, 80, [(1, 1, "Monster")])),
            _state({"type": "error", "message": "retry"}),
            _state(_ms(1, 1, 64, [(1, 1, "Monster")])),
            _act(1, 1),
            _state(_over(1, 2, False))]
    game = extract_transitions(rows)
    assert len(game["transitions"]) == 1 and len(game["snapshots"]) == 1
    assert game["transitions"][0]["pre"]["hp"] == 64


def test_move_to_a_node_not_offered_is_skipped():
    rows = [META,
            _state(_ms(1, 1, 80, [(1, 1, "Monster")])),
            _act(5, 5),
            _state(_over(1, 1, False))]
    game = extract_transitions(rows)
    assert game["transitions"] == [] and game["snapshots"] == []


def test_load_games_skips_a_bad_file_and_keeps_good_ones(tmp_path, capsys):
    good_rows = [META, _state(_ms(1, 1, 80, [(1, 1, "Monster")])), _act(1, 1),
                 _state(_over(1, 2, False))]
    good = tmp_path / "good.jsonl"
    good.write_text("\n".join(json.dumps(r) for r in good_rows) + "\n")
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type": "state", "data": {"decis\n')
    missing = str(tmp_path / "missing.jsonl")
    games = load_games([str(bad), missing, str(good)])
    assert len(games) == 1 and games[0]["seed"] == "s1"
    out = capsys.readouterr().out
    assert "bad.jsonl" in out and "missing.jsonl" in out

