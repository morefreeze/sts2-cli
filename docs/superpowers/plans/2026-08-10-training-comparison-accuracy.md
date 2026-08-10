# Accurate Training Cohort Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every new training/evaluation result versioned and make the dashboard auto-select a baseline only when the server proves the cohorts are strictly comparable.

**Architecture:** A small shared metadata module resolves and validates launch metadata once. `CombatEnv` persists a self-contained start row and terminal row, while training/evaluation entrypoints propagate the resolved values. The metrics layer derives a bounded opaque readiness signature, the catalog chooses compatible defaults without pairwise comparisons, and the browser consumes that server-owned decision while keeping historical single-batch statistics visible.

**Tech Stack:** Python 3 dataclasses and JSONL logging, existing `RunCatalog`/metrics contracts, vanilla JavaScript dashboard, pytest, Node syntax/behavior probes.

---

## File Map

- Create `agent/run_metadata.py`: one source of truth for game-version precedence, provenance, and ascension validation.
- Create `tests/agent/test_run_metadata.py`: isolated resolver and boundary tests.
- Modify `agent/combat_env.py`: persist `run_start`; repeat authoritative metadata on `outcome`.
- Modify `agent/train.py`: require version before environment creation and pass stable metadata to workers and checkpoint evaluations.
- Modify `agent/eval_rl.py`: require version, add ascension, and repeat all comparison axes on each result.
- Modify `agent/run_workbench/models.py`, `agent/run_workbench/adapters.py`: preserve version-source provenance in canonical records.
- Modify `tests/agent/test_combat_env.py`, `tests/agent/test_train.py`, `tests/agent/test_eval_rl.py`: producer RED/GREEN coverage.
- Modify `agent/run_workbench/metrics.py`: immutable readiness descriptor and opaque signature.
- Modify `tests/agent/run_workbench/test_metrics.py`: exact completeness, mismatch, order, bounds, and JSON-safe tests.
- Modify `agent/run_workbench/catalog.py`: attach readiness and newest compatible baseline to each cohort.
- Modify `tests/agent/run_workbench/test_catalog.py`, `tests/agent/run_workbench/test_http_api.py`: catalog/API contract tests.
- Modify `agent/run_workbench/static/app.js`, `agent/run_workbench/static/index.html`: server-owned baseline selection and neutral incomplete states.
- Modify `tests/agent/run_workbench/test_static_contract.py`: static and Node behavior contracts.

### Task 1: Resolve Game Version and Ascension Once

**Files:**
- Create: `agent/run_metadata.py`
- Create: `tests/agent/test_run_metadata.py`

- [ ] **Step 1: Write the failing resolver tests**

```python
from __future__ import annotations

import pytest

from agent.run_metadata import (
    ResolvedGameVersion,
    resolve_game_version,
    validate_ascension,
)


def test_explicit_version_wins_and_records_cli_source():
    resolved = resolve_game_version("  v0.103.2  ", {"STS2_GAME_VERSION": "v0.102.0"})
    assert resolved == ResolvedGameVersion("v0.103.2", "cli")


def test_environment_version_is_used_when_cli_is_missing_or_blank():
    assert resolve_game_version(None, {"STS2_GAME_VERSION": "v0.103.2"}) == (
        ResolvedGameVersion("v0.103.2", "environment")
    )
    assert resolve_game_version("  ", {"STS2_GAME_VERSION": " v0.103.2 "}) == (
        ResolvedGameVersion("v0.103.2", "environment")
    )


@pytest.mark.parametrize("cli_value, environment", [(None, {}), (" ", {}), (None, {"STS2_GAME_VERSION": " "})])
def test_missing_version_is_rejected(cli_value, environment):
    with pytest.raises(ValueError, match="--game-version|STS2_GAME_VERSION"):
        resolve_game_version(cli_value, environment)


@pytest.mark.parametrize("value", [0, 10])
def test_supported_ascension_boundaries(value):
    assert validate_ascension(value) == value


@pytest.mark.parametrize("value", [True, -1, 11, 1.0, "1"])
def test_invalid_ascension_is_rejected(value):
    with pytest.raises(ValueError, match="0..10"):
        validate_ascension(value)
```

- [ ] **Step 2: Run the test and confirm the missing module RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_run_metadata.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent.run_metadata'`.

- [ ] **Step 3: Implement the immutable resolver contract**

```python
"""Authoritative launch metadata shared by training and evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Literal, Mapping


GameVersionSource = Literal["cli", "environment"]


@dataclass(frozen=True)
class ResolvedGameVersion:
    value: str
    source: GameVersionSource

    def to_fields(self) -> dict[str, str]:
        return {
            "game_version": self.value,
            "game_version_source": self.source,
        }


def _nonempty_text(value: object) -> str | None:
    if type(value) is not str:
        return None
    normalized = value.strip()
    return normalized or None


def resolve_game_version(
    cli_value: object,
    environment: Mapping[str, str] | None = None,
) -> ResolvedGameVersion:
    explicit = _nonempty_text(cli_value)
    if explicit is not None:
        return ResolvedGameVersion(explicit, "cli")
    source = os.environ if environment is None else environment
    inherited = _nonempty_text(source.get("STS2_GAME_VERSION"))
    if inherited is not None:
        return ResolvedGameVersion(inherited, "environment")
    raise ValueError(
        "game version is required; pass --game-version or set STS2_GAME_VERSION"
    )


def validate_ascension(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 10:
        raise ValueError("ascension must be an integer in the supported range 0..10")
    return value
```

