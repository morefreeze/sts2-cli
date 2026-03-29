#!/usr/bin/env python3
"""train_loop.py — Autonomous train-eval loop for STS2 RL agent.

Trains to milestones, evaluates with full game runs, logs results to JSONL.
Resumes automatically after interruption.

Usage:
    python3 agent/train_loop.py --character Ironclad --n-eval-games 15
    python3 agent/train_loop.py --milestones 10000,25000,50000
    # Resume after interruption:
    python3 agent/train_loop.py --character Ironclad
"""
import argparse, json, os, random, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import torch
from stable_baselines3.common.vec_env import DummyVecEnv
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker

from agent.combat_env import CombatEnv, greedy_action
from agent.state_encoder import StateEncoder
from agent.train import CHECKPOINT_DIR, ProgressCallback, make_env, mask_fn
from agent.rl_agent import RLAgent
from agent.coordinator import GameCoordinator

DEFAULT_MILESTONES = [25_000, 50_000, 100_000, 200_000, 500_000, 1_000_000]


def find_resume_state(log_path, checkpoint_dir, character, milestones):
    """Determine where to resume based on log + existing checkpoints."""
    steps_done = 0
    ckpt_path = None

    # Read log for completed milestones
    if os.path.isfile(log_path):
        max_steps = 0
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    s = rec.get("total_steps", 0)
                    if s > max_steps:
                        max_steps = s
                except json.JSONDecodeError:
                    continue
        steps_done = max_steps

    # Find latest checkpoint for this character
    if os.path.isdir(checkpoint_dir):
        pattern = re.compile(rf"^ppo_{character.lower()}_(\d+)k\.zip$")
        best_steps = 0
        for fname in os.listdir(checkpoint_dir):
            m = pattern.match(fname)
            if m:
                s = int(m.group(1)) * 1000
                if s > best_steps:
                    best_steps = s
                    ckpt_path = os.path.join(checkpoint_dir, fname)

    # If checkpoint is ahead of log (crash during eval), use checkpoint
    if ckpt_path and best_steps > steps_done:
        steps_done = best_steps

    return steps_done, ckpt_path


def extract_train_metrics(model):
    """Read latest training metrics from model logger."""
    logger = model.logger
    if not hasattr(logger, "name_to_value"):
        return {}
    nv = logger.name_to_value
    result = {}
    for key, short in [("train/entropy_loss", "entropy_loss"),
                       ("train/value_loss", "value_loss"),
                       ("train/explained_variance", "explained_variance"),
                       ("train/policy_gradient_loss", "policy_gradient_loss")]:
        val = nv.get(key)
        if val is not None:
            result[short] = round(val, 6)
    return result


def _run_single_eval(model, encoder, character, game_idx, ascension):
    """Run one eval game. Each call creates its own coordinator (own subprocess)."""
    rl = RLAgent.__new__(RLAgent)
    rl.enc = encoder
    rl.model = model
    coord = GameCoordinator(rl_agent=rl, llm_agent=None, verbose=False, lang="en")
    seed = f"loop_{character.lower()}_{game_idx}_{random.randint(0, 99999)}"
    return game_idx, coord.run_game(character, seed, ascension)


def run_eval(model, encoder, character, n_games, ascension, n_workers=1):
    """Run N full games. If n_workers > 1, evaluate in parallel threads."""
    games = [None] * n_games

    if n_workers <= 1:
        # Sequential
        for i in range(n_games):
            _, result = _run_single_eval(model, encoder, character, i, ascension)
            games[i] = result
            _print_eval_result(i, n_games, result)
    else:
        # Parallel — each thread gets its own coordinator/subprocess
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {
                pool.submit(_run_single_eval, model, encoder, character, i, ascension): i
                for i in range(n_games)
            }
            for future in as_completed(futures):
                i, result = future.result()
                games[i] = result
                _print_eval_result(i, n_games, result)

    wins = sum(1 for g in games if g and g.get("victory"))
    floors = [g["floor"] for g in games if g and g.get("floor") is not None]
    win_rate = wins / max(n_games, 1)
    avg_floor = sum(floors) / max(len(floors), 1)
    return {
        "n_games": n_games,
        "win_rate": round(win_rate, 3),
        "avg_floor": round(avg_floor, 1),
        "games": games,
    }


