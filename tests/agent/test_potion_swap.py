"""agent/potion_swap.py: which potion the autopilot keeps when the belt is full."""
import pytest

from agent.potion_swap import SWAP_MARGIN, choose_potion_swap, potion_keep_score

# Descriptions as the decision JSON carries them (localization_eng/potions.json, numbers resolved).
FAIRY = {"id": "POTION.FAIRY_IN_A_BOTTLE", "name": {"en": "Fairy in a Bottle", "zh": "瓶中精灵"},
         "description": {"en": "When your HP would be reduced to 0, instead this potion is discarded "
                                "and you heal to 30% of your Max HP."}}
STRENGTH = {"id": "POTION.STRENGTH_POTION", "name": {"en": "Strength Potion"},
            "description": {"en": "Gain 2 Strength."}}
FIRE = {"id": "POTION.FIRE_POTION", "name": {"en": "Fire Potion"},
        "description": {"en": "Deal 20 damage."}}
BLOCK = {"id": "POTION.BLOCK_POTION", "name": {"en": "Block Potion"},
         "description": {"en": "Gain 12 Block."}}
SWIFT = {"id": "POTION.SWIFT_POTION", "name": {"en": "Swift Potion"},
         "description": {"en": "Draw 3 cards."}}


def player(held, slots=3):
    return {"potions": [dict(p, index=i) for i, p in enumerate(held)], "potion_slots": slots}


def test_fairy_outranks_every_other_potion():
    # The shop scorer alone ranked it (5.0) below Strength (6.0): the data fix this module exists for.
    fairy = potion_keep_score(FAIRY)
    assert fairy > potion_keep_score(STRENGTH) + SWAP_MARGIN
    for other in (STRENGTH, FIRE, BLOCK, SWIFT):
        assert fairy > potion_keep_score(other)


def test_fairy_is_recognised_by_text_alone():
    # an old decision JSON without ids still scores it as a Fairy would
    bare = {"name": "Fairy in a Bottle", "description": FAIRY["description"]["en"]}
    assert potion_keep_score(bare) < potion_keep_score(FAIRY)  # no id bonus ...
    # ... which is why the id is what the mod sends; the swap decision uses it
    assert potion_keep_score(FAIRY) == pytest.approx(potion_keep_score(bare) + 12.0)


def test_full_belt_discards_the_weakest_for_a_fairy():
    p = player([FIRE, STRENGTH, BLOCK])
    reply = choose_potion_swap(p, [FAIRY])
    assert reply["reason"] == "swap"
    assert reply["discard"] == 0 and reply["discard_id"] == "POTION.FIRE_POTION"  # Fire (3.0) < Block (4.5)
    assert reply["take"] == 0 and reply["take_id"] == "POTION.FAIRY_IN_A_BOTTLE"


def test_fairy_replaces_a_strength_potion_when_that_is_the_worst():
    p = player([STRENGTH, STRENGTH, STRENGTH])
    reply = choose_potion_swap(p, [FAIRY])
    assert reply["reason"] == "swap" and reply["discard"] == 0 and reply["take"] == 0


def test_a_held_fairy_is_never_the_one_discarded():
    p = player([FAIRY, SWIFT, FIRE])
    reply = choose_potion_swap(p, [STRENGTH])
    assert reply["reason"] == "swap" and reply["discard"] == 2  # Fire, not the Fairy


def test_a_worse_or_close_offer_changes_nothing():
    p = player([STRENGTH, SWIFT, BLOCK])
    assert choose_potion_swap(p, [FIRE])["reason"] == "not_better"
    # Swift 5.0 vs Block 4.5: better, but by less than the margin
    reply = choose_potion_swap(player([BLOCK, BLOCK, BLOCK]), [SWIFT])
    assert reply["reason"] == "not_better"
    assert reply["discard"] is None and reply["take"] is None


def test_the_margin_is_inclusive():
    # Strength 6.0 vs Block 4.5 = 1.5: not enough; vs Fire 3.0 = 3.0: swap
    assert choose_potion_swap(player([BLOCK] * 3), [STRENGTH])["reason"] == "not_better"
    assert choose_potion_swap(player([FIRE] * 3), [STRENGTH])["reason"] == "swap"


def test_a_belt_with_room_just_takes():
    reply = choose_potion_swap(player([STRENGTH], slots=3), [FIRE])
    assert reply == dict(reply, reason="free_slot", discard=None, take=0)
    # a high-ascension belt of 2 is full with 2
    assert choose_potion_swap(player([STRENGTH, STRENGTH], slots=2), [FIRE])["reason"] == "not_better"
    # no potion_slots field: the old default of 3
    assert choose_potion_swap({"potions": [STRENGTH, STRENGTH]}, [FIRE])["reason"] == "free_slot"


def test_several_offers_pick_the_best_one():
    assert choose_potion_swap(player([]), [FIRE, STRENGTH])["take"] == 1
    reply = choose_potion_swap(player([FIRE] * 3), [BLOCK, FAIRY, STRENGTH])
    assert reply["reason"] == "swap" and reply["take"] == 1


def test_nothing_offered_and_no_slots():
    assert choose_potion_swap(player([FIRE]), [])["reason"] == "no_offer"
    assert choose_potion_swap(player([], slots=0), [FAIRY])["reason"] == "no_slots"


def test_scores_are_reported_and_strict_json():
    import json
    reply = choose_potion_swap(player([FIRE, STRENGTH, BLOCK]), [FAIRY])
    assert reply["held_scores"] == [3.0, 6.0, 4.5] and reply["offered_scores"] == [17.0]
    json.dumps(reply, allow_nan=False)
