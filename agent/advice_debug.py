"""Debug data for the advice service's `debug` reply key (never read by the panel).

The mod writes the raw reply into advisor_trace.jsonl and ignores unknown top-level
keys, so what is added here lets tools/campaign_report.py tell WHY a decision looked
the way it did (an event whose options greedy cannot score, how far a card reward was
from its threshold) without touching the rule keys the in-game panel renders.

debug_info only reads: it never changes the state or the evidence.
"""
from __future__ import annotations

from agent.card_scoring import deck_quality_metrics

_EPS = 1e-9


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    return value


def _rounded(metrics: dict) -> dict:
    return {key: _round(value) for key, value in metrics.items()}


def _options_by_index(evidence: dict, kind: str | None = None) -> dict:
    found = {}
    for option in evidence.get("options") or []:
        if not isinstance(option, dict) or (kind and option.get("kind") != kind):
            continue
        found[option.get("index")] = option
    return found


def _rule_values(evidence: dict, *keys: str) -> dict:
    for rule in evidence.get("rules") or []:
        if isinstance(rule, dict) and rule.get("key") in keys:
            return rule.get("values") or {}
    return {}


def _is_zero(option: dict | None) -> bool:
    score = _num((option or {}).get("score"))
    return score is None or abs(score) < _EPS


def event_debug(state: dict, evidence: dict) -> dict | None:
    """Options greedy gave no score and no reason for (a title its keyword table lacks)."""
    scored = _options_by_index(evidence)
    unscored, available = [], 0
    all_zero = True
    for option in state.get("options") or []:
        if not isinstance(option, dict) or option.get("is_locked"):
            continue
        available += 1
        ev = scored.get(option.get("index"))
        if not _is_zero(ev):
            all_zero = False
        elif not (ev or {}).get("components"):
            unscored.append(option.get("title"))
    if not unscored:
        return None
    return {"event_unscored": unscored, "event_all_zero": available > 0 and all_zero}


def card_reward_debug(state: dict, evidence: dict) -> dict | None:
    """Deck metrics before/after the best-scoring card and its margin over the threshold."""
    player = state.get("player") or {}
    deck = player.get("deck")
    cards = state.get("cards")
    if not isinstance(deck, list) or not isinstance(cards, list):
        return None
    ok = [o for o in _options_by_index(evidence, "card").values()
          if o.get("status") == "ok" and _num(o.get("score")) is not None]
    values = _rule_values(evidence, "card_reward.pick_best", "card_reward.below_threshold")
    block = {"deck_size": len(deck), "best_index": None, "best_score": _round(_num(values.get("best_score"))),
             "threshold": _round(_num(values.get("threshold"))), "margin": None,
             "metrics_before": _rounded(deck_quality_metrics(deck)), "metrics_after_best": None}
    if ok:
        best = max(ok, key=lambda o: o["score"])
        block["best_index"] = best.get("index")
        block["best_score"] = _round(float(best["score"]))
        card = next((c for c in cards if isinstance(c, dict) and c.get("index") == best.get("index")), None)
        if card is not None:
            block["metrics_after_best"] = _rounded(deck_quality_metrics(deck + [card]))
    if block["best_score"] is not None and block["threshold"] is not None:
        block["margin"] = _round(block["best_score"] - block["threshold"])
    return {"card_reward": block}


def debug_info(state: dict, evidence: dict) -> dict | None:
    """The `debug` object for an advice reply, or None for decisions without one."""
    decision = state.get("decision")
    if decision == "event_choice":
        return event_debug(state, evidence)
    if decision == "card_reward":
        return card_reward_debug(state, evidence)
    return None
