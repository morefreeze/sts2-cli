#!/usr/bin/env python3
"""eval_rl.py — Standalone evaluation for a saved MaskablePPO checkpoint.

Usage:
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip --n-games 20
    python agent/eval_rl.py checkpoints/ppo_ironclad_1448k.zip --verbose
"""
import argparse, json, os, random, signal, sys, time
from collections import Counter

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sb3_contrib import MaskablePPO
from sb3_contrib.common.wrappers import ActionMasker
from agent.combat_env import CombatEnv, greedy_action
from agent.train import mask_fn
from agent.card_scoring import score_card_in_deck, deck_quality_score


_GAME_TIMEOUT_SEC = 300  # 5 min per game — kills deadlocked C# processes

# STS2_PLANNER=1 → combat decisions use turn_planner search instead of the
# PPO policy (Jun 11). Falls back to model.predict per-decision on any failure.
# STS2_PLANNER=lethal → hybrid: planner only intervenes when it finds a
# provable all-enemies-dead sequence this turn; policy plays everything else.
_PLANNER_ENV = os.environ.get("STS2_PLANNER", "").lower()
_PLANNER_ON = _PLANNER_ENV in ("1", "true", "on", "lethal")
_PLANNER_LETHAL_ONLY = _PLANNER_ENV == "lethal"

# STS2_DEFENSE=1 → intent-aware defense override (Jun 13). Narrow hybrid:
# when an enemy telegraphs a dangerous attack and the policy isn't blocking,
# insert the best block card. Leaves attack decisions to the policy.
_DEFENSE_ON = os.environ.get("STS2_DEFENSE", "") in ("1", "true", "on")
_MIDACT_SNAPSHOT_DIR = "data/snapshots/midact_elite"


