"""Phase 3b Task 5: STS2_ROUTE_POLICY -- map routes chosen by the fitted route model.

No game DLLs and no real model: play_full_run.EngineProcess is replaced by the
scripted _FakeEngine, the model comes through the play_full_run._load_route_model
seam and the planner through play_full_run._route_choose, so these tests pin the
harness's dispatch / fallback / accounting, not the planner's judgement (that is
tests/test_route_planner.py). tests/test_map_route_determinism.py pins the
random route itself and must keep passing untouched.
"""
import json
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine

SEED = "route_seed"
CHOICES = [{"col": c, "row": 1, "type": "Monster"} for c in range(4)]
MAP_REPLY = {"type": "map", "rows": [], "boss": {"col": 3, "row": 16, "type": "Boss"}}


def _ms(row, choices=None):
    return {"type": "decision", "decision": "map_select",
            "context": {"act": 1, "floor": row},
            "choices": choices or [{"col": c, "row": row, "type": "Monster"} for c in range(4)],
            "player": {"hp": 50, "max_hp": 80, "gold": 0, "deck": [], "relics": [], "potions": []}}


def _game_over(floor=3):
    return {"type": "decision", "decision": "game_over", "victory": False, "act": 1,
            "floor": floor, "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _pick(col, row, **extra):
    return {"col": col, "row": row, "score": 12.5, "p_alive": 0.9,
            "paths": 3, "checkpoints": {"9": {"hp": 41.0}}, **extra}


class _StubModel:
    data = {"meta": {"n_games": 7}, "validation": {"chosen": "hp_only"}}


def _run(monkeypatch, script, seed=SEED, **kwargs):
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


def _value_env(monkeypatch, choose, model=None):
    """STS2_ROUTE_POLICY=value with a stub model; `choose` replaces the planner
    and receives (map_reply, state, character, model)."""
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    model = model if model is not None else _StubModel()
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: model)
    calls = []

    def _choose(map_reply, state, character, m):
        calls.append((map_reply, state, character, m))
        return choose(map_reply, state, character, m)

    monkeypatch.setattr(play_full_run, "_route_choose", _choose)
    return model, calls


def _cmds(sent, kind):
    return [c for c in sent if c.get("action") == kind or c.get("cmd") == kind]


# ---- policy resolution ----

def test_route_policy_defaults_to_random_and_blank_means_default():
    assert play_full_run.route_policy({}) == "random"
    assert play_full_run.route_policy({"STS2_ROUTE_POLICY": "  "}) == "random"


@pytest.mark.parametrize("raw,expected", [("value", "value"), ("RANDOM", "random"), (" Value ", "value")])
def test_route_policy_accepts_known_values_case_insensitively(raw, expected):
    assert play_full_run.route_policy({"STS2_ROUTE_POLICY": raw}) == expected


def test_route_policy_rejects_unknown_values_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="STS2_ROUTE_POLICY"):
        play_full_run.route_policy({"STS2_ROUTE_POLICY": "smart"})


def test_route_model_path_defaults_to_the_committed_model_and_honours_the_override():
    default = play_full_run.route_model_path({})
    assert default == play_full_run.ROUTE_MODEL_DEFAULT
    assert default.replace(os.sep, "/").endswith("agent/data/route_model_a1.json")
    assert play_full_run.route_model_path({"STS2_ROUTE_MODEL": "  "}) == default
    assert play_full_run.route_model_path({"STS2_ROUTE_MODEL": "/tmp/m.json"}) == "/tmp/m.json"


def test_load_route_model_loads_once_per_path(monkeypatch, tmp_path):
    from agent.route_model import RouteModel
    monkeypatch.setattr(play_full_run, "_ROUTE_MODEL_CACHE", {})
    data = {"version": 1,
            "value": {"features": ["hp"], "mean": [0.0], "scale": [1.0], "coef": [0.1],
                      "intercept": 0.0},
            "transitions": {"split": 0.5, "min_n": 20, "cells": {}}, "meta": {}, "validation": {}}
    a, b = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    RouteModel(data).save(a)
    RouteModel(data).save(b)
    first = play_full_run._load_route_model(a)
    assert isinstance(first, RouteModel)
    assert play_full_run._load_route_model(a) is first
    assert play_full_run._load_route_model(b) is not first


# ---- the random arm is untouched ----

