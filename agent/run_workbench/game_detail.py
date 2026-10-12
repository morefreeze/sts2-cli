"""Per-game detail for the 回归/评测 tab: what a play_full_run game did, step by step.

``build_game_detail(records)`` turns one game log (the GameLogger rows:
``run_meta``, then ``{"step","ts","type":"action"|"state","data":...}``) into:

* ``route``      -- every map choice: the nodes offered vs the one taken;
* ``deck``       -- the final deck, relics and potions, and per room what changed;
* ``rooms``      -- one entry per room with HP/gold, the deck delta, the
  out-of-combat ``choices`` (card reward, shop, rest site, event) and, for a
  fight, ``combat``: per player turn the hand/energy/block/enemies, the legal
  actions at every step with the taken one marked, the solver's plan(s) for the
  turn and where the game stopped following them.

Room structure, player snapshots (deck / relics / potions) and per-action
effects come from :func:`agent.run_workbench.replay.parse_game_progress`; this
module only adds what that parser throws away (the ``combat_plan`` replies and
the engine's error replies) and the pairing of every state with the action
taken in it.

How a plan is tied to a turn.  The harness asks for a plan with a
``plan_combat_turn`` action; the engine answers with a ``combat_plan`` state row
(or an ``error`` row), and the harness then sends the plan's cards one by one
through the first ``end_turn`` (``python/combat_plan_driver.py``).  So the reply
that follows a ``plan_combat_turn`` action belongs to the round of the last
``combat_play`` state, and every action up to the next ``plan_combat_turn`` /
next round was played "under" that plan.  A reply lists the actions of several
future turns; only those up to the first ``end_turn`` are the current turn's.

Where a turn diverged from its plan (``divergence`` markers):

``replan``      a second plan was requested within the same round;
``truncated``   the plan was dropped before its turn was played out (a stale
                plan: the card it wanted was not in hand, or the engine refused it);
``exhausted``   every planned card was played yet the turn did not end -- the
                solver's prediction of the fight ending / turn ending was wrong;
``mismatch``    an executed card / potion is not the plan's next one;
``skipped``     a planned card was left out (its target had died);
``refused``     the engine rejected an action;
``plan_error``  the solver failed and the harness fell back to its heuristic;
``heuristic``   steps played by the harness' heuristic, with no plan driving.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import json
from typing import Any, Iterable, Mapping, Sequence

from .batches import policy_from_experiment
from .localization import Localizer, bare_id
from .replay import parse_game_progress

SCHEMA_VERSION = 1
COMBAT_ROOM_TYPES = frozenset({"Monster", "Elite", "Boss"})
_ERROR_TEXT_LIMIT = 300
_UNKNOWN = "?"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _num(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _epoch(value: Any) -> float | None:
    """A log timestamp -- epoch seconds (run_meta) or an ISO string (rows)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _prettify(entry_id: str) -> str:
    """``FALLING_STAR`` -> ``Falling Star`` (only when no state shows the name)."""
    return " ".join(part.capitalize() for part in str(entry_id).split("_") if part)


def _short_error(message: Any) -> str | None:
    text = _text(message)
    if text is None:
        return None
    first = text.strip().splitlines()[0] if text.strip() else text
    return first[:_ERROR_TEXT_LIMIT]


class _Names:
    """Collects the Chinese name of everything the payload mentions, so each
    item carries just its English name / id and the page looks the rest up."""

    def __init__(self, localizer: Localizer | None) -> None:
        self.localizer = localizer
        self.table: dict[str, dict[str, str]] = {
            "card": {},
            "card_id": {},
            "potion": {},
            "potion_id": {},
            "relic": {},
            "monster": {},
            "power": {},
            "event": {},
        }

    def _put(self, kind: str, key: Any, zh: str | None) -> None:
        if zh and isinstance(key, str) and key:
            self.table[kind].setdefault(key, zh)

    def card(self, card_id: Any = None, name: Any = None) -> None:
        if self.localizer is None:
            return
        zh = self.localizer.card(card_id, name)
        if card_id:
            self._put("card_id", bare_id(card_id), zh)
        if isinstance(name, str):
            self._put("card", name.rstrip("+"), zh)

    def potion(self, potion_id: Any = None, name: Any = None) -> None:
        if self.localizer is None:
            return
        zh = self.localizer.potion(potion_id, name)
        if potion_id:
            self._put("potion_id", bare_id(potion_id), zh)
        self._put("potion", name, zh)

    def relic(self, name: Any) -> None:
        if self.localizer is not None:
            self._put("relic", name, self.localizer.relic(None, name))

    def monster(self, name: Any = None, key: Any = None) -> None:
        if self.localizer is not None:
            self._put("monster", name, self.localizer.monster(name, key))

    def power(self, name: Any) -> None:
        if self.localizer is not None:
            self._put("power", name, self.localizer.power(name))

    def event(self, name: Any) -> None:
        if self.localizer is not None:
            self._put("event", name, self.localizer.event(name))


# ---------------------------------------------------------------------------
# Cards, enemies, potions as they are shown
# ---------------------------------------------------------------------------


