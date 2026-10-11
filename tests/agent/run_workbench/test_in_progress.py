"""Games that are still being played: listed in their cohort, counted in none
of its statistics.

A GameLogger log with no terminal state whose file was written to within
``IN_PROGRESS_STALE_SECONDS`` is an in-progress run.  It is a member of the
cohort its ``run_meta`` header names, shows up (first) in the cohort's run list
and is openable, but every number computed for the cohort is exactly what it
would be without it.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import agent.run_progress_viewer as viewer
import agent.run_workbench.catalog as catalog_module
from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench.catalog import (
    IN_PROGRESS_STALE_SECONDS,
    REINDEX_MIN_INTERVAL_SECONDS,
    CatalogNotFoundError,
    RunCatalog,
)
from agent.run_workbench.replay import parse_game_progress

NOW = 1_800_000_000.0
STARTED = NOW - 600.0


class _Clock:
    """A settable clock, so freshness never depends on the machine's time."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _state(
    decision: str,
    *,
    act: int,
    floor: int,
    hp: int,
    max_hp: int = 80,
    step: int,
    victory: bool | None = None,
) -> dict:
    data: dict = {
        "type": "decision",
        "decision": decision,
        "context": {"act": act, "floor": floor, "room_type": "Monster"},
        "player": {"name": "The Ironclad", "hp": hp, "max_hp": max_hp, "gold": 99},
    }
    if victory is not None:
        data["victory"] = victory
    return {"step": step, "ts": "2026-10-10T10:00:00", "type": "state", "data": data}


def _game_log(
    *,
    seed: str,
    experiment: str = "ooc=greedy",
    character: str = "Ironclad",
    ascension: int = 1,
    started: float = STARTED,
    final_floor: int = 6,
    hp: int = 61,
    max_hp: int = 80,
    ended: str | None = None,
    padding: int = 0,
) -> list[dict]:
    """A GameLogger log reaching ``final_floor`` (global), optionally ended.

    ``ended`` is "dead" / "win" for a log that ends in a game_over decision;
    ``padding`` adds that many extra state rows on the last floor.
    """
    rows: list[dict] = [
        {
            "type": "run_meta",
            "ts": started,
            "experiment": experiment,
            "character": character,
            "seed": seed,
            "ascension": ascension,
        },
        {
            "step": 0,
            "ts": "2026-10-10T10:00:00",
            "type": "action",
            "data": {
                "cmd": "start_run",
                "character": character,
                "seed": seed,
                "ascension": ascension,
            },
        },
    ]
    step = 1
    for global_floor in range(1, final_floor + 1):
        act, floor = (global_floor - 1) // 17 + 1, (global_floor - 1) % 17 + 1
        rows.append(
            _state(
                "map_select",
                act=act,
                floor=floor,
                hp=hp if global_floor == final_floor else max_hp,
                max_hp=max_hp,
                step=step,
            )
        )
        step += 1
    act, floor = (final_floor - 1) // 17 + 1, (final_floor - 1) % 17 + 1
    for _ in range(padding):
        rows.append(
            _state("combat_play", act=act, floor=floor, hp=hp, max_hp=max_hp, step=step)
        )
        step += 1
    if ended is not None:
        rows.append(
            _state(
                "game_over",
                act=act,
                floor=floor,
                hp=0 if ended == "dead" else hp,
                max_hp=max_hp,
                step=step,
                victory=ended == "win",
            )
        )
    return rows


def _write_log(
    root: Path, name: str, rows: list[dict], *, mtime: float = NOW - 5.0
) -> Path:
    path = root / name
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
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


def _cohort(catalog: RunCatalog, label: str) -> dict:
    (cohort,) = [c for c in catalog.list_cohorts() if c["label"] == label]
    return cohort


