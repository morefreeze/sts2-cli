from __future__ import annotations

import json
from pathlib import Path
import subprocess

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
