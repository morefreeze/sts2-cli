#!/usr/bin/env python3
"""Launch a regression / A-B / smoke batch of `play_full_run.py` games.

    .venv/bin/python python/run_batch.py --name regression --kind regression \\
        --games 5 --chars all

One lane per character, each a `play_full_run.py N CHAR` process of its own, at
most --max-lanes at a time. Everything the run workbench's 回归/评测 tab shows is
under logs/batches/<batch_id>/ (kept; game_log.cleanup_old_logs never purges it):

    manifest.json       what was planned (written BEFORE any lane starts)
    lanes_status.json   {lane: {state, pid, started_at, ended_at, returncode}}
                        state: queued | running | done | failed; rewritten
                        atomically on every change
    <lane>.log          the lane's stdout+stderr
    <lane>.jsonl        its results rows (play_full_run --results-log)
    games/              its game logs (play_full_run --keep-game-logs)

The launcher stays in the foreground until every lane has exited, prints a
per-lane summary (results rows and the `Wins/Completed` line from the lane log)
and exits non-zero if any lane did. SIGTERM/SIGINT stops every lane's process
group and exits 130.

An A/B as ONE batch: `--arm NAME:KEY=VAL[,KEY=VAL...]` (repeatable) makes the lanes
characters x arms, named `<Character>/<arm>`. Each lane's environment is the base
environment plus its arm's KEY=VALs (the arm wins over --env); every arm of a
character plays the same seeds, so the arms pair. Lanes are queued character by
character (Ironclad/a, Ironclad/b, Silent/a, ...) so the arms progress together
under --max-lanes. Per-lane files use the lane name with '/' written as '__'
(`Ironclad__value.jsonl`, `.log`, and a `games/Ironclad__value/` log directory:
two arms would otherwise overwrite each other's kept logs). Without --arm nothing
changes, and --kind defaults to `ab` when arms are given.

    .venv/bin/python python/run_batch.py --name route-ab --games 40 --chars all \
        --ascension 1 --seed-prefix p3b_ \
        --arm random:STS2_ROUTE_POLICY=random --arm value:STS2_ROUTE_POLICY=value
"""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCHES_DIR = ROOT / "logs" / "batches"

# Same list as play_full_run.VALID_CHARACTERS (a test pins the two together);
# importing play_full_run here would pull in the engine plumbing for nothing.
CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")
KINDS = ("regression", "ab", "smoke")

# The macOS/Steam default every other entry point (play_full_run, combat_env,
# sts2_bridge) falls back to when STS2_GAME_DIR is unset.
DEFAULT_GAME_DIR = os.path.expanduser(
    "~/Library/Application Support/Steam/steamapps/common/"
    "Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64")

POLL_INTERVAL_S = 1.0   # how often lanes are polled for exit
TERM_GRACE_S = 10.0     # after SIGTERM, before SIGKILL, when stopping lanes

# Flags the launcher sets itself on every lane; a duplicate after `--` would win
# silently (argparse: last one wins) and the manifest would then lie about the batch.
MANAGED_FLAGS = ("--batch-id", "--lane", "--results-log", "--keep-game-logs",
                 "--seed-prefix", "--seed-offset", "--ascension")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# An arm name becomes half of a lane name and of a file name: no '/', no spaces.
_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMPLETED_RE = re.compile(r"Completed: (\d+)/(\d+)")


def _now():
    return dt.datetime.now()


# --- command line ----------------------------------------------------------------

