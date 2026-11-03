"""The Advisor mod's real-game runs, for the workbench's "Advisor Play" tab.

The Sts2Advisor mod plays the *real* Slay the Spire 2: it advises the player, and in
campaign mode it plays unattended -- ``campaign.json`` tells it which character and
ascension to start next, it chains runs, and it writes one line per finished run.  This
module turns what that leaves on disk into one JSON-able payload for
``GET /api/advisor-play``:

* the game's own run histories of the *modded* profile (``saves/history/<start_time>.run``)
  -- the source of truth for results, final HP, killer, deck and the per-floor HP curve.
  A run counts when it started at or after ``ADVISOR_SINCE`` (2026-10-28 00:00 Asia/Shanghai,
  the day the mod was first used in the game); the unmodded profile is never read;
* ``current_run.save`` -- the run in progress (read only; it is not in the history yet);
* ``progress.save`` -- per-character unlocked ascension and lifetime wins/losses;
* ``Sts2Advisor/campaign_runs.jsonl`` -- the mod's own per-run log, written from campaign
  mode on (cross-checked against the histories; ``hp`` is 0 on some victories, so it never
  supplies HP when a history file exists), ``campaign.json`` -- the rotation,
  ``Sts2Advisor.log`` -- the tail says when the autopilot last stepped;
* the campaign operator's free-text ``journal.txt``.

Everything here is a pure function of those files (paths are injectable), read-only, and
never raises out of :func:`build_payload`: a broken input becomes an entry in
``problems`` (or ``error`` when nothing could be built) so the tab can say so.

Notation (kept from the rest of the project): ``A<act>F<floor>a<ascension>`` -- upper-case
``A`` is the act, lower-case ``a`` the ascension, floors are act-local and 1-based.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import re
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# The first day the Advisor mod was used in the real game: 2026-10-28 00:00 Asia/Shanghai.
ADVISOR_SINCE = int(dt.datetime(2026, 10, 28, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
GOAL_ASCENSION = 10
CHARACTERS = ("IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER")
OTHER = "OTHER"  # the character filter value for every character that is not one of CHARACTERS
# Floors (the HP curves) are sent for this many newest runs of the selection; the rest are listed only.
DEFAULT_SHOWN = 30
MAX_SHOWN = 500
LOG_TAIL_BYTES = 400_000
LOG_TAIL_LINES = 30
JOURNAL_LINES = 15
# An elite fight starting below this share of max HP is "low HP".
LOW_HP_RATIO = 0.5
# The autopilot's last step is "active" within 5 minutes and "stalled" after 30.
ACTIVE_SECONDS = 5 * 60
STALLED_SECONDS = 30 * 60

# Room kinds, in the order the aggregates list them.  ``unknown`` map points (the "?" nodes)
# resolve to a fight or to something else; they are split so a "?" fight is not averaged
# with the free events.
KINDS = (
    "monster", "elite", "boss", "unknown_fight", "unknown_other",
    "rest_site", "shop", "treasure", "ancient",
)


def short(value: Any) -> str:
    """``"CHARACTER.IRONCLAD"`` -> ``"IRONCLAD"``; anything else that is not a string -> ``""``."""
    return value.split(".", 1)[-1] if isinstance(value, str) else ""


def _int(value: Any, default: int | None = None) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdvisorPlayPaths:
    """Where the Advisor's files live.  Tests point every field at a tmp dir."""

    history_dir: Path
    current_run: Path
    progress: Path
    campaign_runs: Path
    campaign_json: Path
    advisor_log: Path
    journal: Path
    since: int = ADVISOR_SINCE

    @classmethod
    def from_dirs(
        cls, saves_dir: Path, advisor_dir: Path, journal: Path, since: int = ADVISOR_SINCE,
    ) -> "AdvisorPlayPaths":
        return cls(
            history_dir=saves_dir / "history",
            current_run=saves_dir / "current_run.save",
            progress=saves_dir / "progress.save",
            campaign_runs=advisor_dir / "campaign_runs.jsonl",
            campaign_json=advisor_dir / "campaign.json",
            advisor_log=advisor_dir / "Sts2Advisor.log",
            journal=journal,
            since=since,
        )

    @classmethod
    def default(cls, home: Path | None = None) -> "AdvisorPlayPaths":
        """The macOS Steam install's modded profile (``steam/<id>/modded/profile1/saves``;
        one Steam account in practice).  The unmodded profile is deliberately not here."""
        home = home or Path.home()
        game = home / "Library" / "Application Support" / "SlayTheSpire2"
        candidates = sorted(game.glob("steam/*/modded/profile1/saves"))
        saves = next((c for c in candidates if (c / "history").is_dir()), None)
        if saves is None:
            saves = candidates[0] if candidates else game / "steam" / "unknown" / "modded" / "profile1" / "saves"
        return cls.from_dirs(saves, game / "Sts2Advisor", home / ".sts2-train" / "campaign" / "journal.txt")


