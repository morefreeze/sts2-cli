from __future__ import annotations

import errno
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest

import agent.run_workbench.catalog as catalog_module
from agent.run_workbench.catalog import (
    CatalogNotFoundError,
    RunCatalog,
)
from agent.run_workbench.models import RunStatus


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _native(run_id: str = "native-1") -> dict:
    return {
        "run_id": run_id,
        "build_id": "v1",
        "seed": "seed-native",
        "checkpoint": "ckpt-a",
        "evaluation_mode": "fixed",
        "scenario": "standard",
        "status": "dead",
        "max_global_floor": 7,
        "players": [{"character": "Ironclad"}],
        "map_point_history": [{"id": "n1", "floor": 7}],
    }


def _replay(run_id: str, *, floor: int = 4) -> list[dict]:
    return [
        {
            "run_id": run_id,
            "step": 1,
            "ts": 1,
            "type": "state",
            "data": {
                "decision": "combat_play",
                "context": {"act": 1, "floor": floor},
            },
        }
    ]


def _replay_parser(records: list[dict], source_name: str | None = None) -> dict:
    row = records[0]
    floor = row["data"]["context"]["floor"]
    return {
        "summary": {
            "run_id": row.get("run_id"),
            "character": "Ironclad",
            "game_version": "v1",
            "checkpoint": "ckpt-a",
            "evaluation_mode": "fixed",
            "scenario": "standard",
            "max_global_floor": floor,
        },
        "rooms": [{"id": f"room-{floor}", "global_floor": floor}],
    }


def test_catalog_indexes_mixed_sources_without_normalizing_replays(tmp_path: Path):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "native.run").write_text(json.dumps(_native()), encoding="utf-8")
    _write_jsonl(root / "replay.jsonl", _replay("replay-1"))
    _write_jsonl(
        root / "deck.jsonl",
        [{"event": "outcome", "run_id": "deck-1", "status": "dead", "floor": 6}],
    )
    _write_jsonl(
        root / "eval.jsonl",
        [{"event": "eval_result", "run_id": "eval-1", "status": "win", "max_global_floor": 51}],
    )
    _write_jsonl(
        root / "summary.jsonl",
        [{"event": "summary", "checkpoint": "ckpt-a", "avg_floor": 8.5}],
    )
    (root / "malformed.jsonl").write_text('{"type":"state"}\n{bad\n', encoding="utf-8")

    calls: list[str | None] = []

    def parser(records: list[dict], source_name: str | None = None) -> dict:
        calls.append(source_name)
        return _replay_parser(records, source_name)

    catalog = RunCatalog([root], replay_parser=parser)
    entries = catalog.list_sources()

    assert calls == []
    by_name = {entry["display_name"]: entry for entry in entries}
    assert [entry["display_name"] for entry in entries] == sorted(by_name)
    assert by_name["native.run"]["source_kind"] == "native_run"
    assert by_name["replay.jsonl"]["source_kind"] == "replay_jsonl"
    assert by_name["deck.jsonl"]["source_kind"] == "deck_history"
    assert by_name["eval.jsonl"]["source_kind"] == "eval_results"
    assert by_name["summary.jsonl"]["open_mode"] == "summary"
    assert by_name["malformed.jsonl"]["open_mode"] == "error"
    assert "malformed.jsonl:2" in by_name["malformed.jsonl"]["errors"][0]
    assert by_name["native.run"]["record_count"] == 1
    assert by_name["native.run"]["size"] > 0
    assert isinstance(by_name["native.run"]["mtime"], float)
    assert isinstance(by_name["native.run"]["mtime_ns"], int)
    completeness = by_name["native.run"]["metadata_completeness"]
    assert completeness["present_count"] >= 6
    assert "game_version" in completeness["present_fields"]
    assert all(not value.startswith(str(tmp_path)) for value in (
        entry["source_id"] for entry in entries
    ))


def test_catalog_read_errors_keep_errno_but_never_expose_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "unreadable.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    original_open = Path.open

    def fail_open(path: Path, *args, **kwargs):
        if path == source:
            raise OSError(errno.EACCES, "Permission denied", str(source))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    entry = catalog.list_sources()[0]
    payload = catalog.get_source(entry["source_id"])

    rendered = json.dumps({"entry": entry, "payload": payload})
    assert "unreadable.jsonl" in rendered
    assert "Permission denied" in rendered
    assert f"[Errno {errno.EACCES}]" in rendered
    assert str(tmp_path) not in rendered


