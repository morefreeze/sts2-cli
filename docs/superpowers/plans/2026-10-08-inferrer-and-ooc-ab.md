# Three-arm A/B: restored IL inferrer (BUG-048) and the greedy out-of-combat policy (Phase 3a)

Date: 2026-10-08. Branch `feat/deterministic-planner`. Supersedes Phase 3a Task 7's two-arm A/B
(`2026-09-30-phase3a-ooc-policy-port.md`): that A/B would have measured the out-of-combat policy on a
solver that simulated no attack/block/draw for 486 of 596 card classes (agent/bug.md BUG-048), so
its result would have been obsolete the moment the inferrer was restored.

## Arms (all a1, solver on for all five characters, default 120 s budget)

| Arm | `STS2_SOLVER_INFERRER` | `STS2_OOC_POLICY` | Role |
|---|---|---|---|
| A | off | naive | the harness as it was (pre-BUG-048 solver, placeholder decisions) |
| B | on | naive | isolates the inferrer: B vs A |
| C | on | greedy | isolates the out-of-combat policy on the fixed solver: C vs B |

Seeds `p3x_1..p3x_40` (fresh -- no earlier phase used this prefix) x 5 characters x 3 arms = 600
games. 5 lanes in parallel (one per character, arms A -> B -> C in sequence within a lane),
`STS2_SOLVER_THREADS=3` (5 x 3 = 15 <= 18 cores, same as every earlier A/B). Every arm keeps its game
logs (`--keep-game-logs $AB/games_<arm>`): arm C's logs are the Phase 3b route-model calibration
data.

## Gate before launch

`~/.sts2-train/bug048_gate_*`: the CLAUDE.md regression (5 x 5) under both `naive` and `greedy`
with the inferrer on must be Completed 5/5 for every character, with no HANG.

## Analysis

- `agent/paired_eval.py` per character: A vs B and B vs C on the global floor (valid statuses
  win/dead; `stuck`/`timeout`/`crash` excluded and counted).
- Per arm: A1/A2/A3 boss pass rate (global floor > 17 / > 34 / win) and wins.
- Integrity: `solver=<plans>/<attempts>`, `search.inferrer` is cross-checked by the harness on every
  plan (a mismatched arm fails loudly), `OOC policy` line, fallbacks, stale-plan refusals, HANGs.
- Solve time per arm (the inferrer makes far more cards simulate real effects; watch elapsed_ms and
  NodeLimit).

## Decisions

- Inferrer: stays on (the default) unless B is significantly worse than A (p < 0.05) on some
  character -- then stop and report; do not silently turn it off.
- Out-of-combat policy: flip `OOC_POLICY_DEFAULT` to `greedy` (Phase 3a Task 8) if the mean C - B
  diff over characters is >= 0 and no character is significantly worse; otherwise report.
- Then Phase 3b Task 6 (fit the route model on arm C's logs) and its own A/B.

## Change of plan (2026-10-09): arm A dropped

The inferrer question was answered by data we already had, so arm A (a third of the compute, ~1
day) was dropped and the batch runs B vs C only. The 9/30 BUG-040 regression (inferrer OFF, naive,
a0, seeds run_1..5 x 5 characters, all 25 completed) and the 10/08 gate2 naive lane (inferrer ON,
naive, a0, the same 25 seeds) pair seed-for-seed: global floor 17.5 -> 39.3, **+21.84 (se 2.16,
t = 10.1)**, 24 of 25 pairs improved, every character +13.6..+27.8, wins 0 -> 2. The only other
changes between the two runs (BUG-046/047/049/050/051) only alter games that hang, stall or crash,
and the OFF run had none, so the difference is the inferrer. Evidence:
`~/.sts2-train/bug040_fix_gate_20260930_100630/regression.log`,
`~/.sts2-train/gate2_20261008_202822/regression_naive.log`.

Gate (`~/.sts2-train/gate2_20261008_202822/`): naive and greedy, inferrer on, 5 x 5 each --
Completed 5/5 for all 10 character/policy lanes, no HANG/TIMEOUT/ERROR, 6 wins in 50 games.

## Results

(filled in after the run)
