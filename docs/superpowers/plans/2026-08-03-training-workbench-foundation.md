# Training Workbench Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the canonical run-data layer, record trustworthy evaluation outcomes, and replace the viewer's replay-first landing page with a training-progress dashboard.

**Architecture:** Add a dependency-free `agent.run_workbench` package for source classification, canonical records, cataloging, joining, and metrics. Keep `agent/run_progress_viewer.py` as the compatible CLI/server entrypoint, but move new HTML/CSS/JavaScript into static files and expose stable JSON APIs. Enrich new `CombatEnv` and evaluation rows without rewriting historical logs; old rows remain visible as metadata-incomplete cohorts.

**Tech Stack:** Python 3.11 dataclasses/enums, JSON/JSONL, `http.server`, vanilla HTML/CSS/JavaScript, pytest.

---

## Execution Preconditions

- Start implementation in a dedicated worktree created from commit `97f4e17` or later.
- Do not stage or modify the user's existing `eval_and_report.py` changes, `.superpowers/`, checkpoints, logs, HTML exports, crash logs, or training artifacts.
- Keep the current entrypoint and default port working: `python -m agent.run_progress_viewer --port 8765`.
- Do not add a frontend framework or database in this phase.

### Task 1: Define The Canonical Run Contracts

**Files:**
- Create: `agent/run_workbench/__init__.py`
- Create: `agent/run_workbench/models.py`
- Create: `tests/agent/run_workbench/__init__.py`
- Create: `tests/agent/run_workbench/test_models.py`

- [ ] **Step 1: Write the failing model tests**

Create `tests/agent/run_workbench/test_models.py` with assertions for explicit unknown values, technical statuses, global-floor ordering, and JSON-safe serialization:

```python
from agent.run_workbench.models import (
    Capabilities,
    Coverage,
    DeltaQuality,
    RunDelta,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)


def test_unknown_delta_is_not_serialized_as_zero():
    delta = RunDelta(value=None, quality=DeltaQuality.UNKNOWN)
    assert delta.to_dict() == {"value": None, "quality": "unknown"}


def test_technical_statuses_are_explicit():
    assert RunStatus.CRASH.is_technical
    assert RunStatus.TIMEOUT.is_technical
    assert RunStatus.STUCK.is_technical
    assert not RunStatus.DEAD.is_technical
    assert not RunStatus.WIN.is_technical


def test_record_serializes_capabilities_and_partial_coverage():
    record = RunRecord(
        run_id="run-1",
        source_id="eval.jsonl:1",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(character="Ironclad", seed="eval_fixed_0"),
        outcome=RunOutcome(status=RunStatus.DEAD, max_global_floor=21),
        coverage=Coverage(complete_run=False, first_recorded_floor=18, last_recorded_floor=21),
        capabilities=Capabilities(visited_route=True),
    )
    payload = record.to_dict()
    assert payload["outcome"]["max_global_floor"] == 21
    assert payload["coverage"] == {
        "complete_run": False,
        "first_recorded_floor": 18,
        "last_recorded_floor": 21,
    }
    assert payload["capabilities"]["visited_route"] is True
    assert payload["capabilities"]["turn_replay"] is False
```

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_models.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent.run_workbench'`.

- [ ] **Step 3: Implement the model module**

Use string enums and dataclasses so the API contract has no third-party runtime dependency. The public types must include the following fields and behavior:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SourceKind(str, Enum):
    NATIVE_RUN = "native_run"
    REPLAY_JSONL = "replay_jsonl"
    DECK_HISTORY = "deck_history"
    EVAL_RESULTS = "eval_results"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class RunStatus(str, Enum):
    WIN = "win"
    DEAD = "dead"
    CRASH = "crash"
    TIMEOUT = "timeout"
    STUCK = "stuck"
    RESET_FAILURE = "reset_failure"
    INVALID = "invalid"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"

    @property
    def is_technical(self) -> bool:
        return self in {
            RunStatus.CRASH,
            RunStatus.TIMEOUT,
            RunStatus.STUCK,
            RunStatus.RESET_FAILURE,
            RunStatus.INVALID,
        }


class DeltaQuality(str, Enum):
    EXACT = "exact"
    DERIVED = "derived"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RunDelta:
    value: Any = None
    quality: DeltaQuality = DeltaQuality.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "quality": self.quality.value}


