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

# Env vars that change what greedy_action picks: play_full_run.GREEDY_KNOBS plus
# three it does not stamp. The advisor warns when any is set; the corpus builder
# refuses to run.
GREEDY_BEHAVIOR_ENV = (
    "STS2_RANDOMIZE", "STS2_DECISION_ADVISOR", "STS2_CARD_THRESHOLD_LIFT",
    "STS2_BASIC_PURGE_ALL", "STS2_CARD_QUALITY_GATE",
    "STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL",
)

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
    """Put greedy's hidden per-process state back to its import-time value.

    card_reward rewrites card_scoring._MC_CONTEXT on every call and later
    decisions read it, so a replay that should match a fresh process must
    reset it at the start of each game."""
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
        entry = {"kind": kind, "index": int(index),
                 "score": None if score is None else float(score),
                 "status": status, "components": comps}
        if detail is not None:
            entry["detail"] = detail
        self._options[(kind, int(index))] = entry

    def rule(self, key: str, outcome: str, **values) -> None:
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