def _positive_int(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="run_batch.py",
        description="Launch a batch of play_full_run lanes (one per character, or per "
                    "character x --arm) with a manifest and lane status the run "
                    "workbench reads.",
        epilog="Arguments after a bare `--` are passed to every play_full_run lane.")
    parser.add_argument("--name", required=True,
                        help="Batch name; the batch id is <name>-<YYYYmmdd-HHMMSS>.")
    parser.add_argument("--kind", choices=KINDS, default=None,
                        help="Default: regression, or ab when --arm is given.")
    parser.add_argument("--games", type=_positive_int, required=True,
                        help="Games per lane (per character).")
    parser.add_argument("--chars", required=True,
                        help="'all' or a comma list of: " + ", ".join(CHARACTERS))
    parser.add_argument("--max-lanes", type=_positive_int, default=6,
                        help="Lanes running at once; the rest queue (default 6).")
    parser.add_argument("--threads", type=_positive_int, default=3,
                        help="STS2_SOLVER_THREADS per lane (default 3).")
    parser.add_argument("--ascension", type=_non_negative_int, default=0)
    parser.add_argument("--seed-prefix", default="run_")
    parser.add_argument("--seed-offset", type=_non_negative_int, default=0,
                        help="Start seeds at <prefix><offset+1> (default 0).")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VAL",
                        help="Environment override for every lane (repeatable).")
    parser.add_argument("--arm", action="append", default=[],
                        metavar="NAME:KEY=VAL[,KEY=VAL...]",
                        help="An A/B arm (repeatable). Lanes become characters x arms, "
                             "named <Character>/<NAME>; each lane's environment is the "
                             "base environment plus its arm's KEY=VALs. Every arm plays "
                             "the same seeds. Values may not contain a comma.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Write the manifest and print the lane commands; start nothing.")
    parser.add_argument("--batches-dir", default=str(BATCHES_DIR), metavar="DIR",
                        help="Where batch directories live (default logs/batches).")
    # Test hook: replaces `run_caffeinated.sh .venv/bin/python -u play_full_run.py`.
    parser.add_argument("--play-full-run-cmd", default=None, help=argparse.SUPPRESS)
    return parser


def _parse_arm(parser, item):
    """`NAME:KEY=VAL[,KEY=VAL...]` -> {"name", "env"}; exits via argparse if malformed."""
    name, sep, rest = item.partition(":")
    name = name.strip()
    if not sep or not _ARM_NAME_RE.match(name):
        parser.error(f"--arm expects NAME:KEY=VAL[,KEY=VAL...] where NAME is 1-32 characters "
                     f"of letters, digits, '.', '_' or '-', got {item!r}")
    env = {}
    for pair in rest.split(","):
        key, eq, value = pair.partition("=")
        key = key.strip()
        if not eq or not _ENV_KEY_RE.match(key):
            parser.error(f"--arm {name}: expected KEY=VAL (KEY an environment variable "
                         f"name), got {pair!r}")
        if key in env:
            parser.error(f"--arm {name}: {key} is set twice")
        env[key] = value
    return {"name": name, "env": env}


def parse_args(argv):
    """Parsed, validated arguments. args.chars is a list, args.env a dict,
    args.extra the play_full_run arguments after `--`, args.arms a list of
    {"name", "env"} (empty without --arm) and args.lanes the lane specs
    (see build_lanes). Exits via argparse on bad input."""
    argv = list(argv)
    extra = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1:]
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not _NAME_RE.match(args.name):
        parser.error("--name must be 1-64 characters of letters, digits, '.', '_' or '-' "
                     f"(it becomes a directory name), got {args.name!r}")

    if args.chars.strip() == "all":
        args.chars = list(CHARACTERS)
    else:
        names = [name.strip() for name in args.chars.split(",")]
        unknown = [name for name in names if name not in CHARACTERS]
        if unknown:
            parser.error(f"--chars: unknown character {unknown[0]!r} "
                         f"(valid: {', '.join(CHARACTERS)}, or 'all')")
        duplicated = sorted({name for name in names if names.count(name) > 1})
        if duplicated:
            parser.error(f"--chars: {duplicated[0]} listed more than once")
        args.chars = names

    overrides = {}
    for item in args.env:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--env expects KEY=VAL, got {item!r}")
        overrides[key] = value
    args.env = overrides

    args.arms = [_parse_arm(parser, item) for item in args.arm]
    seen = {}
    for arm in args.arms:
        # APFS is case-insensitive: Random/random would share a results file
        if arm["name"].lower() in seen:
            parser.error(f"--arm: {arm['name']!r} duplicates {seen[arm['name'].lower()]!r}")
        seen[arm["name"].lower()] = arm["name"]
    if args.kind is None:
        args.kind = "ab" if args.arms else "regression"
    args.lanes = build_lanes(args.chars, args.arms)

    for arg in extra:
        flag = arg.split("=", 1)[0]
        if flag in MANAGED_FLAGS:
            parser.error(f"{flag} is set by run_batch.py itself and cannot be passed after "
                         "`--` (use run_batch.py's own flag where one exists)")
    args.extra = extra

    if args.play_full_run_cmd is not None:
        args.play_full_run_cmd = shlex.split(args.play_full_run_cmd)
        if not args.play_full_run_cmd:
            parser.error("--play-full-run-cmd must not be empty")
    return args


