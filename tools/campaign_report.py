#!/usr/bin/env python3
"""Campaign report: what the Sts2Advisor autopilot's ended runs and decision trace say.

Reads the mod's user dir (default ~/Library/Application Support/SlayTheSpire2/Sts2Advisor/):
  campaign_runs.jsonl           one JSON per ended run (character, ascension, victory, ...)
  <row.history_path>            the game's own .run file (map_point_history, per-room stats)
  advisor_trace.1.jsonl + advisor_trace.jsonl   every decision sent to the advice service

and prints (plain text, --json PATH for the same numbers as JSON):
  1 runs per band / character        5 card picks, skips, deck size at each boss
  2 damage per room type per act     6 lookahead predicted vs actual HP at the boss (trace)
  3 HP entering each boss + rest     7 event decisions greedy could not score (trace)
  4 gold                             8 card_reward score components (trace)
Trace lines with a "run" object are grouped by run.start_time; older lines are split at the
`run_end` decision. `combat_end` decisions (when the mod writes them) add a per-room damage
section. Everything is load -> aggregate (pure) -> format, so each step is testable.

    .venv/bin/python tools/campaign_report.py [--min-asc 9] [--character NECRO] [--since 2026-11-01]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

DEFAULT_USER_DIR = os.path.expanduser("~/Library/Application Support/SlayTheSpire2/Sts2Advisor")
TRACE_FILES = ("advisor_trace.1.jsonl", "advisor_trace.jsonl")
ROOM_TYPES = ("monster", "elite", "boss", "unknown")
BANDS = ((0, 5, "a0-5"), (6, 8, "a6-8"), (9, 10, "a9-10"))
UNKNOWN_BAND = "a?"
FLOOR_BUCKETS = (("early(<=6)", 6), ("mid(7-11)", 11), ("late(12+)", 10 ** 6))


# --------------------------------------------------------------------------- small helpers

def band_of(ascension) -> str:
    if not isinstance(ascension, int) or isinstance(ascension, bool):
        return UNKNOWN_BAND
    for low, high, name in BANDS:
        if low <= ascension <= high:
            return name
    return f"a{BANDS[-1][1] + 1}+" if ascension > BANDS[-1][1] else UNKNOWN_BAND


def band_order(name: str) -> int:
    names = [b[2] for b in BANDS]
    return names.index(name) if name in names else len(names) + (name == UNKNOWN_BAND)


def dstats(values) -> dict | None:
    """n / mean / median / p90 (nearest rank) of a list of numbers, None when empty."""
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "p90": values[max(0, math.ceil(0.9 * len(values)) - 1)]}


def mean_or_none(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.mean(values) if values else None


def ratio(num, den):
    return num / den if den else None


def fmt(value, digits=2, width=0) -> str:
    text = "-" if value is None else f"{value:.{digits}f}"
    return text.rjust(width)


def _ps(point: dict) -> dict:
    stats = point.get("player_stats")
    return stats[0] if isinstance(stats, list) and stats and isinstance(stats[0], dict) else {}


def _encounter(point: dict) -> str:
    rooms = point.get("rooms")
    room = rooms[0] if isinstance(rooms, list) and rooms and isinstance(rooms[0], dict) else {}
    return str(room.get("model_id") or "").replace("ENCOUNTER.", "")


def _turns(point: dict):
    rooms = point.get("rooms")
    return rooms[0].get("turns_taken") if isinstance(rooms, list) and rooms and isinstance(rooms[0], dict) else None


# --------------------------------------------------------------------------- load

def load_jsonl(path: str) -> list:
    """Every parseable object line of a JSONL file ([] when it is missing)."""
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def load_campaign_rows(user_dir: str) -> list:
    return load_jsonl(os.path.join(user_dir, "campaign_runs.jsonl"))


def load_trace(user_dir: str) -> list:
    out = []
    for name in TRACE_FILES:
        out.extend(d for d in load_jsonl(os.path.join(user_dir, name)) if "decision" in d)
    return out


def load_history(path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("map_point_history"), list) else None


def row_ok(row: dict, since=None, character=None, min_asc=None, max_asc=None) -> bool:
    asc = row.get("ascension")
    if since and str(row.get("ts") or "")[:10] < since:
        return False
    if character and not str(row.get("character") or "").upper().startswith(character.upper()):
        return False
    if min_asc is not None and not (isinstance(asc, int) and asc >= min_asc):
        return False
    if max_asc is not None and not (isinstance(asc, int) and asc <= max_asc):
        return False
    return True


def build_run(row: dict, history: dict) -> dict:
    """One run: the campaign row's summary plus a flat list of rooms from the .run file."""
    rooms = []
    for act_no, act in enumerate(history["map_point_history"], 1):
        for idx, point in enumerate(act if isinstance(act, list) else []):
            ps = _ps(point)
            choices = ps.get("card_choices") or []
            hp, max_hp = ps.get("current_hp", 0), ps.get("max_hp", 0)
            rooms.append({
                "act": act_no, "idx": idx, "type": point.get("map_point_type"),
                "encounter": _encounter(point), "turns": _turns(point),
                "damage": ps.get("damage_taken", 0) or 0, "healed": ps.get("hp_healed", 0) or 0,
                "hp": hp, "max_hp": max_hp, "gold": ps.get("current_gold", 0),
                "gold_gained": ps.get("gold_gained", 0) or 0, "gold_spent": ps.get("gold_spent", 0) or 0,
                "picked": (any(c.get("was_picked") for c in choices if isinstance(c, dict)) if choices else None),
                "removed": len(ps.get("cards_removed") or []), "rest": list(ps.get("rest_site_choices") or []),
                "delta": len(ps.get("cards_gained") or []) - len(ps.get("cards_removed") or []),
            })
    # deck size after each room, walking back from the final deck
    size = len(row.get("deck") or [])
    for room in reversed(rooms):
        room["deck_after"] = size
        size -= room["delta"]
    return {"character": row.get("character"), "ascension": row.get("ascension"),
            "band": band_of(row.get("ascension")), "win": bool(row.get("victory")),
            "total_floor": row.get("total_floor"), "gold_end": row.get("gold"),
            "killed_by": str(row.get("killed_by") or "").replace("ENCOUNTER.", ""),
            "start_time": row.get("start_time"), "ts": row.get("ts"), "rooms": rooms}


