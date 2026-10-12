"""route_planner.py -- choose the next map node by expected final floor.

Phase 3b (docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md).
The engine's ``get_map`` reply is a DAG of rooms; a route is a path from a
selectable node to the last node before the boss. Every path is enumerated
(measured max 374 per map), the expected state is propagated room by room with
the route model, death risk is accumulated on the way, and the choice whose best
path has the highest expected final global floor is picked.
"""
from __future__ import annotations

from agent.route_data import state_features
from agent.route_model import ACT_FLOORS, CELL_LEVELS

MAX_PATHS = 20000
CHECKPOINT_KEYS = ("hp", "max_hp", "deck_burst", "deck_top8", "n_relics", "n_potions", "gold")
_TIE_EPS = 1e-9


def node_key(node_or_child) -> tuple[int, int]:
    """(col, row) of a map node or child ref, given as {"col","row"} or [col, row]."""
    if isinstance(node_or_child, dict):
        return int(node_or_child["col"]), int(node_or_child["row"])
    return int(node_or_child[0]), int(node_or_child[1])


def _nodes(map_json: dict) -> dict[tuple[int, int], dict]:
    return {node_key(node): node for row in map_json.get("rows") or [] for node in row}


def fixed_rows(map_json: dict) -> dict[int, str]:
    """{row: room type} for every row with at least one node whose nodes all
    share one type (every path crosses that room type on that row)."""
    types_by_row: dict[int, set] = {}
    for node in _nodes(map_json).values():
        types_by_row.setdefault(int(node["row"]), set()).add(node.get("type"))
    return {row: next(iter(types)) for row, types in sorted(types_by_row.items())
            if len(types) == 1}


def enumerate_paths(map_json: dict, start: tuple[int, int]) -> list[list[dict]]:
    """Every path of map nodes from ``start`` (inclusive) following ``children``
    until a node with no children inside ``rows`` (the boss is not in rows).
    Depth-first, children in (col, row) order; a start that is not a node of the
    map gives no path. Raises ValueError past MAX_PATHS paths."""
    index = _nodes(map_json)
    if tuple(start) not in index:
        return []
    paths: list[list[dict]] = []

    def walk(node: dict, prefix: list[dict]) -> None:
        prefix = prefix + [node]
        children = sorted(k for k in (node_key(c) for c in node.get("children") or [])
                          if k in index)
        if not children:
            paths.append(prefix)
            if len(paths) > MAX_PATHS:
                raise ValueError(f"more than {MAX_PATHS} paths from {tuple(start)}")
            return
        for key in children:
            walk(index[key], prefix)

    walk(index[tuple(start)], [])
    return paths


def score_path(path: list[dict], features: dict, character: str, model,
               checkpoint_rows: frozenset = frozenset()) -> dict:
    """Expected final global floor of walking ``path`` from the state
    ``features``: survive-or-die room by room, then the value model on the state
    that is left. A room on map row r is floor r + 1 (dying in it ends the run
    there; surviving it leaves the state on floor r + 1), so the score does not
    depend on how many rooms a path takes to reach a row.

    Returns {"score", "p_alive", "end", "checkpoints": {row: the expected state
    right after stepping the node on that row}, "cell_levels": {level: number of
    the path's rooms whose transition cell resolved at that level}}."""
    p_alive = 1.0
    death_term = 0.0
    s = dict(features)
    checkpoints: dict[int, dict] = {}
    levels = dict.fromkeys(CELL_LEVELS, 0)
    for node in path:
        act, room_type, row = s["act"], node["type"], node["row"]
        levels[model.resolve(act, room_type, s)[0]] += 1
        q = model.death_prob(act, room_type, s)
        death_term += p_alive * q * ((act - 1) * ACT_FLOORS + row + 1)
        p_alive *= 1 - q
        s = model.step(act, room_type, s, row)
        if row in checkpoint_rows:
            checkpoints[row] = s
    score = death_term + p_alive * (s["gf"] + model.value(s, character))
    return {"score": score, "p_alive": p_alive, "end": s, "checkpoints": checkpoints,
            "cell_levels": levels}


def choose_node(map_json: dict, state: dict, character: str, model) -> dict | None:
    """Pick among ``state["choices"]`` (the map_select decision) the node whose
    best path has the highest expected final floor. Ties: lower col, then lower
    row. Only choices that are nodes of ``map_json["rows"]`` are considered,
    plus a Boss choice, which has no rooms left to plan and is scored as the
    empty path (gf + V). Returns {"col", "row", "score", "p_alive", "paths" (paths
    scored over all choices), "checkpoints": {str(row): {hp, max_hp,
    deck_burst, deck_top8, n_relics, n_potions, gold} after that row of the chosen path,
    for the rows every path crosses as the same room type}, "cell_levels" {level:
    rooms of the chosen path resolved at that level -- see RouteModel.resolve; a
    sparse model shows up as rooms below "act"}}; None when no choice can be
    scored."""
    features = state_features(state)
    index = _nodes(map_json)
    crossing = frozenset(fixed_rows(map_json))
    candidates = []
    for choice in state.get("choices") or []:
        key = node_key(choice)
        if key in index or choice.get("type") == "Boss":
            candidates.append(key)

    best = None
    best_key = None
    n_paths = 0
    for key in sorted(candidates):
        if key in index:
            paths = enumerate_paths(map_json, key)
        else:
            paths = [[]]  # Boss: nothing left to plan
        n_paths += len(paths)
        for path in paths:
            scored = score_path(path, features, character, model, crossing)
            if best is None or scored["score"] > best["score"] + _TIE_EPS:
                best, best_key = scored, key
    if best is None:
        return None
    return {"col": best_key[0], "row": best_key[1], "score": best["score"],
            "p_alive": best["p_alive"], "paths": n_paths, "cell_levels": best["cell_levels"],
            "checkpoints": {str(row): {k: s[k] for k in CHECKPOINT_KEYS}
                            for row, s in best["checkpoints"].items()}}
