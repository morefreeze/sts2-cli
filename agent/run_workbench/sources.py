"""Read and classify supported training-run data sources without parsing runs."""

from dataclasses import dataclass
import json
from pathlib import Path

from .models import SourceKind


class SourceFormatError(ValueError):
    """Raised when a supported source file cannot be read as object records."""


@dataclass(frozen=True)
class SourceDescriptor:
    kind: SourceKind
    record_count: int
    message: str


def read_json_records(path: Path) -> list[dict]:
    """Read object records from a native history, JSON, or JSONL file."""
    suffix = path.suffix.lower()
    if suffix not in {".run", ".json", ".jsonl"}:
        raise SourceFormatError(f"{path.name}: unsupported source suffix {path.suffix!r}")

    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise SourceFormatError(
            f"{path.name}: invalid UTF-8 at byte {error.start}"
        ) from error
    except OSError as error:
        raise SourceFormatError(f"{path.name}: could not read source: {error}") from error

    if suffix == ".jsonl":
        return _read_jsonl_records(path, contents)
    return _read_json_document_records(path, contents, suffix)


def _read_json_document_records(path: Path, contents: str, suffix: str) -> list[dict]:
    try:
        value = json.loads(contents)
    except json.JSONDecodeError as error:
        raise SourceFormatError(f"{path.name}:{error.lineno}: invalid JSON: {error.msg}") from error

    if isinstance(value, dict):
        return [value]
    if suffix == ".run":
        raise SourceFormatError(f"{path.name}: expected a top-level object for .run")
    if not isinstance(value, list):
        raise SourceFormatError(f"{path.name}:top-level: expected an object or list of objects")

    records: list[dict] = []
    for index, record in enumerate(value, start=1):
        if not isinstance(record, dict):
            raise SourceFormatError(f"{path.name}:{index}: expected an object record")
        records.append(record)
    return records


def _read_jsonl_records(path: Path, contents: str) -> list[dict]:
    records: list[dict] = []
    for line_number, line in enumerate(contents.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SourceFormatError(
                f"{path.name}:{line_number}: invalid JSON: {error.msg}"
            ) from error
        if not isinstance(record, dict):
            raise SourceFormatError(f"{path.name}:{line_number}: expected an object record")
        records.append(record)
    return records


def classify_records(records: list[dict], *, suffix: str) -> SourceDescriptor:
    """Classify records by their content with fixed precedence."""
    suffix = suffix.lower()
    if suffix == ".run" or (
        len(records) == 1
        and isinstance(records[0].get("players"), list)
        and "map_point_history" in records[0]
    ):
        return SourceDescriptor(SourceKind.NATIVE_RUN, len(records), "native run history")

    types = {str(row.get("type", "")) for row in records}
    events = {str(row.get("event", "")) for row in records}
    has_action_command = any(
        row.get("type") == "action" and isinstance(row.get("data"), dict)
        for row in records
    )
    if "state" in types or has_action_command:
        return SourceDescriptor(SourceKind.REPLAY_JSONL, len(records), "state/action replay")
    if events & {"milestone", "card_pick", "outcome"}:
        return SourceDescriptor(SourceKind.DECK_HISTORY, len(records), "training deck history")
    if "eval_result" in events:
        return SourceDescriptor(
            SourceKind.EVAL_RESULTS, len(records), "per-game evaluation results"
        )
    looks_like_boss_deck = any(
        {"checkpoint", "cards", "enemies", "hp_at_entry"}.issubset(row) for row in records
    )
    if events & {"result", "summary"} or looks_like_boss_deck:
        return SourceDescriptor(
            SourceKind.SUMMARY, len(records), "summary records; no replay states"
        )
    return SourceDescriptor(SourceKind.UNKNOWN, len(records), "unsupported JSON shape")


def classify_path(path: Path) -> SourceDescriptor:
    """Read a source file and describe its supported data shape."""
    suffix = path.suffix.lower()
    return classify_records(read_json_records(path), suffix=suffix)
