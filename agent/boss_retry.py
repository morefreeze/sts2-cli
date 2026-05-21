#!/usr/bin/env python3
"""boss_retry.py — Repeatedly replay boss-entry snapshots to diagnose boss losses.

Workflow:
  1. Run eval with `--boss-snapshot-dir DIR` to capture C# save files at the
     start of every boss combat reached with HP above threshold.
  2. Run this script on each snapshot: it loads the save, lets the policy play
     the boss combat, and aggregates outcomes across many trials per snapshot.

For each snapshot file (`<dir>/*.save`):
  - Play --n-deterministic trials with deterministic policy (variance from C# RNG)
  - Play --n-stochastic trials with stochastic policy (samples action distribution)
  - Report: win rate, average final HP, average combat length

Usage:
  python agent/boss_retry.py checkpoints/ppo_ironclad_11150k.zip \\
      data/boss_snapshots/ --n-deterministic 30 --n-stochastic 30
"""
import argparse
import glob
import json
import os
import sys
import time

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker

from agent.combat_env import CombatEnv
from agent.train import mask_fn


def _player_hp(env: CombatEnv) -> int:
    st = env._current_state or {}
    return int(st.get("player", {}).get("hp", 0) or 0)


def _play_one(model, save_path: str, deterministic: bool, extra_obs: bool,
              max_steps: int = 2000, set_hp: int = None) -> dict:
    """Load snapshot, play until the (boss) episode ends, return outcome dict.

    If set_hp is given, player HP is forced to that value after the save loads
    but before combat starts — used to sweep "how much HP suffices to win"."""
    env = CombatEnv(character="Ironclad",
                    seed=f"boss_retry_{int(time.time()*1e6) % 10**9}",
                    native_save_path=save_path,
                    extra_obs=extra_obs,
                    set_hp_after_load=set_hp)
    env_w = ActionMasker(env, mask_fn)
    won = False
    victory = False
    steps = 0
    final_hp = 0
    error = None
    try:
        obs, _info = env_w.reset()
        if not env._game_alive:
            return {"won": False, "victory": False, "steps": 0,
                    "final_hp": 0, "error": "load_failed"}
        done = False
        info = {}
        while not done and steps < max_steps:
            masks = env_w.action_masks()
            action, _ = model.predict(obs, deterministic=deterministic, action_masks=masks)
            obs, _r, terminated, truncated, info = env_w.step(int(action))
            done = terminated or truncated
            steps += 1
        won = bool(info.get("combat_won")) or bool(info.get("victory"))
        victory = bool(info.get("victory"))
        final_hp = _player_hp(env)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            env_w.close()
        except Exception:
            pass
    return {"won": won, "victory": victory, "steps": steps,
            "final_hp": final_hp, "error": error}


def _summarize(results: list) -> dict:
    n = len(results)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in results if r["won"])
    valid = [r for r in results if r.get("error") is None]
    hps = [r["final_hp"] for r in results]
    steps = [r["steps"] for r in results]
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "avg_final_hp": float(np.mean(hps)),
        "median_final_hp": float(np.median(hps)),
        "avg_steps": float(np.mean(steps)),
        "errors": n - len(valid),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("snapshot_dir",
                   help="Directory containing *.save files from eval_rl.py --boss-snapshot-dir. "
                        "Accepts a single .save path too.")
    p.add_argument("--n-deterministic", type=int, default=30)
    p.add_argument("--n-stochastic", type=int, default=30)
    p.add_argument("--character", default="Ironclad")
    p.add_argument("--hp-sweep", default=None,
                   help="Comma-separated HP values to force-set after loading the snapshot, e.g. "
                        "'30,40,50,60,70,80'. For each value, runs n-stochastic trials (deterministic "
                        "is skipped since determinism + identical save = identical outcome). "
                        "Use to find the HP threshold where the boss becomes winnable.")
    p.add_argument("--report-json", default=None,
                   help="Optional path to write per-snapshot results as JSON.")
    args = p.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = MaskablePPO.load(args.checkpoint, device=device)
    extra_obs = model.observation_space.shape[0] > 161

    if os.path.isfile(args.snapshot_dir) and args.snapshot_dir.endswith(".save"):
        snapshots = [args.snapshot_dir]
    else:
        snapshots = sorted(glob.glob(os.path.join(args.snapshot_dir, "*.save")))
    if not snapshots:
        print(f"No *.save files in {args.snapshot_dir}")
        sys.exit(1)

    hp_sweep = None
    if args.hp_sweep:
        hp_sweep = [int(x) for x in args.hp_sweep.split(",") if x.strip()]

    print(f"Checkpoint : {args.checkpoint}")
    print(f"Internal steps: {getattr(model, 'num_timesteps', '?')}")
    print(f"Snapshots  : {len(snapshots)} from {args.snapshot_dir}")
    if hp_sweep:
        print(f"HP sweep   : {hp_sweep} (each value × {args.n_stochastic} stochastic trials)")
    else:
        print(f"Per snap   : {args.n_deterministic} deterministic + {args.n_stochastic} stochastic")
    print()

    full_report = []
    for snap in snapshots:
        meta = {}
        meta_path = snap + ".meta.json"
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:
                pass
        label = (f"hp={meta.get('hp_at_boss','?')} "
                 f"fl={meta.get('floor','?')} "
                 f"seed={meta.get('seed','?')}")
        print(f"=== {os.path.basename(snap)} ({label}) ===")

        snap_report = {"snapshot": os.path.basename(snap), "meta": meta, "modes": {}}
        if hp_sweep:
            for hp in hp_sweep:
                t0 = time.time()
                results = [
                    _play_one(model, snap, deterministic=False, extra_obs=extra_obs, set_hp=hp)
                    for _ in range(args.n_stochastic)
                ]
                summ = _summarize(results)
                elapsed = time.time() - t0
                print(f"  hp={hp:>3}: win {summ['wins']:>2}/{summ['n']} "
                      f"({100*summ['win_rate']:>3.0f}%) | "
                      f"avg_final_hp={summ['avg_final_hp']:>5.1f} "
                      f"(med={summ['median_final_hp']:>4.0f}) | "
                      f"avg_steps={summ['avg_steps']:>5.0f} | "
                      f"{elapsed:.1f}s")
                snap_report["modes"][f"hp_{hp}"] = {"summary": summ, "results": results}
        else:
            for mode, n_runs, det in [
                ("deterministic", args.n_deterministic, True),
                ("stochastic", args.n_stochastic, False),
            ]:
                if n_runs <= 0:
                    continue
                t0 = time.time()
                results = []
                for i in range(n_runs):
                    r = _play_one(model, snap, det, extra_obs)
                    results.append(r)
                    if r.get("error"):
                        print(f"  {mode} {i+1:>2}: ERROR {r['error']}")
                summ = _summarize(results)
                elapsed = time.time() - t0
                print(f"  {mode:13s}: win {summ['wins']:>2}/{summ['n']} "
                      f"({100*summ['win_rate']:>3.0f}%) | "
                      f"avg_final_hp={summ['avg_final_hp']:>5.1f} "
                      f"(med={summ['median_final_hp']:>4.0f}) | "
                      f"avg_steps={summ['avg_steps']:>5.0f} | "
                      f"{elapsed:.1f}s")
                snap_report["modes"][mode] = {"summary": summ, "results": results}
        full_report.append(snap_report)
        print()

    if args.report_json:
        os.makedirs(os.path.dirname(args.report_json) or ".", exist_ok=True)
        with open(args.report_json, "w") as f:
            json.dump(full_report, f, indent=2)
        print(f"Report written to {args.report_json}")


if __name__ == "__main__":
    main()
