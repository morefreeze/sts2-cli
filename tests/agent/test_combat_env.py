import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import math
import select
import time
import warnings
from types import SimpleNamespace
import pytest
import agent.combat_env as combat_env
from agent.combat_env import CombatEnv, greedy_action
from agent.run_workbench.recorded_maps import (
    latest_recorded_acts,
    parse_recorded_map_row,
)
from agent.strategy import rest_site_action

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def test_env_action_space_size():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.action_space.n == 41


def test_env_observation_space_shape():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    expected = env.enc.obs_size + env._EXTRA_OBS + env._RELIC_OBS
    assert env.observation_space.shape == (expected,)


def test_reward_combat_win():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._combat_start_player_max_hp = 80
    assert env._combat_win_reward({"player": {"hp": 80, "max_hp": 80}}) == pytest.approx(3.0)
    assert env._combat_win_reward({"player": {"hp": 40, "max_hp": 80}}) == pytest.approx(0.75)


def test_reward_terminal():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    assert env._terminal_reward({"victory": False}) == -2.0
    assert env._terminal_reward({"victory": True}) == 2.0


def test_shaping_reward_damage():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._prev_enemy_hp = 30
    env._prev_player_hp = 80
    env._combat_start_enemy_hp = 30
    env._combat_start_player_max_hp = 80
    r = env._shaping_reward({"enemies": [{"hp": 20}], "player": {"hp": 80}})
    assert r > 0
    assert r == pytest.approx(0.15 * 10 / 30 - 0.003)


def _boss_reward_env(room_type="Boss"):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._prev_enemy_hp = 250
    env._prev_player_hp = 80
    env._combat_start_enemy_hp = 250
    env._combat_start_player_max_hp = 80
    env._current_floor = 17
    env._current_combat_room_type = room_type
    return env


def test_boss_dense_reward_adds_progress_signal(monkeypatch):
    next_state = {"enemies": [{"hp": 200}], "player": {"hp": 80}}

    monkeypatch.delenv("STS2_BOSS_DENSE", raising=False)
    baseline = _boss_reward_env()._shaping_reward(next_state)

    monkeypatch.setenv("STS2_BOSS_DENSE", "1")
    dense = _boss_reward_env()._shaping_reward(next_state)

    assert dense >= baseline + 0.09


def test_boss_dense_reward_does_not_affect_non_boss_combats(monkeypatch):
    next_state = {"enemies": [{"hp": 200}], "player": {"hp": 80}}

    monkeypatch.delenv("STS2_BOSS_DENSE", raising=False)
    baseline = _boss_reward_env(room_type="Monster")._shaping_reward(next_state)

    monkeypatch.setenv("STS2_BOSS_DENSE", "1")
    dense = _boss_reward_env(room_type="Monster")._shaping_reward(next_state)

    assert dense == baseline


def test_reset_returns_correct_obs_shape():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    obs, info = env.reset()
    assert obs.shape == env.observation_space.shape


def test_step_dry_run_terminates():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env.reset()
    obs, reward, terminated, truncated, info = env.step(40)  # end_turn
    assert terminated  # dry_run always terminates


def _read_history_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _recording_env(monkeypatch, tmp_path, **kwargs):
    history_path = tmp_path / "deck_history.jsonl"
    monkeypatch.setenv("DECK_HISTORY_PATH", str(history_path))
    context = {
        "run_id": "eval-14000k-000",
        "checkpoint": "model_14000k.zip",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "game_version": "v0.103.2",
        "game_version_source": "cli",
    }
    context.update(kwargs.pop("run_context", {}))
    env = CombatEnv(
        cards_json=CARDS_JSON,
        character="Ironclad",
        ascension=3,
        dry_run=True,
        run_context=context,
        **kwargs,
    )
    env._run_seed = "eval_fixed_0"
    return env, history_path


def _expected_run_metadata():
    return {
        "run_id": "eval-14000k-000",
        "seed": "eval_fixed_0",
        "character": "Ironclad",
        "ascension": 3,
        "checkpoint": "model_14000k.zip",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "game_version": "v0.103.2",
        "game_version_source": "cli",
        "is_multiplayer": False,
    }


def _map_reply(*, act=1, current=(0, 0), ancient_type="Ancient"):
    nodes = [
        {
            "col": 0,
            "row": 0,
            "type": ancient_type,
            "children": [{"col": 0, "row": 1}, {"col": 1, "row": 1}],
            "visited": True,
            "current": current == (0, 0),
        },
        {
            "col": 0,
            "row": 1,
            "type": "Monster",
            "children": [{"col": 0, "row": 16}],
            "visited": current == (0, 1),
            "current": current == (0, 1),
        },
        {
            "col": 1,
            "row": 1,
            "type": "Shop",
            "children": [{"col": 0, "row": 16}],
            "visited": current == (1, 1),
            "current": current == (1, 1),
        },
    ]
    return {
        "type": "map",
        "context": {"act": act},
        "rows": [[nodes[0]], nodes[1:]],
        "boss": {"col": 0, "row": 16, "type": "Boss", "id": "TEST_BOSS"},
        "current_coord": {"col": current[0], "row": current[1]},
    }


def _producer_branch_map_reply(*, current):
    """GetFullMap shape with its distinct starting row and an open branch."""

    start_coord = (3, 0)
    monster_coord = (1, 1)
    shop_coord = (5, 1)
    return {
        "type": "map",
        "context": {
            "act": 1,
            "floor": 1 if current == start_coord else 2,
        },
        "rows": [
            [{
                "col": start_coord[0],
                "row": start_coord[1],
                "type": "Ancient",
                "children": [
                    {"col": monster_coord[0], "row": monster_coord[1]},
                    {"col": shop_coord[0], "row": shop_coord[1]},
                ],
                "visited": True,
                "current": current == start_coord,
            }],
            [
                {
                    "col": monster_coord[0],
                    "row": monster_coord[1],
                    "type": "Monster",
                    "children": [{"col": 3, "row": 16}],
                    "visited": current == monster_coord,
                    "current": current == monster_coord,
                },
                {
                    "col": shop_coord[0],
                    "row": shop_coord[1],
                    "type": "Shop",
                    "children": [{"col": 3, "row": 16}],
                    "visited": False,
                    "current": False,
                },
            ],
        ],
        "boss": {
            "col": 3,
            "row": 16,
            "type": "Boss",
            "id": "TEST_BOSS",
        },
        "current_coord": {"col": current[0], "row": current[1]},
    }


def _map_state(*, hp=80, deck=None, relics=None, potions=None):
    return {
        "decision": "map_select",
        "context": {"act": 1, "floor": 1},
        "player": {
            "hp": hp,
            "max_hp": 80,
            "gold": 99,
            "deck": deck if deck is not None else [{"id": "STRIKE"}],
            "relics": relics if relics is not None else [{"id": "BURNING_BLOOD"}],
            "potions": potions if potions is not None else [{"id": "HEALING"}],
        },
    }


def _csharp_boss_current_map_reply(*, boss_type="Boss", boss_id="TEST_BOSS"):
    """Exact GetFullMap shape at the terminal boss coordinate."""
    reply = _map_reply()
    for row in reply["rows"]:
        for node in row:
            node["current"] = False
    reply["rows"][0][0]["visited"] = True
    reply["rows"][1][0]["visited"] = True
    reply["rows"][1][0]["children"] = [{"col": 0, "row": 16}]
    reply["rows"][1][1]["visited"] = False
    reply["boss"] = {
        "col": 0,
        "row": 16,
        "type": boss_type,
        "id": boss_id,
        "name": "Test Boss",
    }
    reply["current_coord"] = {"col": 0, "row": 16}
    return reply


def _task1_serialized_boss_map_snapshot(monkeypatch, tmp_path, *, entry_hp=55):
    """Focused raw Task1 fixture for downstream map_snapshot consumers."""
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([
        _map_reply(current=(0, 0)),
        _map_reply(current=(0, 1)),
        _csharp_boss_current_map_reply(),
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))
    env._capture_run_map_state(_map_state(hp=entry_hp + 10))
    env._capture_run_map_state(_map_state(hp=entry_hp + 5))
    env._capture_run_map_state(_map_state(hp=entry_hp))
    return env, env._serialized_run_map_snapshots()[0]


