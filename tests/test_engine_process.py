"""EngineProcess: per-reply watchdog and process-group kill (agent/bug.md BUG-040)."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
from engine_process import EngineHang, EngineProcess

FAKE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _spawn(tmp_path, hang_on="hang"):
    pidfile = tmp_path / "grandchild.pid"
    engine = EngineProcess([sys.executable, FAKE, "--pidfile", str(pidfile), "--hang-on", hang_on])
    return engine, pidfile


def _grandchild_pid(pidfile):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = pidfile.read_text().strip() if pidfile.exists() else ""
        if text:
            return int(text)
        time.sleep(0.05)
    raise AssertionError("fake engine never wrote its child's pid")


def _gone(pid, within=5.0):
    # A killed process lingers as a zombie until reaped; once its parent is gone
    # too, launchd reaps it -- poll instead of checking once.
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_reads_ready_and_reports_skipped_non_json_lines(tmp_path):
    engine, _ = _spawn(tmp_path)
    skipped = []
    try:
        assert engine.read_json(10, on_skip=skipped.append) == {"type": "ready"}
        assert skipped == ["warming up, not JSON"]
    finally:
        engine.close()


def test_request_round_trip(tmp_path):
    engine, _ = _spawn(tmp_path)
    try:
        engine.read_json(10)
        engine.write({"cmd": "action", "action": "end_turn"})
        assert engine.read_json(10)["echo"]["action"] == "end_turn"
    finally:
        engine.close()


def test_a_hung_reply_raises_engine_hang_instead_of_blocking_forever(tmp_path):
    engine, _ = _spawn(tmp_path)
    try:
        engine.read_json(10)
        engine.write({"cmd": "action", "action": "hang"})
        started = time.monotonic()
        with pytest.raises(EngineHang):
            engine.read_json(0.5)
        assert time.monotonic() - started < 5
    finally:
        engine.kill()


def test_kill_takes_down_the_engine_behind_the_wrapper(tmp_path):
    # In BUG-040 the spinning process was the real engine, a CHILD of
    # `dotnet run`. Killing only the wrapper would orphan it at 100% CPU.
    engine, pidfile = _spawn(tmp_path)
    engine.read_json(10)
    grandchild = _grandchild_pid(pidfile)
    engine.write({"cmd": "action", "action": "hang"})
    with pytest.raises(EngineHang):
        engine.read_json(0.5)
    engine.kill()
    assert _gone(grandchild), "the engine behind the wrapper survived the watchdog kill"


def test_engine_exit_raises_eof_not_engine_hang(tmp_path):
    engine, _ = _spawn(tmp_path)
    engine.read_json(10)
    engine.write({"cmd": "quit"})
    with pytest.raises(RuntimeError, match="EOF"):
        engine.read_json(10)
    engine.close()


def test_close_after_a_normal_run_leaves_nothing_behind(tmp_path):
    engine, pidfile = _spawn(tmp_path)
    engine.read_json(10)
    grandchild = _grandchild_pid(pidfile)
    engine.close()
    assert _gone(grandchild)


def test_close_after_kill_is_harmless(tmp_path):
    engine, _ = _spawn(tmp_path)
    engine.read_json(10)
    engine.kill()
    engine.close()   # must not raise
