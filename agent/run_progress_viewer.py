#!/usr/bin/env python3
"""Timeline viewer for GameLogger JSONL files.

Usage:
    .venv/bin/python agent/run_progress_viewer.py --port 8765
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import mimetypes
import posixpath
import re
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, unquote, urlparse

# Preserve the documented ``python agent/run_progress_viewer.py`` entrypoint.
# Direct script execution otherwise exposes only ``agent/`` on ``sys.path``.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.run_workbench.catalog import (
    CatalogError,
    CatalogNotFoundError,
    RunCatalog,
)
from agent.run_workbench.assets import InvalidNodeArtModelError, NodeArtResolver
from agent.run_workbench.map_service import (
    MapExecutableNotFoundError,
    MapOutputError,
    MapRequest,
    MapService,
    MapServiceError,
    MapServiceTimeoutError,
    MapSubprocessError,
    _normalize_generated_room_type,
    _normalize_visited_room_type,
    visited_route_map,
)
from agent.run_workbench.recorded_maps import (
    RecordedActSnapshot,
    latest_recorded_acts,
)
from agent.run_workbench.replay import (
    format_room_label,
    parse_game_progress,
)


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
STATIC_DIR = ROOT / "agent" / "run_workbench" / "static"
STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/map.js": ("map.js", "text/javascript; charset=utf-8"),
}
ADVISOR_URL = "https://ing-gom.github.io/sts2-card-advisor/"
TRANSLATION_CACHE_TTL_SECONDS = 24 * 60 * 60
PARSE_BODY_MAX_BYTES = 10 * 1024 * 1024
_TRANSLATION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ACT_COUNT = 4


def _canonical_node_act_index(node: dict[str, Any]) -> int:
    """Return the zero-based act owning one canonical recorded node."""

    raw_id = node.get("id")
    if isinstance(raw_id, str):
        match = re.match(r"^a(\d+):n\d+$", raw_id)
        if match:
            return int(match.group(1))
    act_index = node.get("act_index")
    if isinstance(act_index, int) and not isinstance(act_index, bool):
        return max(0, act_index)
    act = node.get("act")
    if isinstance(act, int) and not isinstance(act, bool):
        return max(0, act - 1)
    global_floor = node.get("global_floor")
    if isinstance(global_floor, int) and not isinstance(global_floor, bool):
        return max(0, (global_floor - 1) // 17)
    return 0


def _is_canonical_route_node(node: dict[str, Any]) -> bool:
    """Exclude joined inventory/decision evidence from the visited map path."""

    evidence_kind = node.get("_workbench_evidence_kind")
    return evidence_kind is None or evidence_kind == "route_node"


def _route_node_source_kinds(node: dict[str, Any]) -> set[str]:
    provenance = node.get("_workbench_provenance")
    if not isinstance(provenance, list):
        return set()
    return {
        item["source_kind"]
        for item in provenance
        if isinstance(item, dict) and isinstance(item.get("source_kind"), str)
    }


def _canonical_route_node_act_index(node: dict[str, Any]) -> int | None:
    """Return one consistent bounded act identity or fail closed."""

    candidates: list[int] = []
    raw_id = node.get("id")
    if isinstance(raw_id, str):
        match = re.match(r"^a(\d+):n\d+$", raw_id)
        if match:
            act_index = int(match.group(1))
            if not 0 <= act_index < _ACT_COUNT:
                return None
            candidates.append(act_index)
    for key, minimum, maximum, offset in (
        ("act_index", 0, _ACT_COUNT - 1, 0),
        ("act", 1, _ACT_COUNT, -1),
        ("global_floor", 1, _ACT_COUNT * 17, -1),
    ):
        if key not in node:
            continue
        value = node[key]
        if type(value) is not int or not minimum <= value <= maximum:
            return None
        candidates.append(
            (value - 1) // 17 if key == "global_floor" else value + offset
        )
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        return None
    return candidates[0]


def _canonical_route_nodes(run: dict[str, Any]) -> list[dict[str, Any]]:
    indexed_nodes = [
        (node, act_index)
        for node in (run.get("nodes") if isinstance(run.get("nodes"), list) else [])
        if isinstance(node, dict)
        and _is_canonical_route_node(node)
        and (act_index := _canonical_route_node_act_index(node)) is not None
    ]
    native_acts = {
        act_index
        for node, act_index in indexed_nodes
        if "native_run" in _route_node_source_kinds(node)
    }
    return [
        node
        for node, act_index in indexed_nodes
        if act_index not in native_acts
        or "native_run" in _route_node_source_kinds(node)
    ]


def _act_identifier(act: dict[str, Any] | None) -> str:
    if not isinstance(act, dict):
        return ""
    for key in ("id", "act_id"):
        value = act.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _descriptor_act_index(act: dict[str, Any], position: int) -> int | None:
    act_index = act.get("act_index")
    if type(act_index) is int and 0 <= act_index < _ACT_COUNT:
        return act_index
    act_number = act.get("act")
    if type(act_number) is int and 1 <= act_number <= _ACT_COUNT:
        return act_number - 1
    if _act_identifier(act) and 0 <= position < _ACT_COUNT:
        return position
    return None


def _act_descriptors(run: dict[str, Any]) -> list[dict[str, Any]]:
    acts = run.get("acts") if isinstance(run.get("acts"), list) else []
    nodes = _canonical_route_nodes(run)
    acts_by_index: dict[int, dict[str, Any]] = {}
    for position, act in enumerate(acts):
        if not isinstance(act, dict):
            continue
        act_index = _descriptor_act_index(act, position)
        if act_index is None:
            continue
        selected = acts_by_index.get(act_index)
        if selected is None or (
            not _act_identifier(selected) and _act_identifier(act)
        ):
            acts_by_index[act_index] = act
    node_indices = {
        act_index
        for node in nodes
        if isinstance(node, dict)
        and 0 <= (act_index := _canonical_node_act_index(node)) < _ACT_COUNT
    }
    indices = sorted(set(acts_by_index) | node_indices)
    if not indices:
        indices = [0]
    descriptors: list[dict[str, Any]] = []
    for index in indices:
        act = acts_by_index.get(index, {})
        act_id = _act_identifier(act)
        visited_count = sum(
            1
            for node in nodes
            if isinstance(node, dict) and _canonical_node_act_index(node) == index
        )
        descriptors.append(
            {
                "index": index,
                "act_id": act_id or None,
                "label": f"第 {index + 1} 幕",
                "available": visited_count > 0,
                "visited_count": visited_count,
            }
        )
    return descriptors


def _node_model_id(node: dict[str, Any]) -> str | None:
    for key in ("model_id", "room_model_id", "boss_id"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    rooms = node.get("rooms")
    if isinstance(rooms, list):
        for room in rooms:
            if isinstance(room, dict):
                value = room.get("model_id")
                if isinstance(value, str) and value:
                    return value
    return None


def _map_visited_entry(node: dict[str, Any]) -> dict[str, Any]:
    """Project canonical evidence onto the generator's bounded route schema."""

    entry: dict[str, Any] = {}
    for key in (
        "map_point_type",
        "room_type",
        "col",
        "column",
        "map_col",
        "row",
        "map_row",
        "floor",
    ):
        value = node.get(key)
        if isinstance(value, str) or (
            isinstance(value, int) and not isinstance(value, bool)
        ):
            entry[key] = value
    model_id = _node_model_id(node)
    if model_id is not None:
        entry["rooms"] = [{"model_id": model_id}]
    return entry


