"""Phase 3b Task 1: per-room transitions and state features from game logs.

Hermetic: the logs are tiny synthetic JSONL rows in the shape python/game_log.py
writes (run_meta line, then {"type": "action"|"state", "data": ...}), and the
card-data functions state_features leans on are replaced by simple stand-ins
(one test at the end runs the real ones).
"""
import json

import pytest

from agent import route_data
from agent.route_data import FEATURE_KEYS, extract_transitions, load_games, state_features

CARD_A = {"id": "A", "type": "Attack", "upgraded": True, "score": 7.0}
CARD_B = {"id": "B", "type": "Skill", "upgraded": False, "score": 3.0}


@pytest.fixture(autouse=True)
def _fake_card_data(monkeypatch):
    """score_card = the card's own "score" (0 if none); deck_5turn_burst = 40 per attack."""
    monkeypatch.setattr(route_data, "score_card", lambda card: card.get("score", 0.0))
    monkeypatch.setattr(route_data, "deck_5turn_burst",
                        lambda deck: 40.0 * sum(1 for c in deck if c.get("type") == "Attack"))


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
    assert "deck_strength" not in f
    for key in FEATURE_KEYS:
        assert key in f


def test_deck_burst_is_the_raw_burst_over_100_and_is_not_clipped():
    attacks = lambda n: [{"id": f"K{i}", "type": "Attack"} for i in range(n)]
    for n_attacks, expected in ((0, 0.0), (1, 0.4), (7, 2.8)):
        f = state_features(_ms(1, 1, 50, [(0, 1, "Monster")], deck=attacks(n_attacks)))
        assert f["deck_burst"] == pytest.approx(expected)
    assert f["deck_burst"] > 1.0  # the old 0..1 strength would have clipped this


def test_deck_top8_averages_the_eight_best_card_scores_of_a_ten_card_deck():
    scores = [5, 1, 9, 3, 7, 10, 2, 8, 4, 6]
    deck = [{"id": f"C{i}", "type": "Skill", "score": float(v)} for i, v in enumerate(scores)]
    f = state_features(_ms(1, 1, 50, [(0, 1, "Monster")], deck=deck))
    assert f["deck_top8"] == pytest.approx((10 + 9 + 8 + 7 + 6 + 5 + 4 + 3) / 8)
    assert f["deck_size"] == 10


def test_deck_top8_of_a_small_or_empty_deck_uses_every_card_or_zero():
    three = [{"id": f"C{i}", "type": "Skill", "score": v} for i, v in enumerate((2.0, 4.0, 9.0))]
    assert state_features(_ms(1, 1, 50, [(0, 1, "Monster")], deck=three))["deck_top8"] == \
        pytest.approx(5.0)
    empty = state_features(_ms(1, 1, 50, [(0, 1, "Monster")], deck=[]))
    assert empty["deck_top8"] == 0.0 and empty["deck_burst"] == 0.0 and empty["deck_size"] == 0


def test_state_features_defaults_for_a_bare_state():
    f = state_features({})
    assert (f["act"], f["floor"], f["gf"]) == (1, 0, 0)
    assert f["hp"] == 0 and f["deck_size"] == 0 and f["n_relics"] == 0 and f["n_potions"] == 0
    assert f["deck_burst"] == 0.0 and f["deck_top8"] == 0.0


def test_state_features_game_over_uses_deck_size_and_top_level_act_floor():
    f = state_features({"decision": "game_over", "act": 2, "floor": 4,
                        "player": {"hp": 0, "max_hp": 70, "deck_size": 13}})
    assert f["deck_size"] == 13 and f["hp"] == 0 and f["gf"] == 21
    assert f["deck_burst"] == 0.0 and f["deck_top8"] == 0.0 and f["n_upgraded"] == 0


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
    # the death happened in the room entered from the floor-2 map_select: game_over reports
    # floor 3 = that map_select's floor + 1
    assert game["final_gf"] == game["transitions"][-1]["pre"]["gf"] + 1
    first, second = game["transitions"]
    assert (first["room_type"], first["pre"]["hp"], first["post"]["hp"], first["died"]) == \
        ("Monster", 80, 70, False)
    assert (second["room_type"], second["pre"]["hp"], second["post"]["hp"], second["died"]) == \
        ("Elite", 70, 0, True)
    assert first["act"] == 1
    assert [s["choice_type"] for s in game["snapshots"]] == ["Monster", "Elite"]
    assert [s["features"]["hp"] for s in game["snapshots"]] == [80, 70]