def test_random_policy_never_asks_for_a_map_or_loads_a_model(monkeypatch):
    monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_load_route_model",
                        lambda path: pytest.fail("random must not load a route model"))
    monkeypatch.setattr(play_full_run, "_route_choose",
                        lambda *a: pytest.fail("random must not call the planner"))
    script = [{"type": "ready"}, _ms(1), _ms(2), _ms(3), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert not _cmds(sent, "get_map")
    assert result["route_policy"] == "random"
    assert result["route_plans"] == 0 and result["route_fallbacks"] == 0
    assert result["route_forced"] == 0 and result["route_cell_fallbacks"] == 0


def test_random_policy_does_not_count_or_special_case_single_choice_map_selects(monkeypatch):
    monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    lone = [{"col": 2, "row": 1, "type": "Monster"}]
    result, sent = _run(monkeypatch, [{"type": "ready"}, _ms(1, lone), _game_over(2)])
    assert [c.get("action") or c.get("cmd") for c in sent] == ["start_run", "select_map_node"]
    assert sent[1]["args"] == {"col": 2, "row": 1}
    assert result["route_forced"] == 0


def test_random_route_is_the_seeded_rng_choice_sequence(monkeypatch):
    monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    script = [{"type": "ready"}, _ms(1), _ms(2), _ms(3), _ms(4), _game_over(5)]
    _, sent = _run(monkeypatch, script)
    picks = [(c["args"]["col"], c["args"]["row"]) for c in _cmds(sent, "select_map_node")]
    rng = random.Random(SEED)
    expected = [(rng.choice(range(4)), row) for row in (1, 2, 3, 4)]
    assert picks == expected


# ---- the value arm ----

def test_value_policy_asks_for_the_map_each_map_select_and_sends_the_planners_pick(monkeypatch):
    model, calls = _value_env(monkeypatch, lambda m, s, ch, mo: _pick(2, s["choices"][0]["row"]))
    script = [{"type": "ready"}, _ms(1), MAP_REPLY, _ms(2), MAP_REPLY, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert [c.get("action") or c.get("cmd") for c in sent] == [
        "start_run", "get_map", "select_map_node", "get_map", "select_map_node"]
    assert [c["args"] for c in _cmds(sent, "select_map_node")] == [
        {"col": 2, "row": 1}, {"col": 2, "row": 2}]
    assert result["route_policy"] == "value"
    assert result["route_plans"] == 2 and result["route_fallbacks"] == 0
    assert result.get("error") is None and result["floor"] == 3
    # the planner got the map reply, the pending map_select decision, the character, the model
    map_reply, state, character, m = calls[0]
    assert map_reply is MAP_REPLY or map_reply == MAP_REPLY
    assert state["decision"] == "map_select" and state["choices"] == _ms(1)["choices"]
    assert character == "Ironclad" and m is model


def test_the_map_reply_does_not_replace_the_pending_decision(monkeypatch):
    _, calls = _value_env(monkeypatch, lambda m, s, ch, mo: _pick(1, 1))
    script = [{"type": "ready"}, _ms(1), MAP_REPLY, _game_over(2)]
    result, sent = _run(monkeypatch, script)
    # the command right after get_map is the pick, sent against the pending map_select ...
    assert [c.get("action") or c.get("cmd") for c in sent] == [
        "start_run", "get_map", "select_map_node"]
    assert sent[2]["args"] == {"col": 1, "row": 1}
    # ... and the planner was handed that decision, not the {"type": "map"} reply
    assert calls[0][1]["decision"] == "map_select" and calls[0][1]["choices"] == _ms(1)["choices"]
    assert result["route_plans"] == 1 and result["floor"] == 2


def test_value_policy_prints_a_route_line_for_each_planned_choice(monkeypatch, capsys):
    _value_env(monkeypatch, lambda m, s, ch, mo: _pick(3, 1))
    _run(monkeypatch, [{"type": "ready"}, _ms(1), MAP_REPLY, _game_over(2)])
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("  [route] "))
    logged = json.loads(line[len("  [route] "):])
    assert logged["act"] == 1 and logged["floor"] == 1
    assert logged["choice"] == {"col": 3, "row": 1}
    assert logged["score"] == 12.5 and logged["p_alive"] == 0.9
    assert logged["checkpoints"] == {"9": {"hp": 41.0}}


def _raises(map_reply, state, character, model):
    raise RuntimeError("planner blew up")


@pytest.mark.parametrize("choose,map_reply", [
    (_raises, MAP_REPLY),                                        # planner raises
    (lambda m, s, c, mo: _pick(9, 9), MAP_REPLY),                # node not among the choices
    (lambda m, s, c, mo: {"score": 1.0}, MAP_REPLY),             # malformed pick
    (lambda m, s, c, mo: None, MAP_REPLY),                       # planner has no answer
    (lambda m, s, c, mo: pytest.fail("planner needs a map"),
     {"type": "error", "message": "no map"}),                    # get_map failed
], ids=["raises", "not_offered", "malformed", "none", "no_map"])
def test_a_planner_failure_falls_back_to_the_seeded_random_pick_and_the_run_goes_on(
        monkeypatch, capsys, choose, map_reply):
    _value_env(monkeypatch, choose)
    script = [{"type": "ready"}, _ms(1), map_reply, _ms(2), map_reply, _game_over(3)]
    result, sent = _run(monkeypatch, script)
    picks = [(c["args"]["col"], c["args"]["row"]) for c in _cmds(sent, "select_map_node")]
    rng = random.Random(SEED)  # fallbacks draw from the same per-run rng, in order
    assert picks == [(rng.choice(range(4)), 1), (rng.choice(range(4)), 2)]
    assert result["route_fallbacks"] == 2 and result["route_plans"] == 0
    assert result.get("error") is None and result["floor"] == 3
    out = capsys.readouterr().out
    assert "[route]" not in out
    assert out.count("!! route planner fallback") == 2


def test_a_raising_planner_prints_its_traceback_once_per_run(monkeypatch, capsys):
    _value_env(monkeypatch, _raises)
    script = [{"type": "ready"}, _ms(1), MAP_REPLY, _ms(2), MAP_REPLY, _game_over(3)]
    _run(monkeypatch, script)
    out = capsys.readouterr().out
    assert out.count("Traceback (most recent call last)") == 1
    assert out.count("RuntimeError: planner blew up") == 1
    assert out.count("planner raised RuntimeError('planner blew up')") == 2  # one line per fallback


def test_planned_and_fallback_picks_are_counted_separately(monkeypatch):
    answers = iter([_pick(1, 1), None, _pick(0, 3)])
    _value_env(monkeypatch, lambda m, s, c, mo: next(answers))
    script = [{"type": "ready"}, _ms(1), MAP_REPLY, _ms(2), MAP_REPLY, _ms(3), MAP_REPLY,
              _game_over(4)]
    result, _ = _run(monkeypatch, script)
    assert result["route_plans"] == 2 and result["route_fallbacks"] == 1


def test_value_policy_stamps_the_eval_row(monkeypatch):
    _value_env(monkeypatch, lambda m, s, c, mo: _pick(1, 1, cell_levels={"act": 4, "zero": 1}))
    lone = [{"col": 0, "row": 2, "type": "Monster"}]
    result, _ = _run(monkeypatch, [{"type": "ready"}, _ms(1), MAP_REPLY, _ms(2, lone),
                                   _game_over(3)])
    row = play_full_run.result_to_eval_row(result, "Ironclad")
    assert row["route_policy"] == "value"
    assert row["route_plans"] == 1 and row["route_fallbacks"] == 0
    assert row["route_forced"] == 1 and row["route_cell_fallbacks"] == 1


def test_a_single_choice_map_select_is_taken_without_get_map_or_the_planner(monkeypatch, capsys):
    _value_env(monkeypatch, lambda *a: pytest.fail("a forced move must not reach the planner"))
    lone = [{"col": 2, "row": 1, "type": "Monster"}]
    result, sent = _run(monkeypatch, [{"type": "ready"}, _ms(1, lone), _game_over(2)])
    assert [c.get("action") or c.get("cmd") for c in sent] == ["start_run", "select_map_node"]
    assert sent[1]["args"] == {"col": 2, "row": 1}
    assert result["route_forced"] == 1
    assert result["route_plans"] == 0 and result["route_fallbacks"] == 0
    out = capsys.readouterr().out
    assert "[route]" not in out and "fallback" not in out


def test_forced_and_planned_map_selects_are_counted_separately(monkeypatch):
    _value_env(monkeypatch, lambda m, s, c, mo: _pick(1, s["choices"][0]["row"]))
    lone = lambda row: [{"col": 0, "row": row, "type": "Shop"}]
    script = [{"type": "ready"}, _ms(1, lone(1)), _ms(2), MAP_REPLY, _ms(3, lone(3)), _ms(4),
              MAP_REPLY, _game_over(5)]
    result, sent = _run(monkeypatch, script)
    assert len(_cmds(sent, "get_map")) == 2
    assert result["route_forced"] == 2 and result["route_plans"] == 2
    assert result["route_fallbacks"] == 0


def test_cell_levels_are_logged_and_the_rooms_below_the_act_level_counted(monkeypatch, capsys):
    levels = {"act": 9, "pooled": 3, "type": 1, "zero": 2}
    _value_env(monkeypatch, lambda m, s, c, mo: _pick(1, s["choices"][0]["row"],
                                                       cell_levels=levels))
    script = [{"type": "ready"}, _ms(1), MAP_REPLY, _ms(2), MAP_REPLY, _game_over(3)]
    result, _ = _run(monkeypatch, script)
    assert result["route_plans"] == 2
    assert result["route_cell_fallbacks"] == 2 * (3 + 1 + 2)   # everything below "act", per decision
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("  [route] ")]
    assert len(lines) == 2
    assert json.loads(lines[0][len("  [route] "):])["cell_levels"] == levels


