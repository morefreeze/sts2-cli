"""Safe local artwork resolution for STS2 map nodes.

Only the fixed dashboard cache layout and fixed/validated basenames are ever
considered.  The public descriptor deliberately omits local filesystem paths
and verified bytes; the HTTP boundary serves :attr:`NodeArt.image_bytes`
without reopening :attr:`NodeArt.image_path`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
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
MAX_NODE_ART_BYTES = 8 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


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
    image_bytes: bytes | None = None

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

        if normalized_room != "unknown":
            filenames = _candidate_filenames(normalized_room, normalized_model)
            for root in self.roots:
                for filename, selected_model in filenames:
                    loaded = self._load_png(root, filename)
                    if loaded is None:
                        continue
                    image_path, image_bytes = loaded
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
                        image_bytes=image_bytes,
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

    def _load_png(self, root: Path, filename: str) -> tuple[Path, bytes] | None:
        # Every filename originates in a constant mapping or a sanitized
        # ancient basename.  Still enforce basename-only here as a final guard.
        if Path(filename).name != filename or not filename.endswith(".png"):
            return None
        icon_root = root if root.name == "map_icons" else root / "map_icons"
        directory_fd: int | None = None
        image_fd: int | None = None
        try:
            resolved_root = icon_root.resolve(strict=True)
            if not resolved_root.is_dir():
                return None
            directory_flags = os.O_RDONLY
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            directory_flags |= getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(resolved_root, directory_flags)

            image_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            image_flags |= nofollow
            before = None
            if not nofollow:
                before = os.stat(
                    filename, dir_fd=directory_fd, follow_symlinks=False
                )
                if not stat.S_ISREG(before.st_mode):
                    return None
            image_fd = os.open(filename, image_flags, dir_fd=directory_fd)
            file_stat = os.fstat(image_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                return None
            if before is not None and (
                before.st_dev != file_stat.st_dev
                or before.st_ino != file_stat.st_ino
            ):
                return None
            if not len(_PNG_SIGNATURE) <= file_stat.st_size <= MAX_NODE_ART_BYTES:
                return None
            image_bytes = _read_bounded(image_fd, file_stat.st_size)
            if image_bytes is None or not image_bytes.startswith(_PNG_SIGNATURE):
                return None
            after = os.fstat(image_fd)
            if (
                after.st_dev != file_stat.st_dev
                or after.st_ino != file_stat.st_ino
                or after.st_size != file_stat.st_size
            ):
                return None
        except (OSError, ValueError):
            return None
        finally:
            if image_fd is not None:
                os.close(image_fd)
            if directory_fd is not None:
                os.close(directory_fd)
        return resolved_root / filename, image_bytes


def _read_bounded(file_descriptor: int, expected_size: int) -> bytes | None:
    """Read one opened regular file without exceeding the fixed art budget."""

    chunks: list[bytes] = []
    total = 0
    while total <= MAX_NODE_ART_BYTES:
        remaining_budget = MAX_NODE_ART_BYTES + 1 - total
        chunk = os.read(
            file_descriptor, min(_READ_CHUNK_BYTES, remaining_budget)
        )
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > MAX_NODE_ART_BYTES or total != expected_size:
        return None
    return b"".join(chunks)


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
    "MAX_NODE_ART_BYTES",
    "NodeArt",
    "NodeArtResolver",
    "ROOM_ART",
    "ROOM_FALLBACK",
]
