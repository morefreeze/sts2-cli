"""The Advisor Play tab's front end: shell, router, tab keyboard, advisor-play-view.js.

Same approach as the 回归/评测 tab's tests in test_static_contract.py: the real scripts
are run by node against a minimal fake DOM, and the page is fed the payload the real
``advisor_play.build_payload`` makes from small fixture files (nothing of the player's).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import agent.run_progress_viewer as viewer
from agent.run_workbench import advisor_play as ap

from .test_advisor_play import BASE, NOW, Env, current_save, log_line, progress
from .test_static_contract import (
    _FAKE_VIEW_DOM,
    STATIC_DIR,
    _get,
    _javascript_section,
    _router_harness,
    _run_node_json,
    _server,
    _ShellParser,
)

import datetime as dt


# ---------------------------------------------------------------------------
# Shell, assets, CSS
# ---------------------------------------------------------------------------


def test_shell_has_the_advisor_play_tab_and_pane():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parser = _ShellParser()
    parser.feed(html)
    for item in ("tabAdvisorPlay", "advisorPlayView", "advisorPlayBody", "advisorPlayNotice"):
        assert item in parser.ids, item
    # third tab, after 训练 and 回归/评测; not selected by default; same pane
    assert re.search(r'id="tabAdvisorPlay"[^>]*role="tab"[^>]*aria-selected="false"[^>]*aria-controls="contentPane"', html)
    assert re.search(r'id="tabAdvisorPlay"[^>]*data-tab="advisor-play">Advisor Play<', html)
    tabs = re.findall(r'id="(tab\w+)"', html)
    assert tabs == ["tabTraining", "tabRegression", "tabAdvisorPlay"]
    # the pane sits inside the content pane next to the regression view, hidden until routed to
    assert re.search(r'<section id="advisorPlayView" class="advisor-play-view" hidden', html)
    order = ["contentPane", "regressionView", "advisorPlayView", "advisorPlayBody"]
    assert [parser.ids.index(item) for item in order] == sorted(parser.ids.index(item) for item in order)
    scripts = re.findall(r'<script src="/static/([\w.-]+)"', html)
    assert scripts.index("advisor-play-view.js") > scripts.index("batches-view.js")
    assert scripts.index("advisor-play-view.js") < scripts.index("map.js") and scripts[-1] == "map.js"


def test_advisor_play_view_is_a_served_utf8_asset(tmp_path: Path):
    viewer_source = Path(viewer.__file__).read_text(encoding="utf-8")
    assert '"/static/advisor-play-view.js": ("advisor-play-view.js", "text/javascript; charset=utf-8")' in viewer_source
    assert "/static/advisor-play-view.js" not in viewer.QUERY_ALLOWED_ASSETS
    with _server(tmp_path) as base:
        status, content_type, body = _get(base, "/static/advisor-play-view.js")
        query_status, _, _ = _get(base, "/static/advisor-play-view.js?x=1")
    assert status == 200 and content_type == "text/javascript; charset=utf-8"
    assert body == (STATIC_DIR / "advisor-play-view.js").read_bytes()
    assert query_status == 404


def test_css_gives_the_advisor_play_pane_the_whole_row_and_the_chart_marks_their_specs():
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
    assert re.search(r'\.workbench-body\[data-tab="advisor-play"\]\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);', css, re.DOTALL)
    assert re.search(r"\.advisor-play-view\[hidden\]\s*\{\s*display:\s*none;", css)
    # dataviz marks: 2px lines, solid hairline grid (never dashed), a surface ring on markers
    assert re.search(r"svg \.adv-line-hp\s*\{[^}]*stroke-width:\s*2;", css)
    assert re.search(r"svg \.adv-grid\s*\{[^}]*stroke-width:\s*1;", css)
    assert "dasharray" not in css[css.index("--- Advisor Play tab"):]
    assert re.search(r"svg \.adv-mark-elite\s*\{[^}]*stroke:\s*var\(--adv-surface\);[^}]*stroke-width:\s*2;", css)
    # the validated categorical slots 1..3 and the ordinal act ramp
    for token in ("--adv-s1: #2a78d6", "--adv-s2: #eb6834", "--adv-s3: #1baf7a",
                  "--adv-act-1: #86b6ef", "--adv-act-2: #2a78d6", "--adv-act-3: #104281"):
        assert token in css, token


# ---------------------------------------------------------------------------
# Router and tabs
# ---------------------------------------------------------------------------


def test_hash_router_parses_the_advisor_play_routes():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    routing = _javascript_section(script, "function cohortRoute", "function navigate")
    payload = _run_node_json(
        f"""
        {routing}
        console.log(JSON.stringify({{
          plain: parseRoute('#/advisor-play'),
          slash: parseRoute('#/advisor-play/'),
          silent: parseRoute('#/advisor-play/SILENT'),
          odd: parseRoute('#/advisor-play/WATCHER-WATCHER'),
          encoded: parseRoute('#/advisor-play/A%20B'),
          deeper: parseRoute('#/advisor-play/SILENT/x'),
          hashes: [advisorPlayRoute(), advisorPlayRoute(''), advisorPlayRoute('SILENT'), advisorPlayRoute('A B')],
          regression: parseRoute('#/regression'), training: parseRoute('#/training'),
        }}));
        """
    )
    assert payload["plain"] == {"view": "advisor-play", "character": ""}
    assert payload["silent"] == {"view": "advisor-play", "character": "SILENT"}
    assert payload["odd"] == {"view": "advisor-play", "character": "WATCHER-WATCHER"}
    assert payload["encoded"] == {"view": "advisor-play", "character": "A B"}
    assert payload["hashes"] == ["#/advisor-play", "#/advisor-play", "#/advisor-play/SILENT", "#/advisor-play/A%20B"]
    # anything else under the prefix is not ours: the router falls back to the training root
    assert payload["slash"] == {"view": "root"} and payload["deeper"] == {"view": "root"}
    assert payload["regression"] == {"view": "regression"} and payload["training"] == {"view": "root", "tab": "training"}


def _advisor_router_harness() -> str:
    return _router_harness([], None).replace(
        "const window = { STS2Map: { showDashboardPage() { calls.push('dashboard'); } }, BatchesView, RunView };",
        """
        const AdvisorPlayView = {
          stop() { calls.push('advisor-stop'); },
          async render(route) { calls.push(`advisor:${route.character}`); },
        };
        const window = { STS2Map: { showDashboardPage() { calls.push('dashboard'); } }, BatchesView, RunView, AdvisorPlayView };
        """,
    ).replace(
        "regressionTabIndex: byId('tabRegression').getAttribute('tabindex'),",
        "regressionTabIndex: byId('tabRegression').getAttribute('tabindex'),\n"
        "                advisorTab: byId('tabAdvisorPlay').getAttribute('aria-selected'),\n"
        "                advisorTabIndex: byId('tabAdvisorPlay').getAttribute('tabindex'),\n"
        "                advisorView: byId('advisorPlayView').hidden,",
    )


def test_router_shows_the_advisor_play_pane_and_stops_its_refresh_when_leaving():
    payload = _run_node_json(
        _advisor_router_harness()
        + """
        (async () => {
          const out = {};
          await applyRoute(parseRoute('#/advisor-play/IRONCLAD'));
          out.open = { calls: take(), ...visibility(), tab: state.tab, resolved: state.tabResolved };
          await applyRoute(parseRoute('#/regression'));
          out.regression = { calls: take(), ...visibility() };
          await applyRoute(parseRoute('#/advisor-play'));
          take();
          state.tree = [{ characters: [{ cohorts: [{ cohort_id: 'c1' }] }] }];
          await applyRoute(parseRoute('#/batch/c1'));
          out.training = { calls: take(), ...visibility() };
          console.log(JSON.stringify(out));
        })();
        """
    )
    opened = payload["open"]
    assert "advisor:IRONCLAD" in opened["calls"] and "advisor-stop" not in opened["calls"]
    assert opened["tab"] == "advisor-play" and opened["resolved"] is True
    assert (opened["bodyTab"], opened["trainingTab"], opened["regressionTab"], opened["advisorTab"]) == (
        "advisor-play", "false", "false", "true")
    assert (opened["trainingTabIndex"], opened["regressionTabIndex"], opened["advisorTabIndex"]) == ("-1", "-1", "0")
    # only the advisor pane is visible, and the training tree is hidden (the pane uses the whole row)
    assert (opened["advisorView"], opened["regressionView"], opened["batchView"], opened["catalogView"], opened["tree"]) == (
        False, True, True, True, True)
    # leaving ends the refresh; the other tabs are untouched by it
    assert "advisor-stop" in payload["regression"]["calls"]
    assert payload["regression"]["advisorView"] is True and payload["regression"]["advisorTab"] == "false"
    assert "advisor-stop" in payload["training"]["calls"]
    assert (payload["training"]["batchView"], payload["training"]["advisorView"], payload["training"]["tree"]) == (False, True, False)


def test_tab_buttons_and_arrow_keys_cycle_through_the_three_tabs():
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
        function advisorPlayRoute() { return '#/advisor-play'; }
        """
        + handlers
        + """
        const press = (tab, key) => { state.tab = tab; byId('workbenchTabs').dispatch('keydown', { key }); };
        byId('tabAdvisorPlay').click();
        press('training', 'ArrowRight');
        press('regression', 'ArrowRight');
        press('advisor-play', 'ArrowRight');
        press('training', 'ArrowLeft');
        press('regression', 'ArrowLeft');
        press('advisor-play', 'ArrowLeft');
        press('advisor-play', 'Tab');
        console.log(JSON.stringify({ navigateCalls }));
        """
    )
    assert payload["navigateCalls"] == [
        "#/advisor-play",                                   # clicking the tab
        "#/regression", "#/advisor-play", "#/training",     # Right: 训练 -> 回归/评测 -> Advisor Play -> 训练
        "#/advisor-play", "#/training", "#/regression",     # Left: the other way round
    ]


