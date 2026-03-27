#!/usr/bin/env python3
"""train.py — RL combat training with progress display.

Usage:
    python3 agent/train.py --character Ironclad --steps 100000
    python3 agent/train.py --character Ironclad --steps 500000 --checkpoint checkpoints/ppo_ironclad_100k.zip
"""
import argparse, os, sys, time
import torch

if not sys.stdout.isatty():
    sys.stdout.reconfigure(line_buffering=True)
from stable_baselines3.common.vec_env import SubprocVecEnv
from stable_baselines3.common.callbacks import BaseCallback
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


class ProgressCallback(BaseCallback):
    """Real-time progress: steps/sec, ETA, training metrics."""

    def __init__(self, total_steps: int, n_envs: int, n_steps_per_iter: int):
        super().__init__()
        self.total_steps = total_steps
        self.n_envs = n_envs
        self.n_steps_per_iter = n_steps_per_iter
        self._iter_start = time.time()
        self._train_start = time.time()
        self._step_in_iter = 0
        self._global_steps = 0
        self._last_print = 0

    def _on_step(self) -> bool:
        self._step_in_iter += self.n_envs
        self._global_steps += self.n_envs
        now = time.time()
        if now - self._last_print >= 2.0:
            self._last_print = now
            elapsed = now - self._train_start
            sps = self._global_steps / max(elapsed, 1)
            remaining = (self.total_steps - self._global_steps) / max(sps, 0.1)
            pct = 100 * self._global_steps / self.total_steps
            iter_pct = 100 * self._step_in_iter / (self.n_steps_per_iter * self.n_envs)
            print(f"\r  [{pct:5.1f}%] {self._global_steps}/{self.total_steps} "
                  f"| {sps:.0f} steps/s | ETA {self._fmt_time(remaining)} "
                  f"| iter {iter_pct:.0f}%",
                  end="", flush=True)
        return True

    def _on_rollout_end(self):
        now = time.time()
        iter_time = now - self._iter_start
        print(f"\r  [{100*self._global_steps/self.total_steps:5.1f}%] "
              f"collected {self._step_in_iter} steps in {iter_time:.0f}s, training...",
              end="", flush=True)

    def _on_training_end(self):
        logger = self.model.logger
        ent = logger.name_to_value.get("train/entropy_loss", None)
        vl = logger.name_to_value.get("train/value_loss", None)
        ev = logger.name_to_value.get("train/explained_variance", None)
        parts = []
        if ent is not None:
            parts.append(f"ent={ent:.3f}")
        if vl is not None:
            parts.append(f"vl={vl:.4f}")
        if ev is not None:
            parts.append(f"ev={ev:.2f}")
        metrics = " | ".join(parts) if parts else ""
        elapsed = time.time() - self._train_start
        sps = self._global_steps / max(elapsed, 1)
        remaining = (self.total_steps - self._global_steps) / max(sps, 0.1)
        print(f"\r  [{100*self._global_steps/self.total_steps:5.1f}%] "
              f"{self._global_steps}/{self.total_steps} "
              f"| {sps:.0f} sps | ETA {self._fmt_time(remaining)} "
              f"| {metrics}              ", flush=True)
        self._step_in_iter = 0
        self._iter_start = time.time()

    @staticmethod
    def _fmt_time(seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds/60:.0f}m{seconds%60:.0f}s"
        else:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            return f"{h}h{m:02d}m"


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
    print(f"Training: {args.character} | {args.steps} steps | {args.n_envs} envs | device={device}")

    vec_env = SubprocVecEnv([make_env(args.character, args.ascension, i) for i in range(args.n_envs)])

    n_steps = 2048
    policy_kwargs = dict(net_arch=dict(pi=[64, 64], vf=[64, 64]))

    try:
        import tensorboard  # noqa: F401
        tb_log = os.path.join(CHECKPOINT_DIR, "tb_logs")
    except ImportError:
        tb_log = None

    if args.checkpoint:
        model = MaskablePPO.load(args.checkpoint, env=vec_env, device=device)
        model.tensorboard_log = tb_log
    else:
        model = MaskablePPO("MlpPolicy", vec_env, verbose=0, device=device,
                            policy_kwargs=policy_kwargs,
                            n_steps=n_steps, batch_size=256, n_epochs=4,
                            learning_rate=3e-4, gamma=0.99, ent_coef=0.05,
                            vf_coef=0.5, max_grad_norm=0.5,
                            tensorboard_log=tb_log)

    callback = ProgressCallback(args.steps, args.n_envs, n_steps)

    save_interval = 25_000
    steps_done = 0
    while steps_done < args.steps:
        chunk = min(save_interval, args.steps - steps_done)
        model.learn(total_timesteps=chunk, callback=callback,
                    reset_num_timesteps=(steps_done == 0))
        steps_done += chunk
        ckpt = os.path.join(CHECKPOINT_DIR, f"ppo_{args.character.lower()}_{steps_done // 1000}k.zip")
        model.save(ckpt)
        print(f"  Checkpoint: {ckpt}", flush=True)

    vec_env.close()
    total_time = time.time() - callback._train_start
    print(f"\nTraining complete. Total time: {ProgressCallback._fmt_time(total_time)}")


if __name__ == "__main__":
    main()
