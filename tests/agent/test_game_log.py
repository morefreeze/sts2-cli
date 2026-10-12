"""Tests for python/game_log.py's optional "run_meta" header line.

python/ is not a regular package (no __init__.py), so it's imported the
same way agent/combat_env.py does at runtime: insert python/ onto
sys.path and import "game_log" as a top-level module.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

_PYTHON_DIR = Path(__file__).resolve().parents[2] / "python"
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

import game_log  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_log_dir(tmp_path, monkeypatch):
    """Redirect GameLogger's module-level LOG_DIR so tests never touch
    the repo's real logs/ directory."""
    monkeypatch.setattr(game_log, "LOG_DIR", str(tmp_path))
    yield tmp_path


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_header_written_with_run_context():
    run_context = {
        "run_id": "run-1",
        "batch_id": "batch-1",
        "experiment": "exp-a",
        "checkpoint": "model.zip",
        "ascension": 5,
        "game_version": "v0.111.0",
        "game_version_source": "cli",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "is_multiplayer": False,
        "capture_map": True,  # must never leak into the header
    }
    logger = game_log.GameLogger(
        "Ironclad", "seed-1", enabled=True, run_context=run_context
    )
    logger.log_state({"decision": "combat_play"})
    logger.close()

    lines = _read_lines(Path(logger.path))
    assert len(lines) == 2

    header = lines[0]
    assert header["type"] == "run_meta"
    assert "step" not in header
    assert header["run_id"] == "run-1"
    assert header["batch_id"] == "batch-1"
    assert header["experiment"] == "exp-a"
    assert header["checkpoint"] == "model.zip"
    assert header["ascension"] == 5
    assert header["game_version"] == "v0.111.0"
    assert header["game_version_source"] == "cli"
    assert header["evaluation_mode"] == "fixed"
    assert header["scenario"] == "full_run"
    assert header["is_multiplayer"] is False  # False is a value, not "omit me"
    assert header["character"] == "Ironclad"
    assert header["seed"] == "seed-1"
    assert "capture_map" not in header
    assert None not in header.values()

    state_line = lines[1]
    assert state_line["type"] == "state"
    assert state_line["step"] == 1


@pytest.mark.parametrize("run_context", [None, {}])
def test_no_header_without_run_context(run_context):
    logger = game_log.GameLogger(
        "Silent", "seed-2", enabled=True, run_context=run_context
    )
    logger.log_state({"decision": "combat_play"})
    logger.log_action({"cmd": "end_turn"})
    logger.close()

    lines = _read_lines(Path(logger.path))
    assert len(lines) == 2
    assert [line["type"] for line in lines] == ["state", "action"]


def test_missing_run_context_keys_are_omitted_not_null():
    logger = game_log.GameLogger(
        "Ironclad", "seed-6", enabled=True, run_context={"run_id": "run-6"}
    )
    logger.close()

    header = _read_lines(Path(logger.path))[0]
    for absent_key in (
        "batch_id",
        "experiment",
        "checkpoint",
        "game_version",
        "game_version_source",
        "evaluation_mode",
        "scenario",
        "ascension",
        "is_multiplayer",
    ):
        assert absent_key not in header
    assert None not in header.values()


def test_step_numbering_matches_no_context_baseline():
    # Different seeds so the two loggers never collide on the same
    # second-granularity log filename.
    plain = game_log.GameLogger(
        "Defect", "seed-3-plain", enabled=True, run_context=None
    )
    with_meta = game_log.GameLogger(
        "Defect", "seed-3-meta", enabled=True,
        run_context={"run_id": "run-3", "checkpoint": "model.zip"},
    )
    for logger in (plain, with_meta):
        logger.log_state({"decision": "combat_play"})
        logger.log_action({"cmd": "end_turn"})
        logger.log_state({"decision": "combat_play"})
    plain.close()
    with_meta.close()

    plain_lines = _read_lines(Path(plain.path))
    meta_lines = [
        line for line in _read_lines(Path(with_meta.path)) if line["type"] != "run_meta"
    ]
    assert [line["type"] for line in plain_lines] == [line["type"] for line in meta_lines]
    assert [line["step"] for line in plain_lines] == [line["step"] for line in meta_lines]
    assert [line["step"] for line in plain_lines] == [1, 1, 2]


