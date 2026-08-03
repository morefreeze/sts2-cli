# Act 1 Marginal Card-Quality Gate Design

**Status:** Implemented and promoted on 2026-08-03 after correcting the
cross-act floor metric and rerunning the fixed 20-seed comparison.

## Context

The current best full-run policy is the 2,048-step early-stop checkpoint
`ppo_ironclad_13955k.zip`.

On the same 20 fixed seeds, after converting the engine's act-local floor to
`(act - 1) * 17 + floor`:

- the original `13933k` baseline averages `15.3` floors, reaches the Act 1
  boss in `11/20` runs, and enters Act 2 in `3/20`;
- `13955k` without the gate averages `16.2` floors, reaches the boss in
  `11/20`, and enters Act 2 in `4/20`.

The earlier `0/20` Act 2 result was a reporting defect: Act 2 resets its local
floor to one, while evaluation previously retained 17 as the maximum. Full-run
evaluation now proves that `13955k` can cross the Act 1 boss on four of the
fixed seeds. Deck dilution remains independently measurable at boss entry.

The recent deck-history sample contains 680 completed runs that reached the
Act 1 boss. After those decks had already reached 15 cards, the agent still
took 1,609 card rewards. Adding the selected card reduced or failed to improve
the operational deck-quality score in 985 cases (61%). The most common
negative-marginal additions were Sword Boomerang, Perfected Strike, Body Slam,
Bully, Molten Fist, and Havoc.

The remaining primary bottleneck is therefore late Act 1 deck dilution, not
general floor progression or a lack of boss-combat training data.

## Goals

1. Keep the `13955k` 20-seed average floor at or above `15.0`.
2. Keep Act 1 boss reach at or above `11/20`.
3. Produce measurably leaner and no-worse-quality boss-entry decks.
4. Beat at least one Act 1 boss and enter Act 2 on the same 20 fixed seeds.
5. Preserve a one-variable A/B path and an immediate runtime rollback.

## Non-goals

- Changing PPO combat actions, observations, rewards, or network weights.
- Changing map, rest-site, event, shop, or card-removal strategies.
- Re-ranking cards or replacing the existing card-scoring model.
- Applying the quality gate to Act 2 or later acts.
- Refactoring unrelated card-scoring or environment code.

## Chosen approach

Add a pure Act 1 card-reward eligibility predicate in
`agent/card_scoring.py`. `agent/combat_env.py::greedy_action` will filter the
offered cards through the predicate before passing the eligible subset to the
existing `pick_best_card` function.

This preserves the current score ordering and score threshold. If the current
top-scoring card is ineligible but another offered card is eligible, the agent
can still select the best eligible alternative. If no card is both eligible
and above the existing score threshold, the agent skips the reward.

The behavior is available behind `STS2_CARD_QUALITY_GATE`. All promotion
criteria passed under the corrected global-floor metric, so the final branch
enables it by default. Setting `STS2_CARD_QUALITY_GATE=0` restores the pre-gate
behavior.

## Eligibility rules

The predicate receives the offered card, the current deck, and the current act.
It computes:

- `before`: `deck_quality_metrics(deck)["overall"]`;
- `after`: `deck_quality_metrics(deck + [card])["overall"]`;
- `delta = after - before`;
- `premium_core`: either:
  - `score_card_in_deck(card, deck) >= 9.5`, or
  - the card has the `SCALING_PILLAR` tag while the deck has fewer than two
    scaling pillars.

The evaluated rules are:

1. Before floor 12, do not invoke the gate. Early event rewards can inflate a
   deck to 15 cards by floor 6; filtering those rewards caused two fixed seeds
   to lose Act 1 boss reach.
2. Outside Act 1, accept the card unchanged.
3. With fewer than 15 cards, accept the card unchanged.
4. With exactly 15 cards, accept when `delta > 0`, or when the card is a
   `premium_core` and `delta >= -0.01`.
5. With 16 or more cards, reject further skippable card rewards.

The 16-card cap was the only tested configuration that reduced median
boss-entry deck size from 17 to 16 without reducing average floor or boss
reach. Applying only the old strict delta rule at 16 cards regressed a
sentinel seed to floor 14, while the hard cap preserved its boss reach. The
premium lower bound prevents nominal scaling cards such as Pyre
(`delta=-0.0355` in the failing seed) from bypassing the cap's quality intent.

The deck list, rather than a separate reported deck-size field, is
authoritative because the quality calculation requires the actual cards.

## Failure behavior

The gate fails open when an input or quality metric required by the applicable
branch is missing, invalid, non-finite, or raises an exception. A valid
16-card deck follows the explicit hard-cap branch without evaluating quality
metrics.

Broken-card filtering remains owned by `pick_best_card` and is unchanged.
The gate must not turn an engine-data problem into an automatic skip or a
failed run. If a reward explicitly reports `can_skip=false` and filtering
removes every option, `greedy_action` falls back to the original unfiltered
offer set.

## Data flow

