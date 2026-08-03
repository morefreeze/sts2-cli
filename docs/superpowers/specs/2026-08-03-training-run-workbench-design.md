# Training Progress and Run Analysis Workbench Design

**Date:** 2026-08-03
**Status:** Approved in conversation; implementation planning follows after document review.

## Problem

The current progress viewer is optimized for inspecting one GameLogger-style
`state`/`action` JSONL file. It can show rooms, choices, combat actions, deck
snapshots, relics, and potions, but it does not answer the primary training
question: whether the policy is improving and how far it advances on average.
It also lists every JSONL file as if it were a replay. Loading an evaluation or
boss-summary file therefore produces an empty-looking run with zero rooms.

The desired workbench has two levels:

1. A training dashboard that compares the current evaluation window with a
   fair baseline and identifies progression bottlenecks.
2. A per-run analysis page that first shows the complete map and visited route,
   then lets the user inspect the reward, cost, decision, and combat details for
   a selected floor.

The project has several related but incompatible sources: native STS2 `.run`
files, GameLogger replay JSONL, `deck_history.jsonl`, evaluation snapshots, and
summary-only JSONL. The UI must not infer that all files have the same shape or
silently treat missing evidence as zero.

## Goals

1. Show average, median, and maximum global progression, Act conversion rates,
   sample size, trend, and technical-failure counts for training/evaluation
   cohorts.
2. Compare a current cohort with a selected baseline only when character,
   game version, evaluation mode, and seed set are comparable.
3. Separate legitimate wins/deaths from engine, automation, timeout, and stuck
   failures. Technical failures are excluded from gameplay aggregates by
   default and remain visible as a separate diagnostic queue.
4. Normalize supported sources into a canonical run model with explicit
   capability and completeness flags.
5. Reconstruct the full STS2 act map when the required metadata is available,
   overlay the visited route, and show real reward/cost data only for visited
   nodes.
6. Preserve the existing room, decision, deck, relic, potion, and turn replay
   depth for GameLogger JSONL.
7. Use original game map-node art when available, emoji as the runtime fallback,
   and letters only as compact/accessibility labels.
8. Degrade honestly for partial or historical data instead of rendering an
   empty or misleading view.

## Non-goals

- Replacing PPO, changing reward functions, or modifying policy behavior.
- Claiming unvisited-node rewards. The full map provides topology and room
  types; rewards exist only after a node is visited.
- Reconstructing missing full maps from historical JSONL that lacks the seed,
  act identifiers, ascension, or modifiers.
- Supporting multiplayer training or multiplayer turn replay in the first
  implementation.
- Copying the unlicensed HerrAA918 dashboard source. Its information hierarchy
  is reference material only.
- Making a remote service mandatory. Run data and analysis remain local.

## Product Structure

### 1. Training dashboard

The default page answers whether training improved before showing individual
runs. It contains:

- Current cohort and baseline selectors.
- Character, game version, evaluation mode, and validity filters.
- Average global floor, median global floor, maximum global floor, Act 2 entry
  rate, valid sample count, and technical-failure count.
- A rolling progression trend with the selected baseline visibly separated.
- A floor/Act conversion funnel that exposes where runs stop progressing.
- A prioritized anomaly list, such as high boss HP loss, early attrition, or
  technical failures.
- Representative run rows for each bottleneck. Selecting a row opens the
  per-run page without losing the active dashboard filters.

`global_floor` is the comparison coordinate. Act-local floor resets must never
make Act 2 appear shallower than Act 1.

### 2. Per-run map overview

The first per-run view is the complete map, not the combat transcript.

- Acts are separate tabs.
- All reconstructed nodes and edges are visible when reconstruction succeeds.
- The visited path is gold; the current/death node is red; unvisited branches
  are neutral.
- Each visited node shows compact deltas: HP, max HP, gold, cards, relics,
  potions, upgrades, removals, transformations, and result as available.
- An Act summary aggregates only visited-node changes.
- Selecting a visited node opens its detail below the map.
- Unvisited nodes show room type and topology only.

