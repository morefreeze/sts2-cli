# tests/agent/test_advisor_server.py
"""agent/advisor_server.py: the in-game mod's local advice service."""
import json
import threading
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
