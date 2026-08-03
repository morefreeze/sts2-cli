import math

from agent.run_workbench.deltas import (
    derive_snapshot_deltas,
    native_node_deltas,
)


def _as_dict(delta_bundle):
    return delta_bundle.to_dict()


def test_native_node_deltas_distinguish_exact_snapshots_from_derived_changes() -> None:
    previous = {
        "player_stats": [
            {
                "current_hp": 73,
                "max_hp": 80,
                "current_gold": 25,
                "damage_taken": 7,
                "hp_healed": 0,
                "cards_gained": [],
                "cards_removed": [],
                "cards_transformed": [],
                "cards_enchanted": [],
                "upgraded_cards": [],
                "relic_choices": [],
                "potion_choices": [],
                "potions_used": [],
                "potions_discarded": [],
            }
        ]
    }
    current = {
        "player_stats": [
            {
                "current_hp": 70,
                "max_hp": 82,
                "current_gold": 44,
                "damage_taken": 9,
                "hp_healed": 6,
                "cards_gained": [{"id": "BASH"}],
                "cards_removed": [{"id": "STRIKE"}],
                "cards_transformed": [{"from": "DEFEND", "to": "SHRUG"}],
                "cards_enchanted": [{"id": "BASH", "enchantment": "FIRE"}],
                "upgraded_cards": [{"id": "BASH"}],
                "relic_choices": [
                    {"choice": "RELIC.ANCHOR", "was_picked": True},
                    {"choice": "RELIC.BAG_OF_MARBLES", "was_picked": False},
                ],
                "potion_choices": [
                    {"choice": "POTION.FIRE_POTION", "was_picked": True},
                    {"choice": "POTION.BLOCK_POTION", "was_picked": False},
                ],
                "potions_used": [{"id": "DEXTERITY_POTION"}],
                "potions_discarded": [],
            }
        ]
    }

    result = _as_dict(native_node_deltas(current, previous))

    assert result["hp_before"] == {"value": 73, "quality": "exact"}
    assert result["hp_after"] == {"value": 70, "quality": "exact"}
    assert result["hp_change"] == {"value": -3, "quality": "derived"}
    assert result["max_hp_change"] == {"value": 2, "quality": "derived"}
    assert result["gold_change"] == {"value": 19, "quality": "derived"}
    assert result["damage_taken"] == {"value": 9, "quality": "exact"}
    assert result["hp_healed"] == {"value": 6, "quality": "exact"}
    assert result["cards_gained"] == {
        "value": [{"id": "BASH"}],
        "quality": "exact",
    }
    assert result["cards_removed"]["value"] == [{"id": "STRIKE"}]
    assert result["cards_transformed"]["value"] == [
        {"from": "DEFEND", "to": "SHRUG"}
    ]
    assert result["cards_enchanted"]["value"] == [
        {"id": "BASH", "enchantment": "FIRE"}
    ]
    assert result["cards_upgraded"]["value"] == [{"id": "BASH"}]
    assert result["relics_gained"] == {
        "value": [{"choice": "RELIC.ANCHOR", "was_picked": True}],
        "quality": "exact",
    }
    assert result["potions_gained"] == {
        "value": [{"choice": "POTION.FIRE_POTION", "was_picked": True}],
        "quality": "exact",
    }
    assert result["potions_used"]["value"] == [{"id": "DEXTERITY_POTION"}]
    assert result["potions_discarded"] == {"value": [], "quality": "exact"}


def test_native_node_deltas_do_not_fabricate_first_node_or_absent_lists() -> None:
    first = {
        "player_stats": [
            {
                "current_hp": 80,
                "cards_gained": [],
                "potion_choices": [],
            }
        ]
    }

    result = _as_dict(native_node_deltas(first, None))

    assert result["hp_after"] == {"value": 80, "quality": "exact"}
    assert result["hp_before"] == {"value": None, "quality": "unknown"}
    assert result["hp_change"] == {"value": None, "quality": "unknown"}
    assert result["gold_change"] == {"value": None, "quality": "unknown"}
    assert result["cards_gained"] == {"value": [], "quality": "exact"}
    assert result["cards_removed"] == {"value": None, "quality": "unknown"}
    assert result["potions_gained"] == {"value": None, "quality": "unknown"}
    assert result["potions_used"] == {"value": None, "quality": "unknown"}