@pytest.fixture(params=["small", "compact"])
def log_size(request, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a test over both ways a replay log is read.

    A log of at most INDEX_RECORD_LIMIT records is adapted whole (status
    ``in_progress`` when unfinished); a longer one is scanned into a compact
    run (status ``unknown`` when unfinished).  In-progress detection has to
    treat both the same.
    """
    if request.param == "compact":
        monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 4)
    return request.param


def _finished_batch(root: Path) -> None:
    """Three finished runs of one cohort: floors 10, 20, 30."""
    for seed, floor in (("s1", 10), ("s2", 20), ("s3", 30)):
        _write_log(
            root,
            f"done_{seed}.jsonl",
            _game_log(seed=seed, final_floor=floor, ended="dead", started=STARTED - 3600),
            mtime=NOW - 3000.0,
        )


# ---------------------------------------------------------------------------
# Classification: no terminal state AND written to recently
# ---------------------------------------------------------------------------


def test_unfinished_log_is_in_progress_only_while_its_file_is_fresh(
    tmp_path: Path, log_size: str
) -> None:
    mtime = NOW - 100.0
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p1"), mtime=mtime)
    clock = _Clock(mtime + 60.0)
    catalog = _catalog(tmp_path, clock)

    (cohort,) = catalog.list_cohorts()
    assert cohort["label"] == "ooc=greedy · Ironclad · a1"
    assert cohort["in_progress_count"] == 1
    assert cohort["run_count"] == 0

    # The boundary itself is still fresh; a second past it is not.  No file
    # changed in between, so this also proves the cached cohorts are not reused
    # across a change of the clock.
    clock.now = mtime + IN_PROGRESS_STALE_SECONDS
    assert [c["in_progress_count"] for c in catalog.list_cohorts()] == [1]
    clock.now = mtime + IN_PROGRESS_STALE_SECONDS + 1
    assert catalog.list_cohorts() == []


def test_stale_unfinished_log_keeps_the_classification_it_had(
    tmp_path: Path, log_size: str
) -> None:
    """Old and unfinished: listed nowhere, and still `in_progress` when opened
    (the log is small) or `unknown` in the cohort scan (the log is large)."""
    _write_log(
        tmp_path,
        "old.jsonl",
        _game_log(seed="p1"),
        mtime=NOW - IN_PROGRESS_STALE_SECONDS - 1,
    )
    catalog = _catalog(tmp_path)

    assert catalog.list_cohorts() == []
    (source,) = catalog.list_sources()
    payload = catalog.get_run_by_source(source["source_id"])
    assert payload["run"]["outcome"]["status"] == "in_progress"
    assert payload["live"] is False


def test_finished_log_is_never_in_progress_however_fresh(
    tmp_path: Path, log_size: str
) -> None:
    _write_log(
        tmp_path,
        "done.jsonl",
        _game_log(seed="p1", ended="dead"),
        mtime=NOW,
    )
    (cohort,) = _catalog(tmp_path).list_cohorts()
    assert (cohort["run_count"], cohort["in_progress_count"]) == (1, 0)


def test_in_progress_detection_can_be_switched_off(tmp_path: Path) -> None:
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p1"))
    assert _catalog(tmp_path, in_progress_window=None).list_cohorts() == []


def test_only_single_run_replay_logs_can_be_in_progress(tmp_path: Path) -> None:
    """A deck history or eval file holds many runs, and its mtime says nothing
    about any one of them."""
    (tmp_path / "deck.jsonl").write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {"event": "milestone", "run_id": "d1", "checkpoint": "c", "character": "Ironclad", "ts": 1},
                {"event": "card_pick", "run_id": "d1", "checkpoint": "c", "character": "Ironclad", "ts": 2},
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "eval.jsonl").write_text(
        json.dumps(
            {"event": "eval_result", "run_id": "e1", "checkpoint": "c", "character": "Ironclad", "ts": 3}
        )
        + "\n",
        encoding="utf-8",
    )
    for name in ("deck.jsonl", "eval.jsonl"):
        os.utime(tmp_path / name, (NOW, NOW))
    assert _catalog(tmp_path).list_cohorts() == []


def test_unfinished_log_sharing_a_run_id_with_another_run_is_left_to_the_join(
    tmp_path: Path,
) -> None:
    """A run id two logs share names one joined run; the join decides what it
    is, exactly as it did before in-progress runs existed."""
    for name, rows in (
        ("a_done.jsonl", _game_log(seed="p1", ended="dead")),
        ("b_live.jsonl", _game_log(seed="p1", final_floor=3)),
    ):
        for row in rows:
            row["run_id"] = "shared"
        _write_log(tmp_path, name, rows)
    (cohort,) = _catalog(tmp_path).list_cohorts()
    assert (cohort["run_count"], cohort["in_progress_count"]) == (1, 0)


# ---------------------------------------------------------------------------
# Membership without influence on any statistic
# ---------------------------------------------------------------------------


def _everything_about(catalog: RunCatalog, label: str) -> dict:
    cohorts = catalog.list_cohorts()
    cohort = next(c for c in cohorts if c["label"] == label)
    cohort_id = cohort["cohort_id"]
    descriptors = [
        {key: value for key, value in c.items() if key != "in_progress_count"}
        for c in cohorts
    ]
    return {
        "descriptors": descriptors,
        "metrics": catalog.get_metrics(cohort_id),
        "records": [
            (record.run_id, record.source_id, record.outcome.status.value)
            for record in catalog.get_cohort_records(cohort_id)
        ],
        "tree": viewer._build_cohort_tree(
            [
                {key: value for key, value in c.items() if key != "in_progress_count"}
                for c in cohorts
            ]
        ),
    }


def test_in_progress_run_changes_no_statistic_of_its_cohort(
    tmp_path: Path, log_size: str
) -> None:
    _finished_batch(tmp_path)
    catalog = _catalog(tmp_path)
    label = "ooc=greedy · Ironclad · a1"
    before = _everything_about(catalog, label)
    assert _cohort(catalog, label)["run_count"] == 3
    assert _cohort(catalog, label)["in_progress_count"] == 0

    # Two live games, one of them far beyond every finished one.
    _write_log(tmp_path, "live1.jsonl", _game_log(seed="p8", final_floor=45, padding=3))
    _write_log(tmp_path, "live2.jsonl", _game_log(seed="p9", final_floor=2))
    after = _everything_about(catalog, label)

    assert _cohort(catalog, label)["in_progress_count"] == 2
    assert after == before
    # ...so the numbers the tree and the metric cards show are the finished ones.
    cohort = _cohort(catalog, label)
    assert (cohort["run_count"], cohort["valid_n"], cohort["technical_count"]) == (3, 3, 0)
    assert cohort["avg_global_floor"] == 20.0
    metrics = catalog.get_metrics(cohort["cohort_id"])["current"]
    assert (metrics["all_n"], metrics["valid_n"], metrics["max_global_floor"]) == (3, 3, 30)
    assert all(
        point["source_id"] != live
        for point in metrics["trend"]
        for live in [s["source_id"] for s in catalog.list_sources() if "live" in s["display_name"]]
    )


def test_in_progress_run_does_not_become_a_baseline_or_change_comparison(
    tmp_path: Path, log_size: str
) -> None:
    """Two comparable cohorts; a live game in the first must leave the
    comparison, and the default baseline link, as they were."""
    for seed, floor in (("a1", 10), ("a2", 14)):
        _write_log(
            tmp_path,
            f"x_{seed}.jsonl",
            _game_log(seed=seed, experiment="exp-x", final_floor=floor, ended="dead"),
            mtime=NOW - 3000,
        )
    for seed, floor in (("b1", 21), ("b2", 25)):
        _write_log(
            tmp_path,
            f"y_{seed}.jsonl",
            _game_log(seed=seed, experiment="exp-y", final_floor=floor, ended="dead"),
            mtime=NOW - 3000,
        )
    catalog = _catalog(tmp_path)
    cohorts = {c["label"]: c for c in catalog.list_cohorts()}
    x, y = cohorts["exp-x · Ironclad · a1"], cohorts["exp-y · Ironclad · a1"]
    before = catalog.get_metrics(x["cohort_id"], y["cohort_id"])
    assert before["comparison"] is not None

    _write_log(tmp_path, "live.jsonl", _game_log(seed="a9", experiment="exp-x", final_floor=50))
    after_cohorts = {c["label"]: c for c in catalog.list_cohorts()}
    assert after_cohorts["exp-x · Ironclad · a1"]["in_progress_count"] == 1
    assert catalog.get_metrics(x["cohort_id"], y["cohort_id"]) == before
    # readiness, signature and the default baseline link: all as they were
    for label, old in cohorts.items():
        new = {k: v for k, v in after_cohorts[label].items() if k != "in_progress_count"}
        assert new == {k: v for k, v in old.items() if k != "in_progress_count"}


def test_cohort_with_only_in_progress_runs_still_appears(
    tmp_path: Path, log_size: str
) -> None:
    _write_log(tmp_path, "l1.jsonl", _game_log(seed="p1", started=STARTED))
    _write_log(
        tmp_path, "l2.jsonl", _game_log(seed="p2", started=STARTED + 100, final_floor=20)
    )
    catalog = _catalog(tmp_path)

    (cohort,) = catalog.list_cohorts()
    assert cohort["label"] == "ooc=greedy · Ironclad · a1"
    assert cohort["in_progress_count"] == 2
    assert cohort["run_count"] == 0
    assert cohort["valid_n"] == 0
    assert cohort["avg_global_floor"] is None
    assert cohort["technical_count"] == 0
    # Without a finished run the games under way are all there is to date it by,
    # so the batch is not sunk below every dated one.
    assert cohort["latest_at"] == STARTED + 100

    # It is a real cohort: it has an (empty) summary and a tree entry.
    summary = catalog.get_metrics(cohort["cohort_id"])["current"]
    assert (summary["all_n"], summary["valid_n"], summary["avg_global_floor"]) == (0, 0, None)
    (version,) = viewer._build_cohort_tree([cohort])
    (leaf,) = version["characters"][0]["cohorts"]
    assert (leaf["run_count"], leaf["avg_global_floor"], leaf["in_progress_count"]) == (0, None, 2)
    assert catalog.get_cohort_records(cohort["cohort_id"]) == ()


def test_cohort_gets_no_latest_at_from_live_games_when_it_has_finished_ones(
    tmp_path: Path,
) -> None:
    _finished_batch(tmp_path)
    catalog = _catalog(tmp_path)
    latest = _cohort(catalog, "ooc=greedy · Ironclad · a1")["latest_at"]
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p9", started=STARTED + 500))
    assert _cohort(catalog, "ooc=greedy · Ironclad · a1")["latest_at"] == latest


def test_in_progress_run_joins_the_same_cohort_a_finished_one_would(
    tmp_path: Path, log_size: str
) -> None:
    """Same key from the run_meta header: experiment, character, ascension."""
    _write_log(tmp_path, "g_a1.jsonl", _game_log(seed="1", ascension=1))
    _write_log(tmp_path, "g_a0.jsonl", _game_log(seed="2", ascension=0))
    _write_log(tmp_path, "n_a1.jsonl", _game_log(seed="3", experiment="ooc=naive"))
    _write_log(tmp_path, "s_a1.jsonl", _game_log(seed="4", character="Silent"))
    labels = {
        c["label"]: c["in_progress_count"] for c in _catalog(tmp_path).list_cohorts()
    }
    assert labels == {
        "ooc=greedy · Ironclad · a1": 1,
        "ooc=greedy · Ironclad · a0": 1,
        "ooc=naive · Ironclad · a1": 1,
        "ooc=greedy · Silent · a1": 1,
    }


# ---------------------------------------------------------------------------
# The cohort's run list
# ---------------------------------------------------------------------------


def test_cohort_runs_list_in_progress_runs_first_newest_first(
    tmp_path: Path, log_size: str
) -> None:
    _finished_batch(tmp_path)
    _write_log(
        tmp_path,
        "live_old.jsonl",
        _game_log(seed="old", started=STARTED - 300, final_floor=19, hp=33, max_hp=70),
    )
    _write_log(
        tmp_path,
        "live_new.jsonl",
        _game_log(seed="new", started=STARTED + 300, final_floor=6, hp=61, max_hp=80, padding=5),
    )
    catalog = _catalog(tmp_path)
    cohort = _cohort(catalog, "ooc=greedy · Ironclad · a1")
    finished_only = viewer._cohort_runs_payload(
        _catalog(_without(tmp_path, "live_old.jsonl", "live_new.jsonl")),
        cohort["cohort_id"],
    )["runs"]

    payload = viewer._cohort_runs_payload(catalog, cohort["cohort_id"])
    rows = payload["runs"]

    assert [row["status"] for row in rows[:2]] == ["in_progress", "in_progress"]
    assert [row["seed"] for row in rows[:2]] == ["new", "old"]
    assert all(row["status"] == "dead" for row in rows[2:])
    # The finished rows are the very rows there were before.
    # (row order among finished runs follows source ids, which hash the directory)
    assert sorted(
        (_strip_source(row) for row in rows[2:]), key=lambda row: row["seed"]
    ) == sorted((_strip_source(row) for row in finished_only), key=lambda row: row["seed"])
    assert payload["runs_complete"] is True

    new, old = rows[:2]
    assert (new["global_floor"], new["act"], new["floor"]) == (6, 1, 6)
    assert (new["hp"], new["max_hp"]) == (61, 80)
    assert (old["global_floor"], old["act"], old["floor"]) == (19, 2, 2)
    assert (old["hp"], old["max_hp"]) == (33, 70)
    assert new["started_at"] == STARTED + 300
    assert new["updated_at"] is not None
    # Addressed like any run without a run id: through its source.
    assert new["ref"] == {"kind": "source", "id": new["source_id"]}
    assert new["run_id"] is None
    assert new["has_map"] is True


def _without(root: Path, *names: str) -> Path:
    """A copy of ``root`` minus some files (a fresh directory next to it)."""
    copy = root.parent / (root.name + "_without")
    copy.mkdir(exist_ok=True)
    for path in root.iterdir():
        if path.name not in names:
            target = copy / path.name
            target.write_bytes(path.read_bytes())
            os.utime(target, (path.stat().st_mtime, path.stat().st_mtime))
    return copy


def _strip_source(row: dict) -> dict:
    # source ids hash the directory they were found in
    return {k: v for k, v in row.items() if k not in {"source_id", "ref"}}


def test_in_progress_rows_do_not_count_against_the_row_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewer, "_COHORT_RUNS_LIMIT", 2)
    _finished_batch(tmp_path)  # three finished runs
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p9"))
    catalog = _catalog(tmp_path)
    cohort = _cohort(catalog, "ooc=greedy · Ironclad · a1")

    payload = viewer._cohort_runs_payload(catalog, cohort["cohort_id"])

    assert [row["status"] for row in payload["runs"]] == [
        "in_progress", "dead", "dead",
    ]
    assert payload["runs_complete"] is False  # three finished runs, limit two


def test_cohort_runs_of_a_cohort_with_only_in_progress_runs(
    tmp_path: Path, log_size: str
) -> None:
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=40))
    catalog = _catalog(tmp_path)
    (cohort,) = catalog.list_cohorts()

    payload = viewer._cohort_runs_payload(catalog, cohort["cohort_id"])

    (row,) = payload["runs"]
    assert (row["status"], row["global_floor"], row["act"], row["floor"]) == (
        "in_progress", 40, 3, 6,
    )
    assert payload["runs_complete"] is True


def test_a_run_that_ends_moves_from_the_live_list_into_the_statistics(
    tmp_path: Path, log_size: str
) -> None:
    _finished_batch(tmp_path)
    path = _write_log(tmp_path, "game.jsonl", _game_log(seed="p9", final_floor=40))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    cohort = _cohort(catalog, "ooc=greedy · Ironclad · a1")
    assert (cohort["run_count"], cohort["in_progress_count"]) == (3, 1)

    _write_log(tmp_path, path.name, _game_log(seed="p9", final_floor=40, ended="dead"))
    # An unfinished log is re-indexed at most once per interval.
    clock.now += REINDEX_MIN_INTERVAL_SECONDS
    cohort = _cohort(catalog, "ooc=greedy · Ironclad · a1")
    assert (cohort["run_count"], cohort["in_progress_count"]) == (4, 0)
    assert cohort["avg_global_floor"] == pytest.approx((10 + 20 + 30 + 40) / 4)


def test_unknown_cohort_still_raises_not_found(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    with pytest.raises(CatalogNotFoundError):
        catalog.get_cohort_in_progress("cohort_nope")


# ---------------------------------------------------------------------------
# Opening a game that is still being played
# ---------------------------------------------------------------------------


def test_run_detail_works_on_a_partial_log(tmp_path: Path, log_size: str) -> None:
    _write_log(
        tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=20, padding=4, hp=47)
    )
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    (cohort,) = catalog.list_cohorts()
    (run,) = catalog.get_cohort_in_progress(cohort["cohort_id"])

    payload = catalog.get_run_by_source(run.source_id)

    detail = payload["run"]
    assert payload["view"] == "run"
    assert detail["outcome"]["status"] == "in_progress"
    assert detail["outcome"]["max_global_floor"] == 20
    assert detail["outcome"]["max_floor_label"] == "A2F3"
    assert detail["metadata"]["seed"] == "p1"
    assert detail["metadata"]["experiment"] == "ooc=greedy"
    assert detail["coverage"]["complete_run"] is False
    assert detail["capabilities"]["visited_route"] is True
    # The route so far: one node per room reached, no more.
    assert len(detail["nodes"]) == 20
    assert payload["live"] is True

    # Once its log goes quiet the same run is no longer live.
    clock.now += IN_PROGRESS_STALE_SECONDS + 1
    stale = catalog.get_run_by_source(run.source_id)
    assert stale["run"]["outcome"]["status"] == "in_progress"
    assert stale["live"] is False


def test_finished_run_payload_has_no_live_key(tmp_path: Path, log_size: str) -> None:
    _write_log(tmp_path, "done.jsonl", _game_log(seed="p1", ended="dead"), mtime=NOW)
    catalog = _catalog(tmp_path)
    (source,) = catalog.list_sources()
    assert "live" not in catalog.get_run_by_source(source["source_id"])


# ---------------------------------------------------------------------------
# Over HTTP, as the page calls it
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


def test_http_surfaces_in_progress_runs_everywhere_the_page_reads_them(
    tmp_path: Path, log_size: str
) -> None:
    _finished_batch(tmp_path)
    _write_log(tmp_path, "live.jsonl", _game_log(seed="p9", final_floor=12, hp=50))
    with _server(_catalog(tmp_path)) as base:
        status, cohorts = _get(base, "/api/cohorts")
        assert status == 200
        (cohort,) = cohorts["cohorts"]
        assert cohort["in_progress_count"] == 1

        status, tree = _get(base, "/api/tree")
        (leaf,) = tree["tree"][0]["characters"][0]["cohorts"]
        assert (leaf["run_count"], leaf["in_progress_count"]) == (3, 1)

        status, runs = _get(base, f"/api/cohort/runs?id={cohort['cohort_id']}")
        row = runs["runs"][0]
        assert row["status"] == "in_progress"

        status, run = _get(base, f"/api/run?source={row['source_id']}")
        assert status == 200
        assert run["run"]["outcome"]["status"] == "in_progress"
        assert run["live"] is True

        status, route = _get(base, f"/api/run/map?source={row['source_id']}&act=0")
        assert status == 200
        assert route["summary"]["visited_count"] == 12

        status, metrics = _get(base, f"/api/metrics?current={cohort['cohort_id']}")
        assert metrics["current"]["all_n"] == 3
        assert deepcopy(metrics["current"]["valid_n"]) == 3


# ---------------------------------------------------------------------------
# Re-index throttle: a growing, unfinished log is not re-parsed on every request
# ---------------------------------------------------------------------------

LABEL = "ooc=greedy · Ironclad · a1"


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The name of every log the catalog parses (indexes), in order."""
    names: list[str] = []
    original = catalog_module._scan_jsonl_index

    def counting(path: Path):
        names.append(path.name)
        return original(path)

    monkeypatch.setattr(catalog_module, "_scan_jsonl_index", counting)
    return names


def _grow(path: Path, **kwargs) -> None:
    """Rewrite a log with more rows and a newer mtime, as a running game would."""
    _write_log(path.parent, path.name, _game_log(**kwargs), mtime=NOW - 1.0)


def _live_floor(catalog: RunCatalog) -> int | None:
    _, playing = catalog.get_cohort_runs(_cohort(catalog, LABEL)["cohort_id"])
    (row,) = playing
    return row.global_floor


def test_growing_unfinished_log_is_not_reparsed_within_the_interval(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    path = _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=6))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    assert _live_floor(catalog) == 6
    assert scans == ["live.jsonl"]

    _grow(path, seed="p1", final_floor=9)
    clock.now += REINDEX_MIN_INTERVAL_SECONDS - 1
    for _ in range(3):  # every kind of request goes through the same refresh
        assert _live_floor(catalog) == 6
        catalog.list_cohorts()
        catalog.list_sources()
    assert scans == ["live.jsonl"]

    clock.now += 1
    assert _live_floor(catalog) == 9
    assert scans == ["live.jsonl", "live.jsonl"]
    # The new index is itself throttled from the moment it was made.
    _grow(path, seed="p1", final_floor=12)
    assert _live_floor(catalog) == 9
    assert scans == ["live.jsonl", "live.jsonl"]