def _read_json(path: Path, problems: list[str]) -> Any:
    """Parsed JSON, or None (missing files are silent; unreadable ones are reported)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{path.name}: {type(exc).__name__}")
        return None
    try:
        return json.loads(text)
    except ValueError:
        problems.append(f"{path.name}: not valid JSON")
        return None


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _read_tail(path: Path, nbytes: int = LOG_TAIL_BYTES) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - nbytes))
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # the first line is probably cut in half
    return lines


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def kind_of(map_point_type: str, room_types: list[str | None]) -> str:
    if map_point_type == "unknown":
        fight = any(t in ("monster", "elite", "boss") for t in room_types)
        return "unknown_fight" if fight else "unknown_other"
    return map_point_type or "unknown_other"


def build_floors(map_point_history: Any) -> list[dict[str, Any]]:
    """Every floor in order: act-local position, kind, room name, damage taken, HP after."""
    floors: list[dict[str, Any]] = []
    for act_index, act in enumerate(map_point_history if isinstance(map_point_history, list) else [], 1):
        for floor_index, point in enumerate(act if isinstance(act, list) else [], 1):
            if not isinstance(point, dict):
                continue
            stats_list = point.get("player_stats")
            stats = stats_list[0] if isinstance(stats_list, list) and stats_list and isinstance(stats_list[0], dict) else {}
            rooms = [r for r in (point.get("rooms") or []) if isinstance(r, dict)] or [{}]
            room_types = [r.get("room_type") for r in rooms]
            map_type = point.get("map_point_type") if isinstance(point.get("map_point_type"), str) else "?"
            names = [short(r.get("model_id")) for r in rooms if r.get("model_id")]
            floors.append({
                "act": act_index,
                "floor": floor_index,
                "type": map_type,
                "kind": kind_of(map_type, room_types),
                "elite": map_type == "elite" or "elite" in room_types,
                "boss": map_type == "boss" or "boss" in room_types,
                "name": " → ".join(n for n in names if n),
                "dmg": _int(stats.get("damage_taken"), 0) or 0,
                "hp": _int(stats.get("current_hp")),
                "max": _int(stats.get("max_hp")),
                "turns": _int(rooms[-1].get("turns_taken")),
            })
    # HP before a floor = HP after the previous floor of the same act (None on an act's first floor).
    for i, floor in enumerate(floors):
        prev = floors[i - 1] if i and floors[i - 1]["act"] == floor["act"] else None
        floor["hp_before"] = prev["hp"] if prev else None
        floor["max_before"] = prev["max"] if prev else None
    return floors


def summarise_run(raw: Mapping[str, Any], *, in_progress: bool = False) -> dict[str, Any]:
    """One run file (a history ``.run`` or ``current_run.save``) as the payload's run dict.

    The scalar fields follow ``analyze_runs.summarise`` exactly (same fields, same sources):
    the final HP is the last floor's HP, ``end`` the last floor reached, the killer the
    killing encounter, else the killing event.
    """
    players = raw.get("players")
    player = players[0] if isinstance(players, list) and players and isinstance(players[0], dict) else {}
    floors = build_floors(raw.get("map_point_history"))
    encounter = short(raw.get("killed_by_encounter"))
    killed = encounter if encounter != "NONE" else short(raw.get("killed_by_event"))
    deck = [c for c in (player.get("deck") or []) if isinstance(c, dict)]
    relics = [short(r.get("id")) for r in (player.get("relics") or []) if isinstance(r, dict)]
    last = floors[-1] if floors else None
    hp = last["hp"] if last else None
    max_hp = last["max"] if last else None
    if in_progress:
        hp = _int(player.get("current_hp"), hp)
        max_hp = _int(player.get("max_hp"), max_hp)
    char = short(player.get("character") or player.get("character_id"))
    return {
        "start": _int(raw.get("start_time")),
        "char": char,
        "character": char,  # the API-facing name; ``char`` is what the older consumers read
        "asc": _int(raw.get("ascension")),
        "result": "running" if in_progress else (
            "win" if raw.get("win") else ("abandoned" if raw.get("was_abandoned") else "loss")),
        "end": f"A{last['act']}F{last['floor']}" if last else None,
        "total_floor": len(floors),
        "killed_by": None if killed in ("", "NONE") else killed,
        "hp": hp,
        "max_hp": max_hp,
        "deck": len(deck),
        "upgraded": sum(1 for c in deck if c.get("current_upgrade_level")),
        "relics": len(relics),
        "relic_names": relics,
        "run_time": _int(raw.get("run_time")),
        "seed": raw.get("seed") if isinstance(raw.get("seed"), str) else None,
        "acts": len(raw.get("map_point_history") or []),
        "origin": "current" if in_progress else "history",
        "floors": floors,
    }


# Parsed history files are immutable once written: keep them by (path, mtime, size).
_RUN_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_RUN_CACHE_LOCK = threading.Lock()


def _load_history_run(path: Path, problems: list[str]) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _RUN_CACHE_LOCK:
        cached = _RUN_CACHE.get(key)
    if cached is not None:
        return cached
    raw = _read_json(path, problems)
    if not isinstance(raw, dict):
        return None
    try:
        run = summarise_run(raw)
    except Exception as exc:  # a malformed file must not take the whole tab down
        problems.append(f"{path.name}: {type(exc).__name__}")
        return None
    with _RUN_CACHE_LOCK:
        # keep the cache bounded: drop older versions of this file, then the oldest entries
        for stale in [k for k in _RUN_CACHE if k[0] == key[0]]:
            del _RUN_CACHE[stale]
        _RUN_CACHE[key] = run
        while len(_RUN_CACHE) > 512:
            del _RUN_CACHE[next(iter(_RUN_CACHE))]
    return run


def load_runs(paths: AdvisorPlayPaths, problems: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """(every finished Advisor run, oldest first; the run in progress or None).  The dicts in
    the first list are copies: callers may add fields."""
    # current_run.save first: a run that finishes between the two reads is then in `runs`,
    # and is dropped from `current` below instead of showing twice.
    current_raw = _read_json(paths.current_run, problems)
    try:
        files = sorted(paths.history_dir.glob("*.run"))
    except OSError:
        files = []
    runs: list[dict[str, Any]] = []
    for path in files:
        if path.stem.isdigit() and int(path.stem) < paths.since:
            continue  # the file name is the run's start time: skip old ones without opening them
        run = _load_history_run(path, problems)
        if run is not None and (run["start"] or 0) >= paths.since:
            runs.append(dict(run))
    runs.sort(key=lambda r: r["start"] or 0)
    current = None
    started = _int(current_raw.get("start_time"), 0) if isinstance(current_raw, dict) else 0
    if isinstance(current_raw, dict) and (started or 0) >= paths.since and not current_raw.get("win_time") \
            and started not in {r["start"] for r in runs}:
        try:
            current = summarise_run(current_raw, in_progress=True)
            current["save_time"] = _int(current_raw.get("save_time"))
        except Exception as exc:
            problems.append(f"{paths.current_run.name}: {type(exc).__name__}")
    return runs, current


def load_all_history(paths: AdvisorPlayPaths, problems: list[str]) -> list[dict[str, Any]]:
    """Every run in the modded profile's history, whatever its start time (a manual run
    counts for the "won A10" goal; the tab's run list stays advisor-only).  Results are
    cached per file, so only the first call parses the old files."""
    try:
        files = sorted(paths.history_dir.glob("*.run"))
    except OSError:
        return []
    runs = []
    for path in files:
        run = _load_history_run(path, problems)
        if run is not None:
            runs.append(run)
    return runs


def load_campaign_log(paths: AdvisorPlayPaths) -> list[dict[str, Any]]:
    rows = []
    for line in _read_lines(paths.campaign_runs):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and (_int(row.get("start_time"), 0) or 0) >= paths.since:
            rows.append(row)
    return rows


def run_from_log_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A campaign_runs.jsonl row whose history file is missing, shaped like a run (no floors)."""
    killed = short(row.get("killed_by")) or short(row.get("killed_by_event"))
    deck = row.get("deck")
    relics = row.get("relics")
    char = short(str(row.get("character") or "")).upper()
    return {
        "start": _int(row.get("start_time")),
        "char": char,
        "character": char,
        "asc": _int(row.get("ascension")),
        "result": "win" if row.get("victory") else "loss",
        "end": f"A{row['act']}F{row['floor']}" if _int(row.get("act")) and _int(row.get("floor")) else None,
        "total_floor": _int(row.get("total_floor")),
        "killed_by": killed or None,
        "hp": _int(row.get("hp")),
        "max_hp": _int(row.get("max_hp")),
        "deck": len(deck) if isinstance(deck, list) else None,
        "upgraded": sum(1 for c in deck if isinstance(c, str) and "+" in c) if isinstance(deck, list) else None,
        "relics": len(relics) if isinstance(relics, list) else None,
        "relic_names": [short(r) for r in relics if isinstance(r, str)] if isinstance(relics, list) else [],
        "run_time": _int(row.get("run_time")),
        "seed": row.get("seed") if isinstance(row.get("seed"), str) else None,
        "acts": 0,
        "origin": "campaign_runs",
        "floors": [],
    }


