# Compact Map Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist bounded non-combat decision evidence in authoritative map snapshots and render each visited floor in a compact 88px map row with a one-line selected decision plus accessible full alternatives.

**Architecture:** A new dependency-light `agent.run_decisions` module owns decision extraction and strict bounds. `CombatEnv` attaches confirmed decisions to the currently validated map node; the existing recorded-map parser, adapters, compact catalog, and recorded-first HTTP path carry that evidence to the UI. The SVG renderer uses a fixed right-hand decision rail and one HTML popover so the graph stays readable while vertical row spacing drops to 88px.

**Tech Stack:** Python 3.11 dataclasses/dicts and JSONL, existing `CombatEnv`, run-workbench parser/catalog/HTTP server, vanilla JavaScript SVG/DOM, pytest, Node v22 static-contract tests.

---

## File map

- Create `agent/run_decisions.py`: exact command-to-decision extraction, bounded Unicode/JSON validation, and bounded append.
- Create `tests/agent/test_run_decisions.py`: unit contract for all six decision kinds and adversarial bounds.
- Modify `agent/combat_env.py`: record only confirmed decisions against an exact captured map state and flush them inside `map_snapshot`.
- Modify `tests/agent/test_combat_env.py`: map-node association, transport failure, potion, graph replacement, and terminal flush tests.
- Modify `agent/run_workbench/recorded_maps.py`: strictly parse optional node `decisions` and preserve it in immutable route output.
- Modify `tests/agent/run_workbench/test_recorded_maps.py`: round-trip, old-log compatibility, and hostile evidence tests.
- Modify `agent/run_workbench/adapters.py`: expose recorded node decisions and the correct `Capabilities.decisions` value.
- Modify `agent/run_workbench/catalog.py`: keep 511/513 capability parity for map-embedded decisions.
- Modify `tests/agent/run_workbench/test_adapters.py`: full-adapter capability and node preservation tests.
- Modify `tests/agent/run_workbench/test_catalog.py`: threshold parity for embedded decisions.
- Modify `agent/run_progress_viewer.py`: copy decisions only from the validated `RecordedActSnapshot` route, never raw canonical fields.
- Modify `tests/agent/run_workbench/test_http_api.py`: recorded-first decision payload, fallback, and trust-boundary tests.
- Modify `agent/run_workbench/static/index.html`: one reusable accessible decision popover.
- Modify `agent/run_workbench/static/map.js`: 88px transform, fixed summary rail, legacy delta inference, and popover interaction.
- Modify `agent/run_workbench/static/styles.css`: single-line decision/effect presentation and popover styles.
- Modify `tests/agent/run_workbench/test_static_contract.py`: layout geometry, text safety, hover/focus/Escape, and old-log fallback.
- Create `tests/fixtures/run_workbench/recorded_decisions.jsonl`: a small recorded-map fixture containing event, card, potion, shop, and rest decisions for browser verification.

## Shared invariants

- Decision kinds are exactly `event`, `card_reward`, `potion`, `relic`, `shop`, and `rest`.
- A node has at most 16 decisions; a decision has at most 32 options; ID/label strings are at most 256 Unicode scalars; effects are at most 512; encoded decisions per node are at most 32 KiB.
- Only exact built-in `dict`, `list`, `str`, `bool`, and bounded integers enter persisted evidence. Lone surrogates, non-finite numbers, subclasses, hostile mappings, and excess structure fail closed.
- A command is recorded only after `_send` returns a non-error state. Non-combat choices require a map poll for the exact pre-command state; combat potion use may reuse the already validated room-entry coordinate.
- Existing `card_pick` rows remain unchanged for the predictor. Embedded decisions are the UI/canonical evidence path.
- Old logs remain valid. Missing decisions are never synthesized into recorded alternatives.

### Task 1: Add bounded decision evidence primitives

**Files:**
- Create: `agent/run_decisions.py`
- Create: `tests/agent/test_run_decisions.py`

- [ ] **Step 1: Write extraction tests for all supported commands**

Create table-driven tests whose input states include actual labels and effects:

```python
@pytest.mark.parametrize(
    ("state", "command", "kind", "selected_label"),
    [
        (
            {
                "decision": "event_choice",
                "options": [
                    {"index": 0, "id": "leave", "label": "离开", "description": "不发生变化"},
                    {"index": 1, "id": "blood", "label": "献血", "description": "失去 8 生命；最大生命 +8"},
                ],
            },
            {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}},
            "event",
            "献血",
        ),
        (
            {
                "decision": "card_reward",
                "can_skip": True,
                "cards": [
                    {"index": 0, "id": "CARD.POMMEL_STRIKE", "name": {"en": "Pommel Strike"}, "description": "造成 9 点伤害，抽 1 张牌"},
                ],
            },
            {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 0}},
            "card_reward",
            "Pommel Strike",
        ),
        (
            {
                "decision": "rest_site",
                "options": [
                    {"index": 0, "option_id": "SMITH", "label": "升级"},
                    {"index": 1, "option_id": "HEAL", "label": "休息"},
                ],
            },
            {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}},
            "rest",
            "升级",
        ),
        (
            {
                "decision": "shop",
                "player": {"potions": []},
                "potions": [
                    {"index": 2, "id": "POTION.FIRE", "name": "火焰药水", "description": "造成 20 点伤害", "cost": 50, "is_stocked": True},
                ],
            },
            {"cmd": "action", "action": "buy_potion", "args": {"potion_index": 2}},
            "potion",
            "购买火焰药水",
        ),
        (
            {
                "decision": "combat_play",
                "player": {"potions": [{"index": 0, "id": "POTION.FIRE", "name": "火焰药水", "description": "造成 20 点伤害"}]},
            },
            {"cmd": "action", "action": "use_potion", "args": {"potion_index": 0, "target_index": 0}},
            "potion",
            "火焰药水",
        ),
        (
            {
                "decision": "shop",
                "player": {"potions": []},
                "relics": [{"index": 3, "id": "RELIC.ANCHOR", "name": "锚", "description": "每场战斗开始获得 10 格挡", "cost": 150, "is_stocked": True}],
            },
            {"cmd": "action", "action": "buy_relic", "args": {"relic_index": 3}},
            "relic",
            "购买锚",
        ),
    ],
)
def test_capture_run_decision_records_selected_option_and_effect(
    state, command, kind, selected_label
):
    evidence = capture_run_decision(state, command)
    assert evidence["kind"] == kind
    assert evidence["selected_label"] == selected_label
    assert sum(option["selected"] for option in evidence["options"]) == 1
    json.dumps(evidence, ensure_ascii=False, allow_nan=False).encode("utf-8")
```

- [ ] **Step 2: Write strict boundary and exclusion tests**

Add tests that require `play_card`, `end_turn`, and `select_map_node` to return `None`; card skip to produce a selected `SKIP` option; disabled event/rest options to remain visible but unselected; and these inputs to raise `DecisionEvidenceError`: 33 options, 513-character effect, lone surrogate, dict subclass, bool index, 10,000-digit integer, depth 6, and encoded payload over 32 KiB.

Also assert bounded append behavior:

```python
def test_append_run_decision_is_detached_and_stops_at_sixteen():
    original = capture_run_decision(EVENT_STATE, EVENT_COMMAND)
    retained = []
    for _ in range(16):
        retained = append_run_decision(retained, original)
    with pytest.raises(DecisionEvidenceError, match="decision limit"):
        append_run_decision(retained, original)
    original["options"][0]["label"] = "MUTATED"
    assert retained[0]["options"][0]["label"] != "MUTATED"
```

- [ ] **Step 3: Run the tests to verify RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_run_decisions.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent.run_decisions'`.

- [ ] **Step 4: Implement the bounded public interface**

Implement these exact exports:

```python
DECISION_KINDS = frozenset({
    "event", "card_reward", "potion", "relic", "shop", "rest",
})
MAX_DECISIONS_PER_NODE = 16
MAX_OPTIONS_PER_DECISION = 32
MAX_ID_CHARS = 256
MAX_LABEL_CHARS = 256
MAX_EFFECT_CHARS = 512
MAX_DECISIONS_BYTES = 32 * 1024

class DecisionEvidenceError(ValueError):
    pass
```

