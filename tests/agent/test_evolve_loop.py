import io
from types import SimpleNamespace

import pytest

import agent.evolve_loop as evolve_loop
from agent.evolve_loop import parse_eval_reach
from agent.run_metadata import ResolvedGameVersion


def test_parse_eval_reach_accepts_act_floor_labels():
    text = """
avg_floor      : 13.0
floor dist     : [A1F7, A1F17, A2F5]
"""

    assert parse_eval_reach(text) == (13.0, 2)


def test_parse_eval_reach_keeps_legacy_numeric_floor_dist():
    text = """
avg_floor      : 12.5
floor dist     : [7, 17, 22]
"""

    assert parse_eval_reach(text) == (12.5, 2)


def test_evolve_main_requires_version_before_any_subprocess(monkeypatch):
    subprocess_calls = []
    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    monkeypatch.setattr(
        evolve_loop.sys,
        "argv",
        ["evolve_loop.py", "--rounds", "0"],
    )
    monkeypatch.setattr(
        evolve_loop.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    with pytest.raises(SystemExit):
        evolve_loop.main()

    assert subprocess_calls == []


@pytest.mark.parametrize(
    ("source", "expects_version_flag"),
    [("cli", True), ("environment", False)],
)
def test_evolve_train_and_eval_commands_preserve_version_source_and_ascension(
    monkeypatch, tmp_path, source, expects_version_flag
):
    commands = []
    resolved = ResolvedGameVersion("v0.103.2", source)
    monkeypatch.setattr(
        evolve_loop,
        "run",
        lambda cmd, timeout, log_path: commands.append(cmd) or 0,
    )
    monkeypatch.setattr(evolve_loop, "kill_orphans", lambda: None)
    monkeypatch.setattr(
        evolve_loop,
        "latest_ckpt",
        lambda out_dir: str(tmp_path / "trained.zip"),
    )

    evolve_loop.train_chunk(
        "base.zip",
        str(tmp_path / "round"),
        25,
        0.08,
        1,
        game_version=resolved,
        ascension=10,
    )
    monkeypatch.setattr(
        evolve_loop,
        "open",
        lambda *args, **kwargs: io.StringIO(
            "avg_floor : 7.0\nfloor dist : [A1F7]"
        ),
        raising=False,
    )
    evolve_loop.eval_reach(
        "trained.zip",
        1,
        1,
        game_version=resolved,
        ascension=10,
    )

    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--ascension") + 1] == "10"
        assert ("--game-version" in command) is expects_version_flag
        if expects_version_flag:
            assert command[command.index("--game-version") + 1] == "v0.103.2"


def test_evolve_subprocess_inherits_current_environment_version(
    monkeypatch, tmp_path
):
    subprocess_kwargs = []
    monkeypatch.setenv("STS2_GAME_VERSION", "v0.103.2")
    monkeypatch.setattr(
        evolve_loop.subprocess,
        "run",
        lambda *args, **kwargs: (
            subprocess_kwargs.append(kwargs) or SimpleNamespace(returncode=0)
        ),
    )

    assert evolve_loop.run(
        ["child"], timeout=1, log_path=str(tmp_path / "child.log")
    ) == 0

    assert subprocess_kwargs[0]["env"]["STS2_GAME_VERSION"] == "v0.103.2"


def test_evolve_eval_nonzero_exit_is_explicit_failure_before_log_parse(
    monkeypatch,
):
    monkeypatch.setattr(evolve_loop, "run", lambda *args, **kwargs: 2)
    monkeypatch.setattr(evolve_loop, "kill_orphans", lambda: None)
    monkeypatch.setattr(
        evolve_loop,
        "open",
        lambda *args, **kwargs: pytest.fail("failed eval log must not be parsed"),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="rc=2"):
        evolve_loop.eval_reach(
            "trained.zip",
            1,
            1,
            game_version=ResolvedGameVersion("v0.103.2", "cli"),
            ascension=0,
        )


def test_evolve_train_nonzero_exit_rejects_partial_checkpoint(
    monkeypatch, tmp_path
):
    partial_checkpoint = str(tmp_path / "partial.zip")
    monkeypatch.setattr(evolve_loop, "run", lambda *args, **kwargs: 2)
    monkeypatch.setattr(evolve_loop, "kill_orphans", lambda: None)
    monkeypatch.setattr(
        evolve_loop,
        "latest_ckpt",
        lambda out_dir: partial_checkpoint,
    )

    result = evolve_loop.train_chunk(
        "base.zip",
        str(tmp_path / "round"),
        25,
        0.08,
        1,
        game_version=ResolvedGameVersion("v0.103.2", "cli"),
        ascension=0,
    )

    assert result is None


def test_evolve_sentinel_nonzero_exit_rejects_partial_passing_log(monkeypatch):
    monkeypatch.setattr(evolve_loop, "run", lambda *args, **kwargs: 2)
    monkeypatch.setattr(evolve_loop, "kill_orphans", lambda: None)
    monkeypatch.setattr(
        evolve_loop,
        "open",
        lambda *args, **kwargs: io.StringIO("hp=72 win 15/15"),
        raising=False,
    )

    assert evolve_loop.sentinel_clutch("partial.zip", 1) is False


@pytest.mark.parametrize(
    ("cli_args", "environment", "expected_source"),
    [
        (["--game-version", "v0.103.2"], {}, "cli"),
        ([], {"STS2_GAME_VERSION": "v0.103.2"}, "environment"),
    ],
)
def test_evolve_main_passes_resolved_metadata_to_train_and_eval(
    monkeypatch, cli_args, environment, expected_source
):
    calls = []
    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        evolve_loop.sys,
        "argv",
        [
            "evolve_loop.py",
            "--rounds",
            "1",
            "--ascension",
            "10",
            *cli_args,
        ],
    )
    monkeypatch.setattr(evolve_loop.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(evolve_loop.os.path, "exists", lambda path: False)
    monkeypatch.setattr(
        evolve_loop,
        "train_chunk",
        lambda *args, **kwargs: calls.append(("train", kwargs)) or "trained.zip",
    )
    monkeypatch.setattr(evolve_loop, "sentinel_clutch", lambda *args: True)
    monkeypatch.setattr(
        evolve_loop,
        "eval_reach",
        lambda *args, **kwargs: calls.append(("eval", kwargs)) or (7.0, 0),
    )
    monkeypatch.setattr(
        evolve_loop,
        "open",
        lambda *args, **kwargs: io.StringIO(),
        raising=False,
    )

    evolve_loop.main()

    assert [kind for kind, _ in calls] == ["train", "eval"]
    for _, kwargs in calls:
        assert kwargs["game_version"] == ResolvedGameVersion(
            "v0.103.2", expected_source
        )
        assert kwargs["ascension"] == 10
