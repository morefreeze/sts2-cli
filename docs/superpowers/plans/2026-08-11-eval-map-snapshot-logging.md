# Eval Map Snapshot Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every new full-run evaluation persist the authoritative map graph, visited route, and each visited node's entry/latest-exit deck, relic, potion, HP, and gold state—even when the run ends in defeat or a technical failure—so the workbench can display the exact v0.107.1 run without seed reconstruction.

**Architecture:** `run_eval_verbose` explicitly opts evaluation environments into a bounded capture mode. `CombatEnv` polls the existing read-only `get_map` command as decisions advance, keeps only the latest graph for each of at most four acts, and attaches first-entry/latest-exit player snapshots to visited coordinates before flushing `map_snapshot` rows ahead of the terminal outcome. A new strict workbench parser converts those rows into the existing `ActMap` model and canonical route nodes; the deck-history adapter exposes their capabilities and the map API prefers this recorded authority over the pinned v0.103.2 generator.

**Tech Stack:** Python 3.11, existing .NET `get_map` JSON protocol, immutable workbench dataclasses, vanilla HTTP/JavaScript frontend, pytest, NVM Node.js for existing static/map regressions.

---

## Execution Preconditions

- Work only in `/Users/bytedance/mygit/sts2-cli/.worktrees/version-character-filters` on `codex/version-character-filters`; preserve the user's main checkout and unrelated data.
- The approved behavior is evaluation-only capture. Training-created `CombatEnv` instances remain capture-disabled unless their `run_context` explicitly contains `capture_map=True`.
- Do not rewrite historical `deck_history.jsonl` rows. The new event is additive and older runs keep their current recorded-route fallback.
- Do not use the pinned v0.103.2 generator as authority for v0.107.1. A valid recorded graph wins; malformed recorded data fails closed to the existing fallback.
- Keep gameplay independent of observability: a `get_map` or logging error records a warning but never changes an action, reward, status, or evaluation result.
- Use `/Users/bytedance/mygit/sts2-cli/.venv/bin/python` and `/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node`. HTTP tests need permission to bind `127.0.0.1:0`.

### Task 1: Capture Bounded Authoritative Maps And Node Inventories

**Files:**
- Modify: `agent/combat_env.py`
- Modify: `agent/eval_rl.py`
- Modify: `tests/agent/test_combat_env.py`
- Modify: `tests/agent/test_eval_rl.py`

- [ ] **Step 1: Add failing producer-contract tests**

In `tests/agent/test_combat_env.py`, add a small map reply factory with two branches and a visited current coordinate:

```python
def _map_reply(*, act=1, current=(1, 1), visited=((0, 0), (1, 1))):
    visited_set = set(visited)
    return {
        "type": "map",
        "context": {"act": act, "floor": 2, "room_type": "Monster"},
        "rows": [
            [{"col": 0, "row": 0, "type": "Ancient",
              "children": [{"col": 1, "row": 1}, {"col": 2, "row": 1}],
              "visited": (0, 0) in visited_set, "current": current == (0, 0)}],
            [
                {"col": 1, "row": 1, "type": "Monster", "children": [],
                 "visited": (1, 1) in visited_set, "current": current == (1, 1)},
                {"col": 2, "row": 1, "type": "Shop", "children": [],
                 "visited": (2, 1) in visited_set, "current": current == (2, 1)},
            ],
        ],
        "boss": {"col": 0, "row": 17, "type": "Boss", "id": "BOSS.TEST"},
        "current_coord": {"col": current[0], "row": current[1]},
    }
```

Add tests proving:

1. a default/training `CombatEnv` never sends `{"cmd": "get_map"}`;
2. `run_context={"capture_map": True}` captures a valid reply without changing the decision state;
3. a second graph for the same act replaces the raw graph rather than appending a second full copy;
4. the first state at a coordinate becomes `entry_player`, while later states at that same coordinate only replace `exit_player`;
5. a later coordinate appends one ordered visited-node entry;
6. deck/relic/potion collections are copied, so mutating the original state after capture cannot mutate the buffered row;
7. collections over 256 entries and non-finite/unsafe values are omitted from that inventory field rather than crashing or partially fabricating it;
8. at most four act snapshots are retained; invalid act numbers and malformed map replies are ignored with a visible logging warning; and
9. `_emit_run_outcome(..., status="dead")` and each technical status (`crash`, `timeout`, `stuck`, `invalid`, `reset_failure`) writes `run_start`, then all `map_snapshot` rows in act order, then the buffered decision rows and final `outcome`, even if `_send` is no longer available.

