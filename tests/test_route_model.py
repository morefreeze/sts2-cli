"""Phase 3b Task 2 (+ Task 4's CLI test): route value model, transition model, JSON.

Hermetic: games are built directly as agent.route_data.load_games-style dicts.
"""
import json
import math
import os
import random

import numpy as np
import pytest

from agent import route_data
from agent.route_model import (
    ACT_FLOORS, CELL_LEVELS, DELTA_KEYS, FULL_FEATURES, HP_ONLY_FEATURES, RouteModel,
    death_prob_from_samples, feature_vector, fit_transition_model, fit_value_model)


def _feat(floor=1, act=1, hp=80, max_hp=80, gold=0, burst=0.5, top8=6.0, deck_size=15, n_up=0,
          n_rel=1, n_pot=0):
    return {"hp": hp, "max_hp": max_hp, "gold": gold, "deck_burst": burst, "deck_top8": top8,
            "deck_size": deck_size, "n_upgraded": n_up, "n_relics": n_rel, "n_potions": n_pot,
            "act": act, "floor": floor, "gf": (act - 1) * 17 + floor}


def _game(final_gf, snapshots, transitions=(), character="Defect", ascension=1, seed="s"):
    return {"character": character, "ascension": ascension, "seed": seed,
            "won": False, "final_gf": final_gf,
            "snapshots": [{"features": f, "choice_type": "Monster"} for f in snapshots],
            "transitions": list(transitions)}


def _trans(room_type, pre, post=None, died=False, act=None):
    """A transition; `post` overrides individual pre fields (None -> unchanged)."""
    pre = dict(pre)
    post_state = dict(pre, **(post or {})) if not died else dict(pre, hp=0)
    return {"act": act if act is not None else pre["act"], "room_type": room_type,
            "pre": pre, "post": post_state, "died": died}


# ------------------------------------------------------- death_prob_from_samples
#
# P(loss >= h) = (#survivors with pre >= h and loss >= h + #deaths with pre >= h)
#              / (#survivors with pre >= h + #deaths with pre >= h)
# survivors are (pre_hp, loss) pairs, deaths are pre_hp values.

def test_death_prob_is_conditional_on_the_room_being_entered_with_at_least_h():
    # h = 6, three survivors that all entered with 50: 1 of 3 lost >= 6 (the loss of 10).
    assert death_prob_from_samples([(50, 0), (50, 5), (50, 10)], [], 6) == pytest.approx(1 / 3)
    # A survivor that entered with 4 cannot have lost 6 and says nothing about h = 6: it is
    # left OUT of the denominator (2 remain, 1 of them lost >= 6 -> 1/2; counting it would
    # dilute the estimate to 1/3).
    assert death_prob_from_samples([(4, 3), (50, 0), (50, 10)], [], 6) == pytest.approx(1 / 2)
    # pre == h is included (pre >= h) and a loss of exactly h counts (loss >= h):
    # (6, 5) entered with 6 but lost 5 -> no; (6, 6) lost 6 -> yes; 1 of 2.
    assert death_prob_from_samples([(6, 5), (6, 6)], [], 6) == pytest.approx(1 / 2)
    # nobody entered with >= h (the death at 3 is uninformative too) -> denominator 0
    assert death_prob_from_samples([(5, 4)], [3], 6) == 0.0


def test_death_prob_counts_deaths_that_entered_with_at_least_h():
    # h = 6: deaths at 8 and 9 both imply loss >= 6; one survivor lost 0 of its 50 hp:
    # (0 + 2 deaths) / (1 survivor + 2 deaths) = 2/3.
    assert death_prob_from_samples([(50, 0)], [8, 9], 6) == pytest.approx(2 / 3)
    # A death at pre-hp 3 (< 6) is uninformative for h = 6: denominator 2, numerator 0.
    assert death_prob_from_samples([(50, 0), (50, 5)], [3], 6) == 0.0
    # A death at exactly 6 counts: (0 + 1) / (2 + 1) = 1/3.
    assert death_prob_from_samples([(50, 0), (50, 5)], [6], 6) == pytest.approx(1 / 3)
    assert death_prob_from_samples([], [8, 9], 6) == 1.0


def test_death_prob_at_the_documented_boundaries():
    assert death_prob_from_samples([(50, 1), (50, 2)], [5], 0) == 1.0
    assert death_prob_from_samples([(50, 1), (50, 2)], [5], -3) == 1.0
    assert death_prob_from_samples([], [], 10) == 0.0


def _reference_death_prob(survivors, deaths, h):
    """The estimator straight from its definition, O(n)."""
    if h <= 0:
        return 1.0
    entered = [(pre, loss) for pre, loss in survivors if pre >= h]
    dead = [d for d in deaths if d >= h]
    denom = len(entered) + len(dead)
    if denom == 0:
        return 0.0
    return (sum(1 for _, loss in entered if loss >= h) + len(dead)) / denom


def test_death_prob_matches_the_definition_on_random_samples():
    rng = random.Random(7)
    for _ in range(40):
        survivors = [(rng.randint(1, 80), rng.randint(0, 60)) for _ in range(rng.randint(0, 40))]
        deaths = [rng.randint(1, 80) for _ in range(rng.randint(0, 10))]
        for h in (0.5, 5, 12.5, 30, 47, 80, 81):
            assert death_prob_from_samples(survivors, deaths, h) == pytest.approx(
                _reference_death_prob(survivors, deaths, h)), (survivors, deaths, h)


