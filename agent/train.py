#!/usr/bin/env python3
"""train.py — RL combat training targeting Act 1 Boss clear in 24h.

Key design decisions vs previous version:
  - n_steps 2048→512: 4x more gradient updates per hour (was ~2/h, now ~8/h)
  - step_penalty -0.01→-0.002: stop discouraging Defend cards
  - Floor bonus in win reward: incentivize progressing to boss (floor 17)
  - Curriculum: 10% phase1 (floor≤3), 40% phase2 (floor≤9), rest full game
  - BrokenPipeError recovery: restart envs and continue without losing steps
  - Periodic eval every 25k steps: report avg_floor, win_rate, combat_win_rate

Usage:
    python3 agent/train.py --steps 500000 --curriculum
    python3 agent/train.py --steps 500000 --checkpoint checkpoints/ppo_ironclad_75k.zip --curriculum
"""
import argparse, os, signal, time, numpy as np
import torch
import torch.nn as nn
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv
from stable_baselines3.common.callbacks import BaseCallback
from sb3_contrib import MaskablePPO

# Ensure all child processes die when this process exits (prevents orphaned dotnet procs)
os.setpgrp()  # become process group leader so kill(-pgid) kills all children

def _cleanup_and_exit(signum, frame):
    """On SIGTERM/SIGINT, kill all child processes in our process group first."""
    import signal as _signal
    try:
        os.killpg(os.getpgid(0), _signal.SIGTERM)
    except Exception:
        pass
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _cleanup_and_exit)
signal.signal(signal.SIGINT,  _cleanup_and_exit)
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv

CARDS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "localization_eng", "cards.json")
CHECKPOINT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "checkpoints")

# Run 8: pure fl≤∞ from start. Model has mastered fl≤9; all 400k steps target Act1 Boss.
CURRICULUM_SCHEDULE = [
    (0.00, 0),    # full game immediately — maximize fl≤∞ exposure for Act1 Boss clear
]


def mask_fn(env):
    return env.action_masks()


def _freeze_actor(model) -> None:
    """Set actor (policy) parameters to requires_grad=False.

    Must freeze features_extractor too — it is shared between actor and critic,
    so VF gradients (vf_coef=1.0) would otherwise update it and silently change
    actor output even with policy_net/action_net frozen.
    """
    for p in model.policy.features_extractor.parameters():
        p.requires_grad_(False)
    for p in model.policy.mlp_extractor.policy_net.parameters():
        p.requires_grad_(False)
    for p in model.policy.action_net.parameters():
        p.requires_grad_(False)


def _unfreeze_actor(model) -> None:
    """Restore actor parameters to requires_grad=True."""
    for p in model.policy.features_extractor.parameters():
        p.requires_grad_(True)
    for p in model.policy.mlp_extractor.policy_net.parameters():
        p.requires_grad_(True)
    for p in model.policy.action_net.parameters():
        p.requires_grad_(True)


def make_env(character: str, ascension: int, worker_id: int = 0, max_floor: int = 0):
    def _init():
        env = CombatEnv(character=character, ascension=ascension,
                        seed_prefix=f"w{worker_id}", max_floor=max_floor)
        return ActionMasker(env, mask_fn)
    return _init


def _curriculum_phase(progress: float) -> tuple[int, int]:
    """Return (max_floor, phase_index) for current training progress."""
    max_floor, phase = 0, 0
    for i, (frac, mf) in enumerate(CURRICULUM_SCHEDULE):
        if progress >= frac:
            max_floor, phase = mf, i
    return max_floor, phase


