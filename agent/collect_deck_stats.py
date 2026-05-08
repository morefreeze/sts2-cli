#!/usr/bin/env python3
"""collect_deck_stats.py — Empirical card scoring from gameplay data.

Plays N games with a saved checkpoint, records the deck at end-of-game
(per game), and emits per-card frequency statistics partitioned by
final-floor reached. Cards that appear more often in boss-reach decks
than early-death decks are flagged as empirically positive — those bonuses
are written to data/card_empirical.json for use in card_scoring.py.

Usage:
    python agent/collect_deck_stats.py checkpoints/ppo_ironclad_3706k.zip --n-games 30
"""
import argparse, json, os, random, signal, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import torch
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv
from agent.train import mask_fn
from agent.card_scoring import _card_id_norm

_GAME_TIMEOUT = 300


def _timeout_handler(signum, frame):
    raise TimeoutError()


def play_one_game(model, character: str, seed: str, extra_obs: bool):
    env = CombatEnv(character=character, seed=seed,
                    seed_prefix=f"stats_{seed}", extra_obs=extra_obs)
    env_w = ActionMasker(env, mask_fn)
    obs, _ = env_w.reset()
    max_floor = 1
    final_deck = []
    timed_out = False
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(_GAME_TIMEOUT)
    try:
        while True:
            done = False
            while not done:
                masks = env_w.action_masks()
                action, _ = model.predict(obs, deterministic=True, action_masks=masks)
                obs, _, term, trunc, info = env_w.step(int(action))
                done = term or trunc
                f = info.get("floor", 0)
                if f: max_floor = max(max_floor, f)
            # Capture deck snapshot at every combat-end (game over → final, otherwise updates)
            if env._current_state:
                deck = env._current_state.get("player", {}).get("deck") or []
                if deck:
                    final_deck = list(deck)
            if info.get("game_over"):
                break
            obs, info = env_w.reset()
            if info.get("game_over"):
                break
    except TimeoutError:
        timed_out = True
    finally:
        signal.alarm(0)
    env_w.close()
    return {"max_floor": max_floor, "deck": final_deck, "timed_out": timed_out,
            "deck_size": len(final_deck)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--character", default="Ironclad")
    p.add_argument("--n-games", type=int, default=0,
                   help="Total games (default 0 = use --target-boss-reach instead)")
    p.add_argument("--target-boss-reach", type=int, default=100,
                   help="Keep playing until this many boss-reach games collected")
    p.add_argument("--max-games", type=int, default=400,
                   help="Hard cap on games when targeting boss-reach")
    p.add_argument("--out", default="data/deck_stats.json")
    p.add_argument("--scores-out", default="data/card_empirical.json")
    p.add_argument("--boss-floor", type=int, default=14,
                   help="Decks reaching ≥ this floor count as 'boss-reach' (positive sample)")
    args = p.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    device = "cpu"  # CPU faster than MPS for small policy inference
    print(f"Loading {args.checkpoint} on {device}...")
    model = MaskablePPO.load(args.checkpoint, device=device)
    extra_obs = (model.observation_space.shape[0] > 161)

    games = []
    t_start = time.time()
    if args.n_games > 0:
        # Fixed N games
        for i in range(args.n_games):
            seed = f"stats_{random.randint(0, 0xFFFFFF):06x}_{i}"
            result = play_one_game(model, args.character, seed, extra_obs)
            games.append(result)
            marker = "★" if result["max_floor"] >= args.boss_floor else "·"
            print(f"  [{i+1:3d}/{args.n_games}] {marker} fl={result['max_floor']:2d} "
                  f"deck_size={result['deck_size']:2d}", flush=True)
    else:
        # Run until N boss-reach decks collected (or hard cap hit)
        n_boss = 0
        i = 0
        while n_boss < args.target_boss_reach and i < args.max_games:
            seed = f"stats_{random.randint(0, 0xFFFFFF):06x}_{i}"
            result = play_one_game(model, args.character, seed, extra_obs)
            games.append(result)
            i += 1
            if result["max_floor"] >= args.boss_floor:
                n_boss += 1
            marker = "★" if result["max_floor"] >= args.boss_floor else "·"
            print(f"  [{i:3d}, ★={n_boss:3d}/{args.target_boss_reach}] {marker} "
                  f"fl={result['max_floor']:2d} deck_size={result['deck_size']:2d}", flush=True)

    dt = time.time() - t_start
    print(f"\nCollected {len(games)} games in {dt:.0f}s")

    # Aggregate
    boss_decks = [g for g in games if g["max_floor"] >= args.boss_floor]
    early_decks = [g for g in games if g["max_floor"] < args.boss_floor]
    print(f"Boss-reach (≥fl{args.boss_floor}): {len(boss_decks)}/{args.n_games}")
    print(f"Early-died: {len(early_decks)}/{args.n_games}")

    if not boss_decks:
        print("No boss-reaching games — nothing to derive empirical scores from.")
        with open(args.out, "w") as f:
            json.dump({"games": games}, f, indent=2)
        return 1

    def card_freq(decks: list) -> dict:
        """count of (card occurrences) per card_id, divided by # decks."""
        counts = {}
        for g in decks:
            for c in g["deck"]:
                cid = _card_id_norm(c)
                counts[cid] = counts.get(cid, 0) + 1
        return {cid: n / len(decks) for cid, n in counts.items()}

    boss_freq = card_freq(boss_decks)
    early_freq = card_freq(early_decks) if early_decks else {}
    all_cards = set(boss_freq) | set(early_freq)

    # Empirical bonus per card: log-ratio of boss-reach freq vs early-died freq.
    # Cards appearing 2x more in boss-reach decks → positive bonus.
    # Cap at ±1.5 to avoid runaway adjustments from small sample sizes.
    import math
    empirical = {}
    for cid in all_cards:
        bf = boss_freq.get(cid, 0.0) + 0.05  # smoothing
        ef = early_freq.get(cid, 0.0) + 0.05
        # bonus is +1 per doubling toward boss, capped
        bonus = max(-1.5, min(math.log2(bf / ef), 1.5))
        empirical[cid] = round(bonus, 2)

    # Top winners and losers
    ranked = sorted(empirical.items(), key=lambda x: -x[1])
    print(f"\n=== Top 15 empirical winners (boss-reach favorites) ===")
    for cid, bonus in ranked[:15]:
        print(f"  {cid:30s} bonus={bonus:+.2f} boss_freq={boss_freq.get(cid,0):.2f} early_freq={early_freq.get(cid,0):.2f}")
    print(f"\n=== Bottom 10 empirical losers ===")
    for cid, bonus in ranked[-10:]:
        print(f"  {cid:30s} bonus={bonus:+.2f} boss_freq={boss_freq.get(cid,0):.2f} early_freq={early_freq.get(cid,0):.2f}")

    # Save raw and bonuses
    with open(args.out, "w") as f:
        json.dump({"games": games, "boss_freq": boss_freq, "early_freq": early_freq,
                   "empirical_bonus": empirical, "boss_floor": args.boss_floor,
                   "n_boss": len(boss_decks), "n_early": len(early_decks)}, f, indent=2)
    print(f"\nRaw stats → {args.out}")

    with open(args.scores_out, "w") as f:
        json.dump(empirical, f, indent=2, sort_keys=True)
    print(f"Per-card bonuses → {args.scores_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
