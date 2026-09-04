from __future__ import annotations

from contextlib import contextmanager
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import re
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


def _run_node_json(source: str):
    result = subprocess.run(
        ["node", "-e", source],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _recorded_decisions_fixture() -> list[dict]:
    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "run_workbench"
        / "recorded_decisions.jsonl"
    )
    rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
    snapshot = next(row for row in rows if row.get("event") == "map_snapshot")
    return [
        decision
        for node in snapshot["visited_nodes"]
        for decision in node.get("decisions", [])
    ]


def test_shell_uses_external_assets_and_stable_landmark_order():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser = _ShellParser()
    parser.feed(html)

    required_ids = [
        "cohortTree",
        "contentPane",
        "batchView",
        "catalogView",
        "baselineCohort",
        "sourceFile",
        "catalogToggle",
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
        "runsTable",
        "sourceCatalog",
        "workbenchStatus",
        "runMapPage",
        "runMeta",
        "mapBackButton",
        "actTabs",
        "mapFallback",
        "mapSvg",
        "mapLegend",
        "mapDecisionPopover",
        "mapDecisionTitle",
        "mapDecisionBody",
        "actSummary",
        "selectedNodeSummary",
        "detailPanel",
    ]
    assert all(item in parser.ids for item in required_ids)
    # The tree precedes the content pane (browse-then-inspect), and the
    # batch view's metrics precede its run table (summary before detail).
    layout_order = [
        "workbenchStatus",
        "sourceFile",
        "cohortTree",
        "contentPane",
        "batchView",
        "baselineCohort",
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
        "runsTable",
        "catalogView",
        "sourceCatalog",
    ]
    assert [parser.ids.index(item) for item in layout_order] == sorted(
        parser.ids.index(item) for item in layout_order
    )
    assert {"header", "main", "nav", "section", "aside"}.issubset(parser.landmarks)
    assert any(link.get("href") == "/static/styles.css" for link in parser.links)
    script_sources = [script.get("src") for script in parser.scripts]
    for src in (
        "/static/util.js",
        "/static/app.js",
        "/static/tree.js",
        "/static/cohort-view.js",
        "/static/runs-table.js",
        "/static/run-view.js",
        "/static/map.js",
    ):
        assert src in script_sources
    # util.js is the shared-helper base: it must load before every module
    # that calls byId/element/getJSON/... at top-level-adjacent code.
    assert script_sources.index("/static/util.js") == 0
    # map.js is deliberately last: app.js and the view modules reference
    # window.STS2Map only from inside callbacks that run after all deferred
    # scripts have executed, so load order does not need to match that, but
    # app.js before map.js is still required by loadAct's history contract.
    assert script_sources.index("/static/app.js") < script_sources.index("/static/map.js")
    assert script_sources[-1] == "/static/map.js"
    assert "<style" not in html.lower()
    assert not any(script.get("src") is None for script in parser.scripts)
    assert any(label.get("for") == "sourceFile" for label in parser.labels)
    assert any(region.get("id") == "workbenchStatus" for region in parser.live_regions)
    assert '<nav id="actTabs" class="act-tabs" role="tablist"' in html
    assert 'id="cohortTree"' in html
    assert "训练进度" in html
    assert "正在读取训练记录…" in html
    assert not hasattr(viewer, "HTML")


def test_tree_nav_precedes_batch_content_and_batch_view_precedes_catalog():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert html.index('id="cohortTree"') < html.index('id="contentPane"')
    assert html.index('id="batchView"') < html.index('id="catalogView"')
    assert html.index('id="baselineCohort"') < html.index('id="avgFloor"')
    assert html.index('id="anomalyList"') < html.index('id="runsTable"')


def test_workbench_body_and_tree_use_semantic_grid_spans_at_responsive_breakpoints():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    desktop = css[: css.index("@media")]
    tablet_start = css.index("@media (max-width: 760px)")
    tablet = css[tablet_start : css.index("@media", tablet_start + 1)]
    mobile_start = css.index("@media (max-width: 480px)")
    mobile = css[mobile_start:]

    assert re.search(
        r"\.workbench-body\s*\{[^}]*grid-template-columns:\s*300px minmax\(0, 1fr\);",
        desktop,
        re.DOTALL,
    )
    # Narrow widths collapse the two-column shell to one column and turn
    # the tree into a bounded, scrollable drawer instead of a sidebar.
    assert re.search(
        r"\.workbench-body\s*\{[^}]*grid-template-columns:\s*1fr;",
        tablet,
        re.DOTALL,
    )
    assert re.search(
        r"\.cohort-tree\s*\{[^}]*max-height:\s*260px;",
        tablet,
        re.DOTALL,
    )
    assert re.search(
        r"\.metric-card\s*\{[^}]*grid-column:\s*1\s*/\s*-1;",
        mobile,
        re.DOTALL,
    )


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
    assert "showMapPage({ focusPage: !focusActTab })" in load
    assert "const selectedTab = renderActTabs(payload)" in load
    assert "if (focusActTab && selectedTab) selectedTab.focus()" in load
    assert ".click()" not in tabs
    assert script.count("history.pushState") == 1
    assert "measurementDisplay(measurement, field)" in tooltip
    assert "QUALITY_LABELS[measurement.quality]" in tooltip
    assert "nonzeroMeasurement" not in tooltip
    assert "'—'" in script


def test_native_delta_item_shapes_render_known_bounded_labels():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("const DELTA_")
    )
    labels = _javascript_section(
        script, "function boundedDeltaLabel", "function measurementDisplay"
    )

    result = _run_node_json(
        f"""
        {constants}
        {labels}
        const examples = [
          {{ choice: 'Bash' }},
          {{ choice: {{ name: {{ en: 'Pommel Strike' }} }} }},
          {{ choice: {{ id: {{ en: 'Offering' }} }} }},
          {{ from: {{ id: {{ en: 'Strike' }} }}, to: {{ name: 'Bash' }} }},
          {{ before: {{ name: {{ en: 'Defend' }} }}, after: {{ id: 'Shrug It Off' }} }},
        ];
        let tooDeep = 'Known but too deep';
        for (let index = 0; index < 12; index += 1) tooDeep = {{ choice: tooDeep }};
        const longLabel = deltaItemLabel({{ choice: 'x'.repeat(200) }});
        const longTransformation = deltaItemLabel({{
          from: {{ id: 'a'.repeat(200) }},
          to: {{ id: 'b'.repeat(200) }},
        }});
        console.log(JSON.stringify({{
          labels: examples.map((item) => deltaItemLabel(item)),
          deep: deltaItemLabel(tooDeep),
          longLabel,
          longTransformation,
        }}));
        """
    )

    assert result["labels"] == [
        "Bash",
        "Pommel Strike",
        "Offering",
        "Strike → Bash",
        "Defend → Shrug It Off",
    ]
    assert result["deep"] == "未知项目"
    assert len(result["longLabel"]) <= 48
    assert result["longLabel"].endswith("…")
    assert len(result["longTransformation"]) <= 48
    assert " → " in result["longTransformation"]
    assert result["longTransformation"].startswith("a")
    assert result["longTransformation"].endswith("…")
    assert "[object Object]" not in json.dumps(result)


def test_act_keyboard_switches_directly_and_async_load_focuses_only_latest_tab():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    handler = _javascript_section(
        script, "function handleActTabKeydown", "function showMapPage"
    )
    load = _javascript_section(script, "async function loadAct", "function closeMapPage")

    result = _run_node_json(
        f"""
        const handlerCalls = [];
        const tabEffects = {{ focus: 0, click: 0 }};
        const mapState = {{ runId: 'run-1', actIndex: 0 }};
        const document = {{ activeElement: null }};
        function tab(actIndex) {{
          return {{
            dataset: {{ actIndex: String(actIndex) }},
            focus() {{ tabEffects.focus += 1; }},
            click() {{ tabEffects.click += 1; }},
          }};
        }}
        const tabs = [tab(0), tab(1), tab(3)];
        const tabList = {{ querySelectorAll() {{ return tabs; }} }};
        function keyboardLoadAct(runId, actIndex, options) {{
          handlerCalls.push({{ runId, actIndex, options }});
          mapState.actIndex = actIndex;
        }}
        function key(key) {{
          let prevented = false;
          handleActTabKeydown({{
            key,
            currentTarget: tabList,
            preventDefault() {{ prevented = true; }},
          }});
          return prevented;
        }}
        {handler.replace('loadAct(', 'keyboardLoadAct(')}
        const prevented = [key('ArrowRight'), key('ArrowRight'), key('Home'), key('End')];

        const pending = [];
        const showCalls = [];
        const renderedActs = [];
        const focusedActs = [];
        const elements = new Map();
        const asyncMapState = {{
          runId: '', actIndex: 0, opener: null, requestToken: 0,
          abortController: null,
        }};
        function byId(id) {{
          if (!elements.has(id)) elements.set(id, {{ hidden: false, textContent: '' }});
          return elements.get(id);
        }}
        function showMapPage(options) {{ showCalls.push(options); }}
        function renderEmpty() {{}}
        function clear() {{}}
        function hideDecisionPopover() {{}}
        const history = {{
          state: {{ fromDashboard: true }},
          pushState() {{}},
          replaceState() {{}},
        }};
        function mapLocation(runId, actIndex) {{ return `#run=${{runId}}&act=${{actIndex}}`; }}
        function setStatus() {{}}
        function getJSON(url) {{
          return new Promise((resolve) => pending.push({{ url, resolve }}));
        }}
        function renderActTabs(payload) {{
          renderedActs.push(payload.act.index);
          return {{ focus() {{ focusedActs.push(payload.act.index); }} }};
        }}
        function renderMap() {{}}
        function renderActSummary() {{}}
        function selectNode() {{}}
        {load.replace('mapState', 'asyncMapState')}
        function payload(actIndex) {{
          return {{
            act: {{ index: actIndex }}, nodes: [], full_map: true,
            fallback_reason: null,
          }};
        }}
        async function exerciseLoads() {{
          const first = loadAct('run-1', 1, {{ historyMode: 'replace', focusActTab: true }});
          const second = loadAct('run-1', 3, {{ historyMode: 'replace', focusActTab: true }});
          pending[1].resolve(payload(3));
          await second;
          pending[0].resolve(payload(1));
          await first;
          return {{
            handlerCalls, tabEffects, prevented, showCalls,
            renderedActs, focusedActs, finalAct: asyncMapState.actIndex,
          }};
        }}
        exerciseLoads().then((value) => console.log(JSON.stringify(value)));
        """
    )

    assert [call["actIndex"] for call in result["handlerCalls"]] == [1, 3, 0, 3]
    assert all(call["options"]["focusActTab"] for call in result["handlerCalls"])
    assert result["tabEffects"] == {"focus": 0, "click": 0}
    assert result["prevented"] == [True, True, True, True]
    assert result["showCalls"] == [{"focusPage": False}, {"focusPage": False}]
    assert result["renderedActs"] == [3]
    assert result["focusedActs"] == [3]
    assert result["finalAct"] == 3


def _js_number(script: str, name: str) -> int:
    match = re.search(rf"const {name} = (\d+);", script)
    assert match, f"missing numeric JavaScript constant {name}"
    return int(match.group(1))


def test_map_fallback_route_compact_geometry_tabs_focus_and_image_contracts():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    route = _javascript_section(script, "function routeEdgeKeys", "function appendEdge")
    measurement = _javascript_section(
        script, "function measurementDisplay", "function nonzeroMeasurement"
    )
    art = _javascript_section(script, "function renderNodeArt", "function nodeTooltip")
    tabs = _javascript_section(script, "function renderActTabs", "function showMapPage")
    page = _javascript_section(script, "function showMapPage", "function showDashboardPage")
    dashboard = _javascript_section(script, "function showDashboardPage", "function mapLocation")

    assert "path_node_ids.length" in route
    assert ".sort((a, b) => a.path_index - b.path_index)" in route
    assert "boundedListLabels" in measurement
    assert "item.name" in script
    assert "localizedDeltaLabel(item.id)" in script
    assert "[object Object]" not in script
    assert "textContent" in script
    assert "addEventListener('error'" in art
    assert "art.emoji" in art
    assert "removeAttribute('display')" in art
    assert "handleActTabKeydown" in tabs
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in script
    assert "detailPanel.contains(mapState.opener)" in page
    assert "document.activeElement" in page
    assert "isUsableFocusTarget" in dashboard

    assert _js_number(script, "MAP_ROW_GAP") == 88
    assert _js_number(script, "MAP_PADDING_BOTTOM") == 76
    assert _js_number(script, "MAP_DECISION_RAIL_GAP") == 24
    assert _js_number(script, "MAP_DECISION_RAIL_WIDTH") >= 340
    assert _js_number(script, "MAP_DECISION_LABEL_LIMIT") == 72
    assert "renderBadges(group, node)" not in script
    assert "renderDecisionSummary" in script
    assert "mapDecisionPopover" in index


def test_map_compact_geometry_keeps_graph_points_stable_when_rail_is_added():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("const MAP_")
        and re.search(r"= \d+;", line)
    )
    transform = _javascript_section(
        script, "function createMapTransform", "function routeEdgeKeys"
    )

    result = _run_node_json(
        f"""
        {constants}
        {transform}
        const nodes = Array.from({{ length: 17 }}, (_, row) => ({{ col: row % 3, row }}));
        const plain = createMapTransform(nodes, false);
        const railed = createMapTransform(nodes, true);
        console.log(JSON.stringify({{
          plain: {{ width: plain.width, height: plain.height, points: nodes.map(plain.point) }},
          railed: {{
            width: railed.width, graphWidth: railed.graphWidth,
            decisionX: railed.decisionX, points: nodes.map(railed.point),
          }},
        }}));
        """
    )

    assert result["plain"]["height"] < 1700
    assert abs(result["plain"]["points"][0]["y"] - result["plain"]["points"][1]["y"]) == 88
    assert result["railed"]["width"] > result["plain"]["width"]
    assert result["railed"]["graphWidth"] == result["plain"]["width"]
    assert result["railed"]["decisionX"] > result["railed"]["graphWidth"]
    assert result["railed"]["points"] == result["plain"]["points"]


