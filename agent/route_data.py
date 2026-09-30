"""route_data.py -- per-room transitions and state features from game logs.

Phase 3b (docs/superpowers/plans/2026-09-30-phase3b-route-value-planner.md).
A game log (python/game_log.py) is JSONL: an optional ``run_meta`` line, then
``{"type": "action", "data": <command>}`` / ``{"type": "state", "data": <engine
reply>}`` entries in send order. This module turns one log into

* ``snapshots``   -- the state at every map_select where a node was chosen, and
                     the chosen room type (the value model's samples);
* ``transitions`` -- for every chosen node, the state before entering the room
                     (``pre``) and at the next map_select / game_over (``post``).

Only ``type == "decision"`` states are game states; maps, combat plans and
errors are ignored. Floors are ACT-LOCAL in the engine, so everything here also
carries the global floor ``gf = (act - 1) * 17 + floor``.
"""
from __future__ import annotations

import json

# Imported directly (not through map_planner._deck_strength, which swallows an
# import error and silently returns a constant): an environment without the
# card data must fail loudly here, not fit a model on a constant feature.
from agent.card_scoring import deck_5turn_burst, score_card

FEATURE_KEYS = ("hp", "max_hp", "gold", "deck_burst", "deck_top8", "deck_size", "n_upgraded",
                "n_relics", "n_potions")

ACT_FLOORS = 17
TOP_CARDS = 8


def _num(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _act_floor(state: dict) -> tuple[int, int]:
    """(act, act-local floor). map_select carries them under ``context`` and at
    the top level; game_over carries both too. context wins; act defaults to 1
    and floor to 0."""
    ctx = state.get("context") if isinstance(state.get("context"), dict) else {}
    act = int(_num(ctx.get("act"), 0) or _num(state.get("act"), 0) or 1)
    floor = int(_num(ctx.get("floor"), 0) or _num(state.get("floor"), 0))
    return act, floor


def state_features(state: dict) -> dict:
    """Numeric features of one decision state (map_select or game_over).

    FEATURE_KEYS plus ``act`` (default 1), ``floor`` (act-local, default 0) and
    ``gf`` (global floor). Deck quality is two UNCLIPPED numbers -- the clipped
    0..1 strength of map_planner is exactly 0 for most Act 1-2 states and carries
    no signal:

    * ``deck_burst`` = card_scoring.deck_5turn_burst(deck) / 100;
    * ``deck_top8`` = mean of the top min(8, len(deck)) card_scoring.score_card
      values (0.0 for an empty deck).

    A game_over reply has no ``deck`` / ``relics`` -- only ``deck_size`` -- so its
    deck-derived features are deck_size and zeros: it only ever serves as the
    ``post`` of a death, whose deltas the model never uses.
    """
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    act, floor = _act_floor(state)
    deck = player.get("deck")
    if isinstance(deck, list):
        cards = [c for c in deck if isinstance(c, dict)]
        burst = deck_5turn_burst(cards) / 100.0
        top = sorted((score_card(c) for c in cards), reverse=True)[:TOP_CARDS]
        top8 = sum(top) / len(top) if top else 0.0
        deck_size = len(cards)
        n_upgraded = sum(1 for c in cards if c.get("upgraded"))
    else:
        burst, top8, n_upgraded = 0.0, 0.0, 0
        deck_size = int(_num(player.get("deck_size"), 0))
    relics = player.get("relics")
    potions = player.get("potions")
    return {
        "hp": _num(player.get("hp")),
        "max_hp": _num(player.get("max_hp")),
        "gold": _num(player.get("gold")),
        "deck_burst": float(burst),
        "deck_top8": float(top8),
        "deck_size": int(deck_size),
        "n_upgraded": int(n_upgraded),
        "n_relics": len(relics) if isinstance(relics, list) else 0,
        "n_potions": sum(1 for p in potions if p) if isinstance(potions, list) else 0,
        "act": act,
        "floor": floor,
        "gf": (act - 1) * ACT_FLOORS + floor,
    }


def extract_transitions(rows: list[dict]) -> dict | None:
    """One json-parsed game log -> {character, ascension, seed, won, final_gf,
    snapshots, transitions}; None when the log has no game_over decision (crash /
    hang / truncated -- excluded, like agent/paired_eval.py).

    A move is a ``select_map_node`` action; its type comes from the choice with
    that (col, row) in the most recent map_select (a move to a node that was not
    offered is skipped). Its ``post`` is the next map_select -- everything in
    between (combat, rewards, events, rest, shop) is inside the room -- or the
    game_over, which is a death (``died``) unless it is a victory. A move whose
    next map_select is in a different act is the boss: room_type "Boss". A
    second move sent before the first one resolved replaces it (cannot happen in
    a harness log: an engine error ends the run).
    """
    meta = next((r for r in rows if isinstance(r, dict) and r.get("type") == "run_meta"), {})
    last_map_select = None
    pending = None
    over = None
    snapshots: list[dict] = []
    transitions: list[dict] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        data = row.get("data")
        if kind not in ("action", "state") or not isinstance(data, dict):
            continue
        if kind == "action":
            if (data.get("cmd") != "action" or data.get("action") != "select_map_node"
                    or last_map_select is None):
                continue
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            node = next((c for c in last_map_select.get("choices") or []
                         if isinstance(c, dict)
                         and (c.get("col"), c.get("row")) == (args.get("col"), args.get("row"))),
                        None)
            if node is None:
                continue
            pre = state_features(last_map_select)
            room_type = node.get("type") or "Unknown"
            snapshots.append({"features": pre, "choice_type": room_type})
            pending = {"act": pre["act"], "room_type": room_type, "pre": pre}
            continue
        if data.get("type") != "decision":
            continue
        decision = data.get("decision")
        if decision == "map_select":
            if pending is not None:
                post = state_features(data)
                if post["act"] != pending["act"]:
                    pending["room_type"] = "Boss"
                transitions.append({**pending, "post": post, "died": False})
                pending = None
            last_map_select = data
        elif decision == "game_over":
            over = data
            died = not data.get("victory")
            if pending is not None:
                transitions.append({**pending, "post": state_features(data), "died": died})
                pending = None
            break

    if over is None:
        return None
    return {
        "character": meta.get("character"),
        "ascension": meta.get("ascension"),
        "seed": meta.get("seed"),
        "won": bool(over.get("victory")),
        "final_gf": state_features(over)["gf"],
        "snapshots": snapshots,
        "transitions": transitions,
    }


def load_games(paths: list[str]) -> list[dict]:
    """extract_transitions over game-log files; skips logs it excludes and never
    raises on one bad file (prints a warning and moves on)."""
    games = []
    for path in paths:
        try:
            with open(path) as fh:
                rows = [json.loads(line) for line in fh if line.strip()]
            game = extract_transitions(rows)
        except (OSError, ValueError) as exc:
            print(f"  !! route_data: skipping {path}: {exc!r}")
            continue
        if game is not None:
            games.append(game)
    return games