def test_the_conditional_estimate_removes_the_low_bias_of_a_plain_loss_histogram():
    # Elite samples: one fight entered at 45 hp cost 30; two entered at 20 / 25 hp cost 5.
    # At h = 30 only the first could have lost 30 -> q = 1/1. A loss histogram over ALL
    # three survivors (the estimator this replaces) would say 1/3.
    survivors = [(45, 30), (20, 5), (25, 5)]
    assert death_prob_from_samples(survivors, [], 30) == pytest.approx(1.0)


# ------------------------------------------------------------------ value model

def test_feature_vector_derives_hp_frac_act_flags_and_character_one_hot():
    f = _feat(floor=4, act=3, hp=40, max_hp=80)
    vec = feature_vector(f, "Regent", ["hp_frac", "act2", "act3", "floor",
                                       "char_Regent", "char_Silent"])
    assert vec == [0.5, 0.0, 1.0, 4.0, 1.0, 0.0]
    assert feature_vector(_feat(act=2), "Defect", ["act2", "act3"]) == [1.0, 0.0]
    assert feature_vector(_feat(), None, ["char_Ironclad"]) == [0.0]


def _deck_games(n_games, rng, final_of, per_game):
    """Games of 10 snapshots (floors 1..10) whose per-game features come from
    per_game(rng) and whose final floor is final_of(features, rng)."""
    games = []
    for i in range(n_games):
        base = per_game(rng)
        snaps = [dict(base, floor=fl, gf=fl) for fl in range(1, 11)]
        games.append(_game(final_of(base, rng), snaps, seed=f"g{i}"))
    return games


def test_fit_value_model_chooses_full_when_the_deck_matters():
    rng = np.random.default_rng(0)
    games = _deck_games(
        40, rng,
        # y = final_gf - floor = 30 - floor + 0.3 * deck_burst * 100, small noise
        final_of=lambda b, r: round(30 + 0.3 * b["deck_burst"] * 100 + r.normal(0, 0.3), 3),
        per_game=lambda r: _feat(burst=float(r.uniform(0.2, 0.9)), top8=float(r.uniform(4, 9)),
                                 gold=int(r.integers(0, 300)),
                                 n_rel=int(r.integers(0, 6))))
    fit = fit_value_model(games)
    assert fit["chosen"] == "full"
    assert np.mean(fit["cv"]["full"]) < np.mean(fit["cv"]["hp_only"])
    assert fit["n_games"] == 40 and fit["n_samples"] == 400
    assert len(fit["cv"]["full"]) == len(fit["cv"]["hp_only"]) == 5
    assert fit["model"]["features"] == FULL_FEATURES


def test_fit_value_model_falls_back_to_hp_only_when_extra_features_are_noise():
    rng = np.random.default_rng(1)
    games = _deck_games(
        25, rng,
        final_of=lambda b, r: round(30 + 0.1 * b["hp"] + r.normal(0, 3.0), 3),
        per_game=lambda r: _feat(hp=int(r.integers(20, 81)), burst=float(r.uniform(0.2, 0.9)),
                                 top8=float(r.uniform(4, 9)), gold=int(r.integers(0, 300)),
                                 n_rel=int(r.integers(0, 6)), n_pot=int(r.integers(0, 4)),
                                 deck_size=int(r.integers(10, 30)), n_up=int(r.integers(0, 8))))
    fit = fit_value_model(games)
    assert fit["chosen"] == "hp_only"
    assert fit["model"]["features"] == HP_ONLY_FEATURES
    # the recovered model is usable: more HP -> more expected floors
    model = RouteModel({"value": fit["model"], "transitions": {"split": 0.5, "min_n": 20, "cells": {}}})
    assert model.value(_feat(hp=80), "Defect") > model.value(_feat(hp=20), "Defect")


def test_fit_value_model_needs_two_cv_groups():
    game = _game(5, [_feat()])
    with pytest.raises(ValueError, match="2 CV groups"):
        fit_value_model([game])
    # two games of ONE seed are still a single group
    with pytest.raises(ValueError, match="2 CV groups"):
        fit_value_model([_game(5, [_feat()], seed="same"), _game(6, [_feat()], seed="same")])


def test_cv_groups_by_seed_so_a_seed_never_straddles_train_and_test(monkeypatch):
    from sklearn.model_selection import GroupKFold
    seen = []

    class _Spy(GroupKFold):
        def split(self, X, y=None, groups=None):
            for train, test in super().split(X, y, groups):
                seen.append((set(groups[train]), set(groups[test])))
                yield train, test

    monkeypatch.setattr("sklearn.model_selection.GroupKFold", _Spy)
    # 6 seeds x 3 characters = 18 games; the same seed appears under every character
    games = []
    for seed_i in range(6):
        for character in ("Defect", "Silent", "Ironclad"):
            base = _feat(hp=40 + 5 * seed_i)
            snaps = [dict(base, floor=fl, gf=fl) for fl in range(1, 4)]
            games.append(_game(8 + seed_i, snaps, character=character, seed=f"seed{seed_i}"))
    fit = fit_value_model(games)
    assert fit["n_games"] == 18 and fit["n_groups"] == 6
    assert len(seen) == 5 and all(train.isdisjoint(test) for train, test in seen)
    assert sorted(g for _, test in seen for g in test) == list(range(6))  # each seed tested once


