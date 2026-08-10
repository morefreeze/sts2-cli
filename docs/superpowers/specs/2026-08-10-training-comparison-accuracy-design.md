# Accurate Training Cohort Comparison Design

**Date:** 2026-08-10
**Status:** Approved in conversation; implementation planning follows after document review.

## Problem

The training dashboard currently selects the newest cohort as current and the
nearest distinct cohort as baseline without first proving that the pair is
comparable. The local catalog is dominated by historical `deck_history` and
evaluation files that do not record character, game version, evaluation mode,
scenario, ascension, or complete seeds. The resulting default page displays a
long list of compatibility errors even though both cohorts can still have
useful single-batch progression statistics.

This is primarily a data-provenance problem, not a presentation problem. A
comparison must not infer missing metadata from filenames, directory names,
timestamps, or nearby records. Game version is especially important because a
build can change cards, encounters, map generation, and policy behavior. New
training and evaluation logs must therefore record it before any game starts.

## Goals

1. Preserve useful single-batch statistics for historical cohorts even when
   they cannot be compared with a baseline.
2. Select a default baseline only when the server proves that all comparison
   axes and the seed set are compatible.
3. Make `game_version` first-class, mandatory metadata for every new training
   or evaluation game and each terminal result.
4. Add ascension to the same producer contract so new logs satisfy the existing
   strict comparison rules.
5. Explain missing or incompatible metadata concisely without presenting an
   automatically chosen invalid baseline.
6. Keep historical records browseable while refusing to manufacture provenance
   for them.

## Non-goals

- Guessing historical game versions from filenames, modification times, or the
  currently installed game.
- Rewriting old logs in place.
- Weakening the existing strict `compare_cohorts` validation.
- Comparing different game versions by default.
- Changing PPO training, rewards, policy behavior, or map reconstruction.
- Replacing detailed run diagnostics or the per-run map view.

## Product Behavior

### Current cohort

The dashboard continues to choose the newest available cohort as the current
cohort. It renders that cohort's own average, median, maximum progression,
conversion funnel, sample size, and failure counts whenever those values are
available.

If comparison metadata is incomplete, the page labels the cohort as
“元数据不完整，仅展示本批次” and lists the missing axes in a compact detail
view. Missing metadata does not turn known progression into zero and does not
hide the cohort.

### Default baseline

The dashboard auto-selects a baseline only when it is a different cohort and is
strictly compatible with the current cohort on all of these axes:

- character;
- game version;
- evaluation mode;
- scenario;
- ascension;
- complete seed set.

The seed set must contain no missing values and must match exactly. Ordering is
irrelevant; duplicates do not create extra matches. The selected baseline is
the newest compatible cohort, with stable cohort identity as the final
tie-breaker.

When no compatible baseline exists, the selector remains on “不比较基线” and
the page shows “当前批次可查看，但暂无可直接比较的基线.” It does not show a wall
of mismatch errors for an automatically selected pair.

Users may manually choose an incompatible baseline for diagnosis. In that case
the server's strict comparison response remains authoritative and the UI shows
the precise mismatch reasons. No comparison delta or improvement claim is
rendered.

### Game version priority

Every new training or evaluation launch must provide a non-empty game version
through this precedence:

1. explicit `--game-version` command-line argument;
2. `STS2_GAME_VERSION` environment variable.

The resolved value is trimmed once, remains case-sensitive, and is otherwise
stored verbatim. Each persisted boundary also records
`game_version_source="cli"` or `"environment"`, so the workbench can explain
how the version was determined. It does not claim the value was auto-detected
from the installed game.

If neither yields a non-empty value, the launcher fails before creating the
first game environment and tells the operator how to provide it. This applies
to standalone evaluation, PPO training, training's periodic evaluation, and
re-created worker environments. None may silently write another unversioned
run.

The normalized value is written at both durability boundaries:

- a persisted `run_start` record before gameplay begins;
- every terminal training `outcome` and evaluation `eval_result` row.

Recording it on every result intentionally duplicates a small value so a
truncated, split, or independently copied result stream retains its version.
The value is also propagated into canonical `RunMetadata`, compact catalog
metadata, cohort filters, and comparison payloads without reinterpretation.

Both launchers resolve this value once and pass it through the run context;
workers do not independently inspect their environment and cannot drift within
a batch. Training records use `evaluation_mode="training"`; periodic and
standalone evaluation retain `fixed` or `random`. Scenario, character,
checkpoint, ascension, and seed are recorded at the same two durability
boundaries. Standalone evaluation adds an explicit ascension argument with a
validated supported range; training continues to use its validated ascension
argument.

Intermediate milestone and card-choice rows need not repeat every comparison
field because they join to their run through `run_id`. The persisted start and
terminal rows must each be independently sufficient to recover the run's game
version. A start without a terminal outcome is partial evidence and never
becomes a successful training result.

## Server-Owned Comparison Readiness

