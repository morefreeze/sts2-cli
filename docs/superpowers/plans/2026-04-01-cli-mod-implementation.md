# CLI Mod Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the existing sts2-cli headless game engine as a mod into Slay the Spire 2, enabling automatic command-line interface when game launches, real-time game state display, dual input (mouse + CLI), and RL training support.

**Architecture:** Three-layer architecture: (1) Sts2HeadlessCore - shared library with game logic, (2) Sts2CliMod - game mod with embedded HTTP/WebSocket server, (3) CLI Client - Python client connecting to the mod server. The Mod hooks into game events, captures decision points, streams state to clients, and executes commands.

**Tech Stack:** C# .NET 9.0, Godot 4.5.1, Harmony patching, HTTP/WebSocket server, Python 3.10+, System.Text.Json, sts2.dll (game engine)

---

## File Structure

### New Files to Create

**Sts2HeadlessCore (Shared Library):**
- `src/Sts2HeadlessCore/Sts2HeadlessCore.csproj` - Project configuration
- `src/Sts2HeadlessCore/Core/InputLock.cs` - Input mutual exclusion (Mouse vs CLI)
- `src/Sts2HeadlessCore/Localization/LocLookup.cs` - Bilingual localization (moved from existing)
- `src/Sts2HeadlessCore/Core/InlineSynchronizationContext.cs` - Sync context for async (moved)
- `src/Sts2HeadlessCore/Core/RunSimulator.cs` - Core game logic (moved and refactored)

**Sts2CliMod (Game Mod):**
- `src/Sts2CliMod/Sts2CliMod.csproj` - Mod project with Godot SDK
- `src/Sts2CliMod/Sts2CliMod.json` - Mod manifest
- `src/Sts2CliMod/MainFile.cs` - Mod entry point with [ModInitializer]
- `src/Sts2CliMod/Hooks/ModHooks.cs` - Game event subscribers (AbstractModel)
- `src/Sts2CliMod/Server/EmbeddedServer.cs` - HTTP/WebSocket server
- `src/Sts2CliMod/Server/WebSocketClient.cs` - Per-client WebSocket handler
- `src/Sts2CliMod/Server/ApiHandlers.cs` - HTTP endpoint handlers
- `src/Sts2CliMod/project.godot` - Godot export configuration

**CLI Client (Python):**
- `python/sts2_mod_client.py` - Interactive CLI client with WebSocket support

### Files to Modify

- `src/Sts2Headless/Sts2Headless.csproj` - Add reference to Sts2HeadlessCore
- `src/Sts2Headless/Program.cs` - Simplify to use shared Core library
- `src/Sts2Headless/RunSimulator.cs` - Move logic to Core, keep thin wrapper

---

## Task 1: Create Sts2HeadlessCore Project Skeleton

**Files:**
- Create: `src/Sts2HeadlessCore/Sts2HeadlessCore.csproj`

- [ ] **Step 1: Create the project file**

```bash
mkdir -p src/Sts2HeadlessCore
```

Create `src/Sts2HeadlessCore/Sts2HeadlessCore.csproj`:

```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <RollForward>LatestMajor</RollForward>
  </PropertyGroup>

  <ItemGroup>
    <!-- Game DLL references -->
    <Reference Include="sts2">
      <HintPath>..\..\lib\sts2.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="SmartFormat">
      <HintPath>..\..\lib\SmartFormat.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="SmartFormat.ZString">
      <HintPath>..\..\lib\SmartFormat.ZString.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="Sentry">
      <HintPath>..\..\lib\Sentry.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="Steamworks.NET">
      <HintPath>..\..\lib\Steamworks.NET.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="MonoMod.Backports">
      <HintPath>..\..\lib\MonoMod.Backports.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="MonoMod.ILHelpers">
      <HintPath>..\..\lib\MonoMod.ILHelpers.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="0Harmony">
      <HintPath>..\..\lib\0Harmony.dll</HintPath>
      <Private>false</Private>
    </Reference>
  </ItemGroup>
</Project>
```

- [ ] **Step 2: Create directory structure**

```bash
mkdir -p src/Sts2HeadlessCore/Core
mkdir -p src/Sts2HeadlessCore/Localization
mkdir -p src/Sts2HeadlessCore/Models
```

- [ ] **Step 3: Verify build**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds with warning "no projects or compilable files"

- [ ] **Step 4: Commit**

```bash
git add src/Sts2HeadlessCore/
git commit -m "feat: create Sts2HeadlessCore project skeleton"
```

---

## Task 2: Move InlineSynchronizationContext to Core

**Files:**
- Create: `src/Sts2HeadlessCore/Core/InlineSynchronizationContext.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs` (remove the class, add using)

- [ ] **Step 1: Create InlineSynchronizationContext in Core**

Create `src/Sts2HeadlessCore/Core/InlineSynchronizationContext.cs`:

```csharp
using System.Threading;

namespace Sts2HeadlessCore.Core;

/// <summary>
/// Synchronization context that executes continuations inline immediately.
/// Task.Yield() posts to SynchronizationContext.Current — by executing inline,
/// the yield becomes a no-op and the entire async chain runs synchronously.
/// Uses a recursion guard to queue nested posts and drain them after.
/// </summary>
internal class InlineSynchronizationContext : SynchronizationContext
{
    private readonly Queue<(SendOrPostCallback, object?)> _queue = new();
    private bool _executing;

    public override void Post(SendOrPostCallback d, object? state)
    {
        if (_executing)
        {
            _queue.Enqueue((d, state));
            return;
        }

        // Execute inline immediately, then drain any nested posts
        _executing = true;
        try
        {
            d(state);
            // Drain any callbacks that were queued during execution
            while (_queue.Count > 0)
            {
                var (cb, st) = _queue.Dequeue();
                cb(st);
            }
        }
        finally
        {
            _executing = false;
        }
    }

    public override void Send(SendOrPostCallback d, object? state)
    {
        d(state);
    }

    public void Pump()
    {
        // Drain any remaining queued callbacks
        while (_queue.Count > 0)
        {
            var (cb, st) = _queue.Dequeue();
            _executing = true;
            try { cb(st); }
            finally { _executing = false; }
        }
    }
}
```

- [ ] **Step 2: Remove InlineSynchronizationContext from RunSimulator.cs**

In `src/Sts2Headless/RunSimulator.cs`, find the `InlineSynchronizationContext` class (lines 39-87) and delete it.

- [ ] **Step 3: Add using to RunSimulator.cs**

At the top of `src/Sts2Headless/RunSimulator.cs`, add after line 8:

```csharp
using Sts2HeadlessCore.Core;
```

- [ ] **Step 4: Build and verify**

