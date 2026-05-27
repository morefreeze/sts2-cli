#!/usr/bin/env python3
"""mc_rollout.py — Generic Monte-Carlo rollout from any saved game state.

Loads a C# .save file and runs N rollouts from there to estimate future
outcomes (max_floor distribution, win rate, etc). Generalizes boss_retry.py
beyond boss combats — works from any save the game can produce.

Two main use cases:

  1. Ad-hoc diagnostic:
       .venv/bin/python -m agent.mc_rollout <save_path> \
           --n-sims 50 --policy heuristic

  2. Programmatic data generation (e.g. labels for v3 predictor training):
       from agent.mc_rollout import rollout
       outcomes = rollout(save_path="data/snapshots/X.save", n_sims=100,
                          ckpt_path="checkpoints/ppo_ironclad_13219k.zip",
                          deterministic=False)
       # outcomes is list[{"max_floor", "won", "steps", "final_hp", "combat_wins"}]

Policy options:
  - "heuristic": pure greedy_action (no RL model loaded — fastest, fully
    deterministic environment seeds notwithstanding)
  - ckpt_path given: MaskablePPO loaded from path; deterministic flag chooses
    argmax vs stochastic sampling.

Depth limit: when --max-floor N (or max_floor=N arg) is set, the rollout
stops after the player crosses floor N. Default 0 = play until game_over.
"""
import argparse
import json
import os
import sys
import time
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.combat_env import CombatEnv


def _maybe_load_model(ckpt_path: Optional[str]):
    if not ckpt_path:
        return None, False
    import torch
    from sb3_contrib import MaskablePPO
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = MaskablePPO.load(ckpt_path, device=device)
    extra_obs = model.observation_space.shape[0] > 161
    return model, extra_obs


def _one_rollout(model, save_path: str, deterministic: bool, extra_obs: bool,
                 set_hp: Optional[int] = None, max_floor: int = 0,
                 max_steps: int = 5000) -> dict:
    """Run a single rollout from `save_path`. Stops at game_over OR when the
    player crosses max_floor (0 = no limit) OR after max_steps env steps.

    With model=None, the env's heuristic policy still drives card_reward /
    map / event picks (via greedy_action); for combat steps we use a random
    action over the action mask (since combat has no built-in heuristic).
    Use this mode only for end-to-end smoke checks — the random-combat policy
    is much weaker than even the heuristic baseline.
    """
    env = CombatEnv(character="Ironclad",
                    seed=f"mc_{int(time.time()*1e6) % 10**9}",
                    native_save_path=save_path,
                    extra_obs=extra_obs,
                    set_hp_after_load=set_hp,
                    max_floor=max_floor)
    from sb3_contrib.common.wrappers import ActionMasker
    from agent.train import mask_fn
    env_w = ActionMasker(env, mask_fn)
    obs, _info = env_w.reset()

    max_floor_seen = env._current_floor
    combat_wins = 0
    won = False
    steps = 0
    error = None
    try:
        if not env._game_alive:
            return {"max_floor": 1, "won": False, "steps": 0, "final_hp": 0,
                    "combat_wins": 0, "error": "load_failed"}
        while steps < max_steps:
            done = False
            info = {}
            while not done and steps < max_steps:
                masks = env_w.action_masks()
                if model is None:
                    valid = np.where(masks)[0]
                    if len(valid) == 0:
                        break
                    action = int(np.random.choice(valid))
                else:
                    action, _ = model.predict(obs, deterministic=deterministic,
                                              action_masks=masks)
                    action = int(action)
                obs, _r, terminated, truncated, info = env_w.step(action)
                done = terminated or truncated
                steps += 1
                if env._current_floor > max_floor_seen:
                    max_floor_seen = env._current_floor
            if info.get("combat_won"):
                combat_wins += 1
            # Check terminal: game_over / crashed / timed-out OR depth limit
            if info.get("game_over") or info.get("crashed") or info.get("timeout"):
                won = bool(info.get("victory"))
                break
            if max_floor > 0 and env._current_floor >= max_floor:
                break
            # Else: combat ended, try to advance to next via reset
            try:
                obs, reset_info = env_w.reset()
                if reset_info.get("game_over"):
                    won = bool(reset_info.get("victory"))
                    break
                if not env._game_alive:
                    break
            except Exception as e:
                error = f"reset_err: {type(e).__name__}: {e}"
                break
        final_hp = int((env._current_state or {}).get("player", {}).get("hp", 0) or 0)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        final_hp = 0
    finally:
        try:
            env_w.close()
        except Exception:
            pass
    return {"max_floor": max_floor_seen, "won": won, "steps": steps,
            "final_hp": final_hp, "combat_wins": combat_wins, "error": error}