@dataclass(frozen=True)
class RunMetadata:
    character: str | None = None
    seed: str | None = None
    game_version: str | None = None
    checkpoint: str | None = None
    evaluation_mode: str | None = None
    scenario: str | None = None
    ascension: int | None = None
    modifiers: tuple[str, ...] = ()
    started_at: float | None = None
    ended_at: float | None = None


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus = RunStatus.UNKNOWN
    victory: bool | None = None
    max_global_floor: int | None = None
    max_floor_label: str | None = None
    technical_failure_kind: str | None = None


@dataclass(frozen=True)
class Coverage:
    complete_run: bool = False
    first_recorded_floor: int | None = None
    last_recorded_floor: int | None = None


@dataclass(frozen=True)
class Capabilities:
    full_map: bool = False
    visited_route: bool = False
    node_rewards: bool = False
    final_inventory: bool = False
    decisions: bool = False
    turn_replay: bool = False


@dataclass
class RunRecord:
    run_id: str
    source_id: str
    source_kind: SourceKind
    metadata: RunMetadata = field(default_factory=RunMetadata)
    outcome: RunOutcome = field(default_factory=RunOutcome)
    coverage: Coverage = field(default_factory=Coverage)
    capabilities: Capabilities = field(default_factory=Capabilities)
    acts: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    replay_by_node: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if hasattr(value, "to_dict"):
                return value.to_dict()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [encode(item) for item in value]
            return value

        return encode(asdict(self))
```

Export the stable public names from `agent/run_workbench/__init__.py`.

- [ ] **Step 4: Run the model tests**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_models.py -q`

Expected: all model tests pass.

- [ ] **Step 5: Commit the canonical contracts**

```bash
git add agent/run_workbench/__init__.py agent/run_workbench/models.py tests/agent/run_workbench/__init__.py tests/agent/run_workbench/test_models.py
git commit -m "feat: define canonical run records"
```

### Task 2: Classify Sources Before Parsing Them

**Files:**
- Create: `agent/run_workbench/sources.py`
- Create: `tests/agent/run_workbench/test_sources.py`
- Create: `tests/fixtures/run_workbench/native_run.json`
- Create: `tests/fixtures/run_workbench/replay.jsonl`
- Create: `tests/fixtures/run_workbench/partial_replay.jsonl`
- Create: `tests/fixtures/run_workbench/deck_history.jsonl`
- Create: `tests/fixtures/run_workbench/eval_results.jsonl`
- Create: `tests/fixtures/run_workbench/summary.jsonl`
- Create: `tests/fixtures/run_workbench/malformed.jsonl`

- [ ] **Step 1: Add minimal synthetic fixtures**

Keep fixtures small and copyright-free. The native fixture must include `players`, `seed`, `build_id`, `acts`, and `map_point_history`. The replay fixtures must use the same `{"type": "state", "data": ...}` / `{"type": "action", "data": ...}` contract already consumed by `parse_game_progress`. The deck-history fixture must contain `milestone`, `card_pick`, and `outcome` rows joined by `run_id`. The evaluation fixture must contain `event: "eval_result"`. The summary fixture must contain boss-deck or aggregate rows but no state/action pair.

- [ ] **Step 2: Write the failing classification tests**

Test both extension and content shape. Include these assertions:

```python
from pathlib import Path

import pytest

from agent.run_workbench.models import SourceKind
from agent.run_workbench.sources import SourceFormatError, classify_path, read_json_records


FIXTURES = Path("tests/fixtures/run_workbench")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("native_run.json", SourceKind.NATIVE_RUN),
        ("replay.jsonl", SourceKind.REPLAY_JSONL),
        ("partial_replay.jsonl", SourceKind.REPLAY_JSONL),
        ("deck_history.jsonl", SourceKind.DECK_HISTORY),
        ("eval_results.jsonl", SourceKind.EVAL_RESULTS),
        ("summary.jsonl", SourceKind.SUMMARY),
    ],
)
def test_classifies_supported_shapes(name, expected):
    assert classify_path(FIXTURES / name).kind is expected


def test_malformed_jsonl_names_the_file_and_line():
    with pytest.raises(SourceFormatError, match=r"malformed.jsonl:2"):
        read_json_records(FIXTURES / "malformed.jsonl")
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_sources.py -q`

