"""Safe local artwork resolution for STS2 map nodes.

Only the fixed dashboard cache layout and fixed/validated basenames are ever
considered.  The public descriptor deliberately omits local filesystem paths;
callers that need bytes use :attr:`NodeArt.image_path` internally.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlencode


ROOM_ART = {
    "ancient": "ancient_node_neow.png",
    "monster": "map_monster.png",
    "elite": "map_elite.png",
    "boss": "map_chest_boss.png",
    "shop": "map_shop.png",
    "rest_site": "map_rest.png",
    "treasure": "map_chest.png",
    "unknown": "map_unknown.png",
}

ROOM_FALLBACK = {
    "ancient": ("🌀", "A", "远古事件"),
    "monster": ("⚔️", "M", "普通战斗"),
    "elite": ("👹", "E", "精英战斗"),
    "boss": ("💀", "B", "首领战斗"),
    "shop": ("🛒", "$", "商店"),
    "rest_site": ("🔥", "R", "休息点"),
    "treasure": ("🎁", "T", "宝箱"),
    "unknown": ("❓", "?", "未知事件"),
}

# Matches the filenames produced by the pinned upstream dashboard's
# ``extract_map_assets.js`` / ``render_svg.js`` pair.  Keeping this as an
# allowlist prevents a model id from becoming a filesystem basename.
BOSS_ART_BY_MODEL = {
    "ENCOUNTER.VANTOM_BOSS": "vantom_boss_icon.png",
    "ENCOUNTER.CEREMONIAL_BEAST_BOSS": "ceremonial_beast_boss_icon.png",
    "ENCOUNTER.THE_KIN_BOSS": "the_kin_boss_icon.png",
    "ENCOUNTER.LAGAVULIN_MATRIARCH_BOSS": "lagavulin_matriarch_boss_icon.png",
    "ENCOUNTER.SOUL_FYSH_BOSS": "soul_fysh_boss_icon.png",
    "ENCOUNTER.WATERFALL_GIANT_BOSS": "waterfall_giant_boss_icon.png",
    "ENCOUNTER.KAISER_CRAB_BOSS": "kaiser_crab_boss_icon.png",
    "ENCOUNTER.KNOWLEDGE_DEMON_BOSS": "knowledge_demon_boss_icon.png",
    "ENCOUNTER.THE_INSATIABLE_BOSS": "the_insatiable_boss_icon.png",
    "ENCOUNTER.DOORMAKER_BOSS": "doormaker_boss_icon.png",
    "ENCOUNTER.QUEEN_BOSS": "false_queen_boss_icon.png",
    "ENCOUNTER.TEST_SUBJECT_BOSS": "test_subject_boss_icon.png",
}

_ROOM_ALIASES = {
    "restsite": "rest_site",
    "rest-site": "rest_site",
    "rest site": "rest_site",
}
_SAFE_MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_ANCIENT_MODEL_ID = re.compile(r"^EVENT\.([A-Z0-9_]+)$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class InvalidNodeArtModelError(ValueError):
    """A model id could be interpreted as a path or unsafe basename."""


@dataclass(frozen=True)
class NodeArt:
    kind: Literal["original", "emoji", "letter"]
    room_type: str
    emoji: str
    letter: str
    accessible_label: str
    tooltip: str
    image_url: str | None = None
    image_path: Path | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Return the browser-safe descriptor without exposing a local path."""

        return {
            "kind": self.kind,
            "room_type": self.room_type,
            "emoji": self.emoji,
            "letter": self.letter,
            "accessible_label": self.accessible_label,
            "tooltip": self.tooltip,
            "image_url": self.image_url,
        }