If full reconstruction is unavailable, the same page renders the recorded
visited route and a visible explanation instead of an incomplete graph that
looks authoritative.

### 3. Floor detail

The selected floor is organized from outcome to evidence:

1. Entry and exit HP/max HP/gold.
2. Enemy or event, turns, chosen option, and actual rewards/costs.
3. Deck, relic, and potion state plus node-level deltas.
4. Factual anomaly signals, such as an unused potion or a high-loss turn.
5. Turn-by-turn actions, targets, hands, enemy intents, and states when replay
   data exists.

Observed facts and diagnostic hypotheses are rendered separately. For example,
“Fire Potion present; no `use_potion` action recorded” is a fact. “The policy
should have used the potion” remains a hypothesis until the relevant hand,
enemy intent, and planner decision are examined.

## Canonical Data Model

All adapters produce a `RunRecord`; the frontend never branches directly on a
raw filename or source schema.

```text
RunRecord
  identity: run_id, source_id, source_kind
  metadata: character, seed, game_version, checkpoint, evaluation_mode,
            ascension, modifiers, started_at, ended_at
  outcome: status, victory, max_global_floor, max_floor_label,
           technical_failure_kind
  coverage: complete_run, first_recorded_floor, last_recorded_floor
  capabilities: full_map, visited_route, node_rewards, final_inventory,
                decisions, turn_replay
  acts[]
  nodes[]
  replay_by_node
```

Each `RunNode` contains stable Act/floor identity, map coordinates when known,
room type, encounter/event identity, visited/status flags, player snapshots,
node deltas, choices, actions, and optional combat rounds.

Each numeric delta carries a quality marker:

- `exact`: directly reported by native history.
- `derived`: computed from adjacent snapshots.
- `unknown`: unavailable and rendered as a dash, never zero.

## Source Adapters

### Native `.run`

Provides run metadata, build ID, seed, ascension, modifiers, final inventory,
`map_point_history`, and native per-node statistics. It supports training-style
overview metrics for imported human runs and detailed visited-node rewards, but
not turn-level actions.

### GameLogger `state`/`action` JSONL

Extends the current `parse_game_progress` output. It remains the authoritative
source for options, actual commands, room snapshots, hands, enemies, intents,
potions, and combat rounds. New logs also record run identity and full-map
metadata so they can be joined with training outcomes and reconstructed.

Historical partial logs retain their first recorded floor and render only the
evidence they contain.

### `deck_history.jsonl`

Joins `milestone`, `card_pick`, and `outcome` rows by `run_id`. It supplies the
large run population used for progression aggregates and card-decision context.
The current outcome schema lacks checkpoint, seed, global Act context, game
version, and technical-failure classification, so new outcome rows must add
those fields without rewriting historical records. Old rows remain loadable but
are grouped into an “unknown metadata” cohort and are not used for strict
checkpoint comparisons.

### Evaluation and summary records

Evaluation records may contribute cohort results when they contain a real
per-game outcome contract. Boss-deck and summary-only JSONL is classified as
`summary`, not `replay`. Opening it displays the supported summary view or a
clear file-type message; it never produces an apparent zero-room run.

## Run Identity and Joining

New training runs use one `run_id` across `deck_history`, progress logs, and
evaluation results. The evaluation launcher supplies checkpoint path/name,
evaluation mode, game index, seed, and game version at run start.

Historical sources are joined only when identity is unambiguous. Seed,
character, and overlapping timestamps may suggest a candidate match but do not
silently merge records. Ambiguous records remain separate.

## Training Metrics and Comparison Rules

Default gameplay aggregates include `win` and legitimate `dead` outcomes.
`crash`, `timeout`, `stuck`, reset failure, and corrupt/incomplete attempts are
technical outcomes and are excluded by default.

The comparison banner states why cohorts are or are not comparable. A strict
checkpoint comparison requires:

- Same character.
- Same game build or an explicit cross-version override.
- Same evaluation mode and scenario/preset.
- Same fixed seed set, or a clearly labeled non-paired comparison.
- At least one valid result in each cohort.

