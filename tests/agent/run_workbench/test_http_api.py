from __future__ import annotations

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import agent.run_progress_viewer as viewer
from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench.assets import NodeArtResolver
from agent.run_workbench.catalog import RunCatalog
from agent.run_workbench.map_service import MapRequest, visited_route_map
from agent.run_workbench.map_service import (
    MapOutputError,
    MapServiceTimeoutError,
)

from .test_catalog import (
    _native,
    _replay,
    _replay_parser,
    _write_duplicate_version_source_eval_files,
    _write_jsonl,
)


@contextmanager
def _server(
    catalog: RunCatalog,
    *,
    art_resolver: NodeArtResolver | None = None,
    map_service: object | None = None,
):
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_viewer_handler(
            catalog,
            art_resolver=art_resolver,
            map_service=map_service,
        ),
    )
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _request(base: str, path: str, *, payload: object | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _raw_request(base: str, path: str, body: bytes) -> tuple[int, dict]:
    request = Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _binary_request(base: str, path: str) -> tuple[int, str, bytes]:
    request = Request(base + path)
    try:
        with urlopen(request, timeout=3) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except HTTPError as error:
        return error.code, error.headers.get_content_type(), error.read()


def _catalog(tmp_path: Path) -> RunCatalog:
    (tmp_path / "native.run").write_text(json.dumps(_native()), encoding="utf-8")
    _write_jsonl(tmp_path / "summary.jsonl", [{"event": "summary", "avg_floor": 8}])
    return RunCatalog([tmp_path], replay_parser=_replay_parser)


def _map_fixture() -> dict:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "run_workbench"
        / "map_v01032_partial.run"
    )
    run = json.loads(fixture.read_text(encoding="utf-8"))
    run["status"] = "dead"
    run["map_point_history"][0]["player_stats"] = [
        {"current_hp": 80, "max_hp": 80, "current_gold": 99}
    ]
    run["map_point_history"][1]["player_stats"] = [
        {
            "current_hp": 74,
            "max_hp": 80,
            "current_gold": 112,
            "damage_taken": 6,
            "cards_gained": ["CARD.STRIKE_PLUS"],
        }
    ]
    return run


def test_catalog_source_run_cohort_and_metrics_http_contracts(tmp_path: Path):
    catalog = _catalog(tmp_path)
    sources = catalog.list_sources()
    native = next(source for source in sources if source["source_kind"] == "native_run")
    summary = next(source for source in sources if source["source_kind"] == "summary")

    with _server(catalog) as base:
        status, catalog_payload = _request(base, "/api/catalog")
        assert status == 200
        assert len(catalog_payload["sources"]) == 2

        status, source_payload = _request(base, f"/api/source?id={native['source_id']}")
        assert status == 200
        assert source_payload["view"] == "run"

        status, summary_payload = _request(base, f"/api/source?id={summary['source_id']}")
        assert status == 200
        assert summary_payload["view"] == "summary"
        assert "rooms" not in summary_payload

        status, run_payload = _request(base, "/api/run?id=native-1")
        assert status == 200
        assert run_payload["run"]["run_id"] == "native-1"

        status, cohorts_payload = _request(base, "/api/cohorts")
        assert status == 200
        cohort_id = cohorts_payload["cohorts"][0]["cohort_id"]
        status, metrics_payload = _request(base, f"/api/metrics?current={cohort_id}")
        assert status == 200
        assert metrics_payload["comparison"] is None


def test_cohort_http_contract_provides_a_strictly_compatible_baseline(
    tmp_path: Path,
) -> None:
    records = []
    for checkpoint, timestamp in (("current", 40), ("baseline", 20)):
        for seed in ("a", "b"):
            records.append(
                {
                    "event": "eval_result",
                    "run_id": f"{checkpoint}-{seed}",
                    "status": "dead",
                    "max_global_floor": 8,
                    "character": "Ironclad",
                    "game_version": "v1",
                    "game_version_source": "cli",
                    "checkpoint": checkpoint,
                    "evaluation_mode": "fixed",
                    "scenario": "full_run",
                    "ascension": 0,
                    "seed": seed,
                    "ts": timestamp,
                }
            )
    _write_jsonl(tmp_path / "eval.jsonl", records)

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, cohorts_payload = _request(base, "/api/cohorts")
        assert status == 200
        current = cohorts_payload["cohorts"][0]
        baseline_id = current["default_baseline_cohort_id"]
        assert current["filters"]["checkpoint"] == "current"
        assert current["comparison_readiness"]["ready"] is True
        assert isinstance(baseline_id, str)

        status, metrics_payload = _request(
            base,
            f"/api/metrics?current={current['cohort_id']}&baseline={baseline_id}",
        )

    assert status == 200
    assert metrics_payload["comparison"]["comparable"] is True
    assert metrics_payload["comparison"]["paired"] is True


