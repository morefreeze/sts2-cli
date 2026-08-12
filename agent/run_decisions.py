"""Bounded evidence for confirmed non-combat run decisions."""

from __future__ import annotations

import json
import math


DECISION_KINDS = frozenset(
    {"event", "card_reward", "potion", "relic", "shop", "rest"}
)
MAX_DECISIONS_PER_NODE = 16
MAX_OPTIONS_PER_DECISION = 32
MAX_ID_CHARS = 256
MAX_LABEL_CHARS = 256
MAX_EFFECT_CHARS = 512
MAX_DECISIONS_BYTES = 32 * 1024

_MAX_STRUCTURE_DEPTH = 4
_MAX_STRUCTURE_NODES = 4096
_MAX_ERROR_CHARS = 160
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_DECISION_FIELDS = frozenset(
    {"kind", "selected_id", "selected_label", "options", "evidence"}
)
_OPTION_FIELDS = frozenset({"id", "label", "effect", "selected"})
_LOCALIZED_KEYS = ("zh-CN", "zh", "en")


class DecisionEvidenceError(ValueError):
    """Raised when persisted decision evidence violates its strict contract."""


def _error(message: str) -> DecisionEvidenceError:
    return DecisionEvidenceError(message[:_MAX_ERROR_CHARS])


def _unicode_text(value: object, maximum: int) -> str | None:
    if type(value) is not str or not value or len(value) > maximum:
        return None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    return value


def _localized_text(value: object, maximum: int) -> str | None:
    direct = _unicode_text(value, maximum)
    if direct is not None:
        return direct
    if type(value) is not dict:
        return None
    for locale in _LOCALIZED_KEYS:
        localized = _unicode_text(value.get(locale), maximum)
        if localized is not None:
            return localized
    return None


def _safe_int(value: object) -> int | None:
    if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
        return None
    return value


def _candidate_index(candidate: dict) -> int | None:
    return _safe_int(candidate.get("index"))


def _candidate_id(candidate: dict) -> str | None:
    for key in ("id", "option_id"):
        text = _unicode_text(candidate.get(key), MAX_ID_CHARS)
        if text is not None:
            return text
    index = _candidate_index(candidate)
    return str(index) if index is not None else None


def _candidate_label(candidate: dict, option_id: str) -> str | None:
    label = _unicode_text(candidate.get("label"), MAX_LABEL_CHARS)
    if label is not None:
        return label
    label = _localized_text(candidate.get("name"), MAX_LABEL_CHARS)
    if label is not None:
        return label
    for key in ("option_id", "id"):
        label = _unicode_text(candidate.get(key), MAX_LABEL_CHARS)
        if label is not None:
            return label
    return _unicode_text(option_id, MAX_LABEL_CHARS)


def _candidate_effect(candidate: dict) -> str | None:
    for key in ("description", "effect", "text"):
        effect = _localized_text(candidate.get(key), MAX_EFFECT_CHARS)
        if effect is not None:
            return effect
    return None


def _candidate_option(candidate: object, selected_index: int) -> dict | None:
    if type(candidate) is not dict:
        return None
    option_id = _candidate_id(candidate)
    if option_id is None:
        return None
    label = _candidate_label(candidate, option_id)
    if label is None:
        return None
    index = _candidate_index(candidate)
    return {
        "id": option_id,
        "label": label,
        "effect": _candidate_effect(candidate),
        "selected": index is not None and index == selected_index,
    }


def _command_arg(command: dict, key: str) -> object:
    args = command.get("args")
    if type(args) is dict and key in args:
        return args.get(key)
    return command.get(key)


def _source_options(
    candidates: object,
    selected_index: object,
) -> list[dict] | None:
    index = _safe_int(selected_index)
    if index is None or type(candidates) is not list:
        return None
    if len(candidates) > _MAX_STRUCTURE_NODES:
        return None
    options: list[dict] = []
    for candidate in candidates:
        option = _candidate_option(candidate, index)
        if option is None:
            continue
        options.append(option)
        if len(options) > MAX_OPTIONS_PER_DECISION:
            return None
    if sum(option["selected"] for option in options) != 1:
        return None
    return options


def _decision(kind: str, options: list[dict]) -> dict | None:
    if kind not in DECISION_KINDS or not options:
        return None
    selected = [option for option in options if option["selected"]]
    if len(selected) != 1:
        return None
    decision = {
        "kind": kind,
        "selected_id": selected[0]["id"],
        "selected_label": selected[0]["label"],
        "options": options,
        "evidence": "recorded",
    }
    try:
        return validate_run_decisions([decision])[0]
    except DecisionEvidenceError:
        return None


