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
    """Act 1 safe strategy: avoid fights early, avoid elites before floor 3.

    Priority: RestSite > Shop (if gold sufficient) > Event/Treasure > Monster > Elite.
    Boss is unavoidable (only choice) so it ranks lowest to never be picked over alternatives.
    On floors 1-2: Elite is deprioritized heavily (penalty +10).
    """
    SHOP_GOLD_THRESHOLD = 100
    ELITE_AVOID_FLOOR = 3  # avoid elites on floors below this

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
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        scored = []
        for i, c in enumerate(choices):
            p = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            # Shop is unattractive when broke
            if c.get("type") == "Shop" and gold < self.SHOP_GOLD_THRESHOLD:
                p += 5
            # Avoid elites on early floors
            if c.get("type") == "Elite" and isinstance(floor, int) and floor < self.ELITE_AVOID_FLOOR:
                p += 10
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}
