"""rl_agent.py — inference-only wrapper for trained MaskablePPO model."""
from sb3_contrib import MaskablePPO
from agent.state_encoder import StateEncoder
from agent.combat_env import CombatEnv
from agent.turn_planner import (defense_override_enabled, intent_defense_override,
                                _action_is_defense)
import numpy as np


class RLAgent:
    def __init__(self, checkpoint_path: str, cards_json: str):
        self.enc = StateEncoder(cards_json)
        self.model = MaskablePPO.load(checkpoint_path)
        # Auto-detect obs mode from model width (161=base, 169=extra, 441=extra+relic)
        from agent.state_encoder import obs_flags_for_size
        model_obs_size = self.model.observation_space.shape[0]
        self._extra_obs, self._relic_obs = obs_flags_for_size(model_obs_size, self.enc.obs_size)
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
        if self._relic_obs:
            from agent.state_encoder import encode_relics
            from agent.combat_env import CombatEnv
            obs = np.concatenate([obs, encode_relics(CombatEnv._state_relic_ids(state))])
        obs = obs.reshape(1, -1)
        mask_1d = self.enc.action_mask(state)
        action, _ = self.model.predict(obs, action_masks=mask_1d.reshape(1, -1),
                                        deterministic=True)
        action_int = int(action[0])

        # Intent-aware defense override, mirroring eval_rl.py's eval loop so
        # live play (this wrapper) gets the same benefit as eval: when an
        # enemy telegraphs a dangerous attack and the policy isn't already
        # blocking, insert the best block/kill card instead. Gated on the
        # same STS2_DEFENSE default-on flag as eval_rl.py (see
        # turn_planner.defense_override_enabled) so the two paths can't
        # drift. intent_defense_override does a 1-D `masks[action]` lookup,
        # so it must get mask_1d, not the (1, N)-reshaped mask above.
        if defense_override_enabled() and state.get("decision") == "combat_play":
            try:
                if not _action_is_defense(state, action_int):
                    override = intent_defense_override(state, mask_1d)
                    if override is not None:
                        action_int = override
            except Exception:
                pass

        return self.enc.decode(action_int, state)