def test_a_pick_without_cell_levels_counts_no_cell_fallbacks(monkeypatch):
    _value_env(monkeypatch, lambda m, s, c, mo: _pick(1, 1))   # a stub planner: no cell_levels
    result, _ = _run(monkeypatch, [{"type": "ready"}, _ms(1), MAP_REPLY, _game_over(2)])
    assert result["route_plans"] == 1 and result["route_cell_fallbacks"] == 0


class _RecordingLogger:
    """Stands in for GameLogger and remembers the run_context it was built with."""
    contexts = []

    def __init__(self, character, seed, enabled=True, run_context=None):
        self.contexts.append(run_context)
        self.path = None

    def log_action(self, cmd):
        pass

    def log_state(self, state):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("route_env,ooc_env,expected", [
    (None, None, "ooc=greedy"),                   # the random arm's tag is untouched
    ("random", "naive", "ooc=naive"),
    (None, "naive", "ooc=naive"),
    (None, "greedy", "ooc=greedy"),
    ("value", None, "ooc=greedy,route=value"),    # unset OOC policy = greedy since 2026-10-12
    ("value", "naive", "ooc=naive,route=value"),
    ("value", "greedy", "ooc=greedy,route=value"),
])
def test_run_meta_experiment_tag_marks_value_route_logs_only(monkeypatch, route_env, ooc_env,
                                                             expected):
    for name, value in (("STS2_ROUTE_POLICY", route_env), ("STS2_OOC_POLICY", ooc_env)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: _StubModel())
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: (lambda state: None))
    monkeypatch.setattr(play_full_run, "GameLogger", _RecordingLogger)
    _RecordingLogger.contexts = []
    _run(monkeypatch, [{"type": "ready"}, _game_over(1)])
    assert _RecordingLogger.contexts[0]["experiment"] == expected


