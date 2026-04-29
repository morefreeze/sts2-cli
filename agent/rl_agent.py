"""rl_agent.py — inference-only wrapper for trained MaskablePPO model."""
from sb3_contrib import MaskablePPO
from agent.state_encoder import StateEncoder
from agent.combat_env import CombatEnv
import numpy as np


class RLAgent:
    def __init__(self, checkpoint_path: str, cards_json: str):
        self.enc = StateEncoder(cards_json)
        self.model = MaskablePPO.load(checkpoint_path)
        # Auto-detect whether model expects extra obs (169-dim vs 161-dim)
        model_obs_size = self.model.observation_space.shape[0]
        self._extra_obs = model_obs_size > self.enc.obs_size
        self._extra_dim = model_obs_size - self.enc.obs_size

        # Runtime state for extra obs features
        self._current_floor = 1
        self._combat_entry_hp_ratio = 1.0

    def set_combat_context(self, floor: int, entry_hp_ratio: float):
        """Call before each combat to set floor and entry HP context for extra obs."""
        self._current_floor = floor
        self._combat_entry_hp_ratio = entry_hp_ratio

    def act(self, state: dict) -> dict:
        base_obs = self.enc.encode(state)
        if self._extra_obs:
            from agent.combat_env import _enemy_power_amount
            enemies = state.get("enemies", [])
            floor_norm = min(self._current_floor / 17.0, 1.0)
            extra = [floor_norm, self._combat_entry_hp_ratio]
            for slot in range(3):
                e = enemies[slot] if slot < len(enemies) else {}
                extra.append(min(_enemy_power_amount(e, "Vulnerable") / 10.0, 1.0))
                extra.append(min(_enemy_power_amount(e, "Weak") / 10.0, 1.0))
            obs = np.concatenate([base_obs, np.array(extra, dtype=np.float32)])
        else:
            obs = base_obs
        obs = obs.reshape(1, -1)
        mask = self.enc.action_mask(state).reshape(1, -1)
        action, _ = self.model.predict(obs, action_masks=mask, deterministic=True)
        return self.enc.decode(int(action[0]), state)
