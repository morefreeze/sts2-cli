# Phase 3 — Out-of-combat policy and holistic route planning (design)

Date: 2026-09-30. Branch: `feat/deterministic-planner`.
Program goal (user, 2026-09-30): keep optimizing until the Act 3 boss clear rate
holds at 60%. Design choices below were approved by the user in chat ("port first,
then route", "a1", "按照你推荐的继续").

## Where we start

Solver-era baseline, a0, all 430 valid games from the Phase 2 ON arms and the
Phase 2b-1 tier runs: **0 wins**.

| Character | n | pass A1 boss | pass A2 boss |
|---|---|---|---|
| Defect | 75 | 65% | 3% |
| Ironclad | 77 | 23% | 0% |
| Regent | 78 | 22% | 1% |
| Silent | 80 | 20% | 0% |
| Necrobinder | 120 | 12% | 0% |

The combat solver drives every fight in `python/play_full_run.py`, but every
out-of-combat decision there is a placeholder: random map node, card reward index 0,
rest always HEAL, shop leaves immediately, bundle 0, card_select index 0. Under that
policy gold is worth nothing and card rewards are lottery tickets, so no route model
can value them. Phase 3 fixes the policy first, then builds the route model on it.

## Map facts (measured)

- Fixed rows are act-dependent, from 430 games' logs: Act 1 — row 1 Monster, row 9
  Treasure, row 15 RestSite, boss row 16. Act 2 — row 0 the act-opening event, row 1
  Monster, row 8 Treasure, row 14 RestSite, boss row 15. Act 3: too few samples.
  The planner therefore detects fixed rows from `get_map` (a row whose nodes all share
  one type), never from hard-coded row numbers.
- An Act 1 map has 32–374 start→boss paths (8 seeds, a1). Full enumeration is
  milliseconds; no beam or segment decomposition is needed for tractability.
- `get_map` names the act boss (`boss.id`, e.g. `THE_KIN_BOSS`) before the first move.

## Phase 3a — out-of-combat policy port

Changes to `python/play_full_run.py`:

1. `--ascension N` (default 0, so existing invocations and the regression command are
   unchanged). Sent as `start_run.ascension`; recorded in every results row as
   `ascension`. Phase 3 runs use `--ascension 1`.
2. `STS2_OOC_POLICY` = `greedy` | `naive` (default `greedy` once 3a ships; `naive` is
   the control arm and reproduces today's behaviour bit-for-bit on a seed). Unknown
   values raise, like `STS2_SOLVER_CHARS`. Under `greedy`, the decisions `card_reward`,
   `rest_site`, `event_choice`, `bundle_select`, `shop`, and out-of-combat
   `card_select` are answered by `agent.combat_env.greedy_action(state)`. `map_select`
   is NOT delegated (routing stays the seeded random choice in 3a so the A/B isolates
   the policy), and `combat_play` stays on the solver path.
3. Loop safety: a greedy command that the engine answers with `type == "error"` falls
   back to the naive command for that decision; a shop visit is capped at 20 actions,
   then `leave_room`. Both events are counted and printed in the SUMMARY
   (`ooc_fallbacks=`), so a policy that silently degrades to naive is visible.
4. `--trace-log <file>`: one JSONL row per `map_select` decision (the state before
   choosing) — seed, character, ascension, act, act-local floor, global floor,
   hp, max_hp, gold, deck (card id + upgraded flag), relic ids, potion ids, chosen
   node (col, row, type), boss id — plus one terminal row per game with the outcome
   and final global floor. Consecutive rows give per-room transitions. This is the
   calibration data for 3b and the death-cause data for later phases.

Acceptance: 67 existing tests plus new unit tests pass; full 5×5 regression
Completed 5/5 every character; paired A/B at a1, 40 fresh seeds × 5 characters,
`naive` vs `greedy`, both with the solver on. Ship `greedy` as default unless it is
significantly worse on any character (paired global-floor diff, p<0.05). Report
per-character diff, se, p, boss pass rates, wins.

## Phase 3b — holistic route planner

New module `agent/route_planner.py` (pure functions, no engine I/O) plus a fitting
script `agent/fit_route_model.py`. Wired into `play_full_run.py` behind
`STS2_ROUTE_POLICY` = `random` | `value` (unknown raises).

**State features** (from a decision state): hp, max_hp, hp/max_hp, gold, deck power
(`map_planner._deck_strength` — `card_scoring.deck_5turn_burst`, normalized), deck
size, count of non-starter relics, potion count, act, rows remaining to the boss,
character.

**Value model V(state)** = predicted remaining global floors from this state. Ridge
regression on the features above, fitted from 3a traces (the `greedy` arm, whose
routes are random — so node-type outcomes are not biased by route selection).
Validation: split by seed, 5 folds; V must beat an HP-only model (hp, max_hp, act,
rows remaining) on held-out MAE. If it does not, the planner ships with the HP-only
model and the report says the richer features added nothing.

**Transition model** per (act, room type): empirical distributions from consecutive
trace rows of Δhp, Δmax_hp, Δgold, Δrelics, Δpotions, Δdeck power, with Monster and
Elite split into two deck-power buckets. Death risk per room = empirical
P(hp loss ≥ current hp) from that room type's loss distribution. Cells with fewer
than 20 samples fall back to the act-pooled cell, then to all acts.

**Planning.** At every `map_select`: enumerate every path from each selectable child
to the boss; propagate the expected state room by room and accumulate survival
probability; score a path as
`P(survive) × (gf_boss_entry + V(state_at_boss_entry)) + Σ_k P(die at room k) × gf_k`.
Pick the child with the best-scoring path. Ties → lower column (deterministic).
The fixed rows are checkpoints, not decomposition: the trace records predicted vs
actual state at the Treasure and RestSite rows so model drift is measurable.

Acceptance: unit tests for enumeration, fixed-row detection, transition fallback, and
scoring on hand-built maps; full 5×5 regression; paired A/B at a1, fresh seeds,
40 × 5, `random` vs `value`, both on `greedy` OOC policy. Ship if mean paired diff is
positive and no character is significantly worse.

## Measurement protocol (both phases)

- a1, solver on for all five, default 120 s budget, `STS2_SOLVER_THREADS=3`,
  5 characters in parallel, fresh seed ranges per phase (never reuse a tuning seed
  set for the verdict).
- Metric: paired global-floor diff via `agent/paired_eval.py` (valid = win/dead;
  `stuck`/`timeout`/`crash` excluded and reported). Secondary: A1/A2/A3 boss pass
  rates and wins.
- Staleness monitor on every long batch (BUG-040 lesson): a log unwritten for 15 min
  is reported, not waited out.

## After Phase 3

Use the 3a/3b traces to rank where runs die (which boss, at what HP and deck power),
and pick the next lever from that ranking — likely candidates are card-pick quality
re-tuned on solver-era data, boss-aware deck building, and Phase 2b-2. Each lever gets
its own spec and paired A/B.

## Out of scope

Changing combat (solver) behaviour; changing `greedy_action` itself (3a ports it
as-is); Act-3-specific fixed rows (detected at runtime, not assumed).