Expected: import failure because `agent.run_workbench.sources` does not exist.

- [ ] **Step 4: Implement record reading and shape classification**

Implement `SourceDescriptor(kind, record_count, message)` and `SourceFormatError`. Classification order must be deterministic:

```python
def classify_records(records: list[dict], *, suffix: str) -> SourceDescriptor:
    if suffix == ".run" or (
        len(records) == 1
        and isinstance(records[0].get("players"), list)
        and "map_point_history" in records[0]
    ):
        return SourceDescriptor(SourceKind.NATIVE_RUN, len(records), "native run history")

    types = {str(row.get("type", "")) for row in records}
    events = {str(row.get("event", "")) for row in records}
    if "state" in types:
        return SourceDescriptor(SourceKind.REPLAY_JSONL, len(records), "state/action replay")
    if events & {"milestone", "card_pick", "outcome"}:
        return SourceDescriptor(SourceKind.DECK_HISTORY, len(records), "training deck history")
    if "eval_result" in events:
        return SourceDescriptor(SourceKind.EVAL_RESULTS, len(records), "per-game evaluation results")
    looks_like_boss_deck = any(
        {"checkpoint", "cards", "enemies", "hp_at_entry"}.issubset(row)
        for row in records
    )
    if events & {"result", "summary"} or looks_like_boss_deck:
        return SourceDescriptor(SourceKind.SUMMARY, len(records), "summary records; no replay states")
    return SourceDescriptor(SourceKind.UNKNOWN, len(records), "unsupported JSON shape")
```

`read_json_records` must parse `.run`/`.json` as one JSON object and `.jsonl` line by line. Blank lines are ignored; JSON objects nested under a top-level list are accepted only for `.json` files.

- [ ] **Step 5: Run the source tests**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_sources.py -q`

Expected: all source tests pass.

- [ ] **Step 6: Commit source classification and fixtures**

```bash
git add agent/run_workbench/sources.py tests/agent/run_workbench/test_sources.py tests/fixtures/run_workbench
git commit -m "feat: classify run data sources"
```

### Task 3: Adapt Supported Sources Into Run Records

**Files:**
- Create: `agent/run_workbench/adapters.py`
- Create: `agent/run_workbench/joiner.py`
- Create: `tests/agent/run_workbench/test_adapters.py`
- Create: `tests/agent/run_workbench/test_joiner.py`
- Modify: `agent/run_progress_viewer.py`

- [ ] **Step 1: Write failing adapter tests**

Cover these contracts:

- native `.run`: `game_version=build_id`, seed, character, route and reward capabilities, no turn replay;
- replay: rooms from `parse_game_progress`, recorded range and replay capabilities;
- partial replay: `complete_run=False` and first/last floor from observed states;
- deck history: one outcome per `run_id`, historical missing metadata preserved as `None`;
- eval results: one canonical record per `eval_result` row;
- summary: returns a source summary object, not a zero-room `RunRecord`;
- technical outcome status remains technical after normalization.

Use dependency injection for the legacy parser so the adapter package does not import the HTTP handler:

```python
records = adapt_path(
    FIXTURES / "replay.jsonl",
    replay_parser=parse_game_progress,
)
assert records[0].capabilities.turn_replay is True
assert records[0].coverage.first_recorded_floor == 1
```

- [ ] **Step 2: Write failing join tests**

Require exact `run_id` matching and reject guessed historical merges:

```python
def test_exact_run_id_merges_metadata_and_capabilities():
    merged = join_records([deck_record, eval_record])
    assert len(merged) == 1
    assert merged[0].metadata.checkpoint == "model_14000k.zip"


def test_seed_and_timestamp_without_run_id_do_not_silently_merge():
    merged = join_records([old_deck_record, replay_record])
    assert len(merged) == 2
    assert any("ambiguous historical identity" in warning for record in merged for warning in record.warnings)
```

- [ ] **Step 3: Run tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_joiner.py -q
```

Expected: missing adapter/joiner modules.

- [ ] **Step 4: Implement adapters and strict joining**

Expose these entrypoints:

```python
def adapt_path(
    path: Path,
    *,
    replay_parser: Callable[[list[dict], str | None], dict] | None = None,
) -> AdaptedSource: ...


def join_records(records: Iterable[RunRecord]) -> list[RunRecord]: ...
```

`AdaptedSource` must carry `descriptor`, `runs`, `summary`, and `errors`. Do not manufacture a run for a summary-only file. Merge records only when all non-empty `run_id` values match exactly. Combine capability booleans with logical OR, choose non-null metadata deterministically, and append a warning if two non-null values conflict.

Move no parsing logic out of `run_progress_viewer.py` yet. Call the existing `parse_game_progress` through the injected function so its tests stay stable.

- [ ] **Step 5: Run focused and legacy parser tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_joiner.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit the adapters**

```bash
git add agent/run_workbench/adapters.py agent/run_workbench/joiner.py tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_joiner.py agent/run_progress_viewer.py
git commit -m "feat: normalize run data sources"
```

### Task 4: Record Evaluation Identity And Technical Outcomes

**Files:**
- Modify: `agent/combat_env.py`
- Modify: `agent/eval_rl.py`
- Modify: `tests/agent/test_combat_env.py`
- Modify: `tests/agent/test_eval_rl.py`

- [ ] **Step 1: Write failing `CombatEnv` outcome tests**

Construct the environment with `dry_run=True`, a temporary `DECK_HISTORY_PATH`, buffered records, and a supplied run context. Verify the emitted outcome adds fields while the historical fields remain:

```python
env = CombatEnv(
    dry_run=True,
    run_context={
        "run_id": "eval-14000k-000",
        "checkpoint": "model_14000k.zip",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "game_version": "v0.103.2",
    },
)
env._run_id = "eval-14000k-000"
env._run_seed = "eval_fixed_0"
env._run_milestone_records = [{"event": "milestone", "run_id": env._run_id}]
env._run_max_floor = 21
env._emit_run_outcome({}, victory=False, status="dead")

outcome = json.loads(history_path.read_text().splitlines()[-1])
assert outcome["run_id"] == "eval-14000k-000"
assert outcome["seed"] == "eval_fixed_0"
assert outcome["checkpoint"] == "model_14000k.zip"
assert outcome["status"] == "dead"
assert outcome["technical_failure_kind"] is None
```

Also test `status="crash"` produces `won=False` and `technical_failure_kind="crash"`.

- [ ] **Step 2: Write failing structured-evaluation tests**

Add tests for a pure helper `append_eval_result_row(path, row)` and parser option `--results-log`. Write one row for every attempt, including attempts that will be retried, so technical failures remain queryable. A written row must contain:

```json
{
  "event": "eval_result",
  "run_id": "eval-14000k-000",
  "batch_id": "eval-14000k-20260803T120000",
  "checkpoint": "model_14000k.zip",
  "character": "Ironclad",
  "game_version": "v0.103.2",
  "evaluation_mode": "fixed",
  "scenario": "full_run",
  "seed": "eval_fixed_0",
  "status": "dead",
  "max_global_floor": 21,
  "combat_wins": 18,
  "attempt_index": 1,
  "retrying": false,
  "included_in_gameplay": true
}
```

The tests must verify `crash`, `timeout`, and `stuck` attempts are written before retrying, use unique `run_id` values, set `included_in_gameplay=false`, and remain excluded from `avg_floor` by the existing `summarize_eval_results` behavior.

