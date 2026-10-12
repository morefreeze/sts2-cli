"""route_model.py -- value model and empirical transition model for route choice.

Phase 3b (docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md).

* Value model V(state) = expected REMAINING global floors, a ridge regression
  fitted on every map_select snapshot of the training games
  (target ``final_gf - gf``). Two feature sets are cross-validated by seed
  (the same seed's map and events are shared across characters) and the richer
  one is only kept if it clearly beats the HP-only one.
* Transition model: per (act, room type, state bucket) cell, the mean change of
  each feature across SURVIVED rooms plus the raw (pre-room HP, HP lost) samples
  of the survivors and the pre-room HP of the deaths, from which the planner
  estimates the death probability at its current HP.

``RouteModel`` is the plain-JSON runtime object -- no sklearn at runtime; only
``fit_*`` need numpy / scikit-learn (imported lazily).
"""
from __future__ import annotations

import bisect
import json
import math
import os
import statistics

CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")

FULL_FEATURES = ["hp", "max_hp", "hp_frac", "gold", "deck_burst", "deck_top8", "deck_size",
                 "n_upgraded", "n_relics", "n_potions", "floor", "act2", "act3",
                 "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]
HP_ONLY_FEATURES = ["hp", "max_hp", "hp_frac", "floor", "act2", "act3",
                    "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]

# Per-room feature changes the transition model tracks (same names as
# route_data.FEATURE_KEYS).
DELTA_KEYS = ("hp", "max_hp", "gold", "deck_burst", "deck_top8", "deck_size", "n_upgraded",
              "n_relics", "n_potions")
MAX_POTIONS = 3
ACT_FLOORS = 17  # same as route_data.ACT_FLOORS (tests pin the equality)
MODEL_VERSION = 1
# Where a cell lookup resolved (RouteModel.resolve): the exact act cell, the
# all-acts cell of the same bucket, the all-buckets cell of the room type, or --
# when none has min_n samples -- the zero cell.
CELL_LEVELS = ("act", "pooled", "type", "zero")
MAX_CARD_SCORE = 10.0  # card_scoring.score_card is a 0-10 scale, so is its top-8 mean
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


def _prepare_samples(survivors: list, death_hp: list) -> tuple:
    """Sorted views of a cell's samples for O(log n) death-probability queries.

    A survivor (pre, loss) has [pre >= h and loss >= h] exactly when
    min(pre, loss) >= h, so one sorted list of minima answers the numerator."""
    return (sorted(pre for pre, _ in survivors),
            sorted(min(pre, loss) for pre, loss in survivors),
            sorted(death_hp))


def _death_prob(prepared: tuple, hp: float) -> float:
    if hp <= 0:
        return 1.0
    pre, reach, deaths = prepared
    n_deaths = len(deaths) - bisect.bisect_left(deaths, hp)
    denom = len(pre) - bisect.bisect_left(pre, hp) + n_deaths
    if denom == 0:
        return 0.0
    return (len(reach) - bisect.bisect_left(reach, hp) + n_deaths) / denom


def death_prob_from_samples(survivors: list, death_hp: list, hp: float) -> float:
    """P(loss >= hp) for one room type, estimated conditionally on the room being
    entered with at least ``hp``.

    Only samples whose PRE-room HP is >= hp are informative about [loss >= hp]: a
    survivor's loss is exact, and a death at pre-hp d >= hp means loss >= d >= hp.
    (A sample entered with less HP could not have lost hp; counting it in the
    denominator, as a plain loss histogram does, biases the estimate low.)

    ``survivors`` = (pre_hp, loss) pairs, ``death_hp`` = pre-room HP of the deaths.
    Estimate = (#survivors with pre >= hp and loss >= hp + #deaths with pre >= hp) /
    (#survivors with pre >= hp + #deaths with pre >= hp); 0.0 when the denominator
    is 0; hp <= 0 -> 1.0.
    """
    return _death_prob(_prepare_samples(survivors, death_hp), hp)


# ---------------------------------------------------------------- value model

def _value_samples(games: list[dict]):
    """(X_full, X_hp_only, y, groups, n_games) over every snapshot of every game.
    The CV group is the game's seed: the same seed's map and events are shared
    across characters, so a seed must not straddle train and test. A game without
    a seed is its own group."""
    import numpy as np
    x_full, x_hp, y, groups = [], [], [], []
    group_ids: dict = {}
    contributing = set()
    for gi, game in enumerate(games):
        final_gf = game.get("final_gf")
        if final_gf is None:
            continue
        seed = game.get("seed")
        group = group_ids.setdefault(seed if seed is not None else ("game", gi), len(group_ids))
        character = game.get("character")
        for snap in game.get("snapshots", []):
            f = snap["features"]
            x_full.append(feature_vector(f, character, FULL_FEATURES))
            x_hp.append(feature_vector(f, character, HP_ONLY_FEATURES))
            y.append(final_gf - f["gf"])
            groups.append(group)
            contributing.add(gi)
    return (np.array(x_full), np.array(x_hp), np.array(y, dtype=float), np.array(groups),
            len(contributing))


def _group_kfold(n_splits: int, seed: int):
    from sklearn.model_selection import GroupKFold
    try:
        return GroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    except TypeError:  # scikit-learn < 1.6: no shuffle
        return GroupKFold(n_splits=n_splits)


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
    HP_ONLY_FEATURES, cross-validated with GroupKFold by SEED (a game without a
    seed is its own group).

    Returns {"chosen": "full" | "hp_only", "cv": {"full": [mae per fold],
    "hp_only": [...]}, "model": {features, mean, scale, coef, intercept},
    "n_games", "n_groups" (distinct seeds), "n_samples"}. ``full`` is chosen only if its mean CV MAE is lower
    AND it beats hp_only on at least ceil(folds / 2) folds; otherwise hp_only.
    ``model`` is the chosen set refit on all samples.
    """
    import numpy as np

    x_full, x_hp, y, groups, n_games = _value_samples(games)
    n_groups = len(set(groups.tolist()))
    n_folds = min(folds, n_groups)
    if n_folds < 2:
        raise ValueError(f"fit_value_model needs at least 2 CV groups (distinct seeds) with "
                         f"snapshots, got {n_groups}")

    sets = {"full": (x_full, FULL_FEATURES), "hp_only": (x_hp, HP_ONLY_FEATURES)}
    cv = {name: [] for name in sets}
    splitter = _group_kfold(n_folds, seed)
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
            "n_games": n_games, "n_groups": n_groups, "n_samples": int(len(y))}


# ---------------------------------------------------------- transition model

def cell_bucket(room_type: str, pre: dict, split: float) -> str:
    """The lo/hi/all bucket of a state for a room type (see fit_transition_model)."""
    if room_type in ("Monster", "Elite"):
        return "lo" if pre.get("deck_burst", 0.0) < split else "hi"
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
            # (pre-room hp, hp lost) of every survivor; pre-room hp of every death
            "survivors": sorted([t["pre"]["hp"], max(0, t["pre"]["hp"] - t["post"]["hp"])]
                                for t in survivors),
            "death_hp": sorted(t["pre"]["hp"] for t in deaths)}


def fit_transition_model(games: list[dict], min_n: int = 20) -> dict:
    """Empirical per-room transition cells from every non-Boss transition.

    Cell key ``"{act}|{room_type}|{bucket}"``; for Monster and Elite the bucket is
    lo/hi by ``deck_burst < split`` (``split`` = the median deck_burst over all
    Monster pre states), for RestSite lo/hi by hp_frac < 0.5, else "all". The
    pooled fallbacks ``"*|{room_type}|{bucket}"`` (all acts) and
    ``"*|{room_type}|all"`` (all buckets) are built here too. Each cell holds n,
    n_died, mean_delta (survivors only), ``survivors`` = sorted [pre_hp, hp_lost]
    pairs and ``death_hp`` = sorted pre-room hp of the deaths. Returns
    {"split", "min_n", "cells"}; RouteModel.resolve walks the fallback chain.
    """
    transitions = [t for g in games for t in g.get("transitions", [])
                   if t["room_type"] != "Boss"]
    monster_bursts = [t["pre"].get("deck_burst", 0.0) for t in transitions
                      if t["room_type"] == "Monster"]
    # No Monster fights at all -> no basis for a split; a typical Act 1 deck_burst.
    split = float(statistics.median(monster_bursts)) if monster_bursts else 0.5

    grouped: dict[str, list[dict]] = {}
    for t in transitions:
        room_type = t["room_type"]
        bucket = cell_bucket(room_type, t["pre"], split)
        for _, key in cell_keys(t["act"], room_type, bucket):
            grouped.setdefault(key, []).append(t)
    return {"split": split, "min_n": min_n,
            "cells": {key: _cell_from(samples) for key, samples in sorted(grouped.items())}}


# ------------------------------------------------------------------ RouteModel

def cell_keys(act, room_type: str, bucket: str) -> list[tuple[str, str]]:
    """The fallback chain of (level, cell key) for a room: the exact act cell, the
    all-acts cell of the same bucket, the all-buckets cell of the room type.
    Duplicate keys (rooms with bucket "all") keep their first level."""
    chain = (("act", f"{act}|{room_type}|{bucket}"),
             ("pooled", f"*|{room_type}|{bucket}"),
             ("type", f"*|{room_type}|all"))
    seen: set = set()
    keys = []
    for level, key in chain:
        if key not in seen:
            seen.add(key)
            keys.append((level, key))
    return keys


def _zero_cell() -> dict:
    return {"n": 0, "n_died": 0, "mean_delta": {k: 0.0 for k in DELTA_KEYS},
            "survivors": [], "death_hp": []}


class RouteModel:
    """Fitted route model, loaded from / saved to plain JSON.

    ``data`` = {"version": 1, "value": <fit_value_model()["model"]>,
    "transitions": <fit_transition_model()>, "meta": {...}, "validation": {...}}.
    """

    def __init__(self, data: dict):
        self.data = data
        self._value = data["value"]
        self._transitions = data["transitions"]
        self._prepared: dict = {}  # cell key -> _prepare_samples(...), built on first use

    @classmethod
    def load(cls, path: str) -> "RouteModel":
        with open(path) as fh:
            data = json.load(fh)
        version = data.get("version") if isinstance(data, dict) else None
        if version != MODEL_VERSION:
            raise ValueError(f"{path}: route model version {version!r}, this code reads "
                             f"version {MODEL_VERSION} -- refit with agent.fit_route_model")
        return cls(data)

    def save(self, path: str) -> None:
        """Write the JSON to a temp file beside ``path`` and rename it into place,
        so a crash or a concurrent reader never sees a half-written model."""
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w") as fh:
                json.dump(self.data, fh, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def value(self, features: dict, character: str) -> float:
        """Expected remaining global floors from this state (ridge prediction, >= 0)."""
        m = self._value
        x = feature_vector(features, character, m["features"])
        pred = m["intercept"] + sum(c * (v - mu) / (sc if sc else 1.0)
                                    for c, v, mu, sc in zip(m["coef"], x, m["mean"], m["scale"]))
        return max(0.0, float(pred))

    def _resolve(self, act: int, room_type: str, features: dict) -> tuple[str, str | None, dict]:
        t = self._transitions
        bucket = cell_bucket(room_type, features, t["split"])
        cells = t["cells"]
        for level, key in cell_keys(act, room_type, bucket):
            cell = cells.get(key)
            if cell is not None and cell["n"] >= t["min_n"]:
                return level, key, cell
        return "zero", None, _zero_cell()

    def resolve(self, act: int, room_type: str, features: dict) -> tuple[str, dict]:
        """(level, cell) for this room and state. The cell is the first of the exact
        act cell (level "act"), the all-acts cell of the bucket ("pooled") and the
        all-buckets cell of the room type ("type") that has at least ``min_n``
        samples; otherwise the zero cell (no delta, no death risk; level "zero")."""
        level, _, cell = self._resolve(act, room_type, features)
        return level, cell

    def cell(self, act: int, room_type: str, features: dict) -> dict:
        return self._resolve(act, room_type, features)[2]

    def death_prob(self, act: int, room_type: str, features: dict) -> float:
        """P(dying in this room at the state's current hp): the conditional
        estimate of death_prob_from_samples over the resolved cell."""
        hp = features.get("hp", 0)
        _, key, cell = self._resolve(act, room_type, features)
        if key is None:
            return 1.0 if hp <= 0 else 0.0
        prepared = self._prepared.get(key)
        if prepared is None:
            prepared = self._prepared[key] = _prepare_samples(cell["survivors"], cell["death_hp"])
        return _death_prob(prepared, hp)

    def step(self, act: int, room_type: str, features: dict, row: int) -> dict:
        """Expected features after SURVIVING the room on map row ``row``: the
        cell's mean deltas added, then hp clamped to [1, max_hp], max_hp >= 1,
        gold >= 0, deck_burst >= 0, deck_top8 to [0, 10], counts >= 0 and
        n_potions <= 3. The floor comes from the row, not from counting rooms: a
        room on row r is floor r + 1 (the floor of the next map_select), so paths
        of different lengths to the same row end on the same floor -- Act 2/3 open
        with an extra "Ancient" room on row 0. gf = (act - 1) * 17 + floor; act is
        unchanged."""
        delta = self.cell(act, room_type, features)["mean_delta"]
        s = dict(features)
        for key in DELTA_KEYS:
            s[key] = features.get(key, 0) + delta.get(key, 0.0)
        s["max_hp"] = max(1.0, s["max_hp"])
        s["hp"] = min(max(s["hp"], 1.0), s["max_hp"])
        s["gold"] = max(0.0, s["gold"])
        s["deck_burst"] = max(0.0, s["deck_burst"])
        s["deck_top8"] = min(max(s["deck_top8"], 0.0), MAX_CARD_SCORE)
        for key in ("deck_size", "n_upgraded", "n_relics"):
            s[key] = max(0.0, s[key])
        s["n_potions"] = min(max(s["n_potions"], 0.0), float(MAX_POTIONS))
        s["floor"] = row + 1
        s["gf"] = (act - 1) * ACT_FLOORS + row + 1
        return s