def test_catalog_scrubs_absolute_paths_from_adapter_errors(
    tmp_path: Path,
):
    _write_jsonl(tmp_path / "replay.jsonl", _replay("run-1"))

    def leaking_parser(records: list[dict], source_name: str | None = None) -> dict:
        raise RuntimeError(f"could not parse source root {tmp_path}")

    catalog = RunCatalog([tmp_path], replay_parser=leaking_parser)
    entry = catalog.list_sources()[0]

    payload = catalog.get_source(entry["source_id"])

    rendered = json.dumps(payload)
    assert "could not parse" in rendered
    assert str(tmp_path) not in rendered
    assert entry["source_id"] in rendered


def test_catalog_source_ids_are_stable_distinct_and_roots_are_safe(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    outside = tmp_path / "outside"
    first.mkdir()
    second.mkdir()
    outside.mkdir()
    _write_jsonl(first / "same.jsonl", _replay("one"))
    _write_jsonl(second / "same.jsonl", _replay("two"))
    _write_jsonl(outside / "secret.jsonl", _replay("secret"))
    try:
        (first / "linked.jsonl").symlink_to(outside / "secret.jsonl")
    except OSError:
        pass

    catalog = RunCatalog([second, first], replay_parser=_replay_parser)
    original = catalog.list_sources()
    refreshed = catalog.list_sources()

    assert len(original) == 2
    assert len({entry["source_id"] for entry in original}) == 2
    assert [entry["source_id"] for entry in original] == [
        entry["source_id"] for entry in refreshed
    ]
    assert {entry["display_name"] for entry in original} == {
        "first/same.jsonl",
        "second/same.jsonl",
    }
    with pytest.raises(CatalogNotFoundError):
        catalog.get_source("../outside/secret.jsonl")
    with pytest.raises(CatalogNotFoundError):
        catalog.get_source(str(outside / "secret.jsonl"))


def test_source_adaptation_is_lazy_cached_and_invalidated_by_file_change(tmp_path: Path):
    source = _write_jsonl(tmp_path / "replay.jsonl", _replay("run-1"))
    calls = 0

    def parser(records: list[dict], source_name: str | None = None) -> dict:
        nonlocal calls
        calls += 1
        return _replay_parser(records, source_name)

    catalog = RunCatalog([tmp_path], replay_parser=parser)
    source_id = catalog.list_sources()[0]["source_id"]
    assert calls == 0

    first = catalog.get_source(source_id)
    second = catalog.get_source(source_id)
    assert calls == 1
    assert first == second
    assert first["view"] == "run"
    assert first["runs"][0]["source_id"] == source_id

    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run_id": "run-1", "type": "action", "data": {"cmd": "end_turn"}}) + "\n")
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    catalog.get_source(source_id)
    assert calls == 2


def test_refresh_reuses_unchanged_index_records_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _write_jsonl(tmp_path / "replay.jsonl", _replay("run-1"))
    read_calls = 0
    parser_calls = 0
    original_scanner = catalog_module._scan_jsonl_index

    def scanner(path: Path):
        nonlocal read_calls
        read_calls += 1
        return original_scanner(path)

    def parser(records: list[dict], source_name: str | None = None) -> dict:
        nonlocal parser_calls
        parser_calls += 1
        return _replay_parser(records, source_name)

    monkeypatch.setattr(catalog_module, "_scan_jsonl_index", scanner)
    catalog = RunCatalog([tmp_path], replay_parser=parser)

    source_id = catalog.list_sources()[0]["source_id"]
    catalog.list_sources()
    catalog.get_source(source_id)
    catalog.get_source(source_id)
    assert read_calls == 1
    assert parser_calls == 1

    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"run_id": "run-1", "type": "action", "data": {"cmd": "end_turn"}}
            )
            + "\n"
        )
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    catalog.get_source(source_id)
    assert read_calls == 2
    assert parser_calls == 2


