"""
Unit tests for the pure card/potion index-resolution helpers used to drive
plan_combat_turn (see python/play_full_run.py's _resolve_* docstrings for the
protocol context). These are plain functions over dicts -- no Game fixture,
no game DLLs needed.

Same sys.path pattern tests/test_advisor_ratings.py uses to import
play_full_run.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run


# ---------------------------------------------------------------------------
# _norm_entity_id
# ---------------------------------------------------------------------------

def test_norm_entity_id_strips_card_prefix():
    assert play_full_run._norm_entity_id("CARD.STRIKE") == "STRIKE"


def test_norm_entity_id_strips_potion_prefix():
    assert play_full_run._norm_entity_id("POTION.FIRE_POTION") == "FIRE_POTION"


def test_norm_entity_id_bare_uppercase_unchanged():
    assert play_full_run._norm_entity_id("STRIKE") == "STRIKE"


def test_norm_entity_id_lowercases_input_gets_uppercased():
    assert play_full_run._norm_entity_id("card.Strike") == "STRIKE"


def test_norm_entity_id_handles_bilingual_dict():
    eid = {"en": "CARD.Strike", "zh": "打击"}
    assert play_full_run._norm_entity_id(eid) == "STRIKE"


# ---------------------------------------------------------------------------
# _resolve_card_index
# ---------------------------------------------------------------------------

def test_resolve_card_index_normal_single_match():
    hand = [
        {"id": "CARD.STRIKE", "index": 0},
        {"id": "CARD.DEFEND", "index": 1},
    ]
    assert play_full_run._resolve_card_index(hand, "STRIKE", 0) == 0
    assert play_full_run._resolve_card_index(hand, "DEFEND", 0) == 1


def test_resolve_card_index_disambiguates_duplicates_by_occurrence():
    hand = [
        {"id": "CARD.STRIKE", "index": 0},
        {"id": "CARD.DEFEND", "index": 1},
        {"id": "CARD.STRIKE", "index": 2},
    ]
    assert play_full_run._resolve_card_index(hand, "STRIKE", 0) == 0
    assert play_full_run._resolve_card_index(hand, "STRIKE", 1) == 2


def test_resolve_card_index_no_match_returns_none():
    hand = [{"id": "CARD.DEFEND", "index": 0}]
    assert play_full_run._resolve_card_index(hand, "STRIKE", 0) is None


def test_resolve_card_index_occurrence_beyond_available_returns_none():
    hand = [{"id": "CARD.STRIKE", "index": 0}]
    assert play_full_run._resolve_card_index(hand, "STRIKE", 1) is None


# ---------------------------------------------------------------------------
# _resolve_potion_index
# ---------------------------------------------------------------------------

def test_resolve_potion_index_normal_match():
    potions = [
        {"id": "POTION.FIRE_POTION", "index": 0},
        {"id": "POTION.BLOCK_POTION", "index": 1},
    ]
    assert play_full_run._resolve_potion_index(potions, "FIRE_POTION") == 0
    assert play_full_run._resolve_potion_index(potions, "BLOCK_POTION") == 1


def test_resolve_potion_index_no_match_returns_none():
    potions = [{"id": "POTION.FIRE_POTION", "index": 0}]
    assert play_full_run._resolve_potion_index(potions, "BLOCK_POTION") is None


def test_resolve_potion_index_first_match_wins_on_duplicates():
    # Potions have no upgrade/enchantment state and no occurrence field to
    # disambiguate duplicates by (unlike cards) -- first match is the
    # documented, expected behavior, not an oversight.
    potions = [
        {"id": "POTION.FIRE_POTION", "index": 0},
        {"id": "POTION.FIRE_POTION", "index": 2},
    ]
    assert play_full_run._resolve_potion_index(potions, "FIRE_POTION") == 0


# ---------------------------------------------------------------------------
# _resolve_choice_indices
# ---------------------------------------------------------------------------

def test_resolve_choice_indices_single_token():
    pending_cards = [
        {"id": "CARD.STRIKE", "index": 0, "upgraded": False},
        {"id": "CARD.DEFEND", "index": 1, "upgraded": False},
    ]
    choice = {"cards": [{"card_id": "DEFEND", "option_occurrence": 0, "upgrade_level": 0}]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice) == [1]


def test_resolve_choice_indices_duplicate_same_state_disambiguation():
    # Two unupgraded Strikes -- no mixed upgrade state involved, so this must
    # pass both before and after the upgrade-awareness fix.
    pending_cards = [
        {"id": "CARD.STRIKE", "index": 0, "upgraded": False},
        {"id": "CARD.STRIKE", "index": 1, "upgraded": False},
    ]
    choice_first = {"cards": [{"card_id": "STRIKE", "option_occurrence": 0, "upgrade_level": 0}]}
    choice_second = {"cards": [{"card_id": "STRIKE", "option_occurrence": 1, "upgrade_level": 0}]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice_first) == [0]
    assert play_full_run._resolve_choice_indices(pending_cards, choice_second) == [1]


def test_resolve_choice_indices_upgrade_state_disambiguation_base_upgraded_base():
    # pending_cards: [base Strike(0), Strike+(1), base Strike(2)]. Target is
    # the SECOND base (unupgraded) Strike, at index 2.
    #
    # Per the vendor's HasStableTokenIdentity (Entry AND upgrade level must
    # both match), OptionOccurrence for that target counts only prior
    # candidates sharing BOTH entry and upgrade state:
    #   index 0 (base Strike): same entry, same upgrade state -> counts -> occurrence=1
    #   index 1 (Strike+): same entry, DIFFERENT upgrade state -> does not count
    # so option_occurrence = 1.
    #
    # An entry-only (upgrade-blind) resolver would instead count index 1
    # (Strike+) as a match too, and incorrectly resolve occurrence=1 to
    # index 1 (the upgraded card) instead of index 2 (the correct base card).
    pending_cards = [
        {"id": "CARD.STRIKE", "index": 0, "upgraded": False},
        {"id": "CARD.STRIKE", "index": 1, "upgraded": True},
        {"id": "CARD.STRIKE", "index": 2, "upgraded": False},
    ]
    choice = {"cards": [{"card_id": "STRIKE", "option_occurrence": 1, "upgrade_level": 0}]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice) == [2]


def test_resolve_choice_indices_upgrade_state_disambiguation_upgraded_base_base():
    # Same idea, different relative order: [Strike+(0), base Strike(1), base Strike(2)].
    # Target is the second base Strike, at index 2.
    #   index 0 (Strike+): different upgrade state -> does not count
    #   index 1 (base Strike): same entry, same upgrade state -> counts -> occurrence=1
    # so option_occurrence = 1, and it must resolve to index 2, not index 1.
    pending_cards = [
        {"id": "CARD.STRIKE", "index": 0, "upgraded": True},
        {"id": "CARD.STRIKE", "index": 1, "upgraded": False},
        {"id": "CARD.STRIKE", "index": 2, "upgraded": False},
    ]
    choice = {"cards": [{"card_id": "STRIKE", "option_occurrence": 1, "upgrade_level": 0}]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice) == [2]


def test_resolve_choice_indices_multi_token_resolves_each_independently():
    pending_cards = [
        {"id": "CARD.STRIKE", "index": 0, "upgraded": False},
        {"id": "CARD.DEFEND", "index": 1, "upgraded": False},
        {"id": "CARD.BASH", "index": 2, "upgraded": False},
    ]
    choice = {"cards": [
        {"card_id": "BASH", "option_occurrence": 0, "upgrade_level": 0},
        {"card_id": "STRIKE", "option_occurrence": 0, "upgrade_level": 0},
    ]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice) == [2, 0]


def test_resolve_choice_indices_unresolvable_token_returns_none():
    pending_cards = [{"id": "CARD.STRIKE", "index": 0, "upgraded": False}]
    choice = {"cards": [{"card_id": "DEFEND", "option_occurrence": 0, "upgrade_level": 0}]}
    assert play_full_run._resolve_choice_indices(pending_cards, choice) is None
