"""strategy.py — Swappable map/non-combat strategies for game agents.

Each strategy implements the same interface: choose(state, choices) -> command dict.
Swap strategies globally via set_map_strategy() or pass directly.
"""
from typing import Protocol


class MapStrategy(Protocol):
    def choose(self, state: dict, choices: list[dict]) -> dict:
        """Select a map node. Returns a full command dict for the engine."""
        ...


class Act1SafeStrategy:
    """Act 1 safe strategy: avoid fights, prefer rest, shop when rich.

    Priority: RestSite > Shop (if gold sufficient) > Event/Treasure > Monster > Elite.
    Boss is unavoidable (only choice) so it ranks lowest to never be picked over alternatives.
    """
    SHOP_GOLD_THRESHOLD = 100

    # Lower = higher priority. Boss=99 because it's never alongside alternatives.
    PRIORITY = {
        "RestSite": 0,
        "Shop": 1,
        "Event": 2,
        "Treasure": 3,
        "Unknown": 4,
        "Ancient": 4,
        "Monster": 5,
        "Elite": 6,
        "Boss": 99,
    }

    def choose(self, state: dict, choices: list[dict]) -> dict:
        gold = state.get("player", {}).get("gold", 0)
        scored = []
        for i, c in enumerate(choices):
            p = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            # Shop is unattractive when broke
            if c.get("type") == "Shop" and gold < self.SHOP_GOLD_THRESHOLD:
                p += 5
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}