def test_mutating_replay_parser_cannot_change_indexed_record_snapshot(tmp_path: Path):
    _write_jsonl(tmp_path / "replay.jsonl", _replay("stable"))
    parser_calls = 0

    def mutating_parser(
        records: list[dict], source_name: str | None = None
    ) -> dict:
        nonlocal parser_calls
        parser_calls += 1
        original_run_id = records[0].pop("run_id")
        original_floor = records[0]["data"]["context"]["floor"]
        records[0]["data"]["context"]["floor"] = 999
        return {
            "summary": {
                "run_id": original_run_id,
                "character": "Ironclad",
                "game_version": "v1",
                "checkpoint": "ckpt-a",
                "evaluation_mode": "fixed",
                "scenario": "standard",
                "ascension": 0,
                "max_global_floor": original_floor,
            },
            "rooms": [{"id": "room", "global_floor": original_floor}],
        }

    catalog = RunCatalog([tmp_path], replay_parser=mutating_parser)
    source_id = catalog.list_sources()[0]["source_id"]
    indexed_before = deepcopy(catalog._sources[source_id].records)

    source_view = catalog.get_source(source_id)
    catalog.list_sources()
    run_view = catalog.get_run("stable")

    assert parser_calls == 1
    assert source_view["runs"][0]["run_id"] == "stable"
    assert run_view["run"]["run_id"] == "stable"
    assert catalog._sources[source_id].records == indexed_before
    assert catalog._sources[source_id].records[0]["run_id"] == "stable"
    assert catalog._sources[source_id].records[0]["data"]["context"]["floor"] == 4


def test_get_run_parses_only_exact_candidate_sources_and_joins(tmp_path: Path):
    _write_jsonl(tmp_path / "wanted.jsonl", _replay("wanted"))
    _write_jsonl(tmp_path / "other.jsonl", _replay("other"))
    calls: list[str | None] = []

    def parser(records: list[dict], source_name: str | None = None) -> dict:
        calls.append(source_name)
        return _replay_parser(records, source_name)

    catalog = RunCatalog([tmp_path], replay_parser=parser)
    result = catalog.get_run("wanted")

    assert result["view"] == "run"
    assert result["run"]["run_id"] == "wanted"
    assert calls == ["wanted.jsonl"]
    with pytest.raises(CatalogNotFoundError, match="unknown run id"):
        catalog.get_run("missing")


def test_get_run_preserves_candidate_adaptation_errors(tmp_path: Path):
    _write_jsonl(tmp_path / "broken-replay.jsonl", _replay("broken"))

    def broken_parser(records: list[dict], source_name: str | None = None) -> dict:
        raise RuntimeError("parser exploded")

    catalog = RunCatalog([tmp_path], replay_parser=broken_parser)

    payload = catalog.get_run("broken")

    assert payload["run"]["run_id"] == "broken"
    assert any("parser exploded" in error for error in payload["errors"])


def test_summary_source_is_explicit_and_never_faked_as_empty_run(tmp_path: Path):
    _write_jsonl(
        tmp_path / "summary.jsonl",
        [{"event": "summary", "checkpoint": "ckpt", "avg_floor": 9}],
    )
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    source_id = catalog.list_sources()[0]["source_id"]

    payload = catalog.get_source(source_id)

    assert payload["view"] == "summary"
    assert payload["summary"]["record_count"] == 1
    assert "rooms" not in payload
    assert "runs" not in payload


def test_cohorts_keep_metadata_axes_separate_and_feed_metrics(tmp_path: Path):
    records = [
        {
            "event": "eval_result",
            "run_id": "a-1",
            "status": "dead",
            "max_global_floor": 8,
            "character": "Ironclad",
            "game_version": "v1",
            "checkpoint": "a",
            "evaluation_mode": "fixed",
            "scenario": "standard",
            "seed": "s1",
        },
        {
            "event": "eval_result",
            "run_id": "a-2",
            "status": "win",
            "max_global_floor": 51,
            "character": "Ironclad",
            "game_version": "v1",
            "checkpoint": "a",
            "evaluation_mode": "fixed",
            "scenario": "standard",
            "seed": "s2",
        },
        {
            "event": "eval_result",
            "run_id": "b-1",
            "status": "dead",
            "max_global_floor": 5,
            "character": "Ironclad",
            "game_version": "v2",
            "checkpoint": "b",
            "evaluation_mode": "fixed",
            "scenario": "standard",
            "seed": "s1",
        },
    ]
    _write_jsonl(tmp_path / "eval.jsonl", records)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    cohorts = catalog.list_cohorts()

    assert len(cohorts) == 2
    assert {cohort["filters"]["game_version"] for cohort in cohorts} == {"v1", "v2"}
    current = next(cohort for cohort in cohorts if cohort["filters"]["checkpoint"] == "a")
    baseline = next(cohort for cohort in cohorts if cohort["filters"]["checkpoint"] == "b")
    assert current["run_count"] == 2
    assert current["run_ids"] == ["a-1", "a-2"]
    metrics = catalog.get_metrics(current["cohort_id"])
    assert metrics["current"]["valid_n"] == 2
    assert metrics["comparison"] is None
    compared = catalog.get_metrics(current["cohort_id"], baseline["cohort_id"])
    assert compared["current"]["valid_n"] == 2
    assert compared["comparison"]["comparable"] is False