def test_cohorts_http_omits_unencodable_game_version_source(
    tmp_path: Path,
) -> None:
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "event": "eval_result",
                "run_id": "invalid-source",
                "status": "dead",
                "max_global_floor": 8,
                "character": "Ironclad",
                "game_version": "v1",
                "game_version_source": json.loads('"\\ud800"'),
                "checkpoint": "current",
                "evaluation_mode": "fixed",
                "scenario": "full_run",
                "ascension": 0,
                "seed": "a",
            }
        ],
    )

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(base, "/api/cohorts")

    assert status == 200
    assert payload["cohorts"][0]["filters"]["game_version_source"] is None


def test_cohorts_http_uses_all_duplicate_run_version_source_evidence(
    tmp_path: Path,
) -> None:
    _write_duplicate_version_source_eval_files(tmp_path, record_count=1)

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(base, "/api/cohorts")

    assert status == 200
    assert payload["cohorts"][0]["filters"]["game_version_source"] is None


def test_supported_native_run_map_returns_full_graph_route_art_and_visited_deltas(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    # The API must use catalog identity and canonical joining, not a filename convention.
    (tmp_path / "arbitrary-name.run").write_text(
        json.dumps(run), encoding="utf-8"
    )
    _write_jsonl(
        tmp_path / "joined-deck-history.jsonl",
        [
            {
                "event": "outcome",
                "run_id": run["run_id"],
                "status": "dead",
                "floor": 17,
            }
        ],
    )
    fixture_root = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "run_workbench"
        / "map_assets"
    )
    resolver = NodeArtResolver(
        explicit_roots=[fixture_root], environ={}, home=tmp_path / "home"
    )

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser), art_resolver=resolver
    ) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert payload["run_id"] == run["run_id"]
    assert payload["act"]["index"] == 0
    assert payload["act"]["act_id"] == "ACT.OVERGROWTH"
    assert payload["full_map"] is True
    assert payload["alignment"]["ok"] is True
    assert len(payload["nodes"]) > len(run["map_point_history"])
    assert payload["summary"] == {
        "node_count": len(payload["nodes"]),
        "edge_count": len(payload["edges"]),
        "visited_count": len(run["map_point_history"]),
        "terminal_node_id": payload["alignment"]["path_node_ids"][-1],
    }
    assert payload["acts"][0]["index"] == 0
    assert payload["acts"][0]["available"] is True

    visited = sorted(
        (node for node in payload["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    unvisited = [node for node in payload["nodes"] if not node["visited"]]
    assert len(visited) == len(run["map_point_history"])
    assert all("deltas" in node for node in visited)
    assert all("deltas" not in node and "rewards" not in node for node in unvisited)
    assert visited[1]["deltas"]["hp_change"] == {
        "value": -6,
        "quality": "derived",
    }
    assert visited[1]["deltas"]["cards_gained"] == {
        "value": ["CARD.STRIKE_PLUS"],
        "quality": "exact",
    }
    assert visited[1]["art"]["kind"] == "original"
    assert visited[-1]["terminal"] is True
    assert visited[-1]["terminal_status"] == "dead"
    assert all(node["terminal"] is False for node in visited[:-1])


def test_historical_replay_map_falls_back_to_recorded_route_with_reason(
    tmp_path: Path,
) -> None:
    _write_jsonl(tmp_path / "old-replay.jsonl", _replay("historic-route", floor=4))

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(
            base, "/api/run/map?id=historic-route&act=0"
        )

    assert status == 200
    assert payload["full_map"] is False
    assert payload["visited_route"] is True
    assert payload["fallback_reason"]
    assert payload["alignment"]["ok"] is False
    assert [node["path_index"] for node in payload["nodes"]] == [0]
    assert payload["nodes"][0]["visited"] is True
    assert payload["nodes"][0]["terminal"] is True


@pytest.mark.parametrize(
    ("multiplayer_value", "reason_fragment"),
    [(True, "multiplayer"), (None, "multiplayer flag")],
)
def test_native_map_never_claims_full_graph_without_exact_single_player_metadata(
    tmp_path: Path, multiplayer_value: bool | None, reason_fragment: str
) -> None:
    run = _map_fixture()
    if multiplayer_value is None:
        run.pop("is_multiplayer")
    else:
        run["is_multiplayer"] = multiplayer_value
    (tmp_path / "native.run").write_text(json.dumps(run), encoding="utf-8")

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert payload["full_map"] is False
    assert reason_fragment in payload["fallback_reason"].lower()


def test_joined_native_modifiers_reach_the_map_service_request(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["modifiers"] = ["MODIFIER.BIG_GAME_HUNTER"]
    (tmp_path / "native.run").write_text(json.dumps(run), encoding="utf-8")
    _write_jsonl(
        tmp_path / "joined-deck.jsonl",
        [{"event": "outcome", "run_id": run["run_id"], "status": "dead"}],
    )

    class CapturingMapService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="captured request")

    service = CapturingMapService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, _ = _request(base, f"/api/run/map?id={run['run_id']}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert service.requests[0].modifiers == ("MODIFIER.BIG_GAME_HUNTER",)


def test_map_request_prefers_native_metadata_over_earlier_named_replay(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["seed"] = "native-seed"
    run["ascension"] = 2
    run["modifiers"] = ["MODIFIER.BIG_GAME_HUNTER"]
    (tmp_path / "z-native.run").write_text(json.dumps(run), encoding="utf-8")
    _write_jsonl(tmp_path / "a-replay.jsonl", _replay(run["run_id"], floor=3))

    def replay_parser(records: list[dict], source_name: str | None = None) -> dict:
        return {
            "summary": {
                "run_id": run["run_id"],
                "seed": "replay-seed",
                "ascension": 10,
                "game_version": "v0.104.0",
            },
            "rooms": [{"id": "replay-room", "global_floor": 3}],
        }

    class CapturingMapService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="captured request")

    service = CapturingMapService()
    with _server(
        RunCatalog([tmp_path], replay_parser=replay_parser),
        map_service=service,
    ) as base:
        status, _ = _request(base, f"/api/run/map?id={run['run_id']}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.seed == "native-seed"
    assert request.ascension == 2
    assert request.modifiers == ("MODIFIER.BIG_GAME_HUNTER",)
    assert request.is_multiplayer is False


def test_dead_supported_prefix_uses_partial_alignment_for_a_full_graph(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["status"] = "dead"
    run["map_point_history"] = run["map_point_history"][:-1]
    (tmp_path / "dead-prefix.run").write_text(json.dumps(run), encoding="utf-8")

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert payload["full_map"] is True
    assert payload["alignment"]["ok"] is True
    assert payload["summary"]["visited_count"] == len(run["map_point_history"])


def test_winning_supported_prefix_still_requires_the_boss(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["status"] = "win"
    run["victory"] = True
    run["map_point_history"] = run["map_point_history"][:-1]
    (tmp_path / "win-prefix.run").write_text(json.dumps(run), encoding="utf-8")

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert payload["full_map"] is False
    assert "boss" in payload["fallback_reason"].lower()


@pytest.mark.parametrize("status_value", ["dead", "in_progress", "crash", "invalid"])
def test_only_final_nonwinning_act_allows_partial_alignment(
    tmp_path: Path, status_value: str
) -> None:
    run = {
        "run_id": f"partial-{status_value}",
        "build_id": "v0.103.2",
        "seed": "two-act-seed",
        "ascension": 0,
        "modifiers": [],
        "is_multiplayer": False,
        "status": status_value,
        "players": [{"character": "IRONCLAD"}],
        "acts": [{"id": "ACT.OVERGROWTH"}, {"id": "ACT.HIVE"}],
        "map_point_history": [
            [{"map_point_type": "ancient"}, {"map_point_type": "boss"}],
            [{"map_point_type": "ancient"}, {"map_point_type": "monster"}],
        ],
    }
    (tmp_path / "two-act.run").write_text(json.dumps(run), encoding="utf-8")

    class CapturingMapService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="captured request")

    service = CapturingMapService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        first_status, _ = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )
        final_status, _ = _request(
            base, f"/api/run/map?id={run['run_id']}&act=1"
        )

    assert first_status == final_status == 200
    assert [request.allow_partial_path for request in service.requests] == [
        False,
        True,
    ]


@pytest.mark.parametrize(
    "error",
    [
        MapServiceTimeoutError("/private/secret/node timed out"),
        MapOutputError("invalid output from /private/secret/map_cli.js"),
    ],
)
def test_map_service_operational_errors_fall_back_without_losing_annotations(
    tmp_path: Path, error: Exception
) -> None:
    run = _map_fixture()
    (tmp_path / "native.run").write_text(json.dumps(run), encoding="utf-8")
    fixture_root = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "run_workbench"
        / "map_assets"
    )
    resolver = NodeArtResolver(
        explicit_roots=[fixture_root], environ={}, home=tmp_path / "home"
    )

    class ExplodingMapService:
        def generate(self, request: MapRequest):
            raise error

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        art_resolver=resolver,
        map_service=ExplodingMapService(),
    ) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert payload["full_map"] is False
    assert payload["visited_route"] is True
    assert payload["fallback_reason"]
    assert "/private/secret" not in payload["fallback_reason"]
    assert all("deltas" in node and "art" in node for node in payload["nodes"])
    assert any(
        node["art"]["kind"] == "original"
        for node in payload["nodes"]
        if node["room_type"] == "Monster"
    )


def test_only_the_globally_last_route_node_is_terminal_across_acts(
    tmp_path: Path,
) -> None:
    run = {
        "run_id": "two-act-terminal",
        "build_id": "v0.104.0",
        "seed": "two-act-seed",
        "ascension": 0,
        "modifiers": [],
        "is_multiplayer": False,
        "status": "dead",
        "players": [{"character": "IRONCLAD"}],
        "acts": [{"id": "ACT.OVERGROWTH"}, {"id": "ACT.HIVE"}],
        "map_point_history": [
            [
                {"map_point_type": "ancient"},
                {"map_point_type": "boss"},
            ],
            [
                {"map_point_type": "ancient"},
                {"map_point_type": "monster"},
            ],
        ],
    }
    (tmp_path / "two-act.run").write_text(json.dumps(run), encoding="utf-8")

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        first_status, first = _request(
            base, "/api/run/map?id=two-act-terminal&act=0"
        )
        final_status, final = _request(
            base, "/api/run/map?id=two-act-terminal&act=1"
        )

    assert first_status == final_status == 200
    assert first["summary"]["terminal_node_id"] is None
    assert all(node["terminal"] is False for node in first["nodes"])
    assert all(node["terminal_status"] is None for node in first["nodes"])
    terminal_nodes = [node for node in final["nodes"] if node["terminal"]]
    assert len(terminal_nodes) == 1
    assert terminal_nodes[0]["path_index"] == 1
    assert terminal_nodes[0]["terminal_status"] == "dead"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/api/run/map", "missing run id"),
        ("/api/run/map?id=native-1&act=x", "act must be an integer"),
        ("/api/run/map?id=native-1&act=-1", "act must be between"),
        ("/api/run/map?id=native-1&act=0&source=anything", "unexpected map query"),
    ],
)
def test_run_map_query_is_strictly_validated(
    tmp_path: Path, path: str, message: str
) -> None:
    with _server(_catalog(tmp_path)) as base:
        status, payload = _request(base, path)

    assert status == 400
    assert message in payload["error"]


def test_node_art_endpoint_streams_only_a_resolved_known_png(tmp_path: Path):
    fixture_root = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "run_workbench"
        / "map_assets"
    )
    expected = (fixture_root / "map_icons" / "map_monster.png").read_bytes()
    resolver = NodeArtResolver(
        explicit_roots=[fixture_root],
        environ={},
        home=tmp_path,
    )

    with _server(_catalog(tmp_path), art_resolver=resolver) as base:
        status, content_type, body = _binary_request(
            base, "/api/node-art?room_type=monster"
        )

    assert status == 200
    assert content_type == "image/png"
    assert body == expected


