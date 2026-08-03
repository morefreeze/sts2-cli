from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess


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


def _invoke(payload: object) -> subprocess.CompletedProcess[str]:
    node = shutil.which("node")
    assert node is not None, "the active NVM-managed Node.js executable is required"
    return subprocess.run(
        [node, str(CLI)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


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