def load_runs(rows: list) -> tuple[list, int]:
    """([run], number of rows whose .run file is missing or unreadable)."""
    runs, skipped = [], 0
    for row in rows:
        history = load_history(row.get("history_path"))
        if history is None:
            skipped += 1
            continue
        runs.append(build_run(row, history))
    return runs, skipped


# --------------------------------------------------------------------------- trace grouping

def group_trace(recs: list) -> list:
    """[{"key", "meta": {start_time, character, ascension, ts}, "recs": [...]}] in file order.

    A line with a "run" object belongs to the group of its start_time; a line without one
    belongs to the segment that ends at the next `run_end` (whose state is the run's row and
    so names the run). The unfinished tail segment has no run_end and only what its lines say."""
    groups, order, segment = {}, [], 0
    for rec in recs:
        run = rec.get("run") if isinstance(rec.get("run"), dict) else None
        key = ("t", run.get("start_time")) if run and run.get("start_time") is not None else ("seg", segment)
        if key not in groups:
            groups[key] = {"key": key, "meta": {"start_time": key[1] if key[0] == "t" else None,
                                                "character": None, "ascension": None, "ts": rec.get("ts")},
                           "recs": []}
            order.append(key)
        group = groups[key]
        group["recs"].append(rec)
        meta = group["meta"]
        for source in (run, rec.get("state") if rec.get("decision") == "run_end" else None):
            if isinstance(source, dict):
                for field in ("character", "ascension", "start_time"):
                    if source.get(field) is not None:
                        meta[field] = source[field]
        state = rec.get("state") if isinstance(rec.get("state"), dict) else {}
        if meta["ascension"] is None and isinstance(state.get("ascension"), int):
            meta["ascension"] = state["ascension"]
        if rec.get("decision") == "run_end":
            segment += 1
    return [groups[k] for k in order]


def group_ok(group: dict, since=None, character=None, min_asc=None, max_asc=None) -> bool:
    meta = {"ts": group["meta"]["ts"], "character": group["meta"]["character"],
            "ascension": group["meta"]["ascension"]}
    return row_ok(meta, since, character, min_asc, max_asc)


