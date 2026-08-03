from __future__ import annotations

from contextlib import contextmanager
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import agent.run_progress_viewer as viewer
from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench.catalog import RunCatalog


STATIC_DIR = Path(viewer.__file__).with_name("run_workbench") / "static"


class _ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.landmarks: list[str] = []
        self.links: list[dict[str, str | None]] = []
        self.scripts: list[dict[str, str | None]] = []
        self.labels: list[dict[str, str | None]] = []
        self.live_regions: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"] or "")
        if tag in {"header", "main", "nav", "section", "aside", "footer"}:
            self.landmarks.append(tag)
        if tag == "link":
            self.links.append(values)
        if tag == "script":
            self.scripts.append(values)
        if tag == "label":
            self.labels.append(values)
        if values.get("aria-live"):
            self.live_regions.append(values)


@contextmanager
def _server(tmp_path: Path):
    catalog = RunCatalog([tmp_path], replay_parser=lambda rows, name=None: {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_viewer_handler(catalog))
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(base: str, path: str) -> tuple[int, str, bytes]:
    try:
        with urlopen(base + path, timeout=3) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def _javascript_section(script: str, start: str, end: str) -> str:
    start_index = script.index(start)
    end_index = script.index(end, start_index)
    return script[start_index:end_index]


def test_shell_uses_external_assets_and_stable_landmark_order():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser = _ShellParser()
    parser.feed(html)

    required_ids = [
        "currentCohort",
        "baselineCohort",
        "characterFilter",
        "versionFilter",
        "validityFilter",
        "sourceFile",
        "avgFloor",
        "medianFloor",
        "maxFloor",
        "act2Rate",
        "validCount",
        "technicalCount",
        "trendChart",
        "funnelChart",
        "comparisonBanner",
        "anomalyList",
        "representativeRuns",
        "sourceCatalog",
        "workbenchStatus",
    ]
    assert all(item in parser.ids for item in required_ids)
    layout_order = [
        "workbenchStatus",
        "sourceFile",
        "currentCohort",
        "baselineCohort",
        "characterFilter",
        "versionFilter",
        "validityFilter",
        "avgFloor",
        "medianFloor",
        "maxFloor",
        "act2Rate",
        "validCount",
        "technicalCount",
        "trendChart",
        "funnelChart",
        "comparisonBanner",
        "anomalyList",
        "representativeRuns",
        "sourceCatalog",
    ]
    assert [parser.ids.index(item) for item in layout_order] == sorted(
        parser.ids.index(item) for item in layout_order
    )
    assert {"header", "main", "section", "aside"}.issubset(parser.landmarks)
    assert any(link.get("href") == "/static/styles.css" for link in parser.links)
    assert any(script.get("src") == "/static/app.js" for script in parser.scripts)
    assert "<style" not in html.lower()
    assert not any(script.get("src") is None for script in parser.scripts)
    assert any(label.get("for") == "sourceFile" for label in parser.labels)
    assert any(region.get("id") == "workbenchStatus" for region in parser.live_regions)
    assert "训练进度" in html
    assert "正在读取训练记录…" in html
    assert not hasattr(viewer, "HTML")


def test_existing_direct_script_entrypoint_still_imports_package():
    script = Path(viewer.__file__)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=script.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--source-root" in result.stdout


def test_static_assets_are_allowlisted_with_utf8_content_types(tmp_path: Path):
    with _server(tmp_path) as base:
        root_status, root_type, root_body = _get(base, "/")
        css_status, css_type, css_body = _get(base, "/static/styles.css")
        js_status, js_type, js_body = _get(base, "/static/app.js")

    assert root_status == css_status == js_status == 200
    assert root_type == "text/html; charset=utf-8"
    assert css_type == "text/css; charset=utf-8"
    assert js_type == "text/javascript; charset=utf-8"
    assert root_body == (STATIC_DIR / "index.html").read_bytes()
    assert css_body == (STATIC_DIR / "styles.css").read_bytes()
    assert js_body == (STATIC_DIR / "app.js").read_bytes()


def test_static_routes_reject_unknown_traversal_encoding_and_queries(tmp_path: Path):
    unsafe = [
        "/static/unknown.js",
        "/static/../run_progress_viewer.py",
        "/static/%2e%2e/run_progress_viewer.py",
        "/%73tatic/app.js",
        "/static/app.js?path=../secret",
        "/static/styles.css?cache=1",
    ]
    with _server(tmp_path) as base:
        responses = [_get(base, path) for path in unsafe]

    assert [status for status, _, _ in responses] == [404] * len(unsafe)


def test_app_bootstraps_apis_and_renders_server_owned_comparison():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "Promise.all" in script
    assert "getJSON('/api/cohorts')" in script
    assert "getJSON('/api/catalog')" in script
    assert "await refreshMetrics()" in script
    for endpoint in ("/api/metrics", "/api/source", "/api/run", "/api/parse"):
        assert endpoint in script
    assert "comparison.mismatch_reasons" in script
    assert "comparison.notes" in script
    assert "payload.view" in script
    assert "formatMissing" in script
    assert "return '—'" in script or 'return "—"' in script
    assert "act2_entry_denominator" in script
    assert "technical_n" in script
    assert "createElementNS" in script


def test_baseline_default_is_distinct_without_client_comparability_logic():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    selector = _javascript_section(
        script, "function nearestDistinctCohortId", "function updateCohortOptions"
    )

    assert "comparisonCompatible" not in script
    assert ".filters" not in selector
    assert "currentIndex - 1" in selector
    assert "currentIndex + 1" in selector
    assert "cohort_id" in selector
    assert "nearestDistinctCohortId(candidates, current)" in script


def test_current_default_uses_server_latest_order_instead_of_label_order():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    selector = _javascript_section(
        script, "function updateCohortOptions", "function resetMetrics"
    )

    assert "candidates[0].cohort_id" in selector
    assert "candidates[candidates.length - 1].cohort_id" not in selector


def test_funnel_is_an_accessible_inline_svg_with_explicit_denominators():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    funnel = _javascript_section(script, "function renderFunnel", "function appendList")

    assert "svgElement('svg'" in funnel
    assert "role: 'img'" in funnel
    assert "svgElement('title'" in funnel
    assert "svgElement('desc'" in funnel
    assert "svgElement('rect'" in funnel
    assert "svgElement('text'" in funnel
    assert "point.count" in funnel
    assert "point.denominator" in funnel
    assert "formatRate(point.rate)" in funnel
    assert "Number.isFinite" in funnel


def test_representative_recency_requires_a_finite_timestamp():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    representatives = _javascript_section(
        script, "function representativeCandidates", "function renderRepresentatives"
    )

    assert "最近一局" not in representatives
    assert "Number.isFinite(point.timestamp)" in representatives
    assert "b.timestamp - a.timestamp" in representatives
    assert "最近有时间记录" in representatives
    assert "趋势样本" in representatives


def test_app_does_not_reparse_sources_or_inject_untrusted_html():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    lowered = script.lower()

    assert ".innerhtml" not in lowered
    assert "eval(" not in lowered
    assert "new function" not in lowered
    assert "chart.js" not in lowered
    assert "d3." not in lowered
    assert "plotly" not in lowered
    assert "splitlines" not in lowered
    assert "split('\\n')" not in script
    assert 'split("\\n")' not in script
    assert "source_kind" in script
    assert "JSON.parse" in script  # HTTP responses only; source text is posted untouched.
    assert "file.text()" in script
