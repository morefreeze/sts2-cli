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
