from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.run_workbench.catalog import (
    CatalogNotFoundError,
    RunCatalog,
)


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
