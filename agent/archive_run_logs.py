#!/usr/bin/env python3
"""archive_run_logs.py — Retention tool for GameLogger run logs.

`python/game_log.py` (via `agent/eval_rl.py --game-log`) writes one
`*.jsonl` file per run to `logs/`, named `<YYYYMMDD_HHMMSS>_<character>_
<seed>.jsonl`. Runs accumulate fast — hundreds of files, gigabytes — and
every single one gets parsed on every page load by
`agent/run_workbench/catalog.py`, which discovers files under its
configured roots (`logs/`, `data/`) with `Path.rglob("*")`. `rglob`
recurses into *any* subdirectory it finds, so an "archive" folder created
underneath `logs/` (e.g. `logs/archive/`) would still be walked and parsed
on every load — that would fix nothing.

This tool keeps the workbench fast by moving old run logs to durable
storage *outside* both catalog roots: `~/.sts2-train/log_archive/`
(NOT `/tmp` — this machine wipes `/tmp` on idle sleep). Archived files are
grouped into per-day subdirectories there
(`~/.sts2-train/log_archive/<YYYY-MM-DD>/`) so months of history stay easy
to browse instead of becoming one giant flat folder.

Retention policy: keep the newest `--keep` (default 30) runs per calendar
day in `logs/`; move everything older to the archive. Both "day" and
"newest" are derived from the filename's `YYYYMMDD_HHMMSS_` prefix,
falling back to the file's mtime for any name that doesn't match that
pattern. The fallback is per-file, not all-or-nothing for the directory.

Only files directly inside `--log-dir` are considered (no recursion), so
this never reaches into an existing archive or any other subdirectory.

Usage:
    # Preview only — nothing is moved.
    .venv/bin/python agent/archive_run_logs.py

    # Actually move files.
    .venv/bin/python agent/archive_run_logs.py --apply

    # Custom keep count / directories (mainly for tests).
    .venv/bin/python agent/archive_run_logs.py --keep 10 \\
        --log-dir /tmp/logs --archive-dir /tmp/archive --apply
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DEFAULT_KEEP = 30
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_ARCHIVE_DIR = Path.home() / ".sts2-train" / "log_archive"

_NAME_TIMESTAMP_RE = re.compile(r"^(\d{8})_(\d{6})_")


@dataclass(frozen=True)
class _LogFile:
    """One discovered `*.jsonl` run log with recency/day resolved up front.

    `size_bytes` is captured at discovery time (before any move) so the
    report table stays accurate even after `execute_archive` has already
    relocated the file.
    """

    path: Path
    sort_key: datetime
    day: date
    size_bytes: int


def _sort_key_for(path: Path) -> tuple[datetime, date]:
    """Return (recency key, calendar day) for one log file.

    Prefers the `YYYYMMDD_HHMMSS_` prefix GameLogger bakes into the
    filename; falls back to the file's mtime when the name doesn't match
    (unrecognized naming, or an invalid date/time in an otherwise
    matching prefix).
    """
    match = _NAME_TIMESTAMP_RE.match(path.name)
    if match is not None:
        try:
            when = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
        else:
            return when, when.date()
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    return mtime, mtime.date()


def _discover(log_dir: Path) -> list[_LogFile]:
    """List `*.jsonl` files directly inside `log_dir` — no recursion."""
    files: list[_LogFile] = []
    for path in log_dir.glob("*.jsonl"):
        if not path.is_file():
            continue
        sort_key, day = _sort_key_for(path)
        files.append(
            _LogFile(
                path=path,
                sort_key=sort_key,
                day=day,
                size_bytes=path.stat().st_size,
            )
        )
    return files


def _group_by_day(files: list[_LogFile]) -> dict[date, list[_LogFile]]:
    groups: dict[date, list[_LogFile]] = {}
    for log_file in files:
        groups.setdefault(log_file.day, []).append(log_file)
    for day_files in groups.values():
        # Newest first. Filename is a deterministic tiebreaker for files
        # that land on the exact same second (or share an mtime fallback).
        day_files.sort(key=lambda f: (f.sort_key, f.path.name), reverse=True)
    return groups


ArchivePlan = dict[date, tuple[list[_LogFile], list[_LogFile]]]


def plan_archive(log_dir: Path, keep: int) -> ArchivePlan:
    """Return {day: (kept, to_archive)}, newest-first within each day."""
    groups = _group_by_day(_discover(log_dir))
    return {day: (files[:keep], files[keep:]) for day, files in groups.items()}


def _unique_destination(dest: Path) -> Path:
    """Return `dest`, or a disambiguated sibling if `dest` already exists.

    Two runs should never legitimately share a filename (the prefix
    embeds a full timestamp), but a stale copy left in the archive from a
    prior manual move could still collide. Never silently overwrite
    whatever is already archived there.
    """
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 1
    while True:
        candidate = dest.with_name(f"{stem}.dup{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def execute_archive(plan: ArchivePlan, archive_dir: Path) -> int:
    """Move every `to_archive` file in `plan` under `archive_dir`.

    Returns the number of files moved. Uses `shutil.move` rather than
    `os.rename` because the archive directory is expected to live on a
    different filesystem than the repo (`os.rename` raises `EXDEV` across
    filesystems; `shutil.move` falls back to copy+delete transparently).
    """
    moved = 0
    for day, (_, to_archive) in plan.items():
        if not to_archive:
            continue
        day_dir = archive_dir / day.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        for log_file in to_archive:
            dest = _unique_destination(day_dir / log_file.path.name)
            shutil.move(str(log_file.path), str(dest))
            moved += 1
    return moved


def format_report(plan: ArchivePlan, *, applied: bool) -> str:
    """Render the per-day table plus a total line."""
    if applied:
        header = "APPLIED — files below have been moved to the archive."
    else:
        header = (
            "DRY RUN (preview only, nothing moved) — pass --apply to archive for real."
        )

    lines = [
        header,
        f"{'day':<12}{'total':>8}{'kept':>8}{'archived':>10}{'bytes_archived':>16}",
    ]
    total_total = total_kept = total_archived = total_bytes = 0
    for day in sorted(plan):
        kept, archived = plan[day]
        day_total = len(kept) + len(archived)
        archived_bytes = sum(f.size_bytes for f in archived)
        lines.append(
            f"{day.isoformat():<12}{day_total:>8}{len(kept):>8}"
            f"{len(archived):>10}{archived_bytes:>16}"
        )
        total_total += day_total
        total_kept += len(kept)
        total_archived += len(archived)
        total_bytes += archived_bytes
    lines.append(
        f"{'TOTAL':<12}{total_total:>8}{total_kept:>8}"
        f"{total_archived:>10}{total_bytes:>16}"
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Keep the newest N run logs per day in logs/; move the rest to "
            "~/.sts2-train/log_archive/<YYYY-MM-DD>/ so the run workbench "
            "catalog (which recurses with rglob) stops parsing stale runs."
        )
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"Newest runs to keep per calendar day (default: {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Directory holding *.jsonl run logs (default: logs/)",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=DEFAULT_ARCHIVE_DIR,
        help=f"Destination root for archived logs (default: {DEFAULT_ARCHIVE_DIR})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files. Without this flag, only a preview is printed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.keep < 0:
        parser.error("--keep must be >= 0")

    plan = plan_archive(args.log_dir, args.keep)
    print(format_report(plan, applied=args.apply))
    if args.apply:
        execute_archive(plan, args.archive_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