Extend `_expected_run_metadata()` so every emitted row includes exact `is_multiplayer=False`. Add a reset-failure test showing a failure before the first valid map writes no fabricated `map_snapshot`.

In `tests/agent/test_eval_rl.py`, assert the exact `run_context` passed to every evaluation `CombatEnv` includes:

```python
{
    "capture_map": True,
    "run_id": expected_run_id,
    "checkpoint": expected_checkpoint,
    "evaluation_mode": expected_mode,
    "scenario": expected_scenario,
    "game_version": expected_version,
    "game_version_source": expected_source,
}
```

- [ ] **Step 2: Run the tests and observe the missing behavior**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/test_combat_env.py \
  tests/agent/test_eval_rl.py -q
```

Expected: the new assertions fail because eval does not opt in, `CombatEnv` has no map buffer/capture helper, metadata omits `is_multiplayer`, and outcomes do not flush map rows.

- [ ] **Step 3: Add the capture state and bounded inventory helper**

In `CombatEnv.__init__`, initialize capture state without leaking the internal flag into emitted comparison metadata:

```python
self._capture_run_maps = self._run_context.get("capture_map") is True
self._run_map_snapshots: dict[int, dict] = {}
self._run_current_map_coord: tuple[int, int, int] | None = None
```

Reset both map fields whenever a fresh run starts. Add constants near the existing logging helpers:

```python
_MAX_RECORDED_ACTS = 4
_MAX_RECORDED_MAP_NODES = 256
_MAX_RECORDED_INVENTORY_ITEMS = 256
```

Implement `_bounded_player_snapshot(state)` so it reads only `state["player"]`, copies finite `hp`, `max_hp`, and `gold`, and deep-copies JSON-safe `deck`, `relics`/`relic_items`, and `potions`/`potion_items` lists when their length is at most 256. Reject booleans as numbers. An invalid field becomes absent; it must not reject other trustworthy fields.

Add a helper that updates node inventory without talking to the child process:

```python
def _update_buffered_node_inventory(self, state: dict | None) -> None:
    key = self._run_current_map_coord
    if not self._capture_run_maps or key is None or not isinstance(state, dict):
        return
    act, col, row = key
    snapshot = self._run_map_snapshots.get(act)
    if snapshot is None:
        return
    player = self._bounded_player_snapshot(state)
    if not player:
        return
    node = snapshot["visited_by_coord"].get((col, row))
    if node is None:
        return
    node.setdefault("entry_player", deepcopy(player))
    node["exit_player"] = deepcopy(player)
```

- [ ] **Step 4: Capture the map without perturbing gameplay**

Implement `_capture_run_map_state(state)` with this sequence:

1. update the previously known current coordinate from `state` so a map-selection or terminal state closes the prior node;
2. if capture is disabled, stop without calling `_send`;
3. call `_send({"cmd": "get_map"})` inside a broad observability-only exception boundary;
4. require exact `type == "map"`, `context.act` to be an integer in `1..4`, `rows` to be a list, and at most 256 flattened nodes;
5. require `current_coord` exact integer `col`/`row`, find that coordinate in the returned rows, and reject inconsistent current markers;
6. replace only the raw graph for that act while preserving its existing `visited_nodes`/coordinate inventory table;
7. append a coordinate to `visited_nodes` only on first observation, copying its `type`, `col`, and `row`; update `_run_current_map_coord`; and
8. apply `_update_buffered_node_inventory(state)` so the new coordinate receives its first entry and latest exit snapshot.

The retained per-act shape should be directly serializable at outcome time:

```python
{
    **self._run_metadata_row("map_snapshot"),
    "act": act,
    "map": bounded_map_reply,
    "visited_nodes": [
        {
            "col": 0,
            "row": 0,
            "room_type": "Ancient",
            "entry_player": {...},
            "exit_player": {...},
        }
    ],
    "ts": finite_timestamp,
}
```

Store the coordinate lookup only as an internal dict; strip it when serializing. Use the existing `_report_run_logging_error` path for malformed/unavailable capture. Never raise the capture failure back into `reset`, `step`, `_advance_to_combat`, or `_emit_run_outcome`.

Call `_capture_run_map_state(state)` at the top of every `_advance_to_combat` iteration, after every `_send(cmd)`, and after `_greedy_use_potions` returns. This ensures noncombat rooms and card rewards update the current node, while the first decision after a coordinate transition opens the next node. Before forming terminal rows, call only `_update_buffered_node_inventory(state)`; do not make a terminal-time `get_map` call that would lose an already buffered graph if the child has crashed.

- [ ] **Step 5: Flush snapshots atomically with outcomes and opt eval in**

Add `"is_multiplayer": False` to `_run_metadata_row`. In `_emit_run_outcome`, serialize immutable copies of `self._run_map_snapshots[act]` between a retried `run_start` and the existing milestone/card-pick rows:

```python
rows.extend(self._serialized_run_map_snapshots())
rows.extend(self._run_milestone_records)
rows.extend(self._run_card_pick_records)
rows.append(outcome)
```

Do not clear the buffer until the append succeeds, preserving the current retry semantics when file I/O fails. In `run_eval_verbose`, add `"capture_map": True` to `run_context`; do not add it to eval-result rows, cohort metadata, or comparison axes.

- [ ] **Step 6: Run producer tests and commit**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/test_combat_env.py \
  tests/agent/test_eval_rl.py -q
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m compileall -q \
  agent/combat_env.py agent/eval_rl.py
git diff --check
```

