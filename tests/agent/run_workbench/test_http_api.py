from __future__ import annotations

from copy import deepcopy
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
from agent.run_workbench.models import ActMap

from .test_catalog import (
    _native,
    _recorded_map_route_snapshot,
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


def _branched_recorded_snapshot(
    run_id: str,
    *,
    act: int,
    ts: int | float,
    visit_boss: bool = False,
) -> dict:
    snapshot = _recorded_map_route_snapshot(
        run_id,
        act=act,
        ts=ts,
        route_length=3,
    )
    snapshot["game_version"] = "v0.107.1"
    boss = snapshot["map"]["boss"]
    snapshot["map"]["rows"][0][0]["children"].append({"col": 1, "row": 1})
    snapshot["map"]["rows"][1].append(
        {
            "col": 1,
            "row": 1,
            "type": "Shop",
            "children": [{"col": boss["col"], "row": boss["row"]}],
            "visited": False,
            "current": False,
        }
    )
    changed = snapshot["visited_nodes"][1]
    changed["exit_player"] = deepcopy(changed["entry_player"])
    changed["exit_player"].update(hp=73, gold=25)
    changed["exit_player"]["deck"].append({"id": "CARD.BASH"})
    if visit_boss:
        snapshot["map"]["rows"][2][0]["current"] = False
        boss.update(visited=True, current=True)
        snapshot["map"]["current_coord"] = {
            "col": boss["col"],
            "row": boss["row"],
        }
        player = deepcopy(snapshot["visited_nodes"][-1]["exit_player"])
        snapshot["visited_nodes"].append(
            {
                "col": boss["col"],
                "row": boss["row"],
                "type": "Boss",
                "entry_player": deepcopy(player),
                "exit_player": player,
            }
        )
    return snapshot


def _recorded_decision(
    selected_label: str,
    *,
    selected_id: str = "EVENT.TRUSTED",
    effect: str = "失去 6 点生命，获得一张牌",
) -> dict:
    return {
        "kind": "event",
        "selected_id": selected_id,
        "selected_label": selected_label,
        "options": [
            {
                "id": selected_id,
                "label": selected_label,
                "effect": effect,
                "selected": True,
            },
            {
                "id": "EVENT.LEAVE",
                "label": "离开",
                "effect": "不获得任何奖励",
                "selected": False,
            },
        ],
        "evidence": "recorded",
    }


def _native_route_matching_recorded_coordinates(
    run_id: str,
    *,
    middle_room_type: str,
    boss_model_id: str | None = None,
) -> dict:
    player_stats = [{"current_hp": 80, "max_hp": 80, "current_gold": 10}]
    route = [
        {
            "map_point_type": " ancient ",
            "col": 0,
            "row": 0,
            "player_stats": deepcopy(player_stats),
        },
        {
            "map_point_type": middle_room_type,
            "col": 0,
            "row": 1,
            "player_stats": deepcopy(player_stats),
        },
        {
            "map_point_type": "MONSTER",
            "col": 0,
            "row": 2,
            "player_stats": deepcopy(player_stats),
        },
    ]
    if boss_model_id is not None:
        route.append(
            {
                "map_point_type": "boss",
                "col": 0,
                "row": 16,
                "rooms": [{"model_id": boss_model_id}],
                "player_stats": deepcopy(player_stats),
            }
        )
    return {
        "run_id": run_id,
        "build_id": "v0.107.1",
        "seed": "joined-native-recorded",
        "ascension": 0,
        "modifiers": [],
        "is_multiplayer": False,
        "status": "dead",
        "players": [{"character": "IRONCLAD"}],
        "acts": [{"id": "ACT.OVERGROWTH"}],
        "map_point_history": route,
    }


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


def test_cohorts_http_keeps_valid_cohort_when_unrelated_metadata_has_surrogates(
    tmp_path: Path,
) -> None:
    base_record = {
        "event": "eval_result",
        "status": "dead",
        "max_global_floor": 8,
        "character": "Ironclad",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "ascension": 0,
    }
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                **base_record,
                "run_id": "normal",
                "game_version": "v1",
                "checkpoint": "normal",
                "seed": "a",
            },
            {
                **base_record,
                "run_id": "malformed",
                "game_version": json.loads('"\\ud800"'),
                "checkpoint": "inspection-only",
                "seed": json.loads('"\\udfff"'),
            },
        ],
    )

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, payload = _request(base, "/api/cohorts")

    assert status == 200
    normal = next(
        row for row in payload["cohorts"] if row["filters"]["checkpoint"] == "normal"
    )
    invalid = next(
        row
        for row in payload["cohorts"]
        if row["filters"]["checkpoint"] == "inspection-only"
    )
    assert normal["comparison_readiness"]["ready"] is True
    assert invalid["comparison_readiness"]["ready"] is False
    assert invalid["filters"]["game_version"] is None
    json.dumps(payload, ensure_ascii=False).encode("utf-8", errors="strict")


def test_surrogate_run_id_is_not_exposed_by_http_catalog_or_metrics(
    tmp_path: Path,
) -> None:
    surrogate = json.loads('"\\ud800"')
    common = {
        "event": "eval_result",
        "status": "dead",
        "max_global_floor": 8,
        "character": "Ironclad",
        "game_version": "v1",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "ascension": 0,
        "ts": 1,
    }
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                **common,
                "run_id": "normal-run",
                "checkpoint": "normal",
                "seed": "seed-a",
            },
            {
                **common,
                "run_id": surrogate,
                "checkpoint": "unrelated",
                "seed": "seed-b",
            },
        ],
    )

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, content_type, body = _binary_request(base, "/api/cohorts")
        payload = json.loads(body)
        unrelated = next(
            row
            for row in payload["cohorts"]
            if row["filters"]["checkpoint"] == "unrelated"
        )
        metrics_status, metrics = _request(
            base, f"/api/metrics?current={unrelated['cohort_id']}"
        )

    assert status == 200
    assert content_type == "application/json"
    assert body.decode("utf-8", errors="strict")
    assert any(
        row["filters"]["checkpoint"] == "normal" for row in payload["cohorts"]
    )
    assert unrelated["run_ids"] == []
    assert unrelated["representative_run_ids"] == []
    assert metrics_status == 200
    assert all(point["run_id"] != surrogate for point in metrics["current"]["trend"])