The dashboard reports both the value and denominator. Act conversion rates are
derived from global progress, not Act-local floor values.

## Full Map Reconstruction

The implementation may reuse the deterministic map-generation modules from
`Akirakato1/Slay-the-Spire-2-dashboard`, which are MIT licensed. The copied or
adapted source retains the copyright and license notice in the repository.

Inputs are seed, Act ID/index, ascension, modifiers, multiplayer flag, game
version, and recorded visited history. The generator returns nodes, coordinates,
edges, and route alignment. The recorded route is then overlaid on the graph.

Map reconstruction is accepted only when route alignment succeeds. A mismatch,
ambiguous alignment, unsupported build, or missing input triggers the visited-
route fallback with a human-readable reason.

## Node Art

The icon resolver uses this order:

1. Original map-node art extracted or cached locally from the installed game.
2. Semantic emoji for the room type.
3. Letter fallback.

Boss and Ancient nodes use their model-specific art where available. Every
node also has a Chinese tooltip and an accessible text label. Missing art is
not an error and never blocks map rendering.

## Components and Boundaries

- **Catalog:** discovers files, classifies source shape, and returns lightweight
  metadata without parsing every full replay on initial load.
- **Adapters:** parse one source kind into canonical records.
- **Joiner:** combines records only through confirmed identity.
- **Metrics:** computes comparable cohorts and exclusion counts from canonical
  outcomes.
- **Map service:** reconstructs and aligns a map independently of UI rendering.
- **Asset resolver:** returns original icon, emoji, or letter without affecting
  data parsing.
- **Viewer API:** serves catalog, aggregate, run, and node-detail payloads.
- **Frontend:** renders dashboard, map, and floor detail from stable contracts.

These boundaries prevent the existing embedded HTML from acquiring more
source-specific parsing rules. Implementation may split the current large
`run_progress_viewer.py` while keeping its existing CLI entrypoint and default
port compatible.

## Error Handling and Honest Degradation

- Invalid JSON/JSONL reports filename and line number.
- Unsupported JSON shape reports the detected type and supported actions.
- Empty replay reports “no state/action records,” not a normal run with zero
  rooms.
- Partial logs display their recorded range.
- Missing values display unknown, not zero.
- Technical failures remain queryable and link to their logs.
- Map mismatch falls back to the visited route.
- Missing icons fall back without interrupting the map.
- One malformed run cannot prevent other runs or charts from rendering.

## Verification

Implementation follows test-driven development. Fixtures cover:

1. Native `.run`, complete GameLogger JSONL, partial JSONL, technical failure,
   summary-only JSONL, and malformed JSONL.
2. Canonical capability and quality flags for each source.
3. Run joining by `run_id` and rejection of ambiguous historical matches.
4. Cross-Act global-floor ordering.
5. Exclusion of technical failures from gameplay aggregates.
6. Strict paired-cohort comparison and visible mismatch reasons.
7. Deterministic full-map nodes, edges, and visited-route alignment for known
   seeds.
8. Map fallback for missing metadata, unsupported versions, and failed
   alignment.
9. Exact versus derived node deltas.
10. Original-art, emoji, and letter resolution order.
11. HTTP/API contracts and frontend rendering for empty, partial, and full
    states.
12. Browser acceptance using the validated A2F4 replay and at least one native
    `.run` fixture.

Existing run-progress-viewer tests remain green. Generated logs, extracted game
assets, checkpoints, and visual brainstorming files remain outside commits.

## Delivery Order

1. Canonical models, source classification, adapters, and error states.
2. Training aggregates and dashboard.
3. Visited-route per-run overview and node deltas.
4. Full-map reconstruction with route alignment and fallback.
5. Original node art with emoji/letter fallback.
6. Floor detail and existing turn replay integration.
7. End-to-end verification on real local records.

Each stage preserves a usable viewer and is independently testable. No stage is
promoted by a screenshot alone; acceptance requires the canonical payload and
rendered result from real input data.
