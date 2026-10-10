"""Traversal-safe, lazy catalog for training workbench run artifacts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import gc
from hashlib import sha256
import json
import math
import operator
from pathlib import Path
import re
import stat as stat_module
from threading import RLock
import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Sequence

from agent.run_metadata import (
    MAX_RUN_ID_LENGTH,
    is_unicode_scalar_text,
    record_run_id,
    safe_run_id as _safe_run_id,
)

from .adapters import AdaptedSource, adapt_records
from .joiner import _plausibly_overlap, join_records
from .metrics import (
    CohortSummary,
    compare_cohorts,
    describe_comparison_readiness,
    summarize_cohort,
)
from .models import (
    Capabilities,
    COMPARISON_METADATA_FIELDS,
    Coverage,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)
from .recorded_maps import RecordedMapError, parse_recorded_map_row
from .sources import SourceDescriptor, SourceFormatError, classify_records, read_json_records


SUPPORTED_SUFFIXES = frozenset({".run", ".json", ".jsonl"})
# A single-run replay log that has no terminal state yet counts as an
# in-progress game (not as an anonymous unfinished source) only while its file
# is still being written: modified within this many seconds of "now".  An
# unfinished log older than that keeps the classification it always had.
IN_PROGRESS_STALE_SECONDS = 30 * 60
# A replay log that was still unfinished when it was last indexed and has
# changed since is re-indexed at most this often.  Indexing parses the whole
# file, and a running experiment appends to every live log continuously, so
# without a floor each request re-parses several tens of MB per log.  A
# finished source that changes, and a source seen for the first time, are
# always indexed straight away.
REINDEX_MIN_INTERVAL_SECONDS = 60.0
INDEX_RECORD_LIMIT = 512
COHORT_ID_SAMPLE_LIMIT = 100
SOURCE_REF_LIMIT = 32
REPLAY_WARNING_ID_LIMIT = 16
RUN_ID_LENGTH_LIMIT = MAX_RUN_ID_LENGTH
ERROR_DETAIL_LIMIT = 32
_RECORDED_MAP_WARNING_LIMIT = 32
_RECORDED_MAP_WARNING_CHARS = 160
_WORKBENCH_JSON_PROBE_BYTES = 64 * 1024
_WORKBENCH_JSON_MARKER_GROUPS = (
    (b'"players"', b'"map_point_history"'),
    (b'"event"', b'"run_id"'),
    (b'"type"', b'"run_id"'),
    (b'"rooms"', b'"run_id"'),
)
_BOSS_DECK_JSON_MARKERS = (
    b'"checkpoint"',
    b'"cards"',
    b'"enemies"',
    b'"hp_at_entry"',
)
_WORKBENCH_FILENAME_TOKENS = frozenset(
    {
        "checkpoint",
        "cohort",
        "eval",
        "evaluation",
        "replay",
        "run",
        "runs",
        "summary",
        "training",
    }
)
_METADATA_FIELDS = (
    "character",
    "seed",
    "game_version",
    "checkpoint",
    "evaluation_mode",
    "scenario",
    "ascension",
)


class CatalogError(ValueError):
    """Base class for stable client-facing catalog errors."""


class CatalogNotFoundError(CatalogError):
    """Raised when an opaque catalog identity is not known."""


@dataclass(frozen=True)
class _IndexedSource:
    source_id: str
    root: Path
    path: Path
    entry: dict[str, Any]
    descriptor: SourceDescriptor
    records: tuple[dict[str, Any], ...] | None
    records_complete: bool
    deck_outcomes: tuple["_CompactRun", ...]
    run_ids: tuple[str, ...]
    cache_key: tuple[Path, int, int]
    # Catalog clock reading when indexing finished (see `_reindex_deferred`).
    indexed_at: float = 0.0


@dataclass(slots=True)
class _GameVersionSourceEvidence:
    value: str | None = None
    blocked: bool = False

    def observe(self, candidate: object) -> None:
        if candidate is None:
            return
        if type(candidate) is not str:
            self.blocked = True
            return
        normalized = candidate.strip()
        if not normalized:
            return
        if not is_unicode_scalar_text(normalized):
            self.blocked = True
            return
        if self.value is None:
            self.value = normalized
        elif self.value != normalized:
            self.blocked = True

    def merge(self, other: "_GameVersionSourceEvidence") -> None:
        self.blocked = self.blocked or other.blocked
        self.observe(other.value)

    def resolved(self) -> str | None:
        return None if self.blocked else self.value


@dataclass(slots=True)
class _CompactRun:
    """One outcome and scalar metadata, never per-room/card evidence."""

    run_id: str
    source_id: str = ""
    source_kind: SourceKind = SourceKind.DECK_HISTORY
    character: str | None = None
    seed: str | None = None
    game_version: str | None = None
    game_version_source: str | None = None
    experiment: str | None = None
    checkpoint: str | None = None
    evaluation_mode: str | None = None
    scenario: str | None = None
    ascension: int | None = None
    modifiers: tuple[str, ...] = ()
    is_multiplayer: bool | None = None
    has_is_multiplayer_observation: bool = False
    is_multiplayer_conflicted: bool = False
    started_at: float | None = None
    ended_at: float | None = None
    status: RunStatus = RunStatus.UNKNOWN
    victory: bool | None = None
    max_global_floor: int | None = None
    max_floor_label: str | None = None
    technical_failure_kind: str | None = None
    first_recorded_floor: int | None = None
    observed_max_floor: int | None = None
    observed_max_floor_label: str | None = None
    outcome_max_floor: int | None = None
    latest_timestamp: float | None = None
    # (act, act-local floor, hp, max_hp) of the newest replay state row seen;
    # only ever read for a game that is still being played.
    live_state: tuple[int | None, int | None, int | None, int | None] | None = None
    has_outcome: bool = False
    has_floor: bool = False
    has_card_pick: bool = False
    has_valid_recorded_map: bool = False
    recorded_map_count: int = 0
    latest_recorded_act_floors: dict[int, tuple[int | float, int]] = field(
        default_factory=dict
    )
    latest_recorded_act_decisions: dict[
        int, tuple[int | float, bool]
    ] = field(default_factory=dict)
    has_replay_action: bool = False
    has_replay_state: bool = False
    replay_parser_succeeded: bool = False
    replay_parser_rejected: bool = False
    replay_parser_complete_run: bool | None = None
    has_replay_nodes: bool = False
    has_node_decisions: bool = False
    usable_per_node_replay: bool = False
    replay_observed_ids: tuple[str, ...] = ()
    replay_ids_omitted: bool = False
    warnings: tuple[str, ...] = ()
    comparison_conflicts: set[str] = field(default_factory=set)

    def to_record(self) -> RunRecord:
        if self.source_kind is SourceKind.EVAL_RESULTS:
            complete_run = self.status not in {
                RunStatus.UNKNOWN,
                RunStatus.IN_PROGRESS,
            }
            first_recorded_floor = None
            visited_route = False
        elif self.source_kind is SourceKind.REPLAY_JSONL:
            complete_run = (
                False
                if self.replay_parser_rejected
                else (
                    self.replay_parser_complete_run
                    if self.replay_parser_complete_run is not None
                    else self.has_outcome and self.first_recorded_floor == 1
                )
            )
            first_recorded_floor = (
                None if self.replay_parser_rejected else self.first_recorded_floor
            )
            visited_route = self.has_floor or self.has_replay_nodes
        elif self.source_kind is SourceKind.DECK_HISTORY:
            complete_run = self.has_outcome
            first_recorded_floor = self.first_recorded_floor
            visited_route = self.has_valid_recorded_map
        else:
            complete_run = self.has_outcome
            first_recorded_floor = self.first_recorded_floor
            visited_route = self.has_floor
        return RunRecord(
            run_id=self.run_id,
            source_id=self.source_id,
            source_kind=self.source_kind,
            metadata=RunMetadata(
                character=self.character,
                seed=self.seed,
                game_version=self.game_version,
                game_version_source=self.game_version_source,
                experiment=self.experiment,
                checkpoint=self.checkpoint,
                evaluation_mode=self.evaluation_mode,
                scenario=self.scenario,
                ascension=self.ascension,
                modifiers=self.modifiers,
                is_multiplayer=self.is_multiplayer,
                started_at=self.started_at,
                ended_at=self.ended_at,
            ),
            outcome=RunOutcome(
                status=self.status,
                victory=self.victory,
                max_global_floor=self.max_global_floor,
                max_floor_label=self.max_floor_label,
                technical_failure_kind=self.technical_failure_kind,
            ),
            coverage=Coverage(
                complete_run=complete_run,
                first_recorded_floor=first_recorded_floor,
                last_recorded_floor=(
                    None
                    if self.source_kind is SourceKind.REPLAY_JSONL
                    and self.replay_parser_rejected
                    else self.observed_max_floor
                    if self.source_kind is SourceKind.REPLAY_JSONL
                    else self.max_global_floor
                ),
            ),
            capabilities=Capabilities(
                full_map=(
                    self.has_valid_recorded_map
                    if self.source_kind is SourceKind.DECK_HISTORY
                    else False
                ),
                visited_route=visited_route,
                node_rewards=(
                    self.has_valid_recorded_map
                    if self.source_kind is SourceKind.DECK_HISTORY
                    else False
                ),
                decisions=(
                    self.has_replay_action or self.has_node_decisions
                    if self.source_kind is SourceKind.REPLAY_JSONL
                    else self.has_card_pick
                    or (
                        self.source_kind is SourceKind.DECK_HISTORY
                        and any(
                            has_decisions
                            for _timestamp, has_decisions in (
                                self.latest_recorded_act_decisions.values()
                            )
                        )
                    )
                ),
                turn_replay=(
                    self.replay_parser_succeeded
                    and (
                        (self.has_replay_state and self.has_replay_action)
                        or self.usable_per_node_replay
                    )
                    if self.source_kind is SourceKind.REPLAY_JSONL
                    else False
                ),
            ),
            warnings=list(self.warnings),
            comparison_conflicts=frozenset(self.comparison_conflicts),
        )


@dataclass(frozen=True)
class _JsonlScan:
    records: tuple[dict[str, Any], ...]
    records_complete: bool
    descriptor: SourceDescriptor
    run_ids: tuple[str, ...]
    metadata_completeness: dict[str, Any]
    deck_outcomes: tuple[_CompactRun, ...]
    errors: tuple[str, ...]
    error_count: int
    # Every record of the file, in order, kept only while the file still looks
    # like a replay (see `_scan_jsonl_index`); lets `_normalize_incomplete_replay`
    # skip re-reading and re-parsing a file the scan has just parsed.
    replay_records: list[dict[str, Any]] | None = None


_CohortItem = RunRecord | _CompactRun


@dataclass(frozen=True)
class InProgressRun:
    """What a cohort's run list shows for a game that is still being played."""

    run_id: str
    source_id: str
    seed: str | None
    started_at: float | None
    # When its log was last written to.
    updated_at: float | None
    # Furthest global floor, and where the newest state row puts the player.
    global_floor: int | None
    act: int | None
    floor: int | None
    hp: int | None
    max_hp: int | None
    has_map: bool


@dataclass(frozen=True)
class _CohortMemo:
    """The descriptor computed for one cohort, valid while its members are the
    very same objects (compact runs and joined records are never mutated)."""

    members: tuple[_CohortItem, ...]
    ordered: tuple[_CohortItem, ...]
    descriptor: dict[str, Any]
    summary: CohortSummary


