"""Inert Fool a9 reward ranker, trained only on verified Solver branches.

Run explicitly with ``.venv/bin/python -m agent.offline_fool_ranker dataset.jsonl``.
No advisor/policy module imports this tool. Its JSON weights live only below
logs/experiments/fool-a9-offline/ and have no online loading path.

Schema 1 uses one record per card reward: full ``state`` + SHA256, engine
fingerprint, legal ``options`` (index identifies duplicate card IDs), the frozen
heuristic choice, and ``results`` for repeated next-combat branches. See the
collector's dataset.jsonl. Technical errors and unverified branches are never
losses. All options need three independent, stable repetitions at ONE common
budget and the same continuation and next combat. Labels compare survival
first, then remaining HP; overlapping observed HP ranges are left unordered.

Features are pre-choice facts and explicit candidate/context interactions.
There are no seed, option-index, raw card-ID, future-result or run-outcome
features. Fool effect semantics reuse the checked metadata helper (e.g. Cards
is not always draw); those estimates are features, never outcome labels.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from agent import fool_scoring

SCHEMA_VERSION = 1
FEATURE_VERSION = 1
HORIZON = "next_combat"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "logs/experiments/fool-a9-offline"
_ENERGY_RELICS = {"LANTERN", "VERY_HOT_COCOA", "VENERABLE_TEA_SET", "PHILOSOPHERS_STONE",
                  "PUMPKIN_CANDLE", "SOZU", "SPIKED_GAUNTLETS", "VELVET_CHOKER",
                  "WHISPERING_EARRING", "BLESSED_ANTLER", "BLOOD_SOAKED_ROSE", "BREAD"}
_CANDIDATE = ("skip", "cost", "x_cost", "attack_type", "skill_type", "power_type", "status_type",
              "upgraded", "attack", "defense", "draw", "energy", "aim", "hits", "sadness",
              "weak", "vulnerable", "aoe", "shot", "exhaust", "retain", "wah", "cho", "ki",
              "duplicate_count", "new_melodies")
_CONTEXT = ("hp_ratio", "hp_missing", "act", "floor", "deck_size", "deck_attack", "deck_defense",
            "deck_draw", "deck_energy", "deck_aim", "deck_sadness", "deck_cost", "deck_attack_fraction",
            "deck_skill_fraction", "deck_power_fraction", "deck_upgraded_fraction", "deck_shot_fraction",
            "deck_wah", "deck_cho", "deck_ki", "relic_count", "energy_relic", "potion_count", "gold")
_CROSSES = tuple((a, b) for a in ("skip", "attack", "defense", "draw", "energy", "cost")
                 for b in ("hp_missing", "deck_size", "act", "relic_count"))
_SPECIAL = ("attack_need", "defense_need", "draw_need", "energy_support", "aim_shots", "shot_aim",
            "sadness_support", "melody_support")
FEATURE_NAMES = tuple([f"candidate.{k}" for k in _CANDIDATE] + [f"context.{k}" for k in _CONTEXT]
                      + [f"interaction.{a}_x_{b}" for a, b in _CROSSES]
                      + [f"interaction.{k}" for k in _SPECIAL])


def state_digest(state: dict) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _is_sha256(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _number(value, default=0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return float(default)
    return float(value)


def _id(value) -> str:
    if isinstance(value, dict):
        value = value.get("id", "")
    return str(value).upper().removeprefix("CARD.").removeprefix("RELIC.")


def _card_values(card: dict, deck: list[dict]) -> dict[str, float]:
    meta = fool_scoring.METADATA.get(_id(card), {})
    stats = {str(k).lower(): _number(v) for k, v in (card.get("stats") or {}).items()}
    cost = _number(card.get("cost"), meta.get("cost", 0))
    if meta:
        effect = fool_scoring.effects(card, deck)
    else:
        # Unknown cards use only explicit numeric facts. Never treat a generic
        # `cards` DynamicVar as actual draw without a semantic annotation.
        effect = {"attack": stats.get("damage", 0) * max(1., stats.get("hitcount", 1)),
                  "defense": stats.get("block", 0), "draw": stats.get("draw", 0),
                  "energy": stats.get("energy", 0), "aim": stats.get("aimpower", 0),
                  "hits": max(1., stats.get("hitcount", 1)), "sadness": stats.get("sadness", 0),
                  "weak": stats.get("weakpower", stats.get("weak", 0)),
                  "vulnerable": stats.get("vulnerablepower", stats.get("vulnerable", 0))}
    kind = str(card.get("type") or meta.get("type", "")).lower()
    keywords = {str(k).lower() for k in card.get("keywords") or []}
    melodies = fool_scoring.melody_types(card) if meta else set()
    return {"cost": max(0., cost), "x_cost": float(cost < 0),
            "attack_type": float(kind == "attack"), "skill_type": float(kind == "skill"),
            "power_type": float(kind == "power"), "status_type": float(kind in {"status", "curse"}),
            "upgraded": float(bool(card.get("upgraded"))),
            **{k: _number(effect.get(k)) for k in ("attack", "defense", "draw", "energy", "aim", "hits", "sadness", "weak", "vulnerable")},
            "aoe": float(bool(meta.get("aoe") or card.get("target_type") == "AllEnemies")),
            "shot": float(bool(meta.get("shot"))), "exhaust": float("exhaust" in keywords),
            "retain": float("retain" in keywords),
            **{v.lower(): float(v in melodies) for v in ("WAH", "CHO", "KI")}}


def option_features(state: dict, option: dict) -> dict[str, float]:
    """Allowlisted pre-choice features; ID is used only for checked semantics."""
    player = state.get("player") or {}
    deck = player.get("deck") or []
    deck_values = [_card_values(card, deck) for card in deck]
    size = max(1, len(deck))
    avg = lambda key: sum(c[key] for c in deck_values) / size
    context = {"hp_ratio": _number(player.get("hp")) / max(1., _number(player.get("max_hp"), 1)),
               "act": _number((state.get("context") or {}).get("act")),
               "floor": _number((state.get("context") or {}).get("floor")),
               "deck_size": float(len(deck)), "gold": _number(player.get("gold")),
               **{f"deck_{k}": avg(k) for k in ("attack", "defense", "draw", "energy", "aim", "sadness", "cost", "wah", "cho", "ki")},
               **{f"deck_{a}_fraction": avg(b) for a, b in (("attack", "attack_type"), ("skill", "skill_type"),
                  ("power", "power_type"), ("upgraded", "upgraded"), ("shot", "shot"))},
               "relic_count": float(len(player.get("relics") or [])),
               "energy_relic": float(bool({_id(r) for r in player.get("relics") or []} & _ENERGY_RELICS)),
               "potion_count": float(sum(bool(p) for p in player.get("potions") or []))}
    context["hp_missing"] = 1. - context["hp_ratio"]
    skip = option.get("index") == -1 and option.get("id") == "SKIP"
    candidate = {k: 0. for k in _CANDIDATE}
    candidate["skip"] = float(skip)
    if not skip:
        card = option.get("card") or {}
        candidate.update(_card_values(card, deck))
        candidate["duplicate_count"] = float(sum(_id(c) == _id(card) for c in deck))
        candidate["new_melodies"] = sum(candidate[k] * (context[f"deck_{k}"] == 0) for k in ("wah", "cho", "ki"))
    values = {**{f"candidate.{k}": candidate[k] for k in _CANDIDATE},
              **{f"context.{k}": context[k] for k in _CONTEXT},
              **{f"interaction.{a}_x_{b}": candidate[a] * context[b] for a, b in _CROSSES}}
    values.update({"interaction.attack_need": candidate["attack"] / (1 + context["deck_attack"]),
                   "interaction.defense_need": candidate["defense"] / (1 + context["deck_defense"]),
                   "interaction.draw_need": candidate["draw"] / (1 + context["deck_draw"]),
                   "interaction.energy_support": candidate["cost"] * (context["deck_energy"] + context["energy_relic"]),
                   "interaction.aim_shots": candidate["aim"] * context["deck_shot_fraction"],
                   "interaction.shot_aim": candidate["shot"] * candidate["hits"] * context["deck_aim"],
                   "interaction.sadness_support": candidate["sadness"] * context["deck_sadness"],
                   "interaction.melody_support": sum(candidate[k] * context[f"deck_{k}"] for k in ("wah", "cho", "ki"))})
    return {key: float(values[key]) for key in FEATURE_NAMES}


@dataclass
class LabeledCase:
    case_id: str
    seed: str
    record: dict
    budget_ms: int
    option_stats: dict[int, dict]
    pairs: list[tuple[int, int, float]]  # better index, worse index, confidence
    oracle_indices: set[int]

    @property
    def vectors(self):
        return {o["index"]: np.array(list(option_features(self.record["state"], o).values()))
                for o in self.record["options"]}


def _validate_case(case: dict):
    if case.get("schema_version") != SCHEMA_VERSION or case.get("character") != "Fool" or case.get("ascension") != 9:
        raise ValueError("wrong_schema_or_character")
    if not case.get("case_id") or not case.get("seed") or case.get("horizon") != HORIZON:
        raise ValueError("missing_identity_or_wrong_horizon")
    state = case.get("state") or {}
    if (case.get("restore_verified") is not True or state.get("decision") != "card_reward"
            or state_digest(state) != case.get("state_sha256")):
        raise ValueError("unverified_state")
    future_hash = case.get("future_control_initial_sha256")
    if (case.get("future_control_verified") is not True or not _is_sha256(future_hash)
            or len(case.get("future_control_checks") or []) < 3
            or any(h != future_hash for h in case["future_control_checks"])
            or len(case.get("restore_checks") or []) < 3
            or any(h != case["state_sha256"] for h in case["restore_checks"])):
        raise ValueError("unverified_future_control")
    engine = case.get("engine") or {}
    if (engine.get("game_version") != "v0.113.1"
            or not _is_sha256(engine.get("dll_sha256")) or not engine.get("policy_commit")
            or ("cli_sha256" in engine and not _is_sha256(engine["cli_sha256"])) or not case.get("continuation_id")):
        raise ValueError("missing_engine_or_continuation")
    options = case.get("options") or []
    indices = [o.get("index") for o in options]
    if len(options) < 2 or len(set(indices)) != len(indices):
        raise ValueError("invalid_option_indices")
    offered = {c["index"]: c for c in state.get("cards") or []}
    expected = set(offered) | ({-1} if state.get("can_skip") is True else set())
    if set(indices) != expected:
        raise ValueError("incomplete_legal_options")
    for option in options:
        index = option["index"]
        command = {"cmd": "action", "action": "skip_card_reward"} if index == -1 else {
            "cmd": "action", "action": "select_card_reward", "args": {"card_index": index}}
        if option.get("command") != command:
            raise ValueError("option_command_mismatch")
        if index == -1:
            if option.get("id") != "SKIP" or option.get("card") is not None:
                raise ValueError("invalid_skip")
        elif option.get("card") != offered[index] or option.get("id") != offered[index].get("id"):
            raise ValueError("option_card_mismatch")
    heuristic = next((o for o in options if o["index"] == case.get("heuristic_option_index")), None)
    if heuristic is None or heuristic["id"] != case.get("heuristic_option_id"):
        raise ValueError("missing_heuristic_baseline")


def _branch_error(case: dict, row: dict) -> str | None:
    if row.get("technical_failure_kind"):
        return "technical_failure"
    if row.get("status") != "completed":
        return "status_" + str(row.get("status"))
    if row.get("restore_verified") is not True or row.get("restore_sha256") != case["state_sha256"]:
        return "unverified_branch_restore"
    if row.get("engine_dll_sha256") != case["engine"]["dll_sha256"]:
        return "branch_engine_mismatch"
    if (case["engine"].get("cli_sha256") is not None
            and row.get("engine_cli_sha256") != case["engine"]["cli_sha256"]):
        return "branch_cli_mismatch"
    if not _is_sha256(row.get("combat_initial_sha256")):
        return "missing_initial_combat_fingerprint"
    for field in ("seed", "case_id", "horizon", "continuation_id"):
        if row.get(field) != case[field]:
            return "branch_" + field + "_mismatch"
    if not any(o["index"] == row.get("option_index") and o["id"] == row.get("option_id") for o in case["options"]):
        return "unknown_option"
    if type(row.get("repeat")) is not int or type(row.get("budget_ms")) is not int or row["budget_ms"] <= 0:
        return "missing_repeat_or_budget"
    if (any(type(row.get(k)) is not int or row[k] <= 0 for k in ("plans", "solver_actions"))
            or row.get("fallback_actions", 0)):
        return "solver_not_executed"
    combat = row.get("combat_identity")
    if (not isinstance(row.get("survived"), bool) or not isinstance(combat, dict)
            or any(type(combat.get(k)) is not int or combat[k] < 1 for k in ("act", "floor"))
            or not (combat.get("encounter") or combat.get("enemyIds"))):
        return "missing_combat_outcome"
    for hp, max_hp in (("hp_start", "max_hp_start"), ("hp_end", "max_hp_end")):
        if (type(row.get(hp)) not in (int, float) or type(row.get(max_hp)) not in (int, float)
                or not math.isfinite(row[hp]) or not math.isfinite(row[max_hp])
                or row[max_hp] <= 0 or not 0 <= row[hp] <= row[max_hp]):
            return "invalid_hp"
    if (row["hp_end"] > 0) != row["survived"]:
        return "inconsistent_survival_hp"
    return None


def _label_case(case: dict, valid: list[dict], min_repetitions: int, hp_margin: float) -> LabeledCase:
    indices = [o["index"] for o in case["options"]]
    by_budget = defaultdict(lambda: defaultdict(list))
    for row in valid:
        by_budget[row["budget_ms"]][row["option_index"]].append(row)
    common = [b for b, rows in by_budget.items() if all(len(rows[i]) >= min_repetitions for i in indices)]
    if not common:
        raise ValueError("insufficient_verified_repetitions")
    budget = max(common)
    groups = by_budget[budget]
    selected = [r for rows in groups.values() for r in rows]
    if len({state_digest(r["combat_identity"]) for r in selected}) != 1:
        raise ValueError("different_next_combat")
    stats = {}
    for index in indices:
        rows = groups[index]
        if len({r["repeat"] for r in rows}) != len(rows):
            raise ValueError("duplicate_repetition")
        if len({r["survived"] for r in rows}) != 1:
            raise ValueError("unstable_survival_outcome")
        if len({(r["hp_start"], r["max_hp_start"]) for r in rows}) != 1:
            raise ValueError("unstable_combat_start")
        if len({r["combat_initial_sha256"] for r in rows}) != 1:
            raise ValueError("unstable_initial_combat_observation")
        hp = [r["hp_end"] for r in rows]
        stats[index] = {"valid_repetitions": len(rows), "survival_rate": float(rows[0]["survived"]),
                        "survival_agreement": 1., "hp_end_mean": float(np.mean(hp)),
                        "hp_end_std": float(np.std(hp, ddof=1)), "hp_end_min": float(min(hp)),
                        "hp_end_max": float(max(hp)), "hp_start": float(rows[0]["hp_start"]),
                        "hp_delta_mean": float(np.mean([r["hp_end"] - r["hp_start"] for r in rows]))}
    pairs = []
    for a, b in combinations(indices, 2):
        x, y = stats[a], stats[b]
        if x["survival_rate"] != y["survival_rate"]:
            better, worse = (a, b) if x["survival_rate"] > y["survival_rate"] else (b, a)
            confidence = 1.
        else:
            gap_ab = x["hp_end_min"] - y["hp_end_max"]
            gap_ba = y["hp_end_min"] - x["hp_end_max"]
            if max(gap_ab, gap_ba) <= hp_margin:
                continue
            better, worse = (a, b) if gap_ab > gap_ba else (b, a)
            spread = x["hp_end_std"] + y["hp_end_std"]
            confidence = max(.1, min(1., max(gap_ab, gap_ba) / (5. + spread)))
        pairs.append((better, worse, confidence))
    # The set of confidently undominated options includes near ties rather
    # than pretending that solver noise identifies a unique best choice.
    oracle = set(indices) - {worse for _, worse, _ in pairs}
    return LabeledCase(case["case_id"], case["seed"], case, budget, stats, pairs, oracle)


def prepare_cases(records: Iterable[dict], min_repetitions=3, hp_margin=1.) -> tuple[list[LabeledCase], dict]:
    """Filter invalid branches, then require a complete comparable decision."""
    accepted, rejected, excluded = [], {}, Counter()
    records = list(records)
    ids = Counter(str(c.get("case_id")) for c in records)
    for case in records:
        key = str(case.get("case_id", "missing"))
        try:
            if ids[key] != 1:
                raise ValueError("duplicate_case_id")
            _validate_case(case)
            valid = []
            for row in case.get("results") or []:
                reason = _branch_error(case, row)
                if reason:
                    excluded[reason] += 1
                else:
                    valid.append(row)
            accepted.append(_label_case(case, valid, min_repetitions, hp_margin))
        except (ValueError, KeyError, TypeError) as exc:
            rejected[key] = str(exc)
    return accepted, {"input_cases": len(records), "accepted_cases": len(accepted),
                       "rejected_cases": rejected, "excluded_results": dict(excluded),
                       "minimum_repetitions": min_repetitions, "hp_separation_margin": hp_margin}


def split_by_seed(cases: list[LabeledCase]) -> dict[str, list[LabeledCase]]:
    """Stable 60/20/20 seed grouping, with at least one seed per held-out set."""
    seeds = sorted({c.seed for c in cases}, key=lambda s: hashlib.sha256(("fool-a9-split-v1:" + s).encode()).hexdigest())
    if len(seeds) < 3:
        raise ValueError("at least three seed groups are required")
    held_out = max(1, len(seeds) // 5)
    seed_parts = {"train": set(seeds[:-2 * held_out]), "validation": set(seeds[-2 * held_out:-held_out]),
                  "test": set(seeds[-held_out:])}
    return {part: sorted((c for c in cases if c.seed in group), key=lambda c: c.case_id)
            for part, group in seed_parts.items()}


def pairwise_matrix(cases: list[LabeledCase]):
    """Symmetric preference differences; each state has total weight one."""
    x, y, weights = [], [], []
    for case in cases:
        vectors = case.vectors
        norm = sum(confidence for _, _, confidence in case.pairs)
        for better, worse, confidence in case.pairs:
            delta = vectors[better] - vectors[worse]
            x.extend((delta, -delta))
            y.extend((1, 0))
            weights.extend((confidence / (2 * norm), confidence / (2 * norm)))
    return np.asarray(x, dtype=float).reshape((-1, len(FEATURE_NAMES))), np.asarray(y), np.asarray(weights)


def evaluate(cases: list[LabeledCase], scorer: Callable[[LabeledCase], dict[int, float]]) -> dict:
    """Exact expected random baseline; no Monte Carlo evaluation noise."""
    totals = {name: Counter() for name in ("ranker", "heuristic", "random", "always_skip")}
    for case in cases:
        if not case.pairs:
            continue
        indices = list(case.option_stats)
        model_scores = scorer(case)
        heuristic = case.record["heuristic_option_index"]
        policies = {"ranker": model_scores, "heuristic": {i: float(i == heuristic) for i in indices},
                    "always_skip": {i: float(i == -1) for i in indices}, "random": {i: 0. for i in indices}}
        for name, scores in policies.items():
            if name == "always_skip" and -1 not in indices:
                continue
            chosen = max(indices, key=lambda i: scores[i])
            distribution = {i: 1 / len(indices) for i in indices} if name == "random" else {chosen: 1.}
            total = totals[name]
            total["states"] += 1
            total["top"] += sum(p for i, p in distribution.items() if i in case.oracle_indices)
            total["hp_regret"] += max(s["hp_end_mean"] for s in case.option_stats.values()) - sum(
                p * case.option_stats[i]["hp_end_mean"] for i, p in distribution.items())
            total["outcome_regret"] += max(s["survival_rate"] for s in case.option_stats.values()) - sum(
                p * case.option_stats[i]["survival_rate"] for i, p in distribution.items())
            for better, worse, _ in case.pairs:
                total["pairs"] += 1
                total["correct_pairs"] += 1. if scores[better] > scores[worse] else (.5 if scores[better] == scores[worse] else 0.)
    return {name: {"states": int(t["states"]), "pairs": int(t["pairs"]),
                   "top_choice_agreement": t["top"] / t["states"] if t["states"] else None,
                   "pairwise_accuracy": t["correct_pairs"] / t["pairs"] if t["pairs"] else None,
                   "hp_regret": t["hp_regret"] / t["states"] if t["states"] else None,
                   "outcome_regret": t["outcome_regret"] / t["states"] if t["states"] else None}
            for name, t in totals.items()}


def score_options(record: dict, artifact: dict) -> dict[int, float]:
    if artifact.get("feature_version") != FEATURE_VERSION or artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("offline feature schema mismatch")
    weights = np.array(artifact["coefficients"])
    scale = np.array(artifact["feature_scale"])
    return {o["index"]: float((np.array(list(option_features(record["state"], o).values())) / scale) @ weights)
            for o in record["options"]}


def _fit(cases: list[LabeledCase], regularization: float) -> dict:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    # Fit scales only on training states; shared context columns cancel in
    # differences, while explicit context x candidate features remain useful.
    scale = StandardScaler().fit(np.stack([v for case in cases for v in case.vectors.values()])).scale_
    x, y, weights = pairwise_matrix(cases)
    fit = LogisticRegression(C=regularization, fit_intercept=False,
                             solver="lbfgs", max_iter=2000, random_state=0)
    fit.fit(x / scale, y, sample_weight=weights)
    return {"schema_version": SCHEMA_VERSION, "feature_version": FEATURE_VERSION,
            "offline_only": True, "model_type": "pairwise_logistic_l2", "horizon": HORIZON,
            "feature_names": list(FEATURE_NAMES), "feature_scale": scale.tolist(),
            "coefficients": fit.coef_[0].tolist(), "regularization_C": regularization}


def train_records(records: list[dict]) -> tuple[dict | None, dict]:
    cases, diagnostics = prepare_cases(records)
    report = {"schema_version": SCHEMA_VERSION, "scope": "next_combat_only",
              "validation_status": "insufficient_data", "full_run_win_rate_validated": False,
              "filtering": diagnostics, "accepted_seeds": sorted({c.seed for c in cases}),
              "label_definition": "stable survival first, then separated observed remaining-HP ranges",
              "top_choice_agreement_definition": "membership in confidently undominated options; only states with informative pairs count",
              "pairwise_accuracy_definition": "unweighted confident pairs; heuristic/skip score their chosen option above the others, remaining ties count half",
              "regret_units": {"hp_regret": "remaining HP vs best mean option", "outcome_regret": "survival probability vs best option"},
              "stability": {c.case_id: {"budget_ms": c.budget_ms, "options": c.option_stats,
                                        "confident_pairs": len(c.pairs), "oracle_indices": sorted(c.oracle_indices)} for c in cases}}
    if len(cases) < 24 or len(report["accepted_seeds"]) < 6:
        report["reason"] = "need at least 24 verified decisions across 6 seeds"
        return None, report
    fingerprints = {state_digest(c.record["engine"]) for c in cases}
    if len(fingerprints) != 1:
        report["reason"] = "engine or policy fingerprints differ; collect and train one cohort"
        return None, report
    splits = split_by_seed(cases)
    report["splits"] = {part: {"states": len(rows), "seeds": sorted({c.seed for c in rows}),
                               "informative_states": sum(bool(c.pairs) for c in rows),
                               "pairs": sum(len(c.pairs) for c in rows)} for part, rows in splits.items()}
    if any(not any(c.pairs for c in rows) for rows in splits.values()):
        report["reason"] = "train, validation and test each need distinguishable stable labels"
        return None, report
    trials = []
    for regularization in (.01, .1, 1.):
        model = _fit(splits["train"], regularization)
        metrics = evaluate(splits["validation"], lambda case: score_options(case.record, model))
        trials.append((model, metrics))
    model, _ = max(trials, key=lambda trial: (trial[1]["ranker"]["pairwise_accuracy"],
                    trial[1]["ranker"]["top_choice_agreement"], -trial[0]["regularization_C"]))
    report["validation_trials"] = [{"C": m["regularization_C"], "metrics": metrics["ranker"]} for m, metrics in trials]
    report["metrics"] = {part: evaluate(rows, lambda case: score_options(case.record, model)) for part, rows in splits.items()}
    report["validation_status"] = "offline_seed_held_out"
    report["reason"] = "held-out next-combat labels only; does not establish full-run or live-policy improvement"
    model["training_seeds"] = report["splits"]["train"]["seeds"]
    model["validation_seeds"] = report["splits"]["validation"]["seeds"]
    model["test_seeds"] = report["splits"]["test"]["seeds"]
    model["engine"] = cases[0].record["engine"]
    model["feature_source_sha256"] = state_digest({"learner": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "fool_scoring": hashlib.sha256(Path(fool_scoring.__file__).read_bytes()).hexdigest(), "metadata": fool_scoring.METADATA})
    return model, report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="collector schema-1 dataset.jsonl")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    output = args.out.resolve()
    if not output.is_relative_to(DEFAULT_OUTPUT.resolve()):
        parser.error("--out must remain below this checkout's logs/experiments/fool-a9-offline/")
    records = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    model, report = train_records(records)
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    report["model_written"] = model is not None
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = output / f"report-{stamp}.json"
    if model is not None:
        model["dataset_sha256"] = report["dataset_sha256"]
        artifact_path = output / f"ranker-{stamp}.json"
        artifact_path.write_text(json.dumps(model, indent=2, allow_nan=False) + "\n")
        report["artifact"] = str(artifact_path)
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"validation_status": report["validation_status"], "model_written": model is not None,
                      "report": str(report_path), "reason": report["reason"]}))
    return 0 if model is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