class NodeArtResolver:
    """Resolve map-node art from explicit and compatible dashboard caches."""

    def __init__(
        self,
        explicit_roots: Iterable[Path | str] | Path | str | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> None:
        if isinstance(explicit_roots, (str, Path)):
            configured_roots: tuple[Path | str, ...] = (explicit_roots,)
        else:
            configured_roots = tuple(explicit_roots or ())
        environment = os.environ if environ is None else environ
        resolved_home = Path.home() if home is None else Path(home)

        ordered: list[Path] = [
            _expand_root(root, resolved_home) for root in configured_roots
        ]
        env_root = environment.get("STS2_MAP_ASSET_DIR")
        if env_root:
            ordered.append(_expand_root(env_root, resolved_home))
        ordered.extend(
            [
                resolved_home
                / "Library"
                / "Application Support"
                / "STS2 Dashboard"
                / "Assets"
                / "images",
                resolved_home
                / "Library"
                / "Application Support"
                / "sts2-dashboard"
                / "Assets"
                / "images",
            ]
        )
        self.roots = tuple(dict.fromkeys(ordered))

    def resolve(self, room_type: str, model_id: str | None = None) -> NodeArt:
        normalized_room = _normalize_room_type(room_type)
        normalized_model = _validate_model_id(model_id)
        emoji, letter, label = ROOM_FALLBACK[normalized_room]

        for filename, selected_model in _candidate_filenames(
            normalized_room, normalized_model
        ):
            image_path = self._find_png(filename)
            if image_path is None:
                continue
            query: dict[str, str] = {"room_type": normalized_room}
            if selected_model is not None:
                query["model_id"] = selected_model
            return NodeArt(
                kind="original",
                room_type=normalized_room,
                emoji=emoji,
                letter=letter,
                accessible_label=label,
                tooltip=label,
                image_url=f"/api/node-art?{urlencode(query)}",
                image_path=image_path,
            )

        kind: Literal["emoji", "letter"] = (
            "letter" if normalized_room == "unknown" else "emoji"
        )
        return NodeArt(
            kind=kind,
            room_type=normalized_room,
            emoji=emoji,
            letter=letter,
            accessible_label=label,
            tooltip=label,
        )

    def _find_png(self, filename: str) -> Path | None:
        # Every filename originates in a constant mapping or a sanitized
        # ancient basename.  Still enforce basename-only here as a final guard.
        if Path(filename).name != filename or not filename.endswith(".png"):
            return None
        for root in self.roots:
            icon_root = root if root.name == "map_icons" else root / "map_icons"
            try:
                resolved_root = icon_root.resolve(strict=False)
                candidate = (icon_root / filename).resolve(strict=True)
                candidate.relative_to(resolved_root)
                if not candidate.is_file():
                    continue
                with candidate.open("rb") as stream:
                    if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                        continue
            except (OSError, ValueError):
                continue
            return candidate
        return None


def _expand_root(value: Path | str, home: Path) -> Path:
    raw = str(value)
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(value)


def _normalize_room_type(value: str) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower()
    normalized = _ROOM_ALIASES.get(normalized, normalized)
    return normalized if normalized in ROOM_ART else "unknown"


def _validate_model_id(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidNodeArtModelError("model_id must be a string")
    if (
        "%" in value
        or "/" in value
        or "\\" in value
        or ".." in value
        or "\x00" in value
        or not _SAFE_MODEL_ID.fullmatch(value)
    ):
        raise InvalidNodeArtModelError("invalid node-art model_id")
    return value.upper()


def _candidate_filenames(
    room_type: str, model_id: str | None
) -> tuple[tuple[str, str | None], ...]:
    candidates: list[tuple[str, str | None]] = []
    if room_type == "boss" and model_id in BOSS_ART_BY_MODEL:
        candidates.append((BOSS_ART_BY_MODEL[model_id], model_id))
    elif room_type == "ancient" and model_id is not None:
        match = _ANCIENT_MODEL_ID.fullmatch(model_id)
        if match is not None:
            basename = match.group(1).lower()
            candidates.append((f"ancient_node_{basename}.png", model_id))
    candidates.append((ROOM_ART[room_type], None))
    return tuple(dict.fromkeys(candidates))


__all__ = [
    "BOSS_ART_BY_MODEL",
    "InvalidNodeArtModelError",
    "NodeArt",
    "NodeArtResolver",
    "ROOM_ART",
    "ROOM_FALLBACK",
]
