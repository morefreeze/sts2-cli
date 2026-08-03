from __future__ import annotations

from pathlib import Path

import pytest

import agent.run_workbench.assets as node_assets
from agent.run_workbench.assets import (
    BOSS_ART_BY_MODEL,
    InvalidNodeArtModelError,
    NodeArtResolver,
    ROOM_ART,
    ROOM_FALLBACK,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "run_workbench"
ASSET_ROOT = FIXTURES / "map_assets"


def _resolver(*roots: Path) -> NodeArtResolver:
    return NodeArtResolver(explicit_roots=roots, environ={}, home=FIXTURES / "empty-home")


def test_room_art_and_fallback_contracts_are_stable() -> None:
    assert ROOM_ART == {
        "ancient": "ancient_node_neow.png",
        "monster": "map_monster.png",
        "elite": "map_elite.png",
        "boss": "map_chest_boss.png",
        "shop": "map_shop.png",
        "rest_site": "map_rest.png",
        "treasure": "map_chest.png",
        "unknown": "map_unknown.png",
    }
    assert ROOM_FALLBACK == {
        "ancient": ("🌀", "A", "远古事件"),
        "monster": ("⚔️", "M", "普通战斗"),
        "elite": ("👹", "E", "精英战斗"),
        "boss": ("💀", "B", "首领战斗"),
        "shop": ("🛒", "$", "商店"),
        "rest_site": ("🔥", "R", "休息点"),
        "treasure": ("🎁", "T", "宝箱"),
        "unknown": ("❓", "?", "未知事件"),
    }


def test_configured_png_wins_and_descriptor_does_not_expose_a_file_path() -> None:
    art = _resolver(ASSET_ROOT).resolve("Monster")

    assert art.kind == "original"
    assert art.image_path == ASSET_ROOT / "map_icons" / "map_monster.png"
    assert art.image_bytes == art.image_path.read_bytes()
    assert art.to_dict() == {
        "kind": "original",
        "room_type": "monster",
        "emoji": "⚔️",
        "letter": "M",
        "accessible_label": "普通战斗",
        "tooltip": "普通战斗",
        "image_url": "/api/node-art?room_type=monster",
    }
    assert str(ASSET_ROOT) not in str(art.to_dict())
    assert "image_path" not in art.to_dict()
    assert "image_bytes" not in art.to_dict()


def test_missing_known_art_falls_back_to_semantic_emoji_and_letter(tmp_path: Path) -> None:
    art = _resolver(tmp_path).resolve("elite")

    assert art.kind == "emoji"
    assert art.emoji == "👹"
    assert art.letter == "E"
    assert art.accessible_label == "精英战斗"
    assert art.image_path is None
    assert art.image_url is None


@pytest.mark.parametrize("room_type", ["unknown", "modded-room"])
def test_unknown_room_type_uses_letter_even_when_unknown_art_exists(
    tmp_path: Path, room_type: str
) -> None:
    icons = tmp_path / "map_icons"
    icons.mkdir(parents=True)
    (icons / "map_unknown.png").write_bytes(b"\x89PNG\r\n\x1a\nunknown")

    art = _resolver(tmp_path).resolve(room_type)

    assert art.kind == "letter"
    assert art.room_type == "unknown"
    assert art.letter == "?"
    assert art.accessible_label == "未知事件"
    assert art.image_path is None
    assert art.image_url is None


def test_explicit_env_and_dashboard_cache_roots_are_checked_in_order(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    title_cache = tmp_path / "Library/Application Support/STS2 Dashboard/Assets/images"
    lower_cache = tmp_path / "Library/Application Support/sts2-dashboard/Assets/images"
    for root, contents in (
        (explicit, b"\x89PNG\r\n\x1a\nexplicit"),
        (environment, b"\x89PNG\r\n\x1a\nenvironment"),
        (title_cache, b"\x89PNG\r\n\x1a\ntitle"),
        (lower_cache, b"\x89PNG\r\n\x1a\nlower"),
    ):
        icon_dir = root / "map_icons"
        icon_dir.mkdir(parents=True)
        (icon_dir / "map_elite.png").write_bytes(contents)

    resolver = NodeArtResolver(
        explicit_roots=[explicit],
        environ={"STS2_MAP_ASSET_DIR": str(environment)},
        home=tmp_path,
    )
    assert resolver.resolve("elite").image_path == explicit / "map_icons/map_elite.png"

    (explicit / "map_icons/map_elite.png").unlink()
    assert resolver.resolve("elite").image_path == environment / "map_icons/map_elite.png"
    (environment / "map_icons/map_elite.png").unlink()
    assert resolver.resolve("elite").image_path == title_cache / "map_icons/map_elite.png"
    (title_cache / "map_icons/map_elite.png").unlink()
    assert resolver.resolve("elite").image_path == lower_cache / "map_icons/map_elite.png"


def test_model_specific_boss_and_ancient_art_precede_generic_art(tmp_path: Path) -> None:
    icons = tmp_path / "map_icons"
    icons.mkdir(parents=True)
    for name in (
        "map_chest_boss.png",
        "vantom_boss_icon.png",
        "ancient_node_neow.png",
        "ancient_node_pael.png",
    ):
        (icons / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    resolver = _resolver(tmp_path)

    boss = resolver.resolve("boss", model_id="ENCOUNTER.VANTOM_BOSS")
    ancient = resolver.resolve("ancient", model_id="EVENT.PAEL")

    assert BOSS_ART_BY_MODEL["ENCOUNTER.VANTOM_BOSS"] == "vantom_boss_icon.png"
    assert boss.image_path == icons / "vantom_boss_icon.png"
    assert "model_id=ENCOUNTER.VANTOM_BOSS" in (boss.image_url or "")
    assert ancient.image_path == icons / "ancient_node_pael.png"
    assert "model_id=EVENT.PAEL" in (ancient.image_url or "")


def test_root_precedence_beats_later_model_specific_art(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    for root in (explicit, environment):
        (root / "map_icons").mkdir(parents=True)
    explicit_generic = explicit / "map_icons/map_chest_boss.png"
    explicit_generic.write_bytes(b"\x89PNG\r\n\x1a\nexplicit-generic")
    (environment / "map_icons/vantom_boss_icon.png").write_bytes(
        b"\x89PNG\r\n\x1a\nenvironment-model"
    )
    resolver = NodeArtResolver(
        explicit_roots=[explicit],
        environ={"STS2_MAP_ASSET_DIR": str(environment)},
        home=tmp_path / "home",
    )

    art = resolver.resolve("boss", model_id="ENCOUNTER.VANTOM_BOSS")

    assert art.image_path == explicit_generic
    assert art.image_url == "/api/node-art?room_type=boss"


@pytest.mark.parametrize(
    "model_id",
    [
        "../map_monster",
        "/tmp/map_monster",
        r"..\map_monster",
        "%2e%2e%2fmap_monster",
        "%252e%252e%252fmap_monster",
        "EVENT.NEOW/../../map_monster",
    ],
)
def test_model_specific_paths_reject_traversal(model_id: str, tmp_path: Path) -> None:
    with pytest.raises(InvalidNodeArtModelError):
        _resolver(tmp_path).resolve("ancient", model_id=model_id)


def test_unlisted_boss_model_uses_generic_known_icon(tmp_path: Path) -> None:
    icons = tmp_path / "map_icons"
    icons.mkdir(parents=True)
    generic = icons / "map_chest_boss.png"
    generic.write_bytes(b"\x89PNG\r\n\x1a\ngeneric")

    art = _resolver(tmp_path).resolve("boss", model_id="ENCOUNTER.MODDED_BOSS")

    assert art.kind == "original"
    assert art.image_path == generic
    assert art.image_url == "/api/node-art?room_type=boss"


def test_non_png_and_symlink_escape_are_not_streamable(tmp_path: Path) -> None:
    icons = tmp_path / "map_icons"
    icons.mkdir(parents=True)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"not png")
    (icons / "map_monster.png").symlink_to(outside)

    art = _resolver(tmp_path).resolve("monster")

    assert art.kind == "emoji"
    assert art.image_path is None


def test_oversized_png_is_rejected_before_an_unbounded_read(tmp_path: Path) -> None:
    icons = tmp_path / "map_icons"
    icons.mkdir(parents=True)
    candidate = icons / "map_monster.png"
    limit = getattr(node_assets, "MAX_NODE_ART_BYTES", 4096)
    with candidate.open("wb") as stream:
        stream.write(b"\x89PNG\r\n\x1a\n")
        stream.seek(limit)
        stream.write(b"x")

    art = _resolver(tmp_path).resolve("monster")

    assert hasattr(node_assets, "MAX_NODE_ART_BYTES")
    assert art.kind == "emoji"
    assert art.image_path is None
