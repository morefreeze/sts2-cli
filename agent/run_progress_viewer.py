#!/usr/bin/env python3
"""Timeline viewer for GameLogger JSONL files.

Usage:
    .venv/bin/python agent/run_progress_viewer.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import posixpath
import re
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
ADVISOR_URL = "https://ing-gom.github.io/sts2-card-advisor/"
TRANSLATION_CACHE_TTL_SECONDS = 24 * 60 * 60
_TRANSLATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def format_room_label(context: dict[str, Any]) -> str:
    """Return an act-relative room label such as A1F7 or A2F5."""
    act = int(context.get("act") or 1)
    floor = int(context.get("floor") or 0)
    return f"A{act}F{floor}"


def _global_floor(context: dict[str, Any]) -> int:
    act = int(context.get("act") or 1)
    floor = int(context.get("floor") or 0)
    return (act - 1) * 17 + floor


def _player_hp(state: dict[str, Any]) -> int | None:
    player = state.get("player") or {}
    hp = player.get("hp")
    return int(hp) if isinstance(hp, (int, float)) else None


def _compact_player(player: dict[str, Any] | None) -> dict[str, Any]:
    if not player:
        return {}
    relic_items = [
        {
            "id": r.get("id") or r.get("relic_id"),
            "name": r.get("name") or r.get("id") or "?",
        }
        for r in player.get("relics") or []
    ]
    potion_items = [
        {
            "id": p.get("id") or p.get("potion_id"),
            "name": p.get("name") or p.get("id") or "?",
        }
        for p in player.get("potions") or []
    ]
    return {
        "name": player.get("name"),
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "block": player.get("block"),
        "gold": player.get("gold"),
        "deck_size": player.get("deck_size"),
        "relics": [r["name"] for r in relic_items],
        "relic_items": relic_items,
        "potions": [p["name"] for p in potion_items],
        "potion_items": potion_items,
        "deck": [
            {
                "id": c.get("id"),
                "name": c.get("name") or c.get("id") or "?",
                "type": c.get("type"),
                "upgraded": bool(c.get("upgraded")),
            }
            for c in (player.get("deck") or [])[:80]
        ],
    }


def _new_room(state: dict[str, Any], source_index: int) -> dict[str, Any]:
    context = state.get("context") or {}
    player = state.get("player") or {}
    hp = _player_hp(state)
    return {
        "id": f"{format_room_label(context)}:{context.get('room_type') or '?'}:{source_index}",
        "label": format_room_label(context),
        "act": int(context.get("act") or 1),
        "floor": int(context.get("floor") or 0),
        "global_floor": _global_floor(context),
        "room_type": context.get("room_type") or "?",
        "act_name": context.get("act_name"),
        "boss": (context.get("boss") or {}).get("name"),
        "start_step": source_index,
        "end_step": source_index,
        "start_hp": hp,
        "end_hp": hp,
        "max_hp": player.get("max_hp"),
        "hp_loss": 0,
        "status": "in_progress",
        "decisions": [],
        "options": [],
        "actions": [],
        "combat": {"turns": [], "rounds": []},
        "start_player": _compact_player(player),
        "end_player": _compact_player(player),
        "event_name": state.get("event_name"),
        "description": state.get("description"),
        "_seen_options": set(),
    }


def _append_decision(room: dict[str, Any], decision: str | None) -> None:
    if decision and decision not in room["decisions"]:
        room["decisions"].append(decision)


def _option_key(kind: str, value: Any) -> str:
    return f"{kind}:{json.dumps(value, sort_keys=True, default=str)}"


def _append_option(room: dict[str, Any], option: dict[str, Any]) -> None:
    key = _option_key(option["kind"], option.get("match"))
    if key in room["_seen_options"]:
        return
    room["_seen_options"].add(key)
    room["options"].append(option)


def _collect_options(room: dict[str, Any], state: dict[str, Any]) -> None:
    decision = state.get("decision")
    if state.get("event_name") and not room.get("event_name"):
        room["event_name"] = state.get("event_name")
    if state.get("description") and not room.get("description"):
        room["description"] = state.get("description")

    for option in state.get("options") or []:
        index = option.get("index")
        _append_option(
            room,
            {
                "kind": "event_option",
                "label": option.get("title") or f"Option {index}",
                "detail": option.get("description") or "",
                "match": {"index": index},
                "selected": False,
            },
        )

    for choice in state.get("choices") or []:
        label = choice.get("type") or "Map node"
        _append_option(
            room,
            {
                "kind": "map_choice",
                "label": label,
                "detail": f"col={choice.get('col')} row={choice.get('row')}",
                "match": {"col": choice.get("col"), "row": choice.get("row")},
                "selected": False,
            },
        )

    if decision in {"card_reward", "card_select"}:
        kind = "card_reward" if decision == "card_reward" else "card_select"
        for card in state.get("cards") or []:
            index = card.get("index")
            parts = [card.get("type"), card.get("rarity")]
            detail = " ".join(str(p) for p in parts if p)
            _append_option(
                room,
                {
                    "kind": kind,
                    "item_id": card.get("id"),
                    "item_type": "card",
                    "label": card.get("name") or card.get("id") or f"Card {index}",
                    "detail": detail,
                    "match": {"index": index},
                    "selected": False,
                },
            )


def _collect_combat(room: dict[str, Any], state: dict[str, Any], source_index: int) -> None:
    if state.get("decision") != "combat_play":
        return
    snapshot = _combat_snapshot(state, source_index)
    room["combat"]["turns"].append(
        {
            "step": source_index,
            "round": snapshot.get("round"),
            "hp": snapshot.get("hp"),
            "block": snapshot.get("block"),
            "energy": snapshot.get("energy"),
            "enemy_names": [e.get("name") for e in snapshot["enemies"]],
            "enemies": snapshot["enemies"],
            "hand": snapshot["hand"],
        }
    )
    _update_combat_round(room, snapshot)


def _compact_card(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not card:
        return None
    return {
        "index": card.get("index"),
        "id": card.get("id"),
        "name": card.get("name") or card.get("id"),
        "cost": card.get("cost"),
        "type": card.get("type"),
        "rarity": card.get("rarity"),
        "can_play": card.get("can_play"),
        "target_type": card.get("target_type"),
    }


def _combat_snapshot(state: dict[str, Any], source_index: int) -> dict[str, Any]:
    player = state.get("player") or {}
    enemies = state.get("enemies") or []
    hand = state.get("hand") or []
    return {
        "step": source_index,
        "round": state.get("round"),
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "block": player.get("block"),
        "energy": state.get("energy"),
        "max_energy": state.get("max_energy"),
        "draw_pile_count": state.get("draw_pile_count"),
        "discard_pile_count": state.get("discard_pile_count"),
        "hand": [_compact_card(card) for card in hand],
        "potions": [
            {
                "index": potion.get("index"),
                "id": potion.get("id") or potion.get("potion_id"),
                "name": potion.get("name") or potion.get("id") or "?",
                "target_type": potion.get("target_type"),
            }
            for potion in player.get("potions") or []
        ],
        "enemies": [
            {
                "index": enemy.get("index"),
                "name": enemy.get("name") or f"Enemy {enemy.get('index')}",
                "hp": enemy.get("hp"),
                "max_hp": enemy.get("max_hp"),
                "block": enemy.get("block"),
                "intents": enemy.get("intents") or [],
                "powers": enemy.get("powers") or [],
            }
            for enemy in enemies
        ],
    }


def _close_round(round_info: dict[str, Any], end_state: dict[str, Any], reason: str) -> None:
    round_info["end_state"] = end_state
    round_info["end_reason"] = reason
    start_hp = (round_info.get("start_state") or {}).get("hp")
    end_hp = end_state.get("hp")
    if isinstance(start_hp, (int, float)) and isinstance(end_hp, (int, float)):
        round_info["hp_loss"] = max(0, int(start_hp) - int(end_hp))
    else:
        round_info["hp_loss"] = 0


def _update_combat_round(room: dict[str, Any], snapshot: dict[str, Any]) -> None:
    active = room.get("_active_combat_round")
    round_no = snapshot.get("round")
    if active is not None and active.get("round") != round_no:
        _close_round(active, snapshot, "next_round")
        active = None
    if active is None:
        active = {
            "round": round_no,
            "start_step": snapshot.get("step"),
            "start_state": snapshot,
            "actions": [],
            "end_state": snapshot,
            "end_reason": "in_round",
            "hp_loss": 0,
        }
        room["combat"]["rounds"].append(active)
        room["_active_combat_round"] = active
    active["_latest_state"] = snapshot
    if active.get("end_reason") == "in_round":
        active["end_state"] = snapshot


def _find_by_index(items: list[dict[str, Any]], index: Any) -> dict[str, Any] | None:
    for item in items:
        if item.get("index") == index:
            return item
    return None


def _combat_action_row(action: dict[str, Any], source_state: dict[str, Any] | None, step: Any) -> dict[str, Any]:
    row = {"step": step, "label": _action_label(action), "action": action}
    if not source_state:
        return row
    args = action.get("args") or {}
    name = action.get("action")
    if name == "play_card":
        card = _find_by_index(source_state.get("hand") or [], args.get("card_index"))
        target = _find_by_index(source_state.get("enemies") or [], args.get("target_index"))
        if card:
            row["card"] = _compact_card(card)
        if target:
            row["target"] = {
                "index": target.get("index"),
                "name": target.get("name"),
                "hp": target.get("hp"),
                "max_hp": target.get("max_hp"),
                "block": target.get("block"),
            }
    elif name == "use_potion":
        potion = _find_by_index((source_state.get("player") or {}).get("potions") or [], args.get("potion_index"))
        target = _find_by_index(source_state.get("enemies") or [], args.get("target_index"))
        if potion:
            row["potion"] = {
                "index": potion.get("index"),
                "id": potion.get("id") or potion.get("potion_id"),
                "name": potion.get("name") or potion.get("id"),
            }
        if target:
            row["target"] = {"index": target.get("index"), "name": target.get("name")}
    elif name == "select_cards":
        raw_indices = args.get("indices")
        if isinstance(raw_indices, str):
            indices = [int(part) for part in raw_indices.replace(",", " ").split() if part.isdigit()]
        elif isinstance(raw_indices, list):
            indices = [part for part in raw_indices if isinstance(part, int)]
        else:
            indices = []
        row["cards"] = [
            _compact_card(card)
            for card in source_state.get("cards") or []
            if card.get("index") in indices
        ]
    return row


def _append_combat_action(room: dict[str, Any], action: dict[str, Any], source_state: dict[str, Any] | None, step: Any) -> None:
    active = room.get("_active_combat_round")
    if active is None:
        return
    action_name = action.get("action")
    if action_name in {"choose_option", "select_map_node", "select_card_reward"}:
        return
    active["actions"].append(_combat_action_row(action, source_state, step))


def _close_active_combat_round(room: dict[str, Any], state: dict[str, Any], source_index: int, reason: str) -> None:
    active = room.get("_active_combat_round")
    if active is None:
        return
    if state.get("decision") == "combat_play":
        end_state = _combat_snapshot(state, source_index)
    else:
        end_state = _combat_snapshot({**state, "hand": [], "enemies": [], "energy": None, "max_energy": None}, source_index)
    _close_round(active, end_state, reason)


def _action_label(action: dict[str, Any]) -> str:
    name = action.get("action") or action.get("cmd") or "action"
    args = action.get("args") or {}
    if not args:
        return str(name)
    arg_text = " ".join(f"{key}={value}" for key, value in args.items())
    return f"{name} {arg_text}"


def _mark_selected(room: dict[str, Any], action: dict[str, Any]) -> None:
    name = action.get("action")
    args = action.get("args") or {}

    if name == "choose_option":
        target_kind = "event_option"
        target = {"index": args.get("option_index")}
    elif name == "select_map_node":
        target_kind = "map_choice"
        target = {"col": args.get("col"), "row": args.get("row")}
    elif name == "select_card_reward":
        target_kind = "card_reward"
        target = {"index": args.get("card_index")}
    elif name == "select_cards":
        target_kind = "card_select"
        raw_indices = args.get("indices")
        if isinstance(raw_indices, str):
            indices = {int(part) for part in raw_indices.replace(",", " ").split() if part.isdigit()}
        elif isinstance(raw_indices, list):
            indices = {int(part) for part in raw_indices if isinstance(part, int)}
        else:
            indices = set()
        for option in room["options"]:
            index = option.get("match", {}).get("index")
            if option["kind"] == target_kind and index in indices:
                option["selected"] = True
        return
    else:
        return

    for option in room["options"]:
        if option["kind"] == target_kind and option.get("match") == target:
            option["selected"] = True


def _finalize_room(room: dict[str, Any]) -> None:
    active_round = room.get("_active_combat_round")
    if active_round is not None and active_round.get("end_reason") == "in_round":
        _close_round(active_round, active_round.get("_latest_state") or active_round["end_state"], "last_state")
    for round_info in room.get("combat", {}).get("rounds") or []:
        round_info.pop("_latest_state", None)
    start = room.get("start_hp")
    end = room.get("end_hp")
    if isinstance(start, int) and isinstance(end, int):
        room["hp_loss"] = max(0, start - end)
    if room.get("status") == "in_progress":
        room["status"] = "completed"
    room.pop("_seen_options", None)
    room.pop("_active_combat_round", None)


def parse_game_progress(entries: list[dict[str, Any]], source_name: str | None = None) -> dict[str, Any]:
    """Parse GameLogger entries into a room timeline suitable for the viewer."""
    rooms: list[dict[str, Any]] = []
    rooms_by_key: dict[tuple[int, int, str], dict[str, Any]] = {}
    rooms_by_floor: dict[tuple[int, int], dict[str, Any]] = {}
    last_state_room: dict[str, Any] | None = None
    active_room: dict[str, Any] | None = None
    character: str | None = None
    seed: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    victory = False
    last_state_data: dict[str, Any] | None = None

    def ensure_room(state: dict[str, Any], source_index: int) -> dict[str, Any]:
        nonlocal active_room
        context = state.get("context") or {}
        act = int(context.get("act") or 1)
        floor = int(context.get("floor") or 0)
        room_type = context.get("room_type") or "?"
        if room_type == "Map":
            room = rooms_by_floor.get((act, floor)) or active_room
            if room is not None:
                return room
        key = (act, floor, room_type)
        room = rooms_by_key.get(key)
        if room is None:
            room = _new_room(state, source_index)
            rooms.append(room)
            rooms_by_key[key] = room
            rooms_by_floor[(act, floor)] = room
        if room_type != "Map":
            active_room = room
        return room

    for source_index, entry in enumerate(entries):
        entry_type = entry.get("type")
        ended_at = entry.get("ts") or ended_at
        if started_at is None:
            started_at = entry.get("ts")

        if entry_type == "action":
            action = entry.get("data") or {}
            if action.get("cmd") == "start_run":
                character = action.get("character") or character
                seed = action.get("seed") or seed
                continue
            if last_state_room is not None:
                action_row = _combat_action_row(action, last_state_data, entry.get("step"))
                last_state_room["actions"].append(action_row)
                _mark_selected(last_state_room, action)
                _append_combat_action(last_state_room, action, last_state_data, entry.get("step"))
            continue

        if entry_type != "state":
            continue
        state = entry.get("data") or {}
        context = state.get("context") or {}
        if not context:
            continue

        room = ensure_room(state, source_index)
        room["end_step"] = source_index
        _append_decision(room, state.get("decision"))
        _collect_options(room, state)
        _collect_combat(room, state, source_index)
        if state.get("decision") not in {"combat_play", "card_select"}:
            _close_active_combat_round(room, state, source_index, "combat_end")

        player = state.get("player") or {}
        hp = _player_hp(state)
        if hp is not None:
            if room["start_hp"] is None:
                room["start_hp"] = hp
            room["end_hp"] = hp
        if player.get("max_hp") is not None:
            room["max_hp"] = player.get("max_hp")
        room["end_player"] = _compact_player(player)

        decision = state.get("decision")
        if decision == "game_over":
            victory = bool(state.get("victory"))
            room["status"] = "won" if victory else "dead"
        last_state_room = room
        last_state_data = state

    for room in rooms:
        _finalize_room(room)

    rooms.sort(key=lambda item: (item["global_floor"], item["start_step"]))
    if not character:
        for room in rooms:
            player_name = (room.get("start_player") or {}).get("name")
            if player_name:
                character = str(player_name).replace("The ", "")
                break
    start_hp = next((room.get("start_hp") for room in rooms if room.get("start_hp") is not None), None)
    end_hp = next((room.get("end_hp") for room in reversed(rooms) if room.get("end_hp") is not None), None)
    max_room = max(rooms, key=lambda item: item["global_floor"], default=None)

    summary = {
        "source": source_name,
        "character": character,
        "seed": seed,
        "started_at": started_at,
        "ended_at": ended_at,
        "room_count": len(rooms),
        "max_floor_label": max_room.get("label") if max_room else None,
        "max_global_floor": max_room.get("global_floor") if max_room else None,
        "start_hp": start_hp,
        "end_hp": end_hp,
        "total_hp_loss": sum(int(room.get("hp_loss") or 0) for room in rooms),
        "victory": victory,
    }
    return {"summary": summary, "rooms": rooms}


def extract_js_assignment_object(source: str, assignment_name: str) -> dict[str, Any]:
    """Extract a JSON object assigned to a JavaScript global/constant."""
    raw_name = assignment_name.removeprefix("window.")
    pattern = re.compile(rf"(?:window\.)?{re.escape(raw_name)}\s*=")
    match = pattern.search(source)
    if match is None:
        raise ValueError(f"{assignment_name} assignment not found")
    start = source.find("{", match.end())
    if start < 0:
        raise ValueError(f"{assignment_name} object start not found")

    depth = 0
    string_quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if string_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_quote:
                string_quote = None
            continue
        if char in {'"', "'"}:
            string_quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(source[start : index + 1])
    raise ValueError(f"{assignment_name} object end not found")


def _localized_name(entry: dict[str, Any], lang: str) -> str | None:
    names = entry.get("name_loc") if isinstance(entry.get("name_loc"), dict) else entry.get("name")
    if not isinstance(names, dict):
        return str(names) if names else None
    return names.get(lang) or names.get("en") or names.get("ko")


def _localized_desc(entry: dict[str, Any], lang: str) -> str | None:
    descs = entry.get("desc_loc") if isinstance(entry.get("desc_loc"), dict) else entry.get("desc") or entry.get("description")
    if not isinstance(descs, dict):
        return str(descs) if descs else None
    return descs.get(lang) or descs.get("en") or descs.get("ko")


def build_translation_catalog(
    cards_locale: dict[str, Any],
    relic_info: dict[str, Any],
    lang: str = "zh",
) -> dict[str, Any]:
    """Build compact card/relic translation maps for the frontend."""
    catalog: dict[str, Any] = {
        "lang": lang,
        "source": ADVISOR_URL,
        "cards": {},
        "card_names": {},
        "card_descs": {},
        "relics": {},
        "relic_names": {},
        "relic_descs": {},
    }

    for card_id, entry in cards_locale.items():
        if not isinstance(entry, dict):
            continue
        translated = _localized_name(entry, lang)
        english = (entry.get("name") or {}).get("en") if isinstance(entry.get("name"), dict) else None
        desc = _localized_desc(entry, lang)
        if translated:
            catalog["cards"][card_id] = translated
        if english and translated:
            catalog["card_names"][english] = translated
        if desc:
            catalog["card_descs"][card_id] = desc

    for relic_id, entry in relic_info.items():
        if not isinstance(entry, dict):
            continue
        translated = _localized_name(entry, lang)
        english = (entry.get("name_loc") or {}).get("en") if isinstance(entry.get("name_loc"), dict) else None
        desc = _localized_desc(entry, lang)
        if translated:
            catalog["relics"][relic_id] = translated
        if english and translated:
            catalog["relic_names"][english] = translated
        if desc:
            catalog["relic_descs"][relic_id] = desc

    return catalog


def _fetch_advisor_html() -> str:
    request = Request(ADVISOR_URL, headers={"User-Agent": "sts2-run-progress-viewer/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def get_translation_catalog(lang: str = "zh") -> dict[str, Any]:
    """Return cached translations from the public card-advisor page."""
    if lang == "en":
        return build_translation_catalog({}, {}, lang="en")
    if lang != "zh":
        raise ValueError(f"unsupported language: {lang}")

    now = time.time()
    cached = _TRANSLATION_CACHE.get(lang)
    if cached and now - cached[0] < TRANSLATION_CACHE_TTL_SECONDS:
        return cached[1]

    html = _fetch_advisor_html()
    cards_locale = extract_js_assignment_object(html, "window.__CARDS_LOCALE__")
    relic_info = extract_js_assignment_object(html, "RELIC_INFO")
    catalog = build_translation_catalog(cards_locale, relic_info, lang=lang)
    catalog["cached_at"] = now
    _TRANSLATION_CACHE[lang] = (now, catalog)
    return catalog


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
    return entries


def parse_log_file(path: Path) -> dict[str, Any]:
    return parse_game_progress(read_jsonl(path), source_name=path.name)


def list_log_files(limit: int = 80) -> list[dict[str, Any]]:
    if not LOG_DIR.exists():
        return []
    paths = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    logs = []
    for path in paths[:limit]:
        stat = path.stat()
        logs.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            }
        )
    return logs


def latest_log_file() -> Path | None:
    logs = list_log_files(limit=1)
    return LOG_DIR / logs[0]["name"] if logs else None


def _safe_log_path(name: str) -> Path:
    decoded = unquote(name)
    basename = posixpath.basename(decoded)
    path = (LOG_DIR / basename).resolve()
    log_root = LOG_DIR.resolve()
    if log_root not in path.parents or path.suffix != ".jsonl":
        raise ValueError("invalid log path")
    return path


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>STS2 Run Progress</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f5f1;
      --panel: #ffffff;
      --panel-2: #fbfbf8;
      --line: #d9d9d1;
      --ink: #202323;
      --muted: #666c6c;
      --teal: #1d6f66;
      --teal-soft: #e5f1ee;
      --red: #9f3737;
      --red-soft: #fff0ee;
      --amber: #8a5b16;
      --amber-soft: #fff4dc;
      --green: #2f7040;
      --shadow: 0 1px 2px rgba(0,0,0,.06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 6px;
      padding: 7px 10px;
      cursor: pointer;
    }
    button.primary {
      background: var(--teal);
      border-color: var(--teal);
      color: #fff;
    }
    button:disabled { opacity: .55; cursor: default; }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto 1fr;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: #ededE7;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .title {
      font-size: 15px;
      font-weight: 700;
      margin-right: 10px;
      white-space: nowrap;
    }
    .file {
      max-width: 260px;
      padding: 6px;
      border: 1px dashed #aaa99f;
      border-radius: 6px;
      background: #fff;
    }
    .status {
      margin-left: auto;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 38vw;
    }
    .language {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-left: auto;
      color: var(--muted);
      white-space: nowrap;
    }
    .language select {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 6px 8px;
    }
    .language + .status {
      margin-left: 0;
    }
    .layout {
      display: grid;
      grid-template-columns: 260px minmax(440px, 1fr) 380px;
      gap: 12px;
      padding: 12px;
      min-height: 0;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
      min-height: 0;
    }
    .side {
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .panel-head {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-height: 42px;
    }
    .panel-title {
      font-weight: 700;
      font-size: 13px;
    }
    .log-list {
      overflow: auto;
      padding: 6px;
    }
    .log-item {
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 8px;
      cursor: pointer;
    }
    .log-item:hover, .log-item.active {
      border-color: var(--teal);
      background: var(--teal-soft);
    }
    .log-name {
      font-weight: 650;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .log-meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .main {
      display: grid;
      grid-template-rows: auto 1fr;
      overflow: hidden;
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(6, minmax(88px, 1fr));
      gap: 8px;
      padding: 10px;
      border-bottom: 1px solid var(--line);
      background: var(--panel-2);
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fff;
      min-width: 0;
    }
    .metric-label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .metric-value {
      margin-top: 3px;
      font-weight: 750;
      font-size: 15px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .timeline-wrap {
      overflow: auto;
      padding: 12px;
    }
    .side-timeline {
      padding: 10px;
    }
    .timeline {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
      gap: 9px;
      align-content: start;
    }
    .side-timeline .timeline {
      grid-template-columns: 1fr;
    }
    .room {
      border: 1px solid var(--line);
      border-left: 4px solid #8b8e89;
      border-radius: 7px;
      background: #fff;
      min-height: 106px;
      padding: 9px;
      cursor: pointer;
      display: grid;
      grid-template-rows: auto auto 1fr auto;
      gap: 5px;
    }
    .room:hover, .room.active {
      outline: 2px solid var(--teal);
      outline-offset: 0;
    }
    .room.Elite, .room.Boss { border-left-color: var(--amber); }
    .room.Event { border-left-color: var(--teal); }
    .room.dead { background: var(--red-soft); border-color: #d8b5b0; }
    .room .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .label {
      font-weight: 800;
      font-size: 14px;
    }
    .pill {
      border-radius: 999px;
      padding: 2px 7px;
      font-size: 11px;
      background: #eee;
      color: #333;
      white-space: nowrap;
    }
    .pill.loss { color: var(--red); background: var(--red-soft); }
    .pill.ok { color: var(--green); background: #eaf4ec; }
    .room-type {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .hp-line {
      display: flex;
      align-items: center;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .bar {
      height: 6px;
      border-radius: 999px;
      background: #e9e9e2;
      overflow: hidden;
      flex: 1;
    }
    .fill {
      height: 100%;
      background: var(--teal);
    }
    .room.dead .fill { background: var(--red); }
    .room-options {
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .detail {
      overflow: auto;
    }
    .detail-body {
      padding: 12px;
    }
    .replay-body {
      overflow: auto;
      min-height: 0;
    }
    .empty {
      color: var(--muted);
      padding: 28px;
      text-align: center;
    }
    .section {
      margin-bottom: 14px;
    }
    .section h3 {
      margin: 0 0 8px;
      font-size: 13px;
    }
    .kv {
      display: grid;
      grid-template-columns: 96px 1fr;
      gap: 6px 10px;
      font-size: 12px;
    }
    .kv .k { color: var(--muted); }
    .option-row, .action-row, .turn-row, .deck-row {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      margin-bottom: 6px;
      background: #fff;
    }
    .option-row.selected {
      border-color: var(--teal);
      background: var(--teal-soft);
    }
    .option-title {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-weight: 650;
    }
    .option-detail, .small {
      color: var(--muted);
      font-size: 12px;
      margin-top: 2px;
    }
    .tag {
      border-radius: 4px;
      padding: 1px 5px;
      font-size: 11px;
      color: var(--teal);
      background: #d9eee9;
      white-space: nowrap;
    }
    .danger { color: var(--red); }
    .turn-row {
      display: grid;
      grid-template-columns: 56px 1fr;
      gap: 8px;
    }
    .round-card {
      border: 1px solid var(--line);
      border-left: 4px solid var(--teal);
      border-radius: 8px;
      margin-bottom: 10px;
      background: #fff;
      overflow: hidden;
    }
    .round-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 9px 10px;
      background: var(--panel-2);
      border-bottom: 1px solid var(--line);
      font-weight: 750;
    }
    .round-flow {
      display: grid;
      grid-template-columns: minmax(180px, 1fr) minmax(180px, 1fr) minmax(180px, 1fr);
      gap: 0;
    }
    .round-col {
      min-width: 0;
      padding: 10px;
    }
    .round-col + .round-col {
      border-left: 1px solid var(--line);
    }
    .round-col h4 {
      margin: 0 0 8px;
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0;
    }
    .state-stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(54px, 1fr));
      gap: 6px;
      margin-bottom: 8px;
    }
    .stat-box {
      background: #f7f7f3;
      border-radius: 6px;
      padding: 6px;
      min-width: 0;
    }
    .stat-box .small {
      margin-top: 0;
    }
    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-bottom: 8px;
    }
    .card-chip, .potion-chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 7px;
      background: #fff;
      font-size: 12px;
      max-width: 100%;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .card-chip.unusable {
      color: var(--muted);
      background: #f0f0ea;
    }
    .enemy-line, .action-line {
      border-top: 1px solid #ecece6;
      padding: 7px 0;
      font-size: 12px;
    }
    .enemy-line:first-child, .action-line:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .action-line b {
      color: var(--ink);
    }
    .deck-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 6px;
    }
    .deck-row {
      margin: 0;
      font-size: 12px;
    }
    @media (max-width: 1100px) {
      .layout { grid-template-columns: 220px minmax(360px, 1fr); }
      .detail { grid-column: 1 / -1; max-height: 420px; }
      .round-flow { grid-template-columns: 1fr; }
      .round-col + .round-col { border-left: 0; border-top: 1px solid var(--line); }
    }
    @media (max-width: 760px) {
      .toolbar { flex-wrap: wrap; }
      .status { max-width: 100%; width: 100%; margin-left: 0; }
      .language { margin-left: 0; }
      .layout { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, 1fr); }
      .side { max-height: 240px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="toolbar">
      <div class="title" data-i18n="appTitle">STS2 Run Progress</div>
      <input class="file" id="fileInput" type="file" accept=".jsonl,.log,.json">
      <button class="primary" id="latestBtn" data-i18n="loadLatest">Load latest</button>
      <button id="refreshBtn" data-i18n="refreshLogs">Refresh logs</button>
      <label class="language">
        <span data-i18n="language">Language</span>
        <select id="langSelect">
          <option value="en">EN</option>
          <option value="zh">中文</option>
        </select>
      </label>
      <div class="status" id="status"></div>
    </div>
    <div class="layout">
      <aside class="panel side">
        <div class="panel-head">
          <div class="panel-title" data-i18n="recentLogs">Recent Logs</div>
          <span class="small" id="logCount"></span>
        </div>
        <div class="log-list" id="logList"></div>
      </aside>
      <main class="panel main">
        <div class="summary" id="summary"></div>
        <div class="detail-body replay-body" id="detail"></div>
      </main>
      <aside class="panel detail">
        <div class="panel-head">
          <div class="panel-title" id="detailTitle" data-i18n="runTimeline">Run Timeline</div>
        </div>
        <div class="timeline-wrap side-timeline">
          <div class="timeline" id="timeline"></div>
        </div>
      </aside>
    </div>
  </div>
  <script>
    const UI_TEXT = {
      en: {
        appTitle: "STS2 Run Progress",
        loadLatest: "Load latest",
        refreshLogs: "Refresh logs",
        language: "Language",
        recentLogs: "Recent Logs",
        roomDetail: "Room Detail",
        runTimeline: "Run Timeline",
        character: "Character",
        seed: "Seed",
        maxFloor: "Max floor",
        rooms: "Rooms",
        hp: "HP",
        totalLoss: "Total loss",
        noLogs: "No JSONL logs found under logs/.",
        loadPrompt: "Load a log to view the run timeline.",
        selectRoom: "Select a room from the timeline.",
        outcome: "Outcome",
        status: "Status",
        boss: "Boss",
        decisions: "Decisions",
        options: "Options",
        noOptions: "No non-combat options were recorded for this room.",
        actions: "Actions",
        combatStates: "Combat States",
        postRoomSnapshot: "Post-room Snapshot",
        roomOutcome: "Room Outcome",
        turnReplay: "Turn Replay",
        round: "Round",
        startOfTurn: "Turn start",
        chosenActions: "Chosen actions",
        afterEnemyTurn: "After enemy turn",
        noActions: "No actions recorded.",
        noCombatReplay: "No combat replay was recorded for this room.",
        intent: "Intent",
        drawPile: "Draw",
        discardPile: "Discard",
        usable: "usable",
        unusable: "unusable",
        target: "Target",
        card: "Card",
        potion: "Potion",
        gold: "Gold",
        relics: "Relics",
        potions: "Potions",
        deckSize: "Deck size",
        enemies: "Enemies",
        hand: "Hand",
        block: "block",
        energy: "energy",
        selected: "selected",
        ready: "Ready",
        foundLogs: "Found {count} logs",
        loadingLatest: "Loading latest log...",
        loadingLog: "Loading {name}...",
        loaded: "Loaded {source} · {count} rooms",
        parsing: "Parsing {name}...",
        loadingTranslations: "Loading Chinese translations...",
        translationFallback: "Chinese object translations unavailable; using fallback names.",
      },
      zh: {
        appTitle: "STS2 进展回放",
        loadLatest: "载入最新",
        refreshLogs: "刷新日志",
        language: "语言",
        recentLogs: "最近日志",
        roomDetail: "房间详情",
        runTimeline: "房间时间线",
        character: "角色",
        seed: "种子",
        maxFloor: "最远层",
        rooms: "房间数",
        hp: "血量",
        totalLoss: "总战损",
        noLogs: "logs/ 下没有 JSONL 日志。",
        loadPrompt: "载入日志后查看进展时间线。",
        selectRoom: "从时间线选择一个房间。",
        outcome: "结果",
        status: "状态",
        boss: "首领",
        decisions: "决策",
        options: "选项",
        noOptions: "这个房间没有记录非战斗选项。",
        actions: "动作",
        combatStates: "战斗状态",
        postRoomSnapshot: "房间后快照",
        roomOutcome: "房间结果",
        turnReplay: "回合复盘",
        round: "回合",
        startOfTurn: "回合初始",
        chosenActions: "本回合选择",
        afterEnemyTurn: "怪物行动后",
        noActions: "没有记录动作。",
        noCombatReplay: "这个房间没有记录战斗回合。",
        intent: "意图",
        drawPile: "抽牌堆",
        discardPile: "弃牌堆",
        usable: "可用",
        unusable: "不可用",
        target: "目标",
        card: "卡牌",
        potion: "药水",
        gold: "金币",
        relics: "遗物",
        potions: "药水",
        deckSize: "牌组数量",
        enemies: "敌人",
        hand: "手牌",
        block: "格挡",
        energy: "能量",
        selected: "已选",
        ready: "就绪",
        foundLogs: "找到 {count} 个日志",
        loadingLatest: "正在载入最新日志...",
        loadingLog: "正在载入 {name}...",
        loaded: "已载入 {source} · {count} 个房间",
        parsing: "正在解析 {name}...",
        loadingTranslations: "正在载入中文翻译...",
        translationFallback: "中文对象翻译不可用，已使用原名。",
      },
    };
    const ROOM_TYPES = {
      zh: { Monster: "普通怪", Elite: "精英", Boss: "首领", Event: "事件", Map: "地图", Rest: "休息", RestSite: "休息", Shop: "商店", Treasure: "宝箱", Unknown: "未知" },
    };
    const DECISIONS = {
      zh: { event_choice: "事件选择", map_select: "地图选择", combat_play: "战斗出牌", card_reward: "卡牌奖励", card_select: "卡牌选择", game_over: "游戏结束", shop: "商店", rest: "休息" },
    };
    const STATUSES = {
      zh: { completed: "已完成", dead: "死亡", won: "胜利", in_progress: "进行中" },
    };
    const OPTION_KINDS = {
      zh: { event_option: "事件选项", map_choice: "地图选项", card_reward: "卡牌奖励", card_select: "卡牌选择" },
    };
    const ACTION_NAMES = {
      zh: { choose_option: "选择事件", select_map_node: "选择路线", select_card_reward: "选择奖励牌", select_cards: "选择卡牌", play_card: "出牌", end_turn: "结束回合", use_potion: "使用药水", start_run: "开始运行", action: "动作" },
    };
    const ARG_NAMES = {
      zh: { option_index: "选项", col: "列", row: "行", card_index: "卡牌", card_indices: "卡牌", indices: "序号", target_index: "目标", potion_index: "药水" },
    };
    const CARD_TYPES = {
      zh: { Attack: "攻击", Skill: "技能", Power: "能力", Status: "状态", Curse: "诅咒", Quest: "任务" },
    };
    const INTENT_TYPES = {
      zh: { Attack: "攻击", StatusCard: "状态牌", Buff: "强化", Debuff: "减益", Defend: "防御", Escape: "逃跑", Unknown: "未知" },
    };
    const state = {
      logs: [],
      progress: null,
      selectedRoomId: null,
      activeLog: null,
      lang: localStorage.getItem("runProgressLang") || "en",
      translations: {},
    };
    const $ = (id) => document.getElementById(id);

    function t(key, vars = {}) {
      const text = (UI_TEXT[state.lang] && UI_TEXT[state.lang][key]) || UI_TEXT.en[key] || key;
      return text.replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? "");
    }

    function translateFromMap(map, value) {
      return (map[state.lang] && map[state.lang][value]) || value;
    }

    function tRoomType(value) { return translateFromMap(ROOM_TYPES, value); }
    function tDecision(value) { return translateFromMap(DECISIONS, value); }
    function tStatus(value) { return translateFromMap(STATUSES, value); }
    function tOptionKind(value) { return translateFromMap(OPTION_KINDS, value); }
    function tActionName(value) { return translateFromMap(ACTION_NAMES, value); }
    function tArgName(value) { return translateFromMap(ARG_NAMES, value); }
    function tCardType(value) { return translateFromMap(CARD_TYPES, value); }
    function tIntentType(value) { return translateFromMap(INTENT_TYPES, value); }

    function translateGameName(kind, id, name) {
      if (state.lang !== "zh") return name || id || "";
      const tr = state.translations || {};
      if (kind === "card") {
        return (id && tr.cards && tr.cards[id]) || (name && tr.card_names && tr.card_names[name]) || name || id || "";
      }
      if (kind === "relic") {
        return (id && tr.relics && tr.relics[id]) || (name && tr.relic_names && tr.relic_names[name]) || name || id || "";
      }
      return name || id || "";
    }

    function applyStaticText() {
      document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
      document.querySelectorAll("[data-i18n]").forEach(el => {
        el.textContent = t(el.dataset.i18n);
      });
      $("langSelect").value = state.lang;
    }

    function setStatus(text, isError = false) {
      $("status").textContent = text;
      $("status").className = isError ? "status danger" : "status";
    }

    async function apiJson(url, options) {
      const response = await fetch(url, options);
      const payload = await response.json();
      if (!response.ok || payload.error) throw new Error(payload.error || response.statusText);
      return payload;
    }

    function fmt(value, fallback = "-") {
      return value === null || value === undefined || value === "" ? fallback : value;
    }

    function bytes(n) {
      if (!Number.isFinite(n)) return "";
      if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
      return `${Math.round(n / 1024)} KB`;
    }

    function renderLogs() {
      $("logCount").textContent = state.logs.length ? `${state.logs.length}` : "";
      $("logList").innerHTML = state.logs.map(log => `
        <div class="log-item ${state.activeLog === log.name ? "active" : ""}" data-name="${escapeHtml(log.name)}">
          <div class="log-name" title="${escapeHtml(log.name)}">${escapeHtml(log.name)}</div>
          <div class="log-meta">${escapeHtml(log.mtime_text || "")} · ${bytes(log.size)}</div>
        </div>
      `).join("") || `<div class="empty">${escapeHtml(t("noLogs"))}</div>`;
      document.querySelectorAll(".log-item").forEach(item => {
        item.addEventListener("click", () => loadLog(item.dataset.name));
      });
    }

    function metric(label, value) {
      return `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value" title="${escapeHtml(String(fmt(value)))}">${escapeHtml(String(fmt(value)))}</div></div>`;
    }

    function renderSummary(summary) {
      if (!summary) {
        $("summary").innerHTML = metric(t("character"), "-") + metric(t("seed"), "-") + metric(t("maxFloor"), "-") + metric(t("rooms"), "-") + metric(t("hp"), "-") + metric(t("totalLoss"), "-");
        return;
      }
      $("summary").innerHTML = [
        metric(t("character"), summary.character),
        metric(t("seed"), summary.seed),
        metric(t("maxFloor"), summary.max_floor_label),
        metric(t("rooms"), summary.room_count),
        metric(t("hp"), `${fmt(summary.start_hp)} -> ${fmt(summary.end_hp)}`),
        metric(t("totalLoss"), summary.total_hp_loss),
      ].join("");
    }

    function optionSummary(room) {
      const selected = (room.options || []).filter(o => o.selected).map(optionLabel);
      if (selected.length) return selected.slice(0, 2).join(", ");
      const count = (room.options || []).length;
      return count ? `${count} ${t("options")}` : `${(room.actions || []).length} ${t("actions")}`;
    }

    function hpPct(room) {
      if (!room.max_hp || room.end_hp === null || room.end_hp === undefined) return 0;
      return Math.max(0, Math.min(100, (room.end_hp / room.max_hp) * 100));
    }

    function renderTimeline() {
      const rooms = state.progress?.rooms || [];
      $("timeline").innerHTML = rooms.map(room => {
        const lossClass = room.hp_loss > 0 ? "loss" : "ok";
        const statusClass = room.status === "dead" ? "dead" : "";
        return `
          <button class="room ${escapeHtml(room.room_type)} ${statusClass} ${state.selectedRoomId === room.id ? "active" : ""}" data-id="${escapeHtml(room.id)}">
            <div class="top">
              <div class="label">${escapeHtml(room.label)}</div>
              <div class="pill ${lossClass}">-${fmt(room.hp_loss, 0)}</div>
            </div>
            <div class="room-type">${escapeHtml(tRoomType(room.room_type))}${room.event_name ? ` · ${escapeHtml(room.event_name)}` : ""}</div>
            <div class="hp-line"><span>${fmt(room.start_hp)}</span><div class="bar"><div class="fill" style="width:${hpPct(room)}%"></div></div><span>${fmt(room.end_hp)}</span></div>
            <div class="room-options">${escapeHtml(optionSummary(room))}</div>
          </button>
        `;
      }).join("") || `<div class="empty">${escapeHtml(t("loadPrompt"))}</div>`;
      document.querySelectorAll(".room").forEach(item => {
        item.addEventListener("click", () => selectRoom(item.dataset.id));
      });
    }

    function selectRoom(roomId) {
      state.selectedRoomId = roomId;
      renderTimeline();
      renderDetail();
    }

    function optionLabel(option) {
      if (option.item_type === "card" || option.kind === "card_reward" || option.kind === "card_select") {
        return translateGameName("card", option.item_id, option.label);
      }
      if (option.kind === "map_choice") return tRoomType(option.label);
      return option.label;
    }

    function actionLabel(actionRow) {
      const raw = actionRow.action || {};
      const name = raw.action || raw.cmd || "action";
      const args = raw.args || {};
      if (name === "play_card" && actionRow.card) {
        const card = translateGameName("card", actionRow.card.id, actionRow.card.name);
        const target = actionRow.target?.name ? ` -> ${actionRow.target.name}` : "";
        return `${tActionName(name)} ${card}${target}`;
      }
      if (name === "use_potion" && actionRow.potion) {
        const target = actionRow.target?.name ? ` -> ${actionRow.target.name}` : "";
        return `${tActionName(name)} ${actionRow.potion.name}${target}`;
      }
      if (name === "select_cards" && actionRow.cards?.length) {
        return `${tActionName(name)} ${actionRow.cards.map(card => translateGameName("card", card.id, card.name)).join(", ")}`;
      }
      const argText = Object.entries(args)
        .map(([key, value]) => `${tArgName(key)}=${value}`)
        .join(" ");
      return `${tActionName(name)}${argText ? ` ${argText}` : ""}`;
    }

    function renderDetail() {
      const room = (state.progress?.rooms || []).find(r => r.id === state.selectedRoomId);
      if (!room) {
        $("detail").innerHTML = `<div class="empty">${escapeHtml(t("selectRoom"))}</div>`;
        return;
      }
      const hasCombatReplay = (room.combat?.rounds || []).length > 0;
      $("detail").innerHTML = `
        <div class="section">
          <h3>${escapeHtml(room.label)} ${escapeHtml(tRoomType(room.room_type))}</h3>
          <div class="kv">
            <div class="k">${escapeHtml(t("status"))}</div><div>${escapeHtml(tStatus(room.status))}</div>
            <div class="k">${escapeHtml(t("hp"))}</div><div>${fmt(room.start_hp)} -> ${fmt(room.end_hp)} <span class="${room.hp_loss ? "danger" : ""}">(-${fmt(room.hp_loss, 0)})</span></div>
            <div class="k">${escapeHtml(t("boss"))}</div><div>${escapeHtml(fmt(room.boss))}</div>
            <div class="k">${escapeHtml(t("decisions"))}</div><div>${escapeHtml((room.decisions || []).map(tDecision).join(", ") || "-")}</div>
          </div>
        </div>
        ${hasCombatReplay ? renderRoundReplay(room) : renderOptions(room)}
        ${hasCombatReplay ? "" : renderActions(room)}
        ${renderPlayer(room)}
      `;
    }

    function formatIntent(intent) {
      if (!intent) return "-";
      const type = tIntentType(intent.type || "Unknown");
      const pieces = [type];
      if (intent.damage !== undefined && intent.damage !== null) pieces.push(String(intent.damage));
      if (intent.amount !== undefined && intent.amount !== null) pieces.push(String(intent.amount));
      return pieces.join(" ");
    }

    function renderStateSnapshot(snapshot) {
      if (!snapshot) return `<div class="small">-</div>`;
      const hand = snapshot.hand || [];
      const potions = snapshot.potions || [];
      const enemies = snapshot.enemies || [];
      return `
        <div class="state-stats">
          <div class="stat-box"><div class="small">${escapeHtml(t("hp"))}</div><b>${fmt(snapshot.hp)}</b></div>
          <div class="stat-box"><div class="small">${escapeHtml(t("block"))}</div><b>${fmt(snapshot.block, 0)}</b></div>
          <div class="stat-box"><div class="small">${escapeHtml(t("energy"))}</div><b>${fmt(snapshot.energy)}</b></div>
          <div class="stat-box"><div class="small">${escapeHtml(t("drawPile"))}/${escapeHtml(t("discardPile"))}</div><b>${fmt(snapshot.draw_pile_count)}/${fmt(snapshot.discard_pile_count)}</b></div>
        </div>
        <div class="small">${escapeHtml(t("hand"))}</div>
        <div class="chip-list">
          ${hand.map(card => `
            <span class="card-chip ${card.can_play === false ? "unusable" : ""}" title="${escapeHtml(card.can_play === false ? t("unusable") : t("usable"))}">
              ${escapeHtml(translateGameName("card", card.id, card.name))}${card.cost !== undefined && card.cost !== null ? ` · ${escapeHtml(String(card.cost))}` : ""}
            </span>
          `).join("") || `<span class="small">-</span>`}
        </div>
        <div class="small">${escapeHtml(t("potions"))}</div>
        <div class="chip-list">
          ${potions.map(potion => `<span class="potion-chip">${escapeHtml(potion.name || "?")}</span>`).join("") || `<span class="small">-</span>`}
        </div>
        <div class="small">${escapeHtml(t("enemies"))}</div>
        <div>
          ${enemies.map(enemy => `
            <div class="enemy-line">
              <b>${escapeHtml(enemy.name || "?")}</b>
              <span class="small">HP ${fmt(enemy.hp)}/${fmt(enemy.max_hp)} · ${escapeHtml(t("block"))} ${fmt(enemy.block, 0)}</span>
              <div class="small">${escapeHtml(t("intent"))}: ${escapeHtml((enemy.intents || []).map(formatIntent).join(", ") || "-")}</div>
            </div>
          `).join("") || `<div class="small">-</div>`}
        </div>
      `;
    }

    function renderCombatAction(action) {
      const detail = [];
      if (action.card) detail.push(`${t("card")}: ${translateGameName("card", action.card.id, action.card.name)}`);
      if (action.potion) detail.push(`${t("potion")}: ${action.potion.name}`);
      if (action.target) detail.push(`${t("target")}: ${action.target.name}`);
      return `
        <div class="action-line">
          <b>${escapeHtml(actionLabel(action))}</b>
          ${detail.length ? `<div class="small">${escapeHtml(detail.join(" · "))}</div>` : ""}
        </div>
      `;
    }

    function renderRoundReplay(room) {
      const rounds = room.combat?.rounds || [];
      if (!rounds.length) return `<div class="section"><h3>${escapeHtml(t("turnReplay"))}</h3><div class="small">${escapeHtml(t("noCombatReplay"))}</div></div>`;
      return `<div class="section"><h3>${escapeHtml(t("turnReplay"))}</h3>${rounds.map(round => `
        <div class="round-card">
          <div class="round-head">
            <span>${escapeHtml(t("round"))} ${fmt(round.round)}</span>
            <span class="pill ${round.hp_loss ? "loss" : "ok"}">-${fmt(round.hp_loss, 0)}</span>
          </div>
          <div class="round-flow">
            <div class="round-col">
              <h4>${escapeHtml(t("startOfTurn"))}</h4>
              ${renderStateSnapshot(round.start_state)}
            </div>
            <div class="round-col">
              <h4>${escapeHtml(t("chosenActions"))}</h4>
              ${(round.actions || []).map(renderCombatAction).join("") || `<div class="small">${escapeHtml(t("noActions"))}</div>`}
            </div>
            <div class="round-col">
              <h4>${escapeHtml(t("afterEnemyTurn"))}</h4>
              ${renderStateSnapshot(round.end_state)}
            </div>
          </div>
        </div>
      `).join("")}</div>`;
    }

    function renderOptions(room) {
      const options = room.options || [];
      if (!options.length) return `<div class="section"><h3>${escapeHtml(t("options"))}</h3><div class="small">${escapeHtml(t("noOptions"))}</div></div>`;
      return `<div class="section"><h3>${escapeHtml(t("options"))}</h3>${options.map(option => `
        <div class="option-row ${option.selected ? "selected" : ""}">
          <div class="option-title">
            <span>${escapeHtml(optionLabel(option))}</span>
            <span class="tag">${escapeHtml(tOptionKind(option.kind))}${option.selected ? ` ${escapeHtml(t("selected"))}` : ""}</span>
          </div>
          <div class="option-detail">${escapeHtml(option.detail || "")}</div>
        </div>
      `).join("")}</div>`;
    }

    function renderActions(room) {
      const actions = room.actions || [];
      if (!actions.length) return "";
      return `<div class="section"><h3>${escapeHtml(t("actions"))}</h3>${actions.map(action => `
        <div class="action-row"><b>step ${fmt(action.step)}</b> · ${escapeHtml(actionLabel(action))}</div>
      `).join("")}</div>`;
    }

    function renderCombat(room) {
      const turns = room.combat?.turns || [];
      if (!turns.length) return "";
      return `<div class="section"><h3>${escapeHtml(t("combatStates"))}</h3>${turns.map(turn => `
        <div class="turn-row">
          <div><b>#${fmt(turn.step)}</b><br><span class="small">R${fmt(turn.round)}</span></div>
          <div>
            <div>${escapeHtml(t("hp"))} ${fmt(turn.hp)} · ${escapeHtml(t("block"))} ${fmt(turn.block)} · ${escapeHtml(t("energy"))} ${fmt(turn.energy)}</div>
            <div class="small">${escapeHtml(t("enemies"))}: ${escapeHtml((turn.enemy_names || []).join(", ") || "-")}</div>
            <div class="small">${escapeHtml(t("hand"))}: ${escapeHtml((turn.hand || []).map(c => translateGameName("card", c.id, c.name)).join(", ") || "-")}</div>
          </div>
        </div>
      `).join("")}</div>`;
    }

    function renderPlayer(room) {
      const player = room.end_player || {};
      const deck = player.deck || [];
      const relics = (player.relic_items || player.relics || []).map(item => {
        if (typeof item === "string") return translateGameName("relic", null, item);
        return translateGameName("relic", item.id, item.name);
      });
      const potions = (player.potion_items || player.potions || []).map(item => typeof item === "string" ? item : item.name);
      return `<div class="section">
        <h3>${escapeHtml(t("postRoomSnapshot"))}</h3>
        <div class="kv">
          <div class="k">${escapeHtml(t("gold"))}</div><div>${fmt(player.gold)}</div>
          <div class="k">${escapeHtml(t("relics"))}</div><div>${escapeHtml(relics.join(", ") || "-")}</div>
          <div class="k">${escapeHtml(t("potions"))}</div><div>${escapeHtml(potions.join(", ") || "-")}</div>
          <div class="k">${escapeHtml(t("deckSize"))}</div><div>${fmt(player.deck_size)}</div>
        </div>
        <div class="deck-grid" style="margin-top:8px">
          ${deck.slice(0, 30).map(card => `<div class="deck-row">${escapeHtml(translateGameName("card", card.id, card.name || "?"))}${card.upgraded ? "+" : ""}<div class="small">${escapeHtml(tCardType(card.type || ""))}</div></div>`).join("")}
        </div>
      </div>`;
    }

    async function refreshLogs() {
      try {
        const payload = await apiJson("/api/logs");
        state.logs = payload.logs || [];
        renderLogs();
        setStatus(t("foundLogs", { count: state.logs.length }));
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    async function loadLatest() {
      try {
        setStatus(t("loadingLatest"));
        const payload = await apiJson("/api/latest");
        loadProgress(payload.progress, payload.name);
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    async function loadLog(name) {
      try {
        setStatus(t("loadingLog", { name }));
        const payload = await apiJson(`/api/log?name=${encodeURIComponent(name)}`);
        loadProgress(payload.progress, name);
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    function loadProgress(progress, activeName) {
      state.progress = progress;
      state.activeLog = activeName || progress?.summary?.source || null;
      state.selectedRoomId = progress?.rooms?.[0]?.id || null;
      renderLogs();
      renderSummary(progress.summary);
      renderTimeline();
      renderDetail();
      setStatus(t("loaded", { source: fmt(progress.summary.source, activeName), count: progress.summary.room_count }));
    }

    async function parseSelectedFile(file) {
      if (!file) return;
      try {
        setStatus(t("parsing", { name: file.name }));
        const text = await file.text();
        const payload = await apiJson("/api/parse", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_name: file.name, text }),
        });
        loadProgress(payload.progress, file.name);
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    function rerender() {
      applyStaticText();
      renderLogs();
      renderSummary(state.progress?.summary || null);
      renderTimeline();
      renderDetail();
    }

    async function loadTranslations() {
      if (state.lang !== "zh") {
        state.translations = {};
        return;
      }
      setStatus(t("loadingTranslations"));
      try {
        const payload = await apiJson("/api/translations?lang=zh");
        state.translations = payload.translations || {};
        if (payload.error) setStatus(t("translationFallback"), true);
      } catch (error) {
        state.translations = {};
        setStatus(t("translationFallback"), true);
      }
    }

    async function setLanguage(lang) {
      state.lang = lang === "zh" ? "zh" : "en";
      localStorage.setItem("runProgressLang", state.lang);
      applyStaticText();
      await loadTranslations();
      rerender();
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[ch]));
    }

    $("latestBtn").addEventListener("click", loadLatest);
    $("refreshBtn").addEventListener("click", refreshLogs);
    $("fileInput").addEventListener("change", event => parseSelectedFile(event.target.files[0]));
    $("langSelect").addEventListener("change", event => setLanguage(event.target.value));
    applyStaticText();
    setStatus(t("ready"));
    renderSummary(null);
    renderDetail();
    setLanguage(state.lang).then(() => refreshLogs()).then(loadLatest);
  </script>
</body>
</html>
"""


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "RunProgressViewer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_text(HTML)
            elif parsed.path == "/api/logs":
                self._send_json({"logs": list_log_files()})
            elif parsed.path == "/api/latest":
                path = latest_log_file()
                if path is None:
                    self._send_error("no logs found", HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"name": path.name, "progress": parse_log_file(path)})
            elif parsed.path == "/api/translations":
                query = parse_qs(parsed.query)
                lang = (query.get("lang") or ["zh"])[0]
                try:
                    self._send_json({"translations": get_translation_catalog(lang)})
                except Exception as exc:
                    self._send_json(
                        {
                            "translations": build_translation_catalog({}, {}, lang=lang),
                            "error": str(exc),
                        }
                    )
            elif parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                name = (query.get("name") or [""])[0]
                if not name:
                    self._send_error("missing log name")
                    return
                path = _safe_log_path(name)
                if not path.exists():
                    self._send_error("log not found", HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"name": path.name, "progress": parse_log_file(path)})
            elif parsed.path == "/favicon.ico":
                self._send_text("", mimetypes.types_map.get(".ico", "image/x-icon"))
            else:
                self._send_error("not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - defensive for interactive server
            self._send_error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/parse":
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            source_name = payload.get("source_name") or "uploaded.jsonl"
            text = payload.get("text") or ""
            entries = []
            for line_no, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped:
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"{source_name}:{line_no}: invalid JSONL: {exc}") from exc
            self._send_json({"progress": parse_game_progress(entries, source_name=source_name)})
        except Exception as exc:
            self._send_error(str(exc))


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), ViewerHandler)
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"Run progress viewer: {url}", flush=True)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--parse", type=Path, help="Parse one JSONL log and print normalized JSON")
    args = parser.parse_args(argv)

    if args.parse:
        print(json.dumps(parse_log_file(args.parse), ensure_ascii=False, indent=2))
        return 0
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