class TrainCallback(BaseCallback):
    """Progress display + curriculum updates + episode stats collection."""

    def __init__(self, total_steps: int, n_envs: int, n_steps_per_iter: int,
                 curriculum: bool = False,
                 normal_clip: float = 0.05, normal_vf_coef: float = 0.10):
        super().__init__()
        self.total_steps = total_steps
        self.n_envs = n_envs
        self.n_steps_per_iter = n_steps_per_iter
        self.curriculum = curriculum
        self._normal_clip = normal_clip
        self._normal_vf_coef = normal_vf_coef
        self._iter_start = time.time()
        self._train_start = time.time()
        self._step_in_iter = 0
        self._global_steps = 0
        self._last_print = 0
        self._last_curriculum_phase = -1
        self._chunk_count = 0
        # VF pre-training: freeze policy for N chunks after each floor transition
        # so VF can calibrate before policy gradient resumes. Prevents collapse
        # caused by VF predicting +2.0 for floor-N states when actual is -2.0.
        self._vf_pretrain_remaining = 0
        # Episode stats for recent window
        self._recent_floors = []
        self._recent_combat_wins = 0
        self._recent_episodes = 0
        self._recent_crashes = 0
        self._recent_timeouts = 0

    def _on_step(self) -> bool:
        self._step_in_iter += self.n_envs
        self._global_steps += self.n_envs

        # Collect episode info from infos
        infos = self.locals.get("infos", [])
        dones = self.locals.get("dones", [])
        for info, done in zip(infos, dones):
            if done:
                self._recent_episodes += 1
                floor = info.get("floor", 0)
                if floor:
                    self._recent_floors.append(floor)
                if info.get("combat_won"):
                    self._recent_combat_wins += 1
                if info.get("crashed") or info.get("timeout") or info.get("stuck"):
                    self._recent_crashes += 1
                if info.get("timeout"):
                    self._recent_timeouts += 1

        # Curriculum update
        if self.curriculum:
            progress = self._global_steps / max(self.total_steps, 1)
            max_floor, phase = _curriculum_phase(progress)
            if phase != self._last_curriculum_phase:
                self._last_curriculum_phase = phase
                self._apply_max_floor(max_floor)

        now = time.time()
        if now - self._last_print >= 3.0:
            self._last_print = now
            elapsed = now - self._train_start
            sps = self._global_steps / max(elapsed, 1)
            remaining = (self.total_steps - self._global_steps) / max(sps, 0.1)
            pct = 100 * self._global_steps / self.total_steps
            iter_pct = 100 * self._step_in_iter / (self.n_steps_per_iter * self.n_envs)
            extra = ""
            if self.curriculum:
                mf, _ = _curriculum_phase(self._global_steps / max(self.total_steps, 1))
                extra = f" | fl≤{mf or '∞'}"
            stats = ""
            if self._recent_episodes >= 5:
                avg_floor = np.mean(self._recent_floors) if self._recent_floors else 0
                cwr = self._recent_combat_wins / max(self._recent_episodes, 1)
                crash_pct = self._recent_crashes / max(self._recent_episodes, 1)
                timeout_pct = self._recent_timeouts / max(self._recent_episodes, 1)
                crash_str = f" cr={crash_pct:.0%}" if crash_pct > 0.02 else ""
                to_str = f" to={timeout_pct:.0%}" if timeout_pct > 0.01 else ""
                stats = f" | floor={avg_floor:.1f} cwr={cwr:.0%}{crash_str}{to_str}"
            print(f"\r  [{pct:5.1f}%] {self._global_steps}/{self.total_steps} "
                  f"| {sps:.0f} sps | ETA {_fmt_time(remaining)}"
                  f" | iter {iter_pct:.0f}%{extra}{stats}",
                  end="", flush=True)
        return True

    def _apply_max_floor(self, max_floor: int):
        floor_str = str(max_floor) if max_floor > 0 else "∞"
        try:
            self.training_env.env_method("set_max_floor", max_floor)
            print(f"\n  [curriculum] Phase → fl≤{floor_str}", flush=True)
        except Exception as e:
            print(f"\n  [curriculum] WARNING: _apply_max_floor failed: {e}", flush=True)

    def _on_rollout_end(self):
        now = time.time()
        iter_time = now - self._iter_start
        collected = self._step_in_iter
        vf_tag = f" [VF-only, {self._vf_pretrain_remaining} chunks left]" if self._vf_pretrain_remaining > 0 else ""
        print(f"\r  [{100*self._global_steps/self.total_steps:5.1f}%] "
              f"collected {collected} steps in {iter_time:.0f}s, training...{vf_tag}",
              end="", flush=True)

    def _on_training_end(self):
        self._chunk_count += 1
        # Count down VF pre-training; restore policy gradient when done
        if self._vf_pretrain_remaining > 0:
            self._vf_pretrain_remaining -= 1
            if self._vf_pretrain_remaining == 0:
                _unfreeze_actor(self.model)
                self.model.vf_coef       = self._normal_vf_coef
                self.model.clip_range_vf = lambda _: self._normal_clip
                print(f"\n  [curriculum] Actor unfrozen at chunk {self._chunk_count}; "
                      f"VF calibrated — fl≤6 deaths→adv≈0, wins→adv≈+4",
                      flush=True)
        logger = self.model.logger
        ent = logger.name_to_value.get("train/entropy_loss")
        vl  = logger.name_to_value.get("train/value_loss")
        ev  = logger.name_to_value.get("train/explained_variance")
        parts = []
        if ent is not None: parts.append(f"ent={ent:.3f}")
        if vl  is not None: parts.append(f"vl={vl:.4f}")
        if ev  is not None: parts.append(f"ev={ev:.2f}")
        metrics = " | ".join(parts)
        elapsed = time.time() - self._train_start
        sps = self._global_steps / max(elapsed, 1)
        remaining = (self.total_steps - self._global_steps) / max(sps, 0.1)
        stats = ""
        if self._recent_episodes >= 5:
            avg_floor = np.mean(self._recent_floors) if self._recent_floors else 0
            cwr = self._recent_combat_wins / max(self._recent_episodes, 1)
            crash_pct = self._recent_crashes / max(self._recent_episodes, 1)
            crash_str = f" cr={crash_pct:.0%}" if crash_pct > 0.02 else ""
            stats = f" | floor={avg_floor:.1f} cwr={cwr:.0%}{crash_str}"
        print(f"\r  [{100*self._global_steps/self.total_steps:5.1f}%] "
              f"{self._global_steps}/{self.total_steps} | {sps:.0f} sps"
              f" | ETA {_fmt_time(remaining)} | {metrics}{stats}              ",
              flush=True)
        self._step_in_iter = 0
        self._iter_start = time.time()
        # Reset rolling stats
        self._recent_floors.clear()
        self._recent_combat_wins = 0
        self._recent_episodes = 0
        self._recent_crashes = 0
        self._recent_timeouts = 0


