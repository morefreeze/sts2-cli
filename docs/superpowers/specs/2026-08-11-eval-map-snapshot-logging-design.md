# Eval Map Snapshot Logging Design

## Goal

New full-run evaluations must retain enough authoritative game data for the
workbench to display every branch of each generated act map and the route that
the run actually visited. A normal defeat or a technical failure after map
entry must not discard the last trustworthy map state.

The existing run
`eval-ppo_ironclad_13172k-20260811T175248-926fb378-099-a100` demonstrates the
gap: its joined record has seed and outcome metadata, but no route nodes,
`is_multiplayer` is unknown, and `/api/run/map` therefore returns an empty
recorded-route fallback.

## Capture and persistence

- `run_eval_verbose` enables map capture explicitly in the `CombatEnv`
  `run_context`. Training environments do not enable it, avoiding a large
  increase in training-log volume.
- After the simulator advances into each combat room, `CombatEnv` requests the
  current authoritative map through the existing `get_map` command. The reply
  contains every node, child edge, visited flag, current coordinate, boss, and
  act context.
- The environment keeps only the latest snapshot for each act. Repeated rooms
  update that act instead of retaining a full-map copy per floor.
- `_emit_run_outcome` writes the retained snapshots before the outcome row in
  `deck_history.jsonl`. All existing terminal paths use this method, including
  death, timeout, stuck, invalid state, reset failure, and a detected child
  process crash. Therefore a process failure after at least one successful map
  capture still preserves the last trustworthy map.
- A failure before the first map exists records its normal technical outcome
  but cannot fabricate map data.
- Each persisted row uses `event="map_snapshot"`, the existing run metadata,
  an exact one-based `act`, `is_multiplayer=false`, the bounded raw `map`
  object, and a finite timestamp. Existing rows and historical files are not
  rewritten.

## Workbench consumption

- The deck-history adapter recognizes validated `map_snapshot` rows, derives
  the visited route in row order, exposes act descriptors, and sets
  `full_map`/`visited_route` capabilities. Ordinary deck decisions remain
  separate evidence and are not mistaken for route nodes.
- The map endpoint prefers a validated recorded snapshot over seed-based map
  reconstruction. This makes the exact v0.107.1 graph viewable even though the
  pinned reconstruction generator only supports v0.103.2.
- Recorded nodes and edges are normalized into the existing `ActMap` contract.
  Duplicate coordinates, dangling/self edges, oversized arrays or strings,
  invalid coordinates, and malformed booleans fail closed. On an invalid
  snapshot the endpoint retains its existing reconstruction/recorded-route
  fallback instead of returning a server error.
- Existing node artwork, terminal markers, act tabs, and route highlighting
  consume the same API payload and require no browser format change.

## Bounds and failure handling

- At most four latest act snapshots are buffered per run.
- A snapshot is accepted only when `get_map` returns the expected map object
  with a supported act number. Logging failures remain visible through the
  existing run-logging warning path and remain retryable with the terminal
  outcome.
- Persisted and served map data uses the existing map limits: at most 256
  nodes, 2048 edges, bounded identifiers/room types, and strict JSON numbers.
- An unavailable or malformed `get_map` response never changes gameplay and
  never turns a valid evaluation result into a technical failure.

## Verification

Tests must first reproduce the missing behavior, then prove:

1. eval environments opt into capture while training defaults remain off;
2. later snapshots replace earlier snapshots for the same act;
3. death and each technical terminal status flush the retained map before the
   outcome row, including when the child process is already unavailable;
4. deck-history adaptation exposes authoritative acts, route nodes,
   multiplayer metadata, and map capabilities;
5. `/api/run/map` serves a recorded v0.107.1 full graph without invoking the
   v0.103.2 generator;
6. malformed snapshots fail closed and historical deck histories remain
   readable; and
7. the complete agent/workbench suites and one fresh evaluation smoke run pass.
