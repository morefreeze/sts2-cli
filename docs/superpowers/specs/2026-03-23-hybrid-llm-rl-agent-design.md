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

**Decision routing** (based on `state["decision"]` field from bridge):

| `decision` value | Handler |
|-----------------|---------|
| `combat_play` | RL Agent |
| `map_select`, `card_reward`, `rest_site`, `event_choice`, `shop` | LLM Agent |
| `bundle_select`, `card_select` | LLM Agent (fallback: pick index 0) |
| `unknown` | skip / `leave_room` |
| `game_over` | terminate loop |

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
Encoding: cards use embeddings built from `localization_eng/cards.json` vocabulary; numeric values normalized to [0, 1]. Total ~200–300 dimensions.

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

### Gymnasium Environment Wrapper

SB3 PPO requires a `gymnasium.Env` interface. `CombatEnv` wraps the bridge:

```python
# agent/combat_env.py
class CombatEnv(gymnasium.Env):
    def reset(self):
        # (re)start bridge subprocess if dead (EOF recovery)
        # start_run → fast-forward to first combat via greedy heuristics
        # return encoded state vector

    def step(self, action):
        # send play_card / end_turn to bridge
        # if EOF: treat as defeat, return terminal state with reward -1.0
        # return (next_state, reward, terminated, truncated, info)
```

**EOF/crash recovery:** When the bridge process dies (e.g., BUG-003 Leaf Slime crash), `reset()` kills and restarts the subprocess. The crashed episode is treated as a defeat (`reward = -1.0`). This is acceptable because crashes are rare and the signal is consistent (crashed = bad outcome).

**MPS device:** SB3 PPO's device must be explicitly set to `"mps"` via `policy_kwargs={"device": "mps"}` or by setting the torch default device before training.

### Training Setup
- Framework: `stable-baselines3` PPO with `CombatEnv`
- Parallel envs: `SubprocVecEnv` with N=4 (4 bridge instances in parallel)
- Training time estimate: ~1–2 hours for 100k steps on Mac M-series
- Strategic decisions during training: replaced with greedy heuristics (always heal at rest, always pick first card reward, random map node) — **no LLM calls during training**

**Training/eval distribution note:** The greedy heuristics used during training will produce different deck compositions and HP states than the LLM does at eval time. This is a known distribution mismatch. Mitigation: in Phase 2, run 20 eval games using the same greedy heuristics first to establish a cleaner RL baseline before adding the LLM layer.

---

## 3. LLM Strategic Agent

### Trigger Points
`map_select`, `card_reward`, `rest_site`, `event_choice`, `shop`, `bundle_select`, `card_select`

### Deck Summary
Source: `state["player"]["deck"]` (list of card objects in game state JSON).
Aggregation in `llm_agent.py`:
- Count cards by type (Attack / Skill / Power)
- Extract unique keywords from `localization_eng/card_keywords.json`
- Example output: `"12 attack, 5 skill, 2 power. Keywords: exhaust, block, strength"`

### Prompt Design
```
[System]
You are an STS2 expert. Goal: defeat the Act 3 boss at maximum possible HP on Ascension 20.

Character: {character} | Floor: {floor} | HP: {hp}/{max_hp}
Deck: {deck_summary}
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

**Note on goal framing:** The primary goal is to *defeat* the Act 3 boss; maximum HP is a secondary constraint. The LLM should prioritize win-condition cards over pure HP conservation.

### Key design decisions
- **Stateless calls**: each decision is an independent API call with no conversation history — reduces token cost and avoids context drift
- **Pruned JSON**: strip Chinese names, `can_play`, and other combat-only fields before sending
- **Structured output only**: `{"choice": N, "reason": "..."}` — prevents LLM from elaborating

---

## 4. Game Coordinator

```python
# agent/coordinator.py
class GameCoordinator:
    def run_game(self, character: str, seed: str) -> dict:
        state = self.bridge.start_run(character, seed)
        while state["decision"] != "game_over":
            if state["decision"] == "combat_play":
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
├── combat_env.py       # gymnasium.Env wrapper around bridge (for SB3 training)
├── rl_agent.py         # PPO inference only (loads trained model, calls .predict())
├── train.py            # training script: instantiates CombatEnv + SB3, runs learn()
├── llm_agent.py        # Claude API calls + prompt building + deck summary
├── state_encoder.py    # JSON state → numeric vector for RL input
└── sts2_bridge.py      # existing, unchanged
```

**Module boundaries:**
- `train.py` owns the training loop: creates `SubprocVecEnv([CombatEnv, ...])`, instantiates SB3 PPO, calls `model.learn(N)`, saves checkpoint
- `rl_agent.py` owns inference only: loads checkpoint, exposes `act(state) → action`
- `combat_env.py` handles all bridge lifecycle (start/stop/EOF recovery) and reward calculation

**Bridge path note:** `sts2_bridge.py` uses `cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` (two levels up from `agent/`) to find the C# project. All new modules in `agent/` that spawn the bridge must preserve this — do not move `sts2_bridge.py` or call it from a different working directory.

### Modes

| Mode | LLM | RL | Usage |
|------|-----|----|-------|
| Train | off (greedy heuristics) | training | Build combat model, no API cost |
| Eval-RL | off (greedy heuristics) | inference | Measure RL combat baseline cleanly |
| Eval-Full | on | inference | Full hybrid run, measure win rate |
| Debug | on | inference | Replay a JSONL log to a specific step |

---

## 5. Implementation Phases

**Phase 1 — RL combat baseline**
- Implement `state_encoder.py`, `combat_env.py`, `rl_agent.py`, `train.py`
- Train PPO on single-character combat (Ironclad first)
- Measure random-agent combat win rate first as baseline, then target >2× that baseline

**Phase 2 — LLM strategic layer + distribution alignment**
- Implement `llm_agent.py` and `coordinator.py`
- Run 20 eval games with greedy heuristics only (Eval-RL mode) to establish clean RL baseline
- Then run 20 games with LLM (Eval-Full mode) to measure LLM strategic benefit
- Success metric: Eval-Full win rate > Eval-RL win rate

**Phase 3 — Full hybrid evaluation**
- Run coordinator end-to-end on 50 games per character
- Measure: win rate, average final floor, average HP at boss
- Iterate on reward shaping and prompt based on failure analysis

**Phase 4 — A20 push**
- Enable Ascension 20 in `start_run`
- Add LLM context: current ascension modifiers, boss identity
- Target: >20% win rate on A20 (competitive with experienced human players)