def test_ascension_levels_form_distinct_stable_catalog_cohorts(tmp_path: Path):
    records = [
        {
            "event": "eval_result",
            "run_id": f"a{ascension}",
            "status": "dead",
            "max_global_floor": 8,
            "character": "Ironclad",
            "game_version": "v1",
            "checkpoint": "same",
            "evaluation_mode": "fixed",
            "scenario": "standard",
            "ascension": ascension,
            "seed": "same-seed",
        }
        for ascension in (0, 20)
    ]
    _write_jsonl(tmp_path / "eval.jsonl", records)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    first = catalog.list_cohorts()
    second = catalog.list_cohorts()

    assert len(first) == 2
    assert [cohort["cohort_id"] for cohort in first] == [
        cohort["cohort_id"] for cohort in second
    ]
    assert {cohort["filters"]["ascension"] for cohort in first} == {0, 20}
    ascension_labels = {
        next(
            part
            for part in cohort["label"].split(" · ")
            if part.startswith("A")
        )
        for cohort in first
    }
    assert ascension_labels == {"A0", "A20"}
    compared = catalog.get_metrics(first[0]["cohort_id"], first[1]["cohort_id"])
    assert compared["comparison"]["comparable"] is False
    assert any(
        "ascension mismatch" in reason
        for reason in compared["comparison"]["mismatch_reasons"]
    )


def test_metrics_uses_one_cohort_snapshot_for_current_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "event": "eval_result",
                "run_id": checkpoint,
                "status": "dead",
                "max_global_floor": 8,
                "character": "Ironclad",
                "game_version": "v1",
                "checkpoint": checkpoint,
                "evaluation_mode": "fixed",
                "scenario": "standard",
                "ascension": 0,
                "seed": "same",
            }
            for checkpoint in ("current", "baseline")
        ],
    )
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    cohorts = catalog.list_cohorts()
    current_id, baseline_id = (cohort["cohort_id"] for cohort in cohorts)
    build_calls = 0
    original_build = catalog._build_cohorts

    def counted_build():
        nonlocal build_calls
        build_calls += 1
        return original_build()

    monkeypatch.setattr(catalog, "_build_cohorts", counted_build)

    catalog.get_metrics(current_id, baseline_id)

    assert build_calls == 1


def test_catalog_get_run_and_refresh_share_one_locked_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _write_jsonl(tmp_path / "run.jsonl", _replay("stable"))
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    catalog.list_sources()
    reader_ready = Event()
    release_reader = Event()
    reader_result: dict[str, object] = {}
    refresher_result: dict[str, object] = {}
    original_refresh = catalog._refresh

    def controlled_refresh():
        original_refresh()
        if current_thread().name == "catalog-reader":
            reader_ready.set()
            assert release_reader.wait(timeout=2)

    monkeypatch.setattr(catalog, "_refresh", controlled_refresh)

    def read_run():
        try:
            reader_result["payload"] = catalog.get_run("stable")
        except Exception as error:  # captured for deterministic assertion
            reader_result["error"] = error

    def refresh_catalog():
        try:
            refresher_result["sources"] = catalog.list_sources()
        except Exception as error:  # captured for deterministic assertion
            refresher_result["error"] = error

    reader = Thread(target=read_run, name="catalog-reader")
    reader.start()
    assert reader_ready.wait(timeout=2)
    assert catalog._lock.acquire(blocking=False) is False
    source.unlink()
    refresher = Thread(target=refresh_catalog, name="catalog-refresher")
    refresher.start()
    release_reader.set()
    reader.join(timeout=2)
    refresher.join(timeout=2)

    assert not reader.is_alive()
    assert not refresher.is_alive()
    assert "error" not in reader_result
    assert reader_result["payload"]["run"]["run_id"] == "stable"
    assert refresher_result == {"sources": []}


def test_refresh_skips_source_that_disappears_during_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    good = _write_jsonl(tmp_path / "good.jsonl", _replay("good"))
    vanished = _write_jsonl(tmp_path / "vanished.jsonl", _replay("bad"))
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    monkeypatch.setattr(
        catalog, "_discover", lambda: [(tmp_path, good), (tmp_path, vanished)]
    )
    original_stat = Path.stat

    def flaky_stat(path: Path, *args, **kwargs):
        if path == vanished:
            raise FileNotFoundError(2, "No such file", str(vanished))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    entries = catalog.list_sources()

    assert [entry["display_name"] for entry in entries] == ["good.jsonl"]