def rollout(save_path: str, n_sims: int = 30,
            ckpt_path: Optional[str] = None, deterministic: bool = False,
            set_hp: Optional[int] = None, max_floor: int = 0) -> list[dict]:
    """Public API: run n_sims rollouts from save_path, return list of outcomes."""
    model, extra_obs = _maybe_load_model(ckpt_path)
    out = []
    for _ in range(n_sims):
        out.append(_one_rollout(model, save_path, deterministic, extra_obs,
                                set_hp=set_hp, max_floor=max_floor))
    return out


def _summarize(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}
    floors = [r["max_floor"] for r in results]
    wins = sum(1 for r in results if r["won"])
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "avg_max_floor": float(np.mean(floors)),
        "median_max_floor": float(np.median(floors)),
        "max_max_floor": int(max(floors)),
        "avg_combat_wins": float(np.mean([r["combat_wins"] for r in results])),
        "avg_steps": float(np.mean([r["steps"] for r in results])),
        "errors": sum(1 for r in results if r.get("error")),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("save_path", help="Path to a single .save file")
    p.add_argument("--n-sims", type=int, default=30)
    p.add_argument("--ckpt", default=None,
                   help="Optional PPO checkpoint; default is random-action policy on combat")
    p.add_argument("--deterministic", action="store_true",
                   help="Use deterministic policy (only meaningful with --ckpt)")
    p.add_argument("--set-hp", type=int, default=None,
                   help="Override player HP after load (e.g. for boss-fight headroom sweeps)")
    p.add_argument("--max-floor", type=int, default=0,
                   help="Stop rollout when player crosses this floor (0 = no limit)")
    p.add_argument("--report-json", default=None)
    args = p.parse_args()

    if not os.path.exists(args.save_path):
        print(f"No save at {args.save_path}")
        return 1

    pol = "ckpt" if args.ckpt else "random-on-combat"
    if args.ckpt:
        pol += f" {os.path.basename(args.ckpt)} ({'det' if args.deterministic else 'stoch'})"
    print(f"Save     : {args.save_path}")
    print(f"Policy   : {pol}")
    print(f"N sims   : {args.n_sims}")
    if args.set_hp is not None:
        print(f"set_hp   : {args.set_hp}")
    if args.max_floor > 0:
        print(f"max_floor: {args.max_floor}")
    print()

    t0 = time.time()
    results = rollout(args.save_path, args.n_sims, args.ckpt,
                      args.deterministic, args.set_hp, args.max_floor)
    elapsed = time.time() - t0
    summ = _summarize(results)
    print(f"--- summary ({elapsed:.1f}s, {elapsed/max(args.n_sims,1):.1f}s/sim) ---")
    for k, v in summ.items():
        if isinstance(v, float):
            print(f"  {k:<22s} {v:.2f}")
        else:
            print(f"  {k:<22s} {v}")
    floor_dist = sorted(r["max_floor"] for r in results)
    print(f"  max_floor distribution : {floor_dist}")

    if args.report_json:
        os.makedirs(os.path.dirname(args.report_json) or ".", exist_ok=True)
        with open(args.report_json, "w") as f:
            json.dump({"summary": summ, "results": results}, f, indent=2)
        print(f"\nWrote {args.report_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
