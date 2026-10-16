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

Continuing a batch whose launcher died (reboot, kill): `--resume <batch dir | batch id>
[--max-lanes N] [--dry-run]`. Every lane restarts from the batch's own manifest, argv and
environment, appending to the same <lane>.jsonl / .log / games/: a lane's progress is the
longest run of its seeds (<prefix><offset+1>, ...) that have a results row, so it resumes at
the next seed with `--seed-offset` and `--game-index-offset` advanced (game_index counts on
from the games already played); a lane with all its games is not started. Rows that do not
continue that run (a gap, a duplicate, a seed outside the lane's range) abort the resume --
it never guesses -- and so does a lane still running play_full_run. A truncated last row
(a lane killed mid-write) is skipped with a warning. The manifest gains a `resumes` entry;
`--dry-run` prints the plan and the lane commands and writes nothing.

    .venv/bin/python python/run_batch.py --resume p3b-route-ab-20261012-160225
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
from collections import Counter
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
                 "--seed-prefix", "--seed-offset", "--ascension", "--game-index-offset")

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
    `<Character>/<arm>`). Every arm of a character gets the same seed arguments.
    A lane spec carrying `resume` ({"games", "seed_offset", "game_index_offset"}, set by
    run_batch.py --resume) plays only its remaining games, from its next seed."""
    prefix = list(args.play_full_run_cmd) if args.play_full_run_cmd else _default_prefix()
    batch_dir = Path(batch_dir)
    spec = lane_spec(args, lane)
    resume = spec.get("resume")
    games = resume["games"] if resume else args.games
    seed_offset = resume["seed_offset"] if resume else args.seed_offset
    stem = safe_lane_name(lane)
    games_dir = batch_dir / "games"
    if spec["arm"]:
        # Kept logs are named <character>_<policy>_a<ascension>_<seed>: the arms of one
        # character play the same seeds, so a shared directory would have the second
        # arm overwrite the first's logs (and the results rows' game_log paths).
        games_dir = games_dir / stem
    cmd = [*prefix, str(games), spec["character"],
           "--ascension", str(args.ascension),
           "--seed-prefix", args.seed_prefix]
    if seed_offset:
        cmd += ["--seed-offset", str(seed_offset)]
    cmd += ["--batch-id", batch_id, "--lane", lane,
            "--keep-game-logs", str(games_dir),
            "--results-log", str(batch_dir / f"{stem}.jsonl")]
    if resume and resume["game_index_offset"] > 0:
        cmd += ["--game-index-offset", str(resume["game_index_offset"])]
    cmd += args.extra
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

    def __init__(self, batch_dir, lanes, max_lanes, make_command, env, clock=time.time,
                 initial_status=None):
        """`env` is the lanes' environment, or a function lane -> environment.
        `initial_status` (lane -> entry; run_batch.py --resume) seeds the status of lanes
        that must not (re)start: a "done" lane is never started and stays "done"."""
        self.batch_dir = Path(batch_dir)
        self.max_lanes = max_lanes
        self._make_command = make_command
        self._env = env
        self._clock = clock
        self._order = list(lanes)
        self._procs = {}
        self.status = {lane: {"state": "queued", "pid": None, "started_at": None,
                              "ended_at": None, "returncode": None} for lane in self._order}
        for lane, entry in (initial_status or {}).items():
            if lane not in self.status:
                raise ValueError(f"initial status for unknown lane {lane!r}")
            if entry.get("state") not in ("queued", "done"):
                raise ValueError(f"initial status of {lane!r} must be queued or done, "
                                 f"got {entry.get('state')!r}")
            self.status[lane] = dict(entry)

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


# --- --resume ---------------------------------------------------------------------------------

def _resume_requested(argv):
    """True if `--resume` is among the launcher's own arguments (before any bare `--`:
    what follows it belongs to play_full_run)."""
    for arg in argv:
        if arg == "--":
            return False
        if arg == "--resume" or arg.startswith("--resume="):
            return True
    return False


def _build_resume_parser():
    parser = argparse.ArgumentParser(
        prog="run_batch.py",
        usage="run_batch.py --resume BATCH [--max-lanes N] [--dry-run] [--batches-dir DIR]",
        description="Continue a batch whose launcher died (reboot, kill): each unfinished "
                    "lane restarts after its last completed seed and appends to the same "
                    "files; finished lanes are skipped. The batch's own manifest supplies "
                    "everything else, so no launch option is accepted.")
    parser.add_argument("--resume", required=True, metavar="BATCH",
                        help="The batch directory, or a batch id under --batches-dir.")
    parser.add_argument("--max-lanes", type=_positive_int, default=None,
                        help="Lanes running at once (default: the batch's own).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan and the lane commands; write and start nothing.")
    parser.add_argument("--batches-dir", default=str(BATCHES_DIR), metavar="DIR",
                        help="Where a batch id is looked up (default logs/batches).")
    parser.add_argument("--play-full-run-cmd", default=None, help=argparse.SUPPRESS)
    return parser


def parse_resume_args(argv):
    """Arguments of `run_batch.py --resume ...`; any other launch option, or a bare `--`,
    is an argparse error (exit 2)."""
    argv = list(argv)
    parser = _build_resume_parser()
    if "--" in argv:
        parser.error("a bare `--` cannot be combined with --resume: the lanes restart with "
                     "the play_full_run arguments the batch was launched with")
    opts, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.error(f"--resume continues the batch exactly as it was launched and takes no "
                     f"other launch options (unexpected: {' '.join(unknown)})")
    if opts.play_full_run_cmd is not None:
        opts.play_full_run_cmd = shlex.split(opts.play_full_run_cmd)
        if not opts.play_full_run_cmd:
            parser.error("--play-full-run-cmd must not be empty")
    return opts


def _resume_error(message):
    return SystemExit(f"run_batch.py: error: {message}")


def _read_json_file(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise _resume_error(f"cannot read {what} {path}: {exc}")


def _resolve_batch_dir(target, batches_dir):
    """`target` if it is an existing directory, else <batches_dir>/<target>."""
    path = Path(target)
    if not path.is_dir():
        path = Path(batches_dir) / target
    if not (path / "manifest.json").is_file():
        raise _resume_error(f"no manifest.json in {path} -- not a batch directory "
                            f"(--resume takes a batch directory or a batch id under "
                            f"{batches_dir})")
    return path.resolve()


def _is_play_full_run_alive(pid):
    """True if `pid` is a live process whose command line mentions play_full_run. (After a
    reboot a recorded pid is often reused by something else: that is not a running lane.)"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "play_full_run" in out.stdout