- [ ] **Step 4: Run resolver tests**

Run the Step 2 command. Expected: all tests pass.

- [ ] **Step 5: Commit the shared metadata boundary**

```bash
git add agent/run_metadata.py tests/agent/test_run_metadata.py
git commit -m "feat: validate run launch metadata"
```

### Task 2: Persist a Versioned Run Start and Terminal Outcome

**Files:**
- Modify: `agent/combat_env.py:622-632, 649-735, 1361-1412`
- Modify: `tests/agent/test_combat_env.py:98-160, 218-245`

- [ ] **Step 1: Write failing durability tests**

Extend `_recording_env` with `"game_version_source": "cli"`, then add:

```python
def test_run_start_and_outcome_repeat_authoritative_metadata(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env._run_seed = "eval_fixed_0"

    env._emit_run_start()
    env._emit_run_start()
    env._emit_run_outcome({}, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert [row["event"] for row in rows] == ["run_start", "outcome"]
    for row in rows:
        assert row["run_id"] == "eval-14000k-000"
        assert row["seed"] == "eval_fixed_0"
        assert row["character"] == "Ironclad"
        assert row["ascension"] == 3
        assert row["checkpoint"] == "model_14000k.zip"
        assert row["evaluation_mode"] == "fixed"
        assert row["scenario"] == "full_run"
        assert row["game_version"] == "v0.103.2"
        assert row["game_version_source"] == "cli"


def test_outcome_recovers_start_row_after_initial_start_write_failure(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    real_open = open
    attempts = 0

    def fail_first_open(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("start unavailable")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_first_open)
    with pytest.warns(RuntimeWarning, match="start unavailable"):
        env._emit_run_start()
    env._emit_run_outcome({}, victory=False, status="dead")

    assert [row["event"] for row in _read_history_rows(history_path)] == [
        "run_start",
        "outcome",
    ]
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_combat_env.py -q
```

Expected: failures show `_emit_run_start` and `game_version_source` are absent.

- [ ] **Step 3: Implement one metadata-row builder and idempotent start logging**

Add per-run state next to `_run_outcome_emitted` and reset it for each new run:

```python
self._run_start_emitted = False
self._run_started_at = time.time()
```

Add these methods before `_emit_run_outcome`:

```python
def _run_metadata_row(self, event: str) -> dict:
    return {
        "event": event,
        "run_id": self._run_id,
        "seed": self._run_seed,
        "character": self.character,
        "ascension": self.ascension,
        "checkpoint": self._run_context.get("checkpoint"),
        "evaluation_mode": self._run_context.get("evaluation_mode"),
        "scenario": self._run_context.get("scenario"),
        "game_version": self._run_context.get("game_version"),
        "game_version_source": self._run_context.get("game_version_source"),
    }

def _emit_run_start(self) -> None:
    if self._run_start_emitted or not self._deck_history_path:
        return
    row = {**self._run_metadata_row("run_start"), "ts": self._run_started_at}
    try:
        os.makedirs(os.path.dirname(self._deck_history_path) or ".", exist_ok=True)
        with open(self._deck_history_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    except Exception as exc:
        self._report_run_logging_error("run start logging failed", exc)
        return
    self._run_start_emitted = True

def _report_run_logging_error(self, prefix: str, exc: Exception) -> None:
    error = f"{prefix} for {self._run_id}: {type(exc).__name__}: {exc}"
    self._run_logging_errors.append(error)
    try:
        warnings.warn(error, RuntimeWarning, stacklevel=2)
    except Warning:
        print(error, file=sys.stderr)
```

At the fresh-run boundary call `_emit_run_start()` after run identity/seed reset and before `_start_proc()`. Replace the duplicated outcome fields with:

```python
outcome = {
    **self._run_metadata_row("outcome"),
    "max_floor": int(self._run_max_floor),
    "won": bool(victory) and technical_failure_kind is None,
    "boss": getattr(self, "_run_boss_id", None),
    "status": final_status,
    "technical_failure_kind": technical_failure_kind,
    "ts": time.time(),
}
start = {
    **self._run_metadata_row("run_start"),
    "ts": self._run_started_at,
}
rows = ([] if self._run_start_emitted else [start]) + [
    *self._run_milestone_records,
    *self._run_card_pick_records,
    outcome,
]
payload = "".join(
    json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows
)
```

Write this payload with the existing single append. Mark `_run_start_emitted=True` and `_run_outcome_emitted=True` only after it succeeds; on failure call `_report_run_logging_error("run outcome logging failed", exc)`.

- [ ] **Step 4: Run combat logging regression tests**

Run the Step 2 command. Expected: all tests pass, including existing idempotency and retryability tests.

- [ ] **Step 5: Commit the durable logging boundary**

