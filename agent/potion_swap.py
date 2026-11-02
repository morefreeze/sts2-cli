# agent/potion_swap.py
"""Which potion to keep when the belt is full (the in-game autopilot's /potion_swap).

greedy never discards: a combat reward's potion is only claimable with a free slot, so a full belt
used to mean the reward was left on the floor -- including a Fairy in a Bottle while a Strength
potion stayed in the belt. This module scores held and offered potions with greedy's own potion
value (`agent.combat_env._score_shop_potion`, the shop scorer) plus a few life-saving adjustments,
and swaps only when the offered potion is clearly better than the worst held one.

Pure functions, no I/O; no greedy state is read or written.
"""
from agent.combat_env import _potion_slots, _score_shop_potion

# The offered potion must beat the worst held one by at least this much. The shop scorer works in
# steps of 1.0-3.0 per keyword (baseline 3.0, Strength +3, Block +1.5, ...), so 2.0 means "at least
# a whole keyword better": a Block potion (4.5) does not displace a Swift potion (5.0), a Strength
# potion (6.0) does displace a plain Fire Potion (3.0). Discarding is irreversible, so a close call
# keeps what is held.
SWAP_MARGIN = 2.0

# Adjustments on top of the shop score, by potion id entry. The shop scorer matches keywords in the
# description and knows nothing about survival: Fairy in a Bottle ("When your HP would be reduced
# to 0 ... heal to 30%") only matched "hp" (+2.0 = 5.0), BELOW a Strength potion (6.0), so a full
# belt of Strength potions would have been kept over a Fairy. A Fairy is a free revive: it must
# never lose to anything, and never be the one discarded.
KEEP_BONUS = {
    "FAIRY_IN_A_BOTTLE": 12.0,  # auto-revive: 5.0 + 12.0 = 17.0, above any keyword stack
    "GHOST_IN_A_JAR": 3.0,      # Intangible: survives a lethal turn (base 3.0 -> 6.0, like Strength)
    "LUCKY_TONIC": 2.0,         # Buffer: negates the next hit (base 3.0 -> 5.0)
}


def _entry(potion: dict) -> str:
    """'POTION.FAIRY_IN_A_BOTTLE' -> 'FAIRY_IN_A_BOTTLE'; '' when the potion has no id."""
    pid = potion.get("id")
    return pid.split(".", 1)[-1].upper() if isinstance(pid, str) else ""


def potion_keep_score(potion: dict) -> float:
    """How much a potion is worth keeping in the belt (higher = keep)."""
    return float(_score_shop_potion(potion)) + KEEP_BONUS.get(_entry(potion), 0.0)


def _label(potion: dict) -> str:
    name = potion.get("name")
    if isinstance(name, dict):
        name = name.get("en") or next(iter(name.values()), None)
    return str(name or potion.get("id") or "?")


def choose_potion_swap(player: dict, offered: list) -> dict:
    """Decide what to do with potion rewards offered to `player`.

    Returns {"discard": held list position or None, "take": offered list position or None,
             "discard_id", "take_id", "held_scores", "offered_scores", "margin", "reason"}.
    Positions are list positions (for the held belt this is the decision JSON's potion `index`).
    `reason` is one of: no_offer, free_slot (just take `take`), swap, not_better (keep the belt),
    no_slots (the belt has no slots at all).
    """
    held = [p for p in (player.get("potions") or []) if isinstance(p, dict)]
    slots = _potion_slots(player)
    held_scores = [round(potion_keep_score(p), 3) for p in held]
    offered_scores = [round(potion_keep_score(p), 3) if isinstance(p, dict) else None
                      for p in offered]
    reply = {"discard": None, "take": None, "discard_id": None, "take_id": None,
             "held_scores": held_scores, "offered_scores": offered_scores,
             "margin": SWAP_MARGIN, "reason": "no_offer"}
    candidates = [i for i, s in enumerate(offered_scores) if s is not None]
    if not candidates:
        return reply
    best = max(candidates, key=lambda i: (offered_scores[i], -i))  # earliest wins a tie
    if len(held) < slots:
        reply.update(take=best, take_id=offered[best].get("id"), reason="free_slot")
        return reply
    if not held:
        reply["reason"] = "no_slots"
        return reply
    worst = min(range(len(held)), key=lambda i: (held_scores[i], i))
    if offered_scores[best] - held_scores[worst] >= SWAP_MARGIN:
        reply.update(discard=worst, discard_id=held[worst].get("id"),
                     take=best, take_id=offered[best].get("id"), reason="swap")
    else:
        reply["reason"] = "not_better"
    reply["worst_held"] = _label(held[worst])
    reply["best_offered"] = _label(offered[best])
    return reply
