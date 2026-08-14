from __future__ import annotations


def _command(action: str, **args: object) -> dict:
    return {"cmd": "action", "action": action, "args": args}


def run_simulator_player_summary(
    *,
    hp: int,
    gold: int,
    deck: list[tuple[str, str, bool]],
    relics: list[tuple[str, str]],
    potions: list[tuple[int, str, str, str]],
) -> dict:
    """Model the current RunSimulator.PlayerSummary JSON field contract."""
    return {
        "name": "铁甲战士",
        "hp": hp,
        "max_hp": 80,
        "block": 0,
        "gold": gold,
        "relics": [
            {"name": name, "description": description, "vars": None}
            for name, description in relics
        ],
        "potions": [
            {
                "index": index,
                "name": name,
                "description": description,
                "vars": None,
                "target_type": target_type,
            }
            for index, name, description, target_type in potions
        ],
        "deck_size": len(deck),
        "deck": [
            {
                "id": card_id,
                "name": name,
                "cost": 2 if card_id == "CARD.BASH" else 1,
                "type": (
                    "Skill"
                    if card_id in {"CARD.DEFEND", "CARD.SHRUG_IT_OFF"}
                    else "Attack"
                ),
                "upgraded": upgraded,
                "description": f"{name}的具体效果",
                "stats": None,
                "keywords": None,
                "after_upgrade": None,
            }
            for card_id, name, upgraded in deck
        ],
    }


