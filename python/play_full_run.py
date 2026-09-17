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

def _norm_entity_id(eid):
    """Normalize a card/potion id for comparison between two different formats
    seen in this protocol: the ordinary combat_play/card_select JSON's "id"
    field is "CARD.<ENTRY>" for cards and "POTION.<ENTRY>" for potions
    (c.Id.ToString() / p.Id.ToString() in RunSimulator.cs), while
    plan_combat_turn's card_id/potion_id is the bare uppercase entry
    (card.Preview.Id.Entry / potion.Id.Entry in the vendored solver) -- same
    discrepancy agent/card_scoring.py's _card_id_norm already strips for
    card-override lookups (confirmed for potions too: live logs show e.g.
    "Player 1 obtained POTION.BLOCK_POTION from potion reward")."""
    if isinstance(eid, dict):
        eid = eid.get("en", str(eid))
    eid = str(eid).upper().strip()
    for prefix in ("CARD.", "POTION."):
        if eid.startswith(prefix):
            return eid[len(prefix):]
    return eid


def _resolve_card_index(hand, card_id, occurrence):
    """Resolve plan_combat_turn's (card_id, card_occurrence) -- a stable card
    identity -- to a live hand card_index (hand position), by walking the
    hand in order and counting matches the same way the vendored solver's
    own FindCardOccurrence (Search/CombatBeamSolver.Expansion.cs) counts
    occurrences when it originally assigned card_occurrence."""
    target = _norm_entity_id(card_id)
    seen = 0
    for c in hand:
        if _norm_entity_id(c.get("id", "")) == target:
            if seen == occurrence:
                return c["index"]
            seen += 1
    return None


def _resolve_potion_index(potions, potion_id):
    """Resolve a plan_combat_turn use_potion action's potion_id (stable
    identity) to a live potion_index -- the position in the CURRENT
    combat_play decision's player.potions list, i.e. the real game's
    player.Potions (RunSimulator.cs's DoUsePotion indexes potion_index into
    player.Potions.ToList()).

    Deliberately does NOT use the plan action's own "potion_slot": that is
    the solver's PHYSICAL belt slot (player.PotionSlots in the real game --
    fixed-size, with a null hole at every empty slot), which is a genuinely
    different numbering from player.Potions (the real game's own compacted
    enumerable that skips empty slots entirely -- confirmed by decompiling
    MegaCrit.Sts2.Core.Entities.Players.Player: `Potions => PotionSlots.Where(p
    => p != null)`). The two numberings only agree while no earlier belt slot
    has been consumed; once one has, PotionSlot no longer means the same thing
    as potion_index, which is exactly what caused a live "Invalid potion
    index" rejection (Task 5 postmortem, docs/superpowers/plans/
    2026-09-17-combatsolver-port-phase1.md). Resolving by identity instead
    (matching potion_id here the same way _resolve_card_index matches
    card_id) sidesteps the whole discrepancy. Potions have no upgrade/
    enchantment-style state to disambiguate duplicates by, so this returns
    the first live match -- unlike cards there is no occurrence field to
    thread through for potions."""
    target = _norm_entity_id(potion_id)
    for p in potions:
        if _norm_entity_id(p.get("id", "")) == target:
            return p["index"]
    return None


def _resolve_choice_indices(pending_cards, choice):
    """Resolve a plan action's bundled card_select sub-choice (Discard/Exhaust/
    Transform/... -- PlanCardChoice in Search/CombatPlan.cs) to indices into
    the live card_select decision's offered "cards" list, matching each
    PlanCardToken's card_id + option_occurrence the same way _resolve_card_index
    matches card_id + card_occurrence. Returns None if any token can't be
    resolved (a real mismatch to surface, not something to paper over)."""
    indices = []
    for token in choice.get("cards", []):
        target = _norm_entity_id(token.get("card_id", ""))
        occ = token.get("option_occurrence", 0)
        seen = 0
        found = None
        for c in pending_cards:
            if _norm_entity_id(c.get("id", "")) == target:
                if seen == occ:
                    found = c["index"]
                    break
                seen += 1
        if found is None:
            return None
        indices.append(found)
    return indices


