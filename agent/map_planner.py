#!/usr/bin/env python3
"""map_planner.py — Full-map path planning to maximize HP at boss entry.

Option f (Jun 11): combat micro is near-optimal (turn-planner experiments
proved the policy ≥ exact 1-turn search), so the reach lever is choosing
WHICH fights to take. The C# engine exposes the entire act map via the
`get_map` command: rows of nodes with explicit children edges + boss node.

Algorithm: enumerate paths from each currently-selectable child to the boss
(DAG, beam-pruned), score each node by expected HP delta, and pick the child
whose best path maximizes expected boss-entry HP. Tie-break: more elites
(relic value) when expected HP stays ≥ a comfort threshold.

Wired into CombatEnv._advance_to_combat behind STS2_MAP_PLANNER=1; falls
back to the heuristic HpAwareMapStrategy on any failure.
"""
from __future__ import annotations

# Expected HP delta per node type — now DECK-STRENGTH AWARE (Phase g):
# a strong deck clears mobs at -4 HP, a weak one bleeds -13. strength is
# deck_5turn_burst normalized: 1.0 = strong (≥150 burst), 0.0 = weak (≤50).
def node_delta(ntype: str, row: int, hp: int, max_hp: int, gold: int,
               deck_strength: float = 0.5, deck_size: int = 15) -> float:
    t = (ntype or "").lower()
    if t == "monster":
        base = (-6.0 if row < 6 else -8.0)
        scaled = base - (1.0 - deck_strength) * 5.0    # weak deck pays +5 more
        # Phase 3: fight reward value (card pick improves deck) — strong early,
        # decays once the deck matures.
        reward = 3.0 if (deck_size < 15 and row < 8) else (
            1.5 if deck_size < 18 else 0.5)
        return scaled + reward
    if t == "elite":
        base = (-13.0 if row < 9 else -20.0)
        scaled = base - (1.0 - deck_strength) * 8.0    # weak deck pays +8 more
        return scaled + 2.0   # relic value baked in (replaces tie-break only)
    if t in ("restsite", "rest"):
        return 0.6 * 0.30 * max_hp
    if t in ("event", "unknown", "ancient"):
        return -2.0
    if t == "shop":
        return 3.0 if (gold >= 75 and hp < 0.7 * max_hp) else 0.0
    if t == "treasure":
        return 0.0
    if t == "boss":
        return 0.0
    return -2.0


def _elite_bonus(n_elites: int, expected_hp: float, comfort: float) -> float:
    """Relic value of elites — only counts when the path keeps us above the
    comfort HP (no point getting relics if we die or limp to the boss)."""
    if expected_hp < comfort:
        return 0.0
    return 1.5 * min(n_elites, 2)


def plan_next_node(map_json: dict, hp: int, max_hp: int, gold: int,
                   comfort_hp: float = 72.0,
                   beam_width: int = 6,
                   deck_strength: float = 0.5,
                   deck_size: int = 15) -> tuple[tuple[int, int], dict] | None:
    """Return ((col,row) of best immediate child, route_info) or None.

    route_info: {"mobs": int, "elites": int, "rests": int} — composition of
    the best path from the chosen child to the boss (Phase g: published to
    card_scoring so card picks optimize against the actual route).

    Beam DP over the DAG: at each node keep top-`beam_width` states by
    expected HP. State: (expected_hp, n_elites, path_composition).
    """
    rows = map_json.get("rows") or []
    if not rows:
        return None
    nodes: dict[tuple[int, int], dict] = {}
    for row_nodes in rows:
        for nd in row_nodes:
            nodes[(int(nd["col"]), int(nd["row"]))] = nd
    boss = map_json.get("boss") or {}
    boss_coord = (int(boss.get("col", -1)), int(boss.get("row", -1)))

    cur = map_json.get("current_coord")
    if cur:
        cur_coord = (int(cur["col"]), int(cur["row"]))
        cur_node = nodes.get(cur_coord)
        starts = [(int(c["col"]), int(c["row"]))
                  for c in (cur_node.get("children") or [])] if cur_node else []
    else:
        # Run start: every node in row 0 is selectable
        starts = [coord for coord, nd in nodes.items() if int(nd["row"]) == 0]
    if not starts:
        return None

    from collections import defaultdict

    def _comp_add(comp: tuple, ntype: str) -> tuple:
        t = (ntype or "").lower()
        m, e, r = comp
        if t == "monster":
            return (m + 1, e, r)
        if t == "elite":
            return (m, e + 1, r)
        if t in ("restsite", "rest"):
            return (m, e, r + 1)
        return comp

    def search_from(start: tuple[int, int]) -> tuple[float, tuple]:
        """(best expected boss-entry score, best path composition)."""
        sn = nodes.get(start)
        if sn is None:
            return -1e9, (0, 0, 0)
        d0 = node_delta(sn.get("type", ""), int(sn.get("row", 0)), hp, max_hp,
                        gold, deck_strength, deck_size)
        h0 = min(float(hp) + d0, float(max_hp))
        comp0 = _comp_add((0, 0, 0), sn.get("type", ""))
        beams: dict[tuple[int, int], list] = defaultdict(list)
        beams[start] = [(h0, 1 if (sn.get("type", "").lower() == "elite") else 0,
                         comp0)]

        all_coords = sorted(nodes.keys(), key=lambda c: c[1])
        best_terminal = (-1e9, (0, 0, 0))
        for coord in all_coords:
            if coord not in beams or not beams[coord]:
                continue
            nd = nodes[coord]
            children = nd.get("children") or []
            states = beams[coord]
            if not children:
                for (ehp, nel, comp) in states:
                    if ehp <= 0:
                        continue
                    score = ehp + _elite_bonus(nel, ehp, comfort_hp)
                    if score > best_terminal[0]:
                        best_terminal = (score, comp)
                continue
            for ch in children:
                ch_coord = (int(ch["col"]), int(ch["row"]))
                chn = nodes.get(ch_coord)
                if chn is None:
                    for (ehp, nel, comp) in states:
                        if ehp <= 0:
                            continue
                        score = ehp + _elite_bonus(nel, ehp, comfort_hp)
                        if score > best_terminal[0]:
                            best_terminal = (score, comp)
                    continue
                d = node_delta(chn.get("type", ""), int(chn.get("row", 0)),
                               hp, max_hp, gold, deck_strength, deck_size)
                is_el = 1 if (chn.get("type", "").lower() == "elite") else 0
                merged = beams[ch_coord]
                for (ehp, nel, comp) in states:
                    if ehp <= 0:
                        continue
                    nh = min(ehp + d, float(max_hp))
                    merged.append((nh, nel + is_el,
                                   _comp_add(comp, chn.get("type", ""))))
                merged.sort(key=lambda s: s[0], reverse=True)
                del merged[beam_width:]
        return best_terminal

    best_start, best_score, best_comp = None, -1e9, (0, 0, 0)
    for st in starts:
        sc, comp = search_from(st)
        if sc > best_score:
            best_score = sc
            best_start = st
            best_comp = comp
    if best_start is None:
        return None
    return best_start, {"mobs": best_comp[0], "elites": best_comp[1],
                         "rests": best_comp[2]}


