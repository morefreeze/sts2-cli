# Dynamic Decision Advisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic strategy-driving advisor that ranks candidate actions with directional scores and integrates it into `greedy_action()`.

**Architecture:** A new `agent/decision_advisor.py` provides focused evaluators and a `DecisionAdvisor.choose(state)` entry point. It reuses `card_scoring.py` for deck metrics and card deltas, `strategy.py` for fallback rest/map behavior where needed, and `turn_planner.py` for combat tactical choices. `agent/combat_env.py::greedy_action()` delegates supported decisions to the advisor first, then falls back to existing logic.

**Tech Stack:** Python 3.11, pytest, existing STS2 agent modules. No new dependencies.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent/decision_advisor.py` | New advisor layer: score envelopes, deck dimensions, reward/map/rest/combat selection, explanation metadata. |
| `agent/combat_env.py` | Delegate `greedy_action()` to `DecisionAdvisor` and retain existing heuristic fallback. |
| `tests/agent/test_decision_advisor.py` | Unit tests for reward deltas, map risk, rest choice, combat command conversion, and fallback behavior. |

## Task 1: Reward, map, rest, and combat advisor tests

**Files:**
- Create: `tests/agent/test_decision_advisor.py`
- Create: `agent/decision_advisor.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_decision_advisor.py` with:

```python
from agent.decision_advisor import DecisionAdvisor, evaluate_deck


def card(cid, *, cost=1, ctype="Attack", damage=0, block=0, draw=0, index=0):
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
        "rarity": "Common",
        "stats": stats,
        "description": "",
        "index": index,
    }


def base_state(decision, **extra):
    state = {
        "decision": decision,
        "floor": 5,
        "player": {"hp": 60, "max_hp": 80, "gold": 0, "deck": []},
    }
    state.update(extra)
    return state


def test_evaluate_deck_exposes_directional_axes():
    deck = [
        card("CARD.STRIKE_IRONCLAD", damage=6),
        card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5),
        card("CARD.SHRUG_IT_OFF", ctype="Skill", block=8, draw=1),
    ]

    scores = evaluate_deck(deck)

    assert set(scores) >= {"attack", "defense", "cycle", "energy", "boss_ready"}
    assert scores["defense"] > 0
    assert scores["cycle"] > 0


def test_card_reward_prefers_card_that_fills_weak_defense_axis():
    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
    cards = [
        card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0),
        card("CARD.STRIKE_IRONCLAD", damage=6, index=1),
    ]
    state = base_state("card_reward", cards=cards)
    state["player"]["deck"] = deck

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {
        "cmd": "action",
        "action": "select_card_reward",
        "args": {"card_index": 0},
    }


def test_card_reward_skips_when_candidate_worsens_large_deck():
    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(20)]
    bad = card("CARD.WOUND", ctype="Status", index=0)
    state = base_state("card_reward", cards=[bad])
    state["player"]["deck"] = deck

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "skip_card_reward"}


def test_map_select_avoids_elite_at_low_hp():
    state = base_state(
        "map_select",
        choices=[
            {"type": "Elite", "col": 0, "row": 1},
            {"type": "Event", "col": 1, "row": 1},
        ],
    )
    state["player"]["hp"] = 20
    state["player"]["deck"] = [card("CARD.STRIKE_IRONCLAD", damage=6)]

    cmd = DecisionAdvisor().choose(state)

    assert cmd["action"] == "select_map_node"
    assert cmd["args"] == {"col": 1, "row": 1}


def test_rest_site_heals_at_critical_hp():
    state = base_state(
        "rest_site",
        options=[
            {"index": 0, "option_id": "SMITH", "is_enabled": True},
            {"index": 1, "option_id": "HEAL", "is_enabled": True},
        ],
    )
    state["player"]["hp"] = 18
    state["player"]["deck"] = [card("CARD.DEMON_FORM", ctype="Power", cost=3)]

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}}


