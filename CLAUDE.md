# CLAUDE.md

## Testing Requirements

Any code change MUST pass a full regression test before claiming completion:

```bash
# Run 5 games per character, ALL must complete (0 crashes/stuck)
for char in Ironclad Silent Defect Regent Necrobinder; do
    STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" python3 python/play_full_run.py 5 "$char" 2>&1 | grep -E "Wins|Completed"
do
```

Expected: `Completed: 5/5` for every character.

## Setup

```bash
./setup.sh                        # pyenv venv + auto-detect Steam, copy DLLs, IL-patch, build
python3 python/play.py            # auto-setup on first run
```

**Python environment** is managed by pyenv + pyenv-virtualenv:
- `.python-version` pins the virtualenv `sts2-cli` (based on Python 3.11.12)
- `setup.sh` auto-installs pyenv, pyenv-virtualenv, Python, creates the venv, and installs dependencies
- Dependencies: `pytest` (for tests), `requirements-agent.txt` (for RL/LLM agents)

**Always use `.venv/bin/python`** for any Python invocation in this repo (training, eval, coordinator, scripts). The system `python3` lacks the project deps (`tensorboard`, `sb3-contrib`, etc.) and will fail with `ModuleNotFoundError`. Either `source .venv/bin/activate` once per shell or invoke as `.venv/bin/python ...` directly.

### Build
```bash
dotnet build src/Sts2Headless/Sts2Headless.csproj
```

### Tests
```bash
pytest tests/                     # all tests (requires game DLLs in lib/)
pytest tests/test_combat.py -k "test_name"  # single test
```

Tests use a `Game` fixture (`tests/conftest.py`) that wraps the headless C# process via subprocess. Each test gets an independent game process.

### Interactive Play
```bash
python3 python/play.py             # interactive (Chinese default)
python3 python/play.py --lang en   # interactive (English)
python3 python/play.py --auto      # auto-play with simple AI
python3 python/play.py --character Silent
```

### JSON Protocol (for AI agents)
```bash
dotnet run --project src/Sts2Headless/Sts2Headless.csproj
# Send JSON commands via stdin
```

### HTTP Bridge
```bash
python3 agent/sts2_bridge.py 9876 --compact --log /tmp/game.jsonl
# Replay to a specific step, then continue interactively:
python3 agent/sts2_bridge.py replay /tmp/game.jsonl --until 42 --port 9876
```

### Automated full run (testing)
```bash
python3 python/play_full_run.py --seed test_seed --character Ironclad
```

## Scratch / Temp Scripts

One-off scripts (perf probes, debugging, experiments) go to `/tmp/sts2-cli/`, NOT the repo root. Only promote to the repo after the script has been used repeatedly and proven useful.

```bash
mkdir -p /tmp/sts2-cli
# write throwaway scripts here, e.g. /tmp/sts2-cli/test_speed.py
```

Rationale: keeps the repo clean of dead scratch files; promotion is an explicit signal that something deserves version control.

## Architecture

```
Your Code (Python / JS / LLM)
    │  JSON stdin/stdout  OR  HTTP (sts2_bridge.py)
    ▼
src/Sts2Headless (C# .NET 9)    ← Program.cs + RunSimulator.cs
    │  Harmony IL patches
    ▼
lib/sts2.dll (game engine, IL-patched)
  + src/GodotStubs (replaces GodotSharp.dll)
```

**Key design choices:**
- **Synchronous execution**: Harmony patches replace `Task.Yield`/`Cmd.Wait()` calls in the game engine with no-ops, allowing the async game loop to run synchronously.
- **`InlineSynchronizationContext`**: Custom `SynchronizationContext` that posts continuations back to the main thread inline, enabling single-threaded headless operation.
- **Bilingual output**: Every card, relic, enemy, and event name is returned as `{"en": "...", "zh": "..."}` using JSON lookup tables in `localization_eng/` and `localization_zhs/`.

## Key Files