def _fields(rec: dict) -> dict:
    """A mod-written summary line keeps its data in `state`; tolerate top-level too."""
    state = rec.get("state") if isinstance(rec.get("state"), dict) else {}
    return {**rec, **state}


def _act_of(state: dict):
    context = state.get("context") if isinstance(state.get("context"), dict) else {}
    return state.get("act") or context.get("act")


# --------------------------------------------------------------------------- aggregates (pure)

def sec_runs(rows: list) -> dict:
    """Runs per band and per character from the campaign rows: n, wins, mean total_floor."""
    def agg(items):
        return {"n": len(items), "wins": sum(1 for r in items if r.get("victory")),
                "avg_floor": mean_or_none(r.get("total_floor") for r in items)}
    by_band, by_char, by_both = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        band, char = band_of(row.get("ascension")), str(row.get("character") or "?")
        by_band[band].append(row)
        by_char[char].append(row)
        by_both[f"{band} {char}"].append(row)
    return {"total": agg(rows), "by_band": {k: agg(v) for k, v in by_band.items()},
            "by_character": {k: agg(v) for k, v in by_char.items()},
            "by_band_character": {k: agg(v) for k, v in by_both.items()}}


def sec_damage(runs: list) -> dict:
    """{band: {n_runs, acts: {act: {room_type: {damage: dstats, rooms_per_run}}}}}."""
    out = {}
    for band in sorted({r["band"] for r in runs}, key=band_order):
        group = [r for r in runs if r["band"] == band]
        acts = {}
        for act in (1, 2, 3):
            reached = [r for r in group if any(room["act"] == act for room in r["rooms"])]
            types = {}
            for rtype in ROOM_TYPES:
                damage = [room["damage"] for r in reached for room in r["rooms"]
                          if room["act"] == act and room["type"] == rtype]
                if damage:
                    types[rtype] = {"damage": dstats(damage), "rooms_per_run": len(damage) / len(reached)}
            if types:
                acts[act] = {"n_runs": len(reached), "types": types}
        out[band] = {"n_runs": len(group), "acts": acts}
    return out


def boss_entries(runs: list) -> list:
    """One entry per boss room reached: HP entering it, whether it was beaten, the rest before it."""
    entries = []
    for run in runs:
        rooms = run["rooms"]
        for pos, room in enumerate(rooms):
            if room["type"] != "boss":
                continue
            max_hp = room["max_hp"] or 1
            hp_in = room["hp"] + room["damage"] - room["healed"]
            entry = {"band": run["band"], "character": run["character"], "act": room["act"],
                     "boss": room["encounter"], "hp_in": hp_in / max_hp, "passed": room["hp"] > 0,
                     "damage": room["damage"], "turns": room["turns"], "rest": None}
            prev = rooms[pos - 1] if pos > 0 and rooms[pos - 1]["act"] == room["act"] else None
            if prev and prev["type"] == "rest_site":
                entry["rest"] = (prev["rest"] or ["?"])[0]
                entry["hp_before_rest"] = (prev["hp"] - prev["healed"]) / (prev["max_hp"] or 1)
            entries.append(entry)
    return entries


def _boss_cell(items: list) -> dict:
    passed, failed = [e for e in items if e["passed"]], [e for e in items if not e["passed"]]
    return {"reached": len(items), "passed": len(passed),
            "hp_in_pass": mean_or_none(e["hp_in"] for e in passed),
            "hp_in_fail": mean_or_none(e["hp_in"] for e in failed),
            "damage_pass": mean_or_none(e["damage"] for e in passed),
            "turns_pass": mean_or_none(e["turns"] for e in passed),
            "turns_fail": mean_or_none(e["turns"] for e in failed)}


def sec_boss(runs: list) -> dict:
    entries = boss_entries(runs)
    out = {}
    for band in sorted({e["band"] for e in entries}, key=band_order):
        mine = [e for e in entries if e["band"] == band]
        by_act = {a: _boss_cell([e for e in mine if e["act"] == a]) for a in sorted({e["act"] for e in mine})}
        by_boss = {b: _boss_cell([e for e in mine if e["boss"] == b]) for b in sorted({e["boss"] for e in mine})}
        rests = {}
        for choice in sorted({e["rest"] for e in mine if e["rest"]}):
            sub = [e for e in mine if e["rest"] == choice]
            rests[choice] = {"n": len(sub), "passed": sum(e["passed"] for e in sub),
                             "hp_before": mean_or_none(e["hp_before_rest"] for e in sub),
                             "hp_in": mean_or_none(e["hp_in"] for e in sub)}
        out[band] = {"by_act": by_act, "by_boss": by_boss, "pre_boss_rest": rests,
                     "no_rest_before_boss": sum(1 for e in mine if not e["rest"])}
    return out


