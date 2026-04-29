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


def _score_shop_relic(relic: dict) -> float:
    """Score a shop relic for purchase desirability. Returns 0 if not worth buying."""
    name_raw = relic.get("name") or {}
    name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
    desc_raw = relic.get("description") or {}
    desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
    text = name + " " + desc

    # Hard pass on these
    if "curse" in text: return -5.0
    if "lose max" in text or "maximum hp" in text: return -3.0

    score = 3.0  # baseline: relics are generally useful
    # Big positive: per-turn stat gains
    if "each turn" in text and ("strength" in text or "vigor" in text): score += 4.0
    if "each turn" in text and ("block" in text or "dexterity" in text): score += 3.0
    if "each combat" in text and ("block" in text or "armor" in text): score += 3.0
    if "start of combat" in text and ("strength" in text or "dexterity" in text): score += 3.0
    # Strength bonus (Red Skull type: gives +str) — skip if relic causes str loss
    if "strength" in text and "lose" not in text: score += 2.0
    # Vulnerable multiplier enhancement (Paper Phrog: +25% vs vulnerable)
    if "vulnerable" in text and "enem" not in text: score += 2.0
    # Draw effects
    if "draw" in text and "card" in text: score += 2.0
    # Energy per combat start
    if "energy" in text and "start" in text: score += 3.0
    # Healing
    if "heal" in text or ("rest" in text and "hp" in text): score += 2.0
    # Max HP
    if "max hp" in text and ("gain" in text or "raise" in text or "increase" in text): score += 3.0
    # Gold generation
    if "gold" in text and ("gain" in text or "additional" in text or "drop" in text): score += 1.0
    # Exhaust synergy
    if "exhaust" in text: score += 1.5
    # Potion slots (more potions = more options)
    if "potion" in text and "slot" in text: score += 1.5
    # Card upgrade (powerful long-term)
    if "upgrade" in text and "card" in text: score += 2.0
    # Bad: adds wounds or curses
    if "wound" in text and "add" in text: score -= 2.0
    # Bad: enemies gain strength/buffs (e.g. Philosopher's Stone)
    if "enem" in text and "strength" in text: score -= 3.0
    # Bad: HP costs (e.g. Runic Dome, Sozu)
    if "hp" in text and "lose" in text and "start" in text: score -= 2.0
    return score


def _score_shop_potion(potion: dict) -> float:
    """Score a shop potion for purchase desirability."""
    name_raw = potion.get("name") or {}
    name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
    desc_raw = potion.get("description") or {}
    desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
    text = name + " " + desc

    score = 3.0  # baseline: potions are generally useful
    if "strength" in text: score += 3.0       # Strength/Flex Potion — huge for bosses
    if "dexterity" in text: score += 2.0       # Speed Potion
    if "duplicate" in text: score += 3.0       # Duplication Potion
    if "draw" in text and "card" in text: score += 2.0   # Swift Potion
    if "energy" in text: score += 2.0          # Energy Potion
    if "block" in text: score += 1.5           # Block/Fortifier Potion
    if "vulnerable" in text: score += 2.0      # Weak/Vulnerable applier
    if "all enemies" in text: score += 1.5     # AOE damage
    if "exhaust" in text: score += 1.0         # Elixir
    if "artifact" in text: score += 2.0        # Ancient Potion
    if "heal" in text or "hp" in text: score += 2.0      # Health Potion — saves runs
    if "curse" in text: score -= 5.0           # Potion that adds curses
    return score


