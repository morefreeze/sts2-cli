# CLI Mod Integration Design

**Date**: 2026-03-31
**Status**: Draft
**Author**: Claude Code

## Overview

Integrate the existing `sts2-cli` headless game engine as a mod into Slay the Spire 2, enabling:
- Automatic command-line window when game launches
- Real-time game state display at decision points
- Dual input: mouse (GUI) and command-line, fully equivalent
- RL training support via CLI interface

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Game Process (sts2.exe / SlayTheSpire2.app)      │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  Sts2CliMod (DLL) - Loaded by game's mod system                │ │
│  │                                                                  │ │
│  │  ┌────────────────────────────┐  ┌──────────────────────────┐  │ │
│  │  │  Game Hooks                │  │  Embedded HTTP Server    │  │ │
│  │  │  ─────────────────────     │  │  ────────────────────    │  │ │
│  │  │  - ModInitializer entry    │  │  - Listen on localhost   │  │ │
│  │  │  - Subscribe to hooks      │  │  - HTTP/WebSocket API    │  │ │
│  │  │  - Capture decision points │  │  - JSON request/response │  │ │
│  │  │  - Push state updates      │  │  - Client management     │  │ │
│  │  │  - Execute commands        │  │  - Mutual exclusion lock │  │ │
│  │  └────────────────────────────┘  └──────────────────────────┘  │ │
│  │                    │                              │             │ │
│  │                    └──────────────┬───────────────┘             │ │
│  │                                   ↓                             │ │
│  │  ┌────────────────────────────────────────────────────────────┐ │ │
│  │              Sts2HeadlessCore (Shared Library)                 │ │
│  │  ────────────────────────────────────────────────────────────  │ │
│  │  - RunSimulator core logic (extracted from existing code)      │ │
│  │  - State serialization (DecisionPoint, GameState, etc.)        │ │
│  │  - Command parsing and execution                              │ │
│  │  - Localization (bilingual support)                            │ │
│  │  - Input locking (mouse vs CLI mutual exclusion)               │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
                              ↕ HTTP/WebSocket
