import pytest

from agent.card_scoring import (
    deck_5turn_burst,
    deck_quality_metrics,
    is_act1_card_reward_eligible,
    pick_best_card,
    score_deck_dimensions,
    set_mc_context,
)


def card(cid, *, cost=1, ctype="Attack", damage=0, block=0, draw=0,
         description="", rarity="Common"):
    stats = {}
    if damage:
        stats["damage"] = damage
    if block:
        stats["block"] = block
    if draw:
        stats["cards"] = draw
    return {
        "id": cid,
        "name": {"en": cid},
        "cost": cost,
        "type": ctype,
        "rarity": rarity,
        "stats": stats,
        "description": description,
    }


def _stub_gate_signals(monkeypatch, *, delta, score=5.0, card_tags=()):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda cards: {"overall": 0.5 + (delta if len(cards) == 16 else 0.0)},
    )
    monkeypatch.setattr(scoring, "score_card_in_deck", lambda offered, deck: score)

    def fake_tags(value):
        if value.get("id") == "CARD.OFFER":
            return set(card_tags)
        return {"SCALING_PILLAR"} if value.get("pillar") else set()

    monkeypatch.setattr(scoring, "_card_tags", fake_tags)


def _gate_deck(size, *, pillars=0):
    return [
        {
            "id": f"CARD.DECK_{i}",
            "pillar": i < pillars,
        }
        for i in range(size)
    ]


def test_act1_card_quality_gate_is_inactive_outside_act1(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda deck: (_ for _ in ()).throw(AssertionError("gate should not score")),
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(18), act=2
    )


def test_act1_card_quality_gate_is_inactive_below_15_cards(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda deck: (_ for _ in ()).throw(AssertionError("gate should not score")),
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(14), act=1
    )


@pytest.mark.parametrize(
    ("delta", "expected"),
    [(0.0, False), (-0.001, False), (0.001, True)],
)
def test_act1_card_quality_gate_midrange_delta_boundary(
        monkeypatch, delta, expected):
    _stub_gate_signals(monkeypatch, delta=delta)
    assert (
        is_act1_card_reward_eligible(
            {"id": "CARD.OFFER"}, _gate_deck(15), act=1
        )
        is expected
    )


@pytest.mark.parametrize(
    ("score", "tags", "pillars"),
    [(9.5, (), 2), (5.0, ("SCALING_PILLAR",), 1)],
)
def test_act1_card_quality_gate_midrange_premium_exception(
        monkeypatch, score, tags, pillars):
    _stub_gate_signals(
        monkeypatch,
        delta=-0.001,
        score=score,
        card_tags=tags,
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(15, pillars=pillars), act=1
    )


def test_act1_card_quality_gate_midrange_premium_rejects_severe_dilution(
        monkeypatch):
    _stub_gate_signals(monkeypatch, delta=-0.011, score=10.0)
    assert not is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(15), act=1
    )


def test_act1_card_quality_gate_rejects_every_card_at_16_card_cap(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda cards: (_ for _ in ()).throw(
            AssertionError("hard cap should reject before scoring")
        ),
    )
    assert not is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(16), act=1
    )


@pytest.mark.parametrize(
    ("offered", "deck", "act"),
    [
        ({}, _gate_deck(15), 1),
        ({"id": "CARD.OFFER"}, [{"name": "missing id"}] * 15, 1),
        ({"id": "CARD.OFFER"}, _gate_deck(15), None),
        ({"id": "CARD.OFFER"}, _gate_deck(15), "invalid"),
    ],
)
def test_act1_card_quality_gate_invalid_inputs_fail_open(offered, deck, act):
    assert is_act1_card_reward_eligible(offered, deck, act)


def test_act1_card_quality_gate_nonfinite_metric_fails_open(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring, "deck_quality_metrics", lambda cards: {"overall": float("nan")}
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(15), act=1
    )


def test_deck_quality_counts_strength_scaling_as_boss_burst():
    deck = (
        [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(4)]
        + [card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5) for _ in range(4)]
        + [
            card("CARD.BASH", cost=2, damage=8,
                 description="Deal 8 damage. Apply 2 Vulnerable."),
            card("CARD.SHRUG_IT_OFF", ctype="Skill", block=8, draw=1,
                 description="Gain 8 Block. Draw 1 card."),
            card("CARD.IRON_WAVE", damage=5, block=5,
                 description="Deal 5 damage. Gain 5 Block."),
        ]
    )
    inflame = card("CARD.INFLAME", ctype="Power",
                   description="Gain 2 Strength.")

    before = deck_quality_metrics(deck)
    after = deck_quality_metrics(deck + [inflame])

    assert after["burst_5turn"] >= before["burst_5turn"] + 15
    assert after["overall"] > before["overall"]