def _print_eval_result(idx, total, result):
    status = "WIN" if result.get("victory") else "LOSS"
    err = result.get("error", "")
    floor_str = result.get("floor", "?")
    hp = result.get("hp", "?")
    mhp = result.get("max_hp", "?")
    suffix = f" ({err})" if err else ""
    print(f"    Eval {idx+1}/{total}: {status} | floor={floor_str} | hp={hp}/{mhp}{suffix}",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description="Autonomous train-eval loop")
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--milestones", default=None,
                        help="Comma-separated step counts (default: 25k,50k,100k,200k,500k,1M)")
    parser.add_argument("--n-envs", type=int, default=1)
    parser.add_argument("--n-eval-games", type=int, default=15,
                        help="Number of eval games per milestone")
    parser.add_argument("--n-eval-workers", type=int, default=1,
                        help="Parallel eval threads (default: 1 = sequential)")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--log-path", default="training_log.jsonl")
    parser.add_argument("--checkpoint-dir", default=CHECKPOINT_DIR)
    args = parser.parse_args()

    # Parse milestones
    if args.milestones:
        milestones = sorted(int(x.strip()) for x in args.milestones.split(","))
    else:
        milestones = DEFAULT_MILESTONES

    character = args.character
    ckpt_dir = args.checkpoint_dir
    os.makedirs(ckpt_dir, exist_ok=True)

    # Resume
    steps_done, ckpt_path = find_resume_state(
        args.log_path, ckpt_dir, character, milestones)
    if steps_done > 0:
        print(f"Resuming from {steps_done} steps (checkpoint: {ckpt_path})")

    # Create training env + model
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    vec_env = DummyVecEnv([make_env(character, args.ascension, i)
                           for i in range(args.n_envs)])

    n_steps = 2048
    policy_kwargs = dict(net_arch=dict(pi=[128, 128], vf=[128, 128]))

    if ckpt_path:
        print(f"Loading checkpoint: {ckpt_path}")
        model = MaskablePPO.load(ckpt_path, env=vec_env, device=device)
    else:
        model = MaskablePPO("MlpPolicy", vec_env, verbose=0, device=device,
                            policy_kwargs=policy_kwargs,
                            n_steps=n_steps, batch_size=512, n_epochs=4,
                            learning_rate=3e-4, gamma=0.99, ent_coef=0.03,
                            vf_coef=0.5, max_grad_norm=0.5,
                            tensorboard_log=os.path.join(ckpt_dir, "tb_logs"))

    encoder = StateEncoder(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "localization_eng", "cards.json"))

    # Training loop
    current_steps = steps_done
    for milestone in milestones:
        if milestone <= current_steps:
            continue

        steps_to_train = milestone - current_steps
        print(f"\n{'='*60}")
        print(f"Training: {current_steps} -> {milestone} ({steps_to_train} steps)")
        print(f"{'='*60}")

        # Train in chunks, saving intermediate checkpoints every 10k steps
        save_interval = 10_000
        trained = 0
        while trained < steps_to_train:
            chunk = min(save_interval, steps_to_train - trained)
            callback = ProgressCallback(chunk, args.n_envs, n_steps)
            model.learn(total_timesteps=chunk, callback=callback,
                        reset_num_timesteps=(current_steps + trained == 0))
            trained += chunk

            # Save intermediate checkpoint
            total = current_steps + trained
            ckpt_name = f"ppo_{character.lower()}_{total // 1000}k.zip"
            ckpt_file = os.path.join(ckpt_dir, ckpt_name)
            model.save(ckpt_file)
            print(f"\n  Checkpoint saved: {ckpt_file}")

        current_steps = milestone

        # Extract training metrics
        train_metrics = extract_train_metrics(model)

        # Evaluate
        workers = args.n_eval_workers
        print(f"\n  Evaluating ({args.n_eval_games} games, {workers} worker(s))...")
        eval_results = run_eval(model, encoder, character,
                                args.n_eval_games, args.ascension,
                                n_workers=workers)

        # Log
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "total_steps": current_steps,
            "milestone": milestone,
            "character": character,
            "checkpoint": ckpt_name,
            "train_metrics": train_metrics,
            "eval": {
                "n_games": eval_results["n_games"],
                "win_rate": eval_results["win_rate"],
                "avg_floor": eval_results["avg_floor"],
                "games": [
                    {"victory": g.get("victory"), "floor": g.get("floor"),
                     "hp": g.get("hp"), "max_hp": g.get("max_hp"),
                     "error": g.get("error")}
                    for g in eval_results["games"]
                ],
            },
        }
        with open(args.log_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Print summary
        print(f"\n  Milestone {milestone}k complete:")
        print(f"    Win rate: {eval_results['win_rate']:.1%} | "
              f"Avg floor: {eval_results['avg_floor']:.1f}")
        if train_metrics:
            parts = [f"{k}={v}" for k, v in train_metrics.items()]
            print(f"    Train: {', '.join(parts)}")

    vec_env.close()
    print(f"\n{'='*60}")
    print(f"Training complete. Log: {args.log_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