def test_map_initial_position_reveals_current_or_last_route_without_side_effects():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("const MAP_")
        and re.search(r"= \d+;", line)
    )
    helpers = _javascript_section(
        script, "function createMapTransform", "function routeEdgeKeys"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        const activeElement = {{ id: 'unchanged-focus' }};
        const document = {{ activeElement }};
        const popover = {{ hidden: false, scrollTop: 73 }};
        const scroll = {{
          clientHeight: 360, clientWidth: 300,
          scrollHeight: 0, scrollWidth: 0,
          scrollTop: 0, scrollLeft: 0,
        }};
        const svg = {{ parentElement: scroll }};
        function byId(id) {{
          if (id === 'mapSvg') return svg;
          if (id === 'mapDecisionPopover') return popover;
          throw new Error(id);
        }}
        function visible(point) {{
          return point.y - 28 >= scroll.scrollTop
            && point.y + 28 <= scroll.scrollTop + scroll.clientHeight;
        }}
        const nodes = Array.from({{ length: 17 }}, (_, row) => ({{
          id: `node-${{row}}`, col: row % 3, row,
          visited: row <= 4, path_index: row <= 4 ? row : null,
          current: row === 3,
        }}));
        const transform = createMapTransform(nodes, true);
        scroll.scrollHeight = transform.height;
        scroll.scrollWidth = transform.width;
        const currentTarget = mapAutoPositionTarget(nodes);
        positionMapViewport(nodes, transform);
        const current = {{
          target: currentTarget.id,
          visible: visible(transform.point(currentTarget)),
          scrollTop: scroll.scrollTop,
        }};

        nodes.forEach((node) => {{ node.current = false; }});
        scroll.scrollTop = 0;
        const lastTarget = mapAutoPositionTarget(nodes);
        positionMapViewport(nodes, transform);
        const last = {{
          target: lastTarget.id,
          visible: visible(transform.point(lastTarget)),
          scrollTop: scroll.scrollTop,
        }};

        const topNodes = nodes.map((node) => ({{
          ...node,
          visited: node.row === 16,
          path_index: node.row === 16 ? 0 : null,
          current: node.row === 16,
        }}));
        scroll.scrollTop = 0;
        const topTransform = createMapTransform(topNodes, false);
        scroll.scrollHeight = topTransform.height;
        scroll.scrollWidth = topTransform.width;
        positionMapViewport(topNodes, topTransform);
        const top = scroll.scrollTop;

        const emptyNodes = nodes.map((node) => ({{
          ...node, visited: false, path_index: null, current: false,
        }}));
        scroll.scrollTop = 0;
        const emptyTransform = createMapTransform(emptyNodes, false);
        scroll.scrollHeight = emptyTransform.height;
        positionMapViewport(emptyNodes, emptyTransform);

        console.log(JSON.stringify({{
          current, last, top, empty: scroll.scrollTop,
          focusUnchanged: document.activeElement === activeElement,
          popover: {{ hidden: popover.hidden, scrollTop: popover.scrollTop }},
        }}));
        """
    )

    assert result["current"]["target"] == "node-3"
    assert result["current"]["visible"] is True
    assert result["current"]["scrollTop"] > 0
    assert result["last"]["target"] == "node-4"
    assert result["last"]["visible"] is True
    assert result["last"]["scrollTop"] > 0
    assert result["top"] == 0
    assert result["empty"] == 0
    assert result["focusUnchanged"] is True
    assert result["popover"] == {"hidden": False, "scrollTop": 73}


def test_map_load_auto_positions_new_run_act_once_and_preserves_same_act_scroll():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    load = _javascript_section(script, "async function loadAct", "function closeMapPage")

    result = _run_node_json(
        f"""
        const pending = [];
        const renderCalls = [];
        const mapState = {{
          runId: '', actIndex: 0, opener: null, requestToken: 0,
          abortController: null, positionedMapKey: '',
        }};
        const mapScroll = {{ scrollTop: 0, scrollLeft: 0 }};
        const elements = new Map();
        elements.set('mapSvg', {{
          hidden: false, textContent: '', parentElement: mapScroll,
        }});
        function byId(id) {{
          if (!elements.has(id)) elements.set(id, {{ hidden: false, textContent: '' }});
          return elements.get(id);
        }}
        function showMapPage() {{}}
        function renderEmpty() {{}}
        function clear(node) {{
          if (node === elements.get('mapSvg')) {{
            mapScroll.scrollTop = 0;
            mapScroll.scrollLeft = 0;
          }}
        }}
        function hideDecisionPopover() {{}}
        const history = {{ state: null, pushState() {{}}, replaceState() {{}} }};
        function mapLocation(runId, actIndex) {{ return `${{runId}}:${{actIndex}}`; }}
        function setStatus() {{}}
        function getJSON(url) {{
          return new Promise((resolve) => pending.push({{ url, resolve }}));
        }}
        function renderActTabs() {{ return null; }}
        function renderMap(payload, options) {{
          renderCalls.push({{
            act: payload.act.index,
            autoPosition: options && options.autoPosition,
            preservePosition: options && options.preservePosition,
          }});
          if (options && options.preservePosition) {{
            mapScroll.scrollTop = options.preservePosition.top;
            mapScroll.scrollLeft = options.preservePosition.left;
          }}
        }}
        function renderActSummary() {{}}
        function selectNode() {{}}
        {load}
        function payload(actIndex) {{
          return {{
            act: {{ index: actIndex }}, nodes: [], full_map: true,
            fallback_reason: null,
          }};
        }}
        async function load(runId, actIndex) {{
          const promise = loadAct(runId, actIndex);
          pending.at(-1).resolve(payload(actIndex));
          await promise;
        }}
        async function exercise() {{
          await load('run-a', 0);
          mapScroll.scrollTop = 321;
          mapScroll.scrollLeft = 24;
          await load('run-a', 0);
          const restoredSameAct = {{ top: mapScroll.scrollTop, left: mapScroll.scrollLeft }};
          await load('run-a', 1);
          await load('run-b', 1);
          return {{ renderCalls, restoredSameAct, positionedMapKey: mapState.positionedMapKey }};
        }}
        exercise().then((value) => console.log(JSON.stringify(value)));
        """
    )

    assert result["renderCalls"] == [
        {"act": 0, "autoPosition": True, "preservePosition": None},
        {
            "act": 0,
            "autoPosition": False,
            "preservePosition": {"top": 321, "left": 24},
        },
        {"act": 1, "autoPosition": True, "preservePosition": None},
        {"act": 1, "autoPosition": True, "preservePosition": None},
    ]
    assert result["restoredSameAct"] == {"top": 321, "left": 24}
    assert result["positionedMapKey"] == "run-b\u00001"


def test_map_decision_summary_prefers_recorded_newest_and_fails_closed_for_unknowns():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        const earlier = {{
          kind: 'event', selected_id: 'leave', selected_label: '离开', evidence: 'recorded',
          options: [{{ id: 'leave', label: '离开', effect: '不发生变化', selected: true }}],
        }};
        const longLabel = `${{'x'.repeat(70)}}😀${{'z'.repeat(100)}}`;
        const latest = {{
          kind: 'card_reward', selected_id: 'pommel', selected_label: longLabel, evidence: 'recorded',
          options: [{{
            id: 'pommel', label: longLabel,
            effect: '造成 9 点伤害，抽 1 张牌' + 'e'.repeat(300), selected: true,
          }}],
        }};
        const recorded = nodeDecisionSummary({{ visited: true, decisions: [earlier, latest] }});
        const derived = nodeDecisionSummary({{
          visited: true,
          deltas: {{
            cards_gained: {{ quality: 'derived', value: ['Bash'] }},
            potions_gained: {{ quality: 'exact', value: [{{ name: 'Fire Potion' }}] }},
          }},
        }});
        const unknown = nodeDecisionSummary({{
          visited: true,
          deltas: {{ cards_gained: {{ quality: 'unknown', value: [{{ name: 'SECRET' }}] }} }},
        }});
        const forgedKind = nodeDecisionSummary({{
          visited: true,
          decisions: [{{
            kind: '__proto__', evidence: 'recorded',
            options: [{{ label: 'FORGED', effect: 'FORGED', selected: true }}],
          }}],
        }});
        console.log(JSON.stringify({{
          recorded,
          recordedLabelScalars: Array.from(recorded.label).length,
          recordedEffectScalars: Array.from(recorded.effect).length,
          recordedLabelValid: !Array.from(recorded.label).some((ch) => ch.length === 1 && /[\\uD800-\\uDFFF]/.test(ch)),
          derived,
          unknown,
          forgedKind,
        }}));
        """
    )

    assert result["recorded"]["prefix"] == "卡"
    assert result["recorded"]["overflow"] == 1
    assert result["recorded"]["recorded"] is True
    assert result["recordedLabelScalars"] <= 72
    assert result["recordedEffectScalars"] <= 72
    assert result["recordedLabelValid"] is True
    assert "😀" in result["recorded"]["label"]
    assert result["derived"]["prefix"] == "推导"
    assert result["derived"]["label"].startswith("获得 Bash")
    assert result["derived"]["recorded"] is False
    assert result["unknown"] is None
    assert result["forgedKind"] is None
    assert "[object Object]" not in json.dumps(result)


def test_recorded_decisions_fixture_renders_all_six_one_line_grey_summaries():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    decisions = _recorded_decisions_fixture()
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )
    renderer = _javascript_section(
        script, "function renderDecisionSummary", "function usableDecisionAnchor"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        const decisions = {json.dumps(decisions, ensure_ascii=True)};
        console.log(JSON.stringify(decisions.map((decision) =>
          nodeDecisionSummary({{ visited: true, decisions: [decision] }}))));
        """
    )

    assert [summary["prefix"] for summary in result] == [
        "事件", "卡", "药水", "商店", "遗物", "休息", "休息",
    ]
    assert [decision["selected_id"] for decision in decisions] == [
        "0",
        "CARD.POMMEL_STRIKE",
        "0",
        "0",
        "0",
        "SMITH",
        "CARD.BASH",
    ]
    assert [summary["label"] for summary in result] == [
        decision["selected_label"] for decision in decisions
    ]
    assert [summary["effect"] for summary in result] == [
        next(
            option["effect"] or ""
            for option in decision["options"]
            if option["selected"]
        )
        for decision in decisions
    ]
    assert all(summary["recorded"] is True and summary["overflow"] == 0 for summary in result)
    assert renderer.count("createSvg('text'") == 1
    assert "createSvg('tspan', { class: 'map-decision-effect' })" in renderer
    assert re.search(
        r"\.map-decision-summary\s+\.map-decision-effect\s*"
        r"\{[^}]*fill:\s*var\(--slate-500\);",
        css,
        re.DOTALL,
    )
    for selector in ("map-decision-selected-effect", "map-decision-option-effect"):
        assert re.search(
            rf"\.{selector}\s*\{{[^}}]*color:\s*var\(--slate-500\);",
            css,
            re.DOTALL,
        )


def test_map_decision_validator_rejects_malformed_or_unbounded_recorded_evidence():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        function option(id, selected = false) {{
          return {{ id, label: `label-${{id}}`, effect: `effect-${{id}}`, selected }};
        }}
        function decision(index = 0, optionCount = 2) {{
          const options = Array.from({{ length: optionCount }}, (_, item) => option(`d${{index}}-o${{item}}`, item === 0));
          return {{
            kind: index % 2 ? 'event' : 'card_reward',
            selected_id: options[0].id,
            selected_label: options[0].label,
            options,
            evidence: 'recorded',
          }};
        }}
        function summary(decisions) {{
          return nodeDecisionSummary({{ visited: true, decisions }});
        }}
        const valid = Array.from({{ length: 16 }}, (_, index) => decision(index));
        const multiSelected = decision();
        multiSelected.options[1].selected = true;
        const idMismatch = decision();
        idMismatch.selected_id = 'other';
        const labelMismatch = decision();
        labelMismatch.selected_label = 'other';
        const blank = decision();
        blank.selected_label = '   ';
        blank.options[0].label = '   ';
        const loneSurrogate = decision();
        loneSurrogate.options[0].effect = '\\ud800';
        const oversizedId = decision();
        oversizedId.selected_id = 'i'.repeat(257);
        oversizedId.options[0].id = oversizedId.selected_id;
        const oversizedEffect = decision();
        oversizedEffect.options[0].effect = 'e'.repeat(513);
        const extraField = decision();
        extraField.options[0].extra = 'forged';
        const ownConstructor = decision();
        Object.defineProperty(ownConstructor, 'constructor', {{ value: 'forged', enumerable: true }});
        const thirtyThree = decision(0, 33);
        let getterCalls = 0;
        const getterDecision = decision();
        Object.defineProperty(getterDecision, 'selected_id', {{
          enumerable: true,
          get() {{ getterCalls += 1; return getterDecision.options[0].id; }},
        }});
        let nodeGetterCalls = 0;
        const getterNode = {{ visited: true }};
        Object.defineProperty(getterNode, 'decisions', {{
          enumerable: true,
          get() {{ nodeGetterCalls += 1; return [decision()]; }},
        }});
        console.log(JSON.stringify({{
          valid: summary(valid),
          invalid: [
            summary([multiSelected]), summary([idMismatch]), summary([labelMismatch]),
            summary([blank]), summary([loneSurrogate]), summary([extraField]),
            summary([oversizedId]), summary([oversizedEffect]),
            summary([ownConstructor]), summary([thirtyThree]),
            summary(Array.from({{ length: 17 }}, (_, index) => decision(index))),
            summary([getterDecision]), nodeDecisionSummary(getterNode),
          ],
          getterCalls,
          nodeGetterCalls,
        }}));
        """
    )

    assert result["valid"]["overflow"] == 15
    assert result["valid"]["label"] == "label-d15-o0"
    assert result["invalid"] == [None] * 13
    assert result["getterCalls"] == 0
    assert result["nodeGetterCalls"] == 0


