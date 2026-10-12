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
    """The tree must reproduce /api/tree's order verbatim -- version groups
    by their newest batch, the null game_version bucket wherever the server
    put it -- and must never re-sort client-side (the old numeric-version-descending sort is
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
        // than re-deriving one client-side. The null-version bucket sits in
        // the middle: it is an ordinary group, not pinned to either end.
        const tree = [
          {{ game_version: '0.50.0', characters: [
            {{ character: 'Ironclad', cohorts: [cohortA] }},
          ] }},
          {{ game_version: null, characters: [
            {{ character: 'Silent', cohorts: [cohortC] }},
          ] }},
          {{ game_version: '0.111.0', characters: [
            {{ character: null, cohorts: [cohortB] }},
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
    order = ["0.50.0", "Ironclad", "Batch A", "Silent", "Batch C", "0.111.0", "Batch B"]
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
            "cohort-view.js", "runs-table.js", "run-view.js", "batches-view.js",
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
    for name in ("app.js", "tree.js", "cohort-view.js", "runs-table.js", "run-view.js", "batches-view.js"):
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
    for module in ("util.js", "tree.js", "cohort-view.js", "runs-table.js", "run-view.js",
                   "batches-view.js", "map.js"):
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


# ---------------------------------------------------------------------------
# Games that are still being played
# ---------------------------------------------------------------------------


def test_tree_leaf_counts_the_games_still_being_played_after_the_usual_label():
    """`20 局 · 平均 45.9` stays exactly as it was; a batch with games under
    way gets ` · N 进行中` appended, and a batch with nothing finished yet still
    renders (no average, never 0)."""
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    tree_script = (STATIC_DIR / "tree.js").read_text(encoding="utf-8")
    element_fn = _javascript_section(util_script, "function element(tag", "function svgElement")
    tree_source = tree_script.replace("window.Tree = (() => {", "const Tree = (() => {", 1)

    payload = _run_node_json(
        f"""
        {_FAKE_TREE_DOM}
        {element_fn}
        {tree_source}

        const base = {{ technical_count: 0, latest_at: 1, unarchived: false }};
        Tree.render([{{ game_version: null, characters: [{{ character: 'Ironclad', cohorts: [
          {{ ...base, cohort_id: 'a', label: 'finished only', run_count: 20, avg_global_floor: 45.9, in_progress_count: 0 }},
          {{ ...base, cohort_id: 'b', label: 'mixed', run_count: 20, avg_global_floor: 45.9, in_progress_count: 2 }},
          {{ ...base, cohort_id: 'c', label: 'only live', run_count: 0, avg_global_floor: null, in_progress_count: 3 }},
          {{ ...base, cohort_id: 'd', label: 'old server', run_count: 4, avg_global_floor: 10 }},
        ] }}] }}]);
        const leaves = treeNode.querySelectorAll('[role="treeitem"]')
          .filter((item) => item._attrs['data-cohort-id'] !== undefined);
        console.log(JSON.stringify(leaves.map((leaf) => leaf.children[1].textContent)));
        """
    )

    assert payload == [
        "20 局 · 平均 45.9",
        "20 局 · 平均 45.9 · 2 进行中",
        "0 局 · 平均 — · 3 进行中",
        "4 局 · 平均 10",
    ]


def _table_harness() -> tuple[str, str]:
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    setup = "\n".join(
        [
            _FAKE_TABLE_DOM,
            _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS"),
            _javascript_section(util_script, "function element(tag", "function svgElement"),
            _javascript_section(util_script, "function clear(node)", "function setStatus"),
            _javascript_section(util_script, "function renderEmpty", "function setSelectOptions"),
            "function formatTime(value) { return `T${value}`; }",
            "let sortKey = null; let sortDir = 'desc';",
            _javascript_section(script, "const TOP_RUNS_RECENT_WINDOW", "function missingCell"),
            _javascript_section(script, "function missingCell", "async function render"),
            """
            const state = { selectedCohortId: 'c' };
            const navigateCalls = [];
            function navigate(hash) { navigateCalls.push(hash); }
            function runRoute(cohortId, ref) { return `#/batch/${cohortId}/run/${ref.kind}:${ref.id}`; }
            function collect(node, test, out = []) {
              if (test(node)) out.push(node);
              node.children.forEach((child) => collect(child, test, out));
              return out;
            }
            """,
        ]
    )
    return setup, script


_LIVE_ROW = """{
  seed: 'live', status: 'in_progress', global_floor: 22, act: 2, floor: 5, hp: 41, max_hp: 70,
  started_at: 900, has_map: true, ref: { kind: 'source', id: 'src-live' },
}"""


def test_runs_table_marks_games_in_progress_with_a_badge_and_where_they_are():
    setup, _ = _table_harness()
    payload = _run_node_json(
        f"""
        {setup}
        renderTable([
          {_LIVE_ROW},
          {{ seed: 'done', status: 'dead', global_floor: 30, act: 2, started_at: 100,
             has_map: true, ref: {{ kind: 'source', id: 'src-done' }} }},
        ]);
        const rows = tableContainer.querySelectorAll('tr').slice(1);
        const badge = collect(rows[0], (n) => n.className.includes('status-badge-in-progress'));
        const note = collect(rows[0], (n) => n.className.includes('runs-table-live-note'));
        const doneBadges = collect(rows[1], (n) => n.className.includes('status-badge'));
        rows[0].dispatchClick();
        console.log(JSON.stringify({{
          badgeText: badge.map((n) => n.textContent),
          noteText: note.map((n) => n.textContent),
          liveRowClass: rows[0].className,
          liveLabel: rows[0].getAttribute('aria-label'),
          doneStatusCell: rows[1].children[2].textContent,
          doneBadges: doneBadges.length,
          doneRowClass: rows[1].className,
          floorCell: rows[0].children[3].textContent,
          navigateCalls,
        }}));
        """
    )

    assert payload["badgeText"] == ["进行中"]
    assert payload["noteText"] == ["A2F5 · HP 41/70"]
    assert "runs-table-row-in-progress" in payload["liveRowClass"]
    assert payload["liveLabel"] == "查看第 1 行进行中的对局"
    # A finished row is exactly what it was: plain status text, no badge.
    assert payload["doneStatusCell"] == "正常结束"
    assert payload["doneBadges"] == 0
    assert "in-progress" not in payload["doneRowClass"]
    assert payload["floorCell"] == "22"
    # ...and opening a live game goes through the same ref route.
    assert payload["navigateCalls"] == ["#/batch/c/run/source:src-live"]


def test_runs_table_keeps_games_in_progress_on_top_and_out_of_the_highlights():
    setup, _ = _table_harness()
    payload = _run_node_json(
        f"""
        {setup}
        const rows = [
          {_LIVE_ROW},
          {{ seed: 'a', status: 'dead', global_floor: 10, started_at: 100 }},
          {{ seed: 'b', status: 'win', global_floor: 40, started_at: 200 }},
          {{ seed: 'c', status: 'dead', global_floor: 25, started_at: 150 }},
        ];
        const order = (key, dir) => {{
          sortKey = key; sortDir = dir;
          return sortedRows(rows).map((row) => row.seed);
        }};
        sortKey = null;
        const unsorted = sortedRows(rows).map((row) => row.seed);
        const result = {{
          unsorted,
          floorDesc: order('global_floor', 'desc'),
          floorAsc: order('global_floor', 'asc'),
          statusDesc: order('status', 'desc'),
          // live has the newest start and a floor: it still must not rank
          top: selectTopRuns(rows).map((row) => row.seed),
        }};
        console.log(JSON.stringify(result));
        """
    )

    assert payload["unsorted"] == ["live", "a", "b", "c"]
    assert payload["floorDesc"] == ["live", "b", "c", "a"]
    assert payload["floorAsc"] == ["live", "a", "c", "b"]
    assert payload["statusDesc"][0] == "live"
    assert payload["top"] == ["b", "c", "a"]


def _run_view_harness() -> str:
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    view_script = (STATIC_DIR / "run-view.js").read_text(encoding="utf-8")
    return "\n".join(
        [
            """
            function makeNode(tag) {
              return {
                tag, className: '', textContent: '', hidden: false, dataset: {}, children: [],
                setAttribute() {}, removeAttribute() {},
                append(...items) { this.children.push(...items); },
                replaceChildren(...items) { this.children = items; this.textContent = ''; },
              };
            }
            const document = { createElement: (tag) => makeNode(tag) };
            const nodes = {};
            function byId(id) { return nodes[id] || (nodes[id] = makeNode('x')); }
            const statuses = [];
            function setStatus(message) { statuses.push(message); }
            const state = { cohorts: [], currentRunReplay: null };
            """,
            _javascript_section(util_script, "const STATUS_LABELS", "const CAPABILITY_LABELS"),
            _javascript_section(util_script, "function element(tag", "function svgElement"),
            _javascript_section(util_script, "function clear(node)", "function setStatus"),
            _javascript_section(app_script, "function parseRoute", "function navigate"),
            _javascript_section(app_script, "function runHasMapCapability", "function renderCanonicalRun"),
            """
            // Timers the test fires by hand, in order.
            const timers = [];
            let nextTimer = 1;
            function setTimeout(fn, ms) {
              const timer = { id: nextTimer++, fn, ms, cleared: false };
              timers.push(timer);
              return timer.id;
            }
            function clearTimeout(id) {
              const timer = timers.find((candidate) => candidate.id === id);
              if (timer) timer.cleared = true;
            }
            const pending = () => timers.filter((timer) => !timer.cleared && !timer.fired);
            async function fire(timer) { timer.fired = true; await timer.fn(); }

            const mapCalls = [];
            const window = {
              STS2Map: {
                openRun(ref) { mapCalls.push('open'); },
                refreshRun(ref) { mapCalls.push('refresh'); },
                showMapPage() { mapCalls.push('no-map'); },
              },
            };
            const location = { hash: '' };
            const fetches = [];
            let responses = [];
            async function getJSON(url) {
              fetches.push(url);
              const next = responses.shift();
              if (next instanceof Error) throw next;
              return next;
            }
            const livePayload = (extra = {}) => ({
              view: 'run', live: true,
              run: { outcome: { status: 'in_progress', max_global_floor: 5 },
                     metadata: { seed: 's' }, capabilities: { visited_route: true } },
              ...extra,
            });
            const endedPayload = {
              view: 'run',
              run: { outcome: { status: 'dead', max_global_floor: 9 },
                     metadata: { seed: 's' }, capabilities: { visited_route: true } },
            };
            const ref = { kind: 'source', id: 'src1' };
            const notice = () => byId('runLiveNotice');
            const reset = () => {
              fetches.length = 0; mapCalls.length = 0; statuses.length = 0;
              location.hash = '#/batch/c/run/source:src1';
            };
            """,
            view_script,
        ]
    )


def test_run_view_refreshes_a_live_run_every_30_seconds_until_it_ends():
    payload = _run_node_json(
        f"""
        {_run_view_harness()}
        (async () => {{
          const out = {{}};
          reset();
          responses = [livePayload(), livePayload(), endedPayload];
          await window.RunView.render('c', ref);
          out.afterOpen = {{
            notice: notice().textContent, hidden: notice().hidden,
            timers: pending().map((timer) => timer.ms), fetches: fetches.length,
            mapCalls: [...mapCalls], status: statuses.at(-1),
          }};
          await fire(pending()[0]);
          out.afterTick1 = {{
            notice: notice().textContent, timers: pending().map((timer) => timer.ms),
            fetches: [...fetches], mapCalls: [...mapCalls],
          }};
          await fire(pending()[0]);
          out.afterEnd = {{
            notice: notice().textContent, hidden: notice().hidden,
            timers: pending().length, fetches: fetches.length, mapCalls: [...mapCalls],
          }};
          console.log(JSON.stringify(out));
        }})();
        """
    )

    assert payload["afterOpen"] == {
        "notice": "进行中 · 每 30 秒自动刷新",
        "hidden": False,
        "timers": [30000],
        "fetches": 1,
        "mapCalls": ["open"],
        "status": "已载入对局",
    }
    # A tick re-reads the run (same address) and the map, then schedules the next.
    assert payload["afterTick1"]["fetches"] == ["/api/run?source=src1", "/api/run?source=src1"]
    assert payload["afterTick1"]["mapCalls"] == ["open", "refresh"]
    assert payload["afterTick1"]["timers"] == [30000]
    assert payload["afterTick1"]["notice"] == "进行中 · 每 30 秒自动刷新"
    # Once the run has an end it is shown, polling stops, and the page says why.
    assert payload["afterEnd"]["timers"] == 0
    assert payload["afterEnd"]["fetches"] == 3
    assert payload["afterEnd"]["mapCalls"] == ["open", "refresh", "refresh"]
    assert payload["afterEnd"]["notice"] == "对局已结束，已停止自动刷新"


def test_run_view_stops_refreshing_when_the_user_leaves_the_run():
    payload = _run_node_json(
        f"""
        {_run_view_harness()}
        (async () => {{
          const out = {{}};

          // Navigating to another view: the router calls stop().
          reset();
          responses = [livePayload()];
          await window.RunView.render('c', ref);
          const armed = pending()[0];
          window.RunView.stop();
          await armed.fn();   // even a timer that slipped through does nothing
          out.afterStop = {{
            pending: pending().length, fetches: fetches.length,
            hidden: notice().hidden, text: notice().textContent,
          }};

          // The hash changed without stop() being called (e.g. typed in the bar).
          reset();
          responses = [livePayload()];
          await window.RunView.render('c', ref);
          location.hash = '#/batch/c';
          await fire(pending()[0]);
          out.afterHashChange = {{
            pending: pending().length, fetches: fetches.length, hidden: notice().hidden,
          }};

          // Opening a different run supersedes the first one's timer.
          reset();
          responses = [livePayload(), livePayload()];
          await window.RunView.render('c', ref);
          const first = pending()[0];
          location.hash = '#/batch/c/run/source:src2';
          await window.RunView.render('c', {{ kind: 'source', id: 'src2' }});
          await first.fn();
          out.afterSwitch = {{ fetches: [...fetches], pending: pending().length }};
          console.log(JSON.stringify(out));
        }})();
        """
    )

    assert payload["afterStop"] == {"pending": 0, "fetches": 1, "hidden": True, "text": ""}
    assert payload["afterHashChange"] == {"pending": 0, "fetches": 1, "hidden": True}
    assert payload["afterSwitch"]["fetches"] == [
        "/api/run?source=src1",
        "/api/run?source=src2",
    ]
    assert payload["afterSwitch"]["pending"] == 1  # only the second run's timer


def test_run_view_does_not_poll_finished_abandoned_or_failing_runs_wrongly():
    payload = _run_node_json(
        f"""
        {_run_view_harness()}
        (async () => {{
          const out = {{}};

          reset();
          responses = [endedPayload];
          await window.RunView.render('c', ref);
          out.finished = {{ pending: pending().length, hidden: notice().hidden, mapCalls: [...mapCalls] }};

          // Unfinished but its log has gone quiet: shown, labelled, not polled.
          reset();
          responses = [livePayload({{ live: false }})];
          await window.RunView.render('c', ref);
          out.abandoned = {{ pending: pending().length, text: notice().textContent }};

          // A failed refresh keeps the screen and tries again.
          reset();
          responses = [livePayload(), new Error('boom'), livePayload()];
          await window.RunView.render('c', ref);
          await fire(pending()[0]);
          out.failed = {{
            text: notice().textContent, pending: pending().map((timer) => timer.ms),
            mapCalls: [...mapCalls],
          }};
          await fire(pending()[0]);
          out.recovered = {{ text: notice().textContent, mapCalls: [...mapCalls] }};

          // A run with no map yet, then one with a route: first open, then refresh.
          reset();
          const noRoute = livePayload();
          noRoute.run.capabilities = {{}};
          responses = [noRoute, livePayload()];
          await window.RunView.render('c', ref);
          await fire(pending()[0]);
          out.noMapYet = [...mapCalls];
          console.log(JSON.stringify(out));
        }})();
        """
    )

    assert payload["finished"] == {"pending": 0, "hidden": True, "mapCalls": ["open"]}
    assert payload["abandoned"] == {
        "pending": 0,
        "text": "进行中 · 日志已长时间未更新，已停止自动刷新",
    }
    assert "本次刷新失败：boom" in payload["failed"]["text"]
    assert payload["failed"]["pending"] == [30000]
    assert payload["failed"]["mapCalls"] == ["open"]
    assert payload["recovered"]["text"] == "进行中 · 每 30 秒自动刷新"
    assert payload["recovered"]["mapCalls"] == ["open", "refresh"]
    assert payload["noMapYet"] == ["no-map", "open"]


def test_router_stops_the_live_refresh_on_every_route_but_a_run():
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    route_fn = _javascript_section(
        app_script, "async function applyRoute", "window.addEventListener('popstate'"
    )

    assert "window.RunView.stop()" in route_fn
    assert route_fn.index("window.RunView.stop()") < route_fn.index("route.view === 'root'")
    assert "route.view !== 'run'" in route_fn
    assert 'id="runLiveNotice"' in html
    # the notice sits with the run detail it describes, hidden until needed
    assert html.index('id="runMeta"') < html.index('id="runLiveNotice"') < html.index('id="actTabs"')
    assert re.search(r'id="runLiveNotice"[^>]*\bhidden\b', html)


def test_map_refresh_redraws_in_place_keeps_the_selected_node_and_survives_errors():
    script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    load = _javascript_section(script, "async function loadAct", "function closeMapPage")

    result = _run_node_json(
        f"""
        const log = [];
        let respond = null;
        const mapState = {{
          runId: '', ref: null, actIndex: 0, opener: null, requestToken: 0,
          abortController: null, positionedMapKey: '', selectedNodeId: null,
        }};
        const mapScroll = {{ scrollTop: 0, scrollLeft: 0 }};
        const groups = [{{ id: 'g0' }}, {{ id: 'g1' }}, {{ id: 'g2' }}];
        const svg = {{ parentElement: mapScroll, querySelectorAll: () => groups }};
        const elements = new Map([['mapSvg', svg]]);
        function byId(id) {{
          if (!elements.has(id)) elements.set(id, {{ hidden: false, textContent: '' }});
          return elements.get(id);
        }}
        function showMapPage() {{ log.push('showMapPage'); }}
        function renderEmpty(node, message) {{ log.push(`renderEmpty:${{message}}`); }}
        function clear() {{ log.push('clear'); }}
        function hideDecisionPopover() {{ log.push('hidePopover'); }}
        const history = {{ state: null, pushState() {{}}, replaceState() {{}} }};
        function mapLocation() {{ return ''; }}
        function setStatus(message, tone) {{ log.push(`status:${{message}}`); }}
        async function getJSON() {{
          if (respond instanceof Error) throw respond;
          return respond;
        }}
        function renderActTabs() {{ return null; }}
        function renderMap() {{ log.push('renderMap'); }}
        function renderActSummary() {{ log.push('renderActSummary'); }}
        function selectNode(node, element) {{
          mapState.selectedNodeId = node.id;
          log.push(`select:${{node.id}}:${{element ? element.id : '-'}}`);
        }}
        {load}
        const node = (id, index) => ({{ id, visited: true, path_index: index }});
        const payload = (nodes) => ({{
          act: {{ index: 0 }}, nodes, full_map: false, fallback_reason: null,
        }});
        (async () => {{
          const ref = {{ kind: 'source', id: 'src1' }};
          respond = payload([node('n0', 0), node('n1', 1)]);
          await loadAct(ref, 0);
          const opened = log.splice(0);

          // The user picks the second node; two ticks later the run is a node longer.
          mapState.selectedNodeId = 'n1';
          respond = payload([node('n0', 0), node('n1', 1), node('n2', 2)]);
          await loadAct(ref, 0, {{ refresh: true }});
          const refreshed = log.splice(0);

          // The selected node is gone from the new payload: fall back to the first.
          mapState.selectedNodeId = 'gone';
          await loadAct(ref, 0, {{ refresh: true }});
          const fellBack = log.splice(0);

          respond = new Error('server down');
          await loadAct(ref, 0, {{ refresh: true }});
          const failed = log.splice(0);
          console.log(JSON.stringify({{ opened, refreshed, fellBack, failed }}));
        }})();
        """
    )

    # A normal open clears, shows the page and the loading state, as before.
    assert "showMapPage" in result["opened"]
    assert "clear" in result["opened"]
    assert any(item.startswith("renderEmpty:") for item in result["opened"])
    # A refresh does none of that: no flicker, no focus grab, no busy status.
    assert result["refreshed"] == [
        "hidePopover",
        "renderMap",
        "renderActSummary",
        "select:n1:g1",
        "status:已载入记录路线",
    ]
    assert result["fellBack"][-2:] == ["select:n0:-", "status:已载入记录路线"]
    # A failed refresh leaves what is on screen alone and says so in the status.
    assert result["failed"] == ["status:地图刷新失败：server down"]


def test_map_namespace_exposes_refresh_run_for_the_run_view():
    map_script = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    view_script = (STATIC_DIR / "run-view.js").read_text(encoding="utf-8")

    namespace = _javascript_section(map_script, "window.STS2Map = Object.freeze", "})();")
    assert "refreshRun(ref)" in namespace
    assert "window.STS2Map.refreshRun(ref)" in view_script


# ---------------------------------------------------------------------------
# 回归/评测 tab: shell, router, default tab, batches-view.js, runs-table reuse
# ---------------------------------------------------------------------------


def test_shell_has_tab_bar_above_the_tree_and_a_regression_pane():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser = _ShellParser()
    parser.feed(html)

    for item in ("workbenchTabs", "tabTraining", "tabRegression", "regressionView",
                 "regressionBody", "regressionNotice"):
        assert item in parser.ids, item
    # tab bar, then the tree, then the content pane that holds the regression view
    order = ["workbenchTabs", "cohortTree", "contentPane", "regressionView", "regressionBody"]
    assert [parser.ids.index(item) for item in order] == sorted(parser.ids.index(item) for item in order)
    assert '<nav id="workbenchTabs" class="workbench-tabs" role="tablist"' in html
    assert html.count('role="tab"') == 2
    assert "回归/评测" in html and ">训练<" in html
    # the training tab is the shipped default until the router decides otherwise
    assert re.search(r'id="tabTraining"[^>]*aria-selected="true"', html)
    assert re.search(r'id="tabRegression"[^>]*aria-selected="false"', html)
    scripts = re.findall(r'<script src="/static/([\w.-]+)"', html)
    assert scripts.index("batches-view.js") > scripts.index("runs-table.js")
    assert scripts[-1] == "map.js"
    assert "<style" not in html.lower()


def test_batches_view_is_a_served_utf8_asset(tmp_path: Path):
    viewer_source = Path(viewer.__file__).read_text(encoding="utf-8")
    assert (
        '"/static/batches-view.js": ("batches-view.js", "text/javascript; charset=utf-8")'
        in viewer_source
    )
    assert "/static/batches-view.js" not in viewer.QUERY_ALLOWED_ASSETS
    with _server(tmp_path) as base:
        status, content_type, body = _get(base, "/static/batches-view.js")
        query_status, _, _ = _get(base, "/static/batches-view.js?x=1")
    assert status == 200
    assert content_type == "text/javascript; charset=utf-8"
    assert body == (STATIC_DIR / "batches-view.js").read_bytes()
    assert query_status == 404


def test_regression_pane_css_keeps_tree_hidden_layout_and_progress_styles():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert re.search(r'\.workbench-body\[data-tab="regression"\]\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);', css, re.DOTALL)
    assert re.search(r"\.cohort-tree\[hidden\]\s*\{\s*display:\s*none;", css)
    assert re.search(r"\.regression-view\[hidden\]\s*\{\s*display:\s*none;", css)
    for selector in (".workbench-tab[aria-selected=\"true\"]", ".batch-progress-bar", ".batch-badge-running",
                     ".batch-badge-alert", ".batch-badge-done"):
        assert selector in css, selector


def test_hash_router_parses_regression_routes_and_keeps_training_ones():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    routing = _javascript_section(script, "function cohortRoute", "function navigate")

    payload = _run_node_json(
        f"""
        {routing}
        const odd = 'legacy:run:ooc=greedy';
        console.log(JSON.stringify({{
          list: parseRoute('#/regression'),
          batch: parseRoute('#/regression/batch/regression-20261012-100000'),
          lane: parseRoute('#/regression/batch/b1/lane/Ironclad'),
          oddBatch: parseRoute(batchRoute(odd)),
          oddLane: parseRoute(laneRoute(odd, 'lane/1')),
          armLane: parseRoute(laneRoute('route-ab-1', 'Ironclad/value')),
          armHash: laneRoute('route-ab-1', 'Ironclad/value'),
          training: parseRoute('#/training'),
          root: parseRoute('#/'),
          trainingBatch: parseRoute('#/batch/coh-1'),
          trainingRun: parseRoute('#/batch/-/run/source:src1'),
          junk: parseRoute('#/regression/nope'),
          junkLane: parseRoute('#/regression/batch/b1/lane'),
          hashes: [regressionRoute(), batchRoute(odd), laneRoute('b1', 'Iron clad')],
          laneRunRoute: runRoute('', {{ kind: 'source', id: 'src_1' }}),
          views: [isRegressionView('regression'), isRegressionView('regression-lane'),
                  isRegressionView('batch'), isRegressionView(undefined)],
        }}));
        """
    )

    assert payload["list"] == {"view": "regression"}
    assert payload["batch"] == {"view": "regression-batch", "batchId": "regression-20261012-100000"}
    assert payload["lane"] == {"view": "regression-lane", "batchId": "b1", "lane": "Ironclad"}
    assert payload["oddBatch"] == {"view": "regression-batch", "batchId": "legacy:run:ooc=greedy"}
    assert payload["oddLane"] == {
        "view": "regression-lane", "batchId": "legacy:run:ooc=greedy", "lane": "lane/1",
    }
    # run_batch.py --arm lanes are named <Character>/<arm>
    assert payload["armHash"] == "#/regression/batch/route-ab-1/lane/Ironclad%2Fvalue"
    assert payload["armLane"] == {
        "view": "regression-lane", "batchId": "route-ab-1", "lane": "Ironclad/value",
    }
    assert payload["training"] == {"view": "root", "tab": "training"}
    assert payload["root"] == {"view": "root"}
    assert payload["trainingBatch"] == {"view": "batch", "cohortId": "coh-1"}
    assert payload["trainingRun"] == {
        "view": "run", "cohortId": "-", "ref": {"kind": "source", "id": "src1"},
    }
    assert payload["junk"] == {"view": "root"}
    assert payload["junkLane"] == {"view": "root"}
    assert payload["hashes"] == [
        "#/regression",
        "#/regression/batch/legacy%3Arun%3Aooc%3Dgreedy",
        "#/regression/batch/b1/lane/Iron%20clad",
    ]
    assert payload["laneRunRoute"] == "#/batch/-/run/source:src_1"
    assert payload["views"] == [True, True, False, False]


def test_default_tab_is_regression_only_when_a_batch_runs_or_ended_within_a_day():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    helper = _javascript_section(script, "const REGRESSION_DEFAULT_WINDOW_SECONDS", "async function applyRoute")

    payload = _run_node_json(
        f"""
        {helper}
        const NOW = 1_800_000_000;
        const H = 3600;
        console.log(JSON.stringify({{
          running: shouldDefaultToRegression([{{ done: false, created_at: NOW - 90 * H }}], NOW),
          justFinished: shouldDefaultToRegression([{{ done: true, created_at: NOW - 30 * H, updated_at: NOW - 2 * H }}], NOW),
          createdRecently: shouldDefaultToRegression([{{ done: true, created_at: NOW - 3 * H }}], NOW),
          exactlyADay: shouldDefaultToRegression([{{ done: true, updated_at: NOW - 24 * H }}], NOW),
          old: shouldDefaultToRegression([{{ done: true, created_at: NOW - 5 * H * 24, updated_at: NOW - 30 * H }}], NOW),
          updatedWinsOverCreated: shouldDefaultToRegression([{{ done: true, created_at: NOW - H, updated_at: NOW - 40 * H }}], NOW),
          oneOfMany: shouldDefaultToRegression([{{ done: true, updated_at: NOW - 99 * H }}, {{ done: false }}], NOW),
          none: shouldDefaultToRegression([], NOW),
          malformed: [shouldDefaultToRegression(null, NOW), shouldDefaultToRegression([null, 3, {{}}], NOW),
                      shouldDefaultToRegression([{{ done: true, updated_at: 'x' }}], NOW),
                      shouldDefaultToRegression([{{ done: true, updated_at: NOW }}], null)],
        }}));
        """
    )

    assert payload["running"] is True
    assert payload["justFinished"] is True
    assert payload["createdRecently"] is True
    assert payload["exactlyADay"] is True
    assert payload["old"] is False
    # last activity beats the creation time: an old batch that just wrote a log is "recent"
    assert payload["updatedWinsOverCreated"] is False
    assert payload["oneOfMany"] is True
    assert payload["none"] is False
    assert payload["malformed"] == [False, False, False, False]


_FAKE_VIEW_DOM = """
function makeNode(tag) {
  const node = {
    tag, className: '', textContent: '', hidden: false, dataset: {},
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
    addEventListener(type, handler) { (this._listeners[type] = this._listeners[type] || []).push(handler); },
    dispatch(type, event = {}) {
      (this._listeners[type] || []).forEach((handler) => handler({ preventDefault() {}, ...event }));
    },
    click() { this.dispatch('click'); },
    focus() { this.focused = true; },
    querySelectorAll(selector) {
      const out = [];
      const walkChildren = (parent) => parent.children.forEach((child) => {
        if (selector === '*' || child.tag === selector) out.push(child);
        walkChildren(child);
      });
      walkChildren(this);
      return out;
    },
  };
  return node;
}
const document = { createElement: (tag) => makeNode(tag) };
const nodes = {};
function byId(id) { return nodes[id] || (nodes[id] = makeNode('div')); }
function collect(node, test, out = []) {
  if (test(node)) out.push(node);
  node.children.forEach((child) => collect(child, test, out));
  return out;
}
function allText(node) {
  return [node.textContent, ...node.children.map(allText)].filter(Boolean).join(' ');
}
"""


def _batches_view_harness() -> str:
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    view_script = (STATIC_DIR / "batches-view.js").read_text(encoding="utf-8")
    return "\n".join(
        [
            _FAKE_VIEW_DOM,
            _javascript_section(util_script, "function element(tag", "function svgElement"),
            _javascript_section(util_script, "function clear(node)", "function setStatus"),
            _javascript_section(util_script, "function formatMissing", "function formatBytes"),
            _javascript_section(util_script, "function renderEmpty", "function setSelectOptions"),
            _javascript_section(app_script, "function cohortRoute", "function navigate"),
            """
            function formatTime(value) { return `T${value}`; }
            const state = { batches: [], batchesNow: null };
            const statuses = [];
            function setStatus(message, tone) { statuses.push([message, tone]); }
            const navigateCalls = [];
            function navigate(hash) { navigateCalls.push(hash); }
            const timers = [];
            let nextTimer = 1;
            function setTimeout(fn, ms) {
              const timer = { id: nextTimer++, fn, ms, cleared: false, fired: false };
              timers.push(timer);
              return timer.id;
            }
            function clearTimeout(id) {
              const timer = timers.find((candidate) => candidate.id === id);
              if (timer) timer.cleared = true;
            }
            const pending = () => timers.filter((timer) => !timer.cleared && !timer.fired);
            async function fire(timer) { timer.fired = true; await timer.fn(); }
            const fetches = [];
            let responses = [];
            async function getJSON(url) {
              fetches.push(url);
              const next = responses.shift();
              if (next instanceof Error) throw next;
              return next;   // a Promise is awaited by the caller: tests resolve it late
            }
            const runsTableCalls = [];
            const RunsTable = { renderRows(container, rows, options) { runsTableCalls.push({ container, rows, options }); } };
            const window = {};
            """,
            view_script,
            "const BV = window.BatchesView;",
        ]
    )


_BATCH_ROW = {
    "batch_id": "regression-20261012-100000", "name": "regression", "kind": "regression",
    "registered": True, "created_at": 1_800_000_000 - 7200, "git_commit": "abc1234",
    "argv": ["python/run_batch.py", "--name", "regression"], "env": {"STS2_SOLVER_THREADS": "3"},
    "lane_count": 2, "planned": 10, "finished": 4, "live": 1, "abandoned": 0, "wins": 0,
    "technical": 1, "failed_lanes": 0, "stalled_lanes": 0, "eta_seconds": 5400.0,
    "eta_at": 1_800_000_000 + 5400, "status": "running", "done": False,
    "updated_at": 1_800_000_000 - 10, "median_duration_seconds": 900.0,
}
_DONE_ROW = {
    **_BATCH_ROW, "batch_id": "smoke-1", "name": "smoke", "kind": "smoke", "planned": 2,
    "finished": 2, "live": 0, "technical": 0, "eta_seconds": None, "eta_at": None,
    "status": "done", "done": True,
}
_LEGACY_ROW = {
    **_BATCH_ROW, "batch_id": "legacy:ooc=greedy", "name": "未登记批次 · ooc=greedy", "kind": None,
    "registered": False, "planned_known": False, "planned": 14, "finished": 14, "live": 0,
    "technical": 3, "eta_seconds": None, "eta_at": None, "status": "done", "done": True,
}
_LANE_ROWS = [
    {"lane": "Ironclad", "character": "Ironclad", "policy": "greedy", "planned": 5, "planned_known": True,
     "finished": 3, "live": 1, "abandoned": 0, "remaining": 2, "wins": 1, "win_rate": 1 / 3,
     "technical": 0, "valid_n": 3, "avg_global_floor": 31.5, "median_duration_seconds": 900.0,
     "eta_applicable": True, "eta_seconds": 1800.0, "eta_at": 1_800_000_000 + 1800, "state": "running",
     "pid": 7, "returncode": None, "error": None},
    {"lane": "Silent", "character": "Silent", "policy": None, "planned": 5, "planned_known": True,
     "finished": 0, "live": 0, "abandoned": 0, "remaining": 5, "wins": 0, "win_rate": None,
     "technical": 0, "valid_n": 0, "avg_global_floor": None, "median_duration_seconds": None,
     "eta_applicable": True, "eta_seconds": None, "eta_at": None, "state": "queued",
     "pid": None, "returncode": None, "error": None},
    {"lane": "Defect", "character": "Defect", "policy": "greedy", "planned": 5, "planned_known": True,
     "finished": 1, "live": 0, "abandoned": 0, "remaining": 4, "wins": 0, "win_rate": 0.0,
     "technical": 1, "valid_n": 0, "avg_global_floor": None, "median_duration_seconds": 600.0,
     "eta_applicable": False, "eta_seconds": None, "eta_at": None, "state": "failed",
     "pid": None, "returncode": None, "error": "FileNotFoundError: run_caffeinated.sh"},
]


def _view_script(body: str, **payloads) -> str:
    script = _batches_view_harness() + "\n" + body
    for name, value in payloads.items():
        script = script.replace(f"__{name.upper()}__", json.dumps(value))
    return script


def test_eta_and_clock_formatting():
    payload = _run_node_json(
        _view_script(
            """
            const local = (y, mo, d, h, mi) => new Date(y, mo, d, h, mi).getTime() / 1000;
            const now = local(2026, 9, 12, 12, 0);
            console.log(JSON.stringify({
              hours: [BV.formatSpan(5400), BV.formatSpan(7200), BV.formatSpan(3600), BV.formatSpan(9 * 3600 + 600)],
              minutes: [BV.formatSpan(1800), BV.formatSpan(90), BV.formatSpan(60)],
              short: [BV.formatSpan(20), BV.formatSpan(0)],
              missing: [BV.formatSpan(null), BV.formatSpan(undefined), BV.formatSpan(NaN), BV.formatSpan(-5)],
              sameDay: BV.formatClock(local(2026, 9, 12, 18, 5), now),
              otherDay: BV.formatClock(local(2026, 9, 13, 3, 7), now),
              noNow: BV.formatClock(local(2026, 9, 12, 18, 5), null),
              badClock: [BV.formatClock(null, now), BV.formatClock(NaN, now)],
              eta: BV.formatEta(5400, local(2026, 9, 12, 13, 30), now),
              etaNextDay: BV.formatEta(16 * 3600, local(2026, 9, 13, 4, 0), now),
              etaNoClock: BV.formatEta(1800, null, now),
              etaNone: [BV.formatEta(null, 5, now), BV.formatEta(undefined, null, now)],
              durations: [BV.formatDuration(45), BV.formatDuration(600), BV.formatDuration(5400), BV.formatDuration(null)],
            }));
            """
        )
    )

    assert payload["hours"] == ["约 1.5 小时", "约 2 小时", "约 1 小时", "约 9.2 小时"]
    assert payload["minutes"] == ["约 30 分钟", "约 2 分钟", "约 1 分钟"]
    assert payload["short"] == ["不到 1 分钟", "不到 1 分钟"]
    assert payload["missing"] == ["—", "—", "—", "—"]
    assert payload["sameDay"] == "18:05"
    assert payload["otherDay"] == "10/13 03:07"
    assert payload["noNow"] == "10/12 18:05"
    assert payload["badClock"] == ["—", "—"]
    assert payload["eta"] == "约 1.5 小时 · 13:30"
    assert payload["etaNextDay"] == "约 16 小时 · 10/13 04:00"
    assert payload["etaNoClock"] == "约 30 分钟"
    assert payload["etaNone"] == ["—", "—"]
    assert payload["durations"] == ["45 秒", "10 分钟", "1.5 小时", "—"]


def test_batch_list_renders_progress_live_technical_and_eta_per_batch():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__LIST__];
              await BV.render({ view: 'regression' });
              const body = byId('regressionBody');
              const rows = collect(body, (n) => n.tag === 'tr').slice(1);
              const cellsOf = (tr) => tr.children.map((td) => allText(td));
              const meters = collect(body, (n) => n.tag === 'progress');
              rows[0].click();
              rows[1].dispatch('keydown', { key: 'Enter' });
              rows[2].dispatch('keydown', { key: ' ' });
              rows[2].dispatch('keydown', { key: 'a' });
              console.log(JSON.stringify({
                fetches,
                head: collect(body, (n) => n.tag === 'th').map((n) => n.textContent),
                cells: rows.map(cellsOf),
                meters: meters.map((m) => [m.getAttribute('value'), m.getAttribute('max'), m.getAttribute('aria-label')]),
                rowAttrs: rows.map((tr) => [tr.getAttribute('tabindex'), tr.getAttribute('role')]),
                navigateCalls,
                pending: pending().map((t) => t.ms),
                state: { batches: state.batches.length, now: state.batchesNow },
                statuses,
                technicalClass: collect(body, (n) => n.className === 'batch-technical').length,
                heading: allText(collect(body, (n) => n.tag === 'h2')[0]),
              }));
            })();
            """,
            list={
                "batches": [_BATCH_ROW, _DONE_ROW, _LEGACY_ROW,
                            {**_DONE_ROW, "batch_id": "stalled-1", "name": "stalled", "status": "stalled",
                             "eta_seconds": 3600.0, "eta_at": 1_800_000_000 + 3600}],
                "problems": [], "now": 1_800_000_000,
            },
        )
    )

    assert payload["fetches"] == ["/api/batches"]
    assert payload["head"] == ["批次", "类型", "开始时间", "进度", "进行中", "技术失败", "状态", "预计完成"]
    running, done, legacy, stalled = payload["cells"]
    assert running[0] == "regression regression-20261012-100000"
    assert running[1] == "回归"
    assert running[2] == "T1799992800"
    assert "4 / 10（40%）" in running[3]
    assert running[4] == "1" and running[5] == "1"
    assert running[6] == "运行中"
    assert running[7].startswith("约 1.5 小时 · ")
    # finished batch: a kind badge, no ETA
    assert done[1] == "冒烟" and done[6] == "已完成" and done[7] == "—"
    # unregistered: no kind, said so, still shown with what was played
    assert legacy[0].startswith("未登记批次 · ooc=greedy")
    assert "无登记信息" in legacy[0]
    assert legacy[1] == "—"
    # no plan -> counts only: never "14 / 14 (100%)", which would claim a completion
    assert legacy[3] == "完成 14 · 进行中 0"
    assert "%" not in legacy[3] and "/" not in legacy[3]
    assert payload["meters"][0] == ["4", "10", "已完成 4 / 10 局"]
    assert len(payload["meters"]) == 3  # the legacy batch draws no bar
    assert payload["rowAttrs"][0] == ["0", "link"]
    assert payload["navigateCalls"] == [
        "#/regression/batch/regression-20261012-100000",
        "#/regression/batch/smoke-1",
        "#/regression/batch/legacy%3Aooc%3Dgreedy",
    ]
    # a batch that is not running shows no ETA even if the payload carried one
    assert stalled[6] == "已停滞" and stalled[7] == "—"
    # one running batch -> the list refreshes itself every 30 s
    assert payload["pending"] == [30000]
    assert payload["state"] == {"batches": 4, "now": 1_800_000_000}
    assert payload["statuses"][-1][0] == "已载入批次"
    assert payload["technicalClass"] == 2  # the running batch (1) and the legacy one (3)
    assert "回归 / 评测批次" in payload["heading"]


