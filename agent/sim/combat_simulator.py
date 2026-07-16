#!/usr/bin/env python3
"""combat_simulator.py — Run a full combat from CombatState to terminal.

Three pieces:

  simulate_combat(state, policy_fn, max_turns) — drives the play_card / end_turn
    loop until combat is over or max_turns reached. Returns an outcome dict.

  Policy hooks:
    - random_policy(state)   — pick any playable card / end turn
    - heuristic_policy(state) — greedy: prefer attacks if enemy is killable
                                or low HP, else block, else end turn
    - rl_policy(model, ...)  — wrap a PPO MaskablePPO model (Phase 3 wiring)

Action format produced by every policy_fn:
    {"kind": "play", "hand_idx": int, "target_idx": int}  OR
    {"kind": "end_turn"}

Phase 2 simulator is intentionally *single-combat*. Multi-floor rollout
(Phase 3) is a separate file that chains combats via run-level state.
"""
from __future__ import annotations

import random
from typing import Callable

from agent.sim.combat_state import CombatState
from agent.sim.combat_step import play_card, end_turn, get_card_data

Policy = Callable[[CombatState, random.Random], dict]


# ─── policies ──────────────────────────────────────────────────────────────
def random_policy(state: CombatState, rng: random.Random) -> dict:
    """Pick a random playable card; end turn if none affordable."""
    playable = []
    for i, cid in enumerate(state.hand):
        data = get_card_data(cid)
        if data is None:
            continue
        cost_raw = data.get("cost", "1")
        try:
            cost = int(cost_raw)
        except (ValueError, TypeError):
            cost = 1 if cost_raw != "X" else state.energy
        if cost <= state.energy:
            playable.append(i)
    if not playable:
        return {"kind": "end_turn"}
    return {"kind": "play",
            "hand_idx": rng.choice(playable),
            "target_idx": rng.randrange(len(state.enemies)) if state.enemies else 0}


def heuristic_policy(state: CombatState, rng: random.Random) -> dict:
    """Heuristic player: attack if lethal damage available; else block when
    incoming is high; else play any playable card.

    Crude but covers ~70% of basic-deck combats reasonably.
    (Jun 10 v2 rewrite tried Power-first + Vuln-prio + greedy-block; n=30 eval
    regressed 12.45→11.8, 4/30→1/30 boss-reach because Power-first wasted T1
    survival energy. Reverted to original logic.)"""
    # Build a quick cost+effect summary per hand slot
    candidates = []
    for i, cid in enumerate(state.hand):
        data = get_card_data(cid)
        if data is None:
            continue
        cost_raw = data.get("cost", "1")
        try:
            cost = int(cost_raw)
        except (ValueError, TypeError):
            cost = 1 if cost_raw != "X" else state.energy
        if cost > state.energy:
            continue
        is_attack = data.get("type") == "Attack"
        # Sum nominal damage / block from normal effects
        nom_damage = 0
        nom_block = 0
        for e in data["parsed"]["normal"]:
            if e["kind"] == "deal_damage": nom_damage += e["amount"]
            if e["kind"] == "deal_aoe":    nom_damage += e["amount"]
            if e["kind"] == "gain_block":  nom_block += e["amount"]
        candidates.append({"hand_idx": i, "cost": cost,
                           "attack": is_attack,
                           "damage": nom_damage, "block": nom_block})

    if not candidates:
        return {"kind": "end_turn"}

    # Estimate incoming damage from each enemy's intent
    incoming = 0
    for e in state.enemies:
        if e.hp <= 0:
            continue
        if e.intent.get("type") == "attack":
            incoming += e.intent.get("damage", 0) * e.intent.get("hits", 1)

    # If we can kill the front-row enemy, do it
    target_idx = 0
    if state.enemies:
        for i, e in enumerate(state.enemies):
            if e.hp > 0 and e.hp <= 6:  # weak enemy → focus
                target_idx = i
                break
        enemy_hp = state.enemies[target_idx].hp
    else:
        enemy_hp = 99

    # If high incoming and we have block, prioritise block
    if incoming > state.block + 6:
        block_cards = sorted([c for c in candidates if c["block"] > 0],
                              key=lambda x: -x["block"])
        if block_cards:
            return {"kind": "play", "hand_idx": block_cards[0]["hand_idx"],
                    "target_idx": target_idx}

    # Else prefer the highest-damage attack we can afford
    attack_cards = sorted([c for c in candidates if c["damage"] > 0],
                          key=lambda x: -x["damage"])
    if attack_cards:
        return {"kind": "play", "hand_idx": attack_cards[0]["hand_idx"],
                "target_idx": target_idx}

    # Else any playable card
    return {"kind": "play", "hand_idx": candidates[0]["hand_idx"],
            "target_idx": target_idx}