def test_map_derived_decision_accepts_real_safe_shapes_for_all_six_fields():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        function derived(key, value, quality = 'derived') {{
          return nodeDecisionSummary({{
            visited: true,
            deltas: {{ [key]: {{ quality, value }} }},
          }});
        }}
        const cases = [
          derived('cards_gained', [{{ id: 'CARD.BASH', upgraded: false }}]),
          derived('potions_gained', [{{ id: 'POTION.FIRE' }}], 'exact'),
          derived('relics_gained', [{{ id: 'RELIC.ANCHOR' }}]),
          derived('cards_upgraded', [{{ id: 'CARD.STRIKE', upgraded: true }}]),
          derived('cards_removed', [{{ id: 'CARD.DEFEND' }}], 'exact'),
          derived('cards_transformed', [{{ from: 'CARD.STRIKE', to: 'CARD.BASH' }}], 'exact'),
          derived('relics_gained', [{{ choice: 'RELIC.MARBLE', was_picked: true }}], 'exact'),
          derived('potions_gained', [{{ choice: 'POTION.BLOCK', was_picked: true }}], 'exact'),
        ];
        const direct = derived('cards_gained', ['CARD.DIRECT']);
        const emoji = derived('cards_gained', [`${{'x'.repeat(40)}}😀${{'z'.repeat(80)}}`]);
        function getterItem() {{
          const item = {{}};
          Object.defineProperty(item, 'id', {{ enumerable: true, get() {{ throw new Error('LEAK'); }} }});
          return item;
        }}
        const invalidItems = [
          0, -1, false, {{}}, null, '   ', '\\ud800',
          {{ id: 7 }}, {{ id: '\\ud800' }}, {{ id: 'x'.repeat(513) }}, getterItem(),
          Object.create({{ id: 'PROTO.SECRET' }}),
        ];
        const invalid = invalidItems.map((value) => derived('cards_gained', [value]));
        const invalidTransformed = [
          {{ from: 1, to: 'CARD.BASH' }},
          {{ from: 'CARD.STRIKE', to: false }},
          {{ from: {{ id: 'NESTED' }}, to: 'CARD.BASH' }},
          Object.create({{ from: 'PROTO', to: 'SECRET' }}),
        ].map((value) => derived('cards_transformed', [value]));
        console.log(JSON.stringify({{
          cases,
          direct,
          emoji,
          emojiScalars: Array.from(emoji.label).length,
          emojiValid: !Array.from(emoji.label).some((ch) => {{
            const code = ch.codePointAt(0);
            return code >= 0xD800 && code <= 0xDFFF;
          }}),
          invalid,
          invalidTransformed,
        }}));
        """
    )

    assert [case["label"] for case in result["cases"]] == [
        "获得 CARD.BASH",
        "获得 POTION.FIRE",
        "获得 RELIC.ANCHOR",
        "升级 CARD.STRIKE",
        "移除 CARD.DEFEND",
        "变化 CARD.STRIKE → CARD.BASH",
        "获得 RELIC.MARBLE",
        "获得 POTION.BLOCK",
    ]
    assert result["direct"]["label"] == "获得 CARD.DIRECT"
    assert result["emoji"]["prefix"] == "推导"
    assert result["emojiScalars"] <= 72
    assert result["emojiValid"] is True
    assert "😀" in result["emoji"]["label"]
    assert result["invalid"] == [None] * 12
    assert result["invalidTransformed"] == [None] * 4


def test_map_decision_transformed_delta_bounds_each_side_by_unicode_scalars():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        function transformed(from, to, next = null) {{
          const deltas = {{
            cards_transformed: {{ quality: 'derived', value: [{{ from, to }}] }},
          }};
          if (next !== null) deltas.potions_gained = {{ quality: 'exact', value: [next] }};
          return nodeDecisionSummary({{ visited: true, deltas }});
        }}
        const reviewer = transformed(`${{'x'.repeat(20)}}😀z`, 'CARD.BASH');
        const bothEmoji = transformed(`${{'f'.repeat(20)}}😀tail`, `${{'t'.repeat(21)}}😀tail`);
        const exactBoundary = transformed(`${{'a'.repeat(21)}}😀`, `${{'界'.repeat(22)}}😀`);
        const longChinese = transformed('前'.repeat(200), '后'.repeat(200));
        const emptyFrom = transformed('', 'CARD.BASH');
        const emptyTo = transformed('CARD.STRIKE', '   ');
        const invalidTo = transformed('CARD.STRIKE', '\\ud800');
        console.log(JSON.stringify({{
          reviewer, bothEmoji, exactBoundary, longChinese,
          emptyFrom, emptyTo, invalidTo,
          scalarLengths: [reviewer, bothEmoji, exactBoundary, longChinese].map((summary) =>
            Array.from(summary.label.replace(/^变化 /, '')).length),
          validUnicode: [reviewer, bothEmoji, exactBoundary, longChinese].every((summary) =>
            !Array.from(summary.label).some((scalar) => {{
              const code = scalar.codePointAt(0);
              return code >= 0xD800 && code <= 0xDFFF;
            }})),
        }}));
        """
    )

    assert result["reviewer"]["label"] == "变化 " + "x" * 20 + "😀z → CARD.BASH"
    assert "😀" in result["bothEmoji"]["label"].split(" → ")[0]
    assert "😀" in result["bothEmoji"]["label"].split(" → ")[1]
    assert result["exactBoundary"]["label"].count("😀") == 2
    assert result["longChinese"]["label"].endswith("…")
    assert all(length <= 48 for length in result["scalarLengths"])
    assert result["validUnicode"] is True
    assert result["emptyFrom"] is None
    assert result["emptyTo"] is None
    assert result["invalidTo"] is None


def test_map_decision_byte_limit_matches_python_default_json_encoding():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderDecisionSummary"
    )

    def decisions(effect_lengths: list[int], *, special: str = "") -> list[dict]:
        result: list[dict] = []
        offset = 0
        options_per_decision = len(effect_lengths) // 2
        for decision_index in range(2):
            options = []
            for option_index in range(options_per_decision):
                effect = "x" * effect_lengths[offset]
                if special and decision_index == 0 and option_index == 0:
                    effect = special
                option_id = f"d{decision_index}-o{option_index}"
                options.append(
                    {
                        "id": option_id,
                        "label": f"label{decision_index}-{option_index}",
                        "effect": effect,
                        "selected": option_index == 0,
                    }
                )
                offset += 1
            result.append(
                {
                    "kind": "event",
                    "selected_id": options[0]["id"],
                    "selected_label": options[0]["label"],
                    "options": options,
                    "evidence": "recorded",
                }
            )
        return result

    reviewer = decisions([490] * 48 + [489] * 10)
    accepted = decisions([488] * 58)
    rejected = decisions([489] * 58)
    escaped = decisions(
        [1] * 58,
        special='中文😀\\"\u2028\u0000\b\f\n\r\t',
    )

    def sizes(value: list[dict]) -> dict[str, int]:
        return {
            "python": len(
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            ),
            "compact": len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        }

    fixtures = {
        "reviewer": reviewer,
        "accepted": accepted,
        "rejected": rejected,
        "escaped": escaped,
    }
    expected = {name: sizes(value) for name, value in fixtures.items()}
    assert expected["reviewer"] == {"compact": 32341, "python": 32822}
    assert expected["accepted"]["python"] <= 32768
    assert expected["rejected"]["python"] > 32768

    result = _run_node_json(
        f"""
        {constants}
        {helpers}
        const fixtures = {json.dumps(fixtures, ensure_ascii=True)};
        const result = Object.fromEntries(Object.entries(fixtures).map(([name, value]) => [name, {{
          accepted: validateRecordedDecisions(value) !== null,
          pythonBytes: pythonDefaultJSONByteLength(value),
          compactBytes: new TextEncoder().encode(JSON.stringify(value)).length,
        }}]));
        console.log(JSON.stringify(result));
        """
    )

    assert result["reviewer"] == {
        "accepted": False,
        "pythonBytes": 32822,
        "compactBytes": 32341,
    }
    assert result["accepted"]["accepted"] is True
    assert result["rejected"]["accepted"] is False
    for name in fixtures:
        assert result[name]["pythonBytes"] == expected[name]["python"]
        assert result[name]["compactBytes"] == expected[name]["compact"]


def test_map_decision_popover_renders_all_records_and_coordinates_hover_focus():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    decisions = _recorded_decisions_fixture()
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderNodeArt"
    )

    result = _run_node_json(
        f"""
        {constants}
        let activeDecisionAnchor = null;
        let decisionClipSerial = 0;
        let decisionPopoverHovered = false;
        let decisionPopoverBound = false;
        const decisionAnchorStates = new WeakMap();
        class FakeNode {{
          constructor(tag, id = '') {{
            this.tag = tag;
            this.id = id;
            this.children = [];
            this.attributes = {{}};
            this.listeners = {{}};
            this.style = {{}};
            this.className = '';
            this.textContent = '';
            this.hidden = false;
            this.disabled = false;
            this.inert = false;
            this.isConnected = true;
            this.rect = {{ left: 20, right: 76, top: 20, bottom: 76, width: 56, height: 56 }};
          }}
          append(...children) {{ this.children.push(...children); }}
          replaceChildren(...children) {{ this.children = [...children]; }}
          setAttribute(name, value) {{ this.attributes[name] = String(value); }}
          removeAttribute(name) {{ delete this.attributes[name]; }}
          getAttribute(name) {{ return this.attributes[name] ?? null; }}
          hasAttribute(name) {{ return Object.hasOwn(this.attributes, name); }}
          addEventListener(name, listener) {{ (this.listeners[name] ||= []).push(listener); }}
          dispatch(name, event = {{}}) {{
            (this.listeners[name] || []).forEach((listener) => listener({{
              preventDefault() {{}}, stopPropagation() {{}}, relatedTarget: null, ...event,
            }}));
          }}
          contains(candidate) {{
            return candidate === this || this.children.some((child) => child && child.contains && child.contains(candidate));
          }}
          closest() {{ return null; }}
          getBoundingClientRect() {{ return this.rect; }}
        }}
        const elements = new Map();
        ['mapDecisionPopover', 'mapDecisionTitle', 'mapDecisionBody'].forEach((id) => elements.set(id, new FakeNode('div', id)));
        const popover = elements.get('mapDecisionPopover');
        popover.hidden = true;
        popover.rect = {{ left: 0, right: 300, top: 0, bottom: 200, width: 300, height: 200 }};
        function byId(id) {{ return elements.get(id); }}
        function clear(node) {{ node.replaceChildren(); }}
        function element(tag, options = {{}}) {{
          const node = new FakeNode(tag);
          if (options.className) node.className = options.className;
          if (options.text !== undefined) node.textContent = String(options.text);
          return node;
        }}
        const window = {{ innerWidth: 500, innerHeight: 400 }};
        function queueMicrotask(callback) {{ callback(); }}
        function treeText(node) {{
          return [node.textContent, ...node.children.flatMap(treeText)].filter(Boolean);
        }}
        {helpers}
        const node = {{ visited: true, decisions: {json.dumps(decisions, ensure_ascii=True)} }};
        const anchor = new FakeNode('g', 'anchor');
        bindDecisionPopover(anchor, node);
        anchor.dispatch('mouseenter');
        const allText = treeText(elements.get('mapDecisionBody'));
        anchor.dispatch('focusin');
        anchor.dispatch('mouseleave');
        const openWhileFocused = !popover.hidden;
        anchor.dispatch('focusout', {{ relatedTarget: new FakeNode('div') }});
        const closedAfterBlur = popover.hidden;
        anchor.dispatch('mouseenter');
        anchor.dispatch('focusin');
        anchor.dispatch('focusout', {{ relatedTarget: new FakeNode('div') }});
        const openWhileHovered = !popover.hidden;
        anchor.dispatch('mouseleave');
        const closedAfterLeave = popover.hidden;
        anchor.dispatch('mouseenter');
        anchor.dispatch('keydown', {{ key: 'Escape' }});
        const escapeClosed = popover.hidden && !anchor.hasAttribute('aria-describedby');
        anchor.dispatch('mouseleave');
        anchor.dispatch('mouseenter');
        const reopened = !popover.hidden && anchor.getAttribute('aria-describedby') === 'mapDecisionPopover';

        const other = new FakeNode('g', 'other');
        bindDecisionPopover(other, node);
        other.dispatch('mouseenter');
        const switched = !anchor.hasAttribute('aria-describedby') && other.hasAttribute('aria-describedby');
        const disconnected = new FakeNode('g', 'disconnected');
        disconnected.isConnected = false;
        showDecisionPopover(node, disconnected);
        const staleClosed = popover.hidden && !other.hasAttribute('aria-describedby');

        const offscreen = new FakeNode('g', 'offscreen');
        offscreen.rect = {{ left: 900, right: 956, top: 900, bottom: 956, width: 56, height: 56 }};
        showDecisionPopover(node, offscreen);
        const positioned = {{ left: Number.parseFloat(popover.style.left), top: Number.parseFloat(popover.style.top) }};
        const negative = new FakeNode('g', 'negative');
        negative.rect = {{ left: -100, right: -44, top: -100, bottom: -44, width: 56, height: 56 }};
        showDecisionPopover(node, negative);
        const negativePositioned = {{
          visible: !popover.hidden,
          left: Number.parseFloat(popover.style.left),
          top: Number.parseFloat(popover.style.top),
        }};
        const invalidAnchors = [
          Object.assign(new FakeNode('g'), {{ hidden: true }}),
          Object.assign(new FakeNode('g'), {{ disabled: true }}),
          Object.assign(new FakeNode('g'), {{ inert: true }}),
        ];
        invalidAnchors.push(new FakeNode('g'));
        invalidAnchors[3].setAttribute('aria-hidden', 'true');
        invalidAnchors.push(new FakeNode('g'));
        invalidAnchors[4].setAttribute('disabled', '');
        const invalidClosed = invalidAnchors.map((candidate) => {{
          showDecisionPopover(node, candidate);
          return popover.hidden && !offscreen.hasAttribute('aria-describedby');
        }});
        console.log(JSON.stringify({{
          allText, openWhileFocused, closedAfterBlur, openWhileHovered,
          closedAfterLeave, escapeClosed, reopened, switched, staleClosed,
          positioned, negativePositioned, invalidClosed,
        }}));
        """
    )

    labels = {
        "event": "事件",
        "card_reward": "卡",
        "potion": "药水",
        "relic": "遗物",
        "shop": "商店",
        "rest": "休息",
    }
    for decision in decisions:
        selected = next(option for option in decision["options"] if option["selected"])
        expected_text = [
            f"{labels[decision['kind']]}：{decision['selected_label']}",
            *(option["label"] for option in decision["options"]),
            *(
                option["effect"]
                for option in decision["options"]
                if option["effect"] is not None
            ),
        ]
        if selected["effect"] is not None:
            expected_text.append(selected["effect"])
        for expected in expected_text:
            assert any(expected in text for text in result["allText"])
    assert result["openWhileFocused"] is True
    assert result["closedAfterBlur"] is True
    assert result["openWhileHovered"] is True
    assert result["closedAfterLeave"] is True
    assert result["escapeClosed"] is True
    assert result["reopened"] is True
    assert result["switched"] is True
    assert result["staleClosed"] is True
    assert 8 <= result["positioned"]["left"] <= 192
    assert 8 <= result["positioned"]["top"] <= 192
    assert result["negativePositioned"] == {"visible": True, "left": 8, "top": 8}
    assert result["invalidClosed"] == [True] * 5


