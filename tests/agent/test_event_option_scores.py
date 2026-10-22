"""Pin the score `combat_env._score_event_option` gives each keyword branch.

The golden corpus only pins which option wins (argmax); this table pins the
numbers. One phrase per branch of the function (28, in source order, the keys
of agent.greedy_trace.EVENT_KEYWORD_KEYS); each fires its branch and, bar
remove_card_hp_cost (which trims remove_card's +6), no other, so a wrong delta
shows up as exactly one wrong entry. The expected values were computed from the
function as it stood when the corpus was built (Task A2), before Task A5
rewrites it; they are literals on purpose.

The same phrases build the corpus's synthetic `event.kw.*` rows
(tools/build_advisor_corpus.EVENT_KEYWORD_CASES); the last tests tie the two.
"""
import os
import sys

import pytest

from agent.combat_env import _score_event_option
from agent.greedy_trace import EVENT_KEYWORD_KEYS
from tests.agent.advisor_corpus import corpus_rows

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools"))
import build_advisor_corpus as bac  # noqa: E402

EXPECTED_SCORES = {
    ('Sacrifice', 'Lose 5 Max HP.'): -10.0,  # event.kw.lose_max_hp
    ('Dark Pact', 'Add a Curse to your deck.'): -8.0,  # event.kw.curse
    ('Tribute', 'Lose all your gold.'): -5.0,  # event.kw.lose_all_gold
    ('Toll', 'Lose 50 gold.'): -2.0,  # event.kw.lose_gold
    ('Torment', 'Accept the mark.'): -5.0,  # event.kw.torment
    ('Brace', 'Take 10 damage.'): -2.0,  # event.kw.take_damage
    ('Bleed', 'Lose 10 HP.'): -2.0,  # event.kw.lose_hp
    ('Wear Down', 'Downgrade a card.'): -4.0,  # event.kw.downgrade
    ('Stowaway', 'Add an additional Strike to your deck.'): -3.0,  # event.kw.add_basic
    ('Gambit', 'Obtain a rare card.'): 8.0,  # event.kw.rare_card
    ('Trade', 'Obtain an uncommon card.'): 4.0,  # event.kw.uncommon_card
    ('Offer', 'Choose a card to obtain.'): 2.0,  # event.kw.card_reward
    ('Purge', 'Remove a card from your deck.'): 6.0,  # event.kw.remove_card
    ('Blood Price', 'Pay 6 HP to remove a card from your deck.'): 3.0,  # event.kw.remove_card_hp_cost
    ('Find', 'Obtain a random relic.'): 5.0,  # event.kw.relic
    ('Idol', 'Obtain a relic and add a Strike to your deck.'): 2.0,  # event.kw.relic_with_downside
    ('Anvil', 'Upgrade a card.'): 4.0,  # event.kw.upgrade
    ('Morph', 'Transform a card.'): 3.0,  # event.kw.transform
    ('Windfall', 'Gain 100 gold.'): 3.0,  # event.kw.gain_gold
    ('Vitality', 'Raise your Max HP by 7.'): 3.0,  # event.kw.gain_max_hp
    ('Elixir', 'Obtain a random potion.'): 2.0,  # event.kw.potion
    ('Rest', 'Heal 20 HP.'): 2.0,  # event.kw.heal
    ('Gift', 'Add a colorless card to your deck.'): 2.0,  # event.kw.colorless_card
    ('Training', 'Gain 1 Strength.'): 4.0,  # event.kw.gain_strength
    ('Rage', 'Enemies gain 1 Strength.'): -3.0,  # event.kw.enemy_strength
    ('Agility', 'Gain 1 Dexterity.'): 3.0,  # event.kw.gain_dexterity
    ('Focus', 'Gain 1 energy at the start of each turn.'): 5.0,  # event.kw.energy_each_turn
    ('Surge', 'Gain 1 energy.'): 1.5,  # event.kw.energy_once
}


def score(title, description):
    return _score_event_option({"title": title, "description": description})


def test_table_has_one_entry_per_keyword_branch():
    assert len(EXPECTED_SCORES) == 28
    assert len(EXPECTED_SCORES) == len(EVENT_KEYWORD_KEYS)


@pytest.mark.parametrize("phrase", sorted(EXPECTED_SCORES), ids=lambda p: p[0])
def test_keyword_phrase_score(phrase):
    assert score(*phrase) == EXPECTED_SCORES[phrase]


def test_the_neutral_and_rival_options_score_what_the_corpus_rows_assume():
    assert score(*bac.EVENT_NEUTRAL_OPTION) == 0.0
    for rival in bac.EVENT_RIVAL_OPTION.values():
        assert score(*rival) == EXPECTED_SCORES[bac.EVENT_KEYWORD_CASES["event.kw.remove_card"]]


def test_table_phrases_are_the_corpus_builders_phrases():
    assert set(EXPECTED_SCORES) == set(bac.EVENT_KEYWORD_CASES.values())


def test_corpus_event_keyword_rows_are_two_option_flips():
    """Each synthetic event.kw.* row has exactly two unlocked options, picks the
    higher-scoring one, and lists the keyword option first iff it scores lower
    (so a tie, i.e. the branch's delta removed, goes to it)."""
    rows = {r["variant"]: r for r in corpus_rows()
            if r["game"] == bac.SYNTHETIC_GAME and r["variant"].startswith("event.kw.")}
    assert list(rows) == list(EVENT_KEYWORD_KEYS)
    for key, row in rows.items():
        options = row["state"]["options"]
        assert [o["index"] for o in options] == [0, 1]
        assert not any(o["is_locked"] for o in options)
        phrases = [(o["title"], o["description"]) for o in options]
        keyword = bac.EVENT_KEYWORD_CASES[key]
        rival = bac.EVENT_RIVAL_OPTION.get(key, bac.EVENT_NEUTRAL_OPTION)
        assert sorted(phrases) == sorted([keyword, rival])
        kw_score, rival_score = EXPECTED_SCORES[keyword], score(*rival)
        assert kw_score != rival_score
        assert (phrases[0] == keyword) == (kw_score < rival_score)
        picked = row["expected"]["args"]["option_index"]
        assert phrases[picked] == (keyword if kw_score > rival_score else rival)


# remove_card_hp_cost's phrase also fires remove_card (it trims remove_card's +6);
# every other phrase fires exactly its own branch.
EXPECTED_HIT_KEYS = {
    "event.kw.remove_card_hp_cost": ["event.kw.remove_card", "event.kw.remove_card_hp_cost"],
}


@pytest.mark.parametrize("key", list(bac.EVENT_KEYWORD_CASES), ids=lambda k: k)
def test_keyword_phrase_records_its_own_key(key):
    """The score table cannot tell two equal-valued branches apart (heal and
    potion are both +2.0); this ties each phrase to the key it is reported under."""
    title, description = bac.EVENT_KEYWORD_CASES[key]
    hits = []
    got = _score_event_option({"title": title, "description": description}, hits)
    assert [h[0] for h in hits] == EXPECTED_HIT_KEYS.get(key, [key])
    assert sum(h[1] for h in hits) == got == EXPECTED_SCORES[(title, description)]