```bash
git add agent/combat_env.py tests/agent/test_combat_env.py
git commit -m "feat: persist versioned run boundaries"
```

### Task 3: Propagate Metadata Through Training and Evaluation

**Files:**
- Modify: `agent/eval_rl.py:454-525, 732-752, 866-873, 955-973`
- Modify: `agent/train.py:204-211, 408-450, 548-582, 582-635, 706-892`
- Modify: `agent/run_workbench/models.py:382-399`
- Modify: `agent/run_workbench/adapters.py:155-175, 402-565`
- Modify: `agent/run_workbench/catalog.py:100-120, 920-945, 1250-1315`
- Modify: `tests/agent/test_eval_rl.py`
- Modify: `tests/agent/test_train.py`
- Modify: `tests/agent/run_workbench/test_models.py`
- Modify: `tests/agent/run_workbench/test_adapters.py`

- [ ] **Step 1: Write failing evaluation tests**

Add a reusable test constant:

```python
VERSION_FIELDS = {
    "game_version": "v0.103.2",
    "game_version_source": "cli",
}
```

Pass `**VERSION_FIELDS` to every direct `run_eval_verbose` call, then add:

```python
def test_run_eval_rejects_missing_version_before_environment_creation(monkeypatch):
    import agent.eval_rl as eval_rl
    created = False

    class ForbiddenEnv:
        def __init__(self, **kwargs):
            nonlocal created
            created = True

    model = SimpleNamespace(observation_space=SimpleNamespace(shape=(161,)))
    monkeypatch.setattr(eval_rl, "CombatEnv", ForbiddenEnv)

    with pytest.raises(ValueError, match="game version"):
        run_eval_verbose(model, "Ironclad", n_games=1)
    assert created is False


def test_eval_rows_and_context_repeat_all_comparison_axes(monkeypatch, tmp_path):
    import agent.eval_rl as eval_rl
    captured = []

    class FakeEnv:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self._current_state = {
                "decision": "combat_play",
                "context": {"floor": 8},
                "player": {"hp": 0, "max_hp": 80},
            }

        def reset(self):
            return [0.0] * 161, {}

        def action_masks(self):
            return [True]

        def step(self, action):
            return [0.0] * 161, 0.0, True, False, {"floor": 8}

        def close(self):
            pass

    class FakeModel:
        observation_space = SimpleNamespace(shape=(161,))

        def predict(self, obs, **kwargs):
            return 0, None

    monkeypatch.setattr(eval_rl, "CombatEnv", FakeEnv)
    monkeypatch.setattr(eval_rl, "ActionMasker", lambda env, mask_fn: env)
    monkeypatch.setattr(eval_rl.signal, "signal", lambda *args: None)
    monkeypatch.setattr(eval_rl.signal, "alarm", lambda *args: None)
    stats = run_eval_verbose(
        FakeModel(),
        "Ironclad",
        n_games=1,
        fixed_seeds=True,
        checkpoint_name="checkpoints/model.zip",
        ascension=10,
        scenario="full_run",
        results_log_path=str(tmp_path / "eval.jsonl"),
        **VERSION_FIELDS,
    )
    row = stats["attempt_results"][0]
    assert row["game_version"] == "v0.103.2"
    assert row["game_version_source"] == "cli"
    assert row["ascension"] == 10
    assert captured[0]["run_context"] == {
        "run_id": row["run_id"],
        "checkpoint": "model.zip",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "game_version": "v0.103.2",
        "game_version_source": "cli",
    }
    assert captured[0]["ascension"] == 10
```

Also assert `_build_parser().parse_args(["model.zip", "--ascension", "10"]).ascension == 10` and that `--game-version` defaults to `None`; environment precedence belongs only to `resolve_game_version`.

- [ ] **Step 2: Write failing training propagation tests**

```python
def test_training_env_factory_preserves_one_resolved_context(monkeypatch):
    import agent.train as train
    captured = []

    class FakeEnv:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(train, "CombatEnv", FakeEnv)
    monkeypatch.setattr(train, "ActionMasker", lambda env, mask_fn: env)
    context = {
        "checkpoint": "model_14000k.zip",
        "evaluation_mode": "training",
        "scenario": "full_run",
        "game_version": "v0.103.2",
        "game_version_source": "environment",
    }

    train.make_env("Ironclad", 10, worker_id=2, run_context=context)()

    assert captured[0]["ascension"] == 10
    assert captured[0]["run_context"] == context


def test_periodic_eval_tags_checkpoint_and_fixed_seed_context(monkeypatch):
    import agent.train as train
    captured = []

    class FakeEnv:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def reset(self):
            return [0.0], {}

        def action_masks(self):
            return [True]

        def step(self, action):
            return [0.0], 0.0, True, False, {"floor": 8}

        def close(self):
            pass

    class FakeModel:
        def predict(self, obs, **kwargs):
            return 0, None

    monkeypatch.setattr(train, "CombatEnv", FakeEnv)
    monkeypatch.setattr(train, "ActionMasker", lambda env, mask_fn: env)
    stats = train.run_eval(
        FakeModel(),
        "Ironclad",
        n_games=1,
        ascension=10,
        checkpoint="ppo_ironclad_14000k.zip",
        game_version="v0.103.2",
        game_version_source="cli",
    )
    context = captured[0]["run_context"]
    assert context["checkpoint"] == "ppo_ironclad_14000k.zip"
    assert context["evaluation_mode"] == "fixed"
    assert context["scenario"] == "full_run"
    assert context["game_version"] == "v0.103.2"
    assert context["game_version_source"] == "cli"
    assert captured[0]["ascension"] == 10
    assert stats["max_floor"] == 8
```