def _card_brief(card: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    names.card(card.get("id"), card.get("name"))
    out: dict[str, Any] = {
        "index": _int(card.get("index")),
        "id": _text(card.get("id")),
        "name": _text(card.get("name")) or _text(card.get("id")) or _UNKNOWN,
        "cost": card.get("cost") if card.get("cost") is not None else card.get("card_cost"),
        "type": _text(card.get("type")),
    }
    for key in ("rarity", "upgraded", "star_cost"):
        if key in card and card.get(key) is not None:
            out[key] = card[key]
    return out


def _hand_card(card: Mapping[str, Any], energy: Any, names: _Names) -> dict[str, Any]:
    names.card(card.get("id"), card.get("name"))
    out = {
        "index": _int(card.get("index")),
        "id": _text(card.get("id")),
        "name": _text(card.get("name")) or _text(card.get("id")) or _UNKNOWN,
        "cost": card.get("cost"),
        "type": _text(card.get("type")),
        "playable": _card_playable(card, energy),
        "target_type": _text(card.get("target_type")),
    }
    if card.get("star_cost") is not None:
        out["star_cost"] = card["star_cost"]
    return out


def _card_playable(card: Mapping[str, Any], energy: Any) -> bool:
    """A card the engine lets the player play right now: ``can_play`` and a cost
    the energy covers (a negative / missing cost is an X-cost card)."""
    if card.get("can_play") is not True:
        return False
    cost = card.get("cost")
    available = _num(energy)
    if type(cost) is int and cost >= 0 and available is not None:
        return cost <= available
    return True


def _potion_brief(potion: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    potion_id = _text(potion.get("id")) or _text(potion.get("potion_id"))
    names.potion(potion_id, potion.get("name"))
    return {
        "index": _int(potion.get("index")),
        "id": potion_id,
        "name": _text(potion.get("name")) or potion_id or _UNKNOWN,
        "target_type": _text(potion.get("target_type")),
    }


def _enemy_full(enemy: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    names.monster(enemy.get("name"))
    intents = []
    for intent in _dicts(enemy.get("intents")):
        item = {"type": _text(intent.get("type"))}
        for key in ("damage", "hits", "total_damage"):
            if _num(intent.get(key)) is not None:
                item[key] = intent[key]
        intents.append(item)
    powers = []
    for power in _dicts(enemy.get("powers")):
        names.power(power.get("name"))
        powers.append({"name": _text(power.get("name")) or _UNKNOWN, "amount": _num(power.get("amount"))})
    return {
        "index": _int(enemy.get("index")),
        "combat_id": _int(enemy.get("combat_id")),
        "name": _text(enemy.get("name")) or _UNKNOWN,
        "hp": _num(enemy.get("hp")),
        "max_hp": _num(enemy.get("max_hp")),
        "block": _num(enemy.get("block")),
        "intents": intents,
        "powers": powers,
    }


def _enemy_live(enemy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "index": _int(enemy.get("index")),
        "combat_id": _int(enemy.get("combat_id")),
        "hp": _num(enemy.get("hp")),
        "block": _num(enemy.get("block")),
    }


def _find_by_index(items: Iterable[Mapping[str, Any]], index: Any) -> Mapping[str, Any] | None:
    for item in items:
        if item.get("index") == index and index is not None:
            return item
    return None


# ---------------------------------------------------------------------------
# Row stream of a room
# ---------------------------------------------------------------------------


def _classify(row: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(row, Mapping):
        return None
    data = row.get("data")
    if not isinstance(data, dict):
        return None
    if row.get("type") == "action":
        return "action", data
    if row.get("type") != "state":
        return None
    if data.get("type") == "combat_plan":
        return "plan", data
    if data.get("type") == "error":
        return "error", data
    if data.get("decision"):
        return "state", data
    return None


class _Item:
    __slots__ = ("kind", "index", "data", "step")

    def __init__(self, kind: str, index: int, data: dict[str, Any], step: Any) -> None:
        self.kind = kind
        self.index = index
        self.data = data
        self.step = step if type(step) is int else None


def _items(records: Sequence[Any], start: int, stop: int) -> list[_Item]:
    out: list[_Item] = []
    for index in range(start, stop):
        classified = _classify(records[index])
        if classified is not None:
            out.append(_Item(classified[0], index, classified[1], records[index].get("step")))
    return out


def _action_name(data: Mapping[str, Any]) -> str | None:
    return _text(data.get("action")) or _text(data.get("cmd"))


def _args(data: Mapping[str, Any]) -> dict[str, Any]:
    args = data.get("args")
    return args if isinstance(args, dict) else {}


def _parse_indices(raw: Any) -> list[int]:
    if isinstance(raw, str):
        return [int(part) for part in raw.replace(",", " ").split() if part.isdigit()]
    if isinstance(raw, list):
        return [part for part in raw if type(part) is int]
    return []


def _actions_after(items: Sequence[_Item], position: int) -> list[tuple[_Item, _Item | None]]:
    """The actions taken in the decision at ``position``, each with the engine
    error that rejected it (``None`` if it was accepted), up to the next state."""
    out: list[tuple[_Item, _Item | None]] = []
    cursor = position + 1
    while cursor < len(items) and items[cursor].kind != "state":
        item = items[cursor]
        if item.kind == "action":
            error = items[cursor + 1] if cursor + 1 < len(items) and items[cursor + 1].kind == "error" else None
            out.append((item, error))
        cursor += 1
    return out


def _accepted(actions: Sequence[tuple[_Item, _Item | None]], *wanted: str) -> _Item | None:
    """The last accepted action of one of the ``wanted`` names (a refused first
    try is followed by the harness' fallback, which is what took effect)."""
    chosen = None
    for item, error in actions:
        if error is None and _action_name(item.data) in wanted:
            chosen = item
    return chosen


# ---------------------------------------------------------------------------
# Out-of-combat choices
# ---------------------------------------------------------------------------


def _gold(data: Mapping[str, Any]) -> int | None:
    return _int((data.get("player") or {}).get("gold")) if isinstance(data.get("player"), dict) else None


def _hp(data: Mapping[str, Any]) -> int | None:
    return _int((data.get("player") or {}).get("hp")) if isinstance(data.get("player"), dict) else None


def _card_reward_entry(item: _Item, following, room_type: str, names: _Names) -> dict[str, Any]:
    data = item.data
    offered = [_card_brief(card, names) for card in _dicts(data.get("cards"))]
    taken = _accepted(following, "select_card_reward")
    skipped = _accepted(following, "skip_card_reward") is not None and taken is None
    picked = _int(_args(taken.data).get("card_index")) if taken is not None else None
    picked_card = next((card for card in offered if card["index"] == picked), None)
    return {
        "type": "card_reward",
        "source": "combat" if room_type in COMBAT_ROOM_TYPES else "event",
        "step": item.step,
        "offered": offered,
        "picked": picked,
        "picked_card": picked_card,
        "skipped": skipped,
        "can_skip": data.get("can_skip") if isinstance(data.get("can_skip"), bool) else None,
        "gold_earned": _int(data.get("gold_earned")),
    }


def _event_entry(item: _Item, following, names: _Names, localizer: Localizer | None) -> dict[str, Any]:
    data = item.data
    event_name = _text(data.get("event_name"))
    names.event(event_name)
    options = []
    for option in _dicts(data.get("options")):
        key = _text(option.get("text_key"))
        title_zh = localizer.event_option(key) if localizer is not None else None
        description = _text(option.get("description"))
        description_zh = (
            localizer.event_option_description(key, option.get("vars")) if localizer is not None else None
        )
        options.append(
            {
                "index": _int(option.get("index")),
                "title": _text(option.get("title")) or _UNKNOWN,
                "title_zh": title_zh,
                "description": description,
                "description_zh": description_zh,
                "is_locked": bool(option.get("is_locked")),
            }
        )
    chosen_action = _accepted(following, "choose_option")
    chosen = _int(_args(chosen_action.data).get("option_index")) if chosen_action is not None else None
    left = _accepted(following, "leave_room", "proceed") is not None and chosen is None
    return {
        "type": "event",
        "step": item.step,
        "event_name": event_name,
        "description": _text(data.get("description")),
        "options": options,
        "chosen": chosen,
        "left": left,
    }


def _card_select_entry(item: _Item, following, names: _Names, *, after: str | None) -> dict[str, Any]:
    offered = [_card_brief(card, names) for card in _dicts(item.data.get("cards"))]
    action = _accepted(following, "select_cards")
    chosen = _parse_indices(_args(action.data).get("indices")) if action is not None else []
    return {
        "type": "card_select",
        "step": item.step,
        "after": after,
        "offered": offered,
        "chosen": chosen,
        "chosen_cards": [card for card in offered if card["index"] in chosen],
        "min_select": _int(item.data.get("min_select")),
        "max_select": _int(item.data.get("max_select")),
    }


def _bundle_entry(item: _Item, following, names: _Names) -> dict[str, Any]:
    bundles = []
    for bundle in _dicts(item.data.get("bundles")):
        bundles.append(
            {
                "index": _int(bundle.get("index")),
                "cards": [_card_brief(card, names) for card in _dicts(bundle.get("cards"))],
            }
        )
    action = _accepted(following, "select_bundle")
    return {
        "type": "bundle_select",
        "step": item.step,
        "bundles": bundles,
        "chosen": _int(_args(action.data).get("bundle_index")) if action is not None else None,
    }


def _next_state(items: Sequence[_Item], position: int) -> int | None:
    for cursor in range(position + 1, len(items)):
        if items[cursor].kind == "state":
            return cursor
    return None


def _rest_entry(items: Sequence[_Item], position: int, names: _Names, localizer: Localizer | None):
    """A rest site: the options vs the one chosen, and for SMITH the card that
    the card_select right after it upgraded.  Returns the entry and the
    position of that folded card_select (``None`` if there was none)."""
    item = items[position]
    following = _actions_after(items, position)
    options = []
    for option in _dicts(item.data.get("options")):
        option_id = _text(option.get("option_id"))
        options.append(
            {
                "index": _int(option.get("index")),
                "option_id": option_id,
                "name": _text(option.get("name")) or option_id or _UNKNOWN,
                "name_zh": localizer.rest_option(option_id) if localizer is not None else None,
                "is_enabled": option.get("is_enabled") is not False,
            }
        )
    action = _accepted(following, "choose_option")
    chosen = _int(_args(action.data).get("option_index")) if action is not None else None
    chosen_option = next((o for o in options if o["index"] == chosen), None)
    entry: dict[str, Any] = {
        "type": "rest",
        "step": item.step,
        "options": options,
        "chosen": chosen,
        "chosen_id": chosen_option["option_id"] if chosen_option else None,
        "upgraded_card": None,
        "hp_before": _hp(item.data),
        "hp_after": None,
    }
    consumed = None
    after = _next_state(items, position)
    if after is not None and action is not None and items[after].data.get("decision") == "card_select":
        sub = _card_select_entry(items[after], _actions_after(items, after), names, after="rest")
        entry["upgraded_card"] = sub["chosen_cards"][0] if sub["chosen_cards"] else None
        entry["card_select"] = sub
        consumed = after
        after = _next_state(items, after)
    if after is not None:
        entry["hp_after"] = _hp(items[after].data)
    return entry, consumed


def _shop_item(kind: str, raw: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    if kind == "card":
        brief = _card_brief(raw, names)
        brief["price"] = _int(raw.get("cost"))
        brief["card_cost"] = _int(raw.get("card_cost"))
    else:
        if kind == "relic":
            names.relic(raw.get("name"))
        else:
            names.potion(raw.get("id") or raw.get("potion_id"), raw.get("name"))
        brief = {
            "index": _int(raw.get("index")),
            "id": _text(raw.get("id")) or _text(raw.get("potion_id")),
            "name": _text(raw.get("name")) or _UNKNOWN,
            "price": _int(raw.get("cost")),
        }
    brief["on_sale"] = bool(raw.get("on_sale"))
    brief["is_stocked"] = raw.get("is_stocked") is not False
    return brief


def _shop_visit(items: Sequence[_Item], position: int, names: _Names):
    """One shop visit: the first stock, then what was bought / removed."""
    first = items[position].data
    stock = {
        "cards": [_shop_item("card", c, names) for c in _dicts(first.get("cards"))],
        "relics": [_shop_item("relic", r, names) for r in _dicts(first.get("relics"))],
        "potions": [_shop_item("potion", p, names) for p in _dicts(first.get("potions"))],
        "card_removal_cost": _int(first.get("card_removal_cost")),
    }
    purchases: list[dict[str, Any]] = []
    gold_start = _gold(first)
    gold_end = gold_start
    cursor = position
    while cursor < len(items):
        item = items[cursor]
        if item.kind != "state":
            cursor += 1
            continue
        decision = item.data.get("decision")
        if decision not in ("shop", "card_select"):
            break
        gold_end = _gold(item.data) if _gold(item.data) is not None else gold_end
        following = _actions_after(items, cursor)
        if decision == "shop":
            for action, error in following:
                if error is not None:
                    continue
                name = _action_name(action.data)
                args = _args(action.data)
                if name == "buy_card":
                    index = _int(args.get("card_index"))
                    raw = _find_by_index(_dicts(item.data.get("cards")), index)
                    if raw is not None:
                        purchases.append({"kind": "card", "item": _shop_item("card", raw, names),
                                          "price": _int(raw.get("cost"))})
                elif name == "buy_relic":
                    raw = _find_by_index(_dicts(item.data.get("relics")), _int(args.get("relic_index")))
                    if raw is not None:
                        purchases.append({"kind": "relic", "item": _shop_item("relic", raw, names),
                                          "price": _int(raw.get("cost"))})
                elif name == "buy_potion":
                    raw = _find_by_index(_dicts(item.data.get("potions")), _int(args.get("potion_index")))
                    if raw is not None:
                        purchases.append({"kind": "potion", "item": _shop_item("potion", raw, names),
                                          "price": _int(raw.get("cost"))})
                elif name == "remove_card":
                    purchases.append({"kind": "remove", "item": None,
                                      "price": _int(item.data.get("card_removal_cost"))})
        else:
            # The card_select that follows remove_card: which card went.
            sub = _card_select_entry(item, following, names, after="shop")
            for purchase in reversed(purchases):
                if purchase["kind"] == "remove" and purchase["item"] is None:
                    purchase["item"] = sub["chosen_cards"][0] if sub["chosen_cards"] else None
                    break
        cursor += 1
    entry = {
        "type": "shop",
        "step": items[position].step,
        "stock": stock,
        "purchases": purchases,
        "gold_start": gold_start,
        "gold_end": gold_end,
    }
    return entry, cursor


def _choices(
    items: Sequence[_Item], room_type: str, names: _Names, localizer: Localizer | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    position = 0
    previous_main: str | None = None
    previous_entry: str | None = None
    consumed: set[int] = set()
    while position < len(items):
        item = items[position]
        if item.kind != "state" or position in consumed:
            position += 1
            continue
        decision = item.data.get("decision")
        if decision == "card_select" and previous_main == "combat_play":
            position += 1  # a choice inside a fight: one of the combat's steps
            continue
        if decision != "card_select":
            previous_main = decision
        if decision == "shop":
            entry, position = _shop_visit(items, position, names)
            out.append(entry)
            previous_entry = "shop"
            continue
        if decision == "rest_site":
            entry, folded = _rest_entry(items, position, names, localizer)
            out.append(entry)
            previous_entry = "rest"
            if folded is not None:
                consumed.add(folded)
            position += 1
            continue
        following = _actions_after(items, position)
        if decision == "card_reward":
            out.append(_card_reward_entry(item, following, room_type, names))
            previous_entry = "card_reward"
        elif decision == "event_choice":
            out.append(_event_entry(item, following, names, localizer))
            previous_entry = "event"
        elif decision == "card_select":
            out.append(_card_select_entry(item, following, names, after=previous_entry))
        elif decision == "bundle_select":
            out.append(_bundle_entry(item, following, names))
            previous_entry = "bundle_select"
        position += 1
    return out


# ---------------------------------------------------------------------------
# Combat: turns, steps, legal actions
# ---------------------------------------------------------------------------


def _legal_actions(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every action the engine accepts in a ``combat_play`` state: each playable
    card (with the ``index`` of each enemy it can hit), each usable potion, and
    end_turn.  Cards and enemies are referenced by index into the same step's
    ``state.hand`` / ``state.enemies``."""
    energy = state.get("energy")
    enemies = [_int(e.get("index")) for e in _dicts(state.get("enemies"))]
    legal: list[dict[str, Any]] = []
    for card in _dicts(state.get("hand")):
        if not _card_playable(card, energy):
            continue
        entry: dict[str, Any] = {"kind": "play_card", "card_index": _int(card.get("index"))}
        if card.get("target_type") == "AnyEnemy":
            entry["targets"] = list(enemies)
        legal.append(entry)
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    for potion in _dicts(player.get("potions")):
        entry = {"kind": "use_potion", "potion_index": _int(potion.get("index"))}
        if potion.get("target_type") == "AnyEnemy":
            entry["targets"] = list(enemies)
        legal.append(entry)
    legal.append({"kind": "end_turn"})
    return legal


def _step_state(state: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    return {
        "round": _int(state.get("round")),
        "energy": _num(state.get("energy")),
        "hp": _num(player.get("hp")),
        "block": _num(player.get("block")),
        "hand": [_hand_card(c, state.get("energy"), names) for c in _dicts(state.get("hand"))],
        "enemies": [_enemy_live(e) for e in _dicts(state.get("enemies"))],
        "potions": [_potion_brief(p, names) for p in _dicts(player.get("potions"))],
    }


def _turn_start(state: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    start = _step_state(state, names)
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    start.update(
        {
            "max_hp": _num(player.get("max_hp")),
            "max_energy": _num(state.get("max_energy")),
            "draw_pile": _int(state.get("draw_pile_count")),
            "discard_pile": _int(state.get("discard_pile_count")),
            "enemies": [_enemy_full(e, names) for e in _dicts(state.get("enemies"))],
        }
    )
    powers = []
    for power in _dicts(state.get("player_powers")):
        names.power(power.get("name"))
        powers.append({"name": _text(power.get("name")) or _UNKNOWN, "amount": _num(power.get("amount"))})
    if powers:
        start["player_powers"] = powers
    orbs = [
        {"type": _text(o.get("type")) or _text(o.get("name")), "passive": _num(o.get("passive")),
         "evoke": _num(o.get("evoke"))}
        for o in _dicts(state.get("orbs"))
    ]
    if orbs:
        start["orbs"] = orbs
    if isinstance(state.get("osty"), dict):
        osty = state["osty"]
        start["osty"] = {"hp": _num(osty.get("hp")), "max_hp": _num(osty.get("max_hp")),
                         "block": _num(osty.get("block")), "alive": osty.get("alive")}
    return start


def _resolve_target(
    state: Mapping[str, Any], target_index: Any, *, needs_target: bool
) -> tuple[dict[str, Any] | None, bool]:
    """The enemy an action hit.  With one enemy alive the engine takes no
    ``target_index`` for a card that needs a target: that enemy was auto-targeted."""
    enemies = _dicts(state.get("enemies"))
    enemy = _find_by_index(enemies, target_index) if target_index is not None else None
    auto = False
    if enemy is None and target_index is None and needs_target and len(enemies) == 1:
        enemy, auto = enemies[0], True
    if enemy is None:
        return None, False
    return (
        {
            "index": _int(enemy.get("index")),
            "combat_id": _int(enemy.get("combat_id")),
            "name": _text(enemy.get("name")) or _UNKNOWN,
            "auto": auto,
        },
        auto,
    )


def _slim_effects(effects: Any) -> dict[str, Any] | None:
    if not isinstance(effects, dict):
        return None
    out = {key: value for key, value in effects.items() if key != "orbs"}
    return out or None


def _make_step(
    item: _Item,
    state: Mapping[str, Any],
    names: _Names,
    effects_by_step: Mapping[int, Any],
    number: int,
    source: str,
) -> dict[str, Any]:
    action = item.data
    name = _action_name(action) or _UNKNOWN
    args = _args(action)
    step: dict[str, Any] = {
        "n": number,
        "step": item.step,
        "kind": name if name in {"play_card", "use_potion", "end_turn", "select_cards"} else "other",
        "source": source,
        "plan": None,
        "rejected": None,
        "taken": {},
        "taken_legal": None,
        "effects": None,
    }
    if step["kind"] == "other":
        step["action"] = name
        step["taken"] = {"label": name}
    if name == "select_cards":
        step["taken"] = {"indices": _parse_indices(args.get("indices"))}
        step["legal"] = []
        if state.get("decision") == "card_select":
            step["state"] = {
                "offered": [_card_brief(c, names) for c in _dicts(state.get("cards"))],
                "min_select": _int(state.get("min_select")),
                "max_select": _int(state.get("max_select")),
            }
        else:
            step["state"] = _step_state(state, names)
        return step
    step["state"] = _step_state(state, names)
    legal = _legal_actions(state)
    step["legal"] = legal
    taken: dict[str, Any] = {}
    if name == "play_card":
        card_index = _int(args.get("card_index"))
        card = _find_by_index(_dicts(state.get("hand")), card_index)
        target, _auto = _resolve_target(
            state, _int(args.get("target_index")),
            needs_target=bool(card) and card.get("target_type") == "AnyEnemy",
        )
        taken = {
            "card_index": card_index,
            "card": _card_brief(card, names) if card is not None else None,
            "target": target,
        }
        step["taken_legal"] = next(
            (i for i, entry in enumerate(legal)
             if entry["kind"] == "play_card" and entry.get("card_index") == card_index),
            None,
        )
    elif name == "use_potion":
        potion_index = _int(args.get("potion_index"))
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        potion = _find_by_index(_dicts(player.get("potions")), potion_index)
        target, _auto = _resolve_target(
            state, _int(args.get("target_index")),
            needs_target=bool(potion) and potion.get("target_type") == "AnyEnemy",
        )
        taken = {
            "potion_index": potion_index,
            "potion": _potion_brief(potion, names) if potion is not None else None,
            "target": target,
        }
        step["taken_legal"] = next(
            (i for i, entry in enumerate(legal)
             if entry["kind"] == "use_potion" and entry.get("potion_index") == potion_index),
            None,
        )
    elif name == "end_turn":
        step["taken_legal"] = len(legal) - 1
    if taken:
        step["taken"] = taken
    effects = effects_by_step.get(item.step) if item.step is not None else None
    step["effects"] = _slim_effects(effects)
    return step


def _normalize_plan_action(
    raw: Mapping[str, Any], state: Mapping[str, Any], names: _Names
) -> dict[str, Any]:
    kind = _text(raw.get("action")) or _UNKNOWN
    out: dict[str, Any] = {"kind": kind, "turn": _int(raw.get("turn")), "status": "not_executed", "step_n": None}
    if kind == "play_card":
        card_id = bare_id(raw.get("card_id"))
        occurrence = _int(raw.get("card_occurrence")) or 0
        seen = 0
        shown = None
        for card in _dicts(state.get("hand")):
            if bare_id(card.get("id")) == card_id:
                if seen == occurrence:
                    shown = card
                    break
                seen += 1
        name = _text(shown.get("name")) if shown else None
        names.card(card_id, name)
        out.update(
            {
                "card_id": card_id,
                "card_name": name or _prettify(card_id),
                "occurrence": occurrence,
                "upgrade_level": _int(raw.get("card_upgrade_level")) or 0,
            }
        )
    elif kind == "use_potion":
        potion_id = bare_id(raw.get("potion_id"))
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        shown = next(
            (p for p in _dicts(player.get("potions"))
             if bare_id(p.get("id") or p.get("potion_id")) == potion_id),
            None,
        )
        name = _text(shown.get("name")) if shown else None
        names.potion(potion_id, name)
        out.update({"potion_id": potion_id, "potion_name": name or _prettify(potion_id)})
    if raw.get("target_combat_id") is not None or raw.get("target_index") is not None:
        combat_id = _int(raw.get("target_combat_id"))
        enemy = next((e for e in _dicts(state.get("enemies")) if e.get("combat_id") == combat_id), None)
        target_key = _text(raw.get("target_name"))
        target_name = _text(enemy.get("name")) if enemy else None
        names.monster(target_name, target_key)
        out["target"] = {
            "combat_id": combat_id,
            "index": _int(raw.get("target_index")),
            "name": target_name or (_prettify(target_key.split(".")[0]) if target_key else None),
        }
    choices = []
    for choice in _dicts(raw.get("choices")):
        choices.append(
            {
                "effect": _text(choice.get("effect")),
                "cards": [bare_id(c.get("card_id")) for c in _dicts(choice.get("cards"))],
            }
        )
    if choices:
        out["choices"] = choices
    return out


def _build_plan(
    data: Mapping[str, Any],
    state: Mapping[str, Any],
    names: _Names,
    *,
    index: int,
    action_step: Any,
    reply_step: Any,
    first_step: int,
) -> dict[str, Any]:
    actions = _dicts(data.get("actions"))
    end_at = next((i for i, a in enumerate(actions) if a.get("action") == "end_turn"), None)
    current = actions if end_at is None else actions[: end_at + 1]
    search = data.get("search") if isinstance(data.get("search"), dict) else {}
    hand_ids = [bare_id(c.get("id")) for c in _dicts(state.get("hand"))]
    return {
        "k": index,
        "ok": True,
        "error": None,
        "action_step": action_step if type(action_step) is int else None,
        "step": reply_step if type(reply_step) is int else None,
        "first_step": first_step,
        "actions": [_normalize_plan_action(a, state, names) for a in current],
        "later_actions": len(actions) - len(current),
        "turns_planned": sorted({t for t in (_int(a.get("turn")) for a in actions) if t is not None}),
        "projected_player_hp": _num(data.get("projected_player_hp")),
        "all_enemies_dead": data.get("all_enemies_dead") if isinstance(data.get("all_enemies_dead"), bool) else None,
        "only_death_routes_found": data.get("only_death_routes_found")
        if isinstance(data.get("only_death_routes_found"), bool) else None,
        "score": _num(data.get("score")),
        "search": {
            key: search.get(key)
            for key in ("budget_ms", "elapsed_ms", "boundary", "expanded_nodes", "total_expanded_nodes", "inferrer")
            if key in search
        },
        "hand_ids": hand_ids,
        "executed": 0,
        "end_reason": None,
        "reason": None,
    }


def _error_plan(error: Mapping[str, Any], *, index: int, action_step: Any, first_step: int) -> dict[str, Any]:
    return {
        "k": index,
        "ok": False,
        "error": _short_error(error.get("message")) or "error",
        "action_step": action_step if type(action_step) is int else None,
        "step": None,
        "first_step": first_step,
        "actions": [],
        "later_actions": 0,
        "turns_planned": [],
        "projected_player_hp": None,
        "all_enemies_dead": None,
        "only_death_routes_found": None,
        "score": None,
        "search": {},
        "hand_ids": [],
        "executed": 0,
        "end_reason": "error",
        "reason": None,
    }


def _plan_matches(planned: Mapping[str, Any], step: Mapping[str, Any]) -> bool:
    if planned["kind"] != step["kind"]:
        return False
    taken = step.get("taken") or {}
    if planned["kind"] == "play_card":
        card = taken.get("card") or {}
        return bare_id(card.get("id")) == planned.get("card_id")
    if planned["kind"] == "use_potion":
        potion = taken.get("potion") or {}
        return bare_id(potion.get("id")) == planned.get("potion_id")
    return planned["kind"] == "end_turn"


def _step_brief(step: Mapping[str, Any]) -> dict[str, Any]:
    taken = step.get("taken") or {}
    out: dict[str, Any] = {"n": step.get("n"), "kind": step.get("kind")}
    if taken.get("card"):
        out["card_id"] = bare_id(taken["card"].get("id"))
        out["card_name"] = taken["card"].get("name")
    if taken.get("potion"):
        out["potion_id"] = bare_id(taken["potion"].get("id"))
        out["potion_name"] = taken["potion"].get("name")
    return out


def _plan_brief(planned: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": planned.get("kind")}
    for key in ("card_id", "card_name", "potion_id", "potion_name"):
        if key in planned:
            out[key] = planned[key]
    return out


def _align_turn(turn: dict[str, Any], *, solver_used: bool, combat_ended: bool) -> None:
    """Tie every executed step to the plan that drove it and mark divergences."""
    steps: list[dict[str, Any]] = turn["steps"]
    plans: list[dict[str, Any]] = turn["solver_plans"]
    divergence: list[dict[str, Any]] = []
    for step in steps:
        step["source"] = "choice" if step["kind"] == "select_cards" else "heuristic"
    # steps played before any plan reply of the turn (no plan was asked for)
    unplanned = plans[0]["first_step"] if plans else len(steps)
    fallback_steps = 0
    for k, plan in enumerate(plans):
        start = plan["first_step"]
        stop = plans[k + 1]["first_step"] if k + 1 < len(plans) else len(steps)
        segment = steps[start:stop]
        previous = plans[k - 1] if k else None
        if k:
            divergence.append(
                {
                    "kind": "replan",
                    "plan": k,
                    "after": previous["end_reason"] if previous else None,
                    "reason": previous["reason"] if previous else None,
                    "step_n": start,
                }
            )
        if not plan["ok"]:
            divergence.append({"kind": "plan_error", "plan": k, "message": plan["error"], "step_n": start})
            fallback_steps += sum(1 for s in segment if s["kind"] != "select_cards")
            continue
        planned: list[dict[str, Any]] = plan["actions"]
        pointer = 0
        executed_end = False
        refused = False
        for step in segment:
            if step["kind"] == "select_cards":
                step["plan"] = {"plan": k, "pos": None}
                continue
            if step["kind"] not in ("play_card", "use_potion", "end_turn"):
                continue
            match = next(
                (j for j in range(pointer, len(planned)) if _plan_matches(planned[j], step)), None
            )
            if step["rejected"]:
                refused = True
                divergence.append(
                    {"kind": "refused", "plan": k, "step_n": step["n"], "message": step["rejected"]}
                )
                if match is not None:
                    planned[match]["status"] = "refused"
                    step["source"] = "plan"
                    step["plan"] = {"plan": k, "pos": match}
                else:
                    step["source"] = "off_plan"
                continue
            if match is None:
                step["source"] = "off_plan"
                expected = planned[pointer] if pointer < len(planned) else None
                divergence.append(
                    {
                        "kind": "mismatch",
                        "plan": k,
                        "step_n": step["n"],
                        "expected": _plan_brief(expected) if expected else None,
                        "actual": _step_brief(step),
                    }
                )
                continue
            for skipped in range(pointer, match):
                if planned[skipped]["status"] == "not_executed":
                    planned[skipped]["status"] = "skipped"
                    divergence.append(
                        {"kind": "skipped", "plan": k, "pos": skipped, "planned": _plan_brief(planned[skipped])}
                    )
            planned[match]["status"] = "executed"
            planned[match]["step_n"] = step["n"]
            step["source"] = "plan"
            step["plan"] = {"plan": k, "pos": match}
            pointer = match + 1
            plan["executed"] += 1
            if step["kind"] == "end_turn":
                executed_end = True
        leftover = [p for p in planned if p["status"] in ("not_executed", "refused")]
        if executed_end and not leftover:
            plan["end_reason"] = "completed"
        elif combat_ended and k + 1 == len(plans):
            plan["end_reason"] = "completed"
        elif not leftover:
            plan["end_reason"] = "exhausted"
            divergence.append({"kind": "exhausted", "plan": k, "planned": len(planned)})
        else:
            plan["end_reason"] = "truncated"
            following = plans[k + 1] if k + 1 < len(plans) else None
            upcoming = next((p for p in leftover if p["kind"] == "play_card"), None)
            if refused:
                reason = "refused"
            elif following is not None and upcoming is not None and upcoming["card_id"] not in following["hand_ids"]:
                reason = "card_missing"
            elif plan["executed"] == 0:
                reason = "no_progress"
            else:
                reason = "interrupted"
            plan["reason"] = reason
            divergence.append(
                {
                    "kind": "truncated",
                    "plan": k,
                    "executed": plan["executed"],
                    "planned": len(planned),
                    "reason": reason,
                    "next": _plan_brief(leftover[0]),
                }
            )
    heuristic = [s for s in steps[:unplanned] if s["kind"] != "select_cards"]
    if not plans:
        heuristic = [s for s in steps if s["kind"] != "select_cards"]
    if heuristic and solver_used:
        divergence.append({"kind": "heuristic", "steps": len(heuristic), "reason": "no_plan"})
    turn["fallback_steps"] = fallback_steps
    divergence.sort(key=lambda item: (item.get("step_n") if item.get("step_n") is not None else -1))
    turn["divergence"] = divergence


def _combat_turns(
    items: Sequence[_Item],
    names: _Names,
    effects_by_step: Mapping[int, Any],
    *,
    solver_used: bool,
) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    last_state: dict[str, Any] | None = None
    last_select: dict[str, Any] | None = None
    pending: _Item | None = None
    previous_main: str | None = None
    last_step: dict[str, Any] | None = None
    last_step_answered = True
    combat_ended = False
    for position, item in enumerate(items):
        if item.kind == "state":
            decision = item.data.get("decision")
            if decision == "combat_play":
                rnd = _int(item.data.get("round"))
                if current is None or current["round"] != rnd:
                    current = {
                        "round": rnd,
                        "start": _turn_start(item.data, names),
                        "steps": [],
                        "solver_plans": [],
                        "divergence": [],
                        "ended": "open",
                    }
                    turns.append(current)
                last_state = item.data
                last_step_answered = True
            elif decision == "card_select" and previous_main == "combat_play":
                last_select = item.data
                last_step_answered = True
            if decision != "card_select":
                previous_main = decision
                if decision != "combat_play":
                    last_select = None
            continue
        if current is None or previous_main != "combat_play":
            continue
        if item.kind == "action":
            name = _action_name(item.data)
            if name == "plan_combat_turn":
                pending = item
                last_step_answered = True
                continue
            if name in ("select_cards",) and last_select is not None:
                step = _make_step(item, last_select, names, effects_by_step, len(current["steps"]), "choice")
                current["steps"].append(step)
                last_step, last_step_answered = step, False
                last_select = None
                continue
            if last_state is None:
                continue
            active = current["solver_plans"][-1] if current["solver_plans"] else None
            source = "plan" if active is not None and active["ok"] else "heuristic"
            step = _make_step(item, last_state, names, effects_by_step, len(current["steps"]), source)
            current["steps"].append(step)
            last_step, last_step_answered = step, False
            continue
        if item.kind == "plan" and pending is not None and last_state is not None:
            plan = _build_plan(
                item.data, last_state, names,
                index=len(current["solver_plans"]), action_step=pending.step,
                reply_step=item.step, first_step=len(current["steps"]),
            )
            current["solver_plans"].append(plan)
            pending = None
            continue
        if item.kind == "error":
            if pending is not None:
                current["solver_plans"].append(
                    _error_plan(item.data, index=len(current["solver_plans"]),
                                action_step=pending.step, first_step=len(current["steps"]))
                )
                pending = None
            elif last_step is not None and not last_step_answered:
                last_step["rejected"] = _short_error(item.data.get("message")) or "error"
                last_step_answered = True
    # a combat is over when its last turn is not followed by another combat_play
    combat_ended = bool(turns) and previous_main not in (None, "combat_play")
    for index, turn in enumerate(turns):
        last_turn = index == len(turns) - 1
        _align_turn(turn, solver_used=solver_used, combat_ended=combat_ended and last_turn)
        for plan in turn["solver_plans"]:
            plan.pop("hand_ids", None)   # only the alignment needed it
        end_steps = [s for s in turn["steps"] if s["kind"] == "end_turn" and not s["rejected"]]
        turn["ended"] = "end_turn" if end_steps else ("combat_end" if combat_ended and last_turn else "open")
        loss = 0
        for step in turn["steps"]:
            hp = (step.get("effects") or {}).get("hp")
            if isinstance(hp, dict) and _num(hp.get("delta")) is not None and hp["delta"] < 0:
                loss += -hp["delta"]
        turn["hp_loss"] = loss
        turn["index"] = index
    return turns


def _combat_summary(turns: list[dict[str, Any]], room: Mapping[str, Any]) -> dict[str, Any]:
    plans = [plan for turn in turns for plan in turn["solver_plans"]]
    first_enemies = turns[0]["start"]["enemies"] if turns else []
    return {
        "enemies": [e["name"] for e in first_enemies],
        "rounds": len(turns),
        "turns": turns,
        "plans": sum(1 for p in plans if p["ok"]),
        "plan_errors": sum(1 for p in plans if not p["ok"]),
        "replans": sum(max(0, len(t["solver_plans"]) - 1) for t in turns),
        "diverged_turns": sum(1 for t in turns if any(d["kind"] in ("replan", "truncated", "mismatch",
                                                                      "exhausted", "plan_error", "refused")
                                                      for d in t["divergence"])),
        "heuristic_steps": sum(1 for t in turns for s in t["steps"] if s["source"] == "heuristic"
                               and s["kind"] != "select_cards"),
        "steps": sum(len(t["steps"]) for t in turns),
        "search_ms": sum((p["search"].get("elapsed_ms") or 0) for p in plans if p["ok"]),
        "hp_start": room.get("start_hp"),
        "hp_end": room.get("end_hp"),
        "hp_loss": room.get("hp_loss"),
    }


# ---------------------------------------------------------------------------
# Deck / relics / potions
# ---------------------------------------------------------------------------


def _deck_key(card: Mapping[str, Any]) -> tuple[str, bool]:
    return (bare_id(card.get("id")) or str(card.get("name") or _UNKNOWN), bool(card.get("upgraded")))


def _deck_card(card: Mapping[str, Any], names: _Names) -> dict[str, Any]:
    names.card(card.get("id"), card.get("name"))
    return {
        "id": _text(card.get("id")),
        "name": _text(card.get("name")) or _text(card.get("id")) or _UNKNOWN,
        "type": _text(card.get("type")),
        "upgraded": bool(card.get("upgraded")),
    }


def _deck_delta(start: Sequence[Mapping[str, Any]], end: Sequence[Mapping[str, Any]], names: _Names):
    """Cards gained / removed / upgraded between two deck snapshots (multiset
    diff by card id and upgrade state; a card that lost its plain form and
    gained its upgraded one counts as upgraded, not as a swap)."""
    before = Counter(_deck_key(c) for c in start)
    after = Counter(_deck_key(c) for c in end)
    sample = {_deck_key(c): c for c in [*start, *end]}
    added = after - before
    removed = before - after
    upgraded: list[dict[str, Any]] = []
    for (card_id, is_upgraded), count in list(added.items()):
        if not is_upgraded:
            continue
        plain = (card_id, False)
        move = min(count, removed.get(plain, 0))
        for _ in range(move):
            upgraded.append(_deck_card(sample[(card_id, True)], names))
        if move:
            added[(card_id, True)] -= move
            removed[plain] -= move
    def expand(counter: Counter) -> list[dict[str, Any]]:
        return [_deck_card(sample[key], names) for key, n in sorted(counter.items()) for _ in range(max(0, n))]
    return {"added": expand(added), "removed": expand(removed), "upgraded": upgraded}


def _names_of(items: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(item.get("name") or item.get("id") or _UNKNOWN) for item in items]


def _multiset_new(before: list[str], after: list[str]) -> list[str]:
    return list((Counter(after) - Counter(before)).elements())


def _inventory(player: Mapping[str, Any], names: _Names) -> tuple[list[dict], list[dict]]:
    relics = []
    for relic in _dicts(player.get("relic_items")):
        names.relic(relic.get("name"))
        relics.append({"id": _text(relic.get("id")), "name": _text(relic.get("name")) or _UNKNOWN})
    potions = []
    for potion in _dicts(player.get("potion_items")):
        names.potion(potion.get("id"), potion.get("name"))
        potions.append({"id": _text(potion.get("id")), "name": _text(potion.get("name")) or _UNKNOWN})
    return relics, potions


# ---------------------------------------------------------------------------
# The whole game
# ---------------------------------------------------------------------------


def _route(records: Sequence[Any], rooms: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every map choice, with the nodes offered, the one taken and the room it
    led to.  A node that opens no decision (a treasure chest) leads to no room:
    the next map choice follows at once, so ``entered`` is ``None``."""
    selects: list[int] = []
    for index, row in enumerate(records):
        classified = _classify(row)
        if classified is not None and classified[0] == "state" and classified[1].get("decision") == "map_select":
            selects.append(index)
    route: list[dict[str, Any]] = []
    for position, index in enumerate(selects):
        data = _classify(records[index])[1]  # type: ignore[index]
        until = selects[position + 1] if position + 1 < len(selects) else len(records)
        choices = [
            {"col": _int(c.get("col")), "row": _int(c.get("row")), "type": _text(c.get("type"))}
            for c in _dicts(data.get("choices"))
        ]
        taken = None
        for later in range(index + 1, until):
            nxt = _classify(records[later])
            if nxt is None:
                continue
            if nxt[0] == "state":
                break
            if nxt[0] == "action" and _action_name(nxt[1]) == "select_map_node":
                args = _args(nxt[1])
                taken = next(
                    (c for c in choices if c["col"] == _int(args.get("col")) and c["row"] == _int(args.get("row"))),
                    {"col": _int(args.get("col")), "row": _int(args.get("row")), "type": None},
                )
                break
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        act, floor = _int(context.get("act")), _int(context.get("floor"))
        entered = next((room for room in rooms if index < room["start_step"] < until), None)
        route.append(
            {
                "act": act,
                "floor": floor,
                "global_floor": (act - 1) * 17 + floor if act and floor is not None else None,
                "label": f"A{act}F{floor}" if act and floor is not None else None,
                "options": choices,
                "taken": taken,
                "taken_index": choices.index(taken) if taken in choices else None,
                "entered": {
                    "room_id": entered["id"],
                    "label": entered["label"],
                    "room_type": entered["room_type"],
                } if entered is not None and taken is not None else None,
                "step": records[index].get("step"),
            }
        )
    return route


def _outcome(progress_summary: Mapping[str, Any]) -> str:
    victory = progress_summary.get("victory")
    if victory is True:
        return "win"
    if victory is False:
        return "dead"
    return "in_progress"


def build_game_detail(
    records: Sequence[Any],
    *,
    source_name: str | None = None,
    localizer: Localizer | None = None,
    run_info: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The per-game payload.  Raises ``ValueError`` when the log has no states."""
    progress = parse_game_progress(list(records), source_name)
    summary_in = progress["summary"]
    rooms_in = sorted(progress["rooms"], key=lambda room: room["start_step"])
    names = _Names(localizer)
    meta = next(
        (row for row in records if isinstance(row, Mapping) and row.get("type") == "run_meta"), {}
    )

    solver_used = any(
        (classified := _classify(row)) is not None
        and classified[0] == "action"
        and _action_name(classified[1]) == "plan_combat_turn"
        for row in records
    )

    rooms_out: list[dict[str, Any]] = []
    final_deck: list[dict] = []
    final_relics: list[dict] = []
    final_potions: list[dict] = []
    for position, room in enumerate(rooms_in):
        stop = rooms_in[position + 1]["start_step"] if position + 1 < len(rooms_in) else len(records)
        stream = _items(records, room["start_step"], stop)
        effects_by_step: dict[int, Any] = {}
        for round_info in _dicts(room.get("combat", {}).get("rounds")):
            for action in _dicts(round_info.get("actions")):
                if action.get("effects") and type(action.get("step")) is int \
                        and _action_name(action.get("action") or {}) != "plan_combat_turn":
                    effects_by_step[action["step"]] = action["effects"]
        has_combat = any(i.kind == "state" and i.data.get("decision") == "combat_play" for i in stream)
        start_player = room.get("start_player") or {}
        end_player = room.get("end_player") or {}
        entry: dict[str, Any] = {
            "id": room["id"],
            "label": room["label"],
            "act": room["act"],
            "floor": room["floor"],
            "global_floor": room["global_floor"],
            "room_type": room["room_type"],
            "act_name": room.get("act_name"),
            "boss": room.get("boss"),
            "status": room.get("status"),
            "start_hp": room.get("start_hp"),
            "end_hp": room.get("end_hp"),
            "max_hp": room.get("max_hp"),
            "hp_loss": room.get("hp_loss"),
            "gold_start": _int(start_player.get("gold")),
            "gold_end": _int(end_player.get("gold")),
            "event_name": room.get("event_name"),
            "has_combat": has_combat,
        }
        entry["deck_delta"] = _deck_delta(
            _dicts(start_player.get("deck")), _dicts(end_player.get("deck")), names
        )
        start_relics, start_potions = _inventory(start_player, names)
        end_relics, end_potions = _inventory(end_player, names)
        choices = _choices(stream, room["room_type"], names, localizer)
        entry["choices"] = choices
        if has_combat:
            turns = _combat_turns(stream, names, effects_by_step, solver_used=solver_used)
            entry["combat"] = _combat_summary(turns, room)
            used = [
                (s["taken"].get("potion") or {}).get("name")
                for t in turns for s in t["steps"]
                if s["kind"] == "use_potion" and not s["rejected"] and s["taken"].get("potion")
            ]
        else:
            entry["combat"] = None
            used = []
        entry["relics_gained"] = _multiset_new(_names_of(start_relics), _names_of(end_relics))
        gained_potions = _multiset_new(
            _names_of(start_potions), _names_of(end_potions) + [u for u in used if u]
        )
        entry["potions_gained"] = gained_potions
        entry["potions_used"] = [u for u in used if u]
        rooms_out.append(entry)
        if _dicts(end_player.get("deck")):
            final_deck = _dicts(end_player.get("deck"))
        if end_player:
            final_relics, final_potions = end_relics, end_potions

    deck_counts: Counter = Counter()
    deck_sample: dict = {}
    for card in final_deck:
        key = _deck_key(card)
        deck_counts[key] += 1
        deck_sample.setdefault(key, card)
    deck_groups = [
        {**_deck_card(deck_sample[key], names), "count": count}
        for key, count in sorted(deck_counts.items(), key=lambda kv: (str(deck_sample[kv[0]].get("type")), kv[0]))
    ]
    by_type = Counter(str(card.get("type") or "?") for card in final_deck)

    info = dict(run_info or {})
    combats = [room for room in rooms_out if room["combat"] is not None]
    plans = [p for room in combats for t in room["combat"]["turns"] for p in t["solver_plans"]]
    outcome = info.get("status") or _outcome(summary_in)
    summary = {
        "run_id": summary_in.get("run_id") or meta.get("run_id"),
        "seed": summary_in.get("seed") or meta.get("seed"),
        "character": summary_in.get("character") or meta.get("character"),
        "ascension": summary_in.get("ascension") if summary_in.get("ascension") is not None
        else meta.get("ascension"),
        "game_version": summary_in.get("game_version") or meta.get("game_version"),
        "experiment": meta.get("experiment"),
        "policy": policy_from_experiment(meta.get("experiment")),
        "harness": meta.get("harness"),
        "batch_id": meta.get("batch_id"),
        "lane": meta.get("lane"),
        "game_index": meta.get("game_index"),
        "games_total": meta.get("games_total"),
        "outcome": outcome,
        "victory": summary_in.get("victory"),
        "complete": bool(summary_in.get("complete_run")),
        "state": info.get("state"),
        "max_global_floor": summary_in.get("max_global_floor"),
        "end_label": summary_in.get("max_floor_label"),
        "start_hp": summary_in.get("start_hp"),
        "end_hp": summary_in.get("end_hp"),
        "total_hp_loss": summary_in.get("total_hp_loss"),
        "room_count": len(rooms_out),
        "combat_count": len(combats),
        "elite_count": sum(1 for r in rooms_out if r["room_type"] == "Elite"),
        "started_at": _epoch(meta.get("ts")) if meta else _epoch(summary_in.get("started_at")),
        "ended_at": _epoch(summary_in.get("ended_at")),
        "deck_size": len(final_deck),
        "relic_count": len(final_relics),
        "solver": {
            "used": solver_used,
            "plans": sum(1 for p in plans if p["ok"]),
            "errors": sum(1 for p in plans if not p["ok"]),
            "replans": sum(room["combat"]["replans"] for room in combats),
            "diverged_turns": sum(room["combat"]["diverged_turns"] for room in combats),
            "heuristic_steps": sum(room["combat"]["heuristic_steps"] for room in combats),
            "search_ms": sum(room["combat"]["search_ms"] for room in combats),
        },
        "final_error": _final_error(records),
    }
    return {
        "view": "game_detail",
        "schema": SCHEMA_VERSION,
        "source_id": info.get("source_id"),
        "source_name": source_name,
        "summary": summary,
        "route": _route(records, rooms_in),
        "deck": {
            "final": deck_groups,
            "size": len(final_deck),
            "by_type": dict(by_type),
            "relics": final_relics,
            "potions": final_potions,
        },
        "rooms": rooms_out,
        "loc": names.table,
    }


def _final_error(records: Sequence[Any]) -> str | None:
    """The engine error the log ends on, if it ends on one (a crashed game)."""
    for row in reversed(records):
        classified = _classify(row)
        if classified is None:
            continue
        if classified[0] == "error":
            return _short_error(classified[1].get("message"))
        return None
    return None


def load_game_records(path: Path) -> list[dict[str, Any]]:
    """Read a game log, skipping blank and malformed lines (a log still being
    written ends on a half line)."""
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                records.append(row)
    return records