def test_the_catalog_toggle_returns_to_the_advisor_pane_when_that_tab_is_open():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    toggle = _javascript_section(script, "byId('catalogToggle').addEventListener", "byId('tabTraining').addEventListener")
    assert "else if (state.tab === 'advisor-play') showAdvisorPlayView();" in toggle


# ---------------------------------------------------------------------------
# advisor-play-view.js
# ---------------------------------------------------------------------------


def _view_harness() -> str:
    util_script = (STATIC_DIR / "util.js").read_text(encoding="utf-8")
    app_script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    view_script = (STATIC_DIR / "advisor-play-view.js").read_text(encoding="utf-8")
    return "\n".join(
        [
            _FAKE_VIEW_DOM,
            """
            // the fake DOM, plus what this view touches: style, text nodes, namespaced elements
            const baseCreate = document.createElement;
            document.createElement = (tag) => { const node = baseCreate(tag); node.style = {}; return node; };
            document.createElementNS = (ns, tag) => document.createElement(tag);
            document.createTextNode = (text) => { const node = document.createElement('#text'); node.textContent = String(text); return node; };
            """,
            "const SVG_NS = 'http://www.w3.org/2000/svg';",
            _javascript_section(util_script, "function element(tag", "function clear(node)"),
            _javascript_section(util_script, "function clear(node)", "function setStatus"),
            _javascript_section(util_script, "function renderEmpty", "function setSelectOptions"),
            _javascript_section(app_script, "function advisorPlayRoute", "function batchRoute"),
            """
            const state = {};
            const statuses = [];
            function setStatus(message, tone) { statuses.push([message, tone]); }
            const navigateCalls = [];
            function navigate(hash) { navigateCalls.push(hash); }
            const timers = [];
            let nextTimer = 1;
            function setTimeout(fn, ms) { const t = { id: nextTimer++, fn, ms, cleared: false, fired: false }; timers.push(t); return t.id; }
            function clearTimeout(id) { const t = timers.find((c) => c.id === id); if (t) t.cleared = true; }
            const intervals = [];
            function setInterval(fn, ms) { const t = { id: nextTimer++, fn, ms, cleared: false }; intervals.push(t); return t.id; }
            function clearInterval(id) { const t = intervals.find((c) => c.id === id); if (t) t.cleared = true; }
            const pending = () => timers.filter((t) => !t.cleared && !t.fired);
            const fetches = [];
            let responses = [];
            async function getJSON(url) {
              fetches.push(url);
              const next = responses.shift();
              if (next instanceof Error) throw next;
              return next;
            }
            const window = {};
            """,
            view_script,
            "const APV = window.AdvisorPlayView;",
        ]
    )


