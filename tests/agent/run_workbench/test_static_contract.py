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
        "runMapPage",
        "actTabs",
        "mapFallback",
        "mapSvg",
        "mapLegend",
        "actSummary",
        "selectedNodeSummary",
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
    script_sources = [script.get("src") for script in parser.scripts]
    assert "/static/map.js" in script_sources
    assert script_sources.index("/static/app.js") < script_sources.index("/static/map.js")
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
        map_status, map_type, map_body = _get(base, "/static/map.js")

    assert root_status == css_status == js_status == map_status == 200
    assert root_type == "text/html; charset=utf-8"
    assert css_type == "text/css; charset=utf-8"
    assert js_type == "text/javascript; charset=utf-8"
    assert map_type == "text/javascript; charset=utf-8"
    assert root_body == (STATIC_DIR / "index.html").read_bytes()
    assert css_body == (STATIC_DIR / "styles.css").read_bytes()
    assert js_body == (STATIC_DIR / "app.js").read_bytes()
    assert map_body == (STATIC_DIR / "map.js").read_bytes()


def test_map_renderer_uses_safe_svg_layering_accessibility_and_history():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")

    assert "createElementNS" in script
    assert "innerHTML" not in script
    assert "renderNeutralEdges" in script
    assert "renderVisitedEdges" in script
    assert script.index("renderNeutralEdges") < script.index("renderVisitedEdges")
    assert "createSvg('image'" in script
    assert "tabindex" in script
    assert "aria-label" in script
    assert "event.key === 'Escape'" in script
    assert "history.pushState" in script
    assert "popstate" in script
    assert "/api/run/map" in script
    assert "path_index" in script
    assert "quality" in script
    assert "terminal_status" in script


def test_act_switch_replaces_the_open_map_history_entry_and_tooltips_keep_unknowns():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    tabs = _javascript_section(script, "function renderActTabs", "function showMapPage")
    load = _javascript_section(script, "async function loadAct", "function closeMapPage")
    tooltip = _javascript_section(script, "function nodeTooltip", "function renderNodes")

    assert "historyMode: 'replace'" in tabs
    assert "historyMode === 'push'" in load
    assert "history.pushState" in load
    assert "historyMode === 'replace'" in load
    assert "history.replaceState" in load
    assert script.count("history.pushState") == 1
    assert "measurementDisplay(measurement, field)" in tooltip
    assert "QUALITY_LABELS[measurement.quality]" in tooltip
    assert "nonzeroMeasurement" not in tooltip
    assert "'—'" in script


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
    assert "boundedTimestampedTrend(rawTrend).points" in representatives
    assert "for (const point of trend)" in representatives
    assert "stablePointKey(point)" in representatives
    assert "point.timestamp > latestTimed.timestamp" in representatives
    assert "趋势样本中最近" in representatives
    assert "趋势样本中最远" in representatives
    assert "趋势样本中最浅" in representatives
    assert "趋势样本" in representatives
    assert "[..." not in representatives
    assert ".sort(" not in representatives


def test_trend_rendering_is_bounded_timestamped_and_explains_sampling():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    bounding = _javascript_section(
        script, "function boundedTimestampedTrend", "function renderTrendProvenance"
    )
    trend = _javascript_section(script, "function renderTrend", "function renderFunnel")

    assert "CLIENT_TREND_POINT_LIMIT" in script
    assert "Number.isFinite(point.timestamp)" in bounding
    assert "for (const point of points)" in bounding
    assert "points: selected" in bounding
    assert "Math.max(1, ..." not in trend
    assert "for (const point of available)" in trend
    assert "trend_eligible_n" in script
    assert "trend_timestamped_n" in script
    assert "trend_unknown_time_n" in script
    assert "trend_sampled_n" in script
    assert "trend_sample_limit" in script
    assert "trend_sampling_method" in script
    assert "时间未知未绘制" in script
    assert "较早" in trend and "较新" in trend


def test_current_default_trusts_server_latest_order_without_inventing_time():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cohorts = _javascript_section(script, "function updateCohortOptions", "function resetMetrics")

    assert "candidates[0].cohort_id" in cohorts
    assert "candidates[candidates.length - 1]" not in cohorts
    assert "latest_at" in cohorts
    assert "时间未知" in cohorts
    assert "默认选择列表中最新的可用批次" not in html


def test_representatives_never_request_an_empty_run_id():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    candidates = _javascript_section(
        script, "function representativeCandidates", "function renderRepresentatives"
    )
    rows = _javascript_section(script, "function renderRepresentatives", "function renderCatalog")
    open_run = _javascript_section(script, "async function openRun", "async function refreshMetrics")

    assert "source_id" in candidates and "run_id" in candidates
    assert "seen.add(key)" in candidates
    assert "new WeakMap" in candidates
    assert "anonymousPointKeys" in candidates
    assert "anonymousPointCounter" in candidates
    assert "if (runId)" in rows
    assert "不可定位" in rows
    assert "openSource(sourceId, event.currentTarget)" in rows
    assert "openRun(runId, event.currentTarget)" in rows
    assert "if (!runId)" in open_run
    assert "不代表全量对局极值" in rows
    assert "trend_sampled_n" in rows
    assert "trend_timestamped_n" in rows


