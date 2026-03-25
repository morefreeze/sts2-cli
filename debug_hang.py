#!/usr/bin/env python3
"""Collect data on game engine hangs during combat training."""
import json, os, subprocess, time, select, numpy as np, random

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(PROJECT_ROOT, "Sts2Headless", "Sts2Headless.csproj")

def find_dotnet():
    for p in [os.path.expanduser("~/.dotnet-arm64/dotnet"),
              os.path.expanduser("~/.dotnet/dotnet"),
              "/usr/local/share/dotnet/dotnet", "dotnet"]:
        try:
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0: return p
        except: pass
    return "dotnet"

DOTNET = find_dotnet()

def run_game_with_logging(seed, max_steps=300):
    """Run a game and log every command/response, detecting hangs."""
    proc = subprocess.Popen(
        [DOTNET, "run", "--no-build", "--project", PROJECT],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=PROJECT_ROOT
    )

    log = []
    def read_json(timeout=5.0):
        fileno = proc.stdout.fileno()
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rem = deadline - time.monotonic()
            if rem <= 0: break
            ready, _, _ = select.select([fileno], [], [], min(rem, 0.5))
            if not ready: continue
            chunk = os.read(fileno, 4096)
            if not chunk: return None
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line.startswith(b"{"):
                    try: return json.loads(line)
                    except: continue
        return "TIMEOUT"

    def send(cmd):
        try:
            proc.stdin.write((json.dumps(cmd) + "\n").encode())
            proc.stdin.flush()
            return read_json()
        except:
            return None

    # Read ready
    ready = read_json(timeout=15)
    if not ready or ready == "TIMEOUT":
        proc.kill()
        return {"seed": seed, "error": "no_ready", "log": []}

    # Start run
    state = send({"cmd": "start_run", "character": "Ironclad", "seed": seed})
    if not state or state == "TIMEOUT":
        proc.kill()
        return {"seed": seed, "error": "start_failed", "log": []}

    for step in range(max_steps):
        if not isinstance(state, dict):
            proc.kill()
            return {"seed": seed, "steps": step, "result": "crash", "crash_context": {"step": step, "decision": "?", "action": "?"}, "log": log[-5:]}
        decision = state.get("decision", "")
        floor = state.get("floor", "?")

        if decision == "game_over":
            proc.terminate()
            return {"seed": seed, "steps": step, "result": "game_over",
                    "victory": state.get("victory"), "log": log[-5:]}

        # Build action
        if decision == "combat_play":
            hand = state.get("hand", [])
            energy = state.get("energy", 0)
            enemies = state.get("enemies", [])
            playable = [c for c in hand if c.get("can_play") and c.get("cost", 99) <= energy]
            if playable:
                card = random.choice(playable)
                args = {"card_index": card["index"]}
                if card.get("target_type") == "AnyEnemy" and enemies:
                    args["target_index"] = enemies[0].get("index", 0)
                cmd = {"cmd": "action", "action": "play_card", "args": args}
            else:
                cmd = {"cmd": "action", "action": "end_turn"}
        elif decision == "map_select":
            choices = state.get("choices", [])
            if choices:
                ch = random.choice(choices)
                cmd = {"cmd": "action", "action": "select_map_node",
                       "args": {"col": ch["col"], "row": ch["row"]}}
            else:
                cmd = {"cmd": "action", "action": "proceed"}
        elif decision == "card_reward":
            cards = state.get("cards", [])
            if cards:
                cmd = {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 0}}
            else:
                cmd = {"cmd": "action", "action": "skip_card_reward"}
        elif decision == "rest_site":
            opts = state.get("options", [])
            enabled = [o for o in opts if o.get("is_enabled")]
            heal = next((o for o in enabled if o.get("option_id") == "HEAL"), None)
            pick = heal or (enabled[0] if enabled else None)
            if pick:
                cmd = {"cmd": "action", "action": "choose_option", "args": {"option_index": pick["index"]}}
            else:
                cmd = {"cmd": "action", "action": "leave_room"}
        elif decision == "event_choice":
            opts = state.get("options", [])
            unlocked = [o for o in opts if not o.get("is_locked")]
            if unlocked:
                cmd = {"cmd": "action", "action": "choose_option", "args": {"option_index": unlocked[0]["index"]}}
            else:
                cmd = {"cmd": "action", "action": "leave_room"}
        elif decision == "shop":
            cmd = {"cmd": "action", "action": "leave_room"}
        elif decision == "bundle_select":
            cmd = {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}
        elif decision == "card_select":
            cards = state.get("cards", [])
            if cards:
                cmd = {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
            else:
                cmd = {"cmd": "action", "action": "skip_select"}
        else:
            cmd = {"cmd": "action", "action": "proceed"}

        # Log context before sending
        context = {
            "step": step, "decision": decision, "floor": floor,
            "action": cmd.get("action"),
        }
        if decision == "combat_play":
            context["energy"] = state.get("energy")
            context["round"] = state.get("round")
            context["hand_size"] = len(state.get("hand", []))
            context["enemies"] = [
                {"name": e.get("name", {}).get("en", "?") if isinstance(e.get("name"), dict) else str(e.get("name", "?")),
                 "hp": e.get("hp"), "intent": e.get("intent", {}).get("type", "?")}
                for e in state.get("enemies", [])
            ]
            if cmd.get("action") == "play_card":
                ci = cmd["args"]["card_index"]
                card = next((c for c in hand if c["index"] == ci), {})
                context["card"] = card.get("name", {}).get("en", "?") if isinstance(card.get("name"), dict) else "?"

        t0 = time.monotonic()
        state = send(cmd)
        dt = time.monotonic() - t0

        context["response_time"] = round(dt, 2)
        if state == "TIMEOUT":
            context["result"] = "TIMEOUT"
            log.append(context)
            proc.kill()
            return {"seed": seed, "steps": step, "result": "hang",
                    "hang_context": context, "log": log[-10:]}
        elif state is None:
            context["result"] = "EOF"
            log.append(context)
            proc.kill()
            return {"seed": seed, "steps": step, "result": "crash",
                    "crash_context": context, "log": log[-10:]}

        if dt > 1.0:
            context["slow"] = True
        log.append(context)

    proc.terminate()
    return {"seed": seed, "steps": max_steps, "result": "max_steps", "log": log[-5:]}


if __name__ == "__main__":
    hangs = []
    crashes = []
    slow_steps = []

    for i in range(30):
        seed = f"hang_test_{i}_{random.randint(0,99999)}"
        print(f"Run {i+1}/30 (seed={seed})...", end=" ", flush=True)
        result = run_game_with_logging(seed)
        status = result["result"]
        steps = result.get("steps", 0)

        if status == "hang":
            ctx = result["hang_context"]
            print(f"HANG at step {steps}: {ctx.get('decision')} {ctx.get('action')} "
                  f"floor={ctx.get('floor')} enemies={ctx.get('enemies','?')}")
            hangs.append(result)
        elif status == "crash":
            ctx = result["crash_context"]
            print(f"CRASH at step {steps}: {ctx.get('decision')} {ctx.get('action')} "
                  f"floor={ctx.get('floor')}")
            crashes.append(result)
        elif status == "game_over":
            v = "WIN" if result.get("victory") else "LOSS"
            print(f"{v} at step {steps}")
        else:
            print(f"{status} at step {steps}")

        # Collect slow steps
        for entry in result.get("log", []):
            if entry.get("slow"):
                slow_steps.append(entry)

    print(f"\n{'='*60}")
    print(f"SUMMARY: {len(hangs)} hangs, {len(crashes)} crashes, {len(slow_steps)} slow steps")

    if hangs:
        print(f"\nHANG DETAILS:")
        for h in hangs:
            ctx = h["hang_context"]
            print(f"  seed={h['seed']} step={h['steps']} decision={ctx.get('decision')} "
                  f"action={ctx.get('action')} card={ctx.get('card','?')} "
                  f"enemies={ctx.get('enemies','?')} floor={ctx.get('floor')}")
            # Print last few log entries before hang
            for entry in h.get("log", [])[-3:]:
                print(f"    {entry.get('step'):3d}: {entry.get('decision'):15s} {entry.get('action'):20s} "
                      f"time={entry.get('response_time',0):.1f}s")

    if crashes:
        print(f"\nCRASH DETAILS:")
        for c in crashes:
            ctx = c["crash_context"]
            print(f"  seed={c['seed']} step={c['steps']} decision={ctx.get('decision')} "
                  f"action={ctx.get('action')}")

    if slow_steps:
        print(f"\nSLOW STEPS (>1s):")
        by_decision = {}
        for s in slow_steps:
            key = f"{s.get('decision')}:{s.get('action')}"
            by_decision.setdefault(key, []).append(s.get('response_time', 0))
        for key, times in sorted(by_decision.items(), key=lambda x: -max(x[1])):
            print(f"  {key}: count={len(times)}, max={max(times):.1f}s, avg={np.mean(times):.1f}s")
