#!/usr/bin/env python3
"""evaluate.py — Evaluate RL agent combat performance (HP retention).

Runs N combat episodes with a trained model and reports:
  - avg_hp_retention: average (hp_after / hp_before) across won combats
  - win_rate: fraction of combats won
  - avg_steps: average steps per combat

Usage:
    python3 agent/evaluate.py --checkpoint checkpoints/ppo_ironclad_500k.zip --episodes 20
"""
import argparse, json, os, sys, time
import numpy as np
from sb3_contrib import MaskablePPO
from agent.combat_env import CombatEnv
from agent.state_encoder import StateEncoder

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS_JSON = os.path.join(PROJECT_ROOT, "localization_eng", "cards.json")


def evaluate(checkpoint: str, character: str, ascension: int,
             episodes: int, seed_prefix: str = "eval") -> dict:
    ckpt_path = checkpoint
    if ckpt_path.endswith(".zip"):
        ckpt_path = ckpt_path[:-4]
    model = MaskablePPO.load(ckpt_path)
    env = CombatEnv(character=character, ascension=ascension,
                    seed_prefix=seed_prefix)

    wins = 0
    losses = 0
    hp_retentions = []
    all_steps = []
    total_reward = 0.0

    for ep in range(episodes):
        obs, info = env.reset()
        state = env._current_state
        if state is None:
            continue
        hp_before = state.get("player", {}).get("hp", 80)
        max_hp = state.get("player", {}).get("max_hp", 80)

        done = False
        ep_reward = 0.0
        steps = 0
        while not done:
            mask = env.action_masks()
            action, _ = model.predict(obs.reshape(1, -1),
                                      action_masks=mask.reshape(1, -1),
                                      deterministic=True)
            obs, reward, terminated, truncated, info = env.step(int(action[0]))
            ep_reward += reward
            steps += 1
            done = terminated or truncated

        total_reward += ep_reward
        all_steps.append(steps)

        if info.get("combat_won"):
            wins += 1
            hp_after = env._current_state.get("player", {}).get("hp", 0) if env._current_state else 0
            retention = hp_after / max(hp_before, 1)
            hp_retentions.append(retention)
        else:
            losses += 1
            hp_retentions.append(0.0)

    env.close()

    total = wins + losses
    win_rate = wins / max(total, 1)
    avg_hp_retention = float(np.mean(hp_retentions)) if hp_retentions else 0.0
    avg_steps = float(np.mean(all_steps)) if all_steps else 0.0
    avg_reward = total_reward / max(total, 1)

    return {
        "episodes": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "avg_hp_retention": avg_hp_retention,
        "avg_steps": avg_steps,
        "avg_reward": avg_reward,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--episodes", type=int, default=20)
    parser.add_argument("--seed-prefix", default="eval")
    args = parser.parse_args()

    print(f"Evaluating {args.checkpoint} | {args.character} | {args.episodes} episodes", flush=True)
    t0 = time.time()
    results = evaluate(args.checkpoint, args.character, args.ascension,
                       args.episodes, args.seed_prefix)
    elapsed = time.time() - t0

    print(f"episodes={results['episodes']}")
    print(f"wins={results['wins']}")
    print(f"losses={results['losses']}")
    print(f"win_rate={results['win_rate']:.4f}")
    print(f"avg_hp_retention={results['avg_hp_retention']:.4f}")
    print(f"avg_steps={results['avg_steps']:.1f}")
    print(f"avg_reward={results['avg_reward']:.4f}")
    print(f"eval_time={elapsed:.1f}s")


if __name__ == "__main__":
    main()