Expected: all focused tests pass, including every failure status and the disabled-by-default case.

Commit:

```bash
git add agent/combat_env.py agent/eval_rl.py \
  tests/agent/test_combat_env.py tests/agent/test_eval_rl.py
git commit -m "feat: persist evaluation map snapshots"
```

### Task 2: Parse Recorded Graphs Into Existing Map And Delta Contracts

**Files:**
- Create: `agent/run_workbench/recorded_maps.py`
- Create: `tests/agent/run_workbench/test_recorded_maps.py`

- [ ] **Step 1: Write strict parser RED tests**

Create a fixture builder that emits one `map_snapshot` row containing an Ancient start node, two room branches, a boss, and a visited route. Give each visited node an `entry_player` and `exit_player` where the exit adds one card and one relic.

Tests must assert:

```python
snapshot = parse_recorded_map_row(row)
assert snapshot.act_index == 0
assert snapshot.act_map.full_map is True
assert snapshot.act_map.visited_route is True
assert len(snapshot.act_map.nodes) == 4
assert {(edge.from_id, edge.to_id) for edge in snapshot.act_map.edges} == {
    ("recorded:0:0", "recorded:1:1"),
    ("recorded:0:0", "recorded:2:1"),
    ("recorded:1:1", "recorded:0:17"),
    ("recorded:2:1", "recorded:0:17"),
}
assert snapshot.act_map.alignment.path_node_ids == (
    "recorded:0:0", "recorded:1:1", "recorded:0:17"
)
assert snapshot.route_nodes[1]["deltas"]["cards_gained"]["value"]
assert snapshot.route_nodes[1]["deltas"]["relics_gained"]["value"]
```

Parameterize malformed rows for: wrong event; act outside `1..4`; non-map payload; more than 256 nodes; more than 2048 edges; duplicate coordinates; duplicate/self/dangling edges; missing child coordinates; non-integer/bool coordinates; multiple current nodes; inconsistent `current_coord`; invalid/non-bool visited flags; a visited-node coordinate not in the graph; duplicate visited coordinates; disconnected or out-of-order visited routes; identifiers/room types over 64 characters; unsafe nested inventory values; and non-finite timestamps.

Add a latest-wins test where two valid rows for act 1 and one for act 2 produce exactly two snapshots, with the later act-1 row selected. An invalid later row must not erase the earlier valid row.

- [ ] **Step 2: Run the new tests and confirm the module is absent**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_recorded_maps.py -q
```

Expected: collection fails because `agent.run_workbench.recorded_maps` does not exist.

- [ ] **Step 3: Implement immutable recorded-map parsing**

Create:

```python
@dataclass(frozen=True)
class RecordedActSnapshot:
    act_index: int
    act_id: str
    act_map: ActMap
    route_nodes: tuple[dict[str, Any], ...]

class RecordedMapError(ValueError):
    pass

def parse_recorded_map_row(row: Mapping[str, Any]) -> RecordedActSnapshot: ...

