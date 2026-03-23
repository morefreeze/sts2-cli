# Hybrid LLM + RL Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hybrid agent that beats Ascension 20 in STS2 — RL handles combat, Claude API handles all strategic decisions.

**Architecture:** A `GameCoordinator` routes decisions: `combat_play` goes to a PPO-trained `RLAgent`, everything else goes to an `LLMAgent` (Claude API). The RL agent is trained by spawning game subprocesses directly (no HTTP bridge overhead) in a `gymnasium.Env` wrapper, with invalid action masking via `MaskablePPO` from `sb3-contrib`.

**Tech Stack:** Python 3.9+, `stable-baselines3`, `sb3-contrib` (MaskablePPO), `gymnasium`, `anthropic` SDK, PyTorch (MPS backend on Mac M-series), `.NET 9` (C# game engine already built).

---

## File Structure

```
agent/
├── __init__.py         # package marker (empty)
├── state_encoder.py    # card vocab + numpy state encoding (pure, no I/O after init)
├── combat_env.py       # gymnasium.Env: spawns game subprocess, manages lifecycle, computes reward
│                       # also exports greedy_action() as a module-level function
├── rl_agent.py         # inference only: loads MaskablePPO checkpoint, act(state) -> action dict
├── train.py            # training entry: SubprocVecEnv × 4, MaskablePPO.learn(), saves checkpoint
├── llm_agent.py        # Claude API: prompt building, deck summary, structured output parsing
├── coordinator.py      # main loop: routes decisions to RL or LLM, handles all decision types
└── sts2_bridge.py      # EXISTING — do not modify

tests/
├── __init__.py
└── agent/
    ├── __init__.py
    ├── test_state_encoder.py
    ├── test_combat_env.py      # mocked subprocess via dry_run=True
    ├── test_rl_agent.py        # mocked MaskablePPO.load
    ├── test_llm_agent.py       # mocked Anthropic client
    └── test_coordinator.py    # mocked _send sequences
```

**Key constraints:**
- `combat_env.py` uses the game subprocess directly (same pattern as `python/play_full_run.py`) — NOT the HTTP bridge. The HTTP bridge is for manual/agent curl use only.
- `combat_env.py` must be importable with no side effects at import time, because `SubprocVecEnv` forks/spawns it in worker processes.
- `state_encoder.py` is a pure function after `__init__`. Vocab built once from `localization_eng/cards.json` (605 cards).
- `coordinator.py` imports `greedy_action` from `combat_env` as a module-level function — avoids the `CombatEnv.__new__()` hack.
- Never modify `sts2_bridge.py`.

---

## Task 1: Install Dependencies

**Files:**
- Create: `requirements-agent.txt`
- Create: `agent/__init__.py`
- Create: `tests/__init__.py`, `tests/agent/__init__.py`

- [ ] **Step 1: Install Python packages**

```bash
pip3 install stable-baselines3 sb3-contrib gymnasium anthropic torch pytest
```

- [ ] **Step 2: Verify MPS backend**

```bash
python3 -c "import torch; print('MPS:', torch.backends.mps.is_available())"
```

Expected: `MPS: True`

- [ ] **Step 3: Verify SB3 imports**

```bash
python3 -c "from sb3_contrib import MaskablePPO; from stable_baselines3.common.vec_env import SubprocVecEnv; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Create package markers and test directories**

```bash
touch agent/__init__.py
mkdir -p tests/agent
touch tests/__init__.py tests/agent/__init__.py
```

- [ ] **Step 5: Commit**

```bash
cat > requirements-agent.txt << 'EOF'
stable-baselines3>=2.3
sb3-contrib>=2.3
gymnasium>=0.29
anthropic>=0.40
torch>=2.2
pytest>=8.0
EOF
git add requirements-agent.txt agent/__init__.py tests/__init__.py tests/agent/__init__.py
git commit -m "chore: add agent requirements and package structure"
```

---

## Task 2: state_encoder.py

**Files:**
- Create: `agent/state_encoder.py`
- Create: `tests/agent/test_state_encoder.py`

Converts raw game state JSON to a fixed-size float32 numpy array (130 dims) and produces an action mask (41 bools).

**State vector layout (130 floats total):**
- `[0]` energy / 3
- `[1]` turn / 20 (capped at 1.0)
- `[2:82]` hand: 10 slots × 8 floats — card_id_norm, cost_norm, is_attack, is_skill, is_power, can_play, needs_target, is_empty
- `[82:85]` player: hp_norm, block_norm, buff_count_norm
- `[85:100]` enemies: 3 slots × 5 floats — hp_norm, block_norm, intent_is_attack, intent_damage_norm, is_empty
- `[100:130]` player buffs: top 30 buff magnitudes (alphabetical, normalized)

**Action space (size=41):**
- Index `i*4 + j` (i=hand slot 0–9, j=target 0–2): play card i at enemy j
- Index `i*4 + 3`: play card i with no target (Self / None / All cards)
- Index 40: end_turn

- [ ] **Step 1: Write failing tests**

Create `tests/agent/test_state_encoder.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
import pytest
from agent.state_encoder import StateEncoder

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


@pytest.fixture
def enc():
    return StateEncoder(CARDS_JSON)


def make_state(hand=None, energy=3, player_hp=80, player_max_hp=80,
               player_block=0, enemies=None):
    return {
        "decision": "combat_play",
        "energy": energy,
        "round": 1,
        "hand": hand or [],
        "player": {"hp": player_hp, "max_hp": player_max_hp, "block": player_block, "buffs": []},
        "enemies": enemies or [],
    }


def make_card(idx, card_id="STRIKE", cost=1, can_play=True, target_type="AnyEnemy"):
    return {"index": idx, "id": {"en": card_id}, "cost": cost,
            "can_play": can_play, "target_type": target_type, "type": "Attack"}


def make_enemy(hp=30, max_hp=30, block=0):
    return {"hp": hp, "max_hp": max_hp, "block": block,
            "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}


def test_encode_returns_correct_shape(enc):
    obs = enc.encode(make_state())
    assert obs.shape == (enc.obs_size,)
    assert obs.dtype == np.float32
    assert enc.obs_size == 130


def test_encode_energy_normalized(enc):
    obs = enc.encode(make_state(energy=3))
    assert abs(obs[0] - 1.0) < 1e-5


def test_encode_hp_normalized(enc):
    obs = enc.encode(make_state(player_hp=40, player_max_hp=80))
    player_start = 1 + 1 + 80  # after energy + turn + hand
    assert abs(obs[player_start] - 0.5) < 1e-5


def test_action_mask_empty_hand(enc):
    mask = enc.action_mask(make_state(hand=[], enemies=[make_enemy()]))
    assert mask.shape == (41,)
    assert mask[40] == True   # end_turn always valid
    assert not any(mask[:40])


def test_action_mask_playable_card_with_target(enc):
    state = make_state(
        hand=[make_card(0, can_play=True, target_type="AnyEnemy")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert mask[0 * 4 + 0] == True   # play card 0 at enemy 0
    assert mask[0 * 4 + 1] == False  # enemy 1 doesn't exist
    assert mask[0 * 4 + 3] == False  # no-target invalid for AnyEnemy


def test_action_mask_self_targeting_card(enc):
    state = make_state(
        hand=[make_card(0, can_play=True, target_type="Self")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert mask[0 * 4 + 3] == True   # no-target slot valid for Self
    assert mask[0 * 4 + 0] == False  # enemy target invalid for Self


def test_action_mask_unplayable_card(enc):
    state = make_state(
        hand=[make_card(0, can_play=False, target_type="AnyEnemy")],
        enemies=[make_enemy()]
    )
    mask = enc.action_mask(state)
    assert not any(mask[:4])


def test_decode_end_turn(enc):
    action = enc.decode(40, make_state())
    assert action == {"cmd": "action", "action": "end_turn"}


def test_decode_play_card_with_target(enc):
    state = make_state(
        hand=[make_card(0, card_id="STRIKE"), make_card(1, card_id="DEFEND")],
        enemies=[make_enemy(), make_enemy()]
    )
    action = enc.decode(0 * 4 + 1, state)  # play card 0 targeting enemy 1
    assert action["action"] == "play_card"
    assert action["args"]["card_index"] == 0
    assert action["args"]["target_index"] == 1


def test_decode_play_card_no_target(enc):
    state = make_state(hand=[make_card(0, target_type="Self")], enemies=[])
    action = enc.decode(0 * 4 + 3, state)
    assert action["action"] == "play_card"
    assert "target_index" not in action["args"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/bytedance/mygit/sts2-cli
python3 -m pytest tests/agent/test_state_encoder.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agent.state_encoder'`

- [ ] **Step 3: Implement `agent/state_encoder.py`**

```python
"""
state_encoder.py — converts STS2 game state JSON to numpy observation vector.

Observation layout (130 floats):
  [0]       energy / 3
  [1]       turn / 20 (capped)
  [2:82]    hand: 10 slots × 8 floats each
  [82:85]   player: hp_norm, block_norm, buff_count_norm
  [85:100]  enemies: 3 slots × 5 floats each
  [100:130] player buffs: top 30 buff magnitudes (alphabetical), normalized
"""
import json
import numpy as np

MAX_HAND = 10
MAX_ENEMIES = 3
MAX_BUFFS = 30
CARD_SLOT = 8
ENEMY_SLOT = 5
ACTION_SIZE = MAX_HAND * 4 + 1  # 41
NO_TARGET_SLOT = 3


class StateEncoder:
    def __init__(self, cards_json_path: str):
        with open(cards_json_path) as f:
            data = json.load(f)
        titles = sorted(set(
            k.rsplit(".", 1)[0] for k in data if k.endswith(".title")
        ))
        self.vocab = {card_id: i for i, card_id in enumerate(titles)}
        self.vocab_size = len(self.vocab)  # 605
        self.obs_size = 1 + 1 + MAX_HAND * CARD_SLOT + 3 + MAX_ENEMIES * ENEMY_SLOT + MAX_BUFFS  # 130

    def encode(self, state: dict) -> np.ndarray:
        obs = np.zeros(self.obs_size, dtype=np.float32)
        idx = 0

        obs[idx] = min(state.get("energy", 0) / 3.0, 1.0); idx += 1
        obs[idx] = min(state.get("round", 1) / 20.0, 1.0); idx += 1

        hand = state.get("hand", [])
        for slot in range(MAX_HAND):
            if slot < len(hand):
                c = hand[slot]
                card_id = c.get("id", {})
                if isinstance(card_id, dict):
                    card_id = card_id.get("en", "")
                ctype = (c.get("type") or "").lower()
                obs[idx]     = self.vocab.get(card_id, 0) / max(self.vocab_size, 1)
                obs[idx + 1] = min(c.get("cost", 0) / 3.0, 1.0)
                obs[idx + 2] = 1.0 if ctype == "attack" else 0.0
                obs[idx + 3] = 1.0 if ctype == "skill" else 0.0
                obs[idx + 4] = 1.0 if ctype == "power" else 0.0
                obs[idx + 5] = 1.0 if c.get("can_play") else 0.0
                obs[idx + 6] = 1.0 if (c.get("target_type") or "").lower() == "anyenemy" else 0.0
                obs[idx + 7] = 0.0  # not empty
            else:
                obs[idx + 7] = 1.0  # empty slot
            idx += CARD_SLOT

        player = state.get("player", {})
        max_hp = max(player.get("max_hp", 1), 1)
        obs[idx]     = player.get("hp", 0) / max_hp; idx += 1
        obs[idx]     = min(player.get("block", 0) / 30.0, 1.0); idx += 1
        obs[idx]     = min(len(player.get("buffs", [])) / 10.0, 1.0); idx += 1

        enemies = state.get("enemies", [])
        for slot in range(MAX_ENEMIES):
            if slot < len(enemies):
                e = enemies[slot]
                max_ehp = max(e.get("max_hp", e.get("hp", 1)), 1)
                intent = e.get("intent") or {}
                obs[idx]     = e.get("hp", 0) / max_ehp
                obs[idx + 1] = min(e.get("block", 0) / 30.0, 1.0)
                obs[idx + 2] = 1.0 if (intent.get("type") or "").lower() == "attack" else 0.0
                obs[idx + 3] = min((intent.get("damage", 0) * intent.get("times", 1)) / 50.0, 1.0)
                obs[idx + 4] = 0.0
            else:
                obs[idx + 4] = 1.0
            idx += ENEMY_SLOT

        buffs = sorted(
            player.get("buffs", []),
            key=lambda b: (b.get("name", {}).get("en", "") if isinstance(b.get("name"), dict) else str(b.get("name", "")))
        )
        for i in range(MAX_BUFFS):
            if i < len(buffs):
                obs[idx] = min(abs(buffs[i].get("amount", 1)) / 10.0, 1.0)
            idx += 1

        return obs

    def action_mask(self, state: dict) -> np.ndarray:
        mask = np.zeros(ACTION_SIZE, dtype=bool)
        mask[40] = True  # end_turn always valid

        hand = state.get("hand", [])
        n_enemies = len(state.get("enemies", []))

        for slot in range(min(len(hand), MAX_HAND)):
            c = hand[slot]
            if not c.get("can_play", False):
                continue
            needs_target = (c.get("target_type") or "").lower() == "anyenemy"
            base = slot * 4
            if needs_target:
                for j in range(n_enemies):
                    mask[base + j] = True
            else:
                mask[base + NO_TARGET_SLOT] = True

        return mask

    def decode(self, action_idx: int, state: dict) -> dict:
        if action_idx == 40:
            return {"cmd": "action", "action": "end_turn"}

        hand_slot = action_idx // 4
        target_slot = action_idx % 4
        card = state.get("hand", [])[hand_slot]
        args = {"card_index": card["index"]}
        if target_slot != NO_TARGET_SLOT:
            args["target_index"] = target_slot
        return {"cmd": "action", "action": "play_card", "args": args}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/agent/test_state_encoder.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/state_encoder.py tests/agent/test_state_encoder.py
git commit -m "feat: add state_encoder with card vocab, obs encoding, and action masking"
```

---

## Task 3: combat_env.py

**Files:**
- Create: `agent/combat_env.py`
- Create: `tests/agent/test_combat_env.py`

`CombatEnv` spawns the game subprocess directly. The module also exports a `greedy_action(state)` module-level function used by `coordinator.py` (avoids instantiation hacks).

- [ ] **Step 1: Write failing tests**

Create `tests/agent/test_combat_env.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import pytest
from agent.combat_env import CombatEnv, greedy_action

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def test_env_action_space_size():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.action_space.n == 41


def test_env_observation_space_shape():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.observation_space.shape == (130,)


def test_reward_victory_full_hp():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    assert abs(env._compute_reward({"victory": True, "player": {"hp": 80, "max_hp": 80}}) - 2.0) < 1e-5


def test_reward_victory_half_hp():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    # (0.5)^2 * 2 = 0.5
    assert abs(env._compute_reward({"victory": True, "player": {"hp": 40, "max_hp": 80}}) - 0.5) < 1e-5


def test_reward_defeat():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    assert env._compute_reward({"victory": False, "player": {"hp": 0, "max_hp": 80}}) == -1.0


def test_reset_returns_correct_obs_shape():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    obs, info = env.reset()
    assert obs.shape == (130,)


def test_step_dry_run_terminates():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env.reset()
    obs, reward, terminated, truncated, info = env.step(40)  # end_turn
    assert terminated  # dry_run always terminates


def test_greedy_action_map_select():
    state = {
        "decision": "map_select",
        "choices": [
            {"col": 1, "row": 3, "type": "rest"},
            {"col": 2, "row": 3, "type": "enemy"},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "select_map_node"
    # col and row must come from the same node (not independently sampled)
    col = action["args"]["col"]
    row = action["args"]["row"]
    valid_pairs = {(c["col"], c["row"]) for c in state["choices"]}
    assert (col, row) in valid_pairs


def test_greedy_action_card_reward():
    state = {
        "decision": "card_reward",
        "cards": [{"index": 0}]
    }
    action = greedy_action(state)
    assert action["action"] == "select_card_reward"


def test_greedy_action_rest_heal():
    state = {
        "decision": "rest_site",
        "options": [
            {"index": 0, "option_id": "SMITH", "is_enabled": True},
            {"index": 1, "option_id": "HEAL", "is_enabled": True},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "choose_option"
    assert action["args"]["option_index"] == 1  # HEAL preferred
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/agent/test_combat_env.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agent.combat_env'`

- [ ] **Step 3: Implement `agent/combat_env.py`**

```python
"""
combat_env.py — gymnasium.Env for STS2 combat training.

Also exports greedy_action(state) as a module-level function for use by
coordinator.py and _advance_to_combat().
"""
import json, os, subprocess, random
import gymnasium as gym
import numpy as np
from gymnasium.spaces import Box, Discrete
from agent.state_encoder import StateEncoder

DOTNET = os.path.expanduser("~/.dotnet-arm64/dotnet")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "Sts2Headless", "Sts2Headless.csproj")


def greedy_action(state: dict) -> dict:
    """Greedy heuristic for non-combat decisions. Used during training and by coordinator."""
    decision = state.get("decision", "")

    if decision == "map_select":
        choices = state.get("choices", [])
        if choices:
            chosen = random.choice(choices)
            return {"cmd": "action", "action": "select_map_node",
                    "args": {"col": chosen["col"], "row": chosen["row"]}}

    elif decision == "card_reward":
        cards = state.get("cards", [])
        if cards:
            return {"cmd": "action", "action": "select_card_reward",
                    "args": {"card_index": 0}}
        return {"cmd": "action", "action": "skip_card_reward"}

    elif decision == "rest_site":
        options = state.get("options", [])
        enabled = [o for o in options if o.get("is_enabled", True)]
        heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
        choice = heal or (enabled[0] if enabled else None)
        if choice:
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": choice["index"]}}

    elif decision == "event_choice":
        options = state.get("options", [])
        choice = next((o for o in options if not o.get("is_locked")), None)
        if choice:
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": choice["index"]}}
        return {"cmd": "action", "action": "leave_room"}

    elif decision == "bundle_select":
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}

    elif decision == "card_select":
        cards = state.get("cards", [])
        if cards:
            return {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        return {"cmd": "action", "action": "leave_room"}

    return {"cmd": "action", "action": "proceed"}


class CombatEnv(gym.Env):
    """
    Gymnasium environment for STS2 combat.

    Observation: float32 vector of shape (130,)
    Action: int in [0, 40]
    Reward: (hp/max_hp)^2 * 2.0 on victory, -1.0 on defeat/crash
    """

    def __init__(self, cards_json: str = None, character: str = "Ironclad",
                 ascension: int = 0, seed: str = None, dry_run: bool = False):
        super().__init__()
        if cards_json is None:
            cards_json = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
        self.enc = StateEncoder(cards_json)
        self.character = character
        self.ascension = ascension
        self._seed = seed
        self.dry_run = dry_run

        self.observation_space = Box(low=0.0, high=1.0,
                                     shape=(self.enc.obs_size,), dtype=np.float32)
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self._kill_proc()

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        self._start_proc()
        run_seed = self._seed or f"train_{self._run_counter}"
        self._run_counter += 1
        state = self._send({"cmd": "start_run", "character": self.character,
                            "seed": run_seed, "ascension": self.ascension})
        if state is None:
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        state = self._advance_to_combat(state)
        self._current_state = state
        return self.enc.encode(state), {}

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size, dtype=np.float32), -1.0, True, False, {}

        cmd = self.enc.decode(int(action), self._current_state)
        next_state = self._send(cmd)

        if next_state is None:
            return np.zeros(self.enc.obs_size, dtype=np.float32), -1.0, True, False, {"crashed": True}

        decision = next_state.get("decision", "")

        if decision == "game_over":
            return np.zeros(self.enc.obs_size, dtype=np.float32), self._compute_reward(next_state), True, False, {}

        if decision == "combat_play":
            self._current_state = next_state
            return self.enc.encode(next_state), 0.0, False, False, {}

        next_state = self._advance_to_combat(next_state)
        if next_state.get("decision") == "game_over":
            return np.zeros(self.enc.obs_size, dtype=np.float32), self._compute_reward(next_state), True, False, {}
        self._current_state = next_state
        return self.enc.encode(next_state), 0.0, False, False, {}

    def action_masks(self) -> np.ndarray:
        if self._current_state is None:
            return np.ones(41, dtype=bool)
        return self.enc.action_mask(self._current_state)

    def close(self):
        self._kill_proc()

    def _compute_reward(self, state: dict) -> float:
        if not state.get("victory", False):
            return -1.0
        player = state.get("player", {})
        hp = player.get("hp", 0)
        max_hp = max(player.get("max_hp", 1), 1)
        return ((hp / max_hp) ** 2) * 2.0

    def _advance_to_combat(self, state: dict) -> dict:
        for _ in range(200):
            if state.get("decision") in ("combat_play", "game_over"):
                return state
            cmd = greedy_action(state)
            next_state = self._send(cmd)
            if next_state is None:
                return {"decision": "game_over", "victory": False,
                        "player": {"hp": 0, "max_hp": 80}}
            state = next_state
        return state

    def _start_proc(self):
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=PROJECT_ROOT
        )
        self._read_json()

    def _kill_proc(self):
        if self._proc is not None:
            try:
                self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def _read_json(self):
        if self._proc is None:
            return None
        while True:
            line = self._proc.stdout.readline().strip()
            if not line:
                return None
            if line.startswith("{"):
                return json.loads(line)

    def _send(self, cmd: dict):
        if self._proc is None:
            return None
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
            return self._read_json()
        except Exception:
            return None


def _dummy_combat_state() -> dict:
    return {
        "decision": "combat_play", "energy": 3, "round": 1,
        "hand": [{"index": 0, "id": {"en": "STRIKE"}, "cost": 1,
                  "can_play": True, "target_type": "AnyEnemy", "type": "Attack"}],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [{"hp": 30, "max_hp": 30, "block": 0,
                     "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/agent/test_combat_env.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/combat_env.py tests/agent/test_combat_env.py
git commit -m "feat: add CombatEnv with greedy_action, gym interface, and EOF recovery"
```

---

## Task 4: rl_agent.py

**Files:**
- Create: `agent/rl_agent.py`
- Create: `tests/agent/test_rl_agent.py`

- [ ] **Step 1: Write failing tests**

Create `tests/agent/test_rl_agent.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import numpy as np
import pytest
from unittest.mock import patch, MagicMock

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def make_combat_state():
    return {
        "decision": "combat_play", "energy": 3, "round": 1,
        "hand": [{"index": 0, "id": {"en": "STRIKE"}, "cost": 1,
                  "can_play": True, "target_type": "AnyEnemy", "type": "Attack"}],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [{"hp": 30, "max_hp": 30, "block": 0,
                     "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}],
    }


def test_rl_agent_act_returns_cmd_dict():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([0]), None)  # action 0: play card 0 at enemy 0

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    state = make_combat_state()
    action = agent.act(state)
    assert isinstance(action, dict)
    assert action.get("cmd") == "action"
    assert action.get("action") in ("play_card", "end_turn")


def test_rl_agent_end_turn_action():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([40]), None)  # end_turn

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    action = agent.act(make_combat_state())
    assert action == {"cmd": "action", "action": "end_turn"}


def test_rl_agent_passes_action_mask_to_predict():
    from agent.rl_agent import RLAgent
    mock_model = MagicMock()
    mock_model.predict.return_value = (np.array([40]), None)

    with patch("agent.rl_agent.MaskablePPO.load", return_value=mock_model):
        agent = RLAgent("fake_path.zip", CARDS_JSON)

    agent.act(make_combat_state())
    call_kwargs = mock_model.predict.call_args[1]
    assert "action_masks" in call_kwargs
    assert call_kwargs["action_masks"].shape == (1, 41)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/agent/test_rl_agent.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agent.rl_agent'`

- [ ] **Step 3: Implement `agent/rl_agent.py`**

```python
"""rl_agent.py — inference-only wrapper for trained MaskablePPO model."""
from sb3_contrib import MaskablePPO
from agent.state_encoder import StateEncoder
import numpy as np


class RLAgent:
    def __init__(self, checkpoint_path: str, cards_json: str):
        self.enc = StateEncoder(cards_json)
        self.model = MaskablePPO.load(checkpoint_path)

    def act(self, state: dict) -> dict:
        obs = self.enc.encode(state).reshape(1, -1)
        mask = self.enc.action_mask(state).reshape(1, -1)
        action, _ = self.model.predict(obs, action_masks=mask, deterministic=True)
        return self.enc.decode(int(action[0]), state)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/agent/test_rl_agent.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/rl_agent.py tests/agent/test_rl_agent.py
git commit -m "feat: add RLAgent inference wrapper with action mask forwarding"
```

---

## Task 5: train.py

**Files:**
- Create: `agent/train.py`

- [ ] **Step 1: Implement `agent/train.py`**

```python
#!/usr/bin/env python3
"""train.py — RL combat training.

Usage:
    python3 agent/train.py --character Ironclad --steps 100000
    python3 agent/train.py --character Ironclad --steps 500000 --checkpoint checkpoints/ppo_ironclad_100k.zip
"""
import argparse, os
import torch
from stable_baselines3.common.vec_env import SubprocVecEnv
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv

CARDS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "checkpoints")


def mask_fn(env):
    return env.action_masks()


def make_env(character: str, ascension: int):
    def _init():
        env = CombatEnv(character=character, ascension=ascension)
        return ActionMasker(env, mask_fn)
    return _init


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--steps", type=int, default=100_000)
    parser.add_argument("--n-envs", type=int, default=4)
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--checkpoint", default=None)
    args = parser.parse_args()

    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Training on device: {device}")

    vec_env = SubprocVecEnv([make_env(args.character, args.ascension) for _ in range(args.n_envs)])

    if args.checkpoint:
        model = MaskablePPO.load(args.checkpoint, env=vec_env, device=device)
    else:
        model = MaskablePPO("MlpPolicy", vec_env, verbose=1, device=device,
                            n_steps=256, batch_size=64, n_epochs=4,
                            learning_rate=3e-4, gamma=0.99, ent_coef=0.01,
                            tensorboard_log=os.path.join(CHECKPOINT_DIR, "tb_logs"))

    save_interval = 25_000
    steps_done = 0
    while steps_done < args.steps:
        chunk = min(save_interval, args.steps - steps_done)
        model.learn(total_timesteps=chunk, reset_num_timesteps=(steps_done == 0))
        steps_done += chunk
        ckpt = os.path.join(CHECKPOINT_DIR, f"ppo_{args.character.lower()}_{steps_done // 1000}k.zip")
        model.save(ckpt)
        print(f"Checkpoint saved: {ckpt}")

    vec_env.close()
    print("Training complete.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test (1 env, 10 steps only)**

```bash
cd /Users/bytedance/mygit/sts2-cli
python3 agent/train.py --character Ironclad --steps 10 --n-envs 1
```

Expected: prints `Checkpoint saved: checkpoints/ppo_ironclad_0k.zip`

- [ ] **Step 3: Commit**

```bash
git add agent/train.py
git commit -m "feat: add PPO training script with SubprocVecEnv and MPS support"
```

---

## Task 6: llm_agent.py

**Files:**
- Create: `agent/llm_agent.py`
- Create: `tests/agent/test_llm_agent.py`

- [ ] **Step 1: Write failing tests**

Create `tests/agent/test_llm_agent.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest, json
from unittest.mock import MagicMock, patch

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


@pytest.fixture
def agent():
    from agent.llm_agent import LLMAgent
    return LLMAgent(api_key="test-key", model="claude-sonnet-4-6", cards_json=CARDS_JSON)


def make_map_state():
    return {
        "decision": "map_select",
        "act": 1, "floor": 5,
        "player": {"hp": 65, "max_hp": 80, "gold": 50,
                   "relics": [{"name": {"en": "Burning Blood"}}],
                   "deck": [
                       {"id": {"en": "STRIKE"}, "type": "Attack"},
                       {"id": {"en": "DEFEND"}, "type": "Skill"},
                   ]},
        "choices": [
            {"col": 0, "row": 1, "type": "enemy"},
            {"col": 1, "row": 1, "type": "rest"},
        ],
    }


def test_deck_summary(agent):
    deck = [
        {"id": {"en": "STRIKE"}, "type": "Attack"},
        {"id": {"en": "STRIKE"}, "type": "Attack"},
        {"id": {"en": "DEFEND"}, "type": "Skill"},
        {"id": {"en": "CORRUPTION"}, "type": "Power"},
    ]
    summary = agent._deck_summary(deck)
    assert "2 attack" in summary.lower()
    assert "1 skill" in summary.lower()
    assert "1 power" in summary.lower()


def test_prune_state_removes_zh(agent):
    state = {
        "decision": "map_select",
        "name": {"en": "Elite", "zh": "精英"},
        "player": {"hp": 70, "max_hp": 80, "deck": [], "relics": [], "gold": 0},
    }
    pruned = agent._prune_state(state)
    assert "zh" not in json.dumps(pruned)


def test_act_returns_map_select_action(agent):
    state = make_map_state()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text='{"choice": 1, "reason": "take the rest site"}')]

    with patch.object(agent.client.messages, 'create', return_value=mock_resp):
        action = agent.act(state)

    assert action["action"] == "select_map_node"
    assert action["args"]["col"] == 1
    assert action["args"]["row"] == 1


def test_act_falls_back_to_index_0_on_bad_json(agent):
    state = make_map_state()
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text='not valid json')]

    with patch.object(agent.client.messages, 'create', return_value=mock_resp):
        action = agent.act(state)

    assert action["args"]["col"] == 0  # fallback to choice 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/agent/test_llm_agent.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agent.llm_agent'`

- [ ] **Step 3: Implement `agent/llm_agent.py`**

```python
"""llm_agent.py — Claude API strategic agent for non-combat decisions."""
import json, os, re
import anthropic


class LLMAgent:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", cards_json: str = None):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def act(self, state: dict) -> dict:
        options = self._extract_options(state)
        if not options:
            return self._default_action(state)
        prompt = self._build_prompt(state, options)
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
                system=self._system_prompt(),
            )
            text = response.content[0].text.strip()
            parsed = self._parse_response(text)
            choice_idx = max(0, min(int(parsed.get("choice", 0)), len(options) - 1))
        except Exception:
            choice_idx = 0
        return self._action_for_choice(state, choice_idx)

    def _system_prompt(self) -> str:
        return (
            "You are an expert Slay the Spire 2 player. "
            "Primary goal: defeat the Act 3 boss. "
            "Secondary goal: maximize HP entering the boss fight. "
            'Respond ONLY with JSON: {"choice": <index>, "reason": "<one sentence>"}'
        )

    def _build_prompt(self, state: dict, options: list) -> str:
        player = state.get("player", {})
        relics = [
            (r.get("name", {}).get("en", "") if isinstance(r.get("name"), dict) else str(r.get("name", "")))
            for r in player.get("relics", [])
        ]
        pruned = self._prune_state(state)
        options_str = "\n".join(f"{i}: {self._option_label(o)}" for i, o in enumerate(options))
        return (
            f"Character: ? | Act {state.get('act', '?')} Floor {state.get('floor', '?')} | "
            f"HP: {player.get('hp')}/{player.get('max_hp')} | Gold: {player.get('gold', 0)}\n"
            f"Deck: {self._deck_summary(player.get('deck', []))}\n"
            f"Relics: {', '.join(relics) or 'none'}\n\n"
            f"State:\n{json.dumps(pruned, ensure_ascii=False)}\n\n"
            f"Options:\n{options_str}\n"
        )

    def _deck_summary(self, deck: list) -> str:
        counts = {"attack": 0, "skill": 0, "power": 0, "other": 0}
        for card in deck:
            t = (card.get("type") or "").lower()
            counts[t if t in counts else "other"] += 1
        parts = [f"{v} {k}" for k, v in counts.items() if v > 0]
        return ", ".join(parts) if parts else "empty deck"

    def _prune_state(self, obj, depth: int = 0):
        _remove = {"zh", "description", "after_upgrade", "id", "can_play", "upgraded", "enchantment"}
        if isinstance(obj, dict):
            return {k: self._prune_state(v, depth + 1) for k, v in obj.items() if k not in _remove}
        if isinstance(obj, list):
            return [self._prune_state(v, depth + 1) for v in obj]
        return obj

    def _extract_options(self, state: dict) -> list:
        decision = state.get("decision", "")
        if decision == "map_select":
            return state.get("choices", [])
        elif decision == "card_reward":
            return state.get("cards", []) + [{"_skip": True}]
        elif decision in ("rest_site", "event_choice"):
            return state.get("options", [])
        elif decision == "shop":
            return [{"_leave": True}]
        elif decision == "bundle_select":
            return state.get("bundles", [{"bundle_index": 0}])
        elif decision == "card_select":
            return state.get("cards", [])
        return []

    def _option_label(self, option: dict) -> str:
        if option.get("_skip"):
            return "Skip (take no card)"
        if option.get("_leave"):
            return "Leave shop"
        name = option.get("name") or option.get("title") or option.get("type") or ""
        if isinstance(name, dict):
            name = name.get("en", str(name))
        return str(name) or json.dumps(option)[:60]

    def _action_for_choice(self, state: dict, choice_idx: int) -> dict:
        decision = state.get("decision", "")
        options = self._extract_options(state)
        if choice_idx >= len(options):
            return self._default_action(state)
        option = options[choice_idx]

        if decision == "map_select":
            return {"cmd": "action", "action": "select_map_node",
                    "args": {"col": option["col"], "row": option["row"]}}
        elif decision == "card_reward":
            if option.get("_skip"):
                return {"cmd": "action", "action": "skip_card_reward"}
            return {"cmd": "action", "action": "select_card_reward",
                    "args": {"card_index": choice_idx}}
        elif decision in ("rest_site", "event_choice"):
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": option.get("index", choice_idx)}}
        elif decision == "bundle_select":
            return {"cmd": "action", "action": "select_bundle",
                    "args": {"bundle_index": choice_idx}}
        elif decision == "card_select":
            return {"cmd": "action", "action": "select_cards",
                    "args": {"indices": str(choice_idx)}}
        return self._default_action(state)

    def _default_action(self, state: dict) -> dict:
        if state.get("decision") == "card_reward":
            return {"cmd": "action", "action": "skip_card_reward"}
        return {"cmd": "action", "action": "leave_room"}

    def _parse_response(self, text: str) -> dict:
        text = re.sub(r"```[a-z]*\n?", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        return {"choice": 0}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/agent/test_llm_agent.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add agent/llm_agent.py tests/agent/test_llm_agent.py
git commit -m "feat: add LLMAgent with Claude API, deck summary, and pruned prompt"
```

---

## Task 7: coordinator.py

**Files:**
- Create: `agent/coordinator.py`
- Create: `tests/agent/test_coordinator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/agent/test_coordinator.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import pytest
from unittest.mock import MagicMock, patch

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def make_state(decision, **kwargs):
    base = {"type": "decision", "decision": decision,
            "act": 1, "floor": 1,
            "player": {"hp": 80, "max_hp": 80, "gold": 0, "deck": [], "relics": []}}
    base.update(kwargs)
    return base


def make_game_over(victory=True, hp=60, max_hp=80):
    return {"type": "decision", "decision": "game_over",
            "victory": victory, "act": 3, "floor": 50,
            "player": {"hp": hp, "max_hp": max_hp}}


def test_coordinator_routes_combat_to_rl():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    llm = MagicMock()
    coord = GameCoordinator(rl_agent=rl, llm_agent=llm)

    # Sequence: combat_play → game_over
    states = [
        json.dumps(make_state("combat_play", energy=3, round=1, hand=[], enemies=[])),
        json.dumps(make_game_over(victory=True)),
    ]
    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'), \
         patch.object(coord, '_read_json', side_effect=[None]):  # ready msg
        with patch.object(coord, '_send', side_effect=[
            json.loads(states[0]),  # start_run response
            json.loads(states[1]),  # after end_turn
        ]):
            result = coord.run_game("Ironclad", "test")

    rl.act.assert_called_once()
    llm.act.assert_not_called()
    assert result["victory"] is True


def test_coordinator_routes_map_select_to_llm():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    llm = MagicMock()
    llm.act.return_value = {"cmd": "action", "action": "select_map_node",
                            "args": {"col": 0, "row": 1}}
    coord = GameCoordinator(rl_agent=rl, llm_agent=llm)

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("map_select", choices=[{"col": 0, "row": 1, "type": "rest"}]),
            make_game_over(victory=False),
        ]):
            coord.run_game("Ironclad", "test")

    llm.act.assert_called_once()
    rl.act.assert_not_called()


def test_coordinator_uses_greedy_when_no_llm():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    coord = GameCoordinator(rl_agent=rl, llm_agent=None)  # no LLM

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("map_select", choices=[{"col": 0, "row": 1, "type": "rest"}]),
            make_game_over(victory=False),
        ]):
            result = coord.run_game("Ironclad", "test")

    # Should not crash; greedy_action handles map_select
    assert result is not None