# ─── MCTS policy (Jun 10) ─────────────────────────────────────────────────
import copy as _copy
import os as _os_mcts


def _enumerate_actions(state: CombatState) -> list[dict]:
    """Return list of legal actions for current turn (each: {kind, hand_idx, target_idx})
    plus the end_turn action. Used by MCTS to enumerate candidates."""
    actions = []
    alive_targets = [i for i, e in enumerate(state.enemies) if e.hp > 0]
    if not alive_targets:
        return [{"kind": "end_turn"}]
    for i, cid in enumerate(state.hand):
        data = get_card_data(cid)
        if data is None:
            continue
        cost_raw = data.get("cost", "1")
        try:
            cost = int(cost_raw)
        except (ValueError, TypeError):
            cost = 1 if cost_raw != "X" else state.energy
        if cost > state.energy:
            continue
        # Only target one enemy for attacks to keep branch factor manageable
        is_attack = data.get("type") == "Attack"
        if is_attack and len(alive_targets) > 1:
            # Pick lowest-HP target + first Vuln-tagged target (max 2 options)
            tgts = [alive_targets[0]]
            vuln_t = next((t for t in alive_targets
                           if state.enemies[t].statuses.get("Vulnerable", 0) > 0), None)
            if vuln_t is not None and vuln_t not in tgts:
                tgts.append(vuln_t)
        else:
            tgts = [alive_targets[0]]
        for t in tgts:
            actions.append({"kind": "play", "hand_idx": i, "target_idx": t})
    actions.append({"kind": "end_turn"})
    return actions


def _evaluate_state(state: CombatState) -> float:
    """Value function: higher = better player position.
    Combines player HP (kept), enemy HP (drained), player block (free HP)."""
    if not state.alive():
        return -200.0
    enemy_hp_total = sum(max(0, e.hp) for e in state.enemies)
    enemy_max_total = sum(max(1, e.max_hp) for e in state.enemies)
    enemy_frac_alive = enemy_hp_total / max(enemy_max_total, 1)
    # Reward: keep HP up, drain enemy, modest block credit
    return (state.hp + 0.3 * state.block) - 60 * enemy_frac_alive


def mcts_policy(state: CombatState, rng: random.Random,
                 n_sims_per_action: int = 2, rollout_depth: int = 3) -> dict:
    """1-ply lookahead search: for each playable action, simulate N rollouts
    using heuristic_policy for the next `rollout_depth` turns, then evaluate.
    Pick action with highest expected value.

    Trade-off: ~5-15× slower than heuristic_policy per turn. Use only when
    MC predictions matter (during card_reward MC rollout, not real combat).

    Env knob: STS2_MCTS_DEPTH overrides rollout_depth, STS2_MCTS_SIMS overrides
    n_sims_per_action. STS2_USE_MCTS=1 enables policy throughout sim.
    """
    actions = _enumerate_actions(state)
    if len(actions) <= 1:
        return actions[0] if actions else {"kind": "end_turn"}

    depth = int(_os_mcts.environ.get("STS2_MCTS_DEPTH", rollout_depth))
    n_sims = int(_os_mcts.environ.get("STS2_MCTS_SIMS", n_sims_per_action))

    best_action = actions[0]
    best_value = -float("inf")
    for action in actions:
        values = []
        for _ in range(n_sims):
            s = _copy.deepcopy(state)
            try:
                if action["kind"] == "play":
                    from agent.sim.combat_step import play_card as _play, end_turn as _end
                    _play(s, action["hand_idx"], action.get("target_idx", 0), rng)
                else:
                    from agent.sim.combat_step import end_turn as _end
                    _end(s, rng)
                # Roll out remaining turns with heuristic for depth turns
                for _ in range(depth):
                    if s.combat_over():
                        break
                    sub = heuristic_policy(s, rng)
                    if sub["kind"] == "end_turn":
                        _end(s, rng)
                    else:
                        from agent.sim.combat_step import play_card as _play2
                        _play2(s, sub["hand_idx"], sub.get("target_idx", 0), rng)
            except Exception:
                pass
            values.append(_evaluate_state(s))
        avg_val = sum(values) / max(len(values), 1)
        if avg_val > best_value:
            best_value = avg_val
            best_action = action
    return best_action