def test_a_game_without_a_seed_is_its_own_cv_group():
    games = [dict(_game(5 + i, [_feat(floor=1), _feat(floor=2)]), seed=None) for i in range(4)]
    fit = fit_value_model(games, folds=4)
    assert fit["n_groups"] == 4 and len(fit["cv"]["full"]) == 4


def test_cv_falls_back_to_unshuffled_group_kfold_when_shuffle_is_unsupported(monkeypatch):
    from sklearn.model_selection import GroupKFold

    class _OldGroupKFold:  # scikit-learn < 1.6: no shuffle / random_state
        def __init__(self, n_splits=5):
            self._inner = GroupKFold(n_splits=n_splits)

        def split(self, X, y=None, groups=None):
            return self._inner.split(X, y, groups)

    monkeypatch.setattr("sklearn.model_selection.GroupKFold", _OldGroupKFold)
    games = [_game(5 + i, [_feat(floor=1, hp=40 + i), _feat(floor=2, hp=40 + i)], seed=f"s{i}")
             for i in range(6)]
    fit = fit_value_model(games, folds=3)
    assert len(fit["cv"]["full"]) == 3 and fit["n_groups"] == 6


# ------------------------------------------------------------- RouteModel / JSON

def _value_dict(features=("hp",), coef=(0.1,), intercept=0.0, mean=None, scale=None):
    n = len(features)
    return {"features": list(features), "mean": list(mean or [0.0] * n),
            "scale": list(scale or [1.0] * n), "coef": list(coef), "intercept": intercept}


def _cell(n=30, n_died=0, survivors=(), death_hp=(), **delta):
    """A transition cell; survivors = (pre_hp, hp_lost) pairs, death_hp = pre-room hp of deaths."""
    return {"n": n, "n_died": n_died,
            "mean_delta": {k: float(delta.get(k, 0.0)) for k in DELTA_KEYS},
            "survivors": [list(p) for p in survivors], "death_hp": list(death_hp)}


def _model(cells=None, value=None, split=0.5, min_n=20):
    return RouteModel({"version": 1, "value": value or _value_dict(),
                       "transitions": {"split": split, "min_n": min_n, "cells": cells or {}},
                       "meta": {}, "validation": {}})


def test_save_load_round_trip_preserves_value_exactly(tmp_path):
    rng = np.random.default_rng(2)
    games = _deck_games(
        10, rng,
        final_of=lambda b, r: round(25 + b["hp"] * 0.1 + r.normal(0, 1), 3),
        per_game=lambda r: _feat(hp=int(r.integers(20, 81)), burst=float(r.uniform(0.2, 0.9)), top8=float(r.uniform(4, 9))))
    data = {"version": 1, "value": fit_value_model(games)["model"],
            "transitions": fit_transition_model(games), "meta": {"n_games": 10}, "validation": {}}
    model = RouteModel(data)
    f = _feat(floor=7, hp=51, burst=0.37, gold=123)
    before = model.value(f, "Defect")
    path = str(tmp_path / "sub" / "m.json")
    model.save(path)
    assert RouteModel.load(path).value(f, "Defect") == pytest.approx(before, abs=1e-9)
    text = open(path).read()
    assert json.loads(text) == json.loads(json.dumps(data))
    assert text.startswith("{\n ")  # indent 1


def test_value_is_the_ridge_prediction_and_never_negative():
    m = _model(value=_value_dict(("hp",), (10.0,), intercept=5.0, mean=[50.0], scale=[10.0]))
    assert m.value(_feat(hp=70), "Defect") == pytest.approx(5.0 + 10.0 * 2.0)
    assert m.value(_feat(hp=0), "Defect") == 0.0  # 5 + 10 * -5 < 0 -> clamped


def test_cell_uses_the_exact_act_cell_when_it_has_enough_samples():
    exact = _cell(n=25, hp=-7.0)
    pooled = _cell(n=90, hp=-3.0)
    m = _model({"2|Monster|hi": exact, "*|Monster|hi": pooled, "*|Monster|all": _cell(n=200)})
    assert m.cell(2, "Monster", _feat(burst=0.9)) is exact
    assert m.cell(3, "Monster", _feat(burst=0.9)) is pooled  # no act-3 cell -> pooled over acts


def test_cell_falls_back_when_a_cell_is_below_min_n():
    thin = _cell(n=19, hp=-30.0)
    pooled = _cell(n=90, hp=-3.0)
    m = _model({"2|Elite|hi": thin, "*|Elite|hi": pooled})
    assert m.cell(2, "Elite", _feat(burst=0.9)) is pooled