# --- lanes ---------------------------------------------------------------------------

def build_lanes(chars, arms):
    """The lane specs, in queue order. Without arms: one lane per character, named for
    it. With arms: characters x arms, character-major (Ironclad/a, Ironclad/b, Silent/a,
    ...), so the arms of a character start together and progress together under
    --max-lanes."""
    if not arms:
        return [{"lane": char, "character": char, "arm": None, "arm_env": {}}
                for char in chars]
    return [{"lane": f"{char}/{arm['name']}", "character": char, "arm": arm["name"],
             "arm_env": dict(arm["env"])} for char in chars for arm in arms]


def safe_lane_name(lane):
    """A lane name as a file name: '/' (character/arm) becomes '__'. Characters are
    letters and arm names are [A-Za-z0-9._-], so this is injective and `Silent` is
    still `Silent`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", lane.replace("/", "__"))


def lane_spec(args, lane):
    """The spec of the lane named `lane`; a name no spec has is a bare character."""
    for spec in args.lanes:
        if spec["lane"] == lane:
            return spec
    return {"lane": lane, "character": lane, "arm": None, "arm_env": {}}


# --- lane command and environment -----------------------------------------------------

def _default_prefix():
    """`run_caffeinated.sh .venv/bin/python -u python/play_full_run.py` (macOS idle-sleeps
    after ~1 min and freezes a long batch otherwise); without caffeinate, no wrapper."""
    venv_python = ROOT / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    prefix = [python, "-u", str(ROOT / "python" / "play_full_run.py")]
    if shutil.which("caffeinate"):
        prefix.insert(0, str(ROOT / "scripts" / "run_caffeinated.sh"))
    return prefix


def build_lane_command(args, batch_id, batch_dir, lane):
    """argv of the play_full_run process for `lane` (the character, or
    `<Character>/<arm>`). Every arm of a character gets the same seed arguments."""
    prefix = list(args.play_full_run_cmd) if args.play_full_run_cmd else _default_prefix()
    batch_dir = Path(batch_dir)
    spec = lane_spec(args, lane)
    stem = safe_lane_name(lane)
    games_dir = batch_dir / "games"
    if spec["arm"]:
        # Kept logs are named <character>_<policy>_a<ascension>_<seed>: the arms of one
        # character play the same seeds, so a shared directory would have the second
        # arm overwrite the first's logs (and the results rows' game_log paths).
        games_dir = games_dir / stem
    cmd = [*prefix, str(args.games), spec["character"],
           "--ascension", str(args.ascension),
           "--seed-prefix", args.seed_prefix]
    if args.seed_offset:
        cmd += ["--seed-offset", str(args.seed_offset)]
    cmd += ["--batch-id", batch_id, "--lane", lane,
            "--keep-game-logs", str(games_dir),
            "--results-log", str(batch_dir / f"{stem}.jsonl"),
            *args.extra]
    return cmd


def lane_env_overrides(args, lane=None):
    """What the launcher sets on top of the inherited environment (and records in
    the manifest): the solver thread count, then the --env overrides, which win; for
    an arm lane, then that arm's KEY=VALs, which win over both."""
    overrides = {"STS2_SOLVER_THREADS": str(args.threads), **args.env}
    if lane is not None:
        overrides.update(lane_spec(args, lane)["arm_env"])
    return overrides


def build_lane_env(args, lane=None):
    env = dict(os.environ)
    if not env.get("STS2_GAME_DIR"):
        env["STS2_GAME_DIR"] = DEFAULT_GAME_DIR
    env.update(lane_env_overrides(args, lane))
    return env


# --- files ---------------------------------------------------------------------------

