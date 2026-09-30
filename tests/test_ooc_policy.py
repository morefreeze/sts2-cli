"""Phase 3a: STS2_OOC_POLICY -- out-of-combat decisions via agent.combat_env.greedy_action.

No game DLLs: play_full_run.EngineProcess is replaced by the scripted
_FakeEngine from test_map_route_determinism, and the greedy policy itself is
replaced through the play_full_run._load_greedy_action seam, so these tests pin
the harness's dispatch/fallback logic, not card_scoring's judgement.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine


def test_ooc_policy_defaults_to_naive():
    assert play_full_run.ooc_policy({}) == "naive"


def test_ooc_policy_blank_means_default():
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": "  "}) == "naive"


@pytest.mark.parametrize("raw,expected", [("greedy", "greedy"), ("NAIVE", "naive"), (" Greedy ", "greedy")])
def test_ooc_policy_accepts_known_values_case_insensitively(raw, expected):
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": raw}) == expected


def test_ooc_policy_rejects_unknown_values_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="STS2_OOC_POLICY"):
        play_full_run.ooc_policy({"STS2_OOC_POLICY": "smart"})


def test_greedy_decisions_exclude_map_and_combat():
    assert "map_select" not in play_full_run.OOC_GREEDY_DECISIONS
    assert "combat_play" not in play_full_run.OOC_GREEDY_DECISIONS
    assert play_full_run.OOC_GREEDY_DECISIONS == {
        "card_reward", "rest_site", "event_choice", "bundle_select", "card_select", "shop"}


@pytest.mark.parametrize("state,expected", [
    ({"decision": "card_reward"}, {"cmd": "action", "action": "skip_card_reward"}),
    ({"decision": "bundle_select"}, {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}),
    ({"decision": "card_select", "cards": [{"index": 0}]},
     {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}),
    ({"decision": "card_select", "cards": []}, {"cmd": "action", "action": "skip_select"}),
    ({"decision": "rest_site"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "event_choice"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "shop"}, {"cmd": "action", "action": "leave_room"}),
])
def test_escape_command_per_decision(state, expected):
    assert play_full_run.ooc_escape_command(state) == expected


def _cmd(action, **args):
    c = {"cmd": "action", "action": action}
    if args:
        c["args"] = args
    return c


# (state, the FIRST command the original naive branch in _play_run sends for it)
NAIVE_CASES = [
    ({"decision": "card_reward", "cards": [{"index": 0}, {"index": 1}]},
     _cmd("select_card_reward", card_index=0)),
    ({"decision": "card_reward", "cards": []}, _cmd("skip_card_reward")),
    ({"decision": "card_reward"}, _cmd("skip_card_reward")),
    ({"decision": "rest_site", "options": [
        {"index": 0, "option_id": "SMITH", "is_enabled": True},
        {"index": 1, "option_id": "HEAL", "is_enabled": True}]}, _cmd("choose_option", option_index=1)),
    ({"decision": "rest_site", "options": [
        {"index": 0, "option_id": "SMITH", "is_enabled": True},
        {"index": 1, "option_id": "HEAL", "is_enabled": False}]}, _cmd("choose_option", option_index=0)),
    ({"decision": "rest_site", "options": [
        {"index": 0, "option_id": "SMITH", "is_enabled": False}]}, _cmd("leave_room")),
    ({"decision": "rest_site", "options": [{"index": 4, "option_id": "SMITH"}]},
     _cmd("choose_option", option_index=4)),
    ({"decision": "rest_site", "options": []}, _cmd("leave_room")),
    ({"decision": "event_choice", "options": [
        {"index": 0, "is_locked": True}, {"index": 1, "is_locked": False}]},
     _cmd("choose_option", option_index=1)),
    ({"decision": "event_choice", "options": [
        {"index": 3, "is_locked": True}, {"index": 5, "is_locked": True}]},
     _cmd("choose_option", option_index=3)),
    ({"decision": "event_choice", "options": []}, _cmd("leave_room")),
    ({"decision": "bundle_select", "bundles": [{}, {}]}, _cmd("select_bundle", bundle_index=0)),
    ({"decision": "card_select", "cards": [{"index": 0}, {"index": 1}]}, _cmd("select_cards", indices="0")),
    ({"decision": "card_select", "cards": []}, _cmd("skip_select")),
    ({"decision": "shop"}, _cmd("leave_room")),
]


@pytest.mark.parametrize("state,expected", NAIVE_CASES)
def test_naive_ooc_command_per_decision(state, expected):
    assert play_full_run.naive_ooc_command(state) == expected


def test_load_greedy_action_returns_the_real_combat_env_function():
    fn = play_full_run._load_greedy_action()
    assert callable(fn) and fn.__name__ == "greedy_action"
    assert fn.__module__ == "agent.combat_env"


def _game_over(act=1, floor=3):
    return {"type": "decision", "decision": "game_over", "victory": False,
            "act": act, "floor": floor,
            "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _run(monkeypatch, script, seed="s1", **kwargs):
    """play_run() against a scripted engine; returns (result, sent commands)."""
    created = []

    def _fake(argv, **kw):
        created.append(_FakeEngine(script))
        return created[-1]

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake)
    kwargs.setdefault("verbose", False)
    kwargs.setdefault("log", False)
    result = play_full_run.play_run(seed, character="Ironclad", **kwargs)
    return result, [json.loads(line) for line in created[0].sent]


def test_start_run_carries_the_requested_ascension(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()], ascension=1)
    start = sent[0]
    assert start["cmd"] == "start_run" and start["ascension"] == 1
    assert result["ascension"] == 1


def test_ascension_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert sent[0]["ascension"] == 0 and result["ascension"] == 0


def test_result_carries_policy_and_zero_counters_under_naive(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert result["ooc_policy"] == "naive"
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 0
    assert result["ooc_fallback_causes"] == {"raised": 0, "refused": 0, "none": 0, "shop_cap": 0}
    assert result["ooc_event_stuck"] == 0
    assert result["ooc_knobs"] == {}
    assert result["game_log"] is None  # log=False writes no game log


def test_keep_game_logs_copies_the_log_and_records_the_kept_path(monkeypatch, tmp_path):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    # game_log.LOG_DIR is read at call time by GameLogger and cleanup_old_logs,
    # so pointing it at tmp_path keeps the test out of the real logs/.
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr("game_log.LOG_DIR", str(logs_dir))
    keep = tmp_path / "kept"
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()], seed="p3a_7",
                     ascension=1, log=True, keep_log_dir=str(keep))
    kept = keep / "Ironclad_naive_a1_p3a_7.jsonl"
    assert result["game_log"] == str(kept)
    lines = [json.loads(l) for l in kept.read_text().splitlines()]
    meta = lines[0]
    assert meta["type"] == "run_meta"
    assert meta["seed"] == "p3a_7" and meta["character"] == "Ironclad"
    assert meta["ascension"] == 1 and meta["experiment"] == "ooc=naive"
    assert any(l.get("type") == "state" for l in lines[1:])


def test_eval_row_carries_phase3a_fields():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "p3a_1", "act": 1, "floor": 9, "steps": 50,
         "solver_plans": 3, "solver_errors": 0, "ascension": 1,
         "ooc_policy": "greedy", "ooc_greedy": 12, "ooc_fallbacks": 1,
         "ooc_fallback_causes": {"raised": 0, "refused": 1, "none": 0, "shop_cap": 0},
         "ooc_event_stuck": 2, "ooc_knobs": {"STS2_RANDOMIZE": "shop"},
         "game_log": "/x/Ironclad_p3a_1.jsonl"}, "Ironclad")
    assert row["ascension"] == 1 and row["ooc_policy"] == "greedy"
    assert row["ooc_greedy"] == 12 and row["ooc_fallbacks"] == 1
    assert row["ooc_fallback_causes"]["refused"] == 1
    assert row["ooc_event_stuck"] == 2 and row["ooc_knobs"] == {"STS2_RANDOMIZE": "shop"}
    assert row["game_log"] == "/x/Ironclad_p3a_1.jsonl"
    assert row["status"] == "dead" and row["floor"] == 9


def _decision(kind, **extra):
    d = {"type": "decision", "decision": kind,
         "context": {"act": 1, "floor": 5, "room_type": extra.pop("room_type", "")},
         "player": {"hp": 50, "max_hp": 80, "gold": extra.pop("gold", 100), "deck_size": 10}}
    d.update(extra)
    return d


def _greedy_env(monkeypatch, policy_fn):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: policy_fn)


def test_greedy_policy_answers_card_reward(monkeypatch):
    seen = []

    def policy(state):
        seen.append(state["decision"])
        return {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}}

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert seen == ["card_reward"]
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}} in sent
    assert result["ooc_policy"] == "greedy"
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 0


def test_naive_policy_never_loads_greedy(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)

    def boom():
        raise AssertionError("naive policy must not import greedy_action")

    monkeypatch.setattr(play_full_run, "_load_greedy_action", boom)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 0}} in sent
    assert result["ooc_greedy"] == 0


def test_refused_greedy_command_falls_back_and_is_counted(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "select_card_reward",
                                        "args": {"card_index": 1}})
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              {"type": "error", "message": "nope"},
              _game_over()]
    result, sent = _run(monkeypatch, script)
    # greedy's pick (card 1) is refused -> the NAIVE choice (card 0), not a skip
    assert sent[1] == _cmd("select_card_reward", card_index=1)
    assert sent[2] == _cmd("select_card_reward", card_index=0)
    # ooc_greedy counts only commands the engine ACCEPTED
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 1
    assert result["ooc_fallback_causes"] == {"raised": 0, "refused": 1, "none": 0, "shop_cap": 0}
    assert "error" not in result  # the fallback recovered; the run ended normally


def test_greedy_exception_falls_back_without_counting_a_greedy_decision(monkeypatch):
    def policy(state):
        raise RuntimeError("scoring blew up")

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"}, _decision("rest_site", options=[]), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1] == {"cmd": "action", "action": "leave_room"}
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 1
    assert result["ooc_fallback_causes"] == {"raised": 1, "refused": 0, "none": 0, "shop_cap": 0}


def test_map_and_combat_are_never_delegated(monkeypatch):
    def policy(state):
        raise AssertionError(f"greedy called for {state['decision']}")

    _greedy_env(monkeypatch, policy)
    monkeypatch.setenv("STS2_SOLVER_CHARS", "none")
    script = [{"type": "ready"},
              _decision("map_select", choices=[{"col": 0, "row": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1]["action"] == "select_map_node"
    # the policy's AssertionError would be swallowed by the fallback's except:
    # a delegated map/combat decision would show up here as a counted fallback
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 0


def test_shop_visit_is_capped(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    cap = play_full_run.SHOP_ACTION_CAP
    # gold changes every step so the global STUCK detector stays quiet
    shops = [_decision("shop", gold=1000 - i) for i in range(cap + 1)]
    script = [{"type": "ready"}, *shops, _game_over()]
    result, sent = _run(monkeypatch, script)
    actions = [c.get("action") for c in sent[1:]]
    assert actions == ["buy_card"] * cap + ["leave_room"]
    assert result["ooc_greedy"] == cap and result["ooc_fallbacks"] == 1
    assert result["ooc_fallback_causes"]["shop_cap"] == 1


def test_shop_cap_resets_for_a_new_shop(monkeypatch):
    # Cap of 1: a second action in the SAME visit would be forced to leave_room,
    # so two buys with zero fallbacks proves the counter reset at the new floor.
    monkeypatch.setattr(play_full_run, "SHOP_ACTION_CAP", 1)
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    first = _decision("shop", gold=100)
    second = _decision("shop", gold=90)
    second["context"] = {"act": 1, "floor": 11, "room_type": ""}
    script = [{"type": "ready"}, first, second, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert [c.get("action") for c in sent[1:]] == ["buy_card", "buy_card"]
    assert result["ooc_greedy"] == 2 and result["ooc_fallbacks"] == 0


def test_event_page_stuck_guard_applies_under_greedy(monkeypatch):
    calls = []

    def policy(state):
        calls.append(1)
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}

    _greedy_env(monkeypatch, policy)
    page = _decision("event_choice",
                     options=[{"index": 0, "is_locked": False, "was_chosen": True}])
    script = [{"type": "ready"}, page, page, page, page, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert len(calls) == 3
    assert sent[-1] == {"cmd": "action", "action": "leave_room"}
    # naive leaves the same way, so this is NOT a fallback -- counted on its own
    assert result["ooc_event_stuck"] == 1 and result["ooc_fallbacks"] == 0


def _r(policy, greedy, fallbacks, **extra):
    r = {"victory": False, "seed": "s", "steps": 10, "act": 1, "floor": 5,
         "solver_plans": 1, "solver_errors": 0,
         "ooc_policy": policy, "ooc_greedy": greedy, "ooc_fallbacks": fallbacks}
    r.update(extra)
    return r


def test_summary_reports_ooc_policy_and_counts():
    text = play_full_run.summarize([_r("greedy", 10, 1), _r("greedy", 7, 0)], 2,
                                   "Ironclad", solver_chars={"Ironclad"})
    assert "OOC policy: greedy -- 17 greedy decisions, 1 fallbacks" in text
    assert "OOC POLICY NEVER ENGAGED" not in text


def test_summary_banner_when_greedy_never_engaged():
    text = play_full_run.summarize([_r("greedy", 0, 3)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "!! OOC POLICY NEVER ENGAGED" in text


def test_summary_naive_prints_policy_without_banner():
    text = play_full_run.summarize([_r("naive", 0, 0)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "OOC policy: naive" in text
    assert "NEVER ENGAGED" not in text


def test_summary_tolerates_results_without_ooc_fields():
    text = play_full_run.summarize([{"victory": False, "seed": "s", "act": 1, "floor": 5,
                                     "solver_plans": 1, "solver_errors": 0}], 1,
                                   "Ironclad", solver_chars={"Ironclad"})
    assert "OOC policy" not in text


def test_greedy_returning_none_falls_back_to_the_naive_choice(monkeypatch):
    _greedy_env(monkeypatch, lambda s: None)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1] == _cmd("select_card_reward", card_index=0)
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 1
    assert result["ooc_fallback_causes"] == {"raised": 0, "refused": 0, "none": 1, "shop_cap": 0}


def test_refused_naive_fallback_sends_the_escape_command(monkeypatch):
    _greedy_env(monkeypatch, lambda s: _cmd("select_card_reward", card_index=1))
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              {"type": "error", "message": "greedy refused"},
              {"type": "error", "message": "naive refused too"},
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1:4] == [_cmd("select_card_reward", card_index=1),
                         _cmd("select_card_reward", card_index=0),
                         _cmd("skip_card_reward")]
    assert result["ooc_fallbacks"] == 1 and "error" not in result


def test_greedy_policy_answers_bundle_select(monkeypatch):
    seen = []

    def policy(state):
        seen.append(state["decision"])
        return _cmd("select_bundle", bundle_index=1)

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"}, _decision("bundle_select", bundles=[{}, {}]), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert seen == ["bundle_select"]
    assert sent[1] == _cmd("select_bundle", bundle_index=1)
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 0


def test_policy_fields_are_stamped_on_an_error_return_path(monkeypatch):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: (lambda s: None))
    result, _ = _run(monkeypatch, [{"type": "ready"}, {"type": "error", "message": "boom"}],
                     ascension=1)
    assert result["error"].startswith("engine_error")
    assert result["ascension"] == 1 and result["ooc_policy"] == "greedy"
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 0
    assert result["ooc_event_stuck"] == 0 and "ooc_fallback_causes" in result
    assert result["game_log"] is None


def test_shop_noop_with_identical_states_is_cut_by_the_cap_not_by_stuck(monkeypatch):
    _greedy_env(monkeypatch, lambda s: _cmd("buy_card", card_index=0))
    cap = play_full_run.SHOP_ACTION_CAP
    shop = _decision("shop", gold=100)  # the engine accepts the buy but nothing changes
    script = [{"type": "ready"}, *([shop] * (cap + 1)), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert [c.get("action") for c in sent[1:]] == ["buy_card"] * cap + ["leave_room"]
    assert not result.get("timeout") and "error" not in result
    assert result["ooc_greedy"] == cap
    assert result["ooc_fallback_causes"]["shop_cap"] == 1


def test_first_raise_per_decision_prints_a_traceback_later_ones_do_not(monkeypatch, capsys):
    def policy(state):
        raise RuntimeError("scoring blew up")

    _greedy_env(monkeypatch, policy)
    # different gold so the STUCK detector stays quiet
    script = [{"type": "ready"},
              _decision("rest_site", options=[], gold=1),
              _decision("rest_site", options=[], gold=2),
              _decision("card_reward", cards=[], gold=3),
              _game_over()]
    result, _ = _run(monkeypatch, script)
    out = capsys.readouterr().out
    assert result["ooc_fallback_causes"]["raised"] == 3
    # one full traceback for rest_site (first raise), none for its repeat, one for card_reward
    assert out.count("Traceback (most recent call last)") == 2
    assert out.count("RuntimeError('scoring blew up')") == 3


# ---- parity: the naive arm's first command per decision == naive_ooc_command ----

@pytest.mark.parametrize("state,expected", NAIVE_CASES)
def test_naive_arm_sends_exactly_naive_ooc_command(monkeypatch, state, expected):
    """naive_ooc_command is what the greedy arm falls back to, so it has to be the
    command the untouched naive branch really sends -- otherwise a fallback
    silently measures a third policy."""
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: pytest.fail("naive loaded greedy"))
    live = _decision(state["decision"], **{k: v for k, v in state.items() if k != "decision"})
    result, sent = _run(monkeypatch, [{"type": "ready"}, live, _game_over()])
    assert sent[1] == expected
    assert sent[1] == play_full_run.naive_ooc_command(live)


# ---- log copy / log collision ----

def test_unwritable_keep_dir_costs_no_result(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    monkeypatch.setattr("game_log.LOG_DIR", str(tmp_path / "logs"))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # makedirs on an existing regular file raises OSError
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()], seed="p3a_9",
                     log=True, keep_log_dir=str(blocker))
    assert result["victory"] is False and result["floor"] == 3  # the game result survived
    assert result["game_log_error"]
    assert result["game_log"] and os.path.exists(result["game_log"])  # original logs/ path kept
    assert str(tmp_path / "logs") in result["game_log"]
    assert "!! could not keep game log" in capsys.readouterr().out


def test_two_loggers_with_the_same_character_and_seed_do_not_share_a_file(monkeypatch, tmp_path):
    import datetime as real_datetime
    import game_log

    class _Frozen(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2026, 9, 30, 12, 0, 0)

    monkeypatch.setattr(game_log, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(game_log, "datetime", _Frozen)
    first = game_log.GameLogger("Ironclad", "same_seed", enabled=True)
    second = game_log.GameLogger("Ironclad", "same_seed", enabled=True)
    first.log_state({"decision": "a"})
    second.log_state({"decision": "b"})
    first.close()
    second.close()
    assert first.path != second.path
    assert str(os.getpid()) in os.path.basename(second.path)
    assert [json.loads(l)["data"]["decision"] for l in open(first.path)] == ["a"]
    assert [json.loads(l)["data"]["decision"] for l in open(second.path)] == ["b"]


# ---- knobs / fail-fast ----

def test_greedy_knobs_reports_only_explicitly_set_knobs():
    assert play_full_run.greedy_knobs({}) == {}
    env = {"STS2_RANDOMIZE": "shop", "STS2_CARD_QUALITY_GATE": "0", "UNRELATED": "1"}
    assert play_full_run.greedy_knobs(env) == {"STS2_RANDOMIZE": "shop", "STS2_CARD_QUALITY_GATE": "0"}


def test_knobs_are_stamped_under_greedy_and_empty_under_naive(monkeypatch):
    monkeypatch.setenv("STS2_DECISION_ADVISOR", "1")
    _greedy_env(monkeypatch, lambda s: _cmd("skip_card_reward"))
    greedy_result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert greedy_result["ooc_knobs"] == {"STS2_DECISION_ADVISOR": "1"}
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    naive_result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert naive_result["ooc_knobs"] == {}


def _main(monkeypatch, argv, played):
    monkeypatch.setattr(sys, "argv", ["play_full_run.py", *argv])

    def fake_play_run(seed, character, verbose=True, **kw):
        played.append(seed)
        return {"victory": False, "seed": seed, "act": 1, "floor": 3, "steps": 1,
                "solver_plans": 1, "solver_errors": 0, "ooc_policy": "greedy",
                "ooc_greedy": 1, "ooc_fallbacks": 0}

    monkeypatch.setattr(play_full_run, "play_run", fake_play_run)
    play_full_run.main()


def test_main_aborts_before_any_game_when_greedy_import_is_broken(monkeypatch):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")

    def broken():
        raise ImportError("no gymnasium")

    monkeypatch.setattr(play_full_run, "_load_greedy_action", broken)
    played = []
    with pytest.raises(ImportError):
        _main(monkeypatch, ["2", "Ironclad"], played)
    assert played == []


def test_main_prints_explicitly_set_greedy_knobs(monkeypatch, capsys):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")
    monkeypatch.setenv("STS2_RANDOMIZE", "card_reward")
    monkeypatch.delenv("STS2_BASIC_PURGE_ALL", raising=False)
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: (lambda s: None))
    _main(monkeypatch, ["1", "Ironclad"], [])
    out = capsys.readouterr().out
    assert "!! greedy_action knob set: STS2_RANDOMIZE=card_reward" in out
    assert "STS2_BASIC_PURGE_ALL" not in out


def test_main_under_naive_never_loads_greedy_or_prints_knobs(monkeypatch, capsys):
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setenv("STS2_RANDOMIZE", "card_reward")
    monkeypatch.setattr(play_full_run, "_load_greedy_action",
                        lambda: pytest.fail("naive must not load greedy_action"))
    _main(monkeypatch, ["1", "Ironclad"], [])
    assert "knob set" not in capsys.readouterr().out


def test_summary_reports_fallback_causes_and_event_stuck_exits():
    causes_a = {"raised": 1, "refused": 2, "none": 0, "shop_cap": 0}
    causes_b = {"raised": 0, "refused": 1, "none": 3, "shop_cap": 1}
    results = [_r("greedy", 200, 3, ooc_fallback_causes=causes_a, ooc_event_stuck=1),
               _r("greedy", 200, 5, ooc_fallback_causes=causes_b, ooc_event_stuck=2)]
    text = play_full_run.summarize(results, 2, "Ironclad", solver_chars={"Ironclad"})
    assert ("OOC policy: greedy -- 400 greedy decisions, 8 fallbacks "
            "(raised 1, refused 3, none 3, shop_cap 1), 3 event-stuck exits") in text
    assert "OOC FALLBACK RATE" not in text  # 8 / 408 = 2.0%


def test_summary_banner_when_the_fallback_rate_is_high():
    causes = {"raised": 0, "refused": 6, "none": 0, "shop_cap": 0}
    text = play_full_run.summarize([_r("greedy", 94, 6, ooc_fallback_causes=causes,
                                       ooc_event_stuck=0)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "!! OOC FALLBACK RATE 6.0% -- the greedy arm is partly running the naive policy" in text
    assert "NEVER ENGAGED" not in text


def test_summary_no_fallback_rate_banner_at_or_below_the_threshold():
    causes = {"raised": 0, "refused": 5, "none": 0, "shop_cap": 0}
    text = play_full_run.summarize([_r("greedy", 95, 5, ooc_fallback_causes=causes,
                                       ooc_event_stuck=0)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "OOC FALLBACK RATE" not in text  # exactly 5.0% is not above the threshold
