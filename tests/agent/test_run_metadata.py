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
