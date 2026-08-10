"""Traversal-safe, lazy catalog for training workbench run artifacts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import stat as stat_module
from threading import RLock
from typing import Any, Callable, Iterable

from agent.run_metadata import (
    MAX_RUN_ID_LENGTH,
    is_unicode_scalar_text,
    record_run_id,
    safe_run_id as _safe_run_id,
)

from .adapters import AdaptedSource, adapt_records
from .joiner import join_records
from .metrics import (
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
from .sources import SourceDescriptor, SourceFormatError, classify_records, read_json_records


SUPPORTED_SUFFIXES = frozenset({".run", ".json", ".jsonl"})
INDEX_RECORD_LIMIT = 512
COHORT_ID_SAMPLE_LIMIT = 100
SOURCE_REF_LIMIT = 32
REPLAY_WARNING_ID_LIMIT = 16
RUN_ID_LENGTH_LIMIT = MAX_RUN_ID_LENGTH
ERROR_DETAIL_LIMIT = 32
_WORKBENCH_JSON_PROBE_BYTES = 64 * 1024
_WORKBENCH_JSON_MARKERS = (
    b'"players"',
    b'"map_point_history"',
    b'"event"',
    b'"type"',
    b'"run_id"',
    b'"rooms"',
)
_BOSS_DECK_JSON_MARKERS = (
    b'"checkpoint"',
    b'"cards"',
    b'"enemies"',
    b'"hp_at_entry"',
)
_WORKBENCH_FILENAME_TOKENS = frozenset(
    {
        "boss",
        "checkpoint",
        "cohort",
        "deck",
        "eval",
        "evaluation",
        "history",
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
    checkpoint: str | None = None
    evaluation_mode: str | None = None
    scenario: str | None = None
    ascension: int | None = None
    modifiers: tuple[str, ...] = ()
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
    has_outcome: bool = False
    has_floor: bool = False
    has_card_pick: bool = False
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
                checkpoint=self.checkpoint,
                evaluation_mode=self.evaluation_mode,
                scenario=self.scenario,
                ascension=self.ascension,
                modifiers=self.modifiers,
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
                visited_route=visited_route,
                node_rewards=(
                    self.has_card_pick
                    if self.source_kind is SourceKind.DECK_HISTORY
                    else False
                ),
                decisions=(
                    self.has_replay_action or self.has_node_decisions
                    if self.source_kind is SourceKind.REPLAY_JSONL
                    else self.has_card_pick
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


_CohortItem = RunRecord | _CompactRun


class RunCatalog:
    """Discover and lazily normalize supported artifacts under explicit roots."""

    def __init__(
        self,
        roots: Iterable[Path],
        replay_parser: Callable[[list[dict], str | None], dict] | None = None,
        *,
        include_policy: str = "all",
    ) -> None:
        if include_policy not in {"all", "workbench"}:
            raise ValueError("include_policy must be 'all' or 'workbench'")
        self.roots = tuple(sorted({Path(root).resolve() for root in roots}, key=str))
        self.replay_parser = replay_parser
        self.include_policy = include_policy
        self._sources: dict[str, _IndexedSource] = {}
        self._run_sources: dict[str, tuple[str, ...]] = {}
        self._adapt_cache: dict[tuple[Path, int, int], AdaptedSource] = {}
        self._cohort_records: dict[str, tuple[_CohortItem, ...]] = {}
        self._cohort_descriptors: list[dict[str, Any]] = []
        self._cohort_cache_key: tuple[tuple[Path, int, int], ...] | None = None
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
                    errors.extend(source.entry["errors"])
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
            merged = join_records(matched)
            if len(merged) != 1:
                raise CatalogError(f"ambiguous run id: {run_id}")
            payload = _scrub_paths(merged[0].to_dict(), path_ids)
            return {
                "view": "run",
                "run": payload,
                "sources": sorted(sources, key=lambda item: item["source_id"]),
                "errors": _scrub_paths(list(dict.fromkeys(errors)), path_ids),
            }

    def list_cohorts(self) -> list[dict[str, Any]]:
        with self._lock:
            descriptors = self._build_cohorts()
            return deepcopy(descriptors)

    def get_cohort_records(self, cohort_id: str) -> tuple[RunRecord, ...]:
        with self._lock:
            self._build_cohorts()
            return tuple(self._iter_cohort_records(self._cohort_items_for_id(cohort_id)))

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
                baseline_summary = summarize_cohort(
                    self._iter_cohort_records(baseline)
                ).to_dict()
                comparison = compare_cohorts(
                    self._iter_cohort_records(current),
                    self._iter_cohort_records(baseline),
                ).to_dict()
            return {
                "current_cohort_id": current_id,
                "baseline_cohort_id": baseline_id,
                "current": summarize_cohort(
                    self._iter_cohort_records(current)
                ).to_dict(),
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
        for item in items:
            if isinstance(item, _CompactRun):
                yield item.to_record()
            else:
                yield deepcopy(item)

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
        run_sources: dict[str, list[str]] = {}
        for root, path in discovered:
            try:
                file_stat = path.stat()
                if not stat_module.S_ISREG(file_stat.st_mode):
                    continue
                relative = path.relative_to(root).as_posix()
                source_id = _source_id(root, relative)
                cache_key = (path, file_stat.st_mtime_ns, file_stat.st_size)
                previous = self._sources.get(source_id)
                source = (
                    previous
                    if previous is not None and previous.cache_key == cache_key
                    else self._index_source(root, path, file_stat)
                )
            except OSError:
                continue
            indexed[source.source_id] = source
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
        elif errors and records_complete:
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

    def _build_cohorts(self) -> list[dict[str, Any]]:
        self._refresh()
        cache_key = tuple(
            source.cache_key for source in self._ordered_sources()
        )
        if self._cohort_cache_key == cache_key:
            return deepcopy(self._cohort_descriptors)
        records: list[RunRecord] = []
        compact_records: list[_CompactRun] = []
        for source in self._ordered_sources():
            if source.entry["open_mode"] != "run":
                continue
            if not source.records_complete and source.deck_outcomes:
                compact_records.extend(source.deck_outcomes)
            else:
                records.extend(self._public_records(source, self._adapt(source)))
        version_source_evidence_by_run_id: dict[
            str, _GameVersionSourceEvidence
        ] = {}
        for candidates in (records, compact_records):
            for record in candidates:
                run_id = _safe_run_id(record.run_id)
                if run_id is None:
                    continue
                evidence = version_source_evidence_by_run_id.setdefault(
                    run_id, _GameVersionSourceEvidence()
                )
                evidence.observe(_item_metadata(record).game_version_source)
        joined = join_records(records)
        merged = _merge_compact_records(joined, compact_records)
        eligible: list[_CohortItem] = [
            record
            for record in merged
            if _item_status(record) in {RunStatus.WIN, RunStatus.DEAD}
            or _item_status(record).is_technical
        ]
        grouped: dict[tuple[Any, ...], list[_CohortItem]] = {}
        for record in eligible:
            metadata = _item_metadata(record)
            checkpoint = metadata.checkpoint
            fallback = checkpoint or f"source:{record.source_id}"
            key = (
                fallback,
                metadata.character,
                metadata.game_version,
                metadata.evaluation_mode,
                metadata.scenario,
                metadata.ascension,
            )
            grouped.setdefault(key, []).append(record)

        descriptors: list[dict[str, Any]] = []
        cohort_records: dict[str, tuple[_CohortItem, ...]] = {}
        for key, group in sorted(
            grouped.items(), key=lambda item: _sortable_key(item[0])
        ):
            (
                checkpoint_or_source,
                character,
                version,
                mode,
                scenario,
                ascension,
            ) = key
            cohort_id = _cohort_id(key)
            ordered = tuple(
                sorted(group, key=lambda record: (record.run_id, record.source_id))
            )
            cohort_records[cohort_id] = ordered
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
            checkpoint = (
                checkpoint_or_source
                if isinstance(checkpoint_or_source, str)
                and not checkpoint_or_source.startswith("source:")
                else None
            )
            version_source_evidence = _GameVersionSourceEvidence()
            for record in ordered:
                run_id = _safe_run_id(record.run_id)
                if run_id is not None:
                    evidence = version_source_evidence_by_run_id.get(
                        run_id
                    )
                    if evidence is not None:
                        version_source_evidence.merge(evidence)
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
            readiness = describe_comparison_readiness(
                self._iter_cohort_records(ordered)
            )
            label_parts = [
                _safe_catalog_scalar(checkpoint or checkpoint_or_source),
                _safe_catalog_scalar(character),
                _safe_catalog_scalar(version),
                _safe_catalog_scalar(mode),
                _safe_catalog_scalar(scenario),
                f"A{ascension}"
                if type(ascension) is int and ascension >= 0
                else "A?",
            ]
            descriptors.append(
                {
                    "cohort_id": cohort_id,
                    "label": " · ".join(str(value) for value in label_parts if value),
                    "filters": filters,
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
            )
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
        self._cohort_descriptors = sorted_descriptors
        self._cohort_cache_key = cache_key
        return deepcopy(self._cohort_descriptors)


def _source_id(root: Path, relative: str) -> str:
    digest = sha256(f"{root}\0{relative}".encode("utf-8")).hexdigest()[:20]
    return f"src_{digest}"


def _merge_compact_records(
    ordinary: list[RunRecord], compact: list[_CompactRun]
) -> list[_CohortItem]:
    """Merge exact IDs while expanding only IDs that occur in multiple sources."""

    ordinary_identified = sorted(
        (record for record in ordinary if record.run_id),
        key=lambda record: record.run_id,
    )
    compact_identified = sorted(
        (record for record in compact if record.run_id),
        key=lambda record: (record.run_id, record.source_id),
    )
    merged: list[_CohortItem] = []
    ordinary_index = 0
    compact_index = 0
    while (
        ordinary_index < len(ordinary_identified)
        or compact_index < len(compact_identified)
    ):
        ordinary_run_id = (
            ordinary_identified[ordinary_index].run_id
            if ordinary_index < len(ordinary_identified)
            else None
        )
        compact_run_id = (
            compact_identified[compact_index].run_id
            if compact_index < len(compact_identified)
            else None
        )
        if compact_run_id is None or (
            ordinary_run_id is not None and ordinary_run_id < compact_run_id
        ):
            merged.append(ordinary_identified[ordinary_index])
            ordinary_index += 1
            continue

        compact_end = compact_index + 1
        while (
            compact_end < len(compact_identified)
            and compact_identified[compact_end].run_id == compact_run_id
        ):
            compact_end += 1
        compact_group = compact_identified[compact_index:compact_end]
        if ordinary_run_id == compact_run_id:
            merged.extend(
                join_records(
                    [
                        ordinary_identified[ordinary_index],
                        *(record.to_record() for record in compact_group),
                    ]
                )
            )
            ordinary_index += 1
        elif len(compact_group) == 1:
            merged.append(compact_group[0])
        else:
            merged.extend(join_records(record.to_record() for record in compact_group))
        compact_index = compact_end

    merged.extend(record for record in ordinary if not record.run_id)
    merged.extend(record for record in compact if not record.run_id)
    return merged


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
        checkpoint=record.checkpoint,
        evaluation_mode=record.evaluation_mode,
        scenario=record.scenario,
        ascension=record.ascension,
        modifiers=record.modifiers,
        started_at=record.started_at,
        ended_at=record.ended_at,
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
                except json.JSONDecodeError as error:
                    error_budget.add(
                        f"{path.name}:{line_number}: invalid JSON: {error.msg}"
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
                event = str(record.get("event", ""))
                if event in {
                    "milestone",
                    "card_pick",
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
                is_replay_row = record_type in {"state", "action"}
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
                elif event in {"milestone", "card_pick", "outcome"}:
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
        error_budget.add(f"{path.name}: invalid UTF-8 at byte {error.start}")
    except OSError as error:
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
                except (_ScanConstantError, json.JSONDecodeError) as error:
                    detail = (
                        str(error)
                        if isinstance(error, _ScanConstantError)
                        else error.msg
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
    _update_observed_floor(
        compact, _first_integral_int(record, "floor_crossed", "floor")
    )
    event = record.get("event")
    compact.has_card_pick = compact.has_card_pick or event == "card_pick"
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


def _finalize_compact(compact: _CompactRun) -> None:
    if compact.source_kind is SourceKind.DECK_HISTORY:
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
) -> str | None:
    """Normalize a replay with one transient whole-list parser invocation.

    Replay parsers have whole-list semantics, so incomplete replay indexing
    deliberately trusts transient memory here. Only the compact result and the
    scanner's bounded prefix survive this function; deck/eval sources never
    enter this path.
    """

    if replay_parser is None:
        return None
    full_records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(
                        line, parse_constant=_reject_scan_constant
                    )
                except (_ScanConstantError, json.JSONDecodeError):
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
    if any(marker in prefix for marker in _WORKBENCH_JSON_MARKERS) or all(
        marker in prefix for marker in _BOSS_DECK_JSON_MARKERS
    ):
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
