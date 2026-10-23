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
    # Only the per-reply watchdog is shortened; the ready wait keeps its own long timeout, so a
    # loaded machine that is slow to start the fake engine cannot flake this test.
    monkeypatch.setattr(replay_states, "REPLY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(replay_states, "READY_TIMEOUT_S", 60)
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
    assert capsys.readouterr().out.strip() == "identical (1 files, 1 replies)"


def test_main_compare_returns_one_and_prints_differences(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g.jsonl").write_text('{"n": 0}\n')
    (b / "g.jsonl").write_text('{"n": 1}\n')
    assert replay_states.main(["--compare", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "identical" not in out
    assert "g.jsonl: first difference at reply 0" in out


# ---- follow-ups: hangs fail the compare, exact line split, richer diffs, counts, basenames ----

def _row(n, cmd, decision=None, **extra):
    row = {"n": n, "cmd": cmd}
    if decision is not None:
        row["reply"] = {"type": "decision", "decision": decision}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def _dump(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(r + "\n" for r in rows).encode("utf-8"))


def test_compare_flags_a_hang_even_when_both_builds_hang_at_the_same_reply(tmp_path):
    rows = [_row(0, {"cmd": "start_run"}, "event_choice"),
            _row(1, {"cmd": "action", "action": "end_turn"}, "combat_play"),
            _row(2, {"cmd": "action", "action": "choose_option"}, hang=True)]
    _dump(tmp_path / "a" / "g.jsonl", rows)
    _dump(tmp_path / "b" / "g.jsonl", rows)  # byte-identical, hang row included
    assert replay_states.compare(str(tmp_path / "a"), str(tmp_path / "b")) == [
        "g.jsonl: engine hang at reply 2 (golden truncated)"]
    assert replay_states.main(["--compare", str(tmp_path / "a"), str(tmp_path / "b")]) == 1


def test_compare_flags_a_hang_that_only_one_side_has(tmp_path):
    ok = [_row(0, {"cmd": "start_run"}, "event_choice"),
          _row(1, {"cmd": "action", "action": "end_turn"}, "combat_play")]
    hung = ok[:1] + [_row(1, {"cmd": "action", "action": "end_turn"}, hang=True)]
    _dump(tmp_path / "a" / "g.jsonl", ok)
    _dump(tmp_path / "b" / "g.jsonl", hung)
    for first, second in (("a", "b"), ("b", "a")):  # either side may be the truncated one
        diffs = replay_states.compare(str(tmp_path / first), str(tmp_path / second))
        assert "g.jsonl: engine hang at reply 1 (golden truncated)" in diffs
        assert any(d.startswith("g.jsonl: first difference at reply 1") for d in diffs)


def test_compare_ignores_hang_text_that_is_not_a_hang_row(tmp_path):
    rows = [json.dumps({"n": 0, "cmd": {"cmd": "start_run"},
                        "reply": {"type": "decision", "note": '"hang": true'}})]
    _dump(tmp_path / "a" / "g.jsonl", rows)
    assert replay_states.compare(str(tmp_path / "a"), str(tmp_path / "a")) == []


def test_replay_hang_dump_fails_compare_against_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_states, "REPLY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(replay_states, "READY_TIMEOUT_S", 60)
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}, {"cmd": "hang"}])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    replay_states.replay(str(log), str(out_dir / "game.jsonl"), argv=argv)
    assert replay_states.compare(str(out_dir), str(out_dir)) == [
        "game.jsonl: engine hang at reply 1 (golden truncated)"]


def test_hang_row_is_written_unescaped_like_reply_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_states, "REPLY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(replay_states, "READY_TIMEOUT_S", 60)
    argv = _engine_file(tmp_path, ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "hang", "note": "打击"}])
    out = tmp_path / "out.jsonl"
    replay_states.replay(str(log), str(out), argv=argv)
    assert "打击".encode("utf-8") in out.read_bytes()


