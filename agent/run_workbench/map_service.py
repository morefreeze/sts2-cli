"""Version-gated reconstruction of STS2 act maps.

The vendored JavaScript generator is treated as an isolated deterministic
engine.  This module owns the public Python contract, supported-build gate,
subprocess failures, output validation, caching, and the conservative
visited-route fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from .models import ActMap, MapAlignment, MapEdge, MapNode


SUPPORTED_MAP_BUILDS = frozenset({"v0.103.2"})


class MapServiceError(RuntimeError):
    """Base class for operational map reconstruction failures."""


class MapServiceTimeoutError(MapServiceError):
    """The Node map generator exceeded its fixed time budget."""


class MapExecutableNotFoundError(MapServiceError):
    """The configured Node executable could not be started."""


class MapSubprocessError(MapServiceError):
    """The Node map generator exited unsuccessfully."""


class MapOutputError(MapServiceError):
    """The Node map generator returned an invalid response contract."""


@dataclass(frozen=True)
class MapRequest:
    run_id: str
    act_id: str
    act_index: int
    seed: str | None
    game_version: str | None
    ascension: int | None
    modifiers: tuple[str, ...]
    is_multiplayer: bool
    visited: tuple[dict, ...]
    allow_partial_path: bool


_ROOM_TYPE_NAMES = {
    "ancient": "Ancient",
    "monster": "Monster",
    "elite": "Elite",
    "boss": "Boss",
    "shop": "Shop",
    "rest_site": "RestSite",
    "restsite": "RestSite",
    "treasure": "Treasure",
    "unknown": "Unknown",
    "event": "Unknown",
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _first_int(entry: dict, *keys: str) -> int | None:
    for key in keys:
        value = entry.get(key)
        if _is_int(value):
            return value
    return None


def _visited_room_type(entry: dict) -> str:
    value = entry.get("map_point_type", entry.get("room_type", "unknown"))
    normalized = str(value).strip().lower()
    return _ROOM_TYPE_NAMES.get(normalized, normalized.title() or "Unknown")


def visited_route_map(request: MapRequest, *, reason: str) -> ActMap:
    """Return only recorded nodes when a full graph cannot be trusted."""

    nodes = tuple(
        _visited_route_node(entry, index)
        for index, entry in enumerate(request.visited)
    )
    edges = tuple(
        MapEdge(from_id=nodes[index].id, to_id=nodes[index + 1].id)
        for index in range(len(nodes) - 1)
    )
    return ActMap(
        act_id=request.act_id,
        nodes=nodes,
        edges=edges,
        alignment=MapAlignment(ok=False, reason=reason),
        full_map=False,
        visited_route=bool(nodes),
        fallback_reason=reason,
    )


def _visited_route_node(entry: dict, index: int) -> MapNode:
    col = _first_int(entry, "col", "column", "map_col")
    row = _first_int(entry, "row", "map_row", "floor")
    return MapNode(
        id=f"visited:{index}",
        col=col if col is not None else 0,
        row=row if row is not None else index,
        room_type=_visited_room_type(entry),
        visited=True,
        path_index=index,
    )


class MapService:
    """Generate supported maps and cache immutable canonical results."""

    def __init__(
        self,
        *,
        node_executable: str = "node",
        cli_path: Path | None = None,
    ) -> None:
        self.node_executable = node_executable
        self.cli_path = cli_path or (
            Path(__file__).resolve().parent
            / "vendor"
            / "akirakato_mapgen"
            / "map_cli.js"
        )
        self._cache: dict[str, ActMap] = {}

    def generate(self, request: MapRequest) -> ActMap:
        cache_key = _serialize_request(request)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        fallback_reason = _preflight_fallback_reason(request)
        if fallback_reason is not None:
            result = visited_route_map(request, reason=fallback_reason)
            self._cache[cache_key] = result
            return result

        response = self._invoke_generator(_generator_payload(request))
        result = self._canonicalize_response(request, response)
        self._cache[cache_key] = result
        return result

    def _invoke_generator(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [self.node_executable, str(self.cli_path)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MapServiceTimeoutError(
                "map generator timed out after 5 seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise MapExecutableNotFoundError(
                f"Node executable not found: {self.node_executable}"
            ) from exc
        except OSError as exc:
            raise MapSubprocessError(f"map generator could not start: {exc}") from exc

        if completed.returncode != 0:
            detail = _subprocess_error_detail(completed)
            raise MapSubprocessError(
                f"map generator exited with code {completed.returncode}: {detail}"
            )

        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise MapOutputError("map generator stdout was not valid JSON") from exc
        if not isinstance(response, dict):
            raise MapOutputError("map generator stdout must be a JSON object")
        if response.get("schema_version") != 1:
            raise MapOutputError("map generator returned an unsupported schema version")
        if response.get("ok") is not True:
            detail = response.get("error")
            raise MapOutputError(
                f"map generator returned an unsuccessful response: {detail or 'unknown error'}"
            )
        return response

    def _canonicalize_response(
        self, request: MapRequest, response: dict[str, Any]
    ) -> ActMap:
        nodes = _parse_nodes(response.get("nodes"))
        edges = _parse_edges(response.get("edges"), nodes)
        alignment = _parse_alignment(response.get("alignment"))

        if not alignment.ok:
            return visited_route_map(
                request,
                reason=alignment.reason or "Generated map path alignment failed.",
            )
        if alignment.ambiguous:
            return visited_route_map(
                request,
                reason="Generated map path alignment is ambiguous.",
            )
        if not _alignment_covers_visited(nodes, alignment, len(request.visited)):
            return visited_route_map(
                request,
                reason="Generated alignment did not map every visited entry exactly once.",
            )

        return ActMap(
            act_id=request.act_id,
            nodes=nodes,
            edges=edges,
            alignment=alignment,
            full_map=True,
            visited_route=True,
            fallback_reason=None,
        )


def _request_dict(request: MapRequest) -> dict[str, Any]:
    return {
        "run_id": request.run_id,
        "act_id": request.act_id,
        "act_index": request.act_index,
        "seed": request.seed,
        "game_version": request.game_version,
        "ascension": request.ascension,
        "modifiers": list(request.modifiers),
        "is_multiplayer": request.is_multiplayer,
        "visited": list(request.visited),
        "allow_partial_path": request.allow_partial_path,
    }


def _serialize_request(request: MapRequest) -> str:
    return json.dumps(
        _request_dict(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _generator_payload(request: MapRequest) -> dict[str, Any]:
    payload = _request_dict(request)
    payload.pop("run_id")
    payload.pop("game_version")
    return payload


def _preflight_fallback_reason(request: MapRequest) -> str | None:
    if not request.act_id.strip():
        return "Act id is missing; showing the recorded visited route only."
    if request.seed is None or not request.seed.strip():
        return "Run seed is missing; showing the recorded visited route only."
    if not request.visited:
        return "Visited history is missing; no full map can be reconstructed."
    if request.game_version is None or not request.game_version.strip():
        return "Game version is missing; showing the recorded visited route only."
    if request.game_version not in SUPPORTED_MAP_BUILDS:
        return (
            f"Unsupported game build {request.game_version}; "
            "showing the recorded visited route only."
        )
    if request.is_multiplayer:
        return "Multiplayer map reconstruction is not supported in this release."
    if not _is_int(request.act_index) or not 0 <= request.act_index <= 3:
        return "Act index is invalid; showing the recorded visited route only."
    if request.ascension is None or not _is_int(request.ascension):
        return "Ascension is missing; showing the recorded visited route only."
    return None


def _subprocess_error_detail(completed: subprocess.CompletedProcess[str]) -> str:
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return completed.stderr.strip() or completed.stdout.strip() or "no diagnostics"


def _parse_nodes(value: object) -> tuple[MapNode, ...]:
    if not isinstance(value, list):
        raise MapOutputError("map generator nodes must be an array")
    nodes: list[MapNode] = []
    seen_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise MapOutputError("map generator node must be an object")
        node_id = raw.get("id")
        col = raw.get("col")
        row = raw.get("row")
        room_type = raw.get("room_type")
        visited = raw.get("visited")
        path_index = raw.get("path_index")
        if not isinstance(node_id, str) or not node_id:
            raise MapOutputError("map generator node id must be a non-empty string")
        if node_id in seen_ids:
            raise MapOutputError(f"map generator returned duplicate node id {node_id}")
        if not _is_int(col) or not _is_int(row):
            raise MapOutputError("map generator node coordinates must be integers")
        if not isinstance(room_type, str) or not room_type:
            raise MapOutputError("map generator node room_type must be a string")
        if not isinstance(visited, bool):
            raise MapOutputError("map generator node visited must be a boolean")
        if path_index is not None and not _is_int(path_index):
            raise MapOutputError("map generator node path_index must be an integer or null")
        if visited and path_index is None:
            raise MapOutputError("visited map generator node is missing path_index")
        if not visited and path_index is not None:
            raise MapOutputError("unvisited map generator node has a path_index")
        seen_ids.add(node_id)
        nodes.append(
            MapNode(
                id=node_id,
                col=col,
                row=row,
                room_type=room_type,
                visited=visited,
                path_index=path_index,
            )
        )
    return tuple(nodes)


def _parse_edges(value: object, nodes: tuple[MapNode, ...]) -> tuple[MapEdge, ...]:
    if not isinstance(value, list):
        raise MapOutputError("map generator edges must be an array")
    node_ids = {node.id for node in nodes}
    edges: list[MapEdge] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise MapOutputError("map generator edge must be an object")
        from_id = raw.get("from")
        to_id = raw.get("to")
        if not isinstance(from_id, str) or not isinstance(to_id, str):
            raise MapOutputError("map generator edge endpoints must be strings")
        if from_id not in node_ids or to_id not in node_ids:
            raise MapOutputError("map generator edge references an unknown node")
        edges.append(MapEdge(from_id=from_id, to_id=to_id))
    return tuple(edges)


def _parse_alignment(value: object) -> MapAlignment:
    if not isinstance(value, dict):
        raise MapOutputError("map generator alignment must be an object")
    ok = value.get("ok")
    ambiguous = value.get("ambiguous")
    reason = value.get("reason")
    path_node_ids = value.get("path_node_ids")
    if not isinstance(ok, bool) or not isinstance(ambiguous, bool):
        raise MapOutputError("map generator alignment flags must be booleans")
    if reason is not None and not isinstance(reason, str):
        raise MapOutputError("map generator alignment reason must be a string or null")
    if not isinstance(path_node_ids, list) or not all(
        isinstance(node_id, str) for node_id in path_node_ids
    ):
        raise MapOutputError("map generator aligned path must be an array of node ids")
    return MapAlignment(
        ok=ok,
        ambiguous=ambiguous,
        reason=reason,
        path_node_ids=tuple(path_node_ids),
    )


def _alignment_covers_visited(
    nodes: tuple[MapNode, ...], alignment: MapAlignment, visited_count: int
) -> bool:
    if len(alignment.path_node_ids) != visited_count:
        return False
    if len(set(alignment.path_node_ids)) != visited_count:
        return False
    nodes_by_id = {node.id: node for node in nodes}
    for path_index, node_id in enumerate(alignment.path_node_ids):
        node = nodes_by_id.get(node_id)
        if node is None or not node.visited or node.path_index != path_index:
            return False
    visited_nodes = [node for node in nodes if node.visited]
    return len(visited_nodes) == visited_count