def test_map_decision_popover_repositions_active_state_on_any_scroll_without_rebuilding():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    position = _javascript_section(
        script, "function usableDecisionAnchor", "function showDecisionPopover"
    )
    handler = _javascript_section(
        script, "function mapDecisionScroll", "function bindDecisionPopover"
    )

    result = _run_node_json(
        f"""
        class FakeNode {{
          constructor(id, parent = null) {{
            this.id = id;
            this.parent = parent;
            this.children = [];
            this.attributes = {{}};
            this.hidden = false;
            this.isConnected = true;
            this.scrollTop = 0;
            this.style = {{}};
            this.rect = {{ left: 20, right: 76, top: 20, bottom: 76, width: 56, height: 56 }};
            if (parent) parent.children.push(this);
          }}
          contains(candidate) {{
            return candidate === this || this.children.some((child) => child.contains(candidate));
          }}
          closest(selector) {{
            if (selector !== '#mapDecisionPopover') return null;
            let candidate = this;
            while (candidate) {{
              if (candidate.id === 'mapDecisionPopover') return candidate;
              candidate = candidate.parent;
            }}
            return null;
          }}
          setAttribute(name, value) {{ this.attributes[name] = String(value); }}
          removeAttribute(name) {{ delete this.attributes[name]; }}
          getAttribute(name) {{ return this.attributes[name] ?? null; }}
          hasAttribute(name) {{ return Object.hasOwn(this.attributes, name); }}
          getBoundingClientRect() {{ return this.rect; }}
        }}
        const popover = new FakeNode('mapDecisionPopover');
        const child = new FakeNode('child', popover);
        const anchor = new FakeNode('anchor');
        const documentTarget = new FakeNode('document');
        let activeDecisionAnchor = anchor;
        let decisionPopoverHovered = false;
        const decisionAnchorStates = new WeakMap([[anchor, {{ hovered: false, focused: true }}]]);
        anchor.setAttribute('aria-describedby', 'mapDecisionPopover');
        popover.scrollTop = 137;
        popover.rect = {{ left: 0, right: 300, top: 0, bottom: 200, width: 300, height: 200 }};
        const bodyChild = new FakeNode('body-child', popover);
        const originalChildren = popover.children.slice();
        const window = {{ innerWidth: 500, innerHeight: 400 }};
        function byId(id) {{ if (id === 'mapDecisionPopover') return popover; throw new Error(id); }}
        function hideDecisionPopover() {{
          if (activeDecisionAnchor) activeDecisionAnchor.removeAttribute('aria-describedby');
          popover.hidden = true;
          activeDecisionAnchor = null;
        }}
        {position}
        {handler}
        function snapshot() {{
          return {{
            hidden: popover.hidden,
            described: anchor.hasAttribute('aria-describedby'),
            scrollTop: popover.scrollTop,
            left: popover.style.left,
            top: popover.style.top,
            sameChildren: popover.children.length === originalChildren.length
              && popover.children.every((item, index) => item === originalChildren[index]),
          }};
        }}
        anchor.rect = {{ left: 110, right: 166, top: 50, bottom: 106, width: 56, height: 56 }};
        mapDecisionScroll({{ target: documentTarget }});
        const afterDocumentScroll = snapshot();
        anchor.rect = {{ left: 900, right: 956, top: 900, bottom: 956, width: 56, height: 56 }};
        mapDecisionScroll({{ target: child }});
        const afterPopoverScroll = snapshot();
        decisionAnchorStates.get(anchor).focused = false;
        mapDecisionScroll({{ target: documentTarget }});
        const inactive = snapshot();
        console.log(JSON.stringify({{ afterDocumentScroll, afterPopoverScroll, inactive }}));
        """
    )

    assert result["afterDocumentScroll"] == {
        "hidden": False,
        "described": True,
        "scrollTop": 137,
        "left": "174px",
        "top": "114px",
        "sameChildren": True,
    }
    assert result["afterPopoverScroll"] == {
        "hidden": False,
        "described": True,
        "scrollTop": 137,
        "left": "192px",
        "top": "192px",
        "sameChildren": True,
    }
    assert result["inactive"]["hidden"] is True
    assert result["inactive"]["described"] is False
    assert result["inactive"]["scrollTop"] == 137
    assert result["inactive"]["sameChildren"] is True


def test_map_decision_popover_shares_pointer_lifecycle_with_active_anchor():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    constants = "\n".join(
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith(("const DELTA_", "const MAP_DECISION_", "const DECISION_"))
    )
    helpers = _javascript_section(
        script, "function boundedDeltaLabel", "function renderNodeArt"
    )

    result = _run_node_json(
        f"""
        {constants}
        let activeDecisionAnchor = null;
        let decisionClipSerial = 0;
        let decisionPopoverHovered = false;
        let decisionPopoverBound = false;
        const decisionAnchorStates = new WeakMap();
        const microtasks = [];
        function queueMicrotask(callback) {{ microtasks.push(callback); }}
        function flushMicrotasks() {{ while (microtasks.length) microtasks.shift()(); }}
        class FakeNode {{
          constructor(tag, id = '', parent = null) {{
            this.tag = tag;
            this.id = id;
            this.parent = parent;
            this.children = [];
            this.attributes = {{}};
            this.listeners = {{}};
            this.style = {{}};
            this.className = '';
            this.textContent = '';
            this.hidden = false;
            this.disabled = false;
            this.inert = false;
            this.isConnected = true;
            this.scrollTop = 0;
            this.rect = {{ left: 20, right: 76, top: 20, bottom: 76, width: 56, height: 56 }};
            if (parent) parent.children.push(this);
          }}
          append(...children) {{
            children.forEach((child) => {{ if (child && typeof child === 'object') child.parent = this; }});
            this.children.push(...children);
          }}
          replaceChildren(...children) {{ this.children = []; this.append(...children); }}
          setAttribute(name, value) {{ this.attributes[name] = String(value); }}
          removeAttribute(name) {{ delete this.attributes[name]; }}
          getAttribute(name) {{ return this.attributes[name] ?? null; }}
          hasAttribute(name) {{ return Object.hasOwn(this.attributes, name); }}
          addEventListener(name, listener) {{ (this.listeners[name] ||= []).push(listener); }}
          dispatch(name, event = {{}}) {{
            (this.listeners[name] || []).forEach((listener) => listener({{
              preventDefault() {{}}, stopPropagation() {{}}, relatedTarget: null, target: this, ...event,
            }}));
          }}
          contains(candidate) {{
            return candidate === this || this.children.some((child) => child && child.contains && child.contains(candidate));
          }}
          closest(selector) {{
            if (selector === '#mapDecisionPopover') {{
              let candidate = this;
              while (candidate) {{
                if (candidate.id === 'mapDecisionPopover') return candidate;
                candidate = candidate.parent;
              }}
            }}
            return null;
          }}
          getBoundingClientRect() {{ return this.rect; }}
        }}
        const elements = new Map();
        ['mapDecisionPopover', 'mapDecisionTitle', 'mapDecisionBody'].forEach((id) => elements.set(id, new FakeNode('div', id)));
        const popover = elements.get('mapDecisionPopover');
        popover.hidden = true;
        popover.rect = {{ left: 0, right: 300, top: 0, bottom: 200, width: 300, height: 200 }};
        const popoverChild = new FakeNode('div', 'popoverChild', popover);
        function byId(id) {{ return elements.get(id); }}
        function clear(node) {{ node.replaceChildren(); }}
        function element(tag, options = {{}}) {{
          const node = new FakeNode(tag);
          if (options.className) node.className = options.className;
          if (options.text !== undefined) node.textContent = String(options.text);
          return node;
        }}
        const window = {{ innerWidth: 500, innerHeight: 400 }};
        function decision(prefix) {{
          const selected = {{ id: `${{prefix}}-1`, label: `${{prefix}}选择`, effect: `${{prefix}}效果`, selected: true }};
          return {{ kind: 'event', selected_id: selected.id, selected_label: selected.label, options: [selected], evidence: 'recorded' }};
        }}
        {helpers}
        const first = new FakeNode('g', 'first');
        const second = new FakeNode('g', 'second');
        const firstNode = {{ visited: true, decisions: [decision('第一')] }};
        const secondNode = {{ visited: true, decisions: [decision('第二')] }};
        bindDecisionPopover(first, firstNode);
        bindDecisionPopover(second, secondNode);

        first.dispatch('mouseenter');
        first.dispatch('mouseleave', {{ relatedTarget: popover }});
        const deferredAtBoundary = !popover.hidden && first.hasAttribute('aria-describedby');
        popover.dispatch('mouseenter', {{ relatedTarget: first }});
        popover.scrollTop = 91;
        mapDecisionScroll({{ target: popoverChild, composedPath() {{ return [popoverChild, popover]; }} }});
        flushMicrotasks();
        const inside = {{ hidden: popover.hidden, described: first.hasAttribute('aria-describedby'), scrollTop: popover.scrollTop }};

        popover.dispatch('mouseleave', {{ relatedTarget: new FakeNode('div', 'outside') }});
        const leftOutside = popover.hidden && !first.hasAttribute('aria-describedby');

        first.rect = {{ left: 300, right: 356, top: 40, bottom: 96, width: 56, height: 56 }};
        first.dispatch('focus');
        mapDecisionScroll({{ target: new FakeNode('div', 'document') }});
        const programmaticFocusSurvivedScroll = !popover.hidden
          && first.hasAttribute('aria-describedby')
          && popover.style.left === '192px';
        first.dispatch('focusout', {{ relatedTarget: new FakeNode('div', 'outside') }});
        const focusoutDeferred = !popover.hidden && first.hasAttribute('aria-describedby');
        flushMicrotasks();
        const focusoutClosed = popover.hidden && !first.hasAttribute('aria-describedby');

        first.dispatch('mouseenter');
        first.dispatch('focusin');
        first.dispatch('mouseleave', {{ relatedTarget: popover }});
        popover.dispatch('mouseenter', {{ relatedTarget: first }});
        first.dispatch('focusout', {{ relatedTarget: new FakeNode('div', 'outside') }});
        const popoverHoverKeepsBlurredAnchor = !popover.hidden && first.hasAttribute('aria-describedby');
        popover.dispatch('mouseleave', {{ relatedTarget: new FakeNode('div', 'outside') }});
        const closedAfterPopoverLeavesBlurredAnchor = popover.hidden && !first.hasAttribute('aria-describedby');

        first.dispatch('mouseenter');
        first.dispatch('mouseleave', {{ relatedTarget: popover }});
        popover.dispatch('mouseenter', {{ relatedTarget: first }});
        popover.dispatch('mouseleave', {{ relatedTarget: first }});
        first.dispatch('mouseenter', {{ relatedTarget: popover }});
        flushMicrotasks();
        const returnedToAnchor = !popover.hidden && first.hasAttribute('aria-describedby');

        first.dispatch('mouseleave', {{ relatedTarget: new FakeNode('div', 'outside') }});
        second.dispatch('mouseenter');
        flushMicrotasks();
        const switched = !popover.hidden
          && !first.hasAttribute('aria-describedby')
          && second.hasAttribute('aria-describedby')
          && elements.get('mapDecisionTitle').textContent.includes('第二');

        second.dispatch('mouseleave', {{ relatedTarget: popover }});
        popover.dispatch('mouseenter', {{ relatedTarget: second }});
        second.dispatch('keydown', {{ key: 'Escape' }});
        const escaped = popover.hidden && !second.hasAttribute('aria-describedby');
        popover.dispatch('mouseenter', {{ relatedTarget: second }});
        flushMicrotasks();
        const stayedClosed = popover.hidden && !second.hasAttribute('aria-describedby');
        second.dispatch('mouseenter');
        const reopenedOnNewEnter = !popover.hidden && second.hasAttribute('aria-describedby');

        console.log(JSON.stringify({{
          deferredAtBoundary, inside, leftOutside,
          programmaticFocusSurvivedScroll, focusoutDeferred, focusoutClosed,
          popoverHoverKeepsBlurredAnchor, closedAfterPopoverLeavesBlurredAnchor,
          returnedToAnchor,
          switched, escaped, stayedClosed, reopenedOnNewEnter,
        }}));
        """
    )

    assert result["deferredAtBoundary"] is True
    assert result["inside"] == {"hidden": False, "described": True, "scrollTop": 91}
    assert result["leftOutside"] is True
    assert result["programmaticFocusSurvivedScroll"] is True
    assert result["focusoutDeferred"] is True
    assert result["focusoutClosed"] is True
    assert result["popoverHoverKeepsBlurredAnchor"] is True
    assert result["closedAfterPopoverLeavesBlurredAnchor"] is True
    assert result["returnedToAnchor"] is True
    assert result["switched"] is True
    assert result["escaped"] is True
    assert result["stayedClosed"] is True
    assert result["reopenedOnNewEnter"] is True