def sec_gold(runs: list) -> dict:
    out = {}
    for band in sorted({r["band"] for r in runs}, key=band_order):
        group = [r for r in runs if r["band"] == band]
        losses = [r for r in group if not r["win"]]
        act_end = {}
        for act in (1, 2, 3):
            values = []
            for r in group:
                rooms = [x for x in r["rooms"] if x["act"] == act]
                if rooms and any(x["type"] == "boss" and x["hp"] > 0 for x in rooms):
                    values.append(rooms[-1]["gold"])
            if values:
                act_end[act] = {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values)}
        out[band] = {
            "n_runs": len(group),
            "spent_per_run": mean_or_none(sum(x["gold_spent"] for x in r["rooms"]) for r in group),
            "gained_per_run": mean_or_none(sum(x["gold_gained"] for x in r["rooms"]) for r in group),
            "removals_per_run": mean_or_none(sum(x["removed"] for x in r["rooms"]) for r in group),
            "gold_at_death": dstats([r["gold_end"] for r in losses]),
            "losses_with_300_plus": sum(1 for r in losses if (r["gold_end"] or 0) >= 300),
            "gold_at_act_end": act_end}
    return out


def sec_cards(runs: list) -> dict:
    out = {}
    for band in sorted({r["band"] for r in runs}, key=band_order):
        group = [r for r in runs if r["band"] == band]
        picks = defaultdict(lambda: {"pick": 0, "skip": 0})
        for r in group:
            for room in r["rooms"]:
                if room["picked"] is not None and room["type"] in ROOM_TYPES:
                    picks[f'A{room["act"]} {room["type"]}']["pick" if room["picked"] else "skip"] += 1
        deck = {}
        for act in (1, 2, 3):
            sizes = [r["rooms"][pos - 1]["deck_after"] for r in group
                     for pos, room in enumerate(r["rooms"]) if pos and room["act"] == act and room["type"] == "boss"]
            if sizes:
                deck[act] = {"n": len(sizes), "mean": statistics.mean(sizes), "median": statistics.median(sizes)}
        out[band] = {"picks": dict(sorted(picks.items())), "deck_size_entering_boss": deck}
    return out


def boss_map_select_hp(recs: list) -> dict:
    """{act: hp ratio} at the map_select whose only choice is the act's Boss."""
    actual = {}
    for rec in recs:
        if rec.get("decision") != "map_select" or not isinstance(rec.get("state"), dict):
            continue
        state = rec["state"]
        choices = state.get("choices") or []
        player = state.get("player") or {}
        if len(choices) == 1 and choices[0].get("type") == "Boss" and player.get("max_hp"):
            actual[_act_of(state)] = player["hp"] / player["max_hp"]
    return actual


def sec_route(groups: list) -> dict:
    """Lookahead's predicted HP ratio at the boss vs the real one, by act and act-local floor."""
    cells = defaultdict(list)
    for group in groups:
        actual = boss_map_select_hp(group["recs"])
        for rec in group["recs"]:
            if rec.get("decision") != "map_select" or not isinstance(rec.get("state"), dict):
                continue
            act = _act_of(rec["state"])
            for rule in (rec.get("reply") or {}).get("rules") or []:
                if rule.get("key") != "map.la_best_path" or act not in actual:
                    continue
                values = rule.get("values") or {}
                floor, predicted = values.get("floor"), values.get("hp_boss_ratio")
                if floor is None or predicted is None:
                    continue
                bucket = next(name for name, top in FLOOR_BUCKETS if floor <= top)
                cells[(act, bucket)].append((predicted, actual[act]))
    out = {}
    for (act, bucket), pairs in sorted(cells.items()):
        out[f"A{act} {bucket}"] = {"n": len(pairs), "predicted": statistics.mean(p for p, _ in pairs),
                                   "actual": statistics.mean(a for _, a in pairs),
                                   "overpredict_gt_0.2": sum(1 for p, a in pairs if p - a > 0.2) / len(pairs)}
    return out