def format_floor_label(floor: int | float | str | None) -> str:
    """Format absolute run floor as act-relative label, e.g. 22 -> A2F5."""
    try:
        floor_i = int(floor)
    except (TypeError, ValueError):
        return "?"
    if floor_i <= 0:
        return "?"
    act = ((floor_i - 1) // 17) + 1
    act_floor = ((floor_i - 1) % 17) + 1
    return f"A{act}F{act_floor}"


def format_floor_labels(floors: list[int] | tuple[int, ...]) -> str:
    return "[" + ", ".join(format_floor_label(f) for f in sorted(floors)) + "]"


def global_floor_from_state(state: dict | None, fallback: int = 1) -> int:
    """Convert the engine's act-local floor into an absolute run floor."""
    if not isinstance(state, dict):
        return int(fallback)
    context = state.get("context") or {}
    act = context.get("act") or 1
    floor = state.get("floor") or context.get("floor")
    if not isinstance(act, (int, float)) or not isinstance(floor, (int, float)):
        return int(fallback)
    if act < 1 or floor < 1:
        return int(fallback)
    return (int(act) - 1) * 17 + int(floor)


def _resolve_combat_snapshot_config(*, preset: str | None,
                                    snapshot_dir: str | None,
                                    floors_spec: str | None) -> tuple[str | None, set[int] | None]:
    if preset == "midact-elite":
        snapshot_dir = snapshot_dir or _MIDACT_SNAPSHOT_DIR
        floors_spec = floors_spec or "6,7"
    floors = None
    if floors_spec:
        try:
            floors = {int(token.strip()) for token in floors_spec.split(",")
                      if token.strip()}
        except ValueError:
            raise ValueError("combat snapshot floors must be comma-separated integers") from None
        if not floors or any(floor <= 0 for floor in floors):
            raise ValueError("combat snapshot floors must be positive integers")
    if bool(snapshot_dir) != bool(floors):
        raise ValueError("combat snapshot directory and floors must both be provided")
    return snapshot_dir, floors


def classify_eval_result(*, timed_out: bool, run_won: bool, info: dict) -> str:
    """Classify one completed evaluation attempt."""
    if timed_out or info.get("timeout"):
        return "timeout"
    if run_won:
        return "win"
    if info.get("stuck"):
        return "stuck"
    if info.get("crashed"):
        return "crash"
    return "dead"


def should_retry_invalid_result(status: str, *, retries_used: int,
                                retry_limit: int) -> bool:
    return status in {"crash", "timeout", "stuck"} and retries_used < retry_limit


def summarize_eval_results(results: list[dict], *, requested_n: int,
                           total_attempts: int,
                           attempt_statuses: list[str] | None = None) -> dict:
    """Aggregate only legitimate wins/deaths into policy performance metrics."""
    valid = [r for r in results if r.get("status") in {"win", "dead"}]
    floors = [int(r.get("floor", 0) or 0) for r in valid]
    combat_wins = [int(r.get("combat_wins", 0) or 0) for r in valid]
    wins = [1 if r.get("status") == "win" else 0 for r in valid]
    statuses = (attempt_statuses if attempt_statuses is not None
                else [str(r.get("status", "unknown")) for r in results])
    counts = Counter(statuses)
    invalid_attempts = sum(
        count for status, count in counts.items()
        if status in {"crash", "timeout", "stuck"}
    )
    return {
        "avg_floor": float(np.mean(floors)) if floors else 0.0,
        "max_floor": int(max(floors)) if floors else 0,
        "win_rate": float(np.mean(wins)) if wins else 0.0,
        "avg_combat_wins": float(np.mean(combat_wins)) if combat_wins else 0.0,
        "floors": floors,
        "n": len(valid),
        "valid_n": len(valid),
        "invalid_n": len(results) - len(valid),
        "invalid_attempts": invalid_attempts,
        "requested_n": requested_n,
        "attempts": total_attempts,
        "status_counts": dict(sorted(counts.items())),
        "results": results,
    }


class _GameTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _GameTimeout()


def _card_id(c: dict) -> str:
    cid = c.get("id", "?")
    if isinstance(cid, dict):
        cid = cid.get("en", str(cid))
    cid = str(cid)
    if cid.upper().startswith("CARD."):
        cid = cid[5:]
    return cid


def _card_name(c: dict) -> str:
    n = c.get("name", {})
    if isinstance(n, dict):
        return n.get("en", str(n))
    return str(n)


def _is_boss_combat_round1(state: dict) -> bool:
    if not isinstance(state, dict) or state.get("decision") != "combat_play":
        return False
    if state.get("round", 0) != 1:
        return False
    room_type = (state.get("context") or {}).get("room_type", "")
    return "boss" in str(room_type).lower()


def _write_boss_deck_record(state: dict, *, checkpoint: str, character: str,
                            game_seed: str, game_index: int, jsonl_path: str) -> None:
    deck = state.get("player", {}).get("deck") or []
    if not deck:
        return
    cards = []
    for c in deck:
        try:
            sc = score_card_in_deck(c, deck)
        except Exception:
            sc = None
        cards.append({
            "id": _card_id(c),
            "name": _card_name(c),
            "score": round(sc, 3) if sc is not None else None,
        })
    _context = state.get("context") or {}
    _boss = _context.get("boss") or {}
    _player = state.get("player") or {}
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checkpoint": os.path.basename(str(checkpoint)) if checkpoint else None,
        "checkpoint_path": os.path.abspath(str(checkpoint)) if checkpoint else None,
        "character": character,
        "seed": game_seed,
        "game_index": game_index,
        "floor": state.get("floor") or _context.get("floor"),
        "act": state.get("act") or _context.get("act"),
        "room_type": _context.get("room_type", ""),
        "boss": str(_boss.get("id") or "").replace("_BOSS", "") or None,
        "hp_at_entry": _player.get("hp"),
        "max_hp": _player.get("max_hp"),
        "relics": [
            (r.get("name", {}) or {}).get("en") if isinstance(r.get("name"), dict)
            else r.get("name")
            for r in (_player.get("relics") or [])
        ],
        "deck_size": len(deck),
        "deck_quality": round(deck_quality_score(deck), 3),
        "cards": cards,
    }
    os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_combat_round1(state: dict) -> bool:
    """Round-1 of any combat (boss-agnostic). Caller does the floor gate using
    env._current_floor, because state['floor'] is None during combat."""
    return (isinstance(state, dict) and state.get("decision") == "combat_play"
            and state.get("round", 0) == 1)


