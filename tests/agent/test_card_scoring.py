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
    import agent.card_scoring as scoring

    monkeypatch.setenv("STS2_BOSS_READINESS_LIFT", "1")
    monkeypatch.setattr(scoring, "q_adjustment", lambda card: 0.0)
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

    # This used to assert `pick_best_card(...) == 1` (HOWL_FROM_BEYOND wins).
    # That stopped holding when data/advisor_card_ratings.json went 131 -> 507
    # cards: the STS2 catalogue rates POMMEL_STRIKE S and HOWL_FROM_BEYOND C,
    # the opposite of the pre-refresh OVERRIDES (7.5 vs 9.0), so POMMEL_STRIKE's
    # *base* now starts 2.25 ahead and no bonus of this size can close it.
    # That ranking flip is a deliberate consequence of trusting the refreshed
    # tier data and is for eval to judge, not for this test — so assert the
    # mechanism under test instead: the readiness lift must favour the card
    # that actually improves boss readiness, by enough to matter.
    pommel, howl = offers[0], offers[1]
    lift_pommel = scoring.boss_readiness_lift_bonus(pommel, deck, 3)
    lift_howl = scoring.boss_readiness_lift_bonus(howl, deck, 3)
    assert lift_howl > lift_pommel
    assert lift_howl - lift_pommel >= 0.5


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


# --- Quality / Curve axes (deck_quality_metrics) -------------------------

def test_quality_axis_scores_known_s_tier_deck_near_1():
    import agent.card_scoring as scoring

    # BATTLE_TRANCE is Ironclad S-tier in data/advisor_card_ratings.json —
    # verify rather than assume.
    rating = scoring._load_advisor_ratings()["BATTLE_TRANCE"]
    assert rating["tier"] == "S"

    deck = [card("CARD.BATTLE_TRANCE", cost=0, ctype="Skill") for _ in range(10)]

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["quality"] == pytest.approx(1.0)
    assert metrics["quality_pass"] == pytest.approx(1.0)


def test_quality_axis_scores_untiered_basics_at_0_2():
    import agent.card_scoring as scoring

    # WOUND/DAZE/SLIMED/BURN carry no advisor tier at all — verify rather
    # than assume.
    ratings = scoring._load_advisor_ratings()
    for cid in ("WOUND", "DAZE", "SLIMED", "BURN"):
        assert cid not in ratings

    deck = [card(f"CARD.{cid}") for cid in ("WOUND", "DAZE", "SLIMED", "BURN")]

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["quality"] == pytest.approx(0.2)


def test_quality_axis_mixed_deck_lands_between_s_tier_and_untiered():
    import agent.card_scoring as scoring

    ratings = scoring._load_advisor_ratings()
    assert ratings["BATTLE_TRANCE"]["tier"] == "S"
    assert "WOUND" not in ratings

    deck = (
        [card("CARD.BATTLE_TRANCE", cost=0, ctype="Skill") for _ in range(5)]
        + [card("CARD.WOUND") for _ in range(5)]
    )

    metrics = scoring.deck_quality_metrics(deck)
    assert 0.2 < metrics["quality"] < 1.0


def test_curve_axis_peaks_at_target_avg_cost():
    import agent.card_scoring as scoring

    deck = [
        card("CARD.A", cost=1), card("CARD.B", cost=1),
        card("CARD.C", cost=2), card("CARD.D", cost=3),
    ]  # avg cost = (1+1+2+3)/4 = 1.75

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["avg_cost"] == pytest.approx(1.75)
    assert metrics["curve_pass"] == pytest.approx(1.0)


def test_curve_axis_zero_at_avg_cost_0():
    import agent.card_scoring as scoring

    deck = [card("CARD.A", cost=0), card("CARD.B", cost=0)]

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["avg_cost"] == pytest.approx(0.0)
    assert metrics["curve_pass"] == pytest.approx(0.0)


def test_curve_axis_zero_at_avg_cost_3_5():
    import agent.card_scoring as scoring

    deck = [card("CARD.A", cost=3), card("CARD.B", cost=4)]

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["avg_cost"] == pytest.approx(3.5)
    assert metrics["curve_pass"] == pytest.approx(0.0)