def sec_events(groups: list) -> dict:
    decisions = zero_decisions = 0
    zero_titles, debug_titles = Counter(), Counter()
    debug_seen = debug_all_zero = 0
    for group in groups:
        for rec in group["recs"]:
            if rec.get("decision") != "event_choice" or not isinstance(rec.get("state"), dict):
                continue
            decisions += 1
            reply = rec.get("reply") or {}
            scored = {o.get("index"): o for o in reply.get("options") or [] if isinstance(o, dict)}
            live = [o for o in scored.values() if o.get("status") != "locked"]
            if live and all(abs(o.get("score") or 0) < 1e-9 for o in live):
                zero_decisions += 1
            for option in rec["state"].get("options") or []:
                ev = scored.get(option.get("index"))
                if ev and ev.get("status") != "locked" and abs(ev.get("score") or 0) < 1e-9:
                    zero_titles[option.get("title")] += 1
            debug = reply.get("debug") if isinstance(reply.get("debug"), dict) else None
            if debug and "event_unscored" in debug:
                debug_seen += 1
                debug_all_zero += bool(debug.get("event_all_zero"))
                debug_titles.update(debug["event_unscored"])
    return {"decisions": decisions, "all_options_zero": zero_decisions,
            "zero_score_titles": zero_titles.most_common(25),
            "debug": {"replies_with_event_unscored": debug_seen, "event_all_zero": debug_all_zero,
                      "titles": debug_titles.most_common(25)} if debug_seen else None}


def sec_card_scores(groups: list) -> dict:
    per_act = defaultdict(lambda: {"n": 0, "best": [], "deck": [], "comp": defaultdict(list), "margin": []})
    for group in groups:
        for rec in group["recs"]:
            if rec.get("decision") != "card_reward" or not isinstance(rec.get("state"), dict):
                continue
            reply = rec.get("reply") or {}
            act = _act_of(rec["state"])
            cell = per_act[act]
            options = [o for o in reply.get("options") or []
                       if isinstance(o, dict) and o.get("kind") == "card" and o.get("score") is not None]
            cell["n"] += 1
            if options:
                cell["best"].append(max(o["score"] for o in options))
            for option in options:
                for comp in option.get("components") or []:
                    cell["comp"][comp["key"]].append(comp["value"])
            size = (rec["state"].get("player") or {}).get("deck_size")
            if size:
                cell["deck"].append(size)
            block = (reply.get("debug") or {}).get("card_reward") if isinstance(reply.get("debug"), dict) else None
            if block and block.get("margin") is not None:
                cell["margin"].append(block["margin"])
    out = {}
    for act in sorted(per_act, key=lambda a: (a is None, a)):
        cell = per_act[act]
        out[f"A{act}"] = {
            "n": cell["n"], "deck_size_median": statistics.median(cell["deck"]) if cell["deck"] else None,
            "best_score_median": statistics.median(cell["best"]) if cell["best"] else None,
            "margin": dstats(cell["margin"]),
            "components": {k: {"mean": statistics.mean(v), "min": min(v), "max": max(v),
                               "frac_nonzero": sum(1 for x in v if abs(x) > 1e-9) / len(v)}
                           for k, v in sorted(cell["comp"].items())}}
    return out


def sec_combat_end(groups: list) -> dict:
    """Damage per act and room type from `combat_end` decisions: {band: {"A1 monster": {...}}}."""
    cells = defaultdict(lambda: defaultdict(list))
    for group in groups:
        band = band_of(group["meta"]["ascension"])
        for rec in group["recs"]:
            if rec.get("decision") != "combat_end":
                continue
            f = _fields(rec)
            if f.get("damage_taken") is None:
                continue
            cells[band][f'A{f.get("act")} {str(f.get("room_type") or "?").lower()}'].append(f)
    return {band: {key: {"damage": dstats([f["damage_taken"] for f in items]),
                         "won": sum(1 for f in items if f.get("won")), "n": len(items)}
                   for key, items in sorted(by_key.items())}
            for band, by_key in sorted(cells.items(), key=lambda kv: band_order(kv[0]))}


# --------------------------------------------------------------------------- format

def _stat_cols(stat: dict | None) -> str:
    if not stat:
        return "n=  0"
    return f'n={stat["n"]:4d} mean={stat["mean"]:6.1f} med={stat["median"]:5.1f} p90={stat["p90"]:5.1f}'


