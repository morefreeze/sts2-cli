"""
combat_env.py — gymnasium.Env for STS2 combat training.

Also exports greedy_action(state) as a module-level function for use by
coordinator.py and _advance_to_combat().

Episode = one single combat (not a full game run).
Reward shaping: per-step damage/block/kill signals + end-of-combat bonus.

Design: simple 1:1 mapping — each env.step() = one game action (including
end_turn). No auto-skip. Policy and value networks are separated in train.py
to prevent value-loss gradient from corrupting policy on forced end_turn steps.
"""
import json, os, subprocess, random, time, select, sys
import gymnasium as gym
import numpy as np
from gymnasium.spaces import Box, Discrete
from agent.state_encoder import StateEncoder
from agent.strategy import Act1SafeStrategy, HpAwareMapStrategy, MapStrategy, rest_site_action
from agent.card_scoring import score_card, pick_best_card, pick_worst_card

# Swappable map strategy — change globally via set_map_strategy()
_map_strategy: MapStrategy = HpAwareMapStrategy()


def set_map_strategy(strategy: MapStrategy):
    """Replace the global map strategy. Call before training or evaluation."""
    global _map_strategy
    _map_strategy = strategy

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
    return "dotnet"

DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")


def _score_event_option(opt: dict) -> float:
    """Score an event option by keyword analysis. Higher = better."""
    title = (opt.get("title") or "").lower()
    desc = (opt.get("description") or "").lower()
    text = title + " " + desc
    score = 0.0
    # Strong negatives
    if "lose max" in text or "maximum hp" in text:
        score -= 10.0
    if "curse" in text:
        score -= 8.0
    if "lose all gold" in text:
        score -= 5.0
    if "torment" in title:
        score -= 5.0  # Neow's Torment adds a negative card
    if "take" in text and "damage" in text:
        score -= 3.0
    if "lose" in text and "gold" in text:
        score -= 2.0
    # Negative: adds basic/weak cards to deck
    if "add" in text and ("additional strike" in text or "additional defend" in text):
        score -= 3.0
    # Strong positives
    if "rare" in text and ("card" in text or "obtain" in text or "random" in text):
        score += 8.0
    if "remove" in text and ("card" in text or "deck" in text):
        score += 6.0  # deck thinning = very valuable
    if "relic" in text and "add" not in text:
        score += 5.0  # relics without downside
    elif "relic" in text:
        score += 2.0  # relics with some downside (e.g. also adds Strike)
    if "upgrade" in text:
        score += 4.0
    if "gain" in text and "gold" in text:
        score += 3.0
    if "max hp" in text and ("raise" in text or "increase" in text or "gain" in text):
        score += 3.0  # gaining max HP is good
    if "potion" in text:
        score += 2.0
    if "heal" in text and "hp" in text:
        score += 2.0
    return score


def greedy_action(state: dict) -> dict:
    """Greedy heuristic for non-combat decisions. Used during training and by coordinator."""
    decision = state.get("decision", "")

    if decision == "map_select":
        choices = state.get("choices", [])
        if choices:
            return _map_strategy.choose(state, choices)

    elif decision == "card_reward":
        cards = state.get("cards", [])
        if cards:
            best = pick_best_card(cards)
            if best is not None:
                return {"cmd": "action", "action": "select_card_reward",
                        "args": {"card_index": best}}
        return {"cmd": "action", "action": "skip_card_reward"}

    elif decision == "rest_site":
        return rest_site_action(state, state.get("options", []))

    elif decision == "event_choice":
        options = state.get("options", [])
        available = [o for o in options if not o.get("is_locked")]
        if available:
            best = max(available, key=_score_event_option)
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": best["index"]}}
        return {"cmd": "action", "action": "leave_room"}

    elif decision == "bundle_select":
        bundles = state.get("bundles", [])
        if len(bundles) >= 2:
            scores = [sum(score_card(c) for c in b.get("cards", [])) for b in bundles]
            best_idx = scores.index(max(scores))
        else:
            best_idx = 0
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": best_idx}}

    elif decision == "card_select":
        cards = state.get("cards", [])
        if cards:
            worst = pick_worst_card(cards, threshold=10.0)  # always remove worst if possible
            idx = worst if worst is not None else 0
            return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        # Try to remove a card if affordable (thins deck)
        removal_cost = state.get("card_removal_cost")
        if removal_cost and gold >= removal_cost:
            return {"cmd": "action", "action": "remove_card"}
        # Buy best card by score that we can afford — only if score >= 6.0
        cards = [c for c in state.get("cards", [])
                 if c.get("is_stocked") and c.get("cost", 999) <= gold]
        if cards:
            best = max(cards, key=lambda c: score_card(c))
            if score_card(best) >= 6.0:
                return {"cmd": "action", "action": "buy_card",
                        "args": {"card_index": best.get("index", 0)}}
        return {"cmd": "action", "action": "leave_room"}

    return {"cmd": "action", "action": "proceed"}


