from __future__ import annotations

import json
import math

import pytest

from tests.agent.run_simulator_contract import (
    run_simulator_contract_capture_inputs,
)

from agent.run_decisions import (
    DECISION_KINDS,
    MAX_DECISIONS_BYTES,
    MAX_DECISIONS_PER_NODE,
    MAX_EFFECT_CHARS,
    MAX_ID_CHARS,
    MAX_LABEL_CHARS,
    MAX_OPTIONS_PER_DECISION,
    DecisionEvidenceError,
    append_run_decision,
    capture_run_decision,
    validate_run_decisions,
)


def _command(action: str, **args: object) -> dict:
    return {"cmd": "action", "action": action, "args": args}


def _option(
    option_id: str,
    label: str,
    *,
    selected: bool = False,
    effect: str | None = None,
) -> dict:
    return {
        "id": option_id,
        "label": label,
        "effect": effect,
        "selected": selected,
    }


def _decision(
    *,
    kind: str = "event",
    selected_id: str = "A",
    selected_label: str = "甲",
    options: list[dict] | None = None,
) -> dict:
    return {
        "kind": kind,
        "selected_id": selected_id,
        "selected_label": selected_label,
        "options": options or [_option(selected_id, selected_label, selected=True)],
        "evidence": "recorded",
    }


@pytest.mark.parametrize(
    ("state", "command", "kind", "selected_id", "selected_label", "effect"),
    [
        (
            {
                "decision": "event_choice",
                "options": [
                    {
                        "index": 4,
                        "option_id": "BLOOD",
                        "name": {"en": "Give blood", "zh-CN": "献血"},
                        "description": {
                            "zh": "失去 8 生命；最大生命 +8",
                            "en": "Lose 8 HP; gain 8 max HP",
                        },
                    },
                    {
                        "index": 9,
                        "option_id": "LEAVE",
                        "name": {"zh-CN": "离开"},
                        "description": {"zh-CN": "什么也不做"},
                    },
                ],
            },
            _command("choose_option", option_index=4),
            "event",
            "BLOOD",
            "献血",
            "失去 8 生命；最大生命 +8",
        ),
        (
            {
                "decision": "card_reward",
                "can_skip": True,
                "cards": [
                    {
                        "index": 2,
                        "id": "POMMEL_STRIKE",
                        "name": "Pommel Strike",
                        "description": "造成 9 点伤害。抽 1 张牌。",
                    }
                ],
            },
            _command("select_card_reward", card_index=2),
            "card_reward",
            "POMMEL_STRIKE",
            "Pommel Strike",
            "造成 9 点伤害。抽 1 张牌。",
        ),
        (
            {
                "decision": "rest_site",
                "options": [
                    {
                        "index": 1,
                        "option_id": "HEAL",
                        "name": {"zh-CN": "休息"},
                    },
                    {
                        "index": 7,
                        "option_id": "SMITH",
                        "name": {"zh-CN": "升级"},
                    },
                ],
            },
            _command("choose_option", option_index=7),
            "rest",
            "SMITH",
            "升级",
            None,
        ),
        (
            {
                "decision": "shop",
                "potions": [
                    {"index": 3, "id": "FIRE_POTION", "name": "火焰药水"}
                ],
            },
            _command("buy_potion", potion_index=3),
            "potion",
            "FIRE_POTION",
            "购买火焰药水",
            None,
        ),
        (
            {
                "decision": "combat_play",
                "context": {"room_type": "Monster"},
                "player": {
                    "potions": [
                        {"index": 1, "id": "FIRE_POTION", "name": "火焰药水"}
                    ]
                },
            },
            _command("use_potion", potion_index=1),
            "potion",
            "FIRE_POTION",
            "火焰药水",
            None,
        ),
        (
            {
                "decision": "shop",
                "relics": [{"index": 5, "id": "ANCHOR", "name": "锚"}],
            },
            _command("buy_relic", relic_index=5),
            "relic",
            "ANCHOR",
            "购买锚",
            None,
        ),
        (
            {
                "decision": "shop",
                "cards": [
                    {
                        "index": 8,
                        "id": "WHIRLWIND",
                        "name": "旋风斩",
                        "cost": 75,
                    }
                ],
            },
            _command("buy_card", card_index=8),
            "shop",
            "WHIRLWIND",
            "购买旋风斩 · 75 金币",
            None,
        ),
        (
            {"decision": "shop", "context": {"room_type": "Shop"}},
            _command("remove_card"),
            "shop",
            "REMOVE_CARD",
            "移除卡牌",
            None,
        ),
        (
            {"decision": "event_choice", "context": {"room_type": "Event"}},
            _command("leave_room"),
            "event",
            "LEAVE_ROOM",
            "离开房间",
            None,
        ),
        (
            {"decision": "shop", "context": {"room_type": "Shop"}},
            _command("leave_room"),
            "shop",
            "LEAVE_ROOM",
            "离开房间",
            None,
        ),
    ],
)
def test_capture_run_decision_extracts_supported_actions(
    state: dict,
    command: dict,
    kind: str,
    selected_id: str,
    selected_label: str,
    effect: str | None,
) -> None:
    result = capture_run_decision(state, command)

    assert result is not None
    assert result["kind"] == kind
    assert result["selected_id"] == selected_id
    assert result["selected_label"] == selected_label
    assert result["evidence"] == "recorded"
    assert sum(option["selected"] for option in result["options"]) == 1
    selected = next(option for option in result["options"] if option["selected"])
    assert selected == _option(selected_id, selected_label, selected=True, effect=effect)