def test_missing_node_art_is_a_bounded_json_404_with_fallback_descriptor(
    tmp_path: Path,
) -> None:
    resolver = NodeArtResolver(explicit_roots=[tmp_path], environ={}, home=tmp_path)

    with _server(_catalog(tmp_path), art_resolver=resolver) as base:
        status, content_type, body = _binary_request(
            base, "/api/node-art?room_type=elite"
        )

    payload = json.loads(body)
    assert status == 404
    assert content_type == "application/json"
    assert payload["error"] == "node art not found"
    assert payload["art"]["kind"] == "emoji"
    assert payload["art"]["emoji"] == "👹"
    assert payload["art"]["letter"] == "E"


def test_node_art_endpoint_serves_verified_bytes_after_path_replacement(
    tmp_path: Path,
) -> None:
    icons = tmp_path / "assets/map_icons"
    icons.mkdir(parents=True)
    candidate = icons / "map_monster.png"
    original = b"\x89PNG\r\n\x1a\noriginal"
    replacement = b"\x89PNG\r\n\x1a\nreplacement"
    candidate.write_bytes(original)
    resolver = NodeArtResolver(
        explicit_roots=[tmp_path / "assets"], environ={}, home=tmp_path / "home"
    )
    verified_art = resolver.resolve("monster")

    outside = tmp_path / "outside.png"
    outside.write_bytes(replacement)
    candidate.unlink()
    candidate.symlink_to(outside)

    class FixedResolver(NodeArtResolver):
        def resolve(self, room_type: str, model_id: str | None = None):
            return verified_art

    with _server(_catalog(tmp_path), art_resolver=FixedResolver()) as base:
        with urlopen(base + "/api/node-art?room_type=monster", timeout=3) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers.get_content_type() == "image/png"
            assert response.headers["Content-Length"] == str(len(original))

    assert verified_art.image_bytes == original
    assert body == original
    assert body != replacement