# ─── main simulation loop ─────────────────────────────────────────────────
def simulate_combat(state: CombatState, policy: Policy = heuristic_policy,
                    max_turns: int = 40, max_steps: int = 1000,
                    rng: random.Random | None = None) -> dict:
    """Run combat to terminal. Returns dict:
        {
          "won":        bool,
          "alive":      bool,
          "turns":      int,
          "steps":      int,
          "final_hp":   int,
          "final_block":int,
          "enemy_hp":   list[int],
        }
    Mutates state.
    """
    rng = rng or random.Random(state.rng_seed)
    # Fire start-of-combat relic effects (Lantern +1 energy etc) BEFORE
    # the first hand is drawn — Anchor and similar block-gain relics also
    # want to set initial block while it's still safe.
    from agent.sim.relics import fire_relics
    fire_relics(state, "combat_start")
    # Start of combat: ensure hand drawn
    if not state.hand and state.draw_pile:
        state.draw(5, rng)
    steps = 0
    while not state.combat_over() and state.turn <= max_turns and steps < max_steps:
        action = policy(state, rng)
        steps += 1
        if action["kind"] == "end_turn":
            end_turn(state, rng)
        elif action["kind"] == "play":
            ok = play_card(state, action["hand_idx"],
                           action.get("target_idx", 0), rng)
            if not ok:
                # Couldn't play (no energy, unknown card) — end turn to avoid loop
                end_turn(state, rng)
        else:
            # Unknown action — terminate
            break
    # Combat ended — fire any combat_end relic effects on victory only
    # (loss handles HP at the room/run level, not here).
    if state.player_won():
        from agent.sim.relics import fire_relics
        fire_relics(state, "combat_end")
    return {
        "won":         state.player_won(),
        "alive":       state.alive(),
        "turns":       state.turn,
        "steps":       steps,
        "final_hp":    state.hp,
        "final_block": state.block,
        "enemy_hp":    [e.hp for e in state.enemies],
    }


# ─── sanity demo ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    from agent.sim.combat_state import Enemy

    # Standard starter combat — Ironclad vs Jaw Worm
    def make_starter():
        s = CombatState(hp=80, max_hp=80, energy=3, max_energy=3, rng_seed=0)
        # Default Ironclad deck: 5 Strike, 4 Defend, 1 Bash, 1 Ascender's Bane (skip)
        s.draw_pile = (["STRIKE_IRONCLAD"] * 5 +
                       ["DEFEND_IRONCLAD"] * 4 +
                       ["BASH"])
        rng = random.Random(0)
        rng.shuffle(s.draw_pile)
        s.enemies = [Enemy(id="JAW_WORM", name="Jaw Worm", hp=42, max_hp=42,
                            intent={"type": "attack", "damage": 11, "hits": 1})]
        return s

    print("=== Run 5 simulations of starter vs Jaw Worm (heuristic policy) ===")
    wins = 0
    floors = []
    for i in range(5):
        s = make_starter()
        s.rng_seed = i
        out = simulate_combat(s, heuristic_policy, max_turns=20)
        wins += 1 if out["won"] else 0
        print(f"  sim {i}: won={out['won']} turns={out['turns']} "
              f"hp={out['final_hp']} enemy_hp={out['enemy_hp']}")
    print(f"\nWin rate: {wins}/5")

    print("\n=== Same with random policy ===")
    wins = 0
    for i in range(5):
        s = make_starter()
        s.rng_seed = i
        out = simulate_combat(s, random_policy, max_turns=20)
        wins += 1 if out["won"] else 0
        print(f"  sim {i}: won={out['won']} turns={out['turns']} hp={out['final_hp']}")
    print(f"\nWin rate: {wins}/5")
