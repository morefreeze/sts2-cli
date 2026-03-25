"""
combat_env.py — gymnasium.Env for STS2 combat training.

Also exports greedy_action(state) as a module-level function for use by
coordinator.py and _advance_to_combat().

Episode = one single combat (not a full game run).
Reward shaping: per-step damage/block/kill signals + end-of-combat bonus.
"""
import json, os, subprocess, random, time
import gymnasium as gym
import numpy as np
from gymnasium.spaces import Box, Discrete
from agent.state_encoder import StateEncoder

def _find_dotnet():
    """Find .NET SDK binary across platforms."""
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet",
              "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "dotnet"  # fallback, let it fail with a clear error

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")


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


def _total_enemy_hp(state: dict) -> int:
    return sum(e.get("hp", 0) for e in state.get("enemies", []))


def _player_hp(state: dict) -> int:
    return state.get("player", {}).get("hp", 0)


class CombatEnv(gym.Env):
    """
    Gymnasium environment for STS2 combat.

    Each episode = one single combat encounter, NOT a full game run.
    After combat ends (win or lose), the episode terminates.
    On reset, the game process is reused — we advance to the next combat.
    If no more combats (game over), we restart a new run.

    Reward:
      Per-step shaping:
        +0.02 per enemy HP point lost (normalized by start-of-combat total)
        -0.02 per player HP point lost (normalized by max HP)
      End-of-combat:
        Win:  +1.0 × (player_hp / max_hp)
        Lose: -0.5
    """

    def __init__(self, cards_json: str = None, character: str = "Ironclad",
                 ascension: int = 0, seed: str = None, dry_run: bool = False,
                 seed_prefix: str = "t"):
        super().__init__()
        if cards_json is None:
            cards_json = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
        self.enc = StateEncoder(cards_json)
        self.character = character
        self.ascension = ascension
        self._seed = seed
        self._seed_prefix = seed_prefix
        self.dry_run = dry_run

        self.observation_space = Box(low=0.0, high=1.0,
                                     shape=(self.enc.obs_size,), dtype=np.float32)
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0
        # Track HP between steps for shaping reward
        self._prev_enemy_hp = 0
        self._prev_player_hp = 0
        self._combat_start_enemy_hp = 1  # avoid /0
        self._combat_start_player_max_hp = 1
        self._game_alive = False  # is there a live game process with an active run?

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        # Try to advance to next combat in the current run
        if self._game_alive and self._current_state is not None:
            state = self._advance_to_combat(self._current_state)
            if state.get("decision") == "combat_play":
                self._init_combat_tracking(state)
                self._current_state = state
                return self.enc.encode(state), {}
            # game_over or stuck — start a fresh run

        # Start a fresh game process + run
        self._kill_proc()
        self._start_proc()
        run_seed = self._seed or f"{self._seed_prefix}_{self._run_counter}_{random.randint(0,99999)}"
        self._run_counter += 1
        state = self._send({"cmd": "start_run", "character": self.character,
                            "seed": run_seed, "ascension": self.ascension})
        if state is None:
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        self._game_alive = True
        state = self._advance_to_combat(state)
        if state is None or state.get("decision") != "combat_play":
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        self._init_combat_tracking(state)
        # Auto-skip through forced single-option states to a real decision point
        state = self._skip_forced_actions(state)
        if state is None or state.get("decision") != "combat_play":
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        self._current_state = state
        return self.enc.encode(state), {}

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size, dtype=np.float32), -0.5, True, False, {}

        cmd = self.enc.decode(int(action), self._current_state)
        state, reward = self._execute_and_skip(cmd)

        if state is None:
            self._game_alive = False
            return np.zeros(self.enc.obs_size, dtype=np.float32), reward - 0.5, True, False, {"crashed": True}

        decision = state.get("decision", "")

        if decision == "game_over":
            self._game_alive = False
            return np.zeros(self.enc.obs_size, dtype=np.float32), reward + self._terminal_reward(state), True, False, {}

        if decision == "combat_play":
            self._current_state = state
            return self.enc.encode(state), reward, False, False, {}

        # Combat ended — we won
        reward += self._combat_win_reward(state)
        self._current_state = state
        return np.zeros(self.enc.obs_size, dtype=np.float32), reward, True, False, {"combat_won": True}

    def _skip_forced_actions(self, state: dict):
        """Auto-execute combat states where only 1 action is valid (no real choice).
        Accumulates shaping reward internally. Returns state with >1 valid action or terminal."""
        for _ in range(50):  # safety limit
            if state is None:
                return None
            decision = state.get("decision", "")
            if decision != "combat_play":
                return state  # game_over, card_reward, etc.

            mask = self.enc.action_mask(state)
            if mask.sum() > 1:
                return state  # real choice — return to agent

            # Single valid action — auto-execute
            # Update shaping tracking
            self._prev_enemy_hp = _total_enemy_hp(state)
            self._prev_player_hp = _player_hp(state)

            only_action = int(np.where(mask)[0][0])
            auto_cmd = self.enc.decode(only_action, state)
            state = self._send(auto_cmd)

        return state

    def _execute_and_skip(self, cmd: dict):
        """Execute agent's chosen action, then skip through any forced follow-ups.
        Returns (state, shaping_reward_from_skipped_steps)."""
        state = self._send(cmd)
        if state is None:
            return None, 0.0

        # Collect shaping reward from the agent's action
        reward = self._shaping_reward(state)

        # Skip through forced single-option states
        if state.get("decision") == "combat_play":
            mask = self.enc.action_mask(state)
            if mask.sum() <= 1:
                # Need to auto-execute through forced actions
                skipped_reward = 0.0
                for _ in range(50):
                    if state is None or state.get("decision") != "combat_play":
                        break
                    m = self.enc.action_mask(state)
                    if m.sum() > 1:
                        break
                    self._prev_enemy_hp = _total_enemy_hp(state)
                    self._prev_player_hp = _player_hp(state)
                    auto_cmd = self.enc.decode(int(np.where(m)[0][0]), state)
                    state = self._send(auto_cmd)
                    if state is not None:
                        skipped_reward += self._shaping_reward(state)
                reward += skipped_reward

        return state, reward

    def action_masks(self) -> np.ndarray:
        if self._current_state is None:
            return np.ones(41, dtype=bool)
        mask = self.enc.action_mask(self._current_state)
        # Should always have >1 valid action since we auto-execute single-option states
        assert mask.sum() >= 1, "No valid actions"
        return mask

    def close(self):
        self._kill_proc()

    def _init_combat_tracking(self, state: dict):
        """Initialize tracking at the start of a new combat."""
        self._prev_enemy_hp = _total_enemy_hp(state)
        self._prev_player_hp = _player_hp(state)
        self._combat_start_enemy_hp = max(self._prev_enemy_hp, 1)
        self._combat_start_player_max_hp = max(state.get("player", {}).get("max_hp", 1), 1)

    def _shaping_reward(self, next_state: dict) -> float:
        """Per-step reward based on HP changes."""
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)

        # Reward for dealing damage to enemies (normalized by start-of-combat total)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        dmg_reward = 0.02 * enemy_hp_lost / self._combat_start_enemy_hp

        # Penalty for losing player HP (normalized by max HP)
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        hp_penalty = -0.02 * player_hp_lost / self._combat_start_player_max_hp

        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp

        return dmg_reward + hp_penalty

    def _combat_win_reward(self, state: dict) -> float:
        """Bonus for winning a combat. Higher HP = bigger bonus."""
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        return 1.0 * (hp / max_hp)

    def _terminal_reward(self, state: dict) -> float:
        """Reward for game_over."""
        if state.get("victory", False):
            return 2.0  # beat the boss — big bonus
        return -0.5

    def _advance_to_combat(self, state: dict) -> dict:
        for _ in range(200):
            if state.get("decision") in ("combat_play", "game_over"):
                return state
            cmd = greedy_action(state)
            next_state = self._send(cmd)
            if next_state is None:
                self._game_alive = False
                return {"decision": "game_over", "victory": False,
                        "player": {"hp": 0, "max_hp": 80}}
            state = next_state
        return state

    def _start_proc(self):
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT
        )
        self._read_json(timeout_sec=15.0)  # startup can be slow

    def _kill_proc(self):
        if self._proc is not None:
            try:
                self._proc.stdin.write(b'{"cmd":"quit"}\n')
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
        self._game_alive = False

    def _read_json(self, timeout_sec: float = 5.0):
        if self._proc is None:
            return None
        try:
            import select
            fileno = self._proc.stdout.fileno()
            buf = ""
            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fileno], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                chunk = os.read(fileno, 4096)
                if not chunk:
                    return None  # EOF
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            return json.loads(line)
                        except json.JSONDecodeError:
                            continue
            # Timeout
            self._kill_proc()
            return None
        except Exception:
            self._kill_proc()
            return None

    def _send(self, cmd: dict):
        if self._proc is None:
            return None
        try:
            data = (json.dumps(cmd) + "\n").encode("utf-8")
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
            return self._read_json()
        except Exception:
            self._kill_proc()
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