- [ ] **Step 3: Write failing canonical provenance tests**

In `test_models.py` add:

```python
def test_run_metadata_serializes_game_version_source():
    record = RunRecord(
        run_id="versioned",
        source_id="eval",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(
            game_version="v0.103.2",
            game_version_source="environment",
        ),
    )
    assert record.to_dict()["metadata"]["game_version_source"] == "environment"
```

In `test_adapters.py` add:

```python
def test_eval_result_preserves_game_version_source(tmp_path: Path) -> None:
    path = tmp_path / "eval.jsonl"
    path.write_text(
        json.dumps({
            "event": "eval_result",
            "run_id": "versioned",
            "status": "dead",
            "game_version": "v0.103.2",
            "game_version_source": "cli",
        }) + "\n",
        encoding="utf-8",
    )
    run = adapt_path(path).runs[0]
    assert run.metadata.game_version == "v0.103.2"
    assert run.metadata.game_version_source == "cli"
```

- [ ] **Step 4: Run producer tests and confirm RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_eval_rl.py tests/agent/test_train.py tests/agent/run_workbench/test_models.py tests/agent/run_workbench/test_adapters.py -q
```

Expected: failures identify missing version-source/ascension fields and unchanged function signatures.

- [ ] **Step 5: Implement evaluation propagation**

Change `run_eval_verbose` to accept `ascension: int = 0` and `game_version_source: str | None = None`. At the top, validate with:

```python
if type(game_version) is not str or not game_version.strip():
    raise ValueError("game version and its source are required before evaluation")
game_version = game_version.strip()
if game_version_source not in {"cli", "environment"}:
    raise ValueError("game version and its source are required before evaluation")
ascension = validate_ascension(ascension)
```

Add `game_version_source` to `run_context`; pass `ascension=ascension` to `CombatEnv`; and add both `game_version_source` and `ascension` to every `attempt_row`. Change parser defaults to:

```python
p.add_argument("--game-version", default=None)
p.add_argument("--ascension", type=int, default=0)
```

In `main`, call `resolve_game_version(args.game_version)` immediately after argument parsing, before checkpoint/model loading. Pass `.value`, `.source`, and validated ascension into `run_eval_verbose`. Replace the old environment-default parser test with:

```python
def test_eval_cli_keeps_game_version_unresolved_until_launch(monkeypatch):
    monkeypatch.setenv("STS2_GAME_VERSION", "v0.103.2")
    args = _build_parser().parse_args(["checkpoints/model.zip"])
    assert args.game_version is None
```

- [ ] **Step 6: Implement training propagation without changing PPO behavior**

Add `--game-version` with default `None`, resolve it before `_make_vec_env`, and construct:

```python
base_run_context = {
    "checkpoint": os.path.basename(args.checkpoint) if args.checkpoint else None,
    "evaluation_mode": "training",
    "scenario": "full_run",
    **resolved_version.to_fields(),
}
```

Add `run_context` to `make_env` and `_make_vec_env`; copy it for each worker and set scenario to `native_save` only for workers that receive a snapshot. Pass the same base context on every environment recreation.

Extend `run_eval` with `ascension`, `checkpoint`, `game_version`, and `game_version_source`; build a per-game context with `evaluation_mode="fixed"`, `scenario="full_run"`, and the current saved checkpoint. Pass the just-written `ckpt` to periodic evaluation and the final saved checkpoint to final evaluation. Do not change model construction, optimizer parameters, rewards, or curriculum scheduling.

- [ ] **Step 7: Preserve version-source provenance in canonical records**

Add `game_version_source: str | None = None` immediately after `game_version` in `RunMetadata` and `_CompactRun`. Include it in `RunMetadata` construction in `_metadata_from_records`, `_CompactRun.to_record`, `_item_metadata`, and compact scanner assignment using the exact `game_version_source` key. Do not infer a source for historical rows that lack it. Add it to the cohort `filters` payload in Task 5, but do not make provenance source a comparison axis.

- [ ] **Step 8: Run producer and adjacent adapter tests**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_run_metadata.py tests/agent/test_combat_env.py tests/agent/test_eval_rl.py tests/agent/test_train.py tests/agent/run_workbench/test_adapters.py tests/agent/run_workbench/test_catalog.py -q
```

Expected: all tests pass and newly written outcome/eval rows adapt with version and ascension intact.

- [ ] **Step 9: Commit producer propagation**

```bash
git add agent/eval_rl.py agent/train.py agent/run_workbench/models.py agent/run_workbench/adapters.py agent/run_workbench/catalog.py tests/agent/test_eval_rl.py tests/agent/test_train.py tests/agent/run_workbench/test_models.py tests/agent/run_workbench/test_adapters.py
git commit -m "feat: require versioned training results"
```

