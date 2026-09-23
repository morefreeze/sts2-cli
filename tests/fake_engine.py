"""Stand-in for the Sts2Headless engine, for the watchdog tests. Not a test module.

Speaks just enough of the JSON line protocol: a non-JSON warm-up line, then
{"type": "ready"}, then one reply per command. `start_run` returns a
combat_play decision; `plan_combat_turn` returns a combat_plan whose
search.budget_ms is --plan-budget-ms; the command or action named by
--hang-on never gets a reply (the BUG-040 shape); `quit` exits.

It also starts a long-sleeping child and writes its pid to --pidfile. That child
stands in for the real engine process that `dotnet run` spawns, so tests can
prove the watchdog kills the whole process group -- killing only the wrapper
would leave a real BUG-040 hang spinning a core forever.
"""
import argparse
import json
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--pidfile", required=True)
parser.add_argument("--hang-on", default="hang")
parser.add_argument("--plan-budget-ms", type=int, default=120_000)
args = parser.parse_args()

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(args.pidfile, "w") as fh:
    fh.write(str(child.pid))

COMBAT = {
    "type": "decision", "decision": "combat_play", "round": 3, "energy": 3,
    "context": {"act": 1, "floor": 8, "room_type": "Monster"},
    "player": {"hp": 28, "max_hp": 80, "gold": 0},
    "hand": [], "enemies": [{"index": 0, "hp": 38, "combat_id": 1}],
}

print("warming up, not JSON", flush=True)
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "quit":
        break
    if args.hang_on in (cmd.get("cmd"), cmd.get("action")):
        time.sleep(600)
    if cmd.get("cmd") == "start_run":
        reply = COMBAT
    elif cmd.get("action") == "plan_combat_turn":
        reply = {"type": "combat_plan", "actions": [],
                 "search": {"budget_ms": args.plan_budget_ms, "elapsed_ms": 1,
                            "boundary": "None", "expanded_nodes": 1,
                            "total_expanded_nodes": 1}}
    else:
        reply = {"type": "decision", "decision": "echo", "echo": cmd}
    print(json.dumps(reply), flush=True)
child.kill()