def _capture_candidates(
    kind: str,
    candidates: object,
    selected_index: object,
    *,
    purchase: bool = False,
) -> dict | None:
    options = _source_options(candidates, selected_index)
    if options is None:
        return None
    if purchase:
        raw_candidates = candidates
        if type(raw_candidates) is not list:
            return None
        selected_candidate = next(
            (
                candidate
                for candidate in raw_candidates
                if type(candidate) is dict
                and _candidate_index(candidate) == _safe_int(selected_index)
            ),
            None,
        )
        selected_option = next(option for option in options if option["selected"])
        label = f"购买{selected_option['label']}"
        if type(selected_candidate) is dict:
            cost = _safe_int(selected_candidate.get("cost"))
            if cost is not None:
                label = f"{label} · {cost} 金币"
        selected_label = _unicode_text(label, MAX_LABEL_CHARS)
        if selected_label is None:
            return None
        selected_option["label"] = selected_label
    return _decision(kind, options)


def _room_kind(state: dict) -> str | None:
    context = state.get("context")
    room_type = context.get("room_type") if type(context) is dict else None
    if type(room_type) is not str or len(room_type) > 64:
        return None
    normalized = "".join(
        character for character in room_type.casefold() if character.isalnum()
    )
    if normalized in {"rest", "restroom", "restsite", "restsiteroom"}:
        return "rest"
    if normalized in {"shop", "shoproom"}:
        return "shop"
    return None


def _selected_card_index(value: object) -> int | None:
    direct = _safe_int(value)
    if direct is not None:
        return direct
    if type(value) is not str or not value or len(value) > 64:
        return None
    pieces = value.split(",")
    if len(pieces) != 1 or not pieces[0].isascii() or not pieces[0].isdigit():
        return None
    try:
        parsed = int(pieces[0])
    except ValueError:
        return None
    return _safe_int(parsed)


def _synthetic(kind: str, option_id: str, label: str) -> dict | None:
    return _decision(
        kind,
        [
            {
                "id": option_id,
                "label": label,
                "effect": None,
                "selected": True,
            }
        ],
    )


def capture_run_decision(state: dict, command: dict) -> dict | None:
    """Extract one confirmed, non-combat decision from a state/action pair."""

    if type(state) is not dict or type(command) is not dict:
        return None
    action = command.get("action")
    if type(action) is not str:
        return None
    state_decision = state.get("decision")
    if type(state_decision) is not str:
        return None

    if action == "select_card_reward":
        if state_decision != "card_reward":
            return None
        options = _source_options(
            state.get("cards"), _command_arg(command, "card_index")
        )
        if options is None:
            return None
        if state.get("can_skip") is True or state.get("skippable") is True:
            if len(options) >= MAX_OPTIONS_PER_DECISION:
                return None
            options.append(_option_value("SKIP", "跳过", selected=False))
        return _decision("card_reward", options)

    if action == "skip_card_reward":
        if (
            state_decision != "card_reward"
            or type(state.get("cards")) is not list
        ):
            return None
        if len(state["cards"]) > _MAX_STRUCTURE_NODES:
            return None
        options: list[dict] = []
        for candidate in state["cards"]:
            if type(candidate) is not dict:
                continue
            option_id = _candidate_id(candidate)
            if option_id is None:
                continue
            label = _candidate_label(candidate, option_id)
            if label is None:
                continue
            options.append(
                {
                    "id": option_id,
                    "label": label,
                    "effect": _candidate_effect(candidate),
                    "selected": False,
                }
            )
            if len(options) >= MAX_OPTIONS_PER_DECISION:
                return None
        options.append(_option_value("SKIP", "跳过", selected=True))
        return _decision("card_reward", options)

    if action == "choose_option":
        kind = (
            "rest"
            if state_decision == "rest_site"
            else "event"
            if state_decision == "event_choice"
            else None
        )
        if kind is None:
            return None
        return _capture_candidates(
            kind, state.get("options"), _command_arg(command, "option_index")
        )

    if action == "use_potion":
        player = state.get("player")
        candidates = player.get("potions") if type(player) is dict else None
        return _capture_candidates(
            "potion", candidates, _command_arg(command, "potion_index")
        )

    purchase_actions = {
        "buy_potion": ("potion", "potions", "potion_index"),
        "buy_relic": ("relic", "relics", "relic_index"),
        "buy_card": ("shop", "cards", "card_index"),
    }
    purchase = purchase_actions.get(action)
    if purchase is not None:
        kind, source_key, index_key = purchase
        return _capture_candidates(
            kind,
            state.get(source_key),
            _command_arg(command, index_key),
            purchase=True,
        )

    if action == "remove_card":
        if state_decision != "shop":
            return None
        return _synthetic("shop", "REMOVE_CARD", "移除卡牌")

    if action == "leave_room":
        if state_decision == "event_choice":
            return _synthetic("event", "LEAVE_ROOM", "离开房间")
        if state_decision == "shop":
            return _synthetic("shop", "LEAVE_ROOM", "离开房间")
        return None

    if action == "select_cards":
        if state_decision != "card_select":
            return None
        kind = _room_kind(state)
        if kind is None:
            return None
        selected_index = _selected_card_index(_command_arg(command, "indices"))
        if selected_index is None:
            return None
        return _capture_candidates(kind, state.get("cards"), selected_index)

    return None


