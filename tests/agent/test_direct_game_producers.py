import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.boss_retry as boss_retry
import agent.collect_deck_stats as collect_deck_stats
import agent.gen_boss_saves as gen_boss_saves
import agent.mc_rollout as mc_rollout
import agent.evolve_loop as evolve_loop
import agent.eval_rl as eval_rl
import agent.train as train
from agent.run_metadata import ResolvedGameVersion


@pytest.mark.parametrize(
    "module",
    [
        collect_deck_stats,
        boss_retry,
        mc_rollout,
        gen_boss_saves,
        evolve_loop,
        train,
    ],
)
def test_current_module_usage_teaches_required_version_and_ascension(module):
    usage = module.__doc__ or ""

    assert "--game-version" in usage
    assert "--ascension" in usage


@pytest.mark.parametrize(
    "module",
    [
        collect_deck_stats,
        boss_retry,
        mc_rollout,
        gen_boss_saves,
        evolve_loop,
        eval_rl,
        train,
    ],
)
def test_cli_help_explains_version_precedence_and_ascension(
    monkeypatch, capsys, module
):
    monkeypatch.setattr(module.sys, "argv", [module.__name__, "--help"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "STS2_GAME_VERSION" in help_text
    assert "takes precedence" in help_text
    assert "Ascension level" in help_text


def test_readme_teaches_versioned_training_and_diagnostic_commands():
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")

    assert "STS2_GAME_VERSION" in readme
    for command in (
        "agent.train",
        "agent.eval_rl",
        "agent.evolve_loop",
        "agent.collect_deck_stats",
        "agent.boss_retry",
        "agent.mc_rollout",
        "agent.gen_boss_saves",
    ):
        assert command in readme
    assert "--game-version" in readme
    assert "--ascension" in readme


def test_agent_readme_training_examples_are_directly_executable_with_metadata():
    readme = (Path(__file__).parents[2] / "agent" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "CLI" in readme and "STS2_GAME_VERSION" in readme and "优先" in readme
    assert (
        "python agent/train.py --character Ironclad --steps 100000 --n-envs 4 "
        "--game-version v0.103.2 --ascension 0"
    ) in readme
    assert (
        "python agent/train.py --character Ironclad --steps 100000 "
        "--game-version v0.103.2 --ascension 1"
    ) in readme
    assert (
        "--checkpoint checkpoints/ppo_ironclad_100k.zip "
        "--game-version v0.103.2 --ascension 0"
    ) in readme


@pytest.mark.parametrize(
    ("module", "argv", "load_attribute"),
    [
        (collect_deck_stats, ["collect", "model.zip"], "MaskablePPO"),
        (boss_retry, ["boss", "model.zip", "snapshot.save"], "MaskablePPO"),
        (mc_rollout, ["mc", "snapshot.save"], None),
        (gen_boss_saves, ["gen", "model.zip"], "MaskablePPO"),
    ],
)
def test_direct_cli_requires_version_before_model_or_file_work(
    monkeypatch, module, argv, load_attribute
):
    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    monkeypatch.setattr(module.sys, "argv", argv)
    if load_attribute is not None:
        monkeypatch.setattr(
            getattr(module, load_attribute),
            "load",
            lambda *args, **kwargs: pytest.fail("model must not be loaded"),
        )
    else:
        monkeypatch.setattr(
            module,
            "_maybe_load_model",
            lambda *args, **kwargs: pytest.fail("model must not be inspected"),
        )

    with pytest.raises(SystemExit):
        module.main()


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (collect_deck_stats, ["collect", "model.zip"]),
        (boss_retry, ["boss", "model.zip", "snapshot.save"]),
        (mc_rollout, ["mc", "snapshot.save"]),
        (gen_boss_saves, ["gen", "model.zip"]),
    ],
)
def test_direct_cli_rejects_invalid_ascension_before_work(monkeypatch, module, argv):
    monkeypatch.setattr(
        module.sys,
        "argv",
        [*argv, "--game-version", "v0.103.2", "--ascension", "11"],
    )
    if hasattr(module, "MaskablePPO"):
        monkeypatch.setattr(
            module.MaskablePPO,
            "load",
            lambda *args, **kwargs: pytest.fail("model must not be loaded"),
        )

    with pytest.raises(SystemExit):
        module.main()


def test_collect_game_builds_full_run_deck_stats_context_before_env(monkeypatch):
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(collect_deck_stats, "CombatEnv", capture)
    with pytest.raises(RuntimeError, match="captured"):
        collect_deck_stats.play_one_game(
            object(),
            "Silent",
            "real-seed",
            False,
            game_version=ResolvedGameVersion("v0.103.2", "environment"),
            ascension=7,
            checkpoint="models/policy.zip",
        )

    assert calls[0]["ascension"] == 7
    assert calls[0]["run_context"] == {
        "game_version": "v0.103.2",
        "game_version_source": "environment",
        "character": "Silent",
        "checkpoint": "policy.zip",
        "evaluation_mode": "deck_stats",
        "scenario": "full_run",
    }


def test_boss_retry_builds_native_save_context_with_sidecar_seed(monkeypatch, tmp_path):
    save = tmp_path / "boss.save"
    save.write_text("save", encoding="utf-8")
    (tmp_path / "boss.save.meta.json").write_text(
        '{"seed":"boss-seed"}', encoding="utf-8"
    )
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(boss_retry, "CombatEnv", capture)
    with pytest.raises(RuntimeError, match="captured"):
        boss_retry._play_one(
            object(),
            str(save),
            True,
            False,
            game_version=ResolvedGameVersion("v0.103.2", "cli"),
            ascension=10,
            character="Silent",
            checkpoint="models/policy.zip",
        )

    assert calls[0]["character"] == "Silent"
    assert calls[0]["ascension"] == 10
    assert calls[0]["run_context"] == {
        "game_version": "v0.103.2",
        "game_version_source": "cli",
        "character": "Silent",
        "checkpoint": "policy.zip",
        "evaluation_mode": "boss_retry",
        "scenario": "native_save",
        "seed": "boss-seed",
    }


def test_mc_rollout_builds_native_save_context_without_guessing_seed(monkeypatch, tmp_path):
    save = tmp_path / "state.save"
    save.write_text("save", encoding="utf-8")
    calls = []

    def capture(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("captured")

    monkeypatch.setattr(mc_rollout, "CombatEnv", capture)
    with pytest.raises(RuntimeError, match="captured"):
        mc_rollout._one_rollout(
            None,
            str(save),
            False,
            False,
            game_version=ResolvedGameVersion("v0.103.2", "cli"),
            ascension=3,
            character="Defect",
            checkpoint=None,
        )

    assert calls[0]["ascension"] == 3
    assert calls[0]["run_context"] == {
        "game_version": "v0.103.2",
        "game_version_source": "cli",
        "character": "Defect",
        "checkpoint": None,
        "evaluation_mode": "mc_rollout",
        "scenario": "native_save",
        "seed": None,
    }


@pytest.mark.parametrize("api", ["collect", "boss", "mc"])
def test_programmatic_direct_api_requires_resolved_version_before_env(monkeypatch, api):
    module = {
        "collect": collect_deck_stats,
        "boss": boss_retry,
        "mc": mc_rollout,
    }[api]
    monkeypatch.setattr(
        module,
        "CombatEnv",
        lambda **kwargs: pytest.fail("env must not be created"),
    )

    with pytest.raises(ValueError, match="ResolvedGameVersion"):
        if api == "collect":
            module.play_one_game(object(), "Ironclad", "seed", False)
        elif api == "boss":
            module._play_one(object(), "snapshot.save", True, False)
        else:
            module.rollout("snapshot.save", n_sims=1)


def test_gen_boss_saves_builds_snapshot_generation_context(monkeypatch, tmp_path):
    captured = []

    class Captured(Exception):
        pass

    monkeypatch.setattr(
        gen_boss_saves.sys,
        "argv",
        [
            "gen",
            "models/policy.zip",
            "--character",
            "Silent",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "8",
            "--out-dir",
            str(tmp_path),
            "--max-attempts",
            "1",
        ],
    )
    monkeypatch.setattr(
        gen_boss_saves.MaskablePPO,
        "load",
        lambda *args, **kwargs: SimpleNamespace(
            observation_space=SimpleNamespace(shape=(161,))
        ),
    )

    def capture(**kwargs):
        captured.append(kwargs)
        raise Captured

    monkeypatch.setattr(gen_boss_saves, "CombatEnv", capture)

    with pytest.raises(Captured):
        gen_boss_saves.main()

    assert captured[0]["ascension"] == 8
    assert captured[0]["run_context"] == {
        "game_version": "v0.103.2",
        "game_version_source": "cli",
        "character": "Silent",
        "checkpoint": "policy.zip",
        "evaluation_mode": "snapshot_generation",
        "scenario": "full_run",
    }


def test_collect_json_report_includes_versioned_context(monkeypatch, tmp_path):
    report = tmp_path / "deck_stats.json"
    scores = tmp_path / "scores.json"
    monkeypatch.setattr(
        collect_deck_stats.sys,
        "argv",
        [
            "collect",
            "models/policy.zip",
            "--n-games",
            "1",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "6",
            "--out",
            str(report),
            "--scores-out",
            str(scores),
        ],
    )
    monkeypatch.setattr(
        collect_deck_stats.MaskablePPO,
        "load",
        lambda *args, **kwargs: SimpleNamespace(
            observation_space=SimpleNamespace(shape=(161,))
        ),
    )
    monkeypatch.setattr(
        collect_deck_stats,
        "play_one_game",
        lambda *args, **kwargs: {
            "max_floor": 20,
            "deck": [{"id": "BASH"}],
            "timed_out": False,
            "deck_size": 1,
        },
    )

    assert collect_deck_stats.main() == 0

    assert json.loads(report.read_text(encoding="utf-8"))["run_context"] == {
        "game_version": "v0.103.2",
        "game_version_source": "cli",
        "character": "Ironclad",
        "checkpoint": "policy.zip",
        "evaluation_mode": "deck_stats",
        "scenario": "full_run",
        "ascension": 6,
    }


def test_boss_report_includes_per_snapshot_context(monkeypatch, tmp_path):
    save = tmp_path / "boss.save"
    save.write_text("save", encoding="utf-8")
    (tmp_path / "boss.save.meta.json").write_text(
        '{"seed":"boss-seed"}', encoding="utf-8"
    )
    report = tmp_path / "boss-report.json"
    monkeypatch.setattr(
        boss_retry.sys,
        "argv",
        [
            "boss",
            "models/policy.zip",
            str(save),
            "--n-deterministic",
            "0",
            "--n-stochastic",
            "0",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "4",
            "--report-json",
            str(report),
        ],
    )
    monkeypatch.setattr(
        boss_retry.MaskablePPO,
        "load",
        lambda *args, **kwargs: SimpleNamespace(
            observation_space=SimpleNamespace(shape=(161,)), num_timesteps=0
        ),
    )

    boss_retry.main()

    context = json.loads(report.read_text(encoding="utf-8"))[0]["run_context"]
    assert context["game_version"] == "v0.103.2"
    assert context["game_version_source"] == "cli"
    assert context["ascension"] == 4
    assert context["evaluation_mode"] == "boss_retry"
    assert context["scenario"] == "native_save"
    assert context["seed"] == "boss-seed"


def test_mc_report_includes_native_context(monkeypatch, tmp_path):
    save = tmp_path / "state.save"
    save.write_text("save", encoding="utf-8")
    report = tmp_path / "mc-report.json"
    monkeypatch.setattr(
        mc_rollout.sys,
        "argv",
        [
            "mc",
            str(save),
            "--n-sims",
            "0",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "2",
            "--report-json",
            str(report),
        ],
    )
    monkeypatch.setattr(mc_rollout, "_maybe_load_model", lambda path: (None, False))

    assert mc_rollout.main() == 0

    context = json.loads(report.read_text(encoding="utf-8"))["run_context"]
    assert context["game_version"] == "v0.103.2"
    assert context["game_version_source"] == "cli"
    assert context["ascension"] == 2
    assert context["evaluation_mode"] == "mc_rollout"
    assert context["scenario"] == "native_save"
    assert context["seed"] is None


def test_generated_save_sidecar_contains_authoritative_context(monkeypatch, tmp_path):
    class FakeEnv:
        def _send(self, command):
            return {"success": True}

        def close(self):
            pass

    monkeypatch.setattr(
        gen_boss_saves.sys,
        "argv",
        [
            "gen",
            "models/policy.zip",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "5",
            "--out-dir",
            str(tmp_path),
            "--max-attempts",
            "1",
            "--n-saves",
            "1",
        ],
    )
    monkeypatch.setattr(
        gen_boss_saves.MaskablePPO,
        "load",
        lambda *args, **kwargs: SimpleNamespace(
            observation_space=SimpleNamespace(shape=(161,))
        ),
    )
    monkeypatch.setattr(gen_boss_saves, "CombatEnv", lambda **kwargs: FakeEnv())
    monkeypatch.setattr(gen_boss_saves, "ActionMasker", lambda env, fn: env)
    monkeypatch.setattr(
        gen_boss_saves,
        "_play_until_floor",
        lambda *args, **kwargs: (
            {"floor": 16, "player": {"hp": 70, "max_hp": 80}},
            16,
        ),
    )
    monkeypatch.setattr(gen_boss_saves.random, "randint", lambda *args: 0xABC)
    monkeypatch.setattr(gen_boss_saves.time, "strftime", lambda pattern: "20260810_120000")

    assert gen_boss_saves.main() == 0

    sidecars = list(tmp_path.glob("*.save.meta.json"))
    assert len(sidecars) == 1
    metadata = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert metadata["seed"] == "genboss_000abc"
    assert metadata["game_version"] == "v0.103.2"
    assert metadata["game_version_source"] == "cli"
    assert metadata["ascension"] == 5
    assert metadata["evaluation_mode"] == "snapshot_generation"
    assert metadata["scenario"] == "full_run"