@pytest.mark.parametrize(
    ("source_name", "text", "view", "source_kind"),
    [
        ("uploaded.run", json.dumps(_native("upload-native")), "run", "native_run"),
        (
            "uploaded.jsonl",
            "\n".join(json.dumps(row) for row in _replay("upload-replay")),
            "run",
            "replay_jsonl",
        ),
        (
            "summary.jsonl",
            json.dumps({"event": "summary", "checkpoint": "a", "avg_floor": 3}),
            "summary",
            "summary",
        ),
    ],
)
def test_uploads_are_parsed_and_adapted_in_memory(
    tmp_path: Path,
    source_name: str,
    text: str,
    view: str,
    source_kind: str,
):
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    payload = catalog.parse_upload(source_name, text)

    assert payload["view"] == view
    assert payload["source_name"] == source_name
    assert payload["source_kind"] == source_kind
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("source_name", "text", "message"),
    [
        ("bad.jsonl", '{"type":"state"}\n{bad', "bad.jsonl:2"),
        ("bad.jsonl", 'NaN\n', "non-standard numeric constant NaN"),
        ("bad.json", '[{"ok": true}, 3]', "bad.json:2: expected an object record"),
    ],
)
def test_uploads_reject_malformed_or_non_object_records(
    tmp_path: Path, source_name: str, text: str, message: str
):
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    payload = catalog.parse_upload(source_name, text)

    assert payload["view"] == "error"
    assert message in payload["errors"][0]


def test_large_deck_history_uses_bounded_index_and_exact_streamed_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 8)
    monkeypatch.setattr(catalog_module, "COHORT_ID_SAMPLE_LIMIT", 5)
    monkeypatch.setattr("agent.run_workbench.metrics.TREND_SAMPLE_LIMIT", 7)
    source = tmp_path / "deck_history.jsonl"
    expected_floors: list[int] = []
    with source.open("w", encoding="utf-8") as handle:
        for index in range(40):
            floor = index % 20 + 1
            expected_floors.append(floor)
            common = {
                "run_id": f"run-{index:03d}",
                "character": "Ironclad",
                "game_version": "2026.08",
                "checkpoint": "model-1",
                "evaluation_mode": "training",
                "scenario": "standard",
                "ascension": 0,
            }
            handle.write(json.dumps({"event": "milestone", "floor": floor, **common}) + "\n")
            handle.write(
                json.dumps(
                    {
                        "event": "card_pick",
                        "floor": floor,
                        "cards": ["Strike"],
                        **common,
                    }
                )
                + "\n"
            )
            if index == 25:
                handle.write("{late malformed json\n")
            handle.write(
                json.dumps(
                    {
                        "event": "outcome",
                        "status": "win" if index == 39 else "dead",
                        "max_floor": floor,
                        "ts": float(index + 1),
                        **common,
                    }
                )
                + "\n"
            )

    scan_calls = 0
    original_scanner = catalog_module._scan_jsonl_index

    def counted_scanner(path: Path):
        nonlocal scan_calls
        scan_calls += 1
        return original_scanner(path)

    monkeypatch.setattr(catalog_module, "_scan_jsonl_index", counted_scanner)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    entry = catalog.list_sources()[0]
    indexed = catalog._sources[entry["source_id"]]
    assert entry["source_kind"] == "deck_history"
    assert entry["open_mode"] == "run"
    assert entry["record_count"] == 120
    assert any("deck_history.jsonl:78" in error for error in entry["errors"])
    assert indexed.records_complete is False
    assert len(indexed.records or ()) == 8
    assert len(indexed.deck_outcomes) == 40

    cohort = catalog.list_cohorts()[0]
    assert cohort["run_count"] == 40
    assert cohort["run_ids_complete"] is False
    assert cohort["run_ids"] == []
    assert len(cohort["representative_run_ids"]) == 5
    assert cohort["latest_at"] == 40.0
    metrics = catalog.get_metrics(cohort["cohort_id"])["current"]
    assert metrics["valid_n"] == 40
    assert metrics["avg_global_floor"] == pytest.approx(sum(expected_floors) / 40)
    assert metrics["median_global_floor"] == 10.5
    assert metrics["max_global_floor"] == 20
    assert metrics["win_n"] == 1
    assert metrics["trend_eligible_n"] == 40
    assert metrics["trend_sampled_n"] == 7
    assert len(metrics["trend"]) == 7

    source_view = catalog.get_source(entry["source_id"])
    assert source_view["view"] == "runs_summary"
    assert source_view["run_count"] == 40
    assert source_view["runs_complete"] is False
    assert len(source_view["representative_run_ids"]) == 5
    run_view = catalog.get_run("run-039")
    assert run_view["run"]["run_id"] == "run-039"
    assert {node["event"] for node in run_view["run"]["nodes"]} == {
        "milestone",
        "card_pick",
        "outcome",
    }
    assert scan_calls == 1
    catalog.list_sources()
    catalog.list_cohorts()
    catalog.get_metrics(cohort["cohort_id"])
    assert scan_calls == 1