The module exports `capture_run_decision(state: dict, command: dict) -> dict | None`, `validate_run_decisions(value: object) -> list[dict]`, and `append_run_decision(existing: object, decision: object) -> list[dict]`. Each function returns newly allocated ordinary dictionaries/lists; only `capture_run_decision` may return `None` for an unsupported command.

Use exact action mapping:

```python
ACTION_KIND = {
    "select_card_reward": "card_reward",
    "skip_card_reward": "card_reward",
    "use_potion": "potion",
    "buy_card": "shop",
    "buy_relic": "relic",
    "buy_potion": "potion",
    "remove_card": "shop",
}
```

For `choose_option`, choose `rest` when `state["decision"] == "rest_site"`, otherwise `event` when it is `event_choice`. For `card_select`, emit `rest` only in a rest room and `shop` only in a shop room; combat card selection is excluded. `leave_room` is recorded only from event/shop state. Labels come from exact `label`, localized `name`, `option_id`, then `id`; effects come from exact/localized `description`, `effect`, then `text`. Localized objects prefer `zh-CN`, then `zh`, then `en`. An option ID falls back to the exact bounded string form of its integer `index`; an option missing both is dropped, and a missing selected option drops the whole decision. Cost is appended to shop labels as ` · N 金币` only for exact bounded non-bool integers.

Validation rebuilds every object from allowlisted fields (`kind`, `selected_id`, `selected_label`, `options`, `evidence`; option fields `id`, `label`, `effect`, `selected`) and requires exactly one selected option. `evidence` is always `recorded` for producer output.

- [ ] **Step 5: Run Task 1 GREEN**

Run the same command. Expected: all `test_run_decisions.py` tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add agent/run_decisions.py tests/agent/test_run_decisions.py
git commit -m "feat: define bounded map decision evidence"
```

### Task 2: Attach confirmed decisions to authoritative map nodes

**Files:**
- Modify: `agent/combat_env.py:620-745, 1335-1384, 1760-1900, 2030-2260`
- Modify: `tests/agent/test_combat_env.py:237-380, 969-1095, 1461-1525, 2151-2215`

- [ ] **Step 1: Write RED tests for exact node association**

Add an integration test that captures Ancient then Monster, sends an event choice from the Monster state, and asserts the decision is attached only to the Monster node after a successful response:

```python
def test_confirmed_event_decision_is_attached_to_exact_current_map_node(monkeypatch):
    env = _map_recording_env(monkeypatch)
    ancient = _map_state(hp=80)
    monster = {**_map_state(hp=74), "decision": "event_choice", "options": EVENT_OPTIONS}
    env._ingest_run_map_reply(_map_reply(current=(3, 0)), ancient)
    env._ingest_run_map_reply(_map_reply(current=(2, 1)), monster)
    env._run_last_map_poll_state_id = id(monster)
    command = {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}}
    monkeypatch.setattr(env, "_send", lambda cmd: {"decision": "map_select", "player": monster["player"]})

    result = env._send_with_run_decision(monster, command)

    first, second = env._run_map_snapshots[1]["visited_nodes"]
    assert "decisions" not in first
    assert second["decisions"][0]["selected_label"] == "献血"
    assert result["decision"] == "map_select"
```

- [ ] **Step 2: Add transport, stale-coordinate, replacement, potion, and flush RED tests**

Cover these exact cases:

- `_send` returns `None` or `{"type": "error"}`: no decision is attached.
- The exact non-combat pre-command state did not complete a map poll: a previous coordinate must not receive the decision.
- A delayed valid map reply completes during `_send`: the decision attaches to that newly validated coordinate.
- Same-act graph replacement retains decisions on surviving coordinates and drops decisions with removed coordinates.
- `_greedy_use_potions` and `_combat_check_heal` record `use_potion` with the potion description but never record `play_card` or `end_turn`.
- Combat potion evidence is rejected when the current state's exact `(act, floor)` room identity does not match the identity stored by the last successful map capture.
- `_emit_run_outcome` writes `map_snapshot` containing decisions before existing `card_pick` and `outcome` rows; append failure retains the buffered decision for retry.
- A fresh `reset` clears all decision state.

- [ ] **Step 3: Run focused RED**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_combat_env.py -q -k 'run_decision or decision_map_node or decision_flush'
```

