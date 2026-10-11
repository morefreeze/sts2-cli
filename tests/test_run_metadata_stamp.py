"""play_full_run game logs carry the metadata the run workbench files them by.

Before this, play_run() wrote a run_meta header of only seed/character/ascension/
experiment, so every batch landed in the workbench's "未知版本（未归档）" cohort
(no game_version / evaluation_mode / scenario), baseline comparison was off, and
a run's detail page warned "Multiplayer flag must be a boolean" (the replay
adapter had no is_multiplayer to read). No game DLLs: the engine is the scripted
fake the other play_full_run tests use.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import game_log
import play_full_run
from tests.test_map_route_determinism import _FakeEngine

from agent.run_metadata import ResolvedGameVersion

_RELEASE_INFO = {"commit": "41cef1ea", "version": "v0.111.0", "branch": "v0.111.0"}


def _game_over(act=1, floor=3):
    return {"type": "decision", "decision": "game_over", "victory": False,
            "act": act, "floor": floor,
            "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _play(monkeypatch, tmp_path, seed="run_7", script=None, **kwargs):
    """play_run() against a scripted engine with logging into tmp_path.
    Returns (result, header, log lines, commands the harness sent)."""
    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(game_log, "LOG_DIR", str(tmp_path / "logs"))
    created = []

    def _fake(argv, **kw):
        created.append(_FakeEngine(script or [{"type": "ready"}, _game_over()]))
        return created[-1]

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake)
    result = play_full_run.play_run(seed, character="Ironclad", verbose=False,
                                    log=True, **kwargs)
    with open(result["game_log"], encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh]
    assert lines[0]["type"] == "run_meta"
    return result, lines[0], lines, [json.loads(c) for c in created[0].sent]


# --- header fields ------------------------------------------------------------

def test_header_carries_everything_the_workbench_groups_cohorts_by(monkeypatch, tmp_path):
    version = ResolvedGameVersion("v0.111.0", "release_info")
    _, header, _, _ = _play(monkeypatch, tmp_path, seed="run_7", ascension=1,
                            game_version=version, batch="run")
    assert header["game_version"] == "v0.111.0"
    assert header["game_version_source"] == "release_info"
    assert header["evaluation_mode"] == "fixed"
    assert header["scenario"] == "full_run"
    assert header["is_multiplayer"] is False  # a strict bool, not merely falsy
    assert header["character"] == "Ironclad" and header["seed"] == "run_7"
    assert header["ascension"] == 1
    assert header["experiment"] == "run:ooc=naive"
    assert re.fullmatch(r"pfr-[0-9a-f]{12}", header["run_id"])


def test_header_without_a_resolved_version_omits_it_rather_than_null(monkeypatch, tmp_path):
    _, header, _, _ = _play(monkeypatch, tmp_path, game_version=None, batch="run")
    assert "game_version" not in header and "game_version_source" not in header
    assert None not in header.values()
    # the rest of the identity is still there
    assert header["evaluation_mode"] == "fixed" and header["scenario"] == "full_run"
    assert header["is_multiplayer"] is False


def test_header_keeps_the_legacy_experiment_without_a_batch(monkeypatch, tmp_path):
    """A direct play_run() caller that names no batch keeps the old tag."""
    _, header, _, _ = _play(monkeypatch, tmp_path)
    assert header["experiment"] == "ooc=naive"


def test_header_does_not_disturb_step_numbering_or_the_engine_traffic(monkeypatch, tmp_path):
    _, _, lines, sent = _play(monkeypatch, tmp_path, batch="run")
    assert [line["type"] for line in lines] == ["run_meta", "action", "state"]
    assert "step" not in lines[0]
    assert [line["step"] for line in lines[1:]] == [0, 1]
    assert [cmd["cmd"] for cmd in sent] == ["start_run"]


# --- run_id -------------------------------------------------------------------

def test_result_and_eval_row_carry_the_headers_run_id(monkeypatch, tmp_path):
    result, header, _, _ = _play(monkeypatch, tmp_path, batch="run")
    assert result["run_id"] == header["run_id"]
    row = play_full_run.result_to_eval_row(result, "Ironclad")
    assert row["run_id"] == header["run_id"]


def test_each_game_gets_its_own_run_id(monkeypatch, tmp_path):
    first, header_a, _, _ = _play(monkeypatch, tmp_path, seed="run_1")
    second, header_b, _, _ = _play(monkeypatch, tmp_path, seed="run_2")
    assert header_a["run_id"] != header_b["run_id"]
    assert first["run_id"] != second["run_id"]


def test_run_id_is_stamped_on_an_error_return_path(monkeypatch, tmp_path):
    result, header, _, _ = _play(
        monkeypatch, tmp_path,
        script=[{"type": "not_ready"}])  # bad_init: play_run's earliest return
    assert result["error"] == "bad_init"
    assert result["run_id"] == header["run_id"]


def test_eval_row_without_a_run_id_has_none():
    # a result that predates the stamp (or a hand-built one)
    row = play_full_run.result_to_eval_row({"seed": "s", "act": 1, "floor": 3}, "Ironclad")
    assert row["run_id"] is None


# --- experiment naming ---------------------------------------------------------

@pytest.mark.parametrize("experiment, prefix, expected", [
    (None, "run_", "run"),            # the default prefix
    (None, "p3a_", "p3a"),
    (None, "ab3_20261012", "ab3_20261012"),  # no trailing underscore to strip
    (None, "weird__", "weird"),
    ("greedy-vs-naive", "run_", "greedy-vs-naive"),  # --experiment wins
    ("  padded  ", "run_", "padded"),
    (None, "", None),                 # nothing to name the batch by
    (None, "_", None),
    ("", "run_", "run"),              # blank --experiment is not a name
])
def test_batch_name(experiment, prefix, expected):
    assert play_full_run.batch_name(experiment, prefix) == expected


def test_experiment_label_keeps_the_policy_tag():
    assert play_full_run.experiment_label("run", "greedy") == "run:ooc=greedy"
    assert play_full_run.experiment_label(None, "naive") == "ooc=naive"


# --- game version resolution ---------------------------------------------------

def _install(tmp_path):
    resources = tmp_path / "Resources"
    data = resources / "data_sts2_macos_arm64"
    data.mkdir(parents=True)
    (resources / "release_info.json").write_text(json.dumps(_RELEASE_INFO))
    return str(data)


def test_resolution_order_is_cli_then_environment_then_release_info(monkeypatch, tmp_path):
    game_dir = _install(tmp_path)
    monkeypatch.setenv("STS2_GAME_DIR", game_dir)
    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    resolve = play_full_run.resolve_run_game_version

    assert resolve(None) == ResolvedGameVersion("v0.111.0", "release_info")
    monkeypatch.setenv("STS2_GAME_VERSION", "v0.102.0")
    assert resolve(None) == ResolvedGameVersion("v0.102.0", "environment")
    assert resolve("v0.103.2") == ResolvedGameVersion("v0.103.2", "cli")


def test_resolution_is_none_when_nothing_names_a_version(monkeypatch, tmp_path):
    monkeypatch.setenv("STS2_GAME_DIR", str(tmp_path / "nowhere" / "data"))
    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    assert play_full_run.resolve_run_game_version(None) is None


# --- main() --------------------------------------------------------------------

def _main(monkeypatch, argv, capsys, **env):
    """Run main() with play_run stubbed; returns (kwargs of every play_run call, stdout)."""
    calls = []

    def fake_play_run(seed, character, verbose=True, **kw):
        calls.append({"seed": seed, **kw})
        return {"victory": False, "seed": seed, "steps": 1, "act": 1, "floor": 2,
                "hp": 0, "max_hp": 80, "solver_plans": 0, "solver_errors": 0}

    monkeypatch.setenv("STS2_OOC_POLICY", "naive")
    monkeypatch.setattr(play_full_run, "play_run", fake_play_run)
    monkeypatch.setattr(sys, "argv", ["play_full_run.py", *argv])
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    play_full_run.main()
    return calls, capsys.readouterr().out


def test_main_passes_the_resolved_version_and_batch_to_every_game(monkeypatch, tmp_path, capsys):
    calls, out = _main(monkeypatch, ["3", "Ironclad"], capsys,
                       STS2_GAME_DIR=_install(tmp_path), STS2_GAME_VERSION=None)
    assert len(calls) == 3
    for call in calls:
        assert call["game_version"] == ResolvedGameVersion("v0.111.0", "release_info")
        assert call["batch"] == "run"
    assert "v0.111.0" in out and "release_info" in out
    assert "game version unresolved" not in out


def test_main_cli_flags_beat_the_environment_and_name_the_batch(monkeypatch, tmp_path, capsys):
    calls, _ = _main(
        monkeypatch,
        ["2", "Silent", "--game-version", "v9.9.9", "--experiment", "my-ab",
         "--seed-prefix", "zzz_"],
        capsys, STS2_GAME_DIR=_install(tmp_path), STS2_GAME_VERSION="v0.102.0")
    assert [c["seed"] for c in calls] == ["zzz_1", "zzz_2"]
    assert {c["batch"] for c in calls} == {"my-ab"}
    assert {c["game_version"] for c in calls} == {ResolvedGameVersion("v9.9.9", "cli")}


def test_main_batch_defaults_to_the_seed_prefix_without_its_underscore(monkeypatch, tmp_path, capsys):
    calls, _ = _main(monkeypatch, ["1", "--seed-prefix", "p3b_"], capsys,
                     STS2_GAME_DIR=_install(tmp_path), STS2_GAME_VERSION=None)
    assert calls[0]["batch"] == "p3b"


def test_main_warns_exactly_once_and_still_plays_when_no_version_resolves(
        monkeypatch, tmp_path, capsys):
    calls, out = _main(monkeypatch, ["3"], capsys,
                       STS2_GAME_DIR=str(tmp_path / "nowhere" / "data"),
                       STS2_GAME_VERSION=None)
    assert len(calls) == 3  # never fails the run
    assert all(call["game_version"] is None for call in calls)
    assert out.count("game version unresolved") == 1


def test_main_rejects_an_invalid_explicit_version_before_any_game(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        _main(monkeypatch, ["1", "--game-version", "v" * 500], capsys,
              STS2_GAME_DIR=_install(tmp_path), STS2_GAME_VERSION=None)
    assert "--game-version" in capsys.readouterr().err


def test_main_rejects_a_blank_experiment_name_before_any_game(monkeypatch, tmp_path, capsys):
    # (blank --experiment is rejected rather than silently renamed after the seed prefix)
    with pytest.raises(SystemExit):
        _main(monkeypatch, ["1", "--experiment", "   "], capsys,
              STS2_GAME_DIR=_install(tmp_path), STS2_GAME_VERSION=None)
    assert "--experiment" in capsys.readouterr().err