def test_workbench_discovery_ignores_unrelated_json_but_accepts_schema_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "unrelated-project"}), encoding="utf-8"
    )
    (tmp_path / "future-training.json").write_text(
        json.dumps({"event": "eval_result", "run_id": "future"}), encoding="utf-8"
    )
    reads: list[str] = []
    original_reader = catalog_module.read_json_records

    def tracked_reader(path: Path):
        reads.append(path.name)
        return original_reader(path)

    monkeypatch.setattr(catalog_module, "read_json_records", tracked_reader)
    catalog = RunCatalog(
        [tmp_path], replay_parser=_replay_parser, include_policy="workbench"
    )

    entries = catalog.list_sources()

    assert [entry["display_name"] for entry in entries] == ["future-training.json"]
    assert reads == ["future-training.json"]


def test_large_compact_deck_merges_with_ordinary_eval_by_exact_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 2)
    _write_jsonl(
        tmp_path / "deck.jsonl",
        [
            {
                "event": "milestone",
                "run_id": "shared",
                "floor_crossed": 5,
                "seed": "fixed-seed",
            },
            {
                "event": "card_pick",
                "run_id": "shared",
                "floor": 6,
                "picked": "BASH",
            },
            {
                "event": "outcome",
                "run_id": "shared",
                "max_floor": 9,
                "won": True,
                "ts": 10,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "event": "eval_result",
                "run_id": "shared",
                "status": "win",
                "max_global_floor": 9,
                "checkpoint": "model-42",
                "character": "Ironclad",
            }
        ],
    )
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    cohort = catalog.list_cohorts()[0]
    records = catalog.get_cohort_records(cohort["cohort_id"])
    metrics = catalog.get_metrics(cohort["cohort_id"])["current"]

    assert cohort["run_count"] == 1
    assert metrics["all_n"] == metrics["valid_n"] == 1
    assert len(records) == 1
    assert records[0].run_id == "shared"
    assert records[0].metadata.checkpoint == "model-42"
    assert records[0].metadata.seed == "fixed-seed"
    assert records[0].capabilities.decisions is True
    assert records[0].outcome.max_global_floor == 9
    assert " | " in records[0].source_id


def test_large_eval_streams_all_outcomes_and_exact_run_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 2)
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "event": "eval_result",
                "run_id": f"eval-{index}",
                "status": "win" if index == 4 else "dead",
                "max_global_floor": index + 3,
                "checkpoint": "model-large",
                "character": "Ironclad",
                "game_version": "v1",
                "evaluation_mode": "fixed",
                "scenario": "standard",
                "ascension": 0,
                "ts": index + 1,
            }
            for index in range(5)
        ],
    )
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    source = catalog.list_sources()[0]
    cohort = catalog.list_cohorts()[0]
    metrics = catalog.get_metrics(cohort["cohort_id"])["current"]
    run = catalog.get_run("eval-4")["run"]

    assert source["source_kind"] == "eval_results"
    assert source["open_mode"] == "run"
    assert source["record_count"] == 5
    assert cohort["run_count"] == 5
    assert metrics["valid_n"] == 5
    assert metrics["win_n"] == 1
    assert metrics["max_global_floor"] == 7
    assert run["run_id"] == "eval-4"
    assert run["metadata"]["checkpoint"] == "model-large"
    assert run["outcome"]["status"] == "win"


