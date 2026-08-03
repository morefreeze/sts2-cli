# Training Workbench Run Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user click any visited floor, understand its outcome and inventory changes, inspect factual anomaly signals, and replay combat round by round when the source contains sufficient evidence.

**Architecture:** Move the proven GameLogger parser into the workbench package while preserving its public compatibility import. Build a source-neutral node-detail payload ordered from outcome to evidence, then attach replay-only rounds and conservative factual diagnostics. Expose node detail through a dedicated API and render it beneath the map; native and partial sources degrade explicitly instead of showing empty replay controls.

**Tech Stack:** Python 3.11, JSON/JSONL, vanilla HTML/CSS/JavaScript, pytest, local browser acceptance.

---

## Execution Preconditions

- Complete `2026-08-03-training-workbench-foundation.md` and `2026-08-03-training-workbench-full-map.md` first.
- Continue in the same dedicated feature worktree.
- Preserve `from agent.run_progress_viewer import parse_game_progress` for existing callers and tests.
- Do not commit the real `logs/eval_13957k_fixed8_progress_replay_20260727.jsonl`; use it only for final local acceptance.
- Diagnostic output must distinguish observed facts from hypotheses. This implementation emits facts only.

### Task 1: Extract And Strengthen The Replay Parser

**Files:**
- Create: `agent/run_workbench/replay.py`
- Create: `tests/agent/run_workbench/test_replay.py`
- Create: `tests/fixtures/run_workbench/replay_a2f4_excerpt.jsonl`
- Modify: `agent/run_progress_viewer.py`
- Modify: `tests/agent/test_run_progress_viewer.py`

- [ ] **Step 1: Create a compact synthetic A2F4-style fixture**

The fixture must be hand-authored from the existing GameLogger schema, not copied wholesale from the real log. Include:

- a `start_run` action with `run_id`, character, seed, build id, ascension, and modifiers;
- at least one Act 1 room and one Act 2 floor 4 combat;
- two combat rounds with hands, energy, enemy HP/intents, potion inventory, card actions, targets, and `end_turn`;
- an HP loss between rounds;
- a card-reward choice and a terminal state or explicit partial end.

Keep all names generic/test-only so no extracted game asset or large log enters Git.

- [ ] **Step 2: Add failing parser coverage tests**

Move the existing grouping and round-replay assertions into package-level tests and add:

```python
progress = parse_game_progress(entries, source_name="replay_a2f4_excerpt.jsonl")
assert progress["summary"]["run_id"] == "fixture-a2f4"
assert progress["summary"]["game_version"] == "v0.103.2"
assert progress["summary"]["ascension"] == 0
assert progress["summary"]["first_recorded_floor"] == 1
assert progress["summary"]["last_recorded_floor"] == 21
assert progress["summary"]["has_state_records"] is True
assert progress["summary"]["has_action_records"] is True
```

Also test an action-only file returns an explicit parser error/result message `no state records` rather than a normal zero-room run.

- [ ] **Step 3: Run the tests before extraction**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_replay.py tests/agent/test_run_progress_viewer.py -q
```

Expected: new package test fails because `agent.run_workbench.replay` is absent; legacy tests still pass.

- [ ] **Step 4: Move parsing helpers without changing legacy payloads**

Move the room, option, action, combat snapshot, round, label, floor, and parser helpers from `agent/run_progress_viewer.py` into `agent/run_workbench/replay.py`. Re-export them from the old module:

```python
from agent.run_workbench.replay import (
    format_room_label,
    parse_game_progress,
)
```

Do not duplicate implementations across modules.

- [ ] **Step 5: Add metadata and coverage extraction**

Read optional fields from the `start_run` action and state context without requiring them:

```python
if action.get("cmd") == "start_run":
    run_id = action.get("run_id") or run_id
    character = action.get("character") or character
    seed = action.get("seed") or seed
    game_version = action.get("game_version") or action.get("build_id") or game_version
    ascension = action.get("ascension") if action.get("ascension") is not None else ascension
    modifiers = action.get("modifiers") or modifiers