def test_cohort_cache_key_changes_when_a_throttled_log_is_reindexed(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    path = _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=6))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    catalog.list_cohorts()
    first_key = catalog._cohort_cache_key

    _grow(path, seed="p1", final_floor=9)
    clock.now += 10
    catalog.list_cohorts()
    assert catalog._cohort_cache_key == first_key  # throttled: nothing to rebuild

    clock.now += REINDEX_MIN_INTERVAL_SECONDS
    catalog.list_cohorts()
    assert catalog._cohort_cache_key != first_key
    assert _live_floor(catalog) == 9


def test_a_game_that_ends_is_finished_within_one_interval(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    _finished_batch(tmp_path)
    path = _write_log(tmp_path, "game.jsonl", _game_log(seed="p9", final_floor=40))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    cohort = _cohort(catalog, LABEL)
    assert (cohort["run_count"], cohort["in_progress_count"]) == (3, 1)

    _grow(path, seed="p9", final_floor=40, ended="dead")
    clock.now += REINDEX_MIN_INTERVAL_SECONDS - 1
    cohort = _cohort(catalog, LABEL)
    assert (cohort["run_count"], cohort["in_progress_count"]) == (3, 1)

    clock.now += 1
    cohort = _cohort(catalog, LABEL)
    assert (cohort["run_count"], cohort["in_progress_count"]) == (4, 0)
    assert cohort["avg_global_floor"] == pytest.approx((10 + 20 + 30 + 40) / 4)


def test_changed_finished_log_is_reindexed_immediately(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    path = _write_log(
        tmp_path,
        "done.jsonl",
        _game_log(seed="s1", final_floor=10, ended="dead"),
        mtime=NOW - 3000.0,
    )
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    assert _cohort(catalog, LABEL)["avg_global_floor"] == 10
    assert scans == ["done.jsonl"]

    _write_log(
        tmp_path,
        path.name,
        _game_log(seed="s1", final_floor=25, ended="dead"),
        mtime=NOW - 2000.0,
    )
    clock.now += 1  # nowhere near the interval
    assert _cohort(catalog, LABEL)["avg_global_floor"] == 25
    assert scans == ["done.jsonl", "done.jsonl"]


def test_new_log_is_indexed_immediately(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    _write_log(tmp_path, "one.jsonl", _game_log(seed="p1"))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    assert _cohort(catalog, LABEL)["in_progress_count"] == 1

    _write_log(tmp_path, "two.jsonl", _game_log(seed="p2"))
    clock.now += 1
    assert _cohort(catalog, LABEL)["in_progress_count"] == 2
    assert scans == ["one.jsonl", "two.jsonl"]


def test_only_the_unfinished_log_waits_when_several_change(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    live = _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=6))
    done = _write_log(
        tmp_path,
        "done.jsonl",
        _game_log(seed="s1", final_floor=10, ended="dead"),
        mtime=NOW - 3000.0,
    )
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    catalog.list_cohorts()
    scans.clear()

    _grow(live, seed="p1", final_floor=9)
    _write_log(
        tmp_path,
        done.name,
        _game_log(seed="s1", final_floor=12, ended="dead"),
        mtime=NOW - 2000.0,
    )
    clock.now += 5
    catalog.list_cohorts()
    assert scans == ["done.jsonl"]


def test_reindex_throttle_can_be_switched_off(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    path = _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=6))
    catalog = _catalog(tmp_path, reindex_min_interval=0)
    assert _live_floor(catalog) == 6

    _grow(path, seed="p1", final_floor=9)
    assert _live_floor(catalog) == 9
    assert scans == ["live.jsonl", "live.jsonl"]


def test_a_clock_that_goes_backwards_does_not_freeze_the_index(
    tmp_path: Path, log_size: str, scans: list[str]
) -> None:
    path = _write_log(tmp_path, "live.jsonl", _game_log(seed="p1", final_floor=6))
    clock = _Clock()
    catalog = _catalog(tmp_path, clock)
    assert _live_floor(catalog) == 6

    _grow(path, seed="p1", final_floor=9)
    clock.now -= 3600
    assert _live_floor(catalog) == 9
