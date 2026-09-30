"""Phase 3b Task 2 (+ Task 4's CLI test): route value model, transition model, JSON.

Hermetic: games are built directly as agent.route_data.load_games-style dicts.
"""
import json
import math

import numpy as np
import pytest

from agent.route_model import (
    DELTA_KEYS, FULL_FEATURES, HP_ONLY_FEATURES, RouteModel, death_prob_from_samples,
    feature_vector, fit_transition_model, fit_value_model)


def _feat(floor=1, act=1, hp=80, max_hp=80, gold=0, ds=0.5, deck_size=15, n_up=0, n_rel=1, n_pot=0):
    return {"hp": hp, "max_hp": max_hp, "gold": gold, "deck_strength": ds,
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

def test_death_prob_counts_only_deaths_that_are_informative_for_the_threshold():
    assert death_prob_from_samples([0, 5, 10], [], 6) == pytest.approx(1 / 3)
    # A death at pre-hp 3 says nothing about a threshold of 6: denominator 2, numerator 0.
    assert death_prob_from_samples([0, 5], [3], 6) == 0.0
    assert death_prob_from_samples([0, 5], [6], 6) == pytest.approx(1 / 3)
    # A death at 8 counts for threshold 6: (0 losses >= 6 + 1 death) / (2 losses + 1 death).
    assert death_prob_from_samples([0, 5], [8], 6) == pytest.approx(1 / 3)
    assert death_prob_from_samples([], [8, 9], 6) == 1.0


def test_death_prob_at_the_documented_boundaries():
    assert death_prob_from_samples([1, 2], [5], 0) == 1.0
    assert death_prob_from_samples([1, 2], [5], -3) == 1.0
    assert death_prob_from_samples([], [], 10) == 0.0


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
        # y = final_gf - floor = 30 - floor + 0.3 * deck_strength * 100, small noise
        final_of=lambda b, r: round(30 + 0.3 * b["deck_strength"] * 100 + r.normal(0, 0.3), 3),
        per_game=lambda r: _feat(ds=float(r.uniform(0, 1)), gold=int(r.integers(0, 300)),
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
        per_game=lambda r: _feat(hp=int(r.integers(20, 81)), ds=float(r.uniform(0, 1)),
                                 gold=int(r.integers(0, 300)), n_rel=int(r.integers(0, 6)),
                                 n_pot=int(r.integers(0, 4)), deck_size=int(r.integers(10, 30)),
                                 n_up=int(r.integers(0, 8))))
    fit = fit_value_model(games)
    assert fit["chosen"] == "hp_only"
    assert fit["model"]["features"] == HP_ONLY_FEATURES
    # the recovered model is usable: more HP -> more expected floors
    model = RouteModel({"value": fit["model"], "transitions": {"split": 0.5, "min_n": 20, "cells": {}}})
    assert model.value(_feat(hp=80), "Defect") > model.value(_feat(hp=20), "Defect")


def test_fit_value_model_needs_two_games():
    game = _game(5, [_feat()])
    with pytest.raises(ValueError, match="2 games"):
        fit_value_model([game])


# ------------------------------------------------------------- RouteModel / JSON

def _value_dict(features=("hp",), coef=(0.1,), intercept=0.0, mean=None, scale=None):
    n = len(features)
    return {"features": list(features), "mean": list(mean or [0.0] * n),
            "scale": list(scale or [1.0] * n), "coef": list(coef), "intercept": intercept}


def _cell(n=30, n_died=0, losses=(), death_hp=(), **delta):
    return {"n": n, "n_died": n_died,
            "mean_delta": {k: float(delta.get(k, 0.0)) for k in DELTA_KEYS},
            "losses": list(losses), "death_hp": list(death_hp)}


def _model(cells=None, value=None, split=0.5, min_n=20):
    return RouteModel({"version": 1, "value": value or _value_dict(),
                       "transitions": {"split": split, "min_n": min_n, "cells": cells or {}},
                       "meta": {}, "validation": {}})


def test_save_load_round_trip_preserves_value_exactly(tmp_path):
    rng = np.random.default_rng(2)
    games = _deck_games(
        10, rng,
        final_of=lambda b, r: round(25 + b["hp"] * 0.1 + r.normal(0, 1), 3),
        per_game=lambda r: _feat(hp=int(r.integers(20, 81)), ds=float(r.uniform(0, 1))))
    data = {"version": 1, "value": fit_value_model(games)["model"],
            "transitions": fit_transition_model(games), "meta": {"n_games": 10}, "validation": {}}
    model = RouteModel(data)
    f = _feat(floor=7, hp=51, ds=0.37, gold=123)
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
    assert m.cell(2, "Monster", _feat(ds=0.9)) is exact
    assert m.cell(3, "Monster", _feat(ds=0.9)) is pooled  # no act-3 cell -> pooled over acts


def test_cell_falls_back_when_a_cell_is_below_min_n():
    thin = _cell(n=19, hp=-30.0)
    pooled = _cell(n=90, hp=-3.0)
    m = _model({"2|Elite|hi": thin, "*|Elite|hi": pooled})
    assert m.cell(2, "Elite", _feat(ds=0.9)) is pooled


def test_cell_fallback_chain_ends_in_the_all_bucket_then_a_zero_cell():
    only_pooled_hi = _cell(n=50, hp=-15.0)
    m = _model({"*|Elite|hi": only_pooled_hi})
    assert m.cell(2, "Elite", _feat(ds=0.9)) is only_pooled_hi
    all_bucket = _cell(n=50, hp=-9.0)
    m2 = _model({"*|Elite|all": all_bucket})
    assert m2.cell(2, "Elite", _feat(ds=0.1)) is all_bucket  # lo bucket missing -> all
    unknown = m.cell(2, "Mystery", _feat())
    assert unknown["n"] == 0 and unknown["losses"] == [] and unknown["death_hp"] == []
    assert all(v == 0.0 for v in unknown["mean_delta"].values())
    assert m.death_prob(2, "Mystery", _feat(hp=5)) == 0.0
    stepped = m.step(2, "Mystery", _feat(floor=4, hp=33, gold=10))
    assert (stepped["floor"], stepped["gf"]) == (5, 5)
    assert all(stepped[k] == _feat(floor=4, hp=33, gold=10)[k] for k in DELTA_KEYS)


def test_cell_bucketing_uses_deck_strength_for_fights_and_hp_frac_for_rest():
    m = _model({"1|Monster|lo": _cell(n=30, hp=-9.0), "1|Monster|hi": _cell(n=30, hp=-3.0),
                "1|RestSite|lo": _cell(n=30, hp=25.0), "1|RestSite|hi": _cell(n=30, hp=8.0),
                "1|Shop|all": _cell(n=30, gold=-60.0)}, split=0.4)
    assert m.cell(1, "Monster", _feat(ds=0.39))["mean_delta"]["hp"] == -9.0
    assert m.cell(1, "Monster", _feat(ds=0.4))["mean_delta"]["hp"] == -3.0
    assert m.cell(1, "RestSite", _feat(hp=39, max_hp=80))["mean_delta"]["hp"] == 25.0
    assert m.cell(1, "RestSite", _feat(hp=40, max_hp=80))["mean_delta"]["hp"] == 8.0
    assert m.cell(1, "Shop", _feat())["mean_delta"]["gold"] == -60.0


def test_death_prob_reads_the_cell_samples_at_the_current_hp():
    m = _model({"*|Elite|all": _cell(n=40, n_died=1, losses=[10, 20, 30, 40], death_hp=[35])})
    assert m.death_prob(1, "Elite", _feat(hp=25)) == pytest.approx(3 / 5)  # 30, 40, death@35
    assert m.death_prob(1, "Elite", _feat(hp=50)) == pytest.approx(0.0)


def test_step_clamps_and_advances_the_floor():
    delta = _cell(n=30, hp=100.0, max_hp=-500.0, gold=-999.0, deck_strength=5.0,
                  deck_size=-50.0, n_upgraded=-9.0, n_relics=-9.0, n_potions=10.0)
    m = _model({"*|Treasure|all": delta})
    s = m.step(2, "Treasure", _feat(floor=8, act=2, hp=30, max_hp=80, gold=40, n_pot=1))
    assert s["max_hp"] == 1.0 and s["hp"] == 1.0            # max_hp floored at 1, hp within [1, max_hp]
    assert s["gold"] == 0.0 and s["deck_strength"] == 1.0
    assert s["deck_size"] == 0.0 and s["n_upgraded"] == 0.0 and s["n_relics"] == 0.0
    assert s["n_potions"] == 3.0
    assert (s["act"], s["floor"], s["gf"]) == (2, 9, 26)


def test_step_caps_hp_at_max_hp_and_does_not_mutate_its_input():
    m = _model({"*|RestSite|all": _cell(n=30, hp=40.0, n_potions=-2.0, deck_strength=-0.9)})
    f = _feat(hp=70, max_hp=80, ds=0.5, n_pot=1)
    frozen = dict(f)
    s = m.step(1, "RestSite", f)
    assert s["hp"] == 80.0 and s["deck_strength"] == 0.0 and s["n_potions"] == 0.0
    assert f == frozen


# ------------------------------------------------------------ transition model

def test_fit_transition_model_buckets_rest_by_hp_frac_and_monsters_by_the_monster_median():
    ds_values = [0.1, 0.2, 0.3, 0.4, 0.9]  # median 0.3
    monsters = [_trans("Monster", _feat(ds=d, hp=70), {"hp": 65}) for d in ds_values]
    rests = ([_trans("RestSite", _feat(hp=24, max_hp=80), {"hp": 60}) for _ in range(2)]
             + [_trans("RestSite", _feat(hp=64, max_hp=80), {"hp": 80}) for _ in range(3)])
    # An Elite bucket uses the MONSTER median too: 0.35 is hi against 0.3.
    elites = [_trans("Elite", _feat(ds=0.35), {"hp": 50}), _trans("Elite", _feat(ds=0.25), {"hp": 40})]
    tm = fit_transition_model([_game(9, [_feat()], monsters + rests + elites)], min_n=1)
    cells = tm["cells"]
    assert tm["split"] == pytest.approx(0.3) and tm["min_n"] == 1
    assert cells["1|RestSite|lo"]["n"] == 2 and cells["1|RestSite|hi"]["n"] == 3
    assert cells["1|RestSite|lo"]["mean_delta"]["hp"] == pytest.approx(36.0)
    assert cells["1|RestSite|hi"]["mean_delta"]["hp"] == pytest.approx(16.0)
    assert cells["1|Monster|lo"]["n"] == 2 and cells["1|Monster|hi"]["n"] == 3  # 0.3 is not < 0.3
    assert cells["1|Elite|hi"]["n"] == 1 and cells["1|Elite|lo"]["n"] == 1
    assert cells["*|RestSite|all"]["n"] == 5 and cells["*|Monster|all"]["n"] == 5


def test_fit_transition_model_cell_contents_use_survivors_and_censored_deaths():
    pre = _feat(hp=60, gold=100, n_rel=1)
    survived_a = _trans("Elite", pre, {"hp": 45, "n_relics": 2, "gold": 130})
    survived_b = _trans("Elite", pre, {"hp": 70, "n_relics": 2, "gold": 130})  # healed: loss clamps at 0
    died_a = _trans("Elite", _feat(hp=22), died=True)
    died_b = _trans("Elite", _feat(hp=9), died=True)
    tm = fit_transition_model([_game(9, [pre], [survived_a, survived_b, died_a, died_b])], min_n=1)
    cell = tm["cells"]["1|Elite|hi"]  # no Monsters -> split 0.5; deck_strength 0.5 is not < 0.5
    assert cell["n"] == 4 and cell["n_died"] == 2
    assert cell["losses"] == [0, 15]
    assert cell["death_hp"] == [9, 22]
    md = cell["mean_delta"]
    assert md["hp"] == pytest.approx((-15 + 10) / 2)        # survivors only
    assert md["n_relics"] == pytest.approx(1.0) and md["gold"] == pytest.approx(30.0)


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

def _write_log(path, character, ascension, seed, hp_curve, final_floor):
    """A JSONL game log: one map_select per floor 1..len(hp_curve), then game_over."""
    def ms(floor, hp):
        return {"type": "decision", "decision": "map_select",
                "context": {"act": 1, "floor": floor},
                "choices": [{"col": 0, "row": floor, "type": "Monster"}],
                "player": {"hp": hp, "max_hp": 80, "gold": 50,
                           "deck": [{"id": "A", "type": "Attack", "upgraded": False}] * 10,
                           "relics": [{"name": "R"}], "potions": []}}
    rows = [{"type": "run_meta", "seed": seed, "character": character, "ascension": ascension}]
    for floor, hp in enumerate(hp_curve, start=1):
        rows.append({"type": "state", "data": ms(floor, hp)})
        rows.append({"type": "action", "data": {"cmd": "action", "action": "select_map_node",
                                                "args": {"col": 0, "row": floor}}})
    rows.append({"type": "state", "data": {
        "type": "decision", "decision": "game_over", "victory": False,
        "context": {"act": 1, "floor": final_floor}, "act": 1, "floor": final_floor,
        "player": {"hp": 0, "max_hp": 80, "gold": 50, "deck_size": 10, "potions": []}}})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cli_end_to_end(tmp_path, capsys):
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


def test_cli_exits_with_an_error_when_no_games_match(tmp_path):
    from agent import fit_route_model
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_log(logs / "a0.jsonl", "Defect", 0, "seed_a0", [80, 70, 60], 4)
    with pytest.raises(SystemExit):
        fit_route_model.main(["--logs", str(logs), "--ascension", "1",
                              "--out", str(tmp_path / "m.json")])
    assert not (tmp_path / "m.json").exists()


def test_cli_accepts_globs_and_repeated_logs_without_double_counting(tmp_path, capsys):
    from agent import fit_route_model
    logs = tmp_path / "logs"
    logs.mkdir()
    for i in range(3):
        _write_log(logs / f"g{i}.jsonl", "Ironclad", 1, f"seed{i}", [80, 60, 40], 4)
    out = tmp_path / "m.json"
    fit_route_model.main(["--logs", str(logs / "g*.jsonl"), "--logs", str(logs),
                          "--ascension", "1", "--out", str(out)])
    assert RouteModel.load(str(out)).data["meta"]["n_games"] == 3