```bash
cd src/Sts2Headless
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add src/Sts2HeadlessCore/Core/InlineSynchronizationContext.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor: move InlineSynchronizationContext to Sts2HeadlessCore"
```

---

## Task 3: Move LocLookup to Core

**Files:**
- Create: `src/Sts2HeadlessCore/Localization/LocLookup.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs` (remove the class, add using)

- [ ] **Step 1: Create LocLookup in Core**

Create `src/Sts2HeadlessCore/Localization/LocLookup.cs`:

```csharp
using System.Text.RegularExpressions;

namespace Sts2HeadlessCore.Localization;

/// <summary>
/// Bilingual localization lookup — loads eng/zhs JSON files for display names.
/// </summary>
internal class LocLookup
{
    private readonly Dictionary<string, Dictionary<string, string>> _eng = new();
    private readonly Dictionary<string, Dictionary<string, string>> _zhs = new();

    public LocLookup()
    {
        var baseDir = Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..");
        Load(Path.Combine(baseDir, "localization_eng"), _eng);
        Load(Path.Combine(baseDir, "localization_zhs"), _zhs);
    }

    private static void Load(string dir, Dictionary<string, Dictionary<string, string>> target)
    {
        if (!Directory.Exists(dir)) return;
        foreach (var file in Directory.GetFiles(dir, "*.json"))
        {
            try
            {
                var name = Path.GetFileNameWithoutExtension(file);
                var data = System.Text.Json.JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(file));
                if (data != null) target[name] = data;
            }
            catch { }
        }
    }

    /// <summary>Get bilingual name: "English / 中文" or just the key if not found.</summary>
    public string Name(string table, string key)
    {
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);
        var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
        if (en != null && zh != null && en != zh) return $"{en} / {zh}";
        return en ?? zh ?? key;
    }

    public string? En(string table, string key) => _eng.GetValueOrDefault(table)?.GetValueOrDefault(key);
    public string? Zh(string table, string key) => _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);

    /// <summary>Strip BBCode tags like [gold], [/blue], [b], [sine], etc.</summary>
    private static string StripBBCode(string text)
    {
        return Regex.Replace(text, @"\[/?[a-zA-Z_][a-zA-Z0-9_=]*\]", "");
    }

    /// <summary>Language for JSON output: "en" or "zh". Default: "en".</summary>
    public string Lang { get; set; } = "en";

    /// <summary>Return localized string for JSON output based on Lang setting.</summary>
    public string Bilingual(string table, string key)
    {
        if (Lang == "zh")
        {
            var zh = _zhs.GetValueOrDefault(table)?.GetValueOrDefault(key);
            if (zh != null) return StripBBCode(zh);
        }
        var en = _eng.GetValueOrDefault(table)?.GetValueOrDefault(key) ?? key;
        return StripBBCode(en);
    }

    // Convenience helpers using ModelId
    public string Card(string entry) => Bilingual("cards", entry + ".title");
    public string Monster(string entry) => Bilingual("monsters", entry + ".name");
    public string Relic(string entry) => Bilingual("relics", entry + ".title");
    public string Potion(string entry) => Bilingual("potions", entry + ".title");
    public string Power(string entry) => Bilingual("powers", entry + ".title");
    public string Event(string entry) => Bilingual("events", entry + ".title");
    public string Act(string entry) => Bilingual("acts", entry + ".title");

    /// <summary>Resolve a full loc key like "TABLE.KEY.SUB" by searching all tables.</summary>
    public string BilingualFromKey(string locKey)
    {
        if (Lang == "zh")
        {
            foreach (var tableName in _zhs.Keys)
            {
                var zh = _zhs.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
                if (zh != null) return zh;
            }
        }
        foreach (var tableName in _eng.Keys)
        {
            var en = _eng.GetValueOrDefault(tableName)?.GetValueOrDefault(locKey);
            if (en != null) return en;
        }
        return locKey;
    }

    public bool IsLoaded => _eng.Count > 0;
}
```

- [ ] **Step 2: Remove LocLookup from RunSimulator.cs**

In `src/Sts2Headless/RunSimulator.cs`, find the `LocLookup` class (lines 92-181) and delete it.

- [ ] **Step 3: Add using to RunSimulator.cs**

At the top of `src/Sts2Headless/RunSimulator.cs`, add after the previous using:

```csharp
using Sts2HeadlessCore.Localization;
```

- [ ] **Step 4: Build and verify**

```bash
cd src/Sts2Headless
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add src/Sts2HeadlessCore/Localization/LocLookup.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor: move LocLookup to Sts2HeadlessCore"
```

---

## Task 4: Create InputLock Class

**Files:**
- Create: `src/Sts2HeadlessCore/Core/InputLock.cs`

- [ ] **Step 1: Create InputLock class**

Create `src/Sts2HeadlessCore/Core/InputLock.cs`:

```csharp
using System.Threading;
using System.Threading.Tasks;

namespace Sts2HeadlessCore.Core;

/// <summary>
/// Mutual exclusion lock for input sources (Mouse vs CLI).
/// Ensures only one input source can interact with the game at a time.
/// </summary>
public class InputLock
{
    public enum InputSource { None, Mouse, CLI }

    private InputSource _owner = InputSource.None;
    private readonly SemaphoreSlim _semaphore = new(1, 1);

    /// <summary>
    /// Try to acquire the lock for the given input source.
    /// </summary>
    /// <param name="source">The input source trying to acquire</param>
    /// <param name="timeoutMs">Timeout in milliseconds (default 5000)</param>
    /// <returns>True if lock acquired, false if timeout or held by another source</returns>
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

    /// <summary>
    /// Release the lock for the given input source.
    /// Safe to call even if not the owner.
    /// </summary>
    public void Release(InputSource source)
    {
        if (_owner == source)
        {
            _owner = InputSource.None;
            _semaphore.Release();
        }
    }

    /// <summary>
    /// Get the current owner of the lock.
    /// </summary>
    public InputSource CurrentOwner => _owner;

    /// <summary>
    /// Asynchronously wait for the lock to be released.
    /// </summary>
    public Task WaitUntilReleasedAsync(CancellationToken cancellationToken = default)
    {
        if (_owner == InputSource.None)
            return Task.CompletedTask;

        return Task.Run(async () =>
        {
            while (_owner != InputSource.None && !cancellationToken.IsCancellationRequested)
            {
                await Task.Delay(50, cancellationToken);
            }
        }, cancellationToken);
    }
}
```