def test_cell_fallback_chain_ends_in_the_all_bucket_then_a_zero_cell():
    only_pooled_hi = _cell(n=50, hp=-15.0)
    m = _model({"*|Elite|hi": only_pooled_hi})
    assert m.cell(2, "Elite", _feat(burst=0.9)) is only_pooled_hi
    all_bucket = _cell(n=50, hp=-9.0)
    m2 = _model({"*|Elite|all": all_bucket})
    assert m2.cell(2, "Elite", _feat(burst=0.1)) is all_bucket  # lo bucket missing -> all
    unknown = m.cell(2, "Mystery", _feat())
    assert unknown["n"] == 0 and unknown["survivors"] == [] and unknown["death_hp"] == []
    assert all(v == 0.0 for v in unknown["mean_delta"].values())
    assert m.death_prob(2, "Mystery", _feat(hp=5)) == 0.0
    stepped = m.step(2, "Mystery", _feat(floor=4, hp=33, gold=10), 4)
    assert (stepped["floor"], stepped["gf"]) == (5, ACT_FLOORS + 5)  # row 4 of act 2
    assert all(stepped[k] == _feat(floor=4, hp=33, gold=10)[k] for k in DELTA_KEYS)


def test_cell_bucketing_uses_deck_burst_for_fights_and_hp_frac_for_rest():
    m = _model({"1|Monster|lo": _cell(n=30, hp=-9.0), "1|Monster|hi": _cell(n=30, hp=-3.0),
                "1|RestSite|lo": _cell(n=30, hp=25.0), "1|RestSite|hi": _cell(n=30, hp=8.0),
                "1|Shop|all": _cell(n=30, gold=-60.0)}, split=0.4)
    assert m.cell(1, "Monster", _feat(burst=0.39))["mean_delta"]["hp"] == -9.0
    assert m.cell(1, "Monster", _feat(burst=0.4))["mean_delta"]["hp"] == -3.0
    assert m.cell(1, "RestSite", _feat(hp=39, max_hp=80))["mean_delta"]["hp"] == 25.0
    assert m.cell(1, "RestSite", _feat(hp=40, max_hp=80))["mean_delta"]["hp"] == 8.0
    assert m.cell(1, "Shop", _feat())["mean_delta"]["gold"] == -60.0


def test_death_prob_reads_the_cell_samples_at_the_current_hp():
    # survivors entered with 60 and lost 10/20/30/40; one death entered with 35.
    m = _model({"*|Elite|all": _cell(n=40, n_died=1, death_hp=[35],
                                     survivors=[(60, 10), (60, 20), (60, 30), (60, 40)])})
    # h = 25: all 4 survivors + the death (35 >= 25) entered with >= 25; losses 30, 40 and
    # the death are >= 25 -> (2 + 1) / (4 + 1)
    assert m.death_prob(1, "Elite", _feat(hp=25)) == pytest.approx(3 / 5)
    # h = 50: the death (35 < 50) is uninformative; 4 survivors, none lost >= 50 -> 0
    assert m.death_prob(1, "Elite", _feat(hp=50)) == pytest.approx(0.0)
    # h = 35: the death counts (35 >= 35): only the loss of 40 is >= 35 -> (1 + 1) / (4 + 1)
    assert m.death_prob(1, "Elite", _feat(hp=35)) == pytest.approx(2 / 5)
    assert m.death_prob(1, "Elite", _feat(hp=0)) == 1.0


def test_model_death_prob_agrees_with_the_definition_and_stays_consistent_when_cached():
    rng = random.Random(3)
    survivors = [(rng.randint(5, 80), rng.randint(0, 40)) for _ in range(300)]
    deaths = [rng.randint(5, 60) for _ in range(30)]
    m = _model({"*|Monster|all": _cell(n=330, n_died=30, survivors=survivors, death_hp=deaths)})
    for _ in range(2):  # the second pass runs on the cached sorted views
        for hp in (1, 7.5, 20, 33.3, 50, 79, 90):
            assert m.death_prob(1, "Monster", _feat(hp=hp)) == pytest.approx(
                _reference_death_prob(survivors, deaths, hp))


def test_resolve_reports_the_level_each_lookup_resolved_at():
    act_cell, pooled, type_cell = _cell(n=25), _cell(n=90), _cell(n=200)
    m = _model({"2|Monster|hi": act_cell, "*|Monster|hi": pooled, "*|Monster|all": type_cell,
                "2|Shop|all": _cell(n=5), "*|Shop|all": _cell(n=60)})
    hi = _feat(burst=0.9)
    assert m.resolve(2, "Monster", hi) == ("act", act_cell)
    assert m.resolve(3, "Monster", hi) == ("pooled", pooled)               # no act-3 cell
    assert m.resolve(3, "Monster", _feat(burst=0.1)) == ("type", type_cell)  # no lo cell at all
    assert m.resolve(2, "Shop", _feat())[0] == "pooled"                     # act cell n=5 < min_n
    assert m.resolve(2, "Ancient", _feat())[0] == "zero"                    # unseen room type
    assert m.resolve(2, "Ancient", _feat())[1]["n"] == 0
    assert set(CELL_LEVELS) == {"act", "pooled", "type", "zero"}


def test_ancient_is_an_ordinary_room_type_with_its_own_cell():
    m = _model({"2|Ancient|all": _cell(n=30, hp=12.0, n_relics=1.0)})
    level, cell = m.resolve(2, "Ancient", _feat(act=2))
    assert level == "act" and cell["mean_delta"]["n_relics"] == 1.0


