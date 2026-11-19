"""Regression cases from v0.113.1 Fool descriptions and live A9 choices."""
import copy
import pytest
from agent.card_scoring import card_dimensions, score_card, score_card_in_deck, pick_best_card
from agent.greedy_trace import GreedyTrace


def card(cid, **stats):
    return {"id": "CARD." + cid, "description": cid + ".description", "stats": stats}


def starter():
    return [card("STRIKE_FOOL", damage=6) for _ in range(5)] + [card("DEFEND_FOOL", block=5) for _ in range(4)] + [card("FOOLISH_SHOT", damage=15)]


@pytest.mark.parametrize("cid,count,draw", [
    ("SORROWFUL_STRIKE", 2, 0), ("JEST", 1, 0), ("ECSTASY", 1, 0),
    ("ENSEMBLE", 4, 0), ("KI_CHANT", 3, 1), ("RUMMAGE", 3, 3),
])
def test_cards_variable_is_not_always_draw(cid, count, draw):
    assert card_dimensions(card(cid, cards=count))["draw"] == draw


def test_rapid_shot_counts_all_hits():
    assert card_dimensions(card("RAPID_SHOT", damage=3, hitcount=3))["attack"] == 9


def test_stretch_counts_as_defense_but_not_as_block_already_gained():
    value = card_dimensions(card("DEEP_STRETCH", stretchpower=7, cards=1))["defense"]
    assert 0 < value <= 7


def test_next_turn_energy_has_lower_value_than_immediate():
    from agent.fool_scoring import effects
    assert effects(card("KI_REST", energy=5))["energy"] < 5
    assert effects(card("BOW_SMACK", energy=1))["energy"] < 1


def test_sadness_has_a_cost_without_payoff():
    deck = starter()
    offered = card("SORROWFUL_STRIKE", damage=13, cards=2)
    assert score_card_in_deck(offered, deck) < score_card_in_deck(card("QUICK_SHOT", damage=10), deck)
    assert score_card_in_deck(offered, deck + [card("ALL_IS_WELL"), card("ANGUISH")]) > score_card_in_deck(offered, deck)


def test_melody_payoff_depends_on_available_melodies():
    bare = starter()
    supported = bare + [card("WAH_TOUCH"), card("CHO_READY"), card("KI_HOP")]
    assert score_card_in_deck(card("LULLABY"), supported) > score_card_in_deck(card("LULLABY"), bare)
    assert score_card_in_deck(card("HARMONIC_SHIELD"), supported) > score_card_in_deck(card("HARMONIC_SHIELD"), bare)


def test_aim_and_quiver_help_multihit_shots():
    plain = starter()
    supported = plain + [card("ARCHERY"), card("QUIVER")]
    assert score_card_in_deck(card("RAPID_SHOT"), supported) > score_card_in_deck(card("RAPID_SHOT"), plain)


def test_large_fool_deck_can_take_core_defense_with_explainable_total():
    deck = starter() + [card("WAH_TOUCH"),card("CHO_READY"),card("KI_HOP")]*6
    offered = [card("SORROWFUL_STRIKE"), card("LULLABY"),card("KI_REST")]
    trace=GreedyTrace()
    assert pick_best_card(offered, threshold=6, deck=deck, trace=trace) == 1
    for option in trace.to_dict()["options"]:
        if option.get("score") is not None:
            assert option["score"] == pytest.approx(sum(c["value"] for c in option["components"]))


def test_fool_scoring_does_not_mutate_observation():
    value=card("KI_CHANT",cards=3)
    before=copy.deepcopy(value)
    card_dimensions(value); score_card(value);score_card_in_deck(value,starter())
    assert value==before


def test_harmonic_shield_recognizes_each_melody_block():
    deck=starter()+[card("WAH_TOUCH"),card("CHO_READY"),card("KI_HOP")]
    assert score_card_in_deck(card("HARMONIC_SHIELD"),deck) >= 6


def test_smith_prefers_real_upgrade_over_high_base_score():
    from agent.fool_scoring import best_upgrade
    a=card("SORROWFUL_STRIKE", damage=13,cards=2)
    a["after_upgrade"]={"cost":1,"stats":{"damage":17,"cards":2}}
    b=card("MELODIC_SHOT",damage=12)
    b["cost"]=1
    b["after_upgrade"]={"cost":0,"stats":{"damage":13}}
    assert best_upgrade([a,b],starter()) == 1


