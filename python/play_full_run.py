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
from game_log import GameLogger
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


def play_run(seed: str, character: str = "Ironclad", verbose: bool = True, log: bool = True):
    """Play a complete run and return the result."""
    # Dedicated RNG for this harness's own decisions (map routing below), keyed
    # off the run seed -- NOT random.seed(), which would reseed the shared
    # global `random` module and silently change behavior for every other
    # component that uses it. Without this, the same `seed` string passed to
    # start_run does not pin the route: the C# engine's own state is
    # deterministic per seed, but the *harness's* map_select choice was drawn
    # from the unseeded global random module, so two runs of the same seed
    # could diverge onto different routes and reach different floors.
    rng = random.Random(seed)
    logger = GameLogger(character, seed, enabled=log)
    proc = subprocess.Popen(
        [DOTNET, "run", "--no-build", "--project", PROJECT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if not verbose else None,
        text=True,
        bufsize=1,
    )

    def read_json_line() -> dict:
        """Read a line from stdout, skipping non-JSON lines (build warnings etc.)"""
        while True:
            resp_line = proc.stdout.readline().strip()
            if not resp_line:
                raise RuntimeError("No response from simulator (EOF)")
            if resp_line.startswith("{"):
                return json.loads(resp_line)
            # Skip non-JSON lines (build warnings, etc.)
            if verbose:
                print(f"  [skip] {resp_line[:120]}")

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        resp = read_json_line()
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
    try:
        # Read ready message (may need to skip build warnings)
        ready = read_json_line()
        if ready.get("type") != "ready":
            print(f"  Unexpected initial response: {ready}")
            return {"victory": False, "seed": seed, "error": "bad_init"}
        if verbose:
            print(f"Connected: {ready}")

        # Start run
        state = send({"cmd": "start_run", "character": character, "seed": seed})

        step = 0
        max_steps = 500  # Safety limit
        stuck_count = 0
        last_state_key = None
        # Per-turn set of refused card `id`s for the combat_play fallback
        # heuristic (see the combat_play/else branch below) plus the turn
        # key it was collected under.
        refused_ids = set()
        refused_turn_key = None
        # Most recent state with type=="decision", tracked so an error
        # response (which carries no context/player, see RunSimulator.cs's
        # Error()/ErrorWithTrace()) can still be reported with real
        # act/floor/hp instead of None/None/None.
        last_decision = {}

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
                        "error": f"engine_error: {msg[:160]}"}

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
                            "timeout": True}
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
                # Phase 2 lifts this gate once the other 4 characters are
                # actually regression-tested against this path.
                if character == "Ironclad":
                    plan = send({"cmd": "action", "action": "plan_combat_turn"})
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
                                     "error": "plan_combat_turn_execution_failed"}
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
        return {"victory": False, "seed": seed, "steps": step, "timeout": True}

    except Exception as e:
        print(f"  EXCEPTION: {e}")
        return {"victory": False, "seed": seed, "steps": step, "error": str(e)}

    finally:
        logger.close()
        if logger.path:
            print(f"  [log] Saved to {logger.path}")
        try:
            proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            proc.stdin.flush()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except:
            proc.kill()


def summarize(results, num_runs, character="Ironclad"):
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
    """
    lines = ["\n" + "=" * 60, f"SUMMARY ({character})", "=" * 60]
    wins = sum(1 for r in results if r and r.get("victory"))
    completed = sum(1 for r in results if r and not r.get("timeout") and not r.get("error"))
    floors = []
    for i, r in enumerate(results):
        if r:
            if r.get("victory"):
                status = "WIN"
            elif r.get("timeout"):
                status = "TIMEOUT"
            elif r.get("error"):
                status = "ERROR"
            else:
                status = "LOSS"
            line = (f"  Run {i+1}: {status} | seed={r.get('seed')} steps={r.get('steps')} "
                    f"act={r.get('act')} floor={r.get('floor')}")
            if r.get("error"):
                line += f" error={r.get('error')}"
            lines.append(line)
            f = r.get("floor")
            if isinstance(f, (int, float)):
                floors.append(f)
    avg_floor = round(sum(floors) / len(floors), 1) if floors else 0.0
    lines.append(f"\nWins: {wins}/{num_runs}, Completed: {completed}/{num_runs}, "
                 f"avg_floor={avg_floor}")
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
    args = parser.parse_args()

    if args.num_runs <= 0:
        parser.error(f"num_runs must be a positive integer, got {args.num_runs}")

    num_runs = args.num_runs
    character = args.character

    print(f"Playing {num_runs} runs as {character}")
    print("=" * 60)

    results = []
    for i in range(num_runs):
        seed = f"run_{i+1}"
        print(f"\n--- Run {i+1}/{num_runs} (seed: {seed}) ---")
        result = play_run(seed, character, verbose=True)
        results.append(result)
        print()

    print(summarize(results, num_runs, character))


if __name__ == "__main__":
    main()