def test_batch_list_empty_and_problem_states():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__EMPTY__];
              await BV.render({ view: 'regression' });
              const empty = allText(byId('regressionBody'));
              const emptyPending = pending().length;
              responses = [__PROBLEMS__];
              await BV.render({ view: 'regression' });
              console.log(JSON.stringify({
                empty, emptyPending,
                withProblems: allText(byId('regressionBody')),
                tables: collect(byId('regressionBody'), (n) => n.tag === 'table').length,
              }));
            })();
            """,
            empty={"batches": [], "problems": [], "now": 5},
            problems={"batches": [_DONE_ROW], "problems": ["broken/manifest.json: ValueError"], "now": 5},
        )
    )

    assert "还没有回归/评测批次" in payload["empty"]
    assert payload["emptyPending"] == 0
    assert "1 个批次文件无法读取" in payload["withProblems"]
    assert "broken/manifest.json" in payload["withProblems"]
    assert payload["tables"] == 1


def test_batch_page_shows_lane_table_with_policy_floor_wins_state_and_eta():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__BATCH__];
              await BV.render({ view: 'regression-batch', batchId: 'regression-20261012-100000' });
              const body = byId('regressionBody');
              const lanes = collect(body, (n) => n.tag === 'tr').slice(1);
              lanes[0].click();
              lanes[2].dispatch('keydown', { key: 'Enter' });
              const back = collect(body, (n) => n.tag === 'button');
              back[0].click();
              const cards = collect(body, (n) => n.className.startsWith('metric-card')).map(allText);
              console.log(JSON.stringify({
                fetches,
                head: collect(body, (n) => n.tag === 'th').map((n) => n.textContent),
                lanes: lanes.map((tr) => tr.children.map(allText)),
                navigateCalls,
                cards,
                title: allText(collect(body, (n) => n.className === 'section-heading')[0]),
                meta: collect(body, (n) => n.className === 'batch-fact').map((n) => n.children.map((c) => c.textContent)),
                details: collect(body, (n) => n.tag === 'pre').map((n) => n.textContent),
                pending: pending().map((t) => t.ms),
              }));
            })();
            """,
            batch={"batch": _BATCH_ROW, "lanes": _LANE_ROWS, "problems": [], "now": 1_800_000_000},
        )
    )

    assert payload["fetches"] == ["/api/batch?id=regression-20261012-100000"]
    assert payload["head"] == ["通道", "策略", "进度", "平均推进层数", "胜场（胜率）", "技术失败", "状态", "预计完成"]
    ironclad, silent, defect = payload["lanes"]
    assert ironclad[0] == "Ironclad"
    assert ironclad[1] == "greedy"
    assert "3 / 5（60%） · 1 进行中" in ironclad[2]
    assert ironclad[3] == "31.5"
    assert ironclad[4] == "1（33.3%）"
    assert ironclad[6] == "运行中"
    assert ironclad[7].startswith("约 30 分钟 · ")
    # queued, nothing played: dashes, never zeros
    assert silent[1] == "—" and silent[3] == "—" and silent[4] == "—"
    assert silent[6] == "排队中" and silent[7] == "—"
    # failed lane: why, and no ETA
    assert defect[5] == "1"
    assert "失败" in defect[6] and "run_caffeinated.sh" in defect[6]
    assert defect[7] == "—"
    assert payload["navigateCalls"] == [
        "#/regression/batch/regression-20261012-100000/lane/Ironclad",
        "#/regression/batch/regression-20261012-100000/lane/Defect",
        "#/regression",
    ]
    assert any(card.startswith("已完成 4 / 10") for card in payload["cards"])
    assert any(card.startswith("预计完成 约 1.5 小时") for card in payload["cards"])
    assert "regression" in payload["title"] and "运行中" in payload["title"] and "回归" in payload["title"]
    meta = dict(payload["meta"])
    assert meta["代码版本"] == "abc1234"
    assert meta["批次 ID"] == "regression-20261012-100000"
    assert meta["中位单局耗时"] == "15 分钟"
    assert any("python/run_batch.py --name regression" in text for text in payload["details"])
    assert any("STS2_SOLVER_THREADS=3" in text for text in payload["details"])
    assert payload["pending"] == [30000]