def _map_service_fallback_reason(error: MapServiceError) -> str:
    """Return a stable public message without exposing local command details."""

    if isinstance(error, MapServiceTimeoutError):
        return "Map reconstruction timed out; showing the recorded route only."
    if isinstance(error, MapExecutableNotFoundError):
        return "Map reconstruction is unavailable; showing the recorded route only."
    if isinstance(error, MapOutputError):
        return "Map reconstruction returned invalid output; showing the recorded route only."
    if isinstance(error, MapSubprocessError):
        return "Map reconstruction failed; showing the recorded route only."
    return "Map reconstruction could not complete; showing the recorded route only."


def _trusted_recorded_map_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only adapter-authenticated raw deck-history map evidence."""

    rows: list[dict[str, Any]] = []
    nodes = run.get("nodes")
    if not isinstance(nodes, list):
        return rows
    for node in nodes:
        if (
            not isinstance(node, dict)
            or node.get("event") != "map_snapshot"
            or node.get("_workbench_evidence_kind") != "deck_history_event"
        ):
            continue
        provenance = node.get("_workbench_provenance")
        if not isinstance(provenance, list) or not any(
            isinstance(item, dict)
            and item.get("source_kind") == "deck_history"
            for item in provenance
        ):
            continue
        rows.append(node)
    return rows


def _recorded_map_matches_canonical_route(
    recorded: RecordedActSnapshot,
    canonical_nodes: list[dict[str, Any]],
) -> bool:
    """Require an exact path pairing before copying canonical node details."""

    act_map = recorded.act_map
    path_ids = act_map.alignment.path_node_ids
    route_nodes = recorded.route_nodes
    if (
        not act_map.full_map
        or not act_map.alignment.ok
        or act_map.alignment.ambiguous
        or len(path_ids) != len(canonical_nodes)
        or len(route_nodes) != len(canonical_nodes)
    ):
        return False
    nodes_by_id = {node.id: node for node in act_map.nodes}
    if len(nodes_by_id) != len(act_map.nodes):
        return False
    visited_nodes = [node for node in act_map.nodes if node.visited]
    if len(visited_nodes) != len(path_ids):
        return False
    for path_index, (path_id, route_node, canonical_node) in enumerate(
        zip(path_ids, route_nodes, canonical_nodes)
    ):
        map_node = nodes_by_id.get(path_id)
        canonical_col = canonical_node.get("col")
        canonical_row = canonical_node.get("row")
        canonical_room_type = _normalize_visited_room_type(canonical_node)
        route_room_type = _normalize_visited_room_type(route_node)
        map_room_type = (
            _normalize_generated_room_type(map_node.room_type)
            if map_node is not None
            else None
        )
        canonical_model_id = _node_model_id(canonical_node)
        route_model_id = _node_model_id(route_node)
        if (
            map_node is None
            or not map_node.visited
            or map_node.path_index != path_index
            or type(canonical_col) is not int
            or type(canonical_row) is not int
            or route_node.get("col") != canonical_col
            or route_node.get("row") != canonical_row
            or map_node.col != canonical_col
            or map_node.row != canonical_row
            or canonical_room_type is None
            or route_room_type != canonical_room_type
            or map_room_type != canonical_room_type
            or (
                canonical_model_id is not None
                and route_model_id is not None
                and canonical_model_id != route_model_id
            )
        ):
            return False
    return True


def _authoritative_recorded_act(
    run: dict[str, Any],
    act_index: int,
    canonical_nodes: list[dict[str, Any]],
) -> RecordedActSnapshot | None:
    """Select one validated recorded map without exposing parser failures."""

    try:
        recorded_acts, _parser_errors = latest_recorded_acts(
            iter(_trusted_recorded_map_rows(run))
        )
        recorded = recorded_acts.get(act_index)
        if recorded is None or not _recorded_map_matches_canonical_route(
            recorded, canonical_nodes
        ):
            return None
        return recorded
    except Exception:
        return None


def _run_map_payload(
    catalog: RunCatalog,
    map_service: MapService,
    art_resolver: NodeArtResolver,
    run_id: str,
    act_index: int,
) -> dict[str, Any]:
    """Build one act map from the catalog's joined canonical run."""

    canonical_payload = catalog.get_run(run_id)
    run = canonical_payload["run"]
    acts = _act_descriptors(run)
    descriptor = next((item for item in acts if item["index"] == act_index), None)
    if descriptor is None:
        raise CatalogNotFoundError(
            f"run {run_id!r} has no act {act_index}"
        )
    all_recorded_nodes = _canonical_route_nodes(run)
    recorded_nodes = [
        node
        for node in all_recorded_nodes
        if _canonical_node_act_index(node) == act_index
    ]
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    outcome = run.get("outcome") if isinstance(run.get("outcome"), dict) else {}
    modifiers = metadata.get("modifiers")
    final_recorded_act_index = (
        _canonical_node_act_index(all_recorded_nodes[-1])
        if all_recorded_nodes
        else None
    )
    run_won = outcome.get("status") == "win" or outcome.get("victory") is True
    authoritative_recorded = _authoritative_recorded_act(
        run,
        act_index,
        recorded_nodes,
    )
    if authoritative_recorded is not None:
        act_map = authoritative_recorded.act_map
    else:
        request = MapRequest(
            run_id=run_id,
            act_id=descriptor.get("act_id") or "",
            act_index=act_index,
            seed=(
                metadata.get("seed")
                if isinstance(metadata.get("seed"), str)
                else None
            ),
            game_version=(
                metadata.get("game_version")
                if isinstance(metadata.get("game_version"), str)
                else None
            ),
            ascension=(
                metadata.get("ascension")
                if isinstance(metadata.get("ascension"), int)
                and not isinstance(metadata.get("ascension"), bool)
                else None
            ),
            modifiers=tuple(
                value for value in modifiers if isinstance(value, str)
            ) if isinstance(modifiers, list) else (),
            is_multiplayer=(
                metadata.get("is_multiplayer")
                if type(metadata.get("is_multiplayer")) is bool
                else None
            ),
            visited=tuple(_map_visited_entry(node) for node in recorded_nodes),
            allow_partial_path=(
                not run_won and act_index == final_recorded_act_index
            ),
        )
        try:
            act_map = map_service.generate(request)
        except MapServiceError as error:
            act_map = visited_route_map(
                request,
                reason=_map_service_fallback_reason(error),
            )
    payload = act_map.to_dict()
    path_ids = payload["alignment"].get("path_node_ids") or [
        node["id"]
        for node in sorted(
            (item for item in payload["nodes"] if item["visited"]),
            key=lambda item: item["path_index"],
        )
    ]
    contains_global_terminal = bool(
        recorded_nodes
        and all_recorded_nodes
        and recorded_nodes[-1] is all_recorded_nodes[-1]
    )
    terminal_node_id = (
        path_ids[-1] if path_ids and contains_global_terminal else None
    )
    terminal_status = outcome.get("status") or "unknown"
    for node in payload["nodes"]:
        source_node = None
        path_index = node.get("path_index")
        if node.get("visited") and isinstance(path_index, int):
            if 0 <= path_index < len(recorded_nodes):
                source_node = recorded_nodes[path_index]
                node["deltas"] = deepcopy(source_node.get("deltas") or {})
                node["recorded_node_id"] = source_node.get("id")
        model_id = _node_model_id(source_node or {})
        try:
            art = art_resolver.resolve(node["room_type"], model_id=model_id)
        except InvalidNodeArtModelError:
            art = art_resolver.resolve(node["room_type"])
        node["art"] = art.to_dict()
        node["terminal"] = node["id"] == terminal_node_id
        node["terminal_status"] = terminal_status if node["terminal"] else None

    payload.update(
        {
            "run_id": run_id,
            "act": descriptor,
            "acts": acts,
            "summary": {
                "node_count": len(payload["nodes"]),
                "edge_count": len(payload["edges"]),
                "visited_count": sum(1 for node in payload["nodes"] if node["visited"]),
                "terminal_node_id": terminal_node_id,
            },
        }
    )
    return payload


