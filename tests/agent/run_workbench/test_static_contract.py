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
        "mapDecisionPopover",
        "mapDecisionTitle",
        "mapDecisionBody",
        "actSummary",
        "selectedNodeSummary",
    ]
    assert all(item in parser.ids for item in required_ids)
    layout_order = [
        "workbenchStatus",
        "sourceFile",
        "versionFilter",
        "characterFilter",
        "currentCohort",
        "baselineCohort",
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
    assert '<nav id="actTabs" class="act-tabs" role="tablist"' in html
    assert "训练进度" in html
    assert "正在读取训练记录…" in html
    assert not hasattr(viewer, "HTML")


def test_filter_panel_orders_scope_before_cohorts():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    panel = html[
        html.index('<section class="panel filter-panel"') : html.index(
            '<section class="metrics-section"'
        )
    ]
    positions = [
        panel.index('id="versionFilter"'),
        panel.index('id="characterFilter"'),
        panel.index('id="currentCohort"'),
        panel.index('id="baselineCohort"'),
        panel.index('id="validityFilter"'),
    ]

    assert positions == sorted(positions)


def test_filter_panel_uses_semantic_grid_spans_at_responsive_breakpoints():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    desktop = css[: css.index("@media")]
    tablet_start = css.index("@media (max-width: 760px)")
    tablet = css[tablet_start : css.index("@media", tablet_start + 1)]
    mobile_start = css.index("@media (max-width: 480px)")
    mobile = css[mobile_start:]

    assert re.search(
        r"\.filter-field-scope,\s*\.filter-field-cohort\s*"
        r"\{[^}]*grid-column:\s*span 6;",
        desktop,
        re.DOTALL,
    )
    assert re.search(
        r"\.filter-field-validity\s*"
        r"\{[^}]*grid-column:\s*span 3;",
        desktop,
        re.DOTALL,
    )
    assert re.search(
        r"\.filter-field-validity\s*"
        r"\{[^}]*grid-column:\s*span 6;",
        tablet,
        re.DOTALL,
    )
    assert re.search(
        r"\.filter-grid\s+\.filter-field\s*(?:,\s*[^{}]+)?\s*"
        r"\{[^}]*grid-column:\s*1\s*/\s*-1;",
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
        function decision(kind, prefix) {{
          const first = {{ id: `${{prefix}}-1`, label: `${{prefix}} 选项一`, effect: `${{prefix}} 效果一`, selected: true }};
          const second = {{ id: `${{prefix}}-2`, label: `${{prefix}} 选项二`, effect: `${{prefix}} 效果二`, selected: false }};
          return {{ kind, selected_id: first.id, selected_label: first.label, options: [first, second], evidence: 'recorded' }};
        }}
        {helpers}
        const node = {{ visited: true, decisions: [decision('event', '事件'), decision('card_reward', '卡牌')] }};
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

    for expected in (
        "事件：事件 选项一",
        "事件 效果一",
        "事件 选项一",
        "事件 选项二",
        "卡：卡牌 选项一",
        "卡牌 效果一",
        "卡牌 选项一",
        "卡牌 选项二",
    ):
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


def test_baseline_default_uses_server_descriptor_without_client_axis_logic():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
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
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function filterValuesFromCohorts"
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
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    help_section = _javascript_section(
        script, "function comparisonAxisLabel", "function updateCohortOptions"
    )

    for wording in (
        "元数据不完整，仅展示本批次",
        "当前批次可查看，但暂无可直接比较的基线",
        "当前与基线不可直接比较",
        "missing_axes",
        "mixed_axes",
        "invalid_axes",
        "版本来源：${sourceLabel}",
        "命令行",
        "环境变量",
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
    assert "currentHelp.textContent}" not in help_section
    assert 'select id="currentCohort" aria-describedby="currentHelp"' in html
    assert 'select id="baselineCohort" aria-describedby="baselineHelp"' in html
    assert 'id="currentHelp"' in html and 'id="baselineHelp"' in html
    assert html.count('aria-live="polite"') >= 5
    assert "当前批次可查看，但暂无可直接比较的基线" in html


def test_cohort_options_preserve_manual_choice_and_only_default_on_current_change():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function filterValuesFromCohorts"
    )
    selector = _javascript_section(
        script, "function defaultBaselineCohortId", "function resetMetrics"
    )
    listener = _javascript_section(script, "function filterChanged", "bootstrap();")

    assert (
        "function updateCohortOptions({ chooseDefaults = false, currentChanged = false } = {})"
        in selector
    )
    assert "updateCohortOptions({ currentChanged: true })" in listener
    assert (
        "updateCohortHelp(currentCohortDescriptor(), byId('baselineCohort').value)"
        in listener
    )
    assert "nearestDistinctCohortId" not in selector

    payload = _run_node_json(
        f"""
        const nodes = {{
          currentCohort: {{ value: '', optionValues: [] }},
          baselineCohort: {{ value: '', optionValues: [] }},
          currentHelp: {{ textContent: '' }},
          baselineHelp: {{ textContent: '' }},
        }};
        const byId = (id) => nodes[id];
        const formatTime = (value) => `time-${{value}}`;
        let candidates = [];
        const filteredCohorts = () => candidates;
        function setSelectOptions(select, options, emptyLabel, preferred) {{
          select.optionValues = options.map((option) => option.value);
          select.emptyLabel = emptyLabel;
          select.value = '';
          if (preferred && options.some((option) => option.value === preferred)) {{
            select.value = preferred;
          }}
        }}
        {identity}
        {selector}
        const ready = {{ ready: true, missing_axes: [], mixed_axes: [], invalid_axes: [] }};
        const cohort = (id, defaultId, extra = {{}}) => ({{
          cohort_id: id,
          label: `cohort-${{id}}`,
          run_count: 2,
          latest_at: id.charCodeAt(0),
          filters: {{ game_version_source: 'cli' }},
          comparison_readiness: ready,
          default_baseline_cohort_id: defaultId,
          ...extra,
        }});
        const a = cohort('a', 'b');
        const b = cohort('b', 'a');
        const c = cohort('c', null);
        candidates = [a, b, c];

        updateCohortOptions({{ chooseDefaults: true }});
        const initial = {{
          current: nodes.currentCohort.value,
          baseline: nodes.baselineCohort.value,
          currentOptions: [...nodes.currentCohort.optionValues],
          baselineOptions: [...nodes.baselineCohort.optionValues],
        }};

        nodes.baselineCohort.value = 'c';
        updateCohortOptions();
        const manual = nodes.baselineCohort.value;
        const manualHelp = nodes.baselineHelp.textContent;

        nodes.baselineCohort.value = '';
        updateCohortOptions();
        const blank = nodes.baselineCohort.value;
        const helpOnce = nodes.currentHelp.textContent;
        updateCohortOptions();
        const helpTwice = nodes.currentHelp.textContent;

        nodes.baselineCohort.value = 'c';
        candidates = [a, b];
        updateCohortOptions();
        const staleManual = nodes.baselineCohort.value;

        candidates = [a, b, c];
        nodes.currentCohort.value = 'b';
        nodes.baselineCohort.value = 'c';
        updateCohortOptions({{ currentChanged: true }});
        const explicitCurrentChange = {{
          current: nodes.currentCohort.value,
          baseline: nodes.baselineCohort.value,
        }};

        nodes.currentCohort.value = 'missing';
        nodes.baselineCohort.value = 'c';
        updateCohortOptions();
        const implicitCurrentChange = {{
          current: nodes.currentCohort.value,
          baseline: nodes.baselineCohort.value,
        }};

        candidates = [a, c];
        nodes.currentCohort.value = 'a';
        nodes.baselineCohort.value = '';
        updateCohortOptions({{ chooseDefaults: true }});
        const filteredServerDefault = nodes.baselineCohort.value;

        console.log(JSON.stringify({{
          initial,
          manual,
          manualHelp,
          blank,
          helpStable: helpOnce === helpTwice,
          helpOnce,
          staleManual,
          explicitCurrentChange,
          implicitCurrentChange,
          filteredServerDefault,
        }}));
        """
    )

    assert payload["initial"] == {
        "current": "a",
        "baseline": "b",
        "currentOptions": ["a", "b", "c"],
        "baselineOptions": ["b", "c"],
    }
    assert payload["manual"] == "c"
    assert payload["manualHelp"] == "已选择基线；服务端将校验口径并提供精确原因"
    assert payload["blank"] == ""
    assert payload["helpStable"] is True
    assert payload["helpOnce"].count("版本来源：命令行") == 1
    assert payload["staleManual"] == ""
    assert payload["explicitCurrentChange"] == {"current": "b", "baseline": "a"}
    assert payload["implicitCurrentChange"] == {"current": "a", "baseline": "b"}
    assert payload["filteredServerDefault"] == ""


def test_version_filter_cascades_character_and_cohort_candidates():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    filters = _javascript_section(
        script, "function setSelectOptions", "function defaultBaselineCohortId"
    )
    cohort_options = _javascript_section(
        script, "function updateCohortOptions", "function resetMetrics"
    )
    bootstrap = _javascript_section(script, "async function bootstrap", "function filterChanged")
    listeners = _javascript_section(script, "function filterChanged", "bootstrap();")

    assert bootstrap.index("populateAxisFilter('versionFilter'") < bootstrap.index(
        "updateCharacterFilterOptions()"
    ) < bootstrap.index("updateCohortOptions({ chooseDefaults: true })")
    version_handler = _javascript_section(
        listeners, "function versionFilterChanged", "byId('characterFilter')"
    )
    assert version_handler.index("updateCharacterFilterOptions()") < version_handler.index(
        "updateCohortOptions({ chooseDefaults: true })"
    ) < version_handler.index("refreshMetrics()")
    assert (
        "byId('versionFilter').addEventListener('change', versionFilterChanged);"
        in listeners
    )
    assert "byId('characterFilter').addEventListener('change', filterChanged);" in listeners
    assert "byId('validityFilter').addEventListener('change', filterChanged);" in listeners

    payload = _run_node_json(
        f"""
        function makeSelect(value = '') {{
          return {{
            value,
            children: [],
            replaceChildren() {{ this.children = []; this.value = ''; }},
            append(option) {{
              this.children.push(option);
              if (this.children.length === 1) this.value = option.value;
            }},
          }};
        }}
        const nodes = {{
          versionFilter: makeSelect(),
          characterFilter: makeSelect(),
          validityFilter: makeSelect(),
          currentCohort: makeSelect(),
          baselineCohort: makeSelect(),
        }};
        const byId = (id) => nodes[id];
        const clear = (node) => node.replaceChildren();
        const element = (tag, options = {{}}) => ({{
          tag,
          textContent: options.text === undefined ? '' : String(options.text),
          value: String(options.attrs.value),
        }});
        const formatTime = (value) => `time-${{value}}`;
        const updateCohortHelp = () => {{}};
        const defaultBaselineCohortId = () => '';
        const state = {{ cohorts: [] }};
        {filters}
        {cohort_options}

        const cohort = (id, version, character, technical = 0) => ({{
          cohort_id: id,
          label: id,
          run_count: 2,
          technical_count: technical,
          latest_at: id.length,
          filters: {{ game_version: version, character }},
        }});
        state.cohorts = [
          cohort('v1-iron', 'v1', 'Ironclad'),
          cohort('v1-necro', 'v1', 'Necrobinder'),
          cohort('v2-iron', 'v2', 'Ironclad'),
        ];

        nodes.versionFilter.value = 'v1';
        nodes.characterFilter.value = 'Necrobinder';
        updateCharacterFilterOptions();
        const v1 = {{
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          character: nodes.characterFilter.value,
        }};

        nodes.versionFilter.value = 'v2';
        updateCharacterFilterOptions();
        const v2 = {{
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          character: nodes.characterFilter.value,
        }};
        updateCohortOptions({{ chooseDefaults: true }});
        const selectedV2 = {{
          currentOptions: nodes.currentCohort.children.map((option) => option.value),
          current: nodes.currentCohort.value,
        }};

        nodes.versionFilter.value = '';
        updateCharacterFilterOptions();
        const allVersions = {{
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          character: nodes.characterFilter.value,
        }};

        nodes.versionFilter.value = 'v2';
        nodes.validityFilter.value = 'technical';
        updateCharacterFilterOptions();
        updateCohortOptions({{ chooseDefaults: true }});
        const empty = {{
          currentOptions: nodes.currentCohort.children.map((option) => option.value),
          current: nodes.currentCohort.value,
          baseline: nodes.baselineCohort.value,
        }};

        state.cohorts = null;
        updateCharacterFilterOptions();
        const malformed = {{
          versionCandidates: cohortsForSelectedVersion().length,
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          character: nodes.characterFilter.value,
          filteredCandidates: filteredCohorts().length,
        }};

        console.log(JSON.stringify({{ v1, v2, selectedV2, allVersions, empty, malformed }}));
        """
    )

    assert payload == {
        "v1": {
            "characterOptions": ["", "Ironclad", "Necrobinder"],
            "character": "Necrobinder",
        },
        "v2": {"characterOptions": ["", "Ironclad"], "character": ""},
        "selectedV2": {"currentOptions": ["v2-iron"], "current": "v2-iron"},
        "allVersions": {
            "characterOptions": ["", "Ironclad", "Necrobinder"],
            "character": "",
        },
        "empty": {"currentOptions": [""], "current": "", "baseline": ""},
        "malformed": {
            "versionCandidates": 0,
            "characterOptions": [""],
            "character": "",
            "filteredCandidates": 0,
        },
    }


def test_filter_values_sort_game_versions_by_numeric_segments_descending():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    filters = _javascript_section(
        script, "function setSelectOptions", "function defaultBaselineCohortId"
    )

    payload = _run_node_json(
        f"""
        const versions = [
          'v0.99.10', 'v1.9.2', 'nightly', 'v0.107.1', 'V2.0',
          'v0.9.12', 'v1.10.0', 'v0.103.2', '2', 'v0.10.0',
          'v1.9.12', undefined, 'v2.0', 'v1.10',
        ];
        const characters = ['Watcher', 'Ironclad', 'Necrobinder'];
        const state = {{ cohorts: versions.map((version, index) => ({{
          cohort_id: `cohort-${{index}}`,
          filters: {{
            game_version: version,
            character: characters[index % characters.length],
          }},
        }})) }};
        {filters}
        console.log(JSON.stringify({{
          versions: filterValuesFromCohorts(state.cohorts, 'game_version'),
          characters: filterValuesFromCohorts(state.cohorts, 'character'),
        }}));
        """
    )

    assert payload == {
        "versions": [
            "2",
            "V2.0",
            "v2.0",
            "v1.10",
            "v1.10.0",
            "v1.9.12",
            "v1.9.2",
            "v0.107.1",
            "v0.103.2",
            "v0.99.10",
            "v0.10.0",
            "v0.9.12",
            "nightly",
            "未标注",
        ],
        "characters": ["Ironclad", "Necrobinder", "Watcher"],
    }


def test_axis_helpers_skip_malformed_cohorts_without_losing_valid_values():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    filters = _javascript_section(
        script, "function setSelectOptions", "function defaultBaselineCohortId"
    )

    payload = _run_node_json(
        f"""
        function makeSelect(value = '') {{
          return {{
            value,
            children: [],
            replaceChildren() {{ this.children = []; this.value = ''; }},
            append(option) {{
              this.children.push(option);
              if (this.children.length === 1) this.value = option.value;
            }},
          }};
        }}
        const nodes = {{
          versionFilter: makeSelect(),
          characterFilter: makeSelect(),
          validityFilter: makeSelect(),
        }};
        const byId = (id) => nodes[id];
        const clear = (node) => node.replaceChildren();
        const element = (tag, options = {{}}) => ({{
          tag,
          textContent: options.text === undefined ? '' : String(options.text),
          value: String(options.attrs.value),
        }});
        const validCohort = {{
          cohort_id: 'valid',
          run_count: 2,
          technical_count: 0,
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
        }};
        const state = {{ cohorts: [null, 42, validCohort] }};
        {filters}

        populateAxisFilter('versionFilter', 'game_version', '全部版本');
        nodes.versionFilter.value = 'v1';
        updateCharacterFilterOptions();
        nodes.characterFilter.value = 'Ironclad';
        const mixedDescriptors = {{
          versionOptions: nodes.versionFilter.children.map((option) => option.value),
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          versionCandidates: cohortsForSelectedVersion().map((cohort) => cohort.cohort_id),
          filteredCandidates: filteredCohorts().map((cohort) => cohort.cohort_id),
        }};

        const throwingVersion = {{
          cohort_id: 'throwing-version',
          get filters() {{ throw new Error('bad version getter'); }},
        }};
        const throwingCharacter = {{
          cohort_id: 'throwing-character',
          run_count: 2,
          technical_count: 0,
          filters: {{
            game_version: 'v1',
            get character() {{ throw new Error('bad character getter'); }},
          }},
        }};
        const throwingCount = {{
          cohort_id: 'throwing-count',
          run_count: 2,
          get technical_count() {{ throw new Error('bad count getter'); }},
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
        }};
        state.cohorts = [throwingVersion, throwingCharacter, throwingCount, validCohort];
        populateAxisFilter('versionFilter', 'game_version', '全部版本');
        nodes.versionFilter.value = 'v1';
        updateCharacterFilterOptions();
        nodes.characterFilter.value = 'Ironclad';
        const throwingGetters = {{
          versionOptions: nodes.versionFilter.children.map((option) => option.value),
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          versionCandidates: cohortsForSelectedVersion().map((cohort) => cohort.cohort_id),
          filteredCandidates: filteredCohorts().map((cohort) => cohort.cohort_id),
        }};
        nodes.characterFilter.value = '';
        throwingGetters.allCharacterCandidates = filteredCohorts()
          .map((cohort) => cohort.cohort_id);
        nodes.versionFilter.value = '';
        throwingGetters.allVersionCandidates = cohortsForSelectedVersion()
          .map((cohort) => cohort.cohort_id);
        throwingGetters.allScopeCandidates = filteredCohorts()
          .map((cohort) => cohort.cohort_id);

        state.cohorts = null;
        populateAxisFilter('versionFilter', 'game_version', '全部版本');
        updateCharacterFilterOptions();
        const nonArray = {{
          versionOptions: nodes.versionFilter.children.map((option) => option.value),
          characterOptions: nodes.characterFilter.children.map((option) => option.value),
          versionCandidates: cohortsForSelectedVersion().length,
          filteredCandidates: filteredCohorts().length,
        }};

        console.log(JSON.stringify({{ mixedDescriptors, throwingGetters, nonArray }}));
        """
    )

    assert payload == {
        "mixedDescriptors": {
            "versionOptions": ["", "v1"],
            "characterOptions": ["", "Ironclad"],
            "versionCandidates": ["valid"],
            "filteredCandidates": ["valid"],
        },
        "throwingGetters": {
            "versionOptions": ["", "v1"],
            "characterOptions": ["", "Ironclad"],
            "versionCandidates": ["throwing-character", "throwing-count", "valid"],
            "filteredCandidates": ["valid"],
            "allCharacterCandidates": ["valid"],
            "allVersionCandidates": ["throwing-character", "throwing-count", "valid"],
            "allScopeCandidates": ["valid"],
        },
        "nonArray": {
            "versionOptions": [""],
            "characterOptions": [""],
            "versionCandidates": 0,
            "filteredCandidates": 0,
        },
    }


def test_cohort_identity_guards_options_current_descriptor_and_comparison():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    filters = _javascript_section(
        script, "function setSelectOptions", "function defaultBaselineCohortId"
    )
    cohort_options = _javascript_section(
        script, "function updateCohortOptions", "function resetMetrics"
    )
    comparison = _javascript_section(
        script, "function renderComparison", "function anomalyRow"
    )
    current_descriptor = _javascript_section(
        script, "function currentCohortDescriptor", "function stablePointKey"
    )

    payload = _run_node_json(
        f"""
        function makeNode() {{
          return {{
            textContent: '', dataset: {{}}, children: [], value: '',
            append(...children) {{ this.children.push(...children); }},
          }};
        }}
        function makeSelect() {{
          const select = makeNode();
          select.replaceChildren = function replaceChildren() {{
            this.children = [];
            this.value = '';
          }};
          select.append = function append(option) {{
            this.children.push(option);
            if (this.children.length === 1) this.value = option.value;
          }};
          return select;
        }}
        const comparisonBody = makeNode();
        const comparisonBanner = makeNode();
        comparisonBanner.querySelector = () => comparisonBody;
        const nodes = {{
          versionFilter: makeSelect(),
          characterFilter: makeSelect(),
          validityFilter: makeSelect(),
          currentCohort: makeSelect(),
          baselineCohort: makeSelect(),
          comparisonBanner,
          comparisonTitle: makeNode(),
        }};
        const byId = (id) => nodes[id];
        const clear = (node) => {{
          if (typeof node.replaceChildren === 'function') node.replaceChildren();
          else node.children = [];
        }};
        const element = (tag, options = {{}}) => {{
          const node = makeNode();
          node.tag = tag;
          node.textContent = options.text === undefined ? '' : String(options.text);
          node.value = options.attrs && options.attrs.value !== undefined
            ? String(options.attrs.value)
            : '';
          return node;
        }};
        const formatTime = (value) => `time-${{value}}`;
        const updateCohortHelp = () => {{}};
        const defaultBaselineCohortId = () => '';
        const valid = {{
          cohort_id: 'valid-cohort',
          label: 'valid',
          run_count: 2,
          technical_count: 0,
          latest_at: 2,
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
          comparison_readiness: {{ ready: false }},
        }};
        const missingId = {{
          label: 'missing-id',
          run_count: 1,
          technical_count: 0,
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
        }};
        const whitespaceId = {{
          cohort_id: '   ',
          label: 'whitespace-id',
          run_count: 1,
          technical_count: 0,
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
        }};
        const throwingId = {{
          get cohort_id() {{ throw new Error('bad cohort id getter'); }},
          label: 'throwing-id',
          run_count: 1,
          technical_count: 0,
          filters: {{ game_version: 'v1', character: 'Ironclad' }},
        }};
        const malformed = [null, 42, missingId, whitespaceId, throwingId];
        const state = {{ cohorts: [...malformed, valid] }};
        {filters}
        {cohort_options}
        {current_descriptor}
        {comparison}

        nodes.versionFilter.value = 'v1';
        nodes.characterFilter.value = 'Ironclad';
        updateCohortOptions({{ chooseDefaults: true }});
        const selection = {{
          candidates: filteredCohorts().map((cohort) => cohort.cohort_id),
          options: nodes.currentCohort.children.map((option) => option.value),
          current: nodes.currentCohort.value,
          descriptor: currentCohortDescriptor() === valid,
        }};

        renderComparison(null);
        const rendered = {{
          title: nodes.comparisonTitle.textContent,
          body: comparisonBody.children.map((child) => child.textContent),
        }};

        const descriptorCases = [];
        for (const descriptor of malformed) {{
          state.cohorts = [descriptor];
          nodes.currentCohort.value = 'valid-cohort';
          descriptorCases.push(currentCohortDescriptor() === null);
        }}
        state.cohorts = null;
        descriptorCases.push(currentCohortDescriptor() === null);

        console.log(JSON.stringify({{ selection, rendered, descriptorCases }}));
        """
    )

    assert payload == {
        "selection": {
            "candidates": ["valid-cohort"],
            "options": ["valid-cohort"],
            "current": "valid-cohort",
            "descriptor": True,
        },
        "rendered": {
            "title": "元数据不完整",
            "body": ["历史记录仍可查看，但不会用于训练提升比较。"],
        },
        "descriptorCases": [True, True, True, True, True, True],
    }


def test_character_and_validity_changes_choose_server_first_candidate():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    filters = _javascript_section(
        script, "function setSelectOptions", "function defaultBaselineCohortId"
    )
    cohort_options = _javascript_section(
        script, "function updateCohortOptions", "function resetMetrics"
    )
    filter_changed = _javascript_section(
        script, "function filterChanged", "function versionFilterChanged"
    )

    payload = _run_node_json(
        f"""
        function makeSelect(value = '') {{
          return {{
            value,
            children: [],
            replaceChildren() {{ this.children = []; this.value = ''; }},
            append(option) {{
              this.children.push(option);
              if (this.children.length === 1) this.value = option.value;
            }},
          }};
        }}
        const nodes = {{
          versionFilter: makeSelect(),
          characterFilter: makeSelect(),
          validityFilter: makeSelect(),
          currentCohort: makeSelect(),
          baselineCohort: makeSelect(),
        }};
        const byId = (id) => nodes[id];
        const clear = (node) => node.replaceChildren();
        const element = (tag, options = {{}}) => ({{
          tag,
          textContent: options.text === undefined ? '' : String(options.text),
          value: String(options.attrs.value),
        }});
        const formatTime = (value) => `time-${{value}}`;
        const updateCohortHelp = () => {{}};
        const defaultBaselineCohortId = () => '';
        let refreshCount = 0;
        const refreshMetrics = () => {{ refreshCount += 1; }};
        const state = {{ cohorts: [
          {{
            cohort_id: 'server-first-iron',
            label: 'server-first-iron',
            run_count: 2,
            technical_count: 0,
            latest_at: 2,
            filters: {{ game_version: 'v1', character: 'Ironclad' }},
          }},
          {{
            cohort_id: 'server-second-necro',
            label: 'server-second-necro',
            run_count: 2,
            technical_count: 2,
            latest_at: 1,
            filters: {{ game_version: 'v1', character: 'Necrobinder' }},
          }},
        ] }};
        {filters}
        {cohort_options}
        {filter_changed}

        nodes.versionFilter.value = 'v1';
        nodes.characterFilter.value = 'Necrobinder';
        filterChanged();
        const narrowCharacter = nodes.currentCohort.value;

        nodes.characterFilter.value = '';
        filterChanged();
        const wideCharacter = {{
          current: nodes.currentCohort.value,
          options: nodes.currentCohort.children.map((option) => option.value),
        }};

        nodes.currentCohort.value = 'server-first-iron';
        nodes.validityFilter.value = 'technical';
        filterChanged();
        const narrowValidity = nodes.currentCohort.value;

        nodes.validityFilter.value = '';
        filterChanged();
        const wideValidity = {{
          current: nodes.currentCohort.value,
          options: nodes.currentCohort.children.map((option) => option.value),
        }};

        console.log(JSON.stringify({{
          narrowCharacter,
          wideCharacter,
          narrowValidity,
          wideValidity,
          refreshCount,
        }}));
        """
    )

    assert payload == {
        "narrowCharacter": "server-second-necro",
        "wideCharacter": {
            "current": "server-first-iron",
            "options": ["server-first-iron", "server-second-necro"],
        },
        "narrowValidity": "server-second-necro",
        "wideValidity": {
            "current": "server-first-iron",
            "options": ["server-first-iron", "server-second-necro"],
        },
        "refreshCount": 4,
    }
    assert "updateCohortOptions({ chooseDefaults: true })" in filter_changed


def test_render_comparison_null_distinguishes_incomplete_and_ready_current():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    identity = _javascript_section(
        script, "function safeCohortId", "function filterValuesFromCohorts"
    )
    comparison = _javascript_section(
        script, "function renderComparison", "function anomalyRow"
    )
    current_descriptor = _javascript_section(
        script, "function currentCohortDescriptor", "function stablePointKey"
    )

    payload = _run_node_json(
        f"""
        function makeNode() {{
          return {{
            textContent: '', dataset: {{}}, children: [],
            append(...children) {{ this.children.push(...children); }},
          }};
        }}
        const body = makeNode();
        const banner = makeNode();
        banner.querySelector = () => body;
        const title = makeNode();
        const currentSelect = {{ value: 'incomplete' }};
        const nodes = {{
          comparisonBanner: banner,
          comparisonTitle: title,
          currentCohort: currentSelect,
        }};
        const byId = (id) => nodes[id];
        const clear = (node) => {{ node.children = []; }};
        const element = (tag, options = {{}}) => ({{
          tag,
          textContent: options.text === undefined ? '' : String(options.text),
          dataset: {{}},
          children: [],
          append(...children) {{ this.children.push(...children); }},
        }});
        const appendList = (container, values) => container.append({{
          tag: 'ul', values: [...values], textContent: values.join('|'),
        }});
        const formatMissing = String;
        function deltaText(value) {{
          return {{ text: String(value), direction: 'flat' }};
        }}
        const state = {{ cohorts: [
          {{ cohort_id: 'incomplete', comparison_readiness: {{ ready: false }} }},
          {{ cohort_id: 'ready', comparison_readiness: {{ ready: true }} }},
        ] }};
        {identity}
        {current_descriptor}
        {comparison}

        renderComparison(null);
        const incomplete = {{
          title: title.textContent,
          body: body.children.map((child) => child.textContent),
        }};
        currentSelect.value = 'ready';
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


def test_current_default_uses_server_latest_order_instead_of_label_order():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    selector = _javascript_section(
        script, "function updateCohortOptions", "function resetMetrics"
    )

    assert "entries[0].id" in selector
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

    assert "entries[0].id" in cohorts
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


def test_open_run_uses_map_only_when_canonical_run_has_route_capability():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "function runHasMapCapability" in script
    capability = _javascript_section(
        script, "function runHasMapCapability", "function renderCanonicalRun"
    )
    canonical = _javascript_section(
        script, "function renderCanonicalRun", "function renderDetail"
    )
    open_run = _javascript_section(
        script, "async function openRun", "async function refreshMetrics"
    )
    assert "if (mapAvailable" in canonical

    payload = _run_node_json(
        f"""
        (async () => {{
          const calls = [];
          const state = {{ detailAbortController: {{}} }};
          const opener = {{ id: 'trend-point' }};
          let response = null;
          const window = {{
            STS2Map: {{
              openRun(runId, candidate) {{
                calls.push({{ type: 'map', runId, opener: candidate && candidate.id }});
              }},
            }},
          }};
          function beginDetailRequest(candidate) {{
            calls.push({{ type: 'begin', opener: candidate && candidate.id }});
            return {{ token: 1, signal: {{}} }};
          }}
          function isCurrentDetailRequest(token) {{ return token === 1; }}
          async function getJSON(path) {{
            calls.push({{ type: 'fetch', path }});
            return response;
          }}
          function renderDetail(value, title, candidate) {{
            calls.push({{
              type: 'detail', title, runId: value.run.run_id,
              opener: candidate && candidate.id,
            }});
          }}
          function setStatus(message, kind) {{ calls.push({{ type: 'status', message, kind }}); }}
          {capability}
          {open_run}

          response = {{
            view: 'run',
            run: {{ run_id: 'deck-only', capabilities: {{ visited_route: false, full_map: false }} }},
          }};
          await openRun('deck-only', opener);
          response = {{
            view: 'run',
            run: {{ run_id: 'native-route', capabilities: {{ visited_route: true, full_map: false }} }},
          }};
          await openRun('native-route', opener);
          console.log(JSON.stringify(calls));
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )

    assert [call["type"] for call in payload].count("fetch") == 2
    assert [call for call in payload if call["type"] == "detail"] == [
        {
            "type": "detail",
            "title": "对局 deck-only",
            "runId": "deck-only",
            "opener": "trend-point",
        }
    ]
    assert [call for call in payload if call["type"] == "map"] == [
        {
            "type": "map",
            "runId": "native-route",
            "opener": "trend-point",
        }
    ]
    assert any(
        call["type"] == "status" and call["message"] == "已载入对局摘要"
        for call in payload
    )


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
    assert "matches(':disabled')" in script
    assert "event.shiftKey" in script
    assert "event.preventDefault()" in script
    assert "focusables[0]" in script
    assert "focusables[focusables.length - 1]" in script
    assert "handleDetailKeydown" in script


def test_metrics_refresh_captures_focus_before_busy_and_restores_after_enable():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function isFocusable" in script
    assert "function restoreMetricsFocus" in script
    refresh = _javascript_section(
        script, "async function refreshMetrics", "async function uploadSelectedFile"
    )
    focus_helper = _javascript_section(
        script, "function isFocusable", "function handleDetailKeydown"
    )

    assert "const focusOpener = document.activeElement" in refresh
    assert refresh.index("const focusOpener = document.activeElement") < refresh.index(
        "setBusy(true)"
    )
    assert "finally" in refresh
    assert refresh.index("setBusy(false)") < refresh.index("restoreMetricsFocus(")
    assert "byId('currentCohort').value === current" in script
    assert "byId('baselineCohort').value === baseline" in script
    assert "candidate.isConnected" in focus_helper
    assert "typeof candidate.focus" in focus_helper and "'function'" in focus_helper
    assert "closest('[hidden], [inert], [aria-hidden=\"true\"]')" in focus_helper
    assert "matches(':disabled')" in focus_helper
    assert "window.getComputedStyle(candidate)" in focus_helper


def test_metrics_refresh_focus_restoration_is_safe_latest_context_only():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function isFocusable" in script
    assert "function restoreMetricsFocus" in script
    focus_helper = _javascript_section(
        script, "function isFocusable", "function handleDetailKeydown"
    )
    restore_helper = _javascript_section(
        script, "function restoreMetricsFocus", "async function refreshMetrics"
    )
    refresh = _javascript_section(
        script, "async function refreshMetrics", "async function uploadSelectedFile"
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
          const currentSelect = makeElement('currentCohort');
          const baselineSelect = makeElement('baselineCohort');
          const elsewhere = makeElement('elsewhere');
          const nodes = {{
            currentCohort: currentSelect,
            baselineCohort: baselineSelect,
          }};
          const byId = (id) => nodes[id];
          const controls = [currentSelect, baselineSelect];
          function setBusy(isBusy) {{
            controls.forEach((control) => {{ control.disabled = isBusy; }});
            if (isBusy && controls.includes(document.activeElement)) {{
              document.activeElement = document.body;
            }}
          }}
          const state = {{ cohorts: [{{}}], currentMetrics: null }};
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

          currentSelect.value = 'current-a';
          baselineSelect.value = 'baseline-a';
          document.activeElement = currentSelect;
          await refreshMetrics();
          const currentRestored = document.activeElement === currentSelect;

          document.activeElement = baselineSelect;
          await refreshMetrics();
          const baselineRestored = document.activeElement === baselineSelect;

          const currentFocusBeforeElsewhere = currentSelect.focusCalls;
          const baselineFocusBeforeElsewhere = baselineSelect.focusCalls;
          document.activeElement = elsewhere;
          await refreshMetrics();
          const elsewherePreserved = document.activeElement === elsewhere
            && currentSelect.focusCalls === currentFocusBeforeElsewhere
            && baselineSelect.focusCalls === baselineFocusBeforeElsewhere;

          document.activeElement = baselineSelect;
          getJSONImpl = async () => {{ throw new Error('metrics failed'); }};
          await refreshMetrics();
          const errorRestored = document.activeElement === baselineSelect
            && status === '训练指标读取失败：metrics failed';

          let resolveStale;
          getJSONImpl = () => new Promise((resolve) => {{ resolveStale = resolve; }});
          document.activeElement = currentSelect;
          const staleRequest = refreshMetrics();
          currentSelect.value = 'current-b';
          resolveStale(metrics);
          await staleRequest;
          const staleSelectionDidNotRestore = document.activeElement === document.body;

          currentSelect.value = 'current-b';
          let resolveMoved;
          getJSONImpl = () => new Promise((resolve) => {{ resolveMoved = resolve; }});
          document.activeElement = currentSelect;
          const movedRequest = refreshMetrics();
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
