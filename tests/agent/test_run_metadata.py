from dataclasses import FrozenInstanceError
import json

import pytest
import agent.run_metadata as run_metadata

from agent.run_metadata import (
    MAX_GAME_VERSION_LENGTH,
    ResolvedGameVersion,
    resolve_game_version,
    validate_ascension,
)


def test_cli_game_version_takes_precedence_and_is_trimmed() -> None:
    assert resolve_game_version(
        "  v0.103.2  ", {"STS2_GAME_VERSION": "v0.102.0"}
    ) == ResolvedGameVersion("v0.103.2", "cli")


@pytest.mark.parametrize("cli_value", [None, "", " \t\n"])
def test_environment_game_version_is_used_when_cli_is_missing_or_blank(
    cli_value: str | None,
) -> None:
    assert resolve_game_version(
        cli_value, {"STS2_GAME_VERSION": "  v0.102.0  "}
    ) == ResolvedGameVersion("v0.102.0", "environment")


def test_process_environment_is_used_when_environment_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STS2_GAME_VERSION", "  v0.101.0  ")

    assert resolve_game_version(None, None) == ResolvedGameVersion(
        "v0.101.0", "environment"
    )


@pytest.mark.parametrize(
    "cli_value, environment",
    [
        (None, {}),
        ("", {}),
        (" \t", {"STS2_GAME_VERSION": " \n"}),
    ],
)
def test_missing_game_version_reports_both_configuration_options(
    cli_value: str | None, environment: dict[str, str]
) -> None:
    with pytest.raises(ValueError) as error:
        resolve_game_version(cli_value, environment)

    assert "--game-version" in str(error.value)
    assert "STS2_GAME_VERSION" in str(error.value)


@pytest.mark.parametrize("cli_value", [1, 1.0, True, b"v0.103.2"])
def test_explicit_non_string_cli_game_version_is_rejected(
    cli_value: object,
) -> None:
    with pytest.raises(ValueError, match="string"):
        resolve_game_version(cli_value, {"STS2_GAME_VERSION": "v0.102.0"})


def test_non_string_environment_game_version_is_rejected() -> None:
    with pytest.raises(ValueError, match="string"):
        resolve_game_version(None, {"STS2_GAME_VERSION": 103})  # type: ignore[dict-item]


def test_game_version_accepts_the_named_length_limit() -> None:
    value = "v" * MAX_GAME_VERSION_LENGTH

    assert resolve_game_version(value, {}) == ResolvedGameVersion(value, "cli")


def test_game_version_rejects_text_beyond_the_named_length_limit() -> None:
    with pytest.raises(ValueError, match=str(MAX_GAME_VERSION_LENGTH)):
        resolve_game_version("v" * (MAX_GAME_VERSION_LENGTH + 1), {})


def test_game_version_rejects_non_unicode_scalar_text() -> None:
    with pytest.raises(ValueError, match="Unicode scalar"):
        resolve_game_version(json.loads('"\\ud800"'), {})


def test_build_run_context_validates_and_returns_fresh_snapshots() -> None:
    version = ResolvedGameVersion("v0.103.2", "environment")

    first = run_metadata.build_run_context(
        version,
        character="Ironclad",
        checkpoint="model.zip",
        evaluation_mode="boss_retry",
        scenario="native_save",
        seed=None,
    )
    second = run_metadata.build_run_context(
        version,
        character="Ironclad",
        checkpoint="model.zip",
        evaluation_mode="boss_retry",
        scenario="native_save",
        seed=None,
    )

    assert first == {
        "game_version": "v0.103.2",
        "game_version_source": "environment",
        "character": "Ironclad",
        "checkpoint": "model.zip",
        "evaluation_mode": "boss_retry",
        "scenario": "native_save",
        "seed": None,
    }
    assert first is not second
    with pytest.raises(ValueError, match="ResolvedGameVersion"):
        run_metadata.build_run_context(
            None,
            character="Ironclad",
            evaluation_mode="boss_retry",
            scenario="native_save",
        )


