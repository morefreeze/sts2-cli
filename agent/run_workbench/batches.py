"""Regression/eval batches for the workbench's 回归/评测 tab.

A *regression run* is a game played by ``python/play_full_run.py`` (as opposed to
an RL training game).  ``python/run_batch.py`` groups such games into *batches*:
it writes ``logs/batches/<batch_id>/manifest.json`` (the plan) and
``lanes_status.json`` (which lane process is queued / running / done) and stamps
``batch_id`` / ``lane`` / ``game_index`` / ``games_total`` into each game log's
``run_meta`` header.

This module is pure data: it classifies runs, reads manifests, and turns a flat
list of :class:`RegressionRun` (built by ``RunCatalog.regression_runs``, which
owns every log parse) into per-lane and per-batch progress with an ETA.  It
never opens a game log and never imports the catalog, so the catalog can import
it.

Statistics follow the training views' rule: only *finished* games (win / dead /
technical failure) count; a game still being played is listed but is in no
number except the ``live`` counter.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Mapping, Sequence

from .metrics import summarize_cohort
from .models import RunRecord, RunStatus

HARNESS = "play_full_run"
# `<batch>:ooc=<policy>` / `ooc=<policy>`: the experiment label play_full_run
# stamped before it also stamped `harness`.
LEGACY_EXPERIMENT_PATTERN = re.compile(r"^(?:[^:]+:)?ooc=")
LEGACY_BATCH_PREFIX = "legacy:"
# A lane that says "running" but whose logs have not been touched for this long
# is stalled (its launcher or lane process died without updating the status).
ACTIVITY_WINDOW_SECONDS = 30 * 60
BATCH_STATES = ("queued", "running", "done", "failed", "stalled", "unknown")
_RECORDED_LANE_STATES = frozenset({"queued", "running", "done", "failed"})
# The policy ends at a comma: the value route arm's label is `<batch>:ooc=greedy,route=value`.
_POLICY_PATTERN = re.compile(r"(?:^|:)ooc=([^:,\s]+)")

FINISHED = "finished"
LIVE = "live"
ABANDONED = "abandoned"


def is_regression_run(meta: Mapping[str, Any] | None) -> bool:
    """Whether a run_meta header describes a play_full_run (regression/eval) game.

    ``harness == "play_full_run"`` is authoritative.  Older logs have no
    ``harness``: they are recognised by an ``ooc=`` experiment label together
    with the absence of a training ``checkpoint``.
    """
    if not isinstance(meta, Mapping):
        return False
    if meta.get("harness") == HARNESS:
        return True
    experiment = meta.get("experiment")
    return (
        not meta.get("checkpoint")
        and isinstance(experiment, str)
        and LEGACY_EXPERIMENT_PATTERN.match(experiment) is not None
    )


def policy_from_experiment(experiment: str | None) -> str | None:
    """The out-of-combat policy named by an experiment label (``b:ooc=greedy``;
    ``b:ooc=greedy,route=value`` is still ``greedy``)."""
    if not isinstance(experiment, str):
        return None
    match = _POLICY_PATTERN.search(experiment)
    return match.group(1) if match else None


@dataclass(frozen=True, eq=False)
class RegressionRun:
    """One regression/eval game, as the batch views need it.

    ``state`` is ``finished`` (win / dead / technical failure), ``live`` (no
    terminal state yet and its log was written to recently) or ``abandoned``
    (no terminal state and the log went quiet).  ``record`` is the normalized
    run for a finished game (what ``summarize_cohort`` consumes) and ``None``
    otherwise.
    """

    run_id: str
    source_id: str
    source_ids: tuple[str, ...]
    state: str
    status: RunStatus
    seed: str | None = None
    character: str | None = None
    experiment: str | None = None
    ascension: int | None = None
    harness: str | None = None
    batch_id: str | None = None
    lane: str | None = None
    game_index: int | None = None
    games_total: int | None = None
    global_floor: int | None = None
    act: int | None = None
    floor: int | None = None
    hp: int | None = None
    max_hp: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    updated_at: float | None = None
    duration_seconds: float | None = None
    has_map: bool = False
    record: RunRecord | None = None

    @property
    def lane_name(self) -> str:
        return self.lane or self.character or "—"


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def _read_json_object(path: Path, problems: list[str] | None) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as error:
        if problems is not None:
            problems.append(f"{path.parent.name}/{path.name}: {type(error).__name__}")
        return None
    if not isinstance(payload, dict):
        if problems is not None:
            problems.append(f"{path.parent.name}/{path.name}: expected a JSON object")
        return None
    return payload


def _clean_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _clean_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _normalize_manifest(
    raw: dict[str, Any], dir_name: str, lanes_status: dict[str, Any] | None
) -> dict[str, Any]:
    lanes: list[dict[str, Any]] = []
    for entry in raw.get("lanes") if isinstance(raw.get("lanes"), list) else []:
        if not isinstance(entry, dict) or not _clean_text(entry.get("lane")):
            continue
        lanes.append(
            {
                "lane": entry["lane"],
                "character": _clean_text(entry.get("character")),
                "planned_games": _clean_int(entry.get("planned_games")),
                "seed_prefix": _clean_text(entry.get("seed_prefix")),
                "seed_offset": _clean_int(entry.get("seed_offset")),
            }
        )
    status: dict[str, dict[str, Any]] = {}
    for lane, entry in (lanes_status or {}).items():
        if isinstance(lane, str) and isinstance(entry, dict):
            status[lane] = entry
    env = raw.get("env")
    argv = raw.get("argv")
    batch_id = _clean_text(raw.get("batch_id")) or dir_name
    return {
        "batch_id": batch_id,
        "name": _clean_text(raw.get("name")) or batch_id,
        "kind": _clean_text(raw.get("kind")),
        "created_at": _clean_number(raw.get("created_at")),
        "git_commit": _clean_text(raw.get("git_commit")),
        "argv": [item for item in argv if isinstance(item, str)]
        if isinstance(argv, list)
        else [],
        "env": {
            key: value
            for key, value in env.items()
            if isinstance(key, str) and isinstance(value, (str, int, float))
        }
        if isinstance(env, dict)
        else {},
        "lanes": lanes,
        "lanes_status": status,
        "registered": True,
        "dry_run": raw.get("dry_run") is True,
    }


def load_manifests(
    root: Path,
    problems: list[str] | None = None,
    *,
    include_dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Every readable ``root/batches/*/manifest.json``, with its lane status.

    A malformed manifest (or lane status) is skipped, never raised; what was
    wrong is appended to ``problems`` when a list is given.  Each manifest comes
    back normalized (plain types, ``lanes_status`` merged in, ``registered``
    true); the result is in directory-name order.  ``run_batch.py --dry-run``
    leaves a manifest flagged ``dry_run`` for a batch that never ran: it is
    skipped unless ``include_dry_run``.
    """
    batches_dir = Path(root) / "batches"
    manifests: list[dict[str, Any]] = []
    try:
        directories = sorted(path for path in batches_dir.iterdir() if path.is_dir())
    except OSError:
        return manifests
    for directory in directories:
        raw = _read_json_object(directory / "manifest.json", problems)
        if raw is None:
            continue
        status = _read_json_object(directory / "lanes_status.json", problems)
        manifest = _normalize_manifest(raw, directory.name, status)
        if manifest["dry_run"] and not include_dry_run:
            continue
        manifests.append(manifest)
    return manifests


