"""Decision advisor for strategy-driving, directional action scoring."""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.card_scoring import (
    BROKEN_CARDS,
    _card_id_norm,
    best_smith_target,
    card_dimensions,
    deck_5turn_burst,
    deck_block_per_turn,
    deck_boss_versatility,
    deck_engine_efficiency,
    score_card_in_deck,
)
from agent.turn_planner import END_TURN_ACTION, NO_TARGET_SLOT, plan_action


@dataclass
class CandidateScore:
    command: dict
    score: float
    dimensions: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    hard_safety: bool = False


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def evaluate_deck(deck: list[dict]) -> dict[str, float]:
    """Return normalized directional deck scores."""
    burst = deck_5turn_burst(deck)
    block = deck_block_per_turn(deck)
    engine = deck_engine_efficiency(deck)
    boss = deck_boss_versatility(deck)
    dims = {"attack": 0.0, "defense": 0.0, "energy": 0.0, "draw": 0.0}
    for c in deck or []:
        cd = card_dimensions(c)
        for k in dims:
            dims[k] += cd.get(k, 0.0)
    n = max(len(deck or []), 1)
    return {
        "attack": _clamp01(max(burst / 200.0, (dims["attack"] / n) / 6.0)),
        "defense": _clamp01(max(block / 10.0, (dims["defense"] / n) / 5.0)),
        "cycle": _clamp01(engine),
        "energy": _clamp01((dims["energy"] / n) / 0.35),
        "boss_ready": _clamp01(0.45 * (burst / 200.0) + 0.35 * (block / 10.0) + 0.20 * boss),
    }


class DecisionAdvisor:
    """Rank legal actions for a game decision state and return an engine command."""

    def choose(self, state: dict) -> dict | None:
        decision = state.get("decision", "")
        if decision == "combat_play":
            return self._choose_combat(state)
        if decision == "card_reward":
            return self._choose_card_reward(state)
        if decision == "map_select":
            return self._choose_map(state)
        if decision == "rest_site":
            return self._choose_rest(state)
        return None

    def _choose_combat(self, state: dict) -> dict | None:
        action = plan_action(state)
        if action is None:
            return None
        if action == END_TURN_ACTION:
            return {"cmd": "action", "action": "end_turn"}
        slot = action // 4
        target_slot = action % 4
        args = {"card_index": slot}
        if target_slot != NO_TARGET_SLOT:
            args["target_index"] = target_slot
        return {"cmd": "action", "action": "play_card", "args": args}

    def _choose_card_reward(self, state: dict) -> dict:
        cards = state.get("cards") or []
        deck = (state.get("player") or {}).get("deck") or []
        if not cards:
            return {"cmd": "action", "action": "skip_card_reward"}
        if not any(c.get("id") or c.get("type") or c.get("stats") or c.get("description") for c in cards):
            return None
        base = evaluate_deck(deck)
        weakest = min(("attack", "defense", "cycle", "energy"), key=lambda k: base.get(k, 0.0))
        scored: list[CandidateScore] = []
        for i, c in enumerate(cards):
            cid = _card_id_norm(c)
            if cid in BROKEN_CARDS:
                continue
            card_score = score_card_in_deck(c, deck)
            if card_score <= 0.0:
                continue
            after = evaluate_deck(deck + [c])
            delta = {k: after.get(k, 0.0) - base.get(k, 0.0) for k in after}
            contrib = card_dimensions(c)
            fill_bonus = 0.20 if contrib.get(weakest, 0.0) > 0 else 0.0
            large_deck_penalty = max(0, len(deck) - 15) * 0.025
            score = (
                0.35 * delta.get("attack", 0.0)
                + 0.35 * delta.get("defense", 0.0)
                + 0.20 * delta.get("cycle", 0.0)
                + 0.10 * delta.get("energy", 0.0)
                + fill_bonus
                + min(card_score / 10.0, 1.0) * 0.18
                - large_deck_penalty
            )
            idx = c.get("index", i)
            scored.append(CandidateScore(
                {"cmd": "action", "action": "select_card_reward", "args": {"card_index": idx}},
                score,
                delta,
                f"fills {weakest}" if fill_bonus else "card reward delta",
            ))
        skip_score = 0.10 + max(0, len(deck) - 15) * 0.03
        if not scored or max(scored, key=lambda s: s.score).score <= skip_score:
            return {"cmd": "action", "action": "skip_card_reward"}
        return max(scored, key=lambda s: s.score).command

    def _choose_map(self, state: dict) -> dict | None:
        choices = state.get("choices") or []
        if not choices:
            return None
        player = state.get("player") or {}
        hp = float(player.get("hp", 80) or 80)
        max_hp = max(float(player.get("max_hp", 80) or 80), 1.0)
        hp_ratio = hp / max_hp
        deck = player.get("deck") or []
        deck_scores = evaluate_deck(deck)
        readiness = 0.5 * deck_scores["attack"] + 0.35 * deck_scores["defense"] + 0.15 * deck_scores["boss_ready"]
        floor = state.get("floor") or (state.get("context") or {}).get("floor", 1)
        scored = []
        for c in choices:
            t = str(c.get("type", "Unknown")).lower()
            risk = 0.0
            reward = 0.0
            if t == "elite":
                risk = 0.75 - 0.45 * readiness - 0.35 * hp_ratio
                reward = 0.35
            elif t == "monster":
                risk = 0.35 - 0.25 * readiness - 0.20 * hp_ratio
                reward = 0.22 if len(deck) < 17 else 0.10
            elif t in ("event", "unknown", "ancient"):
                risk = 0.08
                reward = 0.16
            elif t == "restsite":
                risk = -0.25 if hp_ratio < 0.70 else -0.05
                reward = 0.20
            elif t == "shop":
                gold = float(player.get("gold", 0) or 0)
                risk = -0.05
                reward = 0.25 if gold >= 75 else 0.05
            elif t == "treasure":
                risk = -0.10
                reward = 0.25
            elif t == "boss":
                risk = 0.20 - 0.25 * deck_scores["boss_ready"]
                reward = 0.0
            if hp_ratio < 0.40 and t in ("elite", "monster"):
                risk += 0.50
            if isinstance(floor, int) and floor >= 13 and t == "elite":
                risk += 0.30
            scored.append((reward - risk, c))
        best = max(scored, key=lambda x: x[0])[1]
        return {"cmd": "action", "action": "select_map_node", "args": {"col": best["col"], "row": best["row"]}}

    def _choose_rest(self, state: dict) -> dict | None:
        options = [o for o in (state.get("options") or []) if o.get("is_enabled", True)]
        if not options:
            return None
        player = state.get("player") or {}
        hp = float(player.get("hp", 0) or 0)
        max_hp = max(float(player.get("max_hp", 80) or 80), 1.0)
        hp_ratio = hp / max_hp
        heal = next((o for o in options if "heal" in str(o.get("option_id", "")).lower()), None)
        smith = next((o for o in options if "smith" in str(o.get("option_id", "")).lower()), None)
        if hp_ratio < 0.45 and heal is not None:
            return {"cmd": "action", "action": "choose_option", "args": {"option_index": heal["index"]}}
        if smith is not None and best_smith_target(player.get("deck") or []) is not None:
            return {"cmd": "action", "action": "choose_option", "args": {"option_index": smith["index"]}}
        if hp_ratio < 0.75 and heal is not None:
            return {"cmd": "action", "action": "choose_option", "args": {"option_index": heal["index"]}}
        choice = smith or heal or options[0]
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": choice["index"]}}