Expected: failures for missing `_send_with_run_decision` and absent node `decisions`.

- [ ] **Step 4: Add the confirmed-send wrapper and node append**

Import the new primitives and add an explicit coordinate token so a command that changes rooms cannot move the evidence target after the fact:

```python
def _run_decision_target(self, state: dict, decision: dict | None):
    if decision is None or self._run_current_map_coord is None:
        return None
    exact_poll = self._run_last_map_poll_state_id == id(state)
    combat_potion = (
        decision["kind"] == "potion"
        and state.get("decision") == "combat_play"
        and self._run_current_map_room_identity
            == self._bounded_run_room_identity(state)
    )
    return self._run_current_map_coord if exact_poll or combat_potion else None

def _append_run_decision_to_node(self, target, decision) -> bool:
    if decision is None or target is None:
        return False
    act, col, row = target
    snapshot = self._run_map_snapshots.get(act)
    node = snapshot.get("_coord_lookup", {}).get((col, row)) if snapshot else None
    if node is None:
        return False
    try:
        node["decisions"] = append_run_decision(node.get("decisions", []), decision)
    except DecisionEvidenceError as exc:
        self._report_run_logging_error("decision capture failed", exc)
        return False
    return True

def _send_with_run_decision(self, state: dict, command: dict):
    decision = capture_run_decision(state, command)
    self._retry_run_map_poll_before_gameplay()
    target = self._run_decision_target(state, decision)
    result = self._send(command)
    if type(result) is dict and result.get("type") != "error":
        self._append_run_decision_to_node(target, decision)
    return result
```

Add `_bounded_run_room_identity(state) -> tuple[int, int] | None`, accepting only exact integer act 1..4 and floor 1..17 from `state.context`/`state.floor`. On successful `_ingest_run_map_reply`, store the matching `(act, floor)` from the validated reply as `_run_current_map_room_identity`; clear it on fresh reset, process kill, or when graph replacement clears the current coordinate.

When `_retry_run_map_poll_before_gameplay` completes a retained state, ensure it sets `_run_last_map_poll_state_id` to the retained ID before the wrapper captures `target`. Never infer a coordinate from floor; room identity is only a guard for reusing an already validated coordinate. Add a test proving mid-combat potion use stays on the validated combat node even though the individual combat state object was not map-polled.

- [ ] **Step 5: Route supported sends through the wrapper**

In `_advance_to_combat`, replace the non-combat `state = self._send(cmd)` with `state = self._send_with_run_decision(state, cmd)`. Keep `_buffer_card_pick(state, cmd)` immediately before it.

In `_greedy_use_potions` and `_combat_check_heal`, replace only the `use_potion` sends with `_send_with_run_decision(state, command)`. Do not route RL `step()` combat commands through the wrapper.

Reset needs no second buffer: decisions live inside `_run_map_snapshots`, which is already reset for each run.

- [ ] **Step 6: Run focused and full CombatEnv GREEN**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_run_decisions.py tests/agent/test_combat_env.py -q
```

Expected: new decision tests pass. If the three pre-existing action-mask tests fail in this checkout, rerun with their exact node IDs excluded and report them separately; do not change action-mask production code in this task.

- [ ] **Step 7: Commit Task 2**

```bash
git add agent/combat_env.py tests/agent/test_combat_env.py
git commit -m "feat: persist confirmed map decisions"
```

### Task 3: Parse and freeze node decisions

**Files:**
- Modify: `agent/run_workbench/recorded_maps.py:13-80, 480-620`
- Modify: `tests/agent/run_workbench/test_recorded_maps.py:180-260, 400-535, 580-640, 830-1065`

- [ ] **Step 1: Write parser round-trip and compatibility RED tests**

Take the existing valid `_recorded_row()` fixture, add a valid `decisions` list to one visited node, parse it, mutate the original input, and assert the output remains unchanged. Also assert `dataclasses.asdict`, `replace`, `deepcopy`, and `json.dumps` preserve decisions.

Add a second test with no decisions field and assert its parsed route output is byte-for-byte equivalent to the current expected route contract.

- [ ] **Step 2: Write hostile evidence RED tests**

Parameterize: invalid kind, zero/two selected options, 33 options, non-list decisions, str subclass, lone surrogate, 513-character effect, dict subclass, hostile nested mapping, 17 decisions, and 32KiB overflow. Public parse must raise bounded `RecordedMapError`; `latest_recorded_acts` must keep other valid rows and return a generic bounded error without leaking hostile text.

- [ ] **Step 3: Run parser RED**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench/test_recorded_maps.py -q -k decisions
```

