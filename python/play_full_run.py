#!/usr/bin/env python3
"""
Play a full STS2 run using the headless simulator with a random agent.

Usage:
  python3 play_full_run.py <num_runs> [character]

Arguments:
  num_runs    Positive integer: number of runs to play
  character   One of: Ironclad, Silent, Defect, Regent, Necrobinder (default: Ironclad)

Examples:
  python3 play_full_run.py 5
  python3 play_full_run.py 3 Silent
"""

import argparse
import json
import subprocess
import sys
import random
import os
import shutil
import traceback
from game_log import GameLogger
from engine_process import EngineHang, EngineProcess
from combat_plan_driver import (
    _norm_entity_id,
    _resolve_card_index,
    _resolve_potion_index,
    _resolve_enemy_target_index,
    _resolve_choice_indices,
    _apply_action_choices,
    _execute_combat_plan_actions,
)

VALID_CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]

# Which characters route their combat_play decisions through the ported
# Combat Solver (plan_combat_turn) instead of the simple one-card-at-a-time
# heuristic. Phase 1 hard-coded this to Ironclad because the resolution glue
# (_resolve_card_index / _resolve_potion_index / _apply_action_choices) had
# only ever been exercised against Ironclad's mechanics. Phase 2 smoke-tested
# the other four one at a time (Silent/Defect/Regent/Necrobinder, 3 games
# each): Defect/Regent/Necrobinder produced real plans with zero resolution
# failures and zero engine refusals on the first try, and Silent joined them
# once 5e9a1c9 removed the Neutralize.OnPlay patch that had been making every
# Silent capture throw. STS2_SOLVER_CHARS=none turns the solver off, which is
# what the paired A/B's control arm uses.
_SOLVER_CHARS_DEFAULT = frozenset(VALID_CHARACTERS)


def solver_characters(env=None) -> set:
    """Resolve the set of characters allowed to call plan_combat_turn.

    STS2_SOLVER_CHARS accepts a comma-separated character list, "all", or
    "none". Unset/blank means the default. An unrecognized name raises rather
    than being dropped: a typo that silently disabled the solver would make a
    whole regression or A/B run measure the opposite of what it claims to.
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_SOLVER_CHARS") or "").strip()
    if not raw:
        return set(_SOLVER_CHARS_DEFAULT)
    if raw.lower() == "all":
        return set(VALID_CHARACTERS)
    if raw.lower() == "none":
        return set()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in VALID_CHARACTERS]
    if unknown:
        raise ValueError(
            f"STS2_SOLVER_CHARS names unknown character(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(VALID_CHARACTERS)}"
        )
    return set(names)


# Selectable soft time budgets for one plan_combat_turn call, in seconds. 120
# is the vendored SolverSearchProfile.Default budget that Phase 2's paired A/B
# validated; the others are the tiers under study in Phase 2b-1. Mirrors
# RunSimulator.cs SolverBudgetTiersSeconds -- play_run() cross-checks the
# budget the engine reports on every plan, so a drift fails the run loudly.
SOLVER_BUDGET_TIERS_S = (30, 60, 120, 180, 300)
SOLVER_BUDGET_DEFAULT_S = 120
# How far past its budget a plan_combat_turn reply may run before the watchdog
# declares the engine hung (agent/bug.md BUG-040). The budget is soft -- only
# checked between node expansions -- but the worst overrun in 1,409 measured
# solves was 0.4 s, so 60 s is ~150x headroom while still costing one game
# instead of a whole day.
SOLVER_WATCHDOG_MARGIN_S = 60
# Every other engine reply arrives in well under a second; 120 s only fires on
# a genuine stall.
ENGINE_REPLY_TIMEOUT_S = 120


def solver_budget_seconds(env=None) -> int:
    """Resolve STS2_SOLVER_BUDGET to one of SOLVER_BUDGET_TIERS_S.

    Unset/blank means the validated default. Anything else must be a tier
    written as plain digits; otherwise raise, for the same reason
    solver_characters() does -- a typo must not silently make a run measure a
    different budget than the one it is labelled with.
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_SOLVER_BUDGET") or "").strip()
    if not raw:
        return SOLVER_BUDGET_DEFAULT_S
    if raw.isdigit() and int(raw) in SOLVER_BUDGET_TIERS_S:
        return int(raw)
    raise ValueError(
        f"STS2_SOLVER_BUDGET={raw!r} is not a supported tier; use one of "
        f"{', '.join(str(t) for t in SOLVER_BUDGET_TIERS_S)} (seconds)")


def solver_call_timeout_s(budget_s: int) -> float:
    """Watchdog deadline for one plan_combat_turn reply."""
    return budget_s + SOLVER_WATCHDOG_MARGIN_S


# Out-of-combat decision policy (Phase 3a,
# docs/superpowers/specs/2026-09-30-phase3-outofcombat-and-route-design.md).
# "naive" is the original placeholder policy (card 0, always HEAL, never shop,
# first unlocked event option); "greedy" hands those decisions to
# agent.combat_env.greedy_action, the tuned policy the RL harness uses.
# map_select (seeded random route) and combat_play (solver) are never
# delegated, so an A/B between the two isolates the out-of-combat policy.
OOC_POLICIES = ("naive", "greedy")
OOC_POLICY_DEFAULT = "naive"
OOC_GREEDY_DECISIONS = frozenset({"card_reward", "rest_site", "event_choice",
                                  "bundle_select", "card_select", "shop"})