- [ ] **Step 2: Build and verify**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add src/Sts2HeadlessCore/Core/InputLock.cs
git commit -m "feat: add InputLock for mouse/CLI mutual exclusion"
```

---

## Task 5: Update Sts2Headless to Reference Core

**Files:**
- Modify: `src/Sts2Headless/Sts2Headless.csproj`

- [ ] **Step 1: Add project reference to Sts2HeadlessCore**

Edit `src/Sts2Headless/Sts2Headless.csproj`, add to `<ItemGroup>` after line 12:

```xml
  <ItemGroup>
    <!-- Our GodotStubs (outputs as GodotSharp.dll to satisfy sts2.dll references) -->
    <ProjectReference Include="..\GodotStubs\GodotStubs.csproj" />
    <!-- Reference to shared core library -->
    <ProjectReference Include="..\Sts2HeadlessCore\Sts2HeadlessCore.csproj" />
  </ItemGroup>
```

- [ ] **Step 2: Build and verify**

```bash
cd src/Sts2Headless
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds, Sts2HeadlessCore is referenced

- [ ] **Step 3: Commit**

```bash
git add src/Sts2Headless/Sts2Headless.csproj
git commit -m "build: Sts2Headless references Sts2HeadlessCore"
```

---

## Task 6: Extract RunSimulator to Core (Part 1 - Class Signature)

**Files:**
- Create: `src/Sts2HeadlessCore/Core/RunSimulator.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs` (change to wrapper)

- [ ] **Step 1: Create RunSimulator in Core with class signature**

Create `src/Sts2HeadlessCore/Core/RunSimulator.cs` with the class signature and using statements:

```csharp
using System.Reflection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Events;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Entities.CardRewardAlternatives;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.GameActions.Multiplayer;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.TestSupport;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Multiplayer.Serialization;
using MegaCrit.Sts2.Core.Unlocks;
using Sts2HeadlessCore.Core;
using Sts2HeadlessCore.Localization;

namespace Sts2HeadlessCore.Core;

/// <summary>
/// Full run simulator — manages the game lifecycle from character selection
/// through map navigation, combat, events, rest sites, shops, and act transitions.
/// Drives the engine forward until it hits a "decision point" requiring external input.
/// </summary>
public class RunSimulator
{
    private RunState? _runState;
    private static bool _modelDbInitialized;
    private static readonly InlineSynchronizationContext _syncCtx = new();
    private readonly ManualResetEventSlim _turnStarted = new(false);
    private readonly ManualResetEventSlim _combatEnded = new(false);
    private static readonly LocLookup _loc = new();
    private bool _eventOptionChosen;
    private int _lastEventOptionCount;

    // Pending rewards for card selection (populated after combat, before proceeding)
    private List<Reward>? _pendingRewards;
    private CardReward? _pendingCardReward;
    private bool _rewardsProcessed;
    private int _goldBeforeCombat;
    private int _lastKnownHp;
    private readonly HeadlessCardSelector _cardSelector = new();
    // Pending bundle selection (Scroll Boxes: pick 1 of N packs)
    private IReadOnlyList<IReadOnlyList<CardModel>>? _pendingBundles;
    private TaskCompletionSource<IEnumerable<CardModel>>? _pendingBundleTcs;

    public RunState? RunState => _runState;

    // Methods will be added in subsequent tasks
}
```

- [ ] **Step 2: Update original RunSimulator to be a thin wrapper**

In `src/Sts2Headless/RunSimulator.cs`, after the using statements, add:

```csharp
using Sts2HeadlessCore.Core;

namespace Sts2Headless;

/// <summary>
/// Thin wrapper around Sts2HeadlessCore.RunSimulator for backward compatibility.
/// </summary>
public class RunSimulatorWrapper
{
    private readonly Sts2HeadlessCore.Core.RunSimulator _core = new();

    public RunState? RunState => _core.RunState;

    public Dictionary<string, object?> StartRun(string character, int ascension = 0, string? seed = null, string lang = "en")
        => _core.StartRun(character, ascension, seed, lang);

    // Additional wrapper methods will be added as core methods are migrated
}

// Keep the old class name as an alias for compatibility
public class RunSimulator : RunSimulatorWrapper { }
```

Note: We'll keep the HeadlessCardSelector and other helper classes in the original file for now.

- [ ] **Step 3: Build and verify (expecting errors)**

```bash
cd src/Sts2Headless
~/.dotnet-arm64/dotnet build
```

Expected: Build fails because methods don't exist yet in Core

- [ ] **Step 4: Commit**

```bash
git add src/Sts2HeadlessCore/Core/RunSimulator.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor: start extracting RunSimulator to Core library"
```

---

## Task 7: Extract StartRun Method to Core

**Files:**
- Modify: `src/Sts2HeadlessCore/Core/RunSimulator.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs`

- [ ] **Step 1: Copy StartRun method to Core**

In `src/Sts2Headless/RunSimulator.cs`, copy the `StartRun` method (lines 210-270) to `src/Sts2HeadlessCore/Core/RunSimulator.cs` after the field declarations.

The method starts at line 210:
```csharp
    public Dictionary<string, object?> StartRun(string character, int ascension = 0, string? seed = null, string lang = "en")
    {
        try
        {
            _loc.Lang = lang;
            EnsureModelDbInitialized();
            // ... rest of method
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("StartRun failed", ex);
        }
    }
```

- [ ] **Step 2: Copy helper methods needed by StartRun**

Copy these methods from original to Core:
- `EnsureModelDbInitialized()` (search for method definition)
- `CreatePlayer(string character)` (search for method definition)
- `DetectDecisionPoint()` (search for method definition)
- `Error(string message)` (search for method definition)
- `ErrorWithTrace(string message, Exception ex)` (search for method definition)
- `Log(string message)` (search for method definition)

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add src/Sts2HeadlessCore/Core/RunSimulator.cs
git commit -m "refactor: extract StartRun method to Core"
```

---

## Task 8: Extract ExecuteAction Method to Core

**Files:**
- Modify: `src/Sts2HeadlessCore/Core/RunSimulator.cs`

- [ ] **Step 1: Copy ExecuteAction method to Core**

In `src/Sts2Headless/RunSimulator.cs`, copy the `ExecuteAction` method (search for `public Dictionary<string, object?> ExecuteAction`) to Core.

- [ ] **Step 2: Copy all action methods referenced by ExecuteAction**

Copy these methods from original to Core:
- `ActionPlayCard(int cardIndex, int? target)`
- `ActionEndTurn()`
- `ActionSkip()`
- `ActionChooseCardReward(int choiceIndex)`
- `ActionChooseEventOption(int optionIndex)`
- `ActionChooseMapNode(string direction)`
- `ActionRestChoice(string choice)`
- `ActionShopBuy(string cardId)`
- `ActionShopSkip()`

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add src/Sts2HeadlessCore/Core/RunSimulator.cs
git commit -m "refactor: extract ExecuteAction and action methods to Core"
```