Expected: valid evidence is missing from `snapshot.route_nodes`; malformed evidence is not rejected by the decision contract.

- [ ] **Step 4: Validate decisions during route construction**

Import `DecisionEvidenceError` and `validate_run_decisions`. In `_parse_recorded_row`, after building the ordinary route node:

```python
if "decisions" in raw_route_node:
    try:
        route_node["decisions"] = validate_run_decisions(
            raw_route_node["decisions"]
        )
    except DecisionEvidenceError as exc:
        raise _error(str(exc)) from None
```

The existing root/nested untrusted snapshot must run before this call, so validators receive only exact built-in JSON. `RecordedActSnapshot.__post_init__` continues to encode the complete route tuple, keeping decisions immutable behind fresh decoded views.

- [ ] **Step 5: Run parser GREEN and adjacent delta/model tests**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_recorded_maps.py \
  tests/agent/run_workbench/test_deltas.py \
  tests/agent/run_workbench/test_models.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add agent/run_workbench/recorded_maps.py tests/agent/run_workbench/test_recorded_maps.py
git commit -m "feat: parse recorded map decisions"
```

### Task 4: Preserve adapter and compact-catalog parity

**Files:**
- Modify: `agent/run_workbench/adapters.py:450-535`
- Modify: `agent/run_workbench/catalog.py:150-285, 1510-1575`
- Modify: `tests/agent/run_workbench/test_adapters.py:1180-1305, 1380-1545`
- Modify: `tests/agent/run_workbench/test_catalog.py`

- [ ] **Step 1: Write 511/513 RED tests**

Generate the same deck-history run on both sides of the compact threshold with one valid map snapshot containing decisions and no `card_pick` row. Assert:

```python
assert small.capabilities.decisions is True
assert large.capabilities.decisions is True
assert small.capabilities.to_dict() == large.capabilities.to_dict()
assert small.nodes[0]["decisions"][0]["selected_label"] == "献血"
```

Also test a malformed newer snapshot after a valid older one: it must not erase the older capability or introduce decisions from the malformed row. A no-map historical `card_pick` run must retain the existing decisions capability.

- [ ] **Step 2: Run parity RED**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_catalog.py -q -k 'recorded_decision or decision_parity'
```

Expected: full adapter preserves the node field but reports decisions only from `card_pick`; compact 513 reports false.

- [ ] **Step 3: Update capability derivation**

In `_adapt_deck_run` use:

```python
has_recorded_decisions = any(
    isinstance(node.get("decisions"), list) and bool(node["decisions"])
    for node in route_nodes
)
```

and set `Capabilities.decisions` to `has_recorded_decisions or any(card_pick rows)`.

In `_update_compact_deck`, after successful `parse_recorded_map_row(record)`, set:

```python
compact.has_node_decisions = compact.has_node_decisions or any(
    isinstance(node.get("decisions"), list) and bool(node["decisions"])
    for node in snapshot.route_nodes
)
```

In `_CompactRun.to_record`, the deck-history branch uses `self.has_card_pick or self.has_node_decisions`; replay behavior is unchanged.

- [ ] **Step 4: Run full adapter/catalog GREEN**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_catalog.py \
  tests/agent/run_workbench/test_models.py -q
```

Expected: all pass with exact threshold parity.

- [ ] **Step 5: Commit Task 4**

```bash
git add agent/run_workbench/adapters.py agent/run_workbench/catalog.py \
  tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_catalog.py
