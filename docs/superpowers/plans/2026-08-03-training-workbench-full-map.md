# Training Workbench Full Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every branch of a reconstructable STS2 act map, overlay the actual route, and annotate visited nodes with trustworthy gains and losses using original node art when locally available.

**Architecture:** Vendor the MIT-licensed deterministic map-generation core from `Akirakato1/Slay-the-Spire-2-dashboard` at a pinned upstream commit and wrap it with a small JSON-in/JSON-out Node CLI. A Python map service owns validation, version gating, subprocess execution, caching, and visited-route fallback. Canonical nodes receive typed delta bundles and resolved art metadata; the browser renders graph topology from the API without reimplementing map generation.

**Tech Stack:** Python 3.11, NVM-managed Node.js/CommonJS, vanilla SVG/JavaScript, local PNG assets, pytest.

---

## Execution Preconditions

- Complete `2026-08-03-training-workbench-foundation.md` first.
- Work in the same dedicated feature worktree; do not implement directly over the user's dirty `main` checkout.
- Use the active NVM-managed `node`. This plan requires no `npm install` and adds no package dependency.
- Never commit extracted game PNGs. Only license text, source code, synthetic fixtures, and tiny hand-authored test assets may enter Git.
- Preserve the map generator's supported-build declaration: upstream was ported from STS2 build `v0.103.2`.

### Task 1: Vendor The Pinned Map Generator With Attribution

**Files:**
- Create: `agent/run_workbench/vendor/akirakato_mapgen/LICENSE`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/UPSTREAM.md`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/act_config.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/generator.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/index.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/map_point.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/net_random.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/path_align.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/pruning.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/rng.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/shuffles.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/string_hash.js`
- Create: `agent/run_workbench/vendor/akirakato_mapgen/map_cli.js`
- Create: `tests/agent/run_workbench/test_map_vendor.py`

- [ ] **Step 1: Write the failing attribution and CLI-contract tests**

Test that `LICENSE` contains `Copyright (c) 2026 Akirakato1`, `UPSTREAM.md` contains the repository URL and exact commit `cc9a7ce13bbfe3fcef0d04899de705b1f69d0300`, and `map_cli.js` responds to one JSON object on stdin with one JSON object on stdout.

The CLI contract test sends:

```json
{
  "act_id": "ACT.OVERGROWTH",
  "act_index": 0,
  "seed": "map-contract-seed",
  "ascension": 0,
  "modifiers": [],
  "is_multiplayer": false,
  "visited": null,
  "allow_partial_path": false
}
```

Assert `schema_version == 1`, the start node is `Ancient`, at least one boss exists, every edge references a returned node id, and a second identical invocation produces byte-equivalent JSON after key sorting.

- [ ] **Step 2: Run the tests and confirm missing files**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_map_vendor.py -q
```

Expected: failures because the vendored directory and CLI do not exist.

- [ ] **Step 3: Copy the pinned MIT files and record provenance**

Copy the listed source files from the pinned upstream checkout without stripping copyright comments. Adapt the local `index.js` to remove the `render_svg.js` import and `renderRun()` function, then export only `generateActMap` and `seedToUint32`; this keeps the generator dependency-free and matches the listed vendored files. `UPSTREAM.md` must contain:

```markdown
# Upstream provenance

- Repository: https://github.com/Akirakato1/Slay-the-Spire-2-dashboard
- Commit: `cc9a7ce13bbfe3fcef0d04899de705b1f69d0300`
- Source directory: `scripts/mapgen/`
- License: MIT; see `LICENSE` in this directory.
- Generator port target: STS2 build `v0.103.2`.