def test_event_capture_retains_all_alternatives_including_disabled() -> None:
    state = {
        "decision": "event_choice",
        "options": [
            {"index": 0, "option_id": "TAKE", "label": "拿走"},
            {
                "index": 1,
                "option_id": "LOCKED",
                "label": "尚未解锁",
                "is_locked": True,
            },
            {
                "index": 2,
                "option_id": "DISABLED",
                "label": "不可用",
                "is_enabled": False,
            },
        ],
    }

    result = capture_run_decision(state, _command("choose_option", option_index=0))

    assert result is not None
    assert [option["id"] for option in result["options"]] == [
        "TAKE",
        "LOCKED",
        "DISABLED",
    ]
    assert [option["selected"] for option in result["options"]] == [True, False, False]


def test_capture_matches_current_run_simulator_field_contract() -> None:
    captures = run_simulator_contract_capture_inputs()
    event_state = captures[0][0][0]
    potion_state = captures[1][1][0]
    shop_state = captures[3][0][0]
    rest_state = captures[4][0][0]
    assert set(event_state["options"][0]) == {
        "index", "title", "description", "text_key", "is_locked", "vars",
    }
    assert set(shop_state["cards"][0]) == {
        "index", "name", "type", "rarity", "card_cost", "description",
        "stats", "keywords", "after_upgrade", "cost", "is_stocked",
        "on_sale",
    }
    assert set(shop_state["relics"][0]) == {
        "index", "name", "description", "cost", "is_stocked",
    }
    assert set(potion_state["player"]["potions"][0]) == {
        "index", "name", "description", "vars", "target_type",
    }
    assert set(potion_state["player"]["relics"][0]) == {
        "name", "description", "vars",
    }
    assert set(rest_state["options"][0]) == {
        "index", "option_id", "name", "is_enabled",
    }
    event = capture_run_decision(*captures[0][0])
    card_reward = capture_run_decision(*captures[1][0])
    potion = capture_run_decision(*captures[1][1])
    shop_card = capture_run_decision(*captures[3][0])
    shop_relic = capture_run_decision(*captures[3][1])
    rest_option = capture_run_decision(*captures[4][0])
    rest_card = capture_run_decision(*captures[4][1])

    assert event is not None
    assert event["selected_id"] == "0"
    assert event["selected_label"] == "献血换取金币"
    assert [option["id"] for option in event["options"]] == ["0", "1"]
    assert card_reward is not None and card_reward["selected_id"] == (
        "CARD.POMMEL_STRIKE"
    )
    assert potion is not None
    assert potion["selected_id"] == "0"
    assert potion["selected_label"] == "火焰药水"
    assert potion["options"][0]["effect"] == "对一个敌人造成 20 点伤害"
    assert shop_card is not None
    assert shop_card["selected_id"] == "0"
    assert shop_card["selected_label"] == "购买耸肩无视 · 50 金币"
    assert shop_relic is not None
    assert shop_relic["selected_id"] == "0"
    assert shop_relic["selected_label"] == "购买锚 · 120 金币"
    assert rest_option is not None
    assert rest_option["selected_id"] == "SMITH"
    assert rest_option["selected_label"] == "SmithRestSiteOption"
    assert rest_option["options"][0]["effect"] is None
    assert rest_card is not None and rest_card["selected_id"] == "CARD.BASH"