git commit -m "feat: expose recorded decision capability"
```

### Task 5: Return decisions only through the recorded-map trust boundary

**Files:**
- Modify: `agent/run_progress_viewer.py:300-520`
- Modify: `tests/agent/run_workbench/test_http_api.py:950-1130`

- [ ] **Step 1: Write HTTP RED tests**

Add a valid recorded map whose route has decisions and a canonical route node containing forged raw `decisions`. Assert the endpoint returns the validated recorded decisions, not the forged canonical value, and `MapService.generate` is never called.

Add a malformed/route-mismatched recorded map with forged decisions. Assert fallback generation runs and no `decisions` field appears in the map payload.

Add a mixed native/deck same-act case to prove the recorded decision still maps by validated `path_index` when canonical deltas come from native evidence.

- [ ] **Step 2: Run HTTP RED**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_http_api.py -q -k recorded_decision
```

Expected: endpoint nodes contain deltas/art/terminal fields but no validated decisions.

- [ ] **Step 3: Copy from `RecordedActSnapshot.route_nodes` only**

After `_authoritative_recorded_act` succeeds, retain a local tuple:

```python
trusted_recorded_route = (
    authoritative_recorded.route_nodes
    if authoritative_recorded is not None
    else ()
)
```

Inside the visited path loop, after the bounds check for `path_index`, copy:

```python
if 0 <= path_index < len(trusted_recorded_route):
    trusted_decisions = trusted_recorded_route[path_index].get("decisions")
    if isinstance(trusted_decisions, list) and trusted_decisions:
        node["decisions"] = deepcopy(trusted_decisions)
```

Do not copy `source_node["decisions"]`, `options`, or `choices`. The parser is the sole authority for this new field. Existing canonical deltas and art resolution remain unchanged.

- [ ] **Step 4: Run HTTP and map-service GREEN**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_http_api.py \
  tests/agent/run_workbench/test_map_service.py \
  tests/agent/run_workbench/test_recorded_maps.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add agent/run_progress_viewer.py tests/agent/run_workbench/test_http_api.py
git commit -m "feat: serve trusted map decisions"
```

### Task 6: Render the 88px map and accessible decision rail

**Files:**
- Modify: `agent/run_workbench/static/index.html:30-58`
- Modify: `agent/run_workbench/static/map.js:1-360`
- Modify: `agent/run_workbench/static/styles.css:241-410, 1090-1130`
- Modify: `tests/agent/run_workbench/test_static_contract.py:250-525`

- [ ] **Step 1: Write geometry RED tests**

Replace the badge-extent contract with exact compact invariants:

```python
assert _js_number(script, "MAP_ROW_GAP") == 88
assert _js_number(script, "MAP_DECISION_RAIL_WIDTH") >= 340
assert "renderBadges(group, node)" not in script
assert "renderDecisionSummary" in script
assert "mapDecisionPopover" in index
```

Execute `createMapTransform` under Node with a 17-row map and assert the height is below 1700px, the graph points remain 88px apart, and a requested decision rail increases width without changing node x/y coordinates. A map with no recorded or derived summary must not reserve an empty rail.

- [ ] **Step 2: Write summary and interaction RED tests**

Use the existing Node DOM/SVG harness to assert:

- recorded decision renders `卡：Pommel Strike` and a separate grey effect tspan;
- a 600-character label/effect is bounded and never yields `[object Object]`;
- an emoji at the truncation boundary remains a valid Unicode scalar rather than a split surrogate;
- two decisions render the newest summary plus `+1`;
- no recorded decisions plus exact/derived card/potion/relic deltas renders `推导：获得 …` and the popover says “该对局未记录备选项”;
- unknown deltas render no fabricated summary;
- `mouseenter` and `focusin` open the same popover; `mouseleave`, `focusout`, and `Escape` close it;
- Enter/Space still opens node detail;
- all text is assigned through `textContent`, never `innerHTML`.

- [ ] **Step 3: Run static RED**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_static_contract.py -q -k 'map and (decision or geometry or tooltip)'
```

Expected: old 180px/badge geometry and absent popover fail.

- [ ] **Step 4: Add the popover container**

Inside `.map-layout`, after `.map-scroll`, add:

```html
<aside id="mapDecisionPopover" class="map-decision-popover" role="tooltip" hidden>
  <h3 id="mapDecisionTitle"></h3>
  <div id="mapDecisionBody"></div>
</aside>
```

The container is reused for every node and contains no persistent node-specific HTML.

- [ ] **Step 5: Implement compact transform and summary rail**

Use these constants:

```javascript
const MAP_ROW_GAP = 88;
const MAP_PADDING_BOTTOM = 76;
const MAP_DECISION_RAIL_GAP = 24;
const MAP_DECISION_RAIL_WIDTH = 360;
const MAP_DECISION_LABEL_LIMIT = 72;
```

`createMapTransform(nodes, hasDecisionRail = false)` returns `graphWidth`, `decisionX`, and total `width`. When `hasDecisionRail` is true, width is `graphWidth + MAP_DECISION_RAIL_GAP + MAP_DECISION_RAIL_WIDTH`; otherwise it remains `graphWidth`. `renderMap` computes the flag with `payload.nodes.some((node) => nodeDecisionSummary(node) !== null)`. Node points continue to use only graph inputs, so enabling summaries never moves the route.

Implement four stable helpers named `nodeDecisionSummary(node)`, `renderDecisionSummary(layer, node, point, transform)`, `showDecisionPopover(node, anchor)`, and `hideDecisionPopover(anchor = null)`.

Add `boundedDecisionText(value, limit)` using `Array.from(value)` before slicing, so the 72-character visual bound never splits an emoji/surrogate pair.

`nodeDecisionSummary` reads only an array of recorded decisions with a selected option. It returns `{prefix, label, effect, overflow, recorded}`. If none exists, it checks exact/derived `cards_gained`, `potions_gained`, `relics_gained`, `cards_upgraded`, `cards_removed`, and `cards_transformed` in that order and returns a derived summary. Unknown measurements return `null`.

`renderDecisionSummary` creates one SVG `g` at `transform.decisionX, point.y`, a 340x30 background `rect`, and one `text` with label/effect `tspan`s. The effect tspan has class `map-decision-effect`; the whole group has `pointer-events: none` so node interaction remains authoritative. Remove the `renderBadges` call but keep delta helpers for detail and legacy derivation.

- [ ] **Step 6: Implement the accessible popover**

Populate the HTML popover using `clear`, `element`, and `textContent`. Recorded evidence lists every option and marks the selected option with `✓`; full effect text is a grey child line. Derived evidence renders the selected/inferred result and the sentence `该对局未记录备选项`.

Position with `anchor.getBoundingClientRect()` and `position: fixed`; clamp left/top inside an 8px viewport margin. Bind `mouseenter`, `mouseleave`, `focusin`, `focusout`, and `Escape` on visited node groups. Set `aria-describedby="mapDecisionPopover"` while visible and remove it when hidden. Do not interfere with click/Enter/Space detail selection.

- [ ] **Step 7: Add CSS**

Add styles with one-line truncation enforced by JS bounds and visual clipping:

```css
.map-decision-summary rect { fill: rgba(255, 253, 247, .96); stroke: var(--line); }
.map-decision-summary text { fill: var(--slate-950); font-size: 12px; font-weight: 750; }
.map-decision-summary .map-decision-effect { fill: var(--slate-500); font-weight: 500; }
.map-decision-popover { position: fixed; z-index: 30; width: min(360px, calc(100vw - 16px)); }
.map-decision-popover[hidden] { display: none; }
.map-decision-option-effect { color: var(--slate-500); font-size: 12px; }
```

On narrow screens keep the SVG horizontally scrollable; do not shrink the 56px node target.

- [ ] **Step 8: Run static GREEN**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
node --check agent/run_workbench/static/map.js
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_static_contract.py -q
```

Expected: syntax check exits 0 and the full static contract passes.

- [ ] **Step 9: Commit Task 6**

```bash
git add agent/run_workbench/static/index.html agent/run_workbench/static/map.js \
  agent/run_workbench/static/styles.css tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: render compact map decisions"
