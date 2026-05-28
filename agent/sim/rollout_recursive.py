#!/usr/bin/env python3
"""rollout_recursive.py — MC scoring for card_reward decisions via sim.

Phase 3 step 1: estimate "what happens if I take card X" by Monte-Carlo
simulating one or more downstream combats with the Phase 2 simulator.

Public API (matches the contract card_scoring.predictor_v2_set_bonuses
expects — list[float] per option, set-mean-normalised so absolute bias
cancels and only the ranking signal survives):

    score_candidates_via_rollout(
        cards: list[dict],          # the offered card_reward options
        deck_ids: list[str],        # current deck (normalised ids)
        hp: int, max_hp: int,       # current player HP
        floor: int,                 # current floor (Act-1/2/3 selects enemy pool)
        n_sims: int = 20,           # rollouts per candidate (per add candidate)
        seed: int = 0,              # for reproducibility
    ) -> list[float]                # bonus per card, sums to ~0

Each candidate is scored by:
  outcome(deck + candidate) - outcome(deck baseline), averaged over N rollouts,
  where outcome = win_rate × 80 + survive_bonus(final_hp).
Then set-mean-normalised within the choice set.

For Phase 3 MVP we only simulate ONE combat forward (the next room). Multi-
floor rollout (depth>1) requires sampling card_rewards / map decisions
between combats — TODO for v2 of this module.
"""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_simulator import simulate_combat, heuristic_policy
from agent.sim.combat_step import _load_enemy_db, get_card_data


# Enemy pools by act (rough mapping from data/enemies.json fields).
# Act 1 = The Overgrowth, Act 2 = The Underdocks (etc.) — we pick low-HP
# Monsters for Act-1 sampling, higher for late game.
_ACT1_KEYWORDS = ("Overgrowth", "Underdocks")
_BOSS_FLOOR = 17
_ACT2_FLOOR_START = 18


def _sample_enemy(floor: int, rng: random.Random) -> Enemy | None:
    """Pick a representative enemy for the next combat at this floor."""
    edb = _load_enemy_db()
    if not edb:
        return None
    candidates = []
    for slug, e in edb.items():
        cat = e.get("category", "Monster")
        # Boss room → only boss enemies
        if floor == _BOSS_FLOOR:
            if cat != "Boss":
                continue
        else:
            if cat == "Boss":
                continue
        if e.get("moves"):
            candidates.append(e)
    if not candidates:
        return None
    raw = rng.choice(candidates)
    first_move = raw["moves"][0]
    hp_raw = (raw.get("hp_normal", "30") or "30").split("-")[0].strip()
    try:
        hp = int(hp_raw)
    except ValueError:
        hp = 30
    # Sanity guard against the 9999-HP scrape glitch (伯德幼雏)
    if hp > 500:
        hp = 100
    return Enemy(
        id=raw["id"], name=raw["zh_name"], hp=hp, max_hp=hp,
        intent={
            "type": "attack",
            "damage": first_move.get("damage", 8),
            "hits": 1,
        },
    )


def _make_combat_state(deck_ids: list[str], hp: int, max_hp: int,
                       floor: int, energy: int = 3,
                       enemy: Enemy | None = None,
                       seed: int = 0) -> CombatState:
    """Build a CombatState seeded for combat from current run state."""
    s = CombatState(
        hp=hp, max_hp=max_hp, energy=energy, max_energy=energy,
        floor=floor, rng_seed=seed,
    )
    # Copy deck → draw pile (shuffled)
    s.draw_pile = list(deck_ids)
    random.Random(seed).shuffle(s.draw_pile)
    if enemy:
        s.enemies = [enemy]
    return s


def rollout_outcome(deck_ids: list[str], hp: int, max_hp: int, floor: int,
                    n_sims: int, seed: int = 0) -> dict[str, float]:
    """Run n_sims combats with this deck against random act-appropriate
    enemies. Returns aggregate stats:
        {"win_rate", "avg_final_hp", "avg_turns"}.
    """
    rng = random.Random(seed)
    wins = 0
    hps: list[int] = []
    turns: list[int] = []
    for i in range(n_sims):
        sim_seed = seed * 1000 + i
        enemy = _sample_enemy(floor, rng)
        if enemy is None:
            # No enemy data — treat as draw, no combat to simulate
            hps.append(hp)
            turns.append(0)
            continue
        s = _make_combat_state(deck_ids, hp, max_hp, floor,
                                enemy=enemy, seed=sim_seed)
        out = simulate_combat(s, heuristic_policy, max_turns=25, rng=random.Random(sim_seed))
        if out["won"]:
            wins += 1
            hps.append(out["final_hp"])
        else:
            hps.append(0)  # died
        turns.append(out["turns"])
    return {
        "win_rate": wins / max(n_sims, 1),
        "avg_final_hp": sum(hps) / max(len(hps), 1),
        "avg_turns": sum(turns) / max(len(turns), 1),
        "n_sims": float(n_sims),
    }


def _card_score(outcome: dict[str, float]) -> float:
    """Combine win_rate and final_hp into one scalar for ranking."""
    return outcome["win_rate"] * 80 + outcome["avg_final_hp"] * 0.5


def _norm_id(card: dict[str, Any]) -> str:
    cid = card.get("id", "?")
    if isinstance(cid, dict):
        cid = cid.get("en", str(cid))
    return str(cid).upper().replace("CARD.", "")


def score_candidates_via_rollout(
    cards: list[dict[str, Any]],
    deck_ids: list[str],
    hp: int,
    max_hp: int,
    floor: int,
    n_sims: int = 20,
    seed: int = 0,
) -> list[float]:
    """Per-candidate bonus, normalised so the set sums to 0.

    Workflow:
      baseline = rollout(deck)
      for each candidate c:
          delta = rollout(deck + c) - baseline
      return delta - mean(deltas)   # set-aware normalisation
    """
    if not cards:
        return []
    base = rollout_outcome(deck_ids, hp, max_hp, floor, n_sims, seed)
    base_s = _card_score(base)

    deltas: list[float] = []
    for i, c in enumerate(cards):
        new_deck = deck_ids + [_norm_id(c)]
        out = rollout_outcome(new_deck, hp, max_hp, floor, n_sims,
                              seed=seed + i + 1)
        deltas.append(_card_score(out) - base_s)
    mean = sum(deltas) / len(deltas)
    return [d - mean for d in deltas]


# ─── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Tiny smoke: starter deck at floor 5, score 3 candidates
    starter = ["STRIKE_IRONCLAD"] * 5 + ["DEFEND_IRONCLAD"] * 4 + ["BASH"]
    options = [
        {"id": "INFLAME"},        # +2 Strength power → should be highly scored
        {"id": "ANGER"},          # +6 dmg attack → moderate
        {"id": "BODY_SLAM"},      # Block-scaling attack
    ]
    print("=== MC rollout score for 3 candidates on starter deck ===")
    bonuses = score_candidates_via_rollout(options, starter, hp=70, max_hp=80,
                                            floor=5, n_sims=20, seed=42)
    for c, b in zip(options, bonuses):
        print(f"  {c['id']:<18s} bonus={b:+.3f}")
    print(f"  sum (should ~= 0):  {sum(bonuses):+.4f}")