def test_curve_axis_ignores_non_numeric_costs():
    import agent.card_scoring as scoring

    deck = [
        card("CARD.A", cost=1), card("CARD.B", cost=2),
        card("CARD.X_COST", cost="X"),
    ]  # avg cost over numeric-cost cards only = (1+2)/2 = 1.5

    metrics = scoring.deck_quality_metrics(deck)
    assert metrics["avg_cost"] == pytest.approx(1.5)


def test_overall_reduces_to_old_formula_when_quality_and_curve_weights_zeroed(
        monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(scoring, "QUALITY_WEIGHT", 0.0)
    monkeypatch.setattr(scoring, "CURVE_WEIGHT", 0.0)

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

    metrics = scoring.deck_quality_metrics(deck)
    expected = (
        0.35 * metrics["burst_pass"] + 0.30 * metrics["block_pass"] +
        0.20 * metrics["engine_pass"] + 0.15 * metrics["boss_pass"]
    )
    assert metrics["overall"] == pytest.approx(expected)


# --- Override cap (STS2_OVERRIDE_DELTA_CAP / _OVERRIDE_DELTA_CAP) --------

def _juggernaut_card():
    return card("CARD.JUGGERNAUT", cost=2, ctype="Power", rarity="Rare",
                description="Whenever you gain Block, deal that much damage "
                            "to a random enemy.")


def test_override_cap_zero_trusts_advisor_tier_completely(monkeypatch):
    import agent.card_scoring as scoring

    # JUGGERNAUT has both an advisor tier (D) and an OVERRIDES entry (8.5) —
    # verify rather than assume.
    rating = scoring._load_advisor_ratings()["JUGGERNAUT"]
    assert rating["tier"] == "D"
    assert "JUGGERNAUT" in scoring.OVERRIDES

    monkeypatch.setattr(scoring, "_OVERRIDE_DELTA_CAP", 0.0)

    juggernaut = _juggernaut_card()
    expected = scoring._TIER_BASE["D"] + scoring._context_bonus(juggernaut)
    assert scoring.score_card(juggernaut) == pytest.approx(
        max(0.0, min(expected, 10.0)))


def test_override_cap_default_applies_clamped_delta(monkeypatch):
    import agent.card_scoring as scoring

    rating = scoring._load_advisor_ratings()["JUGGERNAUT"]
    assert rating["tier"] == "D"

    monkeypatch.setattr(scoring, "_OVERRIDE_DELTA_CAP", 2.0)

    juggernaut = _juggernaut_card()
    raw_delta = scoring.OVERRIDES["JUGGERNAUT"] - scoring._TIER_BASE["D"]
    clamped_delta = max(-2.0, min(2.0, raw_delta))
    # The override (8.5) is far above the D anchor (2.0), so the delta must
    # actually be clamped for this test to exercise the cap.
    assert raw_delta > 2.0
    assert clamped_delta == pytest.approx(2.0)

    expected = (scoring._TIER_BASE["D"] + scoring._context_bonus(juggernaut)
                + clamped_delta)
    assert scoring.score_card(juggernaut) == pytest.approx(
        max(0.0, min(expected, 10.0)))


# --- HP-loss predictor (STS2_PREDICTOR_TARGET / agent/train_deck_predictor_hp.py) --

def _hp_pick(floor, hp, max_hp=80, ts=0.0):
    return {"floor": floor, "hp": hp, "max_hp": max_hp, "ts": ts, "picked": "SOME_CARD"}


def test_hp_loss_label_forward_observation_is_hand_computable():
    from agent.train_deck_predictor_hp import compute_hp_loss_labels

    picks_by_run = {
        "R": [
            _hp_pick(floor=2, hp=70, ts=1),
            _hp_pick(floor=6, hp=55, ts=2),
            _hp_pick(floor=10, hp=40, ts=3),
        ],
    }
    outcomes = {"R": 20}  # run survives well past any K=5 window

    labels, stats = compute_hp_loss_labels(picks_by_run, outcomes, k_floors=5)

    # pick0 (F=2): window is floor<=7 -> last qualifying later pick is
    # pick1 (floor 6); pick2 (floor 10) is outside the window.
    assert labels[("R", 0)] == pytest.approx((70 - 55) / 80 / (6 - 2))
    # pick1 (F=6): window is floor<=11 -> last qualifying later pick is pick2.
    assert labels[("R", 1)] == pytest.approx((55 - 40) / 80 / (10 - 6))
    # pick2 (F=10): window is floor<=15, no later picks, run doesn't end in
    # window (max_floor=20) -> no usable label.
    assert ("R", 2) not in labels
    assert stats == {"death_window": 0, "normal": 2, "skipped_no_window": 1}


def test_hp_loss_label_death_window_counts_remaining_hp_as_lost():
    from agent.train_deck_predictor_hp import compute_hp_loss_labels

    picks_by_run = {"R": [_hp_pick(floor=3, hp=70, ts=1)]}
    outcomes = {"R": 6}  # dies at floor 6, within F+K = 3+5 = 8

    labels, stats = compute_hp_loss_labels(picks_by_run, outcomes, k_floors=5)

    assert labels[("R", 0)] == pytest.approx((70 / 80) / max(6 - 3, 1))
    assert stats["death_window"] == 1


def test_hp_loss_label_death_window_overrides_a_later_observation():
    """A card_pick recorded right before death typically shows little/no HP
    change — using it naively would make dying look free (the exact failure
    mode the death-window branch exists to prevent). It must win even when a
    later in-window observation also exists."""
    from agent.train_deck_predictor_hp import compute_hp_loss_labels

    picks_by_run = {
        "R": [
            _hp_pick(floor=3, hp=70, ts=1),
            _hp_pick(floor=4, hp=69, ts=2),  # barely any HP lost right before death
        ],
    }
    outcomes = {"R": 6}  # dies at floor 6, within F+K = 3+5 = 8 for pick0

    labels, stats = compute_hp_loss_labels(picks_by_run, outcomes, k_floors=5)

    # Without the override this would read (70-69)/80/(4-3) = 0.0125.
    assert labels[("R", 0)] == pytest.approx((70 / 80) / max(6 - 3, 1))
    # pick1 (F=4) is itself within its own death window (4+5=9 >= 6), so both
    # picks land in the death branch and none in the forward-observation one.
    assert stats["death_window"] == 2
    assert stats["normal"] == 0


def test_hp_loss_label_numerator_clamped_at_zero_on_hp_gain():
    from agent.train_deck_predictor_hp import compute_hp_loss_labels

    picks_by_run = {
        "R": [
            _hp_pick(floor=2, hp=50, ts=1),
            _hp_pick(floor=5, hp=60, ts=2),  # healed between picks
        ],
    }
    outcomes = {"R": 20}

    labels, _ = compute_hp_loss_labels(picks_by_run, outcomes, k_floors=5)

    assert labels[("R", 0)] == pytest.approx(0.0)


def test_hp_predictor_sign_lower_predicted_loss_gets_positive_bonus(monkeypatch):
    import numpy as np

    import agent.card_scoring as scoring

    class _FakeHpPipe:
        def predict(self, X):
            return np.array([0.30, 0.10])  # card A worse, card B better

    monkeypatch.setattr(scoring, "PREDICTOR_TARGET", "hp")
    monkeypatch.setattr(scoring, "_load_hp_predictor", lambda: _FakeHpPipe())
    monkeypatch.setattr(scoring, "_HP_PREDICTOR_VOCAB", {})

    deck = [{"id": "CARD.STRIKE_IRONCLAD"}] * 5
    cards = [
        {"id": "CARD.A", "cost": 1, "type": "Attack", "rarity": "Common"},
        {"id": "CARD.B", "cost": 1, "type": "Skill", "rarity": "Common"},
    ]
    set_mc_context(hp=60, max_hp=80, floor=5)

    bonuses = scoring.predictor_v2_set_bonuses(cards, deck)

    # Card B has the lower predicted loss -> POSITIVE bonus. Card A is above
    # the set mean -> negative bonus. This is the sign flip vs the floor
    # predictor (which rewards ABOVE the mean, since higher is better there).
    mean = (0.30 + 0.10) / 2
    assert bonuses[1] == pytest.approx((mean - 0.10) * scoring.HP_PREDICTOR_WEIGHT)
    assert bonuses[0] == pytest.approx((mean - 0.30) * scoring.HP_PREDICTOR_WEIGHT)
    assert bonuses[1] > 0
    assert bonuses[0] < 0


def test_predictor_target_defaults_to_floor_and_never_touches_hp_loader(monkeypatch):
    import agent.card_scoring as scoring

    assert scoring.PREDICTOR_TARGET == "floor"

    def _boom():
        raise AssertionError("hp loader must not be called when target is floor")

    monkeypatch.setattr(scoring, "_load_hp_predictor", _boom)

    deck = [{"id": "CARD.STRIKE_IRONCLAD"}] * 5
    card = {"id": "CARD.BASH", "cost": 2, "type": "Attack", "rarity": "Common"}

    # Neither entry point may touch the hp loader on the default target —
    # if either did, the monkeypatched _boom would raise.
    scoring.predictor_lift_bonus(card, deck)
    scoring.predictor_v2_set_bonuses([card], deck)


def test_hp_predictor_missing_pkl_falls_back_to_floor_behavior_silently(
        monkeypatch, tmp_path):
    import agent.card_scoring as scoring

    deck = [{"id": "CARD.STRIKE_IRONCLAD"}] * 5
    card = {"id": "CARD.BASH", "cost": 2, "type": "Attack", "rarity": "Common"}

    monkeypatch.setattr(scoring, "PREDICTOR_TARGET", "floor")
    floor_bonus = scoring.predictor_lift_bonus(card, deck)

    monkeypatch.setattr(scoring, "PREDICTOR_TARGET", "hp")
    monkeypatch.setattr(scoring, "_HP_PREDICTOR_PATH",
                        str(tmp_path / "does_not_exist.pkl"))
    monkeypatch.setattr(scoring, "_HP_PREDICTOR_LOADED", False)

    # Missing file -> loader returns None quietly, no exception.
    assert scoring._load_hp_predictor() is None
    hp_bonus = scoring.predictor_lift_bonus(card, deck)
    assert hp_bonus == floor_bonus


def test_hp_predictor_mismatched_target_key_falls_back_silently(monkeypatch, tmp_path):
    import pickle

    import agent.card_scoring as scoring

    bad_pkl = tmp_path / "wrong_target.pkl"
    with open(bad_pkl, "wb") as f:
        pickle.dump({"pipeline": object(), "target": "run_max_floor"}, f)

    monkeypatch.setattr(scoring, "_HP_PREDICTOR_PATH", str(bad_pkl))
    monkeypatch.setattr(scoring, "_HP_PREDICTOR_LOADED", False)

    assert scoring._load_hp_predictor() is None


# --- HP predictor axis features (agent/train_deck_predictor_hp.py's
# _deck_axis_counts/_cand_axis_onehot + agent/card_scoring.py's
# _hp_axis_features must stay in lockstep) ----------------------------------

def test_train_deck_axis_counts_sums_over_deck():
    from agent.train_deck_predictor_hp import _deck_axis_counts

    axis_by_id = {
        "STRIKE_IRONCLAD": {"DAMAGE", "STARTER"},
        "CINDER": {"DAMAGE", "RANDOM"},
    }
    axis_idx = {"DAMAGE": 0, "STARTER": 1, "RANDOM": 2}
    deck_ids = ["STRIKE_IRONCLAD", "STRIKE_IRONCLAD", "CINDER"]

    counts = _deck_axis_counts(deck_ids, axis_by_id, axis_idx)

    # DAMAGE: 2x strike + 1x cinder = 3; STARTER: 2x strike = 2; RANDOM: 1x cinder = 1
    assert counts == [3.0, 2.0, 1.0]


def test_train_deck_axis_counts_ignores_unknown_axes_and_cards():
    from agent.train_deck_predictor_hp import _deck_axis_counts

    axis_by_id = {"STRIKE_IRONCLAD": {"DAMAGE", "NOT_IN_VOCAB"}}
    axis_idx = {"DAMAGE": 0}

    counts = _deck_axis_counts(["STRIKE_IRONCLAD", "UNKNOWN_CARD"], axis_by_id, axis_idx)

    assert counts == [1.0]  # NOT_IN_VOCAB axis and UNKNOWN_CARD both silently skipped


def test_train_cand_axis_onehot_marks_candidates_axes():
    from agent.train_deck_predictor_hp import _cand_axis_onehot

    axis_by_id = {"THUNDERCLAP": {"AOE", "VULN"}}
    axis_idx = {"AOE": 0, "VULN": 1, "DAMAGE": 2}

    oh = _cand_axis_onehot({"id": "THUNDERCLAP"}, axis_by_id, axis_idx)

    assert oh == [1.0, 1.0, 0.0]


def test_train_cand_axis_onehot_unknown_card_is_all_zero():
    from agent.train_deck_predictor_hp import _cand_axis_onehot

    oh = _cand_axis_onehot({"id": "NOT_A_REAL_CARD"}, {}, {"AOE": 0})

    assert oh == [0.0]


def test_hp_axis_features_deck_counts_and_cand_onehot(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(scoring, "_ADV_AXES_CACHE", {
        "STRIKE_IRONCLAD": {"DAMAGE", "STARTER"},
        "THUNDERCLAP": {"AOE", "VULN"},
    })
    deck = [{"id": "CARD.STRIKE_IRONCLAD"}] * 3
    candidate = {"id": "CARD.THUNDERCLAP"}
    axis_vocab = ["AOE", "DAMAGE", "STARTER", "VULN"]

    feats = scoring._hp_axis_features(deck, candidate, axis_vocab)

    assert len(feats) == 2 * len(axis_vocab)
    deck_part, cand_part = feats[:len(axis_vocab)], feats[len(axis_vocab):]
    # deck: 3x STRIKE_IRONCLAD -> DAMAGE=3, STARTER=3, AOE=0, VULN=0
    assert deck_part == [0.0, 3.0, 3.0, 0.0]
    # candidate: THUNDERCLAP -> AOE=1, VULN=1, DAMAGE=0, STARTER=0
    assert cand_part == [1.0, 0.0, 0.0, 1.0]


def test_hp_axis_features_empty_vocab_is_empty():
    import agent.card_scoring as scoring

    feats = scoring._hp_axis_features([{"id": "CARD.STRIKE_IRONCLAD"}],
                                       {"id": "CARD.THUNDERCLAP"}, [])
    assert feats == []


def test_hp_set_bonuses_feature_vector_includes_axis_dims(monkeypatch):
    """End-to-end: _hp_set_bonuses must build a feature vector whose width
    accounts for the axis features (base-45 + 2*card_ids + top_relics +
    floor_buckets + 2*axis_vocab), not just v2's base schema."""
    import numpy as np

    import agent.card_scoring as scoring

    captured = {}

    class _FakeHpPipe:
        def predict(self, X):
            captured["shape"] = X.shape
            return np.zeros(X.shape[0])

    monkeypatch.setattr(scoring, "_HP_PREDICTOR_VOCAB", {
        "card_ids": ["STRIKE_IRONCLAD"],
        "top_relics": [],
        "floor_buckets": [],
        "axis_vocab": ["DAMAGE", "AOE"],
    })
    monkeypatch.setattr(scoring, "_ADV_AXES_CACHE", {
        "STRIKE_IRONCLAD": {"DAMAGE"},
        "BASH": {"DAMAGE", "AOE"},
    })
    deck = [{"id": "CARD.STRIKE_IRONCLAD"}] * 5
    cards = [{"id": "CARD.BASH", "cost": 2, "type": "Attack", "rarity": "Common"}]
    set_mc_context(hp=60, max_hp=80, floor=5)

    scoring._hp_set_bonuses(cards, deck, _FakeHpPipe())

    # 45 base+interaction + 2*1 card_ids + 0 relics + 0 floor + 2*2 axis = 51
    assert captured["shape"] == (1, 51)


def test_hp_vocab_consistent_true_when_dims_match():
    import agent.card_scoring as scoring

    blob = {
        "feature_names": ["f"] * (45 + 2 * 3 + 5 + 2 + 2 * 4),
        "card_ids": ["A", "B", "C"],
        "top_relics": ["R1", "R2", "R3", "R4", "R5"],
        "floor_buckets": [3, 4],
        "axis_vocab": ["AX1", "AX2", "AX3", "AX4"],
    }
    assert scoring._hp_vocab_consistent(blob) is True


def test_hp_vocab_consistent_false_when_axis_vocab_mismatches_feature_names():
    import agent.card_scoring as scoring

    blob = {
        # feature_names length was computed WITHOUT axis dims (stale/legacy
        # shape) but axis_vocab is non-empty -> must be flagged inconsistent.
        "feature_names": ["f"] * (45 + 2 * 3 + 5 + 2),
        "card_ids": ["A", "B", "C"],
        "top_relics": ["R1", "R2", "R3", "R4", "R5"],
        "floor_buckets": [3, 4],
        "axis_vocab": ["AX1", "AX2", "AX3", "AX4"],
    }
    assert scoring._hp_vocab_consistent(blob) is False


def test_hp_vocab_consistent_true_when_feature_names_absent():
    import agent.card_scoring as scoring

    # Old-format pickles (predating the "feature_names" key entirely) can't
    # be checked -> treated as consistent so they keep working.
    assert scoring._hp_vocab_consistent({"card_ids": ["A"]}) is True


def test_load_hp_predictor_rejects_pickle_with_inconsistent_vocab(monkeypatch, tmp_path):
    import pickle

    import agent.card_scoring as scoring

    bad_pkl = tmp_path / "bad_vocab.pkl"
    with open(bad_pkl, "wb") as f:
        pickle.dump({
            "pipeline": object(),
            "target": "hp_loss_per_floor",
            "feature_names": ["f"] * 10,  # does not match vocab sizes below
            "card_ids": ["A", "B", "C"],
            "top_relics": [],
            "floor_buckets": [],
            "axis_vocab": ["AX1", "AX2"],
        }, f)

    monkeypatch.setattr(scoring, "_HP_PREDICTOR_PATH", str(bad_pkl))
    monkeypatch.setattr(scoring, "_HP_PREDICTOR_LOADED", False)

    assert scoring._load_hp_predictor() is None


def test_energy_value_bonus_prefers_cheaper_equivalent_cards():
    """The Act 3 boss is energy-bound: 3.3 energy/turn against 24.9 incoming
    while needing ~40 damage/turn. Card scoring anchors on `tier`, which is
    pool-relative power and measured Spearman −0.040 against value-per-energy —
    i.e. it cannot see cost-efficiency at all. Unlike block-bias or deck
    trimming (both of which cost arrival rate by optimising FOR the boss),
    cheap-and-effective is a general virtue, so it should not carry that trap.
    """
    from agent import card_scoring

    # A 5-block 1-cost card is more energy-efficient than a 2-cost 8-damage one.
    assert card_scoring._energy_value_bonus("DEFEND_IRONCLAD") > \
        card_scoring._energy_value_bonus("BASH")



def test_orbs_archetype_recognises_defect_orb_cards(monkeypatch):
    """Every existing archetype is an Ironclad one (vulnerable, exhaust,
    self_damage, strikes, infinite_strike, high_cost, rampage), so for Defect —
    the only character that actually reaches the Act 3 boss — the drafter is
    inert.

    That matters because the boss is energy-bound: 3.3 energy/turn against 24.9
    incoming while needing ~40 damage/turn. Orbs are the one mechanic that pays
    out every turn WITHOUT costing energy each turn, so an orb-coherent deck is
    the mechanism that relaxes the actual binding constraint. The advisor data
    already carries the vocabulary (ORB_PRODUCER 22 cards, ORB_EVOKE, FOCUS...).
    """
    from agent import card_scoring

    zap = {"id": "ZAP", "description": {"en": "Channel 1 Lightning."}}
    bash = {"id": "BASH", "description": {"en": "Deal 8 damage."}}

    assert card_scoring._archetype_member(zap, "ZAP", "orbs") is True
    assert card_scoring._archetype_member(bash, "BASH", "orbs") is False


def test_orbs_archetype_treats_focus_as_a_keystone():
    from agent import card_scoring

    defrag = {"id": "DEFRAGMENT", "description": {"en": "Gain 1 Focus."}}
    assert card_scoring._archetype_keystone(defrag, "DEFRAGMENT", "orbs") is True