def test_step_clamps_and_takes_the_floor_from_the_row():
    delta = _cell(n=30, hp=100.0, max_hp=-500.0, gold=-999.0, deck_burst=-5.0, deck_top8=50.0,
                  deck_size=-50.0, n_upgraded=-9.0, n_relics=-9.0, n_potions=10.0)
    m = _model({"*|Treasure|all": delta})
    s = m.step(2, "Treasure", _feat(floor=8, act=2, hp=30, max_hp=80, gold=40, n_pot=1), 8)
    assert s["max_hp"] == 1.0 and s["hp"] == 1.0            # max_hp floored at 1, hp within [1, max_hp]
    assert s["gold"] == 0.0 and s["deck_burst"] == 0.0 and s["deck_top8"] == 10.0
    assert s["deck_size"] == 0.0 and s["n_upgraded"] == 0.0 and s["n_relics"] == 0.0
    assert s["n_potions"] == 3.0
    assert (s["act"], s["floor"], s["gf"]) == (2, 9, 26)   # row 8 -> floor 9, gf 17 + 9


def test_step_caps_hp_at_max_hp_and_does_not_mutate_its_input():
    m = _model({"*|RestSite|all": _cell(n=30, hp=40.0, n_potions=-2.0, deck_top8=-9.0,
                                        deck_burst=2.5)})
    f = _feat(hp=70, max_hp=80, burst=0.5, top8=6.0, n_pot=1)
    frozen = dict(f)
    s = m.step(1, "RestSite", f, 1)
    assert s["hp"] == 80.0 and s["deck_top8"] == 0.0 and s["n_potions"] == 0.0
    assert s["deck_burst"] == pytest.approx(3.0)  # burst is unbounded above: 0.5 + 2.5
    assert f == frozen


def test_step_takes_the_floor_from_the_map_row_not_from_counting_rooms():
    m = _model({})
    f = _feat(floor=0, act=2, hp=50)  # Act 2 opens at floor 0 (gf 17)
    ancient = m.step(2, "Ancient", f, 0)        # the row-0 Ancient room
    assert (ancient["floor"], ancient["gf"]) == (1, 18)
    via_ancient = m.step(2, "Monster", ancient, 1)
    direct = m.step(2, "Monster", f, 1)         # skip the Ancient: straight to a row-1 Monster
    assert (via_ancient["floor"], via_ancient["gf"]) == (direct["floor"], direct["gf"]) == (2, 19)
    act3 = m.step(3, "Monster", _feat(floor=4, act=3), 7)
    assert (act3["floor"], act3["gf"]) == (8, 2 * ACT_FLOORS + 8)


def test_act_floors_matches_route_data():
    assert ACT_FLOORS == route_data.ACT_FLOORS


def test_load_rejects_an_unsupported_version(tmp_path):
    path = tmp_path / "m.json"
    data = {"version": 2, "value": _value_dict(),
            "transitions": {"split": 0.5, "min_n": 20, "cells": {}}}
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="version"):
        RouteModel.load(str(path))
    path.write_text(json.dumps({k: v for k, v in data.items() if k != "version"}))
    with pytest.raises(ValueError, match="version"):
        RouteModel.load(str(path))
    path.write_text(json.dumps(dict(data, version=1)))
    assert RouteModel.load(str(path)).value(_feat(hp=10), "Defect") == pytest.approx(1.0)


def test_save_writes_atomically_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = str(tmp_path / "m.json")
    m = _model({"*|Shop|all": _cell(n=30)})
    m.save(path)
    first = open(path).read()
    assert sorted(os.listdir(tmp_path)) == ["m.json"]
    # a failing write neither clobbers the good file nor leaves its temp file behind
    bad = RouteModel({"version": 1, "value": m.data["value"],
                      "transitions": {"split": 0.5, "min_n": 20, "cells": {}}, "meta": object()})
    with pytest.raises(TypeError):
        bad.save(path)
    assert open(path).read() == first
    assert sorted(os.listdir(tmp_path)) == ["m.json"]
    # the rename goes through os.replace, from a temp file in the same directory
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real_replace(a, b))[1])
    _model({}).save(path)
    assert len(calls) == 1 and calls[0][1] == path and os.path.dirname(calls[0][0]) == str(tmp_path)


# ------------------------------------------------------------ transition model

def test_fit_transition_model_buckets_rest_by_hp_frac_and_monsters_by_the_monster_median():
    burst_values = [0.1, 0.2, 0.3, 0.4, 0.9]  # median 0.3
    monsters = [_trans("Monster", _feat(burst=d, hp=70), {"hp": 65}) for d in burst_values]
    rests = ([_trans("RestSite", _feat(hp=24, max_hp=80), {"hp": 60}) for _ in range(2)]
             + [_trans("RestSite", _feat(hp=64, max_hp=80), {"hp": 80}) for _ in range(3)])
    # An Elite bucket uses the MONSTER median too: 0.35 is hi against 0.3.
    elites = [_trans("Elite", _feat(burst=0.35), {"hp": 50}),
              _trans("Elite", _feat(burst=0.25), {"hp": 40})]
    tm = fit_transition_model([_game(9, [_feat()], monsters + rests + elites)], min_n=1)
    cells = tm["cells"]
    assert tm["split"] == pytest.approx(0.3) and tm["min_n"] == 1
    assert cells["1|RestSite|lo"]["n"] == 2 and cells["1|RestSite|hi"]["n"] == 3
    assert cells["1|RestSite|lo"]["mean_delta"]["hp"] == pytest.approx(36.0)
    assert cells["1|RestSite|hi"]["mean_delta"]["hp"] == pytest.approx(16.0)
    assert cells["1|Monster|lo"]["n"] == 2 and cells["1|Monster|hi"]["n"] == 3  # 0.3 is not < 0.3
    assert cells["1|Elite|hi"]["n"] == 1 and cells["1|Elite|lo"]["n"] == 1
    assert cells["*|RestSite|all"]["n"] == 5 and cells["*|Monster|all"]["n"] == 5


