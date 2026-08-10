"""Parse GameLogger state/action records into a replay timeline."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any


MIN_ACT = 1
MAX_ACT = 4
MIN_FLOOR = 0
MAX_FLOOR = 17


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            key: _json_safe_value(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    return None


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return _json_safe_value(value)


def _optional_mapping(
    record: Mapping[str, Any], key: str, label: str
) -> dict[str, Any]:
    value = record.get(key)
    if value is None:
        return {}
    return _require_mapping(value, label)


def _mapping_list(record: Mapping[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = record.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [
        _json_safe_value(item)
        for item in value
        if isinstance(item, Mapping)
    ]


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _timestamp(value: Any) -> str | int | float | None:
    if isinstance(value, str):
        return value or None
    return _finite_number(value)


def _normalize_numeric_fields(record: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in record:
            record[key] = _finite_number(record[key])


def _normalize_text_field(record: dict[str, Any], key: str) -> None:
    if key in record and not isinstance(record[key], str):
        record[key] = None


def _normalize_action(raw_action: Any) -> dict[str, Any]:
    action = _require_mapping(raw_action, "action data")
    _normalize_text_field(action, "cmd")
    _normalize_text_field(action, "action")
    raw_args = raw_action.get("args")
    if raw_args is not None or "args" in raw_action:
        args = _optional_mapping(raw_action, "args", "action args")
        _normalize_numeric_fields(
            args,
            "option_index",
            "card_index",
            "target_index",
            "potion_index",
            "col",
            "row",
        )
        action["args"] = args
    return action


def _normalize_state(raw_state: Any) -> dict[str, Any]:
    state = _require_mapping(raw_state, "state data")
    context = _optional_mapping(raw_state, "context", "state context")
    player = _optional_mapping(raw_state, "player", "state player")

    for key in ("room_type", "act_name", "run_id", "character", "seed"):
        _normalize_text_field(context, key)
    for key in ("game_version", "build_id"):
        _normalize_text_field(context, key)
    if "boss" in context:
        boss = _optional_mapping(context, "boss", "state context boss")
        for key in ("id", "name"):
            _normalize_text_field(boss, key)
        context["boss"] = boss

    for key in ("name",):
        _normalize_text_field(player, key)
    _normalize_numeric_fields(player, "hp", "max_hp", "block", "gold", "deck_size")
    raw_player = raw_state.get("player")
    for key in ("deck", "relics", "potions"):
        if isinstance(raw_player, Mapping) and key in raw_player:
            player[key] = _mapping_list(raw_player, key, f"state player {key}")

    for card in player.get("deck") or []:
        _normalize_numeric_fields(card, "index", "cost")
    for potion in player.get("potions") or []:
        _normalize_numeric_fields(potion, "index")

    state["context"] = context
    state["player"] = player
    _normalize_text_field(state, "decision")
    _normalize_text_field(state, "event_name")
    _normalize_text_field(state, "description")
    _normalize_numeric_fields(
        state,
        "round",
        "energy",
        "max_energy",
        "draw_pile_count",
        "discard_pile_count",
    )

    for key in ("options", "choices", "cards", "hand", "enemies"):
        state[key] = _mapping_list(raw_state, key, f"state {key}")
    for option in state["options"]:
        _normalize_numeric_fields(option, "index")
    for choice in state["choices"]:
        _normalize_numeric_fields(choice, "col", "row")
    for card in [*state["cards"], *state["hand"]]:
        _normalize_numeric_fields(card, "index", "cost")
    for enemy in state["enemies"]:
        _normalize_numeric_fields(enemy, "index", "hp", "max_hp", "block")
        enemy["intents"] = _mapping_list(enemy, "intents", "state enemy intents")
        enemy["powers"] = _mapping_list(enemy, "powers", "state enemy powers")
        for intent in enemy["intents"]:
            _normalize_numeric_fields(intent, "damage", "hits")
        for power in enemy["powers"]:
            _normalize_numeric_fields(power, "amount")
    return state


def _metadata_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _metadata_ascension(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10:
        return value
    return None


def _metadata_modifiers(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _display_coordinate(
    value: Any, default: int, minimum: int, maximum: int
) -> int:
    """Preserve legacy room-display fallbacks without creating evidence."""
    if isinstance(value, bool) or not value:
        return default
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        return default
    try:
        coordinate = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    return coordinate if minimum <= coordinate <= maximum else default


def _observed_coordinate(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
    ):
        return int(value)
    return None


def _display_act_and_floor(context: dict[str, Any]) -> tuple[int, int]:
    return (
        _display_coordinate(context.get("act"), 1, MIN_ACT, MAX_ACT),
        _display_coordinate(context.get("floor"), 0, MIN_FLOOR, MAX_FLOOR),
    )


def format_room_label(context: dict[str, Any]) -> str:
    """Return an act-relative room label such as A1F7 or A2F5."""
    act, floor = _display_act_and_floor(context)
    return f"A{act}F{floor}"


def _global_floor(context: dict[str, Any]) -> int | None:
    act = _observed_coordinate(context.get("act"))
    floor = _observed_coordinate(context.get("floor"))
    if (
        act is None
        or not MIN_ACT <= act <= MAX_ACT
        or floor is None
        or not MIN_FLOOR <= floor <= MAX_FLOOR
    ):
        return None
    return (act - 1) * 17 + floor


def _display_global_floor(context: dict[str, Any]) -> int:
    act, floor = _display_act_and_floor(context)
    return (act - 1) * 17 + floor


def _player_hp(state: dict[str, Any]) -> int | None:
    player = state.get("player") or {}
    hp = _finite_number(player.get("hp"))
    return int(hp) if hp is not None else None


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
    act, floor = _display_act_and_floor(context)
    return {
        "id": f"{format_room_label(context)}:{context.get('room_type') or '?'}:{source_index}",
        "label": format_room_label(context),
        "act": act,
        "floor": floor,
        "global_floor": _display_global_floor(context),
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
    run_id: str | None = None
    character: str | None = None
    seed: str | None = None
    game_version: str | None = None
    ascension: int | None = None
    modifiers: list[str] | None = None
    started_at: Any = None
    ended_at: Any = None
    victory = False
    last_state_data: dict[str, Any] | None = None
    observed_global_floors: list[int] = []
    observed_floor_labels: dict[int, str] = {}
    has_state_records = False
    has_action_records = False
    seen_start_run = False
    first_evidence: tuple[str, dict[str, Any]] | None = None
    last_evidence: tuple[str, dict[str, Any]] | None = None

    def ensure_room(state: dict[str, Any], source_index: int) -> dict[str, Any]:
        nonlocal active_room
        context = state.get("context") or {}
        act, floor = _display_act_and_floor(context)
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

    for source_index, raw_entry in enumerate(entries):
        entry = _require_mapping(raw_entry, "entry")
        _normalize_numeric_fields(entry, "step")
        entry_type = entry.get("type")
        raw_data = entry.get("data")
        if entry_type == "action":
            data = _normalize_action(raw_data)
            evidence = (entry_type, data)
            if first_evidence is None:
                first_evidence = evidence
            last_evidence = evidence
        elif entry_type == "state":
            data = _normalize_state(raw_data)
            if first_evidence is None:
                first_evidence = (entry_type, data)
            # A state is terminal evidence only after its room coordinates and
            # decision have been accepted below.
            last_evidence = None
        else:
            data = {}
        timestamp = _timestamp(entry.get("ts"))
        if timestamp is not None:
            ended_at = timestamp
            if started_at is None:
                started_at = timestamp

        if entry_type == "action":
            has_action_records = True
            action = data
            if action.get("cmd") == "start_run":
                if seen_start_run:
                    raise ValueError("multiple start_run records")
                seen_start_run = True
                run_id = _metadata_text(action.get("run_id")) or run_id
                character = _metadata_text(action.get("character")) or character
                seed = _metadata_text(action.get("seed")) or seed
                game_version = (
                    _metadata_text(action.get("game_version"))
                    or _metadata_text(action.get("build_id"))
                    or game_version
                )
                normalized_ascension = _metadata_ascension(action.get("ascension"))
                if normalized_ascension is not None:
                    ascension = normalized_ascension
                normalized_modifiers = _metadata_modifiers(action.get("modifiers"))
                if normalized_modifiers is not None:
                    modifiers = normalized_modifiers
                continue
            if last_state_room is not None:
                action_row = _combat_action_row(action, last_state_data, entry.get("step"))
                last_state_room["actions"].append(action_row)
                _mark_selected(last_state_room, action)
                _append_combat_action(last_state_room, action, last_state_data, entry.get("step"))
            continue

        if entry_type != "state":
            continue
        has_state_records = True
        state = data
        context = state.get("context") or {}
        if not context:
            continue

        run_id = run_id or _metadata_text(context.get("run_id"))
        character = character or _metadata_text(context.get("character"))
        seed = seed or _metadata_text(context.get("seed"))
        game_version = (
            game_version
            or _metadata_text(context.get("game_version"))
            or _metadata_text(context.get("build_id"))
        )
        if ascension is None:
            ascension = _metadata_ascension(context.get("ascension"))
        if modifiers is None:
            modifiers = _metadata_modifiers(context.get("modifiers"))
        observed_global_floor = _global_floor(context)
        if observed_global_floor is not None:
            observed_global_floors.append(observed_global_floor)
            observed_floor_labels[observed_global_floor] = format_room_label(context)

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
        if observed_global_floor is not None:
            last_evidence = (entry_type, state)
        last_state_room = room
        last_state_data = state

    if not has_state_records:
        raise ValueError("no state records")

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
    first_recorded_floor = (
        min(observed_global_floors) if observed_global_floors else None
    )
    last_recorded_floor = (
        max(observed_global_floors) if observed_global_floors else None
    )
    starts_at_run_start = bool(
        first_evidence
        and first_evidence[0] == "action"
        and first_evidence[1].get("cmd") == "start_run"
        and first_recorded_floor == 1
    )
    ends_in_game_over = bool(
        last_evidence
        and last_evidence[0] == "state"
        and last_evidence[1].get("decision") == "game_over"
    )

    summary = {
        "source": _metadata_text(source_name),
        "run_id": run_id,
        "character": character,
        "seed": seed,
        "game_version": game_version,
        "ascension": ascension,
        "modifiers": modifiers,
        "started_at": started_at,
        "ended_at": ended_at,
        "room_count": len(rooms),
        "max_floor_label": observed_floor_labels.get(last_recorded_floor),
        "max_global_floor": last_recorded_floor,
        "start_hp": start_hp,
        "end_hp": end_hp,
        "total_hp_loss": sum(int(room.get("hp_loss") or 0) for room in rooms),
        "victory": victory,
        "complete_run": starts_at_run_start and ends_in_game_over,
        "first_recorded_floor": first_recorded_floor,
        "last_recorded_floor": last_recorded_floor,
        "has_state_records": has_state_records,
        "has_action_records": has_action_records,
    }
    return {"summary": summary, "rooms": rooms}