def extract_js_assignment_object(source: str, assignment_name: str) -> dict[str, Any]:
    """Extract a JSON object assigned to a JavaScript global/constant."""
    raw_name = assignment_name.removeprefix("window.")
    pattern = re.compile(rf"(?:window\.)?{re.escape(raw_name)}\s*=")
    match = pattern.search(source)
    if match is None:
        raise ValueError(f"{assignment_name} assignment not found")
    start = source.find("{", match.end())
    if start < 0:
        raise ValueError(f"{assignment_name} object start not found")

    depth = 0
    string_quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if string_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_quote:
                string_quote = None
            continue
        if char in {'"', "'"}:
            string_quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(source[start : index + 1])
    raise ValueError(f"{assignment_name} object end not found")


def _localized_name(entry: dict[str, Any], lang: str) -> str | None:
    names = entry.get("name_loc") if isinstance(entry.get("name_loc"), dict) else entry.get("name")
    if not isinstance(names, dict):
        return str(names) if names else None
    return names.get(lang) or names.get("en") or names.get("ko")


def _localized_desc(entry: dict[str, Any], lang: str) -> str | None:
    descs = entry.get("desc_loc") if isinstance(entry.get("desc_loc"), dict) else entry.get("desc") or entry.get("description")
    if not isinstance(descs, dict):
        return str(descs) if descs else None
    return descs.get(lang) or descs.get("en") or descs.get("ko")


