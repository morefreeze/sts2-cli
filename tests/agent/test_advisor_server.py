# tests/agent/test_advisor_server.py
"""agent/advisor_server.py: the in-game mod's local advice service."""
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent import advisor_server
from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, reset_greedy_context
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
    env = {"STS2_MC_ROLLOUT": "1", "STS2_CARD_QUALITY_GATE": " ", "OTHER": "1"}
    assert advisor_server.knob_warnings(env) == ["STS2_MC_ROLLOUT=1"]
    # order follows GREEDY_BEHAVIOR_ENV, whatever order the env was built in
    env = {key: "1" for key in reversed(GREEDY_BEHAVIOR_ENV)}
    assert advisor_server.knob_warnings(env) == [f"{key}=1" for key in GREEDY_BEHAVIOR_ENV]


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
