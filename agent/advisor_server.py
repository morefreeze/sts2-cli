# agent/advisor_server.py
"""Local advice service for the in-game Sts2Advisor mod. Binds 127.0.0.1 only.

  POST /advise  body: one decision JSON (the headless engine's shape)
                reply: {"action", "options", "rules", "warnings", "unverified", "policy"}
  POST /potion_swap  body: {"state": a decision (player.potions, player.potion_slots),
                           "offered": [potion dicts, as in player.potions]}
                reply: {"discard", "take", "discard_id", "take_id", "held_scores",
                        "offered_scores", "margin", "reason"} -- see agent/potion_swap.py
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

`--reload-on-commit`: hot reload. Every RELOAD_INTERVAL seconds a daemon thread compares
git HEAD with the HEAD at startup; once it moved and agent/, python/ and data/ hold no
uncommitted change, the service waits for the request in flight (if any), logs
`hot reload: <old> -> <new>` and re-executes itself (same argv, same env, same stdin
pipe), so a policy commit is live in the running game without restarting it.
Not supported on Windows (os.execv there starts a new process and exits this one): the flag is then
ignored with a warning.
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
from agent.potion_swap import choose_potion_swap  # noqa: E402
from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, GreedyTrace,  # noqa: E402
                                behavior_env_is_default, reset_greedy_context)

DEFAULT_PORT = 8767
RELOAD_INTERVAL = 30  # seconds between HEAD checks (--reload-on-commit)
# Paths whose uncommitted edits must never be served: everything this process imports or reads.
RELOAD_PATHS = ("agent/", "python/", "data/")
MAX_BODY = 4 * 1024 * 1024  # a decision JSON is a few KB; this only bounds abuse
CLIENT_TIMEOUT = 5  # seconds; requests are served one at a time, so a stalled client must not hold the line
ADVISED = frozenset(play_full_run.OOC_GREEDY_DECISIONS | {"map_select"})


# Settings the advisor itself chooses (config.json -> SidecarProcess sets the env), so they are
# not "the measured policy differs" warnings: {env var: value that is expected}. The panel
# states the active map policy itself (its footer reads "whole-path lookahead (A/B: ...)").
# STS2_MAP_POLICY=lookahead won its paired A/B (a5, 43 pairs, +5.42 floors, t 2.84); a value
# nobody measured, or any other variable, still warns.
#
# STS2_CARD_QUALITY_GATE=0 (2026-11-03): in real games the act-1 gate skipped 149 of 165
# late-act-1 card rewards (43 of them act-1 boss rare choices); its only evidence (July, old
# PPO policy, pre-fix engine) predates the engine fix. The headless default stays ON until a
# paired A/B, so the advice service sets it itself (advisor_env_defaults) -- advice here
# differs from play_full_run on purpose.
EXPECTED_SETTINGS = {"STS2_MAP_POLICY": "lookahead", "STS2_CARD_QUALITY_GATE": "0"}
# Env the advice service defaults at startup (an explicit value in the environment wins).
ADVISOR_ENV_DEFAULTS = {"STS2_CARD_QUALITY_GATE": "0"}


def advisor_env_defaults(environ=None) -> list:
    """setdefault ADVISOR_ENV_DEFAULTS into `environ` (os.environ); returns the log lines
    describing the act-1 card quality gate setting."""
    environ = os.environ if environ is None else environ
    explicit = "STS2_CARD_QUALITY_GATE" in environ
    for k, v in ADVISOR_ENV_DEFAULTS.items():
        environ.setdefault(k, v)
    flag = environ["STS2_CARD_QUALITY_GATE"].strip().lower()
    state = "on" if flag in ("1", "true", "on", "yes") else "off"
    return [f"card quality gate: {state} ({'environment' if explicit else 'advisor default'})"]


def knob_warnings(env) -> list:
    """`KEY=value` for every greedy behaviour env var set to something other than
    its default (STS2_CARD_QUALITY_GATE=1 is the default, so it is not listed) --
    except an EXPECTED_SETTINGS value (STS2_MAP_POLICY=lookahead, the config's
    map_policy; STS2_CARD_QUALITY_GATE=0, the advisor's own default)."""
    return [f"{k}={env[k].strip()}" for k in GREEDY_BEHAVIOR_ENV
            if not behavior_env_is_default(k, env.get(k))
            and env[k].strip().lower() != EXPECTED_SETTINGS.get(k)]


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


def potion_swap(request: dict):
    """(reply, None) or (None, error message) for a /potion_swap body."""
    state, offered = request.get("state"), request.get("offered")
    player = state.get("player") if isinstance(state, dict) else None
    if not isinstance(player, dict):
        return None, "state.player must be an object"
    if not isinstance(offered, list) or not all(isinstance(p, dict) for p in offered):
        return None, "offered must be a list of potion objects"
    return choose_potion_swap(player, offered), None


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
        with self.server.inflight:
            self._get()

    def do_POST(self):
        with self.server.inflight:
            self._post()

    def _get(self):
        try:
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True, "policy": "greedy", "commit": self.server.commit,
                             "warnings": self.server.warnings})
        except Exception as exc:
            self._fail(exc)

    def _post(self):
        try:
            if self.path not in ("/advise", "/potion_swap"):
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
            if self.path == "/potion_swap":
                reply, problem = potion_swap(state)
                if problem:
                    return self._send(400, {"error": problem})
                return self._send(200, reply)
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