def test_combat_uses_lethal_planner_when_available(monkeypatch):
    import agent.decision_advisor as da

    monkeypatch.setattr(da, "plan_action", lambda state: 0)
    state = base_state(
        "combat_play",
        hand=[{"id": "CARD.STRIKE_IRONCLAD"}],
        enemies=[{"hp": 6, "intents": []}],
    )

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_decision_advisor.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'agent.decision_advisor'`.

- [ ] **Step 3: Add minimal implementation**

Create `agent/decision_advisor.py` with:

```python
"""Decision advisor for strategy-driving, directional action scoring."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.card_scoring import (
    BROKEN_CARDS,
    _card_id_norm,
    best_smith_target,
    card_dimensions,
    deck_5turn_burst,
    deck_block_per_turn,
    deck_boss_versatility,
    deck_engine_efficiency,
    pick_best_card,
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
        base = evaluate_deck(deck)
        weakest = min(("attack", "defense", "cycle", "energy"), key=lambda k: base.get(k, 0.0))
        scored: list[CandidateScore] = []
        for i, c in enumerate(cards):
            cid = _card_id_norm(c)
            if cid in BROKEN_CARDS:
                continue
            if score_card_in_deck(c, deck) <= 0.0:
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
                + min(score_card_in_deck(c, deck) / 10.0, 1.0) * 0.18
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
            typ = str(c.get("type", "Unknown"))
            t = typ.lower()
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
            score = reward - risk
            scored.append((score, c))
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/agent/test_decision_advisor.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent/decision_advisor.py tests/agent/test_decision_advisor.py
git commit -m "feat(agent): add dynamic decision advisor"
```

## Task 2: Integrate advisor into `greedy_action()`

**Files:**
- Modify: `agent/combat_env.py`
- Modify: `tests/agent/test_decision_advisor.py`

- [ ] **Step 1: Write the failing integration test**

Append to `tests/agent/test_decision_advisor.py`:

```python
def test_greedy_action_uses_decision_advisor_for_card_rewards():
    from agent.combat_env import greedy_action

    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
    state = base_state(
        "card_reward",
        cards=[
            card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0),
            card("CARD.STRIKE_IRONCLAD", damage=6, index=1),
        ],
    )
    state["player"]["deck"] = deck

    assert greedy_action(state)["args"]["card_index"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/agent/test_decision_advisor.py::test_greedy_action_uses_decision_advisor_for_card_rewards -v`

Expected: FAIL until `greedy_action()` delegates to the advisor.

- [ ] **Step 3: Add advisor delegation**

Modify `agent/combat_env.py` imports:

```python
from agent.decision_advisor import DecisionAdvisor
```

Add a module-level advisor near `_map_strategy`:

```python
_decision_advisor = DecisionAdvisor()
```

At the top of `greedy_action()` after `decision = state.get("decision", "")`, add:

```python
    advised = _decision_advisor.choose(state)
    if advised is not None:
        return advised
```

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/agent/test_decision_advisor.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add agent/combat_env.py tests/agent/test_decision_advisor.py
git commit -m "feat(agent): route greedy decisions through advisor"
```

## Task 3: Verification before training

**Files:**
- Read-only verification.

- [ ] **Step 1: Run focused advisor tests**

Run: `.venv/bin/python -m pytest tests/agent/test_decision_advisor.py -v`

Expected: all tests pass.

- [ ] **Step 2: Run existing affected tests**

Run: `.venv/bin/python -m pytest tests/agent/test_combat_env.py tests/test_card_reward.py tests/test_rest_site.py tests/test_map.py -v`

Expected: tests pass or any unrelated pre-existing failure is recorded with exact output.

- [ ] **Step 3: Smoke import**

Run: `.venv/bin/python - <<'PY'
from agent.combat_env import greedy_action
print(greedy_action({"decision": "unknown"}))
PY`

Expected output includes `{'cmd': 'action', 'action': 'proceed'}`.

## Task 4: Start training

**Files:**
- No code changes.

- [ ] **Step 1: Inspect training command options**

Run: `.venv/bin/python agent/train.py --help`

Expected: command help prints available options.

- [ ] **Step 2: Start a short advisor-backed training run**

Use the existing training entry point with a conservative first run:

```bash
.venv/bin/python agent/train.py --character Ironclad --steps 100000
```

Expected: training starts and writes progress/checkpoints according to existing trainer behavior.