def _apply_action_choices(send, cur, action):
    """Resolve and answer any card_select sub-decisions bundled into a plan
    action (RunSimulator.cs's ConvertPlanActionsToJson attaches "choices" to
    ANY action kind that has one -- e.g. a potion like Ambrosia can trigger a
    card_select just as much as a Discard/Exhaust/Transform card can -- so
    this runs after play_card AND use_potion, not just play_card). Returns
    (state, ok).

    A bundled choice does NOT always surface as a live card_select: when
    there is at most one legal candidate to pick from (e.g. Ironclad's
    Headbutt -- "put a card from your discard pile on top of your draw
    pile" -- with exactly one card sitting in the discard), the real game
    auto-resolves it synchronously inside the same play_card/use_potion call
    and the decision never leaves combat_play. Confirmed live (Task 5
    follow-up regression): Thrash discarded this turn, Headbutt played with
    choices=[{"effect": "MoveToDrawTop", "cards": [{"card_id": "THRASH"}]}],
    decision stayed "combat_play" throughout, and the next state's
    draw_pile[0] was CARD.THRASH -- the solver's intended choice WAS
    applied, just with no round-trip. Treating that as a hard failure was
    the actual bug (this project's own CLAUDE.md documents the engine's
    complementary invariant: any path that can open a card_select "must
    yield as soon as _cardSelector.HasPending appears", so if the decision
    is NOT card_select, nothing is actually pending -- the choice already
    resolved one way or another). If the resulting decision is neither
    card_select nor combat_play (e.g. the card's damage ended the fight,
    moving to card_reward/game_over), there is equally nothing left to
    answer here -- the caller's outer loop handles what comes next. Only a
    genuinely unexpected decision, or an errored select_cards call, is
    treated as a real bug to surface.
    """
    for choice in action.get("choices", []):
        decision = cur.get("decision")
        if decision == "combat_play":
            continue  # auto-resolved synchronously; nothing to answer
        if decision != "card_select":
            # Left combat_play some other way (fight over, etc.) -- also
            # nothing to answer; let the caller's outer loop react to
            # whatever decision this now is.
            return cur, True
        idxs = _resolve_choice_indices(cur.get("cards", []), choice)
        if idxs is None:
            print(f"  !! plan_combat_turn: could not resolve choice tokens "
                  f"{choice.get('cards')} against pending cards "
                  f"{[c.get('id') for c in cur.get('cards', [])]}")
            return cur, False
        cur = send({"cmd": "action", "action": "select_cards",
                    "args": {"indices": ",".join(map(str, idxs))}})
        if cur.get("type") == "error":
            print(f"  !! plan_combat_turn: select_cards failed: {cur.get('message')}")
            return cur, False
    return cur, True


