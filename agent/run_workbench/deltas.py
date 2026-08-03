"""Quality-labelled value changes for native and replay run nodes."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, fields
import json
from typing import Any

from .models import DeltaQuality, RunDelta


def _unknown() -> RunDelta:
    return RunDelta()


@dataclass(frozen=True)
class NodeDeltas:
    """The observable gains and losses associated with one visited node."""

    hp_before: RunDelta = RunDelta()
    hp_after: RunDelta = RunDelta()
    hp_change: RunDelta = RunDelta()
    max_hp_change: RunDelta = RunDelta()
    gold_change: RunDelta = RunDelta()
    damage_taken: RunDelta = RunDelta()
    hp_healed: RunDelta = RunDelta()
    cards_gained: RunDelta = RunDelta()
    cards_removed: RunDelta = RunDelta()
    cards_transformed: RunDelta = RunDelta()
    cards_enchanted: RunDelta = RunDelta()
    cards_upgraded: RunDelta = RunDelta()
    relics_gained: RunDelta = RunDelta()
    potions_gained: RunDelta = RunDelta()
    potions_used: RunDelta = RunDelta()
    potions_discarded: RunDelta = RunDelta()

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {
            field.name: getattr(self, field.name).to_dict()
            for field in fields(self)
        }


def native_node_deltas(
    node: dict[str, Any],
    previous_node: dict[str, Any] | None,
    *,
    player_index: int = 0,
) -> NodeDeltas:
    """Read exact native events and derive changes between adjacent snapshots."""

    stats = _native_player_stats(node, player_index)
    previous_stats = (
        _native_player_stats(previous_node, player_index)
        if previous_node is not None
        else None
    )

    current_hp = _exact_number(stats, "current_hp", "hp")
    previous_hp = (
        _exact_number(previous_stats, "current_hp", "hp")
        if previous_stats is not None
        else _unknown()
    )
    current_max_hp = _exact_number(stats, "max_hp")
    previous_max_hp = (
        _exact_number(previous_stats, "max_hp")
        if previous_stats is not None
        else _unknown()
    )
    current_gold = _exact_number(stats, "current_gold", "gold")
    previous_gold = (
        _exact_number(previous_stats, "current_gold", "gold")
        if previous_stats is not None
        else _unknown()
    )

    return NodeDeltas(
        hp_before=previous_hp,
        hp_after=current_hp,
        hp_change=_subtract(current_hp, previous_hp),
        max_hp_change=_subtract(current_max_hp, previous_max_hp),
        gold_change=_subtract(current_gold, previous_gold),
        damage_taken=_exact_number(stats, "damage_taken"),
        hp_healed=_exact_number(stats, "hp_healed"),
        cards_gained=_native_event_list(node, stats, "cards_gained"),
        cards_removed=_native_event_list(node, stats, "cards_removed"),
        cards_transformed=_native_event_list(node, stats, "cards_transformed"),
        cards_enchanted=_native_event_list(node, stats, "cards_enchanted"),
        cards_upgraded=_native_event_list(
            node, stats, "upgraded_cards", "cards_upgraded"
        ),
        relics_gained=_native_choice_list(
            node, stats, "relic_choices", "relics_gained"
        ),
        potions_gained=_native_choice_list(
            node, stats, "potion_choices", "potions_gained"
        ),
        potions_used=_native_event_list(node, stats, "potions_used"),
        potions_discarded=_native_event_list(node, stats, "potions_discarded"),
    )


def derive_snapshot_deltas(
    snapshot: dict[str, Any],
    previous_snapshot: dict[str, Any] | None,
) -> NodeDeltas:
    """Conservatively derive changes from adjacent player snapshots.

    Replay snapshots do not record event causality, so ambiguous event fields
    remain unknown even when an inventory difference is available.
    """

    current = _player_snapshot(snapshot)
    previous = (
        _player_snapshot(previous_snapshot)
        if previous_snapshot is not None
        else None
    )
    current_hp = _derived_observation(current, "hp", "current_hp")
    previous_hp = (
        _derived_observation(previous, "hp", "current_hp")
        if previous is not None
        else _unknown()
    )
    current_max_hp = _derived_observation(current, "max_hp")
    previous_max_hp = (
        _derived_observation(previous, "max_hp")
        if previous is not None
        else _unknown()
    )
    current_gold = _derived_observation(current, "gold", "current_gold")
    previous_gold = (
        _derived_observation(previous, "gold", "current_gold")
        if previous is not None
        else _unknown()
    )

    cards_gained = _unknown()
    cards_removed = _unknown()
    cards_upgraded = _unknown()
    if previous is not None:
        current_deck = _observed_list(current, "deck")
        previous_deck = _observed_list(previous, "deck")
        if current_deck is not None and previous_deck is not None:
            gained, removed, upgraded = _card_differences(
                current_deck, previous_deck
            )
            cards_gained = _derived(gained)
            cards_removed = _derived(removed)
            cards_upgraded = _derived(upgraded)

    relics_gained = _derived_inventory_gain(
        current,
        previous,
        preferred_key="relic_items",
        fallback_key="relics",
    )
    potions_gained = _derived_inventory_gain(
        current,
        previous,
        preferred_key="potion_items",
        fallback_key="potions",
    )

    return NodeDeltas(
        hp_before=previous_hp,
        hp_after=current_hp,
        hp_change=_subtract(current_hp, previous_hp),
        max_hp_change=_subtract(current_max_hp, previous_max_hp),
        gold_change=_subtract(current_gold, previous_gold),
        cards_gained=cards_gained,
        cards_removed=cards_removed,
        cards_upgraded=cards_upgraded,
        relics_gained=relics_gained,
        potions_gained=potions_gained,
    )


def _native_player_stats(
    node: dict[str, Any] | None, player_index: int
) -> dict[str, Any]:
    if not isinstance(node, dict):
        return {}
    players = node.get("player_stats")
    if not isinstance(players, list) or not 0 <= player_index < len(players):
        return {}
    candidate = players[player_index]
    return candidate if isinstance(candidate, dict) else {}


def _number(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


def _exact_number(record: dict[str, Any], *keys: str) -> RunDelta:
    for key in keys:
        if key in record:
            value = _number(record[key])
            return (
                RunDelta(value=deepcopy(value), quality=DeltaQuality.EXACT)
                if value is not None
                else _unknown()
            )
    return _unknown()


def _derived_observation(record: dict[str, Any], *keys: str) -> RunDelta:
    for key in keys:
        if key in record:
            value = _number(record[key])
            return (
                RunDelta(value=deepcopy(value), quality=DeltaQuality.DERIVED)
                if value is not None
                else _unknown()
            )
    return _unknown()


def _subtract(current: RunDelta, previous: RunDelta) -> RunDelta:
    current_value = _number(current.value)
    previous_value = _number(previous.value)
    if current_value is None or previous_value is None:
        return _unknown()
    return _derived(current_value - previous_value)


def _native_event_list(
    node: dict[str, Any], stats: dict[str, Any], *keys: str
) -> RunDelta:
    value = _first_present(stats, node, keys=keys)
    if value is _MISSING or not isinstance(value, list):
        return _unknown()
    return RunDelta(value=deepcopy(value), quality=DeltaQuality.EXACT)


def _native_choice_list(
    node: dict[str, Any], stats: dict[str, Any], *keys: str
) -> RunDelta:
    value = _first_present(stats, node, keys=keys)
    if value is _MISSING or not isinstance(value, list):
        return _unknown()
    return RunDelta(value=_picked_choices(value), quality=DeltaQuality.EXACT)


def _picked_choices(choices: list[Any]) -> list[Any]:
    selection_keys = (
        "picked",
        "selected",
        "chosen",
        "is_picked",
        "was_picked",
        "was_chosen",
    )
    has_selection_markers = any(
        isinstance(choice, dict) and any(key in choice for key in selection_keys)
        for choice in choices
    )
    if not has_selection_markers:
        return deepcopy(choices)
    return [
        deepcopy(choice)
        for choice in choices
        if isinstance(choice, dict)
        and any(bool(choice.get(key)) for key in selection_keys)
    ]


_MISSING = object()


def _first_present(
    primary: dict[str, Any], secondary: dict[str, Any], *, keys: tuple[str, ...]
) -> Any:
    for record in (primary, secondary):
        for key in keys:
            if key in record:
                return record[key]
    return _MISSING


def _player_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    end_player = snapshot.get("end_player")
    if isinstance(end_player, dict):
        return end_player
    player = snapshot.get("player")
    if isinstance(player, dict):
        return player
    return snapshot


def _observed_list(record: dict[str, Any], key: str) -> list[Any] | None:
    value = record.get(key, _MISSING)
    return value if isinstance(value, list) else None


def _preferred_observed_list(
    record: dict[str, Any], preferred_key: str, fallback_key: str
) -> list[Any] | None:
    preferred = _observed_list(record, preferred_key)
    if preferred is not None:
        return preferred
    return _observed_list(record, fallback_key)


def _derived_inventory_gain(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    preferred_key: str,
    fallback_key: str,
) -> RunDelta:
    if previous is None:
        return _unknown()
    current_items = _preferred_observed_list(current, preferred_key, fallback_key)
    previous_items = _preferred_observed_list(previous, preferred_key, fallback_key)
    if current_items is None or previous_items is None:
        return _unknown()
    gained, _ = _multiset_difference(current_items, previous_items)
    return _derived(gained)


def _item_identity(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("id", "card_id", "relic_id", "potion_id", "model_id", "name"):
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                return f"{key}:{value}"
    return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


def _multiset_difference(
    current: list[Any], previous: list[Any]
) -> tuple[list[Any], list[Any]]:
    remaining_previous: dict[str, list[Any]] = defaultdict(list)
    for item in previous:
        remaining_previous[_item_identity(item)].append(item)

    gained: list[Any] = []
    for item in current:
        identity = _item_identity(item)
        candidates = remaining_previous.get(identity)
        if candidates:
            candidates.pop(0)
        else:
            gained.append(deepcopy(item))

    removed: list[Any] = []
    for items in remaining_previous.values():
        removed.extend(deepcopy(items))
    return gained, removed


def _card_differences(
    current: list[Any], previous: list[Any]
) -> tuple[list[Any], list[Any], list[Any]]:
    previous_by_identity: dict[str, list[Any]] = defaultdict(list)
    current_by_identity: dict[str, list[Any]] = defaultdict(list)
    for card in previous:
        previous_by_identity[_item_identity(card)].append(card)
    for card in current:
        current_by_identity[_item_identity(card)].append(card)

    gained: list[Any] = []
    removed: list[Any] = []
    upgraded: list[Any] = []
    identities = dict.fromkeys(
        [*previous_by_identity.keys(), *current_by_identity.keys()]
    )
    for identity in identities:
        old_cards = previous_by_identity.get(identity, [])
        new_cards = current_by_identity.get(identity, [])
        paired = min(len(old_cards), len(new_cards))
        for old_card, new_card in zip(old_cards[:paired], new_cards[:paired]):
            if (
                isinstance(old_card, dict)
                and isinstance(new_card, dict)
                and not bool(old_card.get("upgraded"))
                and bool(new_card.get("upgraded"))
            ):
                upgraded.append(deepcopy(new_card))
        gained.extend(deepcopy(new_cards[paired:]))
        removed.extend(deepcopy(old_cards[paired:]))
    return gained, removed, upgraded


def _derived(value: Any) -> RunDelta:
    return RunDelta(value=deepcopy(value), quality=DeltaQuality.DERIVED)
