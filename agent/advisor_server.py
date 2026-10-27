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

`--exit-on-stdin-eof`: exit when stdin reaches EOF. The mod starts the service
with a piped stdin it never writes to, so the pipe closes when the game process
dies for any reason (a crash included) and the service does not outlive it.
"""
import os

# Before anything imports numpy / scikit-learn: the card_reward scorer calls
# sklearn HistGradientBoosting predict, whose OpenMP thread pool oversubscribes
# a busy machine (the game is running next to us) and made one card_reward
# ~20x slower (2.1 s vs 47.9 s for a replayed game). One thread gives the same
# predictions; only the speed changes.
os.environ.setdefault("OMP_NUM_THREADS", "1")

import argparse
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import play_full_run  # noqa: E402
from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, GreedyTrace,  # noqa: E402
                                behavior_env_is_default, reset_greedy_context)

DEFAULT_PORT = 8767
MAX_BODY = 4 * 1024 * 1024  # a decision JSON is a few KB; this only bounds abuse
CLIENT_TIMEOUT = 5  # seconds; requests are served one at a time, so a stalled client must not hold the line
ADVISED = frozenset(play_full_run.OOC_GREEDY_DECISIONS | {"map_select"})


def knob_warnings(env) -> list:
    """`KEY=value` for every greedy behaviour env var set to something other than
    its default (STS2_CARD_QUALITY_GATE=1 is the default, so it is not listed)."""
    return [f"{k}={env[k].strip()}" for k in GREEDY_BEHAVIOR_ENV
            if not behavior_env_is_default(k, env.get(k))]


def _warmup_state() -> dict:
    """A minimal valid card_reward decision (Ironclad, A1F3, starter deck), used
    only to warm greedy up. Real ids, so the card scorer takes its real path."""
    def card(card_id, name, cost, kind, rarity=None):
        return {"id": card_id, "name": name, "cost": cost, "type": kind,
                "rarity": rarity, "upgraded": False, "description": "", "stats": None,
                "keywords": None, "after_upgrade": None}
    deck = ([card("CARD.STRIKE_IRONCLAD", "Strike", 1, "Attack")] * 5
            + [card("CARD.DEFEND_IRONCLAD", "Defend", 1, "Skill")] * 4
            + [card("CARD.BASH", "Bash", 2, "Attack")])
    offered = [card("CARD.POMMEL_STRIKE", "Pommel Strike", 1, "Attack", "Common"),
               card("CARD.SHRUG_IT_OFF", "Shrug It Off", 1, "Skill", "Common"),
               card("CARD.IRON_WAVE", "Iron Wave", 1, "Attack", "Common")]
    return {"type": "decision", "decision": "card_reward",
            "context": {"act": 1, "floor": 3, "room_type": "Monster"},
            "cards": [dict(c, index=i) for i, c in enumerate(offered)],
            "can_skip": True, "gold_earned": 15,
            "player": {"name": "The Ironclad", "hp": 72, "max_hp": 80, "block": 0,
                       "gold": 99, "relics": [{"name": "Burning Blood"}], "potions": [],
                       "deck_size": len(deck), "deck": deck}}


def warm_up(greedy) -> float:
    """Run greedy once on a built-in card_reward so the first real request is not
    the one that pays for the lazy loads (sklearn + the deck predictor: ~2 s when
    the machine is idle, 10-19 s when it is busy -- longer than the mod's request
    timeout). Measured per decision type: card_reward is the only cold one; shop,
    event_choice, rest_site, card_select and bundle_select take milliseconds cold.

    Resets greedy's hidden state afterwards, so the warm-up leaves nothing behind.
    A failure is logged and swallowed: the server must start regardless. Returns
    the elapsed seconds."""
    started = time.perf_counter()
    try:
        try:
            greedy(_warmup_state(), trace=GreedyTrace())
        finally:
            reset_greedy_context()
    except Exception as exc:
        print(f"advisor: warm-up failed ({type(exc).__name__}: {exc}); "
              "the first card_reward will be slow", file=sys.stderr)
    return time.perf_counter() - started


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
    advice = dict(reply, action=action, **evidence)
    try:
        # strict JSON only, and the WHOLE reply: a NaN/Infinity anywhere (a score,
        # a rule value, the action) would break the mod's parser
        json.dumps(advice, default=_json_default, allow_nan=False)
    except (ValueError, TypeError):
        reply["warnings"].append("trace_not_serializable")
        return reply
    return advice


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sts2Advisor/1"
    timeout = CLIENT_TIMEOUT  # socket timeout for every read on a connection

    def log_request(self, code="-", size="-"):
        """Log failures only: the mod forwards our stderr into its log and polls /health."""
        try:
            failed = int(code) >= 400
        except (TypeError, ValueError):
            failed = True
        if failed:
            super().log_request(code, size)

    def do_GET(self):
        try:
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True, "policy": "greedy", "commit": self.server.commit,
                             "warnings": self.server.warnings})
        except Exception as exc:
            self._fail(exc)

    def do_POST(self):
        try:
            if self.path != "/advise":
                return self._send(404, {"error": "not found"})
            body, error = self._read_body()
            if error:
                return self._send(*error)
            try:
                state = json.loads(body)
            except ValueError:  # includes UnicodeDecodeError
                return self._send(400, {"error": "body is not JSON"})
            if not isinstance(state, dict):
                return self._send(400, {"error": "body must be a decision object"})
            self._send(200, advise(state, self.server.greedy, self.server.warnings))
        except Exception as exc:
            self._fail(exc)

    def _read_body(self):
        """(body bytes, None) or (None, (status, error body)) -- never hangs for
        longer than the socket timeout and never trusts Content-Length blindly."""
        raw = (self.headers.get("Content-Length") or "").strip()
        if not (raw.isascii() and raw.isdigit()):
            return None, (400, {"error": "Content-Length must be a non-negative integer"})
        length = int(raw)
        if length > MAX_BODY:
            return None, (413, {"error": f"body larger than {MAX_BODY} bytes"})
        try:
            body = self.rfile.read(length)
        except OSError:  # includes the socket timeout and a reset connection
            return None, (400, {"error": "could not read the request body"})
        if len(body) != length:
            return None, (400, {"error": "request body shorter than Content-Length"})
        return body, None

    def _fail(self, exc: Exception) -> None:
        """Unexpected error: one log line, a 500 if the client is still there."""
        self.log_error("%s %s failed: %s: %s", self.command, self.path,
                       type(exc).__name__, exc)
        try:
            self._send(500, {"error": type(exc).__name__})
        except Exception:
            pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=_json_default, ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(HTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):  # client went away: no traceback
            print(f"advisor: {client_address[0]}: {type(exc).__name__}", file=sys.stderr)
        else:
            super().handle_error(request, client_address)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def watch_stdin_eof(stream=None, exit_fn=os._exit) -> threading.Thread:
    """Start a daemon thread that blocks reading `stream` (default: stdin) and calls
    exit_fn(0) at EOF. A stream that cannot be read at all counts as EOF too: the
    parent that owned the pipe is gone. Bytes are read and dropped, never decoded."""
    if stream is None:
        stream = getattr(sys.stdin, "buffer", sys.stdin)

    def run():
        try:
            read = getattr(stream, "read1", stream.read)
            while read(65536):
                pass
        except (OSError, ValueError, AttributeError):
            pass
        exit_fn(0)

    thread = threading.Thread(target=run, name="stdin-eof-watch", daemon=True)
    thread.start()
    return thread


def make_server(port: int, greedy=None, env=None, warm: bool = True) -> HTTPServer:
    greedy = greedy or play_full_run._load_greedy_action()
    # warm greedy up BEFORE binding: a client that connects early waits in the
    # backlog / gets "connection refused" instead of timing out on a cold request
    warmup_s = warm_up(greedy) if warm else None
    server = _Server(("127.0.0.1", port), _Handler)
    server.greedy = greedy
    server.warmup_s = warmup_s
    server.warnings = knob_warnings(os.environ if env is None else env)
    server.commit = _git_commit()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sts2Advisor local advice service")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--exit-on-stdin-eof", action="store_true",
                    help="exit when stdin reaches EOF (the parent process died)")
    args = ap.parse_args(argv)
    if args.exit_on_stdin_eof:
        watch_stdin_eof()  # before the warm-up: an orphan must not sit through that either
    server = make_server(args.port)
    for w in server.warnings:
        print(f"WARNING: greedy behaviour env set, advice differs from the measured policy: {w}",
              file=sys.stderr)
    print(f"Sts2Advisor advice service on http://127.0.0.1:{server.server_address[1]} "
          f"(commit {server.commit}, warm-up {server.warmup_s:.1f} s)",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
