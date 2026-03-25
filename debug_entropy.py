#!/usr/bin/env python3
"""Debug entropy collapse: test MaskablePPO with real STS2 observations in a fast replay env."""
import torch, numpy as np, sys
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from stable_baselines3.common.vec_env import DummyVecEnv
from agent.combat_env import CombatEnv
import gymnasium as gym
from gymnasium.spaces import Box, Discrete

print("Collecting real obs/mask data from game...", flush=True)
env = CombatEnv(character='Ironclad', seed_prefix='data3')
obs, _ = env.reset()
data = []
for _ in range(80):
    mask = env.action_masks()
    data.append((obs.copy(), mask.copy()))
    action = np.random.choice(np.where(mask)[0])
    obs, _, done, _, _ = env.step(action)
    if done:
        obs, _ = env.reset()
env.close()
print(f"Collected {len(data)} obs/mask pairs", flush=True)
print(f"Valid actions range: {min(m.sum() for _,m in data)}-{max(m.sum() for _,m in data)}", flush=True)

class ReplayEnv(gym.Env):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.observation_space = Box(0, 1, shape=(130,), dtype=np.float32)
        self.action_space = Discrete(41)
        self.idx = 0
        self._step = 0
    def reset(self, **kw):
        self.idx = np.random.randint(len(self.data))
        self._step = 0
        return self.data[self.idx][0], {}
    def step(self, action):
        self._step += 1
        self.idx = (self.idx + 1) % len(self.data)
        done = self._step >= 15
        reward = 0.1 if action != 40 else -0.05
        return self.data[self.idx][0], reward, done, False, {}
    def action_masks(self):
        return self.data[self.idx][1]

def mask_fn(env): return env.action_masks()
vec_env = DummyVecEnv([lambda: ActionMasker(ReplayEnv(data), mask_fn)])

print("\nTraining MaskablePPO with real STS2 obs...", flush=True)
model = MaskablePPO('MlpPolicy', vec_env, verbose=0, device='cpu',
                     n_steps=128, batch_size=32, n_epochs=4,
                     learning_rate=3e-4, gamma=0.99, ent_coef=0.05)

for it in range(15):
    model.learn(total_timesteps=128, reset_num_timesteps=(it==0))
    obs_check = vec_env.reset()
    mask_check = vec_env.env_method('action_masks')[0]
    obs_t = torch.tensor(obs_check, dtype=torch.float32)
    mask_t = torch.tensor(mask_check.reshape(1,-1), dtype=torch.bool)
    with torch.no_grad():
        dist = model.policy.get_distribution(obs_t, action_masks=mask_t)
        ent = dist.entropy().item()
        probs = dist.distribution.probs[0]
        valid_idx = np.where(mask_check)[0]
        max_p = probs[valid_idx].max().item()
    print(f"iter {it+1:2d}: entropy={ent:.4f}, valid={mask_check.sum()}, max_prob={max_p:.4f}", flush=True)

vec_env.close()
print("Done.", flush=True)
