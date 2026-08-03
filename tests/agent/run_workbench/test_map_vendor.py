from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


VENDOR_DIR = (
    Path(__file__).resolve().parents[3]
    / "agent"
    / "run_workbench"
    / "vendor"
    / "akirakato_mapgen"
)
CLI = VENDOR_DIR / "map_cli.js"
REQUEST = {
    "act_id": "ACT.OVERGROWTH",
    "act_index": 0,
    "seed": "map-contract-seed",
    "ascension": 0,
    "modifiers": [],
    "is_multiplayer": False,
    "visited": None,
    "allow_partial_path": False,
}


def _invoke_text(text: str) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None, "the active NVM-managed Node.js executable is required"
    return subprocess.run(
        [node, str(CLI)],
        input=text,
        capture_output=True,
        text=True,
        check=False,
    )


def _invoke(payload: object) -> subprocess.CompletedProcess[str]:
    return _invoke_text(json.dumps(payload))


def _without(field: str) -> dict[str, object]:
    return {key: value for key, value in REQUEST.items() if key != field}


def test_vendor_records_license_and_exact_upstream_provenance():
    license_text = (VENDOR_DIR / "LICENSE").read_text(encoding="utf-8")
    upstream_text = (VENDOR_DIR / "UPSTREAM.md").read_text(encoding="utf-8")

    assert "Copyright (c) 2026 Akirakato1" in license_text
    assert "https://github.com/Akirakato1/Slay-the-Spire-2-dashboard" in upstream_text
    assert "cc9a7ce13bbfe3fcef0d04899de705b1f69d0300" in upstream_text
    assert "scripts/mapgen/" in upstream_text
    assert "STS2 build `v0.103.2`" in upstream_text


def test_map_cli_emits_a_deterministic_self_contained_graph():
    first = _invoke(REQUEST)
    second = _invoke(REQUEST)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["schema_version"] == 1
    assert first_payload["ok"] is True
    assert json.dumps(first_payload, sort_keys=True) == json.dumps(
        second_payload, sort_keys=True
    )

    nodes = first_payload["nodes"]
    node_ids = {node["id"] for node in nodes}
    assert any(node["room_type"] == "Ancient" for node in nodes)
    assert any(node["room_type"] == "Boss" for node in nodes)
    assert all(
        edge["from"] in node_ids and edge["to"] in node_ids
        for edge in first_payload["edges"]
    )
    assert first_payload["alignment"] == {
        "ok": False,
        "ambiguous": False,
        "reason": "no visited history",
        "path_node_ids": [],
    }


def test_map_cli_reports_invalid_input_as_json_without_stdout_diagnostics():
    result = _invoke([])

    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": False,
        "error": "request must be a JSON object",
    }


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (_without("act_id"), "act_id must be a non-empty string"),
        ({**REQUEST, "act_id": ""}, "act_id must be a non-empty string"),
        ({**REQUEST, "act_id": "   "}, "act_id must be a non-empty string"),
        (_without("act_index"), "act_index must be a nonnegative integer"),
        ({**REQUEST, "act_index": -1}, "act_index must be a nonnegative integer"),
        ({**REQUEST, "act_index": 0.5}, "act_index must be a nonnegative integer"),
        (_without("seed"), "seed must be a string"),
        ({**REQUEST, "seed": 123}, "seed must be a string"),
        (_without("ascension"), "ascension must be a nonnegative integer"),
        ({**REQUEST, "ascension": -1}, "ascension must be a nonnegative integer"),
        ({**REQUEST, "ascension": 0.5}, "ascension must be a nonnegative integer"),
        (_without("modifiers"), "modifiers must be an array of strings"),
        ({**REQUEST, "modifiers": "none"}, "modifiers must be an array of strings"),
        ({**REQUEST, "modifiers": ["ok", 3]}, "modifiers must be an array of strings"),
        (_without("is_multiplayer"), "is_multiplayer must be a boolean"),
        ({**REQUEST, "is_multiplayer": "false"}, "is_multiplayer must be a boolean"),
        (_without("visited"), "visited must be null or an array of objects"),
        ({**REQUEST, "visited": {}}, "visited must be null or an array of objects"),
        ({**REQUEST, "visited": [None]}, "visited must be null or an array of objects"),
        (_without("allow_partial_path"), "allow_partial_path must be a boolean"),
        (
            {**REQUEST, "allow_partial_path": "false"},
            "allow_partial_path must be a boolean",
        ),
    ],
)
def test_map_cli_rejects_missing_or_wrongly_typed_fields(
    payload: dict[str, object], error: str
):
    result = _invoke(payload)

    assert result.returncode != 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": False,
        "error": error,
    }


def test_map_cli_rejects_visited_history_over_the_safe_act_limit():
    result = _invoke({**REQUEST, "visited": [{} for _ in range(257)]})

    assert result.returncode != 0
    assert json.loads(result.stdout)["error"] == "visited exceeds 256 nodes"


def test_map_cli_rejects_oversized_stdin_before_attempting_json_parse():
    oversized_invalid_json = "{" + (" " * 1_048_576)
    assert len(oversized_invalid_json.encode("utf-8")) > 1_048_576

    result = _invoke_text(oversized_invalid_json)

    assert result.returncode != 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": False,
        "error": "input exceeds 1048576 bytes",
    }