def test_map_decision_tooltip_contract_is_safe_accessible_and_keeps_node_keys():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    show = _javascript_section(
        script, "function showDecisionPopover", "function renderNodeArt"
    )
    position = _javascript_section(
        script, "function positionDecisionPopover", "function showDecisionPopover"
    )
    render = _javascript_section(
        script, "function renderDecisionSummary", "function showDecisionPopover"
    )
    nodes = _javascript_section(script, "function renderNodes", "function renderMap")
    load = _javascript_section(script, "async function loadAct", "function closeMapPage")

    assert '<aside id="mapDecisionPopover" class="map-decision-popover" role="tooltip" hidden>' in index
    assert 'id="mapDecisionTitle"' in index
    assert 'id="mapDecisionBody"' in index
    assert "textContent" in show
    assert "innerHTML" not in script
    assert "该对局未记录备选项" in show
    assert "getBoundingClientRect" in position
    assert "window.innerWidth" in position and "window.innerHeight" in position
    assert "Math.max(8" in position
    assert "aria-describedby" in show
    for event_name in ("mouseenter", "mouseleave", "focusin", "focusout"):
        assert event_name in show
    assert "relatedTarget" in show
    assert "event.key === 'Escape'" in show
    assert "event.stopPropagation()" in show
    assert "window.addEventListener('resize', mapDecisionScroll)" in script
    assert "window.addEventListener('scroll', mapDecisionScroll, true)" in script
    assert load.index("hideDecisionPopover()") < load.index("clear(byId('mapSvg'))")
    assert "event.key === 'Enter' || event.key === ' '" in nodes
    assert "selectNode(node, group)" in nodes
    assert "nodeDecisionSummary(node)" in nodes
    assert "createSvg('clipPath'" in render
    assert "createSvg('tspan', { class: 'map-decision-effect' })" in render
    assert ".map-decision-summary" in css
    assert ".map-decision-effect" in css
    assert ".map-decision-popover[hidden]" in css
    assert ".map-decision-option-effect" in css
    popover_css = css[css.index(".map-decision-popover {") : css.index("}", css.index(".map-decision-popover {"))]
    assert "pointer-events: auto" in popover_css
    assert "pointer-events: none" in css


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
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cohort_script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    runs_table_script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    run_view_script = (STATIC_DIR / "run-view.js").read_text(encoding="utf-8")
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    map_script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    # map.js owns the /api/run/map request; run-view.js delegates to it via
    # STS2Map.openRun rather than issuing the query itself.
    everything = (
        app_script + cohort_script + runs_table_script + run_view_script + map_script
    )

    assert "Promise.all" in app_script
    assert "getJSON('/api/tree')" in app_script
    assert "getJSON('/api/cohorts')" in app_script
    assert "getJSON('/api/catalog')" in app_script
    assert "Tree.render(state.tree)" in app_script
    for endpoint in ("/api/metrics", "/api/source", "/api/run", "/api/parse", "/api/cohort/runs", "/api/run/map"):
        assert endpoint in everything, endpoint
    assert "comparison.mismatch_reasons" in cohort_script
    assert "comparison.notes" in cohort_script
    assert "payload.view" in app_script
    assert "formatMissing" in util_script
    assert "return '—'" in util_script or 'return "—"' in util_script
    assert "act2_entry_denominator" in cohort_script
    assert "technical_n" in cohort_script
    assert "createElementNS" in util_script


def test_baseline_default_uses_server_descriptor_without_client_axis_logic():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    helper = _javascript_section(
        script, "function defaultBaselineCohortId", "function comparisonAxisLabel"
    )

    assert "default_baseline_cohort_id" in helper
    assert "comparison_readiness" in helper
    assert "comparisonCompatible" not in script
    assert "nearestDistinctCohortId" not in script
    assert ".filters" not in helper
    assert "currentIndex" not in helper


def test_default_baseline_helper_is_pure_and_fails_closed_on_bad_descriptors():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function currentCohortDescriptor"
    )
    helper = _javascript_section(
        script, "function defaultBaselineCohortId", "function comparisonAxisLabel"
    )

    payload = _run_node_json(
        f"""
        {identity}
        {helper}
        const ready = {{
          cohort_id: 'a',
          comparison_readiness: {{ ready: true }},
          default_baseline_cohort_id: 'b',
        }};
        const candidateB = {{ cohort_id: 'b' }};
        const throwingCurrent = new Proxy({{}}, {{
          get() {{ throw new Error('untrusted current'); }},
        }});
        const throwingCandidate = new Proxy({{}}, {{
          get() {{ throw new Error('untrusted candidate'); }},
        }});
        console.log(JSON.stringify({{
          ready: defaultBaselineCohortId(ready, [ready, candidateB]),
          filtered: defaultBaselineCohortId(ready, [ready]),
          missing: defaultBaselineCohortId({{
            cohort_id: 'a', comparison_readiness: {{ ready: true }},
            default_baseline_cohort_id: null,
          }}, [candidateB]),
          blank: defaultBaselineCohortId({{
            cohort_id: 'a', comparison_readiness: {{ ready: true }},
            default_baseline_cohort_id: '   ',
          }}, [candidateB]),
          nonString: defaultBaselineCohortId({{
            cohort_id: 'a', comparison_readiness: {{ ready: true }},
            default_baseline_cohort_id: 7,
          }}, [candidateB]),
          self: defaultBaselineCohortId({{
            cohort_id: ' a ', comparison_readiness: {{ ready: true }},
            default_baseline_cohort_id: 'a',
          }}, [{{ cohort_id: 'a' }}]),
          duplicate: defaultBaselineCohortId(ready, [
            ready, candidateB, {{ cohort_id: ' b ' }},
          ]),
          notReady: defaultBaselineCohortId({{
            ...ready, comparison_readiness: {{ ready: false }},
          }}, [ready, candidateB]),
          whitespace: defaultBaselineCohortId({{
            ...ready, default_baseline_cohort_id: ' b ',
          }}, [ready, candidateB]),
          malformedCandidates: defaultBaselineCohortId(
            ready, [null, 42, Object.create(null), throwingCandidate, candidateB]
          ),
          throwingCurrent: defaultBaselineCohortId(throwingCurrent, [candidateB]),
        }}));
        """
    )

    assert payload == {
        "ready": "b",
        "filtered": "",
        "missing": "",
        "blank": "",
        "nonString": "",
        "self": "",
        "duplicate": "",
        "notReady": "",
        "whitespace": "b",
        "malformedCandidates": "b",
        "throwingCurrent": "",
    }


def test_comparison_help_is_neutral_complete_and_redrawn_without_duplication():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    help_section = _javascript_section(
        script, "function comparisonAxisLabel", "function baselineCandidates"
    )

    for wording in (
        "元数据不完整，仅展示本批次",
        "当前批次可查看，但暂无可直接比较的基线",
        "当前与基线不可直接比较",
        "missing_axes",
        "mixed_axes",
        "invalid_axes",
    ):
        assert wording in script
    for axis, label in (
        ("character", "角色"),
        ("game_version", "游戏版本"),
        ("evaluation_mode", "评测模式"),
        ("scenario", "场景"),
        ("ascension", "进阶"),
        ("seed", "种子"),
        ("valid_results", "有效结果"),
    ):
        assert f"{axis}: '{label}'" in help_section
    # There is no more per-cohort "currentHelp" -- the batch title itself
    # is the only current-cohort indicator now; only baselineHelp remains.
    assert 'select id="baselineCohort" aria-describedby="baselineHelp"' in html
    assert 'id="baselineHelp"' in html
    assert html.count('aria-live="polite"') >= 5
    # The shell ships the pre-selection placeholder only. Every comparison
    # verdict above is written into baselineHelp by cohort-view.js once a
    # batch is chosen, so the shell must not hard-code one of them as if it
    # were already true.
    assert "选择批次后可比较基线" in html
    assert "当前批次可查看，但暂无可直接比较的基线" not in html


def test_cohort_options_preserve_manual_choice_and_only_default_on_current_change():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function currentCohortDescriptor"
    )
    selector = _javascript_section(
        script, "function defaultBaselineCohortId", "function baselineChanged"
    )
    render_fn = _javascript_section(script, "let lastRenderedCohortId", "function renderNoCohort")

    assert "function renderBaselineSelect(current, { forceDefault = false } = {})" in selector
    assert "forceDefault: lastRenderedCohortId !== cohortId" in render_fn
    assert "nearestDistinctCohortId" not in selector

    payload = _run_node_json(
        f"""
        const nodes = {{
          baselineCohort: {{ value: '', optionValues: [] }},
          baselineHelp: {{ textContent: '' }},
        }};
        const byId = (id) => nodes[id];
        const formatTime = (value) => `time-${{value}}`;
        function setSelectOptions(select, options, emptyLabel, preferred) {{
          select.optionValues = options.map((option) => option.value);
          select.value = '';
          if (preferred && options.some((option) => option.value === preferred)) {{
            select.value = preferred;
          }}
        }}
        const ready = {{ ready: true, missing_axes: [], mixed_axes: [], invalid_axes: [] }};
        const cohort = (id, signature, defaultId) => ({{
          cohort_id: id,
          label: `cohort-${{id}}`,
          run_count: 2,
          latest_at: id.charCodeAt(0),
          comparison_readiness: {{ ...ready, comparison_signature: signature }},
          default_baseline_cohort_id: defaultId,
        }});
        const a = cohort('a', 'sig-1', 'b');
        const b = cohort('b', 'sig-1', 'a');
        const c = cohort('c', 'sig-1', null);
        const other = cohort('other', 'sig-2', null);
        const state = {{ cohorts: [a, b, c, other] }};
        {identity}
        {selector}

        const initialBaseline = renderBaselineSelect(a);
        const initialOptions = [...nodes.baselineCohort.optionValues];
        const initialHelp = nodes.baselineHelp.textContent;

        nodes.baselineCohort.value = 'c';
        const manualBaseline = renderBaselineSelect(a, {{ forceDefault: false }});
        const manualHelp = nodes.baselineHelp.textContent;

        const forcedBaseline = renderBaselineSelect(b, {{ forceDefault: true }});

        console.log(JSON.stringify({{
          initialBaseline, initialOptions, initialHelp, manualBaseline, manualHelp, forcedBaseline,
        }}));
        """
    )

    assert payload["initialBaseline"] == "b"
    assert payload["initialOptions"] == ["b", "c"]
    assert payload["initialHelp"] == "已采用服务端验证的兼容基线；手动选择后仍会再次校验"
    # Manual choice survives a re-render of the *same* cohort.
    assert payload["manualBaseline"] == "c"
    assert payload["manualHelp"] == "已选择基线；服务端将校验口径并提供精确原因"
    # Switching cohorts re-defaults even though 'c' is still a valid option
    # for cohort b -- forceDefault, not option membership, decides this.
    assert payload["forcedBaseline"] == "a"


_FAKE_TREE_DOM = """
function makeNode(tag) {
  const node = {
    tag, className: '', textContent: '', hidden: false,
    _attrs: {}, children: [], parentElement: null,
    setAttribute(name, value) { this._attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    removeAttribute(name) { delete this._attrs[name]; },
    append(...items) {
      items.forEach((item) => {
        if (item === null || item === undefined) return;
        if (typeof item !== 'object') { this.textContent += String(item); return; }
        item.parentElement = this;
        this.children.push(item);
      });
    },
    addEventListener() {},
    querySelectorAll(selector) {
      const out = [];
      const roleMatch = selector.match(/^\\[role="([^"]+)"\\]$/);
      const walk = (node) => {
        node.children.forEach((child) => {
          if (roleMatch && child._attrs.role === roleMatch[1]) out.push(child);
          walk(child);
        });
      };
      walk(this);
      return out;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
  return node;
}
function collectText(node) {
  return node.textContent + node.children.map(collectText).join('');
}
const document = { createElement: (tag) => makeNode(tag) };
const window = {};
const CSS = { escape: (value) => value };
const treeNode = makeNode('nav');
const nodes = { cohortTree: treeNode };
function byId(id) { return nodes[id]; }
function clear(node) { node.children = []; node.textContent = ''; node.parentElement = null; }
function renderEmpty(container, message) { clear(container); container.textContent = message; }
function formatMissing(value, digits) {
  return value === null || value === undefined ? '\\u2014' : String(value);
}
function navigate() {}
function cohortRoute(id) { return `#/batch/${id}`; }
"""