@pytest.mark.parametrize("cid,key",[("MELODIC_SHOT","attack"),("RHYTHM_STRIKE","attack"),("WAH_BLAST","attack"),("HARMONIC_SHIELD","defense"),("STEADY_CADENCE","draw")])
def test_contextual_melody_effects(cid,key):
    from agent.fool_scoring import effects
    deck=starter()+[card("WAH_TOUCH"),card("CHO_READY"),card("KI_HOP")]
    assert effects(card(cid),deck)[key] > effects(card(cid),starter())[key]


def test_melody_cost_reduction_and_symphony_count():
    from agent.fool_scoring import effects
    deck=starter()+[card("WAH_TOUCH"),card("CHO_READY"),card("KI_HOP")]
    assert effects(card("CODA"),deck)["cost"] < effects(card("CODA"),starter())["cost"]
    assert effects(card("CHO_LIMBS"),deck)["cost"] < effects(card("CHO_LIMBS"),starter())["cost"]
    assert effects(card("WAH_SYMPHONY"),deck)["attack"] > effects(card("WAH_SYMPHONY"),starter())["attack"]


def test_song_path_requires_cho_and_lullaby_requires_all_three():
    bare=starter()
    wrong=bare+[card("WAH_TOUCH"),card("KI_HOP")]
    right=wrong+[card("CHO_READY")]
    assert score_card_in_deck(card("SONG_PATH"),wrong) < 5.5
    assert score_card_in_deck(card("LULLABY"),wrong) < 5.5
    assert score_card_in_deck(card("SONG_PATH"),right) > score_card_in_deck(card("SONG_PATH"),wrong)


def test_mixed_upgrade_uses_cost_reduction_and_records_all_options():
    from agent.combat_env import greedy_action
    a=card("STRIKE_FOOL",damage=6);a["after_upgrade"]={"cost":1,"stats":{"damage":9}}
    b={"id":"CARD.COLORLESS_COST_REDUCTION","cost":3,"stats":{},"after_upgrade":{"cost":0,"stats":{}}}
    trace=GreedyTrace()
    state={"decision":"card_select","context":{"room_type":"RestSite"},"cards":[a,b],"player":{"deck":starter()}}
    result=greedy_action(state,trace=trace)
    assert result["args"]["indices"]=="1"
    assert len(trace.to_dict()["options"])==2


def test_harmonic_upgrade_uses_per_melody_delta():
    from agent.fool_scoring import upgrade_value
    a=card("HARMONIC_SHIELD",calculationextra=5)
    a["after_upgrade"]={"cost":1,"stats":{"calculationextra":7}}
    deck=starter()+[card("WAH_TOUCH"),card("CHO_READY"),card("KI_HOP")]
    assert upgrade_value(a,deck) > 1.5


def test_one_foreign_fool_card_does_not_switch_another_character_policy():
    from agent.fool_scoring import is_fool_deck
    assert not is_fool_deck([card("STRIKE_IRONCLAD")]*10+[card("NOCK")])


def test_lost_lute_requires_a_melody_and_chord_requires_wah():
    assert score_card_in_deck(card('LOST_LUTE'),starter()) < 5.5
    without_wah=starter()+[card('CHO_READY'),card('KI_HOP')]
    assert score_card_in_deck(card('VIOLENT_CHORD'),without_wah) < 3.5


def test_recurring_block_upgrade_beats_a_small_strike_upgrade():
    from agent.fool_scoring import upgrade_value
    a=card('HIDDEN_ROOTS',hiddenrootspower=3)
    a['after_upgrade']={'cost':1,'stats':{'hiddenrootspower':4}}
    assert upgrade_value(a,starter()) > 1.5


def test_vitriol_is_free_weak_utility_not_an_effectless_sadness_card():
    from agent.fool_scoring import upgrade_value
    value=card('VITRIOL',weakpower=2)
    value['cost']=0
    value['after_upgrade']={'cost':0,'stats':{'weakpower':3}}
    assert score_card_in_deck(value,starter()) >= 5.5
    assert upgrade_value(value,starter()) > 0


def test_sparse_upgraded_debuff_metadata_and_observation_aliases():
    from agent.fool_scoring import effects, upgrade_value
    assert effects({'id':'CARD.VITRIOL','upgraded':True})['weak']==3
    value=card('VITRIOL',weak=2)
    value['after_upgrade']={'cost':0,'stats':{'weak':3}}
    assert upgrade_value(value,starter()) > 0
    assert effects(card('UPROOT',vulnerable=7))['vulnerable']==7