Local changes: `map_cli.js` serializes graph objects to a stable JSON contract;
`index.js` is used for generation/alignment only and does not render upstream SVG.
```

Do not copy Electron code, bundled images, `render_svg.js`, resource extraction tools, or frontend source.

- [ ] **Step 4: Implement the JSON CLI**

`map_cli.js` must import `generateActMap` and `PointTypeName`, walk `graph.grid` plus the start/boss ghost nodes, and serialize Sets explicitly. Use stable ids `${col}:${row}` and type names from `PointTypeName`:

```javascript
function serializeGraph(graph, alignment) {
  const points = new Map();
  const add = (point) => {
    if (!point) return;
    points.set(`${point.coord.col}:${point.coord.row}`, point);
  };
  add(graph.startingPoint);
  add(graph.bossPoint);
  add(graph.secondBossPoint);
  for (const column of graph.grid) for (const point of column) add(point);

  const pathIds = new Map();
  if (alignment.ok) {
    alignment.path.forEach((point, index) => {
      pathIds.set(`${point.coord.col}:${point.coord.row}`, index);
    });
  }

  const nodes = [...points.entries()]
    .map(([id, point]) => ({
      id,
      col: point.coord.col,
      row: point.coord.row,
      room_type: PointTypeName[point.PointType],
      visited: pathIds.has(id),
      path_index: pathIds.get(id) ?? null,
    }))
    .sort((a, b) => a.row - b.row || a.col - b.col);

  const edges = [];
  for (const [from, point] of points) {
    for (const child of point.Children) {
      edges.push({from, to: `${child.coord.col}:${child.coord.row}`});
    }
  }
  edges.sort((a, b) => a.from.localeCompare(b.from) || a.to.localeCompare(b.to));
  return {nodes, edges};
}
```

Return alignment as `{ok, ambiguous, reason, path_node_ids}`. Catch all errors and print `{schema_version: 1, ok: false, error: "..."}` with a nonzero exit code. Never write diagnostics to stdout.

- [ ] **Step 5: Run vendor tests directly with the active Node**

Run:

```bash
command -v node
node --version
.venv/bin/python -m pytest tests/agent/run_workbench/test_map_vendor.py -q
```

Expected: Node comes from the NVM installation and all vendor tests pass.

- [ ] **Step 6: Commit the vendored generator**

```bash
git add agent/run_workbench/vendor/akirakato_mapgen tests/agent/run_workbench/test_map_vendor.py
git commit -m "feat: vendor deterministic STS2 map generator"
```

### Task 2: Wrap Generation With Version And Alignment Safety

**Files:**
- Create: `agent/run_workbench/map_service.py`
- Create: `tests/agent/run_workbench/test_map_service.py`
- Create: `tests/fixtures/run_workbench/map_v01032_partial.run`
- Create: `tests/fixtures/run_workbench/map_v01032_request.json`
- Create: `tests/fixtures/run_workbench/map_v01032_expected.json`
- Modify: `agent/run_workbench/models.py`
- Modify: `tests/agent/run_workbench/test_models.py`

- [ ] **Step 1: Extend the canonical map model tests**

Add immutable `MapNode`, `MapEdge`, `MapAlignment`, and `ActMap` contracts. A map payload must distinguish `full_map`, `visited_route`, and fallback reason:

```python
payload = result.to_dict()
assert payload["act_id"] == "ACT.OVERGROWTH"
assert payload["full_map"] is True
assert payload["alignment"]["ok"] is True
assert payload["alignment"]["ambiguous"] is False
assert all(node["visited"] is False or node["path_index"] is not None for node in payload["nodes"])
```

- [ ] **Step 2: Freeze one deterministic golden fixture**

Use build `v0.103.2`, seed `map-contract-seed`, Act 1 `ACT.OVERGROWTH`, and ascension 0. Generate the graph once, follow the first child in sorted `(row, col)` order from Ancient to Boss, and express that room-type sequence as synthetic `map_point_history` in `map_v01032_partial.run`. Store the corresponding direct CLI payload in `map_v01032_request.json`, run the CLI with that request, and store the complete JSON result as `map_v01032_expected.json`.

The Python test must compare actual nodes, edges, and aligned path ids against this committed golden JSON. This freezes upstream behavior and prevents later accidental RNG/order drift.

- [ ] **Step 3: Write failing service tests**

Cover:

- known `v0.103.2` input returns the golden full graph and aligned route;
- a second request hits the in-memory cache;
- missing seed/act/visited history returns a visited-route fallback with a human-readable reason;
- build `v0.104.0` is rejected as unsupported unless explicitly added to the supported-build set;
- `alignment.ok == false` and `alignment.ambiguous == true` both fall back rather than claiming an authoritative full map;
- Node timeout, missing executable, nonzero exit, and invalid stdout become typed map-service errors;
- multiplayer input is rejected in this release even though the vendored generator can accept it.

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_models.py tests/agent/run_workbench/test_map_service.py -q
```

Expected: missing map models/service and golden behavior.

- [ ] **Step 5: Implement the Python service**

Expose:

```python
SUPPORTED_MAP_BUILDS = frozenset({"v0.103.2"})


@dataclass(frozen=True)
class MapRequest:
    run_id: str
    act_id: str
    act_index: int
    seed: str | None
    game_version: str | None
    ascension: int | None
    modifiers: tuple[str, ...]
    is_multiplayer: bool
    visited: tuple[dict, ...]
    allow_partial_path: bool


class MapService:
    def generate(self, request: MapRequest) -> ActMap: ...
```

Invoke Node using an argument list and stdin; do not use `shell=True`:

```python
completed = subprocess.run(
    [self.node_executable, str(self.cli_path)],
    input=json.dumps(payload),
    text=True,
    capture_output=True,
    timeout=5,
    check=False,
)
```

Cache by the fully serialized request. A result is authoritative only when the build is supported, alignment succeeds, alignment is not ambiguous, and every visited entry maps to one path node. Otherwise call `visited_route_map(request, reason=...)`, which returns only ordered visited nodes/edges with `full_map=False`.

- [ ] **Step 6: Run service and vendor tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_models.py tests/agent/run_workbench/test_map_vendor.py tests/agent/run_workbench/test_map_service.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the safe map service**

```bash
git add agent/run_workbench/models.py agent/run_workbench/map_service.py tests/agent/run_workbench/test_models.py tests/agent/run_workbench/test_map_service.py tests/fixtures/run_workbench/map_v01032_partial.run tests/fixtures/run_workbench/map_v01032_request.json tests/fixtures/run_workbench/map_v01032_expected.json
git commit -m "feat: reconstruct validated act maps"
```

### Task 3: Compute Visited-Node Gains And Losses

**Files:**
- Create: `agent/run_workbench/deltas.py`
- Create: `tests/agent/run_workbench/test_deltas.py`
- Modify: `agent/run_workbench/adapters.py`
- Modify: `tests/agent/run_workbench/test_adapters.py`

- [ ] **Step 1: Write failing native-delta tests**

Build two adjacent native nodes with `player_stats[0]`. Verify:

- `damage_taken` and `hp_healed` are exact;
- `current_hp`, `max_hp`, and `current_gold` are exact snapshots;
- net HP and gold changes from adjacent snapshots are derived;
- `cards_gained`, `cards_removed`, `cards_transformed`, `cards_enchanted`, `upgraded_cards`, picked `relic_choices`, and picked `potion_choices` are exact event lists;
- missing fields produce `{value: null, quality: "unknown"}`;
- the first recorded node never gets a fabricated zero delta.

The bundle contract must include:

```python
NodeDeltas(
    hp_before=RunDelta(...),
    hp_after=RunDelta(...),
    hp_change=RunDelta(...),
    max_hp_change=RunDelta(...),
    gold_change=RunDelta(...),
    damage_taken=RunDelta(...),
    hp_healed=RunDelta(...),
    cards_gained=RunDelta(...),
    cards_removed=RunDelta(...),
    cards_transformed=RunDelta(...),
    cards_enchanted=RunDelta(...),
    cards_upgraded=RunDelta(...),
    relics_gained=RunDelta(...),
    potions_gained=RunDelta(...),
    potions_used=RunDelta(...),
    potions_discarded=RunDelta(...),
)
```

- [ ] **Step 2: Write failing replay-delta tests**

