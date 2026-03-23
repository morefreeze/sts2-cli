# Hybrid LLM + RL Agent for STS2-CLI

**Date:** 2026-03-23
**Goal:** Beat Ascension 20 using a hybrid architecture — LLM handles high-level strategic decisions, RL handles combat micro-play.
**Constraint:** RL trains locally on Mac (MPS backend); LLM uses Claude API.

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Game Coordinator                   │
│  coordinator.py — routes decisions to correct layer  │
└────────────┬──────────────────────┬─────────────────┘
             │                      │
   非战斗决策 │                      │ combat_play
             ▼                      ▼
┌────────────────────┐   ┌──────────────────────────┐
│   Strategic Agent  │   │     Combat RL Agent       │
│  (Claude API)      │   │  (PPO, local Mac MPS)     │
└────────────────────┘   └──────────────────────────┘
             │                      │
             └──────────┬───────────┘
                        ▼
              sts2_bridge.py (HTTP)
                        │
              Sts2Headless (C# game engine)
```

**Decision routing:**
- `combat_play` → RL Agent
- `map_select`, `card_reward`, `rest_site`, `event_choice`, `shop` → LLM Agent

**Rationale for the split:**
Combat decisions have bounded state, immediate reward, and are well-suited to RL. Strategic decisions (card picks, routing, shop) require long-horizon planning and domain knowledge — feedback is delayed by 30+ floors. LLM provides this as a pretrained prior at zero training cost.

---

## 2. Combat RL Agent

### State Space
```python
{
  "hand": [(card_id, cost, type), ...],  # up to 10 cards
  "energy": int,                          # 0–3
  "player": (hp, block, buffs_vector),
  "enemies": [(hp, intent, buffs_vector), ...],  # up to 3
  "turn": int
}
```
Encoding: cards use embeddings built from localization JSON vocabulary; numeric values normalized to [0, 1]. Total ~200–300 dimensions.

### Action Space
- `play_card(card_index, target_index?)` — card index × target index
- `end_turn`

Invalid action masking applied: cards with `can_play=false` are masked out before softmax.

### Reward Function
```
victory:  reward = (final_hp / max_hp)^2 × 2.0
defeat:   reward = -1.0
per-step: none
```

Squaring the HP ratio creates a nonlinear bonus for high-HP victories, strongly incentivizing HP preservation. A 50% HP win scores 0.5; a full-HP win scores 2.0. HP is a critical cross-combat resource in STS2 — a linear reward would underweight its importance.

### Training Setup
- Framework: `stable-baselines3` PPO, MPS backend
- Rollout: parallel `sts2_bridge.py` instances (one per combat)
- Training time estimate: ~1–2 hours for 100k steps on Mac M-series
- Strategic decisions during training: replaced with simple greedy heuristics (no LLM calls) to keep training cost zero

---

## 3. LLM Strategic Agent

### Trigger Points
`map_select`, `card_reward`, `rest_site`, `event_choice`, `shop`

### Prompt Design
```
[System]
You are an STS2 expert. Goal: reach the highest possible HP at Act 3 boss on Ascension 20.

Character: {character} | Floor: {floor} | HP: {hp}/{max_hp}
Deck summary: {N} attack cards, {M} defense cards. Key keywords: {keywords}
Relics: {relic_names}
Gold: {gold}

[Current decision]
{pruned decision JSON — English only, combat fields removed}

[Options]
0: {option_0}
1: {option_1}
...

Respond with JSON only: {"choice": <index>, "reason": "<one sentence>"}
```

### Key design decisions
- **Stateless calls**: each decision is an independent API call with no conversation history — reduces token cost and avoids context drift
- **Pruned JSON**: strip Chinese names, `can_play`, and other combat-only fields before sending
- **Deck summary not full deck**: aggregate card counts and keywords instead of full card list
- **Structured output only**: `{"choice": N, "reason": "..."}` — prevents LLM from elaborating

---

## 4. Game Coordinator

```python
# agent/coordinator.py
class GameCoordinator:
    def run_game(self, character: str, seed: str) -> dict:
        state = self.bridge.start_run(character, seed)
        while state["type"] != "game_over":
            if state["type"] == "combat_play":
                action = self.rl_agent.act(state)
            else:
                action = self.llm_agent.act(state)
            state = self.bridge.send(action)
        return state  # contains victory bool + final HP
```

### Module Structure
```
agent/
├── coordinator.py      # main loop, decision routing
├── rl_agent.py         # PPO inference + training entry point
├── llm_agent.py        # Claude API calls + prompt building
├── state_encoder.py    # JSON state → numeric vector for RL
├── train.py            # RL training script (batch rollout)
└── sts2_bridge.py      # existing, unchanged
```

### Modes
| Mode | LLM | RL | Usage |
|------|-----|----|-------|
| Train | off (greedy heuristics) | training | Build combat model, no API cost |
| Eval | on | inference | Full hybrid run, measure win rate |
| Debug | on | inference | Replay a JSONL log to a specific step |

---

## 5. Implementation Phases

**Phase 1 — RL combat baseline**
- Implement `state_encoder.py` and `rl_agent.py`
- Train PPO on single-character combat (Ironclad first)
- Success metric: >70% combat win rate at A0

**Phase 2 — LLM strategic layer**
- Implement `llm_agent.py` with pruned prompt
- Wire into `coordinator.py`
- Success metric: LLM makes sensible card picks / routing validated by manual review

**Phase 3 — Full hybrid evaluation**
- Run coordinator end-to-end on 50 games per character
- Measure: win rate, average final floor, average HP at boss
- Iterate on reward shaping and prompt based on failure analysis

**Phase 4 — A20 push**
- Enable Ascension 20 in `start_run`
- Add LLM context: current ascension modifiers, boss identity
- Target: >20% win rate on A20 (competitive with experienced human players)
