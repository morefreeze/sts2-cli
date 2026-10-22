# agent/greedy_trace.py
"""Evidence recorder for greedy_action -- the in-game Sts2Advisor's reasons.

greedy_action(state, trace=GreedyTrace()) records, in the code that makes each
decision, the numbers that decided it: per-option scores with the components
that were summed into them, and the ordered rule checks (fired / skipped).
trace=None -- every existing caller -- records nothing and runs the same code.

A key outside TRACE_KEYS raises KeyError, so a typo fails the advisor's tests
(and, at runtime, turns into "no advice") instead of reaching the panel.
"""
from __future__ import annotations

import hashlib
import os

# Env vars that change what greedy_action picks: play_full_run.GREEDY_KNOBS plus
# three it does not stamp (STS2_CARD_PICK_TEMP, STS2_MC_ROLLOUT,
# STS2_VANTOM_REST_HEAL) and six that agent/card_scoring.py reads
# (STS2_ARCHETYPE, STS2_BOSS_READINESS_LIFT, STS2_CTX_WEIGHTS,
# STS2_OVERRIDE_DELTA_CAP, STS2_PREDICTOR_TARGET, STS2_MC_SMART_THRESH). Several
# of those are read at import time, so they must be set before `agent` is
# imported. STS2_MC_DIAG and the CombatEnv-only vars (STS2_MAP_PLANNER,
# STS2_RUN_EPISODE, STS2_STUCK_DUMP, ...) were reviewed and are not on
# greedy_action's path. The advisor warns when any is set; the corpus builder
# refuses to run.
GREEDY_BEHAVIOR_ENV = (
    "STS2_RANDOMIZE", "STS2_DECISION_ADVISOR", "STS2_CARD_THRESHOLD_LIFT",
    "STS2_BASIC_PURGE_ALL", "STS2_CARD_QUALITY_GATE",
    "STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL",
    "STS2_ARCHETYPE", "STS2_BOSS_READINESS_LIFT", "STS2_CTX_WEIGHTS",
    "STS2_OVERRIDE_DELTA_CAP", "STS2_PREDICTOR_TARGET", "STS2_MC_SMART_THRESH",
)

# Local data files greedy_action's choices depend on (each loaded by
# agent/card_scoring.py; mostly gitignored under data/), repo-relative. The
# golden corpus records their sha256 so a changed file fails loudly, not as a
# mysterious golden mismatch.
GREEDY_DATA_FILES = (
    "data/advisor_card_ratings.json", "data/advisor_card_tags.json",
    "data/card_empirical.json", "data/card_metadata.json",
    "data/card_q_values.json", "data/card_synergies.py",
    "data/deck_predictor.pkl", "data/deck_predictor_v2.pkl",
    "data/deck_predictor_hp.pkl", "data/ironclad_card_tags.json",
    "data/ironclad_cards.json",
)


def greedy_data_sha256(root: str | None = None) -> dict:
    """{GREEDY_DATA_FILES entry: sha256 hex, or None if the file is absent}."""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = {}
    for rel in GREEDY_DATA_FILES:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            out[rel] = None
            continue
        with open(path, "rb") as f:
            out[rel] = hashlib.sha256(f.read()).hexdigest()
    return out


# card_scoring._MC_CONTEXT as the module defines it at import time.
MC_CONTEXT_START = {"hp": 80, "max_hp": 80, "floor": 5}

# One key per `if` branch of combat_env._score_event_option, in source order.
EVENT_KEYWORD_KEYS = (
    "event.kw.lose_max_hp", "event.kw.curse", "event.kw.lose_all_gold",
    "event.kw.lose_gold", "event.kw.torment", "event.kw.take_damage",
    "event.kw.lose_hp", "event.kw.downgrade", "event.kw.add_basic",
    "event.kw.rare_card", "event.kw.uncommon_card", "event.kw.card_reward",
    "event.kw.remove_card", "event.kw.remove_card_hp_cost", "event.kw.relic",
    "event.kw.relic_with_downside", "event.kw.upgrade", "event.kw.transform",
    "event.kw.gain_gold", "event.kw.gain_max_hp", "event.kw.potion",
    "event.kw.heal", "event.kw.colorless_card", "event.kw.gain_strength",
    "event.kw.enemy_strength", "event.kw.gain_dexterity",
    "event.kw.energy_each_turn", "event.kw.energy_once",
)