def test_character_and_seed_fall_back_to_constructor_args():
    logger = game_log.GameLogger(
        "Regent", "seed-4", enabled=True, run_context={"checkpoint": "model.zip"}
    )
    logger.close()

    header = _read_lines(Path(logger.path))[0]
    assert header["character"] == "Regent"
    assert header["seed"] == "seed-4"


def test_run_context_character_and_seed_take_priority_over_constructor_args():
    logger = game_log.GameLogger(
        "Regent", "ctor-seed", enabled=True,
        run_context={"character": "Necrobinder", "seed": "context-seed"},
    )
    logger.close()

    header = _read_lines(Path(logger.path))[0]
    assert header["character"] == "Necrobinder"
    assert header["seed"] == "context-seed"


def test_ts_is_a_numeric_epoch_not_an_iso_string():
    before = time.time()
    logger = game_log.GameLogger(
        "Ironclad", "seed-5", enabled=True, run_context={"run_id": "run-5"}
    )
    logger.close()
    after = time.time()

    header = _read_lines(Path(logger.path))[0]
    assert isinstance(header["ts"], float)
    assert before <= header["ts"] <= after

    # The other log lines keep their existing ISO string ts -- that
    # inconsistency with the header is intentional and pre-existing.
    logger2 = game_log.GameLogger(
        "Ironclad", "seed-5b", enabled=True, run_context=None
    )
    logger2.log_state({"decision": "combat_play"})
    logger2.close()
    state_line = _read_lines(Path(logger2.path))[0]
    assert isinstance(state_line["ts"], str)


def test_header_write_failure_never_breaks_construction(monkeypatch):
    class _ExplodingDict(dict):
        def get(self, *args, **kwargs):
            raise RuntimeError("boom")

    # Must not raise -- logging is defensive and must never break a run.
    logger = game_log.GameLogger(
        "Ironclad", "seed-7", enabled=True,
        run_context=_ExplodingDict(run_id="run-7"),
    )
    logger.log_state({"decision": "combat_play"})
    logger.close()

    lines = _read_lines(Path(logger.path))
    assert lines[0]["type"] == "state"
    assert lines[0]["step"] == 1


def test_header_carries_the_harness_and_batch_lane_keys():
    run_context = {
        "run_id": "pfr-1",
        "harness": "play_full_run",
        "batch_id": "regression-20261012-101500",
        "lane": "Silent",
        "game_index": 2,
        "games_total": 5,
    }
    logger = game_log.GameLogger("Silent", "seed-9", enabled=True, run_context=run_context)
    logger.close()

    header = _read_lines(Path(logger.path))[0]
    assert header["harness"] == "play_full_run"
    assert header["batch_id"] == "regression-20261012-101500"
    assert header["lane"] == "Silent"
    assert header["game_index"] == 2 and header["games_total"] == 5


def test_game_index_zero_is_a_value_not_omitted():
    # `is None` filtering, not truthiness: a falsy index must survive.
    logger = game_log.GameLogger("Silent", "seed-10", enabled=True,
                                 run_context={"run_id": "r", "game_index": 0})
    logger.close()
    assert _read_lines(Path(logger.path))[0]["game_index"] == 0


def test_cleanup_removes_old_top_level_logs_but_never_touches_logs_batches(_isolated_log_dir):
    """logs/batches/** holds the regression launcher's manifests, lane logs and
    kept game copies; the 7-day purge of logs/*.jsonl must not eat them."""
    import os

    old = time.time() - 30 * 86400
    stale = _isolated_log_dir / "20260901_000000_Ironclad_x.jsonl"
    stale.write_text("{}\n")
    fresh = _isolated_log_dir / "20261012_000000_Ironclad_y.jsonl"
    fresh.write_text("{}\n")

    batch = _isolated_log_dir / "batches" / "regression-20260901-000000"
    kept = batch / "games" / "Ironclad_greedy_a0_run_1.jsonl"
    kept.parent.mkdir(parents=True)
    kept.write_text("{}\n")
    results = batch / "Ironclad.jsonl"
    results.write_text("{}\n")
    manifest = batch / "manifest.json"
    manifest.write_text("{}")
    # a stray directory whose name ends in .jsonl must not crash the purge either
    weird = _isolated_log_dir / "weird.jsonl"
    weird.mkdir()

    for path in (stale, kept, results, manifest, weird):
        os.utime(path, (old, old))
    os.utime(batch, (old, old))

    game_log.cleanup_old_logs()

    assert not stale.exists()
    assert fresh.exists()
    assert kept.exists() and results.exists() and manifest.exists()