def _fixture_payload(tmp_path: Path, runs: int = 3, **kwargs) -> dict:
    """What /api/advisor-play answers for a few small fixture runs (+ a run in progress, a log, a journal)."""
    env = Env(tmp_path)
    env.write("progress.save", progress(IRONCLAD=7, SILENT=10, DEFECT=6, REGENT=4, NECROBINDER=5), where="saves")
    kinds = [("IRONCLAD", 6, True), ("SILENT", 10, False), ("REGENT", 3, False), ("DEFECT", 5, True)]
    for index in range(runs):
        character, ascension, win = kinds[index % len(kinds)]
        env.add_run(BASE + index, character=character, ascension=ascension, win=win, final_hp=40 if win else 0,
                    killed="ENCOUNTER.AEONGLASS_BOSS")
    env.write("current_run.save", current_save(BASE + 5000, character="IRONCLAD", ascension=7), where="saves")
    env.write("campaign.json", {"enabled": True, "rotation": ["IRONCLAD", "DEFECT", "REGENT"]})
    env.write("Sts2Advisor.log", log_line(dt.datetime.fromtimestamp(NOW - 120), "autopilot step 12: rest_site choose_option") + "\n")
    env.journal.write_text("REGENT a2 WIN <script>alert(1)</script>\nsecond line\n", encoding="utf-8")
    return ap.build_payload(env.paths, now=NOW, **kwargs)