def _total_enemy_hp(state: dict) -> int:
    return sum(e.get("hp", 0) for e in state.get("enemies", []))


def _player_hp(state: dict) -> int:
    return state.get("player", {}).get("hp", 0)


class CombatEnv(gym.Env):
    """
    Gymnasium environment for STS2 combat.

    Each episode = one single combat encounter.
    Each step = one game action (play_card or end_turn).
    No auto-skip — forced end_turn steps are in the buffer. The policy and value
    networks must be SEPARATE (net_arch=dict(pi=..., vf=...)) to prevent
    value-loss gradient on forced steps from corrupting the policy head.
    """

    def __init__(self, cards_json: str = None, character: str = "Ironclad",
                 ascension: int = 0, seed: str = None, dry_run: bool = False,
                 seed_prefix: str = "t", max_floor: int = 0):
        super().__init__()
        if cards_json is None:
            cards_json = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
        self.enc = StateEncoder(cards_json)
        self.character = character
        self.ascension = ascension
        self._seed = seed
        self._seed_prefix = seed_prefix
        self.dry_run = dry_run
        self.max_floor = max_floor  # 0 = unlimited

        self.observation_space = Box(low=0.0, high=1.0,
                                     shape=(self.enc.obs_size,), dtype=np.float32)
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0
        self._prev_enemy_hp = 0
        self._prev_player_hp = 0
        self._combat_start_enemy_hp = 1
        self._combat_start_player_max_hp = 1
        self._current_floor = 1
        self._game_alive = False
        self._read_buf = b""
        self._combat_steps = 0
        self.max_combat_steps = 200  # floor 4+ fights can take 100+ steps with a learning policy

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        # Try to advance to next combat in the current run
        if self._game_alive and self._current_state is not None:
            cur_floor = (self._current_state.get("floor")
                         or self._current_state.get("context", {}).get("floor", 0))
            if self.max_floor > 0 and isinstance(cur_floor, int) and cur_floor >= self.max_floor:
                # Curriculum: restart to keep fighting easy enemies
                self._game_alive = False
                self._kill_proc()
            state = self._advance_to_combat(self._current_state)
            if state and state.get("decision") == "combat_play":
                self._init_combat_tracking(state)
                self._current_state = state
                return self.enc.encode(state), {}

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
        self._current_state = state
        self._combat_steps = 0
        return self.enc.encode(state), {}

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size, dtype=np.float32), -2.0, True, False, {}

        self._combat_steps += 1
        if self._combat_steps > self.max_combat_steps:
            # Combat too long — treat as defeat to avoid wasting time
            last_obs = self.enc.encode(self._current_state)
            return last_obs, -2.0, True, False, {"timeout": True}

        cmd = self.enc.decode(int(action), self._current_state)
        self._last_cmd = cmd
        state = self._send(cmd)

        # Detect stuck: end_turn ignored by engine (round/HP unchanged)
        if (state and state.get("decision") == "combat_play"
                and cmd.get("action") == "end_turn"
                and state.get("round") == self._current_state.get("round")
                and state.get("player", {}).get("hp") == self._current_state.get("player", {}).get("hp")):
            # Try proceed to unstick
            for _ in range(5):
                state = self._send({"cmd": "action", "action": "proceed"})
                if state is None or state.get("decision") != "combat_play":
                    break
                if state.get("round") != self._current_state.get("round"):
                    break
            if state and state.get("decision") == "combat_play" and \
                    state.get("round") == self._current_state.get("round"):
                # Still stuck — kill this combat
                last_obs = self.enc.encode(self._current_state)
                self._game_alive = False
                self._kill_proc()
                return last_obs, -2.0, True, False, {"stuck": True}

        if state is None:
            self._game_alive = False
            last_obs = self.enc.encode(self._current_state)
            cmd_str = json.dumps(getattr(self, "_last_cmd", None))
            floor = self._current_floor
            hand_size = len(self._current_state.get("hand", []))
            n_enemies = len(self._current_state.get("enemies", []))
            print(f"\n[CRASH] floor={floor} cmd={cmd_str} hand={hand_size} enemies={n_enemies}",
                  file=sys.stderr, flush=True)
            return last_obs, -2.0, True, False, {"crashed": True, "floor": floor}

        # C# returns {"decision":"stuck"} when enemy turn deadlocks for 15s.
        # Kill the process immediately — returning a garbage combat_play state and
        # continuing would corrupt the C# process → cr=100% cascade.
        if state.get("decision") == "stuck":
            self._game_alive = False
            self._kill_proc()
            last_obs = self.enc.encode(self._current_state)
            return last_obs, -2.0, True, False, {"crashed": True, "floor": self._current_floor}

        decision = state.get("decision", "")
        reward = self._shaping_reward(state)

        # Use last known combat obs for terminal states (NOT zeros — zeros
        # confuse the value function because they're too similar to sparse
        # combat states, causing gradient pollution that collapses entropy)
        last_obs = self.enc.encode(self._current_state)

        if decision == "game_over":
            self._game_alive = False
            r = reward + self._terminal_reward(state)
            return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                               "victory": state.get("victory", False)}

        if decision == "combat_play":
            self._current_state = state
            return self.enc.encode(state), reward, False, False, {}

        # Mid-combat card_select (e.g. boss mechanics, card effects that trigger selection)
        # Auto-handle these without ending the episode — they appear in Boss/Monster/Elite rooms
        # while CombatManager is still active. Without this, Python incorrectly treats them as
        # "combat won" and then resume in the same boss fight as a "new" combat.
        if decision == "card_select":
            context = state.get("context", {})
            if context.get("room_type") in ("Boss", "Monster", "Elite"):
                for _ in range(10):
                    auto_cmd = greedy_action(state)
                    state = self._send(auto_cmd)
                    if state is None:
                        self._game_alive = False
                        return last_obs, -2.0, True, False, {"crashed": True}
                    if state.get("decision") in ("combat_play", "game_over"):
                        break
                    if state.get("decision") != "card_select":
                        break
                if state.get("decision") == "combat_play":
                    self._current_state = state
                    return self.enc.encode(state), reward, False, False, {}
                if state.get("decision") == "game_over":
                    self._game_alive = False
                    r = reward + self._terminal_reward(state)
                    return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                                       "victory": state.get("victory", False)}

        # Combat ended (transitioned to card_reward, map_select, etc.) — we won
        reward += self._combat_win_reward(state)
        self._current_state = state
        return last_obs, reward, True, False, {"floor": self._current_floor, "combat_won": True}

    def action_masks(self) -> np.ndarray:
        if self._current_state is None:
            return np.ones(41, dtype=bool)
        return self.enc.action_mask(self._current_state)

    def close(self):
        self._kill_proc()

    def set_max_floor(self, max_floor: int) -> None:
        self.max_floor = max_floor

    def _init_combat_tracking(self, state: dict):
        self._prev_enemy_hp = _total_enemy_hp(state)
        self._prev_player_hp = _player_hp(state)
        self._combat_start_enemy_hp = max(self._prev_enemy_hp, 1)
        self._combat_start_player_max_hp = max(state.get("player", {}).get("max_hp", 1), 1)
        floor = state.get("floor") or state.get("context", {}).get("floor", 1)
        self._current_floor = int(floor) if isinstance(floor, (int, float)) and floor > 0 else 1

    def _shaping_reward(self, next_state: dict) -> float:
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        dmg_reward = 0.15 * enemy_hp_lost / self._combat_start_enemy_hp
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        # Increased from -0.25: HP conservation is the primary combat objective
        hp_penalty = -0.35 * player_hp_lost / self._combat_start_player_max_hp

        # Block effectiveness: reward blocking incoming damage
        incoming = 0
        for e in next_state.get("enemies", []):
            for it in (e.get("intents") or []):
                if it.get("type", "").lower() == "attack":
                    incoming += it.get("damage", 0) * (it.get("hits") or 1)
        player_block = next_state.get("player", {}).get("block", 0)
        block_reward = 0.0
        if incoming > 0 and player_block > 0:
            effective_block = min(player_block, incoming)
            block_reward = 0.10 * effective_block / self._combat_start_player_max_hp

        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp

        # No step penalty — avoids incentivizing fast death spiral
        step_penalty = 0.0
        return dmg_reward + hp_penalty + block_reward + step_penalty

    def _combat_win_reward(self, state: dict) -> float:
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        hp_ratio = hp / max_hp
        # Base win reward scaled by HP remaining — pure combat efficiency signal
        reward = 2.0 * hp_ratio
        # HP survival bonus tiers
        if hp_ratio >= 0.9:
            reward += 0.5
        elif hp_ratio >= 0.7:
            reward += 0.25
        # Small floor weight so curriculum still has gradient (strategy layer owns this signal)
        # Reduced 0.15→0.05/floor, cap 2.4→0.8 so HP efficiency dominates
        floor_bonus = min((self._current_floor - 1) * 0.05, 0.8)
        reward += floor_bonus
        return reward

    def _terminal_reward(self, state: dict) -> float:
        if state.get("victory", False):
            return 2.0
        return -2.0

    def _advance_to_combat(self, state: dict) -> dict:
        for _ in range(200):
            if state is None:
                return {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}
            if state.get("decision") in ("combat_play", "game_over"):
                return state
            cmd = greedy_action(state)
            state = self._send(cmd)
        return state or {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}

    def _start_proc(self):
        crash_log = os.path.join(PROJECT_ROOT, "crash_stderr.log")
        self._crash_log_f = open(crash_log, "a")
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._crash_log_f, cwd=PROJECT_ROOT,
            start_new_session=True,  # own process group — killed with os.killpg
        )
        self._read_buf = b""
        ready = self._read_json(timeout_sec=15.0)
        if ready is None:
            # Game process failed to produce ready message — kill it now
            self._kill_proc()
            time.sleep(1.0)  # back-off before retry

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
                try: self._proc.kill()
                except Exception: pass
            self._proc = None
        self._game_alive = False
        self._read_buf = b""

    def _read_json(self, timeout_sec: float = 5.0):
        if self._proc is None:
            return None
        try:
            fileno = self._proc.stdout.fileno()
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
                    return None
                self._read_buf += chunk
                while b"\n" in self._read_buf:
                    line, self._read_buf = self._read_buf.split(b"\n", 1)
                    line = line.strip()
                    if line.startswith(b"{"):
                        try:
                            return json.loads(line)
                        except json.JSONDecodeError:
                            continue
            self._kill_proc()
            return None
        except Exception:
            self._kill_proc()
            return None

    def _send(self, cmd: dict):
        if self._proc is None:
            return None
        try:
            self._proc.stdin.write((json.dumps(cmd) + "\n").encode())
            self._proc.stdin.flush()
            # DoEndTurn can take up to ~15s (3s wait + 5s cancel + nuclear fallback).
            # Use 20s timeout to avoid false crash on slow enemy turns.
            return self._read_json(timeout_sec=20.0)
        except Exception:
            self._kill_proc()
            return None


def _dummy_combat_state() -> dict:
    return {
        "decision": "combat_play", "energy": 3, "round": 1,
        "hand": [{"index": 0, "id": {"en": "STRIKE"}, "cost": 1,
                  "can_play": True, "target_type": "AnyEnemy", "type": "Attack",
                  "stats": {"damage": 6}}],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [{"hp": 30, "max_hp": 30, "block": 0,
                     "intent": {"type": "Attack", "damage": 10, "times": 1}, "buffs": []}],
    }