def _execute_combat_plan_actions(send, state, actions):
    """Execute a plan_combat_turn action list up through (and including) the
    first end_turn -- never the whole multi-turn plan blindly, per CLAUDE.md's
    "Protocol notes" on plan_combat_turn (turn>=2 actions' target_index/
    target_combat_id have no counterpart in an ordinary combat_play decision's
    enemy list, so the caller must re-call plan_combat_turn fresh for the next
    turn instead). Returns (state, ok); ok=False means the LIVE engine
    disagreed with the plan (unresolved card/choice, or an action itself
    errored) -- a real bug to surface, not "the solver predicts a loss".

    Also stops (successfully) the moment any action leaves the "combat_play"
    decision for a reason OTHER than the plan's own end_turn -- e.g. a card's
    damage happens to end the fight mid-turn, jumping straight to
    card_reward/game_over. Trying to resolve the next planned action's
    card_id against a hand that no longer exists would misreport a real
    "fight ended early" as a false unresolved-card failure; the caller's
    outer decision loop is what should react to whatever decision this now
    is, not this function.
    """
    cur = state
    for action in actions:
        kind = action.get("action")
        if kind == "play_card":
            hand = cur.get("hand", [])
            idx = _resolve_card_index(hand, action.get("card_id", ""), action.get("card_occurrence", 0))
            if idx is None:
                print(f"  !! plan_combat_turn: could not resolve card_id={action.get('card_id')} "
                      f"occurrence={action.get('card_occurrence')} in live hand "
                      f"{[c.get('id') for c in hand]}")
                return cur, False
            args = {"card_index": idx}
            if "target_index" in action:
                args["target_index"] = action["target_index"]
            cur = send({"cmd": "action", "action": "play_card", "args": args})
            if cur.get("type") == "error":
                print(f"  !! plan_combat_turn: play_card failed: {cur.get('message')}")
                return cur, False
            cur, ok = _apply_action_choices(send, cur, action)
            if not ok:
                return cur, False
            if cur.get("decision") != "combat_play":
                return cur, True
        elif kind == "use_potion":
            potions = cur.get("player", {}).get("potions", [])
            idx = _resolve_potion_index(potions, action.get("potion_id", ""))
            if idx is None:
                print(f"  !! plan_combat_turn: could not resolve potion_id={action.get('potion_id')} "
                      f"(solver's physical slot={action.get('potion_slot')}) in live potions "
                      f"{[p.get('id') for p in potions]}")
                return cur, False
            args = {"potion_index": idx}
            if "target_index" in action:
                args["target_index"] = action["target_index"]
            cur = send({"cmd": "action", "action": "use_potion", "args": args})
            if cur.get("type") == "error":
                print(f"  !! plan_combat_turn: use_potion failed: {cur.get('message')}")
                return cur, False
            cur, ok = _apply_action_choices(send, cur, action)
            if not ok:
                return cur, False
            if cur.get("decision") != "combat_play":
                return cur, True
        elif kind == "end_turn":
            cur = send({"cmd": "action", "action": "end_turn"})
            if cur.get("type") == "error":
                print(f"  !! plan_combat_turn: end_turn failed: {cur.get('message')}")
                return cur, False
            return cur, True
        else:
            print(f"  !! plan_combat_turn: unknown action kind {kind!r}, skipping")
    return cur, True


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

        while step < max_steps:
            step += 1

            if state.get("type") == "error":
                print(f"  ERROR: {state.get('message', 'unknown')}")
                break

            decision = state.get("decision", "")

            # Stuck detection — use comprehensive state key
            hand_len = len(state.get("hand", []))
            enemy_hp = sum(e.get("hp", 0) for e in state.get("enemies", []))
            energy = state.get("energy", 0)
            state_key = f"{decision}:{state.get('round')}:{state.get('player',{}).get('hp')}:{hand_len}:{enemy_hp}:{energy}"
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
                choice = random.choice(choices)
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
                plan = send({"cmd": "action", "action": "plan_combat_turn"})
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
                            return {"victory": False, "seed": seed, "steps": step,
                                     "error": "plan_combat_turn_execution_failed"}
                else:
                    hand = state.get("hand", [])
                    energy = state.get("energy", 0)
                    enemies = state.get("enemies", [])

                    # Simple strategy: play playable cards until out of energy
                    playable = [c for c in hand if c.get("can_play", False)
                               and (c.get("cost", 0) <= energy)]

                    if playable:
                        card = playable[0]
                        args = {"card_index": card["index"]}
                        # If card needs a target, pick first enemy
                        if card.get("target_type") == "AnyEnemy" and enemies:
                            args["target_index"] = 0
                        state = send({
                            "cmd": "action",
                            "action": "play_card",
                            "args": args
                        })
                    else:
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
                    # Pick first unlocked option
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
    STUCK-loop detection, bad_init, an uncaught exception, or a
    plan_combat_turn plan that the live engine rejected mid-execution.
    CLAUDE.md's own regression gate is explicit that STUCK must NOT count as
    completed ("Completed: 5/5" = "0 crashes/stuck") -- both the STUCK path
    and the max-steps path set "timeout": True for exactly this reason, and
    any result dict carrying an "error" key (a plan_combat_turn execution
    failure) is excluded the same way, so none of these can silently hide
    behind an ordinary-looking LOSS line (see git history: this docstring
    previously claimed STUCK counted as completed, which contradicted
    CLAUDE.md and let a real stuck run pass the gate undetected).
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