TRACE_KEYS = frozenset({
    # greedy_action itself
    "greedy.randomized", "greedy.decision_advisor", "greedy.unhandled",
    # card_reward rules
    "card_reward.no_cards", "card_reward.all_filtered", "card_reward.all_broken",
    "card_reward.below_threshold", "card_reward.pick_best",
    # card score components (pick_best_card sums the first nine, in this order)
    "card.base", "card.set_bonus", "card.tag_synergy", "card.combat_type",
    "card.boss_defense_gap", "card.boss_readiness", "card.quality_lift",
    "card.archetype", "card.q_adjust",
    "card.score", "card.in_deck_score",
    # rest_site rules, in rest_site_action's order
    "rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
    "rest.must_upgrade", "rest.heal_threshold", "rest.default_smith",
    "rest.fallback", "rest.no_choice",
    # event_choice
    "event.pick_best", "event.no_available", *EVENT_KEYWORD_KEYS,
    # bundle_select
    "bundle.single", "bundle.pick_best", "bundle.card",
    # card_select
    "card_select.rest_best", "card_select.combat_best", "card_select.top_scores",
    "card_select.lowest_scores", "card_select.no_cards",
    # shop rules, in greedy_action's order, and item score components
    "shop.heal_potion", "shop.remove_basic", "shop.remove_value",
    "shop.card_must_buy", "shop.card_threshold", "shop.relic", "shop.potion",
    "shop.leave", "shop.relic_score", "shop.potion_score",
    # map_select (HpAwareMapStrategy) rule and priority components
    "map.pick_min_priority", "map.base_priority", "map.shop_low_gold",
    "map.elite_early_floor", "map.elite_hp_danger", "map.elite_hp_low",
    "map.elite_act2_healthy", "map.elite_act2", "map.elite_strong_midgame",
    "map.elite_pre_boss", "map.elite_act2_boss_zone",
})

RULE_OUTCOMES = ("fired", "skipped")
OPTION_STATUSES = ("ok", "filtered", "locked", "unstocked", "unaffordable", "broken")


def _check_key(key: str) -> None:
    if key not in TRACE_KEYS:
        raise KeyError(f"unknown greedy trace key {key!r}")


def reset_greedy_context() -> None:
    """Reset card_scoring._MC_CONTEXT to its import-time value (only that).

    card_reward rewrites card_scoring._MC_CONTEXT on every call and later
    decisions read it, so a replay that should match a fresh process must
    reset it at the start of each game.

    greedy also reads local, gitignored data files under data/
    (card_q_values.json, card_empirical.json, card_metadata.json,
    ironclad_card_tags.json, ironclad_cards.json, deck_predictor*.pkl); those
    are not reset -- they are part of the configuration (the corpus records
    their hashes)."""
    from agent import card_scoring
    card_scoring._MC_CONTEXT.clear()
    card_scoring._MC_CONTEXT.update(MC_CONTEXT_START)


class GreedyTrace:
    """What one greedy_action call decided with. See the module docstring."""

    def __init__(self) -> None:
        self._options: dict[tuple[str, int], dict] = {}
        self.rules: list[dict] = []

    def option(self, index, *, kind: str, score=None, status: str = "ok",
               components=(), detail=None) -> None:
        """Record one option. `components` is [(key, value, detail-or-None)];
        for status "ok" their values sum (in order) to `score`."""
        if status not in OPTION_STATUSES:
            raise ValueError(f"unknown option status {status!r}")
        comps = []
        for key, value, extra in components:
            _check_key(key)
            item = {"key": key, "value": float(value)}
            if extra is not None:
                item["detail"] = extra
            comps.append(item)
        if (kind, int(index)) in self._options:
            raise ValueError(f"option {kind}:{int(index)} recorded twice")
        entry = {"kind": kind, "index": int(index),
                 "score": None if score is None else float(score),
                 "status": status, "components": comps}
        if detail is not None:
            entry["detail"] = detail
        self._options[(kind, int(index))] = entry

    def rule(self, key: str, outcome: str, /, **values) -> None:
        _check_key(key)
        if outcome not in RULE_OUTCOMES:
            raise ValueError(f"unknown rule outcome {outcome!r}")
        self.rules.append({"key": key, "outcome": outcome, "values": values})

    def score_of(self, kind: str, index) -> float | None:
        return self._options[(kind, int(index))]["score"]

    def ok_scores(self, kind: str) -> list[float]:
        return [o["score"] for (k, _), o in self._options.items()
                if k == kind and o["status"] == "ok" and o["score"] is not None]

    def fired(self) -> list[dict]:
        return [r for r in self.rules if r["outcome"] == "fired"]

    def to_dict(self) -> dict:
        return {"options": [self._options[k] for k in sorted(self._options)],
                "rules": list(self.rules)}