@pytest.mark.parametrize("route_env,expected", [
    (None, "run:ooc=naive"),                      # exactly play_full_run's `<batch>:ooc=<policy>`
    ("random", "run:ooc=naive"),
    ("value", "run:ooc=naive,route=value"),
])
def test_run_meta_experiment_tag_keeps_the_batch_prefix(monkeypatch, route_env, expected):
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    if route_env is None:
        monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    else:
        monkeypatch.setenv("STS2_ROUTE_POLICY", route_env)
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: _StubModel())
    monkeypatch.setattr(play_full_run, "GameLogger", _RecordingLogger)
    _RecordingLogger.contexts = []
    _run(monkeypatch, [{"type": "ready"}, _game_over(1)], batch="run")
    assert _RecordingLogger.contexts[0]["experiment"] == expected


# ---- the route arms inside a run_batch.py batch (both sides of the 3b/main merge) ----

_BATCH = {"batch_id": "b1", "lane": "Ironclad/value", "game_index": 3, "games_total": 40}


@pytest.mark.parametrize("route_env,expected", [
    (None, "run:ooc=naive"),                      # the random arm stays byte-identical
    ("random", "run:ooc=naive"),
    ("value", "run:ooc=naive,route=value"),
])
def test_run_context_carries_the_batch_fields_and_only_the_value_arm_suffix(
        monkeypatch, route_env, expected):
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    if route_env is None:
        monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    else:
        monkeypatch.setenv("STS2_ROUTE_POLICY", route_env)
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: _StubModel())
    monkeypatch.setattr(play_full_run, "GameLogger", _RecordingLogger)
    _RecordingLogger.contexts = []
    _run(monkeypatch, [{"type": "ready"}, _game_over(1)], batch="run", **_BATCH)
    context = _RecordingLogger.contexts[0]
    assert context["experiment"] == expected
    assert context["harness"] == "play_full_run"
    assert {k: context[k] for k in _BATCH} == _BATCH


