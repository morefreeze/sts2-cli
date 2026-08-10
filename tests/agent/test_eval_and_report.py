import json
import os

import pytest
import eval_and_report
from agent.run_metadata import resolve_game_version

from eval_and_report import (
    _build_parser,
    _evaluation_key,
    run_eval,
)


def test_report_cli_requires_explicit_checkpoint_and_defaults_to_fixed_seeds():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    args = parser.parse_args(["checkpoints/model.zip"])
    assert args.checkpoint == "checkpoints/model.zip"
    assert args.fixed_seeds is True
    assert args.n_games == 10
    assert args.game_version is None
    assert args.ascension == 0
    assert parser.parse_args(
        ["checkpoints/model.zip", "--ascension", "10"]
    ).ascension == 10


def test_report_launch_metadata_prefers_cli_and_falls_back_to_environment(monkeypatch):
    parser = _build_parser()
    monkeypatch.setenv("STS2_GAME_VERSION", "v0.102.0")

    cli_version, cli_ascension = eval_and_report._resolve_launch_metadata(
        parser.parse_args(
            [
                "checkpoints/model.zip",
                "--game-version",
                "  v0.103.2  ",
                "--ascension",
                "10",
            ]
        )
    )
    environment_version, environment_ascension = eval_and_report._resolve_launch_metadata(
        parser.parse_args(["checkpoints/model.zip"])
    )

    assert cli_version.value == "v0.103.2"
    assert cli_version.source == "cli"
    assert cli_ascension == 10
    assert environment_version.value == "v0.102.0"
    assert environment_version.source == "environment"
    assert environment_ascension == 0


def test_report_eval_rejects_missing_version_before_model_or_evaluation(monkeypatch):
    import agent.eval_rl as eval_rl
    from sb3_contrib import MaskablePPO

    monkeypatch.setattr(
        MaskablePPO,
        "load",
        lambda *args, **kwargs: pytest.fail("checkpoint must not be loaded"),
    )
    monkeypatch.setattr(
        eval_rl,
        "run_eval_verbose",
        lambda *args, **kwargs: pytest.fail("evaluation must not run"),
    )

    with pytest.raises(ValueError, match="game_version"):
        run_eval(
            "checkpoints/model.zip",
            game_version=None,
            game_version_source=None,
        )


def test_report_launch_metadata_rejects_bad_ascension(monkeypatch):
    parser = _build_parser()
    args = parser.parse_args(
        [
            "checkpoints/model.zip",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "11",
        ]
    )

    with pytest.raises(ValueError, match=r"0\.\.10"):
        eval_and_report._resolve_launch_metadata(args)


def test_report_main_forwards_resolved_metadata_only_to_evaluation(monkeypatch):
    calls = []

    class Writable:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, value):
            return len(value)

    def fake_open(path, mode="r"):
        if "r" in mode:
            raise FileNotFoundError(path)
        return Writable()

    monkeypatch.setattr(
        eval_and_report.sys,
        "argv",
        [
            "eval_and_report.py",
            "checkpoints/model.zip",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "10",
        ],
    )
    monkeypatch.setattr(eval_and_report, "open", fake_open, raising=False)
    monkeypatch.setattr(eval_and_report.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        eval_and_report,
        "run_eval",
        lambda checkpoint, **kwargs: calls.append((checkpoint, kwargs)) or {},
    )
    monkeypatch.setattr(eval_and_report, "format_report", lambda stats, checkpoint: "report")

    eval_and_report.main()

    assert calls == [
        (
            "checkpoints/model.zip",
            {
                "n_games": 10,
                "fixed_seeds": True,
                "invalid_retries": 1,
                "ascension": 10,
                "game_version": "v0.103.2",
                "game_version_source": "cli",
            },
        )
    ]