def _view_script(body: str, **payloads) -> str:
    script = _view_harness() + "\n" + body
    for name, value in payloads.items():
        script = script.replace(f"__{name.upper()}__", json.dumps(value))
    return script


def test_the_page_draws_every_section_from_the_payload(tmp_path: Path):
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__PAYLOAD__];
              await APV.render({ view: 'advisor-play', character: '' });
              const body = byId('advisorPlayBody');
              const cls = (n) => n.className || '';
              const attr = (n, a) => n.getAttribute(a);
              const rows = collect(body, (n) => n.tag === 'table' && cls(n).includes('adv-runs'))[0]
                .children.find((c) => c.tag === 'tbody').children;
              const svgs = collect(body, (n) => n.tag === 'svg');
              const charts = svgs.filter((n) => attr(n, 'class') === 'adv-hp');
              console.log(JSON.stringify({
                fetches, statuses,
                headings: collect(body, (n) => n.tag === 'h2').map((n) => n.textContent),
                hero: allText(collect(body, (n) => cls(n).includes('adv-hero'))[0]),
                facts: collect(body, (n) => cls(n) === 'adv-line').map(allText),
                rows: rows.map((tr) => tr.children.map(allText)),
                rowClasses: rows.map((tr) => cls(tr)),
                cards: collect(body, (n) => cls(n).startsWith('adv-run-card')).length,
                hpCharts: svgs.filter((n) => attr(n, 'class') === 'adv-hp').length,
                labels: collect(body, (n) => n.tag === 'text' && attr(n, 'class') === 'adv-dlbl').map((n) => n.textContent),
                elite: charts.reduce((sum, c) => sum + collect(c, (n) => attr(n, 'class') === 'adv-mark-elite').length, 0),
                boss: charts.reduce((sum, c) => sum + collect(c, (n) => attr(n, 'class') === 'adv-mark-boss').length, 0),
                ladderRows: collect(body, (n) => n.tag === 'tr' && n.children[0] && n.children[0].tag === 'th' && cls(n.children[0]).includes('adv-char'))
                  .map((tr) => [allText(tr.children[0]), allText(tr.children[1])]),
                bars: collect(body, (n) => (attr(n, 'class') || '').startsWith('adv-bar ')).length,
                deaths: collect(body, (n) => cls(n) === 'adv-death-name').map((n) => n.textContent),
                journal: collect(body, (n) => n.tag === 'pre' && cls(n) === 'adv-pre').map((n) => n.textContent),
                select: collect(body, (n) => n.tag === 'option').map((n) => n.textContent),
                pending: pending().map((t) => t.ms),
                intervals: intervals.map((t) => t.ms),
                noScriptNodes: collect(body, (n) => n.tag === 'script').length,
              }));
            })();
            """,
            payload=_fixture_payload(tmp_path),
        )
    )
    assert payload["fetches"] == ["/api/advisor-play"]
    assert payload["headings"] == ["实机战役 · 全角色进阶 10", "每个角色解锁到哪一级", "对局列表", "逐层 HP",
                                   "各幕各房间类型的平均受伤", "死于哪个遭遇", "低血量打精英", "战役日志"]
    assert "32" in payload["hero"] and "/ 50" in payload["hero"]
    # the fake DOM joins text nodes with spaces; compare without them
    facts = [re.sub(r"\s+", "", f) for f in payload["facts"]]
    assert any("正在进行" in f and "IRONCLADa7" in f and "A1F3" in f for f in facts)
    assert any("自动驾驶" in f and "活跃" in f and "#12rest_sitechoose_option" in f for f in facts)
    assert any("轮换" in f and "IRONCLAD→DEFECT→REGENT" in f and "此局之后：DEFECTmax" in f for f in facts), facts
    # the table: the run in progress first, then newest first; `#` counts up from the oldest
    first = payload["rows"][0]
    assert first[0] == "4" and first[2] == "IRONCLAD" and first[3] == "a7" and "进行中" in first[4]
    assert [row[2] for row in payload["rows"]] == ["IRONCLAD", "REGENT", "SILENT", "IRONCLAD"]
    squash = lambda text: re.sub(r"\s+", " ", text).strip()   # noqa: E731 -- the fake DOM joins text nodes with spaces
    done = payload["rows"][1]
    assert squash(done[4]) == "✗ 负" and done[5].startswith("A2F3") and done[6] == "0/80" and done[7] == "AEONGLASS_BOSS"
    assert squash(done[8]) == "5（+2）" and done[9] == "2"
    won = payload["rows"][3]
    assert squash(won[4]) == "✓ 胜" and won[6] == "40/80" and won[7] == "—"
    assert payload["rowClasses"][0].endswith("in-progress") and payload["rowClasses"][1] == ""
    # one HP chart per run (the one in progress included), with elite/boss markers and "−N" labels
    assert payload["cards"] == 4 and payload["hpCharts"] == 4
    # 3 finished runs x (2 elites + 2 bosses); the run in progress has reached neither yet (the legend's keys are not counted)
    assert payload["elite"] == 6 and payload["boss"] == 6
    assert "−30" in payload["labels"] and "−50" in payload["labels"]
    # ladder: one row per character, unlocked / 10 and what is left
    assert [r[0] for r in payload["ladderRows"]] == ["IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER"]
    unlocked = [re.sub(r"\s+", "", row[1]) for row in payload["ladderRows"]]
    assert unlocked[0] == "7/10还差3" and unlocked[1] == "10/10✓已达成" and unlocked[4] == "5/10还差5"
    assert payload["bars"] > 0 and payload["deaths"] == ["AEONGLASS_BOSS"]
    # the journal is plain text, verbatim (never markup)
    assert payload["journal"][0] == "REGENT a2 WIN <script>alert(1)</script>\nsecond line"
    assert payload["noScriptNodes"] == 0
    assert payload["select"][0] == "全部角色（3 局）" and "IRONCLAD（1 局）" in payload["select"]
    # it refreshes itself every minute and keeps a clock for "minutes since the last step"
    assert payload["pending"] == [60000] and payload["intervals"] == [15000]
    assert payload["statuses"][-1] == ["已载入 Advisor Play", "ready"] or payload["statuses"][-1][0] == "已载入 Advisor Play"


def test_the_hp_chart_geometry_and_the_three_costliest_rooms(tmp_path: Path):
    payload = _run_node_json(
        _view_script(
            """
            const run = __RUN__;
            const svg = APV.hpChart(run);
            const attr = (n, a) => n.getAttribute(a);
            const all = collect(svg, () => true);
            const hp = all.find((n) => attr(n, 'class') === 'adv-line-hp');
            const mx = all.find((n) => attr(n, 'class') === 'adv-line-max');
            const acts = all.filter((n) => attr(n, 'class') === 'adv-actlbl').map((n) => n.textContent);
            const actLines = all.filter((n) => attr(n, 'class') === 'adv-actline');
            const ticks = all.filter((n) => attr(n, 'class') === 'adv-tick').map((n) => n.textContent);
            const labels = all.filter((n) => attr(n, 'class') === 'adv-dlbl').map((n) => n.textContent);
            const pts = (n) => attr(n, 'points').split(' ').map((p) => p.split(',').map(Number));
            console.log(JSON.stringify({
              viewBox: attr(svg, 'viewBox'), role: attr(svg, 'role'), tabindex: attr(svg, 'tabindex'), label: attr(svg, 'aria-label'),
              hpPoints: pts(hp).length, maxPoints: pts(mx).length, acts, actLines: actLines.length, ticks, labels,
              hpX: pts(hp).map((p) => p[0]), hpY: pts(hp).map((p) => p[1]),
              elite: all.filter((n) => attr(n, 'class') === 'adv-mark-elite').length,
              boss: all.filter((n) => attr(n, 'class') === 'adv-mark-boss').length,
              end: all.filter((n) => attr(n, 'class') === 'adv-mark-end').length,
              hit: all.filter((n) => attr(n, 'class') === 'adv-hit').length,
              ymaxTicks: all.filter((n) => attr(n, 'class') === 'adv-tick' && /^\\d+$/.test(n.textContent)).map((n) => n.textContent),
            }));
            """,
            run=_fixture_payload(tmp_path, runs=1)["runs"][0],
        )
    )
    assert payload["viewBox"] == "0 0 380 196" and payload["role"] == "img" and payload["tabindex"] == "0"
    assert "IRONCLAD a6" in payload["label"] and "9 层" in payload["label"]
    assert payload["hpPoints"] == payload["maxPoints"] == 9
    assert payload["acts"] == ["A1", "A2"] and payload["actLines"] == 1
    assert payload["ymaxTicks"] == ["0", "40", "80"]            # 0 .. max HP 80, three clean ticks
    assert payload["ticks"].count("F5") == 1 and "F15" not in payload["ticks"]    # act 1 has 6 floors, act 2 has 3
    assert (payload["elite"], payload["boss"], payload["end"], payload["hit"]) == (2, 2, 1, 1)
    # x is monotone: floor i at ML + i * step over a fixed 48-floor domain
    xs = payload["hpX"]
    assert xs == sorted(xs) and xs[-1] < 370 and round(xs[1] - xs[0], 1) == round((380 - 34 - 10) / 47, 1)
    # y: lower HP is lower on the page (bigger y), the elite fight at A1F4 drops the line
    assert payload["hpY"][3] > payload["hpY"][2]
    # the three costliest rooms are labelled, in floor order: 22 (A1F6) < 30 (A1F4)... the top 3 are 50, 30, 30
    assert sorted(payload["labels"]) == ["−30", "−30", "−50"]


def test_a_refresh_with_unchanged_data_patches_in_place_and_a_changed_one_redraws(tmp_path: Path):
    first = _fixture_payload(tmp_path)
    newer = json.loads(json.dumps(first))
    newer["now"] += 60
    newer["activity"]["last_step_n"] = 13
    newer["activity"]["last_step_text"] = "map_select select_map_node"
    newer["log_tail"] = ["new line one", "new line two"]
    changed = json.loads(json.dumps(newer))
    changed["summary"]["runs"] = 9
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__FIRST__, __NEWER__, __CHANGED__];
              await APV.render({ view: 'advisor-play', character: '' });
              const body = byId('advisorPlayBody');
              const activity = () => allText(collect(body, (n) => n.className === 'adv-line' && allText(n).includes('自动驾驶'))[0]);
              const firstActivity = activity();
              const root = body.children[0];
              const [refresh1] = pending();
              await fire(refresh1);
              const patched = { sameRoot: body.children[0] === root, activity: activity(), pending: pending().map((t) => t.ms) };
              const [refresh2] = pending();
              await fire(refresh2);
              console.log(JSON.stringify({
                firstActivity, patched, redrawn: body.children[0] !== root,
                runsTile: allText(collect(body, (n) => n.className && n.className.startsWith('metric-card'))[1]),
                fetches,
              }));
            })();
            async function fire(timer) { timer.fired = true; await timer.fn(); }
            """,
            first=first, newer=newer, changed=changed,
        )
    )
    assert "#12 rest_site choose_option" in payload["firstActivity"]
    assert payload["patched"]["sameRoot"] is True                       # nothing redrawn: only the live bits
    assert "#13 map_select select_map_node" in payload["patched"]["activity"]
    assert payload["patched"]["pending"] == [60000]                     # and the next refresh is scheduled
    assert payload["redrawn"] is True and "9" in payload["runsTile"]
    assert payload["fetches"] == ["/api/advisor-play"] * 3