### Task 4: Derive a Bounded Comparison Readiness Signature

**Files:**
- Modify: `agent/run_workbench/metrics.py:13-23, 150-220, 515-583`
- Modify: `tests/agent/run_workbench/test_metrics.py:1-45, 420-end`

- [ ] **Step 1: Write failing readiness tests**

Import `describe_comparison_readiness`, then add:

```python
def test_complete_comparison_metadata_has_stable_signature():
    forward = describe_comparison_readiness([
        _run("a", seed="seed-a"),
        _run("b", seed="seed-b"),
    ])
    reverse = describe_comparison_readiness([
        _run("b", seed="seed-b"),
        _run("a", seed="seed-a"),
    ])
    assert forward.ready is True
    assert forward.seed_complete is True
    assert forward.seed_count == 2
    assert forward.comparison_signature == reverse.comparison_signature
    assert forward.missing_axes == ()
    assert forward.mixed_axes == ()
    assert forward.invalid_axes == ()


@pytest.mark.parametrize(
    "records, missing, mixed, invalid",
    [
        ([_run("x", version=None)], ("game_version",), (), ()),
        ([_run("x", seed=None)], ("seed",), (), ()),
        ([_run("x"), _run("y", version="other", seed="seed-2")], (), ("game_version",), ()),
        ([_run("x", ascension=True)], (), (), ("ascension",)),
        ([_run("x", status=RunStatus.IN_PROGRESS)], (), (), ("valid_results",)),
    ],
)
def test_incomplete_or_invalid_metadata_has_no_signature(records, missing, mixed, invalid):
    readiness = describe_comparison_readiness(records)
    assert readiness.ready is False
    assert readiness.comparison_signature is None
    assert readiness.missing_axes == missing
    assert readiness.mixed_axes == mixed
    assert readiness.invalid_axes == invalid
    json.dumps(readiness.to_dict(), allow_nan=False)
```

- [ ] **Step 2: Run metrics tests and confirm RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench/test_metrics.py -q
```

Expected: import fails because `describe_comparison_readiness` does not exist.

- [ ] **Step 3: Implement the immutable readiness contract**

Add:

```python
@dataclass(frozen=True)
class ComparisonReadiness:
    ready: bool
    missing_axes: tuple[str, ...]
    mixed_axes: tuple[str, ...]
    invalid_axes: tuple[str, ...]
    seed_count: int
    seed_complete: bool
    comparison_signature: str | None

    def to_dict(self) -> dict[str, Any]:
        return _to_json_value(self)
