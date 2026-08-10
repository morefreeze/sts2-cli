"""Shared validation for metadata that identifies a training or evaluation run."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


MAX_GAME_VERSION_LENGTH = 128
MAX_NATIVE_SAVE_METADATA_BYTES = 64 * 1024
MAX_RUN_ID_LENGTH = 256
MAX_SEED_LENGTH = 256
GameVersionSource = Literal["cli", "environment"]
_SEED_UNSET = object()


def is_unicode_scalar_text(value: object) -> bool:
    """Return whether ``value`` is text that strict UTF-8 can represent."""

    if type(value) is not str:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def safe_run_id(value: object) -> str | None:
    """Return a bounded, nonempty run identifier safe for public JSON."""

    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_RUN_ID_LENGTH
        or not is_unicode_scalar_text(value)
    ):
        return None
    return value


def record_run_id(record: Mapping[str, object]) -> str | None:
    """Choose the first safe run identifier from canonical record locations."""

    top_level = safe_run_id(record.get("run_id"))
    if top_level is not None:
        return top_level
    data = record.get("data")
    if isinstance(data, Mapping):
        return safe_run_id(data.get("run_id"))
    return None


@dataclass(frozen=True)
class ResolvedGameVersion:
    value: str
    source: GameVersionSource

    def to_fields(self) -> dict[str, str]:
        return {
            "game_version": self.value,
            "game_version_source": self.source,
        }


def _nonempty_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")

    text = value.strip()
    if not is_unicode_scalar_text(text):
        raise ValueError(f"{name} must contain only Unicode scalar values")
    if len(text) > MAX_GAME_VERSION_LENGTH:
        raise ValueError(
            f"{name} must be at most {MAX_GAME_VERSION_LENGTH} characters"
        )
    return text or None


def build_run_context(
    game_version: ResolvedGameVersion | None,
    *,
    character: str,
    evaluation_mode: str,
    scenario: str,
    checkpoint: str | None = None,
    seed: str | None | object = _SEED_UNSET,
    ascension: int | None = None,
) -> dict[str, object]:
    """Build one fresh, validated metadata snapshot for a direct game run."""

    if not isinstance(game_version, ResolvedGameVersion):
        raise ValueError(
            "game_version must be a ResolvedGameVersion from resolve_game_version"
        )
    version = _nonempty_text(game_version.value, name="game_version")
    if version is None or game_version.source not in {"cli", "environment"}:
        raise ValueError("game_version must be a valid ResolvedGameVersion")
    resolved_character = _nonempty_text(character, name="character")
    resolved_mode = _nonempty_text(evaluation_mode, name="evaluation_mode")
    resolved_scenario = _nonempty_text(scenario, name="scenario")
    resolved_checkpoint = _nonempty_text(checkpoint, name="checkpoint")
    if (
        resolved_character is None
        or resolved_mode is None
        or resolved_scenario is None
    ):
        raise ValueError("character, evaluation_mode, and scenario are required")
    context: dict[str, object] = {
        "game_version": version,
        "game_version_source": game_version.source,
        "character": resolved_character,
        "checkpoint": resolved_checkpoint,
        "evaluation_mode": resolved_mode,
        "scenario": resolved_scenario,
    }
    if ascension is not None:
        context["ascension"] = validate_ascension(ascension)
    if seed is not _SEED_UNSET:
        if seed is not None:
            if type(seed) is not str or not seed.strip():
                raise ValueError("seed must be a non-empty string or None")
            if len(seed) > MAX_SEED_LENGTH or not is_unicode_scalar_text(seed):
                raise ValueError("seed must be bounded Unicode scalar text or None")
        context["seed"] = seed
    return context


def load_native_save_seed(save_path: str | os.PathLike[str]) -> str | None:
    """Read an authoritative seed from ``<save>.meta.json`` without guessing."""

    try:
        metadata_path = Path(f"{os.fspath(save_path)}.meta.json")
        with metadata_path.open("rb") as handle:
            payload = handle.read(MAX_NATIVE_SAVE_METADATA_BYTES + 1)
        if len(payload) > MAX_NATIVE_SAVE_METADATA_BYTES:
            return None
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    seed = value.get("seed")
    if (
        type(seed) is not str
        or not seed.strip()
        or len(seed) > MAX_SEED_LENGTH
        or not is_unicode_scalar_text(seed)
    ):
        return None
    return seed


def resolve_game_version(
    cli_value: str | None, environment: Mapping[str, str] | None = None
) -> ResolvedGameVersion:
    cli_version = _nonempty_text(cli_value, name="--game-version")
    if cli_version is not None:
        return ResolvedGameVersion(cli_version, "cli")

    values = os.environ if environment is None else environment
    environment_version = _nonempty_text(
        values.get("STS2_GAME_VERSION"), name="STS2_GAME_VERSION"
    )
    if environment_version is not None:
        return ResolvedGameVersion(environment_version, "environment")

    raise ValueError(
        "game version is required; pass --game-version or set STS2_GAME_VERSION"
    )


def validate_ascension(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 10:
        raise ValueError("ascension must be an integer in the range 0..10")
    return value