class _IdentityMemo:
    """Remember a pure function of a tuple of objects, keyed by object identity.

    Entries hold their arguments, so an object cannot be collected and have its
    id reused while its entry lives, and a hit is confirmed with ``is``.  A new
    memo is built from the previous one on every cohort build and keeps only
    the entries that build used, so it never grows past one build's worth.
    """

    def __init__(
        self,
        previous: dict[tuple[int, ...], tuple[tuple[Any, ...], Any]],
        previous_joined: Sequence[RunRecord] = (),
    ) -> None:
        self._previous = previous
        self._previous_joined = previous_joined
        self.entries: dict[tuple[int, ...], tuple[tuple[Any, ...], Any]] = {}
        self.joined: list[RunRecord] = []

    def reuse_equal(self, records: Sequence[RunRecord]) -> list[RunRecord]:
        """Swap each freshly joined record for an equal one from last build.

        ``join_records`` returns new objects for everything it is given, so
        when one run-id-less source grows every record it touches would look
        changed.  Handing back the previous object for each record whose
        content is unchanged keeps unaffected cohorts recognisable.
        """
        available: dict[tuple[str, str], list[RunRecord]] = {}
        for old in self._previous_joined:
            available.setdefault((old.run_id, old.source_id), []).append(old)
        result: list[RunRecord] = []
        for new in records:
            candidates = available.get((new.run_id, new.source_id), [])
            for index, old in enumerate(candidates):
                # `==` skips the two derived fields marked compare=False, and
                # metrics read `comparison_conflicts`.
                if (
                    old == new
                    and old.comparison_conflicts == new.comparison_conflicts
                    and old._node_provenance_index == new._node_provenance_index
                ):
                    del candidates[index]
                    new = old
                    break
            result.append(new)
        return result

    def get(self, items: Sequence[Any], compute: Callable[[], Any]) -> Any:
        key = tuple(map(id, items))
        entry = self._previous.get(key)
        if entry is None or not all(map(operator.is_, entry[0], items)):
            entry = (tuple(items), compute())
        self.entries[key] = entry
        return entry[1]