def build_translation_catalog(
    cards_locale: dict[str, Any],
    relic_info: dict[str, Any],
    lang: str = "zh",
) -> dict[str, Any]:
    """Build compact card/relic translation maps for the frontend."""
    catalog: dict[str, Any] = {
        "lang": lang,
        "source": ADVISOR_URL,
        "cards": {},
        "card_names": {},
        "card_descs": {},
        "relics": {},
        "relic_names": {},
        "relic_descs": {},
    }

    for card_id, entry in cards_locale.items():
        if not isinstance(entry, dict):
            continue
        translated = _localized_name(entry, lang)
        english = (entry.get("name") or {}).get("en") if isinstance(entry.get("name"), dict) else None
        desc = _localized_desc(entry, lang)
        if translated:
            catalog["cards"][card_id] = translated
        if english and translated:
            catalog["card_names"][english] = translated
        if desc:
            catalog["card_descs"][card_id] = desc

    for relic_id, entry in relic_info.items():
        if not isinstance(entry, dict):
            continue
        translated = _localized_name(entry, lang)
        english = (entry.get("name_loc") or {}).get("en") if isinstance(entry.get("name_loc"), dict) else None
        desc = _localized_desc(entry, lang)
        if translated:
            catalog["relics"][relic_id] = translated
        if english and translated:
            catalog["relic_names"][english] = translated
        if desc:
            catalog["relic_descs"][relic_id] = desc

    return catalog