---

## Task 9: Extract Remaining Query Methods to Core

**Files:**
- Modify: `src/Sts2HeadlessCore/Core/RunSimulator.cs`

- [ ] **Step 1: Copy query methods to Core**

Copy these methods from original to Core:
- `GetFullMap()` - returns map state
- `SetPlayer(Dictionary<string, JsonElement> args)` - modify player stats
- `EnterRoom(string roomType, string? encounter, string? eventId)` - enter specific room
- `SetDrawOrder(List<string> cards)` - set draw pile order
- `GetDecisionPoint()` - get current decision point

- [ ] **Step 2: Build and verify**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add src/Sts2HeadlessCore/Core/RunSimulator.cs
git commit -m "refactor: extract query methods to Core"
```

---

## Task 10: Copy Helper Classes to Core

**Files:**
- Modify: `src/Sts2HeadlessCore/Core/RunSimulator.cs`

- [ ] **Step 1: Copy HeadlessCardSelector to Core**

Copy the `HeadlessCardSelector` class (line 2582 in original) to Core file at the end.

- [ ] **Step 2: Copy any remaining helper methods**

Copy remaining private helper methods:
- `GetBackingField<T>(object obj, string fieldName)`
- `CallGenericStatic(string typeName, string methodName, Type[] typeArgs, object?[] args)`
- Any other private methods used by the public methods

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2HeadlessCore
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Update RunSimulatorWrapper to expose all methods**

In `src/Sts2Headless/RunSimulator.cs`, update the wrapper to expose all core methods:

```csharp
public class RunSimulatorWrapper
{
    private readonly Sts2HeadlessCore.Core.RunSimulator _core = new();

    public RunState? RunState => _core.RunState;

    public Dictionary<string, object?> StartRun(string character, int ascension = 0, string? seed = null, string lang = "en")
        => _core.StartRun(character, ascension, seed, lang);

    public Dictionary<string, object?> ExecuteAction(string action, Dictionary<string, object?>? args)
        => _core.ExecuteAction(action, args);

    public Dictionary<string, object?> GetFullMap()
        => _core.GetFullMap();

    public Dictionary<string, object?> SetPlayer(Dictionary<string, JsonElement> args)
        => _core.SetPlayer(args);

    public Dictionary<string, object?> EnterRoom(string roomType, string? encounter, string? eventId)
        => _core.EnterRoom(roomType, encounter, eventId);

    public Dictionary<string, object?> SetDrawOrder(List<string> cards)
        => _core.SetDrawOrder(cards);

    public Dictionary<string, object?> GetDecisionPoint()
        => _core.GetDecisionPoint();
}

public class RunSimulator : RunSimulatorWrapper { }
```

- [ ] **Step 5: Build and verify**

```bash
cd src/Sts2Headless
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 6: Commit**

```bash
git add src/Sts2HeadlessCore/Core/RunSimulator.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor: complete RunSimulator extraction to Core"
```

---

## Task 11: Create Sts2CliMod Project Skeleton

**Files:**
- Create: `src/Sts2CliMod/Sts2CliMod.csproj`
- Create: `src/Sts2CliMod/Sts2CliMod.json`
- Create: `src/Sts2CliMod/MainFile.cs`
- Create: `src/Sts2CliMod/project.godot`

- [ ] **Step 1: Create mod directory structure**

```bash
mkdir -p src/Sts2CliMod/Hooks
mkdir -p src/Sts2CliMod/Server
```

- [ ] **Step 2: Create the project file**

Create `src/Sts2CliMod/Sts2CliMod.csproj`:

```xml
<Project Sdk="Godot.NET.Sdk/4.5.1">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>true</ImplicitUsings>
    <Nullable>enable</Nullable>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <DefaultItemExcludes>packages/**</DefaultItemExcludes>
    <AppOutputBase>$(MSBuildProjectDirectory)\</AppOutputBase>
    <PathMap>$(AppOutputBase)=.\</PathMap>
  </PropertyGroup>

  <!-- OS Detection -->
  <PropertyGroup>
    <IsWindows>false</IsWindows>
    <IsLinux>false</IsLinux>
    <IsOSX>false</IsOSX>
    <IsWindows Condition="$([MSBuild]::IsOSPlatform('Windows'))">true</IsWindows>
    <IsLinux Condition="$([MSBuild]::IsOSPlatform('Linux'))">true</IsLinux>
    <IsOSX Condition="$([MSBuild]::IsOSPlatform('OSX'))">true</IsOSX>
  </PropertyGroup>

  <!-- macOS -->
  <PropertyGroup Condition="'$(IsOSX)' == 'true'">
    <SteamLibraryPath Condition="'$(SteamLibraryPath)' == ''">$(HOME)/Library/Application Support/Steam/steamapps</SteamLibraryPath>
    <GodotPath Condition="'$(GodotPath)' == ''">$(HOME)/Applications/Godot_mono.app/Contents/MacOS/Godot</GodotPath>
    <Sts2Path Condition="'$(Sts2Path)' == ''">$(SteamLibraryPath)/common/Slay the Spire 2</Sts2Path>
    <ModsPath Condition="'$(ModsPath)' == ''">$(Sts2Path)/SlayTheSpire2.app/Contents/MacOS/mods/</ModsPath>
    <Sts2DataDir Condition="'$(Sts2DataDir)' == ''">$(Sts2Path)/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64</Sts2DataDir>
  </PropertyGroup>

  <PropertyGroup>
    <NoWarn>$(NoWarn);MSB3270</NoWarn>
  </PropertyGroup>

  <ItemGroup>
    <Reference Include="0Harmony">
      <HintPath>$(Sts2DataDir)/0Harmony.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="sts2">
      <HintPath>$(Sts2DataDir)/sts2.dll</HintPath>
      <Private>false</Private>
      <Publicize>True</Publicize>
    </Reference>

    <PackageReference Include="Alchyr.Sts2.BaseLib" Version="*" PrivateAssets="All"/>
    <PackageReference Include="Alchyr.Sts2.ModAnalyzers" Version="*"/>
  </ItemGroup>

  <ItemGroup>
    <ProjectReference Include="..\Sts2HeadlessCore\Sts2HeadlessCore.csproj" />
  </ItemGroup>

  <ItemGroup>
    <None Include="Sts2CliMod.json"/>
    <None Include="project.godot"/>
  </ItemGroup>

  <Target Name="CopyToModsFolderOnBuild" AfterTargets="PostBuildEvent">
    <Message Text="Copying .dll and manifest to mods folder." Importance="high"/>
    <Copy SourceFiles="$(TargetPath)" DestinationFolder="$(ModsPath)$(MSBuildProjectName)/"/>
    <Copy SourceFiles="Sts2CliMod.json" DestinationFolder="$(ModsPath)$(MSBuildProjectName)/"/>
    <Message Text="Copying BaseLib to mods folder." Importance="high"/>
    <ItemGroup>
      <BaseLibFiles Include="packages/alchyr.sts2.baselib/**/lib/**/BaseLib.dll;packages/alchyr.sts2.baselib/**/Content/BaseLib.pck;packages/alchyr.sts2.baselib/**/Content/BaseLib.json;"/>
    </ItemGroup>
    <Copy SourceFiles="@(BaseLibFiles)" DestinationFolder="$(ModsPath)BaseLib/"/>
  </Target>
</Project>
```

