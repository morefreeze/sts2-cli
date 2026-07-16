# Floor 7 Evaluation and Training Optimization Implementation Plan

> Implement sequentially with test-driven development. Do not begin the next
> stage until the current stage's targeted tests pass.

**Goal:** Turn the floor-7 diagnosis into safe defaults, reproducible metrics,
targeted snapshot training, and repeatable fine-tuning profiles.

**Design:** Keep the mature heuristic policy as the default, remove duplicate
card-reward decisions, make evaluation explicit and failure-aware, then add a
decaying snapshot curriculum and named training profiles around the existing
`CombatEnv`/`MaskablePPO` pipeline.

---

## Task 1: Default DecisionAdvisor off

**Files:** `agent/combat_env.py`, `tests/agent/test_decision_advisor.py`

1. Add tests proving an unset variable bypasses the advisor and an explicit
   true value enables it.
2. Run the focused test and confirm the default-off assertion fails.
3. Change `_decision_advisor_enabled()` to explicit opt-in parsing.
4. Run the focused tests and commit.

## Task 2: Unify card-reward action and logging

**Files:** `agent/decision_advisor.py`, `agent/eval_rl.py`,
`tests/agent/test_decision_advisor.py`, `tests/agent/test_eval_rl.py`

1. Add tests proving advisor-enabled card rewards fall through to the shared
   `greedy_action` path and verbose output reports the action actually returned.
2. Confirm the tests fail against the duplicated implementation.
3. Remove card-reward ownership from `DecisionAdvisor` and make verbose logging
   resolve the chosen card from the returned command.
4. Run both focused test modules and commit.

## Task 3: Make evaluation explicit and failure-aware

**Files:** `agent/eval_rl.py`, `eval_and_report.py`,
`tests/agent/test_eval_rl.py`, `tests/agent/test_eval_and_report.py`

1. Add pure unit tests for result classification, retry accounting, valid-only
   summary aggregation, fixed-seed defaults, and explicit checkpoint parsing.
2. Confirm failures before implementation.
3. Introduce a small result record/aggregation layer and retry invalid attempts
   with the same seed.
4. Require an explicit checkpoint in both entry points; make random seeds an
   explicit flag and print the full result contract.
5. Run focused tests and commit.

## Task 4: Capture floor-6/floor-7 snapshots

**Files:** `agent/eval_rl.py`, `tests/agent/test_eval_rl.py`

1. Add tests for the `midact-elite` preset and combat snapshot metadata.
2. Implement preset resolution to floors 6 and 7 plus a deterministic default
   directory when no explicit directory is supplied.
3. Ensure snapshot records retain the actual checkpoint and seed.
4. Run focused tests and commit.

## Task 5: Add training profiles and snapshot curriculum

**Files:** `agent/train.py`, `tests/agent/test_train.py`

1. Add tests for profile defaults, CLI precedence, required save-pool
   validation, and the 75/50/25/0 snapshot schedule.
2. Implement profile resolution as pure functions before wiring it into `main`.
3. Rebuild vector environments only when the snapshot phase changes and retain
   model/callback progress.
4. Run focused tests and commit.

## Task 6: Exercise the complete workflow

1. Capture floor-6/floor-7 snapshots from an explicit baseline checkpoint and
   fixed seeds.
2. Run a minimal `midact-elite` profile smoke train and verify its checkpoint
   loads.
3. Evaluate baseline and smoke candidate using the same explicit fixed seeds.
4. Run Python tests, C# build, and five-game per-character regression.
5. Review the diff for accidental generated artifacts and report metrics without
   claiming promotion unless the fixed-seed candidate actually improves.