def _fetch_advisor_html() -> str:
    request = Request(ADVISOR_URL, headers={"User-Agent": "sts2-run-progress-viewer/1.0"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def get_translation_catalog(lang: str = "zh") -> dict[str, Any]:
    """Return cached translations from the public card-advisor page."""
    if lang == "en":
        return build_translation_catalog({}, {}, lang="en")
    if lang != "zh":
        raise ValueError(f"unsupported language: {lang}")

    now = time.time()
    cached = _TRANSLATION_CACHE.get(lang)
    if cached and now - cached[0] < TRANSLATION_CACHE_TTL_SECONDS:
        return cached[1]

    html = _fetch_advisor_html()
    cards_locale = extract_js_assignment_object(html, "window.__CARDS_LOCALE__")
    relic_info = extract_js_assignment_object(html, "RELIC_INFO")
    catalog = build_translation_catalog(cards_locale, relic_info, lang=lang)
    catalog["cached_at"] = now
    _TRANSLATION_CACHE[lang] = (now, catalog)
    return catalog


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
    return entries


def parse_log_file(path: Path) -> dict[str, Any]:
    return parse_game_progress(read_jsonl(path), source_name=path.name)


def list_log_files(limit: int = 80) -> list[dict[str, Any]]:
    if not LOG_DIR.exists():
        return []
    paths = sorted(LOG_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    logs = []
    for path in paths[:limit]:
        stat = path.stat()
        logs.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            }
        )
    return logs


def latest_log_file() -> Path | None:
    logs = list_log_files(limit=1)
    return LOG_DIR / logs[0]["name"] if logs else None


def _safe_log_path(name: str) -> Path:
    decoded = unquote(name)
    basename = posixpath.basename(decoded)
    path = (LOG_DIR / basename).resolve()
    log_root = LOG_DIR.resolve()
    if log_root not in path.parents or path.suffix != ".jsonl":
        raise ValueError("invalid log path")
    return path



class ViewerHandler(BaseHTTPRequestHandler):
    server_version = "RunProgressViewer/1.0"
    catalog = RunCatalog(
        [LOG_DIR, ROOT / "data"],
        replay_parser=parse_game_progress,
        include_policy="workbench",
    )
    node_art_resolver = NodeArtResolver()
    map_service = MapService()

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self._send_bytes(body, content_type)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, route: str) -> None:
        asset = STATIC_ASSETS.get(route)
        if asset is None:
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        self._send_bytes(body, content_type)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._send_json({"error": message}, status)

    def _send_internal_error(self, error: Exception) -> None:
        self.log_error("unhandled request error: %r", error)
        self._send_error(
            "internal server error", HTTPStatus.INTERNAL_SERVER_ERROR
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in STATIC_ASSETS:
                if parsed.query or parsed.params or parsed.fragment:
                    self._send_error("not found", HTTPStatus.NOT_FOUND)
                    return
                self._send_static(parsed.path)
            elif parsed.path == "/api/catalog":
                self._send_json({"sources": self.catalog.list_sources()})
            elif parsed.path == "/api/cohorts":
                self._send_json({"cohorts": self.catalog.list_cohorts()})
            elif parsed.path == "/api/metrics":
                query = parse_qs(parsed.query)
                current = (query.get("current") or [""])[0]
                baseline = (query.get("baseline") or [None])[0]
                if not current:
                    self._send_error("missing current cohort")
                    return
                self._send_json(self.catalog.get_metrics(current, baseline))
            elif parsed.path == "/api/run/map":
                query = parse_qs(parsed.query, keep_blank_values=True)
                unexpected = sorted(set(query) - {"id", "act"})
                if unexpected:
                    self._send_error(
                        f"unexpected map query parameter: {unexpected[0]}"
                    )
                    return
                run_values = query.get("id") or []
                act_values = query.get("act") or ["0"]
                if len(run_values) != 1 or not run_values[0]:
                    self._send_error("missing run id")
                    return
                if len(act_values) != 1:
                    self._send_error("act must appear exactly once")
                    return
                try:
                    act_index = int(act_values[0])
                except ValueError:
                    self._send_error("act must be an integer")
                    return
                if str(act_index) != act_values[0] or not 0 <= act_index <= 3:
                    self._send_error("act must be between 0 and 3")
                    return
                self._send_json(
                    _run_map_payload(
                        self.catalog,
                        self.map_service,
                        self.node_art_resolver,
                        run_values[0],
                        act_index,
                    )
                )
            elif parsed.path == "/api/run":
                query = parse_qs(parsed.query)
                run_id = (query.get("id") or [""])[0]
                if not run_id:
                    self._send_error("missing run id")
                    return
                self._send_json(self.catalog.get_run(run_id))
            elif parsed.path == "/api/source":
                query = parse_qs(parsed.query)
                source_id = (query.get("id") or [""])[0]
                if not source_id:
                    self._send_error("missing source id")
                    return
                self._send_json(self.catalog.get_source(source_id))
            elif parsed.path == "/api/node-art":
                query = parse_qs(parsed.query, keep_blank_values=True)
                unexpected = sorted(set(query) - {"room_type", "model_id"})
                if unexpected:
                    self._send_error(
                        f"unexpected node-art query parameter: {unexpected[0]}"
                    )
                    return
                room_values = query.get("room_type") or []
                model_values = query.get("model_id") or []
                if len(room_values) != 1 or not room_values[0]:
                    self._send_error("missing room_type")
                    return
                if len(model_values) > 1:
                    self._send_error("model_id must appear at most once")
                    return
                model_id = model_values[0] if model_values else None
                try:
                    art = self.node_art_resolver.resolve(
                        room_values[0], model_id=model_id
                    )
                except InvalidNodeArtModelError as exc:
                    self._send_error(str(exc))
                    return
                if art.kind != "original" or art.image_bytes is None:
                    self._send_json(
                        {"error": "node art not found", "art": art.to_dict()},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_bytes(art.image_bytes, "image/png")
            elif parsed.path == "/api/logs":
                self._send_json({"logs": list_log_files()})
            elif parsed.path == "/api/latest":
                path = latest_log_file()
                if path is None:
                    self._send_error("no logs found", HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"name": path.name, "progress": parse_log_file(path)})
            elif parsed.path == "/api/translations":
                query = parse_qs(parsed.query)
                lang = (query.get("lang") or ["zh"])[0]
                try:
                    self._send_json({"translations": get_translation_catalog(lang)})
                except Exception as exc:
                    self._send_json(
                        {
                            "translations": build_translation_catalog({}, {}, lang=lang),
                            "error": str(exc),
                        }
                    )
            elif parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                name = (query.get("name") or [""])[0]
                if not name:
                    self._send_error("missing log name")
                    return
                path = _safe_log_path(name)
                if not path.exists():
                    self._send_error("log not found", HTTPStatus.NOT_FOUND)
                    return
                self._send_json({"name": path.name, "progress": parse_log_file(path)})
            elif parsed.path == "/favicon.ico":
                self._send_text("", mimetypes.types_map.get(".ico", "image/x-icon"))
            else:
                self._send_error("not found", HTTPStatus.NOT_FOUND)
        except CatalogNotFoundError as exc:
            self._send_error(str(exc), HTTPStatus.NOT_FOUND)
        except CatalogError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive for interactive server
            self._send_internal_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/parse":
            self._send_error("not found", HTTPStatus.NOT_FOUND)
            return
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise CatalogError("invalid Content-Length") from exc
            if length < 0 or length > PARSE_BODY_MAX_BYTES:
                raise CatalogError("request body is too large")
            try:
                raw = self.rfile.read(length).decode("utf-8")
                payload = json.loads(
                    raw,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-standard numeric constant {value}")
                    ),
                )
            except UnicodeDecodeError as exc:
                raise CatalogError(f"invalid UTF-8 request body: {exc}") from exc
            except (json.JSONDecodeError, ValueError) as exc:
                raise CatalogError(f"invalid JSON request: {exc}") from exc
            if not isinstance(payload, dict):
                raise CatalogError("request must be a JSON object")
            source_name = payload.get("source_name", "uploaded.jsonl")
            text = payload.get("text", "")
            if not isinstance(source_name, str):
                raise CatalogError("source_name must be a string")
            if not isinstance(text, str):
                raise CatalogError("text must be a string")
            result = self.catalog.parse_upload(source_name, text)
            if result["view"] == "error":
                message = (result.get("errors") or ["could not parse upload"])[0]
                self._send_json(
                    {"error": message, "result": result}, HTTPStatus.BAD_REQUEST
                )
                return
            self._send_json(result)
        except CatalogError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_internal_error(exc)