def test_node_art_endpoint_returns_fallback_after_invalid_symlink_replacement(
    tmp_path: Path,
) -> None:
    icons = tmp_path / "assets/map_icons"
    icons.mkdir(parents=True)
    candidate = icons / "map_monster.png"
    candidate.write_bytes(b"\x89PNG\r\n\x1a\noriginal")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not a png")
    candidate.unlink()
    candidate.symlink_to(outside)
    resolver = NodeArtResolver(
        explicit_roots=[tmp_path / "assets"], environ={}, home=tmp_path / "home"
    )

    with _server(_catalog(tmp_path), art_resolver=resolver) as base:
        status, content_type, body = _binary_request(
            base, "/api/node-art?room_type=monster"
        )

    payload = json.loads(body)
    assert status == 404
    assert content_type == "application/json"
    assert payload["art"]["kind"] == "emoji"
    assert payload["art"]["image_url"] is None


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/node-art", 400),
        ("/api/node-art?room_type=ancient&model_id=%252e%252e%252fsecret", 400),
        ("/api/node-art?room_type=monster&path=/etc/passwd", 400),
    ],
)
def test_node_art_endpoint_rejects_missing_or_path_like_queries(
    tmp_path: Path, path: str, status: int
) -> None:
    resolver = NodeArtResolver(explicit_roots=[tmp_path], environ={}, home=tmp_path)

    with _server(_catalog(tmp_path), art_resolver=resolver) as base:
        actual_status, content_type, body = _binary_request(base, path)

    assert actual_status == status
    assert content_type == "application/json"
    assert "error" in json.loads(body)