def test_http_ascii_fail_safe_round_trips_valid_chinese_payload(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "训练.jsonl",
        [{"event": "summary", "label": "对局"}],
    )

    with _server(RunCatalog([tmp_path], replay_parser=_replay_parser)) as base:
        status, content_type, body = _binary_request(base, "/api/catalog")

    assert status == 200
    assert content_type == "application/json"
    assert b"\\u8bad\\u7ec3.jsonl" in body
    payload = json.loads(body)
    assert payload["sources"][0]["display_name"] == "训练.jsonl"


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


def test_recorded_maps_are_authoritative_across_sparse_acts_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "recorded-v01071-sparse-acts"
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=1, ts=1),
            _branched_recorded_snapshot(run_id, act=3, ts=2),
            {
                "event": "outcome",
                "run_id": run_id,
                "game_version": "v0.107.1",
                "status": "dead",
                "max_global_floor": 37,
            },
        ],
    )

    class CountingCatalog:
        def __init__(self, catalog: RunCatalog) -> None:
            self.catalog = catalog
            self.get_run_calls = 0

        def get_run(self, requested_run_id: str) -> dict:
            self.get_run_calls += 1
            return self.catalog.get_run(requested_run_id)

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    parser_calls: list[list[dict]] = []
    original_latest_recorded_acts = viewer.latest_recorded_acts

    def counted_latest_recorded_acts(rows):
        inspected = list(rows)
        parser_calls.append(inspected)
        assert all(row["event"] == "map_snapshot" for row in inspected)
        assert all(
            row["_workbench_evidence_kind"] == "deck_history_event"
            for row in inspected
        )
        assert all(
            any(
                item.get("source_kind") == "deck_history"
                for item in row["_workbench_provenance"]
            )
            for row in inspected
        )
        return original_latest_recorded_acts(iter(inspected))

    monkeypatch.setattr(
        viewer,
        "latest_recorded_acts",
        counted_latest_recorded_acts,
    )
    catalog = CountingCatalog(RunCatalog([tmp_path], replay_parser=_replay_parser))
    with _server(catalog, map_service=MustNotGenerate()) as base:
        first_status, first = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )
        third_status, third = _request(
            base, f"/api/run/map?id={run_id}&act=2"
        )
        missing_status, missing = _request(
            base, f"/api/run/map?id={run_id}&act=1"
        )

    assert first_status == third_status == 200
    assert missing_status == 404
    assert "no act 1" in missing["error"]
    assert catalog.get_run_calls == 3
    assert len(parser_calls) == 2
    assert all(len(rows) == 2 for rows in parser_calls)
    expected_keys = {
        "act_id",
        "full_map",
        "visited_route",
        "fallback_reason",
        "nodes",
        "edges",
        "alignment",
        "run_id",
        "act",
        "acts",
        "summary",
    }
    assert set(first) == set(third) == expected_keys
    assert first["run_id"] == third["run_id"] == run_id
    assert first["full_map"] is third["full_map"] is True
    assert first["visited_route"] is third["visited_route"] is True
    assert first["fallback_reason"] is third["fallback_reason"] is None
    assert first["summary"]["node_count"] == third["summary"]["node_count"] == 5
    assert first["summary"]["edge_count"] == third["summary"]["edge_count"] == 5
    assert first["summary"]["visited_count"] == third["summary"]["visited_count"] == 3
    assert [
        (act["index"], act["act_id"], act["available"], act["visited_count"])
        for act in first["acts"]
    ] == [
        (0, "RECORDED.ACT.1", True, 3),
        (2, "RECORDED.ACT.3", True, 3),
    ]
    assert first["acts"] == third["acts"]
    assert first["act"]["index"] == 0
    assert third["act"]["index"] == 2

    first_visited = sorted(
        (node for node in first["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    third_visited = sorted(
        (node for node in third["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert [node["path_index"] for node in first_visited] == [0, 1, 2]
    assert [node["recorded_node_id"] for node in first_visited] == [
        "a0:n0",
        "a0:n1",
        "a0:n2",
    ]
    assert first_visited[1]["deltas"]["hp_change"] == {
        "value": -7,
        "quality": "derived",
    }
    assert first_visited[1]["deltas"]["gold_change"] == {
        "value": 15,
        "quality": "derived",
    }
    assert first_visited[1]["deltas"]["cards_gained"] == {
        "value": [{"id": "CARD.BASH"}],
        "quality": "derived",
    }
    assert any(not node["visited"] for node in first["nodes"])
    assert all(
        "deltas" not in node and "recorded_node_id" not in node
        for node in first["nodes"]
        if not node["visited"]
    )
    assert all("art" in node for node in first["nodes"] + third["nodes"])
    assert first["summary"]["terminal_node_id"] is None
    assert all(node["terminal"] is False for node in first["nodes"])
    terminal = [node for node in third_visited if node["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["path_index"] == 2
    assert terminal[0]["recorded_node_id"] == "a2:n2"
    assert terminal[0]["terminal_status"] == "dead"
    assert third["summary"]["terminal_node_id"] == terminal[0]["id"]


def test_canonical_route_nodes_prefer_native_per_act_and_drop_invalid_acts(
) -> None:
    def route_node(
        token: str,
        node_id: str,
        source_kind: str,
        **fields: object,
    ) -> dict:
        return {
            "token": token,
            "id": node_id,
            "_workbench_evidence_kind": "route_node",
            "_workbench_provenance": [
                {"source_id": token, "source_kind": source_kind}
            ],
            **fields,
        }

    native_act_one = route_node("native-a0", "a0:n0", "native_run")
    recorded_act_one = route_node(
        "recorded-a0", "a0:n0", "deck_history", act=1, act_index=0
    )
    recorded_act_two = route_node(
        "recorded-a1", "a1:n0", "deck_history", act=2, act_index=1
    )
    unknown_act = route_node("unknown", "custom", "deck_history")
    out_of_range_act = route_node("out-of-range", "a9:n0", "deck_history")
    conflicting_act = route_node(
        "conflicting", "a2:n0", "deck_history", act_index=1
    )

    selected = viewer._canonical_route_nodes(
        {
            "nodes": [
                native_act_one,
                recorded_act_one,
                recorded_act_two,
                unknown_act,
                out_of_range_act,
                conflicting_act,
            ]
        }
    )

    assert [node["token"] for node in selected] == [
        "native-a0",
        "recorded-a1",
    ]


def test_final_canonical_act_index_uses_maximum_valid_act_not_node_order(
) -> None:
    nodes = [
        {"id": "a0:n0"},
        {"id": "a2:n0"},
        {"id": "a1:n0"},
        {"id": "a9:n0"},
        {"id": "custom"},
        {"id": "a1:n1", "act_index": 0},
    ]

    assert viewer._final_canonical_act_index(nodes) == 2


def test_joined_native_and_recorded_routes_prefer_authority_per_act(
    tmp_path: Path,
) -> None:
    run_id = "native-act-one-recorded-act-two"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=2, ts=1),
            {
                "event": "outcome",
                "run_id": run_id,
                "game_version": "v0.107.1",
                "status": "dead",
            },
        ],
    )

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="native act fallback")

    service = CapturingFallbackService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        first_status, first = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )
        second_status, second = _request(
            base, f"/api/run/map?id={run_id}&act=1"
        )

    assert first_status == second_status == 200
    assert [request.act_index for request in service.requests] == [0]
    assert len(service.requests[0].visited) == 3
    assert first["full_map"] is False
    assert first["visited_route"] is True
    assert first["fallback_reason"] == "native act fallback"
    assert second["full_map"] is True
    assert second["visited_route"] is True
    assert second["fallback_reason"] is None
    assert [
        (act["index"], act["available"], act["visited_count"])
        for act in first["acts"]
    ] == [(0, True, 3), (1, True, 3)]
    assert first["acts"] == second["acts"]
    assert first["summary"]["visited_count"] == 3
    assert second["summary"]["visited_count"] == 3
    assert first["summary"]["terminal_node_id"] is None
    assert all(node["terminal"] is False for node in first["nodes"])
    second_visited = sorted(
        (node for node in second["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert [node["recorded_node_id"] for node in second_visited] == [
        "a1:n0",
        "a1:n1",
        "a1:n2",
    ]
    assert second["summary"]["terminal_node_id"] == second_visited[-1]["id"]
    assert second_visited[-1]["terminal"] is True
    assert second_visited[-1]["terminal_status"] == "dead"


@pytest.mark.parametrize(
    ("status_value", "expected_terminal_status", "expected_partial"),
    [
        ("dead", "dead", True),
        ("win", "win", False),
        ("in_progress", "in_progress", True),
    ],
    ids=["dead", "win", "current"],
)
def test_terminal_uses_highest_act_when_mixed_sources_are_out_of_order(
    tmp_path: Path,
    status_value: str,
    expected_terminal_status: str,
    expected_partial: bool,
) -> None:
    run_id = f"out-of-order-terminal-{status_value}"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    act_one_route = native["map_point_history"]
    native.update(
        status=status_value,
        acts=[{"id": "ACT.ONE"}, {}, {"id": "ACT.THREE"}],
        map_point_history=[
            act_one_route,
            [],
            deepcopy(act_one_route),
        ],
    )
    if status_value == "win":
        native["victory"] = True
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    outcome = {
        "event": "outcome",
        "run_id": run_id,
        "game_version": "v0.107.1",
        "status": status_value,
    }
    if status_value == "win":
        outcome["victory"] = True
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=2, ts=1),
            outcome,
        ],
    )

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="native act fallback")

    service = CapturingFallbackService()
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    canonical = catalog.get_run(run_id)["run"]
    assert [
        viewer._canonical_route_node_act_index(node)
        for node in viewer._canonical_route_nodes(canonical)
    ] == [0, 0, 0, 2, 2, 2, 1, 1, 1]

    with _server(catalog, map_service=service) as base:
        first_status, first = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )
        second_status, second = _request(
            base, f"/api/run/map?id={run_id}&act=1"
        )
        third_status, third = _request(
            base, f"/api/run/map?id={run_id}&act=2"
        )

    assert first_status == second_status == third_status == 200
    assert [request.act_index for request in service.requests] == [0, 2]
    assert service.requests[0].allow_partial_path is False
    assert service.requests[1].allow_partial_path is expected_partial
    assert [
        (act["index"], act["available"], act["visited_count"])
        for act in third["acts"]
    ] == [(0, True, 3), (1, True, 3), (2, True, 3)]
    assert first["summary"]["terminal_node_id"] is None
    assert second["summary"]["terminal_node_id"] is None
    assert all(node["terminal"] is False for node in first["nodes"])
    assert all(node["terminal"] is False for node in second["nodes"])
    terminal = [node for node in third["nodes"] if node["terminal"]]
    assert len(terminal) == 1
    assert terminal[0]["path_index"] == 2
    assert terminal[0]["recorded_node_id"] == "a2:n2"
    assert terminal[0]["terminal_status"] == expected_terminal_status
    assert third["summary"]["terminal_node_id"] == terminal[0]["id"]


