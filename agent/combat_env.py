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
from agent.card_scoring import (score_card, score_card_in_deck, pick_best_card,
                                  pick_worst_card, deck_quality_score, _card_id_norm)

# Swappable map strategy — change globally via set_map_strategy()
_map_strategy: MapStrategy = HpAwareMapStrategy()


def set_map_strategy(strategy: MapStrategy):
    """Replace the global map strategy. Call before training or evaluation."""
    global _map_strategy
    _map_strategy = strategy

def _find_dotnet():
    """Return a command prefix list for .NET SDK. On Apple Silicon, prefers ARM64 dotnet."""
    import platform
    candidates = [
        os.path.expanduser("~/.dotnet-arm64/dotnet"),
        os.path.expanduser("~/.dotnet/dotnet"),
        "/usr/local/share/dotnet/dotnet",
        "dotnet",
    ]
    for p in candidates:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return [p]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    # On macOS ARM64, try arch -arm64 dotnet to load ARM64 managed assemblies
    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["arch", "-arm64", "dotnet", "--version"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return ["arch", "-arm64", "dotnet"]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return ["dotnet"]

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
    # "lose max hp" directly, OR "lose {N} max hp" (lose N max hp), but NOT
    # "lose {potion}. gain ... max hp" (which is gaining max hp after losing a potion)
    _lose_max_hp = ("lose max" in text or "maximum hp" in text or
                    bool(_re.search(r'lose\s+(?:\{[^}]+\}|\d+)\s+max\s*hp', text)))
    if _lose_max_hp:
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
        score -= 2.0  # one-time HP loss is recoverable; was -3 (too harsh for HP→MaxHP trades)
    if "lose" in text and "hp" in text and "max" not in text:
        score -= 2.0  # "Lose N HP" phrasing (same cost as take damage)
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
    # Removing cards at HP cost (e.g. Precarious Shears): net value reduced vs free removal
    if "remove" in text and ("card" in text or "deck" in text) and "hp" in text and "curse" not in text:
        score -= 3.0
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
    elif "energy" in text and _gain and "lose" not in text:
        score += 1.5  # one-time energy gain: small but real value
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
            deck = state.get("player", {}).get("deck") or []
            deck_size = state.get("player", {}).get("deck_size", len(deck) or 10)
            floor = state.get("floor") or state.get("context", {}).get("floor", 1)
            in_act2 = isinstance(floor, int) and floor >= 16
            if in_act2:
                threshold = 5.5 if deck_size < 18 else 6.0
            elif deck_size >= 18:
                threshold = 6.5
            else:
                threshold = 5.5
            # Provide game-state context for MC rollout (no-op when STS2_MC_ROLLOUT
            # is off — the v2 predictor path doesn't read it).
            from agent.card_scoring import set_mc_context as _set_mc_ctx
            player = state.get("player", {}) or {}
            _set_mc_ctx(
                hp=int(player.get("hp", 80) or 80),
                max_hp=int(player.get("max_hp", 80) or 80),
                floor=int(floor) if isinstance(floor, (int, float)) and floor > 0 else 5,
            )
            # Pass deck for synergy-aware picks (boosts cards that fit the archetype).
            best = pick_best_card(cards, threshold=threshold, deck=deck)
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
            else:
                # Distinguish "add N from external pool" vs "remove/transform from deck"
                # External event pools (discover/cheese) never contain Strikes/Defends.
                # Deck selections (remove/transform) always include the player's junk cards.
                has_junk = any(score_card(c) < 3.0 for c in cards)
                is_deck_selection = has_junk or len(cards) > 10
                if not is_deck_selection:
                    # External pool (no junk, small-ish): event "add N cards" — pick best N
                    scored_by = sorted(enumerate(cards), key=lambda x: score_card(x[1]), reverse=True)
                else:
                    # Deck selection (remove/transform): pick worst N
                    scored_by = sorted(enumerate(cards), key=lambda x: score_card(x[1]))
                selected = [str(scored_by[k][0]) for k in range(min(max_sel, len(scored_by)))]
                return {"cmd": "action", "action": "select_cards",
                        "args": {"indices": ",".join(selected)}}
        return {"cmd": "action", "action": "skip_select"}

    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        held_potions = len(player.get("potions", []))
        floor = state.get("floor") or state.get("context", {}).get("floor", 0)
        removal_cost = state.get("card_removal_cost")
        pre_boss = isinstance(floor, int) and floor >= 11

        # Emergency: buy health potion first when HP is critically low (< 50%)
        # Without this, the agent spends all gold on cards/relics and enters next fight
        # with low HP and no heal potion.
        if hp_ratio < 0.50 and held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            for sp in shop_potions:
                sp_name = (sp.get("name") or {})
                sp_name = (sp_name.get("en", "") if isinstance(sp_name, dict) else str(sp_name)).lower()
                sp_desc = (sp.get("description") or {})
                sp_desc = (sp_desc.get("en", "") if isinstance(sp_desc, dict) else str(sp_desc)).lower()
                if ("heal" in sp_name + sp_desc or "restore" in sp_name + sp_desc) and "curse" not in sp_name + sp_desc:
                    return {"cmd": "action", "action": "buy_potion",
                            "args": {"potion_index": sp.get("index", 0)}}

        in_act2 = isinstance(floor, int) and floor >= 16
        deck = state.get("player", {}).get("deck", []) or []

        # === REMOVAL FIRST when deck has Strike/Defend basics ===
        # Aggressive change (2026-05-07): in Act 1, deck thinning is the highest-leverage
        # gold spend — a removed Strike compounds across every shuffle, vs a single card
        # buy. Always remove when affordable and there's any Strike/Defend in the deck.
        n_strikes = sum(1 for c in deck if "STRIKE" in _card_id_norm(c) and "STRIKE_DUMMY" not in _card_id_norm(c))
        n_defends = sum(1 for c in deck if "DEFEND" in _card_id_norm(c))
        has_basic = (n_strikes + n_defends) >= 2
        has_junk = any(score_card(c) < 3.5 for c in deck)
        if removal_cost and gold >= removal_cost and (has_basic or has_junk):
            return {"cmd": "action", "action": "remove_card"}

        # Find best affordable card — deck-aware so synergy cards rank higher.
        cards_avail = [c for c in state.get("cards", [])
                       if c.get("is_stocked") and c.get("cost", 999) <= gold]
        best_card = max(cards_avail, key=lambda c: score_card_in_deck(c, deck)) if cards_avail else None
        best_score = score_card_in_deck(best_card, deck) if best_card else 0.0
        # Buy elite cards first (score ≥ 8.0 with synergy)
        if best_card and best_score >= 8.0:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Lower thresholds across the board (more aggressive shop usage)
        if in_act2:
            card_buy_threshold = 4.5
        elif pre_boss:
            card_buy_threshold = 5.0
        else:
            card_buy_threshold = 5.5
        if best_card and best_score >= card_buy_threshold:
            return {"cmd": "action", "action": "buy_card",
                    "args": {"card_index": best_card.get("index", 0)}}
        # Buy a relic — smaller buffer (25g vs 50g), lower threshold (4.0 vs 5.0).
        RELIC_GOLD_THRESHOLD = 25
        relics = [r for r in state.get("relics", [])
                  if r.get("is_stocked") and r.get("cost", 999) <= gold - RELIC_GOLD_THRESHOLD]
        if relics:
            best_relic = max(relics, key=_score_shop_relic)
            if _score_shop_relic(best_relic) >= 4.0:
                return {"cmd": "action", "action": "buy_relic",
                        "args": {"relic_index": best_relic.get("index", 0)}}
        # Buy a potion if we have empty slots and it's affordable
        if held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            if shop_potions:
                best_potion = max(shop_potions, key=_score_shop_potion)
                if _score_shop_potion(best_potion) >= 4.5:
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


# Extra reward paid once per Boss-room combat win, on top of _combat_win_reward.
# Boss combats at floor 17/Act 1 / Act 2+ are the chokepoint where training stalled
# (0% win rate across 2.3M steps). +10.0 dominates the per-step hp_penalty/floor_bonus
# scale and pushes the policy to actually clear the fight, not just survive nearby.
BOSS_CLEAR_BONUS = 10.0

# One-shot reward when entering a Boss combat with HP > BOSS_ENTRY_HP_FLOOR,
# paid REGARDLESS of combat outcome. Decouples "deliver HP to boss" from
# "clear boss". HP-sweep diagnostic (May 25) showed boss-policy CAN win at
# hp≥80 (27%/53%/77% at 80/100/120), so the bottleneck is Acts 1-2 leakage
# leaving the player at hp 44-68 — squarely in the 0%-win dead zone. Reward
# every extra HP delivered above the dead zone so the policy explicitly
# learns "save HP for the boss room".
BOSS_ENTRY_HP_FLOOR = 50.0    # below this, no bonus (already in the 0% dead zone)
BOSS_ENTRY_HP_WEIGHT = 0.2    # bonus = (hp - floor) × weight. hp=100 → +10, hp=80 → +6


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
                 seed_prefix: str = "t", max_floor: int = 0, extra_obs: bool = True,
                 replay_actions: list = None, native_save_path: str = None,
                 set_hp_after_load: int = None):
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
        self._current_combat_room_type = ""  # captured at combat start; used by boss-clear bonus
        self._pending_boss_entry_reward = 0.0  # one-shot HP-at-boss-entry bonus, paid on first step
        self._current_floor = 1
        self._game_alive = False
        self._read_buf = b""
        self._combat_steps = 0
        self._dealt_damage_this_turn = False  # tracked for intent-block anti-stall gate
        self.max_combat_steps = 1000  # 200→1000 (May 9): boss/elite fights legitimately take
                                       # 300-600 steps; 200 cap was creating fake "to=89%" timeouts
                                       # mid-fight, which corrupted PPO advantage estimation.
        # Run-level deck-quality milestones (paid once per crossing per game).
        # Cleared when a fresh run starts (in reset's start_run branch).
        self._milestones_paid: set = set()
        # Deck-history JSONL — milestone snapshots + outcome rows for the learned
        # deck predictor (see agent/train_deck_predictor.py). Set DECK_HISTORY=
        # in environment to enable; empty disables recording.
        self._deck_history_path = os.environ.get("DECK_HISTORY_PATH",
            os.path.join(PROJECT_ROOT, "data", "deck_history.jsonl"))
        # Per-run state for the predictor: max floor seen, milestones captured
        self._run_max_floor = 1
        self._run_id = None
        self._run_milestone_records: list = []  # buffered rows until outcome known
        self._run_card_pick_records: list = []   # buffered card-reward decisions (per pick, see _buffer_card_pick)

        self._replay_actions = list(replay_actions) if replay_actions else []
        self._replay_pending = bool(self._replay_actions)
        # native_save_path can be a string (fixed save) or a list[str] (snapshot pool —
        # picked uniformly at random on each reset, so a vec-env spreads over the pool).
        if isinstance(native_save_path, (list, tuple)):
            self._save_pool = list(native_save_path)
            self._native_save_path = self._save_pool[0] if self._save_pool else None
        else:
            self._save_pool = None
            self._native_save_path = native_save_path
        # When set, send {"cmd": "set_player", "hp": N} right after load_save so the
        # subsequent combat starts at N HP. Used by boss_retry.py to sweep "how much
        # HP does the agent need to clear the boss".
        self._set_hp_after_load = (None if set_hp_after_load is None
                                   else int(set_hp_after_load))

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)

        if self.dry_run:
            self._current_state = _dummy_combat_state()
            return self._encode(self._current_state), {}

        # Snapshot pool: pick a random save per reset and force a fresh load
        # below (don't try to continue the previous run — we want fresh boss
        # variety each episode).
        if self._save_pool:
            self._native_save_path = random.choice(self._save_pool)
            self._game_alive = False

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
                self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
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
        self._milestones_paid.clear()  # new run — re-arm deck-quality milestones
        self._run_max_floor = 1
        self._run_id = f"r{int(time.time()*1000) % 10**9:09d}_{random.randint(0, 9999):04d}"
        self._run_milestone_records = []
        self._run_card_pick_records = []
        if self._native_save_path:
            state = self._send({"cmd": "load_save",
                                "path": self._native_save_path, "lang": "en"})
            if state is not None and state.get("type") != "error" and self._set_hp_after_load is not None:
                self._send({"cmd": "set_player", "hp": self._set_hp_after_load})
        else:
            state = self._send({"cmd": "start_run", "character": self.character,
                                "seed": run_seed, "ascension": self.ascension})
        if state is None or state.get("type") == "error":
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
            return self._encode(self._current_state), {
                "load_failed": bool(self._native_save_path),
                "message": (state or {}).get("message", "") if state else "",
            }

        self._game_alive = True

        # Replay any saved actions (one-shot, on first reset only)
        if self._replay_pending:
            for cmd in self._replay_actions:
                state = self._send(cmd)
                if state is None:
                    self._game_alive = False
                    self._current_state = _dummy_combat_state()
                    self._init_combat_tracking(self._current_state)
                    return self._encode(self._current_state), {"replay_failed": True}
                if state.get("decision") == "game_over":
                    self._game_alive = False
                    self._current_state = _dummy_combat_state()
                    self._init_combat_tracking(self._current_state)
                    return self._encode(self._current_state), {
                        "game_over": True,
                        "victory": bool(state.get("victory", False)),
                        "from_replay": True,
                    }
            self._replay_pending = False

        state = self._advance_to_combat(state)
        if state is None or state.get("decision") != "combat_play":
            self._game_alive = False
            self._current_state = _dummy_combat_state()
            self._init_combat_tracking(self._current_state)  # prevent stale max_hp=1
            return self._encode(self._current_state), {}

        self._init_combat_tracking(state)
        self._current_state = state
        self._combat_steps = 0
        return self._encode(state), {}

    def step(self, action: int):
        if self.dry_run or self._current_state is None:
            return np.zeros(self.enc.obs_size + self._EXTRA_OBS, dtype=np.float32), -2.0, True, False, {}

        # Detect dead process (e.g. potion crash in _greedy_use_potions during reset)
        if not self._game_alive:
            return (self._encode(self._current_state), -2.0, True, False,
                    {"crashed": True, "floor": self._current_floor})

        self._combat_steps += 1
        if self._combat_steps > self.max_combat_steps:
            # Combat too long — treat as defeat to avoid wasting time
            last_obs = self._encode(self._current_state)
            return last_obs, -2.0, True, False, {"timeout": True}

        cmd = self.enc.decode(int(action), self._current_state)
        self._last_cmd = cmd
        # Capture pre-action state so end_turn intent-aware shaping can read
        # the block/intents the agent committed to before the enemy turn fires.
        pre_state = self._current_state
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

            # Killing-blow crash: the last enemy died (from play_card or from poison/power
            # during end_turn), and C# crashed in DetectPostCombatState post-combat cleanup.
            # The combat WAS won. Give combat_win_reward instead of -2.0 crash penalty.
            cmd = getattr(self, "_last_cmd", {})
            action = cmd.get("action", "") if isinstance(cmd, dict) else ""
            is_killing_blow = (
                n_enemies == 1 and (
                    action == "play_card" or action == "end_turn"
                )
            )
            if is_killing_blow:
                # Combat won (last enemy killed), but C# crashed in post-combat cleanup.
                # Give full win reward — BUG-031 fix makes this very rare, so no exploitation risk.
                # _game_alive must be False (already set above) so reset() starts a fresh game.
                reward = self._combat_win_reward(self._current_state)
                print(f"\n[CRASH→WIN] floor={floor} reward={reward:.2f}",
                      file=sys.stderr, flush=True)
                return last_obs, reward, True, False, {
                    "floor": floor, "combat_won": True,
                }

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
        if self._current_floor > self._run_max_floor:
            self._run_max_floor = self._current_floor
        reward = self._shaping_reward(state)

        # B5: pay HP-at-boss-entry bonus on the first step of a Boss combat.
        # Set by _init_combat_tracking when room_type=="Boss" and hp>floor;
        # cleared after paying so it only fires once per combat.
        if self._pending_boss_entry_reward > 0:
            reward += self._pending_boss_entry_reward
            self._pending_boss_entry_reward = 0.0

        # Track per-turn damage dealt (used by intent_block_reward gating below)
        if state is not None and cmd.get("action") == "play_card":
            cur_enemy_hp = _total_enemy_hp(state)
            prev_enemy_hp = _total_enemy_hp(pre_state) if pre_state else cur_enemy_hp
            if cur_enemy_hp < prev_enemy_hp:
                self._dealt_damage_this_turn = True
            # Q3b power-safe-turn bonus DISABLED 2026-05-19 — small positive
            # per-card reward accumulates over 1M steps into stall-favoring drift.

        # All end_turn reward shaping (Q3a intent-block, Q3c wasted-energy)
        # DISABLED 2026-05-19 — all attempts to nudge turn-level behavior caused
        # drift collapse. The base reward signal (hp_penalty + combat_win_reward
        # + step_penalty) is sufficient. Reset damage tracker for next turn.
        if cmd.get("action") == "end_turn":
            self._dealt_damage_this_turn = False

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
            self._run_max_floor = max(self._run_max_floor, self._current_floor)
            self._emit_run_outcome(state, bool(state.get("victory", False)))
            return last_obs, r, True, False, {"floor": self._current_floor, "game_over": True,
                                               "victory": state.get("victory", False)}

        if decision == "combat_play":
            state = self._combat_check_heal(state)  # reactive heal if HP critical mid-fight
            if state.get("decision") == "game_over":
                self._game_alive = False
                r = reward + self._terminal_reward(state)
                if state.get("victory", False):
                    r += self._combat_win_reward(state)
                self._run_max_floor = max(self._run_max_floor, self._current_floor)
                self._emit_run_outcome(state, bool(state.get("victory", False)))
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
                    self._run_max_floor = max(self._run_max_floor, self._current_floor)
                    self._emit_run_outcome(state, bool(state.get("victory", False)))
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

    def set_hp_after_load(self, hp: int) -> None:
        """Update the post-load HP override at runtime. 0 or negative disables."""
        self._set_hp_after_load = (None if (hp is None or hp <= 0) else int(hp))

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
        self._dealt_damage_this_turn = False  # fresh combat starts with no damage logged
        # Capture room_type at combat start: by the time _combat_win_reward fires,
        # the state may have transitioned to card_reward and room_type is stale.
        room_type = (state.get("context") or {}).get("room_type", "")
        self._current_combat_room_type = str(room_type)
        # B5 plan (May 25): pay HP-at-boss-entry bonus on the FIRST step of a Boss
        # combat (paid regardless of outcome — see BOSS_ENTRY_HP_* constants).
        if self._current_combat_room_type == "Boss" and hp > BOSS_ENTRY_HP_FLOOR:
            self._pending_boss_entry_reward = (hp - BOSS_ENTRY_HP_FLOOR) * BOSS_ENTRY_HP_WEIGHT
        else:
            self._pending_boss_entry_reward = 0.0

    def _shaping_reward(self, next_state: dict) -> float:
        cur_enemy_hp = _total_enemy_hp(next_state)
        cur_player_hp = _player_hp(next_state)
        enemy_hp_lost = max(self._prev_enemy_hp - cur_enemy_hp, 0)
        # Base damage reward = 0.15 × frac of starting enemy HP. At Act 1 boss
        # (floor 17+) we add an extra 0.10 so each fraction of boss HP burned
        # gives stronger signal — boss combats are long, every chunk matters,
        # and 0% win rate means policy needs more "got close" signal.
        dmg_reward = 0.15 * enemy_hp_lost / self._combat_start_enemy_hp
        if self._current_floor >= 17:
            dmg_reward += 0.10 * enemy_hp_lost / self._combat_start_enemy_hp
        player_hp_lost = max(self._prev_player_hp - cur_player_hp, 0)
        hp_penalty = -0.50 * player_hp_lost / self._combat_start_player_max_hp

        self._prev_enemy_hp = cur_enemy_hp
        self._prev_player_hp = cur_player_hp

        # Step penalty: discourages stalling / timeout (200 steps → -0.60).
        # block_reward removed: it made pure-blocking per-step positive (0.019 > 0.003 step_penalty),
        # causing policy collapse into "block forever, never attack" → 60%+ timeout rate.
        # Blocking is still incentivized implicitly by hp_penalty (blocking prevents damage).
        step_penalty = -0.003
        return dmg_reward + hp_penalty + step_penalty

    def _intent_block_reward(self, pre_state: dict, dealt_damage_this_turn: bool) -> float:
        """Reward block matched to incoming attack damage at end_turn — but only
        if the agent ALSO dealt damage this turn (anti-stall) AND blocked at
        least 80% of incoming (anti-half-block-spam).

        History: prior versions collapsed at cwr<20%. Two new gates added (May 14):
          1. dealt_damage_this_turn — pure stall (all blocks, no attacks) gets 0.
          2. blocked/incoming >= 0.8 — only "good defensive turn" qualifies.
        Capped at 0.05 (half of prior 0.10) to keep magnitude small.
        """
        if pre_state is None or not dealt_damage_this_turn:
            return 0.0
        incoming = 0
        for e in pre_state.get("enemies", []) or []:
            if e.get("alive") is False:
                continue
            for it in (e.get("intents") or []):
                if (it.get("type") or "").lower() != "attack":
                    continue
                try:
                    dmg = int(it.get("damage", 0) or 0)
                    hits = int(it.get("hits", 1) or 1)
                except (TypeError, ValueError):
                    continue
                if dmg > 0 and hits > 0:
                    incoming += dmg * hits
        if incoming <= 0:
            return 0.0
        block = pre_state.get("player", {}).get("block", 0) or 0
        try:
            block = int(block)
        except (TypeError, ValueError):
            return 0.0
        # Require blocking at least 80% of incoming to qualify
        if block < incoming * 0.8:
            return 0.0
        blocked = min(block, incoming)
        max_hp = max(self._combat_start_player_max_hp, 1)
        return 0.05 * blocked / max_hp

    def _power_safe_turn_reward(self, card: dict, pre_state: dict) -> float:
        """+0.05 when playing a Power card on a 'safe' turn (no enemy attack
        intent). Encourages saving expensive setup for non-attack windows."""
        if pre_state is None or not isinstance(card, dict):
            return 0.0
        if (card.get("type") or "").lower() != "power":
            return 0.0
        for e in pre_state.get("enemies", []) or []:
            if e.get("alive") is False:
                continue
            for it in (e.get("intents") or []):
                if (it.get("type") or "").lower() == "attack":
                    return 0.0  # there's incoming damage; not safe
        return 0.05

    def _wasted_energy_penalty(self, pre_state: dict) -> float:
        """At end_turn, penalty if the player ended with ≥2 unspent energy AND
        had playable cards in hand. -0.02 per (small but adds up over a run).
        Acceptable to leave 1 energy (holding for next-turn setup); 2+ is waste."""
        if pre_state is None:
            return 0.0
        player = pre_state.get("player", {})
        energy = player.get("energy", 0) or 0
        try:
            energy = int(energy)
        except (TypeError, ValueError):
            return 0.0
        if energy < 2:
            return 0.0
        # Check for playable cards (cost <= energy, not unplayable status)
        hand = pre_state.get("hand", []) or []
        for c in hand:
            try:
                cost = int(c.get("cost", 99) or 99)
            except (TypeError, ValueError):
                continue
            cid = (c.get("id") or "")
            if isinstance(cid, dict):
                cid = cid.get("en", "")
            cid = str(cid).upper()
            if "WOUND" in cid or "BURN" in cid or "SLIMED" in cid or "DAZE" in cid:
                continue  # status cards can't be played voluntarily
            if cost <= energy:
                return -0.02  # had option, didn't use it
        return 0.0

    def _combat_win_reward(self, state: dict) -> float:
        hp = _player_hp(state)
        max_hp = self._combat_start_player_max_hp
        # hp_ratio is end_hp / start_hp_of_this_combat (not max_hp_of_run)
        # so a "no-damage win" returns ratio 1.0 even if combat started low-hp.
        hp_ratio = hp / max_hp
        # REVERTED 2026-05-19: Q1 0-damage bonus DISABLED. With max_combat_steps=1000,
        # the +2.0/+0.75/+0.50/+0.25 tiers made "block-then-kill" locally optimal —
        # agent drifted to stalling, hit cwr 82%→13%/to=84% collapse twice. Original
        # 8827k baseline (avg_floor=13.1) used only the quadratic curve below.
        reward = 3.0 * hp_ratio * hp_ratio
        # Floor bonus: Act 1 (floor≤15) = 0.10/floor; Act 2+ gets +0.15/floor above 15.
        if self._current_floor <= 15:
            floor_bonus = (self._current_floor - 1) * 0.10
        else:
            floor_bonus = 1.4 + (self._current_floor - 15) * 0.15
        reward += floor_bonus
        # Deck-quality milestone bonus — paid ONCE per run when crossing each
        # of {5, 10, 15} for the first time, scaled by deck quality 0–10.
        # Encourages building a strong deck early; passive Strikes/Defends
        # → low deck_quality → small or zero bonus.
        reward += self._milestone_reward(state)
        if self._current_combat_room_type == "Boss":
            reward += BOSS_CLEAR_BONUS
        return reward

    def _milestone_reward(self, state: dict) -> float:
        """One-shot reward when the run first crosses a milestone floor with a
        decent deck. Bounded ≤0.6 per milestone to avoid policy distortion.
        Also writes a deck snapshot row (buffered) for the predictor dataset."""
        floor = self._current_floor
        bonus = 0.0
        deck = state.get("player", {}).get("deck") or []
        for milestone in (5, 10, 15):
            if floor >= milestone and milestone not in self._milestones_paid:
                self._milestones_paid.add(milestone)
                q = deck_quality_score(deck)
                m_bonus = max(0.0, min((q - 4.5) * 0.15, 0.6))
                bonus += m_bonus
                # Buffer milestone record — final outcome appended in
                # _emit_run_outcome when the run ends.
                self._buffer_milestone_record(milestone, deck, q)
        return bonus

    def _buffer_milestone_record(self, milestone: int, deck: list, quality: float):
        """Save a deck snapshot for later outcome correlation."""
        if not self._deck_history_path:
            return
        try:
            from agent.card_scoring import (score_deck_dimensions,
                                            compute_deck_archetype, _card_id_norm)
        except ImportError:
            return
        try:
            dims = score_deck_dimensions(deck)
            arch = compute_deck_archetype(deck)
            cards = [_card_id_norm(c) for c in deck]
        except Exception:
            return
        self._run_milestone_records.append({
            "event": "milestone",
            "run_id": self._run_id,
            "floor_crossed": milestone,
            "deck_size": len(deck),
            "deck_quality": round(quality, 3),
            "dims": {k: round(v, 3) for k, v in dims.items()},
            "archetype": arch,
            "cards": cards,
            "ts": time.time(),
        })

    def _buffer_card_pick(self, state: dict, cmd: dict):
        """Record one card_reward decision (deck_before + offered options + picked).
        Each per-run buffer flushes to disk in _emit_run_outcome along with
        milestones and the outcome row. Trains the deck predictor on actual
        decision counterfactuals (you saw these 3 options with this deck and
        picked X / SKIP — what was the future floor?), much denser signal than
        the 3 milestone snapshots per run."""
        if not self._deck_history_path:
            return
        try:
            from agent.card_scoring import _card_id_norm
        except ImportError:
            return
        cards = state.get("cards") or []
        if not cards:
            return
        action = cmd.get("action", "")
        args = cmd.get("args", {}) or {}
        picked = None
        if action == "select_card_reward":
            idx = args.get("card_index", -1)
            if isinstance(idx, int) and 0 <= idx < len(cards):
                picked = _card_id_norm(cards[idx])
        elif action == "skip_card_reward":
            picked = "SKIP"
        else:
            return  # not a card_reward action — skip
        deck = state.get("player", {}).get("deck") or []
        floor = state.get("floor") or state.get("context", {}).get("floor", 1)
        opts = []
        for c in cards:
            opts.append({
                "id": _card_id_norm(c),
                "cost": c.get("cost"),
                "rarity": c.get("rarity"),
                "type": c.get("type"),
                "upgraded": c.get("upgraded", False),
            })
        self._run_card_pick_records.append({
            "event": "card_pick",
            "run_id": self._run_id,
            "floor": int(floor) if isinstance(floor, (int, float)) and floor > 0 else 1,
            "hp": state.get("player", {}).get("hp"),
            "max_hp": state.get("player", {}).get("max_hp"),
            "deck_before_ids": [_card_id_norm(c) for c in deck],
            "deck_size": len(deck),
            "options": opts,
            "picked": picked,
            "ts": time.time(),
        })

    def _emit_run_outcome(self, state: dict, victory: bool):
        """Flush buffered milestone + card_pick records to disk with the final
        outcome appended. Called once per run from terminal paths (game_over,
        crash, etc.)."""
        has_any = self._run_milestone_records or self._run_card_pick_records
        if not self._deck_history_path or not has_any:
            return
        outcome = {
            "event": "outcome",
            "run_id": self._run_id,
            "max_floor": int(self._run_max_floor),
            "won": bool(victory),
            "ts": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(self._deck_history_path) or ".", exist_ok=True)
            with open(self._deck_history_path, "a") as f:
                for rec in self._run_milestone_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                for rec in self._run_card_pick_records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
        except Exception:
            pass  # never let logging break training
        self._run_milestone_records = []

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
        room_type = (state.get("context") or {}).get("room_type", "")
        is_boss = "boss" in room_type.lower()
        # Floor-graduated 2026-05-19: Act 1 deaths often had unused heal potions at HP=20-30%
        # because old 0.40 threshold fires too late (one more enemy hit = dead). Act 1 has
        # no margin; better to burn the potion than die holding it.
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        if is_boss:
            heal_threshold = 0.50
        elif isinstance(floor, int) and floor <= 9:
            heal_threshold = 0.55  # Act 1: heal aggressively
        else:
            heal_threshold = 0.40
        if hp_ratio >= heal_threshold:
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
                    self._game_alive = False
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

            # Late-game (floor 10+): survivability matters more than saving potions
            is_late_game = self._current_floor >= 10
            is_act2 = self._current_floor >= 16

            if ("heal" in text or "restore" in text) and "curse" not in text:
                # Heal thresholds 2026-05-19: raised Act 1 monster from 0.30→0.50.
                # Verbose eval showed runs entering Act 1 elite at HP=40-45 with unused
                # heal potions then dying in one combat. Better to burn the potion at
                # 50% HP than die at 17% holding it.
                if is_boss:
                    use = hp_ratio < 0.60
                elif is_act2:
                    use = hp_ratio < 0.50
                elif is_elite or is_late_game:
                    use = hp_ratio < 0.50
                else:
                    use = hp_ratio < 0.50  # Act 1 monster: heal aggressively
            elif "block" in text:
                # Block potion: always use at boss (30 block is always worth it vs boss attacks);
                # at elite/threatening: use when damaged or incoming is severe
                threatening = incoming_dmg > 0 and unblocked_dmg >= hp_cur * 0.45
                use = is_boss or threatening or (is_elite and hp_ratio < 0.60)
            elif not is_tough and not is_late_game:
                continue  # other potions: save for elite/boss (but use freely in late game)
            elif not is_tough:
                # Late-game monster fights: use offensive/utility potions when damaged
                if "strength" in text or "flex" in text or "energy" in text:
                    use = hp_ratio < 0.60  # use offensive boost if damaged
                elif "fire" in text or "explosive" in text or "attack" in text:
                    use = hp_ratio < 0.50
                    target_index = 0 if target_type == "anyenemy" else None
                elif "weak" in text or "fear" in text or "vulnerable" in text:
                    use = hp_ratio < 0.50
                    target_index = 0 if target_type == "anyenemy" else None
                else:
                    continue  # save specialty potions for boss
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
            elif is_boss:
                use = True  # boss fight: dump any remaining unmatched potion

            if not use:
                continue

            # Always send target_index: targeted potions need enemy 0, non-targeted ignore it.
            # Omitting it causes C# to crash with "target ID is null" for single-target potions.
            args: dict = {
                "potion_index": pidx,
                "target_index": target_index if target_index is not None else 0,
            }
            new_state = self._send({"cmd": "action", "action": "use_potion", "args": args})
            if new_state is None:
                self._game_alive = False
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
            # Log every card_reward decision (deck_before + offered options + picked)
            # for the deck predictor's training set. Old milestone/outcome events
            # remain unchanged — this is a strictly-additive event stream.
            if state.get("decision") == "card_reward":
                self._buffer_card_pick(state, cmd)
            state = self._send(cmd)
        return state or {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}

    def _start_proc(self):
        crash_log = os.path.join(PROJECT_ROOT, "crash_stderr.log")
        self._crash_log_f = open(crash_log, "a")
        self._proc = subprocess.Popen(
            DOTNET + ["run", "--no-build", "--project", PROJECT],
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
            # DoEndTurn takes ~3-15s; killing blow triggers DetectPostCombatState
            # which can take up to ~10s for reward generation. Use 60s to be safe.
            return self._read_json(timeout_sec=60.0)
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
