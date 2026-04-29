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


class HpAwareMapStrategy(Act1SafeStrategy):
    """HP-aware map routing: avoids elites when current HP is low.

    Inherits Act1SafeStrategy's floor-based elite avoidance and adds
    HP-ratio penalties so a damaged run doesn't feed into an elite.
    When HP > 70% and past floor 6, elites are actively preferred over
    monsters (relic gain is worth the risk).
    """
    HP_DANGER = 0.35   # below this: strong elite avoidance
    HP_LOW    = 0.55   # below this: moderate elite avoidance
    HP_STRONG = 0.72   # above this (and floor > 6): slight elite preference

    ELITE_PREFER_FLOOR = 6  # only prefer elites after Act 1 mid-game

    def choose(self, state: dict, choices: list[dict]) -> dict:
        player = state.get("player", {})
        hp = player.get("hp", 80)
        max_hp = max(player.get("max_hp", 80), 1)
        hp_ratio = hp / max_hp
        gold = player.get("gold", 0)
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)

        scored = []
        for i, c in enumerate(choices):
            p = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            if c.get("type") == "Shop" and gold < self.SHOP_GOLD_THRESHOLD:
                p += 5
            if c.get("type") == "Elite":
                # Floor-based early avoidance (inherited logic)
                if isinstance(floor, int) and floor < self.ELITE_AVOID_FLOOR:
                    p += 10
                # HP-based avoidance — takes priority over floor preference
                if hp_ratio < self.HP_DANGER:
                    p += 15
                elif hp_ratio < self.HP_LOW:
                    p += 7
                elif hp_ratio >= self.HP_STRONG and isinstance(floor, int) and floor > self.ELITE_PREFER_FLOOR:
                    p -= 2  # healthy + mid-game: elites worth it for relics
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}


def rest_site_action(state: dict, options: list[dict]) -> dict:
    """Decide heal vs upgrade at a rest site based on current HP ratio and floor.

    Rules:
    - floor >= 12 (pre-boss zone): heal if HP < 80%, otherwise upgrade
    - HP < 55%  → always heal (survival first)
    - HP >= 55% → upgrade if SMITH is available, else heal
    - Fallback  → first enabled option
    """
    player = state.get("player", {})
    hp = player.get("hp", 0)
    max_hp = max(player.get("max_hp", 80), 1)
    hp_ratio = hp / max_hp
    floor = state.get("floor") or state.get("context", {}).get("floor", 0)
    pre_boss = isinstance(floor, int) and floor >= 11

    enabled = [o for o in options if o.get("is_enabled", True)]
    heal  = next((o for o in enabled if o.get("option_id") == "HEAL"),  None)
    smith = next((o for o in enabled if o.get("option_id") == "SMITH"), None)

    if pre_boss and hp_ratio < 0.85:
        choice = heal or (enabled[0] if enabled else None)
    elif hp_ratio < 0.62 or smith is None:
        choice = heal or (enabled[0] if enabled else None)
    else:
        choice = smith or heal or (enabled[0] if enabled else None)

    if choice:
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": choice["index"]}}
    return {"cmd": "action", "action": "proceed"}
