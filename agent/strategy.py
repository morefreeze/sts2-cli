"""strategy.py — Swappable map/non-combat strategies for game agents.

Each strategy implements the same interface: choose(state, choices) -> command dict.
Swap strategies globally via set_map_strategy() or pass directly.
"""
from typing import Protocol

from agent.card_scoring import best_smith_target


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
    When HP > 75% and past floor 6, elites are actively preferred over
    monsters (relic gain is worth the risk).
    Act 2 (floor 16+) is handled separately: always avoid elites unless
    very healthy (>90%), heal aggressively, shop actively.
    """
    HP_DANGER = 0.40   # below this: strong elite avoidance
    HP_LOW    = 0.60   # below this: moderate elite avoidance
    HP_STRONG = 0.85   # above this (and floor > 6): slight elite preference

    ELITE_PREFER_FLOOR = 6  # only prefer elites after Act 1 mid-game
    ACT2_START = 16          # Act 2 begins at floor 16
    ACT2_BOSS_ZONE = 19      # pre-Act-2-boss zone: save HP

    def choose(self, state: dict, choices: list[dict]) -> dict:
        player = state.get("player", {})
        hp = player.get("hp", 80)
        max_hp = max(player.get("max_hp", 80), 1)
        hp_ratio = hp / max_hp
        gold = player.get("gold", 0)
        floor = state.get("floor") or state.get("context", {}).get("floor", 99)
        in_act2 = isinstance(floor, int) and floor >= self.ACT2_START

        scored = []
        for i, c in enumerate(choices):
            p = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            # Shop value scales up in Act 2 — lower gold threshold
            act2_shop_threshold = 75 if in_act2 else self.SHOP_GOLD_THRESHOLD
            if c.get("type") == "Shop" and gold < act2_shop_threshold:
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
                elif in_act2:
                    # Act 2 elites: only seek them at 90%+ HP; otherwise avoid
                    if hp_ratio >= 0.90:
                        p -= 1.0  # healthy enough to risk Act 2 elite for relic
                    else:
                        p += 5  # Act 2 elites are significantly harder
                elif hp_ratio >= self.HP_STRONG and isinstance(floor, int) and floor > self.ELITE_PREFER_FLOOR and floor <= 12:
                    p -= 1.9  # very healthy (85%+) + mid-game: elites worth it for relics
                    # Use 1.9 not 2.0: prevents Elite from tying Unknown/Event (p=4)
                    # when both appear — tied priority resolves by index (Elite=0 wins), which
                    # would silently prefer combat over a safe event room.
                    # HP_STRONG raised to 0.85: runs at 75-85% HP shouldn't seek elites.
                elif isinstance(floor, int) and floor >= 13:
                    p += 3  # pre-boss zone: save HP for boss fight, avoid elites
                if isinstance(floor, int) and floor >= self.ACT2_BOSS_ZONE:
                    p += 4  # pre-Act-2-boss: strongly avoid elites regardless of HP
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}


def rest_site_action(state: dict, options: list[dict]) -> dict:
    """Decide heal vs upgrade at a rest site.

    Decision order:
      1. HP critical (< CRITICAL_HEAL ratio)  → always heal, no exceptions
      2. Deck has an un-upgraded high-value card (score ≥ 7) → SMITH that card
         (only if HP isn't critical — a +1 upgrade pays off every remaining combat,
         while +30% HP from rest is one-shot value)
      3. HP below the floor-graduated heal threshold → heal
      4. Otherwise → SMITH (default)

    The SMITH-priority override is the change requested 2026-05-06: "even at low
    HP, upgrade if there's a must-upgrade card; don't always rest."
    """
    player = state.get("player", {})
    hp = player.get("hp", 0)
    max_hp = max(player.get("max_hp", 80), 1)
    hp_ratio = hp / max_hp
    floor = state.get("floor") or state.get("context", {}).get("floor", 0)
    deck = player.get("deck") or []

    # Heal threshold by floor — used when no must-upgrade card present.
    if isinstance(floor, int) and floor >= 16:
        heal_threshold = 0.95
    elif isinstance(floor, int) and floor >= 11:
        heal_threshold = 0.85
    elif isinstance(floor, int) and floor >= 6:
        heal_threshold = 0.80
    else:
        heal_threshold = 0.75

    # Below this we always heal regardless of upgrade opportunities.
    CRITICAL_HEAL = 0.30

    enabled = [o for o in options if o.get("is_enabled", True)]
    heal  = next((o for o in enabled if "heal"  in (o.get("option_id") or "").lower()), None)
    smith = next((o for o in enabled if "smith" in (o.get("option_id") or "").lower()), None)

    has_must_upgrade = smith is not None and best_smith_target(deck) is not None

    if hp_ratio < CRITICAL_HEAL and heal is not None:
        choice = heal
    elif has_must_upgrade:
        choice = smith
    elif hp_ratio < heal_threshold and heal is not None:
        choice = heal
    elif smith is not None:
        choice = smith
    else:
        choice = heal or (enabled[0] if enabled else None)

    if choice:
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": choice["index"]}}
    return {"cmd": "action", "action": "proceed"}
