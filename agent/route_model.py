"""route_model.py -- value model and empirical transition model for route choice.

Phase 3b (docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md).

* Value model V(state) = expected REMAINING global floors, a ridge regression
  fitted on every map_select snapshot of the training games
  (target ``final_gf - gf``). Two feature sets are cross-validated by game and
  the richer one is only kept if it clearly beats the HP-only one.
* Transition model: per (act, room type, state bucket) cell, the mean change of
  each feature across SURVIVED rooms plus the raw HP-loss / death samples the
  planner turns into a censoring-aware death probability.

``RouteModel`` is the plain-JSON runtime object -- no sklearn at runtime; only
``fit_*`` need numpy / scikit-learn (imported lazily).
"""
from __future__ import annotations

import json
import math
import os
import statistics

CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")

FULL_FEATURES = ["hp", "max_hp", "hp_frac", "gold", "deck_strength", "deck_size", "n_upgraded",
                 "n_relics", "n_potions", "floor", "act2", "act3",
                 "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]
HP_ONLY_FEATURES = ["hp", "max_hp", "hp_frac", "floor", "act2", "act3",
                    "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]

# Per-room feature changes the transition model tracks (same names as
# route_data.FEATURE_KEYS).
DELTA_KEYS = ("hp", "max_hp", "gold", "deck_strength", "deck_size", "n_upgraded",
              "n_relics", "n_potions")
MAX_POTIONS = 3
REST_HP_FRAC_SPLIT = 0.5


def _hp_frac(features: dict) -> float:
    return features.get("hp", 0) / max(features.get("max_hp", 0), 1)


def feature_vector(features: dict, character: str, names: list[str]) -> list[float]:
    """The named model inputs for one state. ``floor`` is act-local; act2/act3
    and char_<Name> are indicators (a character outside CHARACTERS is all zeros)."""
    act = features.get("act", 1)
    derived = {"hp_frac": _hp_frac(features),
               "act2": float(act == 2),
               "act3": float(act >= 3)}
    for name in CHARACTERS:
        derived[f"char_{name}"] = float(character == name)
    return [float(derived[n]) if n in derived else float(features.get(n, 0)) for n in names]


def death_prob_from_samples(losses: list[int], death_hp: list[int], hp: float) -> float:
    """Censoring-aware P(loss >= hp) for one room type.

    ``losses`` are the HP lost in survived rooms; ``death_hp`` the pre-room HP of
    the deaths. A death at pre-hp d means loss >= d, so it says something about a
    threshold hp only when hp <= d. Estimate = (#losses >= hp + #death_hp >= hp) /
    (#losses + #death_hp >= hp); 0.0 when the denominator is 0; hp <= 0 -> 1.0.
    """
    if hp <= 0:
        return 1.0
    deaths_informative = sum(1 for d in death_hp if d >= hp)
    denom = len(losses) + deaths_informative
    if denom == 0:
        return 0.0
    return (sum(1 for loss in losses if loss >= hp) + deaths_informative) / denom


# ---------------------------------------------------------------- value model

def _value_samples(games: list[dict]):
    """(X_full, X_hp_only, y, groups) over every snapshot of every game; the
    group is the game's index in ``games``."""
    import numpy as np
    x_full, x_hp, y, groups = [], [], [], []
    for gi, game in enumerate(games):
        final_gf = game.get("final_gf")
        if final_gf is None:
            continue
        character = game.get("character")
        for snap in game.get("snapshots", []):
            f = snap["features"]
            x_full.append(feature_vector(f, character, FULL_FEATURES))
            x_hp.append(feature_vector(f, character, HP_ONLY_FEATURES))
            y.append(final_gf - f["gf"])
            groups.append(gi)
    return np.array(x_full), np.array(x_hp), np.array(y, dtype=float), np.array(groups)


def _fit_ridge(x, y, alpha: float, names: list[str]) -> dict:
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler().fit(x)
    ridge = Ridge(alpha=alpha).fit(scaler.transform(x), y)
    return {"features": list(names),
            "mean": [float(v) for v in scaler.mean_],
            "scale": [float(v) for v in scaler.scale_],
            "coef": [float(v) for v in ridge.coef_],
            "intercept": float(ridge.intercept_)}


def _predict(model: dict, x) -> list[float]:
    import numpy as np
    z = (np.asarray(x, dtype=float) - model["mean"]) / model["scale"]
    return list(z @ np.asarray(model["coef"]) + model["intercept"])


def fit_value_model(games: list[dict], alpha: float = 1.0, folds: int = 5, seed: int = 0) -> dict:
    """Ridge (StandardScaler + Ridge(alpha)) on FULL_FEATURES and on
    HP_ONLY_FEATURES, cross-validated with GroupKFold by game.

    Returns {"chosen": "full" | "hp_only", "cv": {"full": [mae per fold],
    "hp_only": [...]}, "model": {features, mean, scale, coef, intercept},
    "n_games", "n_samples"}. ``full`` is chosen only if its mean CV MAE is lower
    AND it beats hp_only on at least ceil(folds / 2) folds; otherwise hp_only.
    ``model`` is the chosen set refit on all samples.
    """
    import numpy as np
    from sklearn.model_selection import GroupKFold

    x_full, x_hp, y, groups = _value_samples(games)
    n_games = len(set(groups.tolist()))
    n_folds = min(folds, n_games)
    if n_folds < 2:
        raise ValueError(f"fit_value_model needs at least 2 games with snapshots, got {n_games}")

    sets = {"full": (x_full, FULL_FEATURES), "hp_only": (x_hp, HP_ONLY_FEATURES)}
    cv = {name: [] for name in sets}
    splitter = GroupKFold(n_splits=n_folds, shuffle=True, random_state=seed)
    for train, test in splitter.split(x_full, y, groups):
        for name, (x, names) in sets.items():
            model = _fit_ridge(x[train], y[train], alpha, names)
            pred = np.asarray(_predict(model, x[test]))
            cv[name].append(float(np.mean(np.abs(pred - y[test]))))

    wins = sum(1 for f, h in zip(cv["full"], cv["hp_only"]) if f < h)
    full_wins = (statistics.fmean(cv["full"]) < statistics.fmean(cv["hp_only"])
                 and wins >= math.ceil(n_folds / 2))
    chosen = "full" if full_wins else "hp_only"
    x, names = sets[chosen]
    return {"chosen": chosen, "cv": cv, "model": _fit_ridge(x, y, alpha, names),
            "n_games": n_games, "n_samples": int(len(y))}


# ---------------------------------------------------------- transition model

def _bucket(room_type: str, pre: dict, split: float) -> str:
    if room_type in ("Monster", "Elite"):
        return "lo" if pre.get("deck_strength", 0.0) < split else "hi"
    if room_type == "RestSite":
        return "lo" if _hp_frac(pre) < REST_HP_FRAC_SPLIT else "hi"
    return "all"


def _cell_from(samples: list[dict]) -> dict:
    survivors = [t for t in samples if not t["died"] and t.get("post") is not None]
    deaths = [t for t in samples if t["died"]]
    mean_delta = {}
    for key in DELTA_KEYS:
        deltas = [t["post"].get(key, 0) - t["pre"].get(key, 0) for t in survivors]
        mean_delta[key] = float(statistics.fmean(deltas)) if deltas else 0.0
    return {"n": len(samples), "n_died": len(deaths), "mean_delta": mean_delta,
            "losses": sorted(max(0, t["pre"]["hp"] - t["post"]["hp"]) for t in survivors),
            "death_hp": sorted(t["pre"]["hp"] for t in deaths)}


def fit_transition_model(games: list[dict], min_n: int = 20) -> dict:
    """Empirical per-room transition cells from every non-Boss transition.

    Cell key ``"{act}|{room_type}|{bucket}"``; bucket is lo/hi by deck_strength
    against ``split`` (the median deck_strength over all Monster pre states) for
    Monster and Elite, lo/hi by hp_frac < 0.5 for RestSite, else "all". The
    pooled fallbacks ``"*|{room_type}|{bucket}"`` (all acts) and
    ``"*|{room_type}|all"`` (all buckets) are built here too. Returns
    {"split", "min_n", "cells"}; RouteModel.cell walks the fallback chain.
    """
    transitions = [t for g in games for t in g.get("transitions", [])
                   if t["room_type"] != "Boss"]
    monster_strengths = [t["pre"].get("deck_strength", 0.0) for t in transitions
                         if t["room_type"] == "Monster"]
    # No Monster fights at all -> no basis for a split; the middle of [0, 1].
    split = float(statistics.median(monster_strengths)) if monster_strengths else 0.5

    grouped: dict[str, list[dict]] = {}
    for t in transitions:
        room_type = t["room_type"]
        bucket = _bucket(room_type, t["pre"], split)
        keys = {f"{t['act']}|{room_type}|{bucket}", f"*|{room_type}|{bucket}",
                f"*|{room_type}|all"}
        for key in keys:
            grouped.setdefault(key, []).append(t)
    return {"split": split, "min_n": min_n,
            "cells": {key: _cell_from(samples) for key, samples in sorted(grouped.items())}}


# ------------------------------------------------------------------ RouteModel

def _zero_cell() -> dict:
    return {"n": 0, "n_died": 0, "mean_delta": {k: 0.0 for k in DELTA_KEYS},
            "losses": [], "death_hp": []}


class RouteModel:
    """Fitted route model, loaded from / saved to plain JSON.

    ``data`` = {"version": 1, "value": <fit_value_model()["model"]>,
    "transitions": <fit_transition_model()>, "meta": {...}, "validation": {...}}.
    """

    def __init__(self, data: dict):
        self.data = data
        self._value = data["value"]
        self._transitions = data["transitions"]

    @classmethod
    def load(cls, path: str) -> "RouteModel":
        with open(path) as fh:
            return cls(json.load(fh))

    def save(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
            fh.write("\n")

    def value(self, features: dict, character: str) -> float:
        """Expected remaining global floors from this state (ridge prediction, >= 0)."""
        m = self._value
        x = feature_vector(features, character, m["features"])
        pred = m["intercept"] + sum(c * (v - mu) / (sc if sc else 1.0)
                                    for c, v, mu, sc in zip(m["coef"], x, m["mean"], m["scale"]))
        return max(0.0, float(pred))

    def cell(self, act: int, room_type: str, features: dict) -> dict:
        """The transition cell for this room and state: the first of the exact
        (act) cell, the all-acts cell and the all-buckets cell that has at least
        ``min_n`` samples; a zero cell (no delta, no death risk) if none does."""
        t = self._transitions
        bucket = _bucket(room_type, features, t["split"])
        cells = t["cells"]
        for key in (f"{act}|{room_type}|{bucket}", f"*|{room_type}|{bucket}",
                    f"*|{room_type}|all"):
            cell = cells.get(key)
            if cell is not None and cell["n"] >= t["min_n"]:
                return cell
        return _zero_cell()

    def death_prob(self, act: int, room_type: str, features: dict) -> float:
        cell = self.cell(act, room_type, features)
        return death_prob_from_samples(cell["losses"], cell["death_hp"], features.get("hp", 0))

    def step(self, act: int, room_type: str, features: dict) -> dict:
        """Expected features after SURVIVING the room: the cell's mean deltas
        added, then hp clamped to [1, max_hp], max_hp >= 1, gold >= 0,
        deck_strength to [0, 1], counts >= 0 and n_potions <= 3; floor and gf
        advance by one, act is unchanged."""
        delta = self.cell(act, room_type, features)["mean_delta"]
        s = dict(features)
        for key in DELTA_KEYS:
            s[key] = features.get(key, 0) + delta.get(key, 0.0)
        s["max_hp"] = max(1.0, s["max_hp"])
        s["hp"] = min(max(s["hp"], 1.0), s["max_hp"])
        s["gold"] = max(0.0, s["gold"])
        s["deck_strength"] = min(max(s["deck_strength"], 0.0), 1.0)
        for key in ("deck_size", "n_upgraded", "n_relics"):
            s[key] = max(0.0, s[key])
        s["n_potions"] = min(max(s["n_potions"], 0.0), float(MAX_POTIONS))
        s["floor"] = features.get("floor", 0) + 1
        s["gf"] = features.get("gf", 0) + 1
        return s