def test_load_native_save_seed_reads_only_bounded_safe_exact_sidecar(tmp_path) -> None:
    save = tmp_path / "boss.save"
    save.write_text("save", encoding="utf-8")
    sidecar = tmp_path / "boss.save.meta.json"
    sidecar.write_text(json.dumps({"seed": "seed-exact"}), encoding="utf-8")

    assert run_metadata.load_native_save_seed(str(save)) == "seed-exact"

    sidecar.write_text(json.dumps({"seed": json.loads('"\\ud800"')}), encoding="utf-8")
    assert run_metadata.load_native_save_seed(str(save)) is None

    sidecar.write_text('{"seed":"' + ("x" * 70_000) + '"}', encoding="utf-8")
    assert run_metadata.load_native_save_seed(str(save)) is None


def test_resolved_game_version_exposes_serializable_fields_and_is_frozen() -> None:
    resolved = ResolvedGameVersion("v0.103.2", "cli")

    assert resolved.to_fields() == {
        "game_version": "v0.103.2",
        "game_version_source": "cli",
    }
    with pytest.raises(FrozenInstanceError):
        resolved.value = "v0.104.0"  # type: ignore[misc]


@pytest.mark.parametrize("ascension", [0, 10])
def test_valid_ascension_is_returned(ascension: int) -> None:
    assert validate_ascension(ascension) == ascension


@pytest.mark.parametrize("ascension", [True, -1, 11, 1.0, "1"])
def test_invalid_ascension_is_rejected(ascension: object) -> None:
    with pytest.raises(ValueError, match=r"0\.\.10"):
        validate_ascension(ascension)


def test_experiment_from_directory_names_the_training_run() -> None:
    """Training knows its save directory, so it names the experiment from that
    rather than from a checkpoint file inside it."""
    from agent.run_metadata import experiment_from_directory

    assert experiment_from_directory("checkpoints_noadvisor_mix") == "noadvisor_mix"
    assert experiment_from_directory("/abs/path/checkpoints_act2_boss") == "act2_boss"
    # A trailing separator must not erase the name.
    assert experiment_from_directory("checkpoints_best/") == "best"
    # The bare default directory is not an experiment.
    assert experiment_from_directory("checkpoints") is None
    assert experiment_from_directory(None) is None
    assert experiment_from_directory("   ") is None


def test_experiment_from_directory_and_checkpoint_agree() -> None:
    """Training labels from the directory, eval from a checkpoint inside it.
    They must produce the same label or the same training run would split into
    two batches in the workbench."""
    from agent.run_metadata import (
        experiment_from_checkpoint,
        experiment_from_directory,
    )

    assert experiment_from_directory("checkpoints_noadvisor_mix") == (
        experiment_from_checkpoint("checkpoints_noadvisor_mix/ppo_ironclad_13255k.zip")
    )


# --- release_info fallback (python/play_full_run.py) -------------------------
#
# Direct game runs (play_full_run) have no --game-version habit, so when neither
# the CLI nor STS2_GAME_VERSION names a version the game install's own
# release_info.json (one directory ABOVE STS2_GAME_DIR, next to the .pck) is the
# authoritative record of what was played.


def _write_release_info(tmp_path, payload) -> str:
    """Lay out <tmp>/Resources/{release_info.json,data_x}; return the data dir."""
    resources = tmp_path / "Resources"
    data_dir = resources / "data_sts2_macos_arm64"
    data_dir.mkdir(parents=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (resources / "release_info.json").write_text(text, encoding="utf-8")
    return str(data_dir)


def test_release_info_version_is_read_from_the_dir_above_the_game_dir(tmp_path) -> None:
    game_dir = _write_release_info(
        tmp_path, {"commit": "41cef1ea", "version": "v0.111.0", "branch": "v0.111.0"}
    )

    assert run_metadata.read_release_info_version(game_dir) == "v0.111.0"


def test_release_info_game_dir_may_carry_a_trailing_separator(tmp_path) -> None:
    game_dir = _write_release_info(tmp_path, {"version": "v0.111.0"})

    assert run_metadata.read_release_info_version(game_dir + "/") == "v0.111.0"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"version": 111}',
        '{"version": "  "}',
        '{"commit": "41cef1ea"}',
        json.dumps({"version": "v" * (MAX_GAME_VERSION_LENGTH + 1)}),
        '{"version": "\\ud800"}',
    ],
    ids=["not-json", "not-object", "non-string", "blank", "absent", "too-long", "surrogate"],
)
def test_release_info_with_no_usable_version_reads_as_none(tmp_path, payload) -> None:
    assert run_metadata.read_release_info_version(
        _write_release_info(tmp_path, payload)
    ) is None