def _write_json_atomic(path, payload):
    """Readers (the workbench) poll these files: never let them see a half-written one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _git_commit():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _manifest_lane(args, spec):
    entry = {"lane": spec["lane"], "character": spec["character"],
             "planned_games": args.games, "seed_prefix": args.seed_prefix,
             "seed_offset": args.seed_offset, "extra_args": list(args.extra)}
    if spec["arm"]:
        entry["arm"] = spec["arm"]
        entry["arm_env"] = dict(spec["arm_env"])
    return entry


def build_manifest(args, batch_id, argv, created_at):
    manifest = {
        "batch_id": batch_id,
        "name": args.name,
        "kind": args.kind,
        "created_at": created_at,
        "git_commit": _git_commit(),
        "argv": ["python/run_batch.py", *argv],
        "env": lane_env_overrides(args),
        "lanes": [_manifest_lane(args, spec) for spec in args.lanes],
    }
    if args.dry_run:
        # A dry run leaves a manifest behind; flag it so a reader can tell it never ran.
        manifest["dry_run"] = True
    return manifest


# --- running lanes -------------------------------------------------------------------------

class LaneRunner:
    """Runs lanes as subprocesses, at most `max_lanes` at once, in the order given.

    Each lane is its own session (start_new_session) so one SIGTERM to its process
    group stops the lane and anything it started, and a terminal Ctrl-C reaches only
    the launcher. step() advances one poll tick (reap exits, start queued lanes) and
    returns True while any lane is still queued or running.
    """

    def __init__(self, batch_dir, lanes, max_lanes, make_command, env, clock=time.time):
        """`env` is the lanes' environment, or a function lane -> environment."""
        self.batch_dir = Path(batch_dir)
        self.max_lanes = max_lanes
        self._make_command = make_command
        self._env = env
        self._clock = clock
        self._order = list(lanes)
        self._procs = {}
        self.status = {lane: {"state": "queued", "pid": None, "started_at": None,
                              "ended_at": None, "returncode": None} for lane in self._order}

    def write_status(self):
        _write_json_atomic(self.batch_dir / "lanes_status.json", self.status)

    def _running(self):
        return [lane for lane in self._order if self.status[lane]["state"] == "running"]

    def _queued(self):
        return [lane for lane in self._order if self.status[lane]["state"] == "queued"]

    def _start(self, lane):
        entry = self.status[lane]
        entry["started_at"] = self._clock()
        log_path = self.batch_dir / f"{safe_lane_name(lane)}.log"
        try:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(
                    self._make_command(lane), cwd=ROOT,
                    env=self._env(lane) if callable(self._env) else self._env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
        except OSError as exc:
            entry.update(state="failed", ended_at=self._clock(), error=str(exc))
            print(f"[{lane}] could not start: {exc}", flush=True)
            return
        self._procs[lane] = proc
        entry.update(state="running", pid=proc.pid)
        print(f"[{lane}] started (pid {proc.pid}); log {log_path}", flush=True)

    def step(self):
        changed = False
        for lane in self._running():
            returncode = self._procs[lane].poll()
            if returncode is None:
                continue
            self.status[lane].update(state="done" if returncode == 0 else "failed",
                                     ended_at=self._clock(), returncode=returncode)
            print(f"[{lane}] exited with {returncode}", flush=True)
            changed = True
        for lane in self._queued():
            if len(self._running()) >= self.max_lanes:
                break
            self._start(lane)
            changed = True
        if changed:
            self.write_status()
        return bool(self._running() or self._queued())

    def failed_lanes(self):
        return [lane for lane in self._order if self.status[lane]["state"] == "failed"]

    def terminate_all(self):
        """SIGTERM every running lane's process group (SIGKILL after TERM_GRACE_S) and
        mark every lane that has not finished -- running or still queued -- failed."""
        running = self._running()
        for lane in running:
            self._signal_group(self._procs[lane], signal.SIGTERM)
        deadline = time.monotonic() + TERM_GRACE_S
        for lane in running:
            proc = self._procs[lane]
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._signal_group(proc, signal.SIGKILL)
                proc.wait()
            self.status[lane].update(state="failed", ended_at=self._clock(),
                                     returncode=proc.returncode)
        for lane in self._queued():
            self.status[lane].update(state="failed", ended_at=self._clock())
        self.write_status()

    @staticmethod
    def _signal_group(proc, sig):
        try:
            os.killpg(proc.pid, sig)   # start_new_session: the lane's pgid is its pid
        except (ProcessLookupError, PermissionError):
            pass


def _install_stop_handlers(on_signal):
    """Route SIGINT/SIGTERM to on_signal; returns the previous handlers to restore.
    (signal.signal only works on the main thread -- elsewhere there is nothing to hook.)"""
    previous = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, on_signal)
    return previous