# ---------------------------------------------------------------------------
# Progress and ETA
# ---------------------------------------------------------------------------


def _durations(runs: Iterable[RegressionRun]) -> list[float]:
    return [
        run.duration_seconds
        for run in runs
        if run.duration_seconds is not None and run.duration_seconds >= 0
    ]


def median_duration(durations: Sequence[float]) -> float | None:
    return float(statistics.median(durations)) if durations else None


def _summarize(records: Sequence[RunRecord]) -> dict[str, Any]:
    summary = summarize_cohort(records)
    return {
        "wins": summary.win_n,
        "valid_n": summary.valid_n,
        "technical": summary.technical_n,
        "win_rate": summary.win_rate,
        "avg_global_floor": summary.avg_global_floor,
    }


def lane_progress(
    manifest_lane: Mapping[str, Any],
    finished_runs: Sequence[RegressionRun],
    live_runs: Sequence[RegressionRun],
    lane_status: Mapping[str, Any] | None,
    now: float,
    *,
    abandoned_runs: Sequence[RegressionRun] = (),
    fallback_median: float | None = None,
    reference_at: float | None = None,
    window: float = ACTIVITY_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Progress of one lane.

    ``eta_seconds`` is the games still to play times the median duration of this
    lane's finished games, or of the whole batch (``fallback_median``) before the
    lane has finished one; ``None`` when no game anywhere has finished, when the
    plan is unknown (``planned_games`` missing: a lane no manifest registered), or
    when the lane is not going to play any more games (done, failed, stalled).
    ``reference_at`` -- the last time anything in the whole batch was active (or
    its creation) -- is the activity baseline for a lane that has produced no log
    yet: a queued lane waits on the others, so it is only stalled when they are.
    """
    lane = manifest_lane.get("lane")
    planned_games = manifest_lane.get("planned_games")
    planned_known = type(planned_games) is int
    finished = len(finished_runs)
    live = len(live_runs)
    abandoned = len(abandoned_runs)
    planned = planned_games if planned_known else finished + live + abandoned
    remaining = max(0, planned - finished) if planned_known else 0

    everything = [*finished_runs, *live_runs, *abandoned_runs]
    updated_candidates = [run.updated_at for run in everything if run.updated_at is not None]
    status = lane_status if isinstance(lane_status, Mapping) else {}
    started_at = _clean_number(status.get("started_at"))
    if started_at is not None:
        updated_candidates.append(started_at)
    updated_at = max(updated_candidates) if updated_candidates else None
    active_recent = bool(live) or (
        updated_at is not None and 0 <= now - updated_at <= window
    )
    recorded = status.get("state") if status.get("state") in _RECORDED_LANE_STATES else None

    if recorded in ("done", "failed"):
        state = recorded
    elif planned_known and remaining == 0 and finished > 0:
        state = "done"
    elif recorded in ("running", "queued"):
        baseline = updated_at if updated_at is not None else reference_at
        recent = active_recent or (
            baseline is not None and 0 <= now - baseline <= window
        )
        state = recorded if recent else "stalled"
    elif not planned_known:
        # Nothing registered this lane: all that can be said is whether a game
        # of it is being played right now.
        state = "running" if live else "done"
    elif live or active_recent:
        state = "running"
    else:
        state = "stalled" if finished or abandoned else "unknown"

    durations = _durations(finished_runs)
    lane_median = median_duration(durations)
    median = lane_median if lane_median is not None else fallback_median
    eta_applicable = (
        planned_known and remaining > 0 and state in ("queued", "running", "unknown")
    )
    eta_seconds = remaining * median if eta_applicable and median is not None else None
    records = [run.record for run in finished_runs if run.record is not None]
    stats = _summarize(records)
    policies = Counter(
        policy
        for policy in (policy_from_experiment(run.experiment) for run in everything)
        if policy
    )
    characters = Counter(run.character for run in everything if run.character)
    return {
        "lane": lane,
        "character": _clean_text(manifest_lane.get("character"))
        or (characters.most_common(1)[0][0] if characters else None),
        "policy": policies.most_common(1)[0][0] if policies else None,
        "planned": planned,
        "planned_known": planned_known,
        "finished": finished,
        "live": live,
        "abandoned": abandoned,
        "remaining": remaining,
        "wins": stats["wins"],
        "win_rate": stats["win_rate"],
        "technical": stats["technical"],
        "valid_n": stats["valid_n"],
        "avg_global_floor": stats["avg_global_floor"],
        "durations_n": len(durations),
        "median_duration_seconds": lane_median,
        "eta_applicable": eta_applicable,
        "eta_seconds": eta_seconds,
        "eta_at": now + eta_seconds if eta_seconds is not None else None,
        "eta_basis": ("lane" if lane_median is not None else "batch")
        if eta_seconds is not None
        else None,
        "state": state,
        "pid": status.get("pid") if type(status.get("pid")) is int else None,
        "started_at": started_at,
        "ended_at": _clean_number(status.get("ended_at")),
        "returncode": status.get("returncode")
        if type(status.get("returncode")) is int
        else None,
        # A lane the launcher could not spawn says why.
        "error": status["error"][:300] if isinstance(status.get("error"), str) else None,
        "updated_at": updated_at,
    }


def batch_progress(
    manifest: Mapping[str, Any],
    lanes: Sequence[Mapping[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """Totals over a batch's lanes.

    The lanes run in parallel, so the batch ETA is the *latest* lane ETA: the
    largest ``eta_seconds`` over the lanes that still have games to play.  It is
    ``None`` as soon as one of those lanes has no estimate yet (an honest "—"
    beats an under-estimate).
    """
    states = [lane["state"] for lane in lanes]
    pending = [lane for lane in lanes if lane["eta_applicable"]]
    if pending and all(lane["eta_seconds"] is not None for lane in pending):
        eta_seconds: float | None = max(lane["eta_seconds"] for lane in pending)
    else:
        eta_seconds = None
    if any(state in ("running", "queued") for state in states) or any(
        lane["live"] for lane in lanes
    ):
        status = "running"
    elif not lanes or all(state == "done" for state in states):
        status = "done"
    elif "failed" in states:
        status = "failed"
    elif "stalled" in states:
        status = "stalled"
    else:
        status = "unknown"
    updated = [lane["updated_at"] for lane in lanes if lane["updated_at"] is not None]
    created_at = manifest.get("created_at")
    updated_at = max(updated) if updated else created_at
    return {
        "batch_id": manifest["batch_id"],
        "name": manifest.get("name") or manifest["batch_id"],
        "kind": manifest.get("kind"),
        "registered": bool(manifest.get("registered", True)),
        "created_at": created_at,
        "git_commit": manifest.get("git_commit"),
        "argv": list(manifest.get("argv") or []),
        "env": dict(manifest.get("env") or {}),
        "lane_count": len(lanes),
        # Whether `planned` is a real plan.  A batch no manifest registered (or
        # with a lane only the logs know) has none: `planned` is then just what
        # has been played, and a "finished / planned" bar would imply completion.
        "planned_known": bool(lanes) and all(lane["planned_known"] for lane in lanes),
        "planned": sum(lane["planned"] for lane in lanes),
        "finished": sum(lane["finished"] for lane in lanes),
        "live": sum(lane["live"] for lane in lanes),
        "abandoned": sum(lane["abandoned"] for lane in lanes),
        "wins": sum(lane["wins"] for lane in lanes),
        "technical": sum(lane["technical"] for lane in lanes),
        "failed_lanes": sum(1 for state in states if state == "failed"),
        "stalled_lanes": sum(1 for state in states if state == "stalled"),
        "eta_seconds": eta_seconds,
        "eta_at": (now + eta_seconds) if eta_seconds is not None and now is not None else None,
        "status": status,
        "done": status != "running",
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# Putting runs and manifests together
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class BatchView:
    """A batch's summary, its lanes, and the runs behind each lane."""

    summary: dict[str, Any]
    lanes: list[dict[str, Any]]
    lane_runs: dict[str, list[RegressionRun]]


def _pseudo_manifest(batch_id: str, runs: Sequence[RegressionRun]) -> dict[str, Any]:
    """A batch nobody registered: legacy games grouped by experiment label, or
    games that name a ``batch_id`` no manifest exists for."""
    if batch_id.startswith(LEGACY_BATCH_PREFIX):
        label = batch_id[len(LEGACY_BATCH_PREFIX) :] or "（无实验名）"
    else:
        label = batch_id
    started = [run.started_at for run in runs if run.started_at is not None]
    planned: dict[str, int] = {}
    for run in runs:
        if run.games_total is not None:
            planned[run.lane_name] = max(planned.get(run.lane_name, 0), run.games_total)
    lanes = []
    for name in dict.fromkeys(run.lane_name for run in runs):
        lanes.append(
            {
                "lane": name,
                "character": None,
                # Only a launcher that stamped games_total knows its plan.
                "planned_games": planned.get(name),
            }
        )
    return {
        "batch_id": batch_id,
        "name": f"未登记批次 · {label}",
        "kind": None,
        "created_at": min(started) if started else None,
        "git_commit": None,
        "argv": [],
        "env": {},
        "lanes": lanes,
        "lanes_status": {},
        "registered": False,
    }


def _make_view(
    manifest: Mapping[str, Any],
    runs: Sequence[RegressionRun],
    now: float,
    window: float,
) -> BatchView:
    by_lane: dict[str, list[RegressionRun]] = {}
    for run in runs:
        by_lane.setdefault(run.lane_name, []).append(run)
    lane_specs = [dict(spec) for spec in manifest.get("lanes", [])]
    known = {spec["lane"] for spec in lane_specs}
    for name in by_lane:
        if name not in known:
            lane_specs.append({"lane": name, "character": None, "planned_games": None})
    statuses = manifest.get("lanes_status") or {}
    batch_durations = _durations(run for run in runs if run.state == FINISHED)
    fallback = median_duration(batch_durations)
    activity = [manifest.get("created_at")]
    activity.extend(run.updated_at for run in runs)
    for entry in statuses.values():
        if isinstance(entry, Mapping):
            activity.extend(_clean_number(entry.get(key)) for key in ("started_at", "ended_at"))
    seen_active = [value for value in activity if value is not None]
    reference_at = max(seen_active) if seen_active else None
    lanes: list[dict[str, Any]] = []
    lane_runs: dict[str, list[RegressionRun]] = {}
    for spec in lane_specs:
        members = by_lane.get(spec["lane"], [])
        lane_runs[spec["lane"]] = list(members)
        lanes.append(
            lane_progress(
                spec,
                [run for run in members if run.state == FINISHED],
                [run for run in members if run.state == LIVE],
                statuses.get(spec["lane"]),
                now,
                abandoned_runs=[run for run in members if run.state == ABANDONED],
                fallback_median=fallback,
                reference_at=reference_at,
                window=window,
            )
        )
    # The launcher records the policy knob it set; a lane with no game logs yet
    # (queued) has no experiment label to read it from.
    env_policy = (manifest.get("env") or {}).get("STS2_OOC_POLICY")
    for lane in lanes:
        if lane["policy"] is None and isinstance(env_policy, str) and env_policy:
            lane["policy"] = env_policy
    summary = batch_progress(manifest, lanes, now)
    summary["median_duration_seconds"] = fallback
    return BatchView(summary, lanes, lane_runs)


def build_batch_views(
    manifests: Sequence[Mapping[str, Any]],
    runs: Sequence[RegressionRun],
    now: float,
    *,
    window: float = ACTIVITY_WINDOW_SECONDS,
) -> list[BatchView]:
    """Every batch, newest first: registered ones from ``manifests``, then
    pseudo-batches for runs no manifest accounts for."""
    manifest_ids = {manifest["batch_id"] for manifest in manifests}
    groups: dict[str, list[RegressionRun]] = {}
    for run in runs:
        if run.batch_id:
            key = run.batch_id
        else:
            key = LEGACY_BATCH_PREFIX + (run.experiment or "")
        groups.setdefault(key, []).append(run)
    views = [
        _make_view(manifest, groups.get(manifest["batch_id"], []), now, window)
        for manifest in manifests
    ]
    for key, members in groups.items():
        if key in manifest_ids:
            continue
        views.append(_make_view(_pseudo_manifest(key, members), members, now, window))
    views.sort(
        key=lambda view: (
            view.summary["created_at"] is None,
            -(view.summary["created_at"] or 0.0),
            view.summary["batch_id"],
        )
    )
    return views