def test_large_replay_is_exactly_openable_and_only_terminal_run_joins_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 2)
    terminal = [
        {
            "run_id": "terminal",
            "type": "state",
            "ts": 1,
            "checkpoint": "replay-model",
            "data": {"context": {"act": 1, "floor": 4}},
        },
        {
            "run_id": "terminal",
            "type": "action",
            "ts": 2,
            "data": {"cmd": "end_turn"},
        },
        {
            "run_id": "terminal",
            "type": "state",
            "status": "dead",
            "max_global_floor": 7,
            "ts": 3,
        },
    ]
    in_progress = [
        {
            "run_id": "progress",
            "type": "state",
            "ts": index + 1,
            "data": {"context": {"act": 1, "floor": index + 1}},
        }
        for index in range(3)
    ]
    _write_jsonl(tmp_path / "terminal.jsonl", terminal)
    _write_jsonl(tmp_path / "progress.jsonl", in_progress)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    sources = catalog.list_sources()
    cohorts = catalog.list_cohorts()
    terminal_run = catalog.get_run("terminal")["run"]
    progress_run = catalog.get_run("progress")["run"]

    assert {source["source_kind"] for source in sources} == {"replay_jsonl"}
    assert all(source["open_mode"] == "run" for source in sources)
    assert len(cohorts) == 1
    assert cohorts[0]["run_count"] == 1
    assert terminal_run["run_id"] == "terminal"
    assert terminal_run["outcome"]["status"] == "dead"
    assert progress_run["run_id"] == "progress"
    assert progress_run["outcome"]["status"] == "in_progress"


def test_workbench_discovery_rejects_symlink_before_json_content_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "training.json"
    target.write_text(json.dumps({"event": "eval_result"}), encoding="utf-8")
    link = root / "training.json"
    link.symlink_to(target)
    opened: list[Path] = []
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == link:
            opened.append(path)
            raise AssertionError("content probe followed a symlink")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    catalog = RunCatalog(
        [root], replay_parser=_replay_parser, include_policy="workbench"
    )

    assert catalog.list_sources() == []
    assert opened == []


def test_workbench_json_filter_keeps_boss_summary_and_likely_training_errors(
    tmp_path: Path,
):
    (tmp_path / "boss-decks.json").write_text(
        json.dumps(
            [
                {
                    "checkpoint": "model-1",
                    "cards": ["BASH"],
                    "enemies": ["SLIME_BOSS"],
                    "hp_at_entry": 70,
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "malformed-training.json").write_text(
        "{not-json", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "irrelevant"}), encoding="utf-8"
    )
    (tmp_path / "eval_1.save.meta.json").write_text(
        json.dumps({"checkpoint": "model-1", "save_path": "state.save"}),
        encoding="utf-8",
    )
    (tmp_path / "card_metadata.json").write_text(
        json.dumps({"cards": [{"id": "BASH"}]}), encoding="utf-8"
    )
    catalog = RunCatalog(
        [tmp_path], replay_parser=_replay_parser, include_policy="workbench"
    )

    entries = {entry["display_name"]: entry for entry in catalog.list_sources()}

    assert set(entries) == {"boss-decks.json", "malformed-training.json"}
    assert entries["boss-decks.json"]["source_kind"] == "summary"
    assert entries["boss-decks.json"]["open_mode"] == "summary"
    assert entries["malformed-training.json"]["open_mode"] == "error"
    assert "invalid JSON" in entries["malformed-training.json"]["errors"][0]


def test_large_eval_duplicate_ids_match_small_join_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    records = [
        {
            "event": "eval_result",
            "run_id": "shared",
            "status": "crash",
            "max_global_floor": 3,
            "checkpoint": "model-1",
            "character": "Ironclad",
            "seed": "same",
        },
        {
            "event": "eval_result",
            "run_id": "shared",
            "status": "dead",
            "max_global_floor": 9,
            "checkpoint": "model-1",
            "character": "Silent",
            "seed": "same",
        },
        {
            "event": "eval_result",
            "run_id": "other",
            "status": "dead",
            "max_global_floor": 5,
            "checkpoint": "model-1",
            "character": "Ironclad",
            "seed": "other",
        },
    ]
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small_root.mkdir()
    large_root.mkdir()
    _write_jsonl(small_root / "eval.jsonl", records)
    _write_jsonl(large_root / "eval.jsonl", records)

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 10)
    small = RunCatalog([small_root], replay_parser=_replay_parser)
    small_cohort = small.list_cohorts()[0]
    small_runs = small.get_cohort_records(small_cohort["cohort_id"])
    small_metrics = small.get_metrics(small_cohort["cohort_id"])["current"]

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 1)
    large = RunCatalog([large_root], replay_parser=_replay_parser)
    large_cohort = large.list_cohorts()[0]
    large_runs = large.get_cohort_records(large_cohort["cohort_id"])
    large_metrics = large.get_metrics(large_cohort["cohort_id"])["current"]

    small_shared = next(run for run in small_runs if run.run_id == "shared")
    large_shared = next(run for run in large_runs if run.run_id == "shared")
    assert len(small_runs) == len(large_runs) == 2
    assert large_shared.outcome == small_shared.outcome
    assert large_shared.metadata == small_shared.metadata
    assert large_shared.outcome.status is RunStatus.CRASH
    assert large_shared.metadata.character == "Ironclad"
    assert any("conflicting metadata character" in warning for warning in large_shared.warnings)
    for field in (
        "all_n",
        "valid_n",
        "technical_n",
        "avg_global_floor",
        "median_global_floor",
        "max_global_floor",
    ):
        assert large_metrics[field] == small_metrics[field]