@pytest.mark.parametrize(
    ("title", "expected_label"),
    [
        ("题" * MAX_LABEL_CHARS, "题" * MAX_LABEL_CHARS),
        ("题" * (MAX_LABEL_CHARS + 1), "0"),
        ("\ud800", "0"),
        (7, "0"),
    ],
    ids=["max-title", "oversized-title", "invalid-unicode", "non-string"],
)
def test_event_title_label_preserves_exact_builtin_and_size_boundaries(
    title: object, expected_label: str
) -> None:
    state = {
        "decision": "event_choice",
        "options": [
            {
                "index": 0,
                "title": title,
                "description": "具体效果",
                "text_key": "EVENT.OPTION",
            }
        ],
    }

    result = capture_run_decision(
        state, _command("choose_option", option_index=0)
    )

    assert result is not None
    assert result["selected_label"] == expected_label


def test_event_title_label_rejects_string_subclasses() -> None:
    class ForgedTitle(str):
        pass

    state = {
        "decision": "event_choice",
        "options": [{"index": 0, "title": ForgedTitle("伪造标题")}],
    }

    result = capture_run_decision(
        state, _command("choose_option", option_index=0)
    )

    assert result is not None
    assert result["selected_label"] == "0"


def test_skip_card_reward_adds_and_selects_explicit_skip_option() -> None:
    state = {
        "decision": "card_reward",
        "cards": [{"index": 0, "id": "ANGER", "name": "愤怒"}],
        "can_skip": True,
    }

    result = capture_run_decision(state, _command("skip_card_reward"))

    assert result is not None
    assert result["selected_id"] == "SKIP"
    assert result["selected_label"] == "跳过"
    assert result["options"][-1] == _option("SKIP", "跳过", selected=True)
    assert result["options"][0]["selected"] is False


@pytest.mark.parametrize("action", ["play_card", "end_turn", "select_map_node", "dance"])
def test_unsupported_combat_and_map_actions_are_not_captured(action: str) -> None:
    state = {"decision": "combat_play", "context": {"room_type": "Monster"}}
    assert capture_run_decision(state, _command(action)) is None


@pytest.mark.parametrize(
    ("room_type", "expected_kind"),
    [("RestSiteRoom", "rest"), ("ShopRoom", "shop")],
)
def test_card_select_requires_one_valid_index_and_noncombat_room(
    room_type: str, expected_kind: str
) -> None:
    state = {
        "decision": "card_select",
        "context": {"room_type": room_type},
        "cards": [
            {"index": 2, "id": "STRIKE", "name": "打击"},
            {"index": 6, "id": "BASH", "name": "痛击"},
        ],
    }

    result = capture_run_decision(state, _command("select_cards", indices="6"))

    assert result is not None
    assert result["kind"] == expected_kind
    assert result["selected_id"] == "BASH"
    assert capture_run_decision(state, _command("select_cards", indices="2,6")) is None
    assert capture_run_decision(state, _command("select_cards", indices="bogus")) is None


def test_invented_card_select_action_is_not_captured() -> None:
    state = {
        "decision": "card_select",
        "context": {"room_type": "RestSiteRoom"},
        "cards": [{"index": 0, "id": "BASH", "name": "痛击"}],
    }
    assert capture_run_decision(state, _command("card_select", indices="0")) is None


def test_combat_card_select_is_excluded() -> None:
    state = {
        "decision": "card_select",
        "context": {"room_type": "Boss"},
        "cards": [{"index": 0, "id": "BASH", "name": "痛击"}],
    }
    assert capture_run_decision(state, _command("select_cards", indices="0")) is None


def test_capture_drops_candidates_without_identity_and_missing_selection() -> None:
    state = {
        "decision": "event_choice",
        "options": [
            {"label": "没有身份"},
            {"index": 4, "label": "使用索引身份"},
        ],
    }
    result = capture_run_decision(state, _command("choose_option", option_index=4))
    assert result is not None
    assert result["selected_id"] == "4"
    assert len(result["options"]) == 1
    assert capture_run_decision(state, _command("choose_option", option_index=0)) is None


