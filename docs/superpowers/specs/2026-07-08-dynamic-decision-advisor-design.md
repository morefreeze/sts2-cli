# Design: Dynamic Decision Advisor for strategy-driving scores

**Date:** 2026-07-08
**Status:** Approved for planning

## Problem

The current agent has useful pieces for strategy:

- `agent/card_scoring.py` scores cards, deck dimensions, 5-turn burst, block per turn, engine efficiency, and boss versatility.
- `agent/turn_planner.py` searches playable combat sequences for short-term survival and lethal lines.
- `agent/strategy.py` contains hard-coded map/rest heuristics.

These pieces are not yet organized as a single decision layer. Card rewards, map choices, rest sites, shops, and combat interventions should all be evaluated as candidate actions with comparable directional scores, so the automatic strategy can ask: "which action improves this run state the most?"

## Goals

- Add a strategy-driving `DecisionAdvisor` that ranks candidate actions across all major decision points.
- Use directional scores rather than a single opaque card score.
- Preserve hard safety rules: avoid actions that immediately die, prefer guaranteed lethal, and raise survival/risk weights when HP is low.
- Reuse existing scoring and combat-planning code wherever possible.
- Keep the first version inference-only: no retraining and no observation/reward changes.

## Non-goals

- No UI dashboard in the first version.
- No full multi-combat Monte Carlo rollout in the first version.
- No replacement of the proven combat planner with a purely static heuristic.
- No broad retuning of RL models.

## Recommended approach

Use a hybrid scoring decision layer:

1. Generate legal candidate actions for the current decision point.
2. Evaluate each candidate through relevant directional evaluators.
3. Apply hard safety filters and phase-aware weights.
4. Return the highest-ranked action plus explanation metadata for logs.

This balances reliability and cost. Rule scores are fast and explainable for deck-building/pathing, while the existing combat planner handles tactical sequencing better than static formulas.

## Architecture

```text
DecisionAdvisor
  ├─ CombatEvaluator       current turn survival, lethal, block need, future value
  ├─ DeckEvaluator         attack, defense, cycle, energy, boss readiness
  ├─ RewardEvaluator       pick/skip marginal delta for card rewards and shops
  ├─ MapRiskEvaluator      room risk from HP, deck strength, floor, path type
  ├─ RestEvaluator         heal/smith/remove priority from risk and upgrade value
  └─ ActionRanker          safety filters, phase weights, final action selection
```

The output shape is shared across decision points:

```json
{
  "action": "pick_card",
  "target": "SHRUG_IT_OFF",
  "score": 0.76,
  "hard_safety": false,
  "dimensions": {
    "attack": 0.02,
    "defense": 0.18,
    "cycle": 0.09,
    "energy": 0.0,
    "boss_ready": 0.06,
    "risk": -0.01
  },
  "reason": "Improves the weakest axis: defense; also improves cycle."
}
```

## Directional scores

### Deck scores

Computed from the current deck and reused by reward, map, rest, and boss-readiness decisions.

- `attack`: immediate damage, 5-turn burst, strength/vulnerable payoff, AOE.
- `defense`: block per turn, weak, persistent block engines, current HP cushion.
- `cycle`: deck size, draw density, first-cycle speed, core-card access.
- `energy`: cost pressure, energy generation, high-cost card burden.
- `boss_ready`: 5-turn damage, block sustain, scaling, AOE/multi-hit coverage.

Existing `card_scoring.py` metrics should be the first source for these values.

### Combat scores

Combat decisions should remain tactical:

- Prefer guaranteed lethal.
- Reject sequences that die after enemy intent resolves.
- Prefer blocking when incoming unblocked damage exceeds a danger threshold.
- Reward damage, kills, remaining HP, and persistent future value.

Existing `turn_planner.plan_action()` and `intent_defense_override()` are the primary implementation hooks.

### Reward delta scores

Card rewards and shop cards should be scored by marginal impact:

```text
score(deck + candidate) - score(deck)
```

The candidate set always includes skip. Skip should become more attractive when:

- the deck is already large,
- the candidate does not improve a weak axis,
- the candidate worsens cost pressure,
- the candidate is off-plan for the current archetype.

### Map risk scores

Pathing should score candidate rooms by expected risk/reward:

- Monster: good when attack/defense are healthy and HP is stable.
- Elite: only attractive when HP and boss-readiness are both strong enough.
- Rest: more valuable when HP risk or smith target exists.
- Shop: more valuable with enough gold or a removal need.
- Event/Unknown: safer default when deck or HP is weak.
- Treasure: high value, low risk.

This should replace rigid map priority ordering with weighted action ranking while retaining safety thresholds.

### Rest scores

Rest-site choices should compare:

- heal value: HP ratio, next-room risk, boss proximity;
- smith value: `best_smith_target()` and upgrade payoff;
- remove value, if available: removal target quality and deck-thinning benefit.

Hard rule: below critical HP, heal wins unless the next action is guaranteed safe.

## Phase-aware weighting

Weights should change by game phase:

| Phase | Primary pressure | Weight changes |
| --- | --- | --- |
| Early Act 1 | survive first fights, add damage | attack and HP risk high, skip less attractive |
| Late Act 1 | boss readiness | boss_ready and defense rise |
| Act 2+ | scaling and sustain | cycle, defense, scaling, and path risk rise |
| Pre-boss | avoid unnecessary HP loss | elite/monster risk penalty rises |

Initial weights should be conservative and easy to tune in one table.

## Integration points

- `agent/combat_env.py::greedy_action()` should call `DecisionAdvisor.choose(state)` for supported decision types.
- `card_scoring.py` remains the low-level scoring library; avoid duplicating card stat parsing.
- `turn_planner.py` remains the combat tactical planner.
- Existing strategy classes can stay as fallback implementations while the advisor is rolled out.
- Candidate-ranking metadata should be logged when verbose mode is enabled, but normal gameplay should only emit the chosen action.

## Safety and fallback

- If an evaluator cannot score a decision, fall back to the current strategy path.
- Broken cards remain excluded through existing `BROKEN_CARDS`.
- Unknown card effects should receive conservative scores rather than optimistic scores.
- Any candidate that causes a simulated immediate death receives a terminal penalty.
- The advisor should be deterministic for a given state.

## Testing

Unit tests:

- DeckEvaluator returns sensible dimensions for synthetic decks.
- RewardEvaluator prefers a card that improves the weakest axis and skips bad/off-axis cards.
- MapRiskEvaluator avoids elites at low HP and allows elites when HP/deck readiness are high.
- RestEvaluator heals at critical HP and smiths clear must-upgrade cards otherwise.
- ActionRanker applies hard safety before weighted score.

Integration tests:

- Existing combat planner tests continue to pass.
- `greedy_action()` returns valid engine commands for `combat_play`, `card_reward`, `map_select`, and `rest_site`.
- Fallback path is used for unknown decision types.

Regression:

- CLAUDE.md 5 games per character crash/stuck gate remains the hard completion gate.
- Compare fixed-seed before/after on Ironclad for avg floor and boss reach where feasible.

## Success criteria

- The advisor drives at least card reward, map selection, rest-site, and combat decisions through one candidate-ranking interface.
- No increase in crash/stuck failures.
- Logs can explain why a selected action won through directional deltas.
- Low-HP runs take safer paths than the current static map strategy.
- Card rewards respond to deck weakness instead of only absolute single-card strength.