def test_map_assets_cli_option_is_passed_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        viewer,
        "serve",
        lambda host, port, source_roots=None, map_assets_dir=None: calls.append(
            {
                "host": host,
                "port": port,
                "source_roots": source_roots,
                "map_assets_dir": map_assets_dir,
            }
        ),
    )

    assert viewer.main(["--port", "0", "--map-assets-dir", str(tmp_path)]) == 0
    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 0,
            "source_roots": None,
            "map_assets_dir": tmp_path,
        }
    ]


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        ("/api/source", 400, "missing source id"),
        ("/api/source?id=missing", 404, "unknown source id"),
        ("/api/run", 400, "missing run id"),
        ("/api/run?id=missing", 404, "unknown run id"),
        ("/api/metrics", 400, "missing current cohort"),
        ("/api/metrics?current=missing", 404, "unknown cohort id"),
    ],
)
def test_http_errors_use_stable_json_statuses(
    tmp_path: Path, path: str, status: int, message: str
):
    with _server(_catalog(tmp_path)) as base:
        actual_status, payload = _request(base, path)

    assert actual_status == status
    assert message in payload["error"]


def test_unexpected_http_error_is_logged_but_not_exposed(tmp_path: Path):
    secret = "/private/secret/catalog.jsonl"

    class ExplodingCatalog:
        def list_sources(self):
            raise OSError(5, "backend exploded", secret)

    with _server(ExplodingCatalog()) as base:
        status, payload = _request(base, "/api/catalog")

    assert status == 500
    assert payload == {"error": "internal server error"}
    assert secret not in json.dumps(payload)