def run_eval(model, character: str, n_games: int = 5) -> dict:
    """Run n_games full evaluation runs (multiple combats each). Returns stats dict."""
    floors, wins, combat_wins = [], [], []
    for i in range(n_games):
        # fixed_seed makes eval deterministic across checkpoints for fair comparison
        fixed_seed = f"eval_fixed_{i}"
        env = CombatEnv(character=character, seed=fixed_seed, seed_prefix=f"eval_{i}", max_floor=0)
        env_wrapped = ActionMasker(env, mask_fn)
        obs, _ = env_wrapped.reset()
        ep_combat_wins = 0
        max_floor = 1
        run_won = False
        run_over = False

        while not run_over:
            done = False
            last_info = {}
            while not done:
                masks = env_wrapped.action_masks()
                action, _ = model.predict(obs, deterministic=True, action_masks=masks)
                obs, _r, terminated, truncated, last_info = env_wrapped.step(int(action))
                done = terminated or truncated
                f = last_info.get("floor", 0)
                if f:
                    max_floor = max(max_floor, f)

            if last_info.get("combat_won"):
                ep_combat_wins += 1
                # Advance to the next combat via reset(); CombatEnv resumes the run
                obs, reset_info = env_wrapped.reset()
                if reset_info.get("game_over"):
                    run_over = True
                    last_info = reset_info
                    if reset_info.get("victory"):
                        run_won = True
            else:
                # game_over, crash, timeout, stuck — run ended
                run_over = True
                if last_info.get("victory"):
                    run_won = True

        env_wrapped.close()
        floors.append(max_floor)
        combat_wins.append(ep_combat_wins)
        wins.append(1 if run_won else 0)
    return {
        "avg_floor": float(np.mean(floors)),
        "max_floor": int(max(floors)),
        "win_rate": float(np.mean(wins)),
        "avg_combat_wins": float(np.mean(combat_wins)),
        "n": n_games,
    }


