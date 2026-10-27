# tests/agent/test_advisor_server.py
"""agent/advisor_server.py: the in-game mod's local advice service."""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent import advisor_server
from agent.combat_env import greedy_action
from agent.greedy_trace import (GREEDY_BEHAVIOR_DEFAULTS, GREEDY_BEHAVIOR_ENV,
                                MC_CONTEXT_START, reset_greedy_context)
from tests.agent.advisor_corpus import (corpus_games, fresh_greedy_state,
                                        require_same_greedy_data)


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def test_advise_action_is_greedy_action_on_the_whole_corpus():
    require_same_greedy_data()
    for _game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            reply = advisor_server.advise(row["state"], greedy_action, [])
            assert reply["action"] == row["expected"], (row["game"], row["seq"])
            assert reply["policy"] == "greedy"
            assert reply["unverified"] == (row["state"]["decision"] == "map_select")
            assert len([r for r in reply["rules"] if r["outcome"] == "fired"]) == 1
            # the mod parses replies with a strict JSON parser: no NaN/Infinity
            json.dumps(reply, allow_nan=False)


def test_advise_refuses_decisions_it_does_not_cover():
    reply = advisor_server.advise({"decision": "combat_play"}, greedy_action, [])
    assert reply["action"] is None
    assert reply["warnings"] == ["not_advised: combat_play"]


