"""
state_encoder.py — converts STS2 game state JSON to numpy observation vector.

Observation layout (130 floats):
  [0]       energy / 3
  [1]       turn / 20 (capped)
  [2:82]    hand: 10 slots × 8 floats each
  [82:85]   player: hp_norm, block_norm, buff_count_norm
  [85:100]  enemies: 3 slots × 5 floats each
  [100:130] player buffs: top 30 buff magnitudes (alphabetical), normalized
"""
import json
import numpy as np

MAX_HAND = 10
MAX_ENEMIES = 3
MAX_BUFFS = 30
CARD_SLOT = 8
ENEMY_SLOT = 5
ACTION_SIZE = MAX_HAND * 4 + 1  # 41
NO_TARGET_SLOT = 3


class StateEncoder:
    def __init__(self, cards_json_path: str):
        with open(cards_json_path) as f:
            data = json.load(f)
        titles = sorted(set(
            k.rsplit(".", 1)[0] for k in data if k.endswith(".title")
        ))
        self.vocab = {card_id: i for i, card_id in enumerate(titles)}
        self.vocab_size = len(self.vocab)  # 605
        self.obs_size = 1 + 1 + MAX_HAND * CARD_SLOT + 3 + MAX_ENEMIES * ENEMY_SLOT + MAX_BUFFS  # 130

    def encode(self, state: dict) -> np.ndarray:
        obs = np.zeros(self.obs_size, dtype=np.float32)
        idx = 0

        obs[idx] = min(state.get("energy", 0) / 3.0, 1.0); idx += 1
        obs[idx] = min(state.get("round", 1) / 20.0, 1.0); idx += 1

        hand = state.get("hand", [])
        for slot in range(MAX_HAND):
            if slot < len(hand):
                c = hand[slot]
                card_id = c.get("id", {})
                if isinstance(card_id, dict):
                    card_id = card_id.get("en", "")
                ctype = (c.get("type") or "").lower()
                obs[idx]     = self.vocab.get(card_id, 0) / max(self.vocab_size, 1)
                obs[idx + 1] = min(c.get("cost", 0) / 3.0, 1.0)
                obs[idx + 2] = 1.0 if ctype == "attack" else 0.0
                obs[idx + 3] = 1.0 if ctype == "skill" else 0.0
                obs[idx + 4] = 1.0 if ctype == "power" else 0.0
                obs[idx + 5] = 1.0 if c.get("can_play") else 0.0
                obs[idx + 6] = 1.0 if (c.get("target_type") or "").lower() == "anyenemy" else 0.0
                obs[idx + 7] = 0.0  # not empty
            else:
                obs[idx + 7] = 1.0  # empty slot
            idx += CARD_SLOT

        player = state.get("player", {})
        max_hp = max(player.get("max_hp", 1), 1)
        obs[idx]     = player.get("hp", 0) / max_hp; idx += 1
        obs[idx]     = min(player.get("block", 0) / 30.0, 1.0); idx += 1
        obs[idx]     = min(len(player.get("buffs", [])) / 10.0, 1.0); idx += 1

        enemies = state.get("enemies", [])
        for slot in range(MAX_ENEMIES):
            if slot < len(enemies):
                e = enemies[slot]
                max_ehp = max(e.get("max_hp", e.get("hp", 1)), 1)
                intent = e.get("intent") or {}
                obs[idx]     = e.get("hp", 0) / max_ehp
                obs[idx + 1] = min(e.get("block", 0) / 30.0, 1.0)
                obs[idx + 2] = 1.0 if (intent.get("type") or "").lower() == "attack" else 0.0
                obs[idx + 3] = min((intent.get("damage", 0) * intent.get("times", 1)) / 50.0, 1.0)
                obs[idx + 4] = 0.0
            else:
                obs[idx + 4] = 1.0
            idx += ENEMY_SLOT

        buffs = sorted(
            player.get("buffs", []),
            key=lambda b: (b.get("name", {}).get("en", "") if isinstance(b.get("name"), dict) else str(b.get("name", "")))
        )
        for i in range(MAX_BUFFS):
            if i < len(buffs):
                obs[idx] = min(abs(buffs[i].get("amount", 1)) / 10.0, 1.0)
            idx += 1

        return obs

    def action_mask(self, state: dict) -> np.ndarray:
        mask = np.zeros(ACTION_SIZE, dtype=bool)
        mask[40] = True  # end_turn always valid

        hand = state.get("hand", [])
        n_enemies = len(state.get("enemies", []))

        for slot in range(min(len(hand), MAX_HAND)):
            c = hand[slot]
            if not c.get("can_play", False):
                continue
            needs_target = (c.get("target_type") or "").lower() == "anyenemy"
            base = slot * 4
            if needs_target:
                for j in range(n_enemies):
                    mask[base + j] = True
            else:
                mask[base + NO_TARGET_SLOT] = True

        return mask

    def decode(self, action_idx: int, state: dict) -> dict:
        if action_idx == 40:
            return {"cmd": "action", "action": "end_turn"}

        hand_slot = action_idx // 4
        target_slot = action_idx % 4
        card = state.get("hand", [])[hand_slot]
        args = {"card_index": card["index"]}
        if target_slot != NO_TARGET_SLOT:
            args["target_index"] = target_slot
        return {"cmd": "action", "action": "play_card", "args": args}