def test_score_deck_dimensions_uses_wiki_stats_for_id_only_deck_cards():
    deck = [
        {"id": "CARD.STRIKE_IRONCLAD"},
        {"id": "CARD.DEFEND_IRONCLAD"},
    ]

    dims = score_deck_dimensions(deck)

    assert dims["attack"] > 0
    assert dims["defense"] > 0


def test_late_low_block_deck_prefers_real_defense_card():
    deck = (
        [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
        + [card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5) for _ in range(4)]
        + [
            card("CARD.BASH", cost=2, damage=8,
                 description="Deal 8 damage. Apply 2 Vulnerable."),
            card("CARD.UPPERCUT", cost=2, damage=13,
                 description="Deal 13 damage. Apply 1 Weak. Apply 1 Vulnerable."),
            card("CARD.SWORD_BOOMERANG", damage=3,
                 description="Deal 3 damage 3 times."),
            card("CARD.THUNDERCLAP", damage=4,
                 description="Deal 4 damage to ALL enemies. Apply 1 Vulnerable."),
            card("CARD.MOLTEN_FIST", damage=10,
                 description="Deal 10 damage."),
        ]
    )
    offers = [
        card("CARD.POMMEL_STRIKE", damage=9, draw=1,
             description="Deal 9 damage. Draw 1 card."),
        card("CARD.SHRUG_IT_OFF", ctype="Skill", block=8, draw=1,
             description="Gain 8 Block. Draw 1 card."),
    ]

    set_mc_context(hp=59, max_hp=80, floor=14)

    assert pick_best_card(offers, deck=deck) == 1


def test_lean_deck_counts_rampage_as_boss_cycle_burst():
    deck = (
        [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(4)]
        + [card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5) for _ in range(4)]
        + [
            card("CARD.BASH", cost=2, damage=8,
                 description="Deal 8 damage. Apply 2 Vulnerable."),
            card("CARD.SHRUG_IT_OFF", ctype="Skill", block=8, draw=1,
                 description="Gain 8 Block. Draw 1 card."),
        ]
    )
    rampage = card(
        "CARD.RAMPAGE",
        damage=9,
        description="Deal 9 damage. Increase this card's damage by 5 this combat.",
    )

    assert deck_5turn_burst(deck + [rampage]) >= deck_5turn_burst(deck) + 20


def test_lean_boss_deck_prefers_rampage_over_generic_damage():
    deck = (
        [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
        + [card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5) for _ in range(4)]
        + [
            card("CARD.BASH", cost=2, damage=8,
                 description="Deal 8 damage. Apply 2 Vulnerable."),
        ]
    )
    offers = [
        card("CARD.CLEAVE", damage=8,
             description="Deal 8 damage to ALL enemies."),
        card("CARD.TWIN_STRIKE", damage=10,
             description="Deal 5 damage twice."),
        card("CARD.RAMPAGE", damage=9,
             description="Deal 9 damage. Increase this card's damage by 5 this combat."),
    ]

    set_mc_context(hp=70, max_hp=80, floor=10)

    assert pick_best_card(offers, deck=deck) == 2


def test_boss_readiness_lift_can_prefer_large_readiness_lift_over_mob_attack(monkeypatch):
    monkeypatch.setenv("STS2_BOSS_READINESS_LIFT", "1")
    deck = (
        [{"id": "CARD.STRIKE_IRONCLAD"} for _ in range(5)]
        + [{"id": "CARD.DEFEND_IRONCLAD"} for _ in range(4)]
        + [{"id": "CARD.BASH"}, {"id": "CARD.SWORD_BOOMERANG"}]
    )
    offers = [
        {"id": "CARD.POMMEL_STRIKE", "cost": 1, "type": "Attack", "rarity": "Common"},
        {"id": "CARD.HOWL_FROM_BEYOND", "cost": 1, "type": "Attack", "rarity": "Rare"},
        {"id": "CARD.INFERNO", "cost": 2, "type": "Attack", "rarity": "Common"},
    ]

    set_mc_context(
        hp=67,
        max_hp=80,
        floor=3,
        relics=["BURNING_BLOOD", "WINGED_BOOTS"],
    )

    assert pick_best_card(offers, threshold=0, deck=deck) == 1


def test_rampage_cycle_bonus_does_not_beat_strength_premium():
    deck = (
        [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
        + [card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5) for _ in range(4)]
        + [
            card("CARD.BASH", cost=2, damage=8,
                 description="Deal 8 damage. Apply 2 Vulnerable."),
        ]
    )
    offers = [
        card("CARD.RAMPAGE", damage=9,
             description="Deal 9 damage. Increase this card's damage by 5 this combat."),
        card("CARD.INFLAME", ctype="Power",
             description="Gain 2 Strength."),
    ]

    set_mc_context(hp=70, max_hp=80, floor=10)

    assert pick_best_card(offers, deck=deck) == 1