def test_compare_splits_lines_on_newline_only(tmp_path):
    # U+2028 / U+0085 are legal raw characters inside a JSON string (ensure_ascii=False); str.splitlines()
    # would cut a reply in two and shift every later reply index.
    a, b = tmp_path / "a", tmp_path / "b"
    first = json.dumps({"n": 0, "reply": {"text": "x\u2028y\u0085z"}}, ensure_ascii=False)
    _dump(a / "g.jsonl", [first, json.dumps({"n": 1, "v": 1})])
    _dump(b / "g.jsonl", [first, json.dumps({"n": 1, "v": 2})])
    assert replay_states.compare(str(a), str(a)) == []
    assert replay_states.compare_with_counts(str(a), str(a)) == ([], 1, 2)
    assert replay_states.compare(str(a), str(b)) == ["g.jsonl: first difference at reply 1"]


def test_compare_difference_names_command_and_decision_of_both_sides(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    cmd = {"cmd": "action", "action": "choose_option"}
    _dump(a / "g.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice"), _row(1, cmd, "event_choice")])
    _dump(b / "g.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice"), _row(1, cmd, "card_reward")])
    assert replay_states.compare(str(a), str(b)) == [
        "g.jsonl: first difference at reply 1 (cmd action/choose_option; decision event_choice vs card_reward)"]


def test_compare_difference_for_a_command_without_action_and_a_non_decision_reply(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _dump(a / "g.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice")])
    _dump(b / "g.jsonl", [json.dumps({"n": 0, "cmd": {"cmd": "start_run"},
                                      "reply": {"type": "error", "message": "boom"}})])
    assert replay_states.compare(str(a), str(b)) == [
        "g.jsonl: first difference at reply 0 (cmd start_run; decision event_choice vs error)"]


def test_compare_difference_survives_a_line_that_is_not_json(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _dump(a / "g.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice")])
    _dump(b / "g.jsonl", ['{"n": 0, "cmd": {"cmd": "start_run"}, "reply": {"trunc'])
    assert replay_states.compare(str(a), str(b)) == [
        "g.jsonl: first difference at reply 0 (cmd start_run vs ?; decision event_choice vs ?)"]


def test_compare_with_counts_counts_files_and_replies(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        _dump(d / "g1.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice"),
                               _row(1, {"cmd": "action", "action": "end_turn"}, "combat_play")])
        _dump(d / "g2.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice")])
    assert replay_states.compare_with_counts(str(a), str(b)) == ([], 2, 3)
    assert replay_states.compare(str(a), str(b)) == []


def test_main_compare_prints_file_and_reply_counts_when_identical(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        _dump(d / "g1.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice"),
                               _row(1, {"cmd": "action", "action": "end_turn"}, "combat_play")])
        _dump(d / "g2.jsonl", [_row(0, {"cmd": "start_run"}, "event_choice")])
    assert replay_states.main(["--compare", str(a), str(b)]) == 0
    assert capsys.readouterr().out.strip() == "identical (2 files, 3 replies)"


def test_main_refuses_two_logs_with_the_same_basename(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(replay_states, "replay", lambda *a, **kw: calls.append(a) or 0)
    d1, d2 = tmp_path / "x", tmp_path / "y"
    d1.mkdir(); d2.mkdir()
    (d1 / "game.jsonl").write_text("")
    (d2 / "game.jsonl").write_text("")
    out_dir = tmp_path / "out"
    rc = replay_states.main([str(d1 / "game.jsonl"), str(d2 / "game.jsonl"), "--out", str(out_dir)])
    assert rc == 2
    assert "game.jsonl" in capsys.readouterr().err
    assert calls == []  # refused before any replay
    assert not out_dir.exists()


def test_main_replays_logs_with_distinct_basenames(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(replay_states, "replay", lambda log, out: calls.append((log, out)) or 7)
    logs = [str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl")]
    out_dir = tmp_path / "out"
    assert replay_states.main(logs + ["--out", str(out_dir)]) == 0
    assert calls == [(logs[0], str(out_dir / "a.jsonl")), (logs[1], str(out_dir / "b.jsonl"))]
    assert capsys.readouterr().out.splitlines() == ["a.jsonl: 7 replies", "b.jsonl: 7 replies"]
