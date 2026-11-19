"""Native v0.113 Fool integration: use copied libraries and execute Solver plans."""
from collections import Counter
import json

from .conftest import Game


def test_fool_a9_native_start_is_deterministic(tmp_path):
    observations = []
    for attempt in range(2):
        game = Game()
        try:
            state = game.start(character="Fool", seed="fool_native_a9", ascension=9)
            assert state.get("type") != "error", state
            assert state["decision"] == "event_choice"
            deck = Counter(c["id"] for c in state["player"]["deck"])
            assert deck == {"CARD.STRIKE_FOOL": 5, "CARD.DEFEND_FOOL": 4, "CARD.FOOLISH_SHOT": 1, "CARD.ASCENDERS_BANE": 1}
            save = tmp_path / f"native-{attempt}.save"
            result = game.send({"cmd": "write_continue_save", "path": str(save)})
            assert result["success"] is True, result
            saved = json.loads(save.read_text())
            assert saved["ascension"] == 9
            assert saved["game_mode"] == "standard"
            assert saved["players"][0]["character_id"] == "CHARACTER.FOOL"
            assert saved["rng"]["seed"] == "fool_native_a9"
            observations.append(state)
        finally:
            game.close()
    assert observations[0] == observations[1]


def test_fool_a9_solver_executes_native_combat(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
    from combat_plan_driver import _execute_combat_plan_actions

    monkeypatch.setenv("STS2_COLLECTION_BUDGET_MS", "3500")
    monkeypatch.setenv("STS2_SOLVER_THREADS", "1")
    monkeypatch.setenv("STS2_SOLVER_INFERRER", "on")
    game = Game()
    try:
        state = game.skip_neow(game.start(character="Fool", seed="fool_solver_a9", ascension=9))
        assert state["decision"] == "map_select", state
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert state["decision"] == "combat_play", state
        executed = []

        def send(command):
            executed.append(command)
            reply = game.send(command)
            assert reply.get("type") != "error", reply
            return reply

        for _ in range(20):
            if state.get("decision") != "combat_play":
                break
            plan = game.act("plan_combat_turn")
            assert plan["type"] == "combat_plan", plan
            assert plan["search"]["budget_ms"] == 3500
            assert plan["search"]["inferrer"] == "on"
            assert plan["actions"], plan
            state, ok = _execute_combat_plan_actions(send, state, plan["actions"])
            assert ok, state
        assert state.get("decision") == "card_reward", state
        assert any(c.get("action") == "play_card" for c in executed)
        assert "[FATAL]" not in game.stderr_text()
    finally:
        game.close()


def test_fool_arrow_and_songbow_aim_are_native(game):
    state = game.skip_neow(game.start(character="Fool", seed="fool_arrow_a9", ascension=9))
    game.set_player(deck=["FOOLISH_SHOT", "STRIKE_FOOL", "STRIKE_FOOL", "DEFEND_FOOL", "DEFEND_FOOL"])
    state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
    assert state["impaled_arrows"] == []
    assert state["available_arrows"] == 1
    assert state["arrows"] == [{"index": 0, "status": "Retrieved", "destroy_after_impaled": False}]
    target = state["enemies"][0]
    shot = next(c for c in state["hand"] if c["id"] == "CARD.FOOLISH_SHOT")
    state = game.act("play_card", card_index=shot["index"], target_index=target["index"])
    assert state["decision"] == "combat_play", state
    assert state["available_arrows"] == 0
    assert state["arrows"][0]["status"] == "Away"
    assert state["impaled_arrows"] == [{"target_combat_id": target["combat_id"], "arrow_indices": [0], "hit_counter": 3}]
    assert target["hp"] - state["enemies"][0]["hp"] == 21  # 15 Shot + 6 SongBow Aim
    assert not any(p["id"] == "AIM_POWER" for p in (state.get("player_powers") or []))


def test_native_user_data_is_a_process_temp_directory(game):
    import re
    import tempfile
    from pathlib import Path

    state = game.start(character="Fool", seed="fool_userdata", ascension=9)
    assert state.get("type") != "error", state
    match = re.search(r"Headless user data: (.+) \(profile 0\)", game.stderr_text())
    assert match, game.stderr_text()
    user_dir = Path(match.group(1)).resolve()
    assert user_dir.parent == (Path(tempfile.gettempdir()) / "sts2-cli").resolve()
    assert re.fullmatch(r"userdata-\d+", user_dir.name)
    assert "SlayTheSpire2" not in str(user_dir)
