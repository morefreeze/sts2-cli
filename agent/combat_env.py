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

import json, os, subprocess, random, time, select
import gymnasium as gym
import numpy as np
from gymnasium.spaces import Box, Discrete
from agent.state_encoder import StateEncoder


def _find_dotnet():
    """Find .NET SDK binary across platforms."""
    for p in [
        os.path.expanduser("~/.dotnet-arm64/dotnet"),
        os.path.expanduser("~/.dotnet/dotnet"),
        "/usr/local/share/dotnet/dotnet",
        "/usr/local/Cellar/dotnet/10.0.105/bin/dotnet",
        "/usr/local/bin/dotnet",
        "dotnet",
    ]:
        if p != "dotnet" and not os.path.isfile(p):
            continue
        try:
            r = subprocess.run(
                [p, "--version"], capture_output=True, text=True, timeout=30
            )
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "dotnet"


DOTNET = _find_dotnet()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.join(PROJECT_ROOT, "src", "Sts2Headless", "Sts2Headless.csproj")


def greedy_combat_action(state: dict) -> dict:
    """Rule-based combat policy: prioritize damage, use block when threatened."""
    hand = state.get("hand", [])
    energy = state.get("energy", 0)
    enemies = state.get("enemies", [])
    player = state.get("player", {})
    player_hp = player.get("hp", 0)
    player_block = player.get("block", 0)
    player_max_hp = player.get("max_hp", 80)

    playable = [c for c in hand if c.get("can_play", False)]
    if not playable:
        playable = [
            c for c in hand if c.get("cost", 99) <= energy and c.get("cost", 99) >= 0
        ]

    if not playable:
        if not hand and energy == 0:
            return {"cmd": "action", "action": "proceed"}
        return {"cmd": "action", "action": "end_turn"}

    incoming_damage = 0
    for e in enemies:
        intent = e.get("intent") or {}
        itype = (intent.get("type") or "").lower()
        if "attack" in itype:
            dmg = intent.get("damage", 0) * intent.get("times", 1)
            incoming_damage += max(dmg - e.get("block", 0), 0)

    effective_incoming = max(incoming_damage - player_block, 0)
    hp_pct = player_hp / max(player_max_hp, 1)
    in_danger = effective_incoming > player_hp * 0.4 or (
        hp_pct < 0.3 and effective_incoming > 0
    )

    live_enemies = [e for e in enemies if e.get("hp", 0) > 0]
    primary_target_idx = 0
    if live_enemies:
        weakest = min(live_enemies, key=lambda e: e.get("hp", 999))
        primary_target_idx = weakest.get("index", 0)

    powers = [c for c in playable if (c.get("type") or "").lower() == "power"]
    attacks = [c for c in playable if (c.get("type") or "").lower() == "attack"]
    skills = [c for c in playable if (c.get("type") or "").lower() == "skill"]
    blocks = [c for c in skills if _card_gives_block(c)]
    non_block_skills = [c for c in skills if not _card_gives_block(c)]

    if powers:
        card = powers[0]
        return _make_play_action(card, enemies)

    if in_danger and blocks:
        card = max(blocks, key=lambda c: c.get("stats", {}).get("block", 0))
        return _make_play_action(card, enemies)

    if attacks:
        card = max(attacks, key=lambda c: _card_damage_score(c))
        return _make_play_action(card, enemies, primary_target_idx)

    if non_block_skills:
        card = non_block_skills[0]
        return _make_play_action(card, enemies)

    if blocks:
        card = max(blocks, key=lambda c: c.get("stats", {}).get("block", 0))
        return _make_play_action(card, enemies)

    return {"cmd": "action", "action": "end_turn"}


def _card_gives_block(card: dict) -> bool:
    stats = card.get("stats") or {}
    return stats.get("block", 0) > 0


def _card_damage_score(card: dict) -> float:
    stats = card.get("stats") or {}
    dmg = stats.get("damage", 0)
    times = stats.get("times", 1)
    return dmg * times


def _make_play_action(card: dict, enemies: list, target_idx: int = None) -> dict:
    needs_target = (card.get("target_type") or "").lower() == "anyenemy"
    args = {"card_index": card["index"]}
    if needs_target:
        if target_idx is not None:
            args["target_index"] = target_idx
        else:
            live = [e for e in enemies if e.get("hp", 0) > 0]
            if live:
                args["target_index"] = min(live, key=lambda e: e.get("hp", 999)).get(
                    "index", 0
                )
            else:
                args["target_index"] = 0
    return {"cmd": "action", "action": "play_card", "args": args}