- **`src/Sts2Headless/RunSimulator.cs`** (2,900+ lines) — The core. Contains the game state machine, all decision-point detection, all action handlers (`DoPlayCard`, `DoEndTurn`, `DoMapSelect`, etc.), Harmony patches, and `LocLookup`. Nearly all game logic changes go here.
- **`src/Sts2Headless/Program.cs`** — Entry point; parses stdin JSON and dispatches to `RunSimulator`.
- **`python/play.py`** — Interactive terminal UI with auto-setup, display formatting, and command parsing.
- **`agent/sts2_bridge.py`** — HTTP bridge that wraps the C# process; supports compact JSON mode, game logging (JSONL), and replay.
- **`agent/coordinator.py`** — Multi-agent coordination layer for orchestrating LLM/RL agents.
- **`agent/bug.md`** — Active bug tracker. Check here before fixing anything—many issues have known workarounds or are already tracked.
- **`src/GodotStubs/`** — Minimal Godot API stubs (no-op implementations of Node, Vector2, UI components, etc.) that let the game engine compile and run without Godot.

## JSON Protocol

Commands sent via stdin, one JSON object per line:

```json
{"cmd": "start_run", "character": "Ironclad", "seed": "test", "ascension": 0}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "action", "action": "end_turn"}
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}
{"cmd": "action", "action": "skip_card_reward"}
{"cmd": "quit"}
```

Decision point types returned: `map_select`, `combat_play`, `card_reward`, `card_select`, `rest_site`, `event_choice`, `shop`, `bundle_select`, `treasure`, `game_over`.

A typical run starts with a Neow `event_choice` — agents must handle this initial event before reaching `map_select`.

## Bug Fix Workflow

1. Reproduce via replay: `python3 agent/sts2_bridge.py replay <logfile> --until <step>`
2. Fix in `RunSimulator.cs`, rebuild with `dotnet build src/Sts2Headless/Sts2Headless.csproj`
3. Verify replay no longer triggers bug
4. Update `agent/bug.md`

## Localization

- Always use the game's official Chinese translations (from `localization_zhs/`)
- Never invent translations — look them up
- All user-facing strings must go through `t(en, zh)` for bilingual support
- Template variables like `{Damage}`, `{Block}`, `{MaxHp}` must be resolved to actual values before display

## Build

```bash
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj
```

## Key Architecture

- `src/Sts2Headless/RunSimulator.cs` — game lifecycle, decision point detection, state serialization
- `src/Sts2Headless/Program.cs` — JSON command router
- `src/GodotStubs/` — replacement GodotSharp.dll (no-op Godot types)
- `python/play.py` — interactive terminal player
- `python/play_full_run.py` — batch testing tool
- `lib/` — game DLLs (not in repo, copied by setup.sh)
- `localization_eng/`, `localization_zhs/` — bilingual loc data

## Conventions

- **Progress notation — `A<act>F<floor>a<ascension>`**: e.g. `A2F12a10` is act 2, floor 12, ascension 10. **Case carries the meaning**: upper-case `A` is the act, lower-case `a` is the ascension. Acts and floors are 1-based (`global_floor 41` → `A3F7`, i.e. `((floor-1)//17)+1`). Emit only the parts you actually have — a training batch has an ascension but no act or floor, so it is labelled `a0`, or `a?` when the ascension was never recorded. Never write a bare `A0` for an ascension; it reads as an act.
- **Event completion**: trust `localEvent.IsFinished`. Never gate on "option count unchanged after a choice" — events legitimately loop on the same page (Slippery Bridge Hold On) and post-selection continuations (heal/enchant/add-card) often run on the same options page; force-finishing kills them silently.
- **Async selection continuations**: any path whose effect can open a card_select / card_reward / bundle (event option, shop relic pickup, etc.) must run on `Task.Run(...)` and yield as soon as `_cardSelector.HasPending` / `HasPendingReward` / `_pendingBundles != null` appears. The Task completes naturally once the external `select_cards` feeds the selector's TCS. Reference shape: `DoChooseOption`, `DoBuyRelic`.
- **DynamicVar preview during serialization**: `UpdateDynamicVarPreview` mutates the live card. Bracket reads with `ClearPreview` **before and after** — leaving the card in preview state corrupts subsequent play actions (Momentum Strike `PlayCardAction` failure).

## Protocol notes