def test_representative_candidates_dedupe_only_the_same_anonymous_point():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    bounding = _javascript_section(
        script, "function boundedTimestampedTrend", "function renderTrendProvenance"
    )
    representatives = _javascript_section(
        script, "function stablePointKey", "function renderRepresentatives"
    )
    probe = f"""
const CLIENT_TREND_POINT_LIMIT = 256;
{bounding}
{representatives}
const shared = {{ timestamp: 3, global_floor: 10, label: 'shared' }};
const low = {{ timestamp: 1, global_floor: 1, label: 'low' }};
const missing = {{ timestamp: 2, global_floor: null, label: 'missing' }};
const repeated = representativeCandidates({{ current: {{ trend: [shared, shared, shared] }} }}, null);
const distinct = representativeCandidates({{ current: {{ trend: [shared, low, missing] }} }}, null);
if (repeated.length !== 1 || repeated[0].point !== shared) process.exit(11);
if (distinct.length !== 3) process.exit(12);
if (new Set(distinct.map((candidate) => candidate.point)).size !== 3) process.exit(13);
"""

    result = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_detail_requests_are_latest_only_and_drawer_restores_focus():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    requests = _javascript_section(script, "function beginDetailRequest", "async function refreshMetrics")

    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-hidden="true"' in html
    assert 'tabindex="-1"' in html
    assert " hidden" in html and " inert" in html
    assert ".detail-panel[hidden]" in css
    assert "new AbortController" in requests
    assert "detailRequestToken += 1" in requests
    assert ".abort()" in requests
    assert "isCurrentDetailRequest(token)" in requests
    assert "signal" in requests
    assert "panel.hidden = false" in requests
    assert "panel.inert = false" in requests
    assert "panel.hidden = true" in requests
    assert "panel.inert = true" in requests
    assert "state.detailOpener" in requests
    assert ".focus()" in requests
    assert "function focusableDetailElements" in script
    assert "button:not([disabled])" in script
    assert "closest('[hidden], [inert]" in script
    assert "node.matches(':disabled')" in script
    assert "event.shiftKey" in script
    assert "event.preventDefault()" in script
    assert "focusables[0]" in script
    assert "focusables[focusables.length - 1]" in script
    assert "handleDetailKeydown" in script


def test_upload_size_guard_precedes_read_and_has_server_margin():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    viewer_source = Path(viewer.__file__).read_text(encoding="utf-8")
    upload = _javascript_section(script, "async function uploadSelectedFile", "async function bootstrap")

    assert viewer.PARSE_BODY_MAX_BYTES == 10 * 1024 * 1024
    assert "length > PARSE_BODY_MAX_BYTES" in viewer_source
    assert "const SERVER_PARSE_BODY_MAX_BYTES = 10 * 1024 * 1024" in script
    assert "const FILE_UPLOAD_MAX_BYTES = 1 * 1024 * 1024" in script
    assert 1 * 1024 * 1024 * 6 + 64 * 1024 < viewer.PARSE_BODY_MAX_BYTES
    guard_index = upload.index("file.size > FILE_UPLOAD_MAX_BYTES")
    read_index = upload.index("await file.text()")
    stringify_index = upload.index("JSON.stringify")
    fetch_index = upload.index("getJSON('/api/parse'")
    assert guard_index < read_index < fetch_index < stringify_index
    assert "超过本地载入上限" in upload
    assert "未读取文件内容" in upload


def test_catalog_anomalies_are_grouped_and_bounded():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    anomalies = _javascript_section(script, "function renderAnomalies", "function currentCohortDescriptor")

    assert "SOURCE_ERROR_EXAMPLE_LIMIT" in script
    assert "error_count" in anomalies
    assert "errors_omitted" in anomalies
    assert "unknownSources" in anomalies
    assert "trainingSources" in anomalies
    assert "来源目录问题" in anomalies
    assert "(source.errors || []).forEach" not in anomalies


def test_upload_focus_and_mobile_chart_overflow_are_visible():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    label_start = html.index('<label id="sourceFileLabel"')
    label_end = html.index("</label>", label_start)

    assert 'id="sourceFile"' in html[label_start:label_end]
    assert ".upload-button:focus-within" in css
    assert ".chart-stage," in css and ".funnel-list" in css
    assert "overflow-x: auto" in css
    assert ".chart-svg" in css and "min-width: 640px" in css
    assert ".funnel-svg" in css and "min-width: 520px" in css


def test_large_source_summary_has_an_honest_detail_view():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    detail = _javascript_section(script, "function renderDetail", "function beginDetailRequest")

    assert "payload.view === 'runs_summary'" in detail
    assert "payload.run_count" in detail
    assert "payload.runs_complete" in detail
    assert "payload.representative_run_ids" in detail
    assert "大型来源摘要" in detail
    assert "仅返回代表性对局 ID" in detail


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