def test_tree_renders_version_character_cohort_nesting_in_server_order():
    """The tree must reproduce /api/tree's order verbatim -- newest-first
    within a version, with the null game_version bucket last -- and must
    never re-sort client-side (the old numeric-version-descending sort is
    gone along with the dropdown filters it used to serve)."""
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    tree_script = (STATIC_DIR / "tree.js").read_text(encoding="utf-8")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")

    assert "sort(" not in tree_script
    assert "compareGameVersionsDescending" not in tree_script
    tree_source = tree_script.replace("window.Tree = (() => {", "const Tree = (() => {", 1)

    payload = _run_node_json(
        f"""
        {_FAKE_TREE_DOM}
        {element_fn}
        {tree_source}

        const cohortA = {{
          cohort_id: 'a', label: 'Batch A', run_count: 5, technical_count: 0,
          latest_at: 100, unarchived: false,
        }};
        const cohortB = {{
          cohort_id: 'b', label: 'Batch B', run_count: 3, technical_count: 2,
          latest_at: 50, unarchived: true,
        }};
        const cohortC = {{
          cohort_id: 'c', label: 'Batch C', run_count: 1, technical_count: 0,
          latest_at: 10, unarchived: false,
        }};
        // Deliberately NOT numerically descending (0.50.0 before 0.111.0)
        // to prove the tree renders whatever order the server sent rather
        // than re-deriving one client-side. The null-version bucket is
        // last, per the API contract.
        const tree = [
          {{ game_version: '0.50.0', characters: [
            {{ character: 'Ironclad', cohorts: [cohortA] }},
          ] }},
          {{ game_version: '0.111.0', characters: [
            {{ character: null, cohorts: [cohortB] }},
          ] }},
          {{ game_version: null, characters: [
            {{ character: 'Silent', cohorts: [cohortC] }},
          ] }},
        ];

        Tree.render(tree);
        const text = collectText(treeNode);
        console.log(JSON.stringify({{
          text,
          treeitemCount: treeNode.querySelectorAll('[role="treeitem"]').length,
          technicalBadgePresent: text.includes('2 技术失败'),
        }}));
        """
    )

    text = payload["text"]
    order = ["0.50.0", "Ironclad", "Batch A", "0.111.0", "Batch B", "Silent", "Batch C"]
    positions = [text.index(fragment) for fragment in order]
    assert positions == sorted(positions), text
    # 3 version groups + 3 character groups + 3 cohort leaves = 9 treeitems.
    assert payload["treeitemCount"] == 9
    assert payload["technicalBadgePresent"] is True


_FAKE_TABLE_DOM = """
function makeNode(tag) {
  const node = {
    tag, className: '', textContent: '', hidden: false,
    _attrs: {}, children: [], parentElement: null, _listeners: {},
    setAttribute(name, value) { this._attrs[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    removeAttribute(name) { delete this._attrs[name]; },
    append(...items) {
      items.forEach((item) => {
        if (item === null || item === undefined) return;
        if (typeof item !== 'object') { this.textContent += String(item); return; }
        item.parentElement = this;
        this.children.push(item);
      });
    },
    replaceChildren(...items) { this.children = []; this.textContent = ''; this.append(...items); },
    addEventListener(type, handler) {
      (this._listeners[type] = this._listeners[type] || []).push(handler);
    },
    dispatchClick() { (this._listeners.click || []).forEach((handler) => handler({})); },
    querySelectorAll(selector) {
      const out = [];
      const walk = (node) => {
        node.children.forEach((child) => {
          if (selector === '*' || child.tag === selector) out.push(child);
          walk(child);
        });
      };
      walk(this);
      return out;
    },
  };
  return node;
}
const document = { createElement: (tag) => makeNode(tag) };
const tableContainer = makeNode('div');
const nodes = { runsTable: tableContainer, runsTableNotice: makeNode('p') };
function byId(id) { return nodes[id]; }
"""


def test_runs_table_sorts_by_floor_and_status_with_missing_values_last():
    """Sortable by 推进层数 and 状态, per the batch-view run table spec."""
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    sorted_rows = _javascript_section(script, "function sortedRows", "function headerCell")

    payload = _run_node_json(
        f"""
        let sortKey = null;
        let sortDir = 'desc';
        const STATUS_LABELS = {{ win: 'A-win', crash: 'B-crash', dead: 'C-dead' }};
        {sorted_rows}
        const rows = [
          {{ seed: 'a', status: 'win', global_floor: 12 }},
          {{ seed: 'b', status: 'crash', global_floor: null }},
          {{ seed: 'c', status: 'dead', global_floor: 40 }},
        ];
        sortKey = null;
        const unsorted = sortedRows(rows).map((row) => row.seed);
        sortKey = 'global_floor'; sortDir = 'desc';
        const byFloorDesc = sortedRows(rows).map((row) => row.seed);
        sortKey = 'global_floor'; sortDir = 'asc';
        const byFloorAsc = sortedRows(rows).map((row) => row.seed);
        sortKey = 'status'; sortDir = 'asc';
        const byStatusAsc = sortedRows(rows).map((row) => row.seed);
        console.log(JSON.stringify({{ unsorted, byFloorDesc, byFloorAsc, byStatusAsc }}));
        """
    )

    assert payload["unsorted"] == ["a", "b", "c"]
    # A missing floor carries no ranking information, so it sorts last in
    # BOTH directions rather than crowding the top of the ascending view.
    assert payload["byFloorDesc"] == ["c", "a", "b"]
    assert payload["byFloorAsc"] == ["a", "c", "b"]
    # Status labels above are deliberately alphabetic (A/B/C-prefixed) so
    # the localeCompare ordering is unambiguous without depending on
    # Chinese collation specifics.
    assert payload["byStatusAsc"] == ["a", "b", "c"]


def test_runs_table_renders_missing_values_as_dash_never_zero_or_blank():
    """Missing run fields render as em dash; a genuine 0 (e.g. floor 0)
    must still render as '0', never collapse into the missing-value dash
    or an empty cell."""
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    status_labels = _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")
    clear_fn = _javascript_section(util_script, "function clear(node)", "function setStatus")
    render_empty_fn = _javascript_section(util_script, "function renderEmpty", "function setSelectOptions")
    table_section = _javascript_section(script, "function missingCell", "async function render")

    # Scope this to missingCell itself: the surrounding section legitimately
    # compares a sort comparator's result against 0, which is unrelated to
    # how a run's own 0 value is rendered.
    missing_cell = _javascript_section(script, "function missingCell", "function sortedRows")
    assert "=== 0" not in missing_cell  # 0 must not be special-cased into a dash
    assert "!value" not in missing_cell  # nor collapsed by a falsy check

    payload = _run_node_json(
        f"""
        {_FAKE_TABLE_DOM}
        {status_labels}
        {element_fn}
        {clear_fn}
        {render_empty_fn}
        function formatTime(value) {{ return `T${{value}}`; }}
        let sortKey = null;
        let sortDir = 'desc';
        {table_section}

        const rows = [
          {{
            seed: null, status: null, global_floor: null, act: null, started_at: null,
            has_map: false, ref: {{ kind: 'source', id: 's1' }},
          }},
          {{
            seed: 'zero-seed', status: 'win', global_floor: 0, act: 1, started_at: 100,
            has_map: true, ref: {{ kind: 'run', id: 'r1' }},
          }},
        ];
        renderTable(rows);
        const dataRows = tableContainer.querySelectorAll('tr').slice(1);
        const cells = dataRows.map((tr) => tr.children.map((td) => td.textContent));
        console.log(JSON.stringify(cells));
        """
    )

    missing_row, zero_row = payload
    # seed, status, floor, act (indexes 1-4) are all missing -> dash, never blank.
    assert missing_row[1:5] == ["—", "—", "—", "—"]
    assert "" not in missing_row[1:5]
    assert "0" not in missing_row[1:5]
    # A real floor of 0 renders as '0', not as the missing-value dash.
    assert zero_row[3] == "0"


def test_runs_table_rows_address_via_ref_never_a_bare_run_id():
    """Every real run on disk has run_id: null; rows must be addressed
    through the ref object the API already resolved, never by reading
    row.run_id directly."""
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    status_labels = _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")
    clear_fn = _javascript_section(util_script, "function clear(node)", "function setStatus")
    render_empty_fn = _javascript_section(util_script, "function renderEmpty", "function setSelectOptions")
    table_section = _javascript_section(script, "function missingCell", "async function render")

    assert "row.run_id" not in script
    assert "ref.id" in script
    assert "ref.kind" in script

    payload = _run_node_json(
        f"""
        {_FAKE_TABLE_DOM}
        {status_labels}
        {element_fn}
        {clear_fn}
        {render_empty_fn}
        function formatTime(value) {{ return `T${{value}}`; }}
        let sortKey = null;
        let sortDir = 'desc';
        {table_section}

        const state = {{ selectedCohortId: 'cohort-x' }};
        const navigateCalls = [];
        function navigate(hash) {{ navigateCalls.push(hash); }}
        function runRoute(cohortId, ref) {{
          return `#/batch/${{cohortId}}/run/${{ref.kind}}:${{ref.id}}`;
        }}
        const rows = [
          {{
            seed: 's', status: 'win', global_floor: 1, act: 1, started_at: 1,
            run_id: null, source_id: 'legacy-source-1', has_map: true,
            ref: {{ kind: 'source', id: 'legacy-source-1' }},
          }},
        ];
        renderTable(rows);
        const dataRow = tableContainer.querySelectorAll('tr').slice(1)[0];
        const disabledBeforeClick = dataRow.getAttribute('aria-disabled');
        dataRow.dispatchClick();
        console.log(JSON.stringify({{ navigateCalls, disabledBeforeClick }}));
        """
    )

    assert payload["navigateCalls"] == ["#/batch/cohort-x/run/source:legacy-source-1"]
    assert payload["disabledBeforeClick"] is None


def test_cohort_identity_guards_current_descriptor_and_comparison():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function currentCohortDescriptor"
    )
    current_descriptor = _javascript_section(
        script, "function currentCohortDescriptor", "function defaultBaselineCohortId"
    )
    comparison = _javascript_section(
        script, "function renderComparison", "function anomalyRow"
    )

    payload = _run_node_json(
        f"""
        function makeNode() {{
          // `dataset` is a real element property; renderComparison writes the
          // banner tone through it, so the stub must expose one.
          return {{ textContent: '', children: [], dataset: {{}}, append(...children) {{ this.children.push(...children); }} }};
        }}
        const comparisonBody = makeNode();
        const comparisonBanner = makeNode();
        comparisonBanner.querySelector = () => comparisonBody;
        const nodes = {{ comparisonBanner, comparisonTitle: makeNode() }};
        const byId = (id) => nodes[id];
        const clear = (node) => {{ node.children = []; }};
        const element = (tag, options = {{}}) => {{
          const node = makeNode();
          node.textContent = options.text === undefined ? '' : String(options.text);
          return node;
        }};
        const appendList = (container, values) => container.append({{
          values: [...values], textContent: values.join('|'),
        }});
        const valid = {{
          cohort_id: 'valid-cohort', label: 'valid', comparison_readiness: {{ ready: false }},
        }};
        const missingId = {{ label: 'missing-id' }};
        const whitespaceId = {{ cohort_id: '   ', label: 'whitespace-id' }};
        const throwingId = {{
          get cohort_id() {{ throw new Error('bad cohort id getter'); }}, label: 'throwing-id',
        }};
        const malformed = [null, 42, missingId, whitespaceId, throwingId];
        const state = {{ cohorts: [...malformed, valid], selectedCohortId: 'valid-cohort' }};
        {identity}
        {current_descriptor}
        {comparison}

        const found = currentCohortDescriptor() === valid;

        renderComparison(null);
        const rendered = {{
          title: nodes.comparisonTitle.textContent,
          body: comparisonBody.children.map((child) => child.textContent),
        }};

        const descriptorCases = [];
        for (const descriptor of malformed) {{
          state.cohorts = [descriptor];
          descriptorCases.push(currentCohortDescriptor() === null);
        }}
        state.cohorts = null;
        descriptorCases.push(currentCohortDescriptor() === null);
        state.cohorts = [valid];
        state.selectedCohortId = '   ';
        descriptorCases.push(currentCohortDescriptor() === null);

        console.log(JSON.stringify({{ found, rendered, descriptorCases }}));
        """
    )

    assert payload["found"] is True
    assert payload["rendered"] == {
        "title": "元数据不完整",
        "body": ["历史记录仍可查看，但不会用于训练提升比较。"],
    }
    assert payload["descriptorCases"] == [True] * 7


def test_hash_router_parses_root_batch_and_run_routes():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    routing = _javascript_section(script, "function cohortRoute", "function navigate")

    assert "function parseRoute" in routing
    assert "function runRoute" in routing

    payload = _run_node_json(
        f"""
        {routing}
        console.log(JSON.stringify({{
          root: parseRoute(''),
          rootSlash: parseRoute('#/'),
          batch: parseRoute('#/batch/coh-1'),
          runBySource: parseRoute('#/batch/coh-1/run/source:src%2F1'),
          runById: parseRoute('#/batch/coh-1/run/run:r1'),
          malformed: parseRoute('#/nonsense'),
          roundTripBatch: cohortRoute('coh 1'),
          roundTripRun: runRoute('coh-1', {{ kind: 'source', id: 'a/b' }}),
          roundTripRunNoCohort: runRoute('', {{ kind: 'run', id: 'z' }}),
        }}));
        """
    )

    assert payload["root"] == {"view": "root"}
    assert payload["rootSlash"] == {"view": "root"}
    assert payload["batch"] == {"view": "batch", "cohortId": "coh-1"}
    assert payload["runBySource"] == {
        "view": "run", "cohortId": "coh-1", "ref": {"kind": "source", "id": "src/1"},
    }
    assert payload["runById"] == {
        "view": "run", "cohortId": "coh-1", "ref": {"kind": "run", "id": "r1"},
    }
    assert payload["malformed"] == {"view": "root"}
    assert payload["roundTripBatch"] == "#/batch/coh%201"
    assert payload["roundTripRun"] == "#/batch/coh-1/run/source:a%2Fb"
    assert payload["roundTripRunNoCohort"] == "#/batch/-/run/run:z"