def test_native_unmarked_choices_are_unknown_but_explicit_gains_are_exact() -> None:
    ambiguous = {
        "player_stats": [
            {
                "relic_choices": [{"choice": "RELIC.ANCHOR"}],
                "potion_choices": [],
            }
        ]
    }
    explicit = {
        "player_stats": [
            {
                "relic_choices": [{"choice": "RELIC.BAG_OF_MARBLES"}],
                "relics_gained": [],
                "potion_choices": [{"choice": "POTION.BLOCK_POTION"}],
                "potions_gained": [{"id": "POTION.FIRE_POTION"}],
            }
        ]
    }

    ambiguous_result = _as_dict(native_node_deltas(ambiguous, None))
    explicit_result = _as_dict(native_node_deltas(explicit, None))

    assert ambiguous_result["relics_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert ambiguous_result["potions_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert explicit_result["relics_gained"] == {
        "value": [],
        "quality": "exact",
    }
    assert explicit_result["potions_gained"] == {
        "value": [{"id": "POTION.FIRE_POTION"}],
        "quality": "exact",
    }


def test_native_invalid_or_mixed_choice_markers_are_unknown() -> None:
    node = {
        "player_stats": [
            {
                "relic_choices": [
                    {"choice": "RELIC.ANCHOR", "was_picked": True},
                    {"choice": "RELIC.BAG", "was_picked": "false"},
                ],
                "potion_choices": [
                    {"choice": "POTION.FIRE", "was_picked": True},
                    {"choice": "POTION.BLOCK"},
                ],
            }
        ]
    }

    result = _as_dict(native_node_deltas(node, None))

    assert result["relics_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert result["potions_gained"] == {
        "value": None,
        "quality": "unknown",
    }


def test_nonfinite_native_numbers_are_unknown() -> None:
    for invalid in (math.nan, math.inf, -math.inf):
        result = _as_dict(
            native_node_deltas(
                {
                    "player_stats": [
                        {
                            "current_hp": invalid,
                            "damage_taken": invalid,
                        }
                    ]
                },
                None,
            )
        )

        assert result["hp_after"] == {"value": None, "quality": "unknown"}
        assert result["damage_taken"] == {
            "value": None,
            "quality": "unknown",
        }

    overflow = _as_dict(
        derive_snapshot_deltas({"hp": 1e308}, {"hp": -1e308})
    )
    assert overflow["hp_change"] == {"value": None, "quality": "unknown"}


def test_snapshot_deltas_derive_inventory_multiset_differences() -> None:
    previous = {
        "hp": 76,
        "max_hp": 80,
        "gold": 40,
        "deck": [
            {"id": "STRIKE", "upgraded": False},
            {"id": "DEFEND", "upgraded": False},
        ],
        "relic_items": [{"id": "BURNING_BLOOD"}],
        "potion_items": [{"id": "BLOCK_POTION"}],
    }
    current = {
        "hp": 68,
        "max_hp": 85,
        "gold": 12,
        "deck": [
            {"id": "STRIKE", "upgraded": True},
            {"id": "BASH", "upgraded": False},
        ],
        "relic_items": [
            {"id": "BURNING_BLOOD"},
            {"id": "ANCHOR"},
        ],
        "potion_items": [
            {"id": "BLOCK_POTION"},
            {"id": "FIRE_POTION"},
        ],
    }

    result = _as_dict(derive_snapshot_deltas(current, previous))

    assert result["hp_before"] == {"value": 76, "quality": "derived"}
    assert result["hp_after"] == {"value": 68, "quality": "derived"}
    assert result["hp_change"] == {"value": -8, "quality": "derived"}
    assert result["max_hp_change"] == {"value": 5, "quality": "derived"}
    assert result["gold_change"] == {"value": -28, "quality": "derived"}
    assert result["cards_gained"] == {
        "value": [{"id": "BASH", "upgraded": False}],
        "quality": "derived",
    }
    assert result["cards_removed"] == {
        "value": [{"id": "DEFEND", "upgraded": False}],
        "quality": "derived",
    }
    assert result["cards_upgraded"] == {
        "value": [{"id": "STRIKE", "upgraded": True}],
        "quality": "derived",
    }
    assert result["relics_gained"] == {
        "value": [{"id": "ANCHOR"}],
        "quality": "derived",
    }
    assert result["potions_gained"] == {
        "value": [{"id": "FIRE_POTION"}],
        "quality": "derived",
    }
    assert result["cards_transformed"] == {
        "value": None,
        "quality": "unknown",
    }
    assert result["potions_used"] == {"value": None, "quality": "unknown"}


def test_snapshot_deltas_keep_unavailable_inventory_unknown() -> None:
    result = _as_dict(
        derive_snapshot_deltas(
            {"hp": 10, "deck": []},
            {"hp": 12, "deck": []},
        )
    )

    assert result["cards_gained"] == {"value": [], "quality": "derived"}
    assert result["cards_removed"] == {"value": [], "quality": "derived"}
    assert result["relics_gained"] == {"value": None, "quality": "unknown"}
    assert result["potions_gained"] == {"value": None, "quality": "unknown"}
    assert result["max_hp_change"] == {"value": None, "quality": "unknown"}


def test_snapshot_card_differences_are_order_independent_and_conservative() -> None:
    reordered = _as_dict(
        derive_snapshot_deltas(
            {
                "deck": [
                    {"id": "CARD.STRIKE", "upgraded": True},
                    {"id": "CARD.STRIKE", "upgraded": False},
                ]
            },
            {
                "deck": [
                    {"id": "CARD.STRIKE", "upgraded": False},
                    {"id": "CARD.STRIKE", "upgraded": True},
                ]
            },
        )
    )
    transitioned = _as_dict(
        derive_snapshot_deltas(
            {"deck": [{"id": "CARD.STRIKE", "upgraded": True}]},
            {"deck": [{"id": "CARD.STRIKE", "upgraded": False}]},
        )
    )
    gained_and_changed = _as_dict(
        derive_snapshot_deltas(
            {
                "deck": [
                    {"id": "CARD.STRIKE", "upgraded": False},
                    {"id": "CARD.STRIKE", "upgraded": True},
                ]
            },
            {"deck": [{"id": "CARD.STRIKE", "upgraded": False}]},
        )
    )

    assert reordered["cards_gained"]["value"] == []
    assert reordered["cards_removed"]["value"] == []
    assert reordered["cards_upgraded"]["value"] == []
    assert transitioned["cards_upgraded"]["value"] == [
        {"id": "CARD.STRIKE", "upgraded": True}
    ]
    assert gained_and_changed["cards_gained"]["value"] == [
        {"id": "CARD.STRIKE", "upgraded": True}
    ]
    assert gained_and_changed["cards_removed"]["value"] == []
    assert gained_and_changed["cards_upgraded"]["value"] == []


def test_snapshot_cards_match_stable_instances_before_model_counts() -> None:
    result = _as_dict(
        derive_snapshot_deltas(
            {
                "deck": [
                    {
                        "id": "CARD.STRIKE",
                        "instance_id": "copy-2",
                        "upgraded": True,
                    },
                    {
                        "id": "CARD.STRIKE",
                        "instance_id": "copy-1",
                        "upgraded": True,
                    },
                ]
            },
            {
                "deck": [
                    {
                        "id": "CARD.STRIKE",
                        "instance_id": "copy-1",
                        "upgraded": False,
                    },
                    {
                        "id": "CARD.STRIKE",
                        "instance_id": "copy-2",
                        "upgraded": True,
                    },
                ]
            },
        )
    )

    assert result["cards_gained"]["value"] == []
    assert result["cards_removed"]["value"] == []
    assert result["cards_upgraded"]["value"] == [
        {
            "id": "CARD.STRIKE",
            "instance_id": "copy-1",
            "upgraded": True,
        }
    ]


def test_snapshot_identity_aliases_and_localized_names_do_not_fake_changes() -> None:
    result = _as_dict(
        derive_snapshot_deltas(
            {
                "deck": [
                    {
                        "model_id": "CARD.STRIKE",
                        "name": "打击",
                        "upgraded": False,
                    }
                ],
                "relic_items": [{"id": "RELIC.ANCHOR", "name": "锚"}],
                "potion_items": [
                    {"potion_id": "POTION.FIRE", "name": "火焰药水"}
                ],
            },
            {
                "deck": [
                    {
                        "card_id": {"key": "CARD.STRIKE"},
                        "name": "Strike",
                        "upgraded": False,
                    }
                ],
                "relic_items": [
                    {"relic_id": "RELIC.ANCHOR", "name": "Anchor"}
                ],
                "potion_items": [{"id": "POTION.FIRE", "name": "Fire Potion"}],
            },
        )
    )

    assert result["cards_gained"]["value"] == []
    assert result["cards_removed"]["value"] == []
    assert result["relics_gained"]["value"] == []
    assert result["potions_gained"]["value"] == []