_ARM_LANES = [
    {**_LANE_ROWS[0], "lane": "Ironclad/random", "arm": "random",
     "arm_env": {"STS2_ROUTE_POLICY": "random"}},
    {**_LANE_ROWS[0], "lane": "Ironclad/value", "arm": "value",
     "arm_env": {"STS2_ROUTE_POLICY": "value"}, "finished": 2},
    {**_LANE_ROWS[1], "lane": "Silent/random", "arm": "random",
     "arm_env": {"STS2_ROUTE_POLICY": "random"}},
]


def test_batch_page_adds_an_arm_column_only_when_a_lane_has_an_arm():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              const out = {};
              const draw = async (payload) => {
                responses = [payload];
                await BV.render({ view: 'regression-batch', batchId: 'route-ab-1' });
                const body = byId('regressionBody');
                return {
                  head: collect(body, (n) => n.tag === 'th').map((n) => n.textContent),
                  rows: collect(body, (n) => n.tag === 'tr').slice(1).map((tr) => tr.children.map(allText)),
                };
              };
              out.arms = await draw(__ARMS__);
              const body = byId('regressionBody');
              collect(body, (n) => n.tag === 'tr').slice(1)[1].click();
              out.navigateCalls = navigateCalls.splice(0);
              out.plain = await draw(__PLAIN__);
              console.log(JSON.stringify(out));
            })();
            """,
            arms={"batch": {**_BATCH_ROW, "kind": "ab"}, "lanes": _ARM_LANES, "problems": [], "now": 1_800_000_000},
            plain={"batch": _BATCH_ROW, "lanes": _LANE_ROWS, "problems": [], "now": 1_800_000_000},
        )
    )

    assert payload["arms"]["head"] == [
        "通道", "分组", "策略", "进度", "平均推进层数", "胜场（胜率）", "技术失败", "状态", "预计完成"]
    assert [row[:2] for row in payload["arms"]["rows"]] == [
        ["Ironclad/random", "random"], ["Ironclad/value", "value"], ["Silent/random", "random"]]
    # the columns after 分组 keep their order (policy, progress, ..., state)
    assert payload["arms"]["rows"][0][2] == "greedy"
    assert "3 / 5（60%） · 1 进行中" in payload["arms"]["rows"][0][3]
    assert payload["arms"]["rows"][0][7] == "运行中"
    # the slash in a lane name travels as %2F, so the hash router's lane segment stays whole
    assert payload["navigateCalls"] == [
        f"#/regression/batch/{_BATCH_ROW['batch_id']}/lane/Ironclad%2Fvalue"]
    # a batch without arms is unchanged
    assert payload["plain"]["head"] == [
        "通道", "策略", "进度", "平均推进层数", "胜场（胜率）", "技术失败", "状态", "预计完成"]
    assert len(payload["plain"]["rows"][0]) == 8


def test_lane_page_names_the_arm_and_its_environment():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              const meta = async (lane) => {
                responses = [{ batch_id: 'route-ab-1', batch_name: 'route-ab', lane: lane.lane,
                               lane_summary: lane, runs: [], runs_complete: true, now: 1_800_000_000 }];
                await BV.render({ view: 'regression-lane', batchId: 'route-ab-1', lane: lane.lane });
                return collect(byId('regressionBody'), (n) => n.className === 'batch-fact')
                  .map((n) => n.children.map((c) => c.textContent));
              };
              console.log(JSON.stringify({
                arm: await meta(__ARM__),
                plain: await meta(__PLAIN__),
                fetches,
              }));
            })();
            """,
            arm=_ARM_LANES[1],
            plain=_LANE_ROWS[0],
        )
    )

    arm = dict(payload["arm"])
    assert arm["分组"] == "value" and arm["分组环境"] == "STS2_ROUTE_POLICY=value"
    assert "分组" not in dict(payload["plain"]) and "分组环境" not in dict(payload["plain"])
    assert payload["fetches"][0] == "/api/batch/lane?id=route-ab-1&lane=Ironclad%2Fvalue"