class RunCatalog:
    """Discover and lazily normalize supported artifacts under explicit roots."""

    def __init__(
        self,
        roots: Iterable[Path],
        replay_parser: Callable[[list[dict], str | None], dict] | None = None,
        *,
        include_policy: str = "all",
        in_progress_window: float | None = IN_PROGRESS_STALE_SECONDS,
        clock: Callable[[], float] = time.time,
        reindex_min_interval: float | None = REINDEX_MIN_INTERVAL_SECONDS,
    ) -> None:
        if include_policy not in {"all", "workbench"}:
            raise ValueError("include_policy must be 'all' or 'workbench'")
        self.roots = tuple(sorted({Path(root).resolve() for root in roots}, key=str))
        self.replay_parser = replay_parser
        self.include_policy = include_policy
        # `None` switches in-progress detection off altogether.
        self.in_progress_window = in_progress_window
        self._clock = clock
        # `None` (or 0) re-indexes every changed source on every request.
        self.reindex_min_interval = reindex_min_interval
        self._sources: dict[str, _IndexedSource] = {}
        self._run_sources: dict[str, tuple[str, ...]] = {}
        self._adapt_cache: dict[tuple[Path, int, int], AdaptedSource] = {}
        # Per-source public records for cohort building, so an unchanged
        # source hands `_build_cohorts` the very same objects every time (which
        # is what lets `_cohort_memo` recognise an unchanged cohort).
        self._cohort_source_records: dict[
            tuple[Path, int, int], tuple[RunRecord, ...]
        ] = {}
        self._cohort_memo: dict[str, _CohortMemo] = {}
        self._join_memo: dict[tuple[int, ...], tuple[tuple[Any, ...], Any]] = {}
        self._joined_records: list[RunRecord] = []
        self._cohort_records: dict[str, tuple[_CohortItem, ...]] = {}
        # Games still being played, per cohort: never part of `_cohort_records`
        # (which feeds every statistic), only of the cohort's run list.
        self._cohort_in_progress: dict[str, tuple[_CohortItem, ...]] = {}
        self._cohort_descriptors: list[dict[str, Any]] = []
        self._cohort_cache_key: tuple[Any, ...] | None = None
        self._lock = RLock()

    def list_sources(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return [deepcopy(source.entry) for source in self._ordered_sources()]

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            source = self._sources.get(source_id)
            if source is None:
                raise CatalogNotFoundError(f"unknown source id: {source_id}")
            if source.entry["open_mode"] == "error":
                return {
                    "view": "error",
                    "source": deepcopy(source.entry),
                    "errors": list(source.entry["errors"]),
                }
            if (
                source.descriptor.kind is SourceKind.SUMMARY
                and not source.records_complete
            ):
                redactions = _source_redactions(source)
                return {
                    "view": "summary",
                    "source": deepcopy(source.entry),
                    "summary": {
                        "record_count": source.descriptor.record_count,
                        "records": _scrub_paths(
                            deepcopy(list(source.records or ())), redactions
                        ),
                        "records_complete": False,
                        "record_sample_limit": INDEX_RECORD_LIMIT,
                        "record_sampling_method": "prefix",
                    },
                    "errors": list(source.entry["errors"]),
                }
            if not source.records_complete:
                return {
                    "view": "runs_summary",
                    "source": deepcopy(source.entry),
                    "run_count": len(source.deck_outcomes) or len(source.run_ids),
                    "runs_complete": False,
                    "representative_run_ids": list(
                        source.run_ids[:COHORT_ID_SAMPLE_LIMIT]
                    ),
                    "errors": list(source.entry["errors"]),
                }
            adapted = self._adapt(source)
            return self._source_view(source, adapted)

    def get_run(self, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise CatalogError("run id must be a non-empty string")
        if _safe_run_id(run_id) is None:
            raise CatalogNotFoundError("unknown run id")
        with self._lock:
            self._refresh()
            candidate_ids = self._run_sources.get(run_id)
            if not candidate_ids:
                raise CatalogNotFoundError(f"unknown run id: {run_id}")

            matched: list[RunRecord] = []
            sources: list[dict[str, Any]] = []
            path_ids: dict[str, str] = {}
            errors: list[str] = []
            for source_id in candidate_ids:
                source = self._sources[source_id]
                sources.append(deepcopy(source.entry))
                path_ids.update(_source_redactions(source))
                errors.extend(source.entry["errors"])
                if (
                    source.descriptor.kind
                    in {
                        SourceKind.DECK_HISTORY,
                        SourceKind.EVAL_RESULTS,
                        SourceKind.REPLAY_JSONL,
                    }
                    and not source.records_complete
                ):
                    matching_records, scan_errors = _scan_jsonl_run(
                        source.path,
                        run_id,
                        include_all=source.descriptor.kind
                        is SourceKind.REPLAY_JSONL,
                    )
                    errors.extend(scan_errors)
                    adapted = adapt_records(
                        source.path.name,
                        matching_records,
                        descriptor=SourceDescriptor(
                            source.descriptor.kind,
                            len(matching_records),
                            source.descriptor.message,
                        ),
                        replay_parser=self.replay_parser,
                        source_path=source.path,
                    )
                else:
                    adapted = self._adapt(source)
                errors.extend(adapted.errors)
                for record in self._public_records(source, adapted):
                    if record.run_id == run_id:
                        matched.append(record)
            if not matched:
                raise CatalogNotFoundError(
                    f"run id {run_id!r} was indexed but could not be normalized"
                )
            merged = [_join_catalog_group(matched)]
            if len(merged) != 1:
                raise CatalogError(f"ambiguous run id: {run_id}")
            payload = _scrub_paths(merged[0].to_dict(), path_ids)
            result = {
                "view": "run",
                "run": payload,
                "sources": sorted(sources, key=lambda item: item["source_id"]),
                "errors": _scrub_paths(list(dict.fromkeys(errors)), path_ids),
            }
            self._mark_live(
                result, merged[0], [self._sources[sid] for sid in candidate_ids]
            )
            return result

    def get_run_by_source(self, source_id: str) -> dict[str, Any]:
        """Resolve the one canonical run in a source that has no usable run
        id of its own (e.g. a legacy replay log with no run_meta header).

        Reuses the same normalization get_source() uses, rather than
        fabricating a synthetic run id, so it stays exact. A replay JSONL
        source is always exactly one run (unlike deck_history/eval_results,
        which can interleave thousands of runs per file), so a large one is
        fully rescanned the same way get_run(run_id) already does for
        REPLAY_JSONL -- source-addressing must not go dead on the ~20% of
        real logs that exceed INDEX_RECORD_LIMIT. Non-replay sources still
        require records_complete, since a full parse of an arbitrarily
        large multi-run file as "one run" would be wrong and unbounded.
        Raises CatalogError (400) if the source cannot be resolved to
        exactly one run, and CatalogNotFoundError (404) for an unknown
        source id.
        """
        if not isinstance(source_id, str) or not source_id:
            raise CatalogError("source id must be a non-empty string")
        with self._lock:
            self._refresh()
            source = self._sources.get(source_id)
            if source is None:
                raise CatalogNotFoundError(f"unknown source id: {source_id}")
            if source.entry["open_mode"] == "error":
                raise CatalogError(
                    f"source {source_id!r} could not be opened as a run"
                )
            errors: list[str] = []
            if source.records_complete:
                adapted = self._adapt(source)
            elif source.descriptor.kind is SourceKind.REPLAY_JSONL:
                all_records, scan_errors = _scan_jsonl_run(
                    source.path, "", include_all=True
                )
                errors.extend(scan_errors)
                adapted = adapt_records(
                    source.path.name,
                    all_records,
                    descriptor=SourceDescriptor(
                        source.descriptor.kind,
                        len(all_records),
                        source.descriptor.message,
                    ),
                    replay_parser=self.replay_parser,
                    source_path=source.path,
                )
            else:
                raise CatalogError(
                    f"source {source_id!r} is too large to resolve as a "
                    "single run without a run id; address a specific run "
                    "by id instead"
                )
            errors.extend(adapted.errors)
            records = self._public_records(source, adapted)
            if len(records) != 1:
                raise CatalogError(
                    f"source {source_id!r} contains {len(records)} runs; "
                    "address a specific run by id instead"
                )
            redactions = _source_redactions(source)
            payload = _scrub_paths(records[0].to_dict(), redactions)
            result = {
                "view": "run",
                "run": payload,
                "sources": [deepcopy(source.entry)],
                "errors": _scrub_paths(list(dict.fromkeys(errors)), redactions),
            }
            self._mark_live(result, records[0], [source])
            return result

    def _mark_live(
        self,
        result: dict[str, Any],
        record: RunRecord,
        sources: Sequence[_IndexedSource],
    ) -> None:
        """Say whether a run with no end is still being written to.

        Only an in-progress run gets the key, so the payload of a
        finished run is exactly what it always was.  A viewer that polls an
        unfinished run stops when ``live`` turns false: the log has not been
        written to for longer than the in-progress window.
        """

        if record.outcome.status is RunStatus.IN_PROGRESS:
            result["live"] = any(self._is_live_source(source) for source in sources)

    def list_cohorts(self) -> list[dict[str, Any]]:
        with self._lock:
            # `_build_cohorts` already returns a private deep copy.
            return self._build_cohorts()

    def get_cohort_records(self, cohort_id: str) -> tuple[RunRecord, ...]:
        with self._lock:
            self._build_cohorts()
            return tuple(self._iter_cohort_records(self._cohort_items_for_id(cohort_id)))

    def get_cohort_runs(
        self, cohort_id: str
    ) -> tuple[tuple[RunRecord, ...], tuple[InProgressRun, ...]]:
        """A cohort's finished runs and its games still being played.

        One snapshot of the catalog for both.  The games under way are the
        cohort's members but never part of its statistics: they are not in the
        records (nor in `get_metrics`), and are listed most recently started
        first.
        """

        with self._lock:
            self._build_cohorts()
            finished = tuple(
                self._iter_cohort_records(self._cohort_items_for_id(cohort_id))
            )
            playing = [
                self._in_progress_run(item)
                for item in self._cohort_in_progress.get(cohort_id, ())
            ]
        playing.sort(
            key=lambda row: (
                row.started_at is None,
                -(row.started_at or 0.0),
                -(row.updated_at or 0.0),
                row.source_id,
            )
        )
        return finished, tuple(playing)

    def get_cohort_in_progress(self, cohort_id: str) -> tuple[InProgressRun, ...]:
        """Only the games still being played, most recently started first."""

        return self.get_cohort_runs(cohort_id)[1]

    def _in_progress_run(self, item: _CohortItem) -> InProgressRun:
        if isinstance(item, _CompactRun):
            compact = item
            record = item.to_record()
        else:
            record = item
            compact = None
        source = self._sources.get(record.source_id)
        if compact is None and source is not None and source.deck_outcomes:
            # A small replay is adapted, but the index scan has already noted
            # where its newest state row put the player.
            compact = source.deck_outcomes[0]
        live = compact.live_state if compact is not None else None
        act, floor, hp, max_hp = live if live is not None else (None, None, None, None)
        global_floor = record.outcome.max_global_floor
        if global_floor is None and act is not None and floor is not None and act > 0:
            global_floor = (act - 1) * 17 + floor
        if (
            isinstance(global_floor, int)
            and not isinstance(global_floor, bool)
            and global_floor > 0
        ):
            if act is None or floor is None:
                act = (global_floor - 1) // 17 + 1
                floor = (global_floor - 1) % 17 + 1
        return InProgressRun(
            run_id=_safe_run_id(record.run_id) or "",
            source_id=record.source_id,
            seed=record.metadata.seed,
            started_at=record.metadata.started_at,
            updated_at=source.entry["mtime"] if source is not None else None,
            global_floor=global_floor,
            act=act,
            floor=floor,
            hp=hp,
            max_hp=max_hp,
            has_map=bool(record.capabilities.visited_route),
        )

    def get_metrics(
        self, current_id: str, baseline_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._build_cohorts()
            current = self._cohort_items_for_id(current_id)
            comparison = None
            baseline_summary = None
            if baseline_id is not None:
                baseline = self._cohort_items_for_id(baseline_id)
                # `_build_cohorts` already summarized these exact members with
                # this exact function; the summary is frozen, so reuse it
                # instead of copying every run of the cohort again.
                baseline_summary = self._cohort_memo[baseline_id].summary.to_dict()
                comparison = compare_cohorts(
                    self._iter_metric_records(current),
                    self._iter_metric_records(baseline),
                ).to_dict()
            return {
                "current_cohort_id": current_id,
                "baseline_cohort_id": baseline_id,
                "current": self._cohort_memo[current_id].summary.to_dict(),
                "baseline": baseline_summary,
                "comparison": comparison,
            }

    def _cohort_items_for_id(self, cohort_id: str) -> tuple[_CohortItem, ...]:
        records = self._cohort_records.get(cohort_id)
        if records is None:
            raise CatalogNotFoundError(f"unknown cohort id: {cohort_id}")
        return records

    def _iter_cohort_records(
        self, items: Iterable[_CohortItem]
    ) -> Iterable[RunRecord]:
        """Private copies, for callers that keep or modify what they get."""
        for item in items:
            if isinstance(item, _CompactRun):
                yield item.to_record()
            else:
                yield deepcopy(item)

    def _iter_metric_records(
        self, items: Iterable[_CohortItem]
    ) -> Iterable[RunRecord]:
        """The same records without copying the stored ones.

        Only for the metrics helpers, which read a record's metadata, outcome
        and conflicts and never write to it; copying every node and replay of
        every run for them was most of a metrics call.
        """
        for item in items:
            yield item.to_record() if isinstance(item, _CompactRun) else item

    def parse_upload(self, source_name: str, text: str) -> dict[str, Any]:
        with self._lock:
            return self._parse_upload(source_name, text)

    def _parse_upload(self, source_name: str, text: str) -> dict[str, Any]:
        if not isinstance(source_name, str):
            raise CatalogError("source_name must be a string")
        if not isinstance(text, str):
            raise CatalogError("text must be a string")
        safe_name = Path(source_name).name
        if not safe_name:
            safe_name = "uploaded.jsonl"
        try:
            records = _read_upload_records(safe_name, text)
            descriptor = classify_records(records, suffix=Path(safe_name).suffix)
        except SourceFormatError as error:
            return {
                "view": "error",
                "source_name": safe_name,
                "source_kind": SourceKind.UNKNOWN.value,
                "errors": [str(error)],
            }
        replay_result: dict[str, Any] | None = None

        def capture_replay(
            candidate_records: list[dict], candidate_name: str | None = None
        ) -> dict:
            nonlocal replay_result
            if self.replay_parser is None:
                raise ValueError("no replay parser was provided")
            replay_result = self.replay_parser(candidate_records, candidate_name)
            return replay_result

        parser = (
            capture_replay
            if descriptor.kind is SourceKind.REPLAY_JSONL
            else self.replay_parser
        )
        adapted = adapt_records(
            safe_name,
            records,
            descriptor=descriptor,
            replay_parser=parser,
        )
        payload = _adapted_upload_view(safe_name, adapted)
        if descriptor.kind is SourceKind.REPLAY_JSONL and adapted.runs:
            payload["progress"] = (
                deepcopy(replay_result)
                if isinstance(replay_result, dict)
                else _legacy_progress(adapted.runs[0])
            )
        return payload

    def _refresh(self) -> None:
        discovered = self._discover()
        indexed: dict[str, _IndexedSource] = {}
        reindexed = False
        # Indexing allocates millions of long-lived objects (a cold build holds
        # ~1 GB of them).  Left on, the cyclic collector re-walks the growing
        # heap a hundred times -- about a fifth of the build -- to free
        # nothing, because none of it is garbage.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            for root, path in discovered:
                try:
                    file_stat = path.stat()
                    if not stat_module.S_ISREG(file_stat.st_mode):
                        continue
                    relative = path.relative_to(root).as_posix()
                    source_id = _source_id(root, relative)
                    cache_key = (path, file_stat.st_mtime_ns, file_stat.st_size)
                    previous = self._sources.get(source_id)
                    if previous is not None and (
                        previous.cache_key == cache_key
                        or self._reindex_deferred(previous)
                    ):
                        source = previous
                    else:
                        source = self._index_source(root, path, file_stat)
                        reindexed = True
                except OSError:
                    continue
                indexed[source.source_id] = source
        finally:
            if gc_was_enabled:
                gc.enable()
        if not reindexed and indexed.keys() == self._sources.keys():
            return  # every source is the one already indexed
        run_sources: dict[str, list[str]] = {}
        for source in indexed.values():
            if source.entry["open_mode"] == "run":
                for run_id in source.run_ids:
                    run_sources.setdefault(run_id, []).append(source.source_id)
        self._sources = indexed
        self._run_sources = {
            run_id: tuple(sorted(source_ids))
            for run_id, source_ids in run_sources.items()
        }
        live_cache_keys = {source.cache_key for source in indexed.values()}
        self._adapt_cache = {
            key: adapted
            for key, adapted in self._adapt_cache.items()
            if key in live_cache_keys
        }
        self._cohort_source_records = {
            key: public
            for key, public in self._cohort_source_records.items()
            if key in live_cache_keys
        }

    def _reindex_deferred(self, previous: _IndexedSource) -> bool:
        """Whether a changed source keeps its current index for now.

        Only a replay log that looked unfinished when it was indexed -- a game
        under way, whose file grows all the time -- waits; it is re-indexed once
        that index is `reindex_min_interval` old, so a game that ends is shown
        as finished within one interval.  Anything else that changed (a finished
        log, any other kind of source) is re-indexed at once, and a source with
        no previous index never reaches here.
        """

        interval = self.reindex_min_interval
        if not interval or interval <= 0:
            return False
        if previous.descriptor.kind is not SourceKind.REPLAY_JSONL:
            return False
        if not all(
            _item_status(compact) in _UNFINISHED_STATUSES
            for compact in previous.deck_outcomes
        ):
            return False
        # A clock that went backwards must not freeze the index.
        return 0 <= self._clock() - previous.indexed_at < interval

    def _discover(self) -> list[tuple[Path, Path]]:
        discovered: list[tuple[Path, Path]] = []
        seen: set[Path] = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            for candidate in sorted(root.rglob("*"), key=lambda path: path.as_posix()):
                if candidate.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                if candidate.is_symlink() or _has_symlink_component(candidate, root):
                    continue
                try:
                    resolved = candidate.resolve(strict=True)
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                if not resolved.is_file() or resolved in seen:
                    continue
                if (
                    self.include_policy == "workbench"
                    and resolved.suffix.lower() == ".json"
                    and not _looks_like_workbench_json(resolved)
                ):
                    continue
                seen.add(resolved)
                discovered.append((root, resolved))
        return discovered

    def _index_source(
        self, root: Path, path: Path, file_stat: Any
    ) -> _IndexedSource:
        relative = path.relative_to(root).as_posix()
        source_id = _source_id(root, relative)
        display_name = relative
        if len(self.roots) > 1:
            display_name = f"{root.name}/{relative}"
        errors: list[str] = []
        error_count = 0
        records: tuple[dict[str, Any], ...] | None
        records_complete = True
        deck_outcomes: tuple[_CompactRun, ...] = ()
        if path.suffix.lower() == ".jsonl":
            scan = _scan_jsonl_index(path)
            records = scan.records
            records_complete = scan.records_complete
            descriptor = scan.descriptor
            errors.extend(scan.errors)
            error_count = scan.error_count
            run_ids = scan.run_ids
            metadata = scan.metadata_completeness
            deck_outcomes = scan.deck_outcomes
            if (
                descriptor.kind is SourceKind.REPLAY_JSONL
                and not records_complete
                and deck_outcomes
            ):
                parser_error = _normalize_incomplete_replay(
                    deck_outcomes[0],
                    path,
                    self.replay_parser,
                    path.name,
                    scan.replay_records,
                )
                if parser_error is not None:
                    errors.append(parser_error)
                    error_count += 1
                run_ids = (
                    (deck_outcomes[0].run_id,)
                    if deck_outcomes[0].run_id
                    else ()
                )
            for outcome in deck_outcomes:
                outcome.source_id = source_id
        else:
            try:
                loaded_records = read_json_records(path)
                records = tuple(loaded_records)
                descriptor = classify_records(loaded_records, suffix=path.suffix)
            except SourceFormatError as error:
                records = None
                descriptor = SourceDescriptor(SourceKind.UNKNOWN, 0, str(error))
                errors.append(str(error))
                error_count = 1
            run_ids = tuple(sorted(_lightweight_run_ids(list(records or ()))))
            metadata = _metadata_completeness(list(records or ()))
        if descriptor.kind is SourceKind.SUMMARY:
            open_mode = "summary"
        elif descriptor.kind is SourceKind.UNKNOWN:
            open_mode = "error"
            if not errors:
                errors.append(f"{path.name}: {descriptor.message}")
                error_count += 1
        elif (
            errors
            and records_complete
            and not (
                descriptor.kind is SourceKind.DECK_HISTORY
                and deck_outcomes
            )
        ):
            open_mode = "error"
        else:
            open_mode = "run"
        redactions = _path_redactions(path, root, source_id)
        public_errors = _scrub_paths(errors, redactions)
        public_message = _scrub_paths(descriptor.message, redactions)
        entry = {
            "source_id": source_id,
            "display_name": display_name,
            "source_kind": descriptor.kind.value,
            "open_mode": open_mode,
            "mtime": file_stat.st_mtime,
            "mtime_ns": file_stat.st_mtime_ns,
            "size": file_stat.st_size,
            "record_count": descriptor.record_count,
            "message": public_message,
            "errors": public_errors,
            "error_count": error_count,
            "errors_complete": error_count == len(errors),
            "error_sample_limit": ERROR_DETAIL_LIMIT,
            "errors_omitted": max(0, error_count - len(errors)),
            "metadata_completeness": metadata,
        }
        if not records_complete and descriptor.kind is not SourceKind.SUMMARY:
            # The retained prefix of a big replay/deck/eval source is never
            # read again: `_adapt` ignores records of an incomplete source,
            # `get_source` only shows them for SUMMARY, and run lookups
            # re-scan the file.  Holding 512 full states for each of ~200
            # logs was ~3 GB of dead objects the garbage collector kept
            # walking.
            records = None
        return _IndexedSource(
            source_id=source_id,
            root=root,
            path=path,
            entry=entry,
            descriptor=descriptor,
            records=records,
            records_complete=records_complete,
            deck_outcomes=deck_outcomes,
            run_ids=run_ids,
            cache_key=(path, file_stat.st_mtime_ns, file_stat.st_size),
            indexed_at=self._clock(),
        )

    def _ordered_sources(self) -> list[_IndexedSource]:
        return sorted(
            self._sources.values(),
            key=lambda source: (source.entry["display_name"], source.source_id),
        )

    def _adapt(self, source: _IndexedSource) -> AdaptedSource:
        cached = self._adapt_cache.get(source.cache_key)
        if cached is not None:
            return cached
        if source.records is None or not source.records_complete:
            adapted = AdaptedSource(
                source.descriptor, errors=tuple(source.entry["errors"])
            )
        else:
            adapted = adapt_records(
                source.path.name,
                deepcopy(list(source.records)),
                descriptor=source.descriptor,
                replay_parser=self.replay_parser,
                source_path=source.path,
            )
        self._adapt_cache[source.cache_key] = adapted
        return adapted

    def _public_records(
        self, source: _IndexedSource, adapted: AdaptedSource
    ) -> tuple[RunRecord, ...]:
        public: list[RunRecord] = []
        for record in adapted.runs:
            clone = deepcopy(record)
            clone.source_id = source.source_id
            clone.run_id = _safe_run_id(clone.run_id) or ""
            public.append(clone)
        return tuple(public)

    def _source_view(
        self, source: _IndexedSource, adapted: AdaptedSource
    ) -> dict[str, Any]:
        redactions = _source_redactions(source)
        if adapted.summary is not None:
            return {
                "view": "summary",
                "source": deepcopy(source.entry),
                "summary": _scrub_paths(deepcopy(adapted.summary), redactions),
                "errors": _scrub_paths(list(adapted.errors), redactions),
            }
        records = self._public_records(source, adapted)
        if not records:
            return {
                "view": "error",
                "source": deepcopy(source.entry),
                "errors": _scrub_paths(list(adapted.errors), redactions)
                or ["source contains no adaptable runs"],
            }
        return {
            "view": "run" if len(records) == 1 else "runs",
            "source": deepcopy(source.entry),
            "runs": [
                _scrub_paths(record.to_dict(), redactions)
                for record in records
            ],
            "errors": _scrub_paths(list(adapted.errors), redactions),
        }

    def _fresh_replay_source_ids(self) -> frozenset[str]:
        """Single-run replay sources written to within the in-progress window."""

        window = self.in_progress_window
        if window is None:
            return frozenset()
        now = self._clock()
        return frozenset(
            source.source_id
            for source in self._sources.values()
            if source.entry["open_mode"] == "run"
            and source.descriptor.kind is SourceKind.REPLAY_JSONL
            and now - source.entry["mtime"] <= window
        )

    def _is_live_source(self, source: _IndexedSource) -> bool:
        window = self.in_progress_window
        return (
            window is not None
            and source.descriptor.kind is SourceKind.REPLAY_JSONL
            and self._clock() - source.entry["mtime"] <= window
        )

    def _build_cohorts(self) -> list[dict[str, Any]]:
        self._refresh()
        # Whether a log is "still being played" depends on the clock as well as
        # on the files, so the set of recently written replay sources is part of
        # the cache key: a log that stops being written must leave the
        # in-progress list even though no file changed.
        fresh = self._fresh_replay_source_ids()
        cache_key = (
            tuple(source.cache_key for source in self._ordered_sources()),
            fresh,
        )
        if self._cohort_cache_key == cache_key:
            return deepcopy(self._cohort_descriptors)
        records: list[RunRecord] = []
        compact_records: list[_CompactRun] = []
        # Unfinished runs of recently written single-run replay logs.  They are
        # held back from the join below, so nothing that is computed for the
        # finished runs (joined records, cohort summaries, metrics) can see them.
        unfinished: list[_CohortItem] = []
        for source in self._ordered_sources():
            if source.entry["open_mode"] != "run":
                continue
            live = source.source_id in fresh
            if not source.records_complete and source.deck_outcomes:
                for compact in source.deck_outcomes:
                    if live and _item_status(compact) in _UNFINISHED_STATUSES:
                        unfinished.append(compact)
                    else:
                        compact_records.append(compact)
            else:
                public = self._cohort_source_records.get(source.cache_key)
                if public is None:
                    public = self._public_records(source, self._adapt(source))
                    self._cohort_source_records[source.cache_key] = public
                for record in public:
                    if live and _item_status(record) in _UNFINISHED_STATUSES:
                        unfinished.append(record)
                    else:
                        records.append(record)
        if unfinished:
            # A run id that other items also carry belongs to a joined run; the
            # join decides what it is, as it always did.
            taken: dict[str, int] = {}
            for item in (*records, *compact_records, *unfinished):
                if item.run_id:
                    taken[item.run_id] = taken.get(item.run_id, 0) + 1
            in_progress: list[_CohortItem] = []
            for item in unfinished:
                if item.run_id and taken[item.run_id] > 1:
                    if isinstance(item, _CompactRun):
                        compact_records.append(item)
                    else:
                        records.append(item)
                else:
                    in_progress.append(item)
        else:
            in_progress = []
        identified, historical = _group_by_run_id(records, compact_records)
        join_memo = _IdentityMemo(self._join_memo, self._joined_records)
        merged = _merge_identified(identified, historical, join_memo)
        eligible: list[_CohortItem] = [
            record
            for record in merged
            if _item_status(record) in {RunStatus.WIN, RunStatus.DEAD}
            or _item_status(record).is_technical
        ]
        grouped: dict[tuple[Any, ...], list[_CohortItem]] = {}
        for record in eligible:
            grouped.setdefault(_cohort_key(record), []).append(record)
        in_progress_grouped: dict[tuple[Any, ...], list[_CohortItem]] = {}
        for record in in_progress:
            in_progress_grouped.setdefault(_cohort_key(record), []).append(record)

        descriptors: list[dict[str, Any]] = []
        cohort_records: dict[str, tuple[_CohortItem, ...]] = {}
        cohort_in_progress: dict[str, tuple[_CohortItem, ...]] = {}
        cohort_memo: dict[str, _CohortMemo] = {}
        # Insertion order, then a stable sort: ties keep the order they always had.
        cohort_keys = [
            *grouped,
            *(key for key in in_progress_grouped if key not in grouped),
        ]
        for key in sorted(cohort_keys, key=_sortable_key):
            group = grouped.get(key, [])
            cohort_id = _cohort_id(key)
            previous = self._cohort_memo.get(cohort_id)
            if (
                previous is not None
                and len(previous.members) == len(group)
                and all(map(operator.is_, previous.members, group))
            ):
                # Same member objects as last time: nothing the descriptor is
                # derived from can have changed, so skip re-deriving it from
                # (potentially hundreds of thousands of) runs.
                memo = previous
            else:
                memo = self._derive_cohort(key, cohort_id, group, identified)
            cohort_memo[cohort_id] = memo
            cohort_records[cohort_id] = memo.ordered
            # The baseline link below is assigned per descriptor, so every
            # build gets its own top-level dict (nested values are shared and
            # read-only: they are deep-copied before leaving the catalog).
            descriptor = dict(memo.descriptor)
            playing = tuple(in_progress_grouped.get(key, ()))
            cohort_in_progress[cohort_id] = playing
            descriptor["in_progress_count"] = len(playing)
            if playing and not group:
                # Nothing finished yet: the only evidence of when this batch
                # ran is the games under way, and without it the batch would
                # sink below every dated one in the tree.
                stamps = [
                    stamp
                    for item in playing
                    if (stamp := _item_timestamp(item)) is not None
                ]
                descriptor["latest_at"] = max(stamps) if stamps else None
            descriptors.append(descriptor)
        sorted_descriptors = sorted(
            descriptors,
            key=lambda item: (
                item["latest_at"] is None,
                -(item["latest_at"] or 0),
                item["label"],
                item["cohort_id"],
            ),
        )
        compatible_groups: dict[str, list[dict[str, Any]]] = {}
        for descriptor in sorted_descriptors:
            signature = descriptor["comparison_readiness"][
                "comparison_signature"
            ]
            if signature is not None:
                compatible_groups.setdefault(signature, []).append(descriptor)
        for group in compatible_groups.values():
            for descriptor in group:
                descriptor["default_baseline_cohort_id"] = next(
                    (
                        candidate["cohort_id"]
                        for candidate in group
                        if candidate["cohort_id"] != descriptor["cohort_id"]
                    ),
                    None,
                )

        self._cohort_records = cohort_records
        self._cohort_in_progress = cohort_in_progress
        self._cohort_memo = cohort_memo
        self._join_memo = join_memo.entries
        self._joined_records = join_memo.joined
        self._cohort_descriptors = sorted_descriptors
        self._cohort_cache_key = cache_key
        return deepcopy(self._cohort_descriptors)

    def _derive_cohort(
        self,
        key: tuple[Any, ...],
        cohort_id: str,
        group: list[_CohortItem],
        identified: dict[str, list[_CohortItem]],
    ) -> _CohortMemo:
        """Compute one cohort's ordered members, summary and descriptor."""
        (
            experiment,
            checkpoint,
            character,
            version,
            mode,
            scenario,
            ascension,
        ) = key
        unarchived = experiment is None and checkpoint is None
        ordered = tuple(
            sorted(group, key=lambda record: (record.run_id, record.source_id))
        )
        all_source_refs = sorted(
            {
                source_id
                for record in ordered
                for source_id in record.source_id.split(" | ")
                if source_id
            }
        )
        source_refs = all_source_refs[:SOURCE_REF_LIMIT]
        run_ids = sorted(
            run_id
            for record in ordered
            if (run_id := _safe_run_id(record.run_id)) is not None
        )
        run_ids_complete = len(run_ids) <= COHORT_ID_SAMPLE_LIMIT
        timestamps = [
            timestamp
            for record in ordered
            if (timestamp := _item_timestamp(record)) is not None
        ]
        version_source_evidence = _GameVersionSourceEvidence()
        for record in ordered:
            if not isinstance(record, _CompactRun):
                # A merged record answers for every raw item that shares its
                # run id.  (A compact run is only ever merged as a singleton,
                # so its own source is all the evidence there is.)
                raw_items = identified.get(_safe_run_id(record.run_id) or "")
                if raw_items:
                    run_evidence = _GameVersionSourceEvidence()
                    for raw_item in raw_items:
                        run_evidence.observe(
                            _item_metadata(raw_item).game_version_source
                        )
                    version_source_evidence.merge(run_evidence)
                    continue
            version_source_evidence.observe(
                _item_metadata(record).game_version_source
            )
        filters = {
            "checkpoint": _safe_catalog_scalar(checkpoint),
            "character": _safe_catalog_scalar(character),
            "game_version": _safe_catalog_scalar(version),
            "game_version_source": version_source_evidence.resolved(),
            "evaluation_mode": _safe_catalog_scalar(mode),
            "scenario": _safe_catalog_scalar(scenario),
            "ascension": ascension,
        }
        # Materialize once: readiness and the summary below both consume
        # the whole iterator, and re-deriving it would double the work.
        summarized = list(self._iter_metric_records(ordered))
        readiness = describe_comparison_readiness(summarized)
        # Same helper (and therefore the same valid-run / missing-floor
        # rules) the metric cards use, so the number shown next to a batch
        # in the tree always equals the 平均推进 card on its detail page.
        summary = summarize_cohort(summarized)
        safe_experiment = _safe_catalog_scalar(experiment)
        safe_character = _safe_catalog_scalar(character)
        # Project convention: A<act>F<floor>a<ascension>, e.g. A2F12a10 is
        # act 2, floor 12, ascension 10. Case carries the meaning -- upper
        # A is the act, lower a is the ascension -- so a cohort, which has
        # an ascension but no act or floor, is labelled "a0" / "a?".
        ascension_label = (
            f"a{ascension}"
            if type(ascension) is int and ascension >= 0
            else "a?"
        )
        if unarchived:
            # Ascension is the only axis still distinguishing otherwise
            # metadata-less runs (e.g. "未归档 · Defect" for ascension=0
            # vs. ascension unknown) -- without it two unarchived
            # cohorts for the same character render as identical labels.
            base = (
                f"未归档 · {safe_character}" if safe_character else "未归档"
            )
            label = f"{base} · {ascension_label}"
        else:
            label_parts = [
                safe_experiment,
                _safe_catalog_scalar(checkpoint),
                safe_character,
                _safe_catalog_scalar(version),
                _safe_catalog_scalar(mode),
                _safe_catalog_scalar(scenario),
                ascension_label,
            ]
            label = " · ".join(str(value) for value in label_parts if value)
        descriptor = {
            "cohort_id": cohort_id,
            "label": label,
            "avg_global_floor": summary.avg_global_floor,
            "valid_n": summary.valid_n,
            "filters": filters,
            "experiment": safe_experiment,
            "unarchived": unarchived,
            "comparison_readiness": readiness.to_dict(),
            "default_baseline_cohort_id": None,
            "run_count": len(ordered),
            "run_id_count": len(run_ids),
            "run_ids": run_ids if run_ids_complete else [],
            "run_ids_complete": run_ids_complete,
            "representative_run_ids": run_ids[:COHORT_ID_SAMPLE_LIMIT],
            "source_refs": source_refs,
            "source_ref_count": len(all_source_refs),
            "source_refs_complete": len(all_source_refs) <= SOURCE_REF_LIMIT,
            "latest_at": max(timestamps) if timestamps else None,
            "technical_count": sum(
                _item_status(record).is_technical for record in ordered
            ),
        }
        return _CohortMemo(tuple(group), ordered, descriptor, summary)


# A replay with neither of these has no terminal state.  Which of the two a log
# gets depends on its size (see `_adapt_replay` and `_update_compact_replay`).
_UNFINISHED_STATUSES = frozenset({RunStatus.UNKNOWN, RunStatus.IN_PROGRESS})


def _cohort_key(record: _CohortItem) -> tuple[Any, ...]:
    """The cohort a run belongs to; the same for a finished and a live run."""

    (
        experiment,
        checkpoint,
        character,
        game_version,
        evaluation_mode,
        scenario,
        ascension,
    ) = _item_cohort_fields(record)
    if checkpoint is None and experiment is None:
        # No checkpoint and no experiment: this run cannot be
        # attributed to any training/eval batch. Collapse all such
        # runs into one cohort per (character, ascension) rather
        # than exploding into one cohort per source file.
        return (None, None, character, None, None, None, ascension)
    return (
        experiment,
        checkpoint,
        character,
        game_version,
        evaluation_mode,
        scenario,
        ascension,
    )


def _source_id(root: Path, relative: str) -> str:
    digest = sha256(f"{root}\0{relative}".encode("utf-8")).hexdigest()[:20]
    return f"src_{digest}"


def _recorded_act_decision_states(
    item: _CohortItem,
) -> dict[int, tuple[int | float, bool]]:
    if isinstance(item, _CompactRun):
        return dict(item.latest_recorded_act_decisions)
    if item.source_kind is not SourceKind.DECK_HISTORY:
        return {}
    selected: dict[int, tuple[int | float, bool]] = {}
    for node in item.nodes:
        if type(node) is not dict or node.get("event") != "map_snapshot":
            continue
        try:
            snapshot = parse_recorded_map_row(node)
        except RecordedMapError:
            continue
        timestamp = node["ts"]
        has_decisions = any(
            type(route_node) is dict
            and type(route_node.get("decisions")) is list
            and bool(route_node["decisions"])
            for route_node in snapshot.route_nodes
        )
        retained = selected.get(snapshot.act_index)
        if retained is None or timestamp >= retained[0]:
            selected[snapshot.act_index] = (timestamp, has_decisions)
    return selected


def _catalog_item_key(item: _CohortItem) -> tuple[str, str]:
    return item.source_id, type(item).__name__


def _item_has_deck_card_pick(item: _CohortItem) -> bool:
    if isinstance(item, _CompactRun):
        return item.has_card_pick
    return item.source_kind is SourceKind.DECK_HISTORY and any(
        type(node) is dict and node.get("event") == "card_pick"
        for node in item.nodes
    )


def _join_catalog_group(items: Iterable[_CohortItem]) -> RunRecord:
    ordered = sorted(items, key=_catalog_item_key)
    if not ordered:
        raise CatalogError("cannot join an empty catalog group")

    latest_by_act: dict[
        int, tuple[int | float, int, bool]
    ] = {}
    for item_index, item in enumerate(ordered):
        states = _recorded_act_decision_states(item)
        for act_index, (timestamp, has_decisions) in states.items():
            retained = latest_by_act.get(act_index)
            if retained is None or timestamp >= retained[0]:
                latest_by_act[act_index] = (
                    timestamp,
                    item_index,
                    has_decisions,
                )

    deck_decisions = any(_item_has_deck_card_pick(item) for item in ordered) or any(
        has_decisions
        for _timestamp, _item_index, has_decisions in latest_by_act.values()
    )
    normalized: list[RunRecord] = []
    for item_index, item in enumerate(ordered):
        record = item.to_record() if isinstance(item, _CompactRun) else deepcopy(item)
        if record.source_kind is SourceKind.DECK_HISTORY:
            record.capabilities = replace(
                record.capabilities,
                decisions=deck_decisions,
            )
            record.nodes = [
                node
                for node in record.nodes
                if not (
                    type(node) is dict
                    and node.get("_workbench_evidence_kind") == "route_node"
                    and type(node.get("act_index")) is int
                    and (
                        node["act_index"] in latest_by_act
                        and latest_by_act[node["act_index"]][1] != item_index
                    )
                )
            ]
        normalized.append(record)

    joined = join_records(normalized)
    if len(joined) != 1:
        raise CatalogError("catalog group did not resolve to one run")
    return joined[0]


def _group_by_run_id(
    ordinary: list[RunRecord], compact: list[_CompactRun]
) -> tuple[dict[str, list[_CohortItem]], list[_CohortItem]]:
    """Split raw items into per-run-id groups and run-id-less (historical) ones."""

    identified: dict[str, list[_CohortItem]] = {}
    historical: list[_CohortItem] = []
    for item in [*ordinary, *compact]:
        if item.run_id:
            identified.setdefault(item.run_id, []).append(item)
        else:
            historical.append(item)
    return identified, historical


def _merge_compact_records(
    ordinary: list[RunRecord], compact: list[_CompactRun]
) -> list[_CohortItem]:
    """Merge exact IDs while retaining compact single-source records."""

    return _merge_identified(*_group_by_run_id(ordinary, compact))


def _merge_identified(
    identified: dict[str, list[_CohortItem]],
    historical: list[_CohortItem],
    memo: _IdentityMemo | None = None,
) -> list[_CohortItem]:
    """Join each run-id group; ``memo`` makes an unchanged join a lookup."""

    merged: list[_CohortItem] = []
    for run_id in sorted(identified):
        group = identified[run_id]
        if len(group) == 1 and isinstance(group[0], _CompactRun):
            merged.append(group[0])
        elif memo is None:
            merged.append(_join_catalog_group(group))
        else:
            merged.append(
                memo.get(group, lambda group=group: _join_catalog_group(group))
            )
    if not historical:
        return merged
    # `join_records` copies every record it is given and compares each
    # run-id-less one with every identified one (`_plausibly_overlap`: equal
    # seed and overlapping timestamps), adding an "ambiguous historical
    # identity" warning to both only then.  An identified item that overlaps no
    # run-id-less record is therefore never touched by it: for a compact run
    # the join would hand back the same record as `to_record()`, and a merged
    # record has already been through `_join_catalog_group`, so a second pass
    # changes nothing.  Keep those as they are (copying ~20k of them on every
    # request took seconds) and join only the overlapping ones and the
    # run-id-less records themselves, in the final order `join_records` uses.
    anonymous_by_seed: dict[str, list[Any]] = {}
    for item in historical:
        metadata = _item_metadata(item)
        if isinstance(metadata.seed, str) and metadata.seed:
            anonymous_by_seed.setdefault(metadata.seed, []).append(
                SimpleNamespace(metadata=metadata)
            )
    untouched: list[_CohortItem] = []
    to_join: list[_CohortItem] = []
    for item in merged:
        seed = item.seed if isinstance(item, _CompactRun) else item.metadata.seed
        anonymous = anonymous_by_seed.get(seed) if isinstance(seed, str) else None
        if not anonymous or not any(
            _plausibly_overlap(
                candidate, SimpleNamespace(metadata=_item_metadata(item))
            )
            for candidate in anonymous
        ):
            untouched.append(item)
        else:
            to_join.append(item)
    to_join.extend(historical)

    def join_all() -> list[RunRecord]:
        return join_records(
            [
                item.to_record() if isinstance(item, _CompactRun) else item
                for item in to_join
            ]
        )

    if memo is None:
        joined = join_all()
    else:
        joined = memo.get(to_join, lambda: memo.reuse_equal(join_all()))
        memo.joined = list(joined)
    return sorted(
        [*untouched, *joined],
        key=lambda item: (not bool(item.run_id), item.run_id, item.source_id),
    )


def _record_run_id(record: dict[str, Any]) -> str:
    return record_run_id(record) or ""


def _item_metadata(record: _CohortItem) -> RunMetadata:
    if isinstance(record, RunRecord):
        return record.metadata
    return RunMetadata(
        character=record.character,
        seed=record.seed,
        game_version=record.game_version,
        game_version_source=record.game_version_source,
        experiment=record.experiment,
        checkpoint=record.checkpoint,
        evaluation_mode=record.evaluation_mode,
        scenario=record.scenario,
        ascension=record.ascension,
        modifiers=record.modifiers,
        started_at=record.started_at,
        ended_at=record.ended_at,
    )


def _item_cohort_fields(record: _CohortItem) -> tuple[Any, ...]:
    """The metadata fields cohorts are keyed on, without building RunMetadata."""

    if isinstance(record, _CompactRun):
        return (
            record.experiment,
            record.checkpoint,
            record.character,
            record.game_version,
            record.evaluation_mode,
            record.scenario,
            record.ascension,
        )
    metadata = record.metadata
    return (
        metadata.experiment,
        metadata.checkpoint,
        metadata.character,
        metadata.game_version,
        metadata.evaluation_mode,
        metadata.scenario,
        metadata.ascension,
    )


def _item_status(record: _CohortItem) -> RunStatus:
    return record.outcome.status if isinstance(record, RunRecord) else record.status


def _item_timestamp(record: _CohortItem) -> float | None:
    metadata = _item_metadata(record)
    for value in (metadata.ended_at, metadata.started_at):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            continue
        if math.isfinite(numeric):
            return numeric
    return None


class _ScanConstantError(ValueError):
    pass


@dataclass(slots=True)
class _ErrorBudget:
    details: list[str]
    count: int = 0

    def add(self, detail: str) -> None:
        self.count += 1
        if len(self.details) < ERROR_DETAIL_LIMIT:
            self.details.append(detail)


def _reject_scan_constant(value: str) -> None:
    raise _ScanConstantError(f"non-standard numeric constant {value}")


def _add_bounded_replay_id(observed: set[str], value: str | None) -> bool:
    safe_value = _safe_run_id(value)
    if safe_value is None or safe_value in observed:
        return False
    if len(observed) >= REPLAY_WARNING_ID_LIMIT:
        return True
    observed.add(safe_value)
    return False


def _compact_replay_identity_warnings(
    resolved: str,
    observed: set[str],
    ids_omitted: bool,
) -> tuple[str, ...]:
    displayed = set(observed)
    if resolved and resolved not in displayed:
        if len(displayed) >= REPLAY_WARNING_ID_LIMIT:
            displayed.remove(max(displayed))
            ids_omitted = True
        displayed.add(resolved)
    if len(displayed) <= 1 and not ids_omitted:
        return ()
    omitted_note = "; additional run_id values omitted" if ids_omitted else ""
    return (
        "conflicting replay run_id values: "
        f"observed={', '.join(sorted(displayed))}{omitted_note}; using {resolved}",
    )


def _scan_jsonl_index(path: Path) -> _JsonlScan:
    """Scan an entire JSONL source while retaining only bounded raw evidence."""

    records: list[dict[str, Any]] = []
    # `retained` holds *every* parsed record, but only while the file could
    # still be a replay (state/action rows seen, or still within the indexed
    # prefix), so a multi-GB deck history never accumulates here.  Once dropped
    # it stays dropped and the caller re-reads the file.
    retained: list[dict[str, Any]] | None = []
    record_count = 0
    error_budget = _ErrorBudget([])
    run_ids: set[str] = set()
    present_metadata: set[str] = set()
    grouped_runs_by_id: dict[str, _CompactRun] = {}
    anonymous_deck_runs: list[_CompactRun] = []
    source_replay_run: _CompactRun | None = None
    replay_top_level_id: str | None = None
    replay_nested_id: str | None = None
    replay_observed_ids: set[str] = set()
    replay_ids_omitted = False
    eval_runs: list[_CompactRun] = []
    types: set[str] = set()
    events: set[str] = set()
    has_action_command = False
    looks_like_boss_deck = False
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(
                        line, parse_constant=_reject_scan_constant
                    )
                except _ScanConstantError as error:
                    error_budget.add(
                        f"{path.name}:{line_number}: invalid JSON: {error}"
                    )
                    continue
                except ValueError as error:
                    detail = (
                        error.msg
                        if isinstance(error, json.JSONDecodeError)
                        else "invalid numeric literal"
                    )
                    error_budget.add(
                        f"{path.name}:{line_number}: invalid JSON: {detail}"
                    )
                    continue
                if not isinstance(record, dict):
                    error_budget.add(
                        f"{path.name}:{line_number}: expected an object record"
                    )
                    continue
                record_count += 1
                if len(records) < INDEX_RECORD_LIMIT:
                    records.append(record)
                _collect_metadata_presence(record, present_metadata)
                raw_record_type = record.get("type")
                record_type = (
                    raw_record_type if isinstance(raw_record_type, str) else ""
                )
                if record_type in {"state", "action"}:
                    types.add(record_type)
                if retained is not None:
                    if record_count <= INDEX_RECORD_LIMIT or types:
                        retained.append(record)
                    else:
                        retained = None
                event = str(record.get("event", ""))
                if event in {
                    "milestone",
                    "card_pick",
                    "map_snapshot",
                    "outcome",
                    "eval_result",
                    "result",
                    "summary",
                }:
                    events.add(event)
                has_action_command = has_action_command or (
                    record_type == "action"
                    and isinstance(record.get("data"), dict)
                )
                looks_like_boss_deck = looks_like_boss_deck or {
                    "checkpoint",
                    "cards",
                    "enemies",
                    "hp_at_entry",
                }.issubset(record)
                record_run_ids = _lightweight_run_ids([record])
                is_replay_row = record_type in {"state", "action", "run_meta"}
                is_replay_candidate = is_replay_row or event in {
                    "outcome",
                    "result",
                    "eval_result",
                }
                if is_replay_candidate:
                    top_level_id = _replay_scalar_text(record, "run_id")
                    if replay_top_level_id is None and top_level_id is not None:
                        replay_top_level_id = top_level_id
                    data = record.get("data")
                    nested_id = (
                        _replay_scalar_text(data, "run_id")
                        if isinstance(data, dict)
                        else None
                    )
                    if replay_nested_id is None and nested_id is not None:
                        replay_nested_id = nested_id
                    for observed_id in (top_level_id, nested_id):
                        replay_ids_omitted = (
                            _add_bounded_replay_id(
                                replay_observed_ids, observed_id
                            )
                            or replay_ids_omitted
                        )
                    if source_replay_run is None:
                        source_replay_run = _CompactRun(run_id="")
                    _update_compact_replay(source_replay_run, record)

                if event == "eval_result":
                    run_ids.update(record_run_ids)
                    compact = _CompactRun(run_id=_record_run_id(record))
                    _update_compact_eval(compact, record)
                    eval_runs.append(compact)
                elif event in {
                    "milestone",
                    "card_pick",
                    "map_snapshot",
                    "outcome",
                }:
                    run_ids.update(record_run_ids)
                    run_id = _record_run_id(record)
                    if run_id:
                        compact = grouped_runs_by_id.setdefault(
                            run_id, _CompactRun(run_id=run_id)
                        )
                    else:
                        compact = _CompactRun(run_id="")
                        anonymous_deck_runs.append(compact)
                    _update_compact_deck(compact, record)
                elif not is_replay_candidate:
                    run_ids.update(record_run_ids)
    except UnicodeDecodeError as error:
        retained = None
        error_budget.add(f"{path.name}: invalid UTF-8 at byte {error.start}")
    except OSError as error:
        retained = None
        detail = str(error.strerror or type(error).__name__).replace(
            str(path), path.name
        )
        errno_label = f"[Errno {error.errno}] " if error.errno is not None else ""
        error_budget.add(
            f"{path.name}: could not read source: {errno_label}{detail}"
        )

    descriptor = _classify_jsonl_scan(
        record_count=record_count,
        types=types,
        events=events,
        has_action_command=has_action_command,
        looks_like_boss_deck=looks_like_boss_deck,
    )
    if descriptor.kind is SourceKind.EVAL_RESULTS:
        compact_runs = tuple(eval_runs)
    elif descriptor.kind is SourceKind.REPLAY_JSONL:
        if source_replay_run is None:
            compact_runs = ()
            run_ids = set()
        else:
            source_replay_run.run_id = (
                replay_top_level_id or replay_nested_id or ""
            )
            source_replay_run.replay_observed_ids = tuple(
                sorted(replay_observed_ids)
            )
            source_replay_run.replay_ids_omitted = replay_ids_omitted
            source_replay_run.warnings = _compact_replay_identity_warnings(
                source_replay_run.run_id,
                replay_observed_ids,
                replay_ids_omitted,
            )
            compact_runs = (source_replay_run,)
            run_ids = {source_replay_run.run_id} if source_replay_run.run_id else set()
    else:
        compact_runs = tuple(grouped_runs_by_id.values()) + tuple(
            anonymous_deck_runs
        )
    for compact in compact_runs:
        compact.source_kind = descriptor.kind
        _finalize_compact(compact)
    return _JsonlScan(
        records=tuple(records),
        records_complete=record_count <= INDEX_RECORD_LIMIT,
        descriptor=descriptor,
        run_ids=tuple(sorted(run_ids)),
        metadata_completeness=_metadata_completeness_from_present(present_metadata),
        deck_outcomes=compact_runs,
        errors=tuple(error_budget.details),
        error_count=error_budget.count,
        replay_records=(
            retained
            if descriptor.kind is SourceKind.REPLAY_JSONL
            and record_count > INDEX_RECORD_LIMIT
            else None
        ),
    )


def _scan_jsonl_run(
    path: Path, run_id: str, *, include_all: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    error_budget = _ErrorBudget([])
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line, parse_constant=_reject_scan_constant)
                except ValueError as error:
                    detail = (
                        str(error)
                        if isinstance(error, _ScanConstantError)
                        else error.msg
                        if isinstance(error, json.JSONDecodeError)
                        else "invalid numeric literal"
                    )
                    error_budget.add(
                        f"{path.name}:{line_number}: invalid JSON: {detail}"
                    )
                    continue
                if not isinstance(record, dict):
                    error_budget.add(
                        f"{path.name}:{line_number}: expected an object record"
                    )
                    continue
                if include_all or run_id in _lightweight_run_ids([record]):
                    records.append(record)
    except (OSError, UnicodeDecodeError) as error:
        error_budget.add(f"{path.name}: could not rescan source: {error}")
    return records, error_budget.details


def _classify_jsonl_scan(
    *,
    record_count: int,
    types: set[str],
    events: set[str],
    has_action_command: bool,
    looks_like_boss_deck: bool,
) -> SourceDescriptor:
    if "state" in types or has_action_command:
        return SourceDescriptor(
            SourceKind.REPLAY_JSONL, record_count, "state/action replay"
        )
    if events & {"milestone", "card_pick", "outcome"}:
        return SourceDescriptor(
            SourceKind.DECK_HISTORY, record_count, "training deck history"
        )
    if "eval_result" in events:
        return SourceDescriptor(
            SourceKind.EVAL_RESULTS, record_count, "per-game evaluation results"
        )
    if events & {"result", "summary"} or looks_like_boss_deck:
        return SourceDescriptor(
            SourceKind.SUMMARY, record_count, "summary records; no replay states"
        )
    return SourceDescriptor(
        SourceKind.UNKNOWN, record_count, "unsupported JSON shape"
    )


def _update_compact_metadata(
    compact: _CompactRun, record: dict[str, Any]
) -> None:
    for attribute, keys in (
        ("character", ("character",)),
        ("seed", ("seed",)),
        ("game_version", ("game_version", "build_id")),
        ("experiment", ("experiment",)),
        ("checkpoint", ("checkpoint",)),
        ("evaluation_mode", ("evaluation_mode",)),
        ("scenario", ("scenario",)),
    ):
        candidates = {
            value
            for key in keys
            if (value := _first_scalar_text(record, key)) is not None
        }
        candidate = next(iter(candidates)) if len(candidates) == 1 else None
        current = getattr(compact, attribute)
        if len(candidates) > 1 or (
            current is not None and candidate is not None and current != candidate
        ):
            if attribute in COMPARISON_METADATA_FIELDS:
                compact.comparison_conflicts.add(attribute)
        if current is None and candidate is not None:
            setattr(compact, attribute, candidate)
    if compact.game_version_source is None:
        compact.game_version_source = _first_exact_text(
            record, "game_version_source"
        )
    ascension = _first_integral_int(record, "ascension")
    if (
        compact.ascension is not None
        and ascension is not None
        and compact.ascension != ascension
    ):
        compact.comparison_conflicts.add("ascension")
    if compact.ascension is None:
        compact.ascension = ascension
    multiplayer = record.get("is_multiplayer")
    if type(multiplayer) is bool and not compact.is_multiplayer_conflicted:
        if not compact.has_is_multiplayer_observation:
            compact.is_multiplayer = multiplayer
            compact.has_is_multiplayer_observation = True
        elif compact.is_multiplayer is not multiplayer:
            compact.is_multiplayer = None
            compact.is_multiplayer_conflicted = True


def _replay_scalar_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _safe_run_id(record.get(key))
        if value is not None:
            return value
    return None


def _replay_ascension(record: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = record.get(key)
        if type(value) is int and 0 <= value <= 10:
            return value
    return None


def _replay_modifiers(record: dict[str, Any]) -> tuple[str, ...] | None:
    value = record.get("modifiers")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _update_compact_replay_metadata(
    compact: _CompactRun, record: dict[str, Any]
) -> None:
    for attribute, keys in (
        ("character", ("character",)),
        ("seed", ("seed",)),
        ("game_version", ("game_version", "build_id")),
        ("experiment", ("experiment",)),
        ("checkpoint", ("checkpoint",)),
        ("evaluation_mode", ("evaluation_mode",)),
        ("scenario", ("scenario",)),
    ):
        if getattr(compact, attribute) is None:
            value = _replay_scalar_text(record, *keys)
            if value is not None:
                setattr(compact, attribute, value)
    if compact.game_version_source is None:
        compact.game_version_source = _first_exact_text(
            record, "game_version_source"
        )
    if compact.ascension is None:
        compact.ascension = _replay_ascension(record, "ascension")
    modifiers = _replay_modifiers(record)
    if modifiers is not None:
        compact.modifiers = modifiers


def _update_compact_timestamp_range(
    compact: _CompactRun, record: dict[str, Any]
) -> float | None:
    timestamp = _first_finite_number(record, "ts")
    if timestamp is None:
        return None
    compact.started_at = (
        timestamp
        if compact.started_at is None
        else min(compact.started_at, timestamp)
    )
    compact.latest_timestamp = (
        timestamp
        if compact.latest_timestamp is None
        else max(compact.latest_timestamp, timestamp)
    )
    return timestamp


def _update_observed_floor(
    compact: _CompactRun,
    floor: int | None,
    label: str | None = None,
) -> None:
    if floor is None:
        return
    compact.has_floor = True
    compact.first_recorded_floor = (
        floor
        if compact.first_recorded_floor is None
        else min(compact.first_recorded_floor, floor)
    )
    if compact.observed_max_floor is None or floor > compact.observed_max_floor:
        compact.observed_max_floor = floor
        compact.observed_max_floor_label = label


def _update_compact_eval(compact: _CompactRun, record: dict[str, Any]) -> None:
    _update_compact_metadata(compact, record)
    compact.started_at = _first_finite_number(
        record, "started_at", "start_ts"
    )
    compact.ended_at = _first_finite_number(record, "ended_at", "end_ts")
    if compact.ended_at is None:
        compact.ended_at = _first_finite_number(record, "ts", "timestamp")
    compact.max_global_floor = _first_integral_int(
        record, "max_global_floor", "max_floor", "floor"
    )
    compact.max_floor_label = _first_scalar_text(record, "max_floor_label")
    status, victory, technical_kind = _compact_status(record)
    compact.status = status
    compact.victory = victory
    compact.technical_failure_kind = technical_kind
    compact.has_outcome = status not in {
        RunStatus.UNKNOWN,
        RunStatus.IN_PROGRESS,
    }


def _update_compact_deck(compact: _CompactRun, record: dict[str, Any]) -> None:
    _update_compact_metadata(compact, record)
    timestamp = _update_compact_timestamp_range(compact, record)
    event = record.get("event")
    _update_observed_floor(
        compact, _first_integral_int(record, "floor_crossed", "floor")
    )
    compact.has_card_pick = compact.has_card_pick or event == "card_pick"
    if event == "map_snapshot":
        map_ordinal = compact.recorded_map_count
        compact.recorded_map_count += 1
        try:
            snapshot = parse_recorded_map_row(record)
        except RecordedMapError as error:
            if len(compact.warnings) < _RECORDED_MAP_WARNING_LIMIT:
                warning = f"row {map_ordinal}: {error}"[
                    :_RECORDED_MAP_WARNING_CHARS
                ]
                compact.warnings = (*compact.warnings, warning)
            return
        has_decisions = any(
            type(node) is dict
            and type(node.get("decisions")) is list
            and bool(node["decisions"])
            for node in snapshot.route_nodes
        )
        recorded_timestamp = record["ts"]
        last_route_floor = snapshot.route_nodes[-1]["global_floor"]
        retained = compact.latest_recorded_act_floors.get(snapshot.act_index)
        if retained is None or recorded_timestamp >= retained[0]:
            compact.latest_recorded_act_floors[snapshot.act_index] = (
                recorded_timestamp,
                last_route_floor,
            )
            compact.latest_recorded_act_decisions[snapshot.act_index] = (
                recorded_timestamp,
                has_decisions,
            )
        compact.has_valid_recorded_map = True
        return
    if event != "outcome":
        return
    compact.has_outcome = True
    compact.ended_at = timestamp
    compact.outcome_max_floor = _first_integral_int(
        record, "max_global_floor", "max_floor", "floor"
    )
    compact.max_floor_label = _first_scalar_text(record, "max_floor_label")
    status, victory, technical_kind = _compact_status(record)
    compact.status = status
    compact.victory = victory
    compact.technical_failure_kind = technical_kind


def _update_compact_replay(
    compact: _CompactRun, record: dict[str, Any]
) -> None:
    _update_compact_replay_metadata(compact, record)
    _update_compact_timestamp_range(compact, record)

    record_type = record.get("type")
    if not isinstance(record_type, str):
        record_type = ""
    compact.has_replay_action = compact.has_replay_action or (
        record_type == "action" and isinstance(record.get("data"), dict)
    )
    compact.has_replay_state = compact.has_replay_state or (
        record_type == "state" and isinstance(record.get("data"), dict)
    )

    status, victory, technical_kind = _compact_status(record)
    if status is not RunStatus.UNKNOWN:
        compact.status = status
        compact.victory = victory
        compact.technical_failure_kind = technical_kind
    event = record.get("event")
    is_terminal_event = isinstance(event, str) and event in {
        "outcome",
        "result",
        "eval_result",
    }
    if is_terminal_event or status is not RunStatus.UNKNOWN:
        compact.has_outcome = True

    data = record.get("data")
    if not isinstance(data, dict):
        return
    command = _first_scalar_text(data, "cmd", "decision")
    if command == "start_run":
        _update_compact_replay_metadata(compact, data)
    context = data.get("context")
    floor = None
    floor_label = None
    if isinstance(context, dict):
        local_floor = _first_integral_int(context, "floor")
        act = _first_integral_int(context, "act")
        if local_floor is not None:
            floor = (
                (act - 1) * 17 + local_floor
                if act is not None and act > 0
                else local_floor
            )
            floor_label = f"A{act or 1}F{local_floor}"
    if floor is None:
        floor = _first_integral_int(data, "global_floor", "floor")
    _update_observed_floor(compact, floor, floor_label)
    if record_type == "state":
        _update_compact_live_state(compact, data, context)

    nested_status, nested_victory, nested_technical_kind = _compact_status(data)
    if (
        status is RunStatus.UNKNOWN
        and nested_status is not RunStatus.UNKNOWN
    ):
        compact.status = nested_status
        compact.victory = nested_victory
        compact.technical_failure_kind = nested_technical_kind
    if (
        command == "game_over"
        or nested_status is not RunStatus.UNKNOWN
    ):
        compact.has_outcome = True


def _update_compact_live_state(
    compact: _CompactRun, data: dict[str, Any], context: Any
) -> None:
    """Remember where a replay's newest state row put the player."""

    act = floor = None
    if isinstance(context, dict):
        act = _first_integral_int(context, "act")
        floor = _first_integral_int(context, "floor")
    player = data.get("player")
    hp = max_hp = None
    if isinstance(player, dict):
        hp = _first_integral_int(player, "hp")
        max_hp = _first_integral_int(player, "max_hp")
    previous = compact.live_state
    if previous is not None:
        # A state row without a player block (or without a context) must not
        # erase what an earlier one said.
        act = previous[0] if act is None else act
        floor = previous[1] if floor is None else floor
        hp = previous[2] if hp is None else hp
        max_hp = previous[3] if max_hp is None else max_hp
    if act is not None or floor is not None or hp is not None or max_hp is not None:
        compact.live_state = (act, floor, hp, max_hp)


def _finalize_compact(compact: _CompactRun) -> None:
    if compact.source_kind is SourceKind.DECK_HISTORY:
        for act_index, (_timestamp, last_floor) in (
            compact.latest_recorded_act_floors.items()
        ):
            _update_observed_floor(compact, act_index * 17 + 1)
            _update_observed_floor(compact, last_floor)
        compact.max_global_floor = (
            compact.outcome_max_floor
            if compact.outcome_max_floor is not None
            else compact.observed_max_floor
        )
        if not compact.has_outcome:
            compact.ended_at = compact.latest_timestamp
    elif compact.source_kind is SourceKind.REPLAY_JSONL:
        compact.max_global_floor = compact.observed_max_floor
        compact.max_floor_label = compact.observed_max_floor_label
        compact.ended_at = compact.latest_timestamp


def _normalize_incomplete_replay(
    compact: _CompactRun,
    path: Path,
    replay_parser: Callable[[list[dict], str | None], dict] | None,
    source_name: str,
    scanned_records: list[dict[str, Any]] | None = None,
) -> str | None:
    """Normalize a replay with one transient whole-list parser invocation.

    Replay parsers have whole-list semantics, so incomplete replay indexing
    deliberately trusts transient memory here. Only the compact result and the
    scanner's bounded prefix survive this function; deck/eval sources never
    enter this path.

    ``scanned_records`` is the record list the index scan already parsed from
    this file (the same lines, filtered the same way); with it the file is not
    read a second time.  It is only handed over for a scan that read the whole
    file cleanly, so a read error still surfaces exactly as before: by not
    running the parser.
    """

    if replay_parser is None:
        return None
    full_records: list[dict[str, Any]] = []
    if scanned_records is not None:
        full_records = scanned_records
    else:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(
                            line, parse_constant=_reject_scan_constant
                        )
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    full_records.append(record)
        except (OSError, UnicodeDecodeError):
            return None
    try:
        candidate = replay_parser(full_records, source_name)
    except Exception as error:
        compact.replay_parser_rejected = True
        compact.first_recorded_floor = None
        compact.observed_max_floor = None
        compact.observed_max_floor_label = None
        compact.max_global_floor = None
        compact.max_floor_label = None
        return f"{source_name}: replay parser failed: {error}"
    finally:
        del full_records
    if not isinstance(candidate, dict):
        compact.replay_parser_rejected = True
        compact.first_recorded_floor = None
        compact.observed_max_floor = None
        compact.observed_max_floor_label = None
        compact.max_global_floor = None
        compact.max_floor_label = None
        return f"{source_name}: replay parser returned a non-object result"

    compact.replay_parser_succeeded = True
    summary = candidate.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    _apply_compact_replay_summary_metadata(compact, summary)
    if type(summary.get("has_state_records")) is bool:
        compact.has_replay_state = summary["has_state_records"]
    if type(summary.get("has_action_records")) is bool:
        compact.has_replay_action = summary["has_action_records"]
    summary_id = _replay_scalar_text(summary, "run_id")
    if summary_id is not None:
        compact.run_id = summary_id
        observed_ids = set(compact.replay_observed_ids)
        compact.replay_ids_omitted = (
            _add_bounded_replay_id(observed_ids, summary_id)
            or compact.replay_ids_omitted
        )
        compact.replay_observed_ids = tuple(sorted(observed_ids))
        compact.warnings = _compact_replay_identity_warnings(
            compact.run_id,
            observed_ids,
            compact.replay_ids_omitted,
        )

    rooms = candidate.get("rooms")
    nodes = (
        [node for node in rooms if isinstance(node, dict)]
        if isinstance(rooms, list)
        else []
    )
    compact.has_replay_nodes = bool(nodes)
    for node in nodes:
        _update_observed_floor(
            compact,
            _first_integral_int(node, "global_floor", "floor"),
            _first_scalar_text(node, "label"),
        )
    compact.has_node_decisions = any(
        _compact_node_has_decision_evidence(node) for node in nodes
    )
    compact.usable_per_node_replay = any(
        node.get("id") is not None
        and _compact_node_has_decision_evidence(node)
        for node in nodes
    )

    has_parser_coverage = all(
        key in summary
        for key in (
            "complete_run",
            "first_recorded_floor",
            "last_recorded_floor",
            "max_global_floor",
        )
    )
    if has_parser_coverage:
        compact.replay_parser_complete_run = summary.get("complete_run") is True
        compact.first_recorded_floor = _first_integral_int(
            summary, "first_recorded_floor"
        )
        compact.observed_max_floor = _first_integral_int(
            summary, "last_recorded_floor"
        )
        compact.observed_max_floor_label = _first_scalar_text(
            summary, "max_floor_label"
        )
        compact.max_global_floor = _first_integral_int(
            summary, "max_global_floor"
        )
        compact.max_floor_label = _first_scalar_text(summary, "max_floor_label")
    else:
        parser_max_floor = _first_integral_int(summary, "max_global_floor")
        if parser_max_floor is not None:
            compact.max_global_floor = parser_max_floor
            compact.max_floor_label = _first_scalar_text(summary, "max_floor_label")
        else:
            compact.max_global_floor = compact.observed_max_floor
            compact.max_floor_label = compact.observed_max_floor_label
    return None


def _apply_compact_replay_summary_metadata(
    compact: _CompactRun,
    summary: dict[str, Any],
) -> None:
    for attribute, keys in (
        ("character", ("character",)),
        ("seed", ("seed",)),
        ("game_version", ("game_version", "build_id")),
        ("checkpoint", ("checkpoint",)),
        ("evaluation_mode", ("evaluation_mode",)),
        ("scenario", ("scenario",)),
    ):
        value = _replay_scalar_text(summary, *keys)
        if value is not None:
            setattr(compact, attribute, value)
    version_source = _first_exact_text(summary, "game_version_source")
    if version_source is not None:
        compact.game_version_source = version_source
    ascension = _replay_ascension(summary, "ascension")
    if ascension is not None:
        compact.ascension = ascension
    modifiers = _replay_modifiers(summary)
    if modifiers is not None:
        compact.modifiers = modifiers


def _compact_node_has_decision_evidence(node: dict[str, Any]) -> bool:
    return any(
        isinstance(node.get(key), list) and bool(node[key])
        for key in ("actions", "decisions", "options", "choices")
    )


def _compact_status(
    record: dict[str, Any],
) -> tuple[RunStatus, bool | None, str | None]:
    raw_status = _first_scalar_text(
        record, "status", "end_reason", "technical_failure_kind"
    )
    aliases = {
        "won": "win",
        "victory": "win",
        "loss": "dead",
        "lost": "dead",
        "defeat": "dead",
        "reset-failure": "reset_failure",
    }
    normalized = aliases.get((raw_status or "").lower(), (raw_status or "").lower())
    try:
        status = RunStatus(normalized) if normalized else RunStatus.UNKNOWN
    except ValueError:
        status = RunStatus.UNKNOWN
    victory = next(
        (
            record[key]
            for key in ("victory", "won", "run_won")
            if isinstance(record.get(key), bool)
        ),
        None,
    )
    if status is RunStatus.WIN:
        victory = True
    elif status is RunStatus.DEAD or status.is_technical:
        victory = False
    elif status is RunStatus.UNKNOWN and victory is not None:
        status = RunStatus.WIN if victory else RunStatus.DEAD
    technical_kind = status.value if status.is_technical else None
    return status, victory, technical_kind


def _collect_metadata_presence(
    record: dict[str, Any], present: set[str]
) -> None:
    player: dict[str, Any] = {}
    players = record.get("players")
    if isinstance(players, list) and players and isinstance(players[0], dict):
        player = players[0]
    if _present(record.get("character")) or _present(player.get("character")):
        present.add("character")
    if _present(record.get("game_version")) or _present(record.get("build_id")):
        present.add("game_version")
    for field in ("seed", "checkpoint", "evaluation_mode", "scenario", "ascension"):
        if _present(record.get(field)):
            present.add(field)


def _metadata_completeness_from_present(present: set[str]) -> dict[str, Any]:
    fields = list(_METADATA_FIELDS)
    return {
        "present_fields": sorted(present),
        "missing_fields": [field for field in fields if field not in present],
        "present_count": len(present),
        "total_fields": len(fields),
        "score": len(present) / len(fields),
    }


def _first_scalar_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def _first_exact_text(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if type(value) is str and value.strip():
            return value.strip()
    return None


def _first_integral_int(record: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if (
            isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        ):
            return int(value)
    return None


def _first_finite_number(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            continue
        if math.isfinite(numeric):
            return numeric
    return None


def _looks_like_workbench_json(path: Path) -> bool:
    if path.name.lower().endswith(".meta.json"):
        return False
    try:
        with path.open("rb") as handle:
            prefix = handle.read(_WORKBENCH_JSON_PROBE_BYTES)
    except OSError:
        return True
    if any(
        all(marker in prefix for marker in group)
        for group in _WORKBENCH_JSON_MARKER_GROUPS
    ) or all(marker in prefix for marker in _BOSS_DECK_JSON_MARKERS):
        return True
    filename_tokens = {
        token
        for token in re.split(r"[^a-z0-9]+", path.stem.lower())
        if token
    }
    return bool(filename_tokens & _WORKBENCH_FILENAME_TOKENS)


def _path_redactions(path: Path, root: Path, source_id: str) -> dict[str, str]:
    redactions = {str(path): source_id}
    if root.parent != root:
        redactions[str(root)] = "<source-root>"
    return redactions


def _source_redactions(source: _IndexedSource) -> dict[str, str]:
    return _path_redactions(source.path, source.root, source.source_id)


def _cohort_id(key: tuple[Any, ...]) -> str:
    rendered = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
    return "cohort_" + sha256(rendered.encode("utf-8")).hexdigest()[:20]


def _safe_catalog_scalar(value: Any) -> Any:
    if type(value) is str and not is_unicode_scalar_text(value):
        return None
    return value


def _sortable_key(values: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple("" if value is None else str(value) for value in values)


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while current != root:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def _lightweight_run_ids(records: list[dict[str, Any]]) -> set[str]:
    run_ids: set[str] = set()
    for record in records:
        candidates = [record.get("run_id")]
        data = record.get("data")
        if isinstance(data, dict):
            candidates.append(data.get("run_id"))
        for value in candidates:
            safe_value = _safe_run_id(value)
            if safe_value is not None:
                run_ids.add(safe_value)
    return run_ids


def _metadata_completeness(records: list[dict[str, Any]]) -> dict[str, Any]:
    present: set[str] = set()
    for record in records:
        player = {}
        players = record.get("players")
        if isinstance(players, list) and players and isinstance(players[0], dict):
            player = players[0]
        if _present(record.get("character")) or _present(player.get("character")):
            present.add("character")
        if _present(record.get("game_version")) or _present(record.get("build_id")):
            present.add("game_version")
        for field in ("seed", "checkpoint", "evaluation_mode", "scenario", "ascension"):
            if _present(record.get(field)):
                present.add(field)
    fields = list(_METADATA_FIELDS)
    return {
        "present_fields": sorted(present),
        "missing_fields": [field for field in fields if field not in present],
        "present_count": len(present),
        "total_fields": len(fields),
        "score": len(present) / len(fields),
    }


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


class _UploadConstantError(ValueError):
    pass


def _reject_upload_constant(value: str) -> None:
    raise _UploadConstantError(f"non-standard numeric constant {value}")


def _read_upload_records(source_name: str, text: str) -> list[dict[str, Any]]:
    suffix = Path(source_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SourceFormatError(
            f"{source_name}: unsupported source suffix {Path(source_name).suffix!r}"
        )
    if suffix == ".jsonl":
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_constant=_reject_upload_constant)
            except _UploadConstantError as error:
                raise SourceFormatError(
                    f"{source_name}:{line_number}: invalid JSON: {error}"
                ) from error
            except json.JSONDecodeError as error:
                raise SourceFormatError(
                    f"{source_name}:{line_number}: invalid JSON: {error.msg}"
                ) from error
            if not isinstance(record, dict):
                raise SourceFormatError(
                    f"{source_name}:{line_number}: expected an object record"
                )
            records.append(record)
        return records
    try:
        value = json.loads(text, parse_constant=_reject_upload_constant)
    except _UploadConstantError as error:
        line_number = (
            text.count("\n", 0, max(text.find(str(error).split()[-1]), 0)) + 1
        )
        raise SourceFormatError(
            f"{source_name}:{line_number}: invalid JSON: {error}"
        ) from error
    except json.JSONDecodeError as error:
        raise SourceFormatError(
            f"{source_name}:{error.lineno}: invalid JSON: {error.msg}"
        ) from error
    if isinstance(value, dict):
        return [value]
    if suffix == ".run":
        raise SourceFormatError(f"{source_name}: expected a top-level object for .run")
    if not isinstance(value, list):
        raise SourceFormatError(
            f"{source_name}:top-level: expected an object or list of objects"
        )
    records = []
    for index, record in enumerate(value, start=1):
        if not isinstance(record, dict):
            raise SourceFormatError(f"{source_name}:{index}: expected an object record")
        records.append(record)
    return records


def _adapted_upload_view(source_name: str, adapted: AdaptedSource) -> dict[str, Any]:
    base = {
        "source_name": source_name,
        "source_kind": adapted.descriptor.kind.value,
        "errors": list(adapted.errors),
    }
    if adapted.summary is not None:
        return {"view": "summary", **base, "summary": deepcopy(adapted.summary)}
    if not adapted.runs:
        return {"view": "error", **base}
    return {
        "view": "run" if len(adapted.runs) == 1 else "runs",
        **base,
        "runs": [record.to_dict() for record in adapted.runs],
    }


def _legacy_progress(record: RunRecord) -> dict[str, Any]:
    return {
        "summary": {
            **record.metadata.__dict__,
            **record.outcome.__dict__,
        },
        "rooms": deepcopy(record.nodes),
    }


def _scrub_paths(value: Any, path_ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_paths(item, path_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_paths(item, path_ids) for item in value]
    if isinstance(value, str):
        for path, source_id in sorted(
            path_ids.items(), key=lambda item: len(item[0]), reverse=True
        ):
            value = value.replace(path, source_id)
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("catalog response contains a non-finite number")
    return value