def test_coordinator_game_over_result_structure():
    from agent.coordinator import GameCoordinator

    rl = MagicMock()
    rl.act.return_value = {"cmd": "action", "action": "end_turn"}
    coord = GameCoordinator(rl_agent=rl, llm_agent=None)

    with patch.object(coord, '_start_proc'), \
         patch.object(coord, '_kill_proc'):
        with patch.object(coord, '_send', side_effect=[
            make_state("combat_play", energy=3, round=1, hand=[], enemies=[]),
            make_game_over(victory=True, hp=60, max_hp=80),
        ]):
            result = coord.run_game("Ironclad", "test_seed")

    assert result["victory"] is True
    assert result["hp"] == 60
    assert result["max_hp"] == 80
    assert result["seed"] == "test_seed"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/agent/test_coordinator.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'agent.coordinator'`

- [ ] **Step 3: Implement `agent/coordinator.py`**

```python
#!/usr/bin/env python3
"""coordinator.py — full-game runner combining RL combat + LLM strategy.

Usage:
    python3 agent/coordinator.py --character Ironclad --mode eval-full
    python3 agent/coordinator.py --character Ironclad --mode eval-rl --n-games 20
"""
import argparse, json, os, subprocess, sys

DOTNET = os.path.expanduser("~/.dotnet-arm64/dotnet")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "Sts2Headless", "Sts2Headless.csproj")
CARDS_JSON = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "checkpoints")

