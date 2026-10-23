# tests/test_replay_states.py
"""tools/replay_states.py: replay a game log's commands, compare reply dumps."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import replay_states

ECHO_ENGINE = r'''
import json, sys
print("warming up")
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "quit":
        break
    if cmd.get("cmd") == "hang":
        continue  # never replies; the replay's watchdog must give up on it
    print(json.dumps({"type": "decision", "echo": cmd}), flush=True)
'''

# First reply is not "ready", then it waits politely for stdin to close / quit.
NOT_READY_ENGINE = r'''
import json, sys
print(json.dumps({"type": "error", "message": "boom"}), flush=True)
for line in sys.stdin:
    if json.loads(line).get("cmd") == "quit":
        break
'''


def _log(tmp_path, commands):
    path = tmp_path / "game.jsonl"
    rows = [{"type": "run_meta", "run_id": "x"}]
    for i, cmd in enumerate(commands):
        rows.append({"step": i, "type": "action", "data": cmd})
        rows.append({"step": i, "type": "state", "data": {"type": "decision"}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def _engine_file(tmp_path, source, name="engine.py"):
    engine = tmp_path / name
    engine.write_text(source)
    return [sys.executable, str(engine)]


@pytest.fixture
def spawned(monkeypatch):
    """Record every EngineProcess replay() creates so a test can inspect it afterwards."""
    made = []
    real = replay_states.EngineProcess

    class Recording(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(replay_states, "EngineProcess", Recording)
    return made


def test_recorded_commands_skip_plans_and_quit(tmp_path):
    log = _log(tmp_path, [
        {"cmd": "start_run", "character": "Ironclad", "seed": "s"},
        {"cmd": "action", "action": "plan_combat_turn"},
        {"cmd": "action", "action": "end_turn"},
        {"cmd": "quit"},
    ])
    assert list(replay_states.recorded_commands(str(log))) == [
        {"cmd": "start_run", "character": "Ironclad", "seed": "s"},
        {"cmd": "action", "action": "end_turn"},
    ]


def test_replay_writes_one_reply_per_command(tmp_path):
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}, {"cmd": "action", "action": "plan_combat_turn"},
                          {"cmd": "action", "action": "end_turn"}])
    out = tmp_path / "out.jsonl"
    n = replay_states.replay(str(log), str(out), argv=argv)
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert n == 2
    assert [l["reply"]["echo"] for l in lines] == [{"cmd": "start_run"},
                                                    {"cmd": "action", "action": "end_turn"}]


def test_replay_writes_utf8_unescaped(tmp_path):
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "action", "action": "note", "text": "打击"}])
    out = tmp_path / "out.jsonl"
    replay_states.replay(str(log), str(out), argv=argv)
    raw = out.read_bytes()
    assert "打击".encode("utf-8") in raw
    assert json.loads(raw.decode("utf-8").splitlines()[0])["reply"]["echo"]["text"] == "打击"


def test_replay_records_hang_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_states, "REPLY_TIMEOUT_S", 0.5)
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}, {"cmd": "hang"}, {"cmd": "action", "action": "end_turn"}])
    out = tmp_path / "out.jsonl"
    n = replay_states.replay(str(log), str(out), argv=argv)
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert n == 1
    assert lines[1] == {"n": 1, "cmd": {"cmd": "hang"}, "hang": True}
    assert len(lines) == 2  # nothing after the hang


def test_replay_cleans_up_when_first_reply_is_not_ready(tmp_path, spawned):
    argv = _engine_file(tmp_path, NOT_READY_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}])
    with pytest.raises(RuntimeError, match="unexpected first reply"):
        replay_states.replay(str(log), str(tmp_path / "out.jsonl"), argv=argv)
    assert len(spawned) == 1
    assert spawned[0].proc.poll() is not None


def test_replay_cleans_up_when_log_is_unreadable(tmp_path, spawned):
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    with pytest.raises(FileNotFoundError):
        replay_states.replay(str(tmp_path / "missing.jsonl"), str(tmp_path / "out.jsonl"), argv=argv)
    assert len(spawned) == 1
    assert spawned[0].proc.poll() is not None


def test_replay_cleans_up_on_keyboard_interrupt(tmp_path, spawned, monkeypatch):
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}, {"cmd": "action", "action": "end_turn"}])
    real_read = replay_states.EngineProcess.read_json
    calls = []

    def interrupt_on_second(self, timeout, on_skip=None):
        calls.append(1)
        if len(calls) == 2:  # the first reply (ready) passes; the first command's reply is interrupted
            raise KeyboardInterrupt
        return real_read(self, timeout, on_skip)

    monkeypatch.setattr(replay_states.EngineProcess, "read_json", interrupt_on_second)
    with pytest.raises(KeyboardInterrupt):
        replay_states.replay(str(log), str(tmp_path / "out.jsonl"), argv=argv)
    assert spawned[0].proc.poll() is not None


def test_replay_kills_engine_when_close_fails(tmp_path, spawned, monkeypatch):
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}])

    def broken_close(self):
        raise OSError("close blew up")

    monkeypatch.setattr(replay_states.EngineProcess, "close", broken_close)
    replay_states.replay(str(log), str(tmp_path / "out.jsonl"), argv=argv)  # close failure must not escape
    assert spawned[0].proc.poll() is not None  # kill() reaped it


def test_compare_reports_first_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g1.jsonl").write_text('{"n": 0}\n{"n": 1}\n')
    (b / "g1.jsonl").write_text('{"n": 0}\n{"n": 2}\n')
    (a / "g2.jsonl").write_text('{"n": 0}\n')
    (b / "g2.jsonl").write_text('{"n": 0}\n')
    (a / "g3.jsonl").write_text('{"n": 0}\n')
    assert replay_states.compare(str(a), str(b)) == [
        "g1.jsonl: first difference at reply 1",
        "g3.jsonl: only in one directory",
    ]
    assert replay_states.compare(str(a), str(a)) == []


def test_compare_reports_line_count_mismatch_when_one_is_a_prefix(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g.jsonl").write_text('{"n": 0}\n{"n": 1}\n')
    (b / "g.jsonl").write_text('{"n": 0}\n')
    assert replay_states.compare(str(a), str(b)) == ["g.jsonl: 2 vs 1 replies"]


def test_compare_reads_utf8_regardless_of_locale(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (a / "g.jsonl").write_bytes('{"name": "打击"}\n'.encode("utf-8"))
    # An ASCII default encoding would raise UnicodeDecodeError unless open() says utf-8.
    real_open = open

    def ascii_default_open(path, mode="r", *args, **kwargs):
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "ascii"
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", ascii_default_open)
    assert replay_states.compare(str(a), str(a)) == []


def test_main_compare_returns_zero_and_prints_identical(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g.jsonl").write_text('{"n": 0}\n')
    (b / "g.jsonl").write_text('{"n": 0}\n')
    assert replay_states.main(["--compare", str(a), str(b)]) == 0
    assert capsys.readouterr().out.strip() == "identical"


def test_main_compare_returns_one_and_prints_differences(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g.jsonl").write_text('{"n": 0}\n')
    (b / "g.jsonl").write_text('{"n": 1}\n')
    assert replay_states.main(["--compare", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "identical" not in out
    assert "g.jsonl: first difference at reply 0" in out
