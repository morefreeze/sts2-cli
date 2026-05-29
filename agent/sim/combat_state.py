#!/usr/bin/env python3
"""combat_state.py — Combat state container for the Python simulator.

CombatState captures everything needed to resume / replay a combat turn:
player HP/block/energy/statuses, the four piles (draw/hand/discard/exhaust),
and the enemy roster with their HP/intent/statuses.

Designed for cheap deep-copy (clone()) so MC rollouts can branch without
sharing mutable state. Status dicts are shallow-copied; piles are list[str]
of card-ids (lightweight). Enemy intent is stored as a dict so the
enemy_intents module can resolve it per-encounter without hard-coding.

JSON adapter (from_game_state) builds a CombatState from a state dict
produced by the C# Sts2Headless JSON protocol — letting the simulator
take over from any real-engine snapshot.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Enemy:
    id: str
    name: str = ""
    hp: int = 0
    max_hp: int = 0
    block: int = 0
    intent: dict[str, Any] = field(default_factory=dict)  # {"type":"attack","damage":6,"hits":1} etc.
    statuses: dict[str, int] = field(default_factory=dict)


@dataclass
class CombatState:
    # Player
    hp: int = 0
    max_hp: int = 0
    block: int = 0
    energy: int = 0
    max_energy: int = 3  # baseline; relics may bump
    statuses: dict[str, int] = field(default_factory=dict)

    # Piles — card ids (e.g. "STRIKE_IRONCLAD")
    hand: list[str] = field(default_factory=list)
    draw_pile: list[str] = field(default_factory=list)
    discard_pile: list[str] = field(default_factory=list)
    exhaust_pile: list[str] = field(default_factory=list)

    # Enemies (active in combat)
    enemies: list[Enemy] = field(default_factory=list)

    # Active powers — each entry: {"trigger": "on_turn_start"|"on_lose_hp"|...,
    # "effects": [Effect, ...]}. Populated when a Power card resolves; fired by
    # combat_step lifecycle hooks at matching events.
    powers: list[dict] = field(default_factory=list)

    # Per-turn counters / flags
    turn: int = 1
    attacks_played_this_turn: int = 0
    cards_exhausted_this_turn: int = 0
    lost_hp_this_turn: bool = False
    hp_lost_this_combat: int = 0

    # Run-level context — informs scoring even though combat doesn't use them
    floor: int = 1
    relics: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)

    # RNG — explicit so rollouts are reproducible per-seed
    rng_seed: int | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def clone(self) -> "CombatState":
        """Deep copy suitable for branching MC rollouts."""
        return copy.deepcopy(self)

    def alive(self) -> bool:
        return self.hp > 0

    def combat_over(self) -> bool:
        return (not self.alive()) or all(e.hp <= 0 for e in self.enemies)

    def player_won(self) -> bool:
        return self.alive() and all(e.hp <= 0 for e in self.enemies)

    # ── pile helpers ────────────────────────────────────────────────────────
    def draw(self, n: int, rng: random.Random | None = None) -> list[str]:
        """Draw up to n cards, reshuffling discard into draw if needed.
        Returns the list of drawn card ids (mutates self.hand)."""
        rng = rng or random.Random(self.rng_seed)
        drawn: list[str] = []
        for _ in range(n):
            if not self.draw_pile and self.discard_pile:
                self.draw_pile = self.discard_pile[:]
                rng.shuffle(self.draw_pile)
                self.discard_pile = []
            if not self.draw_pile:
                break
            card = self.draw_pile.pop()
            self.hand.append(card)
            drawn.append(card)
        return drawn

    def discard_card(self, card_id: str) -> bool:
        """Move one matching card from hand to discard pile. Returns True if moved."""
        if card_id in self.hand:
            self.hand.remove(card_id)
            self.discard_pile.append(card_id)
            return True
        return False

    def exhaust_card(self, card_id: str) -> bool:
        """Move one matching card from hand to exhaust pile."""
        if card_id in self.hand:
            self.hand.remove(card_id)
            self.exhaust_pile.append(card_id)
            self.cards_exhausted_this_turn += 1
            return True
        return False

    def shuffle_into_draw(self, card_id: str,
                          rng: random.Random | None = None) -> None:
        rng = rng or random.Random(self.rng_seed)
        self.draw_pile.append(card_id)
        rng.shuffle(self.draw_pile)

    # ── damage handlers ────────────────────────────────────────────────────
    def take_damage(self, amount: int) -> int:
        """Apply damage to player after block. Returns HP actually lost."""
        if amount <= 0:
            return 0
        absorbed = min(self.block, amount)
        self.block -= absorbed
        through = amount - absorbed
        if through > 0:
            actual = min(through, self.hp)
            self.hp -= actual
            self.lost_hp_this_turn = True
            self.hp_lost_this_combat += actual
            return actual
        return 0

    def damage_enemy(self, idx: int, amount: int) -> int:
        """Apply damage to enemy[idx] after its block. Returns HP actually lost."""
        if idx < 0 or idx >= len(self.enemies):
            return 0
        e = self.enemies[idx]
        if e.hp <= 0:
            return 0
        absorbed = min(e.block, amount)
        e.block -= absorbed
        through = amount - absorbed
        if through > 0:
            actual = min(through, e.hp)
            e.hp -= actual
            return actual
        return 0

    # ── turn lifecycle ─────────────────────────────────────────────────────
    def start_turn(self) -> None:
        self.turn += 1
        self.energy = self.max_energy
        self.attacks_played_this_turn = 0
        self.cards_exhausted_this_turn = 0
        self.lost_hp_this_turn = False
        # Block decay (unless special status like Barricade)
        if "Barricade" not in self.statuses:
            self.block = 0
        # Status ticks
        if "Strength" in self.statuses and self.statuses["Strength"] == 0:
            del self.statuses["Strength"]

    def end_turn(self) -> None:
        # End-of-turn status decay
        for st in ("Vulnerable", "Weak", "Frail"):
            if st in self.statuses:
                self.statuses[st] -= 1
                if self.statuses[st] <= 0:
                    del self.statuses[st]
        # End-of-turn enemy status decay
        for e in self.enemies:
            for st in ("Vulnerable", "Weak", "Frail"):
                if st in e.statuses:
                    e.statuses[st] -= 1
                    if e.statuses[st] <= 0:
                        del e.statuses[st]

    # ── serialisation ──────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already handles nested Enemy dataclasses
        return d

    @classmethod
    def from_game_state(cls, state: dict) -> "CombatState":
        """Build a CombatState from the C# Sts2Headless state dict
        (the JSON returned by combat_play decisions). Tolerant of missing fields.
        """
        player = state.get("player", {}) or {}
        cs = cls(
            hp=int(player.get("hp", 0) or 0),
            max_hp=int(player.get("max_hp", 0) or 0),
            block=int(player.get("block", 0) or 0),
            energy=int(player.get("energy", 0) or 0),
            statuses=dict(player.get("powers", {}) or {}),
            floor=int(state.get("floor")
                      or (state.get("context", {}) or {}).get("floor", 1)),
        )
        cs.hand = [_card_id(c) for c in (state.get("hand", []) or [])]
        cs.draw_pile = [_card_id(c) for c in (state.get("draw_pile", []) or [])]
        cs.discard_pile = [_card_id(c) for c in (state.get("discard_pile", []) or [])]
        cs.exhaust_pile = [_card_id(c) for c in (state.get("exhaust_pile", []) or [])]
        for raw in state.get("enemies", []) or []:
            name = raw.get("name", "")
            if isinstance(name, dict):
                name = name.get("en", "?")
            powers = {}
            for p in raw.get("powers", []) or []:
                powers[p.get("name", "?")] = int(p.get("amount", 0) or 0)
            intents = raw.get("intents", []) or []
            intent = intents[0] if intents else {}
            cs.enemies.append(Enemy(
                id=str(raw.get("id", "")),
                name=str(name),
                hp=int(raw.get("hp", 0) or 0),
                max_hp=int(raw.get("max_hp", 0) or 0),
                block=int(raw.get("block", 0) or 0),
                intent={"type": intent.get("type", "?"),
                        "damage": int(intent.get("damage", 0) or 0),
                        "hits": int(intent.get("hits", 1) or 1)},
                statuses=powers,
            ))
        cs.relics = [str(r.get("id", "")) for r in (state.get("relics", []) or [])]
        cs.potions = [str(p.get("id", "")) for p in (state.get("potions", []) or [])]
        return cs


def _card_id(c: dict | str) -> str:
    if isinstance(c, str):
        return c
    cid = c.get("id", "?")
    if isinstance(cid, dict):
        cid = cid.get("en", str(cid))
    return str(cid)


# ─── quick sanity test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. Basic construction + clone
    s = CombatState(hp=70, max_hp=80, energy=3)
    s.hand = ["STRIKE_IRONCLAD", "DEFEND_IRONCLAD"] * 5
    s.draw_pile = ["BASH"] * 3
    s.enemies = [Enemy(id="LOUSE", name="Louse", hp=15, max_hp=15)]
    s2 = s.clone()
    s2.hp = 99
    assert s.hp == 70, "clone leaked state"
    assert s2.hp == 99

    # 2. Damage + block
    s.block = 8
    actual = s.take_damage(12)
    assert s.block == 0 and s.hp == 66 and actual == 4, f"got block={s.block} hp={s.hp} actual={actual}"

    # 3. Enemy damage
    lost = s.damage_enemy(0, 10)
    assert lost == 10 and s.enemies[0].hp == 5

    # 4. Combat over
    s.damage_enemy(0, 100)
    assert s.combat_over() and s.player_won()

    # 5. Draw / reshuffle
    s = CombatState(hp=80, max_hp=80, energy=3)
    s.discard_pile = ["A", "B", "C"]
    drawn = s.draw(2)
    assert len(drawn) == 2 and len(s.hand) == 2 and len(s.draw_pile) == 1

    # 6. Status tick
    s = CombatState(statuses={"Vulnerable": 2, "Strength": 3})
    s.end_turn()
    assert s.statuses["Vulnerable"] == 1
    assert s.statuses["Strength"] == 3  # not in end-of-turn decay

    # 7. from_game_state adapter
    fake_gs = {
        "player": {"hp": 50, "max_hp": 80, "block": 5, "energy": 3,
                   "powers": {"Strength": 2}},
        "hand": [{"id": "STRIKE_IRONCLAD"}],
        "draw_pile": [{"id": "DEFEND_IRONCLAD"}, {"id": "BASH"}],
        "enemies": [{
            "id": "LOUSE", "name": {"en": "Louse"}, "hp": 15, "max_hp": 15,
            "block": 0, "powers": [{"name": "Curl Up", "amount": 3}],
            "intents": [{"type": "attack", "damage": 5, "hits": 1}],
        }],
        "floor": 7,
    }
    cs = CombatState.from_game_state(fake_gs)
    assert cs.hp == 50 and cs.energy == 3 and cs.statuses["Strength"] == 2
    assert cs.hand == ["STRIKE_IRONCLAD"]
    assert cs.enemies[0].hp == 15 and cs.enemies[0].intent["damage"] == 5
    assert cs.enemies[0].statuses["Curl Up"] == 3
    assert cs.floor == 7

    print("✓ all CombatState sanity tests pass")
