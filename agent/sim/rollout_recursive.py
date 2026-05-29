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
                    n_sims: int, seed: int = 0,
                    max_depth: int = 1) -> dict[str, float]:
    """Run n_sims rollouts of `max_depth` combats forward. Returns aggregate stats:
        {"win_rate", "avg_final_hp", "avg_turns", "avg_max_floor"}.

    With max_depth=1 (Phase 3 default) this is the single-combat MVP: predicts
    the immediate next room's outcome.

    With max_depth>1 (Phase 4) we chain combats:
      - if the player wins, advance one floor with a heuristic card-reward pick
        (best of 3 random cards from the Ironclad pool by score_card)
      - sample next floor's enemy and repeat
      - stop on death or after max_depth combats
    This captures long-term card value (a card that helps Act 1 but tanks
    Act 2 should now score lower than single-combat sim implied).
    """
    rng = random.Random(seed)
    wins = 0
    hps: list[int] = []
    turns: list[int] = []
    floors_reached: list[int] = []
    for i in range(n_sims):
        sim_seed = seed * 1000 + i
        sim_rng = random.Random(sim_seed)
        cur_deck = list(deck_ids)
        cur_hp = hp
        cur_floor = floor
        last_won = False
        last_turns = 0
        for d in range(max_depth):
            enemy = _sample_enemy(cur_floor, sim_rng)
            if enemy is None:
                break
            s = _make_combat_state(cur_deck, cur_hp, max_hp, cur_floor,
                                    enemy=enemy, seed=sim_seed + d)
            out = simulate_combat(s, heuristic_policy, max_turns=25,
                                   rng=random.Random(sim_seed + d * 7))
            last_turns = out["turns"]
            last_won = out["won"]
            if not out["won"]:
                cur_hp = 0
                break
            cur_hp = out["final_hp"]
            cur_floor += 1
            # Inter-combat: pick one new card via the same heuristic the
            # real agent uses, drawn from 3 random Ironclad cards.
            if d + 1 < max_depth:
                new_card = _heuristic_card_pick(cur_deck, sim_rng)
                if new_card:
                    cur_deck.append(new_card)
        if last_won and cur_hp > 0:
            wins += 1
        hps.append(cur_hp)
        turns.append(last_turns)
        floors_reached.append(cur_floor)
    return {
        "win_rate": wins / max(n_sims, 1),
        "avg_final_hp": sum(hps) / max(len(hps), 1),
        "avg_turns": sum(turns) / max(len(turns), 1),
        "avg_max_floor": sum(floors_reached) / max(len(floors_reached), 1),
        "n_sims": float(n_sims),
    }


# Cache the Ironclad card pool once for between-combat picks
_CARD_POOL_CACHE: list[str] | None = None


def _heuristic_card_pick(deck: list[str], rng: random.Random) -> str | None:
    """Approximate a card_reward pick by drawing 3 random Ironclad cards and
    selecting the best via the existing score_card heuristic (deck-aware)."""
    global _CARD_POOL_CACHE
    if _CARD_POOL_CACHE is None:
        try:
            import json as _json
            with open("data/ironclad_cards.json") as f:
                data = _json.load(f)
            # Only cards that look benign (we have cost+type, not Power that
            # need long-term context); we keep all but skip BROKEN_CARDS later.
            _CARD_POOL_CACHE = [
                c["id"].upper().replace("-", "_")
                for c in data["cards"]
                if c.get("rarity") != "Basic"  # exclude starter Strike/Defend
            ]
        except Exception:
            _CARD_POOL_CACHE = []
    if not _CARD_POOL_CACHE:
        return None
    try:
        from agent.card_scoring import score_card, BROKEN_CARDS
    except Exception:
        return rng.choice(_CARD_POOL_CACHE)
    picks = rng.sample(_CARD_POOL_CACHE, min(3, len(_CARD_POOL_CACHE)))
    # Score each option as a stub card dict
    best, best_score = None, -1.0
    for cid in picks:
        if cid in BROKEN_CARDS:
            continue
        stub = {"id": cid, "cost": 1, "rarity": "Common", "type": "Attack"}
        s = score_card(stub)
        if s > best_score:
            best, best_score = cid, s
    return best


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
    max_depth: int = 3,
) -> list[float]:
    """Per-candidate bonus, normalised so the set sums to 0.

    max_depth=3 (Phase 4 default) — chain 3 combats forward so each card's
    long-term contribution actually matters.
    max_depth=1 (Phase 3 fallback for cheap inference) — single-combat sim.
    """
    if not cards:
        return []
    base = rollout_outcome(deck_ids, hp, max_hp, floor, n_sims, seed, max_depth)
    base_s = _card_score(base) + base.get("avg_max_floor", floor)

    deltas: list[float] = []
    for i, c in enumerate(cards):
        new_deck = deck_ids + [_norm_id(c)]
        out = rollout_outcome(new_deck, hp, max_hp, floor, n_sims,
                              seed=seed + i + 1, max_depth=max_depth)
        candidate_s = _card_score(out) + out.get("avg_max_floor", floor)
        deltas.append(candidate_s - base_s)
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
