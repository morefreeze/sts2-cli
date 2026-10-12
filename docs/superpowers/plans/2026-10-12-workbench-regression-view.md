# Workbench regression/eval view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Interface-level plan (like the Phase 3b plan): exact files, semantics and test cases; implementers write the code TDD-style. Where this plan and the spec disagree, this plan wins.

**Goal:** A separate 回归/评测 tab in the workbench showing play_full_run batches with progress/ETA and, per finished game, route choices, deck evolution, combat legal actions + solver plan, and out-of-combat choices.

**Architecture:** A batch launcher writes a manifest and stamps batch fields into each game's run_meta; the workbench reads manifests + game logs to build batch/lane progress and ETA; a per-game detail payload is derived from the existing replay parser plus the logged `combat_plan` replies.

**Tech Stack:** Python 3.11 (`.venv/bin/python`), stdlib HTTP server, vanilla JS (existing static/*.js), pytest (+ node for static-contract tests).

**Spec:** `docs/superpowers/specs/2026-10-12-workbench-regression-view-design.md`.

---

## Background (read first)

- Game logs: `python/game_log.py` GameLogger writes `logs/<ts>_<char>_<seed>.jsonl`: first line `{"type":"run_meta", ...}` (keys in `_RUN_META_KEYS`), then `{"step","ts","type":"action"|"state","data":...}`. Engine replies include decisions (`map_select` with `choices`, `combat_play` with `hand`/`enemies`/`energy`, `card_reward` with `cards`, `shop`, `rest_site`/`event_choice` with `options`, `game_over`), and `{"type":"combat_plan", "actions":[{"turn","action","card_id","card_occurrence","target_combat_id",...}], "projected_player_hp", "all_enemies_dead", "only_death_routes_found", "search":{"budget_ms","elapsed_ms","boundary","expanded_nodes","total_expanded_nodes","inferrer"}}` replies to `{"cmd":"action","action":"plan_combat_turn"}`.
- play_full_run already stamps run_meta: seed, character, ascension, experiment (`<batch>:ooc=<policy>`), game_version (+source), evaluation_mode `fixed`, scenario `full_run`, is_multiplayer false, run_id (`pfr-…`). Flags: `--seed-prefix`, `--seed-offset`, `--keep-game-logs DIR`, `--results-log FILE`, `--experiment NAME`, `--ascension`.
- `game_log.cleanup_old_logs()` deletes `logs/*.jsonl` older than 7 days on every new GameLogger.
- Workbench: `agent/run_progress_viewer.py` (HTTP + payload builders, tree), `agent/run_workbench/catalog.py` (RunCatalog: discovery under roots `logs/` + `data/`, cohorts, in-progress detection with injectable clock, re-index throttle), `replay.py` (`parse_game_progress` → rooms with options/actions/combat turns/start+end player), `static/` (app.js router, tree.js, cohort-view.js, runs-table.js, run-view.js, map.js, replay.html). Tests: `tests/agent/run_workbench/`, `tests/agent/test_run_progress_viewer.py`, `test_static_contract.py` (node).
- Lesson: static tests passing ≠ page works; the controller verifies in a real browser.
- Rules: no game/engine runs while a batch runs from this checkout (check `pgrep -f play_full_run`); `git add` only task files; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File structure

- Create `python/run_batch.py` — batch launcher.
- Modify `python/play_full_run.py` — `--batch-id`, `--lane`; run_meta batch fields.
- Modify `python/game_log.py` — `_RUN_META_KEYS` += harness, batch_id, lane, game_index, games_total; cleanup exempts `logs/batches/`.
- Create `agent/run_workbench/batches.py` — manifest discovery, lane/batch progress, ETA, regression classification.
- Create `agent/run_workbench/game_detail.py` — per-game detail payload (route, deck evolution, combat legal actions + solver plans, out-of-combat choices).
- Modify `agent/run_progress_viewer.py` — endpoints `/api/batches`, `/api/batch`, `/api/batch/lane`, `/api/game-detail`; tab routing.
- Modify `agent/run_workbench/static/` — `index.html`, `app.js` (tabs + routes), new `batches-view.js`, new `game-detail.js`, `styles.css`.
- Tests: `tests/test_run_batch.py`, `tests/agent/run_workbench/test_batches.py`, `tests/agent/run_workbench/test_game_detail.py`, additions to `test_static_contract.py` / `test_http_api.py`.
- Modify `CLAUDE.md` (regression command), `agent/bug.md` none.

---

## Phase 1

### Task 1: batch fields in play_full_run run_meta + cleanup exemption

- `play_full_run.py`: new args `--batch-id ID` and `--lane NAME` (fallback env `STS2_BATCH_ID` / `STS2_BATCH_LANE`). When a batch id is known, run_meta gains `harness: "play_full_run"`, `batch_id`, `lane` (default: the character), `game_index` (1-based position within this process's `num_runs`), `games_total` (`num_runs`). `harness: "play_full_run"` is stamped on EVERY play_full_run game (with or without a batch). Results rows carry `batch_id`, `lane`, `game_index`.
- `game_log.py`: add the 5 keys to `_RUN_META_KEYS`; `cleanup_old_logs` must skip anything under `logs/batches/` (it only lists top-level `logs/*.jsonl` today — keep it that way and add a test proving files under `logs/batches/**` survive).
- Tests (tests/test_solver_gate.py patterns + tests/agent/test_game_log.py): header has the fields with batch args; env fallback; no batch → only `harness`; results row fields; cleanup skips batches dir.
- Commit: `feat(play_full_run): batch id/lane/index stamped into run_meta`.

### Task 2: `python/run_batch.py` launcher

```
run_batch.py --name NAME [--kind regression|ab|smoke] --games N --chars all|A,B
             [--max-lanes 6] [--threads 3] [--ascension A] [--seed-prefix run_]
             [--env KEY=VAL ...] [--dry-run] [-- <extra play_full_run args>]
```
- batch_id = `<name>-<YYYYmmdd-HHMMSS>`; dir `logs/batches/<batch_id>/`; `manifest.json` = {batch_id, name, kind, created_at (epoch), git_commit (`git rev-parse --short HEAD`), argv, env (the --env overrides + STS2_SOLVER_THREADS), lanes: [{lane, character, planned_games, seed_prefix, seed_offset, extra_args}]}. Written BEFORE any lane starts.
- One lane per character: `scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py N CHAR --ascension A --seed-prefix P --batch-id ID --lane CHAR --keep-game-logs <dir>/games --results-log <dir>/<lane>.jsonl [extra]`, env = os.environ + STS2_GAME_DIR default (the Steam path used elsewhere in the repo) + `STS2_SOLVER_THREADS=threads` + `--env` overrides, stdout/stderr → `<dir>/<lane>.log`, `start_new_session=True`.
- At most `--max-lanes` lanes run at once; the rest wait and start as lanes exit. `lanes_status.json` = {lane: {state: queued|running|done|failed, pid, started_at, ended_at, returncode}} rewritten atomically on every change.
- Foreground process waits for all lanes, prints a summary (per lane: results rows, Completed line from the lane log), exits non-zero if any lane exited non-zero. SIGTERM/SIGINT to the launcher → SIGTERM every running lane's process group, mark `failed`, exit 130.
- `--dry-run` writes the manifest and prints the commands without starting lanes.
- Tests (tests/test_run_batch.py): use a fake play_full_run (a tiny script given via an internal `--play-full-run-cmd` test hook or monkeypatched command builder) to check: manifest content; queueing with max-lanes=2 and 3 lanes; lanes_status transitions; non-zero exit propagates; dry-run starts nothing; env overrides reach lanes.
- Commit: `feat: run_batch.py launcher with manifest and lane status`.

### Task 3: workbench backend — batches, classification, ETA

`agent/run_workbench/batches.py`:
- `is_regression_run(meta: dict) -> bool`: `meta.get("harness") == "play_full_run"`, or (legacy) no `checkpoint` and experiment matches `^(?:[^:]+:)?ooc=`.
- `load_manifests(root: Path) -> list[dict]` from `root/batches/*/manifest.json` (+ `lanes_status.json` if present); tolerate malformed files (skip + report).
- `lane_progress(manifest_lane, finished_runs, live_runs, lane_status, now) -> dict`: planned, finished (win/dead/technical), live, avg_global_floor (valid only), wins, technical, durations (finished games' started→ended seconds from run_meta/first-last ts), eta_seconds = remaining × median(durations) (lane median; fallback batch median; None when no finished game anywhere), state.
- `batch_progress(manifest, lanes) -> dict`: totals, eta_seconds = max over unfinished lanes, eta_at, done flag.
- Legacy grouping: regression runs without batch_id grouped by experiment as pseudo-batches "未登记批次 · <experiment>" (planned = finished+live, no ETA).
- Runs come from the existing RunCatalog (reuse its sources/records; do NOT re-parse logs) — add the `logs/batches` subtree to discovery if it is not already covered by the `logs/` root (check `_discover`), and make sure the same game seen under `logs/` and `logs/batches/<id>/games/` is ONE run (dedupe by run_id; the Task-1 run_id is stable).
- Training views: classification only adds a filter; cohorts/tree for training must exclude regression runs, and their payloads for training runs stay byte-identical (golden check like earlier workbench changes).
- Endpoints in run_progress_viewer.py: `GET /api/batches` (list, newest first), `GET /api/batch?id=` (batch + lanes), `GET /api/batch/lane?id=&lane=` (that lane's runs: finished + live, newest first, with run detail refs).
- Tests (tests/agent/run_workbench/test_batches.py): classification (harness, legacy experiment, training checkpoint run), ETA math (lane median, fallback, none), batch eta = max, dedupe of kept copies, malformed manifest, legacy pseudo-batch, training cohorts exclude regression runs, endpoints.
- Commit: `feat(workbench): regression batches with progress and ETA`.

### Task 4: workbench frontend — tabs, batch list, batch page

- `index.html`: tab bar `训练` | `回归/评测` above the tree; `app.js` routes `#/training…` (existing views) and `#/regression`, `#/regression/batch/<id>`, `#/regression/batch/<id>/lane/<lane>`; the default tab is 回归/评测 if any batch is running or finished in the last 24 h, else 训练.
- `batches-view.js`: batch list (name, kind badge, start time, progress bar finished/planned, live count, technical count, ETA "约 X 小时 · <time>"); batch page (lane table: character, policy, progress bar, avg floor, wins (rate), technical, ETA; click → lane runs table reusing runs-table.js incl. the 进行中 badge); auto-refresh every 30 s while the batch is not done.
- Static-contract tests for the new views (node fake DOM, as existing): render list/page from payload fixtures, ETA formatting, tab routing.
- Commit: `feat(workbench): 回归/评测 tab with batch list and batch page`.

### Task 5: docs

- CLAUDE.md: regression command → `.venv/bin/python python/run_batch.py --name regression --kind regression --games 5 --chars all` (+ how to read `Completed` per lane from the summary); mention `logs/batches/` is kept.
- Commit: `docs: regression via run_batch.py`.

### Task 6 (controller): restart workbench, run a real small batch (e.g. smoke, 1 game x 2 chars) when no other batch is running, verify in agent-browser: tab, list, progress, ETA, lane page.

## Phase 2

### Task 7: per-game detail payload

`agent/run_workbench/game_detail.py::build_game_detail(records) -> dict` from one game log's records (reuse `replay.parse_game_progress` for rooms; read `combat_plan` replies directly from records):
- `route`: per map_select: act, floor, options [{col,row,type}], taken {col,row,type}.
- `deck`: final deck [{id,name,upgraded}] and per room `deck_delta` {added, removed, upgraded} + relics/potions gained/used, from room start/end player snapshots (multiset diff by card id + upgrade state).
- `combats`: per combat room, per player turn: hand [{id,name,cost,playable,target_type}], energy, block, enemies [{name,hp,max_hp,block,intents,powers}], `legal_actions` (playable cards with cost ≤ energy and their targets; usable potions; end_turn), `executed` (the commands sent this turn in order, each with the resolved card/target names), `solver_plans` (each `combat_plan` reply for this turn: planned actions for the CURRENT turn only, projected_player_hp, all_enemies_dead, only_death_routes_found, search{...}), `diverged` markers: a re-plan within the same turn, a `~~`-style stale return (an executed sequence shorter than the plan's turn), or an executed card different from the plan's next card.
- `choices`: card_reward (offered cards, picked/skipped), shop (stock + bought/removed), rest_site (options + chosen + upgraded card), event (options + chosen).
- Endpoint `GET /api/game-detail?source=<source_id>` (and by run_id).
- Tests (tests/agent/run_workbench/test_game_detail.py) on small hand-built fixture logs + one trimmed real log (copy a few rooms of a real play_full_run log into tests/fixtures/): route, deck diff incl. upgrades, legal actions (unplayable / too expensive excluded), plan↔turn alignment, divergence markers, choices.
- Commit: `feat(workbench): per-game detail payload (route, deck, combat, choices)`.

### Task 8: per-game detail page

- `game-detail.js` + route `#/regression/game/<source>`; linked from the lane runs table and the run detail page. Sections: summary (seed, character, policy, outcome, floor), route strip (per floor: taken node + other options greyed), deck (final + per-room deltas), rooms list → each combat expandable per turn: hand, energy, enemies+intents, legal actions with the taken ones highlighted in order, solver plan(s) with projected HP / search stats, divergence badges; out-of-combat rooms show offered vs chosen.
- Static-contract tests with a fixture payload.
- Commit: `feat(workbench): per-game detail page for regression games`.

### Task 9 (controller): restart, real-browser verification on finished batch games (a win and a death, one with a stale-plan re-plan), then report.