def test_monster_split_is_the_median_deck_burst_and_gives_both_buckets_samples():
    bursts = [0.3, 0.3, 0.5, 0.5]  # median 0.4: strict < puts the two 0.3 fights in "lo"
    monsters = [_trans("Monster", _feat(burst=b), {"hp": 75}) for b in bursts]
    tm = fit_transition_model([_game(9, [_feat()], monsters)], min_n=1)
    assert tm["split"] == pytest.approx(0.4)
    assert tm["cells"]["1|Monster|lo"]["n"] == 2
    assert tm["cells"]["1|Monster|hi"]["n"] == 2
    assert tm["cells"]["*|Monster|all"]["n"] == 4


def test_fit_transition_model_cell_contents_use_survivors_and_censored_deaths():
    pre = _feat(hp=60, gold=100, n_rel=1)
    survived_a = _trans("Elite", pre, {"hp": 45, "n_relics": 2, "gold": 130, "deck_burst": 0.7,
                                       "deck_top8": 6.5})
    survived_b = _trans("Elite", pre, {"hp": 70, "n_relics": 2, "gold": 130, "deck_burst": 0.9,
                                       "deck_top8": 7.5})  # healed: loss clamps at 0
    died_a = _trans("Elite", _feat(hp=22), died=True)
    died_b = _trans("Elite", _feat(hp=9), died=True)
    tm = fit_transition_model([_game(9, [pre], [survived_a, survived_b, died_a, died_b])], min_n=1)
    cell = tm["cells"]["1|Elite|hi"]  # no Monsters -> split 0.5; deck_burst 0.5 is not < 0.5
    assert cell["n"] == 4 and cell["n_died"] == 2
    assert cell["survivors"] == [[60, 0], [60, 15]]  # (pre hp, hp lost); the heal loses 0
    assert cell["death_hp"] == [9, 22]
    md = cell["mean_delta"]
    assert md["hp"] == pytest.approx((-15 + 10) / 2)        # survivors only
    assert md["n_relics"] == pytest.approx(1.0) and md["gold"] == pytest.approx(30.0)
    assert md["deck_burst"] == pytest.approx(0.3)   # (0.7 - 0.5, 0.9 - 0.5) -> 0.3
    assert md["deck_top8"] == pytest.approx(1.0)    # (6.5 - 6.0, 7.5 - 6.0) -> 1.0


def test_fit_transition_model_ignores_boss_transitions_and_pools_across_acts():
    a1 = [_trans("Shop", _feat(act=1, gold=200), {"gold": 20}) for _ in range(3)]
    a2 = [_trans("Shop", _feat(act=2, gold=200), {"gold": 100}) for _ in range(2)]
    boss = [_trans("Boss", _feat(), {"hp": 1})]
    tm = fit_transition_model([_game(9, [_feat()], a1 + a2 + boss)], min_n=1)
    assert not any("Boss" in key for key in tm["cells"])
    assert tm["cells"]["1|Shop|all"]["n"] == 3 and tm["cells"]["2|Shop|all"]["n"] == 2
    pooled = tm["cells"]["*|Shop|all"]
    assert pooled["n"] == 5
    assert pooled["mean_delta"]["gold"] == pytest.approx((3 * -180 + 2 * -100) / 5)


def test_fit_transition_model_without_monsters_still_produces_a_split():
    tm = fit_transition_model([_game(9, [_feat()], [_trans("Shop", _feat())])], min_n=1)
    assert 0.0 <= tm["split"] <= 1.0


def test_fit_value_model_caps_folds_at_the_number_of_games():
    games = [_game(5 + i, [_feat(floor=f, hp=60 + i) for f in range(1, 4)], seed=f"t{i}")
             for i in range(3)]
    fit = fit_value_model(games, folds=5)
    assert len(fit["cv"]["full"]) == 3  # min(folds, n_games)
    assert all(math.isfinite(v) for v in fit["cv"]["full"] + fit["cv"]["hp_only"])


# --------------------------------------------------------------------- CLI

@pytest.fixture
def _fake_card_data(monkeypatch):
    from agent import route_data
    monkeypatch.setattr(route_data, "score_card", lambda card: 5.0)
    monkeypatch.setattr(route_data, "deck_5turn_burst", lambda deck: 40.0)