# ---------------------------------------------------------------------------
# progress.save, campaign.json, the advisor log
# ---------------------------------------------------------------------------


def load_character_stats(paths: AdvisorPlayPaths, problems: list[str]) -> dict[str, dict[str, Any]]:
    progress = _read_json(paths.progress, problems)
    stats: dict[str, dict[str, Any]] = {}
    for entry in (progress.get("character_stats") if isinstance(progress, dict) else None) or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            stats[short(entry["id"])] = entry
    return stats


def parse_campaign_json(data: Any) -> dict[str, Any]:
    """The control file: ``rotation`` (entries are a name or ``{character, ascension}``)."""
    result: dict[str, Any] = {"ok": False, "enabled": None, "rotation": [], "next": None, "command": None}
    if not isinstance(data, dict):
        return result
    result["ok"] = True
    result["enabled"] = data.get("enabled") if isinstance(data.get("enabled"), bool) else None
    result["command"] = data.get("command") if isinstance(data.get("command"), str) else None
    for entry in data.get("rotation") or []:
        if isinstance(entry, str) and entry:
            result["rotation"].append({"character": entry.upper(), "ascension": "max"})
        elif isinstance(entry, dict) and isinstance(entry.get("character"), str):
            result["rotation"].append(
                {"character": entry["character"].upper(), "ascension": entry.get("ascension", "max")})
    nxt = data.get("next")
    if isinstance(nxt, dict) and isinstance(nxt.get("character"), str):
        result["next"] = {"character": nxt["character"].upper(), "ascension": nxt.get("ascension", "max")}
    return result