def _write_combat_record(state: dict, *, floor: int, checkpoint: str, character: str,
                         game_seed: str, game_index: int, jsonl_path: str) -> None:
    """Like _write_boss_deck_record but boss-agnostic AND captures the enemies
    (name/hp/intents) — the killer info for diagnosing where runs die."""
    deck = state.get("player", {}).get("deck") or []
    cards = []
    for c in deck:
        try:
            sc = score_card_in_deck(c, deck)
        except Exception:
            sc = None
        cards.append({"id": _card_id(c), "name": _card_name(c),
                      "score": round(sc, 3) if sc is not None else None})
    enemies = []
    for e in state.get("enemies", []) or []:
        nm = e.get("name")
        if isinstance(nm, dict):
            nm = nm.get("en", str(nm))
        intents = [{"type": it.get("type"), "damage": it.get("damage", 0),
                    "hits": it.get("hits") or 1}
                   for it in (e.get("intents") or [])]
        enemies.append({"name": nm, "hp": e.get("hp"),
                        "max_hp": e.get("max_hp"), "intents": intents})
    _player = state.get("player") or {}
    _context = state.get("context") or {}
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "checkpoint": os.path.basename(str(checkpoint)) if checkpoint else None,
        "checkpoint_path": os.path.abspath(str(checkpoint)) if checkpoint else None,
        "character": character,
        "seed": game_seed,
        "game_index": game_index,
        "floor": floor,
        "act": state.get("act") or _context.get("act"),
        "room_type": _context.get("room_type", ""),
        "hp_at_entry": _player.get("hp"),
        "max_hp": _player.get("max_hp"),
        "relics": [
            (r.get("name", {}) or {}).get("en") if isinstance(r.get("name"), dict)
            else r.get("name")
            for r in (_player.get("relics") or [])
        ],
        "deck_size": len(deck),
        "deck_quality": round(deck_quality_score(deck), 3) if deck else None,
        "enemies": enemies,
        "cards": cards,
    }
    os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _fmt_hand(state: dict) -> str:
    hand = state.get("hand", [])
    if not hand:
        return "[]"
    parts = []
    for c in hand:
        cid = _card_id(c)
        cost = c.get("cost", "?")
        parts.append(f"{cid}({cost})")
    return "[" + ", ".join(parts) + "]"


def _fmt_enemies(state: dict) -> str:
    enemies = state.get("enemies", [])
    if not enemies:
        return "[]"
    parts = []
    for e in enemies:
        hp = e.get("hp", "?")
        mhp = e.get("max_hp", "?")
        name = e.get("name", "?")
        if isinstance(name, dict):
            name = name.get("en", str(name))
        intents = e.get("intents") or []
        intent_str = ""
        for it in intents:
            t = it.get("type", "?")
            dmg = it.get("damage", 0)
            hits = it.get("hits") or 1
            if t.lower() == "attack":
                intent_str += f" ATK{dmg}x{hits}"
            else:
                intent_str += f" {t[:3]}"
        parts.append(f"{name}({hp}/{mhp}{intent_str})")
    return "[" + ", ".join(parts) + "]"


def _decode_action_name(env: CombatEnv, action: int, state: dict) -> str:
    """Decode action index to human-readable string."""
    try:
        cmd = env.enc.decode(action, state)
        act = cmd.get("action", "?")
        args = cmd.get("args", {})
        if act == "play_card":
            ci = args.get("card_index", 0)
            hand = state.get("hand", [])
            card = hand[ci] if ci < len(hand) else {}
            return f"play {_card_id(card)}"
        elif act == "end_turn":
            return "end_turn"
        return f"{act}({args})"
    except Exception:
        return f"action_{action}"


