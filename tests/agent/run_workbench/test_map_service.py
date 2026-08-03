from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from agent.run_workbench.map_service import (
    MapExecutableNotFoundError,
    MapOutputError,
    MapRequest,
    MapService,
    MapServiceTimeoutError,
    MapSubprocessError,
    SUPPORTED_MAP_BUILDS,
)


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "run_workbench"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _golden_request(**overrides: object) -> MapRequest:
    run = _load("map_v01032_partial.run")
    values = {
        "run_id": run["run_id"],
        "act_id": run["acts"][0]["id"],
        "act_index": 0,
        "seed": run["seed"],
        "game_version": run["build_id"],
        "ascension": run["ascension"],
        "modifiers": tuple(run["modifiers"]),
        "is_multiplayer": run["is_multiplayer"],
        "visited": tuple(run["map_point_history"]),
        "allow_partial_path": False,
    }
    values.update(overrides)
    return MapRequest(**values)


def _completed(payload: dict, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["node", "map_cli.js"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="vendor failed" if returncode else "",
    )


def test_supported_builds_are_explicitly_version_gated() -> None:
    assert SUPPORTED_MAP_BUILDS == frozenset({"v0.103.2"})


def test_known_build_matches_the_committed_generator_golden() -> None:
    expected = _load("map_v01032_expected.json")

    actual = MapService().generate(_golden_request()).to_dict()

    assert actual["full_map"] is True
    assert actual["fallback_reason"] is None
    assert actual["nodes"] == expected["nodes"]
    assert actual["edges"] == expected["edges"]
    assert actual["alignment"]["path_node_ids"] == expected["alignment"][
        "path_node_ids"
    ]


def test_second_identical_request_uses_the_in_memory_cache(monkeypatch) -> None:
    expected = _load("map_v01032_expected.json")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return _completed(expected)

    monkeypatch.setattr("agent.run_workbench.map_service.subprocess.run", fake_run)
    service = MapService(node_executable="/nvm/bin/node")
    request = _golden_request()

    first = service.generate(request)
    second = service.generate(request)

    assert first is second
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "/nvm/bin/node"
    assert argv[1].endswith("vendor/akirakato_mapgen/map_cli.js")
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 5
    assert kwargs["check"] is False
    assert json.loads(kwargs["input"]) == _load("map_v01032_request.json")


@pytest.mark.parametrize(
    ("overrides", "reason_fragment"),
    [
        ({"seed": None}, "seed"),
        ({"act_id": ""}, "act"),
        ({"visited": ()}, "visited"),
        ({"game_version": None}, "version"),
        ({"game_version": "v0.104.0"}, "unsupported"),
        ({"is_multiplayer": True}, "multiplayer"),
    ],
)
def test_unreconstructable_requests_return_a_human_readable_visited_route(
    overrides: dict[str, object], reason_fragment: str
) -> None:
    result = MapService().generate(_golden_request(**overrides))

    assert result.full_map is False
    assert result.visited_route is bool(result.nodes)
    assert reason_fragment in (result.fallback_reason or "").lower()
    assert [node.path_index for node in result.nodes] == list(range(len(result.nodes)))
    assert all(node.visited for node in result.nodes)
    assert [(edge.from_id, edge.to_id) for edge in result.edges] == [
        (result.nodes[index].id, result.nodes[index + 1].id)
        for index in range(len(result.nodes) - 1)
    ]


@pytest.mark.parametrize(
    ("alignment", "reason_fragment"),
    [
        (
            {
                "ok": False,
                "ambiguous": False,
                "reason": "no generated path matches",
                "path_node_ids": [],
            },
            "no generated path matches",
        ),
        (
            {
                "ok": True,
                "ambiguous": True,
                "reason": None,
                "path_node_ids": ["3:0"],
            },
            "ambiguous",
        ),
    ],
)
def test_failed_or_ambiguous_alignment_falls_back(
    monkeypatch, alignment: dict, reason_fragment: str
) -> None:
    payload = _load("map_v01032_expected.json")
    payload["alignment"] = alignment
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(_golden_request())

    assert result.full_map is False
    assert reason_fragment in (result.fallback_reason or "").lower()


def test_alignment_must_map_every_visited_entry_exactly_once(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    payload["alignment"]["path_node_ids"] = payload["alignment"][
        "path_node_ids"
    ][:-1]
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(_golden_request())

    assert result.full_map is False
    assert "every visited" in (result.fallback_reason or "").lower()


def test_alignment_path_must_follow_directed_graph_edges(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    first, second = payload["alignment"]["path_node_ids"][:2]
    payload["edges"].remove({"from": first, "to": second})
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(_golden_request())

    assert result.full_map is False
    assert "directed edge" in (result.fallback_reason or "").lower()


def test_alignment_path_room_types_must_match_visited_history(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    second_path_id = payload["alignment"]["path_node_ids"][1]
    next(node for node in payload["nodes"] if node["id"] == second_path_id)[
        "room_type"
    ] = "Boss"
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(_golden_request())

    assert result.full_map is False
    assert "room type" in (result.fallback_reason or "").lower()


def test_neow_event_alias_normalizes_to_ancient(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    request = _golden_request()
    request.visited[0]["map_point_type"] = "event"
    request.visited[0]["rooms"] = [{"model_id": "EVENT.NEOW"}]
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(request)

    assert result.full_map is True


def test_unknown_room_alias_falls_back_conservatively(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    request = _golden_request()
    request.visited[1]["map_point_type"] = "combat-ish"
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    result = MapService().generate(request)

    assert result.full_map is False
    assert "room type" in (result.fallback_reason or "").lower()


def test_duplicate_edges_are_rejected_as_invalid_generator_output(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    payload["edges"].append(dict(payload["edges"][0]))
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    with pytest.raises(MapOutputError, match="duplicate edge"):
        MapService().generate(_golden_request())


def test_self_edges_are_rejected_as_invalid_generator_output(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    payload["edges"].append({"from": "3:0", "to": "3:0"})
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    with pytest.raises(MapOutputError, match="self edge"):
        MapService().generate(_golden_request())


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": 7},
        {"act_id": 7},
        {"act_index": True},
        {"act_index": -1},
        {"act_index": 4},
        {"seed": 7},
        {"seed": ""},
        {"game_version": 7},
        {"game_version": ""},
        {"ascension": True},
        {"ascension": -1},
        {"ascension": 11},
        {"modifiers": []},
        {"modifiers": ("ok", 3)},
        {"is_multiplayer": "false"},
        {"visited": []},
        {"visited": ([],)},
        {"visited": tuple({} for _ in range(257))},
        {"allow_partial_path": "false"},
    ],
)
def test_invalid_runtime_request_shapes_fall_back_without_spawning_node(
    monkeypatch, overrides: dict[str, Any]
) -> None:
    calls = 0

    def unexpected_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("invalid request must not start Node")

    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run", unexpected_run
    )

    result = MapService().generate(_golden_request(**overrides))

    assert calls == 0
    assert result.full_map is False
    assert result.fallback_reason


@pytest.mark.parametrize(
    "overrides",
    [
        {"act_index": 0, "ascension": 0},
        {"act_index": 3, "ascension": 10},
        {"visited": tuple({} for _ in range(256))},
    ],
)
def test_request_upper_and_lower_boundaries_are_allowed_to_reach_node(
    monkeypatch, overrides: dict[str, Any]
) -> None:
    calls = 0
    payload = _load("map_v01032_expected.json")

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _completed(payload)

    monkeypatch.setattr("agent.run_workbench.map_service.subprocess.run", fake_run)

    MapService().generate(_golden_request(**overrides))

    assert calls == 1


def test_generator_payload_over_one_mib_falls_back_without_spawning_node(
    monkeypatch,
) -> None:
    request = _golden_request(
        visited=(
            {
                "map_point_type": "ancient",
                "oversized": "x" * (1024 * 1024),
            },
        )
    )
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: pytest.fail("oversized input must not start Node"),
    )

    result = MapService().generate(request)

    assert result.full_map is False
    assert "1 mib" in (result.fallback_reason or "").lower()


def test_cached_snapshot_is_not_poisoned_by_later_caller_mutation(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _completed(payload)

    monkeypatch.setattr("agent.run_workbench.map_service.subprocess.run", fake_run)
    service = MapService()
    caller_request = _golden_request()

    first = service.generate(caller_request)
    caller_request.visited[0]["map_point_type"] = "boss"
    same_original_value = service.generate(_golden_request())

    assert calls == 1
    assert same_original_value is first
    assert first.to_dict()["nodes"] == payload["nodes"]


def test_one_snapshot_drives_both_cache_key_and_generator_payload(monkeypatch) -> None:
    import agent.run_workbench.map_service as map_service

    payload = _load("map_v01032_expected.json")
    caller_request = _golden_request()
    actual_inputs: list[dict] = []
    real_serialize = map_service._serialize_request

    def serialize_then_mutate(snapshot: MapRequest) -> str:
        key = real_serialize(snapshot)
        caller_request.visited[0]["map_point_type"] = "boss"
        return key

    def fake_run(*args, **kwargs):
        actual_inputs.append(json.loads(kwargs["input"]))
        return _completed(payload)

    monkeypatch.setattr(map_service, "_serialize_request", serialize_then_mutate)
    monkeypatch.setattr(map_service.subprocess, "run", fake_run)

    result = MapService().generate(caller_request)

    assert result.full_map is True
    assert actual_inputs[0]["visited"][0]["map_point_type"] == "ancient"


def test_schema_version_bool_is_not_accepted_as_version_one(monkeypatch) -> None:
    payload = _load("map_v01032_expected.json")
    payload["schema_version"] = True
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    with pytest.raises(MapOutputError, match="schema version"):
        MapService().generate(_golden_request())


def test_oversized_stdout_is_rejected_before_json_parsing(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["node", "map_cli.js"],
            returncode=0,
            stdout=" " * ((1024 * 1024) + 1),
            stderr="",
        ),
    )

    with pytest.raises(MapOutputError, match="1 MiB"):
        MapService().generate(_golden_request())


@pytest.mark.parametrize(
    ("field", "count", "message"),
    [("nodes", 257, "node count"), ("edges", 2049, "edge count")],
)
def test_generator_graph_array_counts_are_bounded(
    monkeypatch, field: str, count: int, message: str
) -> None:
    payload = _load("map_v01032_expected.json")
    if field == "nodes":
        payload[field] = [
            {
                "id": f"n:{index}",
                "col": 0,
                "row": index,
                "room_type": "Monster",
                "visited": False,
                "path_index": None,
            }
            for index in range(count)
        ]
    else:
        payload[field] = [{"from": "3:0", "to": "1:1"}] * count
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    with pytest.raises(MapOutputError, match=message):
        MapService().generate(_golden_request())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["nodes"][0].update(id="x" * 65), "node id"),
        (
            lambda payload: payload["nodes"][0].update(room_type="x" * 65),
            "room_type",
        ),
        (
            lambda payload: payload["alignment"].update(reason="x" * 2049),
            "alignment reason",
        ),
        (
            lambda payload: payload["alignment"].update(
                path_node_ids=["x" * 65]
            ),
            "path node id",
        ),
    ],
)
def test_generator_critical_string_lengths_are_bounded(
    monkeypatch, mutate, message: str
) -> None:
    payload = _load("map_v01032_expected.json")
    mutate(payload)
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(payload),
    )

    with pytest.raises(MapOutputError, match=message):
        MapService().generate(_golden_request())


def test_node_timeout_is_a_typed_operational_error(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

    monkeypatch.setattr("agent.run_workbench.map_service.subprocess.run", timeout)

    with pytest.raises(MapServiceTimeoutError, match="timed out"):
        MapService().generate(_golden_request())


def test_missing_node_executable_is_a_typed_operational_error(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("node missing")

    monkeypatch.setattr("agent.run_workbench.map_service.subprocess.run", missing)

    with pytest.raises(MapExecutableNotFoundError, match="not found"):
        MapService().generate(_golden_request())


def test_node_nonzero_exit_is_a_typed_operational_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: _completed(
            {"schema_version": 1, "ok": False, "error": "generator rejected input"},
            returncode=2,
        ),
    )

    with pytest.raises(MapSubprocessError, match="generator rejected input"):
        MapService().generate(_golden_request())


def test_invalid_node_stdout_is_a_typed_operational_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.run_workbench.map_service.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["node", "map_cli.js"], returncode=0, stdout="not-json", stderr=""
        ),
    )

    with pytest.raises(MapOutputError, match="valid JSON"):
        MapService().generate(_golden_request())
