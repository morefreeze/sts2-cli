"""Parse GameLogger state/action records into a replay timeline."""

from __future__ import annotations

import json
from typing import Any


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
    run_id: str | None = None
    character: str | None = None
    seed: str | None = None
    game_version: str | None = None
    ascension: Any = None
    modifiers: Any = None
    started_at: str | None = None
    ended_at: str | None = None
    victory = False
    last_state_data: dict[str, Any] | None = None
    observed_global_floors: list[int] = []
    has_state_records = False
    has_action_records = False
    first_evidence: tuple[str, dict[str, Any]] | None = None
    last_evidence: tuple[str, dict[str, Any]] | None = None

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
        raw_data = entry.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        if entry_type in {"action", "state"} and isinstance(raw_data, dict):
            evidence = (entry_type, data)
            if first_evidence is None:
                first_evidence = evidence
            last_evidence = evidence
        ended_at = entry.get("ts") or ended_at
        if started_at is None:
            started_at = entry.get("ts")

        if entry_type == "action":
            has_action_records = has_action_records or isinstance(raw_data, dict)
            action = data
            if action.get("cmd") == "start_run":
                run_id = action.get("run_id") or run_id
                character = action.get("character") or character
                seed = action.get("seed") or seed
                game_version = (
                    action.get("game_version")
                    or action.get("build_id")
                    or game_version
                )
                ascension = (
                    action.get("ascension")
                    if action.get("ascension") is not None
                    else ascension
                )
                modifiers = action.get("modifiers") or modifiers
                continue
            if last_state_room is not None:
                action_row = _combat_action_row(action, last_state_data, entry.get("step"))
                last_state_room["actions"].append(action_row)
                _mark_selected(last_state_room, action)
                _append_combat_action(last_state_room, action, last_state_data, entry.get("step"))
            continue

        if entry_type != "state":
            continue
        has_state_records = has_state_records or isinstance(raw_data, dict)
        state = data
        raw_context = state.get("context")
        context = raw_context if isinstance(raw_context, dict) else {}
        if not context:
            continue

        run_id = run_id or context.get("run_id")
        character = character or context.get("character")
        seed = seed or context.get("seed")
        game_version = (
            game_version
            or context.get("game_version")
            or context.get("build_id")
        )
        if ascension is None and context.get("ascension") is not None:
            ascension = context.get("ascension")
        modifiers = modifiers or context.get("modifiers")
        observed_global_floors.append(_global_floor(context))

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
    max_room = max(rooms, key=lambda item: item["global_floor"], default=None)
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
        "source": source_name,
        "run_id": run_id,
        "character": character,
        "seed": seed,
        "game_version": game_version,
        "ascension": ascension,
        "modifiers": modifiers,
        "started_at": started_at,
        "ended_at": ended_at,
        "room_count": len(rooms),
        "max_floor_label": max_room.get("label") if max_room else None,
        "max_global_floor": max_room.get("global_floor") if max_room else None,
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
