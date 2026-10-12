# Workbench: regression/eval view (batches, progress/ETA, per-game detail) — design

Date: 2026-10-12. Approved by the user in chat. Branch `feat/deterministic-planner`.

## Goal

The workbench (http://localhost:8765, `agent/run_progress_viewer.py` + `agent/run_workbench/`) shows
training (RL) and regression/eval (`python/play_full_run.py`) games mixed together. The user needs a
separate regression/eval view with: overall batch progress and ETA, and for every finished game the
route choices, the full deck and how it evolved, and per-combat detail with legal (candidate) actions
and the solver's plan.

## Decisions (user)

- Candidate actions = the legal actions at each step (playable hand cards with cost/target, usable
  potions, end turn) + the solver's plan for the turn. The solver's rejected alternative lines are
  OUT of scope (no engine telemetry change).
- Training-game display stays unchanged.

## 1. Batch identity and plan (harness)

- New launcher `python/run_batch.py`: `run_batch.py --name NAME --kind regression|ab|smoke
  --games N --chars all|Ironclad,... [--max-lanes 6] [--threads 3] [--ascension A]
  [--seed-prefix P] [--env KEY=VAL ...] [-- extra play_full_run args]`. It creates
  `logs/batches/<batch_id>/` (`batch_id` = `<name>-<YYYYmmdd-HHMMSS>`), writes `manifest.json`
  (batch_id, name, kind, created_at, git commit, argv, env overrides, lanes: [{lane, character,
  policy env, planned_games, seed_prefix, seed_offset}]), and starts one `play_full_run.py` lane per
  character (at most `--max-lanes` at once, the rest queued) with `--keep-game-logs
  logs/batches/<batch_id>/games` and `--results-log logs/batches/<batch_id>/<lane>.jsonl`, lane
  stdout in `<lane>.log`. It records each lane's pid and exit status into `lanes_status.json`
  (updated as lanes start/finish). Stopping lanes uses SIGTERM (SIGINT is ignored by background jobs).
- `play_full_run.py` stamps on each game's `run_meta`: `harness: "play_full_run"`, `batch_id`,
  `lane`, `game_index` (1-based), `games_total` (from `--batch-id`/`--lane` args or env
  `STS2_BATCH_ID`/`STS2_BATCH_LANE`; absent when not run by the launcher).
- `logs/batches/` is exempt from `game_log.cleanup_old_logs()` (batch evidence must not be purged).
- CLAUDE.md's regression command becomes `python/run_batch.py --name regression --kind regression
  --games 5 --chars all`.

## 2. Regression tab (workbench)

- Two top-level tabs: 训练 (training — today's tree, unchanged) and 回归/评测 (regression/eval).
  A run is regression/eval when its `run_meta.harness == "play_full_run"`, or (legacy) its
  experiment matches `ooc=` / `<batch>:ooc=` and it has no checkpoint.
- Batch list (from `logs/batches/*/manifest.json`, newest first; legacy play_full_run runs without
  a manifest are grouped by experiment as "未登记批次"): name, kind, start time, progress
  (finished / planned games), live games, technical failures, ETA.
- ETA: per lane, remaining games x median duration of that lane's finished games (fallback: the
  batch median; before any game finishes: "—"); batch ETA = max over lanes (lanes run in
  parallel), shown as an absolute time and "约 X 小时".
- Batch page: lane table (character, policy, progress bar, avg global floor, wins, technical,
  ETA); clicking a lane shows its game list (finished + in progress, reusing the runs table and the
  in-progress badge). Statistics never include in-progress runs (existing rule).

## 3. Per-game detail (regression games)

- Route: per map node, the options offered (`map_select.choices`) vs the one taken.
- Deck: final deck (with upgrades) and, per room, cards gained / removed / upgraded, relics and
  potions gained / used, computed from the room's start/end player snapshots.
- Combat (per room, per turn): hand (name, cost, playable), energy, block, enemies (hp, block,
  intents, powers); for each step the legal actions (playable cards with cost/target, usable
  potions, end turn) with the taken one highlighted; the solver plan for the turn (from the
  `combat_plan` reply: planned actions in order, projected player HP, all_enemies_dead, search
  budget/elapsed/boundary/nodes) and a "plan diverged" marker where the harness re-planned
  (stale-plan refusal/unresolved card) or the executed action differs from the plan.
- Out of combat: card reward (offered vs picked/skipped), shop (stock vs bought/removed), rest site
  (options vs chosen, upgraded card), event (options vs chosen).
- Built on the existing replay parser (`agent/run_workbench/replay.py`, `/api/log`) and the
  existing `/replay` page; the run detail page links to it.

## Testing / verification

- Unit tests for the launcher (manifest, lane queueing with fake play_full_run), run_meta stamping,
  ETA math, regression/training classification, deck-diff and plan extraction on fixture logs.
- Golden check: training views' payloads unchanged.
- Real-browser verification (agent-browser) on a real batch before reporting done.

## Phases

1. Launcher + manifest + run_meta + cleanup exemption + regression tab (batch list, batch page,
   progress/ETA).
2. Per-game detail (route, deck evolution, combat legal actions + solver plan, out-of-combat
   choices).
