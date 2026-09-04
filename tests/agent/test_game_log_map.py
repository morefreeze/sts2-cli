"""Replays must carry map snapshots, or the workbench renders no acts.

`_serialized_run_map_snapshots()` is only consumed when `DECK_HISTORY_PATH` is
set (combat_env.py, the deck-history branch), so a `--game-log` replay never
contains a `map_snapshot` row. The workbench keys its map view on
`event == "map_snapshot"` (run_workbench/adapters.py, catalog.py), so loading a
replay via 「载入单个记录」 shows "缺失 · 完整地图分支" for every act.
"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))
from game_log import GameLogger


def test_logger_writes_map_snapshot_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lg = GameLogger("Defect", "seed1", enabled=True,
                    run_context={"run_id": "r1", "character": "Defect"})
    try:
        lg.log_map_snapshot({
            "event": "map_snapshot",
            "act": 2,
            "map": {"rows": [], "boss": {"col": 0, "row": 1, "type": "Boss"}},
            "visited_nodes": [],
        })
        path = lg.path
    finally:
        lg.close()

    rows = [json.loads(l) for l in open(path, encoding="utf-8")]
    snaps = [r for r in rows if r.get("type") == "map_snapshot"]
    assert len(snaps) == 1
    assert snaps[0]["data"]["act"] == 2


def test_map_snapshot_is_a_noop_when_logging_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lg = GameLogger("Defect", "seed2", enabled=False)

    lg.log_map_snapshot({"event": "map_snapshot", "act": 1})  # must not raise


def test_combat_env_flushes_map_snapshots_into_the_replay():
    """The env must hand its captured snapshots to the replay logger.

    Without this the rows exist in memory but only ever reach deck-history,
    which is why every replay in logs/ had zero map rows.
    """
    from agent.combat_env import CombatEnv

    env = CombatEnv(character="Defect", dry_run=True)
    written = []

    class _Logger:
        enabled = True

        def log_map_snapshot(self, snap):
            written.append(snap)

    env._game_logger = _Logger()
    env._run_map_snapshots = {
        1: {"map": {"rows": []}, "visited_nodes": [], "ts": 1.0},
        2: {"map": {"rows": []}, "visited_nodes": [], "ts": 2.0},
    }

    env._flush_map_snapshots_to_game_log()

    assert [w["act"] for w in written] == [1, 2]
    assert all(w["event"] == "map_snapshot" for w in written)


def test_close_flushes_and_closes_the_game_log():
    """env.close() only killed the process, so the FINAL run's replay was never
    closed and its map snapshots never flushed — which is why single-game evals
    produced replays with zero map rows even when capture had succeeded."""
    from agent.combat_env import CombatEnv

    env = CombatEnv(character="Defect", dry_run=True)
    closed = []

    class _Logger:
        enabled = True

        def log_map_snapshot(self, snap):
            closed.append(("snap", snap["act"]))

        def close(self):
            closed.append(("closed", None))

    env._game_logger = _Logger()
    env._run_map_snapshots = {3: {"map": {}, "visited_nodes": [], "ts": 1.0}}

    env.close()

    assert ("snap", 3) in closed
    assert ("closed", None) in closed