# A greedy shop visit normally ends in a few buys and a leave_room; this cap
# only exists so a buy the engine accepts without changing anything cannot
# spin until the global STUCK detector kills the run.
SHOP_ACTION_CAP = 20
# A greedy arm whose fallbacks exceed this share of (accepted greedy decisions +
# fallbacks) is partly running the naive policy; summarize() prints a banner.
OOC_FALLBACK_RATE_WARN = 0.05
# Why a greedy decision fell back to the naive command. "raised": greedy_action
# threw; "none": it returned nothing; "refused": the engine rejected its command;
# "shop_cap": a shop visit exceeded SHOP_ACTION_CAP.
OOC_FALLBACK_CAUSES = ("raised", "refused", "none", "shop_cap")
# Env knobs agent.combat_env.greedy_action reads. Any that is explicitly set is
# announced by main() and stamped into every result, so an A/B arm that differs
# by one of them cannot be mistaken for a pure policy comparison.
GREEDY_KNOBS = ("STS2_RANDOMIZE", "STS2_DECISION_ADVISOR", "STS2_CARD_THRESHOLD_LIFT",
                "STS2_BASIC_PURGE_ALL", "STS2_CARD_QUALITY_GATE")


def ooc_policy(env=None) -> str:
    """Resolve STS2_OOC_POLICY ("naive" | "greedy"; blank/unset = default).

    An unknown value raises instead of silently running the default, for the
    same reason solver_characters() does: an A/B arm that quietly measured the
    wrong policy still prints "Completed: N/N".
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_OOC_POLICY") or "").strip().lower()
    if not raw:
        return OOC_POLICY_DEFAULT
    if raw not in OOC_POLICIES:
        raise ValueError(f"STS2_OOC_POLICY={raw!r} is not one of {OOC_POLICIES}")
    return raw


def _load_greedy_action():
    """Import agent.combat_env.greedy_action lazily -- the import pulls in
    gymnasium/numpy and the card data (~3 s), which the naive policy never needs.
    Also the seam tests replace to script the policy's answers."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from agent.combat_env import greedy_action
    return greedy_action


def greedy_knobs(env=None) -> dict:
    """The GREEDY_KNOBS that are explicitly set in `env` (name -> raw value)."""
    env = os.environ if env is None else env
    return {k: env[k] for k in GREEDY_KNOBS if k in env}