def test_a_failed_first_read_shows_an_error_and_a_failed_refresh_keeps_the_page(tmp_path: Path):
    ok = _fixture_payload(tmp_path)
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [new Error('HTTP 500'), __OK__, new Error('boom')];
              await APV.render({ view: 'advisor-play', character: '' });
              const body = byId('advisorPlayBody');
              const afterFail = { text: allText(body), states: collect(body, (n) => n.className === 'error-state').length,
                                  status: statuses[statuses.length - 1], pending: pending().map((t) => t.ms) };
              await fire(pending()[0]);
              const recovered = collect(body, (n) => n.tag === 'h2').length;
              await fire(pending()[0]);
              console.log(JSON.stringify({
                afterFail, recovered, keptPage: collect(body, (n) => n.tag === 'h2').length,
                notice: [byId('advisorPlayNotice').textContent, byId('advisorPlayNotice').hidden],
              }));
            })();
            async function fire(timer) { timer.fired = true; await timer.fn(); }
            """,
            ok=ok,
        )
    )
    assert "Advisor Play 读取失败：HTTP 500" in payload["afterFail"]["text"] and payload["afterFail"]["states"] == 1
    assert payload["afterFail"]["status"][1] == "error" and payload["afterFail"]["pending"] == [60000]
    assert payload["recovered"] == 8                                     # the next refresh drew the page
    assert payload["keptPage"] == 8                                      # a failing refresh leaves it alone...
    assert payload["notice"] == ["自动刷新失败：boom", False]            # ...and says so


def test_an_error_payload_is_shown_not_drawn(tmp_path: Path):
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__BAD__];
              await APV.render({ view: 'advisor-play', character: '' });
              console.log(JSON.stringify({ text: allText(byId('advisorPlayBody')), h2: collect(byId('advisorPlayBody'), (n) => n.tag === 'h2').length }));
            })();
            """,
            bad={"error": "RuntimeError: kaboom", "now": NOW, "problems": [], "runs": [], "characters": [], "current": None},
        )
    )
    assert "RuntimeError: kaboom" in payload["text"] and payload["h2"] == 0