def test_recorded_map_uses_valid_older_snapshot_when_newest_is_malformed(
    tmp_path: Path,
) -> None:
    run_id = "recorded-valid-before-malformed"
    valid = _branched_recorded_snapshot(run_id, act=1, ts=1)
    malformed = deepcopy(valid)
    malformed["ts"] = 2
    malformed["map"].pop("type")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            valid,
            malformed,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=MustNotGenerate(),
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert payload["full_map"] is True
    assert payload["visited_route"] is True
    assert payload["fallback_reason"] is None
    assert payload["summary"]["node_count"] == 5
    assert payload["summary"]["edge_count"] == 5
    assert "warnings" not in payload
    assert "errors" not in payload


def test_recorded_decision_http_uses_only_authoritative_route_and_detaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "recorded-decision-authoritative-only"
    trusted = [_recorded_decision("可信选择")]
    forged = [_recorded_decision("伪造选择", selected_id="EVENT.FORGED")]
    snapshot = _branched_recorded_snapshot(run_id, act=1, ts=1)
    snapshot["visited_nodes"][0]["decisions"] = []
    snapshot["visited_nodes"][1]["decisions"] = deepcopy(trusted)
    snapshot["map"]["rows"][1][1].update(
        decisions=deepcopy(forged),
        options=deepcopy(forged[0]["options"]),
        choices=deepcopy(forged[0]["options"]),
    )
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            snapshot,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )
    canonical = RunCatalog(
        [tmp_path], replay_parser=_replay_parser
    ).get_run(run_id)
    canonical_route = [
        node
        for node in canonical["run"]["nodes"]
        if node.get("_workbench_evidence_kind") == "route_node"
    ]
    for canonical_node in canonical_route:
        canonical_node.update(
            decisions=deepcopy(forged),
            options=deepcopy(forged[0]["options"]),
            choices=deepcopy(forged[0]["options"]),
        )
    raw_snapshot = next(
        node
        for node in canonical["run"]["nodes"]
        if node.get("event") == "map_snapshot"
    )
    raw_snapshot.update(
        decisions=deepcopy(forged),
        options=deepcopy(forged[0]["options"]),
        choices=deepcopy(forged[0]["options"]),
    )

    class StaticCatalog:
        def get_run(self, requested_run_id: str) -> dict:
            assert requested_run_id == run_id
            return canonical

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    parsed_snapshots = []
    original_latest_recorded_acts = viewer.latest_recorded_acts

    def captured_latest_recorded_acts(rows):
        result = original_latest_recorded_acts(rows)
        parsed_snapshots.extend(result[0].values())
        return result

    monkeypatch.setattr(
        viewer,
        "latest_recorded_acts",
        captured_latest_recorded_acts,
    )
    original_act_map_to_dict = ActMap.to_dict

    def forged_act_map_to_dict(act_map):
        payload = original_act_map_to_dict(act_map)
        for graph_node in payload["nodes"]:
            graph_node.update(
                decisions=deepcopy(forged),
                options=deepcopy(forged[0]["options"]),
                choices=deepcopy(forged[0]["options"]),
            )
        return payload

    monkeypatch.setattr(
        ActMap,
        "to_dict",
        forged_act_map_to_dict,
    )
    with _server(StaticCatalog(), map_service=MustNotGenerate()) as base:
        status, first = _request(base, f"/api/run/map?id={run_id}&act=0")
        visited = sorted(
            (node for node in first["nodes"] if node["visited"]),
            key=lambda node: node["path_index"],
        )
        assert status == 200
        assert "decisions" not in visited[0]
        assert visited[1]["decisions"] == trusted
        assert all(
            "options" not in node
            and "choices" not in node
            for node in first["nodes"]
        )
        assert all(
            "decisions" not in node
            for node in first["nodes"]
            if node.get("path_index") != 1
        )

        visited[1]["decisions"][0]["selected_label"] = "响应篡改"
        second_status, second = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )

    second_visited = sorted(
        (node for node in second["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert second_status == 200
    assert second_visited[1]["decisions"] == trusted
    assert all(node["decisions"] == forged for node in canonical_route)
    assert all(
        parsed.route_nodes[1]["decisions"] == trusted
        for parsed in parsed_snapshots
    )


@pytest.mark.parametrize(
    "invalid_authority",
    [
        "malformed",
        "route-mismatch",
        "room-type-mismatch",
        "model-id-mismatch",
        "act-mismatch",
        "duplicate-coordinate",
        "path-conflict",
    ],
)
def test_recorded_decision_rejected_authority_never_leaks_forged_fields(
    tmp_path: Path,
    invalid_authority: str,
) -> None:
    run_id = f"recorded-decision-rejected-{invalid_authority}"
    include_boss = invalid_authority == "model-id-mismatch"
    middle_room_type = (
        "shop" if invalid_authority == "room-type-mismatch" else "monster"
    )
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type=middle_room_type,
        boss_model_id="BOSS.NATIVE" if include_boss else None,
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    snapshot = _branched_recorded_snapshot(
        run_id,
        act=1,
        ts=1,
        visit_boss=include_boss,
    )
    snapshot["visited_nodes"][1]["decisions"] = [
        _recorded_decision("原始可信选择")
    ]
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            snapshot,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )
    canonical = RunCatalog(
        [tmp_path], replay_parser=_replay_parser
    ).get_run(run_id)
    route_nodes = [
        node
        for node in canonical["run"]["nodes"]
        if node.get("_workbench_evidence_kind") == "route_node"
        and any(
            item.get("source_kind") == "native_run"
            for item in node.get("_workbench_provenance", [])
        )
    ]
    raw_snapshot = next(
        node
        for node in canonical["run"]["nodes"]
        if node.get("event") == "map_snapshot"
    )
    forged = [_recorded_decision("伪造选择", selected_id="EVENT.FORGED")]
    route_nodes[1].update(
        decisions=deepcopy(forged),
        options=deepcopy(forged[0]["options"]),
        choices=deepcopy(forged[0]["options"]),
    )
    raw_snapshot.update(
        decisions=deepcopy(forged),
        options=deepcopy(forged[0]["options"]),
        choices=deepcopy(forged[0]["options"]),
    )

    if invalid_authority == "malformed":
        raw_snapshot["map"].pop("type")
    elif invalid_authority == "route-mismatch":
        route_nodes[1]["col"] = 6
    elif invalid_authority == "act-mismatch":
        raw_snapshot["act"] = 2
        raw_snapshot["map"]["context"]["act"] = 2
    elif invalid_authority == "duplicate-coordinate":
        raw_snapshot["map"]["rows"][1].append(
            deepcopy(raw_snapshot["map"]["rows"][1][0])
        )
    elif invalid_authority == "path-conflict":
        raw_snapshot["map"]["rows"][0][0]["children"] = [
            {"col": 1, "row": 1}
        ]

    class StaticCatalog:
        def get_run(self, requested_run_id: str) -> dict:
            assert requested_run_id == run_id
            return deepcopy(canonical)

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="recorded decision rejected")

    service = CapturingFallbackService()
    with _server(StaticCatalog(), map_service=service) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert payload["fallback_reason"] == "recorded decision rejected"
    assert all(
        "decisions" not in node
        and "options" not in node
        and "choices" not in node
        for node in payload["nodes"]
    )


def test_recorded_decision_mixed_native_deltas_map_by_trusted_path_index(
    tmp_path: Path,
) -> None:
    run_id = "recorded-decision-mixed-native"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    native["map_point_history"][1]["player_stats"][0]["cards_gained"] = [
        "CARD.NATIVE.EXACT"
    ]
    for path_index, native_node in enumerate(native["map_point_history"]):
        native_node.update(
            act=1,
            act_index=0,
            global_floor=path_index + 1,
        )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    snapshot = _branched_recorded_snapshot(run_id, act=1, ts=1)
    trusted = [_recorded_decision("路径 1 的可信选择")]
    snapshot["visited_nodes"][1]["decisions"] = deepcopy(trusted)
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            snapshot,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=MustNotGenerate(),
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    visited = sorted(
        (node for node in payload["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert status == 200
    assert visited[1]["decisions"] == trusted
    assert visited[1]["deltas"]["cards_gained"] == {
        "value": ["CARD.NATIVE.EXACT"],
        "quality": "exact",
    }


def test_recorded_decision_replay_and_deck_same_act_use_recorded_route(
    tmp_path: Path,
) -> None:
    run_id = "recorded-decision-replay-deck-same-act"
    replay = [
        {
            "type": "action",
            "ts": 1,
            "data": {
                "cmd": "start_run",
                "run_id": run_id,
                "character": "Ironclad",
                "seed": "shared-seed",
                "build_id": "v0.107.1",
                "checkpoint": "shared-model",
                "evaluation_mode": "fixed",
                "scenario": "standard",
                "ascension": 0,
            },
        },
        {
            "type": "state",
            "ts": 2,
            "data": {
                "run_id": run_id,
                "decision": "map",
                "context": {
                    "act": 1,
                    "floor": 1,
                    "room_type": "Ancient",
                },
            },
        },
    ]
    snapshot = _branched_recorded_snapshot(run_id, act=1, ts=3)
    trusted = [_recorded_decision("同幕可信选择")]
    snapshot["visited_nodes"][1]["decisions"] = deepcopy(trusted)
    snapshot.update(
        character="Ironclad",
        seed="shared-seed",
        checkpoint="shared-model",
        evaluation_mode="fixed",
        scenario="standard",
        ascension=0,
    )
    _write_jsonl(tmp_path / "replay.jsonl", replay)
    _write_jsonl(
        tmp_path / "deck.jsonl",
        [
            snapshot,
            {
                "event": "outcome",
                "run_id": run_id,
                "game_version": "v0.107.1",
                "status": "dead",
            },
        ],
    )

    class MustNotGenerate:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, request: MapRequest):
            self.calls += 1
            raise AssertionError(f"generator must not run for {request.run_id}")

    service = MustNotGenerate()
    with _server(
        RunCatalog([tmp_path], replay_parser=viewer.parse_game_progress),
        map_service=service,
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    visited = sorted(
        (node for node in payload["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert status == 200
    assert service.calls == 0
    assert payload["full_map"] is True
    assert [node["recorded_node_id"] for node in visited] == [
        "a0:n0",
        "a0:n1",
        "a0:n2",
    ]
    assert visited[1]["decisions"] == trusted


def test_recorded_decision_replay_and_deck_different_acts_remain_available(
    tmp_path: Path,
) -> None:
    run_id = "recorded-decision-replay-deck-different-acts"
    _write_jsonl(
        tmp_path / "replay.jsonl",
        [
            {
                "type": "action",
                "ts": 1,
                "data": {
                    "cmd": "start_run",
                    "run_id": run_id,
                    "character": "Ironclad",
                    "seed": "shared-seed",
                    "build_id": "v0.107.1",
                    "ascension": 0,
                },
            },
            {
                "type": "state",
                "ts": 2,
                "data": {
                    "run_id": run_id,
                    "context": {
                        "act": 1,
                        "floor": 1,
                        "room_type": "Ancient",
                    },
                },
            },
        ],
    )
    act_three = _branched_recorded_snapshot(run_id, act=3, ts=3)
    trusted = [_recorded_decision("第三幕可信选择")]
    act_three["visited_nodes"][1]["decisions"] = deepcopy(trusted)
    _write_jsonl(
        tmp_path / "deck.jsonl",
        [
            act_three,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class CapturingService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="replay route fallback")

    service = CapturingService()
    with _server(
        RunCatalog([tmp_path], replay_parser=viewer.parse_game_progress),
        map_service=service,
    ) as base:
        first_status, first = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )
        third_status, third = _request(
            base, f"/api/run/map?id={run_id}&act=2"
        )

    assert first_status == third_status == 200
    assert [act["index"] for act in first["acts"]] == [0, 2]
    assert [request.act_index for request in service.requests] == [0]
    assert first["visited_route"] is True
    third_visited = sorted(
        (node for node in third["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert third["full_map"] is True
    assert third_visited[1]["decisions"] == trusted


def test_recorded_decision_generator_fallback_scrubs_graph_and_source_fields(
    tmp_path: Path,
) -> None:
    run_id = "recorded-decision-forged-fallback"
    forged = [_recorded_decision("伪造选择", selected_id="EVENT.FORGED")]
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    for native_node in native["map_point_history"]:
        native_node.update(
            decisions=deepcopy(forged),
            options=deepcopy(forged[0]["options"]),
            choices=deepcopy(forged[0]["options"]),
        )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")

    class ForgedFallbackMap:
        def __init__(self, request: MapRequest) -> None:
            self.request = request

        def to_dict(self) -> dict:
            payload = visited_route_map(
                self.request,
                reason="forged generator fallback",
            ).to_dict()
            for graph_node in payload["nodes"]:
                graph_node.update(
                    decisions=deepcopy(forged),
                    options=deepcopy(forged[0]["options"]),
                    choices=deepcopy(forged[0]["options"]),
                )
            return payload

    class ForgedFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return ForgedFallbackMap(request)

    service = ForgedFallbackService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert payload["fallback_reason"] == "forged generator fallback"
    assert all(
        "decisions" not in node
        and "options" not in node
        and "choices" not in node
        for node in payload["nodes"]
    )


def test_recorded_decision_http_rebuilds_allowlisted_map_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "recorded-decision-map-schema-allowlist"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    forged = [_recorded_decision("伪造选择", selected_id="EVENT.FORGED")]
    original_to_dict = ActMap.to_dict

    class DictSubclass(dict):
        pass

    def hostile_to_dict(act_map):
        payload = original_to_dict(act_map)
        payload.update(
            boss={"secret": "BOSS_SECRET"},
            rows=[[{"secret": "ROW_SECRET"}]],
            route={"secret": "ROUTE_SECRET"},
            terminal={"secret": "TERMINAL_SECRET"},
            extra="ROOT_SECRET",
        )
        payload["nodes"][0] = DictSubclass(
            payload["nodes"][0],
            decisions=deepcopy(forged),
            options=deepcopy(forged[0]["options"]),
            choices=deepcopy(forged[0]["options"]),
            extra="NODE_SECRET",
        )
        payload["nodes"][1].update(
            name="SAFE_NAME",
            current=False,
            children=[],
            extra="NODE_SECRET",
        )
        payload["alignment"]["extra"] = "ALIGNMENT_SECRET"
        payload["edges"][0]["extra"] = "EDGE_SECRET"
        return payload

    monkeypatch.setattr(ActMap, "to_dict", hostile_to_dict)

    class OneShotService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="schema fallback")

    service = OneShotService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert set(payload) == {
        "act_id",
        "full_map",
        "visited_route",
        "fallback_reason",
        "nodes",
        "edges",
        "alignment",
        "run_id",
        "act",
        "acts",
        "summary",
    }
    assert set(payload["alignment"]) == {
        "ok",
        "ambiguous",
        "reason",
        "path_node_ids",
    }
    assert all(
        set(edge) == {"from", "to"}
        for edge in payload["edges"]
    )
    assert type(payload["nodes"][0]) is dict
    assert set(payload["nodes"][0]) <= {
        "id",
        "col",
        "row",
        "room_type",
        "visited",
        "path_index",
        "name",
        "current",
        "children",
        "deltas",
        "recorded_node_id",
        "decisions",
        "art",
        "terminal",
        "terminal_status",
    }
    assert all(
        secret not in json.dumps(payload, ensure_ascii=False)
        for secret in (
            "BOSS_SECRET",
            "ROW_SECRET",
            "ROUTE_SECRET",
            "TERMINAL_SECRET",
            "ROOT_SECRET",
            "NODE_SECRET",
            "ALIGNMENT_SECRET",
            "EDGE_SECRET",
            "EVENT.FORGED",
        )
    )


def test_recorded_decision_http_does_not_execute_hostile_map_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "recorded-decision-hostile-map-key"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    original_to_dict = ActMap.to_dict

    class HostileKey(str):
        pass

    hostile_key = HostileKey("decisions")

    def hostile_to_dict(act_map):
        payload = original_to_dict(act_map)
        payload["nodes"][0][hostile_key] = "VERY_SECRET_STATE"

        def explode(*_args, **_kwargs):
            raise AssertionError("hostile key hook must not execute")

        HostileKey.__eq__ = explode
        HostileKey.__hash__ = explode
        HostileKey.__str__ = explode
        return payload

    monkeypatch.setattr(ActMap, "to_dict", hostile_to_dict)

    class FallbackService:
        def generate(self, request: MapRequest):
            return visited_route_map(request, reason="hostile-key fallback")

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=FallbackService(),
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert "VERY_SECRET_STATE" not in json.dumps(payload, ensure_ascii=False)
    assert all("decisions" not in node for node in payload["nodes"])


@pytest.mark.parametrize(
    "identity_failure",
    [
        "gap",
        "duplicate",
        "missing",
        "off-by-one",
        "wrong-act",
        "hostile-id",
    ],
)
def test_recorded_decision_rejects_noncanonical_route_identity(
    tmp_path: Path,
    identity_failure: str,
) -> None:
    run_id = f"recorded-decision-identity-{identity_failure}"
    snapshot = _branched_recorded_snapshot(run_id, act=1, ts=1)
    snapshot["visited_nodes"][1]["decisions"] = [
        _recorded_decision("不得泄漏")
    ]
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            snapshot,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )
    canonical = RunCatalog(
        [tmp_path], replay_parser=_replay_parser
    ).get_run(run_id)
    route_nodes = [
        node
        for node in canonical["run"]["nodes"]
        if node.get("_workbench_evidence_kind") == "route_node"
    ]

    if identity_failure == "gap":
        route_nodes[1]["id"] = "a0:n7"
    elif identity_failure == "duplicate":
        route_nodes[1]["id"] = "a0:n0"
    elif identity_failure == "missing":
        route_nodes[1].pop("id")
    elif identity_failure == "off-by-one":
        for path_index, route_node in enumerate(route_nodes, start=1):
            route_node["id"] = f"a0:n{path_index}"
    elif identity_failure == "wrong-act":
        route_nodes[1]["id"] = "a1:n1"
    else:
        class HostileId(str):
            def __eq__(self, other):
                raise AssertionError("hostile id equality must not execute")

            def __hash__(self):
                raise AssertionError("hostile id hash must not execute")

            def __str__(self):
                raise AssertionError("hostile id string conversion must not execute")

        route_nodes[1]["id"] = HostileId("a0:n1")

    class StaticCatalog:
        def get_run(self, requested_run_id: str) -> dict:
            assert requested_run_id == run_id
            return canonical

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="route identity rejected")

    service = CapturingFallbackService()
    with _server(StaticCatalog(), map_service=service) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert payload["fallback_reason"] == "route identity rejected"
    assert all("decisions" not in node for node in payload["nodes"])


def test_recorded_decision_multi_act_selects_only_the_requested_act(
    tmp_path: Path,
) -> None:
    run_id = "recorded-decision-multi-act"
    act_one = _branched_recorded_snapshot(run_id, act=1, ts=1)
    act_three = _branched_recorded_snapshot(run_id, act=3, ts=2)
    first_decision = [_recorded_decision("第一幕选择", selected_id="EVENT.ACT1")]
    third_decision = [_recorded_decision("第三幕选择", selected_id="EVENT.ACT3")]
    act_one["visited_nodes"][1]["decisions"] = deepcopy(first_decision)
    act_three["visited_nodes"][2]["decisions"] = deepcopy(third_decision)
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            act_one,
            act_three,
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=MustNotGenerate(),
    ) as base:
        first_status, first = _request(
            base, f"/api/run/map?id={run_id}&act=0"
        )
        third_status, third = _request(
            base, f"/api/run/map?id={run_id}&act=2"
        )

    first_visited = sorted(
        (node for node in first["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    third_visited = sorted(
        (node for node in third["nodes"] if node["visited"]),
        key=lambda node: node["path_index"],
    )
    assert first_status == third_status == 200
    assert first_visited[1]["decisions"] == first_decision
    assert all("decisions" not in node for node in first_visited[2:])
    assert third_visited[2]["decisions"] == third_decision
    assert all("decisions" not in node for node in third_visited[:2])


def test_entirely_malformed_recorded_maps_keep_existing_route_fallback(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["run_id"] = "malformed-recorded-map-fallback"
    run["build_id"] = "v0.107.1"
    (tmp_path / "native.run").write_text(json.dumps(run), encoding="utf-8")
    malformed = _branched_recorded_snapshot(run["run_id"], act=1, ts=1)
    malformed["map"].pop("type")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            malformed,
            {"event": "outcome", "run_id": run["run_id"], "status": "dead"},
        ],
    )

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="captured fallback")

    service = CapturingFallbackService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert len(service.requests) == 1
    assert service.requests[0].game_version == "v0.107.1"
    assert payload["full_map"] is False
    assert payload["visited_route"] is True
    assert payload["fallback_reason"] == "captured fallback"
    assert "warnings" not in payload
    assert "errors" not in payload


@pytest.mark.parametrize("invalid_authority", ["coordinate", "path-length", "provenance"])
def test_recorded_map_authority_mismatch_or_untrusted_provenance_falls_back(
    tmp_path: Path,
    invalid_authority: str,
) -> None:
    run_id = f"recorded-authority-{invalid_authority}"
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=1, ts=1),
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )
    canonical = RunCatalog([tmp_path], replay_parser=_replay_parser).get_run(run_id)
    route_nodes = [
        node
        for node in canonical["run"]["nodes"]
        if node.get("_workbench_evidence_kind") == "route_node"
    ]
    raw_map = next(
        node
        for node in canonical["run"]["nodes"]
        if node.get("event") == "map_snapshot"
    )
    if invalid_authority == "coordinate":
        route_nodes[1]["col"] = 6
    elif invalid_authority == "path-length":
        canonical["run"]["nodes"].remove(route_nodes[-1])
    else:
        raw_map["_workbench_provenance"] = [
            {"source_id": "spoofed", "source_kind": "replay_jsonl"}
        ]

    class StaticCatalog:
        def get_run(self, requested_run_id: str) -> dict:
            assert requested_run_id == run_id
            return deepcopy(canonical)

    class CapturingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="authority rejected")

    service = CapturingFallbackService()
    with _server(StaticCatalog(), map_service=service) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert payload["full_map"] is False
    assert payload["fallback_reason"] == "authority rejected"


def test_joined_native_room_type_mismatch_rejects_recorded_map_authority(
    tmp_path: Path,
) -> None:
    run_id = "recorded-authority-room-type-mismatch"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="shop",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=1, ts=1),
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class CountingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="room authority rejected")

    service = CountingFallbackService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert service.requests[0].visited[1]["map_point_type"] == "shop"
    assert payload["full_map"] is False
    assert payload["fallback_reason"] == "room authority rejected"
    assert payload["nodes"][1]["room_type"] == "Shop"


def test_joined_native_room_type_aliases_keep_recorded_map_authority(
    tmp_path: Path,
) -> None:
    run_id = "recorded-authority-normalized-room-type"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="  mOnStEr  ",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(run_id, act=1, ts=1),
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class MustNotGenerate:
        def generate(self, request: MapRequest):
            raise AssertionError(f"generator must not run for {request.run_id}")

    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=MustNotGenerate(),
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert payload["full_map"] is True
    assert payload["fallback_reason"] is None


def test_joined_native_model_id_conflict_rejects_recorded_map_authority(
    tmp_path: Path,
) -> None:
    run_id = "recorded-authority-model-id-mismatch"
    native = _native_route_matching_recorded_coordinates(
        run_id,
        middle_room_type="monster",
        boss_model_id="BOSS.NATIVE",
    )
    (tmp_path / "native.run").write_text(json.dumps(native), encoding="utf-8")
    _write_jsonl(
        tmp_path / "deck-history.jsonl",
        [
            _branched_recorded_snapshot(
                run_id,
                act=1,
                ts=1,
                visit_boss=True,
            ),
            {"event": "outcome", "run_id": run_id, "status": "dead"},
        ],
    )

    class CountingFallbackService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="model authority rejected")

    service = CountingFallbackService()
    with _server(
        RunCatalog([tmp_path], replay_parser=_replay_parser),
        map_service=service,
    ) as base:
        status, payload = _request(base, f"/api/run/map?id={run_id}&act=0")

    assert status == 200
    assert len(service.requests) == 1
    assert service.requests[0].visited[-1]["rooms"] == [
        {"model_id": "BOSS.NATIVE"}
    ]
    assert payload["full_map"] is False
    assert payload["fallback_reason"] == "model authority rejected"