```

Compute first/last from observed global floors. `complete_run` is true only if evidence begins at the run start and ends in a `game_over` state. Preserve `None` for unavailable values.

- [ ] **Step 6: Run replay and legacy tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_replay.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass with unchanged legacy room/round assertions.

- [ ] **Step 7: Commit parser extraction**

```bash
git add agent/run_workbench/replay.py agent/run_progress_viewer.py tests/agent/run_workbench/test_replay.py tests/agent/test_run_progress_viewer.py tests/fixtures/run_workbench/replay_a2f4_excerpt.jsonl
git commit -m "refactor: move replay parsing into workbench"
```

### Task 2: Build Source-Neutral Floor Details

**Files:**
- Create: `agent/run_workbench/details.py`
- Create: `tests/agent/run_workbench/test_details.py`
- Modify: `agent/run_workbench/models.py`
- Modify: `agent/run_workbench/adapters.py`
- Modify: `tests/agent/run_workbench/test_adapters.py`

- [ ] **Step 1: Write failing detail-contract tests**

Define and test these JSON-safe structures:

```python
@dataclass(frozen=True)
class InventorySnapshot:
    hp: int | None = None
    max_hp: int | None = None
    gold: int | None = None
    deck: tuple[dict, ...] = ()
    relics: tuple[dict, ...] = ()
    potions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class NodeDetail:
    run_id: str
    node_id: str
    act: int
    floor: int
    global_floor: int
    label: str
    room_type: str
    status: str
    encounter: dict
    entry: InventorySnapshot
    exit: InventorySnapshot
    deltas: NodeDeltas
    choices: tuple[dict, ...]
    actions: tuple[dict, ...]
    combat_rounds: tuple[dict, ...]
    coverage: dict
    facts: tuple[dict, ...] = ()
    hypotheses: tuple[dict, ...] = ()
```

Test output order semantically:

1. entry/exit status exists;
2. encounter/event and actual outcome exist;
3. inventory/deltas exist;
4. facts and hypotheses are separate;
5. replay rounds exist only when supported.

- [ ] **Step 2: Add failing native detail tests**

For a native node, assert encounter model id, monster ids, event/rest/ancient choices, exact native changes, and final/adjacent inventory evidence are present. Assert `combat_rounds == ()` and coverage contains `turn_replay=False` with message `此记录不包含逐回合操作`.

- [ ] **Step 3: Add failing replay detail tests**

For the A2F4 replay fixture, assert:

- entry/exit HP and gold are taken from room snapshots;
- selected and unselected choices are preserved;
- card actions retain card and target;
- each round contains start state, actions, end state, HP loss, hand, enemies, and intents;
- partial coverage is labeled with its actual recorded floor range.

- [ ] **Step 4: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_details.py tests/agent/run_workbench/test_adapters.py -q
```

Expected: detail contracts/builders are missing.

- [ ] **Step 5: Implement detail builders**

Expose:

```python
def build_node_detail(record: RunRecord, node_id: str) -> NodeDetail:
    node = next((item for item in record.nodes if item["id"] == node_id), None)
    if node is None:
        raise NodeNotFoundError(record.run_id, node_id)
    if record.source_kind is SourceKind.NATIVE_RUN:
        return native_node_detail(record, node)
    if record.capabilities.turn_replay:
        return replay_node_detail(record, node)
    return basic_node_detail(record, node)
```

Builders may branch on canonical capabilities/source kind, never on filename. They must use `NodeDeltas` from the map plan and must not recalculate unknown data as zero.

- [ ] **Step 6: Attach detail source data in adapters**

Native adapters retain the relevant `player_stats`/room fields in canonical node evidence. Replay adapters retain `start_player`, `end_player`, options, actions, and combat rounds. Large raw source objects must not be duplicated for every node.

- [ ] **Step 7: Run detail, adapter, delta, and replay tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_details.py tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_deltas.py tests/agent/run_workbench/test_replay.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit node detail contracts**

```bash
git add agent/run_workbench/models.py agent/run_workbench/details.py agent/run_workbench/adapters.py tests/agent/run_workbench/test_details.py tests/agent/run_workbench/test_adapters.py
git commit -m "feat: build canonical floor details"
```

### Task 3: Emit Conservative Diagnostic Facts

**Files:**
- Create: `agent/run_workbench/diagnostics.py`
- Create: `tests/agent/run_workbench/test_diagnostics.py`
- Modify: `agent/run_workbench/details.py`
- Modify: `tests/agent/run_workbench/test_details.py`

- [ ] **Step 1: Write failing fact tests**

Use typed `DiagnosticFact` objects with `kind`, `severity`, `statement`, and structured `evidence`. Cover only claims that follow directly from the record:

- technical failure kind/status;
- large node HP loss when entry and exit/max HP are known;
- high-loss combat round;
- long combat by observed round count;
- potion present throughout a fully covered combat with no `use_potion` action;
- death while a potion remains in the terminal inventory;
- selected versus skipped card reward;
- partial source coverage warning.

Example:

```python
facts = collect_diagnostic_facts(detail)
unused = next(fact for fact in facts if fact.kind == "unused_potion")
assert unused.statement == "本场战斗记录到药水，但没有 use_potion 操作"
assert unused.evidence == {
    "potion_names": ["Test Fire Potion"],
    "recorded_actions": 4,
    "combat_coverage_complete": True,
}
```

- [ ] **Step 2: Test conditions that must not produce a fact**

Assert no `unused_potion` fact when:

- the replay begins mid-combat;
- potion inventory is absent/unknown;
- a `use_potion` action exists;
- only a native `.run` source is available;
- the potion appears only after the combat.