def test_large_anonymous_replay_matches_small_single_run_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    records = [
        {
            "type": "state",
            "ts": 1,
            "character": "Ironclad",
            "checkpoint": "replay-model",
            "data": {"context": {"act": 1, "floor": 2}},
        },
        {
            "type": "action",
            "ts": 2,
            "data": {"cmd": "end_turn"},
        },
        {
            "type": "state",
            "ts": 3,
            "status": "dead",
            "max_global_floor": 7,
        },
    ]

    def parser(rows: list[dict], source_name: str | None = None) -> dict:
        return {
            "summary": {
                "character": "Ironclad",
                "checkpoint": "replay-model",
                "max_global_floor": 7,
            },
            "rooms": [{"id": "room-7", "global_floor": 7}],
        }

    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small_root.mkdir()
    large_root.mkdir()
    _write_jsonl(small_root / "replay.jsonl", records)
    _write_jsonl(large_root / "replay.jsonl", records)

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 10)
    small = RunCatalog([small_root], replay_parser=parser)
    small_cohort = small.list_cohorts()[0]
    small_runs = small.get_cohort_records(small_cohort["cohort_id"])

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 1)
    large = RunCatalog([large_root], replay_parser=parser)
    large_cohort = large.list_cohorts()[0]
    large_runs = large.get_cohort_records(large_cohort["cohort_id"])

    assert small_cohort["run_count"] == large_cohort["run_count"] == 1
    assert len(small_runs) == len(large_runs) == 1
    assert large_runs[0].run_id == small_runs[0].run_id == ""
    assert large_runs[0].metadata == small_runs[0].metadata
    assert large_runs[0].outcome == small_runs[0].outcome
    assert large_runs[0].metadata.started_at == 1.0
    assert large_runs[0].metadata.ended_at == 3.0
    assert large_runs[0].metadata.checkpoint == "replay-model"
    assert large_runs[0].outcome.status is RunStatus.DEAD
    assert large_runs[0].outcome.max_global_floor == 7


def test_large_anonymous_deck_rows_remain_distinct_like_small_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    records = [
        {"event": "outcome", "status": "dead", "max_floor": floor, "ts": floor}
        for floor in (5, 9)
    ]
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small_root.mkdir()
    large_root.mkdir()
    _write_jsonl(small_root / "deck.jsonl", records)
    _write_jsonl(large_root / "deck.jsonl", records)

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 10)
    small = RunCatalog([small_root], replay_parser=_replay_parser)
    small_cohort = small.list_cohorts()[0]

    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 1)
    large = RunCatalog([large_root], replay_parser=_replay_parser)
    large_cohort = large.list_cohorts()[0]

    assert small_cohort["run_count"] == large_cohort["run_count"] == 2
    assert sorted(
        run.outcome.max_global_floor
        for run in large.get_cohort_records(large_cohort["cohort_id"])
    ) == [5, 9]


def test_large_boss_summary_keeps_summary_view_with_bounded_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rows = [
        {
            "checkpoint": f"model-{index}",
            "cards": ["BASH"],
            "enemies": ["SLIME_BOSS"],
            "hp_at_entry": 70 - index,
        }
        for index in range(3)
    ]
    _write_jsonl(tmp_path / "boss-summary.jsonl", rows)
    monkeypatch.setattr(catalog_module, "INDEX_RECORD_LIMIT", 2)
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)

    source = catalog.list_sources()[0]
    payload = catalog.get_source(source["source_id"])

    assert source["source_kind"] == "summary"
    assert source["record_count"] == 3
    assert payload["view"] == "summary"
    assert payload["summary"]["record_count"] == 3
    assert payload["summary"]["records_complete"] is False
    assert payload["summary"]["record_sample_limit"] == 2
    assert payload["summary"]["record_sampling_method"] == "prefix"
    assert len(payload["summary"]["records"]) == 2
    assert "run_count" not in payload