def test_evaluation_key_distinguishes_path_and_seed_configuration(tmp_path):
    first = tmp_path / "first" / "ppo_ironclad_100k.zip"
    second = tmp_path / "second" / "ppo_ironclad_100k.zip"

    fixed_key = _evaluation_key(
        str(first), n_games=10, fixed_seeds=True, invalid_retries=1,
        game_version="v0.103.2", ascension=0)

    assert fixed_key != _evaluation_key(
        str(second), n_games=10, fixed_seeds=True, invalid_retries=1,
        game_version="v0.103.2", ascension=0)
    assert fixed_key != _evaluation_key(
        str(first), n_games=10, fixed_seeds=False, invalid_retries=1,
        game_version="v0.103.2", ascension=0)
    assert fixed_key != _evaluation_key(
        str(first), n_games=20, fixed_seeds=True, invalid_retries=1,
        game_version="v0.103.2", ascension=0)


def test_evaluation_key_distinguishes_game_version_and_ascension(tmp_path):
    checkpoint = str(tmp_path / "ppo_ironclad_100k.zip")
    base = _evaluation_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version="v0.103.2",
        ascension=0,
    )

    assert base != _evaluation_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version="v0.104.0",
        ascension=0,
    )
    assert base != _evaluation_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version="v0.103.2",
        ascension=10,
    )


def test_evaluation_key_ignores_source_when_version_value_and_ascension_match(
    tmp_path,
):
    checkpoint = str(tmp_path / "ppo_ironclad_100k.zip")
    cli = resolve_game_version("v0.103.2", {})
    environment = resolve_game_version(
        None, {"STS2_GAME_VERSION": "v0.103.2"}
    )

    cli_key = _evaluation_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version=cli.value,
        ascension=0,
    )
    environment_key = _evaluation_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version=environment.value,
        ascension=0,
    )

    assert cli.source == "cli"
    assert environment.source == "environment"
    assert cli_key == environment_key
    assert "game_version_source" not in json.loads(cli_key)


@pytest.mark.parametrize(
    ("cached_version", "cached_ascension", "launch_version", "launch_ascension"),
    [
        ("v0.102.0", 0, "v0.103.2", 0),
        ("v0.103.2", 0, "v0.103.2", 10),
    ],
)
def test_report_main_runs_evaluation_when_cached_game_semantics_change(
    monkeypatch,
    cached_version,
    cached_ascension,
    launch_version,
    launch_ascension,
):
    calls = []
    checkpoint = "checkpoints/model.zip"

    def semantic_key(
        checkpoint,
        *,
        n_games,
        fixed_seeds,
        invalid_retries,
        game_version,
        ascension,
    ):
        return json.dumps(
            {
                "checkpoint": os.path.abspath(checkpoint),
                "n_games": n_games,
                "fixed_seeds": fixed_seeds,
                "invalid_retries": invalid_retries,
                "game_version": game_version,
                "ascension": ascension,
            },
            sort_keys=True,
        )

    cached_key = semantic_key(
        checkpoint,
        n_games=10,
        fixed_seeds=True,
        invalid_retries=1,
        game_version=cached_version,
        ascension=cached_ascension,
    )

    class CachedFile:
        def __init__(self, value=""):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.value

        def write(self, value):
            self.value = value
            return len(value)

    def fake_open(path, mode="r"):
        return CachedFile(cached_key if "r" in mode else "")

    monkeypatch.setattr(
        eval_and_report.sys,
        "argv",
        [
            "eval_and_report.py",
            checkpoint,
            "--game-version",
            launch_version,
            "--ascension",
            str(launch_ascension),
        ],
    )
    monkeypatch.setattr(eval_and_report, "open", fake_open, raising=False)
    monkeypatch.setattr(eval_and_report.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(eval_and_report, "_evaluation_key", semantic_key)
    monkeypatch.setattr(
        eval_and_report,
        "run_eval",
        lambda checkpoint, **kwargs: calls.append((checkpoint, kwargs)) or {},
    )
    monkeypatch.setattr(eval_and_report, "format_report", lambda stats, checkpoint: "report")

    eval_and_report.main()

    assert len(calls) == 1
    assert calls[0][1]["game_version"] == launch_version
    assert calls[0][1]["ascension"] == launch_ascension
