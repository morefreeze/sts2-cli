# agent/advisor_server.py
"""Local advice service for the in-game Sts2Advisor mod. Binds 127.0.0.1 only.

  POST /advise  body: one decision JSON (the headless engine's shape)
                reply: {"action", "options", "rules", "warnings", "unverified", "policy"}
  GET  /health  reply: {"ok", "policy", "commit", "warnings"}

`action` is exactly what play_full_run's greedy policy plays (same import path,
same defaults); `options` / `rules` are the GreedyTrace of that same call.
Requests are served one at a time: greedy has hidden per-process state
(see agent.greedy_trace.reset_greedy_context), which, as in a play_full_run
lane, carries from one decision to the next.

The mod parses replies with a strict JSON parser, so the service never emits
NaN/Infinity: a trace that cannot be encoded strictly becomes "no advice".

    .venv/bin/python agent/advisor_server.py --port 8767
"""
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import play_full_run  # noqa: E402
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, GreedyTrace  # noqa: E402

DEFAULT_PORT = 8767
ADVISED = frozenset(play_full_run.OOC_GREEDY_DECISIONS | {"map_select"})


def knob_warnings(env) -> list:
    """`KEY=value` for every greedy behaviour env var that is set (non-blank)."""
    return [f"{k}={env[k]}" for k in GREEDY_BEHAVIOR_ENV if (env.get(k) or "").strip()]


def _json_default(obj):
    if hasattr(obj, "item"):  # numpy scalars
        return obj.item()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def advise(state: dict, greedy, warnings: list) -> dict:
    decision = state.get("decision")
    reply = {"action": None, "options": [], "rules": [], "warnings": list(warnings),
             "unverified": decision == "map_select", "policy": "greedy"}
    if decision not in ADVISED:
        reply["warnings"].append(f"not_advised: {decision}")
        return reply
    trace = GreedyTrace()
    try:
        action = greedy(state, trace=trace)
        evidence = trace.to_dict()
    except Exception as exc:  # a trace bug lands here too: no advice beats wrong reasons
        reply["warnings"].append(f"greedy_raised: {type(exc).__name__}")
        return reply
    if action is None:
        reply["warnings"].append("greedy_none")
        return reply
    try:
        # strict JSON only: a NaN/Infinity score would break the mod's parser
        json.dumps(evidence, default=_json_default, allow_nan=False)
    except (ValueError, TypeError):
        reply["warnings"].append("trace_not_serializable")
        return reply
    reply["action"] = action
    reply.update(evidence)
    return reply


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sts2Advisor/1"

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True, "policy": "greedy", "commit": self.server.commit,
                         "warnings": self.server.warnings})

    def do_POST(self):
        if self.path != "/advise":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            state = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if not isinstance(state, dict):
            return self._send(400, {"error": "body must be a decision object"})
        self._send(200, advise(state, self.server.greedy, self.server.warnings))

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=_json_default, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def make_server(port: int, greedy=None, env=None) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.greedy = greedy or play_full_run._load_greedy_action()
    server.warnings = knob_warnings(os.environ if env is None else env)
    server.commit = _git_commit()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sts2Advisor local advice service")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    server = make_server(args.port)
    for w in server.warnings:
        print(f"WARNING: greedy behaviour env set, advice differs from the measured policy: {w}",
              file=sys.stderr)
    print(f"Sts2Advisor advice service on http://127.0.0.1:{server.server_address[1]} "
          f"(commit {server.commit})", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
