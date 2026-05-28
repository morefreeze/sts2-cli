#!/usr/bin/env python3
"""validate_sim.py — Phase 2 sanity / coverage report.

Two checks:

  1. Card-effect coverage report:
     - For each card in the parsed DB, classify as
       FULL (no residual), PARTIAL (≥1 primary effect parsed), UNSUPPORTED.
     - Report per-rarity / per-type breakdown.

  2. End-to-end simulator smoke matrix:
     - Run 50 starter-vs-X combats with heuristic policy across a range of
       enemies pulled from data/enemies.json (5 distinct opponents).
     - Report win-rate + avg turns + avg final_hp per enemy.

This isn't a real-engine cross-check (Day 3 work) — just a confidence
report that the pipeline produces stable, plausible outcomes.
"""
import json
import os
import random
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_simulator import simulate_combat, heuristic_policy, random_policy


def coverage_report() -> None:
    with open("data/ironclad_cards_parsed.json") as f:
        data = json.load(f)
    cards = data["cards"]
    PRIMARY = {
        "deal_damage", "deal_aoe", "multi_hit", "gain_block", "apply_status",
        "gain_status", "draw", "gain_energy", "exhaust_self", "exhaust_cards",
        "exhaust_target", "add_copy", "scaling_damage", "lose_hp", "self_buff_combat",
        "double_status", "deal_damage_equal_to", "cost_mod",
        "on_turn_start", "on_turn_end", "on_lose_hp", "on_exhaust",
        "on_play_attack", "on_other_trigger", "if",
        "hits_modifier", "hits_scaling", "innate", "retain", "ethereal",
        "play_condition", "on_nth_play_add_copy", "move_card", "upgrade_cards",
        "play_top_card", "play_top_n",
    }

    def classify(parsed):
        if not parsed["normal"] and not parsed["upgraded"]:
            return "UNSUPPORTED"
        ok_n = any(e["kind"] in PRIMARY for e in parsed["normal"])
        ok_u = any(e["kind"] in PRIMARY for e in parsed["upgraded"])
        no_residual = not parsed["unparsed_normal"] and not parsed["unparsed_upgraded"]
        if ok_n and ok_u and no_residual:
            return "FULL"
        if ok_n or ok_u:
            return "PARTIAL"
        return "UNSUPPORTED"

    buckets = Counter()
    by_rarity = {}
    by_type = {}
    for c in cards:
        cls = classify(c["parsed"])
        buckets[cls] += 1
        rar = c.get("rarity", "?")
        typ = c.get("type", "?")
        by_rarity.setdefault(rar, Counter())[cls] += 1
        by_type.setdefault(typ, Counter())[cls] += 1

    total = len(cards)
    print(f"=== Card-effect coverage ({total} Ironclad cards) ===")
    for k in ("FULL", "PARTIAL", "UNSUPPORTED"):
        n = buckets[k]
        print(f"  {k:<12s}: {n}/{total} ({100*n/total:.1f}%)")
    actionable = buckets["FULL"] + buckets["PARTIAL"]
    print(f"  actionable (FULL+PARTIAL) → simulator can use: {actionable}/{total} "
          f"({100*actionable/total:.1f}%)")
    print()
    print("  By rarity:")
    for rar, c in sorted(by_rarity.items()):
        sub = "  ".join(f"{k}={c[k]}" for k in ("FULL", "PARTIAL", "UNSUPPORTED"))
        print(f"    {rar:<10s} {sub}")
    print()
    print("  By type:")
    for typ, c in sorted(by_type.items()):
        sub = "  ".join(f"{k}={c[k]}" for k in ("FULL", "PARTIAL", "UNSUPPORTED"))
        print(f"    {typ:<10s} {sub}")
    print()
    print("  UNSUPPORTED cards:")
    for c in cards:
        if classify(c["parsed"]) == "UNSUPPORTED":
            print(f"    {c['id']:<25s} {c.get('zh_name','?'):<10s} "
                  f"normal: {c.get('normal_text','')[:80]}")
    print()


def simulator_smoke_matrix() -> None:
    """Run the simulator against a handful of representative enemies from
    data/enemies.json and report win rate / steps / HP-lost stats."""
    with open("data/enemies.json") as f:
        data = json.load(f)
    enemies = data["enemies"]
    # Pick a representative spread: low-HP "Monster" → high-HP "Boss"
    test = [e for e in enemies if e.get("category") == "Monster"][:3]
    test += [e for e in enemies if e.get("category") == "Boss"][:2]

    print(f"=== Simulator smoke matrix (heuristic policy, n=50 per enemy) ===")
    print(f"  {'enemy':<22s} {'cat':<7s} {'HP':<6s} {'wins/50':<10s} {'avg_turns':<10s} {'avg_lost_hp':<10s}")
    for e in test:
        if not e.get("moves"):
            continue
        # Pick the first declared move; default damage=8 if not specified
        first_move = e["moves"][0]
        first_dmg = first_move.get("damage", 8)
        hp_raw = (e.get("hp_normal", "30") or "30").split("-")[0].strip()
        try:
            hp = int(hp_raw)
        except ValueError:
            hp = 30
        wins = 0
        turns_total = 0
        hp_lost_total = 0
        for seed in range(50):
            s = CombatState(hp=80, max_hp=80, energy=3, max_energy=3, rng_seed=seed)
            rng = random.Random(seed)
            s.draw_pile = (["STRIKE_IRONCLAD"] * 5 +
                            ["DEFEND_IRONCLAD"] * 4 +
                            ["BASH"])
            rng.shuffle(s.draw_pile)
            s.enemies = [Enemy(id=e["id"], name=e["zh_name"], hp=hp, max_hp=hp,
                                intent={"type": "attack", "damage": first_dmg, "hits": 1})]
            out = simulate_combat(s, heuristic_policy, max_turns=30)
            if out["won"]:
                wins += 1
            turns_total += out["turns"]
            hp_lost_total += 80 - out["final_hp"]
        print(f"  {e['zh_name']:<22s} {e.get('category','?'):<7s} {hp:<6d} "
              f"{wins}/50      {turns_total/50:.1f}        {hp_lost_total/50:.1f}")


if __name__ == "__main__":
    coverage_report()
    print()
    simulator_smoke_matrix()
