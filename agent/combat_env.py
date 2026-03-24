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