class InFlight:
    """Counts requests being handled. `run_if_idle(fn)` runs fn while holding the lock only when
    nothing is in flight; a request that arrives meanwhile waits at `with guard:` until fn
    returns (for a reload fn never returns: the process image is replaced)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count = 0

    def __enter__(self):
        with self._lock:
            self._count += 1
        return self

    def __exit__(self, *exc):
        with self._lock:
            self._count -= 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def run_if_idle(self, fn) -> bool:
        with self._lock:
            if self._count:
                return False
            fn()
            return True


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


def run_git(args, root: str = ROOT) -> str:
    """stdout of `git -C root <args>`; "" when git is missing or fails (no lock files taken)."""
    try:
        done = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def reload_decision(start_head: str, head: str, dirty: bool) -> str:
    """"none" (HEAD unchanged or unreadable), "wait_dirty" (moved, but the served code has
    uncommitted edits) or "reload"."""
    if not head or head == start_head:
        return "none"
    return "wait_dirty" if dirty else "reload"


class HotReloader:
    """--reload-on-commit. check() is one poll; start() runs it every `interval` seconds on a
    daemon thread. `git(args) -> stdout` and `execv` are injectable for tests."""

    def __init__(self, inflight: InFlight, start_head: str, git=run_git, execv=os.execv,
                 argv=None, log=None, interval: float = RELOAD_INTERVAL):
        self.inflight = inflight
        self.start_head = start_head
        self.git = git
        self.execv = execv
        self.argv = [sys.executable] + (sys.argv if argv is None else argv)
        self.log = log or (lambda msg: print(f"[advisor_server] {msg}", file=sys.stderr, flush=True))
        self.interval = interval
        self._dirty_logged_for = None

    def check(self) -> str:
        """One poll. Returns the decision; "reload" also means the request guard was idle and the
        exec was attempted (a real execv does not return; if it raises, the old image keeps
        serving and the next poll retries)."""
        head = self.git(["rev-parse", "HEAD"])
        # Cheap first: no need to ask about the tree while HEAD has not moved.
        dirty = bool(head and head != self.start_head
                     and self.git(["status", "--porcelain", "--untracked-files=no", "--", *RELOAD_PATHS]))
        decision = reload_decision(self.start_head, head, dirty)
        if decision == "wait_dirty":
            if self._dirty_logged_for != head:  # once per new HEAD, not every 30 s
                self._dirty_logged_for = head
                self.log(f"HEAD moved {self.start_head[:7]} -> {head[:7]} but {' '.join(RELOAD_PATHS)} "
                         "have uncommitted changes; keeping the running code until they are committed")
        elif decision == "reload":
            def go():
                self.log(f"hot reload: {self.start_head[:7]} -> {head[:7]}")
                try:
                    self.execv(sys.executable, self.argv)
                except OSError as exc:
                    self.log(f"hot reload failed, keeping the running code: {exc}")
            if not self.inflight.run_if_idle(go):
                return "busy"
        return decision

    def start(self) -> threading.Thread:
        def run():
            while True:
                time.sleep(self.interval)
                try:
                    self.check()
                except Exception as exc:  # never let the watcher die silently
                    self.log(f"hot reload check failed: {type(exc).__name__}: {exc}")
        thread = threading.Thread(target=run, name="hot-reload", daemon=True)
        thread.start()
        return thread


def reload_supported(os_name: str | None = None) -> bool:
    """--reload-on-commit needs a real exec(): on Windows (os.name == "nt") os.execv spawns a NEW process
    and exits this one, so the parent (the mod, which owns the stdin pipe and watches this pid) would
    see the service exit. Hot reload is therefore POSIX-only."""
    return (os.name if os_name is None else os_name) != "nt"


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
    server.inflight = InFlight()
    server.warmup_s = warmup_s
    server.warnings = knob_warnings(os.environ if env is None else env)
    server.commit = _git_commit()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sts2Advisor local advice service")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--exit-on-stdin-eof", action="store_true",
                    help="exit when stdin reaches EOF (the parent process died)")
    ap.add_argument("--reload-on-commit", action="store_true",
                    help="re-exec the service when git HEAD moves and agent/ python/ data/ are clean")
    args = ap.parse_args(argv)
    reload_on_commit = args.reload_on_commit
    if reload_on_commit and not reload_supported():
        print("[advisor_server] WARNING: --reload-on-commit is not supported on Windows "
              "(os.execv would spawn a new process and exit this one); hot reload is off, "
              "restart the game to pick up a policy change", file=sys.stderr, flush=True)
        reload_on_commit = False
    start_head = run_git(["rev-parse", "HEAD"]) if reload_on_commit else ""
    gate_lines = advisor_env_defaults()  # before make_server: the warm-up and knob_warnings read os.environ
    if args.exit_on_stdin_eof:
        watch_stdin_eof()  # before the warm-up: an orphan must not sit through that either
    server = make_server(args.port)
    for line in gate_lines:
        print(f"[advisor_server] {line}", file=sys.stderr, flush=True)
    for w in server.warnings:
        print(f"WARNING: greedy behaviour env set, advice differs from the measured policy: {w}",
              file=sys.stderr)
    print(f"Sts2Advisor advice service on http://127.0.0.1:{server.server_address[1]} "
          f"(commit {server.commit}, warm-up {server.warmup_s:.1f} s)",
          file=sys.stderr, flush=True)
    if reload_on_commit and start_head:
        HotReloader(server.inflight, start_head).start()
        print(f"[advisor_server] hot reload on: HEAD {start_head[:7]} checked every {RELOAD_INTERVAL} s",
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