def rotation_next(rotation: list[dict[str, Any]], last_character: str | None) -> dict[str, Any] | None:
    """The entry after the one matching ``last_character``, cycling -- the mod's own rule
    (``CampaignRotation.Next``): a function of the character alone, so a restart never
    loses the place.  A character not in the list, or none yet, starts at the first entry."""
    if not rotation:
        return None
    if last_character:
        for i, entry in enumerate(rotation):
            if entry["character"] == last_character:
                return rotation[(i + 1) % len(rotation)]
    return rotation[0]


_LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ ")
_LOG_STEP = re.compile(r"autopilot step (\d+): (.*)$")
_LOG_START = re.compile(r"campaign: (starting|continuing saved run) \(?([A-Z_]+) a(\d+)")


def parse_log_tail(lines: list[str]) -> dict[str, Any]:
    """When the autopilot last stepped and which run the campaign last started (log times are local)."""
    out: dict[str, Any] = {
        "last_step_ts": None, "last_step_n": None, "last_step_text": None,
        "last_line_ts": None, "last_start": None,
    }

    def stamp(line: str) -> float | None:
        match = _LOG_TS.match(line)
        if not match:
            return None
        try:
            return dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return None

    for line in reversed(lines):
        ts = stamp(line)
        if ts is None:
            continue
        if out["last_line_ts"] is None:
            out["last_line_ts"] = ts
        if out["last_step_ts"] is None and (m := _LOG_STEP.search(line)):
            out["last_step_ts"], out["last_step_n"], out["last_step_text"] = ts, int(m.group(1)), m.group(2)
        if out["last_start"] is None and (m := _LOG_START.search(line)):
            out["last_start"] = {"ts": ts, "character": m.group(2), "ascension": int(m.group(3)), "how": m.group(1)}
        if out["last_step_ts"] is not None and out["last_start"] is not None:
            break
    return out


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def damage_by_kind_and_act(runs: list[dict[str, Any]], acts: int = 3) -> list[dict[str, Any]]:
    """Mean damage taken per floor, by room kind and act, over every floor (zeros included)."""
    buckets: dict[tuple[str, int], list[int]] = collections.defaultdict(list)
    for run in runs:
        for floor in run["floors"]:
            buckets[(floor["kind"], floor["act"])].append(floor["dmg"])
    rows = []
    for kind in KINDS:
        per_act = []
        for act in range(1, acts + 1):
            values = buckets.get((kind, act), [])
            per_act.append({
                "act": act, "n": len(values),
                "mean": round(statistics.mean(values), 2) if values else None,
                "max": max(values) if values else None,
            })
        every = [v for act in range(1, acts + 1) for v in buckets.get((kind, act), [])]
        if every:
            rows.append({
                "kind": kind, "acts": per_act, "n": len(every),
                "mean": round(statistics.mean(every), 2), "total": sum(every),
            })
    return rows