- [ ] **Step 3: Run the focused tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_combat_env.py tests/agent/test_eval_rl.py -q
```

Expected: failures for the new constructor argument, status argument, writer, and CLI option.

- [ ] **Step 4: Add run context to `CombatEnv` without changing training behavior**

Add `run_context: dict | None = None` to the end of `CombatEnv.__init__`, copy it into `self._run_context`, and initialize the current seed:

```python
self._run_context = dict(run_context or {})
self._run_seed = self._seed
self._run_outcome_emitted = False
```

When a new run starts:

```python
self._run_id = str(
    self._run_context.get("run_id")
    or f"r{int(time.time()*1000) % 10**9:09d}_{random.randint(0, 9999):04d}"
)
self._run_seed = run_seed
self._run_outcome_emitted = False
```

Change `_emit_run_outcome` to accept `status: str | None = None`. Preserve all existing fields and add `seed`, `character`, `ascension`, `checkpoint`, `evaluation_mode`, `scenario`, `game_version`, `status`, and `technical_failure_kind`. Existing natural terminal call sites may omit `status`; infer `win` or `dead` there so training behavior is unchanged.

Make emission idempotent with `_run_outcome_emitted`. Audit the existing crash, stuck, timeout/reset-failure, and natural `game_over` paths: any path that ends the current run must flush buffered history once with its exact status. Add a focused test for duplicate terminal notifications and one test for each technical status that the environment can observe directly.

Do not let logging exceptions propagate into the environment.

- [ ] **Step 5: Add the structured evaluation writer**

Add a dependency-free append helper that writes one row immediately after each attempt is classified. In `run_eval_verbose`, set a unique run id before constructing each environment, pass the complete `run_context`, capture it in the attempt row, and retain finalized results in `stats["results"]`:

```python
run_id = f"{batch_id}-{i:03d}-a{total_attempts:02d}"
env_kwargs["run_context"] = {
    "run_id": run_id,
    "checkpoint": os.path.basename(str(checkpoint_name)) if checkpoint_name else None,
    "evaluation_mode": "fixed" if fixed_seeds else "random",
    "scenario": "native_save" if native_save_path else "full_run",
    "game_version": game_version,
}
```

Call the writer before the existing `if retrying: ... continue` branch. The final valid/dead attempt and every technical attempt therefore share one schema; `included_in_gameplay` is true only for `win`/`dead`. The adapter creates one canonical attempt record per row, while metrics continue to aggregate only valid gameplay statuses.

Add optional `batch_id`, `game_version`, `scenario`, and `results_log_path` keyword arguments at the end of `run_eval_verbose`. Use the supplied `scenario` in both `run_context` and result rows. Add CLI flags:

```python
p.add_argument("--results-log", default="data/eval_results.jsonl")
p.add_argument("--game-version", default=os.environ.get("STS2_GAME_VERSION"))
p.add_argument("--scenario", default="full_run")
```

`none` disables the results log. Use an explicit supplied game version or `None`; do not guess a build from the Python package version.

- [ ] **Step 6: Run the evaluation and environment tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/test_combat_env.py tests/agent/test_eval_rl.py -q
```

Expected: all tests pass, including existing result aggregation tests.

- [ ] **Step 7: Commit outcome instrumentation**

```bash
git add agent/combat_env.py agent/eval_rl.py tests/agent/test_combat_env.py tests/agent/test_eval_rl.py
git commit -m "feat: record comparable evaluation outcomes"
```

### Task 5: Compute Comparable Training Cohorts

**Files:**
- Create: `agent/run_workbench/metrics.py`
- Create: `tests/agent/run_workbench/test_metrics.py`

- [ ] **Step 1: Write failing metrics tests**

Build small `RunRecord` factories and test:

- average, median, maximum global floor, valid N, win rate;
- Act 2 entry rate from `max_global_floor >= 18`;
- technical outcomes counted separately and excluded by default;
- missing floor remains excluded, not converted to zero;
- strict comparison accepts equal character/version/mode/scenario/fixed seed sets;
- comparison returns visible mismatch reasons for character, version, mode, scenario, seed set, or empty valid cohort;
- a non-paired seed comparison can be requested but is labeled `paired=False`.

The main assertion should make denominator behavior unambiguous:

```python
summary = summarize_cohort([dead_at_7, dead_at_21, crash_at_1])
assert summary.valid_n == 2
assert summary.technical_n == 1
assert summary.avg_global_floor == 14.0
assert summary.median_global_floor == 14.0
assert summary.max_global_floor == 21
assert summary.act2_entry_rate == 0.5
```

- [ ] **Step 2: Run and confirm the missing-module failure**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_metrics.py -q`

Expected: import failure for `agent.run_workbench.metrics`.

- [ ] **Step 3: Implement metrics and comparison contracts**

Expose immutable `CohortSummary` and `ComparisonResult` dataclasses plus:

```python
def summarize_cohort(records: Iterable[RunRecord], *, include_technical: bool = False) -> CohortSummary: ...