def test_render_comparison_null_distinguishes_incomplete_and_ready_current():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function currentCohortDescriptor"
    )
    current_descriptor = _javascript_section(
        script, "function currentCohortDescriptor", "function defaultBaselineCohortId"
    )
    comparison = _javascript_section(
        script, "function renderComparison", "function anomalyRow"
    )

    payload = _run_node_json(
        f"""
        function makeNode() {{
          // `dataset` is a real element property; renderComparison writes the
          // banner tone through it, so the stub must expose one.
          return {{ textContent: '', children: [], dataset: {{}}, append(...children) {{ this.children.push(...children); }} }};
        }}
        const body = makeNode();
        const banner = makeNode();
        banner.querySelector = () => body;
        const title = makeNode();
        const nodes = {{ comparisonBanner: banner, comparisonTitle: title }};
        const byId = (id) => nodes[id];
        const clear = (node) => {{ node.children = []; }};
        const element = (tag, options = {{}}) => {{
          const node = makeNode();
          node.textContent = options.text === undefined ? '' : String(options.text);
          return node;
        }};
        const appendList = (container, values) => container.append({{
          values: [...values], textContent: values.join('|'),
        }});
        const formatMissing = String;
        function deltaText(value) {{
          return {{ text: String(value), direction: 'flat' }};
        }}
        const state = {{
          cohorts: [
            {{ cohort_id: 'incomplete', comparison_readiness: {{ ready: false }} }},
            {{ cohort_id: 'ready', comparison_readiness: {{ ready: true }} }},
          ],
          selectedCohortId: 'incomplete',
        }};
        {identity}
        {current_descriptor}
        {comparison}

        renderComparison(null);
        const incomplete = {{
          title: title.textContent,
          body: body.children.map((child) => child.textContent),
        }};
        state.selectedCohortId = 'ready';
        renderComparison(null);
        const ready = {{
          title: title.textContent,
          body: body.children.map((child) => child.textContent),
        }};
        renderComparison({{
          comparable: false,
          mismatch_reasons: ['服务端精确原因 A', '服务端精确原因 B'],
          notes: ['服务端说明'],
        }});
        const incompatible = {{
          title: title.textContent,
          lists: body.children.map((child) => child.values),
        }};
        console.log(JSON.stringify({{ incomplete, ready, incompatible }}));
        """
    )

    assert payload["incomplete"] == {
        "title": "元数据不完整",
        "body": ["历史记录仍可查看，但不会用于训练提升比较。"],
    }
    assert payload["ready"] == {
        "title": "未选择基线",
        "body": ["当前批次可查看，但暂无可直接比较的基线。"],
    }
    assert payload["incompatible"] == {
        "title": "当前与基线不可直接比较",
        "lists": [
            ["服务端精确原因 A", "服务端精确原因 B"],
            ["服务端说明"],
        ],
    }


def test_root_route_selects_first_cohort_in_tree_order():
    """#/ (default) auto-selects the first batch in the tree -- server
    order, not any client-side re-derivation."""
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    helper = _javascript_section(script, "function firstCohortIdInTree", "function showBatchView")

    assert "for (const version of tree)" in helper
    assert ".sort(" not in helper

    payload = _run_node_json(
        f"""
        {helper}
        const withCohorts = [
          {{ characters: [{{ cohorts: [] }}, {{ cohorts: [{{ cohort_id: 'first' }}, {{ cohort_id: 'second' }}] }}] }},
          {{ characters: [{{ cohorts: [{{ cohort_id: 'third' }}] }}] }},
        ];
        const empty = [{{ characters: [{{ cohorts: [] }}] }}];
        console.log(JSON.stringify({{
          first: firstCohortIdInTree(withCohorts),
          none: firstCohortIdInTree(empty),
          malformed: firstCohortIdInTree(null),
        }}));
        """
    )

    assert payload == {"first": "first", "none": "", "malformed": ""}


def test_funnel_is_an_accessible_inline_svg_with_explicit_denominators():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    funnel = _javascript_section(script, "function renderFunnel", "function deltaText")

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


def test_trend_rendering_is_bounded_timestamped_and_explains_sampling():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
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


def test_new_static_assets_are_served_and_allowlisted(tmp_path: Path):
    viewer_source = Path(viewer.__file__).read_text(encoding="utf-8")
    new_files = ["util.js", "tree.js", "cohort-view.js", "runs-table.js", "run-view.js"]
    for name in new_files:
        assert (
            f'"/static/{name}": ("{name}", "text/javascript; charset=utf-8")'
            in viewer_source
        )
        assert f"/static/{name}" not in viewer.QUERY_ALLOWED_ASSETS

    with _server(tmp_path) as base:
        for name in new_files:
            status, content_type, body = _get(base, f"/static/{name}")
            assert status == 200, name
            assert content_type == "text/javascript; charset=utf-8"
            assert body == (STATIC_DIR / name).read_bytes()


def test_runs_table_rows_without_map_capability_stay_clickable():
    """has_map: false rows must still be clickable -- they route to the
    run view same as any other row, just without a map to show there."""
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    status_labels = _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")
    clear_fn = _javascript_section(util_script, "function clear(node)", "function setStatus")
    render_empty_fn = _javascript_section(util_script, "function renderEmpty", "function setSelectOptions")
    table_section = _javascript_section(script, "function missingCell", "async function render")

    payload = _run_node_json(
        f"""
        {_FAKE_TABLE_DOM}
        {status_labels}
        {element_fn}
        {clear_fn}
        {render_empty_fn}
        function formatTime(value) {{ return `T${{value}}`; }}
        let sortKey = null;
        let sortDir = 'desc';
        {table_section}
        const state = {{ selectedCohortId: 'c' }};
        const navigateCalls = [];
        function navigate(hash) {{ navigateCalls.push(hash); }}
        function runRoute(cohortId, ref) {{ return `#/batch/${{cohortId}}/run/${{ref.kind}}:${{ref.id}}`; }}
        const rows = [
          {{
            seed: 's', status: 'dead', global_floor: 5, act: 1, started_at: 1,
            has_map: false, ref: {{ kind: 'source', id: 'no-map-1' }},
          }},
        ];
        renderTable(rows);
        const row = tableContainer.querySelectorAll('tr').slice(1)[0];
        row.dispatchClick();
        console.log(JSON.stringify({{
          navigateCalls,
          classHasNoMap: row.className.includes('runs-table-row-no-map'),
        }}));
        """
    )

    assert payload["navigateCalls"] == ["#/batch/c/run/source:no-map-1"]
    assert payload["classHasNoMap"] is True


def test_open_run_navigates_to_the_run_view_route_for_the_selected_cohort():
    """openRun (used by trend points and the source-detail drawer's 查看地图
    button) is now a thin router shim: it never fetches or touches
    window.STS2Map itself -- run-view.js does that once the route lands."""
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function runHasMapCapability" in script
    canonical = _javascript_section(
        script, "function renderCanonicalRun", "function renderDetail"
    )
    open_run = _javascript_section(
        script, "function openRun(runId", "async function uploadSelectedFile"
    )
    assert "if (mapAvailable" in canonical
    assert "openRun(run.run_id, event.currentTarget)" in canonical
    assert "window.STS2Map" not in open_run
    assert "getJSON" not in open_run

    payload = _run_node_json(
        f"""
        const calls = [];
        const state = {{ selectedCohortId: 'cohort-1', detailOpener: null }};
        function navigate(hash) {{ calls.push({{ type: 'navigate', hash }}); }}
        function runRoute(cohortId, ref) {{ return `#/batch/${{cohortId}}/run/${{ref.kind}}:${{ref.id}}`; }}
        function setStatus(message, kind) {{ calls.push({{ type: 'status', message, kind }}); }}
        {open_run}

        const opener = {{ isConnected: true, id: 'trend-point' }};
        openRun('  run-42  ', opener);
        openRun('');
        console.log(JSON.stringify({{ calls, opener: state.detailOpener && state.detailOpener.id }}));
        """
    )

    assert payload["calls"][0] == {"type": "navigate", "hash": "#/batch/cohort-1/run/run:run-42"}
    assert payload["opener"] == "trend-point"
    assert {"type": "status", "message": "无法打开对局：缺少对局 ID", "kind": "error"} in payload["calls"]


def test_runs_table_sort_button_reflects_active_column_via_aria_sort():
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    status_labels = _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")
    clear_fn = _javascript_section(util_script, "function clear(node)", "function setStatus")
    render_empty_fn = _javascript_section(util_script, "function renderEmpty", "function setSelectOptions")
    table_section = _javascript_section(script, "function missingCell", "async function render")

    payload = _run_node_json(
        f"""
        {_FAKE_TABLE_DOM}
        {status_labels}
        {element_fn}
        {clear_fn}
        {render_empty_fn}
        function formatTime(value) {{ return `T${{value}}`; }}
        let sortKey = null;
        let sortDir = 'desc';
        {table_section}
        const rows = [
          {{ seed: 'a', status: 'win', global_floor: 5, act: 1, started_at: 1, ref: {{ kind: 'run', id: 'r1' }} }},
        ];

        renderTable(rows);
        const buttonsBefore = tableContainer.querySelectorAll('button').map((b) => b.getAttribute('aria-sort'));

        sortKey = 'global_floor';
        sortDir = 'asc';
        renderTable(rows);
        const floorButton = tableContainer.querySelectorAll('button')[1];

        console.log(JSON.stringify({{
          buttonsBefore,
          floorAriaSort: floorButton.getAttribute('aria-sort'),
        }}));
        """
    )

    assert payload["buttonsBefore"] == ["none", "none"]
    assert payload["floorAriaSort"] == "ascending"


def test_detail_requests_are_latest_only_and_drawer_restores_focus():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # refreshMetrics moved to cohort-view.js; the drawer block in app.js now
    # ends at the upload handler.
    requests = _javascript_section(
        script, "function beginDetailRequest", "async function uploadSelectedFile"
    )

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
    assert "matches(':disabled')" in script
    assert "event.shiftKey" in script
    assert "event.preventDefault()" in script
    assert "focusables[0]" in script
    assert "focusables[focusables.length - 1]" in script
    assert "handleDetailKeydown" in script


def test_metrics_refresh_captures_focus_before_busy_and_restores_after_enable():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cohort_script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    assert "function isFocusable" in script
    assert "function restoreMetricsFocus" in cohort_script
    refresh = _javascript_section(
        cohort_script, "async function refreshMetrics", "let lastRenderedCohortId"
    )
    focus_helper = _javascript_section(
        script, "function isFocusable", "function closeDetail"
    )

    assert "const focusOpener = document.activeElement" in refresh
    assert refresh.index("const focusOpener = document.activeElement") < refresh.index(
        "setBusy(true)"
    )
    assert "finally" in refresh
    assert refresh.index("setBusy(false)") < refresh.index("restoreMetricsFocus(")
    # There is no longer a currentCohort select to compare against; the
    # selected cohort id is the single source of truth for "is this still the
    # request the user is looking at", and it guards both the focus
    # restoration and every render inside refreshMetrics.
    assert "state.selectedCohortId !== cohortId" in cohort_script
    assert "candidate.isConnected" in focus_helper
    assert "typeof candidate.focus" in focus_helper and "'function'" in focus_helper
    assert "closest('[hidden], [inert], [aria-hidden=\"true\"]')" in focus_helper
    assert "matches(':disabled')" in focus_helper
    assert "window.getComputedStyle(candidate)" in focus_helper


