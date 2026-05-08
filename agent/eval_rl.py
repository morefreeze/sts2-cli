#!/usr/bin/env python3
"""eval_rl.py — Standalone evaluation for a saved MaskablePPO checkpoint.

Usage:
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip --n-games 20
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip --verbose
"""
import argparse, os, random, signal, sys
import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv, greedy_action
from agent.train import mask_fn


_GAME_TIMEOUT_SEC = 300  # 5 min per game — kills deadlocked C# processes


class _GameTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _GameTimeout()


def _card_id(c: dict) -> str:
    cid = c.get("id", "?")
    if isinstance(cid, dict):
        cid = cid.get("en", str(cid))
    cid = str(cid)
    if cid.upper().startswith("CARD."):
        cid = cid[5:]
    return cid


def _fmt_hand(state: dict) -> str:
    hand = state.get("hand", [])
    if not hand:
        return "[]"
    parts = []
    for c in hand:
        cid = _card_id(c)
        cost = c.get("cost", "?")
        parts.append(f"{cid}({cost})")
    return "[" + ", ".join(parts) + "]"


def _fmt_enemies(state: dict) -> str:
    enemies = state.get("enemies", [])
    if not enemies:
        return "[]"
    parts = []
    for e in enemies:
        hp = e.get("hp", "?")
        mhp = e.get("max_hp", "?")
        name = e.get("name", "?")
        if isinstance(name, dict):
            name = name.get("en", str(name))
        intents = e.get("intents") or []
        intent_str = ""
        for it in intents:
            t = it.get("type", "?")
            dmg = it.get("damage", 0)
            hits = it.get("hits") or 1
            if t.lower() == "attack":
                intent_str += f" ATK{dmg}x{hits}"
            else:
                intent_str += f" {t[:3]}"
        parts.append(f"{name}({hp}/{mhp}{intent_str})")
    return "[" + ", ".join(parts) + "]"


def _decode_action_name(env: CombatEnv, action: int, state: dict) -> str:
    """Decode action index to human-readable string."""
    try:
        cmd = env.enc.decode(action, state)
        act = cmd.get("action", "?")
        args = cmd.get("args", {})
        if act == "play_card":
            ci = args.get("card_index", 0)
            hand = state.get("hand", [])
            card = hand[ci] if ci < len(hand) else {}
            return f"play {_card_id(card)}"
        elif act == "end_turn":
            return "end_turn"
        return f"{act}({args})"
    except Exception:
        return f"action_{action}"


