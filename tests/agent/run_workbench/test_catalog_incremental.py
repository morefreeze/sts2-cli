"""Behaviour of the catalog's caches: what is reused, what is recomputed.

The workbench runs next to a day-long experiment, so its logs keep growing
while it serves requests.  These tests pin that an unchanged source or cohort
is not re-derived when another one changes, and that every cache returns
exactly what a catalog built from scratch would.
"""

from __future__ import annotations

import json
import random
from copy import deepcopy
from pathlib import Path

import pytest

import agent.run_workbench.catalog as catalog_module
from agent.run_progress_viewer import parse_game_progress
from agent.run_workbench.catalog import RunCatalog
from agent.run_workbench.joiner import join_records
from agent.run_workbench.metrics import summarize_cohort
from agent.run_workbench.models import (
    Capabilities,
    Coverage,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    stat = path.stat()
    # Guarantee a new cache key even on a coarse-grained clock.
    import os

    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


def _deck_history(prefix: str, runs: int = 300) -> list[dict]:
    """Two rows per run: over INDEX_RECORD_LIMIT, so only compact outcomes."""
    rows = []
    for index in range(runs):
        common = {
            "run_id": f"{prefix}{index}",
            "character": "Ironclad",
            "ascension": 0,
            "seed": f"seed-{prefix}{index}",
            "game_version": "v1",
        }
        rows.append({"event": "milestone", "floor": 3, "ts": 100.0 + index, **common})
        rows.append(
            {
                "event": "outcome",
                "status": "dead",
                "max_floor": 5 + index % 7,
                "ts": 101.0 + index,
                **common,
            }
        )
    return rows


def _replay(
    run_id: str | None,
    *,
    experiment: str,
    seed: str,
    floors: int = 4,
    pad_states: int = 0,
    ts: float = 1_000.0,
) -> list[dict]:
    """A GameLogger-style log; ``run_id=None`` makes it a run-id-less legacy log."""
    ident = {} if run_id is None else {"run_id": run_id}
    rows: list[dict] = [
        {
            "type": "run_meta",
            "ts": ts,
            "experiment": experiment,
            "character": "Ironclad",
            "seed": seed,
            "ascension": 0,
            **ident,
        },
        {
            "type": "action",
            "step": 0,
            "ts": ts + 1,
            "data": {"cmd": "start_run", "character": "Ironclad", "seed": seed},
            **ident,
        },
    ]
    step = 1
    for floor in range(1, floors + 1):
        rows.append(
            {
                "type": "state",
                "step": step,
                "ts": ts + 1 + step,
                "data": {
                    "decision": "map_select",
                    "context": {"act": 1, "floor": floor, "room_type": "Map"},
                    "player": {"hp": 70, "max_hp": 80},
                },
                **ident,
            }
        )
        step += 1
    for _ in range(pad_states):
        rows.append(
            {
                "type": "state",
                "step": step,
                "ts": ts + 1 + step,
                "data": {
                    "decision": "map_select",
                    "context": {"act": 1, "floor": floors, "room_type": "Map"},
                    "player": {"hp": 70, "max_hp": 80},
                },
                **ident,
            }
        )
        step += 1
    rows.append(
        {
            "type": "state",
            "step": step,
            "ts": ts + 2 + step,
            "data": {
                "decision": "game_over",
                "victory": False,
                "context": {"act": 1, "floor": floors, "room_type": "Monster"},
                "player": {"hp": 0, "max_hp": 80},
            },
            **ident,
        }
    )
    return rows


class _Counters:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.scans = 0
        self.adapts = 0
        self.derives = 0
        scan = catalog_module._scan_jsonl_index
        adapt = catalog_module.adapt_records
        derive = RunCatalog._derive_cohort

        def counted_scan(path):
            self.scans += 1
            return scan(path)

        def counted_adapt(*args, **kwargs):
            self.adapts += 1
            return adapt(*args, **kwargs)

        def counted_derive(catalog, *args, **kwargs):
            self.derives += 1
            return derive(catalog, *args, **kwargs)

        monkeypatch.setattr(catalog_module, "_scan_jsonl_index", counted_scan)
        monkeypatch.setattr(catalog_module, "adapt_records", counted_adapt)
        monkeypatch.setattr(RunCatalog, "_derive_cohort", counted_derive)

    def reset(self) -> None:
        self.scans = self.adapts = self.derives = 0


def _fresh(root: Path) -> RunCatalog:
    return RunCatalog([root], replay_parser=parse_game_progress)


def test_growing_one_source_only_recomputes_that_source_and_its_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(
        tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa", ts=2_000.0)
    )
    _write(
        tmp_path / "b.jsonl", _replay("run-b", experiment="exp-b", seed="sb", ts=3_000.0)
    )
    counters = _Counters(monkeypatch)
    catalog = _fresh(tmp_path)

    first = catalog.list_cohorts()
    assert len(first) == 3
    assert (counters.scans, counters.adapts, counters.derives) == (3, 2, 3)

    counters.reset()
    assert catalog.list_cohorts() == first
    assert (counters.scans, counters.adapts, counters.derives) == (0, 0, 0)

    _append(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa")[-1])
    counters.reset()
    second = catalog.list_cohorts()

    # One file re-read, one re-adapted, one cohort re-derived: the other
    # replay's cohort and the 300-run deck history cohort are reused.
    assert (counters.scans, counters.adapts, counters.derives) == (1, 1, 1)
    assert second == _fresh(tmp_path).list_cohorts()
    unchanged = [c for c in first if c["experiment"] != "exp-a"]
    assert all(c in second for c in unchanged)


def test_run_id_less_log_growing_does_not_rederive_other_record_cohorts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run-id-less (historical) log sends every ordinary record through
    # `join_records`, which returns brand-new objects each time.
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    _write(tmp_path / "b.jsonl", _replay("run-b", experiment="exp-b", seed="sb"))
    _write(
        tmp_path / "anon.jsonl",
        _replay(None, experiment="exp-anon", seed="sa", ts=1_000.0),
    )
    counters = _Counters(monkeypatch)
    catalog = _fresh(tmp_path)
    first = catalog.list_cohorts()
    assert counters.derives == len(first) == 4

    _append(tmp_path / "anon.jsonl", _replay(None, experiment="exp-anon", seed="sa")[-1])
    counters.reset()
    second = catalog.list_cohorts()

    assert counters.derives == 1  # only exp-anon
    assert second == _fresh(tmp_path).list_cohorts()


def test_shared_run_id_group_is_not_rejoined_when_an_unrelated_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "run-a" appears in a replay and in a big deck history: one joined record.
    deck = _deck_history("d")
    deck[0]["run_id"] = deck[1]["run_id"] = "run-a"
    _write(tmp_path / "deck.jsonl", deck)
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    _write(tmp_path / "other.jsonl", _replay("run-o", experiment="exp-o", seed="so"))
    catalog = _fresh(tmp_path)
    catalog.list_cohorts()
    joins = 0
    real_join = catalog_module._join_catalog_group

    def counted_join(items):
        nonlocal joins
        joins += 1
        return real_join(items)

    monkeypatch.setattr(catalog_module, "_join_catalog_group", counted_join)

    _append(tmp_path / "other.jsonl", _replay("run-o", experiment="exp-o", seed="so")[-1])
    second = catalog.list_cohorts()

    assert joins == 1  # run-o's single record; run-a's joined group is reused
    assert second == _fresh(tmp_path).list_cohorts()
    joined = catalog.get_run("run-a")["run"]
    assert "|" in joined["source_id"]


def test_removed_and_added_sources_match_a_fresh_catalog(tmp_path: Path) -> None:
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    _write(tmp_path / "b.jsonl", _replay("run-b", experiment="exp-b", seed="sb"))
    catalog = _fresh(tmp_path)
    catalog.list_cohorts()

    (tmp_path / "b.jsonl").unlink()
    _write(tmp_path / "c.jsonl", _replay("run-c", experiment="exp-c", seed="sc"))
    _write(tmp_path / "anon.jsonl", _replay(None, experiment="exp-a", seed="sa"))

    cohorts = catalog.list_cohorts()
    fresh = _fresh(tmp_path)
    assert cohorts == fresh.list_cohorts()
    for cohort in cohorts:
        assert catalog.get_metrics(cohort["cohort_id"]) == fresh.get_metrics(
            cohort["cohort_id"]
        )


def test_metrics_reuse_the_cohort_summary_computed_at_build_time(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    catalog = _fresh(tmp_path)

    for cohort in catalog.list_cohorts():
        items = catalog._cohort_records[cohort["cohort_id"]]
        expected = summarize_cohort(catalog._iter_cohort_records(items)).to_dict()
        assert catalog.get_metrics(cohort["cohort_id"])["current"] == expected


def test_get_cohort_records_still_hands_out_private_copies(tmp_path: Path) -> None:
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    catalog = _fresh(tmp_path)
    cohort = catalog.list_cohorts()[0]

    record = catalog.get_cohort_records(cohort["cohort_id"])[0]
    record.nodes.append({"tampered": True})
    record.metadata = RunMetadata(seed="tampered")

    again = catalog.get_cohort_records(cohort["cohort_id"])[0]
    assert {"tampered": True} not in again.nodes
    assert again.metadata.seed == "sa"
    assert catalog.list_cohorts()[0] == cohort


# --------------------------------------------------------------------- scan


def _scan(path: Path) -> catalog_module._JsonlScan:
    return catalog_module._scan_jsonl_index(path)


def test_scan_keeps_every_record_only_for_replay_like_files(tmp_path: Path) -> None:
    limit = catalog_module.INDEX_RECORD_LIMIT
    replay = _write(
        tmp_path / "replay.jsonl", _replay("r", experiment="e", seed="s", pad_states=limit)
    )
    scan = _scan(replay)
    assert scan.replay_records is not None
    assert len(scan.replay_records) == scan.descriptor.record_count > limit

    deck = _write(tmp_path / "deck.jsonl", _deck_history("d"))
    assert _scan(deck).replay_records is None

    # State rows that only show up after the indexed prefix: too late to have
    # been retaining, so the file is re-read (and still parsed the same way).
    late = _write(
        tmp_path / "late.jsonl",
        _deck_history("x", runs=limit // 2 + 5)
        + _replay("r", experiment="e", seed="s"),
    )
    assert _scan(late).replay_records is None

    small = _write(tmp_path / "small.jsonl", _replay("r", experiment="e", seed="s"))
    assert _scan(small).replay_records is None  # complete sources keep `records`


def test_big_replay_is_parsed_from_the_scan_without_a_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = catalog_module.INDEX_RECORD_LIMIT
    log = _write(
        tmp_path / "big.jsonl",
        _replay("run-big", experiment="exp", seed="sb", pad_states=limit + 20),
    )
    parsed_sizes: list[int] = []

    def parser(rows: list[dict], name: str | None = None) -> dict:
        parsed_sizes.append(len(rows))
        return parse_game_progress(rows, name)

    opens = 0
    original_open = Path.open

    def counted_open(path: Path, *args, **kwargs):
        nonlocal opens
        if path == log:
            opens += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    catalog = RunCatalog([tmp_path], replay_parser=parser)
    single_read = catalog.list_cohorts()
    single_read_source = catalog.list_sources()

    assert opens == 1
    assert len(parsed_sizes) == 1 and parsed_sizes[0] > limit

    # Same answers when the scan cannot hand its records over.
    real_scan = catalog_module._scan_jsonl_index
    monkeypatch.setattr(
        catalog_module,
        "_scan_jsonl_index",
        lambda path: __import__("dataclasses").replace(
            real_scan(path), replay_records=None
        ),
    )
    opens = 0
    reread = RunCatalog([tmp_path], replay_parser=parser)
    assert reread.list_cohorts() == single_read
    assert reread.list_sources() == single_read_source
    assert opens == 2


def test_incomplete_sources_keep_no_raw_prefix_except_summaries(tmp_path: Path) -> None:
    limit = catalog_module.INDEX_RECORD_LIMIT
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(
        tmp_path / "replay.jsonl",
        _replay("r", experiment="e", seed="s", pad_states=limit),
    )
    _write(
        tmp_path / "summary.jsonl",
        [{"event": "summary", "index": index} for index in range(limit + 50)],
    )
    catalog = _fresh(tmp_path)
    by_name = {
        source["display_name"]: source["source_id"] for source in catalog.list_sources()
    }

    for name in ("deck.jsonl", "replay.jsonl"):
        indexed = catalog._sources[by_name[name]]
        assert indexed.records_complete is False
        assert indexed.records is None
    summary = catalog._sources[by_name["summary.jsonl"]]
    assert summary.records is not None and len(summary.records) == limit
    view = catalog.get_source(by_name["summary.jsonl"])
    assert view["view"] == "summary"
    assert len(view["summary"]["records"]) == limit
    # Incomplete run sources still open and resolve their runs by re-scanning.
    assert catalog.get_source(by_name["deck.jsonl"])["view"] == "runs_summary"
    assert catalog.get_run("r")["run"]["run_id"] == "r"


# ------------------------------------------------------------------- merging


def _reference_merge(ordinary, compact):
    """`_merge_compact_records` as it was before untouched compacts were kept."""
    identified: dict[str, list] = {}
    historical: list = []
    for item in [*ordinary, *compact]:
        if item.run_id:
            identified.setdefault(item.run_id, []).append(item)
        else:
            historical.append(item)
    merged = []
    for run_id in sorted(identified):
        group = identified[run_id]
        if len(group) == 1 and isinstance(group[0], catalog_module._CompactRun):
            merged.append(group[0])
        else:
            merged.append(catalog_module._join_catalog_group(group))
    if not historical:
        return merged
    return join_records(
        [
            *(i.to_record() if hasattr(i, "to_record") else i for i in merged),
            *(i.to_record() if hasattr(i, "to_record") else i for i in historical),
        ]
    )


def _as_record(item):
    return item.to_record() if isinstance(item, catalog_module._CompactRun) else item


def _signature(record: RunRecord):
    return (
        record,
        record.comparison_conflicts,
        record._node_provenance_index,
        record.warnings,
    )


def _random_items(rng: random.Random):
    seeds = ["s1", "s2", None, ""]
    ids = [f"r{i}" for i in range(rng.randint(2, 7))]

    def times():
        start = round(rng.uniform(0, 30), 1)
        return start, round(start + rng.uniform(0, 30), 1)

    def meta():
        started, ended = times()
        return dict(
            seed=rng.choice(seeds),
            character=rng.choice(["Ironclad", None]),
            experiment=rng.choice(["e1", None]),
            started_at=started,
            ended_at=ended,
        )

    def compact(run_id: str, source: str):
        item = catalog_module._CompactRun(
            run_id=run_id,
            source_id=source,
            source_kind=rng.choice([SourceKind.DECK_HISTORY, SourceKind.EVAL_RESULTS]),
            status=rng.choice([RunStatus.DEAD, RunStatus.WIN]),
            max_global_floor=rng.randint(1, 30),
            **meta(),
        )
        item.has_outcome = True
        return item

    def record(run_id: str, source: str):
        m = meta()
        return RunRecord(
            run_id=run_id,
            source_id=source,
            source_kind=SourceKind.REPLAY_JSONL,
            metadata=RunMetadata(**m),
            outcome=RunOutcome(status=RunStatus.DEAD, max_global_floor=rng.randint(1, 30)),
            coverage=Coverage(complete_run=True),
            capabilities=Capabilities(),
            nodes=[{"id": f"{source}-n{i}", "floor": i} for i in range(rng.randint(0, 3))],
            replay_by_node={"n": {"actions": [1]}} if rng.random() < 0.3 else {},
        )

    ordinary = [record(rng.choice(ids + [""]), f"o{i}") for i in range(rng.randint(0, 4))]
    compacts = [compact(rng.choice(ids + [""]), f"c{i}") for i in range(rng.randint(0, 9))]
    return ordinary, compacts


@pytest.mark.parametrize("seed", range(150))
def test_merge_keeping_untouched_compacts_equals_the_full_join(seed: int) -> None:
    rng = random.Random(seed)
    ordinary, compacts = _random_items(rng)

    actual = catalog_module._merge_compact_records(
        deepcopy(ordinary), deepcopy(compacts)
    )
    expected = _reference_merge(deepcopy(ordinary), deepcopy(compacts))

    assert [_signature(_as_record(item)) for item in actual] == [
        _signature(_as_record(item)) for item in expected
    ]
    # Whatever stayed compact is something the join would have left alone.
    assert all(
        not isinstance(item, catalog_module._CompactRun) or item.run_id
        for item in actual
    )


def test_list_cohorts_returns_a_private_copy_every_time(tmp_path: Path) -> None:
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    catalog = _fresh(tmp_path)

    first = catalog.list_cohorts()
    pristine = deepcopy(first)
    for cohort in first:
        cohort["label"] = "tampered"
        cohort["filters"]["character"] = "tampered"
        cohort["run_ids"].append("tampered")
        cohort["comparison_readiness"].clear()

    assert catalog.list_cohorts() == pristine  # served from the cache
    _append(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa")[-1])
    rebuilt = catalog.list_cohorts()  # rebuilt, reusing memoised descriptors
    for cohort in rebuilt:
        cohort["filters"]["character"] = "tampered"
    assert [c["filters"]["character"] for c in catalog.list_cohorts()] == [
        c["filters"]["character"] for c in pristine
    ]


def test_compact_run_is_joined_only_when_it_overlaps_a_run_id_less_record() -> None:
    def compact(run_id: str, seed: str, start: float, end: float):
        item = catalog_module._CompactRun(
            run_id=run_id,
            source_id=f"src-{run_id}",
            source_kind=SourceKind.DECK_HISTORY,
            status=RunStatus.DEAD,
            seed=seed,
            started_at=start,
            ended_at=end,
        )
        item.has_outcome = True
        return item

    anonymous = RunRecord(
        run_id="",
        source_id="src-anon",
        source_kind=SourceKind.REPLAY_JSONL,
        metadata=RunMetadata(seed="s", started_at=10.0, ended_at=20.0),
        outcome=RunOutcome(status=RunStatus.DEAD),
    )
    overlapping = compact("overlap", "s", 15.0, 25.0)
    disjoint = compact("disjoint", "s", 100.0, 110.0)
    other_seed = compact("other", "t", 10.0, 20.0)

    merged = catalog_module._merge_compact_records(
        [anonymous], [overlapping, disjoint, other_seed]
    )
    by_id = {item.run_id: item for item in merged}

    assert isinstance(by_id["disjoint"], catalog_module._CompactRun)
    assert isinstance(by_id["other"], catalog_module._CompactRun)
    joined = by_id["overlap"]
    assert isinstance(joined, RunRecord)
    assert any("src-anon" in warning for warning in joined.warnings)
    assert any("src-overlap" in warning for warning in by_id[""].warnings)
    # Identical to joining everything, which is what used to happen.
    reference = _reference_merge([anonymous], [overlapping, disjoint, other_seed])
    assert [_signature(_as_record(i)) for i in merged] == [
        _signature(_as_record(i)) for i in reference
    ]


def test_indexing_pauses_the_cyclic_collector_and_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gc

    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    seen: list[bool] = []
    real_scan = catalog_module._scan_jsonl_index

    def watching_scan(path):
        seen.append(gc.isenabled())
        return real_scan(path)

    monkeypatch.setattr(catalog_module, "_scan_jsonl_index", watching_scan)
    assert gc.isenabled()
    catalog = _fresh(tmp_path)
    catalog.list_cohorts()
    assert seen == [False]
    assert gc.isenabled()

    # ... also when indexing blows up, and when the collector was off already.
    _write(tmp_path / "b.jsonl", _replay("run-b", experiment="exp-b", seed="sb"))
    monkeypatch.setattr(
        catalog_module,
        "_scan_jsonl_index",
        lambda path: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        catalog.list_cohorts()
    assert gc.isenabled()
    gc.disable()
    try:
        monkeypatch.setattr(catalog_module, "_scan_jsonl_index", real_scan)
        catalog.list_cohorts()
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_refresh_with_nothing_changed_keeps_the_run_index(tmp_path: Path) -> None:
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    _write(tmp_path / "b.jsonl", _replay("run-b", experiment="exp-b", seed="sb"))
    catalog = _fresh(tmp_path)
    catalog.list_sources()
    index, sources = catalog._run_sources, catalog._sources

    catalog.list_sources()
    assert catalog._run_sources is index and catalog._sources is sources

    _append(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa")[-1])
    catalog.list_sources()
    assert catalog._run_sources is not index
    assert set(catalog._run_sources) == {"run-a", "run-b"}

    (tmp_path / "b.jsonl").unlink()
    catalog.list_sources()
    assert set(catalog._run_sources) == {"run-a"}
    assert len(catalog._sources) == 1


def test_metrics_helpers_leave_the_stored_cohort_records_untouched(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "deck.jsonl", _deck_history("d"))
    _write(tmp_path / "a.jsonl", _replay("run-a", experiment="exp-a", seed="sa"))
    _write(tmp_path / "b.jsonl", _replay("run-b", experiment="exp-a", seed="sb"))
    _write(tmp_path / "anon.jsonl", _replay(None, experiment="exp-a", seed="sa"))
    catalog = _fresh(tmp_path)
    cohorts = catalog.list_cohorts()
    stored = {
        cohort["cohort_id"]: [
            deepcopy(item) for item in catalog._cohort_records[cohort["cohort_id"]]
        ]
        for cohort in cohorts
    }
    assert any(
        isinstance(item, RunRecord) for items in stored.values() for item in items
    )

    for current in cohorts:
        for baseline in [None, *cohorts]:
            catalog.get_metrics(
                current["cohort_id"], baseline["cohort_id"] if baseline else None
            )

    for cohort_id, before in stored.items():
        after = catalog._cohort_records[cohort_id]
        assert [_item_state(item) for item in after] == [
            _item_state(item) for item in before
        ]


def _item_state(item):
    if isinstance(item, catalog_module._CompactRun):
        return item
    return (item, item.comparison_conflicts, item._node_provenance_index)