def test_advise_reports_greedy_failures_without_a_fallback():
    def boom(state, trace=None):
        raise ZeroDivisionError("x")
    reply = advisor_server.advise({"decision": "rest_site"}, boom, ["STS2_RANDOMIZE=1"])
    assert reply["action"] is None and reply["options"] == [] and reply["rules"] == []
    assert reply["warnings"] == ["STS2_RANDOMIZE=1", "greedy_raised: ZeroDivisionError"]
    reply = advisor_server.advise({"decision": "rest_site"}, lambda s, trace=None: None, [])
    assert reply["action"] is None and reply["warnings"] == ["greedy_none"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_advise_gives_no_advice_when_the_trace_is_not_strict_json(bad):
    def greedy_with_bad_score(state, trace=None):
        trace.option(0, kind="rest", score=bad)
        trace.rule("rest.no_choice", "fired")
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}
    reply = advisor_server.advise({"decision": "rest_site"}, greedy_with_bad_score, [])
    assert reply["action"] is None
    assert reply["options"] == [] and reply["rules"] == []
    assert "trace_not_serializable" in reply["warnings"]
    json.dumps(reply, allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_advise_gives_no_advice_when_the_action_is_not_strict_json(bad):
    def greedy_with_bad_action(state, trace=None):
        trace.rule("rest.no_choice", "fired")
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": bad}}
    reply = advisor_server.advise({"decision": "rest_site"}, greedy_with_bad_action, [])
    assert reply["action"] is None
    assert reply["options"] == [] and reply["rules"] == []
    assert "trace_not_serializable" in reply["warnings"]
    json.dumps(reply, allow_nan=False)


def test_knob_warnings_lists_set_behaviour_env():
    env = {"STS2_MC_ROLLOUT": "1", "STS2_RANDOMIZE": " ", "OTHER": "1"}
    assert advisor_server.knob_warnings(env) == ["STS2_MC_ROLLOUT=1"]
    # order follows GREEDY_BEHAVIOR_ENV, whatever order the env was built in;
    # "x" is a non-default value of every variable
    env = {key: " x " for key in reversed(GREEDY_BEHAVIOR_ENV)}
    assert advisor_server.knob_warnings(env) == [f"{key}=x" for key in GREEDY_BEHAVIOR_ENV]


def test_knob_warnings_ignores_values_equal_to_the_default():
    defaults = dict(GREEDY_BEHAVIOR_DEFAULTS)
    assert set(defaults) == set(GREEDY_BEHAVIOR_ENV)
    # STS2_CARD_QUALITY_GATE=1 is the shipped behaviour (the one that used to warn)
    assert defaults["STS2_CARD_QUALITY_GATE"] == "1"
    assert advisor_server.knob_warnings(defaults) == []
    assert advisor_server.knob_warnings({}) == []
    # case, whitespace and a numerically equal spelling are still the default
    assert advisor_server.knob_warnings({"STS2_OVERRIDE_DELTA_CAP": " 2 ",
                                         "STS2_PREDICTOR_TARGET": "FLOOR",
                                         "STS2_MC_SMART_THRESH": "0.150",
                                         "STS2_CARD_THRESHOLD_LIFT": "0.0"}) == []
    # one non-default among defaults is the only warning
    assert advisor_server.knob_warnings(dict(defaults, STS2_CARD_QUALITY_GATE="0")) == [
        "STS2_CARD_QUALITY_GATE=0"]
    assert advisor_server.knob_warnings(dict(defaults, STS2_OVERRIDE_DELTA_CAP="3",
                                             STS2_MC_ROLLOUT="smart")) == [
        "STS2_MC_ROLLOUT=smart", "STS2_OVERRIDE_DELTA_CAP=3"]
    # blank equals only a blank default: a blank STS2_CARD_QUALITY_GATE turns the gate off
    assert advisor_server.knob_warnings({"STS2_CARD_QUALITY_GATE": ""}) == ["STS2_CARD_QUALITY_GATE="]
    # the server reports the same list, from the env it is given
    server = advisor_server.make_server(0, env=dict(defaults, STS2_RANDOMIZE="card_reward"),
                                        warm=False)
    try:
        assert server.warnings == ["STS2_RANDOMIZE=card_reward"]
    finally:
        server.server_close()


def test_http_round_trip():
    server = advisor_server.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/health", timeout=10) as r:
            health = json.load(r)
        assert health["ok"] is True and health["policy"] == "greedy"
        rest = next(row["state"] for _g, rows in corpus_games() for row in rows
                    if row["state"]["decision"] == "rest_site")
        req = urllib.request.Request(base + "/advise", data=json.dumps(rest).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            reply = json.load(r)
        assert reply["action"] == greedy_action(rest)
        bad = urllib.request.Request(base + "/advise", data=b"{not json")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad, timeout=10)
        assert err.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_server_binds_loopback_only():
    server = advisor_server.make_server(0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


@pytest.fixture
def live_server():
    """make_server(0) serving in a thread; yields (server, port)."""
    server = advisor_server.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _post_with_content_length(port, value, body=b""):
    """POST /advise with a hand-written Content-Length header; (status, json)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("POST", "/advise")
        if value is not None:
            conn.putheader("Content-Length", value)
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["abc", "-1", "1.5", "", "1_0", " "])
def test_bad_content_length_is_a_400(live_server, value):
    _server, port = live_server
    status, body = _post_with_content_length(port, value)
    assert status == 400 and "Content-Length" in body["error"]


def test_missing_content_length_is_a_400(live_server):
    _server, port = live_server
    status, _body = _post_with_content_length(port, None)
    assert status == 400


def test_oversized_content_length_is_a_413_without_reading_the_body(live_server):
    _server, port = live_server
    status, body = _post_with_content_length(port, str(advisor_server.MAX_BODY + 1))
    assert status == 413 and "error" in body
    # a body of the declared (small) length still reaches the JSON checks
    status, body = _post_with_content_length(port, "2", b"[]")
    assert status == 400 and body["error"] == "body must be a decision object"


def test_short_body_is_a_400(live_server):
    _server, port = live_server
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"POST /advise HTTP/1.0\r\nContent-Length: 100\r\n\r\n{}")
        sock.shutdown(socket.SHUT_WR)  # EOF after 2 of the 100 promised bytes
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
    assert reply.startswith(b"HTTP/1.0 400") or reply.startswith(b"HTTP/1.1 400")
    assert b"shorter than Content-Length" in reply


def test_idle_client_does_not_stop_health_from_answering(live_server):
    _server, port = live_server
    with socket.create_connection(("127.0.0.1", port), timeout=10):  # connects, sends nothing
        started = time.monotonic()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
            assert json.load(r)["ok"] is True
        assert time.monotonic() - started < 6.5


def test_unexpected_error_is_a_500_with_the_exception_type(live_server, monkeypatch, capsys):
    def explode(state, greedy, warnings):
        raise RuntimeError("boom")
    monkeypatch.setattr(advisor_server, "advise", explode)
    _server, port = live_server
    req = urllib.request.Request(f"http://127.0.0.1:{port}/advise", data=b"{}")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 500
    assert json.load(err.value) == {"error": "RuntimeError"}
    assert "RuntimeError: boom" in capsys.readouterr().err


def test_only_failed_requests_are_logged(live_server, capsys):
    _server, port = live_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10):
        pass
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=10)
    err = capsys.readouterr().err
    assert " 404 " in err and "/health" not in err and " 200 " not in err


def test_handle_error_logs_one_line_for_a_vanished_client(capsys):
    server = advisor_server.make_server(0)
    try:
        try:
            raise BrokenPipeError()
        except BrokenPipeError:
            server.handle_error(None, ("127.0.0.1", 1234))
    finally:
        server.server_close()
    err = capsys.readouterr().err
    assert err.strip().count("\n") == 0 and "BrokenPipeError" in err
    assert "Traceback" not in err


def test_warm_up_runs_one_card_reward_and_leaves_no_hidden_state():
    from agent import card_scoring
    seen = []

    def spy(state, trace=None):
        action = greedy_action(state, trace=trace)
        seen.append((state["decision"], action["action"],
                     dict(card_scoring._MC_CONTEXT) != MC_CONTEXT_START))
        return action

    card_scoring.set_mc_context(hp=1, max_hp=2, floor=40)  # junk from "an earlier request"
    server = advisor_server.make_server(0, greedy=spy)
    try:
        # the built-in state is a valid card_reward that really picks a card, and
        # it did change greedy's hidden context while it ran ...
        assert seen == [("card_reward", "select_card_reward", True)]
        # ... but the warm-up put it back to the fresh-process value
        assert card_scoring._MC_CONTEXT == MC_CONTEXT_START
        assert server.warmup_s is not None and server.warmup_s >= 0
    finally:
        server.server_close()


def test_warm_false_skips_the_warm_up():
    calls = []
    server = advisor_server.make_server(
        0, greedy=lambda state, trace=None: calls.append(state) or None, warm=False)
    try:
        assert calls == [] and server.warmup_s is None
    finally:
        server.server_close()


def test_a_failing_warm_up_still_yields_a_working_server(capsys):
    calls = []

    def fails_once(state, trace=None):
        calls.append(state["decision"])
        if len(calls) == 1:
            raise RuntimeError("cold start broke")
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}

    server = advisor_server.make_server(0, greedy=fails_once)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        err = capsys.readouterr().err
        assert err.count("warm-up failed") == 1 and "RuntimeError: cold start broke" in err
        assert len(err.strip().splitlines()) == 1
        base = f"http://127.0.0.1:{server.server_address[1]}"
        req = urllib.request.Request(base + "/advise",
                                     data=json.dumps({"decision": "rest_site"}).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            reply = json.load(r)
        assert reply["action"]["args"] == {"option_index": 0}
        assert calls == ["card_reward", "rest_site"]
    finally:
        server.shutdown()
        server.server_close()


_COLD_PROCESS_PROBE = """
import json, sys, time
sys.path.insert(0, ".")
from agent import advisor_server
from tests.agent.advisor_corpus import corpus_rows
state = next(r["state"] for r in corpus_rows() if r["state"]["decision"] == "card_reward")
server = advisor_server.make_server(0)
started = time.perf_counter()
reply = advisor_server.advise(state, server.greedy, [])
elapsed = time.perf_counter() - started
server.server_close()
print(json.dumps({"elapsed": elapsed, "warmup_s": server.warmup_s,
                  "picked": reply["action"] is not None}))
"""


def test_first_card_reward_after_make_server_is_warm():
    """In a FRESH process (this one is already warm from other tests): the first
    real card_reward after make_server must not pay for the lazy loads."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", _COLD_PROCESS_PROBE], cwd=root,
                         capture_output=True, text=True, timeout=300, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["picked"] is True
    # Measured in a fresh process at load ~9 on 18 cores (5 runs each): the first
    # request after the warm-up took 0.003-0.005 s; the same request WITHOUT the
    # warm-up (make_server(warm=False)) took 1.1-1.6 s, and the warm-up itself
    # 1.1-2.6 s. The warm-up is the cold cost paid up front, in the same process
    # under the same load, so the bound follows it: a warmed-up request is a few
    # ms (~50x under the 0.25 s floor), a cold one costs about warmup_s -- and
    # both scale together when the machine is busy, which a fixed bound cannot.
    assert result["warmup_s"] is not None and result["warmup_s"] > 0.2, result
    assert result["elapsed"] < max(0.25, result["warmup_s"] / 4), result


# --- --exit-on-stdin-eof: no orphan service after a game crash -----------------

def test_watch_stdin_eof_exits_only_at_eof():
    read_fd, write_fd = os.pipe()
    exited = threading.Event()
    codes = []
    with os.fdopen(read_fd, "rb") as reader:
        thread = advisor_server.watch_stdin_eof(reader, exit_fn=lambda code: (codes.append(code), exited.set()))
        os.write(write_fd, b"not a request, and not valid utf-8: \xff\xfe")
        assert not exited.wait(0.3), "data on stdin must not stop the service"
        os.close(write_fd)
        assert exited.wait(5), "EOF on stdin must stop the service"
        thread.join(5)
    assert codes == [0]


def test_watch_stdin_eof_treats_an_unreadable_stdin_as_eof():
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")
    reader.close()
    os.close(write_fd)
    exited = threading.Event()
    advisor_server.watch_stdin_eof(reader, exit_fn=lambda code: exited.set())
    assert exited.wait(5)


def test_main_watches_stdin_only_with_the_flag(monkeypatch):
    class Stop(Exception):
        pass

    watched = []
    monkeypatch.setattr(advisor_server, "watch_stdin_eof", lambda *a, **k: watched.append(1))
    monkeypatch.setattr(advisor_server, "make_server", lambda port: (_ for _ in ()).throw(Stop()))
    with pytest.raises(Stop):
        advisor_server.main(["--port", "0"])
    assert watched == []
    with pytest.raises(Stop):
        advisor_server.main(["--port", "0", "--exit-on-stdin-eof"])
    assert watched == [1]


def test_service_process_exits_when_its_stdin_closes(tmp_path):
    """The real thing: the service the mod starts must not outlive the game."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, "agent", "advisor_server.py"), "--port", str(port),
         "--exit-on-stdin-eof"],
        cwd=root, stdin=subprocess.PIPE, stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 180  # warm-up is 2-19 s idle, longer on a busy machine
        while True:
            assert proc.poll() is None, (tmp_path / "server.log").read_text()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    assert json.load(r)["ok"] is True
                break
            except (urllib.error.URLError, OSError):
                assert time.monotonic() < deadline, "the service never came up"
                time.sleep(0.5)
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()