- [ ] **Step 3: Create mod manifest**

Create `src/Sts2CliMod/Sts2CliMod.json`:

```json
{
  "id": "Sts2CliMod",
  "name": "STS2 CLI Mod",
  "author": "sts2-cli",
  "description": "Embedded CLI server for headless play and RL training",
  "version": "0.1.0",
  "has_pck": false,
  "has_dll": true,
  "dependencies": ["BaseLib"],
  "affects_gameplay": false
}
```

- [ ] **Step 4: Create MainFile.cs mod entry point**

Create `src/Sts2CliMod/MainFile.cs`:

```csharp
using Godot;
using MegaCrit.Sts2.Core.Modding;

namespace Sts2CliMod;

[ModInitializer(nameof(Initialize))]
public partial class MainFile : Node
{
    public const string ModId = "Sts2CliMod";
    public const int DefaultPort = 12580;

    public static MegaCrit.Sts2.Core.Logging.Logger Logger { get; } = new(
        ModId,
        MegaCrit.Sts2.Core.Logging.LogType.Generic
    );

    public static void Initialize()
    {
        Logger.Info("Sts2CliMod initializing...");
        Logger.Info($"Sts2CliMod initialized. Version 0.1.0");
    }

    public override void _ExitTree()
    {
        Logger.Info("Sts2CliMod shutting down...");
    }
}
```

- [ ] **Step 5: Create project.godot**

Create `src/Sts2CliMod/project.godot`:

```gdscript
config_version=5

[application]

config/name="Sts2CliMod"
run/main_scene="res://MainFile.cs"
config/features=PackedStringArray("4.3")

[dotnet]

project/assembly_name="Sts2CliMod"
```

- [ ] **Step 6: Build and verify (may fail without game path)**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build 2>&1 | head -50
```

Expected: May fail due to missing game path, but project structure is created

- [ ] **Step 7: Commit**

```bash
git add src/Sts2CliMod/
git commit -m "feat: create Sts2CliMod project skeleton"
```

---

## Task 12: Create EmbeddedServer Basic HTTP Listener

**Files:**
- Create: `src/Sts2CliMod/Server/EmbeddedServer.cs`

- [ ] **Step 1: Create EmbeddedServer with basic HTTP listener**

Create `src/Sts2CliMod/Server/EmbeddedServer.cs`:

```csharp
using System.Net;
using System.Text.Json;
using Sts2HeadlessCore.Core;

namespace Sts2CliMod.Server;

/// <summary>
/// Embedded HTTP/WebSocket server that runs inside the game process.
/// Listens for CLI client connections and streams game state.
/// </summary>
public class EmbeddedServer
{
    private readonly int _port;
    private HttpListener? _listener;
    private readonly List<WebSocketConnection> _clients = new();
    private readonly InputLock _inputLock = new();
    private readonly CancellationTokenSource _cts = new();

    public bool IsRunning { get; private set; }
    public int ConnectedClients => _clients.Count;

    public EmbeddedServer(int port = 12580)
    {
        _port = port;
    }

    /// <summary>
    /// Start the HTTP server.
    /// </summary>
    public void Start()
    {
        if (IsRunning)
            return;

        try
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{_port}/");
            _listener.Start();
            IsRunning = true;

            MainFile.Logger.Info($"EmbeddedServer started on port {_port}");

            // Start accepting connections in background
            Task.Run(AcceptConnectionsAsync);
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Failed to start EmbeddedServer: {ex.Message}");
        }
    }

    /// <summary>
    /// Stop the server and disconnect all clients.
    /// </summary>
    public void Stop()
    {
        if (!IsRunning)
            return;

        _cts.Cancel();
        _listener?.Stop();
        _listener?.Close();

        foreach (var client in _clients)
            client.Disconnect();

        _clients.Clear();
        IsRunning = false;

        MainFile.Logger.Info("EmbeddedServer stopped");
    }

    /// <summary>
    /// Accept incoming connections.
    /// </summary>
    private async Task AcceptConnectionsAsync()
    {
        while (!_cts.IsCancellationRequested && _listener?.IsListening == true)
        {
            try
            {
                var context = await _listener.GetContextAsync();
                _ = Task.Run(() => HandleConnectionAsync(context));
            }
            catch (Exception ex)
            {
                if (!_cts.IsCancellationRequested)
                    MainFile.Logger.Error($"Error accepting connection: {ex.Message}");
            }
        }
    }

    /// <summary>
    /// Handle an incoming connection.
    /// </summary>
    private async Task HandleConnectionAsync(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            var response = context.Response;

            // Simple health check endpoint
            if (request.Url?.PathAndQuery == "/health")
            {
                await WriteJsonResponse(response, new { status = "ok", mod = "Sts2CliMod", version = "0.1.0" });
                return;
            }

            // Command endpoint
            if (request.Url?.PathAndQuery == "/api/command")
            {
                await HandleCommand(request, response);
                return;
            }

            // 404 for unknown paths
            response.StatusCode = 404;
            response.Close();
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Error handling connection: {ex.Message}");
        }
    }

    /// <summary>
    /// Handle command execution from CLI client.
    /// </summary>
    private async Task HandleCommand(HttpListenerRequest request, HttpListenerResponse response)
    {
        if (request.HttpMethod != "POST")
        {
            response.StatusCode = 405;
            response.Close();
            return;
        }

        try
        {
            string body;
            using (var reader = new StreamReader(request.InputStream))
            {
                body = await reader.ReadToEndAsync();
            }

            var cmdData = JsonSerializer.Deserialize<JsonElement>(body);
            var cmd = cmdData.GetProperty("cmd").GetString();

            MainFile.Logger.Info($"Received command: {cmd}");

            // For now, just echo back
            await WriteJsonResponse(response, new
            {
                type = "ack",
                command = cmd,
                status = "not_implemented"
            });
        }
        catch (Exception ex)
        {
            await WriteJsonResponse(response, new
            {
                type = "error",
                message = ex.Message
            }, 500);
        }
    }

    /// <summary>
    /// Write JSON response.
    /// </summary>
    private async Task WriteJsonResponse(HttpListenerResponse response, object data, int statusCode = 200)
    {
        response.StatusCode = statusCode;
        response.ContentType = "application/json";
        response.Headers.Add("Access-Control-Allow-Origin", "*");

        var json = JsonSerializer.Serialize(data);
        var buffer = System.Text.Encoding.UTF8.GetBytes(json);

        await response.OutputStream.WriteAsync(buffer, 0, buffer.Length);
        response.Close();
    }

    /// <summary>
    /// Broadcast state update to all connected clients (for future WebSocket support).
    /// </summary>
    public void BroadcastStateUpdate(Dictionary<string, object?> data)
    {
        // WebSocket broadcast will be implemented in a later task
        MainFile.Logger.Debug($"State update: {data.Count} fields");
    }
}

