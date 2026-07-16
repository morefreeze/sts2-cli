from agent.eval_rl import _VerboseCombatEnv, format_floor_label, format_floor_labels


def test_format_floor_label_uses_act_relative_floor():
    assert format_floor_label(1) == "A1F1"
    assert format_floor_label(7) == "A1F7"
    assert format_floor_label(17) == "A1F17"
    assert format_floor_label(18) == "A2F1"
    assert format_floor_label(22) == "A2F5"
    assert format_floor_label(35) == "A3F1"


def test_format_floor_labels_sorts_numeric_floors_before_formatting():
    assert format_floor_labels([22, 7, 18]) == "[A1F7, A2F1, A2F5]"


def test_verbose_card_reward_log_uses_actual_greedy_command(monkeypatch):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl,
        "greedy_action",
        lambda state: {
            "cmd": "action",
            "action": "select_card_reward",
            "args": {"card_index": 7},
        },
    )
    env = object.__new__(_VerboseCombatEnv)
    env.room_log = []
    state = {
        "decision": "card_reward",
        "floor": 7,
        "player": {"hp": 34, "max_hp": 80},
        "cards": [
            {
                "index": 7,
                "id": "CARD.BASH",
                "cost": 2,
                "type": "Attack",
                "rarity": "Basic",
                "stats": {"damage": 8},
                "description": "Deal damage. Apply Vulnerable.",
            },
            {
                "index": 2,
                "id": "CARD.BLUDGEON",
                "cost": 3,
                "type": "Attack",
                "rarity": "Rare",
                "stats": {"damage": 32},
                "description": "Deal 32 damage.",
            },
        ],
    }

    env._log_room(state, "card_reward")

    assert env.room_log[-1].endswith("→ BASH")
