#!/usr/bin/env python3
"""gen_boss_saves.py — Generate .save files at a target floor for boss-focused training.

Plays games with a saved checkpoint until the chosen floor is reached, then
sends `write_continue_save` to dump the engine state to disk. Repeats until
N usable saves are collected.

Usage:
    python agent/gen_boss_saves.py checkpoints/ppo_ironclad_3200k.zip \
           --target-floor 16 --n-saves 5 --out-dir saves/boss
"""
import argparse, os, random, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv, greedy_action
from agent.train import mask_fn


def _state_floor(state):
    return state.get("floor") or state.get("context", {}).get("floor", 0)


def _play_until_floor(env, env_wrapped, model, target_floor: int, min_hp_frac: float):
    """Play one game; return (reached_state, max_floor_seen) where reached_state
    is the engine state at decision-point on target_floor (combat_play, map_select,
    etc.) with HP >= min_hp_frac. None if not reached."""
    obs, _ = env_wrapped.reset()
    max_floor = 1
    reached_state = None

    # Walk through combats; between combats greedy_action handles map/event/rest etc.
    for _ in range(500):  # safety bound
        cur_state = env._current_state
        if cur_state is None:
            return None, max_floor

        cur_floor = _state_floor(cur_state) or 0
        max_floor = max(max_floor, cur_floor)

        # Did we just enter target floor at a non-combat decision? Save BEFORE combat.
        if cur_floor >= target_floor and cur_state.get("decision") != "game_over":
            player = cur_state.get("player", {})
            hp = player.get("hp", 0); mhp = max(player.get("max_hp", 80), 1)
            if hp / mhp >= min_hp_frac:
                return cur_state, max_floor

        if cur_state.get("decision") == "game_over":
            return None, max_floor

        if cur_state.get("decision") != "combat_play":
            # Should be handled by env._advance_to_combat in reset, but step
            # may also leave us mid-decision (treasure, card_select). Send a
            # greedy action through the raw pipe to skip past.
            cmd = greedy_action(cur_state)
            new_state = env._send(cmd)
            env._current_state = new_state
            continue

        # Combat — let the model play it out
        masks = env_wrapped.action_masks()
        action, _ = model.predict(obs, deterministic=True, action_masks=masks)
        obs, _r, terminated, truncated, info = env_wrapped.step(int(action))
        if terminated or truncated:
            if info.get("game_over"):
                return None, max_floor
            # Combat ended (won) — reset advances to next combat
            obs, info = env_wrapped.reset()
            if info.get("game_over"):
                return None, max_floor
    return None, max_floor


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--character", default="Ironclad")
    p.add_argument("--target-floor", type=int, default=16)
    p.add_argument("--min-hp-frac", type=float, default=0.5)
    p.add_argument("--n-saves", type=int, default=5)
    p.add_argument("--out-dir", default="saves/boss")
    p.add_argument("--max-attempts", type=int, default=40)
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Loading {args.checkpoint} on {device}...")
    model = MaskablePPO.load(args.checkpoint, device=device)
    model_obs_size = model.observation_space.shape[0]
    extra_obs = (model_obs_size > 161)

    saves_made = 0
    for attempt in range(1, args.max_attempts + 1):
        if saves_made >= args.n_saves:
            break
        seed = f"genboss_{random.randint(0, 0xFFFFFF):06x}"
        env = CombatEnv(character=args.character, seed=seed,
                        seed_prefix=f"gen_{attempt}", extra_obs=extra_obs)
        env_wrapped = ActionMasker(env, mask_fn)
        try:
            reached, max_floor = _play_until_floor(env, env_wrapped, model,
                                                   args.target_floor, args.min_hp_frac)
        except Exception as e:
            print(f"  attempt {attempt:2d}: error {e}")
            env_wrapped.close()
            continue

        if reached is None:
            print(f"  attempt {attempt:2d}: max_floor={max_floor} — not reached")
            env_wrapped.close()
            continue

        player = reached.get("player", {})
        hp = player.get("hp", 0); mhp = player.get("max_hp", 80)
        ts = time.strftime("%Y%m%d_%H%M%S")
        save_name = f"boss_fl{_state_floor(reached)}_hp{hp}_{seed}_{ts}.save"
        save_path = os.path.abspath(os.path.join(args.out_dir, save_name))
        # Engine is still alive — send write_continue_save directly.
        result = env._send({"cmd": "write_continue_save", "path": save_path})
        ok = result and result.get("success")
        if ok:
            saves_made += 1
            print(f"  attempt {attempt:2d}: SAVED fl={_state_floor(reached)} "
                  f"hp={hp}/{mhp} → {save_name}")
        else:
            err = (result or {}).get("message", "unknown error")
            print(f"  attempt {attempt:2d}: SAVE FAILED at fl={_state_floor(reached)}: {err}")
        env_wrapped.close()

    print(f"\nGenerated {saves_made}/{args.n_saves} saves in {args.out_dir}")
    return 0 if saves_made > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