def _write_log(path, character, ascension, seed, hp_curve, final_floor, experiment=None,
               map_state=False):
    """A JSONL game log: one map_select per floor 1..len(hp_curve), then game_over.
    `experiment` tags run_meta; `map_state` adds the {"type": "map"} reply a value-policy
    run leaves after each map_select."""
    def ms(floor, hp):
        return {"type": "decision", "decision": "map_select",
                "context": {"act": 1, "floor": floor},
                "choices": [{"col": 0, "row": floor, "type": "Monster"}],
                "player": {"hp": hp, "max_hp": 80, "gold": 50,
                           "deck": [{"id": "A", "type": "Attack", "upgraded": False}] * 10,
                           "relics": [{"name": "R"}], "potions": []}}
    rows = [{"type": "run_meta", "seed": seed, "character": character, "ascension": ascension}]
    if experiment is not None:
        rows[0]["experiment"] = experiment
    for floor, hp in enumerate(hp_curve, start=1):
        rows.append({"type": "state", "data": ms(floor, hp)})
        if map_state:
            rows.append({"type": "action", "data": {"cmd": "get_map"}})
            rows.append({"type": "state", "data": {"type": "map", "rows": []}})
        rows.append({"type": "action", "data": {"cmd": "action", "action": "select_map_node",
                                                "args": {"col": 0, "row": floor}}})
    rows.append({"type": "state", "data": {
        "type": "decision", "decision": "game_over", "victory": False,
        "context": {"act": 1, "floor": final_floor}, "act": 1, "floor": final_floor,
        "player": {"hp": 0, "max_hp": 80, "gold": 50, "deck_size": 10, "potions": []}}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cli_end_to_end(tmp_path, capsys, _fake_card_data):
    from agent import fit_route_model
    logs = tmp_path / "logs"
    logs.mkdir()
    curves = [[80, 70, 55, 40], [80, 78, 60, 50, 30], [80, 72, 66]]
    for i, curve in enumerate(curves):
        _write_log(logs / f"g{i}.jsonl", ["Defect", "Silent", "Defect"][i], 1, f"seed{i}",
                   curve, len(curve) + 1)
    _write_log(logs / "a0.jsonl", "Defect", 0, "seed_a0", [80, 70, 60], 4)
    out = tmp_path / "out" / "route_model.json"
    rc = fit_route_model.main(["--logs", str(logs), "--ascension", "1", "--out", str(out),
                               "--min-n", "1"])
    assert rc in (None, 0)
    model = RouteModel.load(str(out))
    assert model.data["meta"]["n_games"] == 3
    assert model.data["meta"]["ascension"] == 1
    assert model.data["meta"]["characters"] == {"Defect": 2, "Silent": 1}
    assert model.data["meta"]["n_transitions"] == 12
    assert model.data["validation"]["chosen"] in ("full", "hp_only")
    assert set(model.data["validation"]["cv"]) == {"full", "hp_only"}
    assert model.value(_feat(hp=70), "Defect") >= 0.0
    report = capsys.readouterr().out
    assert "dropped 1" in report.lower() or "dropped: 1" in report.lower()
    assert "Defect" in report and "hp_only" in report
    assert "d_burst" in report and "d_top8" in report
    assert "deck_strength" not in report
    assert "grouped by seed (3 seeds)" in report
    assert "Sparse cells" in report and "Calibration" in report


def test_cli_exits_with_an_error_when_no_games_match(tmp_path, _fake_card_data):
    from agent import fit_route_model
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_log(logs / "a0.jsonl", "Defect", 0, "seed_a0", [80, 70, 60], 4)
    with pytest.raises(SystemExit):
        fit_route_model.main(["--logs", str(logs), "--ascension", "1",
                              "--out", str(tmp_path / "m.json")])
    assert not (tmp_path / "m.json").exists()


def test_cli_accepts_globs_and_repeated_logs_without_double_counting(tmp_path, capsys,
                                                                     _fake_card_data):
    from agent import fit_route_model
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(3):
        _write_log(logs / f"g{i}.jsonl", "Ironclad", 1, f"seed{i}", [80, 60, 40], 4)
    out = tmp_path / "m.json"
    fit_route_model.main(["--logs", str(logs / "g*.jsonl"), "--logs", str(logs),
                          "--ascension", "1", "--out", str(out)])
    assert RouteModel.load(str(out)).data["meta"]["n_games"] == 3


def _fit(tmp_path, logs, *extra, ascension="1", name="m.json"):
    from agent import fit_route_model
    out = tmp_path / name
    argv = [arg for log in logs for arg in ("--logs", log)]
    fit_route_model.main([*argv, "--ascension", ascension, "--out", str(out), "--min-n", "1",
                          *extra])
    return out


def test_cli_skips_value_policy_logs_unless_allowed(tmp_path, capsys, _fake_card_data):
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_log(logs / "r1.jsonl", "Defect", 1, "s1", [80, 70, 60], 4, experiment="ooc=greedy")
    _write_log(logs / "r2.jsonl", "Defect", 1, "s2", [80, 70, 60], 4, experiment="ooc=greedy")
    _write_log(logs / "v1.jsonl", "Defect", 1, "s3", [80, 70, 60], 4,
               experiment="ooc=greedy,route=value")
    _write_log(logs / "v2.jsonl", "Defect", 1, "s4", [80, 70, 60], 4)  # untagged, but asked for a map
    with open(logs / "v2.jsonl") as fh:
        rows = [json.loads(line) for line in fh]
    rows.insert(2, {"type": "state", "data": {"type": "map", "rows": []}})
    (logs / "v2.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _write_log(logs / "v3.jsonl", "Defect", 1, "s5", [80, 70, 60], 4, map_state=True)

    out = _fit(tmp_path, [str(logs)])
    assert RouteModel.load(str(out)).data["meta"]["n_games"] == 2
    assert "Skipped 3 game(s) played under the value route policy" in capsys.readouterr().out

    out = _fit(tmp_path, [str(logs)], "--allow-value-logs", name="all.json")
    assert RouteModel.load(str(out)).data["meta"]["n_games"] == 5
    assert "Skipped" not in capsys.readouterr().out


def test_cli_dedupes_the_same_run_seen_through_two_log_specs(tmp_path, capsys, _fake_card_data):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_log(a / "x.jsonl", "Defect", 1, "s1", [80, 70, 60], 4)
    _write_log(b / "y.jsonl", "Defect", 1, "s1", [80, 70, 60], 4)      # same run, other file
    _write_log(a / "z.jsonl", "Silent", 1, "s1", [80, 70, 60], 4)      # same seed, other character
    _write_log(a / "w.jsonl", "Defect", 1, "s2", [80, 65, 50], 4)
    out = _fit(tmp_path, [str(a), str(b)])
    meta = RouteModel.load(str(out)).data["meta"]
    assert meta["n_games"] == 3 and meta["characters"] == {"Defect": 2, "Silent": 1}
    assert "Dropped 1 duplicate game(s)" in capsys.readouterr().out


def test_dedupe_games_never_merges_games_without_a_seed():
    from agent.fit_route_model import dedupe_games
    games = [dict(_game(5, [_feat()]), seed=None) for _ in range(3)]
    assert len(dedupe_games(games)) == 3
    same = [_game(5, [_feat()], seed="s"), _game(6, [_feat()], seed="s"),
            _game(7, [_feat()], seed="s", character="Silent")]
    assert [g["final_gf"] for g in dedupe_games(same)] == [5, 7]  # first of each (char, seed, asc)


def test_sparse_cells_lists_each_thin_cell_and_where_it_falls_back():
    from agent.fit_route_model import sparse_cells
    transitions = {"split": 0.5, "min_n": 20, "cells": {
        "1|Monster|hi": _cell(n=30), "1|Monster|lo": _cell(n=4), "2|Monster|hi": _cell(n=19),
        "*|Monster|hi": _cell(n=49), "*|Monster|lo": _cell(n=8), "*|Monster|all": _cell(n=57),
        "2|Ancient|all": _cell(n=7), "*|Ancient|all": _cell(n=7)}}
    rows = {r["cell"]: r for r in sparse_cells(transitions)}
    assert "1|Monster|hi" not in rows
    assert rows["1|Monster|lo"] == {"cell": "1|Monster|lo", "n": 4,
                                    "falls_back_to": "*|Monster|all"}   # pooled lo is thin too
    assert rows["2|Monster|hi"]["falls_back_to"] == "*|Monster|hi"
    assert rows["*|Monster|lo"]["falls_back_to"] == "*|Monster|all"
    assert rows["2|Ancient|all"]["falls_back_to"] == "zero"             # the whole chain is thin
    assert rows["*|Ancient|all"]["falls_back_to"] == "zero"


def test_calibration_rows_compare_the_empirical_death_rate_with_the_model_q():
    from agent.fit_route_model import calibration_rows
    # Act 1 Monster fights at burst 0.5 (split 0.5 -> "hi"): 4 entered at 30 hp, 1 died;
    # 4 entered at 70 hp, none died.
    fights = ([_trans("Monster", _feat(hp=30), {"hp": 20}) for _ in range(3)]
              + [_trans("Monster", _feat(hp=30), died=True)]
              + [_trans("Monster", _feat(hp=70), {"hp": 60}) for _ in range(4)]
              + [_trans("Shop", _feat(hp=30), {"hp": 30})])                # not a fight: ignored
    games = [_game(9, [_feat()], fights)]
    transitions = fit_transition_model(games, min_n=1)
    model = RouteModel({"version": 1, "value": _value_dict(), "transitions": transitions})
    rows = {(r["cell"], r["hp_bin"]): r for r in calibration_rows(games, model)}
    assert set(rows) == {("1|Monster|hi", "25-35"), ("1|Monster|hi", ">=65")}
    low = rows[("1|Monster|hi", "25-35")]
    assert (low["n"], low["died"]) == (4, 1) and low["rate"] == pytest.approx(0.25)
    # model q at 30 hp: samples entered with >= 30: 3 survivors (30, loss 10) + 4 (70, loss 10)
    # + the death at 30 -> (0 losses >= 30 + 1 death) / (7 + 1) = 1/8; q at 70 hp: 4 survivors
    # entered with >= 70, none lost 70 -> 0.
    assert low["model_q"] == pytest.approx(1 / 8)
    high = rows[("1|Monster|hi", ">=65")]
    assert (high["n"], high["died"]) == (4, 0) and high["model_q"] == pytest.approx(0.0)
