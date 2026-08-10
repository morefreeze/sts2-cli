"""Shared validation for metadata that identifies a training or evaluation run."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


MAX_GAME_VERSION_LENGTH = 128
GameVersionSource = Literal["cli", "environment"]


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
    if len(text) > MAX_GAME_VERSION_LENGTH:
        raise ValueError(
            f"{name} must be at most {MAX_GAME_VERSION_LENGTH} characters"
        )
    return text or None


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