def format_runs(data: dict) -> list:
    lines = ["== 1. Runs =="]

    def row(name, cell):
        return f'  {name:24s} n={cell["n"]:3d} wins={cell["wins"]:3d} avg_total_floor={fmt(cell["avg_floor"], 1)}'
    lines.append(row("ALL", data["total"]))
    lines.append(" by ascension band")
    lines += [row(k, v) for k, v in sorted(data["by_band"].items(), key=lambda kv: band_order(kv[0]))]
    lines.append(" by character")
    lines += [row(k, v) for k, v in sorted(data["by_character"].items())]
    lines.append(" by band and character")
    lines += [row(k, v) for k, v in sorted(data["by_band_character"].items(),
                                           key=lambda kv: (band_order(kv[0].split()[0]), kv[0]))]
    return lines


def format_damage(data: dict) -> list:
    lines = ["== 2. Damage taken per room (history) =="]
    for band, cell in data.items():
        lines.append(f' {band}: {cell["n_runs"]} runs')
        for act, acell in cell["acts"].items():
            for rtype, tcell in acell["types"].items():
                lines.append(f'  A{act} {rtype:8s} {_stat_cols(tcell["damage"])} rooms/run={tcell["rooms_per_run"]:.2f}')
    return lines


def format_boss(data: dict) -> list:
    lines = ["== 3. HP entering the boss (fraction of max), passed vs failed =="]

    def cols(name, c):
        return (f'  {name:24s} reached={c["reached"]:3d} passed={c["passed"]:3d} hp_in pass={fmt(c["hp_in_pass"])} '
                f'fail={fmt(c["hp_in_fail"])} dmg_pass={fmt(c["damage_pass"], 0)} '
                f'turns pass={fmt(c["turns_pass"], 1)} fail={fmt(c["turns_fail"], 1)}')
    for band, cell in data.items():
        lines.append(f" {band}")
        lines += [cols(f"A{a}", c) for a, c in cell["by_act"].items()]
        lines += [cols(b or "?", c) for b, c in cell["by_boss"].items()]
        lines.append(f'  pre-boss rest choice (no rest before the boss: {cell["no_rest_before_boss"]})')
        for choice, r in cell["pre_boss_rest"].items():
            lines.append(f'    {choice:12s} n={r["n"]:3d} passed={r["passed"]:3d} '
                         f'hp_before_rest={fmt(r["hp_before"])} hp_in_boss={fmt(r["hp_in"])}')
    return lines


def format_gold(data: dict) -> list:
    lines = ["== 4. Gold =="]
    for band, c in data.items():
        death = c["gold_at_death"]
        lines.append(f' {band}: {c["n_runs"]} runs  spent/run={fmt(c["spent_per_run"], 0)} '
                     f'gained/run={fmt(c["gained_per_run"], 0)} removals/run={fmt(c["removals_per_run"])}')
        lines.append(f'  gold at death (losses): {_stat_cols(death)}  losses with >=300: {c["losses_with_300_plus"]}')
        for act, a in c["gold_at_act_end"].items():
            lines.append(f'  gold at end of A{act} (boss beaten): n={a["n"]} mean={a["mean"]:.0f} median={a["median"]:.0f}')
    return lines


def format_cards(data: dict) -> list:
    lines = ["== 5. Card picks / skips and deck size =="]
    for band, c in data.items():
        lines.append(f" {band}")
        for key, v in c["picks"].items():
            total = v["pick"] + v["skip"]
            lines.append(f'  {key:12s} pick={v["pick"]:4d} skip={v["skip"]:4d} skip_rate={fmt(ratio(v["skip"], total))}')
        for act, d in c["deck_size_entering_boss"].items():
            lines.append(f'  deck size entering A{act} boss: n={d["n"]} mean={d["mean"]:.1f} median={d["median"]:.0f}')
    return lines


def format_route(data: dict) -> list:
    lines = ["== 6. Trace: lookahead predicted vs actual HP ratio at the boss =="]
    if not data:
        lines.append("  (no map.la_best_path rules with a boss map_select after them)")
    for key, c in data.items():
        lines.append(f'  {key:18s} n={c["n"]:4d} predicted={c["predicted"]:.2f} actual={c["actual"]:.2f} '
                     f'overpredict>0.2: {c["overpredict_gt_0.2"]:.2f}')
    return lines