def latest_recorded_acts(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[int, RecordedActSnapshot], tuple[str, ...]]: ...
```

Normalize coordinate ids as `recorded:{col}:{row}`. Flatten `map.rows`, include `map.boss`, and make boss edges explicit when ordinary terminal nodes list the boss as a child. Normalize known room names to the existing map vocabulary: `Ancient`, `Monster`, `Elite`, `Boss`, `Shop`, `RestSite`, `Treasure`, and `Unknown`; an unknown bounded type becomes `Unknown`, not executable/art input.

Use `visited_nodes` order as the route authority. For each route entry:

```python
route_node = {
    "id": f"a{act_index}:n{path_index}",
    "act": act_index + 1,
    "act_index": act_index,
    "floor": path_index + 1,
    "global_floor": act_index * 17 + path_index + 1,
    "col": col,
    "row": row,
    "map_point_type": room_type,
    "room_type": room_type,
    "start_player": deepcopy(entry_player),
    "end_player": deepcopy(exit_player),
    "deltas": derive_snapshot_deltas(exit_player, entry_player).to_dict(),
}
```

For a boss, retain its bounded `id` as `model_id`. Create `MapNode` instances with `visited=True`/sequential `path_index` for route coordinates, `MapEdge` instances for the complete graph, and `MapAlignment(ok=True, ambiguous=False, path_node_ids=...)`. Set `ActMap(full_map=True, visited_route=bool(route))` with a stable recorded act id such as `RECORDED.ACT.1`; do not claim generator support.

All parser output must be detached from mutable input, JSON-safe with `allow_nan=False`, and bounded before it reaches an adapter or cache. `latest_recorded_acts` must consume its iterable once, keep the latest valid row for each act, collect bounded public errors, and never let one malformed row erase a previous valid snapshot.

- [ ] **Step 4: Run parser tests and commit**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_recorded_maps.py \
  tests/agent/run_workbench/test_deltas.py \
  tests/agent/run_workbench/test_models.py -q
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m compileall -q \
  agent/run_workbench/recorded_maps.py
git diff --check
```

Commit:

```bash
git add agent/run_workbench/recorded_maps.py \
  tests/agent/run_workbench/test_recorded_maps.py
git commit -m "feat: parse recorded act map snapshots"
```

### Task 3: Expose Recorded Acts, Routes, Inventories, And Capabilities

**Files:**
- Modify: `agent/run_workbench/adapters.py`
- Modify: `tests/agent/run_workbench/test_adapters.py`
- Modify: `tests/agent/run_workbench/test_details.py`

- [ ] **Step 1: Write adapter and node-detail RED tests**

Build one deck-history run with `run_start`, two valid `map_snapshot` acts, an ordinary `card_pick`, and `outcome`. Assert the adapted record has:

```python
assert record.metadata.is_multiplayer is False
assert record.capabilities.full_map is True
assert record.capabilities.visited_route is True
assert record.capabilities.node_rewards is True
assert len(record.acts) == 2
route_nodes = [
    node for node in record.nodes
    if node.get("_workbench_evidence_kind") == "route_node"
]
assert [node["id"] for node in route_nodes] == ["a0:n0", "a0:n1", "a1:n0"]
assert any(node.get("event") == "card_pick" for node in record.nodes)
```

Assert the route nodes have typed `SourceKind.DECK_HISTORY` provenance while raw `map_snapshot` and `card_pick` rows remain `deck_history_event` evidence. This keeps `_canonical_route_nodes` from mistaking raw rows for floors.

In `test_details.py`, build a selected recorded node and assert `build_node_detail` displays its exact entry/exit deck, relics, potions, HP, and gold, with the precomputed `NodeDeltas`. A mixed or duplicate-provenance node must keep the existing conservative basic-detail fallback.

Add historical deck-history regression tests with no map rows and malformed map rows: both remain readable, do not claim full-map capability, and preserve existing warnings/outcome data.