def _option_value(option_id: str, label: str, *, selected: bool) -> dict:
    return {
        "id": option_id,
        "label": label,
        "effect": None,
        "selected": selected,
    }


def _scan_json_value(value: object, *, depth: int, count: list[int]) -> None:
    count[0] += 1
    if depth > _MAX_STRUCTURE_DEPTH:
        raise _error("decision evidence exceeds depth limit")
    if count[0] > _MAX_STRUCTURE_NODES:
        raise _error("decision evidence exceeds node limit")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise _error("decision evidence contains an invalid integer")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _error("decision evidence contains a non-finite number")
        return
    if type(value) is str:
        if len(value) > MAX_EFFECT_CHARS:
            raise _error("decision evidence contains an oversized string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _error("decision evidence contains invalid Unicode") from None
        return
    if type(value) is list:
        for item in value:
            _scan_json_value(item, depth=depth + 1, count=count)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _error("decision evidence contains an invalid object key")
            if len(key) > MAX_EFFECT_CHARS:
                raise _error("decision evidence contains an invalid object key")
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise _error("decision evidence contains an invalid object key") from None
            _scan_json_value(item, depth=depth + 1, count=count)
        return
    raise _error("decision evidence contains a non-JSON value")


def _validated_text(value: object, maximum: int, label: str) -> str:
    text = _unicode_text(value, maximum)
    if text is None:
        raise _error(f"decision {label} must be bounded Unicode text")
    return text


def _validate_option(value: object) -> dict:
    if type(value) is not dict or set(value) != _OPTION_FIELDS:
        raise _error("decision option has invalid fields")
    option_id = _validated_text(value.get("id"), MAX_ID_CHARS, "option id")
    label = _validated_text(value.get("label"), MAX_LABEL_CHARS, "option label")
    effect_value = value.get("effect")
    if effect_value is None:
        effect = None
    else:
        effect = _validated_text(effect_value, MAX_EFFECT_CHARS, "option effect")
    selected = value.get("selected")
    if type(selected) is not bool:
        raise _error("decision option selected marker must be a boolean")
    return {
        "id": option_id,
        "label": label,
        "effect": effect,
        "selected": selected,
    }


def _validate_decision(value: object) -> dict:
    if type(value) is not dict or set(value) != _DECISION_FIELDS:
        raise _error("decision evidence has invalid fields")
    kind = value.get("kind")
    if type(kind) is not str or kind not in DECISION_KINDS:
        raise _error("decision evidence has invalid kind")
    selected_id = _validated_text(
        value.get("selected_id"), MAX_ID_CHARS, "selected id"
    )
    selected_label = _validated_text(
        value.get("selected_label"), MAX_LABEL_CHARS, "selected label"
    )
    raw_options = value.get("options")
    if (
        type(raw_options) is not list
        or not 1 <= len(raw_options) <= MAX_OPTIONS_PER_DECISION
    ):
        raise _error("decision evidence exceeds option limit")
    options = [_validate_option(option) for option in raw_options]
    selected = [option for option in options if option["selected"]]
    if len(selected) != 1:
        raise _error("decision evidence must select exactly one option")
    if selected[0]["id"] != selected_id or selected[0]["label"] != selected_label:
        raise _error("decision evidence selected option is inconsistent")
    if type(value.get("evidence")) is not str or value.get("evidence") != "recorded":
        raise _error("decision evidence marker must be recorded")
    return {
        "kind": kind,
        "selected_id": selected_id,
        "selected_label": selected_label,
        "options": options,
        "evidence": "recorded",
    }


def validate_run_decisions(value: object) -> list[dict]:
    """Validate and detach a persisted list of decision evidence."""

    if type(value) is not list:
        raise _error("run decisions must be an ordinary list")
    if len(value) > MAX_DECISIONS_PER_NODE:
        raise _error("run decision limit exceeded")
    _scan_json_value(value, depth=0, count=[0])
    decisions = [_validate_decision(decision) for decision in value]
    try:
        encoded = json.dumps(
            decisions,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise _error("run decisions are not strict JSON") from None
    if len(encoded) > MAX_DECISIONS_BYTES:
        raise _error("run decisions exceed payload limit")
    return decisions


def append_run_decision(existing: object, decision: object) -> list[dict]:
    """Append one validated decision while enforcing node-level bounds."""

    retained = validate_run_decisions(existing)
    addition = validate_run_decisions([decision])[0]
    if len(retained) >= MAX_DECISIONS_PER_NODE:
        raise _error("run decision limit exceeded")
    return validate_run_decisions([*retained, addition])


__all__ = [
    "DECISION_KINDS",
    "MAX_DECISIONS_PER_NODE",
    "MAX_OPTIONS_PER_DECISION",
    "MAX_ID_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_EFFECT_CHARS",
    "MAX_DECISIONS_BYTES",
    "DecisionEvidenceError",
    "capture_run_decision",
    "validate_run_decisions",
    "append_run_decision",
]