def _read_lane_status(batch_dir):
    path = Path(batch_dir) / "lanes_status.json"
    try:
        status = _read_json_file(path, "lanes_status.json")
    except FileNotFoundError:
        return {}
    if not isinstance(status, dict):
        raise _resume_error(f"{path} is not a JSON object")
    return status


def _check_batch_not_running(status):
    for lane, entry in status.items():
        if not isinstance(entry, dict) or entry.get("state") != "running":
            continue
        pid = entry.get("pid")
        if _is_play_full_run_alive(pid):
            raise _resume_error(
                f"the batch is still running: lane {lane} (pid {pid}) is alive and running "
                "play_full_run; stop it first (resuming now would play its seeds twice)")


def _read_result_seeds(path):
    """The "seed" of every results row of `path`, in file order (None for a row without a
    string seed). A line that is not a JSON object -- typically the last one of a lane
    killed mid-write -- is skipped with a warning naming the file and line."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    seeds = []
    for number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except ValueError:
            row = None
        if not isinstance(row, dict):
            print(f"WARNING: {path.name} line {number}: not a JSON object (a lane killed "
                  "mid-write?), skipped", flush=True)
            continue
        seed = row.get("seed")
        seeds.append(seed if isinstance(seed, str) else None)
    return seeds


def _plan_lane(entry, seeds):
    """Where a lane stands: its `done` games are the longest run of seeds
    <prefix><offset+1>, <prefix><offset+2>, ... that all have a results row. Rows that are
    not part of that run (a gap, a duplicate, a seed outside the lane's range) are `stray`."""
    prefix, base, planned = entry["seed_prefix"], entry["seed_offset"], entry["planned_games"]
    expected = [f"{prefix}{base + k}" for k in range(1, planned + 1)]
    counts = Counter(seeds)
    done = 0
    while done < planned and counts[expected[done]]:
        done += 1
    played = set(expected[:done])
    stray = []
    for seed in seeds:
        label = "<row without a seed>" if seed is None else seed
        if seed not in played and label not in stray:
            stray.append(label)
    stray += [f"{seed} (appears {counts[seed]} times)" for seed in expected[:done]
              if counts[seed] > 1]
    return {"lane": entry["lane"], "prefix": prefix, "base": base, "planned": planned,
            "done": done, "stray": stray, "complete": done >= planned,
            "remaining": planned - done}


def _lane_stray_error(plan, file_name):
    shown = ", ".join(plan["stray"][:8]) + (" ..." if len(plan["stray"]) > 8 else "")
    run = (f"{plan['prefix']}{plan['base'] + 1}..{plan['prefix']}{plan['base'] + plan['done']}"
           if plan["done"] else "none")
    return (f"lane {plan['lane']}: {file_name} has rows that do not continue the lane's seeds "
            f"{plan['prefix']}{plan['base'] + 1}..{plan['prefix']}{plan['base'] + plan['planned']} "
            f"(completed run: {run}): {shown}. Fix or move those rows by hand; --resume "
            "never guesses")


def _validate_manifest_lane(entry, args):
    ok = (isinstance(entry, dict) and isinstance(entry.get("lane"), str)
          and isinstance(entry.get("seed_prefix"), str)
          and all(isinstance(entry.get(k), int) and not isinstance(entry.get(k), bool)
                  for k in ("planned_games", "seed_offset")))
    if not ok:
        raise _resume_error(f"manifest lane entry is malformed: {entry!r}")
    if entry["seed_prefix"] != args.seed_prefix:
        raise _resume_error(f"manifest lane {entry['lane']} has seed_prefix "
                            f"{entry['seed_prefix']!r} but the batch's argv says "
                            f"{args.seed_prefix!r}")


def _terminate_last_line(path):
    """Make a non-empty file end with a newline (a lane killed mid-write leaves a partial
    last line; the resumed lane's first row would otherwise be glued onto it). Returns True
    if one was added."""
    try:
        with open(path, "rb+") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return False
            fh.seek(0, os.SEEK_END)
            fh.write(b"\n")
            return True
    except FileNotFoundError:
        return False


def _plan_text(plans, batch_id, batch_dir, max_lanes):
    resumed = [plan for plan in plans if not plan["complete"]]
    width = max([12, *(len(plan["lane"]) for plan in plans)])
    lines = [f"Resuming batch {batch_id}: {len(resumed)} of {len(plans)} lane(s) to resume, "
             f"max {max_lanes} at once\n  {batch_dir}"]
    for plan in plans:
        done = f"{plan['done']}/{plan['planned']} done"
        if plan["complete"]:
            what = "complete, not started"
        else:
            first = f"{plan['prefix']}{plan['base'] + plan['done'] + 1}"
            last = f"{plan['prefix']}{plan['base'] + plan['planned']}"
            what = f"resumed seeds {first}..{last} ({plan['remaining']} game(s))"
        lines.append(f"  {plan['lane']:<{width}} {done:<12} {what}")
    return "\n".join(lines)


def _run_to_completion(runner, batch_id, batch_dir, lanes, planned):
    started = time.monotonic()
    code = run_lanes(runner)
    if code == 130:
        print(f"Batch {batch_id} stopped; lanes terminated, see {batch_dir}/lanes_status.json")
        return code
    print(format_summary(batch_id, batch_dir, lanes, planned, runner.status,
                         time.monotonic() - started), flush=True)
    return code


def resume_batch(argv):
    """`run_batch.py --resume BATCH ...`: restart the unfinished lanes of an interrupted
    batch in its own directory. Returns the launcher's exit code (0 / 1 / 130)."""
    opts = parse_resume_args(argv)
    batch_dir = _resolve_batch_dir(opts.resume, opts.batches_dir)
    manifest = _read_json_file(batch_dir / "manifest.json", "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("argv"), list) \
            or not manifest.get("batch_id") or not isinstance(manifest.get("lanes"), list):
        raise _resume_error(f"{batch_dir / 'manifest.json'} is not a run_batch manifest "
                            "(needs batch_id, argv and lanes)")
    if manifest.get("dry_run"):
        raise _resume_error(f"batch {manifest['batch_id']} was a --dry-run: it never ran; "
                            "start a new batch")
    batch_id = manifest["batch_id"]

    # the original arguments, exactly as launched (arms, env, ascension, extra args ...)
    args = parse_args(manifest["argv"][1:])
    if opts.max_lanes is not None:
        args.max_lanes = opts.max_lanes
    args.dry_run = opts.dry_run
    if opts.play_full_run_cmd is not None:
        args.play_full_run_cmd = opts.play_full_run_cmd
    known = [spec["lane"] for spec in args.lanes]
    for entry in manifest["lanes"]:
        _validate_manifest_lane(entry, args)
    lanes = [entry["lane"] for entry in manifest["lanes"]]
    if sorted(lanes) != sorted(known):
        raise _resume_error(f"the manifest's lanes ({', '.join(lanes)}) do not match the "
                            f"lanes its argv builds ({', '.join(known)})")

    status = _read_lane_status(batch_dir)
    _check_batch_not_running(status)

    plans, errors = [], []
    for entry in manifest["lanes"]:
        file_name = f"{safe_lane_name(entry['lane'])}.jsonl"
        plan = _plan_lane(entry, _read_result_seeds(batch_dir / file_name))
        if plan["stray"]:
            errors.append(_lane_stray_error(plan, file_name))
        plans.append(plan)
    if errors:
        raise _resume_error("\n  ".join(errors))

    initial = {}
    for plan in plans:
        lane = plan["lane"]
        if plan["complete"]:
            old = status.get(lane) if isinstance(status.get(lane), dict) else {}
            initial[lane] = dict(old) if old.get("state") == "done" else {
                "state": "done", "pid": old.get("pid"), "started_at": old.get("started_at"),
                "ended_at": old.get("ended_at"), "returncode": old.get("returncode")}
        else:
            lane_spec(args, lane)["resume"] = {
                "games": plan["remaining"], "seed_offset": plan["base"] + plan["done"],
                "game_index_offset": plan["done"]}
    resumed = [plan for plan in plans if not plan["complete"]]

    print(_plan_text(plans, batch_id, batch_dir, args.max_lanes), flush=True)
    resumes = manifest.get("resumes")
    previous = (resumes[-1].get("git_commit") if resumes and isinstance(resumes[-1], dict)
                else manifest.get("git_commit"))
    current = _git_commit()
    if previous and current and previous != current:
        print(f"WARNING: the remaining games run on different code than the batch started "
              f"on (HEAD is {current}, the batch last ran on {previous}); results from "
              "before and after the resume are not strictly comparable", flush=True)
    if not resumed:
        print(f"Batch {batch_id} is already complete: every lane has all its games; "
              "nothing to resume")
        return 0

    if args.dry_run:
        for plan in resumed:
            lane = plan["lane"]
            print(shlex.join(build_lane_command(args, batch_id, batch_dir, lane)))
            if lane_spec(args, lane)["arm"]:
                print(f"  {lane} env: " + " ".join(
                    f"{k}={v}" for k, v in lane_spec(args, lane)["arm_env"].items()))
        print("env: " + " ".join(f"{k}={v}" for k, v in lane_env_overrides(args).items()))
        print("dry run: nothing written, no lane started")
        return 0

    now = _now()
    for plan in resumed:
        stem = safe_lane_name(plan["lane"])
        if _terminate_last_line(batch_dir / f"{stem}.jsonl"):
            print(f"[{plan['lane']}] {stem}.jsonl ended mid-row: added a newline so the "
                  "resumed lane's rows start on their own line", flush=True)
        log_path = batch_dir / f"{stem}.log"
        _terminate_last_line(log_path)
        first = f"{plan['prefix']}{plan['base'] + plan['done'] + 1}"
        last = f"{plan['prefix']}{plan['base'] + plan['planned']}"
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"=== run_batch --resume {now.isoformat(timespec='seconds')}: seeds "
                      f"{first}..{last} ({plan['done']} already done) ===\n")
    manifest.setdefault("resumes", []).append({
        "at": now.timestamp(), "git_commit": current, "max_lanes": args.max_lanes,
        "lanes": {plan["lane"]: {"done": plan["done"], "seed_offset": plan["base"] + plan["done"],
                                 "games": plan["remaining"]} for plan in resumed}})
    _write_json_atomic(batch_dir / "manifest.json", manifest)

    runner = LaneRunner(batch_dir, lanes, args.max_lanes,
                        lambda lane: build_lane_command(args, batch_id, batch_dir, lane),
                        lambda lane: build_lane_env(args, lane), initial_status=initial)
    return _run_to_completion(runner, batch_id, batch_dir, lanes, args.games)


# --- main -------------------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if _resume_requested(argv):
        return resume_batch(argv)
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
    return _run_to_completion(runner, batch_id, batch_dir, lanes, args.games)


if __name__ == "__main__":
    sys.exit(main())
