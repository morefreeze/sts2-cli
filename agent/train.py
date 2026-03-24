#!/usr/bin/env python3
"""train.py — RL combat training.

Usage:
    python3 agent/train.py --character Ironclad --steps 100000
    python3 agent/train.py --character Ironclad --steps 500000 --checkpoint checkpoints/ppo_ironclad_100k.zip
"""
import argparse, os
import torch
from stable_baselines3.common.vec_env import SubprocVecEnv
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv

CARDS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "checkpoints")


def mask_fn(env):
    return env.action_masks()


def make_env(character: str, ascension: int, worker_id: int = 0):
    def _init():
        env = CombatEnv(character=character, ascension=ascension,
                        seed_prefix=f"w{worker_id}")
        return ActionMasker(env, mask_fn)
    return _init


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--character", default="Ironclad")
    parser.add_argument("--steps", type=int, default=100_000)
    parser.add_argument("--n-envs", type=int, default=4)
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--checkpoint", default=None)
    args = parser.parse_args()

    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Training on device: {device}")

    vec_env = SubprocVecEnv([make_env(args.character, args.ascension, i) for i in range(args.n_envs)])

    if args.checkpoint:
        model = MaskablePPO.load(args.checkpoint, env=vec_env, device=device)
    else:
        model = MaskablePPO("MlpPolicy", vec_env, verbose=1, device=device,
                            n_steps=512, batch_size=128, n_epochs=4,
                            learning_rate=3e-4, gamma=0.99, ent_coef=0.05,
                            vf_coef=0.5, max_grad_norm=0.5,
                            tensorboard_log=os.path.join(CHECKPOINT_DIR, "tb_logs"))

    save_interval = 25_000
    steps_done = 0
    while steps_done < args.steps:
        chunk = min(save_interval, args.steps - steps_done)
        model.learn(total_timesteps=chunk, reset_num_timesteps=(steps_done == 0))
        steps_done += chunk
        ckpt = os.path.join(CHECKPOINT_DIR, f"ppo_{args.character.lower()}_{steps_done // 1000}k.zip")
        model.save(ckpt)
        print(f"Checkpoint saved: {ckpt}")

    vec_env.close()
    print("Training complete.")


if __name__ == "__main__":
    main()
