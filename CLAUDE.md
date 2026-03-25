# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Codebase Overview

`sts2-cli` runs the real Slay the Spire 2 game engine headless in a terminal, with all damage, card effects, enemy AI, relics, and RNG identical to the actual game. The primary use case is programmatic control via a JSON protocol for AI agents, RL research, and automated gameplay.

## Common Commands

### Setup
```bash
./setup.sh                        # auto-detect Steam, copy DLLs, IL-patch, build
python3 python/play.py            # auto-setup on first run
```

### Build
```bash
dotnet build Sts2Headless/Sts2Headless.csproj
```

### Interactive Play
```bash
python3 python/play.py             # interactive (Chinese default)
python3 python/play.py --lang en   # interactive (English)
python3 python/play.py --auto      # auto-play with simple AI
python3 python/play.py --character Silent
```

### JSON Protocol (for AI agents)
```bash
dotnet run --project Sts2Headless/Sts2Headless.csproj
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
Sts2Headless (C# .NET 9)       ← Program.cs + RunSimulator.cs
    │  Harmony IL patches
    ▼
sts2.dll (game engine, IL-patched)
  + GodotStubs (replaces GodotSharp.dll)
```

**Key design choices:**
- **Synchronous execution**: Harmony patches replace `Task.Yield`/`Cmd.Wait()` calls in the game engine with no-ops, allowing the async game loop to run synchronously.
- **`InlineSynchronizationContext`**: Custom `SynchronizationContext` that posts continuations back to the main thread inline, enabling single-threaded headless operation.
- **Bilingual output**: Every card, relic, enemy, and event name is returned as `{"en": "...", "zh": "..."}` using JSON lookup tables in `localization_eng/` and `localization_zhs/`.

## Key Files

- **`Sts2Headless/RunSimulator.cs`** (2,900+ lines) — The core. Contains the game state machine, all decision-point detection (`MapSelectState`, `CombatPlayState`, `CardRewardState`, `RestSiteState`, `ShopState`, `EventChoiceState`), all action handlers (`DoPlayCard`, `DoEndTurn`, `DoMapSelect`, etc.), Harmony patches, and `LocLookup`.
- **`Sts2Headless/Program.cs`** — Entry point; parses stdin JSON and dispatches to `RunSimulator`.
- **`python/play.py`** — Interactive terminal UI with auto-setup, display formatting, and command parsing.
- **`agent/sts2_bridge.py`** — HTTP bridge that wraps the C# process; supports compact JSON mode, game logging (JSONL), and replay.
- **`agent/bug.md`** — Active bug tracker. Check here before fixing anything—many issues have known workarounds or are already tracked.
- **`GodotStubs/`** — Minimal Godot API stubs (no-op implementations of Node, Vector2, UI components, etc.) that let the game engine compile and run without Godot.

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

Decision point types returned: `map_select`, `combat_play`, `card_reward`, `rest_site`, `event_choice`, `shop`, `bundle_select`, `game_over`.

## Bug Fix Workflow

1. Reproduce via replay: `python3 agent/sts2_bridge.py replay <logfile> --until <step>`
2. Fix in `RunSimulator.cs`, rebuild with `dotnet build`
3. Verify replay no longer triggers bug
4. Update `agent/bug.md`

## Localization

- `localization_eng/<table>.json` and `localization_zhs/<table>.json` — ~50 tables each (cards, relics, monsters, events, powers, etc.)
- `LocLookup` in `RunSimulator.cs` provides: `_loc.Card("KEY")`, `_loc.Relic("KEY")`, `_loc.Event("KEY")`, `_loc.Bilingual(table, key)`