class _VerboseCombatEnv(CombatEnv):
    """CombatEnv subclass that logs room transitions during _advance_to_combat."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.room_log: list[str] = []   # per-game room entries
        self.combat_steps: list[str] = []  # per-combat step log
        self.trace_combat = False  # set True for last-combat step trace

    def _advance_to_combat(self, state):
        for _ in range(200):
            if state is None:
                return {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}
            if state.get("decision") == "combat_play":
                return self._greedy_use_potions(state)
            if state.get("decision") == "game_over":
                return state
            dec = state.get("decision", "")
            self._log_room(state, dec)
            cmd = greedy_action(state)
            state = self._send(cmd)
        return state or {"decision": "game_over", "victory": False, "player": {"hp": 0, "max_hp": 80}}

    def _log_room(self, state: dict, dec: str):
        fl = state.get("floor") or state.get("context", {}).get("floor", "?")
        player = state.get("player", {})
        hp = player.get("hp", "?")
        mhp = player.get("max_hp", "?")
        hp_str = f"HP={hp}/{mhp}" if hp != "?" else "HP=?"

        if dec == "map_select":
            choices = state.get("choices", [])
            types = [c.get("type", "?") for c in choices]
            cmd = greedy_action(state)
            sel_col = cmd.get("args", {}).get("col", -1)
            sel = next((c for c in choices if c.get("col") == sel_col), {})
            chosen = sel.get("type", "?")
            self.room_log.append(
                f"  [map  fl={fl}] {hp_str} options={types} → {chosen}")

        elif dec == "event_choice":
            name = state.get("event_name", "?")
            opts = state.get("options", [])
            available = [o for o in opts if not o.get("is_locked")]
            from agent.combat_env import _score_event_option
            if available:
                best = max(available, key=_score_event_option)
                chosen_title = best.get("title", "?")
            else:
                chosen_title = "leave"
            titles = [o.get("title", "?") for o in opts]
            self.room_log.append(
                f"  [event fl={fl}] {hp_str} {name}: {titles} → {chosen_title}")

        elif dec == "rest_site":
            opts = [o.get("option_id", o.get("title", "?")) for o in state.get("options", [])]
            cmd = greedy_action(state)
            chosen = cmd.get("args", {}).get("option_index", "?")
            opted = next((o for o in state.get("options", []) if o.get("index") == chosen), {})
            chosen_name = opted.get("option_id", opted.get("title", "?"))
            self.room_log.append(
                f"  [rest fl={fl}] {hp_str} options={opts} → {chosen_name}")

        elif dec == "card_reward":
            from agent.card_scoring import score_card
            cards = state.get("cards", [])
            cmd = greedy_action(state)
            chosen = "SKIP"
            if cmd.get("action") == "select_card_reward":
                selected_idx = (cmd.get("args") or {}).get("card_index")
                selected = next(
                    (card for pos, card in enumerate(cards)
                     if card.get("index", pos) == selected_idx),
                    None,
                )
                if selected is not None:
                    chosen = _card_id(selected)
            top3 = [(score_card(c), _card_id(c)) for c in cards]
            top3.sort(reverse=True)
            self.room_log.append(
                f"  [reward fl={fl}] {hp_str} top={[f'{n}({s:.1f})' for s,n in top3[:3]]} → {chosen}")

        elif dec == "shop":
            gold = state.get("player", {}).get("gold", 0)
            self.room_log.append(f"  [shop  fl={fl}] {hp_str} gold={gold}")

        elif dec == "treasure":
            self.room_log.append(f"  [treas fl={fl}] {hp_str}")


def run_eval_verbose(model, character: str, n_games: int = 10,
                     fixed_seeds: bool = True, seed_offset: int = 0,
                     invalid_retries: int = 1,
                     verbose: bool = False,
                     replay_actions: list = None,
                     load_seed: str = None,
                     native_save_path: str = None,
                     boss_deck_log_path: str = None,
                     boss_snapshot_dir: str = None,
                     boss_snapshot_min_hp: int = 50,
                     combat_snapshot_dir: str = None,
                     combat_snapshot_floors: set = None,
                     checkpoint_name: str = None) -> dict:
    """Full-run eval with per-game floor breakdown.

    verbose=True: show per-room summaries; for wins, show last combat step-by-step.
    replay_actions/load_seed: replay action log after start_run.
    native_save_path: load a binary .save via load_save instead of start_run.
    boss_deck_log_path: when set, append a JSONL record of the deck + per-card
        scores at the start of each boss combat (one record per boss-floor).
    """
    # Auto-detect obs_size from model width (161=legacy, 169=extra, 441=extra+relic)
    from agent.state_encoder import obs_flags_for_size
    model_obs_size = model.observation_space.shape[0]
    extra_obs, relic_obs = obs_flags_for_size(model_obs_size, 161)

    if n_games < 1:
        raise ValueError("n_games must be at least 1")
    if invalid_retries < 0:
        raise ValueError("invalid_retries cannot be negative")

    results: list[dict] = []
    attempt_statuses: list[str] = []
    boss_records = []  # per-game: {"boss": id, "reached": bool, "won": bool}
    i = 0
    retries_used = 0
    total_attempts = 0
    game_seed = None
    while i < n_games:
        total_attempts += 1
        if retries_used == 0:
            if load_seed:
                game_seed = load_seed
            elif fixed_seeds:
                game_seed = f"eval_fixed_{i + seed_offset}"
            else:
                game_seed = f"eval_r{random.randint(0, 0xFFFFFF):06x}_{i}"

        env_kwargs = dict(character=character, seed=game_seed,
                          seed_prefix=f"eval_{i}", max_floor=0, extra_obs=extra_obs,
                          relic_obs=relic_obs)
        if replay_actions:
            env_kwargs["replay_actions"] = replay_actions
        if native_save_path:
            env_kwargs["native_save_path"] = native_save_path

        if verbose:
            env = _VerboseCombatEnv(**env_kwargs)
        else:
            env = CombatEnv(**env_kwargs)
        env_wrapped = ActionMasker(env, mask_fn)
        obs, _ = env_wrapped.reset()
        ep_combat_wins = 0
        max_floor = global_floor_from_state(env._current_state, fallback=1)
        run_won = False
        run_over = False
        timed_out = False
        all_combat_logs = []  # list of (floor, steps_log)
        # Track which boss floors we've already logged this run, so each boss
        # combat (Act 1 / Act 2 / Act 3) records exactly once at round 1.
        logged_boss_floors: set = set()
        snapshotted_boss_floors: set = set()  # boss snapshot save dedup, per game
        snapshotted_combat_floors: set = set()  # combat-floor snapshot dedup, per game
        game_boss_id = None  # act boss from state.context.boss (known from floor 1)

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(_GAME_TIMEOUT_SEC)
        try:
            while not run_over:
                done = False
                last_info = {}
                cur_floor = global_floor_from_state(
                    env._current_state,
                    fallback=env._current_floor,
                )
                hp_before = (env._current_state.get("player", {}).get("hp", "?")
                             if env._current_state else "?")
                steps_log = []

                while not done:
                    state_snap = env._current_state
                    state_floor = global_floor_from_state(
                        state_snap,
                        fallback=env._current_floor,
                    )
                    max_floor = max(max_floor, state_floor)
                    if game_boss_id is None and state_snap:
                        _b = (state_snap.get("context") or {}).get("boss") or {}
                        _bid = _b.get("id")
                        if _bid:
                            game_boss_id = str(_bid).replace("_BOSS", "")
                    if boss_deck_log_path and _is_boss_combat_round1(state_snap):
                        fl = state_floor
                        if fl not in logged_boss_floors:
                            _write_boss_deck_record(
                                state_snap, checkpoint=checkpoint_name,
                                character=character, game_seed=game_seed,
                                game_index=i, jsonl_path=boss_deck_log_path)
                            logged_boss_floors.add(fl)
                    if boss_snapshot_dir and _is_boss_combat_round1(state_snap):
                        fl = state_floor
                        hp = state_snap.get("player", {}).get("hp", 0)
                        if (fl not in snapshotted_boss_floors
                                and isinstance(hp, (int, float))
                                and hp >= boss_snapshot_min_hp):
                            os.makedirs(boss_snapshot_dir, exist_ok=True)
                            safe_seed = "".join(c if c.isalnum() else "_" for c in str(game_seed))
                            snap_name = f"{safe_seed}_fl{fl}_hp{int(hp)}.save"
                            snap_path = os.path.join(boss_snapshot_dir, snap_name)
                            save_result = env._send({"cmd": "write_continue_save",
                                                     "path": snap_path})
                            if save_result and save_result.get("success"):
                                meta_path = snap_path + ".meta.json"
                                with open(meta_path, "w") as mf:
                                    json.dump({
                                        "seed": game_seed,
                                        "character": character,
                                        "floor": fl,
                                        "hp_at_boss": int(hp),
                                        "save_path": snap_path,
                                        "checkpoint": (os.path.basename(str(checkpoint_name))
                                                       if checkpoint_name else None),
                                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    }, mf, indent=2)
                                print(f"  [snapshot] {snap_name} (hp={int(hp)})")
                            snapshotted_boss_floors.add(fl)
                    if (combat_snapshot_floors and _is_combat_round1(state_snap)
                            and state_floor in combat_snapshot_floors):
                        fl = state_floor
                        if fl not in snapshotted_combat_floors:
                            snapshotted_combat_floors.add(fl)
                            os.makedirs(combat_snapshot_dir, exist_ok=True)
                            _write_combat_record(
                                state_snap, floor=fl, checkpoint=checkpoint_name,
                                character=character, game_seed=game_seed,
                                game_index=i,
                                jsonl_path=os.path.join(combat_snapshot_dir, "records.jsonl"))
                            hp = state_snap.get("player", {}).get("hp", 0)
                            hp_i = int(hp) if isinstance(hp, (int, float)) else 0
                            safe_seed = "".join(c if c.isalnum() else "_" for c in str(game_seed))
                            snap_path = os.path.join(
                                combat_snapshot_dir, f"{safe_seed}_fl{fl}_hp{hp_i}.save")
                            sr = env._send({"cmd": "write_continue_save", "path": snap_path})
                            if sr and sr.get("success"):
                                print(f"  [combat-snap] fl{fl} hp{hp_i} {os.path.basename(snap_path)}")
                    masks = env_wrapped.action_masks()
                    action = None
                    if _PLANNER_ON and state_snap and state_snap.get("decision") == "combat_play":
                        try:
                            from agent.turn_planner import plan_action
                            action = plan_action(state_snap, masks,
                                                 lethal_only=_PLANNER_LETHAL_ONLY)
                        except Exception:
                            action = None
                    if action is None:
                        action, _ = model.predict(obs, deterministic=True, action_masks=masks)

                    # Intent-aware defense override: only when policy isn't
                    # already blocking and a dangerous attack is incoming.
                    if (_DEFENSE_ON and state_snap
                            and state_snap.get("decision") == "combat_play"):
                        try:
                            from agent.turn_planner import (intent_defense_override,
                                                            _action_is_defense)
                            if not _action_is_defense(state_snap, int(action)):
                                ov = intent_defense_override(state_snap, masks)
                                if ov is not None:
                                    action = ov
                        except Exception:
                            pass

                    if verbose and state_snap:
                        action_name = _decode_action_name(env, int(action), state_snap)
                        ph = state_snap.get("player", {}).get("hp", "?")
                        pb = state_snap.get("player", {}).get("block", 0)
                        rnd = state_snap.get("round", "?")
                        hand_str = _fmt_hand(state_snap)
                        enemy_str = _fmt_enemies(state_snap)
                        steps_log.append(
                            f"    r{rnd} HP={ph}({pb}blk) | {enemy_str} | hand={hand_str} → {action_name}")

                    obs, _r, terminated, truncated, last_info = env_wrapped.step(int(action))
                    done = terminated or truncated
                    f = last_info.get("floor", 0)
                    if f:
                        max_floor = max(max_floor, f)

                hp_after = (env._current_state.get("player", {}).get("hp", "?")
                            if env._current_state else "?")
                all_combat_logs.append((cur_floor, hp_before, hp_after, steps_log))

                if last_info.get("combat_won"):
                    ep_combat_wins += 1
                    floor_won = last_info.get("floor", 0)
                    obs, reset_info = env_wrapped.reset()
                    # reset() returns game_over info when run ended during advance
                    # (crash or legit game_over between combats)
                    if reset_info.get("game_over"):
                        run_over = True
                        last_info = reset_info
                        if reset_info.get("victory"):
                            run_won = True
                else:
                    run_over = True
                    if last_info.get("victory"):
                        run_won = True
        except _GameTimeout:
            timed_out = True
        finally:
            signal.alarm(0)

        env_wrapped.close()
        status = classify_eval_result(
            timed_out=timed_out,
            run_won=run_won,
            info=last_info,
        )
        attempt_statuses.append(status)
        retrying = should_retry_invalid_result(
            status,
            retries_used=retries_used,
            retry_limit=invalid_retries,
        )
        end_reason = status.upper() if status in {"win", "timeout"} else status
        retry_note = (f"; retry {retries_used + 1}/{invalid_retries}"
                      if retrying else "")
        print(f"  game {i+1:2d}: floor={format_floor_label(max_floor):>5s} combats={ep_combat_wins} "
              f"boss={game_boss_id or '?':<20s} [{end_reason}{retry_note}]")

        if verbose:
            # Print per-room log
            room_log = getattr(env, "room_log", [])
            # Print combat summaries interleaved with room log
            combat_idx = 0
            for entry in room_log:
                # Flush pending combat summary if any
                print(entry)

            # Print all combat summaries
            for fl, hp_b, hp_a, steps in all_combat_logs:
                mhp = "?"
                if env._current_state:
                    mhp = env._current_state.get("player", {}).get("max_hp", "?")
                result = "won" if (fl, hp_b, hp_a, steps) != all_combat_logs[-1] or run_won else "dead"
                if (fl, hp_b, hp_a, steps) == all_combat_logs[-1]:
                    result = "WIN" if run_won else "dead"
                else:
                    result = "won"
                print(f"  [combat {format_floor_label(fl)}] HP {hp_b}→{hp_a} [{result}]")

            # Print last combat step-by-step for wins OR for boss-reach (fl≥17) defeats —
            # the latter is what we need to debug "got close to boss kill" scenarios.
            if all_combat_logs:
                last_fl, _, _, last_steps = all_combat_logs[-1]
                if run_won or (isinstance(last_fl, int) and last_fl >= 14):
                    label = "WIN" if run_won else f"DEFEAT (deep, {format_floor_label(last_fl)})"
                    print(f"\n  === Last combat ({format_floor_label(last_fl)}) [{label}] step-by-step ===")
                    for step in last_steps:
                        print(step)
            print()

        if retrying:
            retries_used += 1
            continue

        results.append({
            "seed": game_seed,
            "game_index": i,
            "status": status,
            "floor": max_floor,
            "combat_wins": ep_combat_wins,
            "boss": game_boss_id or "?",
            "run_won": run_won,
            "attempts": retries_used + 1,
        })
        boss_beaten = run_won or max_floor > 17
        if status in {"win", "dead"}:
            boss_records.append({"boss": game_boss_id or "?",
                                 "reached": max_floor >= 17, "won": boss_beaten})

        # Result row for the boss-deck log: joined by seed in the viewer.
        # Only written when this game logged a boss-entry deck (reached boss).
        if boss_deck_log_path and logged_boss_floors:
            try:
                with open(boss_deck_log_path, "a") as _bf:
                    _bf.write(json.dumps({
                        "event": "result", "seed": game_seed,
                        "game_index": i, "boss": game_boss_id,
                        "max_floor": max_floor, "run_won": run_won,
                        "boss_beaten": boss_beaten,
                        "end_reason": status,
                        "attempts": retries_used + 1,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass

        i += 1
        retries_used = 0

    # Per-boss aggregation: games / boss-reach / wins per act boss.
    per_boss: dict = {}
    for rec in boss_records:
        b = per_boss.setdefault(rec["boss"], {"games": 0, "reached": 0, "won": 0})
        b["games"] += 1
        b["reached"] += int(rec["reached"])
        b["won"] += int(rec["won"])

    stats = summarize_eval_results(
        results,
        requested_n=n_games,
        total_attempts=total_attempts,
        attempt_statuses=attempt_statuses,
    )
    stats["per_boss"] = per_boss
    return stats


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint", help="Explicit path to checkpoint zip")
    p.add_argument("--character", default="Ironclad")
    p.add_argument("--n-games", type=int, default=20)
    seed_group = p.add_mutually_exclusive_group()
    seed_group.add_argument("--fixed-seeds", dest="fixed_seeds", action="store_true",
                            help="Use fixed eval_fixed_0..N seeds (default)")
    seed_group.add_argument("--random-seeds", dest="fixed_seeds", action="store_false",
                            help="Use a new random seed for each requested game")
    p.set_defaults(fixed_seeds=True)
    p.add_argument("--seed-offset", type=int, default=0,
                   help="Offset for fixed seed index")
    p.add_argument("--invalid-retries", type=int, default=1,
                   help="Retry crash/timeout/stuck attempts on the same seed (default: 1)")
    p.add_argument("--verbose", action="store_true", default=False,
                   help="Per-room summaries + detailed last-combat trace on wins")
    p.add_argument("--load", default=None,
                   help="Replay actions from a play.py save (.json) before agent takes over")
    p.add_argument("--deck-log", default="data/eval_decks.jsonl",
                   help="JSONL file to append deck + per-card scores at the start of each "
                        "boss combat. Pass 'none' to disable.")
    p.add_argument("--boss-snapshot-dir", default=None,
                   help="Directory to write C# game-state save when entering Boss combat "
                        "at round 1 with HP >= --boss-snapshot-min-hp. One save per boss "
                        "floor per game. Pair with agent/boss_retry.py to diagnose boss loss.")
    p.add_argument("--boss-snapshot-min-hp", type=int, default=50,
                   help="Only snapshot when player HP at boss-entry is >= this (default 50).")
    p.add_argument("--combat-snapshot-dir", default=None,
                   help="Directory to write a save + records.jsonl (deck/enemies/hp) at "
                        "round 1 of combats on --combat-snapshot-floors (boss-agnostic). "
                        "Diagnose mid-Act death walls (e.g. floor 15). Replay saves with boss_retry.py.")
    p.add_argument("--combat-snapshot-floors", default=None,
                   help="Comma-separated floors to capture for --combat-snapshot-dir, e.g. '7,8,9,15'.")
    p.add_argument("--snapshot-preset", choices=("midact-elite",), default=None,
                   help="Named snapshot preset; midact-elite captures floors 6 and 7")
    return p


def main():
    import json as _json

    p = _build_parser()
    args = p.parse_args()
    boss_deck_log_path = (None if str(args.deck_log).lower() in ("", "none")
                          else args.deck_log)
    try:
        combat_snapshot_dir, combat_snapshot_floors = _resolve_combat_snapshot_config(
            preset=args.snapshot_preset,
            snapshot_dir=args.combat_snapshot_dir,
            floors_spec=args.combat_snapshot_floors,
        )
    except ValueError as exc:
        p.error(str(exc))

    replay_actions = None
    load_seed = None
    native_save_path = None
    character = args.character
    n_games = args.n_games
    if args.load:
        load_path = os.path.abspath(args.load)
        if load_path.endswith(".save"):
            native_save_path = load_path
        else:
            with open(load_path) as f:
                save = _json.load(f)
            if "actions" not in save:
                print(f"Error: {load_path} is not a play.py replay save (no 'actions' key)")
                sys.exit(1)
            character = save.get("character", character)
            load_seed = save.get("seed")
            replay_actions = save["actions"]
        # Result is deterministic post-load; default to 1 game unless user passed --n-games.
        if "--n-games" not in sys.argv and "-n" not in sys.argv:
            n_games = 1

    checkpoint = args.checkpoint
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = MaskablePPO.load(checkpoint, device=device)

    seed_mode = ("native_save" if native_save_path
                 else "loaded" if load_seed
                 else "fixed" if args.fixed_seeds
                 else "random")
    print(f"Checkpoint    : {checkpoint}")
    print(f"Internal steps: {getattr(model, 'num_timesteps', '?')}")
    if native_save_path:
        print(f"Native save   : {native_save_path}")
    elif args.load:
        print(f"Loaded save   : {args.load}")
        print(f"  character   : {character}")
        print(f"  seed        : {load_seed}")
        print(f"  actions     : {len(replay_actions)}")
    print(f"Running {n_games} games ({seed_mode} seeds, max_floor=unlimited)...")

    stats = run_eval_verbose(model, character, n_games=n_games,
                             fixed_seeds=args.fixed_seeds, seed_offset=args.seed_offset,
                             invalid_retries=args.invalid_retries,
                             verbose=args.verbose,
                             replay_actions=replay_actions, load_seed=load_seed,
                             native_save_path=native_save_path,
                             boss_deck_log_path=boss_deck_log_path,
                             boss_snapshot_dir=args.boss_snapshot_dir,
                             boss_snapshot_min_hp=args.boss_snapshot_min_hp,
                             combat_snapshot_dir=combat_snapshot_dir,
                             combat_snapshot_floors=combat_snapshot_floors,
                             checkpoint_name=checkpoint)
    print(f"---")
    print(f"valid games    : {stats['valid_n']}/{stats['requested_n']} "
          f"(attempts={stats['attempts']}, invalid_seeds={stats['invalid_n']}, "
          f"invalid_attempts={stats['invalid_attempts']})")
    print(f"status counts  : {stats['status_counts']}")
    print(f"avg_floor      : {stats['avg_floor']:.1f}")
    print(f"max_floor      : {format_floor_label(stats['max_floor'])}")
    print(f"win_rate       : {stats['win_rate']:.0%}")
    print(f"avg_combat_wins: {stats['avg_combat_wins']:.1f}")
    print(f"floor dist     : {format_floor_labels(stats['floors'])}")
    per_boss = stats.get("per_boss") or {}
    if per_boss:
        print(f"--- per-boss ---")
        for bid in sorted(per_boss):
            b = per_boss[bid]
            print(f"  {bid:<22s} games={b['games']:>2} "
                  f"reach={b['reached']:>2} ({b['reached']/max(b['games'],1):.0%}) "
                  f"win={b['won']:>2}")


if __name__ == "__main__":
    main()