def test_metrics_refresh_focus_restoration_is_safe_latest_context_only():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cohort_script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    assert "function isFocusable" in script
    assert "function restoreMetricsFocus" in cohort_script
    focus_helper = _javascript_section(
        script, "function isFocusable", "function closeDetail"
    )
    restore_helper = _javascript_section(
        cohort_script, "function restoreMetricsFocus", "async function refreshMetrics"
    )
    refresh = _javascript_section(
        cohort_script, "async function refreshMetrics", "let lastRenderedCohortId"
    )

    payload = _run_node_json(
        f"""
        (async () => {{
          const document = {{ body: null, documentElement: null, activeElement: null }};
          const window = {{
            getComputedStyle(candidate) {{
              return {{
                display: candidate.display || 'block',
                visibility: candidate.visibility || 'visible',
              }};
            }},
          }};
          function makeElement(id) {{
            return {{
              id,
              value: '',
              isConnected: true,
              disabled: false,
              blockedAncestor: false,
              focusCalls: 0,
              focus() {{ this.focusCalls += 1; document.activeElement = this; }},
              closest() {{ return this.blockedAncestor ? {{}} : null; }},
              matches(selector) {{ return selector === ':disabled' && this.disabled; }},
              hasAttribute(name) {{ return name === 'disabled' && this.disabled; }},
            }};
          }}
          document.body = makeElement('body');
          document.documentElement = makeElement('html');
          // The current-cohort dropdown is gone: the left tree selects the
          // batch and state.selectedCohortId is the identity. reloadButton
          // stands in as the second busy-toggled control.
          const reloadButton = makeElement('reloadButton');
          const baselineSelect = makeElement('baselineCohort');
          const elsewhere = makeElement('elsewhere');
          const nodes = {{
            reloadButton,
            baselineCohort: baselineSelect,
          }};
          const byId = (id) => nodes[id];
          const controls = [reloadButton, baselineSelect];
          function setBusy(isBusy) {{
            controls.forEach((control) => {{ control.disabled = isBusy; }});
            if (isBusy && controls.includes(document.activeElement)) {{
              document.activeElement = document.body;
            }}
          }}
          const state = {{ cohorts: [{{}}], currentMetrics: null, selectedCohortId: null }};
          let status = '';
          const setStatus = (message) => {{ status = message; }};
          const resetMetrics = () => {{}};
          const renderSummary = () => {{}};
          const renderTrend = () => {{}};
          const renderFunnel = () => {{}};
          const renderComparison = () => {{}};
          const renderAnomalies = () => {{}};
          const renderRepresentatives = () => {{}};
          const metrics = {{ current: {{}}, comparison: null }};
          let getJSONImpl = async () => metrics;
          const getJSON = (...args) => getJSONImpl(...args);
          {focus_helper}
          {restore_helper}
          {refresh}

          state.selectedCohortId = 'current-a';
          baselineSelect.value = 'baseline-a';
          document.activeElement = reloadButton;
          await refreshMetrics('current-a');
          const currentRestored = document.activeElement === reloadButton;

          document.activeElement = baselineSelect;
          await refreshMetrics('current-a');
          const baselineRestored = document.activeElement === baselineSelect;

          const currentFocusBeforeElsewhere = reloadButton.focusCalls;
          const baselineFocusBeforeElsewhere = baselineSelect.focusCalls;
          document.activeElement = elsewhere;
          await refreshMetrics('current-a');
          const elsewherePreserved = document.activeElement === elsewhere
            && reloadButton.focusCalls === currentFocusBeforeElsewhere
            && baselineSelect.focusCalls === baselineFocusBeforeElsewhere;

          document.activeElement = baselineSelect;
          getJSONImpl = async () => {{ throw new Error('metrics failed'); }};
          await refreshMetrics('current-a');
          const errorRestored = document.activeElement === baselineSelect
            && status === '训练指标读取失败：metrics failed';

          // The user picks a different batch in the tree while the request is
          // in flight: the stale response must neither render nor steal focus.
          let resolveStale;
          getJSONImpl = () => new Promise((resolve) => {{ resolveStale = resolve; }});
          document.activeElement = reloadButton;
          const staleRequest = refreshMetrics('current-a');
          state.selectedCohortId = 'current-b';
          resolveStale(metrics);
          await staleRequest;
          const staleSelectionDidNotRestore = document.activeElement === document.body;

          state.selectedCohortId = 'current-b';
          let resolveMoved;
          getJSONImpl = () => new Promise((resolve) => {{ resolveMoved = resolve; }});
          document.activeElement = reloadButton;
          const movedRequest = refreshMetrics('current-b');
          document.activeElement = elsewhere;
          resolveMoved(metrics);
          await movedRequest;
          const movedFocusDidNotRestore = document.activeElement === elsewhere;

          const valid = makeElement('valid');
          const disconnected = makeElement('disconnected');
          disconnected.isConnected = false;
          const hidden = makeElement('hidden');
          hidden.blockedAncestor = true;
          const disabled = makeElement('disabled');
          disabled.disabled = true;
          const displayNone = makeElement('displayNone');
          displayNone.display = 'none';
          console.log(JSON.stringify({{
            currentRestored,
            baselineRestored,
            elsewherePreserved,
            errorRestored,
            staleSelectionDidNotRestore,
            movedFocusDidNotRestore,
            focusable: {{
              valid: isFocusable(valid),
              disconnected: isFocusable(disconnected),
              hidden: isFocusable(hidden),
              disabled: isFocusable(disabled),
              displayNone: isFocusable(displayNone),
            }},
          }}));
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )

    assert payload == {
        "currentRestored": True,
        "baselineRestored": True,
        "elsewherePreserved": True,
        "errorRestored": True,
        "staleSelectionDidNotRestore": True,
        "movedFocusDidNotRestore": True,
        "focusable": {
            "valid": True,
            "disconnected": False,
            "hidden": False,
            "disabled": False,
            "displayNone": False,
        },
    }


def test_upload_size_guard_precedes_read_and_has_server_margin():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    viewer_source = Path(viewer.__file__).read_text(encoding="utf-8")
    upload = _javascript_section(script, "async function uploadSelectedFile", "async function bootstrap")

    # A full-run replay is the artifact this control exists to open, and one
    # real Act 3 clear measures ~13.4 MiB — so a 1 MiB cap rejected exactly the
    # runs worth inspecting. Raised to 32 MiB (~2.4x the largest real replay).
    assert viewer.PARSE_BODY_MAX_BYTES == 128 * 1024 * 1024
    assert "length > PARSE_BODY_MAX_BYTES" in viewer_source
    assert "const SERVER_PARSE_BODY_MAX_BYTES = 128 * 1024 * 1024" in script
    assert "const FILE_UPLOAD_MAX_BYTES = 32 * 1024 * 1024" in script
    # Envelope margin. The old bound assumed 6x JSON expansion, which came from
    # Python's json.dumps(ensure_ascii=True) escaping non-ASCII as \uXXXX. The
    # browser does NOT do that -- JSON.stringify emits UTF-8 raw and escapes
    # only quote/backslash/control chars. Measured on two real replays the POST
    # body is 1.125x the file. 3x is kept as a pathological-input guard.
    assert 32 * 1024 * 1024 * 3 + 64 * 1024 < viewer.PARSE_BODY_MAX_BYTES
    guard_index = upload.index("file.size > FILE_UPLOAD_MAX_BYTES")
    read_index = upload.index("await file.text()")
    stringify_index = upload.index("JSON.stringify")
    fetch_index = upload.index("getJSON('/api/parse'")
    assert guard_index < read_index < fetch_index < stringify_index
    assert "超过本地载入上限" in upload
    assert "未读取文件内容" in upload


def test_catalog_anomalies_are_grouped_and_bounded():
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    anomalies = _javascript_section(
        script, "function renderAnomalies", "function restoreMetricsFocus"
    )

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
    # The injection and re-parsing bans apply to every view module, not just
    # app.js -- splitting the bundle must not open a hole in one of the parts.
    modules = {
        name: (STATIC_DIR / name).read_text(encoding="utf-8")
        for name in (
            "util.js", "app.js", "tree.js",
            "cohort-view.js", "runs-table.js", "run-view.js",
        )
    }
    script = "".join(modules.values())
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
    assert "source_kind" in modules["app.js"]
    # HTTP responses only; source text is posted untouched.
    assert "JSON.parse" in modules["util.js"]
    assert "file.text()" in modules["app.js"]


def test_baseline_menu_offers_only_server_comparable_batches():
    """The baseline picker is populated from the server's comparison
    signature, never from a client-side axis comparison. A batch the server
    has not declared comparable must not be offerable as a baseline, because
    the resulting delta would be meaningless."""
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    candidates = _javascript_section(
        script, "function baselineCandidates", "function updateBaselineHelp"
    )

    # Comparability is read from the server descriptor only.
    assert "comparison_signature" in candidates
    for axis in ("evaluation_mode", "ascension", "scenario", "game_version"):
        assert axis not in candidates, f"client-side axis logic leaked in: {axis}"

    payload = _run_node_json(
        f"""
        function safeCohortId(cohort) {{
          return cohort && typeof cohort.cohort_id === 'string' ? cohort.cohort_id.trim() : '';
        }}
        const ready = (signature) => ({{ comparison_signature: signature }});
        const state = {{ cohorts: [
          {{ cohort_id: 'cur', comparison_readiness: ready('sig-a') }},
          {{ cohort_id: 'same', comparison_readiness: ready('sig-a') }},
          {{ cohort_id: 'other', comparison_readiness: ready('sig-b') }},
          {{ cohort_id: 'unready', comparison_readiness: ready(null) }},
          {{ cohort_id: 'malformed', comparison_readiness: null }},
        ] }};
        {candidates}

        const current = state.cohorts[0];
        const comparable = baselineCandidates(current).map(safeCohortId);
        // A batch the server could not sign is comparable to nothing at all,
        // including other unsigned batches.
        const fromUnready = baselineCandidates(state.cohorts[3]).map(safeCohortId);
        const fromMalformed = baselineCandidates(state.cohorts[4]).map(safeCohortId);
        const fromGarbage = baselineCandidates(null).map(safeCohortId);
        console.log(JSON.stringify({{ comparable, fromUnready, fromMalformed, fromGarbage }}));
        """
    )

    assert payload["comparable"] == ["same"], "only a matching signature is offerable"
    assert payload["fromUnready"] == []
    assert payload["fromMalformed"] == []
    assert payload["fromGarbage"] == []


def test_baseline_help_explains_why_no_baseline_is_offerable():
    """With an empty picker the user must still learn why -- an empty menu
    with no explanation is how the old dashboard hid incomplete metadata."""
    script = (STATIC_DIR / "cohort-view.js").read_text(encoding="utf-8")
    help_section = _javascript_section(
        script, "function updateBaselineHelp", "function renderBaselineSelect"
    )

    assert "元数据不完整，仅展示本批次" in help_section
    assert "当前批次可查看，但暂无可直接比较的基线" in help_section
    assert "missing_axes" in help_section
    assert "mixed_axes" in help_section
    assert "invalid_axes" in help_section


def test_view_modules_never_shadow_a_shared_util_helper():
    """util.js's helpers are plain globals, so a same-named function declared
    inside a view module's IIFE silently shadows the shared one for that whole
    module.

    This is not hypothetical: cohort-view.js once declared a zero-argument
    renderEmpty() for "no batch selected", which captured every
    renderEmpty(container, message) call meant for util.js. Because the real
    logs carry no timestamps, the trend chart always took its empty-state
    branch, so selecting any batch wiped the batch view a moment after it
    rendered -- and every static assertion still passed.
    """
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    shared = set(re.findall(r"^function (\w+)", util_script, re.MULTILINE))
    shared |= set(re.findall(r"^const ([A-Z][A-Z0-9_]*)\s*=", util_script, re.MULTILINE))
    assert "renderEmpty" in shared, "guard is only meaningful if util.js still exports it"

    offenders = []
    for name in ("app.js", "tree.js", "cohort-view.js", "runs-table.js", "run-view.js"):
        script = (STATIC_DIR / name).read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s+function (\w+)", script, re.MULTILINE))
        declared |= set(re.findall(r"^\s+(?:const|let|var) (\w+)\s*=", script, re.MULTILINE))
        for clash in sorted(declared & shared):
            offenders.append(f"{name} shadows util.js helper '{clash}'")

    assert offenders == [], "\n".join(offenders)


def test_leaving_a_run_route_closes_the_map_page_through_the_map_namespace():
    """showMapPage() hides #workbenchBody wholesale, so the router must call
    the matching close when it leaves a run route.

    showDashboardPage is declared inside map.js's IIFE and is therefore NOT a
    global. A bare `typeof showDashboardPage === 'function'` guard in app.js
    is always false, which left the map page covering the batch view and the
    tree collapsed after pressing 返回.
    """
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    map_script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")

    # Reachable: exported on the namespace rather than assumed global.
    assert "showDashboardPage," in map_script or "showDashboardPage:" in map_script
    assert "window.STS2Map.showDashboardPage" in app_script
    assert "typeof showDashboardPage === 'function'" not in app_script

    # Called from the router, before it dispatches to any view.
    route_fn = _javascript_section(
        app_script, "async function applyRoute", "window.addEventListener('popstate'"
    )
    assert "showDashboardPage()" in route_fn
    assert route_fn.index("showDashboardPage()") < route_fn.index("route.view === 'root'")


def test_bootstrap_waits_for_every_deferred_view_module():
    """app.js is deferred and evaluated BEFORE tree.js / cohort-view.js /
    runs-table.js / run-view.js / map.js, so calling bootstrap() at the bottom
    of app.js races those namespaces into existence and only wins because it
    awaits the network first. DOMContentLoaded fires after every deferred
    script has run."""
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "document.addEventListener('DOMContentLoaded', bootstrap)" in app_script
    assert not re.search(r"^bootstrap\(\);", app_script, re.MULTILINE), (
        "bootstrap must not be invoked at app.js top level"
    )

    # The race only exists because app.js is ordered before the modules it
    # calls into; assert that ordering so this test keeps its meaning.
    order = re.findall(r'<script src="/static/([\w.-]+)"', html)
    assert order.index("app.js") < order.index("tree.js")
    assert order.index("app.js") < order.index("map.js")
    for module in ("util.js", "tree.js", "cohort-view.js", "runs-table.js", "run-view.js", "map.js"):
        assert module in order, module


def test_skip_link_jumps_without_hijacking_the_hash_router():
    """The skip link's href is a fragment, but this app's router owns
    location.hash. Letting the anchor navigate writes '#contentPane', which
    parseRoute cannot match, so it degrades to the root route and bounces the
    user to the first batch instead of jumping to the content they were
    already reading."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert 'class="skip-link" href="#contentPane"' in html
    handler = _javascript_section(
        app_script, "const skipLink", "document.addEventListener('DOMContentLoaded'"
    )
    assert "preventDefault()" in handler
    assert "byId('contentPane')" in handler and ".focus()" in handler

    # Router-driven focus must not paint a ring; the skip link's own jump must.
    assert ".content-pane:focus" in css
    assert ".content-pane.skip-focus:focus" in css
    assert "skip-focus" in handler


def test_combat_replay_renders_card_names_targets_and_effects():
    """The node panel's fight view must read as play, not as raw protocol.

    The recorded action carries `label` ("play_card card_index=3"), which is
    unreadable on its own, plus `card`, `target` and `effects` that make it
    legible. Boss names arrive with unresolved template vars
    ("Test Subject #C{Count}") and must be stripped, not shown raw.
    """
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    section = _javascript_section(script, "function stripTemplate", "function roundSummaryText")
    result = _run_node_json(
        f"""
        {section}
        const played = {{
          label: 'play_card card_index=3 target_index=0',
          card: {{ name: 'Maul', cost: 1 }},
          target: {{ name: 'Test Subject #C{{Count}}' }},
          effects: {{ enemy_hp: [{{ name: 'Test Subject #C{{Count}}', delta: -22 }}] }},
        }};
        const ended = {{
          label: 'end_turn',
          effects: {{
            hp: {{ delta: -15 }}, block: {{ delta: 2 }},
            enemy_hp: [{{ name: 'Test Subject #C{{Count}}', delta: -19 }}],
          }},
        }};
        const untargeted = {{ label: 'play_card card_index=1', card: {{ name: 'Defragment', cost: 1 }} }};
        const unknown = {{ label: 'select_cards indices=2' }};
        const zeroDelta = {{ label: 'end_turn', effects: {{ hp: {{ delta: 0 }} }} }};
        console.log(JSON.stringify({{
          played: actionText(played),
          ended: actionText(ended),
          untargeted: actionText(untargeted),
          unknown: actionText(unknown),
          zeroDelta: actionText(zeroDelta),
          stripped: stripTemplate('Test Subject #C{{Count}}'),
        }}));
        """
    )
    assert result["played"] == "打出 Maul(1) → Test Subject  ⇒ Test Subject -22"
    assert result["ended"] == "结束回合  ⇒ Test Subject -19，自身生命 -15，格挡 +2"
    # No target recorded -> no arrow, and no effects -> no trailing clause.
    assert result["untargeted"] == "打出 Defragment(1)"
    # Unrecognised verbs keep the exact recorded label rather than inventing one.
    assert result["unknown"] == "select_cards indices=2"
    # A zero delta is not an effect; it must not render an empty "⇒".
    assert result["zeroDelta"] == "结束回合"
    assert result["stripped"] == "Test Subject"
