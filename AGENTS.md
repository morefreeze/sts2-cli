# AGENTS.md - Agent Coding Guidelines

This project is a CLI for Slay the Spire 2, running the game engine headless via .NET C# with Python wrapper scripts.

## Build Commands

```bash
# Build the main C# project
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj

# Run the headless simulator (JSON protocol)
~/.dotnet-arm64/dotnet run --project src/Sts2Headless/Sts2Headless.csproj

# Interactive terminal player (auto-sets up on first run)
python3 python/play.py                        # Chinese
python3 python/play.py --lang en              # English

# Full regression test (REQUIRED before claiming completion)
for char in Ironclad Silent Defect Regent Necrobinder; do
    STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" \
    python3 python/play_full_run.py 5 "$char" 2>&1 | grep -E "Wins|Completed"
done
```

**Expected test result:** `Completed: 5/5` for every character with 0 crashes/stuck.

## Code Style Guidelines

### General
- **Language:** C# with .NET 9, ImplicitUsings enabled, Nullable reference types enabled
- **TargetFramework:** net9.0 with RollForward LatestMajor
- **Formatting:** Standard Visual Studio / dotnet format defaults
- **No trailing whitespace**

### Naming Conventions
- **Classes/Methods:** PascalCase (e.g., `RunSimulator`, `ExecuteAction`)
- **Private fields:** _camelCase with underscore prefix (e.g., `_queue`, `_executing`)
- **Properties:** PascalCase
- **Constants:** PascalCase
- **Interfaces:** I-prefix (e.g., `ISomething`)

### Imports
- Use explicit `using` statements (no global usings except implicit)
- Group: System → Third-party → Project (MegaCrit.* for game DLL, Sts2Headless for local)
- Sort alphabetically within groups

Example:
```csharp
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using HarmonyLib;
using Sts2Headless;
```

### Types
- Use nullable reference types (`string?`, `T?`) when value can be null
- Prefer `var` for type inference when obvious
- Use collection interfaces (`IEnumerable<T>`, `List<T>`, `Dictionary<TKey, TValue>`)
- Use `record` for immutable data types

### Error Handling
- Use try-catch for recoverable errors
- Log errors to stderr with clear messages
- Catch specific exception types when possible
- Unhandled exceptions: use `AppDomain.CurrentDomain.UnhandledException` and `TaskScheduler.UnobservedTaskException`

### JSON Protocol
- PropertyNamingPolicy: SnakeCaseLower
- DefaultIgnoreCondition: WhenWritingNull
- Commands: `start_run`, `action`, `get_map`, `set_player`, `enter_room`, `set_draw_order`, `quit`
- Responses contain `type` field with decision points: `map_select`, `combat_play`, `card_reward`, `rest_site`, `event_choice`, `shop`, `game_over`, `ready`, `error`

### Localization
- Use bilingual support via `LocLookup` class
- Always use game's official Chinese translations from `localization_zhs/`
- Never invent translations — look them up
- Template variables like `{Damage}`, `{Block}`, `{MaxHp}` must be resolved before display

### Architecture
- `src/Sts2Headless/Program.cs` — JSON command router, stdin/stdout
- `src/Sts2Headless/RunSimulator.cs` — game lifecycle, decision point detection
- `src/GodotStubs/` — replacement GodotSharp.dll (no-op Godot types)
- `python/play.py` — interactive terminal player
- `python/play_full_run.py` — batch testing tool
- `lib/` — game DLLs (not in repo, copied by setup.sh)
- `localization_eng/`, `localization_zhs/` — bilingual loc data

## Important Notes

1. **Game DLLs not in repo:** The `lib/` directory contains game DLLs (sts2.dll, etc.) that must be copied from Steam via `./setup.sh`
2. **STS2_GAME_DIR:** Set this env var to the game's data directory for standalone operation
3. **Regression Testing:** Any code change MUST pass the full regression test before claiming completion
4. **Bug Reports:** Attach logs from `logs/` directory (JSONL files with full game state)

## File Structure

```
src/
  Sts2Headless/
    Sts2Headless.csproj    # Main project
    Program.cs             # JSON stdin/stdout router
    RunSimulator.cs        # Game lifecycle (3168 lines)
  GodotStubs/
    GodotStubs.csproj      # Stub project (outputs GodotSharp.dll)

python/
  play.py                  # Interactive terminal player
  play_full_run.py         # Batch testing tool
  game_log.py             # Logging utilities

lib/                       # Game DLLs (populated by setup.sh)
localization_eng/         # English translations
localization_zhs/         # Chinese translations
logs/                     # Game run logs (auto-generated)
```