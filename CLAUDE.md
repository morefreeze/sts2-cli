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

## Localization

- Always use the game's official Chinese translations (from `localization_zhs/`)
- Never invent translations — look them up
- All user-facing strings must go through `t(en, zh)` for bilingual support
- Template variables like `{Damage}`, `{Block}`, `{MaxHp}` must be resolved to actual values before display

## Build

```bash
# Build all projects
dotnet build src/Sts2Headless/Sts2Headless.csproj
dotnet build src/Sts2CliMod/Sts2CliMod.csproj

# Or build solution
dotnet build sts2-cli.sln
```

## Key Architecture

- `src/Sts2HeadlessCore/Core/RunSimulator.cs` — Core game lifecycle, decision point detection, state serialization
- `src/Sts2Headless/RunSimulator.cs` — Headless-mode simulator (uses Core)
- `src/Sts2Headless/Program.cs` — JSON command router for headless mode
- `src/Sts2CliMod/` — In-game mod with embedded HTTP server
- `src/Sts2CliMod/Server/EmbeddedServer.cs` — HTTP server for command execution
- `src/Sts2CliMod/Hooks/ModHooks.cs` — Game event hooks for state streaming
- `src/GodotStubs/` — replacement GodotSharp.dll (no-op Godot types)
- `python/play.py` — interactive terminal player
- `python/play_full_run.py` — batch testing tool
- `python/sts2_mod_client.py` — Python client for in-game mod
- `lib/` — game DLLs (not in repo, copied by setup.sh)
- `localization_eng/`, `localization_zhs/` — bilingual loc data

## Sts2CliMod Installation

The mod allows running the game programmatically via HTTP commands while the game is running.

### Installation Steps

1. **Build the mod:**
   ```bash
   dotnet build src/Sts2CliMod/Sts2CliMod.csproj
   ```
   This copies the mod DLL to `mods/Sts2CliMod/`.

2. **Verify mod files:**
   ```bash
   ls -la mods/Sts2CliMod/
   # Should show: Sts2CliMod.dll, manifest.json
   ```

3. **Launch game** — The mod will automatically start on game load and listen on port 12580.

### Using the Mod

#### Python Client

```bash
# Check mod health
python3 python/sts2_mod_client.py --health

# Start a new run
python3 python/sts2_mod_client.py --cmd '{"cmd": "start_run", "character": "Ironclad", "ascension": 0}'

# Interactive mode
python3 python/sts2_mod_client.py
```

#### HTTP API

```bash
# Health check
curl http://localhost:12580/health

# Start run
curl -X POST http://localhost:12580/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd": "start_run", "character": "Ironclad", "ascension": 0}'

# Get map state
curl -X POST http://localhost:12580/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd": "get_map"}'

# Execute action
curl -X POST http://localhost:12580/api/command \
  -H "Content-Type: application/json" \
  -d '{"cmd": "action", "action": "select_map_node", "args": {"col": 0, "row": 0}}'

# Get current state
curl http://localhost:12580/state
```

### Available Commands

- `start_run` — Start a new run (params: character, ascension, seed, lang)
- `get_map` — Get current map state
- `get_state` — Get current decision point state
- `action` — Execute an action (params: action, args)

### Actions

- `select_map_node` — Navigate to map node (args: col, row)
- `play_card` — Play a card (args: card_index, target_index)
- `end_turn` — End current turn
- `choose_option` — Choose event option (args: option_index)
- `select_card_reward` — Select card reward (args: card_index)
- `skip_card_reward` — Skip card reward selection
- `buy_card` — Buy card from shop (args: card_index)
- `buy_relic` — Buy relic from shop (args: relic_index)
- `buy_potion` — Buy potion from shop (args: potion_index)
- `remove_card` — Remove card at rest site
- `use_potion` — Use potion (args: potion_index, target_index)
- `leave_room` — Leave current room
- `proceed` — Proceed to next room/state

## Development Workflow

1. Make changes to source files
2. Build: `dotnet build src/Sts2CliMod/Sts2CliMod.csproj`
3. Test: Launch game and send commands via Python client or curl
4. Debug logs available in game console/logs
5. Commit after successful testing
