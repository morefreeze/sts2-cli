#!/usr/bin/env python3
"""Debug: check episode lengths and termination causes in CombatEnv."""
import numpy as np
from agent.combat_env import CombatEnv

env = CombatEnv(character='Ironclad', seed_prefix='ep')
episodes = []

for ep in range(20):
    obs, _ = env.reset()
    ep_len = 0
    ep_reward = 0
    for step in range(200):
        mask = env.action_masks()
        action = np.random.choice(np.where(mask)[0])
        obs, reward, done, _, info = env.step(action)
        ep_len += 1
        ep_reward += reward
        if done:
            episodes.append({
                'len': ep_len, 'reward': ep_reward,
                'won': info.get('combat_won', False),
                'crashed': info.get('crashed', False),
            })
            print(f"Episode {ep+1}: len={ep_len}, reward={ep_reward:.4f}, "
                  f"won={info.get('combat_won', False)}, crashed={info.get('crashed', False)}", flush=True)
            break
    else:
        episodes.append({'len': ep_len, 'reward': ep_reward, 'won': False, 'crashed': False})
        print(f"Episode {ep+1}: len={ep_len}, reward={ep_reward:.4f}, TRUNCATED", flush=True)

env.close()
print(f"\nSummary: {len(episodes)} episodes")
lens = [e['len'] for e in episodes]
print(f"  Lengths: min={min(lens)}, max={max(lens)}, mean={np.mean(lens):.1f}")
wins = sum(1 for e in episodes if e['won'])
crashes = sum(1 for e in episodes if e['crashed'])
print(f"  Wins: {wins}, Crashes: {crashes}, Losses: {len(episodes)-wins-crashes}")