def _score_event_option(opt: dict) -> float:
    """Score an event option by keyword analysis. Higher = better."""
    import re as _re
    title = (opt.get("title") or "").lower()
    desc = (opt.get("description") or "").lower()
    # Strip rich-text markup ([gold], [red], [/gold]) to prevent false keyword matches
    # e.g. "[gold]ALL[/gold]" causing "lose all gold" to trigger.
    # Keep template vars like {RandomRelic} since their names carry useful semantics.
    raw = title + " " + desc
    text = _re.sub(r'\[/?[a-z0-9_]+\]', ' ', raw)   # markup tags only
    score = 0.0
    # Strong negatives — losing Max HP is permanent and devastating
    # Covers "lose max hp", "lose 10 max hp", "maximum hp", "lose N max hp"
    if "lose max" in text or "maximum hp" in text or ("lose" in text and "max hp" in text):
        score -= 10.0
    if "curse" in text:
        score -= 8.0
    # "lose ALL gold" — losing all gold is very bad
    if "lose" in text and "all" in text and "gold" in text:
        score -= 5.0
    elif "lose" in text and "gold" in text:
        score -= 2.0
    if "torment" in title:
        score -= 5.0  # Neow's Torment adds a negative card
    if "take" in text and "damage" in text:
        score -= 3.0
    if "downgrade" in text:
        score -= 4.0  # downgrading cards is very bad
    # Negative: adds basic/weak cards to deck
    if "add" in text and ("additional strike" in text or "additional defend" in text):
        score -= 3.0
    # Strong positives
    if "rare" in text and ("card" in text or "obtain" in text or "random" in text):
        score += 8.0
    elif "uncommon" in text and "card" in text:
        score += 4.0  # uncommon card reward: decent, not as good as rare
    elif "card" in text and ("obtain" in text or "choose" in text) and "curse" not in text:
        score += 2.0  # generic card reward (common): better than nothing
    if "remove" in text and ("card" in text or "deck" in text):
        score += 6.0  # deck thinning = very valuable
    if "relic" in text and "add" not in text:
        score += 5.0  # relics without downside
    elif "relic" in text:
        score += 2.0  # relics with some downside (e.g. also adds Strike)
    if "upgrade" in text:
        score += 4.0
    if "transform" in text:
        score += 3.0  # transform replaces bad starters with random cards
    # Use word-boundary search for "gain" to avoid "bargain", "again", "regain" false positives
    _gain = bool(_re.search(r'\bgain\b', text))
    if _gain and "gold" in text:
        score += 3.0
    if "max hp" in text and ("raise" in text or "increase" in text or _gain):
        score += 3.0  # gaining max HP is good
    if "potion" in text:
        score += 2.0
    if "heal" in text and "hp" in text:
        score += 2.0
    if "colorless" in text and "card" in text:
        score += 2.0  # colorless cards add utility
    # Permanent stat gains are very strong
    if "strength" in text and _gain and "enem" not in text and "lose" not in text:
        score += 4.0  # +str permanently is game-warping for Ironclad
    elif "strength" in text and _gain and "enem" in text:
        score -= 3.0  # enemies gaining strength is very bad
    if "dexterity" in text and _gain and "enem" not in text and "lose" not in text:
        score += 3.0  # +dex permanently is strong defense
    if "energy" in text and "each turn" in text and "lose" not in text:
        score += 5.0  # extra energy per turn = unlimited scaling
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
            deck_size = state.get("player", {}).get("deck_size", 10)
            # Raise pick threshold as deck grows: keep deck lean
            if deck_size >= 20:
                threshold = 6.5
            elif deck_size >= 15:
                threshold = 5.5
            else:
                threshold = 3.5
            best = pick_best_card(cards, threshold=threshold)
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
            room_type = state.get("context", {}).get("room_type", "")
            max_sel = max(state.get("max_select", 1), 1)
            combat_rooms = ("RestSiteRoom", "Boss", "Monster", "Elite", "CombatRoom")
            if "rest" in room_type.lower():
                # SMITH upgrade: always single-card selection, pick best
                best = pick_best_card(cards, threshold=0.0)
                idx = best if best is not None else 0
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            elif room_type in ("Boss", "Monster", "Elite", "CombatRoom") or not room_type:
                # Mid-combat select (potion: pick best; boss mechanic: rare, pick best as heuristic)
                best = pick_best_card(cards, threshold=0.0)
                idx = best if best is not None else 0
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            elif len(cards) <= 5 and max_sel == 1:
                # Small pool (2-5 cards): likely "choose 1 to ADD/discover" — pick best
                best = pick_best_card(cards, threshold=0.0)
                idx = best if best is not None else 0
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            else:
                # Larger pool (likely full deck): remove/transform — pick worst card(s)
                scored = sorted(enumerate(cards), key=lambda x: score_card(x[1]))
                selected = [str(scored[k][0]) for k in range(min(max_sel, len(scored)))]
                return {"cmd": "action", "action": "select_cards",
                        "args": {"indices": ",".join(selected)}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        removal_cost = state.get("card_removal_cost")
        # Find best affordable card
        cards = [c for c in state.get("cards", [])
                 if c.get("is_stocked") and c.get("cost", 999) <= gold]
        best_card = max(cards, key=lambda c: score_card(c)) if cards else None
        best_score = score_card(best_card) if best_card else 0.0
        # Buy elite cards first (score ≥ 8.5) before spending gold on removal
        if best_card and best_score >= 8.5:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Remove a card if affordable (deck thinning is very valuable)
        if removal_cost and gold >= removal_cost:
            return {"cmd": "action", "action": "remove_card"}
        # Buy good card (score ≥ 6.0) after removal opportunity checked
        if best_card and best_score >= 6.0:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Buy a relic if score is high enough and affordable (threshold: keep 50g buffer)
        RELIC_GOLD_THRESHOLD = 50
        relics = [r for r in state.get("relics", [])
                  if r.get("is_stocked") and r.get("cost", 999) <= gold - RELIC_GOLD_THRESHOLD]
        if relics:
            best_relic = max(relics, key=_score_shop_relic)
            if _score_shop_relic(best_relic) >= 5.0:
                return {"cmd": "action", "action": "buy_relic",
                        "args": {"relic_index": best_relic.get("index", 0)}}
        # Buy a potion if we have empty slots and it's affordable
        held_potions = len(state.get("player", {}).get("potions", []))
        if held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            if shop_potions:
                best_potion = max(shop_potions, key=_score_shop_potion)
                if _score_shop_potion(best_potion) >= 5.0:
                    return {"cmd": "action", "action": "buy_potion",
                            "args": {"potion_index": best_potion.get("index", 0)}}
        return {"cmd": "action", "action": "leave_room"}

    return {"cmd": "action", "action": "proceed"}


def _total_enemy_hp(state: dict) -> int:
    return sum(e.get("hp", 0) for e in state.get("enemies", []))


def _player_hp(state: dict) -> int:
    return state.get("player", {}).get("hp", 0)


def _enemy_power_amount(enemy: dict, power_name: str) -> float:
    """Return the amount of a named power on an enemy (0.0 if not present)."""
    for p in (enemy.get("powers") or []):
        pname = p.get("name", {})
        if isinstance(pname, dict):
            pname = pname.get("en", "")
        if str(pname).lower() == power_name.lower():
            return float(p.get("amount", 1))
    return 0.0


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
                 seed_prefix: str = "t", max_floor: int = 0, extra_obs: bool = True):
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

        # Extra features appended after enc.obs_size:
        #   [floor/17, entry_hp_ratio, e0_vuln, e0_weak, e1_vuln, e1_weak, e2_vuln, e2_weak]
        # extra_obs=False: legacy mode for checkpoints trained with 161-dim obs
        self._EXTRA_OBS = 8 if extra_obs else 0
        self.observation_space = Box(low=0.0, high=1.0,
                                     shape=(self.enc.obs_size + self._EXTRA_OBS,), dtype=np.float32)
        self.action_space = Discrete(41)

        self._proc = None
        self._current_state = None
        self._run_counter = 0
        self._prev_enemy_hp = 0
        self._prev_player_hp = 0
        self._combat_start_enemy_hp = 1
        self._combat_start_player_max_hp = 1
        self._combat_entry_hp_ratio = 1.0  # HP ratio when combat started
        self._current_floor = 1
        self._game_alive = False
        self._read_buf = b""
        self._combat_steps = 0
        self.max_combat_steps = 200  # floor 4+ fights can take 100+ steps with a learning policy

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self._encode(self._current_state), {}

        # Try to advance to next combat in the current run
        if self._game_alive and self._current_state is not None:
            cur_floor = (self._current_state.get("floor")
                         or self._current_state.get("context", {}).get("floor", 0))
            if self.max_floor > 0 and isinstance(cur_floor, int) and cur_floor >= self.max_floor:
                # Curriculum: restart to keep fighting easy enemies
                self._game_alive = False
                self._kill_proc()
            else:
                state = self._advance_to_combat(self._current_state)
                if state and state.get("decision") == "combat_play":
                    self._init_combat_tracking(state)
                    self._current_state = state
                    return self._encode(state), {}
                # Advance failed — game ended (natural game_over or crash).
                # Signal game_over via info instead of silently restarting:
                # eval_rl.py checks info["game_over"] to end the eval game correctly.
                self._game_alive = False
                self._kill_proc()
                self._current_state = _dummy_combat_state()
                crashed = (state is None or state.get("decision") == "stuck")
                return self._encode(self._current_state), {
                    "game_over": True,
                    "crashed": crashed,
                    "victory": bool(state and state.get("victory", False)),
                }

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
            return self._encode(self._current_state), {}

        self._game_alive = True
        state = self._advance_to_combat(state)
        if state is None or state.get("decision") != "combat_play":
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            return self._encode(self._current_state), {}

        self._init_combat_tracking(state)
        self._current_state = state
        self._combat_steps = 0
        return self._encode(state), {}

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size + self._EXTRA_OBS, dtype=np.float32), -2.0, True, False, {}

        self._combat_steps += 1
        if self._combat_steps > self.max_combat_steps:
            # Combat too long — treat as defeat to avoid wasting time
            last_obs = self._encode(self._current_state)
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
                last_obs = self._encode(self._current_state)
                self._game_alive = False
                self._kill_proc()
                return last_obs, -2.0, True, False, {"stuck": True}

        if state is None:
            self._game_alive = False
            last_obs = self._encode(self._current_state)
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
            last_obs = self._encode(self._current_state)
            return last_obs, -2.0, True, False, {"crashed": True, "floor": self._current_floor}

        decision = state.get("decision", "")
        reward = self._shaping_reward(state)

        # Use last known combat obs for terminal states (NOT zeros — zeros
        # confuse the value function because they're too similar to sparse
        # combat states, causing gradient pollution that collapses entropy)
        last_obs = self._encode(self._current_state)

        if decision == "game_over":
            self._game_alive = False
            r = reward + self._terminal_reward(state)
            if state.get("victory", False):
                # Boss kill: also award combat_win_reward so victory > regular combat win
                r += self._combat_win_reward(state)
            return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                               "victory": state.get("victory", False)}

        if decision == "combat_play":
            state = self._combat_check_heal(state)  # reactive heal if HP critical mid-fight
            if state.get("decision") == "game_over":
                self._game_alive = False
                r = reward + self._terminal_reward(state)
                if state.get("victory", False):
                    r += self._combat_win_reward(state)
                return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                                   "victory": state.get("victory", False)}
            self._current_state = state
            return self._encode(state), reward, False, False, {}

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
                    return self._encode(state), reward, False, False, {}
                if state.get("decision") == "game_over":
                    self._game_alive = False
                    r = reward + self._terminal_reward(state)
                    if state.get("victory", False):
                        r += self._combat_win_reward(state)
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

    def _encode(self, state: dict) -> np.ndarray:
        """Encode state + optional extra features: floor, entry_hp, enemy vuln/weak × 3."""
        base = self.enc.encode(state)
        if self._EXTRA_OBS == 0:
            return base
        floor_norm = min(self._current_floor / 17.0, 1.0)
        enemies = state.get("enemies", [])
        extra = [floor_norm, self._combat_entry_hp_ratio]
        for slot in range(3):
            e = enemies[slot] if slot < len(enemies) else {}
            extra.append(min(_enemy_power_amount(e, "Vulnerable") / 10.0, 1.0))
            extra.append(min(_enemy_power_amount(e, "Weak") / 10.0, 1.0))
        return np.concatenate([base, np.array(extra, dtype=np.float32)])

    def _init_combat_tracking(self, state: dict):
        self._prev_enemy_hp = _total_enemy_hp(state)
        self._prev_player_hp = _player_hp(state)
        self._combat_start_enemy_hp = max(self._prev_enemy_hp, 1)
        self._combat_start_player_max_hp = max(state.get("player", {}).get("max_hp", 1), 1)
        floor = state.get("floor") or state.get("context", {}).get("floor", 1)
        self._current_floor = int(floor) if isinstance(floor, (int, float)) and floor > 0 else 1
        hp = state.get("player", {}).get("hp", self._combat_start_player_max_hp)
        self._combat_entry_hp_ratio = hp / self._combat_start_player_max_hp

    def _shaping_reward(self, next_state: dict) -> float:
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        dmg_reward = 0.15 * enemy_hp_lost / self._combat_start_enemy_hp
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        # Increased from -0.35: stronger HP conservation drives cumulative HP health across floors
        hp_penalty = -0.50 * player_hp_lost / self._combat_start_player_max_hp

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
            block_reward = 0.15 * effective_block / self._combat_start_player_max_hp

        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp

        # No step penalty — avoids incentivizing fast death spiral
        step_penalty = 0.0
        return dmg_reward + hp_penalty + block_reward + step_penalty

    def _combat_win_reward(self, state: dict) -> float:
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        hp_ratio = hp / max_hp
        # Steeper quadratic HP curve: winning with high HP is worth much more
        # e.g. 100% HP → 3.0, 70% HP → 1.47, 50% HP → 0.75, 30% HP → 0.27
        reward = 3.0 * hp_ratio * hp_ratio
        # HP survival bonus tiers (stronger than before)
        if hp_ratio >= 0.9:
            reward += 0.75
        elif hp_ratio >= 0.8:
            reward += 0.50
        elif hp_ratio >= 0.7:
            reward += 0.25
        # Floor bonus: 0.10/floor (up from 0.05), cap 1.5 (up from 0.8)
        # Incentivizes reaching higher floors without overwhelming HP signal
        floor_bonus = min((self._current_floor - 1) * 0.10, 1.5)
        reward += floor_bonus
        return reward

    def _terminal_reward(self, state: dict) -> float:
        if state.get("victory", False):
            return 2.0
        return -2.0

    def _combat_check_heal(self, state: dict) -> dict:
        """Reactively use heal potions during combat when HP drops critically low.

        Called on every combat_play state so we can heal mid-fight if HP tanks.
        Only uses heal/restore potions; all other potions are handled at combat start.
        """
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        if hp_ratio >= 0.40:
            return state  # healthy enough, no heal needed
        potions = player.get("potions", []) or []
        for p in potions:
            name_raw = p.get("name") or {}
            name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
            desc_raw = p.get("description") or {}
            desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
            text = name + " " + desc
            if ("heal" in text or "restore" in text) and "curse" not in text:
                pidx = p.get("index", 0)
                target_type = (p.get("target_type") or "").lower()
                args: dict = {"potion_index": pidx}
                if target_type == "anyenemy":
                    args["target_index"] = 0
                new_state = self._send({"cmd": "action", "action": "use_potion", "args": args})
                if new_state is None:
                    break
                state = new_state
                if state.get("decision") not in ("combat_play",):
                    break
                # Re-check HP after heal — might need another heal potion
                p2 = state.get("player", {})
                hp_ratio = p2.get("hp", 80) / max(p2.get("max_hp", 80), 1)
                if hp_ratio >= 0.35:
                    break
                potions = p2.get("potions", []) or []
        return state

    def _greedy_use_potions(self, state: dict) -> dict:
        """Auto-use potions before RL policy acts (RL action space has no potion actions).

        Strategy: use strength/flex/duplication at boss/elite fights; block if low HP.
        Fire/explosive potions target enemy 0 and are used at elite+ fights.
        """
        room_type = (state.get("context") or {}).get("room_type", "")
        is_boss  = "boss" in room_type.lower()
        is_elite = "elite" in room_type.lower()
        is_tough = is_boss or is_elite

        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        potions = player.get("potions", []) or []

        for p in potions:
            name_raw = p.get("name") or {}
            name = (name_raw.get("en", "") if isinstance(name_raw, dict) else str(name_raw)).lower()
            desc_raw = p.get("description") or {}
            desc = (desc_raw.get("en", "") if isinstance(desc_raw, dict) else str(desc_raw)).lower()
            text = name + " " + desc
            target_type = (p.get("target_type") or "").lower()
            pidx = p.get("index", 0)

            use = False
            target_index = None
            # Pre-compute incoming attack damage for threat-aware decisions
            incoming_dmg = sum(
                it.get("damage", 0) * (it.get("hits") or 1)
                for e in state.get("enemies", [])
                for it in (e.get("intents") or [])
                if it.get("type", "").lower() == "attack"
            )
            hp_cur = player.get("hp", 80)
            blk_cur = player.get("block", 0)
            # Damage that bypasses current block (what we'll actually take)
            unblocked_dmg = max(0, incoming_dmg - blk_cur)

            if ("heal" in text or "restore" in text) and "curse" not in text:
                # Heal: any fight at 30%, elite/boss at 50%
                use = hp_ratio < 0.30 or (is_tough and hp_ratio < 0.50)
            elif "block" in text:
                # Block potion: use when incoming damage is threatening regardless of fight type
                # - Incoming > 50% of remaining HP (will take a serious hit)
                # - OR standard HP-ratio threshold at elite/boss
                threatening = incoming_dmg > 0 and unblocked_dmg >= hp_cur * 0.45
                use = threatening or (is_tough and hp_ratio < (0.70 if is_boss else 0.60))
            elif not is_tough:
                continue  # other potions: save for elite/boss
            elif "strength" in text or "flex" in text:
                use = True  # always use strength/flex at elite/boss
            elif "dexterity" in text:
                use = True  # dexterity potion at elite/boss
            elif "energy" in text and "channel" not in text:
                use = is_tough  # energy potion: useful burst at both elite and boss
                target_index = 0 if target_type == "anyenemy" else None
            elif "duplicat" in text:
                # duplicator/duplication: boss always; elite if damaged (doubles best card = big swing)
                use = is_boss or (is_elite and hp_ratio < 0.60)
            elif "blessing" in text or "forge" in text:
                use = is_tough  # upgrade hand at elite/boss
            elif "fire" in text or "explosive" in text:
                use = is_tough  # damage potions at elite and boss
                target_index = 0
            elif "attack" in text:
                # attack potion: always at elite/boss — killing faster = less damage taken
                use = is_tough
                target_index = 0 if target_type == "anyenemy" else None
            elif "weak" in text or "fear" in text or "vulnerable" in text:
                use = is_tough  # fear/weak potions apply debuffs — great at elite/boss
                target_index = 0 if target_type == "anyenemy" else None
            elif "power" in text or "ancient" in text:
                use = is_boss  # power/ancient potion: save for boss
            elif "speed" in text:
                use = is_tough  # speed potion: dex bonus at elite/boss

            if not use:
                continue

            args: dict = {"potion_index": pidx}
            if target_type == "anyenemy" or target_index is not None:
                args["target_index"] = target_index or 0
            new_state = self._send({"cmd": "action", "action": "use_potion", "args": args})
            if new_state is None:
                break
            # Resolve card_select (Attack Potion picks a card) or proceed
            for _ in range(10):
                if new_state.get("decision") == "card_select":
                    new_state = self._send(greedy_action(new_state))
                elif new_state.get("decision") in ("combat_play", "game_over"):
                    break
                else:
                    break
                if new_state is None:
                    break
            if new_state is None or new_state.get("decision") == "game_over":
                return new_state or state
            state = new_state
            # Refresh potion list from updated state
            potions = (state.get("player") or {}).get("potions", []) or []

        return state

    def _advance_to_combat(self, state: dict) -> dict:
        for _ in range(200):
            if state is None:
                return {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}
            if state.get("decision") == "game_over":
                return state
            if state.get("decision") == "combat_play":
                return self._greedy_use_potions(state)
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