def test_act_descriptors_prefer_explicit_bounded_indices_and_ignore_identityless_acts(
) -> None:
    descriptors = viewer._act_descriptors(
        {
            "acts": [
                {"id": "NATIVE.ACT.1"},
                {"act_index": 1},
                {},
                {"act": 2, "id": "RECORDED.ACT.2"},
                {"act_index": 1, "id": "LATER.ACT.2"},
                {},
            ],
            "nodes": [
                {
                    "id": "a3:n0",
                    "_workbench_evidence_kind": "route_node",
                }
            ],
        }
    )

    assert descriptors == [
        {
            "index": 0,
            "act_id": "NATIVE.ACT.1",
            "label": "第 1 幕",
            "available": False,
            "visited_count": 0,
        },
        {
            "index": 1,
            "act_id": "RECORDED.ACT.2",
            "label": "第 2 幕",
            "available": False,
            "visited_count": 0,
        },
        {
            "index": 3,
            "act_id": None,
            "label": "第 4 幕",
            "available": True,
            "visited_count": 1,
        },
    ]


def test_mixed_native_and_duplicate_sparse_recorded_acts_keep_http_tabs_aligned(
    tmp_path: Path,
) -> None:
    run = _map_fixture()
    run["run_id"] = "mixed-sparse-recorded-acts"
    (tmp_path / "native.run").write_text(json.dumps(run), encoding="utf-8")
    recorded_rows = [
        _recorded_map_route_snapshot(
            run["run_id"],
            act=2,
            ts=1,
            route_length=1,
        ),
        {
            "event": "outcome",
            "run_id": run["run_id"],
            "status": "dead",
        },
    ]
    _write_jsonl(tmp_path / "recorded-a.jsonl", recorded_rows)
    _write_jsonl(tmp_path / "recorded-b.jsonl", recorded_rows)

    class CapturingMapService:
        def __init__(self) -> None:
            self.requests: list[MapRequest] = []

        def generate(self, request: MapRequest):
            self.requests.append(request)
            return visited_route_map(request, reason="captured request")

    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    service = CapturingMapService()
    with _server(catalog, map_service=service) as base:
        status, payload = _request(
            base, f"/api/run/map?id={run['run_id']}&act=0"
        )

    assert status == 200
    assert [
        (act["index"], act["act_id"], act["available"])
        for act in payload["acts"]
    ] == [
        (0, "ACT.OVERGROWTH", True),
        (1, "RECORDED.ACT.2", True),
    ]
    assert len(service.requests) == 1
    assert service.requests[0].act_id == "ACT.OVERGROWTH"
    assert service.requests[0].act_index == 0


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


def test_explicit_source_roots_keep_workbench_discovery_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_calls: list[dict] = []

    class FakeCatalog:
        def __init__(self, roots, replay_parser=None, *, include_policy="all"):
            catalog_calls.append(
                {
                    "roots": list(roots),
                    "replay_parser": replay_parser,
                    "include_policy": include_policy,
                }
            )

    class FakeHTTPServer:
        server_address = ("127.0.0.1", 61123)

        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            return None

    monkeypatch.setattr(viewer, "RunCatalog", FakeCatalog)
    monkeypatch.setattr(viewer, "ThreadingHTTPServer", FakeHTTPServer)

    viewer.serve("127.0.0.1", 0, source_roots=[tmp_path])

    assert catalog_calls == [
        {
            "roots": [tmp_path],
            "replay_parser": viewer.parse_game_progress,
            "include_policy": "workbench",
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