┌─────────────────────────────────────────────────────────────────────┐
│                    CLI Client Process (Independent)                 │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  sts2-cli-client (Python or C# executable)                     │ │
│  │  ────────────────────────────────────────────────────────────  │ │
│  │  - Auto-connect to Mod's HTTP server on startup                │ │
│  │  - Receive and display game state updates                      │ │
│  │  - Read user input and send commands                           │ │
│  │  - Provide REPL interface (interactive prompt)                  │ │
│  │  - Support script mode for RL training                         │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Directory Structure

```
sts2-cli/
├── docs/
│   └── superpowers/specs/
│       └── 2026-03-31-cli-mod-design.md    # This file
│
├── src/
│   ├── Sts2HeadlessCore/                   # NEW: Shared core library
│   │   ├── Sts2HeadlessCore.csproj
│   │   ├── Core/
│   │   │   ├── RunSimulator.cs             # Extracted game lifecycle logic
│   │   │   ├── StateSerializer.cs          # State → JSON conversion
│   │   │   ├── CommandExecutor.cs          # Command parsing & execution
│   │   │   └── InputLock.cs                # Mutual exclusion for mouse/CLI
│   │   ├── Models/
│   │   │   ├── DecisionPoint.cs            # Decision point state model
│   │   │   ├── GameState.cs                # Full game state snapshot
│   │   │   └── CommandResult.cs            # Command execution result
│   │   └── Localization/
│   │       └── LocLookup.cs                # Bilingual localization
│   │
│   ├── Sts2CliMod/                         # NEW: Mod project
│   │   ├── Sts2CliMod.csproj
│   │   ├── MainFile.cs                     # Mod entry point
│   │   ├── Sts2CliMod.json                 # Mod manifest
│   │   ├── Hooks/
│   │   │   ├── ModHooks.cs                 # Game event subscribers
│   │   │   └── DecisionPointDetector.cs    # Detect when input is needed
│   │   ├── Server/
│   │   │   ├── EmbeddedServer.cs           # HTTP/WebSocket server
│   │   │   ├── ApiHandlers.cs              # HTTP endpoint handlers
│   │   │   └── ClientManager.cs            # Connected client tracking
│   │   └── project.godot                   # Godot export config
│   │
│   ├── Sts2Headless/                       # EXISTING: Standalone CLI
│   │   ├── Sts2Headless.csproj
│   │   ├── Program.cs                      # JSON command router (minimal)
│   │   └── RunSimulator.cs                 # DEPRECATED: moved to Core
│   │
│   └── GodotStubs/                         # UNCHANGED
│       ├── GodotStubs.csproj
│       └── ...
│
├── python/
│   ├── play.py                             # EXISTING: Current interactive client
│   ├── play_full_run.py                    # EXISTING: Batch testing
│   └── sts2_mod_client.py                  # NEW: Mod-aware CLI client
│
├── lib/                                    # UNCHANGED: Game DLLs
│
├── localization_eng/                       # UNCHANGED
├── localization_zhs/                       # UNCHANGED
│
├── CLAUDE.md                               # Updated with mod info
└── README.md                               # Updated with mod instructions
```

## Component Details

### 1. Sts2HeadlessCore (Shared Library)

**Purpose**: Extract all reusable game logic from `Sts2Headless` into a shared library that both the Mod and CLI client can use.

**Key Classes**:

```csharp
namespace Sts2HeadlessCore
{
    // Core game simulation logic
    public class RunSimulator
    {
        public RunState? RunState { get; }
        public Task<Dictionary<string, object?>> StartRun(string character, int ascension, string? seed, string lang);
        public Task<Dictionary<string, object?>> ExecuteAction(string action, Dictionary<string, object?>? args);
        public Dictionary<string, object?> GetFullMap();
        public Dictionary<string, object?> GetDecisionPoint();
        // ... more methods
    }

    // State serialization for JSON output
    public class StateSerializer
    {
        public static Dictionary<string, object?> SerializeDecisionPoint(DecisionContext ctx);
        public static Dictionary<string, object?> SerializeGameState(RunState runState);
        public static Dictionary<string, object?> SerializeCombatState(CombatState combat);
    }

    // Command parsing and execution
    public class CommandExecutor
    {
        public static Task<Dictionary<string, object?>> Execute(RunSimulator sim, string cmd, JsonElement args);
    }

    // Input source mutual exclusion
    public class InputLock
    {
        public enum InputSource { None, Mouse, CLI }
        public bool TryAcquire(InputSource source);
        public void Release(InputSource source);
        public InputSource CurrentOwner { get; }
    }
}
```

**Dependencies**:
- sts2.dll (game engine)
- 0Harmony.dll (for patching, if needed in core)
- System.Text.Json

---

### 2. Sts2CliMod (The Mod)

**Purpose**: Game mod that hooks into the game, runs the embedded HTTP server, and bridges game events to the CLI client.

**File Structure**:

#### MainFile.cs - Mod Entry Point

```csharp
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Modding;
using Sts2HeadlessCore;
using Sts2CliMod.Server;
using Sts2CliMod.Hooks;

namespace Sts2CliMod;

[ModInitializer(nameof(Initialize))]
public partial class MainFile : Node
{
    public const string ModId = "Sts2CliMod";
    public const int DefaultPort = 12580;

    private static EmbeddedServer? _server;
    private static ModHooks? _hooks;

    public static void Initialize()
    {
        Logger.Info("Sts2CliMod initializing...");

        // Initialize the embedded HTTP server
        _server = new EmbeddedServer(DefaultPort);
        _server.Start();

        // Subscribe to game hooks
        _hooks = new ModHooks(_server);
        _hooks.SubscribeAll();

        Logger.Info($"Sts2CliMod initialized. Server listening on port {DefaultPort}");
    }

    public override void _ExitTree()
    {
        _server?.Stop();
        _hooks?.UnsubscribeAll();
    }
}
```

#### Hooks/ModHooks.cs - Game Event Subscribers

```csharp
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Rooms;
using Sts2CliMod.Server;

namespace Sts2CliMod.Hooks;

public class ModHooks : AbstractModel
{
    private readonly EmbeddedServer _server;
    private readonly RunSimulator _simulator;

    public override bool ShouldReceiveCombatHooks => true;
    public override bool ShouldReceiveMapHooks => true;
    public override bool ShouldReceiveRoomHooks => true;

    public ModHooks(EmbeddedServer server)
    {
        _server = server;
        _simulator = new RunSimulator();
    }

    public void SubscribeAll()
    {
        ModHelper.SubscribeForRunStateHooks(ModId, rs => [this]);
        ModHelper.SubscribeForCombatStateHooks(ModId, cs => [this]);
        // Subscribe to other hook types as needed
    }

    // Called when player needs to make a choice
    public override Task BeforePlayerChoice(PlayerChoiceContext context)
    {
        var decisionPoint = StateSerializer.SerializeDecisionPoint(context);
        _server.BroadcastDecisionPoint(decisionPoint);
        return Task.CompletedTask;
    }

    // Called after state changes
    public override Task AfterCardPlayed(PlayerChoiceContext context, CardPlay cardPlay)
    {
        var state = StateSerializer.SerializeGameState(_simulator.RunState);
        _server.BroadcastStateUpdate(state);
        return Task.CompletedTask;
    }

    // Map room entered
    public override Task AfterRoomEntered(Room room)
    {
        var state = StateSerializer.SerializeGameState(_simulator.RunState);
        _server.BroadcastStateUpdate(state);
        return Task.CompletedTask;
    }

    // ... more hook overrides as needed
}
```

#### Server/EmbeddedServer.cs - HTTP/WebSocket Server

```csharp
namespace Sts2CliMod.Server;

public class EmbeddedServer
{
    private readonly int _port;
    private HttpListener? _listener;
    private readonly List<WebSocketClient> _clients = new();
    private readonly InputLock _inputLock = new();
    private readonly CancellationTokenSource _cts = new();

    public void Start()
    {
        _listener = new HttpListener();
        _listener.Prefixes.Add($"http://localhost:{_port}/");
        _listener.Start();

        // Accept connections loop
        Task.Run(AcceptConnectionsAsync);
    }

    private async Task AcceptConnectionsAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            var context = await _listener.GetContextAsync();
            _ = Task.Run(() => HandleConnectionAsync(context));
        }
    }

    private async Task HandleConnectionAsync(HttpListenerContext context)
    {
        // WebSocket upgrade for real-time updates
        if (context.Request.IsWebSocketRequest)
        {
            var ws = await context.AcceptWebSocketAsync(null);
            var client = new WebSocketClient(ws, this);
            _clients.Add(client);
            _ = Task.Run(() => client.ReceiveLoop());
        }
        // REST API for command execution
        else
        {
            await HandleHttpRequest(context);
        }
    }

    // Broadcast state to all connected clients
    public void BroadcastDecisionPoint(Dictionary<string, object?> data)
    {
        var json = JsonSerializer.Serialize(data);
        foreach (var client in _clients)
            client.Send(json);
    }

    // Execute command from CLI
    public async Task<Dictionary<string, object?>> ExecuteCommand(string cmd, JsonElement args)
    {
        // Acquire input lock
        if (!_inputLock.TryAcquire(InputLock.InputSource.CLI))
            return new Dictionary<string, object?> { ["type"] = "error", ["message"] = "Input locked by mouse" };

        try
        {
            return await CommandExecutor.Execute(_simulator, cmd, args);
        }
        finally
        {
            _inputLock.Release(InputLock.InputSource.CLI);
        }
    }
}
```

#### Sts2CliMod.json - Mod Manifest

```json
{
  "id": "Sts2CliMod",
  "name": "STS2 CLI Mod",
  "author": "sts2-cli",
  "description": "Embedded CLI server for headless play and RL training",
  "version": "0.1.0",
  "has_pck": true,
  "has_dll": true,
  "dependencies": ["BaseLib"],
  "affects_gameplay": false
}
```

#### Sts2CliMod.csproj - Build Configuration

```xml
<Project Sdk="Godot.NET.Sdk/4.5.1">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>true</ImplicitUsings>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
  </PropertyGroup>

  <!-- OS Detection - same as Minty-Spire-2 -->
  <ItemGroup>
    <!-- Game DLL references -->
    <Reference Include="0Harmony">
      <HintPath>$(Sts2DataDir)/0Harmony.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="sts2">
      <HintPath>$(Sts2DataDir)/sts2.dll</HintPath>
      <Private>false</Private>
      <Publicize>True</Publicize>
    </Reference>

    <!-- Project reference to shared core -->
    <ProjectReference Include="..\Sts2HeadlessCore\Sts2HeadlessCore.csproj" />

    <!-- NuGet packages -->
    <PackageReference Include="Alchyr.Sts2.BaseLib" Version="*" PrivateAssets="All"/>
  </ItemGroup>

  <!-- Auto-copy to mods folder on build -->
  <Target Name="CopyToModsFolderOnBuild" AfterTargets="PostBuildEvent">
    <Copy SourceFiles="$(TargetPath)" DestinationFolder="$(ModsPath)$(MSBuildProjectName)/"/>
    <Copy SourceFiles="Sts2CliMod.json" DestinationFolder="$(ModsPath)$(MSBuildProjectName)/"/>
  </Target>

  <!-- Export .pck file -->
  <Target Name="GodotPublish" AfterTargets="Publish" Condition="'$(GodotPath)' != ''">
    <Exec Command="&quot;$(GodotPath)&quot; --headless --export-pack &quot;BasicExport&quot; &quot;$(ModsPath)$(MSBuildProjectName)/$(MSBuildProjectName).pck&quot;"/>
  </Target>
</Project>
```

---

### 3. CLI Client (Python)

**Purpose**: Standalone Python script that connects to the Mod's server and provides an interactive CLI.

**File**: `python/sts2_mod_client.py`

```python
#!/usr/bin/env python3
"""
STS2 Mod CLI Client
Connects to the embedded HTTP server in Sts2CliMod and provides an interactive interface.
"""

import asyncio
import json
import sys
import websockets
from typing import Any, Dict
import aiohttp

DEFAULT_PORT = 12580
BASE_URL = f"http://localhost:{DEFAULT_PORT}"
WS_URL = f"ws://localhost:{DEFAULT_PORT}/ws"


class Sts2ModClient:
    def __init__(self):
        self.session = aiohttp.ClientSession()
        self.ws: websockets.WebSocketClientProtocol | None = None
        self.current_state: Dict[str, Any] = {}

    async def connect(self) -> bool:
        """Connect to the mod server via WebSocket."""
        try:
            self.ws = await websockets.connect(WS_URL)
            print(f"✓ Connected to Sts2CliMod on port {DEFAULT_PORT}")
            return True
        except (ConnectionRefusedError, OSError):
            print(f"✗ Failed to connect. Is the game running with Sts2CliMod loaded?")
            return False

    async def listen(self):
        """Listen for state updates from the server."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                await self.handle_state_update(data)
        except websockets.exceptions.ConnectionClosed:
            print("\n✗ Connection closed")

    async def handle_state_update(self, data: Dict[str, Any]):
        """Display incoming state updates."""
        self.current_state = data

        if data.get("type") == "decision_point":
            self.display_decision_point(data)
        elif data.get("type") == "state_update":
            # Optionally display incremental updates
            pass

    def display_decision_point(self, data: Dict[str, Any]):
        """Show current decision point with available actions."""
        print("\n" + "=" * 60)
        print(f"Room: {data.get('room_type', 'Unknown')}")
        print(f"HP: {data.get('hp', '?')} | Gold: {data.get('gold', '?')}")
        print("=" * 60)

        choices = data.get("choices", [])
        if choices:
            print("\nAvailable choices:")
            for i, choice in enumerate(choices, 1):
                print(f"  {i}. {choice.get('label', choice.get('id'))}")
        print()

    async def send_command(self, cmd: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Send a command to the server."""
        payload = {"cmd": cmd, **(args or {})}
        async with self.session.post(f"{BASE_URL}/api/command", json=payload) as resp:
            return await resp.json()

    async def interactive_loop(self):
        """Run the interactive command loop."""
        print("Interactive CLI ready. Type 'help' for commands.")

        while True:
            try:
                line = input("sts2> ").strip()
                if not line:
                    continue

                parts = line.split(maxsplit=1)
                cmd = parts[0]
                args_str = parts[1] if len(parts) > 1 else ""

                result = await self.send_command(cmd, {"args": args_str})
                self.display_command_result(result)

            except EOFError:
                print("\nExiting...")
                break
            except KeyboardInterrupt:
                print("\nUse 'quit' to exit.")
            except Exception as e:
                print(f"Error: {e}")

    def display_command_result(self, result: Dict[str, Any]):
        """Display command execution result."""
        if result.get("type") == "error":
            print(f"Error: {result.get('message')}")
        elif result.get("type") == "state":
            # Show state info
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(result, ensure_ascii=False))

    async def run(self):
        """Main entry point."""
        if not await self.connect():
            return

        # Run listener and interactive loop concurrently
        await asyncio.gather(
            self.listen(),
            self.interactive_loop()
        )


async def main():
    client = Sts2ModClient()

    # Optional: Auto-start a command from arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--watch":
            # Watch-only mode, no interactive input
            if await client.connect():
                await client.listen()
        else:
            # Execute single command
            await client.connect()
            result = await client.send_command(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else None)
            print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        # Interactive mode
        await client.run()


if __name__ == "__main__":
    asyncio.run(main())
```

---

### 4. Input Lock (Mutual Exclusion)

**Purpose**: Ensure mouse and CLI input don't conflict.

**Implementation**:

```csharp
public class InputLock
{
    public enum InputSource { None, Mouse, CLI }

    private InputSource _owner = InputSource.None;
    private readonly SemaphoreSlim _semaphore = new(1, 1);

    public bool TryAcquire(InputSource source, int timeoutMs = 5000)
    {
        if (!_semaphore.Wait(timeoutMs))
            return false;

        if (_owner != InputSource.None && _owner != source)
        {
            _semaphore.Release();
            return false;
        }

        _owner = source;
        return true;
    }

    public void Release(InputSource source)
    {
        if (_owner == source)
        {
            _owner = InputSource.None;
            _semaphore.Release();
        }
    }

    public InputSource CurrentOwner => _owner;
}
```

**Integration Points**:

- **Mouse input**: Patch game's input handling to acquire lock on first interaction
- **CLI input**: Mod's `ExecuteCommand` acquires lock before executing

---

## Communication Protocol

### WebSocket Messages (Server → Client)

```json
// Decision point (when player needs to act)
{
  "type": "decision_point",
  "room_type": "combat|event|rest|shop|map",
  "hp": 80,
  "max_hp": 100,
  "gold": 150,
  "choices": [
    { "id": "play_card_0", "label": "Strike", "cost": 1, "damage": 6 },
    { "id": "play_card_1", "label": "Defend", "cost": 1, "block": 5 },
    { "id": "end_turn", "label": "End Turn" }
  ],
  "hand": [
    { "id": "strike", "cost": 1, "damage": 6, "upgraded": false },
    { "id": "defend", "cost": 1, "block": 5, "upgraded": false }
  ],
  "enemies": [
    { "name": "Jaw Worm", "hp": 42, "max_hp": 44, "intent": "attack 11" }
  ]
}

// State update (after actions)
{
  "type": "state_update",
  "hp": 74,
  "block": 5,
  "hand_count": 4,
  "draw_pile_count": 3,
  "discard_pile_count": 2
}
```

### HTTP API (Client → Server)

```http
POST /api/command
Content-Type: application/json

{
  "cmd": "action",
  "args": {
    "action": "play_card",
    "card_index": 0,
    "target": 0
  }
}

Response:
{
  "type": "success",
  "state": { ... }
}
```

**Supported Commands** (same as existing CLI):

| Command | Description | Args |
|---------|-------------|------|
| `start_run` | Start a new run | `character`, `ascension`, `seed`, `lang` |
| `action` | Execute an action | `action`, `card_index`, `target`, etc. |
| `get_map` | Get full map state | - |
| `set_player` | Modify player stats | `hp`, `gold`, `max_hp`, etc. |
| `enter_room` | Enter specific room | `type`, `encounter`, `event` |
| `set_draw_order` | Set draw pile order | `cards` (array) |

---

## Build and Deployment

### Build Steps

1. **Build Sts2HeadlessCore**:
   ```bash
   cd src/Sts2HeadlessCore
   ~/.dotnet-arm64/dotnet build
   ```

2. **Build Sts2CliMod**:
   ```bash
   cd src/Sts2CliMod
   ~/.dotnet-arm64/dotnet build
   # Automatically copies to mods/ folder
   ```

3. **Install BaseLib dependency** (if not already installed):
   ```bash
   # The build script automatically copies BaseLib to mods/
   ```

### Installation

1. Ensure game directory structure:
   ```
   Slay the Spire 2/
   ├── SlayTheSpire2.app/
   │   └── Contents/
   │       └── MacOS/
   │           └── mods/
   │               ├── BaseLib/
   │               │   ├── BaseLib.dll
   │               │   ├── BaseLib.pck
   │               │   └── BaseLib.json
   │               └── Sts2CliMod/
   │                   ├── Sts2CliMod.dll
   │                   ├── Sts2CliMod.pck
   │                   └── Sts2CliMod.json
   ```

2. Launch game normally (via Steam or directly)

3. Mod automatically:
   - Initializes on load
   - Starts HTTP server on localhost:12580
   - Begins streaming game state

### Running the CLI Client

```bash
# Interactive mode
python3 python/sts2_mod_client.py

# Watch-only mode
python3 python/sts2_mod_client.py --watch

# Single command
python3 python/sts2_mod_client.py get_map
```

---

## Testing Strategy

### Unit Tests

- `StateSerializer` serialization/deserialization
- `InputLock` concurrent access
- `CommandExecutor` command parsing

### Integration Tests

1. **Mod Loading**: Verify mod loads without errors
2. **Server Startup**: Confirm HTTP server starts on correct port
3. **State Streaming**: Verify decision points are pushed to client
4. **Command Execution**: Test all CLI commands work through the API
5. **Input Locking**: Test mouse/CLI mutual exclusion

### Regression Tests

Use existing `play_full_run.py` to verify full runs complete successfully:

```bash
for char in Ironclad Silent Defect Regent Necrobinder; do
    STS2_GAME_DIR="..." python3 python/play_full_run.py 5 "$char"
done
```

---

## Migration Plan

### Phase 1: Core Extraction (Week 1)

1. Create `Sts2HeadlessCore` project
2. Extract `RunSimulator` logic into Core
3. Move serialization classes to Core
4. Update existing `Sts2Headless` to reference Core
5. Verify existing CLI still works

### Phase 2: Mod Skeleton (Week 1-2)

1. Create `Sts2CliMod` project structure
2. Set up build configuration (csproj)
3. Create minimal `MainFile.cs` with mod initializer
4. Verify mod loads in-game

### Phase 3: Embedded Server (Week 2)

1. Implement `EmbeddedServer` with HTTP listener
2. Add basic WebSocket support
3. Create API endpoint for command execution
4. Test server independently

### Phase 4: Game Hooks (Week 2-3)

1. Implement `ModHooks` extending `AbstractModel`
2. Subscribe to combat/map/room hooks
3. Capture decision points
4. Stream state to connected clients

### Phase 5: CLI Client (Week 3)

1. Create `sts2_mod_client.py`
2. Implement WebSocket connection
3. Add interactive REPL
4. Integrate with existing `play.py` patterns

### Phase 6: Input Lock (Week 3-4)

1. Implement `InputLock`
2. Patch game input handlers
3. Test concurrent mouse/CLI access

### Phase 7: Testing & Polish (Week 4)

1. Full regression testing
2. Performance optimization
3. Documentation
4. Bug fixes

---

## Open Questions

1. **Port Selection**: Should the port be configurable? Default 12580?
2. **Multiple Clients**: Should the server support multiple connected CLI clients?
3. **Security**: Should we add any authentication for local connections?
4. **Log Output**: Where should mod logs go? Game log or separate file?
5. **Crash Recovery**: How should the client handle game crashes/disconnects?

---

## Appendix

### References

- Minty-Spire-2: https://github.com/erasels/Minty-Spire-2
- Existing CLI Implementation: `src/Sts2Headless/RunSimulator.cs`
- Python Client: `python/play.py`

### Compatibility

- **Game Version**: Slay the Spire 2 (Godot 4.5.1)
- **.NET Version**: .NET 9.0
- **Python Version**: 3.10+
- **Platforms**: macOS, Linux, Windows (via .csproj OS detection)