def test_an_unplanned_batch_shows_counts_not_a_completion_bar():
    """A legacy / unregistered batch has no plan: finished 17 + live 3 used to
    read "17 / 20 (85%)" -- a completion nobody planned.  Counts only now."""
    unplanned = {**_LEGACY_ROW, "planned": 20, "finished": 17, "live": 3, "done": False,
                 "status": "running"}
    unplanned_lanes = [
        {**_LANE_ROWS[0], "planned": 4, "planned_known": False, "finished": 3, "live": 1,
         "remaining": 0, "eta_applicable": False, "eta_seconds": None, "eta_at": None},
        {**_LANE_ROWS[0], "lane": "Silent", "planned_known": True, "planned": 5, "finished": 2,
         "live": 0},
    ]
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__LIST__];
              await BV.render({ view: 'regression' });
              const list = collect(byId('regressionBody'), (n) => n.tag === 'tr').slice(1)
                .map((tr) => allText(tr.children[3]));
              const listMeters = collect(byId('regressionBody'), (n) => n.tag === 'progress').length;
              responses = [__BATCH__];
              await BV.render({ view: 'regression-batch', batchId: 'legacy:ooc=greedy' });
              const body = byId('regressionBody');
              const lanes = collect(body, (n) => n.tag === 'tr').slice(1)
                .map((tr) => allText(tr.children[2]));
              const cards = collect(body, (n) => n.className.startsWith('metric-card')).map(allText);
              const batchMeters = collect(body, (n) => n.tag === 'progress').length;
              responses = [__LANE__];
              await BV.render({ view: 'regression-lane', batchId: 'legacy:ooc=greedy', lane: 'Ironclad' });
              const facts = collect(byId('regressionBody'), (n) => n.className === 'batch-fact')
                .map((n) => n.children.map((c) => c.textContent));
              console.log(JSON.stringify({ list, listMeters, lanes, cards, batchMeters, facts }));
            })();
            """,
            list={"batches": [unplanned], "problems": [], "now": 1_800_000_000},
            batch={"batch": unplanned, "lanes": unplanned_lanes, "problems": [], "now": 1_800_000_000},
            lane={"batch_id": "legacy:ooc=greedy", "batch_name": "x", "lane": "Ironclad",
                  "lane_summary": unplanned_lanes[0], "runs": [], "runs_complete": True,
                  "now": 1_800_000_000},
        )
    )

    assert payload["list"] == ["完成 17 · 进行中 3"]
    assert payload["listMeters"] == 0
    # the lane without a plan: counts; the lane with one keeps its bar
    assert payload["lanes"][0] == "完成 3 · 进行中 1"
    assert "2 / 5（40%）" in payload["lanes"][1]
    assert payload["batchMeters"] == 1
    completed = next(card for card in payload["cards"] if card.startswith("已完成"))
    assert "/" not in completed and "%" not in completed and "17" in completed
    facts = dict(payload["facts"])
    assert facts["进度"].startswith("完成 3") and "/" not in facts["进度"]


def test_a_finished_batch_page_has_no_eta_and_does_not_refresh():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__BATCH__];
              await BV.render({ view: 'regression-batch', batchId: 'smoke-1' });
              const cards = collect(byId('regressionBody'), (n) => n.className.startsWith('metric-card')).map(allText);
              console.log(JSON.stringify({ cards, pending: pending().length }));
            })();
            """,
            batch={"batch": _DONE_ROW, "lanes": [], "problems": [], "now": 1_800_000_000},
        )
    )
    assert any(card.startswith("预计完成 —") and "批次已结束" in card for card in payload["cards"])
    assert payload["pending"] == 0


