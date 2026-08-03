"""Traversal-safe, lazy catalog for training workbench run artifacts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable

from .adapters import AdaptedSource, adapt_path, adapt_records
from .joiner import join_records
from .metrics import compare_cohorts, summarize_cohort
from .models import RunRecord, RunStatus, SourceKind
from .sources import SourceDescriptor, SourceFormatError, classify_records, read_json_records


SUPPORTED_SUFFIXES = frozenset({".run", ".json", ".jsonl"})
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
    run_ids: tuple[str, ...]
    cache_key: tuple[Path, int, int]


class RunCatalog:
    """Discover and lazily normalize supported artifacts under explicit roots."""

    def __init__(
        self,
        roots: Iterable[Path],
        replay_parser: Callable[[list[dict], str | None], dict] | None = None,
    ) -> None:
        self.roots = tuple(sorted({Path(root).resolve() for root in roots}, key=str))
        self.replay_parser = replay_parser
        self._sources: dict[str, _IndexedSource] = {}
        self._run_sources: dict[str, tuple[str, ...]] = {}
        self._adapt_cache: dict[tuple[Path, int, int], AdaptedSource] = {}
        self._cohort_records: dict[str, tuple[RunRecord, ...]] = {}

    def list_sources(self) -> list[dict[str, Any]]:
        self._refresh()
        return [deepcopy(source.entry) for source in self._ordered_sources()]

    def get_source(self, source_id: str) -> dict[str, Any]:
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
        adapted = self._adapt(source)
        return self._source_view(source, adapted)

    def get_run(self, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise CatalogError("run id must be a non-empty string")
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
        descriptors = self._build_cohorts()
        return deepcopy(descriptors)

    def get_cohort_records(self, cohort_id: str) -> tuple[RunRecord, ...]:
        self._build_cohorts()
        records = self._cohort_records.get(cohort_id)
        if records is None:
            raise CatalogNotFoundError(f"unknown cohort id: {cohort_id}")
        return tuple(deepcopy(record) for record in records)

    def get_metrics(
        self, current_id: str, baseline_id: str | None = None
    ) -> dict[str, Any]:
        current = self.get_cohort_records(current_id)
        comparison = None
        baseline_summary = None
        if baseline_id is not None:
            baseline = self.get_cohort_records(baseline_id)
            baseline_summary = summarize_cohort(baseline).to_dict()
            comparison = compare_cohorts(current, baseline).to_dict()
        return {
            "current_cohort_id": current_id,
            "baseline_cohort_id": baseline_id,
            "current": summarize_cohort(current).to_dict(),
            "baseline": baseline_summary,
            "comparison": comparison,
        }

    def parse_upload(self, source_name: str, text: str) -> dict[str, Any]:
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
            source = self._index_source(root, path)
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
                seen.add(resolved)
                discovered.append((root, resolved))
        return discovered

    def _index_source(self, root: Path, path: Path) -> _IndexedSource:
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        source_id = _source_id(root, relative)
        display_name = relative
        if len(self.roots) > 1:
            display_name = f"{root.name}/{relative}"
        errors: list[str] = []
        records: list[dict[str, Any]] | None
        try:
            records = read_json_records(path)
            descriptor = classify_records(records, suffix=path.suffix)
        except SourceFormatError as error:
            records = None
            descriptor = SourceDescriptor(SourceKind.UNKNOWN, 0, str(error))
            errors.append(str(error))
        if descriptor.kind is SourceKind.SUMMARY:
            open_mode = "summary"
        elif descriptor.kind is SourceKind.UNKNOWN:
            open_mode = "error"
            if not errors:
                errors.append(f"{path.name}: {descriptor.message}")
        else:
            open_mode = "run"
        metadata = _metadata_completeness(records or [])
        redactions = _path_redactions(path, root, source_id)
        public_errors = _scrub_paths(errors, redactions)
        public_message = _scrub_paths(descriptor.message, redactions)
        entry = {
            "source_id": source_id,
            "display_name": display_name,
            "source_kind": descriptor.kind.value,
            "open_mode": open_mode,
            "mtime": stat.st_mtime,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "record_count": descriptor.record_count,
            "message": public_message,
            "errors": public_errors,
            "metadata_completeness": metadata,
        }
        return _IndexedSource(
            source_id=source_id,
            root=root,
            path=path,
            entry=entry,
            descriptor=descriptor,
            records=tuple(deepcopy(records)) if records is not None else None,
            run_ids=tuple(sorted(_lightweight_run_ids(records or []))),
            cache_key=(path, stat.st_mtime_ns, stat.st_size),
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
        adapted = adapt_path(source.path, replay_parser=self.replay_parser)
        self._adapt_cache[source.cache_key] = adapted
        return adapted

    def _public_records(
        self, source: _IndexedSource, adapted: AdaptedSource
    ) -> tuple[RunRecord, ...]:
        public: list[RunRecord] = []
        for record in adapted.runs:
            clone = deepcopy(record)
            clone.source_id = source.source_id
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
        records: list[RunRecord] = []
        for source in self._ordered_sources():
            if source.entry["open_mode"] != "run":
                continue
            records.extend(self._public_records(source, self._adapt(source)))
        joined = join_records(records)
        eligible = [
            record
            for record in joined
            if record.outcome.status in {RunStatus.WIN, RunStatus.DEAD}
            or record.outcome.status.is_technical
        ]
        grouped: dict[tuple[Any, ...], list[RunRecord]] = {}
        for record in eligible:
            checkpoint = record.metadata.checkpoint
            fallback = checkpoint or f"source:{record.source_id}"
            key = (
                fallback,
                record.metadata.character,
                record.metadata.game_version,
                record.metadata.evaluation_mode,
                record.metadata.scenario,
            )
            grouped.setdefault(key, []).append(record)

        descriptors: list[dict[str, Any]] = []
        cohort_records: dict[str, tuple[RunRecord, ...]] = {}
        for key, group in sorted(
            grouped.items(), key=lambda item: _sortable_key(item[0])
        ):
            checkpoint_or_source, character, version, mode, scenario = key
            cohort_id = _cohort_id(key)
            ordered = tuple(
                sorted(group, key=lambda record: (record.run_id, record.source_id))
            )
            cohort_records[cohort_id] = ordered
            source_refs = sorted(
                {
                    source_id
                    for record in ordered
                    for source_id in record.source_id.split(" | ")
                    if source_id
                }
            )
            checkpoint = (
                checkpoint_or_source
                if isinstance(checkpoint_or_source, str)
                and not checkpoint_or_source.startswith("source:")
                else None
            )
            filters = {
                "checkpoint": checkpoint,
                "character": character,
                "game_version": version,
                "evaluation_mode": mode,
                "scenario": scenario,
            }
            label_parts = [
                checkpoint or checkpoint_or_source,
                character,
                version,
                mode,
                scenario,
            ]
            descriptors.append(
                {
                    "cohort_id": cohort_id,
                    "label": " · ".join(str(value) for value in label_parts if value),
                    "filters": filters,
                    "run_count": len(ordered),
                    "run_ids": sorted(record.run_id for record in ordered if record.run_id),
                    "source_refs": source_refs,
                    "technical_count": sum(
                        record.outcome.status.is_technical for record in ordered
                    ),
                }
            )
        self._cohort_records = cohort_records
        return sorted(descriptors, key=lambda item: (item["label"], item["cohort_id"]))


def _source_id(root: Path, relative: str) -> str:
    digest = sha256(f"{root}\0{relative}".encode("utf-8")).hexdigest()[:20]
    return f"src_{digest}"


def _path_redactions(path: Path, root: Path, source_id: str) -> dict[str, str]:
    redactions = {str(path): source_id}
    if root.parent != root:
        redactions[str(root)] = "<source-root>"
    return redactions


def _source_redactions(source: _IndexedSource) -> dict[str, str]:
    return _path_redactions(source.path, source.root, source.source_id)


def _cohort_id(key: tuple[Any, ...]) -> str:
    rendered = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return "cohort_" + sha256(rendered.encode("utf-8")).hexdigest()[:20]


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
            if isinstance(value, str) and value:
                run_ids.add(value)
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