def test_release_info_missing_file_or_game_dir_reads_as_none(tmp_path) -> None:
    assert run_metadata.read_release_info_version(str(tmp_path / "nowhere" / "data")) is None
    assert run_metadata.read_release_info_version(None) is None
    assert run_metadata.read_release_info_version("") is None


def test_release_info_oversized_file_is_not_trusted(tmp_path) -> None:
    padding = " " * (run_metadata.MAX_RELEASE_INFO_BYTES + 1)
    game_dir = _write_release_info(tmp_path, '{"version": "v0.111.0"}' + padding)

    assert run_metadata.read_release_info_version(game_dir) is None


def test_optional_resolution_order_is_cli_then_environment_then_release_info(
    tmp_path,
) -> None:
    game_dir = _write_release_info(tmp_path, {"version": "v0.111.0"})
    env = {"STS2_GAME_VERSION": "v0.102.0", "STS2_GAME_DIR": game_dir}

    assert run_metadata.resolve_optional_game_version("v0.103.2", env) == ResolvedGameVersion(
        "v0.103.2", "cli"
    )
    assert run_metadata.resolve_optional_game_version(None, env) == ResolvedGameVersion(
        "v0.102.0", "environment"
    )
    assert run_metadata.resolve_optional_game_version(
        None, {"STS2_GAME_DIR": game_dir}
    ) == ResolvedGameVersion("v0.111.0", "release_info")
    assert run_metadata.resolve_optional_game_version(
        " ", {"STS2_GAME_VERSION": "  ", "STS2_GAME_DIR": game_dir}
    ) == ResolvedGameVersion("v0.111.0", "release_info")


def test_optional_resolution_returns_none_instead_of_raising(tmp_path) -> None:
    assert run_metadata.resolve_optional_game_version(None, {}) is None
    assert run_metadata.resolve_optional_game_version(
        None, {"STS2_GAME_DIR": str(tmp_path / "nowhere" / "data")}
    ) is None


def test_optional_resolution_still_rejects_an_invalid_explicit_value() -> None:
    # An explicit value that is wrong is a typo to surface, not a reason to
    # silently fall through to the install's version.
    with pytest.raises(ValueError, match="--game-version"):
        run_metadata.resolve_optional_game_version("v" * (MAX_GAME_VERSION_LENGTH + 1), {})
    with pytest.raises(ValueError, match="STS2_GAME_VERSION"):
        run_metadata.resolve_optional_game_version(
            None, {"STS2_GAME_VERSION": "v" * (MAX_GAME_VERSION_LENGTH + 1)}
        )


def test_explicit_game_dir_argument_overrides_the_environment(tmp_path) -> None:
    game_dir = _write_release_info(tmp_path, {"version": "v0.111.0"})

    assert run_metadata.resolve_optional_game_version(
        None, {"STS2_GAME_DIR": "/nowhere"}, game_dir=game_dir
    ) == ResolvedGameVersion("v0.111.0", "release_info")


def test_release_info_source_never_satisfies_the_strict_run_context(tmp_path) -> None:
    """train/eval keep demanding an explicit cli/environment version: the
    automatic source is for direct play_full_run logs only."""
    resolved = run_metadata.resolve_optional_game_version(
        None, {"STS2_GAME_DIR": _write_release_info(tmp_path, {"version": "v0.111.0"})}
    )

    with pytest.raises(ValueError, match="game_version"):
        run_metadata.build_run_context(
            resolved, character="Ironclad", evaluation_mode="fixed", scenario="full_run"
        )