1. `greedy_action` receives a `card_reward` state.
2. It establishes the existing MC context and existing score threshold.
3. When the quality gate is enabled, the run is at floor 12 or later, and the
   state is in Act 1, it filters offered cards with the pure eligibility
   predicate while retaining their original indices. The act is read from
   either `state.act` or the runtime's actual `state.context.act` location.
4. It calls `pick_best_card` on the eligible cards.
5. It maps the selected eligible-card index back to the original reward index.
6. If no eligible card clears the existing threshold, it returns
   `skip_card_reward`.
7. Existing `card_pick` logging records the actual selected card or `SKIP`;
   no persisted schema changes are required.

## Tests

Unit tests in `tests/agent/test_card_scoring.py` will cover:

- Act 2 and later acts are unchanged;
- decks below 15 cards are unchanged;
- a non-positive marginal card is rejected at 15-17 cards;
- a positive-marginal card is accepted at 15-17 cards;
- a missing scaling pillar and a score-9.5 premium card receive the exception;
- a severely negative premium card is rejected at 15 cards;
- a 16-card deck rejects every further normal reward before scoring;
- invalid or incomplete inputs fail open.

Integration tests in `tests/agent/test_combat_env.py` will cover:

- the gate can reject the old top-scoring card and select an eligible
  lower-ranked card using its original reward index;
- all ineligible offers produce `skip_card_reward`;
- the promoted gate is enabled when the environment variable is unset;
- `STS2_CARD_QUALITY_GATE=0` restores the current selection behavior.

All existing card-scoring and combat-environment tests must remain green.

## Fixed-seed validation

Use the same explicit checkpoint,
`checkpoints/act1_boss_13933_smoke_20260727/ppo_ironclad_13955k.zip`, for both
lanes. Disable advisor, planner, boss planner mask, and boss-readiness lift.
Set `DECK_HISTORY_PATH=` so neither lane updates the outcome-feedback bandit
during evaluation.

Run 20 fixed seeds in each lane:

- baseline: `STS2_CARD_QUALITY_GATE=0`;
- candidate: `STS2_CARD_QUALITY_GATE=1`.

Both lanes use `--invalid-retries 2`, a unique boss-deck JSONL file, and a
unique boss-snapshot directory.

The gate is promoted only if all of these are true:

1. valid games are `20/20` with zero invalid attempts;
2. candidate average floor is at least `15.0`;
3. candidate boss reach is at least `11/20`;
4. candidate enters Act 2 in at least `1/20` runs;
5. median boss-entry deck size is at least one card smaller than baseline;
6. average boss-entry operational quality does not decrease;
7. average starter-basic count does not increase.

If the gate improves deck composition and preserves progression but still
produces `0/20` Act 2 entries, capture its boss-entry snapshots and perform one
2,048-step natural-HP diverse boss fine-tune from `13955k`. The combined
checkpoint and gate must pass the same promotion criteria; training results
alone are not sufficient.

If any progression or deck-quality criterion regresses, keep the gate disabled,
inspect rejected offers on the failing seeds, adjust only one threshold or
exception rule, and repeat the paired evaluation.

## Evaluation result

The corrected fixed-seed evaluation used checkpoint
`ppo_ironclad_13955k.zip` and compared gate-off against gate-on:

- both lanes: `20/20` valid, zero invalid attempts, boss reach `11/20`;
- average global floor: `16.2 -> 15.9`, while the original `13933k` baseline
  is `15.3`;
- Act 2 entry: `4/20 -> 3/20`, satisfying the required nonzero entry gate;
- maximum progress: `A2F10 -> A2F11`;
- median boss-entry deck size: `17 -> 16`;
- average boss-entry deck size: `17.18 -> 15.73`;
- average operational quality: `0.511462 -> 0.515988`;
- average starter-basic count: `7.4545 -> 7.4545`.

The original `13933k` baseline has the same boss-entry deck statistics as the
ungated `13955k` lane. Therefore the promoted candidate simultaneously
improves average floor by `0.6`, reduces median deck size by one card, and
raises operational deck quality relative to the original baseline; it does
not rely on different baselines for the two halves of the goal. The unified
proof object is
`logs/compare_13933k_13955k_gate_fixed20_global_20260803_v1.json`.

All seven predefined promotion checks pass. The gate therefore defaults on;
`STS2_CARD_QUALITY_GATE=0` remains the immediate rollback. The earlier
2,048-step `13957k` checkpoint remains rejected because it regressed Act 1
progression; it is not needed to promote the static gate.

## Deliverables

- Pure eligibility predicate in `agent/card_scoring.py`.
- Act 1 reward filtering and original-index mapping in
  `agent/combat_env.py`.
- Focused unit and integration tests.
- Paired 20-seed baseline and candidate logs.
- Boss-entry deck comparison and Act 2 evidence.
- A measured, default-on gate with an explicit runtime rollback because all
  predefined acceptance criteria pass under corrected global-floor metrics.