def deaths_by_encounter(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = collections.OrderedDict()
    for run in runs:
        if run["result"] == "loss" and run["killed_by"]:
            grouped.setdefault(run["killed_by"], []).append(
                {"character": run["char"], "ascension": run["asc"], "end": run["end"], "start": run["start"]})
    rows = [{"encounter": name, "count": len(items), "runs": items} for name, items in grouped.items()]
    rows.sort(key=lambda r: (-r["count"], r["encounter"]))
    return rows


def elite_fights(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Every elite fight with the HP it started at (the previous floor's HP after), bucketed."""
    fights = []
    for run in runs:
        for floor in run["floors"]:
            if floor["elite"] and floor["hp_before"] is not None and floor["max_before"]:
                fights.append({
                    "character": run["char"], "ascension": run["asc"], "start": run["start"],
                    "act": floor["act"], "floor": floor["floor"], "name": floor["name"],
                    "hp_before": floor["hp_before"], "max_before": floor["max_before"],
                    "ratio": round(floor["hp_before"] / floor["max_before"], 4),
                    "dmg": floor["dmg"], "hp_after": floor["hp"],
                })
    low = [f for f in fights if f["ratio"] < LOW_HP_RATIO]
    low.sort(key=lambda f: (-(f["start"] or 0), f["act"], f["floor"]))

    def bucket(label: str, members: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "label": label, "n": len(members),
            "mean_dmg": round(statistics.mean(f["dmg"] for f in members), 2) if members else None,
        }

    return {
        "total": len(fights), "low": len(low), "threshold": LOW_HP_RATIO,
        "buckets": [
            bucket("<50%", low),
            bucket("50-75%", [f for f in fights if LOW_HP_RATIO <= f["ratio"] < 0.75]),
            bucket(">=75%", [f for f in fights if f["ratio"] >= 0.75]),
        ],
        "low_fights": low[:12],
    }


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def _ladder(runs: list[dict[str, Any]], current: dict[str, Any] | None,
            stats: Mapping[str, Mapping[str, Any]],
            advisor_runs: list[dict[str, Any]] | None = None,
            won_goal: set[str] | None = None) -> list[dict[str, Any]]:
    """One row per character: unlocked ascension (global), whether an A10 run was ever won,
    the selected runs' results per ascension and the character's whole advisor-era record.

    ``runs`` (+ ``current``) are the *selected* runs (they fill the per-ascension counts);
    ``advisor_runs`` are every advisor-era run regardless of the tab's character filter (they
    give the advisor-era summary and the "passed before the advisor" marker, which must not
    flicker with the filter); ``won_goal`` is the set of characters that have won a run at
    the goal ascension in the whole modded history."""
    advisor_runs = runs if advisor_runs is None else advisor_runs
    won_goal = set() if won_goal is None else won_goal

    def blank() -> dict[str, int]:
        return {"wins": 0, "losses": 0, "abandoned": 0, "running": 0}

    key_of = {"win": "wins", "loss": "losses", "abandoned": "abandoned", "running": "running"}
    attempts: dict[tuple[str, int], dict[str, int]] = collections.defaultdict(blank)
    for run in runs + ([current] if current else []):
        if run["asc"] is None:
            continue
        attempts[(run["char"], run["asc"])][key_of[run["result"]]] += 1
    advisor_at: dict[tuple[str, int], int] = collections.Counter()
    advisor_total: dict[str, dict[str, int]] = collections.defaultdict(blank)
    for run in advisor_runs:
        advisor_total[run["char"]][key_of[run["result"]]] += 1
        if run["asc"] is not None:
            advisor_at[(run["char"], run["asc"])] += 1
    rows = []
    for char in CHARACTERS:
        entry = stats.get(char) or {}
        unlocked = _int(entry.get("max_ascension"), 0) or 0
        first_asc = min((asc for (c, asc) in advisor_at if c == char), default=None)
        total = advisor_total.get(char) or blank()
        rows.append({
            "character": char,
            "max_ascension": unlocked,
            "goal": GOAL_ASCENSION,
            "left": max(0, GOAL_ASCENSION - unlocked),
            "a10_won": char in won_goal,
            "total_wins": _int(entry.get("total_wins")),
            "total_losses": _int(entry.get("total_losses")),
            "advisor": {**total, "runs": total["wins"] + total["losses"] + total["abandoned"]},
            "first_advisor_ascension": first_asc,
            "known": char in stats,
            "ascensions": [{
                "ascension": asc,
                "unlocked": asc <= unlocked,
                "top": asc == unlocked,
                # unlocked before the advisor played this character: cleared in an earlier era
                "pre_advisor": (asc <= unlocked and first_asc is not None and asc < first_asc
                                and advisor_at.get((char, asc), 0) == 0),
                **attempts.get((char, asc), blank()),
            } for asc in range(1, GOAL_ASCENSION + 1)],
        })
    return rows


def _activity(parsed: Mapping[str, Any], now: float) -> dict[str, Any]:
    step = parsed.get("last_step_ts")
    age = None if step is None else max(0.0, now - step)
    state = "unknown" if age is None else ("active" if age <= ACTIVE_SECONDS else (
        "quiet" if age <= STALLED_SECONDS else "stalled"))
    return {
        "state": state, "age_seconds": age,
        "last_step_ts": step, "last_step_n": parsed.get("last_step_n"), "last_step_text": parsed.get("last_step_text"),
        "last_line_ts": parsed.get("last_line_ts"), "last_start": parsed.get("last_start"),
    }


def _cross_check(runs: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """campaign_runs.jsonl against the history files of the campaign (``auto`` runs)."""
    by_start = {r["start"]: r for r in runs}
    log_starts = {_int(r.get("start_time")) for r in rows}
    mismatched = []
    for row in rows:
        run = by_start.get(_int(row.get("start_time")))
        if run and (bool(row.get("victory")) != (run["result"] == "win") or _int(row.get("ascension")) != run["asc"]):
            mismatched.append(_int(row.get("start_time")))
    return {
        "log_rows": len(rows),
        "history_runs": len(runs),
        "missing_in_history": sorted(s for s in log_starts if s is not None and s not in by_start),
        "missing_in_log": sorted(s for s in by_start if s not in log_starts and s is not None),
        "zero_hp_victories": sum(1 for r in rows if r.get("victory") and not r.get("hp")),
        "mismatched": mismatched,
    }


def parse_filter(character: Any = None, shown: Any = None) -> dict[str, Any]:
    """Normalise the tab's filter.  Raises ValueError on an invalid character or shown count."""
    char = None
    if character not in (None, "", "all"):
        char = str(character).upper()
        if char != OTHER and not re.fullmatch(r"[A-Z0-9_-]{1,40}", char):
            raise ValueError("invalid character")
    count = DEFAULT_SHOWN
    if shown not in (None, ""):
        try:
            count = int(shown)
        except (TypeError, ValueError):
            raise ValueError("shown must be an integer") from None
        if not 1 <= count <= MAX_SHOWN:
            raise ValueError(f"shown must be between 1 and {MAX_SHOWN}")
    return {"character": char, "shown": count}


def _character_group(char: str) -> str:
    return char if char in CHARACTERS else OTHER


def build_payload(
    paths: AdvisorPlayPaths | None = None,
    *,
    now: float | None = None,
    character: Any = None,
    shown: Any = None,
) -> dict[str, Any]:
    """The whole Advisor Play tab.  Never raises: a failure is the ``error`` field.

    ``character`` / ``shown`` are the tab's filter (see :func:`parse_filter`; an invalid one
    raises ValueError -- the HTTP layer turns that into a 400).  The character filter scopes
    the runs table, the HP curves, the per-ascension results of the ladder, the summary tiles
    and the aggregates; the goal, the unlocked ascensions, the run in progress, the rotation
    and the journal are the campaign's and never filtered.
    """
    flt = parse_filter(character, shown)
    now = time.time() if now is None else float(now)
    problems: list[str] = []
    try:
        paths = paths or AdvisorPlayPaths.default()
        return _build(paths, now, problems, flt)
    except Exception as exc:  # defensive boundary: this is a monitoring page
        return {"error": f"{type(exc).__name__}: {exc}", "now": now, "problems": problems, "filter": flt,
                "goal": {"ascension": GOAL_ASCENSION, "levels": 0, "levels_goal": GOAL_ASCENSION * len(CHARACTERS),
                         "won": 0, "won_goal": len(CHARACTERS), "won_characters": []},
                "characters": [], "runs": [], "current": None}


def _build(paths: AdvisorPlayPaths, now: float, problems: list[str], flt: dict[str, Any]) -> dict[str, Any]:
    everything, current = load_runs(paths, problems)
    log_rows = load_campaign_log(paths)
    check = _cross_check(everything, log_rows)
    # A run the mod logged whose history file is missing still belongs in the list (no HP curve).
    rows_by_start = {_int(r.get("start_time")): r for r in log_rows}
    for start in check["missing_in_history"]:
        everything.append(run_from_log_row(rows_by_start[start]))
    everything.sort(key=lambda r: r["start"] or 0)

    stats = load_character_stats(paths, problems)
    camp = parse_campaign_json(_read_json(paths.campaign_json, problems))
    log_tail = _read_tail(paths.advisor_log)
    parsed_log = parse_log_tail(log_tail)

    group_counts = collections.Counter(_character_group(r["char"]) for r in everything)
    selected = [r for r in everything if flt["character"] is None or _character_group(r["char"]) == flt["character"]]
    for number, run in enumerate(selected, 1):
        run["number"] = number
    current_selected = current is not None and (
        flt["character"] is None or _character_group(current["char"]) == flt["character"])
    if current:
        current["number"] = len(selected) + 1

    # "Done" = the character has WON a run at the goal ascension -- anywhere in the modded
    # profile's history (a manual run counts), or in the mod's own log if its file is missing.
    all_history = load_all_history(paths, [])  # an unreadable pre-advisor file is not this tab's problem
    won_goal = {
        r["char"] for r in all_history + everything
        if r["result"] == "win" and (r["asc"] or 0) >= GOAL_ASCENSION and r["char"] in CHARACTERS}

    wins = sum(1 for r in selected if r["result"] == "win")
    losses = sum(1 for r in selected if r["result"] == "loss")
    abandoned = sum(1 for r in selected if r["result"] == "abandoned")
    times = [r["run_time"] for r in selected if r["run_time"]]
    levels = sum(min(GOAL_ASCENSION, (_int((stats.get(c) or {}).get("max_ascension"), 0) or 0)) for c in CHARACTERS)

    last_logged = log_rows[-1].get("character") if log_rows else None
    anchor = current["char"] if current else (str(last_logged).upper() if last_logged else None)
    next_entry = rotation_next(camp["rotation"], anchor)
    if next_entry is not None:
        next_entry = dict(next_entry)
        top = _int((stats.get(next_entry["character"]) or {}).get("max_ascension"))
        next_entry["max_ascension"] = top
        next_entry["resolved_ascension"] = top if next_entry["ascension"] == "max" else _int(next_entry["ascension"])
    rotation_chars = {e["character"] for e in camp["rotation"]}

    # Aggregates use every selected run (all their floors); the run list sends floors only for
    # the newest `shown` of them.
    aggregates = {
        "damage": damage_by_kind_and_act(selected),
        "deaths": deaths_by_encounter(selected),
        "elites": elite_fights(selected),
    }
    newest_first = list(reversed(selected))
    listed = [r if i < flt["shown"] else dict(r, floors=None) for i, r in enumerate(newest_first)]

    return {
        "error": None,
        "now": now,
        "problems": problems,
        "since": paths.since,
        "filter": flt,
        "character_options": [{"id": c, "count": group_counts.get(c, 0)} for c in (*CHARACTERS, OTHER)],
        "goal": {
            "ascension": GOAL_ASCENSION, "levels": levels, "levels_goal": GOAL_ASCENSION * len(CHARACTERS),
            "won": len(won_goal), "won_goal": len(CHARACTERS),
            "won_characters": [c for c in CHARACTERS if c in won_goal],
        },
        "summary": {
            "runs": len(selected), "wins": wins, "losses": losses, "abandoned": abandoned,
            "win_rate": wins / (wins + losses) if wins + losses else None,
            "mean_run_time": round(statistics.mean(times)) if times else None,
        },
        "characters": _ladder(
            selected, current if current_selected else None, stats, advisor_runs=everything, won_goal=won_goal),
        "current": current,
        "current_selected": current_selected,
        "activity": _activity(parsed_log, now),
        "campaign": {
            "ok": camp["ok"], "enabled": camp["enabled"], "command": camp["command"],
            "rotation": camp["rotation"],
            "not_in_rotation": [c for c in CHARACTERS if c not in rotation_chars] if camp["ok"] else [],
            "next": next_entry,
            "next_after_current": current is not None,
        },
        "runs": listed,
        "runs_with_floors": min(len(listed), flt["shown"]),
        "aggregates": aggregates,
        "journal": [ln.rstrip("\r") for ln in _read_lines(paths.journal)[-JOURNAL_LINES:]],
        "log_tail": log_tail[-LOG_TAIL_LINES:],
        "checks": check,
    }
