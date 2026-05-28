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

    Crude but covers ~70% of basic-deck combats reasonably."""
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
