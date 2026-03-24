"""rl_agent.py — inference-only wrapper for trained MaskablePPO model."""
from sb3_contrib import MaskablePPO
from agent.state_encoder import StateEncoder
import numpy as np


class RLAgent:
    def __init__(self, checkpoint_path: str, cards_json: str):
        self.enc = StateEncoder(cards_json)
        self.model = MaskablePPO.load(checkpoint_path)

    def act(self, state: dict) -> dict:
        obs = self.enc.encode(state).reshape(1, -1)
        mask = self.enc.action_mask(state).reshape(1, -1)
        action, _ = self.model.predict(obs, action_masks=mask, deterministic=True)
        return self.enc.decode(int(action[0]), state)