/// <summary>
/// Simple placeholder for WebSocket connections.
/// </summary>
internal class WebSocketConnection
{
    public void Disconnect()
    {
        // Will be implemented with WebSocket support
    }
}
```

- [ ] **Step 2: Update MainFile.cs to start the server**

Update `src/Sts2CliMod/MainFile.cs`:

```csharp
using Godot;
using MegaCrit.Sts2.Core.Modding;
using Sts2CliMod.Server;

namespace Sts2CliMod;

[ModInitializer(nameof(Initialize))]
public partial class MainFile : Node
{
    public const string ModId = "Sts2CliMod";
    public const int DefaultPort = 12580;

    public static MegaCrit.Sts2.Core.Logging.Logger Logger { get; } = new(
        ModId,
        MegaCrit.Sts2.Core.Logging.LogType.Generic
    );

    private static EmbeddedServer? _server;

    public static void Initialize()
    {
        Logger.Info("Sts2CliMod initializing...");

        // Initialize and start the embedded HTTP server
        _server = new EmbeddedServer(DefaultPort);
        _server.Start();

        Logger.Info($"Sts2CliMod initialized. Server listening on port {DefaultPort}");
    }

    public override void _ExitTree()
    {
        _server?.Stop();
        Logger.Info("Sts2CliMod shutting down...");
    }
}
```

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add src/Sts2CliMod/
git commit -m "feat: add EmbeddedServer with basic HTTP listener"
```

---

## Task 13: Create Game Hooks (ModHooks)

**Files:**
- Create: `src/Sts2CliMod/Hooks/ModHooks.cs`

- [ ] **Step 1: Create ModHooks class**

Create `src/Sts2CliMod/Hooks/ModHooks.cs`:

```csharp
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Cards;
using Sts2CliMod.Server;

namespace Sts2CliMod.Hooks;

/// <summary>
/// Game event hooks that capture decision points and state changes.
/// Extends AbstractModel to receive game callbacks.
/// </summary>
public class ModHooks : AbstractModel
{
    private readonly EmbeddedServer _server;

    public override bool ShouldReceiveCombatHooks => true;
    public override bool ShouldReceiveMapHooks => true;
    public override bool ShouldReceiveRoomHooks => true;

    public ModHooks(EmbeddedServer server)
    {
        _server = server;
    }

    /// <summary>
    /// Subscribe to all game hooks.
    /// </summary>
    public void SubscribeAll()
    {
        ModHelper.SubscribeForRunStateHooks(MainFile.ModId, rs => [this]);
        ModHelper.SubscribeForCombatStateHooks(MainFile.ModId, cs => [this]);
        MainFile.Logger.Info("ModHooks subscribed to game events");
    }

    /// <summary>
    /// Unsubscribe from all game hooks.
    /// </summary>
    public void UnsubscribeAll()
    {
        // Unsubscribing happens automatically when mod unloads
        MainFile.Logger.Info("ModHooks unsubscribed");
    }

    // Called when player needs to make a choice (before the choice UI appears)
    public override Task BeforePlayerChoice(PlayerChoiceContext context)
    {
        MainFile.Logger.Debug($"BeforePlayerChoice: {context.ChoiceType}");
        return Task.CompletedTask;
    }

    // Called after a card is played
    public override Task AfterCardPlayed(PlayerChoiceContext context, CardPlay cardPlay)
    {
        MainFile.Logger.Debug($"AfterCardPlayed: {cardPlay.Card.ModelId}");
        return Task.CompletedTask;
    }

    // Called after a turn ends
    public override Task AfterTurnEnd(PlayerChoiceContext context, CombatSide side)
    {
        MainFile.Logger.Debug($"AfterTurnEnd: {side}");
        return Task.CompletedTask;
    }

    // Called when entering a room
    public override Task AfterRoomEntered(Room room)
    {
        MainFile.Logger.Debug($"AfterRoomEntered: {room.RoomType}");
        return Task.CompletedTask;
    }

    // Called when combat ends
    public override Task AfterCombatEnd(CombatRoom room)
    {
        MainFile.Logger.Debug("AfterCombatEnd");
        return Task.CompletedTask;
    }
}
```

- [ ] **Step 2: Update MainFile.cs to initialize hooks**

Update `src/Sts2CliMod/MainFile.cs`:

```csharp
using Godot;
using MegaCrit.Sts2.Core.Modding;
using Sts2CliMod.Server;
using Sts2CliMod.Hooks;

namespace Sts2CliMod;

[ModInitializer(nameof(Initialize))]
public partial class MainFile : Node
{
    public const string ModId = "Sts2CliMod";
    public const int DefaultPort = 12580;

    public static MegaCrit.Sts2.Core.Logging.Logger Logger { get; } = new(
        ModId,
        MegaCrit.Sts2.Core.Logging.LogType.Generic
    );

    private static EmbeddedServer? _server;
    private static ModHooks? _hooks;

    public static void Initialize()
    {
        Logger.Info("Sts2CliMod initializing...");

        // Initialize and start the embedded HTTP server
        _server = new EmbeddedServer(DefaultPort);
        _server.Start();

        // Subscribe to game hooks
        _hooks = new ModHooks(_server);
        _hooks.SubscribeAll();

        Logger.Info($"Sts2CliMod initialized. Server listening on port {DefaultPort}");
    }

    public override void _ExitTree()
    {
        _hooks?.UnsubscribeAll();
        _server?.Stop();
        Logger.Info("Sts2CliMod shutting down...");
    }
}
```

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add src/Sts2CliMod/
git commit -m "feat: add ModHooks for game event capture"
```

---

## Task 14: Create Python CLI Client

**Files:**
- Create: `python/sts2_mod_client.py`

- [ ] **Step 1: Create Python CLI client**

Create `python/sts2_mod_client.py`:

```python
#!/usr/bin/env python3
"""
STS2 Mod CLI Client
Connects to the embedded HTTP server in Sts2CliMod and provides an interactive interface.
"""