def test_value_arm_result_and_eval_row_carry_route_counters_and_batch_fields(monkeypatch):
    _value_env(monkeypatch, lambda m, s, c, mo: _pick(1, 1))
    result, _ = _run(monkeypatch, [{"type": "ready"}, _ms(1), MAP_REPLY, _game_over(2)],
                     batch="run", **_BATCH)
    row = play_full_run.result_to_eval_row(result, "Ironclad")
    assert row["route_policy"] == "value" and row["route_plans"] == 1
    # the eval row carries the batch identity (not games_total, which only the header does)
    assert {k: row[k] for k in ("batch_id", "lane", "game_index")} == {
        k: _BATCH[k] for k in ("batch_id", "lane", "game_index")}


def test_main_names_the_route_and_the_batch_in_both_header_lines(monkeypatch, capsys):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: _StubModel())
    monkeypatch.setattr(play_full_run, "_load_route_planner", lambda: None)
    calls = []

    def fake_play_run(seed, character, verbose=True, **kw):
        calls.append(kw)
        return {"victory": False, "seed": seed, "act": 1, "floor": 3, "steps": 1}

    monkeypatch.setattr(play_full_run, "play_run", fake_play_run)
    monkeypatch.setattr(sys, "argv", ["play_full_run.py", "2", "Ironclad", "--seed-prefix",
                                      "p3b_", "--batch-id", "b1", "--lane", "Ironclad/value"])
    play_full_run.main()
    out = capsys.readouterr().out
    assert "route policy value" in out
    assert "experiment p3b:ooc=naive,route=value, batch b1 lane Ironclad/value" in out
    assert [(c["batch_id"], c["lane"], c["game_index"], c["games_total"]) for c in calls] == [
        ("b1", "Ironclad/value", 1, 2), ("b1", "Ironclad/value", 2, 2)]


def test_a_route_model_that_cannot_load_fails_the_game_as_an_error(monkeypatch):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")

    def broken(path):
        raise FileNotFoundError("/no/such/model.json")

    monkeypatch.setattr(play_full_run, "_load_route_model", broken)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _ms(1), _game_over()])
    # never a silent fall back to random: the result is an error that summarize() reports
    assert "/no/such/model.json" in result["error"]
    assert not _cmds(sent, "select_map_node")
    assert "ERROR" in _summary([result])


# ---- summary ----

def _r(policy, plans, fallbacks, forced=0, cell_fallbacks=0):
    return {"victory": False, "seed": "s", "steps": 10, "act": 1, "floor": 5,
            "solver_plans": 1, "solver_errors": 0,
            "route_policy": policy, "route_plans": plans, "route_fallbacks": fallbacks,
            "route_forced": forced, "route_cell_fallbacks": cell_fallbacks}


def _summary(results):
    return play_full_run.summarize(results, len(results), "Ironclad", solver_chars={"Ironclad"})


def test_summary_reports_the_route_policy_and_counts():
    text = _summary([_r("value", 12, 0, forced=20, cell_fallbacks=5),
                     _r("value", 9, 0, forced=13, cell_fallbacks=2)])
    assert "Route policy: value -- 21 planned, 33 forced, 0 fallbacks, 7 cell fallbacks" in text
    assert "ROUTE PLANNER NEVER ENGAGED" not in text
    assert "route planner fell back" not in text


def test_summary_banner_when_the_value_planner_never_engaged():
    text = _summary([_r("value", 0, 4)])
    assert "Route policy: value -- 0 planned, 0 forced, 4 fallbacks, 0 cell fallbacks" in text
    assert "!! ROUTE PLANNER NEVER ENGAGED" in text


def test_summary_banner_when_the_value_planner_fell_back():
    text = _summary([_r("value", 10, 3), _r("value", 8, 0)])
    assert "!! route planner fell back 3 times" in text
    assert "NEVER ENGAGED" not in text