def test_capture_returns_detached_builtins() -> None:
    card = {"index": 0, "id": "ANGER", "name": {"zh-CN": "愤怒"}}
    state = {"decision": "card_reward", "cards": [card], "can_skip": False}
    command = _command("select_card_reward", card_index=0)

    result = capture_run_decision(state, command)
    assert result is not None
    card["name"]["zh-CN"] = "已修改"
    command["args"]["card_index"] = 99

    assert result["selected_label"] == "愤怒"
    assert type(result) is dict
    assert type(result["options"]) is list
    assert type(result["options"][0]) is dict


def test_public_constants_are_exact() -> None:
    assert DECISION_KINDS == frozenset(
        {"event", "card_reward", "potion", "relic", "shop", "rest"}
    )
    assert MAX_DECISIONS_PER_NODE == 16
    assert MAX_OPTIONS_PER_DECISION == 32
    assert MAX_ID_CHARS == 256
    assert MAX_LABEL_CHARS == 256
    assert MAX_EFFECT_CHARS == 512
    assert MAX_DECISIONS_BYTES == 32 * 1024


@pytest.mark.parametrize(
    "bad_value",
    [
        [
            _decision(
                selected_id="0",
                selected_label="选项",
                options=[
                    _option(str(index), "选项", selected=index == 0)
                    for index in range(33)
                ],
            )
        ],
        [_decision(options=[_option("A", "甲", selected=True, effect="效" * 513)])],
        [_decision(selected_label="\ud800")],
        [_decision(options=[_option("A", "甲", selected=True)])] + [math.nan],
    ],
    ids=["33-options", "513-char-effect", "lone-surrogate", "non-finite"],
)
def test_validation_rejects_boundary_violations(bad_value: object) -> None:
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions(bad_value)


class _DictSubclass(dict):
    pass


class _ListSubclass(list):
    pass


class _StrSubclass(str):
    pass


class _HostileStr(str):
    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("VERY_SECRET_STATE")


@pytest.mark.parametrize(
    ("state", "command"),
    [
        (
            {
                "decision": _HostileStr("event_choice"),
                "options": [{"index": 0, "id": "A", "label": "甲"}],
            },
            _command("choose_option", option_index=0),
        ),
        (
            {
                "decision": _HostileStr("card_reward"),
                "cards": [{"index": 0, "id": "A", "label": "甲"}],
            },
            _command("select_card_reward", card_index=0),
        ),
        (
            {
                "decision": _HostileStr("card_reward"),
                "cards": [{"index": 0, "id": "A", "label": "甲"}],
            },
            _command("skip_card_reward"),
        ),
        (
            {
                "decision": _HostileStr("combat_play"),
                "player": {
                    "potions": [{"index": 0, "id": "P", "label": "药水"}]
                },
            },
            _command("use_potion", potion_index=0),
        ),
        (
            {
                "decision": _HostileStr("shop"),
                "potions": [{"index": 0, "id": "P", "label": "药水"}],
            },
            _command("buy_potion", potion_index=0),
        ),
        (
            {
                "decision": _HostileStr("shop"),
                "relics": [{"index": 0, "id": "R", "label": "遗物"}],
            },
            _command("buy_relic", relic_index=0),
        ),
        (
            {
                "decision": _HostileStr("shop"),
                "cards": [{"index": 0, "id": "C", "label": "卡牌"}],
            },
            _command("buy_card", card_index=0),
        ),
        ({"decision": _HostileStr("shop")}, _command("remove_card")),
        ({"decision": _HostileStr("event_choice")}, _command("leave_room")),
        (
            {
                "decision": _HostileStr("card_select"),
                "context": {"room_type": "RestSiteRoom"},
                "cards": [{"index": 0, "id": "C", "label": "卡牌"}],
            },
            _command("select_cards", indices="0"),
        ),
    ],
)
def test_capture_rejects_hostile_decision_string_without_comparing_it(
    state: dict, command: dict
) -> None:
    assert capture_run_decision(state, command) is None


def test_capture_rejects_hostile_action_string_without_comparing_it() -> None:
    state = {"decision": "event_choice"}
    command = {"cmd": "action", "action": _HostileStr("leave_room"), "args": {}}
    assert capture_run_decision(state, command) is None