_LANE_RUNS = [
    {"run_id": "pfr-3", "source_id": "src_3", "seed": "run_3", "status": "in_progress", "state": "live",
     "global_floor": 4, "act": 1, "floor": 4, "hp": 76, "max_hp": 80, "started_at": 300, "updated_at": 310,
     "duration_seconds": None, "game_index": 3, "has_map": True, "ref": {"kind": "source", "id": "src_3"}},
    {"run_id": "pfr-2", "source_id": "src_2", "seed": "run_2", "status": "dead", "state": "finished",
     "global_floor": 20, "act": 2, "floor": 3, "hp": None, "max_hp": None, "started_at": 200, "updated_at": 290,
     "duration_seconds": 1200.0, "game_index": 2, "has_map": True, "ref": {"kind": "source", "id": "src_2"}},
]


def test_lane_page_reuses_the_runs_table_with_source_routes_and_extra_columns():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__LANE__];
              await BV.render({ view: 'regression-lane', batchId: 'b1', lane: 'Ironclad' });
              const body = byId('regressionBody');
              const call = runsTableCalls[0];
              const options = call.options;
              console.log(JSON.stringify({
                fetches,
                calls: runsTableCalls.length,
                rows: call.rows.map((r) => r.seed),
                containerIsTableScroll: call.container.className,
                routeFor: options.routeFor({ kind: 'source', id: 'src_3' }),
                routeForRun: options.routeFor({ kind: 'run', id: 'pfr-1' }),
                extra: options.extraColumns.slice(0, 2).map((c) => [c.label, c.value(call.rows[0]), c.value(call.rows[1])]),
                extraMissing: options.extraColumns.slice(0, 2).map((c) => c.value({ game_index: null, duration_seconds: null })),
                columns: options.extraColumns.map((c) => c.label),
                mapLink: (() => {
                  const link = options.extraColumns[2].render({ ref: { kind: 'source', id: 'src_3' } });
                  let stopped = 0;
                  link.click();
                  link.dispatch('click', { stopPropagation() { stopped += 1; } });
                  link.dispatch('keydown', { key: 'Enter', stopPropagation() { stopped += 1; } });
                  return { tag: link.tag, text: link.textContent, stopped, calls: navigateCalls.splice(0),
                           noRef: options.extraColumns[2].render({ ref: null }) };
                })(),
                title: allText(collect(body, (n) => n.className === 'section-heading')[0]),
                buttons: collect(body, (n) => n.tag === 'button').map((n) => n.textContent),
                meta: collect(body, (n) => n.className === 'batch-fact').map((n) => n.children.map((c) => c.textContent)),
                note: collect(body, (n) => n.className.includes('batch-problems')).map(allText),
                pending: pending().map((t) => t.ms),
              }));
            })();
            """,
            lane={
                "batch_id": "b1", "batch_name": "regression", "lane": "Ironclad",
                "lane_summary": {**_LANE_ROWS[0], "abandoned": 2}, "runs": _LANE_RUNS,
                "runs_complete": True, "now": 1_800_000_000,
            },
        )
    )

    assert payload["fetches"] == ["/api/batch/lane?id=b1&lane=Ironclad"]
    assert payload["calls"] == 1
    assert payload["rows"] == ["run_3", "run_2"]
    assert payload["containerIsTableScroll"] == "table-scroll"
    # a lane row opens the per-game detail; the old map view stays one click away
    assert payload["routeFor"] == "#/regression/game/src_3"
    assert payload["routeForRun"] == "#/regression/game/run%3Apfr-1"
    assert payload["columns"] == ["局号", "耗时", "地图视图"]
    link = payload["mapLink"]
    assert (link["tag"], link["text"]) == ("button", "地图视图")
    # inside a clickable row: it never lets the row's own click through
    assert link["stopped"] == 2
    # a regression game belongs to no cohort: the placeholder cohort id keeps the run route valid
    assert link["calls"] == ["#/batch/-/run/source:src_3", "#/batch/-/run/source:src_3"]
    assert link["noRef"] is None
    assert payload["extra"] == [["局号", 3, 2], ["耗时", "—", "20 分钟"]]
    assert payload["extraMissing"] == ["—", "—"]
    assert "Ironclad" in payload["title"] and "regression" in payload["title"] and "运行中" in payload["title"]
    assert payload["buttons"] == ["← 全部批次", "← 返回批次"]
    meta = dict(payload["meta"])
    assert meta["进度"] == "3 / 5" and meta["进行中"] == "1"
    assert meta["平均推进层数"] == "31.5" and meta["胜场（胜率）"] == "1（33.3%）"
    assert meta["策略"] == "greedy" and meta["中位单局耗时"] == "15 分钟"
    assert meta["预计完成"].startswith("约 30 分钟")
    assert any("2 局日志已长时间未更新" in note for note in payload["note"])
    assert payload["pending"] == [30000]


def test_lane_page_navigation_buttons_and_empty_lane():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__LANE__];
              await BV.render({ view: 'regression-lane', batchId: 'legacy:ooc=x', lane: 'Silent' });
              const buttons = collect(byId('regressionBody'), (n) => n.tag === 'button');
              buttons[0].click();
              buttons[1].click();
              console.log(JSON.stringify({
                navigateCalls,
                runsTableCalls: runsTableCalls.length,
                text: allText(byId('regressionBody')),
                pending: pending().length,
              }));
            })();
            """,
            lane={"batch_id": "legacy:ooc=x", "batch_name": "x", "lane": "Silent",
                  "lane_summary": {**_LANE_ROWS[1], "state": "done", "live": 0},
                  "runs": [], "runs_complete": True, "now": 1},
        )
    )
    assert payload["navigateCalls"] == ["#/regression", "#/regression/batch/legacy%3Aooc%3Dx"]
    assert payload["runsTableCalls"] == 0
    assert "该通道还没有对局记录" in payload["text"]
    assert payload["pending"] == 0


def test_running_batches_refresh_every_30_seconds_until_they_end_or_the_user_leaves():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              const running = __RUNNING__;
              const changed = JSON.parse(JSON.stringify(running));
              changed.batches[0].finished = 5;
              const samePlusNow = JSON.parse(JSON.stringify(running));
              samePlusNow.now += 30;
              const ended = JSON.parse(JSON.stringify(running));
              ended.batches[0].done = true;
              ended.batches[0].status = 'done';
              const out = {};

              responses = [running];
              await BV.render({ view: 'regression' });
              const firstBody = byId('regressionBody').children.slice();
              out.first = pending().map((t) => t.ms);

              // nothing but the clock changed: the DOM is not rebuilt
              responses = [samePlusNow];
              await fire(pending()[0]);
              out.unchangedKeptDom = firstBody.every((node, i) => byId('regressionBody').children[i] === node);
              out.afterUnchanged = pending().length;

              // a real change redraws
              responses = [changed];
              await fire(pending()[0]);
              out.changedRedrawn = byId('regressionBody').children.some((node, i) => node !== firstBody[i]);
              out.fetchCount = fetches.length;

              // a failing refresh keeps what is shown, says so, and tries again
              responses = [new Error('server down')];
              await fire(pending()[0]);
              out.notice = byId('regressionNotice').textContent;
              out.noticeHidden = byId('regressionNotice').hidden;
              out.afterFailure = pending().length;

              // the batch ends: the notice clears and polling stops
              responses = [ended];
              await fire(pending()[0]);
              out.noticeAfter = byId('regressionNotice').hidden;
              out.afterEnd = pending().length;
              console.log(JSON.stringify(out));
            })();
            """,
            running={"batches": [_BATCH_ROW], "problems": [], "now": 1_800_000_000},
        )
    )

    assert payload["first"] == [30000]
    assert payload["unchangedKeptDom"] is True
    assert payload["afterUnchanged"] == 1
    assert payload["changedRedrawn"] is True
    assert payload["fetchCount"] == 3
    assert "自动刷新失败：server down" in payload["notice"]
    assert payload["noticeHidden"] is False
    assert payload["afterFailure"] == 1
    assert payload["noticeAfter"] is True
    assert payload["afterEnd"] == 0


def test_leaving_the_regression_tab_stops_polling_and_ignores_late_responses():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              const running = __RUNNING__;
              responses = [running];
              await BV.render({ view: 'regression' });
              const timer = pending()[0];
              BV.stop();
              const out = { cleared: timer.cleared, pendingAfterStop: pending().length };
              // a refresh already past its timer when the user left must not draw or re-arm
              const before = byId('regressionBody').children.slice();
              responses = [{ batches: [], problems: [], now: 1 }];
              await fire(timer);
              out.drewAfterStop = byId('regressionBody').children.some((n, i) => n !== before[i]) ||
                                  byId('regressionBody').children.length !== before.length;
              out.rearmed = pending().length;
              console.log(JSON.stringify(out));
            })();
            """,
            running={"batches": [_BATCH_ROW], "problems": [], "now": 1_800_000_000},
        )
    )
    assert payload == {"cleared": True, "pendingAfterStop": 0, "drewAfterStop": False, "rearmed": 0}


def test_a_response_that_arrives_after_the_user_left_is_dropped():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              let resolveLate;
              responses = [new Promise((resolve) => { resolveLate = resolve; })];
              const inFlight = BV.render({ view: 'regression' });
              BV.stop();                         // the user went to another route
              resolveLate(__RUNNING__);
              await inFlight;
              console.log(JSON.stringify({
                text: allText(byId('regressionBody')),
                pending: pending().length,
                stateBatches: state.batches.length,
                lastStatus: statuses[statuses.length - 1][0],
              }));
            })();
            """,
            running={"batches": [_BATCH_ROW], "problems": [], "now": 1_800_000_000},
        )
    )
    assert "正在读取回归/评测批次" in payload["text"]  # nothing was drawn over the new page
    assert payload["pending"] == 0
    assert payload["stateBatches"] == 0
    assert payload["lastStatus"] == "正在读取批次…"


def test_a_batch_that_cannot_be_read_says_so_and_offers_the_way_back():
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [new Error('unknown batch id: nope')];
              await BV.render({ view: 'regression-batch', batchId: 'nope' });
              const body = byId('regressionBody');
              const buttons = collect(body, (n) => n.tag === 'button');
              buttons[0].click();
              console.log(JSON.stringify({
                text: allText(body),
                buttons: buttons.map((b) => b.textContent),
                navigateCalls,
                status: statuses[statuses.length - 1],
                pending: pending().length,
              }));
            })();
            """
        )
    )
    assert "批次读取失败：unknown batch id: nope" in payload["text"]
    assert payload["buttons"] == ["← 全部批次"]
    assert payload["navigateCalls"] == ["#/regression"]
    assert payload["status"] == ["批次读取失败：unknown batch id: nope", "error"]
    assert payload["pending"] == 0


def test_needs_refresh_follows_what_is_still_being_played():
    payload = _run_node_json(
        _view_script(
            """
            const lane = (extra) => ({ lane_summary: { state: 'done', live: 0, ...extra } });
            console.log(JSON.stringify({
              list: [BV.needsRefresh({ view: 'regression' }, { batches: [{ done: true }, { done: false }] }),
                     BV.needsRefresh({ view: 'regression' }, { batches: [{ done: true }] }),
                     BV.needsRefresh({ view: 'regression' }, {})],
              batch: [BV.needsRefresh({ view: 'regression-batch' }, { batch: { done: false } }),
                      BV.needsRefresh({ view: 'regression-batch' }, { batch: { done: true } }),
                      BV.needsRefresh({ view: 'regression-batch' }, {})],
              lane: [BV.needsRefresh({ view: 'regression-lane' }, lane({ state: 'running' })),
                     BV.needsRefresh({ view: 'regression-lane' }, lane({ state: 'queued' })),
                     BV.needsRefresh({ view: 'regression-lane' }, lane({ live: 1 })),
                     BV.needsRefresh({ view: 'regression-lane' }, lane({}))],
            }));
            """
        )
    )
    assert payload == {
        "list": [True, False, False],
        "batch": [True, False, False],
        "lane": [True, True, True, False],
    }


def test_batches_view_never_injects_html_or_shadows_a_shared_helper():
    script = (STATIC_DIR / "batches-view.js").read_text(encoding="utf-8")
    assert ".innerhtml" not in script.lower()
    assert "eval(" not in script.lower()
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    shared = set(re.findall(r"^function (\w+)", util_script, re.MULTILINE))
    shared |= set(re.findall(r"^const ([A-Z][A-Z0-9_]*)\s*=", util_script, re.MULTILINE))
    declared = set(re.findall(r"^\s+function (\w+)", script, re.MULTILINE))
    declared |= set(re.findall(r"^\s+(?:const|let|var) (\w+)\s*=", script, re.MULTILINE))
    assert sorted(declared & shared) == []