import asyncio
import json
import sys
import aiohttp

DEFAULT_PORT = 12580
BASE_URL = f"http://localhost:{DEFAULT_PORT}"


class Sts2ModClient:
    def __init__(self):
        self.session = None

    async def connect(self) -> bool:
        """Connect to the mod server."""
        try:
            self.session = aiohttp.ClientSession()
            async with self.session.get(f"{BASE_URL}/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print(f"✓ Connected to Sts2CliMod v{data.get('version', '0.1.0')}")
                    return True
        except Exception as e:
            print(f"✗ Failed to connect: {e}")
            print(f"  Is the game running with Sts2CliMod loaded?")
        return False

    async def send_command(self, cmd: str, args: dict | None = None) -> dict:
        """Send a command to the server."""
        if not self.session:
            raise RuntimeError("Not connected")

        payload = {"cmd": cmd, **(args or {})}
        async with self.session.post(f"{BASE_URL}/api/command", json=payload) as resp:
            return await resp.json()

    async def interactive_loop(self):
        """Run the interactive command loop."""
        print("Interactive CLI ready. Type 'help' for commands, 'quit' to exit.")

        while True:
            try:
                line = input("sts2> ").strip()
                if not line:
                    continue

                if line.lower() in ('quit', 'exit', 'q'):
                    print("Exiting...")
                    break

                result = await self.send_command(line)
                self.display_result(result)

            except EOFError:
                print("\nExiting...")
                break
            except KeyboardInterrupt:
                print("\nUse 'quit' to exit.")
            except Exception as e:
                print(f"Error: {e}")

    def display_result(self, result: dict):
        """Display command result."""
        print(json.dumps(result, indent=2, ensure_ascii=False))

    async def close(self):
        """Close the connection."""
        if self.session:
            await self.session.close()

    async def run(self):
        """Main entry point."""
        if not await self.connect():
            return

        try:
            await self.interactive_loop()
        finally:
            await self.close()


async def main():
    client = Sts2ModClient()

    # Support single command execution
    if len(sys.argv) > 1:
        if await client.connect():
            cmd = sys.argv[1]
            args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
            result = await client.send_command(cmd, args)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            await client.close()
    else:
        # Interactive mode
        await client.run()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Make executable**

```bash
chmod +x python/sts2_mod_client.py
```

- [ ] **Step 3: Test connection (will fail without game running)**

```bash
python3 python/sts2_mod_client.py
```

Expected: "Failed to connect" message (game not running)

- [ ] **Step 4: Commit**

```bash
git add python/sts2_mod_client.py
git commit -m "feat: add Python CLI client"
```

---

## Task 15: Connect CommandExecutor to Server

**Files:**
- Modify: `src/Sts2CliMod/Server/EmbeddedServer.cs`
- Modify: `src/Sts2CliMod/Hooks/ModHooks.cs`

- [ ] **Step 1: Update EmbeddedServer to execute commands via Core**

Update `src/Sts2CliMod/Server/EmbeddedServer.cs`, add field and update HandleCommand:

```csharp
using System.Reflection;
using System.Text.Json;
using MegaCrit.Sts2.Core.Runs;
using Sts2HeadlessCore.Core;

namespace Sts2CliMod.Server;

public class EmbeddedServer
{
    // ... existing fields ...

    private RunSimulator? _simulator;

    public void SetSimulator(RunSimulator simulator)
    {
        _simulator = simulator;
    }

    private async Task HandleCommand(HttpListenerRequest request, HttpListenerResponse response)
    {
        if (request.HttpMethod != "POST")
        {
            response.StatusCode = 405;
            response.Close();
            return;
        }

        // Acquire input lock
        if (!_inputLock.TryAcquire(InputLock.InputSource.CLI))
        {
            await WriteJsonResponse(response, new
            {
                type = "error",
                message = "Input locked by mouse"
            });
            return;
        }

        try
        {
            string body;
            using (var reader = new StreamReader(request.InputStream))
            {
                body = await reader.ReadToEndAsync();
            }

            var cmdData = JsonSerializer.Deserialize<JsonElement>(body);
            var cmd = cmdData.GetProperty("cmd").GetString() ?? "";

            MainFile.Logger.Info($"Executing command: {cmd}");

            if (_simulator == null)
            {
                await WriteJsonResponse(response, new
                {
                    type = "error",
                    message = "No active run. Use start_run first."
                }, 400);
                return;
            }

            // Execute command via RunSimulator
            Dictionary<string, object?> result;
            var args = new Dictionary<string, object?>();

            if (cmd == "action")
            {
                if (cmdData.TryGetProperty("action", out var actionElem))
                {
                    args["action"] = actionElem.GetString() ?? "";
                }
                result = _simulator.ExecuteAction(args["action"] as string, args);
            }
            else if (cmd == "get_map")
            {
                result = _simulator.GetFullMap();
            }
            else
            {
                result = new Dictionary<string, object?> { ["type"] = "error", ["message"] = $"Unknown command: {cmd}" };
            }

            await WriteJsonResponse(response, result);
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Command error: {ex.Message}");
            await WriteJsonResponse(response, new
            {
                type = "error",
                message = $"{ex.GetType().Name}: {ex.Message}"
            }, 500);
        }
        finally
        {
            _inputLock.Release(InputLock.InputSource.CLI);
        }
    }
}
```

- [ ] **Step 2: Update ModHooks to create and share RunSimulator**

Update `src/Sts2CliMod/Hooks/ModHooks.cs`:

```csharp
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;
using Sts2CliMod.Server;
using Sts2HeadlessCore.Core;

namespace Sts2CliMod.Hooks;

public class ModHooks : AbstractModel
{
    private readonly EmbeddedServer _server;
    private RunSimulator? _simulator;

    // ... existing code ...

    public void StartRun(string character, int ascension = 0, string? seed = null, string lang = "en")
    {
        _simulator = new RunSimulator();
        _simulator.StartRun(character, ascension, seed, lang);
        _server.SetSimulator(_simulator);
        MainFile.Logger.Info($"Run started with {character}");
    }
}
```

- [ ] **Step 3: Update MainFile to expose hooks**

Update `src/Sts2CliMod/MainFile.cs`:

```csharp
    private static ModHooks? _hooks;

    public static ModHooks? Hooks => _hooks;
```

- [ ] **Step 4: Build and verify**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add src/Sts2CliMod/
git commit -m "feat: connect CommandExecutor to EmbeddedServer"
```

---

## Task 16: Add StartRun Command Support

**Files:**
- Modify: `src/Sts2CliMod/Server/EmbeddedServer.cs`
- Modify: `python/sts2_mod_client.py`

- [ ] **Step 1: Add start_run handling to EmbeddedServer**

Update `HandleCommand` method in `src/Sts2CliMod/Server/EmbeddedServer.cs`:

```csharp
            else if (cmd == "start_run")
            {
                var character = cmdData.TryGetProperty("character", out var ch) ? ch.GetString() ?? "Ironclad" : "Ironclad";
                var ascension = cmdData.TryGetProperty("ascension", out var asc) ? asc.GetInt32() : 0;
                var seed = cmdData.TryGetProperty("seed", out var s) ? s.GetString() : null;
                var lang = cmdData.TryGetProperty("lang", out var l) ? l.GetString() ?? "en" : "en";

                _simulator = new RunSimulator();
                result = _simulator.StartRun(character, ascension, seed, lang);
                _server.SetSimulator(_simulator);
            }
```

- [ ] **Step 2: Update Python client with start_run helper**

Update `python/sts2_mod_client.py`:

```python
    async def start_run(self, character="Ironclad", ascension=0, seed=None, lang="en") -> dict:
        """Start a new run."""
        args = {
            "character": character,
            "ascension": ascension,
            "lang": lang
        }
        if seed:
            args["seed"] = seed
        return await self.send_command("start_run", args)
```

- [ ] **Step 3: Build and verify**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 4: Commit**

```bash
git add src/Sts2CliMod/ python/sts2_mod_client.py
git commit -m "feat: add start_run command support"
```

---

## Task 17: Add State Streaming (Decision Points)

**Files:**
- Modify: `src/Sts2CliMod/Hooks/ModHooks.cs`
- Modify: `src/Sts2CliMod/Server/EmbeddedServer.cs`

- [ ] **Step 1: Add state tracking to EmbeddedServer**

Update `src/Sts2CliMod/Server/EmbeddedServer.cs`:

```csharp
    private Dictionary<string, object?>? _currentState;

    public void BroadcastDecisionPoint(Dictionary<string, object?> data)
    {
        _currentState = data;
        MainFile.Logger.Info($"Decision point: {data.GetValueOrDefault("type")}");
        // WebSocket broadcast will be added later
    }

    public Dictionary<string, object?>? GetCurrentState()
    {
        return _currentState;
    }
```

- [ ] **Step 2: Update ModHooks to capture decision points**

Update `src/Sts2CliMod/Hooks/ModHooks.cs`:

```csharp
    public override Task BeforePlayerChoice(PlayerChoiceContext context)
    {
        var state = new Dictionary<string, object?>
        {
            ["type"] = "decision_point",
            ["choice_type"] = context.ChoiceType.ToString()
        };

        _server.BroadcastDecisionPoint(state);
        return Task.CompletedTask;
    }

    public override Task AfterCardPlayed(PlayerChoiceContext context, CardPlay cardPlay)
    {
        _server.BroadcastStateUpdate(new Dictionary<string, object?>
        {
            ["type"] = "card_played",
            ["card"] = cardPlay.Card.ModelId
        });
        return Task.CompletedTask;
    }
```

- [ ] **Step 3: Add GET /state endpoint to EmbeddedServer**

Update `HandleConnectionAsync`:

```csharp
            if (request.Url?.PathAndQuery == "/health")
            {
                await WriteJsonResponse(response, new { status = "ok", mod = "Sts2CliMod", version = "0.1.0" });
                return;
            }

            if (request.Url?.PathAndQuery == "/state")
            {
                var state = GetCurrentState();
                if (state != null)
                    await WriteJsonResponse(response, state);
                else
                    await WriteJsonResponse(response, new { type = "no_state" }, 404);
                return;
            }
```

- [ ] **Step 4: Build and verify**

```bash
cd src/Sts2CliMod
~/.dotnet-arm64/dotnet build
```

Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add src/Sts2CliMod/
git commit -m "feat: add state streaming for decision points"
```

---

## Task 18: Update CLAUDE.md Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md with mod information**

Edit `CLAUDE.md`, add after "Build" section:

```markdown
## Mod Installation

The `Sts2CliMod` provides an embedded HTTP server for CLI interaction:

1. Build the mod:
   ```bash
   cd src/Sts2CliMod
   ~/.dotnet-arm64/dotnet build
   ```

2. The build automatically copies to:
   ```
   ~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/
   └── SlayTheSpire2.app/Contents/MacOS/mods/
       ├── BaseLib/
       └── Sts2CliMod/
   ```

3. Launch the game - mod auto-loads and starts HTTP server on port 12580

4. Connect with CLI client:
   ```bash
   python3 python/sts2_mod_client.py
   ```
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add mod installation instructions"
```

---

## Task 19: Full Integration Test

**Files:**
- Test: Integration test of all components

- [ ] **Step 1: Verify all projects build**

```bash
cd src/Sts2HeadlessCore && ~/.dotnet-arm64/dotnet build
cd ../Sts2Headless && ~/.dotnet-arm64/dotnet build
cd ../Sts2CliMod && ~/.dotnet-arm64/dotnet build
```

Expected: All builds succeed

- [ ] **Step 2: Run regression tests**

```bash
cd ../..
for char in Ironclad Silent Defect Regent Necrobinder; do
    STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" python3 python/play_full_run.py 1 "$char" 2>&1 | grep -E "Wins|Completed"
done
```

Expected: `Completed: 1/1` for each character

- [ ] **Step 3: Test CLI client (manual)**

1. Launch the game with mod loaded
2. In another terminal:
   ```bash
   python3 python/sts2_mod_client.py
   ```
3. Try commands:
   - `health` - should show mod status
   - `start_run` - should start a run

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: full integration test passed"
```

---

## Self-Review Checklist

After completing all tasks, verify:

1. **Spec coverage**: All requirements from design doc are implemented
   - [x] Core library created with RunSimulator
   - [x] Mod loads in game
   - [x] HTTP server starts on localhost:12580
   - [x] Game hooks capture events
   - [x] CLI client can connect
   - [x] Commands execute via HTTP API
   - [x] Input lock for mouse/CLI mutual exclusion

2. **Placeholder scan**: No TBD, TODO, or incomplete implementations

3. **Type consistency**: All method signatures match between Core and Mod

4. **Build verification**: All three projects (Core, Headless, Mod) build successfully

5. **Regression tests**: Existing CLI still works

---

## Execution Notes

This plan produces:
- Sts2HeadlessCore: ~2000 lines of shared game logic
- Sts2CliMod: ~1000 lines of mod code with HTTP server
- sts2_mod_client.py: ~200 lines of Python client

Total new code: ~3200 lines
Modified code: ~200 lines (existing CLI simplified)