def test_summary_random_prints_the_policy_without_banners():
    text = _summary([_r("random", 0, 0)])
    assert "Route policy: random -- 0 planned, 0 forced, 0 fallbacks, 0 cell fallbacks" in text
    # "fell back" alone also appears in the solver engagement line ("N turns fell
    # back to the heuristic"), so name the route banner's own wording.
    assert "NEVER ENGAGED" not in text and "route planner fell back" not in text


def test_summary_tolerates_results_from_before_the_forced_and_cell_counters():
    old = {"victory": False, "seed": "s", "act": 1, "floor": 5, "solver_plans": 1,
           "solver_errors": 0, "route_policy": "value", "route_plans": 3, "route_fallbacks": 0}
    assert "Route policy: value -- 3 planned, 0 forced, 0 fallbacks, 0 cell fallbacks" in \
        _summary([old])


def test_summary_tolerates_results_without_route_fields():
    text = play_full_run.summarize([{"victory": False, "seed": "s", "act": 1, "floor": 5,
                                     "solver_plans": 1, "solver_errors": 0}], 1,
                                   "Ironclad", solver_chars={"Ironclad"})
    assert "Route policy" not in text


# ---- main() ----

def _main(monkeypatch, argv, played):
    monkeypatch.setattr(sys, "argv", ["play_full_run.py", *argv])

    def fake_play_run(seed, character, verbose=True, **kw):
        played.append(seed)
        return {"victory": False, "seed": seed, "act": 1, "floor": 3, "steps": 1,
                "solver_plans": 1, "solver_errors": 0, "route_policy": "value",
                "route_plans": 2, "route_fallbacks": 0}

    monkeypatch.setattr(play_full_run, "play_run", fake_play_run)
    play_full_run.main()


def test_main_announces_the_route_model_under_value(monkeypatch, capsys):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_ROUTE_MODEL", "/models/m.json")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    loaded, planner_loads = [], []
    monkeypatch.setattr(play_full_run, "_load_route_model",
                        lambda path: loaded.append(path) or _StubModel())
    monkeypatch.setattr(play_full_run, "_load_route_planner",
                        lambda: planner_loads.append(1))
    played = []
    _main(monkeypatch, ["2", "Ironclad"], played)
    out = capsys.readouterr().out
    assert loaded == ["/models/m.json"]  # once, before any game
    assert planner_loads == [1]          # the planner (and card data) is imported up front too
    assert "Route policy: value (model /models/m.json, n_games 7, value hp_only)" in out
    assert len(played) == 2


def test_main_aborts_before_any_game_when_the_route_model_cannot_load(monkeypatch):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_ROUTE_MODEL", "/nonexistent/model.json")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_ROUTE_MODEL_CACHE", {})
    played = []
    with pytest.raises(FileNotFoundError):
        _main(monkeypatch, ["2", "Ironclad"], played)
    assert played == []


def test_main_aborts_before_any_game_when_the_planner_cannot_be_imported(monkeypatch):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "value")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_load_route_model", lambda path: _StubModel())

    def broken():
        raise FileNotFoundError("data/relics.json")  # card data missing

    monkeypatch.setattr(play_full_run, "_load_route_planner", broken)
    played = []
    with pytest.raises(FileNotFoundError, match="relics"):
        _main(monkeypatch, ["2", "Ironclad"], played)
    assert played == []


def test_the_real_planner_module_loads_through_the_seam():
    planner = play_full_run._load_route_planner()
    assert callable(planner.choose_node) and planner.__name__ == "agent.route_planner"


def test_main_rejects_an_unknown_route_policy_before_any_game(monkeypatch):
    monkeypatch.setenv("STS2_ROUTE_POLICY", "smart")
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    played = []
    with pytest.raises(ValueError, match="STS2_ROUTE_POLICY"):
        _main(monkeypatch, ["1", "Ironclad"], played)
    assert played == []


def test_main_under_random_never_loads_a_route_model(monkeypatch, capsys):
    monkeypatch.delenv("STS2_ROUTE_POLICY", raising=False)
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "_load_route_model",
                        lambda path: pytest.fail("random must not load a route model"))
    monkeypatch.setattr(play_full_run, "_load_route_planner",
                        lambda: pytest.fail("random must not import the planner"))
    _main(monkeypatch, ["1", "Ironclad"], [])
    out = capsys.readouterr().out
    assert "Route policy: value (model" not in out
    assert "route policy random" in out