def test_experiment_tag_and_map_state_are_reported_for_the_value_route_filter():
    plain = extract_transitions([dict(META, experiment="ooc=greedy"), _state(_over(1, 1, False))])
    assert plain["experiment"] == "ooc=greedy" and plain["has_map_state"] is False
    asked = extract_transitions([META, _state(_ms(1, 1, 80, [(1, 1, "Monster")])),
                                 {"step": 1, "type": "action", "data": {"cmd": "get_map"}},
                                 _state({"type": "map", "rows": []}), _state(_over(1, 1, False))])
    assert asked["experiment"] is None and asked["has_map_state"] is True


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


def test_state_features_with_the_real_card_scoring_on_a_starter_like_deck(monkeypatch):
    card_scoring = pytest.importorskip("agent.card_scoring")
    strike = {"id": "CARD.STRIKE_IRONCLAD", "name": "Strike", "cost": 1, "type": "Attack",
              "upgraded": False, "stats": {"damage": 6}}
    defend = {"id": "CARD.DEFEND_IRONCLAD", "name": "Defend", "cost": 1, "type": "Skill",
              "upgraded": False, "stats": {"block": 5}}
    bash = {"id": "CARD.BASH", "name": "Bash", "cost": 2, "type": "Attack",
            "upgraded": False, "stats": {"damage": 8}}
    deck = [strike] * 5 + [defend] * 4 + [bash]
    try:
        burst = card_scoring.deck_5turn_burst(deck)
        top8 = sorted((card_scoring.score_card(c) for c in deck), reverse=True)[:8]
    except (OSError, ValueError, KeyError) as exc:  # card data missing in this checkout
        pytest.skip(f"card data unavailable: {exc!r}")
    monkeypatch.setattr(route_data, "score_card", card_scoring.score_card)
    monkeypatch.setattr(route_data, "deck_5turn_burst", card_scoring.deck_5turn_burst)
    f = state_features(_ms(1, 1, 80, [(0, 1, "Monster")], deck=deck))
    assert f["deck_burst"] == pytest.approx(burst / 100.0)
    assert f["deck_top8"] == pytest.approx(sum(top8) / 8)
    assert f["deck_burst"] > 0.0 and 0.0 < f["deck_top8"] <= 10.0 and f["deck_size"] == 10


def test_load_games_survives_any_exception_from_a_file(tmp_path, capsys, monkeypatch):
    def score(card):
        if card.get("id") == "BOOM":
            raise AttributeError("'str' object has no attribute 'get'")  # a card of unexpected shape
        return card.get("score", 0.0)

    monkeypatch.setattr(route_data, "score_card", score)
    good_rows = [META, _state(_ms(1, 1, 80, [(1, 1, "Monster")])), _act(1, 1),
                 _state(_over(1, 2, False))]
    boom_rows = [META, _state(_ms(1, 1, 80, [(1, 1, "Monster")], deck=[{"id": "BOOM"}])),
                 _act(1, 1), _state(_over(1, 2, False))]
    good = tmp_path / "good.jsonl"
    boom = tmp_path / "boom.jsonl"
    good.write_text("\n".join(json.dumps(r) for r in good_rows) + "\n")
    boom.write_text("\n".join(json.dumps(r) for r in boom_rows) + "\n")
    games = load_games([str(boom), str(good)])
    assert len(games) == 1
    out = capsys.readouterr().out
    assert "boom.jsonl" in out and "AttributeError" in out