- [ ] **Step 2: Run focused tests and observe missing capabilities**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_details.py -q
```

Expected: new assertions fail because `_adapt_deck_run` treats every row only as a deck-history event and metadata does not retain `is_multiplayer`.

- [ ] **Step 3: Integrate parser output without trusting raw marker fields**

In `_metadata_from_records`, accept `is_multiplayer` only when an observed value has exact `bool` type. Conflicting exact booleans must enter the existing typed comparison-conflict path or conservatively become `None`; never coerce strings/numbers.

In `_adapt_deck_run`:

1. call `latest_recorded_acts(records)` once;
2. convert each `RecordedActSnapshot.route_nodes` item into canonical route evidence with `_workbench_evidence_kind="route_node"` and the existing typed `_single_source_node_provenance` sidecar;
3. keep all original rows as annotated deck-history evidence after the derived route nodes;
4. expose one act descriptor per parsed act;
5. set `Capabilities.full_map`, `visited_route`, and `node_rewards` from validated snapshots/deltas, while retaining existing `decisions` behavior; and
6. append bounded parser errors to warnings without leaking raw inventory or local paths.

Do not teach details or the map endpoint to trust client-controlled raw `_workbench_*` strings. The adapter must build the existing typed `NodeOrigin` sidecar for derived route nodes, so `build_node_detail` follows its current provenance rules.

- [ ] **Step 4: Run adapter/details regressions and commit**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_recorded_maps.py \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_details.py \
  tests/agent/run_workbench/test_joiner.py \
  tests/agent/run_workbench/test_catalog.py -q
git diff --check
```

Commit:

```bash
git add agent/run_workbench/adapters.py \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_details.py
git commit -m "feat: expose recorded evaluation routes"
```

### Task 4: Prefer Recorded Full Maps In The HTTP Workbench

**Files:**
- Modify: `agent/run_progress_viewer.py`
- Modify: `tests/agent/run_workbench/test_http_api.py`
- Modify: `tests/agent/run_workbench/test_static_contract.py` only if an existing frontend assertion needs a fixture update; do not change the browser payload format.

- [ ] **Step 1: Write the v0.107.1 recorded-map HTTP RED test**

Create a temporary deck-history source containing a v0.107.1 run with a branched `map_snapshot`, node inventories, and a dead outcome. Inject a map service whose `generate` method fails if invoked:

```python
class MustNotGenerate:
    def generate(self, request):
        raise AssertionError("recorded map must bypass the v0.103.2 generator")
```

Call `/api/run/map?run_id=<id>&act=0` and assert HTTP 200 plus:

```python
assert payload["full_map"] is True
assert payload["visited_route"] is True
assert payload["fallback_reason"] is None
assert payload["summary"] == {
    "node_count": 4,
    "edge_count": 4,
    "visited_count": 3,
    "terminal_node_id": "recorded:0:17",
}
assert any(node["visited"] is False for node in payload["nodes"])
visited = sorted(
    (node for node in payload["nodes"] if node["visited"]),
    key=lambda node: node["path_index"],
)
assert visited[1]["recorded_node_id"] == "a0:n1"
assert visited[1]["deltas"]["cards_gained"]["quality"] == "derived"
```

Also test: two acts produce working tabs; a malformed newest row falls back to the previous valid recorded row; an entirely malformed recording invokes the existing `MapService`/visited-route fallback rather than returning 500; unknown act indices remain 404/400 under the current API contract.

- [ ] **Step 2: Run the HTTP test and confirm generator use/fallback**

Run with loopback permission and the explicit Node path:

```bash
env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_http_api.py -q
```

Expected: the new test fails because `_run_map_payload` always builds `MapRequest` and calls `MapService.generate`.

- [ ] **Step 3: Add recorded authority before reconstruction**

In `_run_map_payload`, obtain the joined run once, scan only raw deck-history `map_snapshot` evidence with `latest_recorded_acts`, and select `act_index`. When valid, use `recorded.act_map` directly; do not create or invoke a generator request. When absent/invalid, execute the current `MapRequest` and exception-to-route-fallback path unchanged.

Then pass both recorded and generated `ActMap` instances through the same existing payload decoration:

- map visited nodes to canonical `recorded_nodes[path_index]`;
- attach copied deltas and `recorded_node_id` only to visited nodes;
- resolve artwork using the existing safe resolver;
- mark a terminal only in the global final visited act;
- retain `acts`, `summary`, full branch edges, and frontend field names exactly.

Add one explicit invariant: a recorded graph is authoritative only when its aligned route length equals the canonical route-node count for that act and each coordinate/path index matches. On mismatch, fail closed to reconstruction/fallback instead of pairing deltas with the wrong node.

- [ ] **Step 4: Run HTTP, static, and workbench regressions and commit**

Run:

```bash
env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_http_api.py \
  tests/agent/run_workbench/test_static_contract.py \
  tests/agent/run_workbench/test_legacy_viewer.py -q
/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node --check \
  agent/run_workbench/static/app.js
/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node --check \
  agent/run_workbench/static/map.js
git diff --check
```