def _card_reward_score(card: dict) -> float:
    rarity = card.get("rarity", "Common")
    ctype = (card.get("type") or "").lower()
    stats = card.get("stats") or {}
    score = 0.0

    rarity_bonus = {"Rare": 30, "Uncommon": 15, "Common": 5}
    score += rarity_bonus.get(rarity, 5)

    if ctype == "power":
        score += 20
    elif ctype == "attack":
        dmg = stats.get("damage", 0) * stats.get("times", 1)
        score += 10 + dmg * 0.5
    elif ctype == "skill":
        blk = stats.get("block", 0)
        score += 8 + blk * 0.3

    if stats.get("draw", 0) > 0:
        score += 15
    if stats.get("energy", 0) > 0:
        score += 12

    return score


def greedy_action(state: dict) -> dict:
    """Greedy heuristic for non-combat decisions. Used during training and by coordinator."""
    decision = state.get("decision", "")

    if decision == "combat_play":
        return greedy_combat_action(state)

    if decision == "map_select":
        choices = state.get("choices", [])
        if choices:
            player = state.get("player", {})
            hp = player.get("hp", 80)
            max_hp = max(player.get("max_hp", 80), 1)
            hp_pct = hp / max_hp
            ctx = state.get("context", {})
            floor = ctx.get("floor", 0)
            act = ctx.get("act", 1)
            deck = player.get("deck", [])
            deck_size = len(deck) if deck else 10

            type_priority = {
                "Treasure": 100,
                "RestSite": 90 if hp_pct < 0.6 else (50 if hp_pct < 0.8 else 25),
                "Shop": 60,
                "Unknown": 45,
                "Monster": 40 if hp_pct > 0.3 else 15,
                "Elite": 55 if (hp_pct > 0.75 and deck_size >= 12) else 5,
                "Boss": 90,
            }

            if act >= 2:
                type_priority["RestSite"] = min(type_priority["RestSite"] + 10, 95)
                if hp_pct < 0.5:
                    type_priority["Monster"] = 10
                    type_priority["Elite"] = 2

            chosen = max(choices, key=lambda c: type_priority.get(c.get("type", ""), 0))
            return {
                "cmd": "action",
                "action": "select_map_node",
                "args": {"col": chosen["col"], "row": chosen["row"]},
            }

    elif decision == "card_reward":
        cards = state.get("cards", [])
        if cards:
            best = max(cards, key=lambda c: _card_reward_score(c))
            idx = cards.index(best)
            return {
                "cmd": "action",
                "action": "select_card_reward",
                "args": {"card_index": idx},
            }
        return {"cmd": "action", "action": "skip_card_reward"}

    elif decision == "rest_site":
        options = state.get("options", [])
        enabled = [o for o in options if o.get("is_enabled", True)]
        player = state.get("player", {})
        hp_pct = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
        upgrade = next((o for o in enabled if o.get("option_id") == "UPGRADE"), None)
        if hp_pct > 0.75 and upgrade:
            choice = upgrade
        elif hp_pct < 0.55 and heal:
            choice = heal
        elif heal:
            choice = heal
        else:
            choice = enabled[0] if enabled else None
        if choice:
            return {
                "cmd": "action",
                "action": "choose_option",
                "args": {"option_index": choice["index"]},
            }

    elif decision == "event_choice":
        options = state.get("options", [])
        choice = next((o for o in options if not o.get("is_locked")), None)
        if choice:
            return {
                "cmd": "action",
                "action": "choose_option",
                "args": {"option_index": choice["index"]},
            }
        return {"cmd": "action", "action": "leave_room"}

    elif decision == "bundle_select":
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}

    elif decision == "card_select":
        cards = state.get("cards", [])
        if cards:
            return {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        # Try to remove a card if affordable (thins deck)
        removal_cost = state.get("card_removal_cost")
        if removal_cost and gold >= removal_cost:
            return {"cmd": "action", "action": "remove_card"}
        # Buy cheapest affordable card
        cards = [
            c
            for c in state.get("cards", [])
            if c.get("is_stocked") and c.get("cost", 999) <= gold
        ]
        if cards:
            cheapest = min(cards, key=lambda c: c.get("cost", 999))
            return {
                "cmd": "action",
                "action": "buy_card",
                "args": {"card_index": cheapest.get("index", 0)},
            }
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

    def __init__(
        self,
        cards_json: str = None,
        character: str = "Ironclad",
        ascension: int = 0,
        seed: str = None,
        dry_run: bool = False,
        seed_prefix: str = "t",
    ):
        super().__init__()
        if cards_json is None:
            cards_json = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")
        self.enc = StateEncoder(cards_json)
        self.character = character
        self.ascension = ascension
        self._seed = seed
        self._seed_prefix = seed_prefix
        self.dry_run = dry_run

        self.observation_space = Box(
            low=0.0, high=1.0, shape=(self.enc.obs_size,), dtype=np.float32
        )
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0
        self._prev_enemy_hp = 0
        self._prev_player_hp = 0
        self._combat_start_enemy_hp = 1
        self._combat_start_player_max_hp = 1
        self._game_alive = False
        self._read_buf = b""
        self._combat_steps = 0
        self.max_combat_steps = 150  # ~30 turns × 5 actions/turn
        self._consecutive_end_turn = 0
        self._max_consecutive_end_turn = (
            10  # timeout if player ends turn 10x without progress
        )

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self.enc.encode(self._current_state), {}

        # Try to advance to next combat in the current run
        if self._game_alive and self._current_state is not None:
            state = self._advance_to_combat(self._current_state)
            if state and state.get("decision") == "combat_play":
                self._init_combat_tracking(state)
                self._current_state = state
                return self.enc.encode(state), {}

        # Start a fresh game process + run
        self._kill_proc()
        self._start_proc()
        run_seed = (
            self._seed
            or f"{self._seed_prefix}_{self._run_counter}_{random.randint(0, 99999)}"
        )
        self._run_counter += 1
        state = self._send(
            {
                "cmd": "start_run",
                "character": self.character,
                "seed": run_seed,
                "ascension": self.ascension,
            }
        )
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
        self._consecutive_end_turn = 0
        return self.enc.encode(state), {}

    def _is_dangerous(self, state: dict) -> bool:
        """Check if player is about to die in next enemy attack or can't progress."""
        if state is None:
            return False
        player_hp = state.get("player", {}).get("hp", 999)
        player_block = state.get("player", {}).get("block", 0)

        # Check if we have any playable cards (can't end_turn)
        hand = state.get("hand", [])
        has_playable = any(c.get("can_play", False) for c in hand)

        # If we have playable cards and HP is low, we're in danger of crash
        if has_playable and player_hp <= 25:
            return True

        if player_hp > 30:
            return False
        for e in state.get("enemies", []):
            intents = e.get("intents") or []
            if not isinstance(intents, list):
                intents = [intents] if intents else []
            for it in intents:
                if it.get("type") == "Attack":
                    dmg = it.get("damage", 0)
                    actual_dmg = max(0, dmg - player_block)
                    if actual_dmg >= player_hp:
                        return True
        return False

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size, dtype=np.float32), -0.5, True, False, {}

        self._combat_steps += 1
        if self._combat_steps > self.max_combat_steps:
            # Combat too long — treat as defeat to avoid wasting time
            last_obs = self.enc.encode(self._current_state)
            return last_obs, -0.5, True, False, {"timeout": True}

        cmd = self.enc.decode(int(action), self._current_state)
        state = self._send(cmd)

        # Track consecutive end_turn for timeout detection (only when round doesn't advance)
        if (
            cmd.get("action") == "end_turn"
            and state
            and state.get("round") == self._current_state.get("round")
        ):
            self._consecutive_end_turn += 1
        else:
            self._consecutive_end_turn = 0

        # Detect crash: end_turn doesn't advance round (stuck state)
        if (
            cmd.get("action") == "end_turn"
            and state
            and state.get("round") == self._current_state.get("round")
        ):
            # Round didn't advance - check if this is a stuck/crash state
            self._game_alive = False
            self._kill_proc()
            last_obs = self.enc.encode(self._current_state)
            return (
                last_obs,
                -0.5,
                True,
                False,
                {
                    "stuck_round_not_advance": True,
                    "round": self._current_state.get("round", 0),
                },
            )

        # Timeout if player keeps ending turn without progress (no round advance)
        if self._consecutive_end_turn >= self._max_consecutive_end_turn:
            last_obs = self.enc.encode(self._current_state)
            self._game_alive = False
            self._kill_proc()
            return (
                last_obs,
                -0.5,
                True,
                False,
                {"timeout": True, "consecutive_end_turn": self._consecutive_end_turn},
            )

        # Check if player is in dangerous state BEFORE checking for crash
        # This prevents the engine crash when player has playable cards but low HP
        if self._consecutive_end_turn > 0 and self._is_dangerous(self._current_state):
            last_obs = self.enc.encode(self._current_state)
            self._game_alive = False
            self._kill_proc()
            return (
                last_obs,
                -0.5,
                True,
                False,
                {
                    "dangerous_stuck": True,
                    "hp": self._current_state.get("player", {}).get("hp", 0),
                },
            )

        # Detect stuck: end_turn ignored by engine (round/HP unchanged)
        if (
            state
            and state.get("decision") == "combat_play"
            and cmd.get("action") == "end_turn"
            and state.get("round") == self._current_state.get("round")
            and state.get("player", {}).get("hp")
            == self._current_state.get("player", {}).get("hp")
        ):
            # Try proceed to unstick
            for _ in range(5):
                state = self._send({"cmd": "action", "action": "proceed"})
                if state is None or state.get("decision") != "combat_play":
                    break
                if state.get("round") != self._current_state.get("round"):
                    break
            if (
                state
                and state.get("decision") == "combat_play"
                and state.get("round") == self._current_state.get("round")
            ):
                # Still stuck — kill this combat
                last_obs = self.enc.encode(self._current_state)
                self._game_alive = False
                self._kill_proc()
                return last_obs, -0.5, True, False, {"stuck": True}

        if state is None:
            # Check if this was a dangerous crash
            if self._is_dangerous(self._current_state):
                last_obs = self.enc.encode(self._current_state)
                player_hp = (
                    self._current_state.get("player", {}).get("hp", 0)
                    if self._current_state
                    else 0
                )
                return (
                    last_obs,
                    -0.5,
                    True,
                    False,
                    {
                        "dangerous_crash": True,
                        "hp": player_hp,
                    },
                )
            self._game_alive = False
            last_obs = self.enc.encode(self._current_state)
            return last_obs, -0.5, True, False, {"crashed": True}

        decision = state.get("decision", "")
        reward = self._shaping_reward(state)

        # Use last known combat obs for terminal states (NOT zeros — zeros
        # confuse the value function because they're too similar to sparse
        # combat states, causing gradient pollution that collapses entropy)
        last_obs = self.enc.encode(self._current_state)

        if decision == "game_over":
            self._game_alive = False
            return last_obs, reward + self._terminal_reward(state), True, False, {}

        if decision == "combat_play":
            self._current_state = state
            return self.enc.encode(state), reward, False, False, {}

        # Combat ended (transitioned to card_reward, map_select, etc.) — we won
        reward += self._combat_win_reward(state)
        self._current_state = state
        return last_obs, reward, True, False, {"combat_won": True}

    def action_masks(self) -> np.ndarray:
        if self._current_state is None:
            return np.ones(41, dtype=bool)
        return self.enc.action_mask(self._current_state)

    def close(self):
        self._kill_proc()

    def _init_combat_tracking(self, state: dict):
        self._prev_enemy_hp = _total_enemy_hp(state)
        self._prev_player_hp = _player_hp(state)
        self._combat_start_enemy_hp = max(self._prev_enemy_hp, 1)
        self._combat_start_player_max_hp = max(
            state.get("player", {}).get("max_hp", 1), 1
        )

    def _shaping_reward(self, next_state: dict) -> float:
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        dmg_reward = 0.02 * enemy_hp_lost / self._combat_start_enemy_hp
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        hp_penalty = -0.02 * player_hp_lost / self._combat_start_player_max_hp
        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp
        return dmg_reward + hp_penalty

    def _combat_win_reward(self, state: dict) -> float:
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        return 1.0 * (hp / max_hp)

    def _terminal_reward(self, state: dict) -> float:
        if state.get("victory", False):
            return 2.0
        return -0.5

    def _advance_to_combat(self, state: dict) -> dict:
        for _ in range(200):
            if state is None:
                return {
                    "decision": "game_over",
                    "victory": False,
                    "player": {"hp": 0, "max_hp": 80},
                }
            if state.get("decision") in ("combat_play", "game_over"):
                return state
            cmd = greedy_action(state)
            state = self._send(cmd)
        return state or {
            "decision": "game_over",
            "victory": False,
            "player": {"hp": 0, "max_hp": 80},
        }

    def _start_proc(self):
        self._proc = subprocess.Popen(
            [DOTNET, "run", "--no-build", "--project", PROJECT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=PROJECT_ROOT,
        )
        self._read_buf = b""
        self._read_json(timeout_sec=15.0)

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
            return self._read_json(timeout_sec=10.0)
        except Exception:
            self._kill_proc()
            return None


def _dummy_combat_state() -> dict:
    return {
        "decision": "combat_play",
        "energy": 3,
        "round": 1,
        "hand": [
            {
                "index": 0,
                "id": {"en": "STRIKE"},
                "cost": 1,
                "can_play": True,
                "target_type": "AnyEnemy",
                "type": "Attack",
            }
        ],
        "player": {"hp": 80, "max_hp": 80, "block": 0, "buffs": []},
        "enemies": [
            {
                "hp": 30,
                "max_hp": 30,
                "block": 0,
                "intent": {"type": "Attack", "damage": 10, "times": 1},
                "buffs": [],
            }
        ],
    }