def _fmt_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.0f}m{int(seconds)%60:02d}s"
    else:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m"


def _reinit_value_network(model) -> None:
    """Reset value network weights after loading a checkpoint.

    The loaded policy weights are preserved (learned combat knowledge) but the
    value function is discarded — it was calibrated to a different reward scale
    and causes corrupted GAE advantages if kept, which collapses the policy
    within the first 1-2 PPO updates.

    Uses xavier_uniform on CPU to avoid MPS linalg_qr limitation.
    """
    policy = model.policy

    def _reinit_linear(layer: nn.Linear, gain: float = 1.0) -> None:
        device = layer.weight.device
        with torch.no_grad():
            w = layer.weight.data.cpu()
            nn.init.xavier_uniform_(w, gain=gain)
            layer.weight.data.copy_(w.to(device))
            layer.bias.data.zero_()

    try:
        for layer in policy.mlp_extractor.value_net:
            if isinstance(layer, nn.Linear):
                _reinit_linear(layer, gain=np.sqrt(2))
        if hasattr(policy, 'value_net') and isinstance(policy.value_net, nn.Linear):
            _reinit_linear(policy.value_net, gain=1.0)
        print("  Value network reinitialized (stale value weights discarded)")
    except Exception as e:
        print(f"  Warning: could not reinit value network: {e}")


def _expand_obs_checkpoint(checkpoint_path: str, vec_env, device: str,
                           old_obs_size: int, hyperparams: dict) -> "MaskablePPO":
    """Load a checkpoint with smaller obs_size and expand its input layers.

    Expands the first linear layer of both policy_net and value_net from
    old_obs_size → current obs_size by zero-padding the new feature columns.
    All other weights are copied unchanged. Value network is always reinit'd
    since the reward scale typically changes when obs_size changes.
    """
    new_obs_size = vec_env.observation_space.shape[0]
    # Load old model without env to bypass obs_space mismatch check
    old_model = MaskablePPO.load(checkpoint_path, env=None, device=device)
    old_ts = getattr(old_model, "num_timesteps", 0)

    # Create new model with correct obs size
    policy_kwargs = dict(net_arch=dict(pi=[256, 256], vf=[256, 256]))
    new_model = MaskablePPO(
        "MlpPolicy", vec_env, verbose=0, device=device,
        policy_kwargs=policy_kwargs, **hyperparams
    )

    # Copy weights, expanding input layers
    old_sd = old_model.policy.state_dict()
    new_sd = new_model.policy.state_dict()
    expanded = []
    for name in list(new_sd.keys()):
        if name not in old_sd:
            continue
        old_p, new_p = old_sd[name], new_sd[name]
        if old_p.shape == new_p.shape:
            new_sd[name] = old_p.clone()
        elif old_p.dim() == 2 and old_p.shape[1] == old_obs_size and new_p.shape[1] == new_obs_size:
            new_sd[name].zero_()
            new_sd[name][:, :old_obs_size] = old_p
            expanded.append(f"{name} {list(old_p.shape)}→{list(new_p.shape)}")
        # shapes differ for other reason — keep random init for that layer

    new_model.policy.load_state_dict(new_sd)
    new_model.num_timesteps = old_ts
    print(f"  Obs expanded {old_obs_size}→{new_obs_size}, layers: {expanded}")
    _reinit_value_network(new_model)  # reward scale changes with obs change
    return new_model


