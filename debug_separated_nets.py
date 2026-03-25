#!/usr/bin/env python3
"""Verify that separated pi/vf networks prevent entropy collapse
when 87% of buffer has single-option masks."""
import torch, numpy as np
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv
import gymnasium as gym
from gymnasium.spaces import Box, Discrete

class MixedMaskEnv(gym.Env):
    """Simulates STS2 combat: 87% single-option (end_turn), 13% multi-option."""
    def __init__(self):
        super().__init__()
        self.observation_space = Box(0, 1, shape=(130,), dtype=np.float32)
        self.action_space = Discrete(41)
        self._step = 0
        self._multi = True  # start with a real choice

    def reset(self, **kw):
        self._step = 0
        self._multi = True
        return np.random.rand(130).astype(np.float32), {}

    def step(self, action):
        self._step += 1
        # After a multi-option step, ~3 forced end_turns before next choice
        if self._multi:
            reward = 0.1 if action != 40 else -0.02
            self._multi = False
            self._forced_count = 3  # 3 forced steps before next choice
        else:
            reward = 0.0
            self._forced_count -= 1
            if self._forced_count <= 0:
                self._multi = True
        done = self._step >= 20
        return np.random.rand(130).astype(np.float32), reward, done, False, {}

    def action_masks(self):
        if self._multi:
            # Random 5-8 valid actions
            mask = np.zeros(41, dtype=bool)
            mask[40] = True  # end_turn always valid
            indices = np.random.choice(40, size=np.random.randint(4, 8), replace=False)
            mask[indices] = True
            return mask
        else:
            # Only end_turn
            mask = np.zeros(41, dtype=bool)
            mask[40] = True
            return mask

def mask_fn(env): return env.action_masks()

print("=== Test 1: SHARED network (default) ===", flush=True)
vec_env = DummyVecEnv([lambda: ActionMasker(MixedMaskEnv(), mask_fn)])
model = MaskablePPO('MlpPolicy', vec_env, verbose=0, device='cpu',
                     n_steps=256, batch_size=64, n_epochs=4,
                     learning_rate=3e-4, gamma=0.99, ent_coef=0.05)
for it in range(10):
    model.learn(total_timesteps=256, reset_num_timesteps=(it==0))
    obs = vec_env.reset()
    env = vec_env.envs[0]
    env.env._multi = True  # force multi-option state
    mask = env.action_masks()
    obs_t = torch.tensor(obs, dtype=torch.float32)
    mask_t = torch.tensor(mask.reshape(1,-1), dtype=torch.bool)
    with torch.no_grad():
        dist = model.policy.get_distribution(obs_t, action_masks=mask_t)
        ent = dist.entropy().item()
    print(f"  iter {it+1:2d}: entropy={ent:.4f} (valid={mask.sum()})", flush=True)
vec_env.close()

print("\n=== Test 2: SEPARATED pi/vf networks ===", flush=True)
vec_env = DummyVecEnv([lambda: ActionMasker(MixedMaskEnv(), mask_fn)])
model = MaskablePPO('MlpPolicy', vec_env, verbose=0, device='cpu',
                     policy_kwargs=dict(net_arch=dict(pi=[64,64], vf=[64,64])),
                     n_steps=256, batch_size=64, n_epochs=4,
                     learning_rate=3e-4, gamma=0.99, ent_coef=0.05)
for it in range(10):
    model.learn(total_timesteps=256, reset_num_timesteps=(it==0))
    obs = vec_env.reset()
    env = vec_env.envs[0]
    env.env._multi = True
    mask = env.action_masks()
    obs_t = torch.tensor(obs, dtype=torch.float32)
    mask_t = torch.tensor(mask.reshape(1,-1), dtype=torch.bool)
    with torch.no_grad():
        dist = model.policy.get_distribution(obs_t, action_masks=mask_t)
        ent = dist.entropy().item()
    print(f"  iter {it+1:2d}: entropy={ent:.4f} (valid={mask.sum()})", flush=True)
vec_env.close()

print("\nDone.", flush=True)