Commit:

```bash
git add agent/run_progress_viewer.py \
  tests/agent/run_workbench/test_http_api.py
git add tests/agent/run_workbench/test_static_contract.py  # only if actually changed
git commit -m "feat: serve authoritative recorded run maps"
```

### Task 5: Verify A Real Failed Evaluation Still Has Its Graph And Inventory

**Files:**
- No planned production changes
- Temporary outputs only under `/private/tmp/sts2-eval-map-smoke-*`

- [ ] **Step 1: Run all agent tests with the actual runtime dependencies**

Run with loopback permission:

```bash
env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent -q
```

Expected: all tests pass. If the isolated worktree lacks ignored game DLL/build artifacts, temporarily point its ignored `lib` path at `/Users/bytedance/mygit/sts2-cli/lib`, build `src/Sts2Headless`, run the tests, then remove the temporary link before inspecting Git status. Never commit the link or binaries.

- [ ] **Step 2: Run one isolated real `13172k` evaluation**

Use the confirmed checkpoint and keep all new logs out of the user's main data:

```bash
env \
  PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
  DECK_HISTORY_PATH=/private/tmp/sts2-eval-map-smoke-deck-history.jsonl \
  MPLCONFIGDIR=/private/tmp/sts2-eval-map-smoke-mpl \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m agent.eval_rl \
  /Users/bytedance/mygit/sts2-cli/checkpoints_tier3_131/ppo_ironclad_13172k.zip \
  --n-games 1 \
  --fixed-seeds \
  --seed-offset 100 \
  --invalid-retries 0 \
  --game-version v0.107.1 \
  --ascension 0 \
  --results-log /private/tmp/sts2-eval-map-smoke-results.jsonl \
  --deck-log none
```

This smoke is valid whether the run wins, dies, or ends technically. The acceptance condition is that once the run entered a map, its terminal sequence contains at least one `map_snapshot` before `outcome`, each valid act has a nonempty complete graph, and every visited coordinate observed during the run has entry/latest-exit inventory. A reset failure before map entry is the only allowed no-map result and must be reported as such rather than accepted as proof.

- [ ] **Step 3: Inspect the raw and canonical artifacts**

Run a read-only verifier that loads the temporary JSONL with `RunCatalog`, finds the new run id, and asserts:

```python
events = [row["event"] for row in raw_rows]
assert events[0] == "run_start"
assert "map_snapshot" in events
assert events.index("map_snapshot") < events.index("outcome")
assert all(row["is_multiplayer"] is False for row in raw_rows)

record = catalog.get_run(run_id)["run"]
assert record["capabilities"]["full_map"] is True
assert record["capabilities"]["visited_route"] is True
assert any(node.get("start_player", {}).get("deck") for node in record["nodes"])
assert any(node.get("end_player", {}).get("relics") is not None for node in record["nodes"])
```

Start the viewer against only the temporary source and request the recorded map endpoint. Confirm `full_map=true`, at least one unvisited branch, highlighted visited nodes, and node detail containing entry/exit deck and relic inventory. Capture the run id and counts in the implementation handoff; do not claim a browser result from static inspection alone.

- [ ] **Step 4: Final hygiene and verification commit (only if needed)**

Run:

```bash
git status --short --untracked-files=all
git diff --check
git log --oneline -5
```

Expected: no temporary smoke artifacts or ignored dependency links are in the worktree, and only the intended commits are present. If the smoke uncovered a bug, add a focused RED regression, apply the smallest fix, rerun the focused suite plus `tests/agent -q`, and commit that fix separately. Otherwise do not create an empty commit.

## Completion Evidence

Before reporting completion, provide all of the following:

- focused RED and GREEN counts for producer capture, recorded-map parsing, adapter/details, and HTTP authority;
- fresh full `tests/agent -q` result with explicit NVM Node and loopback permission;
- actual smoke run id, outcome status, act count, graph node/edge counts, visited count, and at least one node's entry/exit deck and relic counts;
- proof that a failed/technical outcome still ordered `map_snapshot` before `outcome` when the map existed;
- proof that the v0.107.1 `/api/run/map` response bypassed the v0.103.2 generator and returned full branches; and
- clean `git status --short --untracked-files=all` plus the scoped commit hashes.