def _make_vec_env(character: str, ascension: int, n_envs: int,
                  max_floor: int, seed_offset: int = 0) -> SubprocVecEnv | DummyVecEnv:
    makers = [make_env(character, ascension, i + seed_offset, max_floor=max_floor)
              for i in range(n_envs)]
    return SubprocVecEnv(makers) if n_envs > 1 else DummyVecEnv(makers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--character",   default="Ironclad")
    parser.add_argument("--steps",       type=int, default=500_000)
    parser.add_argument("--n-envs",      type=int, default=4)
    parser.add_argument("--ascension",   type=int, default=0)
    parser.add_argument("--checkpoint",  default=None)
    parser.add_argument("--reinit-value", action="store_true",
                        help="Reinitialize value network when loading checkpoint (use when reward scale changes)")
    parser.add_argument("--obs-expand", type=int, default=0,
                        help="Old obs_size of checkpoint to expand from (e.g. 161 for 161→169 expansion)")
    parser.add_argument("--curriculum",  action="store_true")
    parser.add_argument("--eval-freq",   type=int, default=50_000,
                        help="Run full eval every N steps (0=disable)")
    args = parser.parse_args()

    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"

    # Hyperparameters
    N_STEPS    = 512    # was 2048 — 4x more gradient updates/hour
    BATCH_SIZE = 128    # was 256
    N_EPOCHS   = 1      # was 2 — halves per-chunk VF gradient updates, prevents fl≤6 ev overshoot
    LR         = 2e-5   # restored: 3e-5 caused cr 0%→8% and floor collapse after first update
    ENT_COEF   = 0.08   # Run10: 0.10→0.08 slight reduction for more exploitation at floor 15+
    GAMMA      = 0.99
    CLIP_RANGE = 0.05   # was 0.1 — tighter clipping limits per-update policy change
    VF_COEF    = 0.10   # was 0.25 — much slower VF learning to prevent ev overshoot on floor transitions

    initial_floor = CURRICULUM_SCHEDULE[0][1] if args.curriculum else 0
    suffix = " + curriculum" if args.curriculum else ""
    print(f"Training: {args.character} | {args.steps} steps | {args.n_envs} envs"
          f" | device={device}{suffix}")
    print(f"  n_steps={N_STEPS} batch={BATCH_SIZE} lr={LR} ent={ENT_COEF} "
          f"hp_penalty=-0.50 floor_bonus=0.10×floor(cap1.5) hp_curve=quadratic")

    vec_env = _make_vec_env(args.character, args.ascension, args.n_envs, initial_floor)

    policy_kwargs = dict(net_arch=dict(pi=[256, 256], vf=[256, 256]))

    if args.checkpoint:
        print(f"  Loading checkpoint: {args.checkpoint}")
        tb_log = os.path.join(CHECKPOINT_DIR, "tb_logs")
        _hp = dict(n_steps=N_STEPS, batch_size=BATCH_SIZE, n_epochs=N_EPOCHS,
                   learning_rate=LR, gamma=GAMMA, ent_coef=ENT_COEF,
                   clip_range=CLIP_RANGE, vf_coef=VF_COEF, max_grad_norm=0.5,
                   tensorboard_log=tb_log)
        if args.obs_expand:
            model = _expand_obs_checkpoint(
                args.checkpoint, vec_env, device, args.obs_expand, _hp)
        else:
            model = MaskablePPO.load(
                args.checkpoint, env=vec_env, device=device,
                custom_objects={"tensorboard_log": tb_log},
            )
            # Override hyperparameters that affect learning speed/quality
            model.n_steps        = N_STEPS
            model.batch_size     = BATCH_SIZE
            model.n_epochs       = N_EPOCHS
            model.ent_coef       = ENT_COEF
            model.vf_coef        = VF_COEF
            model.learning_rate  = LR
            model.clip_range     = lambda _: CLIP_RANGE
            model.clip_range_vf  = lambda _: CLIP_RANGE
            if args.reinit_value:
                _reinit_value_network(model)
        # Recreate rollout buffer with new n_steps
        try:
            from sb3_contrib.common.maskable.buffers import MaskableRolloutBuffer
            model.rollout_buffer = MaskableRolloutBuffer(
                N_STEPS, model.observation_space, model.action_space,
                device=model.device, gamma=model.gamma,
                gae_lambda=model.gae_lambda, n_envs=model.n_envs,
            )
            print(f"  Rollout buffer recreated: {N_STEPS} steps × {model.n_envs} envs")
        except Exception as e:
            print(f"  Warning: could not recreate rollout buffer: {e}")
    else:
        model = MaskablePPO(
            "MlpPolicy", vec_env, verbose=0, device=device,
            policy_kwargs=policy_kwargs,
            n_steps=N_STEPS, batch_size=BATCH_SIZE, n_epochs=N_EPOCHS,
            learning_rate=LR, gamma=GAMMA, ent_coef=ENT_COEF,
            clip_range=CLIP_RANGE, vf_coef=VF_COEF, max_grad_norm=0.5,
            tensorboard_log=os.path.join(CHECKPOINT_DIR, "tb_logs"),
        )

    callback = TrainCallback(args.steps, args.n_envs, N_STEPS,
                             curriculum=args.curriculum,
                             normal_clip=CLIP_RANGE,
                             normal_vf_coef=VF_COEF)

    save_interval = 25_000
    eval_freq     = args.eval_freq
    steps_done    = 0
    last_eval_at  = 0
    # Checkpoint names include steps from prior runs so we never overwrite old files
    checkpoint_base = int(getattr(model, "num_timesteps", 0) or 0)

    while steps_done < args.steps:
        chunk = min(save_interval, args.steps - steps_done)
        try:
            model.learn(
                total_timesteps=chunk, callback=callback,
                reset_num_timesteps=(steps_done == 0 and not args.checkpoint),
            )
        except BrokenPipeError:
            print(f"\n  [warn] BrokenPipeError at {steps_done} steps — restarting envs...",
                  flush=True)
            try:
                vec_env.close()
            except Exception:
                pass
            # Progress curriculum to current phase before restarting
            progress = steps_done / max(args.steps, 1)
            floor, _ = _curriculum_phase(progress) if args.curriculum else (initial_floor, 0)
            vec_env = _make_vec_env(args.character, args.ascension, args.n_envs,
                                    floor, seed_offset=steps_done // 1000)
            model.set_env(vec_env)
            callback = TrainCallback(args.steps, args.n_envs, N_STEPS,
                                     curriculum=args.curriculum)
            callback._global_steps = steps_done
            callback._train_start  = time.time() - steps_done / 5.0  # approx
            continue

        steps_done += chunk
        ckpt = os.path.join(CHECKPOINT_DIR,
                            f"ppo_{args.character.lower()}_{(checkpoint_base + steps_done) // 1000}k.zip")
        model.save(ckpt)
        print(f"\n  [save] {ckpt}", flush=True)

        # Periodic evaluation
        if eval_freq > 0 and steps_done - last_eval_at >= eval_freq:
            last_eval_at = steps_done
            print(f"  [eval] Running {5} games...", flush=True)
            try:
                stats = run_eval(model, args.character, n_games=5)
                print(f"  [eval] avg_floor={stats['avg_floor']:.1f} "
                      f"max_floor={stats['max_floor']} "
                      f"win_rate={stats['win_rate']:.0%} "
                      f"avg_combats={stats['avg_combat_wins']:.1f}", flush=True)
                # Log to tensorboard
                if hasattr(model, 'logger') and model.logger:
                    model.logger.record("eval/avg_floor",      stats["avg_floor"])
                    model.logger.record("eval/max_floor",      stats["max_floor"])
                    model.logger.record("eval/win_rate",       stats["win_rate"])
                    model.logger.record("eval/avg_combat_wins",stats["avg_combat_wins"])
                    model.logger.dump(steps_done)
            except Exception as e:
                print(f"  [eval] Error: {e}", flush=True)

    vec_env.close()
    total_time = time.time() - callback._train_start
    print(f"\nTraining complete. Total time: {_fmt_time(total_time)}")

    # Final evaluation
    print(f"\nFinal evaluation ({10} games)...")
    try:
        stats = run_eval(model, args.character, n_games=10)
        print(f"  avg_floor={stats['avg_floor']:.1f} max_floor={stats['max_floor']} "
              f"win_rate={stats['win_rate']:.0%} avg_combats={stats['avg_combat_wins']:.1f}")
    except Exception as e:
        print(f"  Error: {e}")


if __name__ == "__main__":
    main()