The frontend must not duplicate cohort compatibility logic. The cohort service
derives a bounded comparison descriptor while it already aggregates canonical
records:

```text
comparison_readiness
  ready: bool
  missing_axes: string[]
  mixed_axes: string[]
  invalid_axes: string[]
  seed_count: int
  seed_complete: bool
  comparison_signature: opaque string | null
```

`comparison_signature` is present only when character, game version,
evaluation mode, scenario, ascension, and the complete normalized seed set are
known. It is an opaque deterministic digest or bounded key; the browser never
parses it.

Each cohort descriptor also supplies `default_baseline_cohort_id`, or `null`
when that cohort has no compatible baseline. The service groups ready cohorts
by signature and chooses the newest other member for every descriptor, avoiding
an all-pairs comparison across a large catalog. The browser reads the field from
the selected current cohort. A full `compare_cohorts` call still validates the
final selected pair before any delta is returned. This keeps one server-owned
source of truth and prevents a descriptor bug from producing a false claim.

The readiness descriptor is bounded: it stores counts, validation labels, and
a digest rather than the full seed list. Existing catalog size and payload
limits continue to apply.

## Data Flow

```text
training/eval launcher
  -> validate version and ascension before first environment
  -> persist run_start with all comparison axes
  -> write each outcome/eval_result with the same axes and seed

catalog/adapters
  -> preserve exact validated metadata
  -> build canonical RunRecord cohorts

cohort service
  -> compute single-batch aggregates
  -> compute comparison readiness/signature
  -> choose newest strictly compatible baseline, if any

frontend
  -> always render current single-batch aggregates
  -> render comparison only after server validation
  -> otherwise show a neutral no-baseline or incomplete-metadata state
```

## Historical Data

Historical cohorts with missing metadata remain visible and selectable. They
receive `ready=false`, their actual missing axes, and no comparison signature.
They can never become an automatic baseline.

No migration guesses values. If an operator possesses authoritative external
metadata, a future explicit import/sidecar workflow may attach it, but that is
outside this design. Merely running the viewer under a newer installed game
must never relabel an old run.

## Error Handling

- Missing or blank new `game_version`: fail training/evaluation before the first
  environment is created.
- Invalid ascension: fail training/evaluation before the first game and report
  the supported range.
- Mixed metadata inside one cohort: retain the cohort for inspection, mark the
  relevant axis mixed, and exclude it from automatic comparison.
- Missing seed on any run: set `seed_complete=false`; do not compare the cohort.
- No compatible baseline: return `default_baseline_cohort_id=null`; this is a
  normal state, not an API error.
- Manually selected incompatible pair: return the existing structured reasons
  and no deltas.
- Malformed unrelated records: preserve existing per-source isolation so they
  cannot make valid cohorts disappear.

## Verification

Implementation follows test-driven development and covers:

1. Training and evaluation reject missing and blank game versions before
   creating the game environment.
2. Explicit `--game-version` takes precedence over `STS2_GAME_VERSION`.
3. Persisted `run_start`, training `outcome`, and every `eval_result` contain the
   exact same version, version source, ascension, character, evaluation mode,
   scenario, checkpoint, and relevant seed for their run.
4. Ascension validation accepts supported boundaries and rejects invalid type
   or range before gameplay.
5. Complete metadata produces a stable comparison signature; missing, mixed,
   invalid, or incomplete-seed cohorts do not.
6. The newest strictly compatible cohort is selected as baseline with a stable
   tie-breaker.
7. Version, mode, scenario, character, ascension, or seed mismatch produces no
   automatic baseline.
8. Manual incompatible comparison still returns precise server-owned reasons
   and no improvement delta.
9. Historical incomplete cohorts retain their single-batch statistics and are
   labeled as inspection-only.
10. Empty catalogs, one-cohort catalogs, and catalogs with no compatible pair
    render a neutral no-baseline state.
11. Existing strict comparison, catalog bounds, API, and frontend tests remain
    green.

The normal dashboard never enables the existing low-level cross-version
comparison override. Cross-version data may be inspected side by side, but the
UI does not label the difference as training improvement or regression.

## Acceptance Criteria

- Starting training or evaluation without an explicit or environmental game
  version is impossible.
- A newly written training or evaluation result can be classified by every
  strict comparison axis without consulting its filename.
- Opening the current local catalog no longer auto-selects two metadata-empty
  historical cohorts or displays their mismatch wall.
- The newest cohort still shows its own known progression statistics.
- The UI claims improvement or regression only after the server proves an
  exact compatible pair, including game version and complete seed set.
- Historical data is never silently rewritten, inferred, or presented as
  comparable.

## Implementation Scope

The implementation plan may touch only the training/evaluation metadata
producers, run-context logging boundary, canonical/catalog cohort descriptor,
metrics/cohort API, dashboard selection and messaging, and their focused tests.
PPO learning behavior, detailed run diagnostics, full-map rendering, assets,
and historical file contents remain out of scope.