def test_runs_table_can_draw_another_pages_games_with_extra_columns_and_its_own_routes():
    setup, script = _table_harness()
    render_rows = _javascript_section(script, "function renderRows", "return { render, renderEmpty")
    payload = _run_node_json(
        f"""
        {setup}
        {render_rows}
        const other = makeNode('div');
        const rows = [
          {{ seed: 'live', status: 'in_progress', global_floor: 4, act: 1, floor: 4, hp: 70, max_hp: 80,
             started_at: 900, has_map: true, game_index: 3, duration_seconds: null,
             ref: {{ kind: 'source', id: 'src-live' }} }},
          {{ seed: 'a', status: 'dead', global_floor: 10, started_at: 100, has_map: true, game_index: 1,
             duration_seconds: 1200, ref: {{ kind: 'source', id: 'src-a' }} }},
          {{ seed: 'b', status: 'win', global_floor: 40, started_at: 200, has_map: false, game_index: 2,
             duration_seconds: 600, ref: {{ kind: 'source', id: 'src-b' }} }},
        ];
        const options = {{
          routeFor: (ref) => `#/batch/-/run/${{ref.kind}}:${{ref.id}}`,
          extraColumns: [
            {{ label: '局号', value: (row) => row.game_index }},
            {{ label: '耗时', value: (row) => (row.duration_seconds === null ? null : `${{row.duration_seconds}}s`) }},
          ],
        }};
        renderRows(other, rows, options);
        const headers = other.querySelectorAll('th').map((th) => th.textContent || th.children.map((c) => c.children.map((s) => s.textContent).join('')).join(''));
        const body = other.querySelectorAll('tr').slice(1);
        body[1].dispatchClick();
        body[0].dispatchClick();
        // sorting re-renders into the SAME container with the same options
        const floorButton = other.querySelectorAll('button')[1];
        floorButton.dispatchClick();
        const sorted = other.querySelectorAll('tr').slice(1);
        sorted[2].dispatchClick();
        console.log(JSON.stringify({{
          headers,
          cells: body.map((tr) => tr.children.map((td) => td.textContent)),
          navigateCalls,
          sortedSeeds: sorted.map((tr) => tr.children[1].textContent),
          trainingContainerUntouched: tableContainer.children.length,
        }}));
        """
    )

    assert payload["headers"][:3] == ["序号", "种子", "状态"]
    assert payload["headers"][-2:] == ["局号", "耗时"]
    live, dead, win = payload["cells"]
    # 序号 seed 状态 推进层数 幕 时间 + the two extra columns + the (header-less) map note cell
    assert live[1] == "live" and live[6] == "3" and live[7] == "—"
    assert dead[6] == "1" and dead[7] == "1200s"
    assert win[6] == "2" and win[8] == "无地图"
    assert payload["navigateCalls"] == [
        "#/batch/-/run/source:src-a",
        "#/batch/-/run/source:src-live",
        "#/batch/-/run/source:src-a",
    ]
    # live row pinned first, then floor descending: b (40), a (10)
    assert payload["sortedSeeds"] == ["live", "b", "a"]
    assert payload["trainingContainerUntouched"] == 0


def test_runs_table_exports_render_rows_that_resets_its_sort_and_targets_a_container():
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    assert "return { render, renderEmpty: renderEmptyTable, renderRows };" in script
    section = _javascript_section(script, "function renderRows", "return { render")
    assert "sortKey = null" in section and "sortDir = 'desc'" in section
    assert "container" in section
    # the training batch table still draws into #runsTable by default
    assert "options.container || byId('runsTable')" in script
    assert "runRoute(state.selectedCohortId, ref)" in script


def _router_harness(batches: list, now: float | None) -> str:
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    return "\n".join(
        [
            _FAKE_VIEW_DOM,
            """
            const calls = [];
            const state = { tree: [], batches: __BATCHES__, batchesNow: __NOW__, tab: 'training',
                            tabResolved: false, catalogOpen: false, selectedCohortId: '' };
            const Tree = { setSelected(id) { calls.push(`tree:${id}`); } };
            const CohortView = {
              async render(id) { calls.push(`cohort:${id}`); },
              renderNoCohort() { calls.push('no-cohort'); },
            };
            const RunView = { stop() { calls.push('run-stop'); }, async render() { calls.push('run-render'); } };
            const BatchesView = {
              stop() { calls.push('batches-stop'); },
              async render(route) { calls.push(`batches:${route.view}:${route.batchId || ''}:${route.lane || ''}`); },
            };
            const window = { STS2Map: { showDashboardPage() { calls.push('dashboard'); } }, BatchesView, RunView };
            const history = {
              replaceState(s, t, hash) { calls.push(`replace:${hash}`); },
              pushState(s, t, hash) { calls.push(`push:${hash}`); },
            };
            """.replace("__BATCHES__", json.dumps(batches)).replace("__NOW__", json.dumps(now)),
            _javascript_section(app_script, "function cohortRoute", "function navigate"),
            # navigate() itself (pushState + applyRoute), then the show/tab helpers and the router
            _javascript_section(app_script, "function navigate", "function firstCohortIdInTree"),
            _javascript_section(app_script, "function firstCohortIdInTree", "async function applyRoute"),
            _javascript_section(app_script, "async function applyRoute", "window.addEventListener('popstate'"),
            """
            function visibility() {
              return {
                tree: byId('cohortTree').hidden, batchView: byId('batchView').hidden,
                catalogView: byId('catalogView').hidden, regressionView: byId('regressionView').hidden,
                bodyTab: byId('workbenchBody').dataset.tab,
                trainingTab: byId('tabTraining').getAttribute('aria-selected'),
                regressionTab: byId('tabRegression').getAttribute('aria-selected'),
                trainingTabIndex: byId('tabTraining').getAttribute('tabindex'),
                regressionTabIndex: byId('tabRegression').getAttribute('tabindex'),
              };
            }
            function take() { return calls.splice(0); }
            """,
        ]
    )


def test_router_opens_the_regression_tab_by_default_only_when_batches_are_active():
    now = 1_800_000_000
    script = """
        (async () => {
          const out = {};
          await applyRoute(parseRoute(''));
          out.firstVisit = take();
          await applyRoute(parseRoute(''));
          out.secondVisit = take();
          console.log(JSON.stringify(out));
        })();
    """
    running = [{"done": False, "created_at": now - 1000}]
    idle = [{"done": True, "created_at": now - 90 * 3600, "updated_at": now - 80 * 3600}]

    with_batch = _run_node_json(_router_harness(running, now) + script)
    without = _run_node_json(_router_harness(idle, now) + script)
    explicit_training = _run_node_json(
        _router_harness(running, now)
        + """
        (async () => {
          await applyRoute(parseRoute('#/training'));
          const out = { calls: take(), tab: state.tab, resolved: state.tabResolved };
          console.log(JSON.stringify(out));
        })();
        """
    )

    # active batch: the first `#/` is replaced by the regression list, training is not rendered
    first = with_batch["firstVisit"]
    assert "replace:#/regression" in first
    assert not any(item.startswith("cohort:") for item in first)
    assert first[-1] == "batches:regression::"
    # later visits to `#/` are the training root (the choice is made once)
    assert "no-cohort" in with_batch["secondVisit"]
    # nothing active: straight to training
    assert "no-cohort" in without["firstVisit"]
    assert not any(item.startswith("replace:") for item in without["firstVisit"])
    # `#/training` never bounces, even with a running batch
    assert "no-cohort" in explicit_training["calls"]
    assert explicit_training["tab"] == "training" and explicit_training["resolved"] is True


def test_router_swaps_tabs_panes_and_stops_polling_when_leaving_regression():
    payload = _run_node_json(
        _router_harness([], None)
        + """
        (async () => {
          const out = {};
          await applyRoute(parseRoute('#/regression/batch/b1/lane/Ironclad'));
          out.lane = { calls: take(), ...visibility(), tab: state.tab };
          await applyRoute(parseRoute('#/regression'));
          out.list = { calls: take(), ...visibility() };
          state.tree = [{ characters: [{ cohorts: [{ cohort_id: 'c1' }] }] }];
          await applyRoute(parseRoute('#/batch/c1'));
          out.training = { calls: take(), ...visibility(), selected: state.selectedCohortId };
          await applyRoute(parseRoute('#/regression/batch/b1'));
          take();
          await applyRoute(parseRoute('#/batch/-/run/source:src1'));
          out.run = { calls: take(), regressionTabStays: state.tab };
          console.log(JSON.stringify(out));
        })();
        """
    )

    lane = payload["lane"]
    assert "batches:regression-lane:b1:Ironclad" in lane["calls"]
    assert "batches-stop" not in lane["calls"]
    assert lane["tab"] == "regression"
    assert (lane["tree"], lane["batchView"], lane["catalogView"], lane["regressionView"]) == (True, True, True, False)
    assert (lane["bodyTab"], lane["trainingTab"], lane["regressionTab"]) == ("regression", "false", "true")
    assert (lane["trainingTabIndex"], lane["regressionTabIndex"]) == ("-1", "0")
    assert payload["list"]["calls"][-1] == "batches:regression::"

    training = payload["training"]
    assert "batches-stop" in training["calls"]
    assert "cohort:c1" in training["calls"] and "tree:c1" in training["calls"]
    assert (training["tree"], training["batchView"], training["regressionView"]) == (False, False, True)
    assert (training["bodyTab"], training["trainingTab"], training["regressionTab"]) == ("training", "true", "false")
    assert training["selected"] == "c1"

    # opening a game from a lane keeps whichever tab you were on and stops the batch polling
    assert "batches-stop" in payload["run"]["calls"] and "run-render" in payload["run"]["calls"]
    assert payload["run"]["regressionTabStays"] == "regression"


def test_tab_buttons_and_keyboard_navigate_between_the_two_tabs():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    handlers = _javascript_section(script, "byId('tabTraining').addEventListener", "byId('baselineCohort')")
    payload = _run_node_json(
        _FAKE_VIEW_DOM
        + """
        const state = { tab: 'training', selectedCohortId: '' };
        const navigateCalls = [];
        function navigate(hash) { navigateCalls.push(hash); }
        function cohortRoute(id) { return `#/batch/${id}`; }
        function regressionRoute() { return '#/regression'; }
        """
        + handlers
        + """
        byId('tabRegression').click();
        state.tab = 'regression';
        byId('tabTraining').click();
        state.selectedCohortId = 'c9';
        byId('tabTraining').click();
        state.tab = 'training';
        byId('workbenchTabs').dispatch('keydown', { key: 'ArrowRight' });
        state.tab = 'regression';
        byId('workbenchTabs').dispatch('keydown', { key: 'ArrowLeft' });
        byId('workbenchTabs').dispatch('keydown', { key: 'Tab' });
        console.log(JSON.stringify({ navigateCalls }));
        """
    )
    assert payload["navigateCalls"] == [
        "#/regression", "#/training", "#/batch/c9", "#/regression", "#/batch/c9",
    ]


def test_bootstrap_loads_the_batch_list_without_letting_it_block_the_workbench():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    bootstrap = _javascript_section(script, "async function bootstrap", "async function reloadAll")
    assert "getJSON('/api/batches').catch(" in bootstrap
    assert "state.batches" in bootstrap
    # the existing three reads are untouched
    for endpoint in ("/api/tree", "/api/cohorts", "/api/catalog"):
        assert f"getJSON('{endpoint}')" in bootstrap


# ---------------------------------------------------------------------------
# 回归/评测 tab: the per-game detail page (game-detail.js)
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "run_workbench"


def _game_payload(name: str, **summary) -> dict:
    """A real per-game payload: what /api/game-detail answers for a trimmed log."""
    from agent.run_workbench.game_detail import build_game_detail, load_game_records
    from agent.run_workbench.localization import Localizer

    detail = build_game_detail(
        load_game_records(_FIXTURE_DIR / f"game_detail_{name}.jsonl"),
        source_name=f"{name}.jsonl", localizer=Localizer(),
    )
    detail["source_id"] = "src_abc"
    detail["regression"] = True
    detail["live"] = False
    detail["summary"] = {**detail["summary"], **summary}
    return detail


def _game_detail_harness() -> str:
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    view_script = (STATIC_DIR / "game-detail.js").read_text(encoding="utf-8")
    return "\n".join(
        [
            _FAKE_VIEW_DOM,
            _javascript_section(util_script, "function element(tag", "function svgElement"),
            _javascript_section(util_script, "function clear(node)", "function setStatus"),
            _javascript_section(util_script, "function formatMissing", "function formatBytes"),
            _javascript_section(util_script, "function renderEmpty", "function setSelectOptions"),
            _javascript_section(app_script, "function cohortRoute", "function navigate"),
            """
            function formatTime(value) { return `T${value}`; }
            const statuses = [];
            function setStatus(message, tone) { statuses.push([message, tone]); }
            const navigateCalls = [];
            function navigate(hash) { navigateCalls.push(hash); }
            const timers = [];
            let nextTimer = 1;
            function setTimeout(fn, ms) {
              const timer = { id: nextTimer++, fn, ms, cleared: false, fired: false };
              timers.push(timer);
              return timer.id;
            }
            function clearTimeout(id) {
              const timer = timers.find((candidate) => candidate.id === id);
              if (timer) timer.cleared = true;
            }
            const pending = () => timers.filter((timer) => !timer.cleared && !timer.fired);
            async function fire(timer) { timer.fired = true; await timer.fn(); }
            const fetches = [];
            let responses = [];
            async function getJSON(url) {
              fetches.push(url);
              const next = responses.shift();
              if (next instanceof Error) throw next;
              return next;
            }
            const window = {};
            // open a <details> the way the browser does: set `open`, fire `toggle`
            function openDetails(node) { node.open = true; node.dispatch('toggle'); }
            const GD_ROUTE = { view: 'regression-game', source: 'src_abc' };
            """,
            view_script,
            "const GD = window.GameDetail;",
        ]
    )


