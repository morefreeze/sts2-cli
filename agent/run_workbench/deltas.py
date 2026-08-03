"""Quality-labelled value changes for native and replay run nodes."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, fields
import math
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
        relics_gained=_native_gain_list(
            node,
            stats,
            explicit_key="relics_gained",
            choice_key="relic_choices",
        ),
        potions_gained=_native_gain_list(
            node,
            stats,
            explicit_key="potions_gained",
            choice_key="potion_choices",
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
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
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
    difference = _number(current_value - previous_value)
    return _derived(difference) if difference is not None else _unknown()


def _native_event_list(
    node: dict[str, Any], stats: dict[str, Any], *keys: str
) -> RunDelta:
    value = _first_present(stats, node, keys=keys)
    if value is _MISSING or not isinstance(value, list):
        return _unknown()
    return RunDelta(value=deepcopy(value), quality=DeltaQuality.EXACT)


def _native_gain_list(
    node: dict[str, Any],
    stats: dict[str, Any],
    *,
    explicit_key: str,
    choice_key: str,
) -> RunDelta:
    explicit = _first_present(stats, node, keys=(explicit_key,))
    if explicit is not _MISSING:
        if not isinstance(explicit, list):
            return _unknown()
        return RunDelta(value=deepcopy(explicit), quality=DeltaQuality.EXACT)

    choices = _first_present(stats, node, keys=(choice_key,))
    if choices is _MISSING or not isinstance(choices, list):
        return _unknown()
    picked = _picked_choices(choices)
    if picked is None:
        return _unknown()
    return RunDelta(value=picked, quality=DeltaQuality.EXACT)


def _picked_choices(choices: list[Any]) -> list[Any] | None:
    selection_keys = (
        "picked",
        "selected",
        "chosen",
        "is_picked",
        "was_picked",
        "was_chosen",
    )
    selections: list[bool] = []
    for choice in choices:
        if not isinstance(choice, dict):
            return None
        markers = [choice[key] for key in selection_keys if key in choice]
        if not markers or any(type(marker) is not bool for marker in markers):
            return None
        if any(marker != markers[0] for marker in markers[1:]):
            return None
        selections.append(markers[0])
    if not selections:
        return None
    return [
        deepcopy(choice)
        for choice, selected in zip(choices, selections)
        if selected
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
        for key in ("id", "card_id", "relic_id", "potion_id", "model_id"):
            if key not in item:
                continue
            identifier = _canonical_identifier(item[key])
            if identifier is not None:
                return f"model:{identifier}"
        # Display names are deliberately excluded: localization changes must
        # not appear as inventory churn when no model identifier is available.
        return "anonymous:dict"
    identifier = _canonical_identifier(item)
    if identifier is not None:
        return f"scalar:{identifier}"
    return f"anonymous:{type(item).__name__}"


def _canonical_identifier(value: Any, *, depth: int = 0) -> str | None:
    if depth > 4:
        return None
    if isinstance(value, str):
        normalized = value.strip()
        return f"str:{normalized}" if normalized else None
    if isinstance(value, int) and not isinstance(value, bool):
        return f"int:{value}"
    if isinstance(value, dict):
        for key in (
            "id",
            "card_id",
            "relic_id",
            "potion_id",
            "model_id",
            "key",
            "value",
            "text_key",
            "TextKey",
        ):
            if key not in value:
                continue
            identifier = _canonical_identifier(value[key], depth=depth + 1)
            if identifier is not None:
                return identifier
    return None


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
    old_remaining = list(previous)
    new_remaining = list(current)
    old_matched: set[int] = set()
    new_matched: set[int] = set()
    upgraded: list[Any] = []

    old_instances = _unique_instance_indices(old_remaining)
    new_instances = _unique_instance_indices(new_remaining)
    for instance_id in old_instances:
        if instance_id not in new_instances:
            continue
        old_index = old_instances[instance_id]
        new_index = new_instances[instance_id]
        old_matched.add(old_index)
        new_matched.add(new_index)
        if _is_explicit_upgrade(old_remaining[old_index], new_remaining[new_index]):
            upgraded.append(deepcopy(new_remaining[new_index]))

    old_by_model: dict[str, list[Any]] = defaultdict(list)
    new_by_model: dict[str, list[Any]] = defaultdict(list)
    for index, card in enumerate(old_remaining):
        if index not in old_matched:
            old_by_model[_item_identity(card)].append(card)
    for index, card in enumerate(new_remaining):
        if index not in new_matched:
            new_by_model[_item_identity(card)].append(card)

    gained: list[Any] = []
    removed: list[Any] = []
    model_ids = dict.fromkeys([*old_by_model.keys(), *new_by_model.keys()])
    for model_id in model_ids:
        old_cards = old_by_model.get(model_id, [])
        new_cards = new_by_model.get(model_id, [])
        old_total = len(old_cards)
        new_total = len(new_cards)
        if old_total == new_total:
            upgraded.extend(_conservative_model_upgrades(old_cards, new_cards))
            continue
        if new_total > old_total:
            candidates = _state_surplus(new_cards, old_cards)
            gained.extend(deepcopy(candidates[: new_total - old_total]))
        else:
            candidates = _state_surplus(old_cards, new_cards)
            removed.extend(deepcopy(candidates[: old_total - new_total]))
    return gained, removed, upgraded


def _unique_instance_indices(cards: list[Any]) -> dict[str, int]:
    indices: dict[str, list[int]] = defaultdict(list)
    for index, card in enumerate(cards):
        instance_id = _card_instance_identity(card)
        if instance_id is not None:
            indices[instance_id].append(index)
    return {
        instance_id: positions[0]
        for instance_id, positions in indices.items()
        if len(positions) == 1
    }


def _card_instance_identity(card: Any) -> str | None:
    if not isinstance(card, dict):
        return None
    for key in ("instance_id", "card_instance_id", "uuid", "unique_id", "entity_id"):
        if key not in card:
            continue
        identifier = _canonical_identifier(card[key])
        if identifier is not None:
            return f"instance:{identifier}"
    return None


def _card_upgrade_state(card: Any) -> bool | None:
    if not isinstance(card, dict):
        return None
    if "upgraded" in card:
        return card["upgraded"] if type(card["upgraded"]) is bool else None
    level = card.get("current_upgrade_level")
    if isinstance(level, int) and not isinstance(level, bool):
        return level > 0
    if isinstance(level, float) and math.isfinite(level):
        return level > 0
    return None


def _is_explicit_upgrade(previous: Any, current: Any) -> bool:
    return (
        _card_upgrade_state(previous) is False
        and _card_upgrade_state(current) is True
    )


def _conservative_model_upgrades(
    previous: list[Any], current: list[Any]
) -> list[Any]:
    old_states = [_card_upgrade_state(card) for card in previous]
    new_states = [_card_upgrade_state(card) for card in current]
    if any(state is None for state in [*old_states, *new_states]):
        return []
    old_false = old_states.count(False)
    old_true = old_states.count(True)
    new_false = new_states.count(False)
    new_true = new_states.count(True)
    transition_count = old_false - new_false
    if transition_count <= 0 or new_true - old_true != transition_count:
        return []
    unmatched_true = max(0, new_true - old_true)
    candidates = [
        deepcopy(card)
        for card in current
        if _card_upgrade_state(card) is True
    ]
    return candidates[:unmatched_true]


def _state_surplus(primary: list[Any], comparison: list[Any]) -> list[Any]:
    available: dict[bool | None, int] = defaultdict(int)
    for card in comparison:
        available[_card_upgrade_state(card)] += 1
    surplus: list[Any] = []
    for card in primary:
        state = _card_upgrade_state(card)
        if available[state] > 0:
            available[state] -= 1
        else:
            surplus.append(card)
    return surplus


def _derived(value: Any) -> RunDelta:
    return RunDelta(value=deepcopy(value), quality=DeltaQuality.DERIVED)