def compare_cohorts(
    current: Iterable[RunRecord],
    baseline: Iterable[RunRecord],
    *,
    allow_cross_version: bool = False,
    require_paired_seeds: bool = True,
) -> ComparisonResult: ...
```

Use the `statistics` module, not NumPy, in this package. The summary must also return a global-floor histogram/funnel and a chronological trend point list. Treat only `win` and `dead` as valid gameplay outcomes by default.

- [ ] **Step 4: Run metrics tests**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_metrics.py -q`

Expected: all metrics tests pass.

- [ ] **Step 5: Commit metrics**

```bash
git add agent/run_workbench/metrics.py tests/agent/run_workbench/test_metrics.py
git commit -m "feat: compute training progress cohorts"
```

### Task 6: Add A Lightweight Catalog And Stable Viewer APIs

**Files:**
- Create: `agent/run_workbench/catalog.py`
- Create: `tests/agent/run_workbench/test_catalog.py`
- Create: `tests/agent/run_workbench/test_http_api.py`
- Modify: `agent/run_progress_viewer.py`

- [ ] **Step 1: Write failing catalog tests**

Use temporary roots containing mixed `.run`, `.json`, and `.jsonl` fixtures. Verify that:

- the catalog classifies files before exposing them;
- summary files have `open_mode="summary"`, not `open_mode="run"`;
- malformed files have an error but do not prevent good files from loading;
- the index exposes `source_kind`, `mtime`, `size`, metadata completeness, and a stable source id;
- `get_run(run_id)` lazily parses only the requested source;
- roots are configurable and path traversal is rejected.

- [ ] **Step 2: Write failing HTTP contract tests**

Start a temporary `ThreadingHTTPServer` with a handler factory bound to a test catalog. Test JSON shapes for:

```text
GET /api/catalog
GET /api/cohorts
GET /api/metrics?current=<cohort>&baseline=<cohort>
GET /api/run?id=<run_id>
GET /api/source?id=<source_id>
```

Retain and retest the existing endpoints:

```text
GET /api/logs
GET /api/latest
GET /api/log?name=<name>
GET /api/translations?lang=zh
POST /api/parse
```

The `/api/source` response for a summary file must contain `view="summary"` and never `rooms: []` as if it were a completed run. Change `POST /api/parse` to classify the uploaded `source_name` and text before adapting it; test native `.run`, replay JSONL, summary JSONL, and malformed JSONL uploads.

- [ ] **Step 3: Run catalog/API tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py -q
```

Expected: missing catalog and API contracts.

- [ ] **Step 4: Implement the catalog**

Construct `RunCatalog` with explicit roots and the legacy replay parser:

```python
catalog = RunCatalog(
    roots=[ROOT / "logs", ROOT / "data"],
    replay_parser=parse_game_progress,
)
```

The catalog may sample a bounded number of rows for classification, but parsing a requested run must read the complete source. Cache parsed results by `(resolved_path, mtime_ns, size)` so changed logs invalidate naturally.

Add repeatable CLI `--source-root PATH` support. With no explicit roots, keep `logs/` and `data/` as defaults. Uploaded files are parsed in memory and never written to disk by the viewer.

- [ ] **Step 5: Refactor the handler for dependency injection**

Keep `ViewerHandler` public, but add `make_viewer_handler(catalog)` so tests and the real server can bind a catalog without globals. Parse query strings with `parse_qs`, return JSON errors with status codes, and keep filesystem access inside `RunCatalog`.

- [ ] **Step 6: Run catalog, API, and existing viewer tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the catalog and API**

```bash
git add agent/run_workbench/catalog.py agent/run_progress_viewer.py tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py
git commit -m "feat: expose training workbench APIs"
```

### Task 7: Build The Training-First Dashboard

**Files:**
- Create: `agent/run_workbench/static/index.html`
- Create: `agent/run_workbench/static/styles.css`
- Create: `agent/run_workbench/static/app.js`
- Create: `tests/agent/run_workbench/test_static_contract.py`
- Modify: `agent/run_progress_viewer.py`

- [ ] **Step 1: Write the failing static contract tests**

Verify the root document loads `/static/styles.css` and `/static/app.js`, contains stable DOM targets, and does not embed source-shape parsing logic. Required ids:

```text
currentCohort baselineCohort characterFilter versionFilter validityFilter sourceFile
avgFloor medianFloor maxFloor act2Rate validCount technicalCount
trendChart funnelChart comparisonBanner anomalyList representativeRuns
sourceCatalog workbenchStatus
```

Test static responses for correct content types and path traversal rejection.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py -q`