@pytest.mark.parametrize(
    ("state", "command"),
    [
        (
            {
                _HostileStr("decision"): "event_choice",
                "options": [{"index": 0, "id": "A", "label": "甲"}],
            },
            _command("choose_option", option_index=0),
        ),
        (
            {
                "decision": "event_choice",
                "options": [
                    {"index": 0, _HostileStr("id"): "A", "label": "甲"}
                ],
            },
            _command("choose_option", option_index=0),
        ),
        (
            {
                "decision": "event_choice",
                "options": [{"index": 0, "id": "A", "label": "甲"}],
            },
            {
                "cmd": "action",
                "action": "choose_option",
                "args": {_HostileStr("option_index"): 0},
            },
        ),
    ],
    ids=["state-key", "candidate-key", "args-key"],
)
def test_capture_rejects_hostile_dict_keys_without_comparing_them(
    state: dict, command: dict
) -> None:
    assert capture_run_decision(state, command) is None


@pytest.mark.parametrize(
    ("state", "command"),
    [
        (
            {
                "decision": "event_choice",
                "options": [{"index": 0, "id": "A", "label": "甲"}],
            },
            {
                "cmd": "map",
                "action": "choose_option",
                "args": {"option_index": 0},
            },
        ),
        (
            {
                "decision": "event_choice",
                "potions": [{"index": 0, "id": "P", "label": "药水"}],
            },
            _command("buy_potion", potion_index=0),
        ),
        (
            {
                "decision": "shop",
                "player": {
                    "potions": [{"index": 0, "id": "P", "label": "药水"}]
                },
            },
            _command("use_potion", potion_index=0),
        ),
    ],
    ids=["non-action-command", "event-buy", "noncombat-use"],
)
def test_capture_rejects_mismatched_command_and_state_pairs(
    state: dict, command: dict
) -> None:
    assert capture_run_decision(state, command) is None


@pytest.mark.parametrize(
    ("state", "command", "expected_kind"),
    [
        (
            {
                "decision": "event_choice",
                "options": [{"index": 0, "id": "A", "label": "甲"}],
            },
            _command("choose_option", option_index=0),
            "event",
        ),
        (
            {
                "decision": "shop",
                "potions": [{"index": 0, "id": "P", "label": "药水"}],
            },
            _command("buy_potion", potion_index=0),
            "potion",
        ),
        (
            {
                "decision": "combat_play",
                "player": {
                    "potions": [{"index": 0, "id": "P", "label": "药水"}]
                },
            },
            _command("use_potion", potion_index=0),
            "potion",
        ),
        (
            {
                "decision": "card_select",
                "context": {"room_type": "RestSiteRoom"},
                "cards": [{"index": 0, "id": "C", "label": "卡牌"}],
            },
            _command("select_cards", indices="0"),
            "rest",
        ),
    ],
    ids=["event-choice", "shop-buy", "combat-use", "rest-card-select"],
)
def test_capture_accepts_valid_command_and_state_pairs(
    state: dict, command: dict, expected_kind: str
) -> None:
    result = capture_run_decision(state, command)
    assert result is not None
    assert result["kind"] == expected_kind


@pytest.mark.parametrize(
    "bad_value",
    [
        _ListSubclass([_decision()]),
        [_DictSubclass(_decision())],
        [_decision(selected_label=_StrSubclass("甲"))],
    ],
    ids=["list-subclass", "dict-subclass", "str-subclass"],
)
def test_validation_rejects_container_and_string_subclasses(bad_value: object) -> None:
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions(bad_value)


@pytest.mark.parametrize(
    "bad_index", [True, 10**10_000], ids=["bool", "10000-digit-int"]
)
def test_capture_rejects_bool_and_huge_integer_indices(bad_index: object) -> None:
    state = {
        "decision": "card_reward",
        "cards": [{"index": bad_index, "id": "ANGER", "name": "愤怒"}],
    }
    assert (
        capture_run_decision(
            state, _command("select_card_reward", card_index=bad_index)
        )
        is None
    )


def test_validation_rejects_excessive_depth_before_schema_access() -> None:
    decision = _decision()
    decision["extra"] = [[[[["too deep"]]]]]
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions([decision])


def test_validation_rejects_more_than_4096_nodes() -> None:
    decision = _decision()
    decision["extra"] = [None] * 4097
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions([decision])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value[0].__setitem__("extra", "no"),
        lambda value: value[0]["options"][0].__setitem__("extra", "no"),
    ],
)
def test_validation_rejects_unknown_fields(mutate) -> None:
    value = [_decision()]
    mutate(value)
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions(value)