def run_lanes(runner):
    """Drive `runner` to completion. Returns 0, 1 (a lane failed) or 130 (stopped by a
    signal: every lane has been terminated and marked failed)."""
    wake = threading.Event()
    received = []

    def on_signal(signum, frame):
        received.append(signum)
        wake.set()

    previous = _install_stop_handlers(on_signal)
    try:
        runner.write_status()
        while True:
            if received:
                name = signal.Signals(received[0]).name
                print(f"\n{name} received: stopping every lane", flush=True)
                runner.terminate_all()
                return 130
            if not runner.step():
                break
            wake.wait(POLL_INTERVAL_S)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 1 if runner.failed_lanes() else 0


# --- summary -------------------------------------------------------------------------------

def _count_rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0


def _completed_line(log_path):
    """The last `Wins: .., Completed: a/b, ..` line of a lane log, or None."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 262144))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [line.strip() for line in tail.splitlines() if _COMPLETED_RE.search(line)]
    return lines[-1] if lines else None


def format_summary(batch_id, batch_dir, lanes, planned, status, elapsed_s):
    batch_dir = Path(batch_dir)
    lines = [f"\nBatch {batch_id} finished in {elapsed_s / 60:.1f} min -- {batch_dir}"]
    failed = 0
    width = max([12, *(len(lane) for lane in lanes)])
    for lane in lanes:
        entry = status[lane]
        stem = safe_lane_name(lane)
        rows = _count_rows(batch_dir / f"{stem}.jsonl")
        completed = _completed_line(batch_dir / f"{stem}.log")
        if entry["state"] == "failed":
            failed += 1
            verdict = "FAILED"
            if entry.get("error"):
                verdict += f" ({entry['error']})"
            elif entry["returncode"] is not None:
                verdict += f" (exit {entry['returncode']})"
        else:
            verdict = "done"
        text = completed if completed else "no Wins/Completed line in the lane log"
        match = _COMPLETED_RE.search(completed or "")
        if entry["state"] == "done" and (not match or int(match.group(1)) < int(match.group(2))):
            text += "   <-- INCOMPLETE"
        lines.append(f"  {lane:<{width}} {verdict:<14} rows {rows}/{planned}   {text}")
    lines.append(f"{failed} lane(s) failed" if failed else "All lanes exited 0")
    return "\n".join(lines)


# --- main -------------------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)

    created = _now()
    batch_id = f"{args.name}-{created:%Y%m%d-%H%M%S}"
    batch_dir = Path(args.batches_dir).resolve() / batch_id
    try:
        batch_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SystemExit(f"run_batch.py: error: {batch_dir} already exists "
                         "(same --name started twice in one second?)")
    _write_json_atomic(batch_dir / "manifest.json",
                       build_manifest(args, batch_id, argv, created.timestamp()))

    lanes = [spec["lane"] for spec in args.lanes]
    print(f"Batch {batch_id}: {len(lanes)} lane(s) x {args.games} game(s), "
          f"max {args.max_lanes} at once\n  {batch_dir}", flush=True)
    if args.dry_run:
        for lane in lanes:
            print(shlex.join(build_lane_command(args, batch_id, batch_dir, lane)))
            if lane_spec(args, lane)["arm"]:
                print(f"  {lane} env: " + " ".join(
                    f"{k}={v}" for k, v in lane_spec(args, lane)["arm_env"].items()))
        print("env: " + " ".join(f"{k}={v}" for k, v in lane_env_overrides(args).items()))
        print("dry run: manifest written, no lane started")
        return 0

    runner = LaneRunner(batch_dir, lanes, args.max_lanes,
                        lambda lane: build_lane_command(args, batch_id, batch_dir, lane),
                        lambda lane: build_lane_env(args, lane))
    started = time.monotonic()
    code = run_lanes(runner)
    if code == 130:
        print(f"Batch {batch_id} stopped; lanes terminated, see {batch_dir}/lanes_status.json")
        return code
    print(format_summary(batch_id, batch_dir, lanes, args.games, runner.status,
                         time.monotonic() - started), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