def make_viewer_handler(
    catalog: RunCatalog,
    *,
    art_resolver: NodeArtResolver | None = None,
    map_service: MapService | None = None,
) -> type[ViewerHandler]:
    """Bind one catalog to an isolated handler class for a server instance."""

    class BoundViewerHandler(ViewerHandler):
        pass

    BoundViewerHandler.catalog = catalog
    if art_resolver is not None:
        BoundViewerHandler.node_art_resolver = art_resolver
    if map_service is not None:
        BoundViewerHandler.map_service = map_service
    return BoundViewerHandler


def serve(
    host: str,
    port: int,
    source_roots: list[Path] | tuple[Path, ...] | None = None,
    map_assets_dir: Path | None = None,
) -> None:
    roots = list(source_roots) if source_roots is not None else [LOG_DIR, ROOT / "data"]
    catalog = RunCatalog(
        roots,
        replay_parser=parse_game_progress,
        include_policy="workbench",
    )
    art_resolver = NodeArtResolver(
        explicit_roots=[map_assets_dir] if map_assets_dir is not None else ()
    )
    httpd = ThreadingHTTPServer(
        (host, port),
        make_viewer_handler(catalog, art_resolver=art_resolver),
    )
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"Run progress viewer: {url}", flush=True)
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--parse", type=Path, help="Parse one JSONL log and print normalized JSON")
    parser.add_argument(
        "--source-root",
        action="append",
        type=Path,
        help="Catalog source root (repeatable; defaults to logs and data)",
    )
    parser.add_argument(
        "--map-assets-dir",
        type=Path,
        help="Map artwork cache root (expects a map_icons subdirectory)",
    )
    args = parser.parse_args(argv)

    if args.parse:
        print(json.dumps(parse_log_file(args.parse), ensure_ascii=False, indent=2))
        return 0
    serve(
        args.host,
        args.port,
        source_roots=args.source_root,
        map_assets_dir=args.map_assets_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