Feed adjacent GameLogger room snapshots into a pure `derive_snapshot_deltas` helper. HP, max HP, gold, deck, relic, and potion differences are `derived`; unavailable inventory is unknown. Preserve the existing parser's room ids so map nodes can link to replay details later.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_deltas.py tests/agent/run_workbench/test_adapters.py -q
```

Expected: missing delta helpers and fields.

- [ ] **Step 4: Implement exact and derived delta helpers**

Expose `native_node_deltas(node, previous_node, *, player_index=0)` and `derive_snapshot_deltas(snapshot, previous_snapshot)`. Empty observed lists are exact empty lists; absent keys are unknown. This distinction is required for both cards and consumables.

When adapting native history, assign stable ids `a{act_index}:n{node_index}` and retain raw `map_point_type`, room model id, monster ids, choices, and the delta bundle. When adapting replay rooms, retain the legacy room id and derived delta bundle.

- [ ] **Step 5: Run delta, adapter, and legacy replay tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_deltas.py tests/agent/run_workbench/test_adapters.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit node deltas**

```bash
git add agent/run_workbench/deltas.py agent/run_workbench/adapters.py tests/agent/run_workbench/test_deltas.py tests/agent/run_workbench/test_adapters.py
git commit -m "feat: annotate run nodes with value deltas"
```

### Task 4: Resolve Original Art With Emoji And Letter Fallbacks

**Files:**
- Create: `agent/run_workbench/assets.py`
- Create: `tests/agent/run_workbench/test_assets.py`
- Create: `tests/fixtures/run_workbench/map_assets/map_icons/map_monster.png`
- Modify: `agent/run_progress_viewer.py`
- Modify: `tests/agent/run_workbench/test_http_api.py`

- [ ] **Step 1: Write failing resolver tests**

Test this exact resolution order:

1. configured local PNG exists: `kind="original"`;
2. file missing: `kind="emoji"` with semantic emoji and letter label;
3. unknown room type: `kind="letter"` with `?`.

Cover filename mapping:

```python
ROOM_ART = {
    "ancient": "ancient_node_neow.png",
    "monster": "map_monster.png",
    "elite": "map_elite.png",
    "boss": "map_chest_boss.png",
    "shop": "map_shop.png",
    "rest_site": "map_rest.png",
    "treasure": "map_chest.png",
    "unknown": "map_unknown.png",
}
```

Boss/Ancient model-specific filenames take precedence only after sanitizing the model id to a known basename. Tests must reject `../`, absolute paths, and encoded traversal.

- [ ] **Step 2: Write failing HTTP image tests**

Test `GET /api/node-art?room_type=monster` returns `image/png` from the configured fixture root. Missing art returns `404` JSON; the frontend uses the already returned emoji and letter and does not treat 404 as a map failure.

- [ ] **Step 3: Run and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_assets.py tests/agent/run_workbench/test_http_api.py -q
```

Expected: resolver and endpoint do not exist.

- [ ] **Step 4: Implement the resolver and safe asset roots**

`NodeArtResolver` accepts explicit roots and may discover these local caches in order:

1. CLI `--map-assets-dir` value;
2. `STS2_MAP_ASSET_DIR` environment variable;
3. `~/Library/Application Support/STS2 Dashboard/Assets/images`;
4. `~/Library/Application Support/sts2-dashboard/Assets/images`.

Do not scan the whole home directory. Do not extract the game PCK in this plan. The locally cached files produced by the upstream dashboard are compatible and count as original art. If no cache exists, emoji must render immediately.

Use Chinese tooltips and accessible labels:

```python
ROOM_FALLBACK = {
    "ancient": ("🌀", "A", "远古事件"),
    "monster": ("⚔️", "M", "普通战斗"),
    "elite": ("👹", "E", "精英战斗"),
    "boss": ("💀", "B", "首领战斗"),
    "shop": ("🛒", "$", "商店"),
    "rest_site": ("🔥", "R", "休息点"),
    "treasure": ("🎁", "T", "宝箱"),
    "unknown": ("❓", "?", "未知事件"),
}
```

- [ ] **Step 5: Add the safe binary endpoint and CLI option**

Add `--map-assets-dir` to `run_progress_viewer.py`. The handler asks the resolver for a known room/model pair and streams only the resolved PNG path. It never accepts a filesystem path from the query string.

- [ ] **Step 6: Run asset and API tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_assets.py tests/agent/run_workbench/test_http_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit art resolution**

```bash
git add agent/run_workbench/assets.py agent/run_progress_viewer.py tests/agent/run_workbench/test_assets.py tests/agent/run_workbench/test_http_api.py tests/fixtures/run_workbench/map_assets/map_icons/map_monster.png
git commit -m "feat: resolve map node artwork safely"
```

### Task 5: Expose And Render The Complete Map

**Files:**
- Create: `agent/run_workbench/static/map.js`
- Modify: `agent/run_workbench/static/index.html`
- Modify: `agent/run_workbench/static/styles.css`
- Modify: `agent/run_workbench/static/app.js`
- Modify: `agent/run_progress_viewer.py`
- Modify: `tests/agent/run_workbench/test_http_api.py`
- Modify: `tests/agent/run_workbench/test_static_contract.py`

- [ ] **Step 1: Write failing map API tests**

Add:

```text
GET /api/run/map?id=<run_id>&act=0
```