@pytest.mark.parametrize(
    ("payload", "status", "message"),
    [
        ([], 400, "request must be a JSON object"),
        ({"source_name": 5, "text": ""}, 400, "source_name must be a string"),
        ({"source_name": "x.jsonl", "text": []}, 400, "text must be a string"),
        ({"source_name": "bad.jsonl", "text": "{bad"}, 400, "bad.jsonl:1"),
    ],
)
def test_parse_upload_http_validation(
    tmp_path: Path, payload: object, status: int, message: str
):
    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        actual_status, body = _request(base, "/api/parse", payload=payload)

    assert actual_status == status
    assert message in body["error"]


def test_parse_upload_invalid_utf8_is_json_bad_request(tmp_path: Path):
    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, body = _raw_request(base, "/api/parse", b'\xff\xfe{"text":"x"}')

    assert status == 400
    assert "error" in body
    assert "UTF-8" in body["error"]


def test_parse_upload_http_supports_native_replay_and_summary(tmp_path: Path):
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    uploads = [
        {"source_name": "native.run", "text": json.dumps(_native("up-native"))},
        {
            "source_name": "replay.jsonl",
            "text": "\n".join(json.dumps(row) for row in _replay("up-replay")),
        },
        {
            "source_name": "summary.jsonl",
            "text": json.dumps({"event": "summary", "avg_floor": 4}),
        },
    ]
    with _server(catalog) as base:
        responses = [_request(base, "/api/parse", payload=payload) for payload in uploads]

    assert [status for status, _ in responses] == [200, 200, 200]
    assert [body["view"] for _, body in responses] == ["run", "run", "summary"]
    assert "progress" in responses[1][1]
    assert list(tmp_path.iterdir()) == []


def test_legacy_http_endpoints_remain_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_jsonl(log_dir / "legacy.jsonl", _replay("legacy"))
    monkeypatch.setattr(viewer, "LOG_DIR", log_dir)
    monkeypatch.setattr(viewer, "get_translation_catalog", lambda lang: {"lang": lang})
    catalog = RunCatalog([log_dir], replay_parser=viewer.parse_game_progress)

    with _server(catalog) as base:
        logs_status, logs = _request(base, "/api/logs")
        latest_status, latest = _request(base, "/api/latest")
        log_status, log = _request(base, "/api/log?name=legacy.jsonl")
        translations_status, translations = _request(base, "/api/translations?lang=zh")

    assert logs_status == latest_status == log_status == translations_status == 200
    assert logs["logs"][0]["name"] == "legacy.jsonl"
    assert latest["name"] == "legacy.jsonl"
    assert log["name"] == "legacy.jsonl"
    assert translations["translations"] == {"lang": "zh"}
