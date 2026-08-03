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

# Preserve the documented ``python agent/run_progress_viewer.py`` entrypoint.
# Direct script execution otherwise exposes only ``agent/`` on ``sys.path``.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.run_workbench.catalog import (
    CatalogError,
    CatalogNotFoundError,
    RunCatalog,
)


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
STATIC_DIR = ROOT / "agent" / "run_workbench" / "static"
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
ADVISOR_URL = "https://ing-gom.github.io/sts2-card-advisor/"
TRANSLATION_CACHE_TTL_SECONDS = 24 * 60 * 60
PARSE_BODY_MAX_BYTES = 10 * 1024 * 1024
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



class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "RunProgressViewer/1.0"
    catalog = RunCatalog(
        [LOG_DIR, ROOT / "data"],
        replay_parser=parse_game_progress,
        include_policy="workbench",
    )

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self._send_bytes(body, content_type)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, route: str) -> None:
        asset = STATIC_ASSETS.get(route)
        if asset is None:
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(body, content_type)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _send_internal_error(self, error: Exception) -> None:
        self.log_error("unhandled request error: %r", error)
        self._send_error(
            "internal server error", HTTPStatus.INTERNAL_SERVER_ERROR
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in STATIC_ASSETS:
                if parsed.query or parsed.params or parsed.fragment:
                    self._send_error("not found", HTTPStatus.NOT_FOUND)
                    return
                self._send_static(parsed.path)
            elif parsed.path == "/api/catalog":
                self._send_json({"sources": self.catalog.list_sources()})
            elif parsed.path == "/api/cohorts":
                self._send_json({"cohorts": self.catalog.list_cohorts()})
            elif parsed.path == "/api/metrics":
                query = parse_qs(parsed.query)
                current = (query.get("current") or [""])[0]
                baseline = (query.get("baseline") or [None])[0]
                if not current:
                    self._send_error("missing current cohort")
                    return
                self._send_json(self.catalog.get_metrics(current, baseline))
            elif parsed.path == "/api/run":
                query = parse_qs(parsed.query)
                run_id = (query.get("id") or [""])[0]
                if not run_id:
                    self._send_error("missing run id")
                    return
                self._send_json(self.catalog.get_run(run_id))
            elif parsed.path == "/api/source":
                query = parse_qs(parsed.query)
                source_id = (query.get("id") or [""])[0]
                if not source_id:
                    self._send_error("missing source id")
                    return
                self._send_json(self.catalog.get_source(source_id))
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
        except CatalogNotFoundError as exc:
            self._send_error(str(exc), HTTPStatus.NOT_FOUND)
        except CatalogError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive for interactive server
            self._send_internal_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/parse":
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise CatalogError("invalid Content-Length") from exc
            if length < 0 or length > PARSE_BODY_MAX_BYTES:
                raise CatalogError("request body is too large")
            try:
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(
                    raw,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-standard numeric constant {value}")
                    ),
                )
            except UnicodeDecodeError as exc:
                raise CatalogError(f"invalid UTF-8 request body: {exc}") from exc
            except (json.JSONDecodeError, ValueError) as exc:
                raise CatalogError(f"invalid JSON request: {exc}") from exc
            if not isinstance(payload, dict):
                raise CatalogError("request must be a JSON object")
            source_name = payload.get("source_name", "uploaded.jsonl")
            text = payload.get("text", "")
            if not isinstance(source_name, str):
                raise CatalogError("source_name must be a string")
            if not isinstance(text, str):
                raise CatalogError("text must be a string")
            result = self.catalog.parse_upload(source_name, text)
            if result["view"] == "error":
                message = (result.get("errors") or ["could not parse upload"])[0]
                self._send_json(
                    {"error": message, "result": result}, HTTPStatus.BAD_REQUEST
                )
                return
            self._send_json(result)
        except CatalogError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_internal_error(exc)


def make_viewer_handler(catalog: RunCatalog) -> type[ViewerHandler]:
    """Bind one catalog to an isolated handler class for a server instance."""

    class BoundViewerHandler(ViewerHandler):
        pass

    BoundViewerHandler.catalog = catalog
    return BoundViewerHandler


def serve(
    host: str, port: int, source_roots: list[Path] | tuple[Path, ...] | None = None
) -> None:
    roots = list(source_roots) if source_roots is not None else [LOG_DIR, ROOT / "data"]
    catalog = RunCatalog(
        roots,
        replay_parser=parse_game_progress,
        include_policy="workbench" if source_roots is None else "all",
    )
    httpd = ThreadingHTTPServer((host, port), make_viewer_handler(catalog))
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"Run progress viewer: {url}", flush=True)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--parse", type=Path, help="Parse one JSONL log and print normalized JSON")
    parser.add_argument(
        "--source-root",
        action="append",
        type=Path,
        help="Catalog source root (repeatable; defaults to logs and data)",
    )
    args = parser.parse_args(argv)

    if args.parse:
        print(json.dumps(parse_log_file(args.parse), ensure_ascii=False, indent=2))
        return 0
    serve(args.host, args.port, source_roots=args.source_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