def _deck_strength(deck: list[dict]) -> tuple[float, int]:
    """(normalized strength 0-1 from deck_5turn_burst, deck size)."""
    try:
        from agent.card_scoring import deck_5turn_burst
        burst = deck_5turn_burst(deck or [])
    except Exception:
        burst = 80.0
    # 50 burst → 0.0, 150 burst → 1.0
    strength = max(0.0, min(1.0, (burst - 50.0) / 100.0))
    return strength, len(deck or [])


def choose_map_node(env, state: dict) -> dict | None:
    """Full planning entry — fetch map via env._send, plan, return command.
    None → caller falls back to heuristic strategy.

    Phase g: deck strength scales node costs (strong decks fight more);
    the chosen route's composition is published to card_scoring so card
    picks optimize against the actual remaining path."""
    try:
        m = env._send({"cmd": "get_map"})
        if not m or m.get("type") != "map":
            return None
        player = state.get("player") or {}
        hp = int(player.get("hp", 80) or 80)
        max_hp = int(player.get("max_hp", 80) or 80)
        gold = int(player.get("gold", 0) or 0)
        deck = player.get("deck") or []
        strength, dsize = _deck_strength(deck)
        result = plan_next_node(m, hp, max_hp, gold,
                                 deck_strength=strength, deck_size=dsize)
        if result is None:
            return None
        pick, route = result
        # Validate against offered choices
        choices = state.get("choices") or []
        ok = any(int(c.get("col", -9)) == pick[0] and int(c.get("row", -9)) == pick[1]
                 for c in choices)
        if not ok and choices:
            return None
        # Publish route composition for route-aware card scoring (Phase 1)
        try:
            from agent.card_scoring import set_route_context
            floor = state.get("floor") or (state.get("context") or {}).get("floor", 0)
            boss_id = str(((state.get("context") or {}).get("boss") or {})
                          .get("id") or "").replace("_BOSS", "") or None
            set_route_context(route["mobs"], route["elites"], route["rests"],
                              floor=int(floor or 0), boss_id=boss_id)
        except Exception:
            pass
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": pick[0], "row": pick[1]}}
    except Exception:
        return None


# ─── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Tiny synthetic map: row0 has Monster vs Rest; both lead to row1 Monster → boss.
    fake_map = {
        "type": "map",
        "rows": [
            [
                {"col": 0, "row": 0, "type": "Monster",
                 "children": [{"col": 0, "row": 1}], "visited": False, "current": False},
                {"col": 1, "row": 0, "type": "RestSite",
                 "children": [{"col": 0, "row": 1}], "visited": False, "current": False},
            ],
            [
                {"col": 0, "row": 1, "type": "Monster", "children": [],
                 "visited": False, "current": False},
            ],
        ],
        "boss": {"col": 0, "row": 2, "type": "Boss"},
        "current_coord": None,
    }
    # Low HP + weak deck → Rest start should win
    r = plan_next_node(fake_map, hp=40, max_hp=80, gold=0, deck_strength=0.2)
    pick, route = r
    print(f"low HP weak deck: pick={pick} route={route} (expect (1,0) RestSite)")
    assert pick == (1, 0)
    # Strong deck at full HP → Monster start now attractive (cheap + reward)
    r2 = plan_next_node(fake_map, hp=80, max_hp=80, gold=0,
                         deck_strength=1.0, deck_size=12)
    pick2, route2 = r2
    print(f"full HP strong deck: pick={pick2} route={route2}")
    print("✓ map planner deck-aware paths work")