RL_DECISIONS = {"combat_play"}
LLM_DECISIONS = {"map_select", "card_reward", "rest_site", "event_choice",
                 "shop", "bundle_select", "card_select"}


class GameCoordinator:
    def __init__(self, rl_agent, llm_agent=None):
        self.rl = rl_agent
        self.llm = llm_agent
        self._proc = None

    def run_game(self, character: str, seed: str, ascension: int = 0) -> dict:
        from agent.combat_env import greedy_action
        self._start_proc()
        try:
            state = self._send({"cmd": "start_run", "character": character,
                                "seed": seed, "ascension": ascension})
            if state is None:
                return {"victory": False, "seed": seed, "error": "start_failed"}

            for step in range(600):
                decision = state.get("decision", "")

                if decision == "game_over":
                    return {
                        "victory": state.get("victory", False),
                        "seed": seed, "steps": step,
                        "act": state.get("act"),
                        "floor": state.get("floor"),
                        "hp": state.get("player", {}).get("hp"),
                        "max_hp": state.get("player", {}).get("max_hp"),
                    }

                if decision in RL_DECISIONS:
                    action = self.rl.act(state)
                elif decision in LLM_DECISIONS:
                    action = self.llm.act(state) if self.llm else greedy_action(state)
                else:
                    action = {"cmd": "action", "action": "proceed"}

                next_state = self._send(action)
                if next_state is None:
                    return {"victory": False, "seed": seed, "steps": step, "error": "eof"}
                state = next_state

            return {"victory": False, "seed": seed, "steps": 600, "error": "timeout"}
        finally:
            self._kill_proc()

    def _start_proc(self):
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=PROJECT_ROOT
        )
        self._read_json()

    def _kill_proc(self):
        if self._proc:
            try:
                self._proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                self._proc.stdin.flush()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def _read_json(self):
        if not self._proc:
            return None
        while True:
            line = self._proc.stdout.readline().strip()
            if not line:
                return None
            if line.startswith("{"):
                return json.loads(line)

    def _send(self, cmd: dict):
        if not self._proc:
            return None
        try:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()
            return self._read_json()
        except Exception:
            return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--mode", choices=["eval-rl", "eval-full"], default="eval-rl")
    parser.add_argument("--n-games", type=int, default=10)
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    args = parser.parse_args()

    if args.checkpoint is None:
        files = sorted(f for f in os.listdir(CHECKPOINT_DIR) if f.startswith(f"ppo_{args.character.lower()}"))
        if not files:
            print(f"No checkpoint found in {CHECKPOINT_DIR}"); sys.exit(1)
        args.checkpoint = os.path.join(CHECKPOINT_DIR, files[-1])
    print(f"Loading RL checkpoint: {args.checkpoint}")

    from agent.rl_agent import RLAgent
    rl = RLAgent(args.checkpoint, CARDS_JSON)

    llm = None
    if args.mode == "eval-full":
        if not args.api_key:
            print("ANTHROPIC_API_KEY not set"); sys.exit(1)
        from agent.llm_agent import LLMAgent
        llm = LLMAgent(api_key=args.api_key, cards_json=CARDS_JSON)

    coord = GameCoordinator(rl_agent=rl, llm_agent=llm)
    print(f"\nRunning {args.n_games} games | {args.character} | {args.mode} | A{args.ascension}")
    print("=" * 60)
    results = []
    for i in range(args.n_games):
        seed = f"eval_{args.character.lower()}_{i}"
        result = coord.run_game(args.character, seed, args.ascension)
        results.append(result)
        status = "WIN" if result.get("victory") else "LOSS"
        print(f"  Game {i+1:2d}: {status} | floor={result.get('floor')} | "
              f"hp={result.get('hp')}/{result.get('max_hp')}")

    wins = sum(1 for r in results if r.get("victory"))
    print(f"\nWin rate: {wins}/{args.n_games} ({100*wins//args.n_games}%)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest tests/agent/ -v
```

Expected: all tests in all 5 test files PASS

- [ ] **Step 5: Commit**

```bash
git add agent/coordinator.py tests/agent/test_coordinator.py
git commit -m "feat: add GameCoordinator with RL+LLM routing and greedy fallback"
```

---

## Task 8: Baseline + Phase 1 Training

**Files:** none (operations only)

- [ ] **Step 1: Measure random-agent baseline (5 games)**

```bash
cd /Users/bytedance/mygit/sts2-cli
python3 python/play_full_run.py 5 Ironclad
```

Record the win rate printed at the end. This is the floor RL must beat 2×.

- [ ] **Step 2: Run 100k-step RL training**

```bash
python3 agent/train.py --character Ironclad --steps 100000 --n-envs 4
```

Expected runtime: ~1–2 hours on Mac M-series.

- [ ] **Step 3: Eval-RL mode (20 games, greedy strategy)**

```bash
python3 agent/coordinator.py --character Ironclad --mode eval-rl --n-games 20
```

Target: win rate > 2× random baseline.

- [ ] **Step 4: Append results to agent/bug.md**

Add a `## Phase 1 Results` section at the bottom of `agent/bug.md` with:
- Random baseline win rate (N/5)
- RL eval-rl win rate (N/20)
- Average floor reached

```bash
git add agent/bug.md
git commit -m "docs: record Phase 1 RL training results"
```

---

## Task 9: Phase 2 — LLM Eval

**Files:** none (requires `ANTHROPIC_API_KEY`)

- [ ] **Step 1: Run eval-full (20 games with LLM strategy)**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
python3 agent/coordinator.py --character Ironclad --mode eval-full --n-games 20
```

- [ ] **Step 2: Append Phase 2 results to agent/bug.md**

Add `## Phase 2 Results` section comparing:
- eval-rl win rate (from Task 8 Step 3)
- eval-full win rate (from this task)

Success metric: eval-full win rate > eval-rl win rate.

```bash
git add agent/bug.md
git commit -m "docs: record Phase 2 results (LLM strategy vs greedy heuristics)"
```