Assert the implementation never emits statements such as `应该使用药水`, `策略失误`, or `最优选择`.

- [ ] **Step 3: Run tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_diagnostics.py -q`

Expected: diagnostic module does not exist.

- [ ] **Step 4: Implement pure fact collectors**

Expose:

```python
def collect_diagnostic_facts(detail: NodeDetail) -> tuple[DiagnosticFact, ...]: ...

def rank_run_anomalies(details: Iterable[NodeDetail]) -> tuple[DiagnosticFact, ...]: ...
```

Use transparent thresholds as named constants:

```python
LARGE_NODE_HP_LOSS_RATIO = 0.25
HIGH_LOSS_ROUND_RATIO = 0.20
LONG_COMBAT_ROUNDS = 8
```

The evidence object includes values and thresholds so the UI can explain every flag. Sort severity `critical`, `warning`, `info`, then by global floor. Do not use model inference or LLM-generated explanations.

- [ ] **Step 5: Attach facts to detail payloads**

`build_node_detail` calls the collector after constructing evidence. Set `hypotheses=()` in this release. Partial-data warnings are facts about coverage, not policy judgments.

- [ ] **Step 6: Run diagnostic and detail tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_diagnostics.py tests/agent/run_workbench/test_details.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit factual diagnostics**

```bash
git add agent/run_workbench/diagnostics.py agent/run_workbench/details.py tests/agent/run_workbench/test_diagnostics.py tests/agent/run_workbench/test_details.py
git commit -m "feat: flag factual run anomalies"
```

### Task 4: Add Run And Node Detail APIs

**Files:**
- Modify: `agent/run_workbench/catalog.py`
- Modify: `agent/run_progress_viewer.py`
- Modify: `tests/agent/run_workbench/test_catalog.py`
- Modify: `tests/agent/run_workbench/test_http_api.py`

- [ ] **Step 1: Write failing catalog detail tests**

Add lazy methods:

```python
catalog.get_run(run_id)
catalog.get_node_detail(run_id, node_id)
catalog.get_run_anomalies(run_id)
```

Verify repeated detail access uses the parsed-source cache, file modification invalidates it, and one malformed source does not poison other runs.

- [ ] **Step 2: Write failing API tests**

Retain the foundation plan's canonical run endpoint and add node/anomaly endpoints:

```text
GET /api/run?id=<run_id>
GET /api/run/node?id=<run_id>&node=<node_id>
GET /api/run/anomalies?id=<run_id>
```

The node response must expose `view="node_detail"`, capabilities, coverage, entry/exit, encounter, deltas, choices/actions, facts, hypotheses, and replay rounds. Test 400 for missing params, 404 for unknown run/node, and 422 for a summary source that has no run detail.

- [ ] **Step 3: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py -q
```

Expected: detail/anomaly catalog methods and endpoints are absent.

- [ ] **Step 4: Implement lazy detail access and endpoints**

Keep all source parsing and lookups inside `RunCatalog`. The handler only validates query parameters, calls catalog methods, and serializes returned contracts. Do not return raw filesystem paths or unbounded raw logs.

- [ ] **Step 5: Limit payload size without deleting evidence**

Return one selected node's rounds at a time. Cap displayed deck cards at a documented high bound only in the UI, not the canonical detail. Keep source names, line numbers for parse errors, and full action counts.

- [ ] **Step 6: Run API, catalog, and legacy endpoint tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass, including old `/api/log` and `/api/parse` contracts.

- [ ] **Step 7: Commit detail APIs**

```bash
git add agent/run_workbench/catalog.py agent/run_progress_viewer.py tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py
git commit -m "feat: expose run floor diagnostics APIs"
```

### Task 5: Render Floor Evidence And Round Replay Beneath The Map

**Files:**
- Create: `agent/run_workbench/static/detail.js`
- Modify: `agent/run_workbench/static/index.html`
- Modify: `agent/run_workbench/static/styles.css`
- Modify: `agent/run_workbench/static/app.js`
- Modify: `agent/run_workbench/static/map.js`
- Modify: `tests/agent/run_workbench/test_static_contract.py`

- [ ] **Step 1: Write failing DOM/static tests**

Require these targets in this order inside the run page:

```text
selectedNodeSummary
nodeOutcome
nodeEncounter
nodeInventory
nodeDeltas
nodeChoices
diagnosticFacts
diagnosticHypotheses
combatReplay
roundTabs
roundStartState
roundActions
roundEndState
```

