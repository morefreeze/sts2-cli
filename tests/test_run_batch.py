"""python/run_batch.py -- the regression/eval batch launcher.

No game and no engine: every lane is a tiny fake `play_full_run` (FAKE_LANE below,
handed to the launcher through its hidden --play-full-run-cmd hook) that records
what it was started with, waits for a per-lane "release" file, then writes the
results rows / Completed line a real lane would and exits with the code in the
release file. That makes queueing and status transitions deterministic.
"""
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIR = ROOT / "python"
sys.path.insert(0, str(PYTHON_DIR))
import run_batch  # noqa: E402

RUN_BATCH_PY = PYTHON_DIR / "run_batch.py"

FAKE_LANE = r'''
import json, os, sys, time
args = sys.argv[1:]


def opt(name):
    return args[args.index(name) + 1] if name in args else None


n = int(args[0])
lane = opt("--lane")
fname = lane.replace("/", "__")      # an --arm lane is "<Character>/<arm>"
dump = os.environ["FAKE_DUMP_DIR"]
batch_dir = os.path.dirname(opt("--results-log"))
with open(os.path.join(dump, fname + ".start.json"), "w") as fh:
    json.dump({
        "argv": args, "pid": os.getpid(), "pgid": os.getpgid(0), "cwd": os.getcwd(),
        "manifest_seen": os.path.exists(os.path.join(batch_dir, "manifest.json")),
        "env": {k: os.environ.get(k)
                for k in ("STS2_GAME_DIR", "STS2_SOLVER_THREADS", "FAKE_MARK",
                          "STS2_ROUTE_POLICY")},
    }, fh)
lane_release = os.path.join(dump, "release_" + fname)
all_release = os.path.join(dump, "release_all")
deadline = time.time() + 120          # never outlive a broken test
while not (os.path.exists(lane_release) or os.path.exists(all_release)):
    if time.time() > deadline:
        sys.exit(99)
    time.sleep(0.02)
path = lane_release if os.path.exists(lane_release) else all_release
code = int(open(path).read().strip() or "0")
rows = int(os.environ.get("FAKE_ROWS", n))
print("chatter on stdout")
print("chatter on stderr", file=sys.stderr)
with open(opt("--results-log"), "a") as fh:
    for i in range(rows):
        fh.write(json.dumps({"seed": "run_%d" % (i + 1), "status": "dead", "lane": lane,
                             "batch_id": opt("--batch-id")}) + "\n")
print("Wins: 0/%d, Completed: %d/%d, avg_floor=3.0" % (n, rows, n))
sys.exit(code)
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake lane script, a dump dir, and a launcher pointed at tmp_path/batches."""
    fake = tmp_path / "fake_lane.py"
    fake.write_text(FAKE_LANE)
    dump = tmp_path / "dump"
    dump.mkdir()
    monkeypatch.setenv("FAKE_DUMP_DIR", str(dump))
    monkeypatch.delenv("FAKE_ROWS", raising=False)
    monkeypatch.setattr(run_batch, "POLL_INTERVAL_S", 0.02)

    class Env:
        batches = tmp_path / "batches"
        dump_dir = dump
        prefix = shlex.join([sys.executable, str(fake)])

        @staticmethod
        def argv(*extra, name="reg", games="2", chars="Ironclad,Silent"):
            return ["--name", name, "--games", games, "--chars", chars,
                    "--batches-dir", str(Env.batches),
                    "--play-full-run-cmd", Env.prefix, *extra]

        @staticmethod
        def release(lane="all", code=0):
            (dump / f"release_{lane.replace('/', '__')}").write_text(str(code))

        @staticmethod
        def started(lane):
            return json.loads((dump / f"{lane.replace('/', '__')}.start.json").read_text())

        @staticmethod
        def batch_dir():
            (only,) = list(Env.batches.iterdir())
            return only

    yield Env
    # a test that leaves a lane waiting must not leave its process spinning
    (dump / "release_all").write_text("0")


def _wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- argument parsing --------------------------------------------------------------

def test_launcher_character_list_matches_play_full_run():
    import play_full_run
    assert list(run_batch.CHARACTERS) == play_full_run.VALID_CHARACTERS


def test_chars_all_is_every_character_in_order():
    args = run_batch.parse_args(["--name", "r", "--games", "5", "--chars", "all"])
    assert args.chars == list(run_batch.CHARACTERS)


def test_chars_accepts_a_comma_list_and_strips_space():
    args = run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "Silent, Defect"])
    assert args.chars == ["Silent", "Defect"]


@pytest.mark.parametrize("bad", ["Ironcladd", "Silent,Silent", "", "Silent,,Defect"])
def test_bad_chars_are_rejected(bad, capsys):
    with pytest.raises(SystemExit):
        run_batch.parse_args(["--name", "r", "--games", "1", "--chars", bad])
    assert "--chars" in capsys.readouterr().err


@pytest.mark.parametrize("argv, flag", [
    (["--name", "r", "--games", "0", "--chars", "all"], "--games"),
    (["--name", "r", "--games", "-3", "--chars", "all"], "--games"),
    (["--name", "r", "--games", "1", "--chars", "all", "--max-lanes", "0"], "--max-lanes"),
    (["--name", "r", "--games", "1", "--chars", "all", "--threads", "0"], "--threads"),
    (["--name", "r", "--games", "1", "--chars", "all", "--ascension", "-1"], "--ascension"),
    (["--name", "r", "--games", "1", "--chars", "all", "--seed-offset", "-1"], "--seed-offset"),
    (["--name", "r", "--games", "1", "--chars", "all", "--env", "NOEQUALS"], "--env"),
    (["--name", "r", "--games", "1", "--chars", "all", "--env", "=x"], "--env"),
    (["--name", "bad/name", "--games", "1", "--chars", "all"], "--name"),
    (["--name", "has space", "--games", "1", "--chars", "all"], "--name"),
    (["--name", "", "--games", "1", "--chars", "all"], "--name"),
    (["--name", "r", "--games", "1", "--chars", "all", "--kind", "weird"], "--kind"),
])
def test_bad_values_are_rejected_naming_the_flag(argv, flag, capsys):
    with pytest.raises(SystemExit):
        run_batch.parse_args(argv)
    assert flag in capsys.readouterr().err


def test_arguments_after_a_double_dash_go_to_every_lane_verbatim():
    args = run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all",
                                 "--", "--game-version", "v0.111.0", "--experiment=x"])
    assert args.extra == ["--game-version", "v0.111.0", "--experiment=x"]
    assert run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all"]).extra == []


@pytest.mark.parametrize("managed", ["--batch-id", "--lane", "--results-log",
                                     "--keep-game-logs", "--seed-prefix", "--seed-offset",
                                     "--ascension"])
def test_extra_args_may_not_override_launcher_managed_flags(managed, capsys):
    """The manifest describes what ran; a duplicate flag after `--` would silently win."""
    for spelled in ([managed, "x"], [f"{managed}=x"]):
        with pytest.raises(SystemExit):
            run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all",
                                  "--", *spelled])
        assert managed in capsys.readouterr().err


def test_env_overrides_are_collected_in_order():
    args = run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all",
                                 "--env", "A=1", "--env", "STS2_OOC_POLICY=naive",
                                 "--env", "B=x=y"])
    assert args.env == {"A": "1", "STS2_OOC_POLICY": "naive", "B": "x=y"}


# --- lane command ----------------------------------------------------------------

def _lane_args(cmd):
    """Everything after the play_full_run script in a default-prefix command."""
    i = next(i for i, part in enumerate(cmd) if part.endswith("play_full_run.py"))
    return cmd[i + 1:]


def test_default_lane_command_is_the_caffeinated_unbuffered_play_full_run(tmp_path):
    args = run_batch.parse_args(["--name", "r", "--games", "5", "--chars", "all",
                                 "--ascension", "1", "--seed-prefix", "p_", "--",
                                 "--game-version", "v0.111.0"])
    cmd = run_batch.build_lane_command(args, "r-20261012-101500", tmp_path, "Silent")
    assert "-u" in cmd[:-1] and cmd[cmd.index("-u") + 1].endswith("play_full_run.py")
    assert _lane_args(cmd) == [
        "5", "Silent", "--ascension", "1", "--seed-prefix", "p_",
        "--batch-id", "r-20261012-101500", "--lane", "Silent",
        "--keep-game-logs", str(tmp_path / "games"),
        "--results-log", str(tmp_path / "Silent.jsonl"),
        "--game-version", "v0.111.0"]
    # the whole thing runs under scripts/run_caffeinated.sh when caffeinate exists
    assert cmd[0].endswith("run_caffeinated.sh") or cmd[0].endswith("python")


def test_seed_offset_is_passed_only_when_set(tmp_path):
    plain = run_batch.parse_args(["--name", "r", "--games", "5", "--chars", "all"])
    assert "--seed-offset" not in run_batch.build_lane_command(plain, "b", tmp_path, "Silent")
    shifted = run_batch.parse_args(["--name", "r", "--games", "5", "--chars", "all",
                                    "--seed-offset", "10"])
    cmd = run_batch.build_lane_command(shifted, "b", tmp_path, "Silent")
    assert cmd[cmd.index("--seed-offset") + 1] == "10"


def test_play_full_run_cmd_hook_replaces_the_default_prefix(tmp_path):
    args = run_batch.parse_args(["--name", "r", "--games", "2", "--chars", "all",
                                 "--play-full-run-cmd", "/bin/echo hello world"])
    cmd = run_batch.build_lane_command(args, "b", tmp_path, "Defect")
    assert cmd[:3] == ["/bin/echo", "hello", "world"] and cmd[3:5] == ["2", "Defect"]


def test_lane_env_defaults_the_game_dir_threads_and_applies_overrides(monkeypatch):
    monkeypatch.delenv("STS2_GAME_DIR", raising=False)
    monkeypatch.setenv("FROM_PARENT", "yes")
    args = run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all",
                                 "--threads", "4", "--env", "FAKE_MARK=hi",
                                 "--env", "STS2_OOC_POLICY=naive"])
    lane_env = run_batch.build_lane_env(args)
    assert lane_env["STS2_GAME_DIR"].endswith("data_sts2_macos_arm64")
    assert "Slay the Spire 2" in lane_env["STS2_GAME_DIR"]
    assert lane_env["STS2_SOLVER_THREADS"] == "4"
    assert lane_env["FAKE_MARK"] == "hi" and lane_env["STS2_OOC_POLICY"] == "naive"
    assert lane_env["FROM_PARENT"] == "yes"  # the rest of the environment is inherited


def test_lane_env_keeps_an_explicit_game_dir_and_lets_env_beat_threads(monkeypatch):
    monkeypatch.setenv("STS2_GAME_DIR", "/elsewhere/data")
    args = run_batch.parse_args(["--name", "r", "--games", "1", "--chars", "all",
                                 "--threads", "4", "--env", "STS2_SOLVER_THREADS=7"])
    lane_env = run_batch.build_lane_env(args)
    assert lane_env["STS2_GAME_DIR"] == "/elsewhere/data"
    assert lane_env["STS2_SOLVER_THREADS"] == "7"


# --- manifest and dry run ----------------------------------------------------------------

def test_manifest_is_written_with_the_planned_lanes(env):
    env.release("all")
    before = time.time()
    rc = run_batch.main(env.argv("--env", "FAKE_MARK=m1", "--threads", "2",
                                 "--ascension", "1", "--seed-prefix", "z_",
                                 "--kind", "ab", "--", "--game-version", "v9",
                                 name="my-ab", games="3", chars="Silent,Defect"))
    assert rc == 0
    batch_dir = env.batch_dir()
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    assert manifest["batch_id"] == batch_dir.name
    assert manifest["batch_id"].startswith("my-ab-")
    stamp = manifest["batch_id"][len("my-ab-"):]
    assert len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit()
    assert manifest["name"] == "my-ab" and manifest["kind"] == "ab"
    assert before - 1 <= manifest["created_at"] <= time.time() + 1
    assert manifest["git_commit"] == subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
        text=True, check=True).stdout.strip()
    assert manifest["argv"][0].endswith("run_batch.py") and "--kind" in manifest["argv"]
    assert manifest["env"] == {"FAKE_MARK": "m1", "STS2_SOLVER_THREADS": "2"}
    assert manifest["lanes"] == [
        {"lane": "Silent", "character": "Silent", "planned_games": 3, "seed_prefix": "z_",
         "seed_offset": 0, "extra_args": ["--game-version", "v9"]},
        {"lane": "Defect", "character": "Defect", "planned_games": 3, "seed_prefix": "z_",
         "seed_offset": 0, "extra_args": ["--game-version", "v9"]},
    ]
    assert "dry_run" not in manifest


def test_manifest_exists_before_any_lane_starts(env):
    env.release("all")
    run_batch.main(env.argv())
    assert env.started("Ironclad")["manifest_seen"] is True


def test_dry_run_writes_the_manifest_and_starts_nothing(env, capsys):
    rc = run_batch.main(env.argv("--dry-run", games="4"))
    assert rc == 0
    batch_dir = env.batch_dir()
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    assert manifest["dry_run"] is True
    assert [lane["lane"] for lane in manifest["lanes"]] == ["Ironclad", "Silent"]
    assert not list(env.dump_dir.iterdir())          # no fake lane ever ran
    assert not (batch_dir / "lanes_status.json").exists()
    assert not list(batch_dir.glob("*.log"))
    out = capsys.readouterr().out
    assert out.count(env.prefix) == 2                # one printed command per lane
    assert "--lane Ironclad" in out and "--lane Silent" in out and "4 Silent" in out


def test_an_existing_batch_directory_is_never_reused(env, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(run_batch, "_now", lambda: dt.datetime(2026, 10, 12, 10, 15, 0))
    run_batch.main(env.argv("--dry-run"))
    with pytest.raises(SystemExit) as exc:
        run_batch.main(env.argv("--dry-run"))
    assert exc.value.code not in (0, None)
    assert [p.name for p in env.batches.iterdir()] == ["reg-20261012-101500"]


# --- running lanes: queueing and status transitions ------------------------------------

def _runner(env, *extra, chars="Ironclad,Silent,Defect", max_lanes="2"):
    """A LaneRunner over fake lanes, plus its batch dir."""
    args = run_batch.parse_args(env.argv(*extra, "--max-lanes", max_lanes, chars=chars,
                                         games="1"))
    batch_dir = env.batches / "b-1"
    batch_dir.mkdir(parents=True)
    runner = run_batch.LaneRunner(
        batch_dir, args.chars, args.max_lanes,
        lambda lane: run_batch.build_lane_command(args, "b-1", batch_dir, lane),
        run_batch.build_lane_env(args))
    return runner, batch_dir


def _status(batch_dir):
    return json.loads((batch_dir / "lanes_status.json").read_text())


def test_at_most_max_lanes_run_and_the_rest_queue_until_one_exits(env):
    runner, batch_dir = _runner(env)
    runner.write_status()
    assert {s["state"] for s in _status(batch_dir).values()} == {"queued"}

    runner.step()
    status = _status(batch_dir)
    assert [status[lane]["state"] for lane in ("Ironclad", "Silent", "Defect")] == [
        "running", "running", "queued"]
    _wait_for(lambda: (env.dump_dir / "Silent.start.json").exists(), what="lanes 1+2 start")
    assert not (env.dump_dir / "Defect.start.json").exists()

    env.release("Ironclad")
    _wait_for(lambda: (runner.step(), _status(batch_dir)["Ironclad"]["state"] == "done")[1],
              what="Ironclad to finish")
    _wait_for(lambda: (env.dump_dir / "Defect.start.json").exists(), what="Defect to start")
    status = _status(batch_dir)
    assert status["Ironclad"]["state"] == "done"
    assert status["Silent"]["state"] == "running" and status["Defect"]["state"] == "running"

    env.release("all")
    _wait_for(lambda: not runner.step(), what="all lanes done")
    assert {s["state"] for s in _status(batch_dir).values()} == {"done"}


def test_status_records_pid_times_and_return_code(env):
    runner, batch_dir = _runner(env, chars="Ironclad,Silent", max_lanes="1")
    runner.write_status()
    before = time.time()
    runner.step()
    running = _status(batch_dir)["Ironclad"]
    assert running["state"] == "running" and isinstance(running["pid"], int)
    assert before <= running["started_at"] <= time.time()
    assert running["ended_at"] is None and running["returncode"] is None
    assert _status(batch_dir)["Silent"] == {
        "state": "queued", "pid": None, "started_at": None, "ended_at": None,
        "returncode": None}

    env.release("Ironclad", code=0)
    _wait_for(lambda: (runner.step(), _status(batch_dir)["Ironclad"]["state"] == "done")[1],
              what="Ironclad done")
    done = _status(batch_dir)["Ironclad"]
    assert done["returncode"] == 0 and done["pid"] == running["pid"]
    assert done["ended_at"] >= done["started_at"]


def test_a_non_zero_lane_is_failed_with_its_return_code(env):
    runner, batch_dir = _runner(env, chars="Ironclad,Silent", max_lanes="2")
    runner.write_status()
    env.release("Ironclad", code=3)
    env.release("Silent", code=0)
    _wait_for(lambda: not runner.step(), what="both lanes to exit")
    status = _status(batch_dir)
    assert status["Ironclad"]["state"] == "failed" and status["Ironclad"]["returncode"] == 3
    assert status["Silent"]["state"] == "done" and status["Silent"]["returncode"] == 0
    assert runner.failed_lanes() == ["Ironclad"]


def test_status_file_is_replaced_atomically_not_left_as_a_temp(env):
    runner, batch_dir = _runner(env, chars="Ironclad", max_lanes="1")
    runner.write_status()
    runner.step()
    env.release("all")
    _wait_for(lambda: not runner.step(), what="lane exit")
    assert sorted(p.name for p in batch_dir.iterdir() if p.name.startswith("lanes_status")) \
        == ["lanes_status.json"]


def test_a_lane_that_cannot_be_spawned_fails_without_stopping_the_others(env):
    args = run_batch.parse_args(env.argv(chars="Ironclad,Silent", games="1"))
    batch_dir = env.batches / "b-1"
    batch_dir.mkdir(parents=True)

    def command(lane):
        if lane == "Ironclad":
            return ["/definitely/not/a/real/binary"]
        return run_batch.build_lane_command(args, "b-1", batch_dir, lane)

    runner = run_batch.LaneRunner(batch_dir, ["Ironclad", "Silent"], 2, command,
                                  run_batch.build_lane_env(args))
    runner.write_status()
    env.release("all")
    _wait_for(lambda: not runner.step(), what="lanes to settle")
    status = _status(batch_dir)
    assert status["Ironclad"]["state"] == "failed" and status["Ironclad"]["error"]
    assert status["Silent"]["state"] == "done"


# --- running lanes through main() ---------------------------------------------------------

def test_lanes_log_to_files_in_their_own_session_with_the_env_applied(env, monkeypatch):
    monkeypatch.delenv("STS2_GAME_DIR", raising=False)
    env.release("all")
    rc = run_batch.main(env.argv("--env", "FAKE_MARK=hello", "--threads", "5"))
    assert rc == 0
    batch_dir = env.batch_dir()
    log = (batch_dir / "Ironclad.log").read_text()
    assert "chatter on stdout" in log and "chatter on stderr" in log  # stderr merged
    started = env.started("Ironclad")
    assert started["pgid"] == started["pid"] != os.getpgid(0)         # own session/group
    assert Path(started["cwd"]).resolve() == ROOT
    assert started["env"]["FAKE_MARK"] == "hello"
    assert started["env"]["STS2_SOLVER_THREADS"] == "5"
    assert started["env"]["STS2_GAME_DIR"].endswith("data_sts2_macos_arm64")
    argv = started["argv"]
    assert argv[:2] == ["2", "Ironclad"]
    assert argv[argv.index("--batch-id") + 1] == batch_dir.name
    assert argv[argv.index("--keep-game-logs") + 1] == str(batch_dir / "games")
    assert argv[argv.index("--results-log") + 1] == str(batch_dir / "Ironclad.jsonl")


def test_summary_reports_rows_and_the_completed_line_per_lane(env, capsys):
    env.release("all")
    assert run_batch.main(env.argv(games="3")) == 0
    out = capsys.readouterr().out
    assert "Ironclad" in out and "Silent" in out
    assert out.count("Wins: 0/3, Completed: 3/3") == 2
    assert out.count("rows 3/3") == 2
    assert "INCOMPLETE" not in out


def test_summary_flags_a_lane_that_completed_fewer_games_than_planned(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_ROWS", "1")
    env.release("all")
    assert run_batch.main(env.argv(games="3", chars="Silent")) == 0  # exit codes are 0...
    out = capsys.readouterr().out
    assert "Completed: 1/3" in out and "INCOMPLETE" in out         # ...but it is called out


def test_a_non_zero_lane_makes_the_launcher_exit_non_zero_after_all_lanes_finish(env, capsys):
    env.release("Ironclad", code=2)
    env.release("Silent", code=0)
    rc = run_batch.main(env.argv())
    assert rc != 0
    status = json.loads((env.batch_dir() / "lanes_status.json").read_text())
    assert status["Ironclad"]["state"] == "failed" and status["Ironclad"]["returncode"] == 2
    assert status["Silent"]["state"] == "done"                     # the other lane still ran
    out = capsys.readouterr().out
    assert "FAILED" in out and "Ironclad" in out


def test_all_lanes_clean_exits_zero(env):
    env.release("all")
    assert run_batch.main(env.argv()) == 0


# --- --arm: an A/B as one batch ---------------------------------------------------------------

ROUTE_ARMS = ("--arm", "random:STS2_ROUTE_POLICY=random", "--arm", "value:STS2_ROUTE_POLICY=value")


def _arm_args(*extra, chars="Ironclad,Silent", games="2"):
    return run_batch.parse_args(["--name", "ab", "--games", games, "--chars", chars,
                                 *extra])


def test_arms_make_lanes_of_characters_times_arms_in_interleaved_queue_order():
    args = _arm_args(*ROUTE_ARMS)
    assert [lane["lane"] for lane in args.lanes] == [
        "Ironclad/random", "Ironclad/value", "Silent/random", "Silent/value"]
    assert [lane["character"] for lane in args.lanes] == ["Ironclad"] * 2 + ["Silent"] * 2
    assert [lane["arm"] for lane in args.lanes] == ["random", "value"] * 2
    assert args.lanes[1]["arm_env"] == {"STS2_ROUTE_POLICY": "value"}
    assert args.chars == ["Ironclad", "Silent"]          # still the characters


def test_without_arms_the_lanes_are_the_characters_and_nothing_else_changes():
    args = _arm_args()
    assert args.arms == []
    assert args.lanes == [{"lane": "Ironclad", "character": "Ironclad", "arm": None,
                           "arm_env": {}},
                          {"lane": "Silent", "character": "Silent", "arm": None,
                           "arm_env": {}}]
    assert args.kind == "regression"


def test_kind_defaults_to_ab_with_arms_and_an_explicit_kind_wins():
    assert _arm_args(*ROUTE_ARMS).kind == "ab"
    assert _arm_args(*ROUTE_ARMS, "--kind", "smoke").kind == "smoke"
    assert _arm_args("--kind", "ab").kind == "ab"


def test_an_arm_may_set_several_variables_and_a_value_may_contain_equals_or_be_empty():
    args = _arm_args("--arm", "x:A=1,B=two=2,C=", "--arm", "y.v-2:STS2_OOC_POLICY=naive")
    assert [arm["name"] for arm in args.arms] == ["x", "y.v-2"]
    assert args.arms[0]["env"] == {"A": "1", "B": "two=2", "C": ""}


@pytest.mark.parametrize("bad", [
    "novalue",                       # no colon
    "x",                             # a name alone
    "x:",                            # no KEY=VAL
    "x:NOEQUALS",
    ":A=1",                          # blank name
    "a/b:A=1",                       # '/' would split the lane name
    "a b:A=1",
    "x:A=1,,B=2",                    # empty pair
    "x:=1",                          # empty key
    "x:1BAD=1",                      # not an environment variable name
    "x:A=1,A=2",                     # the same key twice
    "-x:A=1",
    "n" * 33 + ":A=1",
])
def test_malformed_arms_are_rejected_naming_the_flag(bad, capsys):
    with pytest.raises(SystemExit) as exc:
        _arm_args("--arm", bad)
    assert exc.value.code == 2
    assert "--arm" in capsys.readouterr().err


@pytest.mark.parametrize("second", ["a:B=2", "A:B=2"])
def test_duplicate_arm_names_are_rejected_even_when_only_the_case_differs(second, capsys):
    with pytest.raises(SystemExit):
        _arm_args("--arm", "a:A=1", "--arm", second)       # APFS: a.jsonl == A.jsonl
    assert "duplicates" in capsys.readouterr().err


def test_each_lane_env_is_the_base_env_plus_its_arms_and_the_arm_wins(monkeypatch):
    monkeypatch.setenv("STS2_GAME_DIR", "/elsewhere/data")
    monkeypatch.setenv("FROM_PARENT", "yes")
    monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    args = _arm_args(*ROUTE_ARMS, "--arm", "slow:STS2_SOLVER_THREADS=9,FAKE_MARK=arm",
                     "--threads", "4", "--env", "FAKE_MARK=base", "--env", "KEEP=1")
    rand = run_batch.build_lane_env(args, "Ironclad/random")
    value = run_batch.build_lane_env(args, "Silent/value")
    slow = run_batch.build_lane_env(args, "Silent/slow")
    assert rand["STS2_ROUTE_POLICY"] == "random" and value["STS2_ROUTE_POLICY"] == "value"
    assert "STS2_ROUTE_POLICY" not in slow            # an arm's variables stay in its lane
    for lane_env in (rand, value):
        assert lane_env["STS2_SOLVER_THREADS"] == "4" and lane_env["FAKE_MARK"] == "base"
        assert lane_env["KEEP"] == "1" and lane_env["FROM_PARENT"] == "yes"
        assert lane_env["STS2_GAME_DIR"] == "/elsewhere/data"
    assert slow["STS2_SOLVER_THREADS"] == "9" and slow["FAKE_MARK"] == "arm"  # arm > base
    # no lane -> just the base environment, exactly as before --arm existed
    assert "STS2_ROUTE_POLICY" not in run_batch.build_lane_env(args)
    assert run_batch.lane_env_overrides(args) == {
        "STS2_SOLVER_THREADS": "4", "FAKE_MARK": "base", "KEEP": "1"}


def test_arm_lane_commands_share_seeds_and_differ_in_lane_results_and_game_logs(tmp_path):
    args = _arm_args(*ROUTE_ARMS, "--ascension", "1", "--seed-prefix", "p3b_",
                     "--seed-offset", "5", "--", "--game-version", "v0.111.0")
    rand = _lane_args(run_batch.build_lane_command(args, "ab-1", tmp_path, "Ironclad/random"))
    value = _lane_args(run_batch.build_lane_command(args, "ab-1", tmp_path, "Ironclad/value"))
    assert rand == [
        "2", "Ironclad", "--ascension", "1", "--seed-prefix", "p3b_", "--seed-offset", "5",
        "--batch-id", "ab-1", "--lane", "Ironclad/random",
        "--keep-game-logs", str(tmp_path / "games" / "Ironclad__random"),
        "--results-log", str(tmp_path / "Ironclad__random.jsonl"),
        "--game-version", "v0.111.0"]
    # the arms differ only in the lane name and its two paths: same character, same seeds
    differing = [(a, b) for a, b in zip(rand, value) if a != b]
    assert differing == [("Ironclad/random", "Ironclad/value"),
                         (str(tmp_path / "games" / "Ironclad__random"),
                          str(tmp_path / "games" / "Ironclad__value")),
                         (str(tmp_path / "Ironclad__random.jsonl"),
                          str(tmp_path / "Ironclad__value.jsonl"))]


def test_safe_lane_name_keeps_plain_lanes_and_flattens_the_slash():
    assert run_batch.safe_lane_name("Silent") == "Silent"
    assert run_batch.safe_lane_name("Necrobinder/value") == "Necrobinder__value"
    assert run_batch.safe_lane_name("Defect/v1.2-b_3") == "Defect__v1.2-b_3"
    names = {run_batch.safe_lane_name(f"{c}/{a}")
             for c in run_batch.CHARACTERS for a in ("a", "b", "a_b", "_a")}
    assert len(names) == len(run_batch.CHARACTERS) * 4        # no two lanes share a file


def test_manifest_lists_arm_lanes_with_their_arm_and_arm_env(env):
    env.release("all")
    assert run_batch.main(env.argv(*ROUTE_ARMS, "--env", "FAKE_MARK=m", games="3",
                                   name="route-ab", chars="Silent,Defect")) == 0
    manifest = json.loads((env.batch_dir() / "manifest.json").read_text())
    assert manifest["kind"] == "ab"
    assert manifest["env"] == {"FAKE_MARK": "m", "STS2_SOLVER_THREADS": "3"}   # base only
    assert [(lane["lane"], lane["character"], lane["arm"], lane["arm_env"])
            for lane in manifest["lanes"]] == [
        ("Silent/random", "Silent", "random", {"STS2_ROUTE_POLICY": "random"}),
        ("Silent/value", "Silent", "value", {"STS2_ROUTE_POLICY": "value"}),
        ("Defect/random", "Defect", "random", {"STS2_ROUTE_POLICY": "random"}),
        ("Defect/value", "Defect", "value", {"STS2_ROUTE_POLICY": "value"})]
    assert {(lane["planned_games"], lane["seed_prefix"], lane["seed_offset"])
            for lane in manifest["lanes"]} == {(3, "run_", 0)}    # one seed range for all
    assert manifest["argv"].count("--arm") == 2


def test_manifest_without_arms_has_no_arm_keys(env):
    run_batch.main(env.argv("--dry-run"))
    manifest = json.loads((env.batch_dir() / "manifest.json").read_text())
    assert all("arm" not in lane and "arm_env" not in lane for lane in manifest["lanes"])


def test_dry_run_with_arms_prints_each_lane_and_its_arm_env(env, capsys):
    assert run_batch.main(env.argv(*ROUTE_ARMS, "--dry-run")) == 0
    out = capsys.readouterr().out
    assert out.count(env.prefix) == 4
    assert "--lane Ironclad/value" in out and "--lane Silent/random" in out
    assert "Ironclad/value env: STS2_ROUTE_POLICY=value" in out
    assert not list(env.dump_dir.iterdir())                   # nothing started


def _arm_runner(env, max_lanes, chars="Ironclad,Silent,Defect"):
    args = run_batch.parse_args(env.argv(*ROUTE_ARMS, "--max-lanes", str(max_lanes),
                                         chars=chars, games="1"))
    batch_dir = env.batches / "b-1"
    batch_dir.mkdir(parents=True)
    runner = run_batch.LaneRunner(
        batch_dir, [lane["lane"] for lane in args.lanes], args.max_lanes,
        lambda lane: run_batch.build_lane_command(args, "b-1", batch_dir, lane),
        lambda lane: run_batch.build_lane_env(args, lane))
    return runner, batch_dir


def test_queue_order_starts_both_arms_of_a_character_together(env):
    runner, batch_dir = _arm_runner(env, max_lanes=3)
    runner.write_status()
    runner.step()
    status = _status(batch_dir)
    assert [lane for lane, entry in status.items() if entry["state"] == "running"] == [
        "Ironclad/random", "Ironclad/value", "Silent/random"]
    assert [lane for lane, entry in status.items() if entry["state"] == "queued"] == [
        "Silent/value", "Defect/random", "Defect/value"]
    env.release("Ironclad/random")                   # one lane exits: the next in order starts
    _wait_for(lambda: (runner.step(), _status(batch_dir)["Ironclad/random"]["state"]
                       == "done")[1], what="Ironclad/random to finish")
    _wait_for(lambda: (env.dump_dir / "Silent__value.start.json").exists(),
              what="Silent/value to start")
    assert _status(batch_dir)["Defect/random"]["state"] == "queued"
    env.release("all")
    _wait_for(lambda: not runner.step(), what="all lanes done")


def test_an_arm_batch_runs_each_lane_with_its_arms_env_and_per_lane_files(env, capsys):
    env.release("all")
    assert run_batch.main(env.argv(*ROUTE_ARMS, "--max-lanes", "4", games="2")) == 0
    batch_dir = env.batch_dir()
    assert env.started("Ironclad/random")["env"]["STS2_ROUTE_POLICY"] == "random"
    assert env.started("Ironclad/value")["env"]["STS2_ROUTE_POLICY"] == "value"
    assert env.started("Silent/value")["env"]["STS2_ROUTE_POLICY"] == "value"
    value_argv = env.started("Silent/value")["argv"]
    assert value_argv[:2] == ["2", "Silent"]
    assert value_argv[value_argv.index("--lane") + 1] == "Silent/value"
    # per-lane files, safe names, nothing nested by the slash
    assert {p.name for p in batch_dir.glob("*.jsonl")} == {
        "Ironclad__random.jsonl", "Ironclad__value.jsonl", "Silent__random.jsonl",
        "Silent__value.jsonl"}
    assert {p.name for p in batch_dir.glob("*.log")} == {
        "Ironclad__random.log", "Ironclad__value.log", "Silent__random.log",
        "Silent__value.log"}
    assert "chatter on stdout" in (batch_dir / "Silent__value.log").read_text()
    rows = [json.loads(line) for line in (batch_dir / "Silent__value.jsonl").read_text()
            .splitlines()]
    assert [row["lane"] for row in rows] == ["Silent/value"] * 2
    # lanes_status.json is keyed by the lane name the manifest and the game logs carry
    status = json.loads((batch_dir / "lanes_status.json").read_text())
    assert list(status) == ["Ironclad/random", "Ironclad/value", "Silent/random",
                            "Silent/value"]
    assert {entry["state"] for entry in status.values()} == {"done"}
    out = capsys.readouterr().out
    assert out.count("Wins: 0/2, Completed: 2/2") == 4 and out.count("rows 2/2") == 4
    assert "Ironclad/value" in out and "All lanes exited 0" in out


# --- signals --------------------------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_to_the_launcher_terminates_every_lane_group(env, sig, tmp_path):
    cmd = [sys.executable, str(RUN_BATCH_PY),
           *env.argv("--max-lanes", "2", chars="Ironclad,Silent,Defect", games="1")]
    launcher = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, cwd=ROOT, env=dict(os.environ))
    try:
        status_path = _wait_for(
            lambda: next(iter(env.batches.glob("*/lanes_status.json")), None)
            if env.batches.exists() else None, what="lanes_status.json")

        def running():
            try:
                status = json.loads(status_path.read_text())
            except ValueError:
                return None
            lanes = [s for s in status.values() if s["state"] == "running" and s["pid"]]
            return lanes if len(lanes) == 2 else None

        lanes = _wait_for(running, what="two lanes running")
        pids = [lane["pid"] for lane in lanes]
        assert all(_alive(pid) for pid in pids)

        launcher.send_signal(sig)
        assert launcher.wait(timeout=30) == 130
        _wait_for(lambda: not any(_alive(pid) for pid in pids), what="lanes to be gone")
        status = json.loads(status_path.read_text())
        assert {s["state"] for s in status.values()} == {"failed"}   # incl. the queued one
        assert status["Defect"]["pid"] is None and status["Defect"]["started_at"] is None
        assert all(status[lane]["ended_at"] for lane in status)
    finally:
        if launcher.poll() is None:
            launcher.kill()
        launcher.stdout.close()


# play_full_run turns the SIGTERM a stopped lane receives into a clean exit, so
# play_run's `finally` closes the engine -- which lives in its OWN session and
# would otherwise outlive the lane's process group.
STOP_PROBE = r'''
import os, runpy, sys, time
pydir, marker, logdir = sys.argv[1:4]
sys.path.insert(0, pydir)
import engine_process, game_log
game_log.LOG_DIR = logdir


class FakeEngine:
    def __init__(self, argv, **kw):
        pass

    def write(self, obj):
        pass

    def read_json(self, timeout, on_skip=None):
        open(marker + ".waiting", "w").close()
        time.sleep(60)

    def kill(self):
        pass

    def close(self):
        open(marker + ".closed", "w").close()


engine_process.EngineProcess = FakeEngine
sys.argv = ["play_full_run.py", "1", "Ironclad"]
runpy.run_path(os.path.join(pydir, "play_full_run.py"), run_name="__main__")
'''


def test_play_full_run_closes_its_engine_when_stopped_with_sigterm(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(STOP_PROBE)
    marker = tmp_path / "m"
    proc = subprocess.Popen(
        [sys.executable, str(probe), str(PYTHON_DIR), str(marker), str(tmp_path / "logs")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
        env=dict(os.environ, STS2_OOC_POLICY="naive"))
    try:
        _wait_for(lambda: Path(str(marker) + ".waiting").exists(), what="the game to start")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 128 + signal.SIGTERM
        assert Path(str(marker) + ".closed").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()