Expected: the existing embedded page lacks the required static contracts.

- [ ] **Step 3: Implement the page shell and static serving**

Replace the root response with `static/index.html`, and serve only a fixed allowlist of `styles.css` and `app.js`. Keep the old embedded `HTML` only until this task's tests pass, then delete it in the same commit so the UI has one source of truth.

The dashboard layout order must be:

1. cohort and baseline controls;
2. metric cards with explicit denominators;
3. trend and conversion funnel;
4. comparison warning or improvement banner;
5. anomalies and representative runs;
6. classified source catalog.

- [ ] **Step 4: Render charts without a charting dependency**

Use accessible inline SVG generated from API arrays. `app.js` must format missing values as `—`, not `0`, and show technical failures in a separate count/list. The initial fetch sequence is:

```javascript
async function bootstrap() {
  setStatus("正在读取训练记录…");
  const [{ cohorts }, { sources }] = await Promise.all([
    getJSON("/api/cohorts"),
    getJSON("/api/catalog"),
  ]);
  renderCohortOptions(cohorts);
  renderSources(sources);
  await refreshMetrics();
  setStatus("已载入");
}
```

If comparison is invalid, render every reason returned by the API; do not compute comparability in JavaScript.

Wire `sourceFile` to `POST /api/parse`. Render the returned `view` contract: run uploads open the canonical run view, summary uploads open the summary panel, and malformed uploads show their filename/line error without modifying the catalog.

- [ ] **Step 5: Run static and API tests**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py tests/agent/run_workbench/test_http_api.py tests/agent/test_run_progress_viewer.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Run a local HTTP smoke test**

Start the server on a disposable port:

```bash
.venv/bin/python -m agent.run_progress_viewer --host 127.0.0.1 --port 8766
```

In another shell:

```bash
curl -fsS http://127.0.0.1:8766/ | rg 'training-workbench|avgFloor|app.js'
curl -fsS http://127.0.0.1:8766/api/catalog | .venv/bin/python -m json.tool >/dev/null
curl -fsS http://127.0.0.1:8766/api/cohorts | .venv/bin/python -m json.tool >/dev/null
```

Expected: each command exits 0; malformed/summary files appear with their actual classification.

- [ ] **Step 7: Commit the dashboard**

```bash
git add agent/run_workbench/static agent/run_progress_viewer.py tests/agent/run_workbench/test_static_contract.py
git commit -m "feat: show training progress dashboard"
```

### Task 8: Foundation Regression And Acceptance

**Files:**
- Modify only if verification exposes a defect in files already listed above.

- [ ] **Step 1: Run the complete workbench test set**

Run:

```bash
.venv/bin/python -m pytest tests/agent/run_workbench tests/agent/test_run_progress_viewer.py tests/agent/test_eval_rl.py tests/agent/test_combat_env.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the broader agent regression set**

Run:

```bash
.venv/bin/python -m pytest tests/agent -q
```

Expected: all tests pass. If an existing unrelated test fails, record the exact pre-existing failure and do not hide it.

- [ ] **Step 3: Validate source honesty with real local data**

Run the server against the repository's current `logs/` and `data/`. Verify through `/api/catalog` that:

- a GameLogger state/action JSONL opens as a replay-capable run;
- `deck_history.jsonl` contributes outcome records;
- boss/evaluation summary JSONL is labeled summary;
- malformed records do not remove valid sources;
- unknown metadata is displayed as unknown and excluded from strict comparisons.

- [ ] **Step 4: Review the worktree before handoff**

Run:

```bash
git status --short
git diff --check
git log --oneline --max-count=8
```

Expected: only planned source/test/doc changes are present; no checkpoints, logs, game assets, `.superpowers/`, or user-owned `eval_and_report.py` changes are staged.

- [ ] **Step 5: Record the verified foundation boundary**

The handoff must state that this plan delivers training aggregates, cohort comparison, source classification, and a training-first homepage. Full-map reconstruction, original node artwork, node deltas, and turn-level diagnostics are intentionally delivered by the next two plans.