```

### Task 7: Add an end-to-end fixture and verify the feature

**Files:**
- Create: `tests/fixtures/run_workbench/recorded_decisions.jsonl`
- Modify: `tests/agent/run_workbench/test_http_api.py`
- Modify: `tests/agent/run_workbench/test_static_contract.py`

- [ ] **Step 1: Create a bounded browser fixture**

Create a single complete run with `run_start`, one valid Act 1 `map_snapshot`, and `outcome`. The map has Ancient → Monster → Unknown → Shop → RestSite, correct visited/current markers, valid entry/exit inventories, and decisions for event, card reward, potion, relic purchase, shop card purchase, and rest upgrade. Every option includes a concrete effect string; no private paths or raw HTML appear.

- [ ] **Step 2: Write fixture-to-HTTP RED/GREEN test**

Run `RunCatalog` against only the fixture root, request `/api/run/map`, and assert:

```python
assert response["full_map"] is True
assert response["summary"]["visited_count"] == 5
assert response["nodes"][1]["decisions"][0]["kind"] == "card_reward"
assert response["nodes"][2]["decisions"][0]["options"][0]["effect"]
assert service.requests == []
```

Run the test before adding the fixture to confirm RED (not found), then after adding it to confirm GREEN.

- [ ] **Step 3: Run related Python suites**

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/test_run_decisions.py \
  tests/agent/test_combat_env.py \
  tests/agent/run_workbench/test_recorded_maps.py \
  tests/agent/run_workbench/test_adapters.py \
  tests/agent/run_workbench/test_catalog.py \
  tests/agent/run_workbench/test_http_api.py -q
```

Expected: all task-related tests pass; report any unchanged pre-existing action-mask failures separately.

- [ ] **Step 4: Run full workbench and full agent regression**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench -q
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH \
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent -q
```

Expected: both commands exit 0. If ignored runtime assets are absent in the worktree, temporarily link the exact missing read-only files from `/Users/bytedance/mygit/sts2-cli/data`, rerun, remove the links, and verify none are staged.

- [ ] **Step 5: Run static and compile checks**

```bash
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH node --check agent/run_workbench/static/app.js
PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:$PATH node --check agent/run_workbench/static/map.js
PYTHONPYCACHEPREFIX=/tmp/sts2-cli-pycache \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m compileall -q agent tests/agent
git diff --check
```

Expected: all exit 0.

- [ ] **Step 6: Verify in a real browser**

Start the viewer with the fixture source root, open the fixture run, and verify:

1. Five floors fit within roughly 500px and a 17-floor act is below 1700px.
2. Each recorded decision is one line; effect text is grey and clipped.
3. Hover and keyboard focus show the same full alternatives.
4. Escape closes the popover; Enter opens node detail; focus remains usable.
5. An old run without decisions shows only exact derived gains and “该对局未记录备选项”.
6. Browser console and page-error collections are empty.

Capture a screenshot for the handoff but do not commit it unless explicitly requested.

- [ ] **Step 7: Commit the fixture and E2E tests**

```bash
git add tests/fixtures/run_workbench/recorded_decisions.jsonl \
  tests/agent/run_workbench/test_http_api.py \
  tests/agent/run_workbench/test_static_contract.py
git commit -m "test: cover compact recorded decisions end to end"
```

- [ ] **Step 8: Final scope and history audit**

```bash
git status --short --untracked-files=all
git log --oneline --decorate -8
git diff HEAD~7..HEAD --check
```

Expected: only the brainstorming `.superpowers/` preview directory may remain untracked; no runtime data links, build products, checkpoint files, or unrelated user changes are staged or committed.

## Plan self-review checklist

- Spec coverage: compact 88px layout, one-line summaries, grey effects, hover/focus alternatives, six non-combat decision kinds, old-log fail-closed behavior, failure-terminal persistence, parser/catalog/HTTP trust, and real browser verification are each assigned to a task.
- Type consistency: all layers use the field name `decisions`; each decision uses `kind`, `selected_id`, `selected_label`, `options`, and `evidence`; each option uses `id`, `label`, `effect`, and `selected`.
- Trust consistency: only `validate_run_decisions` accepts persisted evidence, and HTTP copies only from `RecordedActSnapshot.route_nodes`.
- Scope consistency: no training algorithm, reward, PPO, action-mask, map generator, or C# game behavior changes are included.
