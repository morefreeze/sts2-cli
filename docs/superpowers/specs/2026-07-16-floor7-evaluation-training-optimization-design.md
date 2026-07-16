# Floor 7 Evaluation and Training Optimization Design

**Date:** 2026-07-16
**Status:** Approved in conversation; implementation follows the ordered stages below.

## Problem

The latest ten-game evaluation was not a trustworthy measure of the strongest
available policy. The report silently selected an older checkpoint from the
top-level `checkpoints/` directory, random seeds made comparisons noisy, and a
runtime crash was counted alongside real deaths. Two genuine floor-7 failures
entered forced elite combats with insufficient HP. On the same ten seeds,
disabling `DecisionAdvisor` improved average floor from 9.8 to 13.2 and Act 1
boss reach from 2/10 to 4/10.

The system therefore needs a safe policy default, one card-reward decision
path, reproducible evaluation, targeted floor-6/floor-7 training data, and
repeatable training profiles.

## Goals

1. Make the strongest verified behavior the default without removing the
   advisor experiment.
2. Ensure the card-reward action shown in verbose output is the action actually
   sent to the game.
3. Compare checkpoints on explicit paths and fixed seeds while separating real
   deaths from infrastructure failures.
4. Capture reusable floor-6/floor-7 combat snapshots and train against them
   without forgetting full-run behavior.
5. Provide named, inspectable profiles for mid-Act elite and Act 1 boss
   fine-tuning.

## Non-goals

- Replacing PPO or changing the observation-space contract.
- Enabling planner, defense override, or advisor experiments by default.
- Treating a short smoke run as evidence that a checkpoint is better. Promotion
  still requires a fixed-seed comparison.

## Ordered Design

### 1. Advisor is explicit opt-in

`STS2_DECISION_ADVISOR` defaults to disabled. Values `1`, `true`, `on`, and
`yes` enable it. This preserves the experiment while matching the ten-seed
result that favored the mature fallback policy.

### 2. Card rewards have one decision path

`greedy_action` remains the owner of card-reward selection and continues to use
the mature `pick_best_card` pipeline, including deck synergy, thresholds, and
Monte Carlo context. `DecisionAdvisor` no longer overrides card rewards.

Verbose room logging asks `greedy_action` for the command, then resolves the
selected card by its public `index` field. It never recomputes a second choice.
The log records `SKIP` when the actual command skips.

### 3. Evaluation is reproducible and failure-aware

Normal evaluation requires an explicit checkpoint path. The reporting wrapper
also requires an explicit checkpoint, so a nested newer model cannot be hidden
by an older top-level file.

Fixed seeds are the default comparison mode. Random evaluation remains an
explicit opt-in. Each requested seed produces a result with one of these
statuses:

- `win`: completed run victory.
- `dead`: legitimate game-over loss.
- `crash`, `timeout`, or `stuck`: invalid infrastructure attempt.

Invalid attempts retry the same seed up to a configurable limit. Exhausted
invalid seeds are reported separately and excluded from average floor, win
rate, and combat-win averages. The summary exposes requested seeds, valid
games, invalid seeds, total attempts, status counts, and per-game result rows.

### 4. Floor-6/floor-7 snapshot curriculum

Evaluation supports a `midact-elite` snapshot preset. It captures round-one
combat saves at floors 6 and 7 and writes a JSONL record containing seed,
checkpoint, HP, room type, enemies, deck, and deck-quality information.

The mid-Act profile uses a snapshot pool plus fresh runs. Snapshot exposure
decays over training so early updates concentrate on the failure wall and later
updates recover the full-run distribution:

| Progress | Snapshot env ratio |
|---:|---:|
| 0% | 75% |
| 35% | 50% |
| 70% | 25% |
| 90% | 0% |

The phase is applied at chunk boundaries by rebuilding vector environments;
model and callback progress remain intact.

### 5. Named training profiles

`--profile midact-elite` enables the snapshot curriculum and uses a moderate
entropy coefficient. `--profile act1-boss` keeps a mixed snapshot/full-run
batch, enables the existing HP curriculum, and uses the existing higher boss
entropy recommendation. Explicit CLI flags override profile defaults.

Profiles validate that a save pool was supplied. Startup output prints every
effective profile value so runs are reproducible from logs.

## Verification and Promotion Gates

Each behavior change starts with a failing unit test. The implementation is
accepted only when:

1. Targeted tests pass after each stage.
2. `pytest tests/ -q` and the C# build pass.
3. Five full runs for each character complete without crash/stuck.
4. A short profile smoke run writes a loadable checkpoint.
5. Baseline and candidate are evaluated on the same explicit checkpoint pair
   and fixed seed set; invalid attempts are visible and do not masquerade as
   deaths.

Generated saves, logs, and checkpoints remain outside version control.