def naive_ooc_command(state: dict) -> dict:
    """The FIRST command the naive policy sends for this out-of-combat decision.

    Mirrors the naive branches in _play_run() exactly (tests/test_ooc_policy.py
    pins the parity by running the naive arm). It is what a greedy decision falls
    back to when greedy_action raises, returns nothing, or has its command
    refused -- so a fallback plays the control arm's move, never a third policy.
    The naive event-page stuck exit is not mirrored; the greedy branch applies
    that same guard before it ever asks for a fallback.
    """
    decision = state.get("decision", "")
    if decision == "card_reward":
        if state.get("cards", []):
            return {"cmd": "action", "action": "select_card_reward",
                    "args": {"card_index": 0}}
        return {"cmd": "action", "action": "skip_card_reward"}
    if decision == "rest_site":
        enabled = [o for o in state.get("options", []) if o.get("is_enabled", True)]
        heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
        choice = heal or (enabled[0] if enabled else None)
        if choice:
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": choice["index"]}}
        return {"cmd": "action", "action": "leave_room"}
    if decision == "event_choice":
        options = state.get("options", [])
        if options:
            choice = next((o for o in options if not o.get("is_locked")), options[0])
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": choice["index"]}}
        return {"cmd": "action", "action": "leave_room"}
    if decision == "bundle_select":
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}
    if decision == "card_select":
        if state.get("cards", []):
            return {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
        return {"cmd": "action", "action": "skip_select"}
    # "shop" (the naive policy never buys) -- and any decision outside
    # OOC_GREEDY_DECISIONS, which never reaches here.
    return {"cmd": "action", "action": "leave_room"}


def ooc_escape_command(state: dict) -> dict:
    """Last resort when even naive_ooc_command's reply is an error.

    Chosen to always make progress: skip a card reward, take the first bundle,
    select the first card of a mandatory card_select, and otherwise leave the
    room (the same escape the naive branches use after an engine error).
    """
    decision = state.get("decision", "")
    if decision == "card_reward":
        return {"cmd": "action", "action": "skip_card_reward"}
    if decision == "bundle_select":
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}
    if decision == "card_select":
        if state.get("cards"):
            return {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
        return {"cmd": "action", "action": "skip_select"}
    return {"cmd": "action", "action": "leave_room"}


def _find_dotnet():
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet",
              "/usr/share/dotnet/dotnet",
              "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return p
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "dotnet"


DOTNET = _find_dotnet()
# Don't clobber an explicit STS2_GAME_DIR — a caller may point at a
# different install (or a different platform's data dir) and the
# hardcoded path below is only a macOS/Steam default.
os.environ.setdefault("STS2_GAME_DIR", os.path.expanduser(
    "~/Library/Application Support/Steam/steamapps/common/"
    "Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"))
PROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src", "Sts2Headless", "Sts2Headless.csproj")


def engine_argv() -> list:
    """Command line that starts the headless engine. A function (not a constant)
    so tests can point play_run() at tests/fake_engine.py."""
    return [DOTNET, "run", "--no-build", "--project", PROJECT]


def play_run(seed: str, character: str = "Ironclad", verbose: bool = True, log: bool = True,
             ascension: int = 0, keep_log_dir: str | None = None):
    """Play a complete run and return the result.

    Thin wrapper over _play_run() that stamps the fields every result needs
    regardless of which of _play_run's many return paths produced it:
    ascension, the out-of-combat policy and its counters, and the path of the
    game's full state log. With keep_log_dir, that log is copied out of
    logs/ first -- game_log.cleanup_old_logs() deletes anything older than
    7 days, which already erased every Phase 2 game, and Phase 3b fits its
    route model from these logs.
    """
    policy = ooc_policy()
    stats = {"ooc_policy": policy,
             # commands greedy_action produced AND the engine accepted
             "ooc_greedy": 0,
             "ooc_fallbacks": 0,  # = sum(ooc_fallback_causes), set below
             "ooc_fallback_causes": {cause: 0 for cause in OOC_FALLBACK_CAUSES},
             # greedy-arm event-page stuck exits (leave_room); the naive branch
             # exits the same way, so these are NOT fallbacks
             "ooc_event_stuck": 0,
             "ooc_knobs": greedy_knobs() if policy == "greedy" else {},
             "game_log": None}
    result = _play_run(seed, character, verbose, log, ascension, stats)
    stats["ooc_fallbacks"] = sum(stats["ooc_fallback_causes"].values())
    game_log = stats.pop("game_log")
    if keep_log_dir and game_log and os.path.exists(game_log):
        # Keeping the log is a convenience -- it must never cost the game's result.
        kept = os.path.join(
            keep_log_dir,
            f"{character}_{policy}_a{ascension}_{str(seed).replace('/', '_')}.jsonl")
        try:
            os.makedirs(keep_log_dir, exist_ok=True)
            shutil.copy2(game_log, kept)
            game_log = kept
        except OSError as exc:
            print(f"  !! could not keep game log {game_log} -> {kept}: {exc}")
            result["game_log_error"] = str(exc)
    result.update(stats)
    result["ascension"] = ascension
    result["game_log"] = game_log
    return result


def _play_run(seed: str, character: str, verbose: bool, log: bool, ascension: int,
              stats: dict):
    """The run loop. `stats` is play_run()'s per-run dict: this function bumps
    stats["ooc_greedy"], stats["ooc_fallback_causes"][...] and
    stats["ooc_event_stuck"], and sets stats["game_log"]."""
    # Dedicated RNG for this harness's own decisions (map routing below), keyed
    # off the run seed -- NOT random.seed(), which would reseed the shared
    # global `random` module and silently change behavior for every other
    # component that uses it. Without this, the same `seed` string passed to
    # start_run does not pin the route: the C# engine's own state is
    # deterministic per seed, but the *harness's* map_select choice was drawn
    # from the unseeded global random module, so two runs of the same seed
    # could diverge onto different routes and reach different floors.
    rng = random.Random(seed)
    solver_chars = solver_characters()
    solver_budget_s = solver_budget_seconds()
    # Engagement counters for this one run: how many plan_combat_turn calls
    # returned a usable plan vs. an error (which silently falls back to the
    # one-card-at-a-time heuristic below). A 3-game Silent smoke test once
    # printed "Completed: 3/3" while all 502 plan_combat_turn calls in it
    # errored with PredictionUnsupportedException and the solver produced
    # zero plans -- the harness had no way to notice it was measuring the
    # fallback heuristic instead. These counters (surfaced by summarize())
    # are what makes that impossible to miss again.
    solver_plans = 0
    solver_errors = 0
    solver_error_printed = False
    logger = GameLogger(character, seed, enabled=log,
                        run_context={"seed": seed, "character": character,
                                     "ascension": ascension,
                                     "experiment": f"ooc={stats['ooc_policy']}"})
    # stderr: inherit when verbose, otherwise DEVNULL -- never an unread PIPE,
    # which fills up and blocks the engine's Console.Error.WriteLine (the stderr
    # deadlock described in RunSimulator.cs DoPlanCombatTurn's diagnostics note).
    engine = EngineProcess(engine_argv(), stderr=None if verbose else subprocess.DEVNULL)
    solver_call_timeout = solver_call_timeout_s(solver_budget_s)

    def read_json_line(timeout: float = ENGINE_REPLY_TIMEOUT_S) -> dict:
        """Next JSON reply, skipping non-JSON lines (build warnings etc.).
        Raises EngineHang if nothing arrives in time (agent/bug.md BUG-040)."""
        on_skip = (lambda text: print(f"  [skip] {text[:120]}")) if verbose else None
        return engine.read_json(timeout, on_skip=on_skip)

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        engine.write(cmd)
        timeout = (solver_call_timeout if cmd.get("action") == "plan_combat_turn"
                   else ENGINE_REPLY_TIMEOUT_S)
        resp = read_json_line(timeout)
        logger.log_state(resp)
        if verbose:
            rtype = resp.get("type", "?")
            decision = resp.get("decision", "")
            if rtype == "decision":
                player = resp.get("player", {})
                hp = player.get("hp", "?")
                max_hp = player.get("max_hp", "?")
                gold = player.get("gold", "?")
                act = resp.get("act", "?")
                floor = resp.get("floor", "?")
                print(f"  < {rtype}/{decision} act={act} floor={floor} hp={hp}/{max_hp} gold={gold}")
            else:
                print(f"  < {json.dumps(resp)[:200]}")
        return resp

    step = 0
    # Most recent state with type=="decision", tracked so an error response
    # (which carries no context/player, see RunSimulator.cs's
    # Error()/ErrorWithTrace()) -- or an engine hang -- can still be reported
    # with real act/floor/hp instead of None/None/None. Bound before `try:` so
    # the EngineHang handler below can read it even if the hang comes first.
    last_decision = {}
    try:
        # Read ready message (may need to skip build warnings)
        ready = read_json_line()
        if ready.get("type") != "ready":
            print(f"  Unexpected initial response: {ready}")
            return {"victory": False, "seed": seed, "error": "bad_init",
                    "solver_plans": solver_plans, "solver_errors": solver_errors}
        if verbose:
            print(f"Connected: {ready}")

        # Start run
        state = send({"cmd": "start_run", "character": character, "seed": seed,
                      "ascension": ascension})

        step = 0
        max_steps = 500  # Safety limit
        stuck_count = 0
        last_state_key = None
        # Per-turn set of refused card `id`s for the combat_play fallback
        # heuristic (see the combat_play/else branch below) plus the turn
        # key it was collected under.
        refused_ids = set()
        refused_turn_key = None
        # Phase 3a: out-of-combat decisions go to agent.combat_env.greedy_action
        # when STS2_OOC_POLICY=greedy (see OOC_GREEDY_DECISIONS). None = naive.
        greedy = _load_greedy_action() if stats["ooc_policy"] == "greedy" else None
        shop_visit = None
        shop_actions = 0
        raised_decisions = set()  # decision types whose first raise printed a traceback

        def ooc_fallback(state, cause):
            """Play the naive choice for a greedy decision that could not be
            answered, count it under `cause`, and return the engine's reply. If
            that reply is an error too, send the escape command (the same
            recovery the naive branches use)."""
            stats["ooc_fallback_causes"][cause] += 1
            reply = send(naive_ooc_command(state))
            if reply.get("type") == "error":
                print(f"  !! naive fallback refused on {state.get('decision')}: "
                      f"{reply.get('message', 'unknown')} -- using escape command")
                reply = send(ooc_escape_command(state))
            return reply

        while step < max_steps:
            step += 1

            if state.get("type") == "decision":
                last_decision = state

            if state.get("type") == "error":
                # An engine error is a genuine failure, not a timeout -- return
                # here (instead of `break`-ing into the max-steps timeout path
                # below) so summarize() reports ERROR, not the dishonestly
                # less-alarming TIMEOUT (same class of bug already fixed once
                # for plan_combat_turn, see the return a few lines above this
                # branch's sibling). The error dict itself has no context/player
                # (RunSimulator.cs Error()/ErrorWithTrace()), so fall back to
                # the last genuine decision seen for act/floor/hp.
                context = state.get("context") or last_decision.get("context", {})
                player = state.get("player") or last_decision.get("player", {})
                msg = state.get("message", "unknown")
                print(f"  ERROR: {msg}")
                return {"victory": False, "seed": seed, "steps": step,
                        "act": context.get("act"), "floor": context.get("floor"),
                        "hp": player.get("hp"), "max_hp": player.get("max_hp"),
                        "error": f"engine_error: {msg[:160]}",
                        "solver_plans": solver_plans, "solver_errors": solver_errors}

            decision = state.get("decision", "")

            # Stuck detection — use comprehensive state key. Includes gold and (for
            # event_choice) each option's is_locked/was_chosen flags so a decision that
            # keeps re-offering the same event page is recognized as no-progress even
            # when hp/hand/enemy/energy alone don't capture it — an event option can
            # become a permanent silent no-op after one use (WasChosen latches true
            # internally) while is_locked never reflects that (see
            # docs/superpowers/plans/2026-09-17-combatsolver-port-phase1.md's
            # event_choice stuck postmortem).
            hand_len = len(state.get("hand", []))
            enemy_hp = sum(e.get("hp", 0) for e in state.get("enemies", []))
            energy = state.get("energy", 0)
            gold = state.get("player", {}).get("gold")
            opts_fp = ""
            if decision == "event_choice":
                opts_fp = ",".join(f"{o.get('index')}:{o.get('is_locked')}:{o.get('was_chosen')}"
                                    for o in state.get("options", []))
            state_key = f"{decision}:{state.get('round')}:{state.get('player',{}).get('hp')}:{hand_len}:{enemy_hp}:{energy}:{gold}:{opts_fp}"
            if state_key == last_state_key:
                stuck_count += 1
                if stuck_count > 20:
                    print(f"  STUCK after {step} steps, forcing quit")
                    return {"victory": False, "seed": seed, "steps": step,
                            "act": state.get("act"), "floor": state.get("floor"),
                            "hp": state.get("player", {}).get("hp"),
                            "max_hp": state.get("player", {}).get("max_hp"),
                            "timeout": True,
                            "solver_plans": solver_plans, "solver_errors": solver_errors}
            else:
                stuck_count = 0
                last_state_key = state_key

            if decision == "game_over":
                victory = state.get("victory", False)
                player = state.get("player", {})
                print(f"\n{'VICTORY' if victory else 'DEFEAT'} at act {state.get('act')}, "
                      f"floor {state.get('floor')} "
                      f"(HP: {player.get('hp')}/{player.get('max_hp')}, "
                      f"Gold: {player.get('gold')}, "
                      f"Deck: {player.get('deck_size')} cards)")
                return {
                    "victory": victory,
                    "seed": seed,
                    "steps": step,
                    "act": state.get("act"),
                    "floor": state.get("floor"),
                    "hp": player.get("hp"),
                    "max_hp": player.get("max_hp"),
                    "solver_plans": solver_plans,
                    "solver_errors": solver_errors,
                }

            elif decision == "map_select":
                choices = state.get("choices", [])
                if not choices:
                    print("  No map choices available!")
                    break
                # Random selection
                choice = rng.choice(choices)
                state = send({
                    "cmd": "action",
                    "action": "select_map_node",
                    "args": {"col": choice["col"], "row": choice["row"]}
                })

            elif decision == "combat_play":
                # Try the ported Combat Solver first (plan_combat_turn): resolve
                # its card_id/card_occurrence-addressed actions to live
                # card_index and execute through the first end_turn, then let
                # the outer loop re-call plan_combat_turn fresh for the next
                # turn (see CLAUDE.md's "Protocol notes" on plan_combat_turn).
                # Only fall back to the old one-card-at-a-time heuristic below
                # if plan_combat_turn itself errors out -- a losing-but-valid
                # plan is still followed, since judging play quality is a
                # separate concern from this regression run.
                #
                # Gated to Ironclad only: this whole validation pass (Task 5,
                # docs/superpowers/plans/2026-09-17-combatsolver-port-phase1.md)
                # scoped itself to "在 Ironclad 上验证" -- all-character
                # rollout is explicitly deferred to Phase 2. _resolve_card_index/
                # _resolve_potion_index/_apply_action_choices have never been
                # exercised against Silent/Defect/Regent/Necrobinder's different
                # mechanics (orbs, stances, poison/shivs, etc.), and this
                # project's own memory records a prior planner that was "only
                # SAFE on Ironclad" and cost Defect real floors when used
                # cross-character without validation -- don't repeat that.
                # Which characters take this path is resolved by
                # solver_characters() (STS2_SOLVER_CHARS); see
                # docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md.
                if character in solver_chars:
                    plan = send({"cmd": "action", "action": "plan_combat_turn"})
                    if plan.get("type") == "error":
                        solver_errors += 1
                        # Print the engine's message only the FIRST time this
                        # run hits it -- a run where the solver is disabled
                        # for the whole combat (e.g. the PredictionUnsupportedException
                        # regression fixed in 5e9a1c9) would otherwise print
                        # the identical line hundreds of times. Style matches
                        # combat_plan_driver.py's own "!!" hard-failure prefix.
                        if not solver_error_printed:
                            solver_error_printed = True
                            print(f"  !! plan_combat_turn error (falling back to heuristic): "
                                  f"{plan.get('message', 'unknown')}")
                    else:
                        solver_plans += 1
                        # The engine resolves STS2_SOLVER_BUDGET itself (RunSimulator.cs
                        # ResolveSolverBudget); this harness resolves it independently for
                        # the watchdog and the results row. If their tier tables ever
                        # drift, fail the run loudly rather than label a 30 s run as a
                        # 120 s one in an A/B. A MISSING budget fails too: that is an
                        # engine binary built before the tiers existed, which runs every
                        # tier at 120 s -- a tier A/B would compare 120 s with 120 s and
                        # report "no difference". `dotnet run --no-build` runs whatever is
                        # in bin/, so a stale build is a real hazard, not a hypothetical.
                        reported = (plan.get("search") or {}).get("budget_ms")
                        if reported != solver_budget_s * 1000:
                            raise RuntimeError(
                                f"engine solver budget {reported} ms != STS2_SOLVER_BUDGET "
                                f"resolved here as {solver_budget_s} s"
                                + (" (engine reported none -- stale build? rebuild "
                                   "src/Sts2Headless)" if reported is None else ""))
                else:
                    plan = {"type": "error"}
                if plan.get("type") != "error":
                    plan_actions = plan.get("actions", [])
                    if not plan_actions:
                        # A valid plan with nothing to do this turn still needs
                        # to progress; the solver's own list is expected to
                        # include a terminal end_turn, but don't spin forever
                        # if it somehow doesn't.
                        state = send({"cmd": "action", "action": "end_turn"})
                    else:
                        state, plan_ok = _execute_combat_plan_actions(send, state, plan_actions)
                        if not plan_ok:
                            print("  ERROR: plan_combat_turn's plan did not execute "
                                  "cleanly against the live engine")
                            player = state.get("player", {}) or {}
                            context = state.get("context", {}) or {}
                            return {"victory": False, "seed": seed, "steps": step,
                                     "act": context.get("act"), "floor": context.get("floor"),
                                     "hp": player.get("hp"), "max_hp": player.get("max_hp"),
                                     "error": "plan_combat_turn_execution_failed",
                                     "solver_plans": solver_plans, "solver_errors": solver_errors}
                else:
                    hand = state.get("hand", [])
                    energy = state.get("energy", 0)
                    enemies = state.get("enemies", [])
                    context = state.get("context", {}) or {}

                    # Refused-card ids are scoped to one turn (act, floor,
                    # round) -- a new turn/room deals a fresh hand, so a
                    # previous turn's refusals no longer apply.
                    turn_key = (context.get("act"), context.get("floor"), state.get("round"))
                    if turn_key != refused_turn_key:
                        refused_turn_key = turn_key
                        refused_ids = set()

                    # Resolve the whole refusal cascade (if any) inside this
                    # one outer-loop step -- otherwise the outer STUCK
                    # detector sees an unchanged state_key and misreports a
                    # refusal storm as a hang.
                    played = False
                    while True:
                        playable = [c for c in hand if c.get("can_play", False)
                                   and (c.get("cost", 0) <= energy)
                                   and c.get("id") not in refused_ids]
                        if not playable:
                            break
                        card = playable[0]
                        args = {"card_index": card["index"]}
                        # If card needs a target, pick first enemy
                        if card.get("target_type") == "AnyEnemy" and enemies:
                            args["target_index"] = 0
                        resp = send({
                            "cmd": "action",
                            "action": "play_card",
                            "args": args
                        })
                        if resp.get("type") != "error":
                            state = resp
                            played = True
                            break
                        # Engine refused the play (DoPlayCard's "still in hand
                        # after action" check, RunSimulator.cs:988) even though
                        # can_play was true -- CanPlay() and the post-play
                        # verification aren't the same check (BUG-004/BUG-006).
                        # Key the block by card `id`, not hand index: a
                        # successful play of a DIFFERENT card re-indexes the
                        # hand, so a stored index would go stale and silently
                        # block the wrong card, while `id` stays stable and a
                        # refusal is a property of the card's own behaviour
                        # (every copy is equally suspect). A refusal leaves the
                        # engine untouched (same hand/energy/indices), so the
                        # pre-refusal `state` is still exactly accurate and is
                        # deliberately left alone -- only the candidate set
                        # shrinks. Each refusal permanently removes >=1 id, so
                        # this terminates.
                        print(f"  REFUSED: card {card.get('id')} "
                              f"({card.get('name')}) rejected by engine: "
                              f"{resp.get('message', 'unknown')}")
                        refused_ids.add(card.get("id"))

                    if not played:
                        # End turn - retry a few times if we get "Not in play phase"
                        for retry in range(5):
                            state = send({
                                "cmd": "action",
                                "action": "end_turn"
                            })
                            if state.get("type") != "error":
                                break
                            import time
                            time.sleep(0.5)
                        if state.get("type") == "error":
                            # Try proceeding instead
                            state = send({"cmd": "action", "action": "proceed"})

            elif greedy is not None and decision in OOC_GREEDY_DECISIONS:
                context = state.get("context") or {}
                if decision == "shop":
                    visit = (context.get("act"), context.get("floor"))
                    if visit != shop_visit:
                        shop_visit, shop_actions = visit, 0
                    shop_actions += 1
                if decision == "event_choice" and stuck_count >= 3:
                    # Same guard as the naive event branch below: a page that keeps
                    # coming back unchanged means the chosen option is a latched
                    # no-op (was_chosen) -- leave rather than spin to STUCK. The
                    # naive arm exits the same way, so this is not a fallback.
                    print("  event_choice stuck on same page, leaving room")
                    stats["ooc_event_stuck"] += 1
                    state = send({"cmd": "action", "action": "leave_room"})
                elif decision == "shop" and shop_actions > SHOP_ACTION_CAP:
                    print(f"  !! shop visit exceeded {SHOP_ACTION_CAP} actions, leaving")
                    state = ooc_fallback(state, "shop_cap")
                else:
                    try:
                        cmd = greedy(state)
                        raised = False
                    except Exception as exc:
                        raised = True
                        print(f"  !! greedy_action raised on {decision}: {exc!r} "
                              f"-- using naive fallback")
                        if decision not in raised_decisions:
                            raised_decisions.add(decision)
                            print(traceback.format_exc())
                    if raised:
                        state = ooc_fallback(state, "raised")
                    elif cmd is None:
                        print(f"  !! greedy_action returned None on {decision} "
                              f"-- using naive fallback")
                        state = ooc_fallback(state, "none")
                    else:
                        reply = send(cmd)
                        if reply.get("type") == "error":
                            print(f"  !! greedy {cmd.get('action')} refused on {decision}: "
                                  f"{reply.get('message', 'unknown')} -- using naive fallback")
                            state = ooc_fallback(state, "refused")
                        else:
                            stats["ooc_greedy"] += 1
                            state = reply

            elif decision == "event_choice":
                options = state.get("options", [])
                if options:
                    if stuck_count >= 3:
                        # Same event page, same option states, 3+ times running — the
                        # previously-picked option is a confirmed dead no-op (WasChosen
                        # latched true, is_locked never reflects it) and there's no
                        # headless-drivable way to make further progress here (e.g.
                        # Crystal Sphere's "Uncover Future" starts an interactive
                        # tile-reveal minigame with no headless equivalent). Bail out of
                        # the room rather than spinning to the global STUCK cap.
                        print("  event_choice stuck on same page, leaving room")
                        state = send({"cmd": "action", "action": "leave_room"})
                    else:
                        choice = next((o for o in options if not o.get("is_locked")), options[0])
                        state = send({
                            "cmd": "action",
                            "action": "choose_option",
                            "args": {"option_index": choice["index"]}
                        })
                        if state and state.get("type") == "error":
                            state = send({"cmd": "action", "action": "leave_room"})
                else:
                    state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "rest_site":
                options = state.get("options", [])
                # Prefer heal (HEAL), then smith
                enabled = [o for o in options if o.get("is_enabled", True)]
                heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
                choice = heal or (enabled[0] if enabled else None)
                if choice:
                    state = send({
                        "cmd": "action",
                        "action": "choose_option",
                        "args": {"option_index": choice["index"]}
                    })
                    if state and state.get("type") == "error":
                        state = send({"cmd": "action", "action": "leave_room"})
                else:
                    state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "card_reward":
                # Pick the first card offered
                cards = state.get("cards", [])
                if cards:
                    state = send({
                        "cmd": "action",
                        "action": "select_card_reward",
                        "args": {"card_index": 0}
                    })
                else:
                    state = send({"cmd": "action", "action": "skip_card_reward"})

            elif decision == "bundle_select":
                state = send({"cmd": "action", "action": "select_bundle",
                             "args": {"bundle_index": 0}})

            elif decision == "card_select":
                # Auto-select first card
                cards = state.get("cards", [])
                if cards:
                    state = send({"cmd": "action", "action": "select_cards",
                                 "args": {"indices": "0"}})
                else:
                    state = send({"cmd": "action", "action": "skip_select"})

            elif decision == "shop":
                state = send({"cmd": "action", "action": "leave_room"})

            elif decision == "unknown":
                state = send({"cmd": "action", "action": "proceed"})

            else:
                state = send({"cmd": "action", "action": "proceed"})
                state = send({"cmd": "action", "action": "proceed"})

        print(f"  Reached max steps ({max_steps})")
        return {"victory": False, "seed": seed, "steps": step, "timeout": True,
                "solver_plans": solver_plans, "solver_errors": solver_errors}

    except EngineHang as e:
        context = last_decision.get("context") or {}
        player = last_decision.get("player") or {}
        print(f"  !! ENGINE HANG: {e} -- killing the engine process group (agent/bug.md BUG-040)")
        engine.kill()
        return {"victory": False, "seed": seed, "steps": step,
                "act": context.get("act"), "floor": context.get("floor"),
                "hp": player.get("hp"), "max_hp": player.get("max_hp"),
                "error": f"engine_hang: {e}", "hang": True,
                "solver_plans": solver_plans, "solver_errors": solver_errors}

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return {"victory": False, "seed": seed, "steps": step, "error": str(e),
                "solver_plans": solver_plans, "solver_errors": solver_errors}

    finally:
        logger.close()
        stats["game_log"] = logger.path
        if logger.path:
            print(f"  [log] Saved to {logger.path}")
        engine.close()


def global_floor(act, floor):
    """Absolute run floor from the engine's ACT-LOCAL floor.

    game_over's top-level `floor` resets to 1 at every act, so on its own it
    ranks a death at act 2 floor 16 below one at act 1 floor 17. This is the
    same (act - 1) * 17 + floor convention agent/eval_rl.py uses
    (global_floor_from_state, eval_rl.py:109) and writes into the results JSONL
    that agent/paired_eval.py consumes -- so the two harnesses stay comparable.
    A missing act means act 1, matching eval_rl's `context.get("act") or 1`.
    """
    if not isinstance(floor, (int, float)) or floor < 1:
        return None
    if not isinstance(act, (int, float)) or act < 1:
        act = 1
    return (int(act) - 1) * 17 + int(floor)


def result_to_eval_row(result: dict, character: str) -> dict:
    """Convert one play_run() result into an agent/paired_eval.py input row.

    paired_eval pairs only rows whose status is "win" or "dead"
    (agent/paired_eval.py:54) and reads `floor` for the run-level metric
    (agent/paired_eval.py:287). Harness-internal failures therefore have to map
    onto names outside that set -- "timeout" for the STUCK/max-steps path and
    "crash" for an engine error -- so a run that never finished cannot be
    averaged in as if it were an ordinary death.

    `solver_plans`/`solver_errors` are carried through too so the paired A/B
    can prove the ON arm actually engaged the solver and the OFF arm did not,
    rather than trusting STS2_SOLVER_CHARS alone.
    """
    if result.get("victory"):
        status = "win"
    elif result.get("hang"):
        # "stuck" is in eval_rl's technical-status set, so paired_eval drops the
        # seed from pairing instead of averaging a killed run in as a death.
        status = "stuck"
    elif result.get("timeout"):
        status = "timeout"
    elif result.get("error"):
        status = "crash"
    else:
        status = "dead"
    act_floor = result.get("floor")
    return {
        "seed": result.get("seed"),
        "status": status,
        # GLOBAL floor -- paired_eval's floor metric. The act-local value is kept
        # alongside as act_floor; see global_floor() for why the two differ.
        "floor": global_floor(result.get("act"), act_floor),
        "act_floor": act_floor if isinstance(act_floor, (int, float)) else None,
        "act": result.get("act"),
        "steps": result.get("steps"),
        "character": character,
        "solver_plans": result.get("solver_plans"),
        "solver_errors": result.get("solver_errors"),
        "solver": sorted(solver_characters()),
        "solver_budget_s": solver_budget_seconds(),
        "ascension": result.get("ascension"),
        "ooc_policy": result.get("ooc_policy"),
        "ooc_greedy": result.get("ooc_greedy"),
        "ooc_fallbacks": result.get("ooc_fallbacks"),
        "ooc_fallback_causes": result.get("ooc_fallback_causes"),
        "ooc_event_stuck": result.get("ooc_event_stuck"),
        "ooc_knobs": result.get("ooc_knobs"),
        "game_log": result.get("game_log"),
        "game_log_error": result.get("game_log_error"),
    }


def summarize(results, num_runs, character="Ironclad", solver_chars=None):
    """Build the SUMMARY text block, including avg_floor over numeric floors.

    "Completed" means the run reached a genuine game_over (win or loss) --
    NOT a run cut short by an internal failure: max-steps safety limit,
    STUCK-loop detection, bad_init, an uncaught exception, a
    plan_combat_turn plan that the live engine rejected mid-execution, or
    any other engine error response ("engine_error: ...").
    CLAUDE.md's own regression gate is explicit that STUCK must NOT count as
    completed ("Completed: 5/5" = "0 crashes/stuck") -- both the STUCK path
    and the max-steps path set "timeout": True for exactly this reason, and
    any result dict carrying an "error" key is excluded the same way, so
    none of these can silently hide behind an ordinary-looking LOSS line
    (see git history: this docstring previously claimed STUCK counted as
    completed, which contradicted CLAUDE.md and let a real stuck run pass
    the gate undetected).

    The inverse matters just as much: a failure must not masquerade as a
    DIFFERENT failure either. The engine-error path deliberately returns
    "error" (never "timeout") so it renders as ERROR here rather than as the
    less alarming TIMEOUT -- it used to `break` into the max-steps return and
    print "TIMEOUT | act=None floor=None" for what was really a refused
    action. Same class as the plan_combat_turn "问题 1" postmortem in
    docs/superpowers/plans/2026-09-17-combatsolver-port-phase1.md.

    Solver engagement ("solver=<plans>/<attempts>" per run, plus an aggregate
    line) is reported alongside -- but deliberately NOT folded into
    Completed/WIN/LOSS/TIMEOUT/ERROR above: a run that fell back to the
    one-card-at-a-time heuristic for its entire combat is still a completed,
    valid run of *something*; conflating "solver didn't engage" with "run
    crashed" would make the regression gate mean two different things at
    once. `solver_chars` defaults to solver_characters() (STS2_SOLVER_CHARS)
    but takes an explicit override so callers (and tests) don't have to
    monkeypatch the environment to control it.
    """
    if solver_chars is None:
        solver_chars = solver_characters()
    lines = ["\n" + "=" * 60, f"SUMMARY ({character})", "=" * 60]
    wins = sum(1 for r in results if r and r.get("victory"))
    completed = sum(1 for r in results if r and not r.get("timeout") and not r.get("error"))
    floors = []
    total_solver_plans = 0
    total_solver_errors = 0
    for i, r in enumerate(results):
        if r:
            if r.get("victory"):
                status = "WIN"
            elif r.get("hang"):
                status = "HANG"
            elif r.get("timeout"):
                status = "TIMEOUT"
            elif r.get("error"):
                status = "ERROR"
            else:
                status = "LOSS"
            solver_plans = r.get("solver_plans", 0)
            solver_errors = r.get("solver_errors", 0)
            total_solver_plans += solver_plans
            total_solver_errors += solver_errors
            attempts = solver_plans + solver_errors
            line = (f"  Run {i+1}: {status} | seed={r.get('seed')} steps={r.get('steps')} "
                    f"act={r.get('act')} floor={r.get('floor')} solver={solver_plans}/{attempts}")
            if r.get("error"):
                line += f" error={r.get('error')}"
            lines.append(line)
            # Average the GLOBAL floor: the act-local one made an act-2 death
            # pull avg_floor down relative to a deep act-1 death.
            f = global_floor(r.get("act"), r.get("floor"))
            if f is not None:
                floors.append(f)
    avg_floor = round(sum(floors) / len(floors), 1) if floors else 0.0
    lines.append(f"\nWins: {wins}/{num_runs}, Completed: {completed}/{num_runs}, "
                 f"avg_floor={avg_floor}")
    total_solver_attempts = total_solver_plans + total_solver_errors
    lines.append(f"Solver engagement: {total_solver_plans}/{total_solver_attempts} "
                 f"plan_combat_turn calls returned a usable plan")
    policies = sorted({r.get("ooc_policy") for r in results if r and r.get("ooc_policy")})
    if policies:
        total_greedy = sum(r.get("ooc_greedy") or 0 for r in results if r)
        total_fallbacks = sum(r.get("ooc_fallbacks") or 0 for r in results if r)
        line = (f"OOC policy: {'/'.join(policies)} -- {total_greedy} greedy decisions, "
                f"{total_fallbacks} fallbacks")
        cause_rows = [r["ooc_fallback_causes"] for r in results
                      if r and r.get("ooc_fallback_causes")]
        if cause_rows:
            by_cause = {c: sum(row.get(c, 0) for row in cause_rows) for c in OOC_FALLBACK_CAUSES}
            line += " (" + ", ".join(f"{c} {n}" for c, n in by_cause.items()) + ")"
        if any(r and r.get("ooc_event_stuck") is not None for r in results):
            line += (f", {sum(r.get('ooc_event_stuck') or 0 for r in results if r)} "
                     f"event-stuck exits")
        lines.append(line)
        # Same blindness the solver banner guards against: a greedy arm whose
        # policy never had a command accepted is secretly the naive arm.
        if policies == ["greedy"] and total_greedy == 0:
            lines.append("!! OOC POLICY NEVER ENGAGED -- STS2_OOC_POLICY=greedy but "
                         "the engine accepted no greedy_action command; these results "
                         "measure the naive fallback")
        attempts = total_greedy + total_fallbacks
        if attempts and total_fallbacks / attempts > OOC_FALLBACK_RATE_WARN:
            lines.append(f"!! OOC FALLBACK RATE {100 * total_fallbacks / attempts:.1f}% -- "
                         f"the greedy arm is partly running the naive policy")
        # Two causes that are never legitimate, and that the rate banner misses:
        # a raise is a greedy_action bug, and a capped shop is at most 1/21 of a
        # visit's decisions however broken the shop command is.
        if cause_rows:
            raised = sum(row.get("raised", 0) for row in cause_rows)
            capped = sum(row.get("shop_cap", 0) for row in cause_rows)
            if raised:
                lines.append(f"!! greedy_action raised {raised} times -- see the tracebacks "
                             f"in this log; those decisions ran the naive policy")
            if capped:
                lines.append(f"!! {capped} shop visit(s) hit SHOP_ACTION_CAP -- a greedy "
                             f"shop command made no progress")
    unkept = sum(1 for r in results if r and r.get("game_log_error"))
    if unkept:
        lines.append(f"!! {unkept} game log(s) could not be kept -- their game_log points "
                     f"into logs/, which is purged after 7 days")
    # Zero engagement is only alarming for a character the solver is SUPPOSED
    # to drive -- for the A/B's control arm (character not in solver_chars),
    # zero plans is the whole point and must stay silent. This is the check
    # that would have caught the Silent 502/0 smoke result: it "passed"
    # (Completed: 3/3) while every single plan_combat_turn call errored with
    # PredictionUnsupportedException and the run was secretly measuring the
    # fallback heuristic (fixed in 5e9a1c9, but the blindness itself wasn't).
    if character in solver_chars and total_solver_plans == 0:
        # Two different zero-engagement stories, and the banner must not tell
        # the wrong one: "every call failed" is false when no call was ever
        # made (a batch that ended before reaching any combat_play decision).
        # A diagnostic that cries wolf inaccurately stops being trusted, which
        # would defeat the point of adding it.
        if total_solver_errors:
            cause = (f"every plan_combat_turn call failed ({total_solver_errors} "
                     f"errors); these results measure the FALLBACK heuristic, "
                     f"not the solver")
        else:
            cause = ("no plan_combat_turn call was ever made -- the batch never "
                     "reached a combat_play decision")
        lines.append(f"!! SOLVER NEVER ENGAGED for {character} -- {cause}.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Play full STS2 runs using the headless simulator with a random agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Valid characters: " + ", ".join(VALID_CHARACTERS),
    )
    parser.add_argument("num_runs", type=int, help="Number of runs to play (must be positive)")
    parser.add_argument("character", nargs="?", default="Ironclad",
                        choices=VALID_CHARACTERS, metavar="character",
                        help=f"Character to play as (default: Ironclad). Choices: {', '.join(VALID_CHARACTERS)}")
    parser.add_argument("--results-log", default=None,
                        help="Append one JSONL row per run, in agent/paired_eval.py's "
                             "input format (seed/status/floor). Use for A/B arms.")
    parser.add_argument("--ascension", type=int, default=0,
                        help="Ascension level sent with start_run (default 0).")
    parser.add_argument("--seed-prefix", default="run_",
                        help="Seeds are <prefix><i> for i = 1..num_runs (default run_). "
                             "Use a fresh prefix per experiment so a verdict never "
                             "runs on seeds an earlier phase tuned on.")
    parser.add_argument("--keep-game-logs", default=None, metavar="DIR",
                        help="Copy each game's full state log to "
                             "DIR/<character>_<policy>_a<ascension>_<seed>.jsonl "
                             "(logs/ is purged after 7 days) and record that path in the "
                             "results row.")
    args = parser.parse_args()

    if args.num_runs <= 0:
        parser.error(f"num_runs must be a positive integer, got {args.num_runs}")
    if args.ascension < 0:
        parser.error(f"--ascension must be >= 0, got {args.ascension}")
    policy = ooc_policy()  # fail on a bad STS2_OOC_POLICY before any game starts
    if policy == "greedy":
        _load_greedy_action()  # a broken import aborts now, not as N "crash" games
        for knob, value in greedy_knobs().items():
            print(f"!! greedy_action knob set: {knob}={value}")

    num_runs = args.num_runs
    character = args.character

    print(f"Playing {num_runs} runs as {character} "
          f"(ascension {args.ascension}, ooc policy {policy})")
    print("=" * 60)

    results = []
    for i in range(num_runs):
        seed = f"{args.seed_prefix}{i+1}"
        print(f"\n--- Run {i+1}/{num_runs} (seed: {seed}) ---")
        result = play_run(seed, character, verbose=True, ascension=args.ascension,
                          keep_log_dir=args.keep_game_logs)
        results.append(result)
        if args.results_log:
            with open(args.results_log, "a") as fh:
                fh.write(json.dumps(result_to_eval_row(result, character)) + "\n")
        print()

    print(summarize(results, num_runs, character))


if __name__ == "__main__":
    main()