Verify a supported native run returns `full_map=true`, all nodes/edges, route alignment, node art descriptors, and deltas only on visited nodes. Verify a historical replay without map metadata returns `full_map=false`, its recorded route, and `fallback_reason`. Verify unvisited nodes contain no fabricated deltas or rewards.

- [ ] **Step 2: Write failing static contract tests**

Require Act tabs, a map SVG container, legend, fallback banner, Act summary, and selected-node target:

```text
runMapPage actTabs mapFallback mapSvg mapLegend actSummary selectedNodeSummary
```

Require `index.html` to load `/static/map.js` after `/static/app.js`.

- [ ] **Step 3: Run the tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_http_api.py tests/agent/run_workbench/test_static_contract.py -q
```

Expected: map endpoint and DOM contracts are absent.

- [ ] **Step 4: Add the map endpoint**

Resolve the canonical run through `RunCatalog`, build `MapRequest` from its native metadata/history, attach delta/art data by aligned `path_index`, and return a stable payload. If a run has multiple source records, the joined canonical record is used; never select a source by filename convention.

- [ ] **Step 5: Render the graph in SVG**

`map.js` must:

- compute SVG coordinates from integer `col` and `row` with one shared transform;
- draw neutral edges first, then gold visited-route edges;
- draw unvisited nodes neutrally;
- draw visited nodes with original `<image>` art when `kind="original"`, otherwise emoji text, with a tiny letter accessibility label;
- mark the terminal death/current node red;
- place compact visited-node badges for nonzero exact/derived HP, max HP, gold, cards, relics, potions, upgrades, removals, or transforms;
- make visited nodes keyboard-focusable and clickable;
- leave unvisited nodes without reward badges.

Use returned `quality` to distinguish exact and derived values in tooltips. Render `unknown` as `—`.

- [ ] **Step 6: Connect dashboard rows to the run map**

Clicking a representative run or run catalog row opens `runMapPage`, preserves current dashboard filters in JavaScript state, loads Act 0, and shows the map before any transcript/detail section. Browser back returns to the same cohort selection.

- [ ] **Step 7: Run map UI and API tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_http_api.py tests/agent/run_workbench/test_static_contract.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit full-map rendering**

```bash
git add agent/run_workbench/static agent/run_progress_viewer.py tests/agent/run_workbench/test_http_api.py tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: render full run maps and visited routes"
```

### Task 6: Full-Map Regression And Browser Acceptance

**Files:**
- Modify only if verification exposes a defect in files already listed above.

- [ ] **Step 1: Run all map/workbench tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete agent regression set**

Run: `.venv/bin/python -m pytest tests/agent -q`

Expected: all tests pass.

- [ ] **Step 3: Verify deterministic output outside pytest**

Run the CLI twice against the golden request and compare normalized JSON:

```bash
node agent/run_workbench/vendor/akirakato_mapgen/map_cli.js < tests/fixtures/run_workbench/map_v01032_request.json > /tmp/sts2-map-a.json
node agent/run_workbench/vendor/akirakato_mapgen/map_cli.js < tests/fixtures/run_workbench/map_v01032_request.json > /tmp/sts2-map-b.json
diff -u /tmp/sts2-map-a.json /tmp/sts2-map-b.json
```

Expected: `diff` exits 0 and both files match `map_v01032_expected.json` after JSON normalization.

- [ ] **Step 4: Browser acceptance with one full and one fallback run**

Start the viewer on a disposable port and verify:

- known `v0.103.2` native fixture shows all branches and a gold route;
- an unsupported build visibly falls back to its visited route;
- every visited node has real/derived badges only when data exists;
- unvisited nodes have topology/type only;
- configured original Monster art loads;
- missing Elite art falls back to emoji without breaking the map;
- selecting a visited node updates the summary target;
- dashboard filters remain after returning from the run.

- [ ] **Step 5: Check repository hygiene**

Run:

```bash
git diff --check
git status --short
find agent/run_workbench -type f \( -name '*.png' -o -name '*.jpg' \) -print
```

Expected: the only committed image is the tiny synthetic test PNG. No extracted game artwork, app cache, logs, checkpoints, or `.superpowers/` files are staged.

- [ ] **Step 6: Record the map boundary**

The handoff must state the exact supported map build set, the upstream commit/license, whether an original-art cache was detected, and which runs used visited-route fallback. Turn replay and diagnostic facts are completed by the final plan.
