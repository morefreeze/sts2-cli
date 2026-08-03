"""Version-gated reconstruction of STS2 act maps.

The vendored JavaScript generator is treated as an isolated deterministic
engine.  This module owns the public Python contract, supported-build gate,
subprocess failures, output validation, caching, and the conservative
visited-route fallback.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any

from .models import ActMap, MapAlignment, MapEdge, MapNode


SUPPORTED_MAP_BUILDS = frozenset({"v0.103.2"})
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_VISITED_NODES = 256
MAX_MAP_NODES = 256
MAX_MAP_EDGES = 2048
MAX_NODE_ID_CHARS = 64
MAX_ROOM_TYPE_CHARS = 64
MAX_ALIGNMENT_REASON_CHARS = 2048


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


class MapServiceInputError(MapServiceError):
    """A request could not be snapshotted safely."""


@dataclass(frozen=True)
class MapRequest:
    run_id: str
    act_id: str
    act_index: int
    seed: str | None
    game_version: str | None
    ascension: int | None
    modifiers: tuple[str, ...]
    is_multiplayer: bool | None
    visited: tuple[dict, ...]
    allow_partial_path: bool


_ROOM_TYPE_ALIASES = {
    "ancient": "Ancient",
    "monster": "Monster",
    "elite": "Elite",
    "boss": "Boss",
    "shop": "Shop",
    "rest_site": "RestSite",
    "restsite": "RestSite",
    "rest site": "RestSite",
    "treasure": "Treasure",
    "unknown": "Unknown",
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
    return _normalize_visited_room_type(entry) or "Unknown"


def _normalize_visited_room_type(entry: dict) -> str | None:
    value = entry.get("map_point_type", entry.get("room_type"))
    if type(value) is not str:
        return None
    normalized = value.strip().lower()
    if normalized == "event":
        return "Ancient" if _is_neow_entry(entry) else "Unknown"
    return _ROOM_TYPE_ALIASES.get(normalized)


def _normalize_generated_room_type(value: str) -> str | None:
    return _ROOM_TYPE_ALIASES.get(value.strip().lower())


def _is_neow_entry(entry: dict) -> bool:
    rooms = entry.get("rooms")
    if type(rooms) is not list or not rooms or type(rooms[0]) is not dict:
        return False
    model_id = rooms[0].get("model_id")
    return type(model_id) is str and model_id.upper() == "EVENT.NEOW"


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
        snapshot = _snapshot_request(request)
        shape_reason = _request_shape_fallback_reason(snapshot)
        if shape_reason is not None:
            return _safe_fallback(snapshot, shape_reason)

        try:
            generator_input = _serialize_generator_payload(snapshot)
            cache_key = _serialize_request(snapshot)
        except (TypeError, ValueError, RecursionError) as exc:
            return _safe_fallback(
                snapshot,
                f"Map metadata is not JSON serializable ({type(exc).__name__}); "
                "showing the recorded visited route only.",
            )
        if len(generator_input.encode("utf-8")) > MAX_INPUT_BYTES:
            return _safe_fallback(
                snapshot,
                "Map generator input exceeds 1 MiB; showing the recorded "
                "visited route only.",
            )

        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        fallback_reason = _preflight_fallback_reason(snapshot)
        if fallback_reason is not None:
            result = visited_route_map(snapshot, reason=fallback_reason)
            self._cache[cache_key] = result
            return result

        response = self._invoke_generator(generator_input)
        result = self._canonicalize_response(snapshot, response)
        self._cache[cache_key] = result
        return result

    def _invoke_generator(self, generator_input: str) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [self.node_executable, str(self.cli_path)],
                input=generator_input,
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

        # The bundled CLI is trusted and time-bounded. subprocess.run captures
        # its output, so enforce the response budget before parsing or using it.
        if not isinstance(completed.stdout, str):
            raise MapOutputError("map generator stdout must be text")
        if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise MapOutputError("map generator stdout exceeds the 1 MiB limit")
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
        schema_version = response.get("schema_version")
        if type(schema_version) is not int or schema_version != 1:
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
        alignment_failure = _alignment_failure_reason(
            nodes, edges, alignment, request.visited
        )
        if alignment_failure is not None:
            return visited_route_map(
                request,
                reason=alignment_failure,
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
        allow_nan=False,
    )


def _generator_payload(request: MapRequest) -> dict[str, Any]:
    payload = _request_dict(request)
    payload.pop("run_id")
    payload.pop("game_version")
    return payload


def _serialize_generator_payload(request: MapRequest) -> str:
    return json.dumps(
        _generator_payload(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _snapshot_request(request: MapRequest) -> MapRequest:
    if not isinstance(request, MapRequest):
        raise MapServiceInputError("map request must be a MapRequest")
    try:
        snapshot = deepcopy(request)
    except Exception as exc:
        raise MapServiceInputError("map request could not be copied safely") from exc
    if not isinstance(snapshot, MapRequest):
        raise MapServiceInputError("map request snapshot is invalid")
    return snapshot


def _request_shape_fallback_reason(request: MapRequest) -> str | None:
    if type(request.run_id) is not str:
        return "Run id must be a string; showing the recorded visited route only."
    if type(request.act_id) is not str:
        return "Act id must be a string; showing the recorded visited route only."
    if not _is_int(request.act_index) or not 0 <= request.act_index <= 3:
        return "Act index must be an integer from 0 to 3; showing the recorded route only."
    if request.seed is not None and type(request.seed) is not str:
        return "Run seed must be a string; showing the recorded visited route only."
    if request.game_version is not None and type(request.game_version) is not str:
        return "Game version must be a string; showing the recorded visited route only."
    if request.ascension is not None and (
        not _is_int(request.ascension) or not 0 <= request.ascension <= 10
    ):
        return "Ascension must be an integer from 0 to 10; showing the recorded route only."
    if type(request.modifiers) is not tuple or not all(
        type(modifier) is str for modifier in request.modifiers
    ):
        return "Modifiers must be a tuple of strings; showing the recorded route only."
    if type(request.is_multiplayer) is not bool:
        return "Multiplayer flag must be a boolean; showing the recorded route only."
    if type(request.visited) is not tuple:
        return "Visited history must be a tuple; showing the recorded route only."
    if len(request.visited) > MAX_VISITED_NODES:
        return (
            f"Visited history exceeds {MAX_VISITED_NODES} nodes; "
            "showing the recorded route only."
        )
    if not all(type(entry) is dict for entry in request.visited):
        return "Visited history entries must be plain objects; showing the recorded route only."
    if type(request.allow_partial_path) is not bool:
        return "Partial-path flag must be a boolean; showing the recorded route only."
    return None


def _safe_fallback(request: MapRequest, reason: str) -> ActMap:
    act_id = request.act_id if type(request.act_id) is str else ""
    raw_visited = request.visited
    if type(raw_visited) not in {tuple, list}:
        raw_visited = ()
    visited = tuple(
        entry for entry in raw_visited[:MAX_VISITED_NODES] if type(entry) is dict
    )
    safe_request = MapRequest(
        run_id=request.run_id if type(request.run_id) is str else "",
        act_id=act_id,
        act_index=request.act_index if _is_int(request.act_index) else 0,
        seed=request.seed if type(request.seed) is str else None,
        game_version=(
            request.game_version if type(request.game_version) is str else None
        ),
        ascension=request.ascension if _is_int(request.ascension) else None,
        modifiers=(
            request.modifiers
            if type(request.modifiers) is tuple
            and all(type(item) is str for item in request.modifiers)
            else ()
        ),
        is_multiplayer=(
            request.is_multiplayer if type(request.is_multiplayer) is bool else False
        ),
        visited=visited,
        allow_partial_path=(
            request.allow_partial_path
            if type(request.allow_partial_path) is bool
            else False
        ),
    )
    return visited_route_map(safe_request, reason=reason)


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
    if len(value) > MAX_MAP_NODES:
        raise MapOutputError(
            f"map generator node count exceeds {MAX_MAP_NODES}"
        )
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
        if type(node_id) is not str or not node_id:
            raise MapOutputError("map generator node id must be a non-empty string")
        if len(node_id) > MAX_NODE_ID_CHARS:
            raise MapOutputError("map generator node id exceeds the safe length")
        if node_id in seen_ids:
            raise MapOutputError(f"map generator returned duplicate node id {node_id}")
        if not _is_int(col) or not _is_int(row):
            raise MapOutputError("map generator node coordinates must be integers")
        if type(room_type) is not str or not room_type:
            raise MapOutputError("map generator node room_type must be a string")
        if len(room_type) > MAX_ROOM_TYPE_CHARS:
            raise MapOutputError("map generator node room_type exceeds the safe length")
        if _normalize_generated_room_type(room_type) is None:
            raise MapOutputError("map generator node room_type is not recognized")
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
    if len(value) > MAX_MAP_EDGES:
        raise MapOutputError(
            f"map generator edge count exceeds {MAX_MAP_EDGES}"
        )
    node_ids = {node.id for node in nodes}
    edges: list[MapEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise MapOutputError("map generator edge must be an object")
        from_id = raw.get("from")
        to_id = raw.get("to")
        if type(from_id) is not str or type(to_id) is not str:
            raise MapOutputError("map generator edge endpoints must be strings")
        if len(from_id) > MAX_NODE_ID_CHARS or len(to_id) > MAX_NODE_ID_CHARS:
            raise MapOutputError("map generator edge node id exceeds the safe length")
        if from_id not in node_ids or to_id not in node_ids:
            raise MapOutputError("map generator edge references an unknown node")
        if from_id == to_id:
            raise MapOutputError("map generator returned a self edge")
        edge_key = (from_id, to_id)
        if edge_key in seen_edges:
            raise MapOutputError("map generator returned a duplicate edge")
        seen_edges.add(edge_key)
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
    if reason is not None and type(reason) is not str:
        raise MapOutputError("map generator alignment reason must be a string or null")
    if reason is not None and len(reason) > MAX_ALIGNMENT_REASON_CHARS:
        raise MapOutputError("map generator alignment reason exceeds the safe length")
    if not isinstance(path_node_ids, list) or not all(
        type(node_id) is str for node_id in path_node_ids
    ):
        raise MapOutputError("map generator aligned path must be an array of node ids")
    if len(path_node_ids) > MAX_VISITED_NODES:
        raise MapOutputError("map generator aligned path exceeds the safe node count")
    if any(len(node_id) > MAX_NODE_ID_CHARS for node_id in path_node_ids):
        raise MapOutputError("map generator path node id exceeds the safe length")
    return MapAlignment(
        ok=ok,
        ambiguous=ambiguous,
        reason=reason,
        path_node_ids=tuple(path_node_ids),
    )


def _alignment_failure_reason(
    nodes: tuple[MapNode, ...],
    edges: tuple[MapEdge, ...],
    alignment: MapAlignment,
    visited: tuple[dict, ...],
) -> str | None:
    visited_count = len(visited)
    if len(alignment.path_node_ids) != visited_count:
        return "Generated alignment did not map every visited entry exactly once."
    if len(set(alignment.path_node_ids)) != visited_count:
        return "Generated alignment did not map every visited entry exactly once."
    nodes_by_id = {node.id: node for node in nodes}
    for path_index, node_id in enumerate(alignment.path_node_ids):
        node = nodes_by_id.get(node_id)
        if node is None or not node.visited or node.path_index != path_index:
            return "Generated alignment did not map every visited entry exactly once."
    visited_nodes = [node for node in nodes if node.visited]
    if len(visited_nodes) != visited_count:
        return "Generated alignment did not map every visited entry exactly once."

    directed_edges = {(edge.from_id, edge.to_id) for edge in edges}
    for from_id, to_id in zip(
        alignment.path_node_ids, alignment.path_node_ids[1:]
    ):
        if (from_id, to_id) not in directed_edges:
            return "Generated aligned path is missing a required directed edge."

    for index, entry in enumerate(visited):
        expected_room_type = _normalize_visited_room_type(entry)
        node = nodes_by_id[alignment.path_node_ids[index]]
        actual_room_type = _normalize_generated_room_type(node.room_type)
        if expected_room_type is None or actual_room_type != expected_room_type:
            return (
                "Generated aligned path room type does not match the recorded "
                "visited history."
            )
    return None
