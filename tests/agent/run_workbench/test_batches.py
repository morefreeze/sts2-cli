"""Regression/eval batches: classification, progress, ETA, and the endpoints.

A play_full_run game is a *regression run*.  It is never part of a training
cohort; it shows up in the 回归/评测 tab, grouped into the batch
``python/run_batch.py`` registered it under.  Everything here builds its own
manifests and small game logs under tmp dirs -- the live logs/ is never read.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import agent.run_progress_viewer as viewer
import agent.run_workbench.catalog as catalog_module
from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench.batches import (
    ABANDONED,
    FINISHED,
    LIVE,
    RegressionRun,
    batch_progress,
    build_batch_views,
    is_regression_run,
    lane_progress,
    load_manifests,
    median_duration,
    policy_from_experiment,
)
from agent.run_workbench.catalog import RunCatalog
from agent.run_workbench.models import (
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)
from agent.run_workbench.replay import parse_game_progress

NOW = 1_800_000_000.0
HOUR = 3600.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta, expected",
    [
        ({"harness": "play_full_run"}, True),
        ({"harness": "play_full_run", "checkpoint": "ckpt"}, True),
        ({"experiment": "ooc=greedy"}, True),
        ({"experiment": "run:ooc=greedy"}, True),
        ({"experiment": "p3b:ooc=naive", "checkpoint": None}, True),
        # Phase 3b's value route arm: `,route=value` is appended to the label. The legacy
        # pattern is a prefix match, and run_batch games also carry the harness.
        ({"experiment": "p3b:ooc=greedy,route=value"}, True),
        ({"experiment": "ooc=greedy,route=value"}, True),
        ({"experiment": "p3b:ooc=greedy,route=value", "harness": "play_full_run",
          "batch_id": "b1", "lane": "Ironclad/value"}, True),
        ({"experiment": "p3b:ooc=greedy,route=value", "checkpoint": "ckpt_1"}, False),
        # a training run: a checkpoint, however its experiment is spelled
        ({"experiment": "run:ooc=greedy", "checkpoint": "ckpt_1"}, False),
        ({"experiment": "train_a", "checkpoint": "ckpt_1"}, False),
        ({"experiment": "train_a"}, False),
        ({"experiment": "x:y:ooc=greedy"}, False),
        ({"harness": "eval_rl"}, False),
        ({"character": "Ironclad"}, False),
        ({}, False),
        (None, False),
        ("play_full_run", False),
    ],
)
def test_is_regression_run(meta, expected) -> None:
    assert is_regression_run(meta) is expected


def test_policy_comes_from_the_experiment_label() -> None:
    assert policy_from_experiment("regression:ooc=greedy") == "greedy"
    assert policy_from_experiment("ooc=naive") == "naive"
    assert policy_from_experiment("train_a") is None
    # the value route arm's label keeps the OOC policy clean (no ",route=value" tail)
    assert policy_from_experiment("p3b:ooc=greedy,route=value") == "greedy"
    assert policy_from_experiment("ooc=naive,route=value") == "naive"
    assert policy_from_experiment(None) is None


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def _write_manifest(root: Path, batch_id: str, **overrides) -> Path:
    directory = root / "batches" / batch_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "batch_id": batch_id,
        "name": "regression",
        "kind": "regression",
        "created_at": NOW - 2 * HOUR,
        "git_commit": "abc1234",
        "argv": ["python/run_batch.py", "--name", "regression"],
        "env": {"STS2_SOLVER_THREADS": "3"},
        "lanes": [
            {"lane": "Ironclad", "character": "Ironclad", "planned_games": 5,
             "seed_prefix": "run_", "seed_offset": 0, "extra_args": []},
            {"lane": "Silent", "character": "Silent", "planned_games": 5,
             "seed_prefix": "run_", "seed_offset": 0, "extra_args": []},
        ],
    }
    manifest.update(overrides)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_load_manifests_reads_manifest_and_lane_status(tmp_path: Path) -> None:
    directory = _write_manifest(tmp_path, "regression-20261012-100000")
    (directory / "lanes_status.json").write_text(
        json.dumps(
            {
                "Ironclad": {"state": "running", "pid": 123, "started_at": NOW - 100,
                             "ended_at": None, "returncode": None},
                "Silent": {"state": "queued", "pid": None},
            }
        ),
        encoding="utf-8",
    )
    problems: list[str] = []

    (manifest,) = load_manifests(tmp_path, problems)

    assert problems == []
    assert manifest["batch_id"] == "regression-20261012-100000"
    assert manifest["kind"] == "regression"
    assert manifest["registered"] is True
    assert [lane["lane"] for lane in manifest["lanes"]] == ["Ironclad", "Silent"]
    assert manifest["lanes"][0]["planned_games"] == 5
    assert manifest["lanes_status"]["Ironclad"]["pid"] == 123
    assert manifest["env"] == {"STS2_SOLVER_THREADS": "3"}


def test_load_manifests_without_lane_status_still_loads(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "b1")
    (manifest,) = load_manifests(tmp_path)
    assert manifest["lanes_status"] == {}


def test_load_manifests_skips_and_reports_malformed_files(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "good")
    bad = tmp_path / "batches" / "bad-json"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    not_object = tmp_path / "batches" / "bad-shape"
    not_object.mkdir()
    (not_object / "manifest.json").write_text("[1, 2]", encoding="utf-8")
    no_manifest = tmp_path / "batches" / "empty-dir"
    no_manifest.mkdir()
    broken_status = _write_manifest(tmp_path, "good-status-broken")
    (broken_status / "lanes_status.json").write_text("nope", encoding="utf-8")
    problems: list[str] = []

    manifests = load_manifests(tmp_path, problems)

    assert [m["batch_id"] for m in manifests] == ["good", "good-status-broken"]
    assert manifests[1]["lanes_status"] == {}
    assert len(problems) == 3
    assert any("bad-json" in item for item in problems)
    assert any("bad-shape" in item for item in problems)
    assert any("lanes_status.json" in item for item in problems)


def test_load_manifests_normalizes_hostile_field_types(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "odd",
        name=7,
        kind=["x"],
        created_at="yesterday",
        lanes=[
            {"lane": "ok", "planned_games": "five"},
            {"lane": "", "planned_games": 3},
            "nonsense",
            {"lane": "fine", "character": "Silent", "planned_games": 4},
        ],
        argv="not a list",
        env=[1],
    )
    (manifest,) = load_manifests(tmp_path)
    assert manifest["name"] == "odd"
    assert manifest["kind"] is None
    assert manifest["created_at"] is None
    assert [(lane["lane"], lane["planned_games"]) for lane in manifest["lanes"]] == [
        ("ok", None),
        ("fine", 4),
    ]
    assert manifest["argv"] == [] and manifest["env"] == {}


def test_load_manifests_skips_dry_runs(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "real")
    _write_manifest(tmp_path, "dry", dry_run=True)
    assert [m["batch_id"] for m in load_manifests(tmp_path)] == ["real"]
    assert [m["batch_id"] for m in load_manifests(tmp_path, include_dry_run=True)] == [
        "dry",
        "real",
    ]


def test_load_manifests_without_a_batches_directory_is_empty(tmp_path: Path) -> None:
    assert load_manifests(tmp_path) == []
    assert load_manifests(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# Progress and ETA (pure)
# ---------------------------------------------------------------------------


def _record(status: RunStatus, floor: int | None) -> RunRecord:
    return RunRecord(
        run_id="r",
        source_id="s",
        source_kind=SourceKind.REPLAY_JSONL,
        metadata=RunMetadata(),
        outcome=RunOutcome(status=status, max_global_floor=floor),
    )


def _run(
    state: str = FINISHED,
    *,
    status: RunStatus = RunStatus.DEAD,
    floor: int | None = 10,
    duration: float | None = None,
    lane: str = "Ironclad",
    updated_at: float | None = NOW - 60,
    started_at: float | None = None,
    batch_id: str | None = "b1",
    experiment: str | None = "regression:ooc=greedy",
    n: list[int] = [0],
    **extra,
) -> RegressionRun:
    n[0] += 1
    finished = state == FINISHED
    return RegressionRun(
        run_id=f"pfr-{n[0]}",
        source_id=f"src-{n[0]}",
        source_ids=(f"src-{n[0]}",),
        state=state,
        status=status if finished else RunStatus.IN_PROGRESS,
        seed=f"run_{n[0]}",
        character=extra.pop("character", lane),
        experiment=experiment,
        batch_id=batch_id,
        lane=lane,
        global_floor=floor,
        started_at=started_at,
        updated_at=updated_at,
        duration_seconds=duration,
        record=_record(status, floor) if finished else None,
        **extra,
    )


def _lane(
    finished: list[RegressionRun],
    *,
    live: list[RegressionRun] = (),
    planned: int | None = 5,
    status: dict | None = None,
    fallback: float | None = None,
    abandoned: list[RegressionRun] = (),
    now: float = NOW,
    lane: str = "Ironclad",
) -> dict:
    return lane_progress(
        {"lane": lane, "character": lane, "planned_games": planned},
        finished,
        list(live),
        status,
        now,
        abandoned_runs=list(abandoned),
        fallback_median=fallback,
        reference_at=NOW - 60,
    )


def test_median_duration() -> None:
    assert median_duration([]) is None
    assert median_duration([10.0]) == 10.0
    assert median_duration([10.0, 30.0, 20.0]) == 20.0
    assert median_duration([10.0, 20.0]) == 15.0


def test_lane_eta_is_remaining_games_times_the_lanes_median_duration() -> None:
    finished = [_run(duration=d) for d in (600.0, 1200.0, 900.0)]
    live = [_run(LIVE)]

    lane = _lane(finished, live=live, planned=5, status={"state": "running"})

    assert (lane["planned"], lane["finished"], lane["live"], lane["remaining"]) == (5, 3, 1, 2)
    assert lane["median_duration_seconds"] == 900.0
    assert lane["eta_seconds"] == 2 * 900.0
    assert lane["eta_at"] == NOW + 1800.0
    assert lane["eta_basis"] == "lane"
    assert lane["state"] == "running"


def test_lane_eta_falls_back_to_the_batch_median_before_its_first_finish() -> None:
    lane = _lane([], planned=4, status={"state": "queued", "pid": None}, fallback=500.0)

    assert lane["median_duration_seconds"] is None
    assert lane["eta_seconds"] == 4 * 500.0
    assert lane["eta_basis"] == "batch"


def test_lane_eta_is_none_when_no_game_anywhere_has_finished() -> None:
    lane = _lane([], live=[_run(LIVE)], planned=3, status={"state": "running"})

    assert lane["eta_seconds"] is None
    assert lane["eta_at"] is None
    assert lane["eta_basis"] is None
    assert lane["remaining"] == 3


def test_games_without_a_duration_do_not_count_toward_the_median() -> None:
    finished = [_run(duration=None), _run(duration=100.0), _run(duration=300.0)]
    assert _lane(finished, status={"state": "running"})["median_duration_seconds"] == 200.0


def test_lane_statistics_count_only_finished_games() -> None:
    finished = [
        _run(status=RunStatus.WIN, floor=51, duration=1.0),
        _run(status=RunStatus.DEAD, floor=20, duration=1.0),
        _run(status=RunStatus.DEAD, floor=None, duration=1.0),
        _run(status=RunStatus.STUCK, floor=7, duration=1.0),
    ]
    # a live game at floor 40 must not move the average
    lane = _lane(finished, live=[_run(LIVE, floor=40)], status={"state": "running"})

    assert lane["finished"] == 4
    assert lane["wins"] == 1
    assert lane["technical"] == 1
    assert lane["valid_n"] == 3
    assert lane["win_rate"] == pytest.approx(1 / 3)
    # valid runs with a floor: 51 and 20 -- a missing floor is not a zero
    assert lane["avg_global_floor"] == pytest.approx(35.5)


def test_lane_states() -> None:
    done = _lane([_run(duration=1.0)] * 5, planned=5, status={"state": "running"})
    assert done["state"] == "done"  # all planned games are in; the status file lags

    assert _lane([_run(duration=1.0)], status={"state": "failed", "returncode": 1})["state"] == "failed"
    assert _lane([_run(duration=1.0)], status={"state": "done", "returncode": 0})["state"] == "done"

    # nothing recorded: queued lane that the launcher has not started yet
    queued = _lane([], status={"state": "queued"})
    assert queued["state"] == "queued"

    # "running" with no log touched for more than the window is stalled, no ETA
    quiet = NOW - 2 * HOUR
    stalled = _lane(
        [_run(duration=10.0, updated_at=quiet)],
        status={"state": "running", "started_at": quiet},
    )
    assert stalled["state"] == "stalled"
    assert stalled["eta_seconds"] is None

    # a live game keeps it running whatever the status file says about time
    assert _lane([], live=[_run(LIVE)], status={"state": "running", "started_at": quiet})["state"] == "running"


def test_lane_state_without_a_lane_status_file() -> None:
    # registered plan, no status file, a game is being played: running
    assert _lane([_run(duration=1.0)], live=[_run(LIVE)], status=None)["state"] == "running"
    # recent activity between two games: still running
    assert _lane([_run(duration=1.0, updated_at=NOW - 30)], status=None)["state"] == "running"
    # nothing for hours: stalled
    assert _lane([_run(duration=1.0, updated_at=NOW - 3 * HOUR)], status=None)["state"] == "stalled"
    # nothing at all
    assert _lane([], status=None)["state"] == "unknown"


def test_lane_without_a_plan_has_no_eta() -> None:
    lane = _lane([_run(duration=100.0)] * 2, live=[_run(LIVE)], planned=None)
    assert lane["planned_known"] is False
    assert lane["planned"] == 3  # finished + live
    assert lane["eta_seconds"] is None
    assert lane["state"] == "running"
    assert _lane([_run(duration=100.0)], planned=None)["state"] == "done"


def test_lane_policy_and_character_come_from_the_runs() -> None:
    lane = lane_progress(
        {"lane": "L", "planned_games": 2},
        [_run(experiment="b:ooc=greedy", character="Defect", duration=1.0)] * 2,
        [],
        None,
        NOW,
    )
    assert lane["policy"] == "greedy"
    assert lane["character"] == "Defect"


def test_batch_eta_is_the_latest_lane_eta_because_lanes_run_in_parallel() -> None:
    fast = _lane([_run(duration=100.0)] * 3, planned=5, status={"state": "running"}, lane="A")
    slow = _lane([_run(duration=1000.0)] * 3, planned=5, status={"state": "running"}, lane="B")
    finished_lane = _lane([_run(duration=10.0)] * 5, planned=5, status={"state": "done"}, lane="C")

    batch = batch_progress({"batch_id": "b1", "created_at": NOW - HOUR}, [fast, slow, finished_lane], NOW)

    assert fast["eta_seconds"] == 200.0 and slow["eta_seconds"] == 2000.0
    assert finished_lane["eta_seconds"] is None  # nothing left to play
    assert batch["eta_seconds"] == 2000.0
    assert batch["eta_at"] == NOW + 2000.0
    assert batch["status"] == "running"
    assert batch["done"] is False
    assert (batch["planned"], batch["finished"]) == (15, 11)


def test_batch_eta_is_unknown_while_a_running_lane_has_no_estimate() -> None:
    known = _lane([_run(duration=100.0)], planned=5, status={"state": "running"}, lane="A")
    unknown = _lane([], planned=5, status={"state": "queued"}, lane="B")
    batch = batch_progress({"batch_id": "b1"}, [known, unknown], NOW)
    assert batch["eta_seconds"] is None and batch["eta_at"] is None


def test_batch_status() -> None:
    def lane(state: str, **kwargs) -> dict:
        return _lane([_run(duration=1.0)], planned=5, status={"state": state, **kwargs})

    manifest = {"batch_id": "b"}
    assert batch_progress(manifest, [lane("running"), lane("done")], NOW)["status"] == "running"
    assert batch_progress(manifest, [lane("queued")], NOW)["status"] == "running"
    done = batch_progress(manifest, [lane("done"), lane("done")], NOW)
    assert (done["status"], done["done"]) == ("done", True)
    failed = batch_progress(manifest, [lane("done"), lane("failed")], NOW)
    assert (failed["status"], failed["done"], failed["failed_lanes"]) == ("failed", True, 1)
    stale = {"state": "running", "started_at": NOW - 3 * HOUR}
    stalled = batch_progress(
        manifest,
        [_lane([_run(duration=1.0, updated_at=NOW - 3 * HOUR)], planned=5, status=stale)],
        NOW,
    )
    assert (stalled["status"], stalled["done"]) == ("stalled", True)
    assert batch_progress(manifest, [], NOW)["status"] == "done"


# ---------------------------------------------------------------------------
# Grouping: registered batches, unregistered batch ids, legacy games
# ---------------------------------------------------------------------------


def test_runs_are_grouped_into_their_manifests_batch_and_lane() -> None:
    manifest = {
        "batch_id": "b1", "name": "regression", "kind": "regression", "created_at": NOW - HOUR,
        "lanes": [
            {"lane": "Ironclad", "character": "Ironclad", "planned_games": 3},
            {"lane": "Silent", "character": "Silent", "planned_games": 3},
        ],
        "lanes_status": {"Ironclad": {"state": "running"}, "Silent": {"state": "queued"}},
        "registered": True, "env": {}, "argv": [],
    }
    runs = [
        _run(duration=600.0, lane="Ironclad", batch_id="b1"),
        _run(duration=1200.0, lane="Ironclad", batch_id="b1"),
        _run(LIVE, lane="Ironclad", batch_id="b1"),
        _run(duration=300.0, lane="Silent", batch_id="b1"),
        _run(duration=50.0, lane="Ironclad", batch_id="other"),
    ]

    views = build_batch_views([manifest], runs, NOW)

    by_id = {view.summary["batch_id"]: view for view in views}
    view = by_id["b1"]
    lanes = {lane["lane"]: lane for lane in view.lanes}
    assert [lane["lane"] for lane in view.lanes] == ["Ironclad", "Silent"]
    assert (lanes["Ironclad"]["finished"], lanes["Ironclad"]["live"]) == (2, 1)
    assert lanes["Ironclad"]["eta_seconds"] == 1 * 900.0
    assert lanes["Silent"]["eta_seconds"] == 2 * 300.0
    assert view.summary["eta_seconds"] == 900.0
    assert view.summary["median_duration_seconds"] == 600.0
    assert view.summary["registered"] is True
    # the stray batch id has no manifest: an unregistered batch of its own
    assert by_id["other"].summary["registered"] is False
    assert by_id["other"].summary["name"] == "未登记批次 · other"


def test_a_lane_missing_from_the_manifest_still_gets_a_row() -> None:
    manifest = {
        "batch_id": "b1", "name": "n", "created_at": NOW, "lanes": [],
        "lanes_status": {}, "registered": True,
    }
    (view,) = build_batch_views([manifest], [_run(duration=10.0, lane="Defect")], NOW)
    assert [lane["lane"] for lane in view.lanes] == ["Defect"]
    assert view.lanes[0]["planned_known"] is False


def test_legacy_runs_without_a_batch_id_group_by_experiment() -> None:
    runs = [
        _run(duration=10.0, batch_id=None, experiment="ooc=greedy", started_at=NOW - 500),
        _run(duration=10.0, batch_id=None, experiment="ooc=greedy", started_at=NOW - 400,
             lane="Silent"),
        _run(duration=10.0, batch_id=None, experiment="run:ooc=naive", started_at=NOW - 300),
    ]

    views = build_batch_views([], runs, NOW)

    names = {view.summary["batch_id"]: view.summary["name"] for view in views}
    assert names == {
        "legacy:ooc=greedy": "未登记批次 · ooc=greedy",
        "legacy:run:ooc=naive": "未登记批次 · run:ooc=naive",
    }
    greedy = next(v for v in views if v.summary["batch_id"] == "legacy:ooc=greedy")
    # no plan, so no ETA: planned is simply what has been played
    assert greedy.summary["planned"] == greedy.summary["finished"] == 2
    assert greedy.summary["eta_seconds"] is None
    assert greedy.summary["registered"] is False
    # ...and with no plan there is no "N of M": the bar must not imply completion
    assert greedy.summary["planned_known"] is False
    assert all(lane["planned_known"] is False for lane in greedy.lanes)
    assert greedy.summary["created_at"] == NOW - 500
    assert greedy.summary["status"] == "done"
    assert sorted(lane["lane"] for lane in greedy.lanes) == ["Ironclad", "Silent"]


def test_unregistered_batch_that_stamped_games_total_gets_a_plan_and_eta() -> None:
    runs = [
        _run(duration=100.0, batch_id="direct-1", games_total=4, game_index=1),
        _run(duration=100.0, batch_id="direct-1", games_total=4, game_index=2),
        _run(LIVE, batch_id="direct-1", games_total=4, game_index=3),
    ]
    (view,) = build_batch_views([], runs, NOW)
    (lane,) = view.lanes
    assert (lane["planned"], lane["remaining"]) == (4, 2)
    assert lane["eta_seconds"] == 200.0
    assert lane["state"] == "running"
    assert lane["planned_known"] is True and view.summary["planned_known"] is True


def test_a_registered_batch_knows_its_plan_unless_a_lane_is_missing_from_the_manifest() -> None:
    manifest = {
        "batch_id": "b1", "name": "n", "created_at": NOW, "registered": True,
        "lanes": [{"lane": "Ironclad", "character": "Ironclad", "planned_games": 5}],
        "lanes_status": {},
    }
    (planned,) = build_batch_views([manifest], [_run(duration=10.0, lane="Ironclad")], NOW)
    assert planned.summary["planned_known"] is True
    # a lane only the logs know about has no plan: the batch total is no plan either
    (partial,) = build_batch_views(
        [manifest], [_run(duration=10.0, lane="Ironclad"), _run(duration=10.0, lane="Defect")], NOW
    )
    assert partial.summary["planned_known"] is False
    assert [lane["planned_known"] for lane in partial.lanes] == [True, False]


def test_batches_are_listed_newest_first_with_unregistered_ones_by_first_game() -> None:
    def manifest(batch_id: str, created: float) -> dict:
        return {"batch_id": batch_id, "name": batch_id, "created_at": created,
                "lanes": [], "lanes_status": {}, "registered": True}

    views = build_batch_views(
        [manifest("old", NOW - 3 * HOUR), manifest("new", NOW - HOUR)],
        [_run(duration=1.0, batch_id=None, experiment="ooc=x", started_at=NOW - 2 * HOUR)],
        NOW,
    )
    assert [v.summary["batch_id"] for v in views] == ["new", "legacy:ooc=x", "old"]


# ---------------------------------------------------------------------------
# Real logs through the catalog
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat()


def _state(decision: str, *, floor: int, hp: int, ts: float, step: int, victory=None) -> dict:
    act, local = (floor - 1) // 17 + 1, (floor - 1) % 17 + 1
    data: dict = {
        "type": "decision",
        "decision": decision,
        "context": {"act": act, "floor": local, "room_type": "Monster"},
        "player": {"name": "The Ironclad", "hp": hp, "max_hp": 80, "gold": 99},
    }
    if victory is not None:
        data["victory"] = victory
    return {"step": step, "ts": _iso(ts), "type": "state", "data": data}


def _game_rows(
    seed: str,
    *,
    started: float = NOW - 3000,
    duration: float | None = 600.0,
    floor: int = 8,
    ended: str | None = "dead",
    character: str = "Ironclad",
    run_id: str | None = None,
    harness: bool = True,
    batch_id: str | None = None,
    lane: str | None = None,
    game_index: int | None = None,
    games_total: int | None = None,
    experiment: str | None = "regression:ooc=greedy",
    checkpoint: str | None = None,
    iso_ts: bool = True,
) -> list[dict]:
    """A GameLogger log: run_meta header (epoch ts), then rows stamped with ISO
    ts that end ``duration`` seconds after ``started``."""
    meta: dict = {"type": "run_meta", "ts": started, "character": character,
                  "seed": seed, "ascension": 0}
    if experiment is not None:
        meta["experiment"] = experiment
    if checkpoint is not None:
        meta["checkpoint"] = checkpoint
    if run_id is not None:
        meta["run_id"] = run_id
    if harness:
        meta["harness"] = "play_full_run"
    for key, value in (("batch_id", batch_id), ("lane", lane), ("game_index", game_index),
                       ("games_total", games_total)):
        if value is not None:
            meta[key] = value
    rows: list[dict] = [meta]
    span = duration if duration is not None else 30.0
    steps = floor + (1 if ended else 0)
    rows.append({"step": 0, "ts": _iso(started + 1), "type": "action",
                 "data": {"cmd": "start_run", "character": character, "seed": seed}})
    for index in range(1, floor + 1):
        rows.append(_state("map_select", floor=index, hp=80 - index,
                           ts=started + 1 + (span - 1) * index / steps, step=index))
    if ended:
        rows.append(_state("game_over", floor=floor, hp=0 if ended == "dead" else 40,
                           ts=started + span, step=floor + 1, victory=ended == "win"))
    if not iso_ts:
        for row in rows[1:]:
            row["ts"] = "garbled"
    return rows


def _write(root: Path, relative: str, rows: list[dict], *, mtime: float | None = None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _catalog(root: Path, clock: _Clock | None = None, **kwargs) -> RunCatalog:
    return RunCatalog(
        [root],
        replay_parser=parse_game_progress,
        include_policy="workbench",
        clock=clock or _Clock(),
        **kwargs,
    )


@pytest.fixture(params=["small", "compact"])
def log_size(request, monkeypatch: pytest.MonkeyPatch) -> str:
    """Both ways a replay is read: adapted whole, or scanned into a compact run."""
    if request.param == "compact":
        monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 4)
    return request.param


def test_regression_games_are_in_regression_runs_and_in_no_training_cohort(
    tmp_path: Path, log_size: str
) -> None:
    _write(tmp_path, "train_1.jsonl",
           _game_rows("t1", harness=False, experiment="train_a", checkpoint="ckpt_a",
                      run_id="train-1"), mtime=NOW - 2000)
    _write(tmp_path, "legacy.jsonl",
           _game_rows("l1", harness=False, experiment="ooc=greedy"), mtime=NOW - 2000)
    _write(tmp_path, "new.jsonl",
           _game_rows("n1", run_id="pfr-1", batch_id="b1", lane="Ironclad", game_index=1,
                      games_total=3), mtime=NOW - 2000)
    catalog = _catalog(tmp_path)

    cohorts = catalog.list_cohorts()
    runs = catalog.regression_runs()

    assert [c["experiment"] for c in cohorts] == ["train_a"]
    assert {run.seed for run in runs} == {"l1", "n1"}
    assert all(run.state == FINISHED for run in runs)
    # nothing about the regression games leaks into the training sources' cohort
    (cohort,) = cohorts
    assert cohort["run_count"] == 1
    (new,) = [run for run in runs if run.seed == "n1"]
    assert (new.batch_id, new.lane, new.game_index, new.games_total) == ("b1", "Ironclad", 1, 3)
    assert new.harness == "play_full_run"
    assert new.run_id == "pfr-1"
    assert new.status is RunStatus.DEAD
    assert new.global_floor == 8
    (legacy,) = [run for run in runs if run.seed == "l1"]
    assert (legacy.harness, legacy.batch_id, legacy.lane) == (None, None, None)
    assert legacy.lane_name == "Ironclad"  # the character, when no lane was stamped


def test_a_training_run_with_an_ooc_label_stays_a_training_run(tmp_path: Path) -> None:
    _write(tmp_path, "t.jsonl",
           _game_rows("t1", harness=False, experiment="run:ooc=greedy", checkpoint="ckpt_a"))
    catalog = _catalog(tmp_path)
    assert catalog.regression_runs() == ()
    assert len(catalog.list_cohorts()) == 1


def test_training_payloads_are_identical_with_regression_games_present(
    tmp_path: Path, log_size: str
) -> None:
    """The golden property: adding play_full_run games changes no training
    payload -- cohorts, tree, run lists, metrics -- byte for byte."""
    for i, floor in enumerate((6, 12, 20), 1):
        _write(tmp_path, f"t{i}.jsonl",
               _game_rows(f"t{i}", harness=False, experiment="train_a", checkpoint="ckpt_a",
                          run_id=f"train-{i}", floor=floor), mtime=NOW - 2000)
    _write(tmp_path, "t_live.jsonl",
           _game_rows("tl", harness=False, experiment="train_a", checkpoint="ckpt_a",
                      run_id="train-live", ended=None, duration=None), mtime=NOW - 5)

    def snapshot() -> str:
        catalog = _catalog(tmp_path)
        cohorts = catalog.list_cohorts()
        payload = {
            "cohorts": cohorts,
            "tree": viewer._build_cohort_tree(cohorts),
            "runs": {c["cohort_id"]: viewer._cohort_runs_payload(catalog, c["cohort_id"]) for c in cohorts},
            "metrics": {c["cohort_id"]: catalog.get_metrics(c["cohort_id"]) for c in cohorts},
        }
        return json.dumps(payload, sort_keys=True)

    before = snapshot()
    assert '"train_a"' in before

    for i in (1, 2):
        rows = _game_rows(f"p{i}", run_id=f"pfr-{i}", batch_id="b1", lane="Ironclad")
        _write(tmp_path, f"pfr_{i}.jsonl", rows, mtime=NOW - 1500)
        _write(tmp_path, f"batches/b1/games/pfr_{i}.jsonl", rows, mtime=NOW - 1500)
    _write(tmp_path, "legacy.jsonl", _game_rows("l1", harness=False, experiment="ooc=greedy"))
    _write(tmp_path, "live.jsonl",
           _game_rows("pl", run_id="pfr-live", batch_id="b1", lane="Ironclad", ended=None,
                      duration=None), mtime=NOW - 3)
    _write_manifest(tmp_path, "b1")
    (tmp_path / "batches" / "b1" / "Ironclad.jsonl").write_text(
        json.dumps({"seed": "p1", "status": "dead", "floor": 8}) + "\n", encoding="utf-8"
    )

    assert snapshot() == before


def test_regression_log_changes_leave_the_training_cache_alone(tmp_path: Path) -> None:
    _write(tmp_path, "t1.jsonl",
           _game_rows("t1", harness=False, experiment="train_a", checkpoint="ckpt_a"))
    catalog = _catalog(tmp_path)
    catalog.list_cohorts()
    key = catalog._cohort_cache_key
    memo = catalog._cohort_memo

    _write(tmp_path, "p1.jsonl", _game_rows("p1", run_id="pfr-1", batch_id="b1"))
    catalog.list_cohorts()
    assert catalog._cohort_cache_key == key
    assert catalog._cohort_memo is memo
    assert len(catalog.regression_runs()) == 1


def test_batch_bookkeeping_files_are_not_sources(tmp_path: Path) -> None:
    rows = _game_rows("p1", run_id="pfr-1", batch_id="b1")
    _write(tmp_path, "batches/b1/games/p1.jsonl", rows, mtime=NOW - 100)
    _write_manifest(tmp_path, "b1")
    (tmp_path / "batches" / "b1" / "lanes_status.json").write_text("{}", encoding="utf-8")
    (tmp_path / "batches" / "b1" / "Ironclad.jsonl").write_text(
        json.dumps({"seed": "p1", "status": "dead"}) + "\n", encoding="utf-8"
    )
    # a batches/ directory that is not ours is scanned like anything else
    other = tmp_path / "batches" / "not-ours"
    other.mkdir()
    (other / "notes.json").write_text(json.dumps({"hello": 1}), encoding="utf-8")

    catalog = RunCatalog([tmp_path], replay_parser=parse_game_progress, clock=_Clock())
    names = sorted(source["display_name"] for source in catalog.list_sources())

    assert names == ["batches/b1/games/p1.jsonl", "batches/not-ours/notes.json"]


def test_the_same_game_in_logs_and_in_the_kept_copy_is_one_run(
    tmp_path: Path, log_size: str
) -> None:
    rows = _game_rows("p1", run_id="pfr-1", batch_id="b1", lane="Ironclad")
    original = _write(tmp_path, "20261012_100000_Ironclad_p1.jsonl", rows, mtime=NOW - 1000)
    copy = tmp_path / "batches" / "b1" / "games" / original.name
    copy.parent.mkdir(parents=True)
    shutil.copy2(original, copy)
    _write(tmp_path, "other.jsonl", _game_rows("p2", run_id="pfr-2", batch_id="b1"),
           mtime=NOW - 900)

    catalog = _catalog(tmp_path)
    runs = catalog.regression_runs()

    assert sorted(run.run_id for run in runs) == ["pfr-1", "pfr-2"]
    (run,) = [run for run in runs if run.run_id == "pfr-1"]
    assert len(run.source_ids) == 2
    # the original is the canonical source: its id stays valid while it is played
    source_names = {s["source_id"]: s["display_name"] for s in catalog.list_sources()}
    assert source_names[run.source_id] == original.name
    # and the copy alone (original purged after 7 days) is still that run
    original.unlink()
    (alone,) = [r for r in _catalog(tmp_path).regression_runs() if r.run_id == "pfr-1"]
    assert alone.source_ids == (alone.source_id,)


def test_live_finished_and_abandoned_follow_the_clock(tmp_path: Path, log_size: str) -> None:
    _write(tmp_path, "done.jsonl", _game_rows("d", run_id="pfr-d", batch_id="b1"), mtime=NOW - 50)
    _write(tmp_path, "live.jsonl",
           _game_rows("l", run_id="pfr-l", batch_id="b1", ended=None, duration=None),
           mtime=NOW - 100)
    clock = _Clock(NOW)
    catalog = _catalog(tmp_path, clock)

    states = {run.seed: run.state for run in catalog.regression_runs()}
    assert states == {"d": FINISHED, "l": LIVE}

    clock.now = NOW + 31 * 60  # the unfinished log has gone quiet
    states = {run.seed: run.state for run in catalog.regression_runs()}
    assert states == {"d": FINISHED, "l": ABANDONED}
    live = [run for run in catalog.regression_runs() if run.seed == "d"]
    assert live[0].status is RunStatus.DEAD


def test_a_live_game_reports_where_it_is(tmp_path: Path, log_size: str) -> None:
    _write(tmp_path, "live.jsonl",
           _game_rows("l", run_id="pfr-l", batch_id="b1", ended=None, duration=None, floor=9),
           mtime=NOW - 10)
    (run,) = _catalog(tmp_path).regression_runs()
    assert run.state == LIVE
    assert (run.act, run.floor, run.global_floor) == (1, 9, 9)
    assert run.hp == 71 and run.max_hp == 80
    assert run.duration_seconds is None and run.record is None


def test_duration_comes_from_the_logs_own_timestamps(tmp_path: Path, log_size: str) -> None:
    _write(tmp_path, "a.jsonl",
           _game_rows("a", run_id="pfr-a", started=NOW - 4000, duration=750.0),
           mtime=NOW - 10)  # mtime far from the last row: it must not be used
    (run,) = _catalog(tmp_path).regression_runs()
    assert run.started_at == NOW - 4000
    assert run.duration_seconds == pytest.approx(750.0, abs=1e-3)
    assert run.ended_at == pytest.approx(NOW - 4000 + 750.0, abs=1e-3)


def test_duration_falls_back_to_the_files_mtime(tmp_path: Path, log_size: str) -> None:
    _write(tmp_path, "a.jsonl",
           _game_rows("a", run_id="pfr-a", started=NOW - 4000, iso_ts=False),
           mtime=NOW - 3400)
    (run,) = _catalog(tmp_path).regression_runs()
    assert run.duration_seconds == pytest.approx(600.0)


def test_regression_run_ids_that_collide_with_nothing_are_kept_separate(tmp_path: Path) -> None:
    """Legacy games carry no run id: none can be merged."""
    for i in (1, 2):
        _write(tmp_path, f"l{i}.jsonl", _game_rows(f"l{i}", harness=False, experiment="ooc=greedy"))
    assert len(_catalog(tmp_path).regression_runs()) == 2


# ---------------------------------------------------------------------------
# Whole batches from disk
# ---------------------------------------------------------------------------


def _batch_on_disk(root: Path, *, batch_id: str = "regression-20261012-100000") -> None:
    """Ironclad: 5 planned, 2 finished (600 s, 1200 s), 1 live.  Silent: 3 planned,
    none played yet, queued behind the lane limit."""
    directory = _write_manifest(
        root, batch_id,
        lanes=[
            {"lane": "Ironclad", "character": "Ironclad", "planned_games": 5},
            {"lane": "Silent", "character": "Silent", "planned_games": 3},
        ],
    )
    (directory / "lanes_status.json").write_text(
        json.dumps({"Ironclad": {"state": "running", "pid": 7, "started_at": NOW - 3000,
                                 "ended_at": None, "returncode": None},
                    "Silent": {"state": "queued", "pid": None}}),
        encoding="utf-8",
    )
    for index, duration in ((1, 600.0), (2, 1200.0)):
        rows = _game_rows(f"run_{index}", run_id=f"pfr-{index}", batch_id=batch_id,
                          lane="Ironclad", game_index=index, games_total=5,
                          started=NOW - 3000 + (index - 1) * 1300, duration=duration,
                          floor=10 * index)
        _write(root, f"20261012_10000{index}_Ironclad_run_{index}.jsonl", rows,
               mtime=NOW - 1700 + index)
        _write(root, f"batches/{batch_id}/games/20261012_10000{index}_Ironclad_run_{index}.jsonl",
               rows, mtime=NOW - 1700 + index)
    _write(root, "20261012_100003_Ironclad_run_3.jsonl",
           _game_rows("run_3", run_id="pfr-3", batch_id=batch_id, lane="Ironclad", game_index=3,
                      games_total=5, started=NOW - 200, ended=None, duration=None, floor=4),
           mtime=NOW - 20)


def test_batch_views_from_logs_and_manifest(tmp_path: Path, log_size: str) -> None:
    _batch_on_disk(tmp_path)
    catalog = _catalog(tmp_path)
    (manifest,) = load_manifests(tmp_path)

    (view,) = build_batch_views([manifest], catalog.regression_runs(), catalog.now())

    lanes = {lane["lane"]: lane for lane in view.lanes}
    ironclad, silent = lanes["Ironclad"], lanes["Silent"]
    assert (ironclad["planned"], ironclad["finished"], ironclad["live"]) == (5, 2, 1)
    assert ironclad["policy"] == "greedy"
    assert ironclad["median_duration_seconds"] == pytest.approx(900.0, abs=1e-3)
    assert ironclad["eta_seconds"] == pytest.approx(3 * 900.0, abs=1e-3)  # 5 - 2 finished
    assert ironclad["state"] == "running"
    assert ironclad["avg_global_floor"] == 15.0  # 10 and 20; the live game is not in it
    assert (silent["finished"], silent["state"]) == (0, "queued")
    assert silent["eta_seconds"] == pytest.approx(3 * 900.0, abs=1e-3)  # the batch median
    assert silent["eta_basis"] == "batch"
    assert view.summary["eta_seconds"] == pytest.approx(2700.0, abs=1e-3)
    assert view.summary["eta_at"] == pytest.approx(NOW + 2700.0, abs=1e-3)
    assert (view.summary["planned"], view.summary["finished"], view.summary["live"]) == (8, 2, 1)
    assert view.summary["status"] == "running"


def test_eta_follows_the_injected_clock(tmp_path: Path) -> None:
    _batch_on_disk(tmp_path)
    clock = _Clock(NOW)
    catalog = _catalog(tmp_path, clock)

    def eta_at() -> float:
        (manifest,) = load_manifests(tmp_path)
        (view,) = build_batch_views([manifest], catalog.regression_runs(), catalog.now())
        return view.summary["eta_at"]

    first = eta_at()
    clock.now = NOW + 120
    assert eta_at() == pytest.approx(first + 120)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@contextmanager
def _server(catalog: RunCatalog):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_viewer_handler(catalog))
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urlopen(base + path, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_batches_endpoint_lists_batches_with_progress(tmp_path: Path, log_size: str) -> None:
    _batch_on_disk(tmp_path)
    _write(tmp_path, "legacy.jsonl",
           _game_rows("l1", harness=False, experiment="ooc=greedy", started=NOW - 90000),
           mtime=NOW - 80000)
    clock = _Clock(NOW)

    with _server(_catalog(tmp_path, clock)) as base:
        status, payload = _get(base, "/api/batches")

    assert status == 200
    assert payload["now"] == NOW
    assert payload["problems"] == []
    ids = [batch["batch_id"] for batch in payload["batches"]]
    assert ids == ["regression-20261012-100000", "legacy:ooc=greedy"]
    batch = payload["batches"][0]
    assert batch["name"] == "regression" and batch["kind"] == "regression"
    assert (batch["planned"], batch["finished"], batch["live"], batch["technical"]) == (8, 2, 1, 0)
    assert batch["eta_seconds"] == pytest.approx(2700.0, abs=1e-3)
    assert batch["status"] == "running" and batch["done"] is False
    assert batch["git_commit"] == "abc1234"
    assert batch["planned_known"] is True
    legacy = payload["batches"][1]
    assert legacy["registered"] is False
    assert legacy["planned_known"] is False
    assert legacy["eta_seconds"] is None
    assert legacy["done"] is True


def test_batches_endpoint_without_any_batch(tmp_path: Path) -> None:
    with _server(_catalog(tmp_path)) as base:
        status, payload = _get(base, "/api/batches")
    assert status == 200
    assert payload["batches"] == []


def test_batches_endpoint_reports_malformed_manifests(tmp_path: Path) -> None:
    bad = tmp_path / "batches" / "broken"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{", encoding="utf-8")
    with _server(_catalog(tmp_path)) as base:
        status, payload = _get(base, "/api/batches")
    assert status == 200
    assert payload["batches"] == []
    assert len(payload["problems"]) == 1 and "broken" in payload["problems"][0]


def test_batch_endpoint_returns_the_batch_and_its_lanes(tmp_path: Path, log_size: str) -> None:
    _batch_on_disk(tmp_path)
    with _server(_catalog(tmp_path)) as base:
        status, payload = _get(base, "/api/batch?id=regression-20261012-100000")
        missing_status, missing = _get(base, "/api/batch?id=nope")
        no_id_status, _ = _get(base, "/api/batch")
        extra_status, _ = _get(base, "/api/batch?id=x&bogus=1")

    assert status == 200
    assert payload["batch"]["batch_id"] == "regression-20261012-100000"
    assert payload["batch"]["argv"][:1] == ["python/run_batch.py"]
    assert [lane["lane"] for lane in payload["lanes"]] == ["Ironclad", "Silent"]
    ironclad = payload["lanes"][0]
    assert ironclad["character"] == "Ironclad" and ironclad["pid"] == 7
    assert (ironclad["wins"], ironclad["technical"]) == (0, 0)
    assert missing_status == 404 and "nope" in missing["error"]
    assert no_id_status == 400
    assert extra_status == 400


def test_lane_endpoint_lists_live_first_then_finished_newest_first(
    tmp_path: Path, log_size: str
) -> None:
    _batch_on_disk(tmp_path)
    with _server(_catalog(tmp_path)) as base:
        status, payload = _get(
            base, "/api/batch/lane?id=regression-20261012-100000&lane=Ironclad"
        )
        silent_status, silent = _get(
            base, "/api/batch/lane?id=regression-20261012-100000&lane=Silent"
        )
        bad_lane_status, _ = _get(base, "/api/batch/lane?id=regression-20261012-100000&lane=Nope")
        no_lane_status, _ = _get(base, "/api/batch/lane?id=regression-20261012-100000")

    assert status == 200
    assert payload["lane"] == "Ironclad" and payload["runs_complete"] is True
    assert payload["batch_name"] == "regression"
    rows = payload["runs"]
    assert [row["seed"] for row in rows] == ["run_3", "run_2", "run_1"]
    live, newest, oldest = rows
    assert live["status"] == "in_progress" and live["state"] == "live"
    assert (live["act"], live["floor"], live["global_floor"]) == (1, 4, 4)
    assert (live["hp"], live["max_hp"]) == (76, 80)
    assert newest["status"] == "dead" and newest["global_floor"] == 20
    assert newest["duration_seconds"] == pytest.approx(1200.0, abs=1e-3)
    assert oldest["game_index"] == 1
    # same keys the cohort run table reads, addressed by source
    for row in rows:
        assert {"run_id", "source_id", "seed", "status", "global_floor", "act",
                "started_at", "has_map", "ref"} <= set(row)
        assert row["ref"] == {"kind": "source", "id": row["source_id"]}
    assert payload["lane_summary"]["finished"] == 2
    assert silent_status == 200 and silent["runs"] == []
    assert bad_lane_status == 404
    assert no_lane_status == 400


def _arm_batch_on_disk(root: Path, batch_id: str = "route-ab-20261012-100000") -> None:
    """run_batch.py --arm: Ironclad x {random, value}, one finished game per Ironclad
    lane (the value arm's experiment label carries `,route=value`), Silent not started."""
    lanes = []
    for character in ("Ironclad", "Silent"):
        for arm in ("random", "value"):
            lanes.append({"lane": f"{character}/{arm}", "character": character,
                          "planned_games": 2, "seed_prefix": "p3b_", "seed_offset": 0,
                          "extra_args": [], "arm": arm,
                          "arm_env": {"STS2_ROUTE_POLICY": arm}})
    directory = _write_manifest(root, batch_id, name="route-ab", kind="ab", lanes=lanes)
    (directory / "lanes_status.json").write_text(
        json.dumps({"Ironclad/random": {"state": "running", "pid": 7, "started_at": NOW - 900},
                    "Ironclad/value": {"state": "running", "pid": 8, "started_at": NOW - 900},
                    "Silent/random": {"state": "queued"}, "Silent/value": {"state": "queued"}}),
        encoding="utf-8")
    for arm in ("random", "value"):
        experiment = "p3b:ooc=greedy" + (",route=value" if arm == "value" else "")
        rows = _game_rows("p3b_1", run_id=f"pfr-{arm}", batch_id=batch_id,
                          lane=f"Ironclad/{arm}", game_index=1, games_total=2,
                          started=NOW - 800, duration=600.0, floor=10 if arm == "random" else 14,
                          experiment=experiment)
        _write(root, f"batches/{batch_id}/games/Ironclad__{arm}/Ironclad_greedy_a1_p3b_1.jsonl",
               rows, mtime=NOW - 100)


def test_arm_batch_lanes_carry_their_arm_and_keep_the_manifest_order(
    tmp_path: Path, log_size: str
) -> None:
    _arm_batch_on_disk(tmp_path)
    catalog = _catalog(tmp_path)
    (manifest,) = load_manifests(tmp_path)
    assert [(lane["lane"], lane["arm"], lane["arm_env"]) for lane in manifest["lanes"]] == [
        ("Ironclad/random", "random", {"STS2_ROUTE_POLICY": "random"}),
        ("Ironclad/value", "value", {"STS2_ROUTE_POLICY": "value"}),
        ("Silent/random", "random", {"STS2_ROUTE_POLICY": "random"}),
        ("Silent/value", "value", {"STS2_ROUTE_POLICY": "value"})]

    (view,) = build_batch_views([manifest], catalog.regression_runs(), catalog.now())

    assert [lane["lane"] for lane in view.lanes] == [
        "Ironclad/random", "Ironclad/value", "Silent/random", "Silent/value"]
    by_lane = {lane["lane"]: lane for lane in view.lanes}
    assert [by_lane[name]["arm"] for name in by_lane] == ["random", "value"] * 2
    assert by_lane["Ironclad/value"]["arm_env"] == {"STS2_ROUTE_POLICY": "value"}
    assert by_lane["Ironclad/random"]["character"] == "Ironclad"
    # the value arm's `,route=value` label is not part of the out-of-combat policy
    assert by_lane["Ironclad/value"]["policy"] == "greedy"
    assert by_lane["Ironclad/random"]["policy"] == "greedy"
    # each lane owns its game: the two arms played the same seed without merging
    assert (by_lane["Ironclad/random"]["finished"], by_lane["Ironclad/value"]["finished"]) == (1, 1)
    assert by_lane["Ironclad/random"]["avg_global_floor"] == 10.0
    assert by_lane["Ironclad/value"]["avg_global_floor"] == 14.0
    assert by_lane["Silent/value"]["state"] == "queued"
    assert {run.lane_name for run in catalog.regression_runs()} == {
        "Ironclad/random", "Ironclad/value"}
    assert len(catalog.regression_runs()) == 2


def test_a_batch_without_arms_reports_no_arm(tmp_path: Path, log_size: str) -> None:
    _batch_on_disk(tmp_path)
    catalog = _catalog(tmp_path)
    (manifest,) = load_manifests(tmp_path)
    (view,) = build_batch_views([manifest], catalog.regression_runs(), catalog.now())
    assert all(lane["arm"] is None and lane["arm_env"] == {} for lane in view.lanes)


def test_endpoints_serve_a_lane_whose_name_has_a_slash(tmp_path: Path, log_size: str) -> None:
    _arm_batch_on_disk(tmp_path)
    with _server(_catalog(tmp_path)) as base:
        batch_status, batch = _get(base, "/api/batch?id=route-ab-20261012-100000")
        status, payload = _get(
            base, "/api/batch/lane?id=route-ab-20261012-100000&lane=Ironclad%2Fvalue")
        other_status, other = _get(
            base, "/api/batch/lane?id=route-ab-20261012-100000&lane=Silent%2Fvalue")
        raw_status, _ = _get(base, "/api/batch/lane?id=route-ab-20261012-100000&lane=Ironclad")

    assert batch_status == 200 and batch["batch"]["kind"] == "ab"
    assert [lane["lane"] for lane in batch["lanes"]] == [
        "Ironclad/random", "Ironclad/value", "Silent/random", "Silent/value"]
    assert [lane["arm"] for lane in batch["lanes"]] == ["random", "value", "random", "value"]
    assert status == 200 and payload["lane"] == "Ironclad/value"
    assert payload["lane_summary"]["arm"] == "value"
    assert [row["seed"] for row in payload["runs"]] == ["p3b_1"]
    assert payload["runs"][0]["global_floor"] == 14
    assert other_status == 200 and other["runs"] == []
    assert raw_status == 404                                  # the bare character is no lane


def test_lane_endpoint_shows_an_abandoned_game_as_unknown(tmp_path: Path, log_size: str) -> None:
    _batch_on_disk(tmp_path)
    clock = _Clock(NOW + 2 * HOUR)  # the live log has been quiet for hours
    with _server(_catalog(tmp_path, clock)) as base:
        _, payload = _get(base, "/api/batch/lane?id=regression-20261012-100000&lane=Ironclad")
    by_seed = {row["seed"]: row for row in payload["runs"]}
    assert by_seed["run_3"]["status"] == "unknown" and by_seed["run_3"]["state"] == "abandoned"
    assert payload["lane_summary"]["abandoned"] == 1
    assert payload["lane_summary"]["live"] == 0


def test_a_finished_batch_reports_done_and_no_eta(tmp_path: Path) -> None:
    directory = _write_manifest(
        tmp_path, "b-done",
        lanes=[{"lane": "Ironclad", "character": "Ironclad", "planned_games": 2}],
    )
    (directory / "lanes_status.json").write_text(
        json.dumps({"Ironclad": {"state": "done", "pid": 1, "returncode": 0,
                                 "started_at": NOW - 4000, "ended_at": NOW - 100}}),
        encoding="utf-8",
    )
    for index in (1, 2):
        _write(tmp_path, f"g{index}.jsonl",
               _game_rows(f"s{index}", run_id=f"pfr-{index}", batch_id="b-done", lane="Ironclad",
                          started=NOW - 3500 + index * 600), mtime=NOW - 1000)
    with _server(_catalog(tmp_path)) as base:
        _, payload = _get(base, "/api/batches")
    (batch,) = payload["batches"]
    assert (batch["status"], batch["done"], batch["eta_seconds"]) == ("done", True, None)
    assert (batch["planned"], batch["finished"]) == (2, 2)


def test_a_lane_that_could_not_spawn_is_failed_with_its_reason(tmp_path: Path) -> None:
    directory = _write_manifest(
        tmp_path, "b-fail",
        lanes=[{"lane": "Ironclad", "character": "Ironclad", "planned_games": 2}],
    )
    (directory / "lanes_status.json").write_text(
        json.dumps({"Ironclad": {"state": "failed", "pid": None, "started_at": None,
                                 "ended_at": NOW - 5, "returncode": None,
                                 "error": "FileNotFoundError: run_caffeinated.sh"}}),
        encoding="utf-8",
    )
    with _server(_catalog(tmp_path)) as base:
        _, payload = _get(base, "/api/batch?id=b-fail")
    (lane,) = payload["lanes"]
    assert lane["state"] == "failed" and "run_caffeinated" in lane["error"]
    assert payload["batch"]["status"] == "failed"
    assert payload["batch"]["failed_lanes"] == 1


def test_batch_endpoints_never_emit_non_finite_numbers(tmp_path: Path, log_size: str) -> None:
    """json.dumps(allow_nan=False) is the server's contract: any NaN would be a 500."""
    _batch_on_disk(tmp_path)
    _write(tmp_path, "weird.jsonl",
           _game_rows("w", run_id="pfr-w", batch_id="b-weird", floor=0, ended="dead"),
           mtime=NOW - 100)
    with _server(_catalog(tmp_path)) as base:
        for path in ("/api/batches", "/api/batch?id=b-weird",
                     "/api/batch/lane?id=b-weird&lane=Ironclad"):
            status, _ = _get(base, path)
            assert status == 200, path