def test_default_combat_env_never_requests_authoritative_map(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    commands = []
    monkeypatch.setattr(env, "_send", lambda command: commands.append(command))
    monkeypatch.setattr(
        env, "_send_read_only", lambda command: commands.append(command)
    )

    env._capture_run_map_state(_map_state())

    assert commands == []


def test_capture_replaces_graph_and_tracks_detached_ordered_node_inventories(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    first = _map_state()
    second = _map_state(hp=70, deck=[{"id": "STRIKE"}, {"id": "BASH"}])
    third = _map_state(hp=65, relics=[{"id": "ANCHOR"}])
    replies = iter([
        _map_reply(ancient_type="Ancient"),
        _map_reply(ancient_type="AncientUpdated"),
        _map_reply(current=(0, 1), ancient_type="AncientLatest"),
    ])
    commands = []

    def fake_send(command):
        commands.append(command)
        return next(replies)

    monkeypatch.setattr(env, "_send", fake_send)
    monkeypatch.setattr(env, "_send_read_only", fake_send)
    original_first = json.loads(json.dumps(first))

    env._capture_run_map_state(first)
    env._capture_run_map_state(second)
    env._capture_run_map_state(third)

    assert first == original_first
    assert commands == [{"cmd": "get_map"}] * 3
    snapshot = env._run_map_snapshots[1]
    assert snapshot["map"]["rows"][0][0]["type"] == "AncientLatest"
    assert [(node["col"], node["row"]) for node in snapshot["visited_nodes"]] == [
        (0, 0),
        (0, 1),
    ]
    first_node, second_node = snapshot["visited_nodes"]
    assert first_node["entry_player"]["hp"] == 80
    assert first_node["exit_player"]["hp"] == 65
    assert second_node["entry_player"]["hp"] == 65
    assert second_node["exit_player"]["hp"] == 65

    first["player"]["deck"][0]["id"] = "MUTATED"
    third["player"]["relics"][0]["id"] = "MUTATED"
    replies_list = snapshot["map"]["rows"]
    assert first_node["entry_player"]["deck"] == [{"id": "STRIKE"}]
    assert second_node["entry_player"]["relics"] == [{"id": "ANCHOR"}]
    assert replies_list[0][0]["type"] == "AncientLatest"


def test_producer_starting_point_capture_roundtrips_with_outcome_floor(
    monkeypatch, tmp_path
):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([
        _producer_branch_map_reply(current=(3, 0)),
        _producer_branch_map_reply(current=(1, 1)),
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda _command: next(replies))
    starting_state = _map_state(hp=80)
    ordinary_state = _map_state(hp=74)
    starting_state["context"] = {"act": 1, "floor": 1}
    ordinary_state["context"] = {"act": 1, "floor": 2}

    assert env._capture_run_map_state(starting_state) is True
    assert env._capture_run_map_state(ordinary_state) is True
    env._emit_run_outcome(ordinary_state, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    serialized = next(row for row in rows if row["event"] == "map_snapshot")
    snapshots, errors = latest_recorded_acts(iter((serialized,)))

    assert errors == ()
    parsed = snapshots[0]
    assert [
        (node["room_type"], node["col"], node["row"], node["floor"])
        for node in parsed.route_nodes
    ] == [
        ("Ancient", 3, 0, 1),
        ("Monster", 1, 1, 2),
    ]
    assert any(
        node.room_type == "Shop" and node.visited is False
        for node in parsed.act_map.nodes
    )
    outcome = next(row for row in rows if row["event"] == "outcome")
    assert outcome["status"] == "dead"
    assert outcome["max_floor"] == parsed.route_nodes[-1]["global_floor"] == 2


def test_bounded_player_snapshot_omits_only_invalid_fields(monkeypatch, tmp_path):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    state = _map_state(
        deck=[{"id": str(index)} for index in range(257)],
        relics=[{"value": float("nan")}],
        potions=[{"unsafe": object()}],
    )
    state["player"].update({"hp": 73, "max_hp": True, "gold": 123})

    snapshot = env._bounded_player_snapshot(state)

    assert snapshot == {"hp": 73, "gold": 123}


def test_player_snapshot_recursively_bounds_items_and_uses_safe_alias_fallback(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(monkeypatch, tmp_path)
    state = _map_state(
        deck=[{"id": "x" * 100_000, "upgraded": True}],
        relics=[],
        potions=[{f"key-{index}": index for index in range(33)}],
    )
    state["player"]["relics"] = {"malicious": "alias"}
    state["player"]["relic_items"] = [
        {"id": "ANCHOR", "upgraded": False}
    ]

    snapshot = env._bounded_player_snapshot(state)

    assert snapshot["hp"] == 80
    assert "deck" not in snapshot
    assert snapshot["relics"] == [{"id": "ANCHOR", "upgraded": False}]
    assert "potions" not in snapshot


@pytest.mark.parametrize(
    "unsafe_value",
    [
        {"a": {"b": {"c": {"d": {"e": "too deep"}}}}},
        {"nested": list(range(257))},
        {"huge": ["x" * 256 for _ in range(256)]},
    ],
)
def test_player_snapshot_omits_only_collection_with_unsafe_nested_value(
    monkeypatch, tmp_path, unsafe_value
):
    env, _ = _recording_env(monkeypatch, tmp_path)
    state = _map_state(
        deck=[unsafe_value],
        relics=[{"id": "ANCHOR"}],
        potions=[{"id": "HEALING"}],
    )

    snapshot = env._bounded_player_snapshot(state)

    assert "deck" not in snapshot
    assert snapshot["relics"] == [{"id": "ANCHOR"}]
    assert snapshot["potions"] == [{"id": "HEALING"}]


def test_player_snapshot_drops_unbounded_descriptions_but_keeps_item_identity(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(monkeypatch, tmp_path)
    state = _map_state(
        deck=[{
            "id": "BASH",
            "upgraded": True,
            "description": "x" * 100_000,
        }],
        relics=[{"id": "ANCHOR", "description": "x" * 100_000}],
        potions=[{"id": "HEALING", "description": "x" * 100_000}],
    )

    snapshot = env._bounded_player_snapshot(state)

    assert snapshot["deck"] == [{"id": "BASH", "upgraded": True}]
    assert snapshot["relics"] == [{"id": "ANCHOR"}]
    assert snapshot["potions"] == [{"id": "HEALING"}]


def test_player_snapshot_total_serialized_size_is_at_most_64_kib(monkeypatch, tmp_path):
    env, _ = _recording_env(monkeypatch, tmp_path)
    state = _map_state(
        # Compact JSON is under 64 KiB while the actual default serializer used
        # for map inventories is over it.  The persisted representation governs.
        deck=[{"id": "x" * 226, "upgraded": True} for _ in range(256)],
        relics=[{"id": "ANCHOR"}],
        potions=[{"id": "HEALING"}],
    )

    snapshot = env._bounded_player_snapshot(state)

    assert "deck" not in snapshot
    assert snapshot["relics"] == [{"id": "ANCHOR"}]
    assert snapshot["potions"] == [{"id": "HEALING"}]
    assert len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) <= 64 * 1024


def test_capture_rejects_invalid_acts_and_malformed_maps_with_warning(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    malformed = _map_reply(act=1)
    malformed["rows"][0][0]["current"] = False
    replies = iter([
        _map_reply(act=1),
        _map_reply(act=2),
        _map_reply(act=3),
        _map_reply(act=4),
        _map_reply(act=5),
        malformed,
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))

    with pytest.warns(RuntimeWarning) as warnings_seen:
        for _ in range(6):
            env._capture_run_map_state(_map_state())

    # Consecutive malformed observations are one failure episode.  A later
    # successful capture re-arms the warning for a new episode.
    assert len(warnings_seen) == 1
    assert set(env._run_map_snapshots) == {1, 2, 3, 4}
    assert len(env._run_map_snapshots) <= 4
    assert len(env._run_logging_errors) == 1


def test_capture_bounds_map_nodes(monkeypatch, tmp_path):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    oversized = _map_reply()
    oversized["rows"] = [[
        {
            "col": index,
            "row": 0,
            "type": "Monster",
            "children": [],
            "visited": False,
            "current": index == 0,
        }
        for index in range(257)
    ]]
    monkeypatch.setattr(env, "_send_read_only", lambda command: oversized)

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        env._capture_run_map_state(_map_state())

    assert env._run_map_snapshots == {}


class _FakeProtocolInput:
    def __init__(self, *, write_error=None, flush_error=None, on_write=None):
        self.payloads = []
        self.write_error = write_error
        self.flush_error = flush_error
        self.on_write = on_write

    def write(self, payload):
        if self.write_error is not None:
            raise self.write_error
        self.payloads.append(payload)
        if self.on_write is not None:
            self.on_write(payload)
        return len(payload)

    def flush(self):
        if self.flush_error is not None:
            raise self.flush_error
        return None


def _fake_protocol_process(*, write_error=None):
    return SimpleNamespace(
        stdin=_FakeProtocolInput(write_error=write_error),
        stdout=SimpleNamespace(fileno=lambda: 123),
    )


def test_read_only_map_timeout_preserves_process_and_drains_before_next_action(
    monkeypatch
):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=True, run_context={"capture_map": True}
    )
    proc = _fake_protocol_process()
    env._proc = proc
    env._game_alive = True
    monkeypatch.setattr(
        env,
        "_write_read_only_frame",
        lambda frame, timeout: proc.stdin.write(frame) == len(frame),
    )

    assert env._send_read_only({"cmd": "get_map"}, timeout_sec=0) is None
    assert env._proc is proc
    assert env._game_alive is True
    assert env._send_read_only({"cmd": "get_map"}, timeout_sec=0) is None
    assert len(proc.stdin.payloads) == 1
    assert env._pending_read_only_replies == 1

    replies = iter([_map_reply(), {"decision": "combat_play", "round": 2}])
    def fake_read_json(**kwargs):
        reply = next(replies)
        return ("valid", reply) if kwargs.get("return_frame_outcome") else reply
    monkeypatch.setattr(env, "_read_json", fake_read_json)

    result = env._send({"cmd": "action", "action": "end_turn"})

    assert result == {"decision": "combat_play", "round": 2}
    assert [json.loads(payload) for payload in proc.stdin.payloads] == [
        {"cmd": "get_map"},
        {"cmd": "action", "action": "end_turn"},
    ]


def test_read_only_map_write_exception_preserves_process_and_gameplay(monkeypatch):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=True, run_context={"capture_map": True}
    )
    proc = _fake_protocol_process(write_error=OSError("map write unavailable"))
    env._proc = proc
    env._game_alive = True
    monkeypatch.setattr(
        env,
        "_write_read_only_frame",
        lambda frame, timeout: (_ for _ in ()).throw(
            OSError("map write unavailable")
        ),
    )

    assert env._send_read_only({"cmd": "get_map"}) is None
    assert env._proc is proc
    assert env._game_alive is True


def test_read_only_pipe_delivery_failure_does_not_create_phantom_pending_or_block_action(
    monkeypatch,
):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=True, run_context={"capture_map": True}
    )
    input_read_fd, input_write_fd = os.pipe()
    output_read_fd, output_write_fd = os.pipe()
    os.set_blocking(input_write_fd, False)
    while True:
        try:
            os.write(input_write_fd, b"x" * 4096)
        except BlockingIOError:
            break
    protocol_input = os.fdopen(input_write_fd, "wb", buffering=4096)
    proc = _pipe_process(output_read_fd, protocol_input)
    env._proc = proc
    env._game_alive = True
    _fast_pipe_reads(monkeypatch, env)
    try:
        assert env._send_read_only({"cmd": "get_map"}, timeout_sec=0.01) is None
        assert env._pending_read_only_replies == 0
        assert env._proc is proc
        assert env._game_alive is True

        os.set_blocking(input_read_fd, False)
        while True:
            try:
                os.read(input_read_fd, 4096)
            except BlockingIOError:
                break
        combat = {"decision": "combat_play", "round": 7}
        os.write(output_write_fd, (json.dumps(combat) + "\n").encode())

        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        delivered = os.read(input_read_fd, 4096)
        assert json.loads(delivered) == {"cmd": "action", "action": "end_turn"}
        assert env._proc is proc
        assert env._game_alive is True
    finally:
        try:
            protocol_input.close()
        except OSError:
            pass
        os.close(input_read_fd)
        os.close(output_write_fd)
        os.close(output_read_fd)


def test_read_only_map_error_reply_warns_without_mutating_gameplay(monkeypatch, tmp_path):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    proc = _fake_protocol_process()
    env._proc = proc
    env._game_alive = True
    monkeypatch.setattr(env, "_write_read_only_frame", lambda frame, timeout: True)
    monkeypatch.setattr(
        env,
        "_read_json",
        lambda **kwargs: (
            ("valid", {"type": "error", "message": "map unavailable"})
            if kwargs.get("return_frame_outcome")
            else {"type": "error", "message": "map unavailable"}
        ),
    )

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        env._capture_run_map_state(_map_state())

    assert env._proc is proc
    assert env._game_alive is True
    assert env._pending_read_only_replies == 0
    assert env._run_map_snapshots == {}


@pytest.mark.parametrize("malformed_frame", [b"{not-json}\n", b"{\xff}\n"])
def test_malformed_map_frame_does_not_discard_next_gameplay_response(
    monkeypatch, malformed_frame
):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=True, run_context={"capture_map": True}
    )
    read_fd, write_fd = os.pipe()
    terminated = []
    proc = SimpleNamespace(
        stdin=_FakeProtocolInput(),
        stdout=SimpleNamespace(fileno=lambda: read_fd),
        terminate=lambda: terminated.append("terminate"),
        wait=lambda timeout=None: None,
        kill=lambda: terminated.append("kill"),
    )
    env._proc = proc
    env._game_alive = True
    monkeypatch.setattr(env, "_write_read_only_frame", lambda frame, timeout: True)
    real_read_json = env._read_json

    def fast_read_json(timeout_sec=5.0, **kwargs):
        return real_read_json(timeout_sec=0.03, **kwargs)

    monkeypatch.setattr(env, "_read_json", fast_read_json)
    try:
        os.write(write_fd, malformed_frame)

        assert env._send_read_only({"cmd": "get_map"}) is None
        assert env._pending_read_only_replies == 0

        combat = {"decision": "combat_play", "round": 2}
        os.write(write_fd, (json.dumps(combat) + "\n").encode())

        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        assert env._proc is proc
        assert env._game_alive is True
        assert terminated == []
        assert all(b'"cmd":"quit"' not in payload.replace(b" ", b"")
                   for payload in proc.stdin.payloads)
    finally:
        os.close(write_fd)
        os.close(read_fd)


def _pipe_process(read_fd, protocol_input, events=None):
    events = events if events is not None else []
    return SimpleNamespace(
        stdin=protocol_input,
        stdout=SimpleNamespace(fileno=lambda: read_fd),
        terminate=lambda: events.append("terminate"),
        wait=lambda timeout=None: None,
        kill=lambda: events.append("kill"),
    )


def _fast_pipe_reads(monkeypatch, env):
    real_read_json = env._read_json
    monkeypatch.setattr(
        env,
        "_read_json",
        lambda timeout_sec=5.0, **kwargs: real_read_json(
            timeout_sec=0.05, **kwargs
        ),
    )


def test_pending_map_is_drained_before_action_and_buffered_gameplay_is_reused(
    monkeypatch
):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    combat = {"decision": "combat_play", "round": 3}

    def reply_to_action(payload):
        if json.loads(payload).get("cmd") == "action":
            assert env._pending_read_only_replies == 0
            os.write(write_fd, (json.dumps(combat) + "\n").encode())

    protocol_input = _FakeProtocolInput(on_write=reply_to_action)
    proc = _pipe_process(read_fd, protocol_input)
    env._proc = proc
    env._game_alive = True
    env._pending_read_only_replies = 1
    _fast_pipe_reads(monkeypatch, env)
    try:
        late_map = json.dumps(_map_reply()).encode() + b"\n"
        os.write(write_fd, late_map)

        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        assert env._proc is proc
        assert env._game_alive is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_read_json_reuses_second_complete_frame_already_in_buffer(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    proc = _pipe_process(read_fd, _FakeProtocolInput())
    env._proc = proc
    env._game_alive = True
    _fast_pipe_reads(monkeypatch, env)
    first = _map_reply()
    second = {"decision": "combat_play", "round": 4}
    try:
        os.write(
            write_fd,
            (json.dumps(first) + "\n" + json.dumps(second) + "\n").encode(),
        )

        assert env._read_json() == first
        assert env._read_json() == second
        assert env._proc is proc
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_pending_partial_map_frame_completes_before_action_write(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    combat = {"decision": "combat_play", "round": 5}
    full_map = (json.dumps(_map_reply()) + "\n").encode()
    split = len(full_map) // 2

    def reply_to_action(payload):
        if json.loads(payload).get("cmd") == "action":
            assert env._pending_read_only_replies == 0
            os.write(write_fd, (json.dumps(combat) + "\n").encode())

    proc = _pipe_process(
        read_fd, _FakeProtocolInput(on_write=reply_to_action)
    )
    env._proc = proc
    env._game_alive = True
    env._pending_read_only_replies = 1
    _fast_pipe_reads(monkeypatch, env)
    writer = None
    try:
        os.write(write_fd, full_map[:split])

        import threading
        writer = threading.Thread(
            target=lambda: (time.sleep(0.01), os.write(write_fd, full_map[split:])),
        )
        writer.start()
        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        writer.join(timeout=1)
        assert env._proc is proc
    finally:
        if writer is not None:
            writer.join(timeout=1)
        os.close(write_fd)
        os.close(read_fd)


def test_pending_malformed_map_frame_does_not_consume_gameplay(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    combat = {"decision": "combat_play", "round": 6}

    def reply_to_action(payload):
        if json.loads(payload).get("cmd") == "action":
            assert env._pending_read_only_replies == 0
            os.write(write_fd, (json.dumps(combat) + "\n").encode())

    proc = _pipe_process(
        read_fd, _FakeProtocolInput(on_write=reply_to_action)
    )
    env._proc = proc
    env._game_alive = True
    env._pending_read_only_replies = 1
    _fast_pipe_reads(monkeypatch, env)
    try:
        os.write(write_fd, b"{late-malformed}\n")

        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        assert env._proc is proc
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_pending_eof_stops_before_writing_gameplay(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    protocol_input = _FakeProtocolInput()
    proc = _pipe_process(read_fd, protocol_input)
    env._proc = proc
    env._game_alive = True
    env._pending_read_only_replies = 1
    _fast_pipe_reads(monkeypatch, env)
    os.close(write_fd)
    try:
        assert env._send({"cmd": "action", "action": "end_turn"}) is None
        assert not any(json.loads(payload).get("cmd") == "action"
                       for payload in protocol_input.payloads)
        assert env._proc is None
        assert env._game_alive is False
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("failure_field", ["write_error", "flush_error"])
def test_action_transport_failure_happens_after_pending_map_drain(
    monkeypatch, failure_field
):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    read_fd, write_fd = os.pipe()
    kwargs = {failure_field: OSError(f"{failure_field} unavailable")}
    protocol_input = _FakeProtocolInput(**kwargs)
    proc = _pipe_process(read_fd, protocol_input)
    env._proc = proc
    env._game_alive = True
    env._pending_read_only_replies = 1
    _fast_pipe_reads(monkeypatch, env)
    try:
        os.write(write_fd, (json.dumps(_map_reply()) + "\n").encode())

        assert env._send({"cmd": "action", "action": "end_turn"}) is None
        assert env._pending_read_only_replies == 0
        assert env._proc is None
        assert select.select([read_fd], [], [], 0)[0] == []
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_start_proc_clears_pending_protocol_state(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    fake_proc = _fake_protocol_process()
    env._pending_read_only_replies = 1
    env._read_buf = b"stale\n"
    env._run_pending_map_capture = {"state_id": 1, "state": {"player": {}}}
    env._run_map_retry_state = {"state_id": 2, "state": {"player": {}}}
    monkeypatch.setattr(combat_env.subprocess, "Popen", lambda *args, **kwargs: fake_proc)
    monkeypatch.setattr(
        combat_env, "open", lambda *args, **kwargs: SimpleNamespace(), raising=False
    )
    monkeypatch.setattr(env, "_read_json", lambda **kwargs: {"type": "ready"})

    env._start_proc()

    assert env._pending_read_only_replies == 0
    assert env._read_buf == b""
    assert env._run_pending_map_capture is None
    assert env._run_map_retry_state is None


def test_late_valid_map_is_ingested_with_detached_entry_before_gameplay(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    state = {**_map_state(hp=61), "decision": "combat_play"}
    read_fd, write_fd = os.pipe()
    combat = {**state, "round": 2}

    def reply_to_action(payload):
        if json.loads(payload).get("cmd") == "action":
            os.write(write_fd, (json.dumps(combat) + "\n").encode())

    proc = _pipe_process(read_fd, _FakeProtocolInput(on_write=reply_to_action))
    env._proc = proc
    env._game_alive = True
    _fast_pipe_reads(monkeypatch, env)

    def timeout_after_delivery(command, **kwargs):
        env._pending_read_only_replies = 1
        return None

    monkeypatch.setattr(env, "_send_read_only", timeout_after_delivery)
    try:
        with pytest.warns(RuntimeWarning, match="map capture failed"):
            env._poll_run_map_state_once(state)
        state["player"]["hp"] = 12
        os.write(write_fd, (json.dumps(_map_reply(current=(0, 0))) + "\n").encode())

        assert env._send({"cmd": "action", "action": "end_turn"}) == combat
        node = env._run_map_snapshots[1]["visited_nodes"][0]
        assert (node["col"], node["row"]) == (0, 0)
        assert node["entry_player"]["hp"] == 61
        assert env._run_last_map_poll_state_id == id(state)
        assert env._run_pending_map_capture is None
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_immediate_map_error_retries_once_at_first_gameplay_boundary(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    env.dry_run = False
    current = combat_env._dummy_combat_state()
    current["context"] = {"act": 1, "floor": 1, "room_type": "Monster"}
    current["player"].update({
        "gold": 50,
        "deck": [{"id": "STRIKE"}],
        "relics": [{"id": "BURNING_BLOOD"}],
        "potions": [],
    })
    next_state = json.loads(json.dumps(current))
    next_state["round"] = 2
    env._current_state = current
    env._game_alive = True
    replies = iter([
        {"type": "error", "message": "temporary"},
        _map_reply(),
    ])
    map_calls = []

    def map_reply(command, **kwargs):
        map_calls.append(command)
        return next(replies)

    monkeypatch.setattr(env, "_send_read_only", map_reply)
    monkeypatch.setattr(env.enc, "decode", lambda action, state: {"action": "play_card"})
    monkeypatch.setattr(env, "_send", lambda command: next_state)
    monkeypatch.setattr(env, "_shaping_reward", lambda state: 0.0)

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        env._poll_run_map_state_once(current)
    env.step(0)

    assert map_calls == [{"cmd": "get_map"}, {"cmd": "get_map"}]
    assert env._run_last_map_poll_state_id == id(current)
    assert env._run_map_snapshots[1]["visited_nodes"][0]["entry_player"]["hp"] == 80


def test_late_new_coord_terminal_inventory_is_not_attributed_to_old_coord(
    monkeypatch, tmp_path
):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    old_state = _map_state(hp=80)
    new_state = {**_map_state(hp=55), "decision": "combat_play"}
    monkeypatch.setattr(env, "_send_read_only", lambda command: _map_reply())
    env._poll_run_map_state_once(old_state)

    def timeout_after_delivery(command, **kwargs):
        env._pending_read_only_replies = 1
        return None

    monkeypatch.setattr(env, "_send_read_only", timeout_after_delivery)
    with pytest.warns(RuntimeWarning, match="map capture failed"):
        env._poll_run_map_state_once(new_state)

    read_fd, write_fd = os.pipe()
    terminal = {
        "decision": "game_over",
        "victory": False,
        "player": {"hp": 0, "max_hp": 80, "gold": 50, "deck": [],
                   "relics": [], "potions": []},
    }

    def reply_to_action(payload):
        if json.loads(payload).get("cmd") == "action":
            os.write(write_fd, (json.dumps(terminal) + "\n").encode())

    env._proc = _pipe_process(
        read_fd, _FakeProtocolInput(on_write=reply_to_action)
    )
    env._game_alive = True
    _fast_pipe_reads(monkeypatch, env)
    try:
        os.write(write_fd, (json.dumps(_map_reply(current=(0, 1))) + "\n").encode())
        result = env._send({"cmd": "action", "action": "end_turn"})
        env._emit_run_outcome(result, victory=False, status="dead")

        rows = _read_history_rows(history_path)
        snapshot = next(row for row in rows if row["event"] == "map_snapshot")
        old_node, new_node = snapshot["visited_nodes"]
        assert (old_node["col"], old_node["row"]) == (0, 0)
        assert old_node["exit_player"]["hp"] == 55
        assert (new_node["col"], new_node["row"]) == (0, 1)
        assert new_node["entry_player"]["hp"] == 55
        assert new_node["exit_player"]["hp"] == 0
    finally:
        os.close(write_fd)
        os.close(read_fd)


def _set_map_reply(monkeypatch, env, reply):
    monkeypatch.setattr(env, "_send", lambda command: reply)
    monkeypatch.setattr(env, "_send_read_only", lambda command: reply, raising=False)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda reply: reply["rows"][0][0].update({
            "children": [{"col": index, "row": 1} for index in range(2049)]
        }),
        lambda reply: reply["rows"][0][0].update({"type": "x" * 100_000}),
        lambda reply: reply["boss"].update({"type": "x" * 100_000}),
        lambda reply: reply.update({"rows": ([[] for _ in range(257)] + reply["rows"]) }),
        lambda reply: (
            reply["rows"][0][0].update({"col": 10**100_000}),
            reply["current_coord"].update({"col": 10**100_000}),
        ),
    ],
)
def test_capture_rejects_unbounded_raw_map_shapes(monkeypatch, tmp_path, mutate):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    reply = _map_reply()
    mutate(reply)
    _set_map_reply(monkeypatch, env, reply)

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        env._capture_run_map_state(_map_state())

    assert env._run_map_snapshots == {}


def test_capture_sanitizes_map_allowlist_and_omits_oversized_optional_labels(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    reply = _map_reply()
    reply["arbitrary"] = {"nested": ["x" * 100_000]}
    reply["context"].update({
        "floor": 1,
        "room_type": "Ancient",
        "arbitrary": {"nested": True},
    })
    reply["rows"][0][0]["arbitrary"] = ["x" * 100_000]
    reply["rows"][0][0]["children"][0]["arbitrary"] = "x" * 100_000
    reply["boss"].update({
        "id": "x" * 100_000,
        "name": "x" * 100_000,
        "arbitrary": {"nested": True},
    })
    reply["current_coord"]["arbitrary"] = "x" * 100_000
    _set_map_reply(monkeypatch, env, reply)

    env._capture_run_map_state(_map_state())

    raw_map = env._run_map_snapshots[1]["map"]
    assert set(raw_map) == {"type", "context", "rows", "boss", "current_coord"}
    assert set(raw_map["context"]) == {"act", "floor", "room_type"}
    assert set(raw_map["rows"][0][0]) == {
        "col", "row", "type", "children", "visited", "current",
    }
    assert set(raw_map["rows"][0][0]["children"][0]) == {"col", "row"}
    assert raw_map["boss"] == {
        "col": 0,
        "row": 16,
        "type": "Boss",
        "visited": False,
        "current": False,
    }
    assert raw_map["current_coord"] == {"col": 0, "row": 0}
    assert len(json.dumps(raw_map)) < 10_000


def test_csharp_boss_current_map_is_accepted_and_serializes_boss_inventory(
    monkeypatch, tmp_path
):
    reply = _csharp_boss_current_map_reply()

    act, raw_map, current_coord, current_node, graph_coords = (
        CombatEnv._validated_map_reply(reply)
    )

    assert act == 1
    assert current_coord == (0, 16)
    assert current_node == {
        "col": 0,
        "row": 16,
        "type": "Boss",
        "id": "TEST_BOSS",
        "name": "Test Boss",
        "visited": True,
        "current": True,
    }
    assert (0, 16) in graph_coords
    assert all(
        node["current"] is False
        for row in raw_map["rows"]
        for node in row
    )
    assert raw_map["boss"]["visited"] is True
    assert raw_map["boss"]["current"] is True

    env, snapshot = _task1_serialized_boss_map_snapshot(
        monkeypatch, tmp_path, entry_hp=55
    )
    env._update_buffered_node_inventory(_map_state(hp=31))
    snapshot = env._serialized_run_map_snapshots()[0]
    boss_visit = snapshot["visited_nodes"][-1]
    assert snapshot["event"] == "map_snapshot"
    assert snapshot["map"]["boss"]["current"] is True
    assert (boss_visit["col"], boss_visit["row"]) == (0, 16)
    assert boss_visit["type"] == "Boss"
    assert boss_visit["id"] == "TEST_BOSS"
    assert boss_visit["entry_player"]["hp"] == 55
    assert boss_visit["exit_player"]["hp"] == 31


def test_first_successful_poll_at_boss_does_not_erase_authoritative_prior_visits(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([
        {"type": "error", "message": "temporary"},
        {"type": "error", "message": "temporary"},
        _csharp_boss_current_map_reply(),
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        assert env._capture_run_map_state(_map_state(hp=70)) is False
    assert env._capture_run_map_state(_map_state(hp=65)) is False
    accepted = env._capture_run_map_state(_map_state(hp=55))

    assert accepted is False
    assert env._run_map_snapshots == {}
    assert env._serialized_run_map_snapshots() == []


def test_nonboss_current_infers_false_boss_markers_and_ignores_producer_values():
    reply = _map_reply(current=(0, 0))
    reply["boss"].update({"visited": True, "current": True})

    _, raw_map, current_coord, current_node, graph_coords = (
        CombatEnv._validated_map_reply(reply)
    )

    assert current_coord == (0, 0)
    assert current_node["type"] == "Ancient"
    assert (0, 16) in graph_coords
    assert raw_map["boss"]["visited"] is False
    assert raw_map["boss"]["current"] is False


@pytest.mark.parametrize("case", ["row_and_boss_current", "missing_current"])
def test_map_rejects_inconsistent_current_coord_across_rows_and_boss(case):
    if case == "row_and_boss_current":
        reply = _map_reply(current=(0, 0))
        reply["current_coord"] = {"col": 0, "row": 16}
    else:
        reply = _csharp_boss_current_map_reply()
        reply["current_coord"] = {"col": 99, "row": 99}

    with pytest.raises(ValueError):
        CombatEnv._validated_map_reply(reply)


def test_same_act_forward_route_reconciles_markers_and_remains_parseable(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    middle = _map_reply(current=(0, 1))
    boss = _csharp_boss_current_map_reply()
    for reply in (middle, boss):
        reply["rows"][1][0]["children"] = [{"col": 0, "row": 16}]
    replies = iter([
        _map_reply(current=(0, 0)),
        middle,
        boss,
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))

    env._capture_run_map_state(_map_state(hp=55))
    env._capture_run_map_state(_map_state(hp=50))
    env._capture_run_map_state(_map_state(hp=44))

    snapshot = env._run_map_snapshots[1]
    assert [(node["col"], node["row"]) for node in snapshot["visited_nodes"]] == [
        (0, 0),
        (0, 1),
        (0, 16),
    ]
    assert snapshot["visited_nodes"][0]["entry_player"]["hp"] == 55
    assert snapshot["visited_nodes"][1]["entry_player"]["hp"] == 50
    assert snapshot["visited_nodes"][2]["entry_player"]["hp"] == 44
    assert (0, 16) in snapshot["_coord_lookup"]
    serialized = env._serialized_run_map_snapshots()[0]
    assert serialized["map"]["rows"][1][0]["visited"] is True
    assert serialized["map"]["boss"]["visited"] is True
    assert parse_recorded_map_row(serialized).route_nodes[-1]["model_id"] == (
        "TEST_BOSS"
    )


def test_boss_to_ordinary_refresh_fails_closed_without_mutating_prior_snapshot(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([
        _map_reply(current=(0, 0)),
        _map_reply(current=(0, 1)),
        _csharp_boss_current_map_reply(),
        _map_reply(current=(0, 0)),
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))

    env._capture_run_map_state(_map_state(hp=65))
    env._capture_run_map_state(_map_state(hp=60))
    env._capture_run_map_state(_map_state(hp=55))
    before = env._serialized_run_map_snapshots()[0]
    assert parse_recorded_map_row(before).route_nodes[-1]["model_id"] == "TEST_BOSS"

    with pytest.warns(RuntimeWarning, match="map capture failed"):
        accepted = env._capture_run_map_state(_map_state(hp=44))

    serialized = env._serialized_run_map_snapshots()[0]
    assert accepted is False
    assert serialized == before
    assert env._run_current_map_coord == (1, 0, 16)
    assert serialized["visited_nodes"][-1]["exit_player"]["hp"] == 55
    assert "children" not in serialized["map"]["boss"]
    assert parse_recorded_map_row(serialized).route_nodes[-1]["model_id"] == (
        "TEST_BOSS"
    )


def test_same_act_graph_replacement_drops_stale_visits_and_preserves_survivors(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    third_reply = _map_reply(current=(1, 1))
    third_reply["rows"][1][0]["children"] = [{"col": 1, "row": 1}]
    third_reply["rows"][1][0]["visited"] = True
    third_reply["rows"] = [third_reply["rows"][1]]
    replies = iter([
        _map_reply(current=(0, 0)),
        _map_reply(current=(0, 1)),
        third_reply,
    ])
    monkeypatch.setattr(env, "_send", lambda command: next(replies))
    monkeypatch.setattr(
        env, "_send_read_only", lambda command: next(replies), raising=False
    )

    env._capture_run_map_state(_map_state(hp=80))
    env._capture_run_map_state(_map_state(hp=70))
    env._capture_run_map_state(_map_state(hp=60))

    snapshot = env._run_map_snapshots[1]
    assert [(node["col"], node["row"]) for node in snapshot["visited_nodes"]] == [
        (0, 1),
        (1, 1),
    ]
    assert set(snapshot["_coord_lookup"]) == {(0, 1), (1, 1)}
    assert snapshot["visited_nodes"][0]["entry_player"]["hp"] == 70
    assert snapshot["visited_nodes"][0]["exit_player"]["hp"] == 60
    assert env._run_current_map_coord == (1, 1, 1)


def test_pending_map_capture_skip_does_not_repeat_warning(monkeypatch, tmp_path):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    env._pending_read_only_replies = 1
    monkeypatch.setattr(
        env,
        "_send_read_only",
        lambda command: (_ for _ in ()).throw(AssertionError("duplicate poll")),
    )

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        for _ in range(25):
            env._capture_run_map_state(_map_state())

    assert seen == []
    assert env._run_logging_errors == []


def test_map_capture_warns_once_per_failure_episode_and_resets_after_success(
    monkeypatch, tmp_path
):
    env, _ = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([
        {"type": "error", "message": "unavailable"},
        {"type": "error", "message": "unavailable"},
        _map_reply(),
        {"type": "error", "message": "unavailable"},
    ])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        for _ in range(4):
            env._capture_run_map_state(_map_state())

    assert len(seen) == 2
    assert len(env._run_logging_errors) == 1


def test_run_logging_errors_are_deduplicated_and_bounded(monkeypatch, tmp_path):
    env, _ = _recording_env(monkeypatch, tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for index in range(100):
            env._report_run_logging_error(f"failure-{index}", ValueError("bad"))
        env._report_run_logging_error("failure-99", ValueError("bad"))

    assert len(env._run_logging_errors) == 64
    assert len(set(env._run_logging_errors)) == 64


def test_fresh_run_resets_logging_error_state(monkeypatch, tmp_path):
    env, _ = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._run_logging_errors = ["old failure"]
    env._run_map_capture_failure_active = True
    env._run_pending_map_capture = {"state_id": 1, "state": {"player": {}}}
    env._run_map_retry_state = {"state_id": 2, "state": {"player": {}}}
    state = combat_env._dummy_combat_state()
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)
    monkeypatch.setattr(env, "_send", lambda command: state)
    monkeypatch.setattr(env, "_advance_to_combat", lambda current: current)

    env.reset()

    assert env._run_logging_errors == []
    assert env._run_map_capture_failure_active is False
    assert env._run_pending_map_capture is None
    assert env._run_map_retry_state is None


@pytest.mark.parametrize(
    "status", ["dead", "crash", "timeout", "stuck", "invalid", "reset_failure"]
)
def test_terminal_outcome_flushes_maps_before_decisions_without_subprocess(
    monkeypatch, tmp_path, status
):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    replies = iter([_map_reply(act=2), _map_reply(act=1)])
    monkeypatch.setattr(env, "_send_read_only", lambda command: next(replies))
    env._capture_run_map_state(_map_state(hp=80))
    env._capture_run_map_state(_map_state(hp=70))
    env._run_milestone_records = [{"event": "milestone", "floor": 7}]
    env._run_card_pick_records = [{"event": "card_pick", "picked": "BASH"}]
    monkeypatch.setattr(
        env,
        "_send",
        lambda command: (_ for _ in ()).throw(AssertionError("terminal map poll")),
    )

    env._emit_run_outcome(_map_state(hp=0), victory=False, status=status)

    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == [
        "run_start",
        "map_snapshot",
        "map_snapshot",
        "milestone",
        "card_pick",
        "outcome",
    ]
    assert [row["act"] for row in rows[1:3]] == [1, 2]
    assert all(row["is_multiplayer"] is False for row in rows)
    assert all(math.isfinite(row["ts"]) for row in rows if "ts" in row)
    assert rows[-1]["status"] == status


def test_failure_before_first_map_does_not_fabricate_snapshot(monkeypatch, tmp_path):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )

    env._emit_run_outcome({}, victory=False, status="reset_failure")

    assert [row["event"] for row in _read_history_rows(history_path)] == [
        "run_start",
        "outcome",
    ]


def test_advance_polls_each_distinct_state_once_without_post_potion_duplicate(monkeypatch):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=True, run_context={"capture_map": True}
    )
    start = _map_state()
    combat = {**_map_state(hp=70), "decision": "combat_play"}
    post_potion = {**_map_state(hp=75), "decision": "combat_play"}
    captured = []
    monkeypatch.setattr(env, "_capture_run_map_state", lambda state: captured.append(state))
    monkeypatch.setattr(combat_env, "greedy_action", lambda state: {"cmd": "action"})
    monkeypatch.setattr(env, "_send", lambda command: combat)
    monkeypatch.setattr(env, "_greedy_use_potions", lambda state: post_potion)

    result = env._advance_to_combat(start)

    assert result is post_potion
    assert captured == [start, combat]


def test_combat_step_updates_inventory_without_polling_map(monkeypatch):
    env = CombatEnv(
        cards_json=CARDS_JSON, dry_run=False, run_context={"capture_map": True}
    )
    current = combat_env._dummy_combat_state()
    current["context"] = {"act": 1, "floor": 1, "room_type": "Monster"}
    next_state = json.loads(json.dumps(current))
    next_state["round"] = 2
    env._current_state = current
    env._game_alive = True
    captured = []
    updated = []
    monkeypatch.setattr(env.enc, "decode", lambda action, state: {"action": "play_card"})
    monkeypatch.setattr(env, "_send", lambda command: next_state)
    monkeypatch.setattr(env, "_capture_run_map_state", lambda state: captured.append(state))
    monkeypatch.setattr(env, "_update_buffered_node_inventory", lambda state: updated.append(state))
    monkeypatch.setattr(env, "_shaping_reward", lambda state: 0.0)

    _, _, terminated, _, _ = env.step(0)

    assert terminated is False
    assert captured == []
    assert updated == [next_state]


def test_run_start_and_outcome_share_authoritative_metadata(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)

    env._emit_run_start()
    env._emit_run_start()
    env._emit_run_outcome({}, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "outcome"]
    for row in rows:
        assert {key: row[key] for key in _expected_run_metadata()} == _expected_run_metadata()
        assert math.isfinite(row["ts"])


def test_native_save_context_can_authoritatively_override_generated_seed(
    monkeypatch, tmp_path
):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"seed": None}
    )
    env._run_seed = "fake-generated-seed"

    env._emit_run_start()
    env._emit_run_outcome({}, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert [row["seed"] for row in rows] == [None, None]


def test_failed_run_start_is_retried_with_terminal_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    real_open = open
    attempts = 0

    def flaky_open(path, *args, **kwargs):
        nonlocal attempts
        if path == str(history_path):
            attempts += 1
            if attempts == 1:
                raise OSError("start disk unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(combat_env, "open", flaky_open, raising=False)

    with pytest.warns(RuntimeWarning, match="start disk unavailable"):
        env._emit_run_start()

    assert env._run_start_emitted is False
    assert len(env._run_logging_errors) == 1

    env._emit_run_outcome({}, victory=False, status="dead")

    assert env._run_start_emitted is True
    assert env._run_outcome_emitted is True
    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "outcome"]
    for row in rows:
        assert {key: row[key] for key in _expected_run_metadata()} == _expected_run_metadata()


def test_run_start_close_failure_after_flush_does_not_duplicate_start(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    real_open = open
    wrapped = False
    flush_calls = 0

    class CloseFailingFile:
        def __init__(self, file_obj):
            self._file = file_obj

        def __enter__(self):
            self._file.__enter__()
            return self

        def write(self, payload):
            return self._file.write(payload)

        def flush(self):
            nonlocal flush_calls
            flush_calls += 1
            return self._file.flush()

        def fileno(self):
            return self._file.fileno()

        def seek(self, *args):
            return self._file.seek(*args)

        def tell(self):
            return self._file.tell()

        def truncate(self, *args):
            return self._file.truncate(*args)

        def __exit__(self, exc_type, exc_value, traceback):
            self._file.__exit__(exc_type, exc_value, traceback)
            raise OSError("close failed after flush")

    def close_failing_open(path, *args, **kwargs):
        nonlocal wrapped
        file_obj = real_open(path, *args, **kwargs)
        if path == str(history_path) and not wrapped:
            wrapped = True
            return CloseFailingFile(file_obj)
        return file_obj

    monkeypatch.setattr(combat_env, "open", close_failing_open, raising=False)

    with pytest.warns(RuntimeWarning, match="close failed after flush"):
        env._emit_run_start()

    assert flush_calls == 1
    assert env._run_start_emitted is True

    env._emit_run_outcome({}, victory=False, status="dead")

    assert [row["event"] for row in _read_history_rows(history_path)] == [
        "run_start",
        "outcome",
    ]


def test_run_start_flush_failure_remains_retryable(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    real_open = open
    wrapped = False

    class FlushFailingFile:
        def __init__(self, file_obj):
            self._file = file_obj
            self._failed = False

        def __enter__(self):
            self._file.__enter__()
            return self

        def write(self, payload):
            return self._file.write(payload)

        def flush(self):
            if not self._failed:
                self._failed = True
                raise OSError("flush unavailable")
            return self._file.flush()

        def fileno(self):
            return self._file.fileno()

        def seek(self, *args):
            return self._file.seek(*args)

        def tell(self):
            return self._file.tell()

        def truncate(self, *args):
            return self._file.truncate(*args)

        def __exit__(self, exc_type, exc_value, traceback):
            return self._file.__exit__(exc_type, exc_value, traceback)

    def flush_failing_open(path, *args, **kwargs):
        nonlocal wrapped
        file_obj = real_open(path, *args, **kwargs)
        if path == str(history_path) and not wrapped:
            wrapped = True
            return FlushFailingFile(file_obj)
        return file_obj

    monkeypatch.setattr(combat_env, "open", flush_failing_open, raising=False)

    with pytest.warns(RuntimeWarning, match="flush unavailable"):
        env._emit_run_start()

    assert env._run_start_emitted is False

    env._emit_run_outcome({}, victory=False, status="dead")

    assert [row["event"] for row in _read_history_rows(history_path)] == [
        "run_start",
        "outcome",
    ]


def test_run_outcome_retains_identity_and_is_idempotent(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env._run_milestone_records = [{"event": "milestone", "floor": 7}]
    env._run_max_floor = 21

    env._emit_run_outcome({}, victory=False, status="dead")
    env._emit_run_outcome({}, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "milestone", "outcome"]
    outcome = rows[-1]
    assert outcome["event"] == "outcome"
    assert outcome["run_id"] == "eval-14000k-000"
    assert outcome["max_floor"] == 21
    assert outcome["won"] is False
    assert outcome["seed"] == "eval_fixed_0"
    assert outcome["character"] == "Ironclad"
    assert outcome["ascension"] == 3
    assert outcome["checkpoint"] == "model_14000k.zip"
    assert outcome["evaluation_mode"] == "fixed"
    assert outcome["scenario"] == "full_run"
    assert outcome["game_version"] == "v0.103.2"
    assert outcome["game_version_source"] == "cli"
    assert outcome["status"] == "dead"
    assert outcome["technical_failure_kind"] is None


@pytest.mark.parametrize("status", ["crash", "timeout", "stuck", "reset_failure", "invalid"])
def test_technical_run_outcome_is_never_a_win(monkeypatch, tmp_path, status):
    env, history_path = _recording_env(monkeypatch, tmp_path)

    env._emit_run_outcome({}, victory=True, status=status)

    outcome = _read_history_rows(history_path)[-1]
    assert outcome["status"] == status
    assert outcome["won"] is False
    assert outcome["technical_failure_kind"] == status


def test_dead_process_step_emits_crash_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._game_alive = False

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["crashed"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "crash"


def test_combat_step_limit_emits_timeout_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._game_alive = True
    env._combat_steps = env.max_combat_steps

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["timeout"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "timeout"


def test_ignored_end_turn_emits_stuck_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._current_state["round"] = 2
    env._game_alive = True
    monkeypatch.setattr(env.enc, "decode", lambda action, state: {"action": "end_turn"})
    monkeypatch.setattr(env, "_send", lambda command: env._current_state)
    monkeypatch.setattr(env, "_kill_proc", lambda: None)

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["stuck"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "stuck"


def test_failed_start_emits_reset_failure_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)
    monkeypatch.setattr(env, "_send", lambda command: {"type": "error", "message": "nope"})

    _, info = env.reset()

    assert info["reset_failure"] is True
    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "outcome"]
    assert rows[-1]["status"] == "reset_failure"


def test_fresh_reset_emits_run_start_before_process_start(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    calls = []
    state = combat_env._dummy_combat_state()

    monkeypatch.setattr(env, "_kill_proc", lambda: None)

    def fake_start_proc():
        calls.append("start_proc")
        assert [row["event"] for row in _read_history_rows(history_path)] == ["run_start"]

    def fake_send(command):
        calls.append(command["cmd"])
        return state

    monkeypatch.setattr(env, "_start_proc", fake_start_proc)
    monkeypatch.setattr(env, "_send", fake_send)
    monkeypatch.setattr(env, "_advance_to_combat", lambda current: current)

    env.reset()

    assert calls == ["start_proc", "start_run"]


def test_run_outcome_logging_failure_is_visible_and_retryable(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env._emit_run_start()
    real_open = open
    attempts = 0

    def flaky_open(path, *args, **kwargs):
        nonlocal attempts
        if path == str(history_path):
            attempts += 1
            if attempts == 1:
                raise OSError("disk unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(combat_env, "open", flaky_open, raising=False)

    with pytest.warns(RuntimeWarning, match="disk unavailable"):
        env._emit_run_outcome({}, victory=False, status="crash")

    assert env._run_start_emitted is True
    assert env._run_outcome_emitted is False
    assert len(env._run_logging_errors) == 1
    assert "disk unavailable" in env._run_logging_errors[0]

    env._emit_run_outcome({}, victory=False, status="crash")

    assert env._run_outcome_emitted is True
    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "outcome"]
    assert [row["event"] for row in rows].count("run_start") == 1
    assert [row["event"] for row in rows].count("outcome") == 1


class _DelegatingAppendFile:
    def __init__(self, file_obj):
        self._file = file_obj

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._file.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._file, name)


class _PartialWriteFile(_DelegatingAppendFile):
    def write(self, payload):
        self._file.write(payload[: max(1, len(payload) // 2)])
        self._file.flush()
        raise OSError("partial append")


class _FlushAfterWriteFile(_DelegatingAppendFile):
    def __init__(self, file_obj):
        super().__init__(file_obj)
        self._failed = False

    def write(self, payload):
        return self._file.write(payload)

    def flush(self):
        if not self._failed:
            self._failed = True
            raise OSError("flush after write")
        return self._file.flush()


class _CloseAfterFlushFile(_DelegatingAppendFile):
    def write(self, payload):
        return self._file.write(payload)

    def flush(self):
        return self._file.flush()

    def __exit__(self, exc_type, exc_value, traceback):
        self._file.__exit__(exc_type, exc_value, traceback)
        raise OSError("close after committed flush")


def _terminal_batch_env(monkeypatch, tmp_path):
    env, history_path = _recording_env(
        monkeypatch, tmp_path, run_context={"capture_map": True}
    )
    env._emit_run_start()
    _set_map_reply(monkeypatch, env, _map_reply())
    env._capture_run_map_state(_map_state(hp=70))
    env._run_milestone_records = [{"event": "milestone", "floor": 7}]
    env._run_card_pick_records = [{"event": "card_pick", "picked": "BASH"}]
    return env, history_path


@pytest.mark.parametrize("wrapper_type", [_PartialWriteFile, _FlushAfterWriteFile])
def test_terminal_batch_append_rolls_back_before_retry(
    monkeypatch, tmp_path, wrapper_type
):
    env, history_path = _terminal_batch_env(monkeypatch, tmp_path)
    before = history_path.read_bytes()
    real_open = open
    wrapped = False

    def failing_open(path, *args, **kwargs):
        nonlocal wrapped
        file_obj = real_open(path, *args, **kwargs)
        if path == str(history_path) and not wrapped:
            wrapped = True
            return wrapper_type(file_obj)
        return file_obj

    monkeypatch.setattr(combat_env, "open", failing_open, raising=False)

    with pytest.warns(RuntimeWarning):
        env._emit_run_outcome(_map_state(hp=0), victory=False, status="dead")

    assert history_path.read_bytes() == before
    assert env._run_outcome_emitted is False

    env._emit_run_outcome(_map_state(hp=0), victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == [
        "run_start", "map_snapshot", "milestone", "card_pick", "outcome",
    ]


def test_terminal_batch_close_after_committed_flush_does_not_retry(
    monkeypatch, tmp_path
):
    env, history_path = _terminal_batch_env(monkeypatch, tmp_path)
    real_open = open
    wrapped = False

    def close_failing_open(path, *args, **kwargs):
        nonlocal wrapped
        file_obj = real_open(path, *args, **kwargs)
        if path == str(history_path) and not wrapped:
            wrapped = True
            return _CloseAfterFlushFile(file_obj)
        return file_obj

    monkeypatch.setattr(combat_env, "open", close_failing_open, raising=False)

    with pytest.warns(RuntimeWarning, match="close after committed flush"):
        env._emit_run_outcome(_map_state(hp=0), victory=False, status="dead")
    env._emit_run_outcome(_map_state(hp=0), victory=False, status="dead")

    assert env._run_outcome_emitted is True
    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == [
        "run_start", "map_snapshot", "milestone", "card_pick", "outcome",
    ]


def test_run_logging_rejects_nested_nan_without_marking_emitted(monkeypatch, tmp_path):
    env, history_path = _recording_env(
        monkeypatch,
        tmp_path,
        run_context={"scenario": {"temperature": float("nan")}},
    )

    with pytest.warns(RuntimeWarning, match="Out of range float"):
        env._emit_run_start()

    assert env._run_start_emitted is False
    assert env._run_outcome_emitted is False
    assert len(env._run_logging_errors) == 1

    with pytest.warns(RuntimeWarning, match="Out of range float"):
        env._emit_run_outcome({}, victory=False, status="invalid")

    assert env._run_start_emitted is False
    assert env._run_outcome_emitted is False
    assert len(env._run_logging_errors) == 2
    assert not history_path.exists()


def test_initial_auto_advance_transport_failure_is_crash(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)
    monkeypatch.setattr(combat_env, "greedy_action", lambda state: {"cmd": "action"})
    replies = iter([
        {"decision": "map_select", "context": {"act": 1, "floor": 7}},
        None,
    ])
    monkeypatch.setattr(env, "_send", lambda command: next(replies))

    _, info = env.reset()

    assert info["crashed"] is True
    assert info["game_over"] is False
    assert _read_history_rows(history_path)[-1]["status"] == "crash"
    from agent.eval_rl import classify_eval_result, summarize_eval_results
    status = classify_eval_result(timed_out=False, run_won=False, info=info)
    stats = summarize_eval_results(
        [{"status": status, "floor": 7, "combat_wins": 0}],
        requested_n=1,
        total_attempts=1,
    )
    assert status == "crash"
    assert stats["valid_n"] == 0


def test_between_combat_auto_advance_transport_failure_is_crash(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._game_alive = True
    env._current_state = {
        "decision": "card_reward",
        "context": {"act": 1, "floor": 7},
        "player": {"hp": 40, "max_hp": 80},
        "cards": [],
    }
    monkeypatch.setattr(combat_env, "greedy_action", lambda state: {"cmd": "action"})
    monkeypatch.setattr(env, "_send", lambda command: None)
    monkeypatch.setattr(env, "_kill_proc", lambda: None)

    _, info = env.reset()

    assert info["crashed"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "crash"


def test_auto_advance_iteration_exhaustion_is_stuck(monkeypatch):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    state = {"decision": "map_select", "context": {"act": 1, "floor": 7}}
    monkeypatch.setattr(combat_env, "greedy_action", lambda current: {"cmd": "action"})
    monkeypatch.setattr(env, "_send", lambda command: state)

    result = env._advance_to_combat(state)

    assert result["decision"] == "stuck"


@pytest.mark.parametrize(
    ("act", "floor", "expected_global"),
    [(1, 4, 4), (2, 4, 21)],
)
def test_run_outcome_uses_absolute_floor(monkeypatch, tmp_path, act, floor, expected_global):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    state = combat_env._dummy_combat_state()
    state["floor"] = None
    state["context"] = {"act": act, "floor": floor, "room_type": "Monster"}

    env._init_combat_tracking(state)
    env._emit_run_outcome(state, victory=False, status="dead")

    assert env._current_floor == floor
    assert _read_history_rows(history_path)[-1]["max_floor"] == expected_global


def test_reset_uses_updated_state_after_hp_override(monkeypatch):
    load_state = {
        "decision": "combat_play",
        "context": {"floor": 17, "room_type": "Boss"},
        "player": {"hp": 80, "max_hp": 80},
        "enemies": [{"hp": 100, "max_hp": 100}],
    }
    hp_state = {
        **load_state,
        "player": {"hp": 72, "max_hp": 80},
    }
    calls = []

    env = CombatEnv(cards_json=CARDS_JSON, native_save_path="boss.save", set_hp_after_load=72)
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)

    def fake_send(cmd):
        calls.append(cmd)
        if cmd["cmd"] == "load_save":
            return load_state
        if cmd["cmd"] == "set_player":
            return hp_state
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(env, "_send", fake_send)
    monkeypatch.setattr(env, "_advance_to_combat", lambda state: state)

    env.reset()

    assert calls == [
        {"cmd": "load_save", "path": "boss.save", "lang": "en"},
        {"cmd": "set_player", "hp": 72},
    ]
    assert env._current_state["player"]["hp"] == 72
    assert env._combat_entry_hp_ratio == pytest.approx(0.9)


def test_greedy_use_potions_refreshes_indices_after_use(monkeypatch):
    vulnerable_0 = {
        "index": 0,
        "name": {"en": "Vulnerable Potion"},
        "description": {"en": "Apply Vulnerable."},
        "target_type": "AnyEnemy",
    }
    blood_1 = {
        "index": 1,
        "name": {"en": "Blood Potion"},
        "description": {"en": "Heal."},
        "target_type": "AnyPlayer",
    }
    vulnerable_2 = {
        "index": 2,
        "name": {"en": "Vulnerable Potion"},
        "description": {"en": "Apply Vulnerable."},
        "target_type": "AnyEnemy",
    }

    def state_with(potions):
        return {
            "decision": "combat_play",
            "context": {"floor": 8, "room_type": "Elite"},
            "player": {"hp": 87, "max_hp": 87, "block": 0, "potions": potions},
            "enemies": [
                {
                    "name": "Bygone Effigy",
                    "hp": 127,
                    "max_hp": 127,
                    "intents": [{"type": "Sleep"}],
                }
            ],
        }

    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_floor = 8
    used_indices = []
    captured = []
    updated = []

    def fake_send(cmd):
        idx = cmd["args"]["potion_index"]
        used_indices.append(idx)
        if idx == 0:
            return state_with([
                {**blood_1, "index": 0},
                {**vulnerable_2, "index": 1},
            ])
        if idx == 1:
            return state_with([{**blood_1, "index": 0}])
        return {"type": "error", "message": f"Invalid potion index {idx}"}

    monkeypatch.setattr(env, "_send", fake_send)
    monkeypatch.setattr(env, "_capture_run_map_state", lambda state: captured.append(state))
    monkeypatch.setattr(env, "_update_buffered_node_inventory", lambda state: updated.append(state))

    result = env._greedy_use_potions(state_with([vulnerable_0, blood_1, vulnerable_2]))

    assert used_indices == [0, 1]
    assert captured == []
    assert len(updated) == 2
    assert result["decision"] == "combat_play"
    assert result.get("type") != "error"


def _vantom_slippery_mask_state():
    return {
        "decision": "combat_play",
        "energy": 3,
        "player": {"hp": 80, "max_hp": 80, "block": 0},
        "hand": [
            {
                "index": 0,
                "id": "CARD.CINDER",
                "cost": 2,
                "type": "Attack",
                "can_play": True,
                "target_type": "AnyEnemy",
                "stats": {"damage": 17},
                "description": "Deal 17 damage.",
            },
            {
                "index": 1,
                "id": "CARD.STRIKE_IRONCLAD",
                "cost": 1,
                "type": "Attack",
                "can_play": True,
                "target_type": "AnyEnemy",
                "stats": {"damage": 6},
                "description": "Deal 6 damage.",
            },
            {
                "index": 2,
                "id": "CARD.DEFEND_IRONCLAD",
                "cost": 1,
                "type": "Skill",
                "can_play": True,
                "target_type": "Self",
                "stats": {"block": 5},
                "description": "Gain 5 Block.",
            },
        ],
        "enemies": [
            {
                "name": "Vantom",
                "hp": 173,
                "max_hp": 173,
                "intents": [{"type": "Attack", "damage": 7}],
                "powers": [{"name": "Slippery", "amount": 9}],
            }
        ],
    }


def test_action_masks_leave_vantom_slippery_filter_off_by_default(monkeypatch):
    monkeypatch.delenv("STS2_VANTOM_SLIPPERY_MASK", raising=False)
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _vantom_slippery_mask_state()

    masks = env.action_masks()

    assert masks[0]
    assert masks[4]
    assert masks[11]
    assert masks[40]


def test_action_masks_apply_vantom_slippery_strip_filter(monkeypatch):
    monkeypatch.setenv("STS2_VANTOM_SLIPPERY_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _vantom_slippery_mask_state()

    masks = env.action_masks()

    assert not masks[0]
    assert masks[4]
    assert masks[11]
    assert masks[40]


def _boss_planner_mask_state():
    strike = {
        "index": 0,
        "id": "CARD.STRIKE_IRONCLAD",
        "cost": 1,
        "type": "Attack",
        "can_play": True,
        "target_type": "AnyEnemy",
        "stats": {"damage": 6},
        "description": "Deal 6 damage.",
    }
    defend = {
        "index": 1,
        "id": "CARD.DEFEND_IRONCLAD",
        "cost": 1,
        "type": "Skill",
        "can_play": True,
        "target_type": "Self",
        "stats": {"block": 5},
        "description": "Gain 5 Block.",
    }
    return {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "round": 1,
        "context": {"floor": 17, "room_type": "Boss"},
        "player": {
            "hp": 30,
            "max_hp": 80,
            "block": 0,
            "deck": [strike, defend],
            "relics": [],
        },
        "player_powers": None,
        "hand": [strike, defend],
        "enemies": [
            {
                "name": "Boss",
                "hp": 6,
                "max_hp": 50,
                "block": 0,
                "intents": [{"type": "attack", "damage": 12, "hits": 1}],
                "powers": None,
            }
        ],
    }


def test_action_masks_can_force_boss_planner_choice(monkeypatch):
    monkeypatch.setenv("STS2_BOSS_PLANNER_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _boss_planner_mask_state()

    masks = env.action_masks()

    assert masks.sum() == 1
    assert masks[0]


def test_action_masks_can_force_elite_planner_choice(monkeypatch):
    monkeypatch.setenv("STS2_BOSS_PLANNER_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    state = _boss_planner_mask_state()
    state["context"]["room_type"] = "Elite"
    env._current_state = state

    masks = env.action_masks()

    assert masks.sum() == 1
    assert masks[0]


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
        "player": {"deck": []},
        "cards": [{
            "index": 0,
            "id": "CARD.BLUDGEON",
            "cost": 3,
            "type": "Attack",
            "rarity": "Rare",
            "stats": {"damage": 32},
            "description": "Deal 32 damage.",
        }],
    }
    action = greedy_action(state)
    assert action["action"] == "select_card_reward"


def _late_card_reward_state():
    return {
        "decision": "card_reward",
        "act": 1,
        "floor": 12,
        "player": {
            "deck": [{"id": f"CARD.DECK_{i}"} for i in range(15)],
            "deck_size": 15,
        },
        "cards": [
            {"id": "CARD.OLD_TOP", "index": 0},
            {"id": "CARD.ELIGIBLE", "index": 1},
        ],
    }


def test_card_quality_gate_selects_eligible_card_at_original_index(monkeypatch):
    state = _late_card_reward_state()
    seen = {}
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: card["id"] == "CARD.ELIGIBLE",
    )

    def fake_pick(cards, *, threshold, deck):
        seen["ids"] = [card["id"] for card in cards]
        return 0

    monkeypatch.setattr(combat_env, "pick_best_card", fake_pick)
    action = greedy_action(state)
    assert seen["ids"] == ["CARD.ELIGIBLE"]
    assert action["args"]["card_index"] == 1


def test_card_quality_gate_reads_act_from_runtime_context(monkeypatch):
    state = _late_card_reward_state()
    state.pop("act")
    state["context"] = {"act": 1}
    seen = {}
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")

    def fake_eligibility(card, deck, act):
        seen["act"] = act
        return card["id"] == "CARD.ELIGIBLE"

    monkeypatch.setattr(
        combat_env, "is_act1_card_reward_eligible", fake_eligibility
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert seen["act"] == 1
    assert action["args"]["card_index"] == 1


def test_card_quality_gate_bypasses_early_inflated_deck(monkeypatch):
    state = _late_card_reward_state()
    state["floor"] = 7
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("early Act 1 rewards should remain unfiltered")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert action["args"]["card_index"] == 0


def test_card_quality_gate_skips_when_every_offer_is_ineligible(monkeypatch):
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: False,
    )
    monkeypatch.setattr(
        combat_env,
        "pick_best_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty eligible set should skip before ranking")
        ),
    )
    assert greedy_action(_late_card_reward_state()) == {
        "cmd": "action",
        "action": "skip_card_reward",
    }


def test_card_quality_gate_falls_back_for_forced_reward(monkeypatch):
    state = _late_card_reward_state()
    state["can_skip"] = False
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: False,
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert action["action"] == "select_card_reward"
    assert action["args"]["card_index"] == 0


def test_card_quality_gate_is_enabled_by_default_after_promotion(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.delenv("STS2_CARD_QUALITY_GATE", raising=False)
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: False,
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["action"] == "skip_card_reward"


@pytest.mark.parametrize("flag", ["", "garbage"])
def test_card_quality_gate_requires_recognized_opt_in(monkeypatch, flag):
    state = _late_card_reward_state()
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", flag)
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("unknown flag value must not enable the gate")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 0


def test_card_quality_gate_zero_restores_unfiltered_selection(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "0")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("disabled gate should not inspect offers")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 0


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


def _rest_options():
    return [
        {"index": 0, "option_id": "SMITH", "is_enabled": True},
        {"index": 1, "option_id": "HEAL", "is_enabled": True},
    ]


def _must_smith_deck():
    return [
        {
            "id": "CARD.INFLAME",
            "type": "Power",
            "rarity": "Uncommon",
            "cost": 1,
            "description": "Gain 2 Strength.",
        }
    ]


def test_vantom_mid_act_rest_keeps_must_smith_override_by_default(monkeypatch):
    monkeypatch.delenv("STS2_VANTOM_REST_HEAL", raising=False)
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "VANTOM_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 0


def test_vantom_mid_act_rest_heals_below_seventy_when_enabled(monkeypatch):
    monkeypatch.setenv("STS2_VANTOM_REST_HEAL", "1")
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "VANTOM_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 1


def test_non_vantom_mid_act_rest_keeps_must_smith_override():
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "CEREMONIAL_BEAST_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 0