def test_the_character_filter_navigates_and_a_new_filter_resets_the_page_size(tmp_path: Path):
    many = _fixture_payload(tmp_path, runs=4, shown=2)
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__MANY__, __MANY__, __MANY__];
              await APV.render({ view: 'advisor-play', character: 'SILENT' });
              const body = byId('advisorPlayBody');
              const select = collect(body, (n) => n.tag === 'select')[0];
              select.value = 'DEFECT';
              select.dispatch('change');
              const more = collect(body, (n) => n.tag === 'button' && n.textContent.startsWith('显示更多'))[0];
              const moreText = more.textContent;
              more.click();
              await new Promise((resolve) => setImmediate(resolve));
              await APV.render({ view: 'advisor-play', character: 'SILENT' });   // same filter again: keeps shown
              await APV.render({ view: 'advisor-play', character: 'REGENT' });   // another filter: back to the default
              console.log(JSON.stringify({
                fetches, navigateCalls, moreText, selected: select.value,
                cards: collect(body, (n) => (n.className || '').startsWith('adv-run-card')).length,
              }));
            })();
            """,
            many=many,
        )
    )
    assert payload["navigateCalls"] == ["#/advisor-play/DEFECT"]
    assert payload["moreText"] == "显示更多（已显示 3 / 5 局）"          # the run in progress + 2 curves, of 4 + 1 listed
    assert payload["fetches"][0] == "/api/advisor-play?character=SILENT"
    assert payload["fetches"][1] == "/api/advisor-play?character=SILENT&shown=60"      # 30 + 30 more
    assert payload["fetches"][2] == "/api/advisor-play?character=SILENT&shown=60"
    assert payload["fetches"][3] == "/api/advisor-play?character=REGENT"


def test_stop_ends_the_refresh_and_the_clock(tmp_path: Path):
    payload = _run_node_json(
        _view_script(
            """
            (async () => {
              responses = [__PAYLOAD__];
              await APV.render({ view: 'advisor-play', character: '' });
              const before = { timers: pending().length, intervals: intervals.filter((t) => !t.cleared).length };
              APV.stop();
              console.log(JSON.stringify({ before, after: { timers: pending().length, intervals: intervals.filter((t) => !t.cleared).length } }));
            })();
            """,
            payload=_fixture_payload(tmp_path),
        )
    )
    assert payload["before"] == {"timers": 1, "intervals": 1} and payload["after"] == {"timers": 0, "intervals": 0}


def test_formatting_helpers():
    payload = _run_node_json(
        _view_script(
            """
            const local = (y, mo, d, h, mi) => new Date(y, mo, d, h, mi).getTime() / 1000;
            console.log(JSON.stringify({
              duration: [APV.duration(0), APV.duration(59), APV.duration(60), APV.duration(2716), APV.duration(9028), APV.duration(3600), APV.duration(null), APV.duration(-1)],
              minutes: [APV.minutesText(90), APV.minutesText(0), APV.minutesText(-5), APV.minutesText(null), APV.minutesText(NaN)],
              stamp: [APV.stamp(local(2026, 10, 2, 9, 5)), APV.stamp(null)],
            }));
            """
        )
    )
    assert payload["duration"] == ["0分", "0分", "1分", "45分", "2时30分", "1时00分", "—", "—"]
    assert payload["minutes"] == ["1.5", "0.0", "0.0", "—", "—"]
    assert payload["stamp"] == ["11-02 09:05", "—"]