- `card_select` decision uses key `cards` (not `options`) and action `select_cards` with comma-separated `indices`.
- `AnyEnemy` cards/potions require `target_index` when ≥2 enemies are alive; with a single alive enemy the adapter auto-targets.
- `plan_combat_turn` action (at a `combat_play` decision) returns a multi-turn plan from the ported Combat Solver engine (`src/Sts2Headless/CombatSolverEngine/`, vendored — see its `VENDORED.md`; never hand-edit it). Unlike every other action in this file, its actions address things by **stable identity, not position**: cards by `card_id`/`card_occurrence` (not hand `card_index`), enemies by `target_combat_id` (matches the `combat_id` field on each enemy in the `combat_play` decision; positional `target_index` goes stale as soon as an earlier action kills an enemy), potions by `potion_id` (matches each potion's `id`; the plan's `potion_slot` is the physical belt slot, which differs from `potion_index` once any earlier slot is used). `python/combat_plan_driver.py` resolves all of these against the live decision — reuse it rather than re-deriving indices. **Only execute the actions up through the plan's first `end_turn`, then re-call `plan_combat_turn` fresh** — the plan's later turns assume draws and intents the protocol does not expose. If a planned card is not in the live hand (the solver's prediction drifted), stop and re-plan; that is expected, not an error.
- `plan_combat_turn` costs ~3.4 s median / ~9 s mean per call at the default 120 s soft budget (`SolverSearchProfile.Default` — the only profile; 8 threads, one process) versus ~0.01 s for the heuristic, so a solver-driven game takes minutes. `STS2_SOLVER_BUDGET` selects the budget: **only `30`/`60`/`120`/`180`/`300` (seconds)**, unset = 120; anything else warns in the engine and raises in `play_full_run.py` before a game starts, and `play_full_run.py` fails a run if the engine reports a different budget (or none — a stale build). Measured (`docs/superpowers/plans/2026-09-23-combatsolver-phase2b1-budget-tiers.md`): 30 s is non-inferior to 120 s for Ironclad/Silent/Defect/Necrobinder and cuts batch solve time 13–55% (Regent still undecided); 300 s buys nothing, because longer searches stop at the 120 000-node cap (`MaxExpandedNodes`) instead. Every `combat_plan` reply carries `search.{budget_ms, elapsed_ms, boundary, expanded_nodes, total_expanded_nodes}`; `boundary == "TimeLimit"` under-counts time-capped searches (it is the chosen line's boundary), so compare `elapsed_ms` with `budget_ms`.
- A `plan_combat_turn` call can **hang indefinitely** (`agent/bug.md` BUG-040 — reproducible: Ironclad seed `run_32` hangs at the A1F17 boss): the budget is only checked between search-node expansions. `python/play_full_run.py` guards against it through `python/engine_process.py`: every engine reply has a deadline (`plan_combat_turn`: budget + 60 s; anything else: 120 s), and a miss kills the engine's whole process group (the real engine is a child of `dotnet run`) and records the game as `HANG` (paired_eval status `stuck`). **`tests/conftest.py`'s Game fixture and `agent/sts2_bridge.py` start the engine without this protection.**
- `python/play_full_run.py` routes combat through `plan_combat_turn` for the characters in `STS2_SOLVER_CHARS` (comma list / `all` / `none`; default all five — a paired A/B measured +4.7..+10.5 floors on every character, see `docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md`). `STS2_SOLVER_CHARS=none` is the heuristic control arm; an unknown name raises rather than silently disabling the solver. `STS2_SOLVER_THREADS` pins the solver's search threads (default 8 on an 18-core machine) — set it when running several solver processes at once, because a time-budgeted search starved of CPU plays measurably worse. The SUMMARY prints per-run `solver=<plans>/<attempts>` and a `SOLVER NEVER ENGAGED` banner if a solver-enabled character got zero plans, since a batch that silently fell back to the heuristic still reports `Completed: N/N`. `--results-log <file>` writes `agent/paired_eval.py` input rows.
- The engine's `floor` field is **act-local** (resets to 1 each act). Anything comparing runs across acts must use the global floor `(act - 1) * 17 + floor` (`agent/eval_rl.py` `global_floor_from_state`). `play_full_run.py`'s `avg_floor` was act-local until a6472da — older numbers only compare between runs that died in the same act.
