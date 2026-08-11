# Version-First Cohort Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorder the workbench controls to game version, character, current cohort, and baseline cohort, with version-aware character options and cohort filtering.

**Architecture:** Keep the existing server cohort descriptor contract and implement the hierarchy entirely in the static client. Separate axis-option derivation from cohort filtering so it can be exercised by the existing Node-based static contract tests, while leaving server-validated baseline compatibility unchanged.

**Tech Stack:** HTML, CSS Grid, browser JavaScript, pytest static contracts, Node v22 syntax/runtime checks.

---

### Task 1: Lock the visual hierarchy

**Files:**
- Modify: `tests/agent/run_workbench/test_static_contract.py`
- Modify: `agent/run_workbench/static/index.html`
- Modify: `agent/run_workbench/static/styles.css`

- [ ] **Step 1: Write the failing DOM-order test**

Add a test that parses the filter-panel fragment and requires the controls in this exact order:

```python
def test_filter_panel_orders_scope_before_cohorts():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    panel = html[html.index('<section class="panel filter-panel"') : html.index('<section class="metrics-section"')]
    positions = [
        panel.index('id="versionFilter"'),
        panel.index('id="characterFilter"'),
        panel.index('id="currentCohort"'),
        panel.index('id="baselineCohort"'),
        panel.index('id="validityFilter"'),
    ]
    assert positions == sorted(positions)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest \
  tests/agent/run_workbench/test_static_contract.py::test_filter_panel_orders_scope_before_cohorts -q
```

Expected: FAIL because `currentCohort` currently appears before `versionFilter`.

- [ ] **Step 3: Reorder the HTML and make row ownership explicit**

Move the existing labels without changing their IDs or accessibility attributes. Add classes rather than using positional CSS:

```html
<label class="filter-field filter-field-scope">游戏版本 ...</label>
<label class="filter-field filter-field-scope">角色 ...</label>
<label class="filter-field filter-field-cohort">当前批次 ...</label>
<label class="filter-field filter-field-cohort">基线批次 ...</label>
<label class="filter-field filter-field-validity">记录状态 ...</label>
```

Use CSS Grid spans so the two scope fields occupy the first row, the two cohort fields the second row, and mobile remains one column:

```css
.filter-field-scope,
.filter-field-cohort { grid-column: span 6; }
.filter-field-validity { grid-column: span 3; }
@media (max-width: 480px) { .filter-grid .filter-field { grid-column: 1 / -1; } }
```

- [ ] **Step 4: Run the focused test and static suite**

Run the focused test above, then the full `test_static_contract.py`. Expected: PASS, with the two socket tests run outside the sandbox if necessary.

- [ ] **Step 5: Commit the layout contract**

```bash
git add agent/run_workbench/static/index.html agent/run_workbench/static/styles.css tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: prioritize version and character filters"
```

### Task 2: Add version-aware character and cohort filtering

**Files:**
- Modify: `tests/agent/run_workbench/test_static_contract.py`
- Modify: `agent/run_workbench/static/app.js`

- [ ] **Step 1: Write a failing Node behavior test**

Extract the filter helper section and run it with cohorts spanning two versions and two characters. Assert these transitions:

```javascript
state.cohorts = [
  cohort('v1-iron', 'v1', 'Ironclad'),
  cohort('v1-necro', 'v1', 'Necrobinder'),
  cohort('v2-iron', 'v2', 'Ironclad'),
];
nodes.versionFilter.value = 'v1';
nodes.characterFilter.value = 'Necrobinder';
updateCharacterFilterOptions();
// character options: Ironclad, Necrobinder; selection preserved

nodes.versionFilter.value = 'v2';
updateCharacterFilterOptions();
// character options: Ironclad; selection resets to ''

updateCohortOptions({ chooseDefaults: true });
// current cohort options contain only v2-iron
```

- [ ] **Step 2: Run the new test and verify RED**

Expected: FAIL because `updateCharacterFilterOptions` does not exist and the character list is populated only once from all cohorts.

- [ ] **Step 3: Implement minimal option derivation**

Add helpers near `populateAxisFilter`:

```javascript
function cohortsForSelectedVersion() {
  const version = byId('versionFilter').value;
  return state.cohorts.filter((cohort) => (
    !version || filterValue(cohort, 'game_version') === version
  ));
}

function updateCharacterFilterOptions() {
  const select = byId('characterFilter');
  const previous = select.value;
  const values = Array.from(new Set(
    cohortsForSelectedVersion().map((cohort) => filterValue(cohort, 'character')),
  )).sort();
  setSelectOptions(
    select,
    values.map((value) => ({ value, label: value })),
    '全部角色',
    previous,
  );
}
```

Change bootstrap to populate version first and then call `updateCharacterFilterOptions()`. Split the handlers so version changes rebuild character options before updating cohorts:

```javascript
function versionFilterChanged() {
  updateCharacterFilterOptions();
  updateCohortOptions({ chooseDefaults: true });
  refreshMetrics();
}
```

Keep character and validity changes on the existing cohort update path. Do not alter `defaultBaselineCohortId` or server comparison semantics.

- [ ] **Step 4: Run focused and full static tests**

Run the new Node behavior test, all of `test_static_contract.py`, and:

```bash
/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node --check agent/run_workbench/static/app.js
```

Expected: all PASS and Node exits 0.

- [ ] **Step 5: Commit the interaction**

```bash
git add agent/run_workbench/static/app.js tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: cascade version and character cohorts"
```

### Task 3: Full regression and browser preview

**Files:**
- Verify: `agent/run_workbench/static/index.html`
- Verify: `agent/run_workbench/static/styles.css`
- Verify: `agent/run_workbench/static/app.js`

- [ ] **Step 1: Run the full workbench suite**

```bash
env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin \
  /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench -q
```

Expected: all tests PASS; run outside the sandbox for localhost fixtures.

- [ ] **Step 2: Run static integrity checks**

```bash
/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node --check agent/run_workbench/static/app.js
git diff --check
```

Expected: both exit 0.

- [ ] **Step 3: Start an isolated viewer and verify in the browser**

Start the worktree viewer on an unused localhost port with the same source roots as the live workbench. Verify:

- version appears before character;
- current and baseline appear on the next row;
- changing version changes available character options;
- changing character filters both cohort selects;
- an unavailable previous character resets to “全部角色”;
- current/baseline select focus is restored after metrics refresh;
- mobile viewport shows one ordered column;
- no console or page errors.

- [ ] **Step 4: Present the preview for user acceptance**

Leave the isolated preview tab open on the new layout. Do not replace the existing `61122` launchd service until the user approves the preview.