def run_simulator_contract_capture_inputs() -> list[list[tuple[dict, dict]]]:
    """Exact field shapes emitted by the current RunSimulator decision states."""
    starter_deck = [
        ("CARD.STRIKE", "打击", False),
        ("CARD.DEFEND", "防御", False),
        ("CARD.BASH", "重击", False),
    ]
    starter_relics = [("燃烧之血", "战斗结束时回复生命")]
    combat_potions = [
        (0, "火焰药水", "对一个敌人造成 20 点伤害", "AnyEnemy"),
        (1, "格挡药水", "获得 12 点格挡", "None"),
    ]
    event_player = run_simulator_player_summary(
        hp=80,
        gold=99,
        deck=starter_deck,
        relics=starter_relics,
        potions=combat_potions,
    )
    combat_player = run_simulator_player_summary(
        hp=74,
        gold=189,
        deck=starter_deck,
        relics=starter_relics,
        potions=combat_potions,
    )
    reward_player = run_simulator_player_summary(
        hp=68,
        gold=189,
        deck=starter_deck,
        relics=starter_relics,
        potions=[combat_potions[1]],
    )
    shop_player = run_simulator_player_summary(
        hp=68,
        gold=189,
        deck=[*starter_deck, ("CARD.POMMEL_STRIKE", "剑柄打击", False)],
        relics=starter_relics,
        potions=[combat_potions[1]],
    )
    shop_after_card_player = run_simulator_player_summary(
        hp=68,
        gold=139,
        deck=[
            *starter_deck,
            ("CARD.POMMEL_STRIKE", "剑柄打击", False),
            ("CARD.SHRUG_IT_OFF", "耸肩无视", False),
        ],
        relics=starter_relics,
        potions=[combat_potions[1]],
    )
    rest_player = run_simulator_player_summary(
        hp=68,
        gold=19,
        deck=[
            *starter_deck,
            ("CARD.POMMEL_STRIKE", "剑柄打击", False),
            ("CARD.SHRUG_IT_OFF", "耸肩无视", False),
        ],
        relics=[*starter_relics, ("锚", "每场战斗开始时获得 10 点格挡")],
        potions=[combat_potions[1]],
    )
    card_reward_options = [
        {
            "index": 0,
            "id": "CARD.POMMEL_STRIKE",
            "name": "剑柄打击",
            "cost": 1,
            "type": "Attack",
            "rarity": "Common",
            "description": "造成 9 点伤害，抽 1 张牌",
            "stats": None,
            "keywords": None,
            "after_upgrade": None,
        },
        {
            "index": 1,
            "id": "CARD.CLEAVE",
            "name": "顺劈斩",
            "cost": 1,
            "type": "Attack",
            "rarity": "Common",
            "description": "对所有敌人造成 8 点伤害",
            "stats": None,
            "keywords": None,
            "after_upgrade": None,
        },
        {
            "index": 2,
            "id": "CARD.IRON_WAVE",
            "name": "钢铁波浪",
            "cost": 1,
            "type": "Attack",
            "rarity": "Common",
            "description": "获得 5 点格挡并造成 5 点伤害",
            "stats": None,
            "keywords": None,
            "after_upgrade": None,
        },
    ]
    shop_cards = [
        {
            "index": 0,
            "name": "耸肩无视",
            "type": "Skill",
            "rarity": "Common",
            "card_cost": 1,
            "description": "获得 8 点格挡，抽 1 张牌",
            "stats": None,
            "keywords": None,
            "after_upgrade": None,
            "cost": 50,
            "is_stocked": True,
            "on_sale": False,
        },
        {
            "index": 1,
            "name": "顺劈斩",
            "type": "Attack",
            "rarity": "Common",
            "card_cost": 1,
            "description": "对所有敌人造成 8 点伤害",
            "stats": None,
            "keywords": None,
            "after_upgrade": None,
            "cost": 55,
            "is_stocked": True,
            "on_sale": False,
        },
    ]
    shop_relics = [
        {
            "index": 0,
            "name": "锚",
            "description": "每场战斗开始时获得 10 点格挡",
            "cost": 120,
            "is_stocked": True,
        },
        {
            "index": 1,
            "name": "蓝蜡烛",
            "description": "可以打出诅咒牌并失去 1 点生命",
            "cost": 140,
            "is_stocked": True,
        },
    ]
    shop_potions = [
        {
            "index": 0,
            "name": "力量药水",
            "description": "获得 2 点力量",
            "cost": 50,
            "is_stocked": True,
        }
    ]
    rest_cards = [
        {
            "index": 0,
            "id": "CARD.BASH",
            "name": "重击",
            "cost": 2,
            "type": "Attack",
            "rarity": "Basic",
            "upgraded": False,
            "stats": None,
            "description": "造成 8 点伤害，施加 2 层易伤",
            "keywords": None,
            "after_upgrade": None,
        },
        {
            "index": 1,
            "id": "CARD.STRIKE",
            "name": "打击",
            "cost": 1,
            "type": "Attack",
            "rarity": "Basic",
            "upgraded": False,
            "stats": None,
            "description": "造成 6 点伤害",
            "keywords": None,
            "after_upgrade": None,
        },
    ]
    return [
        [
            (
                {
                    "type": "decision",
                    "decision": "event_choice",
                    "context": {"act": 1, "floor": 1, "room_type": "Event"},
                    "event_name": "鲜血祭坛",
                    "description": "祭坛要求鲜血。",
                    "options": [
                        {
                            "index": 0,
                            "title": "献血换取金币",
                            "description": "失去 6 点生命，获得 90 金币",
                            "text_key": "EVENT.BLOOD_FOR_GOLD",
                            "is_locked": False,
                            "vars": {"HpLoss": 6, "Gold": 90},
                        },
                        {
                            "index": 1,
                            "title": "离开",
                            "description": "生命与金币保持不变",
                            "text_key": "EVENT.LEAVE",
                            "is_locked": False,
                            "vars": {"HpLoss": 6, "Gold": 90},
                        },
                    ],
                    "player": event_player,
                },
                _command("choose_option", option_index=0),
            )
        ],
        [
            (
                {
                    "type": "decision",
                    "decision": "card_reward",
                    "context": {"act": 1, "floor": 2, "room_type": "Monster"},
                    "cards": card_reward_options,
                    "can_skip": False,
                    "gold_earned": 0,
                    "player": reward_player,
                },
                _command("select_card_reward", card_index=0),
            ),
            (
                {
                    "type": "decision",
                    "decision": "combat_play",
                    "context": {"act": 1, "floor": 2, "room_type": "Monster"},
                    "player": combat_player,
                },
                _command("use_potion", potion_index=0),
            ),
        ],
        [],
        [
            (
                {
                    "type": "decision",
                    "decision": "shop",
                    "context": {"act": 1, "floor": 4, "room_type": "Shop"},
                    "cards": shop_cards,
                    "relics": shop_relics,
                    "potions": shop_potions,
                    "card_removal_cost": 75,
                    "player": shop_player,
                },
                _command("buy_card", card_index=0),
            ),
            (
                {
                    "type": "decision",
                    "decision": "shop",
                    "context": {"act": 1, "floor": 4, "room_type": "Shop"},
                    "cards": shop_cards,
                    "relics": shop_relics,
                    "potions": shop_potions,
                    "card_removal_cost": 75,
                    "player": shop_after_card_player,
                },
                _command("buy_relic", relic_index=0),
            ),
        ],
        [
            (
                {
                    "type": "decision",
                    "decision": "rest_site",
                    "context": {"act": 1, "floor": 5, "room_type": "RestSite"},
                    "options": [
                        {
                            "index": 0,
                            "option_id": "SMITH",
                            "name": "SmithRestSiteOption",
                            "is_enabled": True,
                        },
                        {
                            "index": 1,
                            "option_id": "HEAL",
                            "name": "HealRestSiteOption",
                            "is_enabled": True,
                        },
                    ],
                    "player": rest_player,
                },
                _command("choose_option", option_index=0),
            ),
            (
                {
                    "type": "decision",
                    "decision": "card_select",
                    "context": {"act": 1, "floor": 5, "room_type": "RestSite"},
                    "cards": rest_cards,
                    "min_select": 1,
                    "max_select": 1,
                    "player": rest_player,
                },
                _command("select_cards", indices="0"),
            ),
        ],
    ]