def format_events(data: dict) -> list:
    lines = ["== 7. Trace: event_choice decisions =="]
    lines.append(f'  decisions={data["decisions"]} all-options-zero={data["all_options_zero"]}')
    lines.append("  most common zero-score titles:")
    lines += [f"    {n:4d}  {title}" for title, n in data["zero_score_titles"][:15]]
    debug = data["debug"]
    if debug:
        lines.append(f'  debug.event_unscored: {debug["replies_with_event_unscored"]} replies, '
                     f'{debug["event_all_zero"]} with event_all_zero')
        lines += [f"    {n:4d}  {title}" for title, n in debug["titles"][:15]]
    else:
        lines.append("  (no replies carry debug.event_unscored yet)")
    return lines


def format_card_scores(data: dict) -> list:
    lines = ["== 8. Trace: card_reward score components =="]
    for act, c in data.items():
        lines.append(f'  {act}: n={c["n"]} deck_size med={fmt(c["deck_size_median"], 0)} '
                     f'best score med={fmt(c["best_score_median"], 1)}')
        if c["margin"]:
            lines.append(f'     debug margin over threshold: {_stat_cols(c["margin"])}')
        for key, v in c["components"].items():
            lines.append(f'     {key:28s} mean={v["mean"]:6.2f} min={v["min"]:6.2f} max={v["max"]:6.2f} '
                         f'frac_nonzero={v["frac_nonzero"]:.2f}')
    return lines


def format_combat_end(data: dict) -> list:
    lines = ["== 2b. Damage per room from combat_end decisions (trace) =="]
    for band, cells in data.items():
        lines.append(f" {band}")
        lines += [f'  {key:14s} {_stat_cols(c["damage"])} won={c["won"]}/{c["n"]}' for key, c in cells.items()]
    return lines


# --------------------------------------------------------------------------- driver

def build_report(user_dir: str, since=None, character=None, min_asc=None, max_asc=None) -> tuple[str, dict]:
    rows = [r for r in load_campaign_rows(user_dir) if row_ok(r, since, character, min_asc, max_asc)]
    runs, skipped = load_runs(rows)
    groups = [g for g in group_trace(load_trace(user_dir)) if group_ok(g, since, character, min_asc, max_asc)]
    data = {"runs": sec_runs(rows), "damage": sec_damage(runs), "boss": sec_boss(runs),
            "gold": sec_gold(runs), "cards": sec_cards(runs), "route": sec_route(groups),
            "events": sec_events(groups), "card_scores": sec_card_scores(groups),
            "combat_end": sec_combat_end(groups),
            "inputs": {"user_dir": user_dir, "campaign_rows": len(rows), "runs_with_history": len(runs),
                       "history_missing": skipped, "trace_groups": len(groups),
                       "trace_lines": sum(len(g["recs"]) for g in groups)}}
    head = (f'Campaign report: {user_dir}\n  {len(rows)} runs ({len(runs)} with a readable .run file, '
            f'{skipped} skipped), {data["inputs"]["trace_lines"]} trace lines in {len(groups)} run groups')
    blocks = [head.splitlines(), format_runs(data["runs"]), format_damage(data["damage"])]
    if data["combat_end"]:
        blocks.append(format_combat_end(data["combat_end"]))
    blocks += [format_boss(data["boss"]), format_gold(data["gold"]), format_cards(data["cards"]),
               format_route(data["route"]), format_events(data["events"]),
               format_card_scores(data["card_scores"])]
    return "\n\n".join("\n".join(b) for b in blocks) + "\n", data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user-dir", default=DEFAULT_USER_DIR)
    parser.add_argument("--json", metavar="PATH", help="also write the aggregates as JSON")
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--character", help="character id prefix, e.g. NECRO or Regent")
    parser.add_argument("--min-asc", type=int)
    parser.add_argument("--max-asc", type=int)
    args = parser.parse_args(argv)
    if not os.path.isdir(args.user_dir):
        print(f"no such directory: {args.user_dir}", file=sys.stderr)
        return 2
    text, data = build_report(args.user_dir, args.since, args.character, args.min_asc, args.max_asc)
    sys.stdout.write(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