class _VerboseCombatEnv(CombatEnv):
    """CombatEnv subclass that logs room transitions during _advance_to_combat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.room_log: list[str] = []   # per-game room entries
        self.combat_steps: list[str] = []  # per-combat step log
        self.trace_combat = False  # set True for last-combat step trace

    def _advance_to_combat(self, state):
        for _ in range(200):
            if state is None:
                return {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}
            if state.get("decision") == "combat_play":
                return self._greedy_use_potions(state)
            if state.get("decision") == "game_over":
                return state
            dec = state.get("decision", "")
            self._log_room(state, dec)
            cmd = greedy_action(state)
            state = self._send(cmd)
        return state or {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}

    def _log_room(self, state: dict, dec: str):
        fl = state.get("floor") or state.get("context", {}).get("floor", "?")
        player = state.get("player", {})
        hp = player.get("hp", "?")
        mhp = player.get("max_hp", "?")
        hp_str = f"HP={hp}/{mhp}" if hp != "?" else "HP=?"

        if dec == "map_select":
            choices = state.get("choices", [])
            types = [c.get("type", "?") for c in choices]
            cmd = greedy_action(state)
            sel_col = cmd.get("args", {}).get("col", -1)
            sel = next((c for c in choices if c.get("col") == sel_col), {})
            chosen = sel.get("type", "?")
            self.room_log.append(
                f"  [map  fl={fl}] {hp_str} options={types} → {chosen}")

        elif dec == "event_choice":
            name = state.get("event_name", "?")
            opts = state.get("options", [])
            available = [o for o in opts if not o.get("is_locked")]
            from agent.combat_env import _score_event_option
            if available:
                best = max(available, key=_score_event_option)
                chosen_title = best.get("title", "?")
            else:
                chosen_title = "leave"
            titles = [o.get("title", "?") for o in opts]
            self.room_log.append(
                f"  [event fl={fl}] {hp_str} {name}: {titles} → {chosen_title}")

        elif dec == "rest_site":
            opts = [o.get("option_id", o.get("title", "?")) for o in state.get("options", [])]
            cmd = greedy_action(state)
            chosen = cmd.get("args", {}).get("option_index", "?")
            opted = next((o for o in state.get("options", []) if o.get("index") == chosen), {})
            chosen_name = opted.get("option_id", opted.get("title", "?"))
            self.room_log.append(
                f"  [rest fl={fl}] {hp_str} options={opts} → {chosen_name}")

        elif dec == "card_reward":
            from agent.card_scoring import score_card, pick_best_card
            cards = state.get("cards", [])
            best_idx = pick_best_card(cards)
            if best_idx is not None and best_idx < len(cards):
                chosen = _card_id(cards[best_idx])
            else:
                chosen = "SKIP"
            top3 = [(score_card(c), _card_id(c)) for c in cards]
            top3.sort(reverse=True)
            self.room_log.append(
                f"  [reward fl={fl}] {hp_str} top={[f'{n}({s:.1f})' for s,n in top3[:3]]} → {chosen}")

        elif dec == "shop":
            gold = state.get("player", {}).get("gold", 0)
            self.room_log.append(f"  [shop  fl={fl}] {hp_str} gold={gold}")

        elif dec == "treasure":
            self.room_log.append(f"  [treas fl={fl}] {hp_str}")


def run_eval_verbose(model, character: str, n_games: int = 10,
                     fixed_seeds: bool = False, seed_offset: int = 0,
                     verbose: bool = False,
                     replay_actions: list = None,
                     load_seed: str = None,
                     native_save_path: str = None) -> dict:
    """Full-run eval with per-game floor breakdown.

    verbose=True: show per-room summaries; for wins, show last combat step-by-step.
    replay_actions/load_seed: replay action log after start_run.
    native_save_path: load a binary .save via load_save instead of start_run.
    """
    # Auto-detect obs_size: legacy=161, run11=169 (extra_obs adds 8 features)
    model_obs_size = model.observation_space.shape[0]
    extra_obs = (model_obs_size > 161)

    floors, wins, combat_wins_list = [], [], []
    for i in range(n_games):
        if load_seed:
            game_seed = load_seed
        elif fixed_seeds:
            game_seed = f"eval_fixed_{i + seed_offset}"
        else:
            game_seed = f"eval_r{random.randint(0, 0xFFFFFF):06x}_{i}"

        env_kwargs = dict(character=character, seed=game_seed,
                          seed_prefix=f"eval_{i}", max_floor=0, extra_obs=extra_obs)
        if replay_actions:
            env_kwargs["replay_actions"] = replay_actions
        if native_save_path:
            env_kwargs["native_save_path"] = native_save_path

        if verbose:
            env = _VerboseCombatEnv(**env_kwargs)
        else:
            env = CombatEnv(**env_kwargs)
        env_wrapped = ActionMasker(env, mask_fn)
        obs, _ = env_wrapped.reset()
        ep_combat_wins = 0
        max_floor = 1
        run_won = False
        run_over = False
        timed_out = False
        all_combat_logs = []  # list of (floor, steps_log)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_GAME_TIMEOUT_SEC)
        try:
            while not run_over:
                done = False
                last_info = {}
                cur_floor = env._current_floor
                hp_before = (env._current_state.get("player", {}).get("hp", "?")
                             if env._current_state else "?")
                steps_log = []

                while not done:
                    state_snap = env._current_state
                    masks = env_wrapped.action_masks()
                    action, _ = model.predict(obs, deterministic=True, action_masks=masks)

                    if verbose and state_snap:
                        action_name = _decode_action_name(env, int(action), state_snap)
                        ph = state_snap.get("player", {}).get("hp", "?")
                        pb = state_snap.get("player", {}).get("block", 0)
                        rnd = state_snap.get("round", "?")
                        hand_str = _fmt_hand(state_snap)
                        enemy_str = _fmt_enemies(state_snap)
                        steps_log.append(
                            f"    r{rnd} HP={ph}({pb}blk) | {enemy_str} | hand={hand_str} → {action_name}")

                    obs, _r, terminated, truncated, last_info = env_wrapped.step(int(action))
                    done = terminated or truncated
                    f = last_info.get("floor", 0)
                    if f:
                        max_floor = max(max_floor, f)

                hp_after = (env._current_state.get("player", {}).get("hp", "?")
                            if env._current_state else "?")
                all_combat_logs.append((cur_floor, hp_before, hp_after, steps_log))

                if last_info.get("combat_won"):
                    ep_combat_wins += 1
                    floor_won = last_info.get("floor", 0)
                    obs, reset_info = env_wrapped.reset()
                    # reset() returns game_over info when run ended during advance
                    # (crash or legit game_over between combats)
                    if reset_info.get("game_over"):
                        run_over = True
                        last_info = reset_info
                        if reset_info.get("victory"):
                            run_won = True
                else:
                    run_over = True
                    if last_info.get("victory"):
                        run_won = True
        except _GameTimeout:
            timed_out = True
        finally:
            signal.alarm(0)

        env_wrapped.close()
        floors.append(max_floor)
        combat_wins_list.append(ep_combat_wins)
        wins.append(1 if run_won else 0)

        if timed_out:
            end_reason = "TIMEOUT"
        elif run_won:
            end_reason = "WIN"
        elif last_info.get("crashed"):
            end_reason = "crash/stuck"
        else:
            end_reason = "dead"

        print(f"  game {i+1:2d}: floor={max_floor:2d} combats={ep_combat_wins} [{end_reason}]")

        if verbose:
            # Print per-room log
            room_log = getattr(env, "room_log", [])
            # Print combat summaries interleaved with room log
            combat_idx = 0
            for entry in room_log:
                # Flush pending combat summary if any
                print(entry)

            # Print all combat summaries
            for fl, hp_b, hp_a, steps in all_combat_logs:
                mhp = "?"
                if env._current_state:
                    mhp = env._current_state.get("player", {}).get("max_hp", "?")
                result = "won" if (fl, hp_b, hp_a, steps) != all_combat_logs[-1] or run_won else "dead"
                if (fl, hp_b, hp_a, steps) == all_combat_logs[-1]:
                    result = "WIN" if run_won else "dead"
                else:
                    result = "won"
                print(f"  [combat fl={fl}] HP {hp_b}→{hp_a} [{result}]")

            # For wins: print last combat step-by-step
            if run_won and all_combat_logs:
                last_fl, _, _, last_steps = all_combat_logs[-1]
                print(f"\n  === Last combat (fl={last_fl}) step-by-step ===")
                for step in last_steps:
                    print(step)
            print()

    return {
        "avg_floor": float(np.mean(floors)),
        "max_floor": int(max(floors)),
        "win_rate": float(np.mean(wins)),
        "avg_combat_wins": float(np.mean(combat_wins_list)),
        "floors": floors,
        "n": n_games,
    }


def _latest_checkpoint(checkpoints_dir: str = "checkpoints") -> str:
    import glob, re
    zips = glob.glob(os.path.join(checkpoints_dir, "ppo_ironclad_*.zip"))
    if not zips:
        raise FileNotFoundError(f"No checkpoints found in {checkpoints_dir}/")
    def _steps(p):
        m = re.search(r"_(\d+)k\.zip$", p)
        return int(m.group(1)) if m else 0
    return max(zips, key=_steps)


def main():
    import json as _json

    p = argparse.ArgumentParser()
    p.add_argument("checkpoint", nargs="?", default=None,
                   help="Path to checkpoint zip (default: latest in checkpoints/)")
    p.add_argument("--character", default="Ironclad")
    p.add_argument("--n-games", type=int, default=20)
    p.add_argument("--fixed-seeds", action="store_true",
                   help="Use fixed eval_fixed_0..N seeds (reproducible but risks overfitting)")
    p.add_argument("--seed-offset", type=int, default=0,
                   help="Offset for fixed seed index (use with --fixed-seeds)")
    p.add_argument("--verbose", action="store_true", default=False,
                   help="Per-room summaries + detailed last-combat trace on wins")
    p.add_argument("--load", default=None,
                   help="Replay actions from a play.py save (.json) before agent takes over")
    args = p.parse_args()

    replay_actions = None
    load_seed = None
    native_save_path = None
    character = args.character
    n_games = args.n_games
    if args.load:
        load_path = os.path.abspath(args.load)
        if load_path.endswith(".save"):
            native_save_path = load_path
        else:
            with open(load_path) as f:
                save = _json.load(f)
            if "actions" not in save:
                print(f"Error: {load_path} is not a play.py replay save (no 'actions' key)")
                sys.exit(1)
            character = save.get("character", character)
            load_seed = save.get("seed")
            replay_actions = save["actions"]
        # Result is deterministic post-load; default to 1 game unless user passed --n-games.
        if "--n-games" not in sys.argv and "-n" not in sys.argv:
            n_games = 1

    checkpoint = args.checkpoint or _latest_checkpoint()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = MaskablePPO.load(checkpoint, device=device)

    seed_mode = ("native_save" if native_save_path
                 else "loaded" if load_seed
                 else "fixed" if args.fixed_seeds
                 else "random")
    print(f"Checkpoint    : {checkpoint}")
    print(f"Internal steps: {getattr(model, 'num_timesteps', '?')}")
    if native_save_path:
        print(f"Native save   : {native_save_path}")
    elif args.load:
        print(f"Loaded save   : {args.load}")
        print(f"  character   : {character}")
        print(f"  seed        : {load_seed}")
        print(f"  actions     : {len(replay_actions)}")
    print(f"Running {n_games} games ({seed_mode} seeds, max_floor=unlimited)...")

    stats = run_eval_verbose(model, character, n_games=n_games,
                             fixed_seeds=args.fixed_seeds, seed_offset=args.seed_offset,
                             verbose=args.verbose,
                             replay_actions=replay_actions, load_seed=load_seed,
                             native_save_path=native_save_path)
    print(f"---")
    print(f"avg_floor      : {stats['avg_floor']:.1f}")
    print(f"max_floor      : {stats['max_floor']}")
    print(f"win_rate       : {stats['win_rate']:.0%}")
    print(f"avg_combat_wins: {stats['avg_combat_wins']:.1f}")
    print(f"floor dist     : {sorted(stats['floors'])}")


if __name__ == "__main__":
    main()
