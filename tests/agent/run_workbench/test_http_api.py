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
from agent.run_workbench.catalog import RunCatalog

from .test_catalog import _native, _replay, _replay_parser, _write_jsonl


@contextmanager
def _server(catalog: RunCatalog):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_viewer_handler(catalog))
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


def _catalog(tmp_path: Path) -> RunCatalog:
    (tmp_path / "native.run").write_text(json.dumps(_native()), encoding="utf-8")
    _write_jsonl(tmp_path / "summary.jsonl", [{"event": "summary", "avg_floor": 8}])
    return RunCatalog([tmp_path], replay_parser=_replay_parser)


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