Require `/static/detail.js` after `map.js`. Assert every image has an alt attribute and every clickable node/round control can be reached by keyboard.

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py -q`

Expected: detail/replay DOM contracts are absent.

- [ ] **Step 3: Implement selected-node loading**

`map.js` dispatches a custom event with `run_id` and canonical `node_id`. `detail.js` fetches `/api/run/node`, clears stale content before rendering, and ignores out-of-order responses with a monotonically increasing request token.

Display order is fixed:

1. floor label, status, entry/exit HP/max HP/gold;
2. enemy/event/room identity, turn count, selected option, actual rewards/costs;
3. deck, relic, potion snapshots and exact/derived deltas;
4. observed facts;
5. hypotheses section, showing `暂无经过验证的推断` when empty;
6. round replay when available.

- [ ] **Step 4: Render round replay from state to action to state**

For each round show:

- starting HP/block/energy, hand, enemy HP/block, and intents;
- ordered actions with card/potion, target, and original action label;
- ending HP/block/energy, hand, enemy states, HP loss, and end reason.

Do not synthesize intermediate states between actions. If only the round start and end snapshots exist, label that limitation.

- [ ] **Step 5: Implement honest non-replay and partial states**

Native `.run` nodes show their available choices/rewards and `此记录不包含逐回合操作`. A partial replay shows `记录范围：A… 至 A…` at the top. Unknown values render as `—`. Summary sources never navigate to this page.

- [ ] **Step 6: Connect anomaly lists to nodes**

Dashboard/run anomaly rows carry `run_id` and `node_id`. Clicking one opens the run, selects the correct Act, highlights the node, scrolls to detail, and preserves the dashboard filters for back navigation.

- [ ] **Step 7: Run static, API, replay, and legacy tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py tests/agent/run_workbench/test_http_api.py tests/agent/run_workbench/test_replay.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit the detail interface**

```bash
git add agent/run_workbench/static tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: inspect floor evidence and combat rounds"
```

### Task 6: End-To-End Verification With Real And Synthetic Runs

**Files:**
- Modify only if verification exposes a defect in files already listed above.

- [ ] **Step 1: Run the complete workbench suite**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench tests/agent/test_run_progress_viewer.py tests/agent/test_eval_rl.py tests/agent/test_combat_env.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run all agent tests**

Run: `.venv/bin/python -m pytest tests/agent -q`

Expected: all tests pass.

- [ ] **Step 3: Parse the validated local A2F4 replay at the CLI boundary**

If the local file still exists, run:

```bash
.venv/bin/python -m agent.run_progress_viewer --parse logs/eval_13957k_fixed8_progress_replay_20260727.jsonl
```

Expected: valid JSON, six recorded rooms, maximum label `A2F4`, nonempty combat rounds, and explicit partial/complete coverage according to its terminal evidence. If the file is absent, record that fact and use the committed synthetic excerpt; do not download or recreate it silently.

- [ ] **Step 4: Start the viewer and verify HTTP contracts**

Run:

```bash
.venv/bin/python -m agent.run_progress_viewer --host 127.0.0.1 --port 8766
```

Then request catalog, metrics, run map, node detail, and anomaly endpoints for the synthetic fixtures. Each response must pass `.venv/bin/python -m json.tool`.

- [ ] **Step 5: Browser acceptance on the real replay**

Open the local viewer and verify:

- training progress remains the default page;
- selecting the A2F4 run opens the map/visited-route overview first;
- each visited node shows only observed/derived gains and losses;
- clicking a combat node shows outcome and inventory before replay;
- round tabs show hand, energy, enemy intent, action card/target, and end state;
- an unused-potion signal appears only when the full evidence conditions hold;
- fact wording does not claim the policy should have acted differently;
- returning to the dashboard preserves the selected current/baseline cohorts.

- [ ] **Step 6: Browser acceptance on a native `.run` fixture**

Verify all reconstructable branches, gold visited route, exact native rewards/choices, original/emoji node art fallback, and the explicit no-turn-replay message.

- [ ] **Step 7: Verify technical and malformed cases**

Open one technical evaluation result, one summary file, one malformed fixture, and one action-only fixture. Confirm respectively:

- technical status is separated from gameplay metrics and links to available evidence;
- summary opens a supported summary view, not zero rooms;
- malformed source shows filename/line error without breaking other runs;
- action-only replay says no state records.

- [ ] **Step 8: Review scope and repository hygiene**

Run:

```bash
git diff --check
git status --short
git log --oneline --max-count=20
```

Expected: planned source, tests, fixtures, and docs only. No real logs, extracted assets, checkpoints, `.superpowers/`, crash files, or the user's `eval_and_report.py` change are staged.

- [ ] **Step 9: Prepare implementation handoff evidence**

The handoff must include:

- test commands and pass counts;
- current/baseline cohort used for dashboard acceptance;
- supported map build set and fallback cases;
- one full-map payload and one node-detail payload inspected;
- real A2F4 maximum floor/room count if available;
- browser URL and the exact run/node used for replay acceptance;
- any pre-existing unrelated failures, without masking them.