```

Import `json`, then implement the single-pass descriptor:

```python
def describe_comparison_readiness(
    records: Iterable[RunRecord],
) -> ComparisonReadiness:
    accumulator = _ComparisonAccumulator()
    for record in records:
        accumulator.observe(record)

    missing: list[str] = []
    mixed: list[str] = []
    invalid: list[str] = []
    resolved: dict[str, str | int] = {}
    axis_order = ("character", "game_version", "evaluation_mode", "scenario")
    for axis in axis_order:
        details = accumulator.axes[axis].details()
        if details.missing:
            missing.append(axis)
        if len(details.values) > 1 or (details.missing and bool(details.values)):
            mixed.append(axis)
        if details.invalid_types or details.overflow:
            invalid.append(axis)
        if (
            len(details.values) == 1
            and not details.missing
            and not details.invalid_types
            and not details.overflow
        ):
            resolved[axis] = next(iter(details.values))

    ascension = accumulator.ascension_details()
    if ascension.missing:
        missing.append("ascension")
    if len(ascension.values) > 1 or (ascension.missing and bool(ascension.values)):
        mixed.append("ascension")
    if ascension.invalid:
        invalid.append("ascension")
    if len(ascension.values) == 1 and not ascension.missing and not ascension.invalid:
        resolved["ascension"] = next(iter(ascension.values))

    seed_details = accumulator.seeds.details()
    if seed_details.missing:
        missing.append("seed")
    if seed_details.invalid_types or seed_details.overflow:
        invalid.append("seed")
    seed_complete = bool(seed_details.values) and not (
        seed_details.missing
        or seed_details.invalid_types
        or seed_details.overflow
    )
    if accumulator.valid_n == 0:
        invalid.append("valid_results")

    ready = (
        accumulator.valid_n > 0
        and not missing
        and not mixed
        and not invalid
        and seed_complete
        and set(resolved) == {*axis_order, "ascension"}
    )
    signature = None
    if ready:
        signature_payload = json.dumps(
            {
                **resolved,
                "seeds": sorted(seed_details.values),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = sha256(signature_payload).hexdigest()

    return ComparisonReadiness(
        ready=ready,
        missing_axes=tuple(missing),
        mixed_axes=tuple(mixed),
        invalid_axes=tuple(invalid),
        seed_count=len(seed_details.values),
        seed_complete=seed_complete,
        comparison_signature=signature,
    )
```

The seed list exists only transiently while hashing and is never serialized.

- [ ] **Step 4: Run metrics and comparison regressions**

Run the Step 2 command. Expected: all readiness and existing `compare_cohorts` tests pass.

- [ ] **Step 5: Commit readiness derivation**

```bash
git add agent/run_workbench/metrics.py tests/agent/run_workbench/test_metrics.py
git commit -m "feat: describe cohort comparison readiness"
```

### Task 5: Assign the Newest Strictly Compatible Baseline Server-Side

**Files:**
- Modify: `agent/run_workbench/catalog.py:17-25, 384-419, 730-846`
- Modify: `tests/agent/run_workbench/test_catalog.py:450-590`
- Modify: `tests/agent/run_workbench/test_http_api.py:135-150`

- [ ] **Step 1: Write the failing catalog selection test**

Create four checkpoint cohorts with exact timestamps: current and compatible baseline share two seeds and all axes; a newer candidate differs by version; another has one missing seed.

```python
def test_catalog_assigns_only_newest_strictly_compatible_baseline(tmp_path):
    rows = []
    for checkpoint, version, seeds, timestamp in [
        ("current", "v1", ("a", "b"), 40),
        ("compatible-old", "v1", ("a", "b"), 20),
        ("wrong-version", "v2", ("a", "b"), 30),
        ("missing-seed", "v1", ("a", None), 35),
    ]:
        for index, seed in enumerate(seeds):
            rows.append({
                "event": "eval_result",
                "run_id": f"{checkpoint}-{index}",
                "status": "dead",
                "max_global_floor": 8,
                "character": "Ironclad",
                "game_version": version,
                "game_version_source": "cli",
                "checkpoint": checkpoint,
                "evaluation_mode": "fixed",
                "scenario": "full_run",
                "ascension": 0,
                "seed": seed,
                "ts": timestamp + index,
            })
    _write_jsonl(tmp_path / "eval.jsonl", rows)
    cohorts = RunCatalog([tmp_path], replay_parser=_replay_parser).list_cohorts()
    by_checkpoint = {item["filters"]["checkpoint"]: item for item in cohorts}

    current = by_checkpoint["current"]
    compatible = by_checkpoint["compatible-old"]
    assert current["filters"]["game_version_source"] == "cli"
    assert current["comparison_readiness"]["ready"] is True
    assert current["default_baseline_cohort_id"] == compatible["cohort_id"]
    assert compatible["default_baseline_cohort_id"] == current["cohort_id"]
    assert by_checkpoint["wrong-version"]["default_baseline_cohort_id"] is None
    assert by_checkpoint["missing-seed"]["comparison_readiness"]["seed_complete"] is False
    assert by_checkpoint["missing-seed"]["default_baseline_cohort_id"] is None
```

- [ ] **Step 2: Add a failing HTTP payload assertion**

```python
def test_http_advertised_baseline_is_strictly_comparable(tmp_path: Path):
    _write_jsonl(
        tmp_path / "eval.jsonl",
        [
            {
                "event": "eval_result",
                "run_id": checkpoint,
                "status": "dead",
                "max_global_floor": floor,
                "character": "Ironclad",
                "game_version": "v0.103.2",
                "game_version_source": "cli",
                "checkpoint": checkpoint,
                "evaluation_mode": "fixed",
                "scenario": "full_run",
                "ascension": 0,
                "seed": "eval_fixed_0",
                "ts": timestamp,
            }
            for checkpoint, floor, timestamp in (
                ("current", 10, 20),
                ("baseline", 8, 10),
            )
        ],
    )
    catalog = RunCatalog([tmp_path], replay_parser=_replay_parser)
    with _server(catalog) as base:
        status, payload = _request(base, "/api/cohorts")
        assert status == 200
        current = payload["cohorts"][0]
        baseline_id = current["default_baseline_cohort_id"]
        assert current["comparison_readiness"]["ready"] is True
        assert baseline_id
        status, metrics = _request(
            base,
            f"/api/metrics?current={current['cohort_id']}&baseline={baseline_id}",
        )
        assert status == 200
        assert metrics["comparison"]["comparable"] is True
        assert metrics["comparison"]["paired"] is True
```

- [ ] **Step 3: Run catalog/API tests and confirm RED**

Run:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py -q
```

Expected: descriptor keys are missing.

- [ ] **Step 4: Attach readiness during the existing cohort pass**

Import `describe_comparison_readiness`. Do not add provenance source to the cohort identity: two copies of the same checkpoint must not become artificial baseline cohorts merely because one came from CLI and one from the environment. Derive a `version_sources` set from `ordered` and expose `filters["game_version_source"]` only when that set contains exactly one non-empty string; otherwise expose `None`. After `ordered` is created, derive one readiness value from `_iter_cohort_records(ordered)` and add:

```python
"comparison_readiness": readiness.to_dict(),
"default_baseline_cohort_id": None,
```

Replace the final descriptor assignment with a named latest-first list, then group ready descriptors by non-null `comparison_signature`. For each descriptor, choose the first group member whose `cohort_id` differs:

```python
sorted_descriptors = sorted(
    descriptors,
    key=lambda item: (
        item["latest_at"] is None,
        -(item["latest_at"] or 0),
        item["label"],
        item["cohort_id"],
    ),
)
groups: dict[str, list[dict[str, Any]]] = {}
for descriptor in sorted_descriptors:
    signature = descriptor["comparison_readiness"]["comparison_signature"]
    if signature is not None:
        groups.setdefault(signature, []).append(descriptor)
for group in groups.values():
    for descriptor in group:
        descriptor["default_baseline_cohort_id"] = next(
            (
                candidate["cohort_id"]
                for candidate in group
                if candidate["cohort_id"] != descriptor["cohort_id"]
            ),
            None,
        )
self._cohort_descriptors = sorted_descriptors
```

Do not call `compare_cohorts` while listing cohorts. `get_metrics` remains the final validation boundary and continues returning no deltas for incompatible manual pairs.

- [ ] **Step 5: Run catalog, HTTP, and bounds regressions**

Run the Step 3 command. Expected: all tests pass, including compact 511/513-record parity and source bounds.

- [ ] **Step 6: Commit the server-owned default**

```bash
git add agent/run_workbench/catalog.py tests/agent/run_workbench/test_catalog.py tests/agent/run_workbench/test_http_api.py
git commit -m "feat: select compatible cohort baselines"
```

### Task 6: Render Neutral Historical States and Consume the Server Default

**Files:**
- Modify: `agent/run_workbench/static/app.js:157-228, 494-516, 1048-1075, 1135-1175`
- Modify: `agent/run_workbench/static/index.html:63-75`
- Modify: `tests/agent/run_workbench/test_static_contract.py:469-510, 570-580`

- [ ] **Step 1: Replace the old static baseline contract with failing server-owned assertions**

```python
def test_baseline_default_uses_server_descriptor_without_client_axis_logic():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    selector = _javascript_section(
        script, "function defaultBaselineCohortId", "function resetMetrics"
    )
    assert "default_baseline_cohort_id" in selector
    assert "comparison_readiness" in selector
    assert "nearestDistinctCohortId" not in script
    assert ".filters" not in selector
    assert "currentIndex - 1" not in selector
    assert "currentIndex + 1" not in selector
```

Add a Node probe for the pure helper:

```python
def test_default_baseline_helper_rejects_missing_or_filtered_server_choice():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    helper = _javascript_section(
        script, "function defaultBaselineCohortId", "function updateCohortHelp"
    )
    payload = _run_node_json(f"""
      {helper}
      const ready = {{cohort_id: 'c', default_baseline_cohort_id: 'b'}};
      const missing = {{cohort_id: 'x', default_baseline_cohort_id: null}};
      console.log(JSON.stringify([
        defaultBaselineCohortId(ready, [ready, {{cohort_id: 'b'}}]),
        defaultBaselineCohortId(ready, [ready]),
        defaultBaselineCohortId(missing, [missing]),
      ]));
    """)
    assert payload == ["b", "", ""]
```

- [ ] **Step 2: Add failing messaging assertions**

```python
def test_comparison_help_is_neutral_and_explains_version_provenance():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "元数据不完整，仅展示本批次" in script
    assert "当前批次可查看，但暂无可直接比较的基线" in script
    assert "当前与基线不可直接比较" in script
    assert "missing_axes" in script
    assert "mixed_axes" in script
    assert "invalid_axes" in script
    assert "版本来源：${sourceLabel}" in script
    assert "命令行" in script
    assert "环境变量" in script
```

- [ ] **Step 3: Run static tests and confirm RED**

Run:

```bash
/usr/bin/env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin:/usr/sbin:/sbin /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py -q
```

Expected: old nearest-neighbor code violates the new assertions.

- [ ] **Step 4: Implement pure server-default and help-text helpers**

Replace `nearestDistinctCohortId` with:

```javascript
function defaultBaselineCohortId(current, candidates) {
  if (!current || typeof current.default_baseline_cohort_id !== 'string') return '';
  const baselineId = current.default_baseline_cohort_id.trim();
  return candidates.some((candidate) => candidate.cohort_id === baselineId) ? baselineId : '';
}

function comparisonAxisLabel(axis) {
  return ({
    character: '角色',
    game_version: '游戏版本',
    evaluation_mode: '评测模式',
    scenario: '场景',
    ascension: '进阶',
    seed: '种子',
    valid_results: '有效结果',
  })[axis] || axis;
}

function updateCohortHelp(current, baselineId) {
  const readiness = current && current.comparison_readiness;
  const labels = (key) => readiness && Array.isArray(readiness[key])
    ? readiness[key].map(comparisonAxisLabel)
    : [];
  const missing = labels('missing_axes');
  const mixed = labels('mixed_axes');
  const invalid = labels('invalid_axes');
  const issues = [
    missing.length ? `缺少${missing.join('、')}` : '',
    mixed.length ? `混合${mixed.join('、')}` : '',
    invalid.length ? `无效${invalid.join('、')}` : '',
  ].filter(Boolean);
  const source = current && current.filters && current.filters.game_version_source;
  const sourceLabel = source === 'cli'
    ? '命令行'
    : source === 'environment' ? '环境变量' : '记录未提供';
  const currentHelp = byId('currentHelp');
  currentHelp.textContent = `${currentHelp.textContent}；版本来源：${sourceLabel}`;
  if (!readiness || readiness.ready !== true) {
    const detail = issues.length ? `：${issues.join('；')}` : '';
    byId('baselineHelp').textContent = `元数据不完整，仅展示本批次${detail}`;
  } else if (!baselineId) {
    byId('baselineHelp').textContent = '当前批次可查看，但暂无可直接比较的基线';
  } else {
    byId('baselineHelp').textContent = '已采用服务端验证的兼容基线；手动选择后仍会再次校验';
  }
}
```

Change the function signature to `updateCohortOptions({ chooseDefaults = false, currentChanged = false } = {})`. Retain the newest server-ordered current cohort. Select the server default when `chooseDefaults`, `currentChanged`, or `current !== previousCurrent`; otherwise preserve a valid manual baseline, including blank. If a nonblank manual baseline disappears from the filtered candidates, clear it instead of choosing an arbitrary neighbor. Call `updateCohortHelp(selected, baseline)` after setting both selectors. Change the listener to:

```javascript
byId('currentCohort').addEventListener('change', () => {
  updateCohortOptions({ currentChanged: true });
  refreshMetrics();
});
```

- [ ] **Step 5: Render a neutral no-baseline banner**

When `renderComparison` receives `null`, inspect `currentCohortDescriptor()`:

```javascript
const readiness = current && current.comparison_readiness;
if (readiness && readiness.ready === false) {
  title.textContent = '元数据不完整，仅展示本批次';
  body.append(element('p', { text: '历史记录仍可查看，但不会用于训练提升比较。' }));
} else {
  title.textContent = '未选择基线';
  body.append(element('p', { text: '当前批次可查看，但暂无可直接比较的基线。' }));
}
```

Leave the non-null incompatible branch unchanged so manual diagnosis still shows exact server reasons. Update the static HTML help default to the same neutral wording; do not add a client-side comparison implementation.

- [ ] **Step 6: Run static syntax and behavior tests**

Run:

```bash
/usr/bin/env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin:/usr/sbin:/sbin /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/run_workbench/test_static_contract.py -q
/Users/bytedance/.nvm/versions/node/v22.19.0/bin/node --check agent/run_workbench/static/app.js
```

Expected: pytest passes and Node exits 0.

- [ ] **Step 7: Commit the dashboard behavior**

```bash
git add agent/run_workbench/static/app.js agent/run_workbench/static/index.html tests/agent/run_workbench/test_static_contract.py
git commit -m "fix: show only compatible training baselines"
```

### Task 7: End-to-End Verification on the Real Local Catalog

**Files:**
- No production changes expected.

- [ ] **Step 1: Run the focused producer and workbench suite**

```bash
/usr/bin/env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin:/usr/sbin:/sbin /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent/test_run_metadata.py tests/agent/test_combat_env.py tests/agent/test_eval_rl.py tests/agent/test_train.py tests/agent/run_workbench -q
```

Expected: all tests pass. If loopback tests fail only with sandbox `PermissionError`, rerun the exact command with localhost permission; do not treat permission failures as product failures.

- [ ] **Step 2: Run the complete agent suite**

```bash
/usr/bin/env PATH=/Users/bytedance/.nvm/versions/node/v22.19.0/bin:/usr/bin:/bin:/usr/sbin:/sbin /Users/bytedance/mygit/sts2-cli/.venv/bin/python -m pytest tests/agent -q
```

Expected: 100% collection completes with exit code 0.

- [ ] **Step 3: Verify the real catalog API**

Start a temporary viewer on an unused port with the real roots:

```bash
/Users/bytedance/mygit/sts2-cli/.venv/bin/python -m agent.run_progress_viewer --host 127.0.0.1 --port 61122 --source-root /Users/bytedance/mygit/sts2-cli/logs --source-root /Users/bytedance/mygit/sts2-cli/data
```

Request `/api/cohorts` and verify:

- the newest cohort still appears and has its existing `run_count`/`latest_at`;
- metadata-empty historical cohorts have `ready=false` and `default_baseline_cohort_id=null`;
- no descriptor contains a raw seed list;
- any advertised default baseline is accepted by `/api/metrics` as `comparable=true` and `paired=true`.

- [ ] **Step 4: Perform browser acceptance**

Open `http://127.0.0.1:61122/` and verify the rendered page:

1. Current single-batch average/median/maximum remain visible.
2. Historical incomplete current cohort shows `元数据不完整，仅展示本批次`.
3. Baseline defaults to `不比较基线` when no strict match exists.
4. The previous ten-line mismatch wall is absent on initial load.
5. Manually selecting an incompatible cohort still shows precise mismatch reasons.
6. A synthetic complete compatible fixture auto-selects its newest compatible baseline and renders server-owned deltas.
7. Browser console and page-error streams remain empty.

- [ ] **Step 5: Verify repository scope and cleanliness**

```bash
git diff --check
git status --short
git log --oneline 44ed2de..HEAD
```

Expected: no unstaged files, no generated logs/assets, and only the planned narrow commits after the approved design commit.
