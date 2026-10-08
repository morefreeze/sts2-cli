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

import pytest

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


# ---------------------------------------------------------------------------
# _execute_combat_plan_actions: live CanPlay refusals are stale plans (BUG-042)
# ---------------------------------------------------------------------------

def _combat(hand_ids, energy=1):
    return {"type": "decision", "decision": "combat_play", "round": 1,
            "energy": energy, "enemies": [],
            "hand": [{"id": f"CARD.{cid}", "index": i} for i, cid in enumerate(hand_ids)]}


def _scripted_send(replies):
    """A send() that answers each command with the next scripted reply and
    records what was sent."""
    sent = []
    it = iter(replies)

    def send(cmd):
        sent.append(cmd)
        return next(it)

    return send, sent


def _play(card_id):
    return {"action": "play_card", "card_id": card_id, "card_occurrence": 0}


TWO_PLAYS_THEN_END = [_play("STRIKE"), _play("BODYGUARD"), {"action": "end_turn"}]


def test_live_can_play_refusal_is_a_stale_plan_not_a_failure(capsys):
    start = _combat(["STRIKE", "BODYGUARD"])
    after_strike = _combat(["BODYGUARD"], energy=0)
    refusal = {"type": "error", "message": "Cannot play card Bodyguard: EnergyCostTooHigh"}
    send, sent = _scripted_send([after_strike, refusal])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, TWO_PLAYS_THEN_END, stats=stats)
    assert ok is True
    # The last GOOD decision (after the first play), never the error dict.
    assert state is after_strike
    assert stats == {"stale_refusals": 1}
    # Both plays were attempted, then nothing: no end_turn, no retry.
    assert [c["action"] for c in sent] == ["play_card", "play_card"]
    out = capsys.readouterr().out
    assert "live engine refused BODYGUARD" in out
    assert "Cannot play card Bodyguard: EnergyCostTooHigh" in out
    assert "BUG-042" in out and "re-planning from the live state" in out


def test_refusal_of_the_first_action_returns_the_input_state():
    start = _combat(["BODYGUARD"])
    refusal = {"type": "error", "message": "Cannot play card Bodyguard: EnergyCostTooHigh"}
    send, sent = _scripted_send([refusal])
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [_play("BODYGUARD"), {"action": "end_turn"}])
    assert ok is True and state is start  # stats is optional
    assert len(sent) == 1


def test_stale_refusals_accumulate_in_a_caller_supplied_stats_dict():
    start = _combat(["BODYGUARD"])
    refusal = {"type": "error", "message": "Cannot play card Bodyguard: EnergyCostTooHigh"}
    send, _ = _scripted_send([refusal, refusal])
    stats = {"stale_refusals": 1}
    play_full_run._execute_combat_plan_actions(send, start, [_play("BODYGUARD")], stats=stats)
    play_full_run._execute_combat_plan_actions(send, start, [_play("BODYGUARD")], stats=stats)
    assert stats["stale_refusals"] == 3


def test_still_in_hand_after_action_stays_a_hard_failure():
    start = _combat(["STRIKE", "BODYGUARD"])
    err = {"type": "error", "message":
           "Card could not be played (still in hand after action): Bodyguard [CARD.BODYGUARD]"}
    send, _ = _scripted_send([_combat(["BODYGUARD"], energy=0), err])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, TWO_PLAYS_THEN_END, stats=stats)
    assert ok is False and state is err
    assert stats == {}


@pytest.mark.parametrize("message", [
    "'target_index' is required when multiple enemies are alive (2)",
    "Invalid target_index 5",
    "Not in play phase",
])
def test_other_play_card_errors_stay_hard_failures(message):
    start = _combat(["STRIKE"])
    err = {"type": "error", "message": message}
    send, _ = _scripted_send([err])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [_play("STRIKE")], stats=stats)
    assert ok is False and state is err
    assert stats == {}


def test_potion_errors_stay_hard_failures():
    start = _combat(["STRIKE"])
    start["player"] = {"potions": [{"id": "POTION.FIRE_POTION", "index": 0}]}
    err = {"type": "error", "message": "Cannot play card Whatever: EnergyCostTooHigh"}
    send, _ = _scripted_send([err])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [{"action": "use_potion", "potion_id": "FIRE_POTION"}], stats=stats)
    # The BUG-042 relaxation is for a planned play_card only.
    assert ok is False and state is err
    assert stats == {}


# ---------------------------------------------------------------------------
# _execute_combat_plan_actions: unresolved card_id / potion_id are stale plans
# too, and are counted (BUG-051)
# ---------------------------------------------------------------------------

def test_unresolved_card_id_is_counted_as_a_stale_plan(capsys):
    start = _combat(["DEFEND"])
    send, sent = _scripted_send([])   # nothing may be sent: next() would raise
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [_play("STRIKE"), {"action": "end_turn"}], stats=stats)
    assert ok is True and state is start
    assert sent == []
    assert stats["stale_unresolved"] == 1
    assert "stale_refusals" not in stats
    assert "not found in live hand" in capsys.readouterr().out


def test_unresolved_potion_id_is_counted_as_a_stale_plan():
    start = _combat(["STRIKE"])
    start["player"] = {"potions": [{"id": "POTION.BLOCK_POTION", "index": 0}]}
    send, sent = _scripted_send([])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [{"action": "use_potion", "potion_id": "FIRE_POTION"},
                      {"action": "end_turn"}], stats=stats)
    assert ok is True and state is start
    assert sent == []
    assert stats["stale_unresolved"] == 1


def test_unresolved_ids_accumulate_in_a_caller_supplied_stats_dict():
    start = _combat(["DEFEND"])
    send, _ = _scripted_send([])
    stats = {"stale_unresolved": 2}
    play_full_run._execute_combat_plan_actions(send, start, [_play("STRIKE")], stats=stats)
    assert stats["stale_unresolved"] == 3


def test_unresolved_id_without_stats_still_returns_the_input_state():
    start = _combat(["DEFEND"])
    send, sent = _scripted_send([])
    state, ok = play_full_run._execute_combat_plan_actions(send, start, [_play("STRIKE")])
    assert ok is True and state is start and sent == []


def test_a_stale_plan_that_executed_nothing_is_counted_as_no_progress():
    # Unresolved first action: nothing sent. Refused first action: the one
    # command sent was refused, so the engine is untouched. Both are plans
    # that made no progress -- the shape that looped until STUCK in BUG-051.
    refusal = {"type": "error", "message": "Cannot play card Bodyguard: EnergyCostTooHigh"}
    stats = {}
    send, _ = _scripted_send([])
    play_full_run._execute_combat_plan_actions(
        send, _combat(["DEFEND"]), [_play("STRIKE")], stats=stats)
    send, _ = _scripted_send([refusal])
    play_full_run._execute_combat_plan_actions(
        send, _combat(["BODYGUARD"]), [_play("BODYGUARD")], stats=stats)
    assert stats == {"stale_unresolved": 1, "stale_refusals": 1, "stale_no_progress": 2}


def test_a_stale_plan_that_executed_something_is_not_no_progress():
    # The first play was accepted, so this plan changed the live state (and the
    # outer loop's STUCK detector sees a different state next time).
    start = _combat(["STRIKE", "DEFEND"])
    after_strike = _combat(["DEFEND"], energy=0)
    send, sent = _scripted_send([after_strike])
    stats = {}
    state, ok = play_full_run._execute_combat_plan_actions(
        send, start, [_play("STRIKE"), _play("BASH"), {"action": "end_turn"}], stats=stats)
    assert ok is True and state is after_strike and len(sent) == 1
    assert stats == {"stale_unresolved": 1}