def _gd_script(body: str, **payloads) -> str:
    script = _game_detail_harness() + "\n" + body
    for name, value in payloads.items():
        script = script.replace(f"__{name.upper()}__", json.dumps(value))
    return script


def test_shell_loads_game_detail_after_the_batches_view_and_has_the_run_page_link():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/([\w.-]+)"', html)
    assert scripts.index("game-detail.js") > scripts.index("batches-view.js")
    assert scripts.index("game-detail.js") < scripts.index("map.js") and scripts[-1] == "map.js"
    parser = _ShellParser()
    parser.feed(html)
    assert "runGameLink" in parser.ids
    # the link sits with the run page's own actions, hidden until a regression game opens
    assert re.search(r'id="runGameLink"[^>]*\bhidden\b', html)
    assert parser.ids.index("runGameLink") < parser.ids.index("mapBackButton")


def test_game_detail_script_never_injects_html_or_shadows_a_shared_helper():
    script = (STATIC_DIR / "game-detail.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script and "outerHTML" not in script
    shared = ("function element(", "function clear(", "function getJSON(", "function setStatus(",
              "function byId(", "function formatTime(", "function renderEmpty(")
    for declaration in shared:
        assert declaration not in script, declaration
    assert script.startswith("'use strict';")
    assert "window.GameDetail = (() => {" in script


def test_hash_router_parses_and_builds_game_routes():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    routing = _javascript_section(script, "function cohortRoute", "function navigate")
    payload = _run_node_json(
        f"""
        {routing}
        console.log(JSON.stringify({{
          game: parseRoute('#/regression/game/src_abc123'),
          byRun: parseRoute(gameRoute('run:pfr-1')),
          built: [gameRoute('src_abc'), gameRoute('run:pfr-1')],
          list: parseRoute('#/regression'),
          lane: parseRoute('#/regression/batch/b1/lane/Ironclad'),
          missing: parseRoute('#/regression/game/'),
          region: isRegressionView('regression-game'),
        }}));
        """
    )
    assert payload["game"] == {"view": "regression-game", "source": "src_abc123"}
    assert payload["byRun"] == {"view": "regression-game", "source": "run:pfr-1"}
    assert payload["built"] == ["#/regression/game/src_abc", "#/regression/game/run%3Apfr-1"]
    assert payload["list"] == {"view": "regression"}
    assert payload["lane"] == {"view": "regression-lane", "batchId": "b1", "lane": "Ironclad"}
    assert payload["missing"] == {"view": "root"}
    assert payload["region"] is True


def test_router_renders_the_game_page_in_the_regression_tab_and_stops_polling_elsewhere():
    harness = _router_harness([], None).replace(
        "const window = { STS2Map: { showDashboardPage() { calls.push('dashboard'); } }, BatchesView, RunView };",
        """const GameDetail = {
              stop() { calls.push('game-stop'); },
              async render(route) { calls.push(`game:${route.view}:${route.source}`); },
            };
            const window = { STS2Map: { showDashboardPage() { calls.push('dashboard'); } }, BatchesView, RunView, GameDetail };""",
    )
    payload = _run_node_json(
        harness
        + """
        (async () => {
          const out = {};
          await applyRoute(parseRoute('#/regression/game/src_abc'));
          out.game = { calls: take(), tab: state.tab, regressionView: byId('regressionView').hidden,
                       cohortTree: byId('cohortTree').hidden };
          await applyRoute(parseRoute('#/regression/batch/b1'));
          out.batch = take();
          await applyRoute(parseRoute('#/batch/c1'));
          out.training = take();
          console.log(JSON.stringify(out));
        })();
        """
    )
    game = payload["game"]
    # the game page polls like a batch page: the batch polling is stopped, the game's own is not
    assert "batches-stop" in game["calls"] and "game:regression-game:src_abc" in game["calls"]
    assert "game-stop" not in game["calls"]
    assert (game["tab"], game["regressionView"], game["cohortTree"]) == ("regression", False, True)
    assert "game-stop" in payload["batch"] and "batches:regression-batch:b1:" in payload["batch"]
    assert "game-stop" in payload["training"]


def test_game_page_header_route_strip_and_deck():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const buttons = collect(body, (n) => n.tag === 'button' && n.className === 'quiet-button');
              buttons[0].click();
              buttons[1].click();
              const strips = collect(body, (n) => n.className === 'gd-route-act');
              const taken = collect(body, (n) => n.className.includes('gd-node-taken'));
              const alt = collect(body, (n) => n.className.includes('gd-node-alt'));
              console.log(JSON.stringify({
                fetches,
                statuses,
                head: allText(collect(body, (n) => n.className === 'section-heading')[0]),
                facts: collect(body, (n) => n.className === 'batch-fact').map((n) => n.children.map((c) => c.textContent)),
                solver: allText(collect(body, (n) => n.className === 'gd-line')[0]),
                navigateCalls,
                actTitles: strips.map((s) => allText(s.children[0])),
                cells: strips.map((s) => collect(s, (n) => n.className === 'gd-route-cell').length),
                taken: taken.length, alt: alt.length,
                firstCell: allText(collect(body, (n) => n.className === 'gd-route-cell')[0]),
                deckHead: allText(collect(body, (n) => n.className === 'section-heading').find((n) => allText(n).includes('最终牌组'))),
                deckRows: collect(body, (n) => n.className === 'gd-deck-type').map(allText),
                upgraded: collect(body, (n) => n.className.includes('gd-chip-upgraded')).map(allText),
                timeline: allText(collect(body, (n) => n.className === 'gd-delta-list')[0]),
              }));
            })();
            """,
            detail=_game_payload("full_run", batch_id="reg-1", lane="Ironclad", game_index=2,
                                 games_total=5, duration_seconds=900),
        )
    )

    assert payload["fetches"] == ["/api/game-detail?source=src_abc"]
    assert payload["statuses"][-1][0] == "已载入对局详情"
    facts = dict(payload["facts"])
    assert facts["批次"] == "reg-1" and facts["通道"] == "Ironclad" and facts["局号"] == "2 / 5"
    assert facts["最远"].startswith("A1F17") and facts["生命"] == "80 → 0"
    assert facts["耗时"] == "15 分钟" and facts["牌组 / 遗物"] == "13 / 4"
    assert "Ironclad" in payload["head"] and "run_5" in payload["head"]
    assert payload["solver"].startswith("求解器") and "计划 10 次" in payload["solver"]
    # back goes to the lane list (no browser history here), the map view to the old run page
    assert payload["navigateCalls"] == ["#/regression/batch/reg-1/lane/Ironclad", "#/batch/-/run/source:src_abc"]
    assert payload["actTitles"] == ["第 1 幕"]
    assert payload["cells"] == [7]
    assert payload["taken"] == 7 and payload["alt"] >= 2   # offered-but-not-taken nodes are greyed
    assert "F1" in payload["firstCell"] and "战斗" in payload["firstCell"]
    assert "13 张" in payload["deckHead"]
    rows = payload["deckRows"]
    assert any(row.startswith("攻击（4）") for row in rows)
    assert any(row.startswith("技能（7）") and "防御 ×4" in row for row in rows)
    assert any(row.startswith("遗物（4）") and "燃烧之血" in row for row in rows)
    assert any(row.startswith("药水（1）") and "流动铜液" in row for row in rows)
    assert "巨像+" in payload["timeline"] and "移除 打击×3" in payload["timeline"]


def test_game_page_rooms_are_collapsed_until_opened_and_group_by_act():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const rooms = collect(body, (n) => n.tag === 'details' && n.className === 'gd-room');
              const acts = collect(body, (n) => n.className === 'gd-act-header').map(allText);
              const before = rooms.map((r) => collect(r, (n) => n.className.includes('gd-body')).length);
              const summaries = rooms.map((r) => allText(r.children[0]));
              openDetails(rooms[1]);   // A1F2: a fight with a card reward
              const afterOpen = collect(rooms[1], (n) => n.className.includes('gd-body')).length;
              const roomBody = allText(rooms[1].children[1]);
              const turns = collect(rooms[1], (n) => n.tag === 'details' && n.className === 'gd-turn');
              const turnBodiesBefore = collect(rooms[1], (n) => n.className.includes('gd-turn-body')).length;
              openDetails(rooms[1]);   // a second toggle never builds the body twice
              console.log(JSON.stringify({
                n: rooms.length, acts, before, afterOpen, summaries, roomBody,
                turns: turns.length, turnBodiesBefore,
                opened: collect(rooms[1], (n) => n.className.includes('gd-body') && n.className.includes('gd-room-body')).length,
              }));
            })();
            """,
            detail=_game_payload("full_run"),
        )
    )

    assert payload["n"] == 7
    assert payload["acts"] == ["第 1 幕 · Overgrowth · Boss：仪式兽"]
    assert payload["before"] == [0] * 7                   # nothing is built until a room is opened
    assert payload["afterOpen"] >= 1 and payload["opened"] == 1
    assert "A1F1" in payload["summaries"][0] and "事件" in payload["summaries"][0]
    assert "A1F2" in payload["summaries"][1] and "普通战斗" in payload["summaries"][1]
    assert "4 回合 · 计划 4" in payload["summaries"][1]
    assert "阵亡" in payload["summaries"][6] and "−64" in payload["summaries"][6]
    # an opened fight lists its turns, each collapsed too
    assert payload["turns"] == 4 and payload["turnBodiesBefore"] == 0
    assert "卡牌奖励" in payload["roomBody"] and "选择了" in payload["roomBody"]


def test_game_page_out_of_combat_rooms_show_offered_versus_chosen():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const rooms = collect(body, (n) => n.tag === 'details' && n.className === 'gd-room');
              const byLabel = (label) => rooms.find((r) => allText(r.children[0]).includes(label));
              ['A1F2', 'A1F7', 'A1F8', 'A1F9'].forEach((label) => openDetails(byLabel(label)));
              const chips = (room, tone) => collect(room.children[1], (n) => n.className.includes(`gd-chip-${tone}`)).map(allText);
              console.log(JSON.stringify({
                reward: { taken: chips(byLabel('A1F2'), 'taken'), alt: chips(byLabel('A1F2'), 'alt'),
                          text: allText(byLabel('A1F2').children[1]) },
                event: allText(byLabel('A1F7').children[1]),
                eventTaken: collect(byLabel('A1F7'), (n) => n.className.includes('gd-option-taken')).length,
                rest: { taken: chips(byLabel('A1F8'), 'taken'), alt: chips(byLabel('A1F8'), 'alt'),
                        text: allText(byLabel('A1F8').children[1]) },
                shop: { taken: chips(byLabel('A1F9'), 'taken'), alt: chips(byLabel('A1F9'), 'alt').length,
                        text: allText(byLabel('A1F9').children[1]) },
              }));
            })();
            """,
            detail=_game_payload("full_run"),
        )
    )

    reward = payload["reward"]
    assert len(reward["taken"]) == 1 and reward["taken"][0].startswith("残酷")
    assert len(reward["alt"]) == 2
    assert "选择了 残酷" in reward["text"]
    assert "事件：木雕" in payload["event"] and "事件中的选牌" in payload["event"]
    assert payload["eventTaken"] == 2          # chosen on both pages of the event
    rest = payload["rest"]
    assert rest["taken"] == ["锻造"] and rest["alt"] == ["休息"]
    assert "升级了 巨像" in rest["text"]
    shop = payload["shop"]
    assert [c.split(" ·")[0] for c in shop["taken"] if "·" in c] == ["被遗忘的仪式"]
    assert [c for c in shop["taken"] if c.startswith("移除")] == ["移除 打击"] * 3
    assert shop["alt"] > 5                      # the stock that was not bought
    assert "金币 266 → 4" in shop["text"] and "移除卡牌价格 75 金" in shop["text"]


def test_game_page_combat_turn_lists_candidates_taken_actions_and_the_plan():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const room = collect(body, (n) => n.tag === 'details' && n.className === 'gd-room')
                .find((r) => allText(r.children[0]).includes('A2F8'));
              openDetails(room);
              const turns = collect(room, (n) => n.tag === 'details' && n.className === 'gd-turn');
              const first = turns[0];
              const fifth = turns.find((t) => allText(t.children[0]).includes('第 5 回合'));
              openDetails(first);
              openDetails(fifth);
              const rows = (turn) => collect(turn, (n) => n.tag === 'tr' && n.className.startsWith('gd-step'));
              const stepRow = (row) => {
                const cells = row.children;
                return {
                  n: allText(cells[0]), state: allText(cells[1]),
                  chips: collect(cells[2], (n) => n.className.startsWith('gd-chip')).map((n) => [allText(n), n.className]),
                  taken: collect(cells[2], (n) => n.className.includes('gd-chip-taken')).map(allText),
                  targetTaken: collect(cells[2], (n) => n.className.includes('gd-target-taken')).map(allText),
                  actual: allText(cells[3]), source: allText(cells[4]), result: allText(cells[5]),
                };
              };
              console.log(JSON.stringify({
                summaries: turns.map((t) => allText(t.children[0])),
                firstSteps: rows(first).map(stepRow),
                firstPlan: collect(first, (n) => /^gd-plan( |$)/.test(n.className)).map(allText),
                fifthDivergence: collect(fifth, (n) => n.className.startsWith('gd-divergence-')).map(allText),
                fifthPlans: collect(fifth, (n) => /^gd-plan( |$)/.test(n.className)).map(allText),
                fifthPlanActions: collect(fifth, (n) => n.className.startsWith('gd-plan-action ')).map(allText),
                fifthSources: rows(fifth).map((r) => allText(r.children[4])),
                enemies: collect(first, (n) => n.tag === 'table' && n.className.includes('gd-enemies')).map(allText),
              }));
            })();
            """,
            detail=_game_payload("replan"),
        )
    )

    first_steps = payload["firstSteps"]
    assert [s["actual"] for s in first_steps][:3] == ["打出 防御", "打出 挑衅 → 蜂群术士", "打出 剑柄打击 → 蜂群术士"]
    assert len(first_steps) == 8
    # every step highlights exactly the one chip that was taken
    for step in first_steps:
        assert len(step["taken"]) == 1, step
    potion_step = first_steps[3]
    assert potion_step["actual"].startswith("使用药水") and "攻击药水" in potion_step["actual"]
    assert first_steps[4]["actual"].startswith("选牌") and first_steps[4]["source"] == "选牌"
    # candidate chips: more than the one taken, unplayable cards marked as such
    play = first_steps[0]
    assert len(play["chips"]) > 3 and play["state"].startswith("能量 5")
    assert "结束回合" in [text for text, _cls in play["chips"]]
    assert [text for text, cls in play["chips"] if "gd-chip-dim" in cls] == ["星界脉冲"]   # the 0-cost card the engine will not play
    assert first_steps[-1]["actual"] == "结束回合"
    assert any(row["targetTaken"] for row in first_steps if "→" in row["actual"])
    assert all(row["source"] == "计划 #1" for row in first_steps if row["actual"] != first_steps[4]["actual"])
    # the plan: projected HP, search stats, each planned action tagged executed
    plan = payload["firstPlan"][0]
    assert "求解器计划 #1" in plan and "预计结束生命 67" in plan and "已执行 7 / 7" in plan
    assert "用时 20.1 秒" in plan and "累计节点 21439" in plan and "边界 None" in plan
    assert plan.count("已执行 #") == 7
    # the diverged turn: markers, both plans, and which plan drove which step
    assert "第 5 回合" in payload["summaries"][4] and "重新规划" in payload["summaries"][4] \
        and "打完未结束" in payload["summaries"][4]
    assert any("5 个动作都打完了" in text for text in payload["fifthDivergence"])
    assert len(payload["fifthPlans"]) == 2
    assert "打完但回合未结束" in payload["fifthPlans"][0] and "计划完成" in payload["fifthPlans"][1]
    assert payload["fifthSources"] == ["计划 #1"] * 5 + ["计划 #2"]
    assert len(payload["fifthPlanActions"]) == 6
    assert "蜂群术士" in payload["enemies"][0] and "攻击" in payload["enemies"][0]