def test_validation_rejects_payload_larger_than_32_kib() -> None:
    options = [
        _option(
            str(index),
            "标" * MAX_LABEL_CHARS,
            selected=index == 0,
            effect="效" * MAX_EFFECT_CHARS,
        )
        for index in range(MAX_OPTIONS_PER_DECISION)
    ]
    oversized = [
        _decision(
            selected_id="0",
            selected_label="标" * MAX_LABEL_CHARS,
            options=options,
        )
        for _ in range(MAX_DECISIONS_PER_NODE)
    ]

    with pytest.raises(DecisionEvidenceError, match="payload limit"):
        validate_run_decisions(oversized)


def test_validation_uses_actual_writer_encoding_for_payload_limit() -> None:
    label = "x" * 254
    effect = "y" * 254
    options = [
        _option(str(index), label, selected=index == 0, effect=effect)
        for index in range(3)
    ]
    value = [
        _decision(selected_id="0", selected_label=label, options=options)
        for _ in range(MAX_DECISIONS_PER_NODE)
    ]
    compact = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    writer = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(compact) == 32305
    assert len(writer) == 32832

    with pytest.raises(DecisionEvidenceError, match="payload limit"):
        validate_run_decisions(value)


@pytest.mark.parametrize(
    "value",
    [
        _decision(
            selected_id="i" * (MAX_ID_CHARS + 1),
            options=[
                _option("i" * (MAX_ID_CHARS + 1), "label", selected=True)
            ],
        ),
        _decision(
            selected_label="l" * (MAX_LABEL_CHARS + 1),
            options=[
                _option("id", "l" * (MAX_LABEL_CHARS + 1), selected=True)
            ],
        ),
    ],
    ids=["257-char-id", "257-char-label"],
)
def test_validation_rejects_257_character_ids_and_labels(value: dict) -> None:
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions([value])


def test_validation_accepts_exactly_256_character_ids_and_labels() -> None:
    option_id = "i" * MAX_ID_CHARS
    label = "l" * MAX_LABEL_CHARS
    result = validate_run_decisions(
        [
            _decision(
                selected_id=option_id,
                selected_label=label,
                options=[_option(option_id, label, selected=True)],
            )
        ]
    )
    assert result[0]["selected_id"] == option_id
    assert result[0]["selected_label"] == label


def test_append_accepts_16_then_rejects_17_and_detaches_inputs() -> None:
    existing: list[dict] = []
    source_decisions: list[dict] = []
    for index in range(MAX_DECISIONS_PER_NODE):
        source = _decision(
            selected_id=str(index),
            selected_label=f"选项 {index}",
        )
        source_decisions.append(source)
        existing = append_run_decision(existing, source)

    source_decisions[0]["selected_label"] = "篡改"
    source_decisions[0]["options"][0]["label"] = "篡改"
    assert existing[0]["selected_label"] == "选项 0"
    assert existing[0]["options"][0]["label"] == "选项 0"
    assert len(existing) == MAX_DECISIONS_PER_NODE

    with pytest.raises(DecisionEvidenceError, match="decision limit"):
        append_run_decision(existing, _decision(selected_id="17", selected_label="17"))


def test_validation_detaches_and_is_strict_utf8_json_safe() -> None:
    original = [_decision()]
    result = validate_run_decisions(original)

    original[0]["selected_label"] = "已修改"
    original[0]["options"][0]["label"] = "已修改"
    assert result[0]["selected_label"] == "甲"
    assert result[0]["options"][0]["label"] == "甲"
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert payload


def test_validation_rejects_inconsistent_or_multiple_selection() -> None:
    multiple = _decision(
        options=[
            _option("A", "甲", selected=True),
            _option("B", "乙", selected=True),
        ]
    )
    inconsistent = _decision()
    inconsistent["selected_id"] = "B"

    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions([multiple])
    with pytest.raises(DecisionEvidenceError):
        validate_run_decisions([inconsistent])


def test_errors_are_bounded_and_do_not_echo_hostile_values() -> None:
    secret = "VERY_SECRET_" * 100
    with pytest.raises(DecisionEvidenceError) as caught:
        validate_run_decisions([{secret: secret}])
    assert len(str(caught.value)) <= 160
    assert "VERY_SECRET" not in str(caught.value)