def test_game_page_shows_plan_errors_heuristic_steps_and_filters_rooms():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const room = collect(body, (n) => n.tag === 'details' && n.className === 'gd-room')[0];
              openDetails(room);
              const turn = collect(room, (n) => n.tag === 'details' && n.className === 'gd-turn')
                .find((t) => allText(t.children[0]).includes('第 6 回合'));
              openDetails(turn);
              const filters = collect(body, (n) => n.className === 'quiet-button gd-filter');
              const visible = () => collect(body, (n) => n.tag === 'details' && n.className === 'gd-room')
                .filter((r) => !r.hidden).length;
              const out = { all: visible(), buttons: filters.map((f) => f.textContent) };
              filters[4].click();       // 偏离计划
              out.diverged = visible();
              out.pressed = filters.map((f) => f.getAttribute('aria-pressed'));
              filters[2].click();       // 精英/首领
              out.elite = visible();
              filters[3].click();       // 事件/商店/篝火
              out.other = visible();
              out.headerHidden = collect(body, (n) => n.className === 'gd-act-header').map((h) => h.hidden);
              console.log(JSON.stringify({
                ...out,
                summary: allText(turn.children[0]),
                divergence: collect(turn, (n) => n.className.startsWith('gd-divergence-')).map(allText),
                plans: collect(turn, (n) => /^gd-plan( |$)/.test(n.className)).map(allText),
                rejected: collect(turn, (n) => n.className === 'gd-rejected').map(allText),
                sources: collect(turn, (n) => n.tag === 'tr' && n.className.startsWith('gd-step'))
                  .map((r) => [allText(r.children[4]), r.className]),
              }));
            })();
            """,
            detail=_game_payload("plan_error"),
        )
    )

    assert payload["buttons"] == ["全部", "战斗", "精英/首领", "事件/商店/篝火", "偏离计划"]
    assert payload["all"] == 1 and payload["diverged"] == 1 and payload["elite"] == 1 and payload["other"] == 0
    assert payload["pressed"] == ["false", "false", "false", "false", "true"]
    assert payload["headerHidden"] == [True]       # a filter that hides every room hides its act header too
    assert "求解器出错" in payload["summary"] and "重新规划" in payload["summary"]
    assert any("求解器出错，改用启发式" in text and "Cannot fork with pending Power" in text
               for text in payload["divergence"])
    assert any(text.startswith("求解器计划 #1 求解器出错") for text in payload["plans"])
    assert any("Cannot fork with pending Power" in text for text in payload["rejected"])
    assert [s[0] for s in payload["sources"]][:2] == ["启发式", "计划 #2"]
    assert "gd-step-heuristic" in payload["sources"][0][1]


def test_game_page_route_strip_opens_the_room_it_led_to():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const enter = collect(body, (n) => n.className === 'gd-route-enter');
              const rooms = collect(body, (n) => n.tag === 'details' && n.className === 'gd-room');
              const before = rooms.map((r) => !!r.open);
              enter[0].click();         // the first choice led to A1F2
              console.log(JSON.stringify({
                n: enter.length, labels: enter.map((n) => n.textContent),
                before, after: rooms.map((r) => !!r.open),
                built: collect(rooms[1], (n) => n.className.includes('gd-room-body')).length,
              }));
            })();
            """,
            detail=_game_payload("full_run"),
        )
    )
    assert payload["before"] == [False] * 7
    assert payload["after"][1] is True and sum(payload["after"]) == 1
    assert payload["built"] == 1            # opened from the strip without waiting for the toggle event
    assert payload["n"] >= 5
    assert "→ 事件" in payload["labels"]    # a "?" node shows what it turned into


def test_game_page_refreshes_a_live_game_and_leaves_an_unchanged_page_alone():
    live = {**_game_payload("replan"), "live": True}
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__LIVE__];
              await GD.render(GD_ROUTE);
              const out = { armed: pending().map((t) => t.ms) };
              const room = collect(byId('regressionBody'), (n) => n.tag === 'details' && n.className === 'gd-room')[0];
              openDetails(room);
              responses = [__LIVE__];
              await fire(pending()[0]);
              // nothing changed: the open room is still the same open node
              out.sameRoom = collect(byId('regressionBody'), (n) => n.tag === 'details' && n.className === 'gd-room')[0] === room;
              out.fetchesAfterRefresh = fetches.length;
              out.armedAgain = pending().map((t) => t.ms);
              responses = [new Error('boom')];
              await fire(pending()[0]);
              out.notice = byId('regressionNotice').textContent;
              out.stillArmed = pending().map((t) => t.ms);
              GD.stop();
              out.afterStop = pending().length;
              out.noticeAfterStop = [byId('regressionNotice').hidden, byId('regressionNotice').textContent];
              console.log(JSON.stringify(out));
            })();
            """,
            live=live,
        )
    )
    assert payload["armed"] == [30000]
    assert payload["sameRoom"] is True and payload["fetchesAfterRefresh"] == 2
    assert payload["armedAgain"] == [30000]
    assert "自动刷新失败：boom" in payload["notice"] and payload["stillArmed"] == [30000]
    assert payload["afterStop"] == 0 and payload["noticeAfterStop"] == [True, ""]


def test_game_page_does_not_poll_a_finished_game_and_addresses_runs_by_id():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [__DETAIL__];
              await GD.render({ view: 'regression-game', source: 'run:pfr-1 x' });
              console.log(JSON.stringify({ fetches, pending: pending().length,
                endpoint: GD.endpointFor({ source: 'src_a/b' }) }));
            })();
            """,
            detail=_game_payload("plan_error"),
        )
    )
    assert payload["fetches"] == ["/api/game-detail?run=pfr-1%20x"]
    assert payload["pending"] == 0
    assert payload["endpoint"] == "/api/game-detail?source=src_a%2Fb"


def test_game_page_unreadable_game_says_so_and_offers_the_way_back():
    payload = _run_node_json(
        _gd_script(
            """
            (async () => {
              responses = [new Error('unknown source id: src_abc')];
              await GD.render(GD_ROUTE);
              const body = byId('regressionBody');
              const buttons = collect(body, (n) => n.tag === 'button');
              buttons[0].click();
              console.log(JSON.stringify({
                text: allText(body), navigateCalls, statuses: statuses.slice(-1), pending: pending().length,
              }));
            })();
            """
        )
    )
    assert "对局详情读取失败：unknown source id: src_abc" in payload["text"]
    assert payload["navigateCalls"] == ["#/regression"]
    assert payload["statuses"][-1][1] == "error" and payload["pending"] == 0


def test_game_page_name_and_effect_formatting():
    payload = _run_node_json(
        _gd_script(
            """
            const loc = { card_id: { BASH: '痛击' }, card: { Strike: '打击' }, monster: { Louse: '虱虫' } };
            console.log(JSON.stringify({
              card: [GD.cardName(loc, { id: 'CARD.BASH', name: 'Bash' }),
                     GD.cardName(loc, { id: 'CARD.BASH', name: 'Bash', upgraded: true }),
                     GD.cardName(loc, { name: 'Strike' }), GD.cardName(loc, { name: 'Odd Card' }),
                     GD.cardName(null, { id: 'CARD.X', name: 'X' }), GD.cardName(loc, null)],
              effects: GD.formatEffects(loc, {
                enemy_hp: [{ name: 'Louse', delta: -8 }], block: { delta: 5 }, hp: { delta: -3 },
                spans_enemy_turn: true }),
              noEffects: GD.formatEffects(loc, null),
              search: [GD.formatSearchTime(400), GD.formatSearchTime(2500), GD.formatSearchTime(120000), GD.formatSearchTime(null)],
              div: [
                GD.divergenceText({ kind: 'truncated', plan: 0, executed: 1, planned: 3, reason: 'card_missing' }),
                GD.divergenceText({ kind: 'mismatch', step_n: 0, expected: { card_name: 'Strike' }, actual: { card_name: 'Defend' } }),
                GD.divergenceText({ kind: 'refused', step_n: 1, message: 'nope' }),
                GD.divergenceText({ kind: 'heuristic', steps: 2 }),
                GD.divergenceText({ kind: 'weird' }),
              ],
            }));
            """
        )
    )
    assert payload["card"] == ["痛击", "痛击+", "打击", "Odd Card", "X", "？"]
    assert payload["effects"] == "虱虫 -8 · 格挡 +5 · 生命 -3 · （含敌方回合）"
    assert payload["noEffects"] == ""
    assert payload["search"] == ["400 毫秒", "2.5 秒", "2.0 分钟", "—"]
    truncated, mismatch, refused, heuristic, weird = payload["div"]
    assert "计划 #1 中断" in truncated and "1/3" in truncated and "手牌里没有计划要打的牌" in truncated
    assert "步骤 1 偏离计划" in mismatch and "Strike" in mismatch and "Defend" in mismatch
    assert "被引擎拒绝：nope" in refused and "2 步由启发式出牌" in heuristic and weird == "weird"


def test_run_page_links_to_the_game_detail_only_for_a_game_opened_from_the_regression_tab():
    payload = _run_node_json(
        f"""
        {_run_view_harness()}
        const navigateCalls = [];
        function navigate(hash) {{ navigateCalls.push(hash); }}
        {_javascript_section((STATIC_DIR / "app.js").read_text(encoding="utf-8"), "function gameRoute", "function isRegressionView")}
        (async () => {{
          const out = {{}};
          const link = () => byId('runGameLink');
          reset();
          responses = [endedPayload];
          await window.RunView.render('-', {{ kind: 'source', id: 'src1' }});
          out.fromLane = {{ hidden: link().hidden }};
          link().onclick();
          responses = [endedPayload];
          await window.RunView.render('-', {{ kind: 'run', id: 'pfr-9' }});
          link().onclick();
          responses = [endedPayload];
          await window.RunView.render('c1', {{ kind: 'source', id: 'src1' }});
          out.fromCohort = {{ hidden: link().hidden, onclick: link().onclick }};
          responses = [endedPayload];
          await window.RunView.render('-', {{ kind: 'source', id: '' }});
          out.noRef = {{ hidden: link().hidden }};
          out.navigateCalls = navigateCalls;
          console.log(JSON.stringify(out));
        }})();
        """
    )
    assert payload["fromLane"] == {"hidden": False}
    assert payload["fromCohort"] == {"hidden": True, "onclick": None}
    assert payload["noRef"] == {"hidden": True}
    assert payload["navigateCalls"] == ["#/regression/game/src1", "#/regression/game/run%3Apfr-9"]


def test_runs_table_extra_columns_may_render_a_node_instead_of_text():
    script = (STATIC_DIR / "runs-table.js").read_text(encoding="utf-8")
    assert "typeof column.render !== 'function'" in script
    assert "column.value(row)" in script    # the text form keeps working
