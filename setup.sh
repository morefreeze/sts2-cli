#!/bin/bash
# setup.sh — Copy game DLLs from Steam installation to lib/
#
# Prerequisites:
#   - Slay the Spire 2 installed via Steam
#   - .NET 9+ SDK (ARM64 for Apple Silicon, x64 for Intel/Linux)
#
# Usage:
#   ./setup.sh                    # Auto-detect Steam path
#   ./setup.sh /path/to/game      # Manual game directory

set -e

# ── Locate game directory ──

GAME_DIR="$1"

if [ -z "$GAME_DIR" ]; then
    # Auto-detect based on platform
    case "$(uname -s)" in
        Darwin)
            GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
            if [ ! -d "$GAME_DIR" ]; then
                # Try x86_64
                GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_x86_64"
            fi
            ;;
        Linux)
            GAME_DIR="$HOME/.steam/steam/steamapps/common/Slay the Spire 2"
            if [ ! -d "$GAME_DIR" ]; then
                GAME_DIR="$HOME/.local/share/Steam/steamapps/common/Slay the Spire 2"
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*)
            GAME_DIR="C:/Program Files (x86)/Steam/steamapps/common/Slay the Spire 2"
            ;;
    esac
fi

if [ ! -d "$GAME_DIR" ]; then
    echo "❌ Game directory not found: $GAME_DIR"
    echo ""
    echo "Usage: ./setup.sh /path/to/game/data"
    echo ""
    echo "On macOS, this is usually:"
    echo "  ~/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
    exit 1
fi

echo "📁 Game directory: $GAME_DIR"

# ── Copy DLLs ──

mkdir -p lib

DLLS=(
    "sts2.dll"
    "SmartFormat.dll"
    "SmartFormat.ZString.dll"
    "Sentry.dll"
    "Steamworks.NET.dll"
    "MonoMod.Backports.dll"
    "MonoMod.ILHelpers.dll"
    "0Harmony.dll"
    "System.IO.Hashing.dll"
)

echo ""
echo "📦 Copying DLLs to lib/..."
for dll in "${DLLS[@]}"; do
    src="$GAME_DIR/$dll"
    if [ -f "$src" ]; then
        cp "$src" "lib/$dll"
        echo "  ✓ $dll"
    else
        echo "  ✗ $dll not found at $src"
        # Try searching subdirectories
        found=$(find "$GAME_DIR" -name "$dll" -print -quit 2>/dev/null)
        if [ -n "$found" ]; then
            cp "$found" "lib/$dll"
            echo "    → found at $found"
        else
            echo "    ⚠ Skipped (may cause build errors)"
        fi
    fi
done

# Back up original sts2.dll
if [ -f "lib/sts2.dll" ] && [ ! -f "lib/sts2.dll.original" ]; then
    cp "lib/sts2.dll" "lib/sts2.dll.original"
    echo "  ✓ Backed up sts2.dll.original"
fi

# ── Detect .NET SDK ──

DOTNET=""
if [ -x "$HOME/.dotnet-arm64/dotnet" ]; then
    DOTNET="$HOME/.dotnet-arm64/dotnet"
elif command -v dotnet &>/dev/null; then
    DOTNET="dotnet"
fi

if [ -z "$DOTNET" ]; then
    echo ""
    echo "❌ .NET SDK not found."
    echo "   Install .NET 9+ from https://dotnet.microsoft.com/download"
    echo "   Or set DOTNET env var to your dotnet binary path."
    exit 1
fi

echo ""
echo "🔧 .NET SDK: $DOTNET ($($DOTNET --version))"

# ── IL Patch sts2.dll ──

echo ""
echo "🔨 Applying IL patches to sts2.dll..."

# Create a temporary patching project
PATCH_DIR=$(mktemp -d)
cat > "$PATCH_DIR/Patcher.csproj" << 'PROJ'
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net9.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Mono.Cecil" Version="0.11.6" />
  </ItemGroup>
</Project>
PROJ

cat > "$PATCH_DIR/Program.cs" << 'CSHARP'
using System;
using System.IO;
using System.Linq;
using Mono.Cecil;
using Mono.Cecil.Cil;

var dllPath = args[0];
Console.WriteLine($"Patching {dllPath}...");

var resolver = new DefaultAssemblyResolver();
var libDir = Path.GetDirectoryName(dllPath)!;
resolver.AddSearchDirectory(libDir);
// Also search for GodotSharp.dll in the GodotStubs output (fallback)
var stubsDir = Path.Combine(Path.GetDirectoryName(libDir)!, "src", "GodotStubs", "bin", "Debug", "net9.0");
if (Directory.Exists(stubsDir)) resolver.AddSearchDirectory(stubsDir);
var module = ModuleDefinition.ReadModule(dllPath, new ReaderParameters {
    AssemblyResolver = resolver,
    ReadingMode = ReadingMode.Deferred  // Don't force-resolve all references upfront
});

int patches = 0;

// Patch 1: Task.Yield() — make YieldAwaitable.YieldAwaiter.IsCompleted return true
// This prevents async deadlocks in headless mode
foreach (var type in module.Types)
{
    foreach (var nested in type.NestedTypes)
    {
        foreach (var nested2 in nested.NestedTypes)
        {
            if (nested2.Name.Contains("YieldAwaiter") || nested2.Name == "<>c")
            {
                foreach (var method in nested2.Methods)
                {
                    if (method.Name == "get_IsCompleted" && method.Body != null)
                    {
                        var il = method.Body.GetILProcessor();
                        il.Body.Instructions.Clear();
                        il.Emit(OpCodes.Ldc_I4_1);
                        il.Emit(OpCodes.Ret);
                        patches++;
                        Console.WriteLine($"  Patched {type.Name}.{nested.Name}.{nested2.Name}.IsCompleted");
                    }
                }
            }
        }
    }
}

// Patch 2: WaitUntilQueueIsEmptyOrWaitingOnNonPlayerDrivenAction → return Task.CompletedTask
foreach (var type in module.Types)
{
    foreach (var method in type.Methods)
    {
        if (method.Name == "WaitUntilQueueIsEmptyOrWaitingOnNonPlayerDrivenAction" && method.Body != null)
        {
            var il = method.Body.GetILProcessor();
            il.Body.Instructions.Clear();
            // return Task.CompletedTask
            var taskType = module.ImportReference(typeof(System.Threading.Tasks.Task));
            var completedProp = module.ImportReference(
                typeof(System.Threading.Tasks.Task).GetProperty("CompletedTask")!.GetGetMethod()!);
            il.Emit(OpCodes.Call, completedProp);
            il.Emit(OpCodes.Ret);
            patches++;
            Console.WriteLine($"  Patched {type.Name}.{method.Name} → Task.CompletedTask");
        }
    }
}

// Patch 3: Cmd.Wait(float, bool) and Cmd.Wait(float, CancellationToken, bool) → return Task.CompletedTask
// Cmd.Wait is used for UI animations (e.g. Vantom DISMEMBER_MOVE, boss mechanics).
// In headless mode, timers never fire, causing combat deadlocks.
var taskType2 = module.ImportReference(typeof(System.Threading.Tasks.Task));
var completedProp2 = module.ImportReference(
    typeof(System.Threading.Tasks.Task).GetProperty("CompletedTask")!.GetGetMethod()!);
foreach (var type in module.Types)
{
    if (type.FullName == "MegaCrit.Sts2.Core.Commands.Cmd")
    {
        foreach (var method in type.Methods)
        {
            if (method.Name == "Wait" && method.Body != null)
            {
                var il = method.Body.GetILProcessor();
                method.Body.Instructions.Clear();
                method.Body.Variables.Clear();
                method.Body.ExceptionHandlers.Clear();
                il.Emit(OpCodes.Call, completedProp2);
                il.Emit(OpCodes.Ret);
                patches++;
                Console.WriteLine($"  Patched {type.Name}.{method.Name}({string.Join(",", method.Parameters.Select(p => p.ParameterType.Name))}) → Task.CompletedTask");
            }
        }
    }
}

// Patch 4: Generic null guards for `static get_Instance()` singletons in monster/
// power/event/VFX state machines. Many enemy moves call XManager.get_Instance() then
// immediately invoke a method on the result; in headless mode the singleton is often
// null, so the unguarded callvirt throws NullReferenceException inside the async task
// chain → faulted task → ActionExecutor stalls → 10s boss-stuck signal → eval reports
// STUCK with hp>0 (BUG-037 / BUG-038 / BUG-039 family).
//
// Singletons covered: any type with `static T get_Instance()` returning its own type,
// e.g. NCombatRoom, NGame, NRunMusicController, CombatManager, RunManager, SaveManager,
// NDebugAudioManager, etc. The sink allowlist (IsSkippableSink) restricts patching to
// effect-style methods (Trigger/Play/Update/get_*/etc.) — load-bearing methods like
// ExecuteAttack are NOT patched, so game logic invariants stay intact.
//
// Namespace whitelist: Models.Monsters, Models.Powers, Models.Events, Nodes.Vfx —
// i.e. paths where a missing VFX/audio call is benign. Card/UI/CombatManager logic is
// excluded because their singleton interactions carry game state.
{
    bool ShouldGuardType(TypeDefinition declaringType)
    {
        var ns = declaringType?.Namespace ?? "";
        if (ns.Contains("Models.Monsters")) return true;
        if (ns.Contains("Models.Powers")) return true;
        if (ns.Contains("Models.Events")) return true;
        if (ns.Contains("Nodes.Vfx")) return true;
        return false;
    }

    // Methods/props safe to skip when the owning singleton is null.
    // CONSERVATIVE allowlist: only known-cosmetic effects. Earlier tried broader
    // allowlists with `Set/Add/Remove/Update/...` substring matches; that nuked
    // load-bearing CombatManager.SetReadyToEndTurn etc. → stuck rate exploded
    // (PhrogParasite/FuzzyWurmCrawler etc. all hung). Add new entries only when
    // a stack trace concretely points at a missing-effect call site.
    bool IsSkippableSink(string name)
    {
        // Visual / camera / hit-stop effects
        if (name == "DoHitStop") return true;
        if (name.Contains("ScreenShake") || name.Contains("ScreenRumble")) return true;
        if (name.Contains("ScreenShakeTrauma") || name.Contains("DoScreenFlash")) return true;
        if (name == "RadialBlur" || name == "GetViewportRect") return true;
        if (name == "ApplyDisplaySettings" || name == "ReturnToMainMenu") return true;
        // VFX scene-graph getters (return value used purely for animation)
        if (name == "GetCreatureNode") return true;
        if (name == "get_CombatVfxContainer" || name == "get_BackCombatVfxContainer") return true;
        if (name == "get_GlobalUi" || name == "get_SceneContainer") return true;
        if (name == "get_RootSceneContainer" || name == "get_Transition") return true;
        if (name == "get_Ui" || name == "get_MainMenu") return true;
        // Audio (NRunMusicController, NDebugAudioManager)
        if (name == "TriggerEliteSecondPhase") return true;
        if (name == "UpdateMusicParameter") return true;
        // NDebugAudioManager.Play/Stop are pure audio sinks (full-name match avoids
        // matching load-bearing methods like CombatManager.PlayCard).
        if (name == "Play" || name == "Stop") return true;
        return false;
    }

    foreach (var type in module.Types)
    {
        foreach (var nested in type.NestedTypes)
        {
            if (!nested.Name.Contains("d__")) continue;
            if (nested.DeclaringType == null) continue;
            if (!ShouldGuardType(nested.DeclaringType)) continue;

            foreach (var method in nested.Methods)
            {
                if (method.Name != "MoveNext" || method.Body == null) continue;

                bool restart = true;
                while (restart)
                {
                    restart = false;
                    var instrs = method.Body.Instructions.ToList();
                    var il = method.Body.GetILProcessor();
                    for (int idx = 0; idx < instrs.Count - 1; idx++)
                    {
                        var i0 = instrs[idx];
                        if (i0.OpCode != OpCodes.Call) continue;
                        if (i0.Operand is not MethodReference i0ref) continue;
                        // Match `static T get_Instance()` returning singleton's own type.
                        if (i0ref.Name != "get_Instance") continue;
                        if (i0ref.HasThis) continue;
                        var singletonType = i0ref.DeclaringType?.FullName;
                        if (singletonType == null) continue;
                        if (i0ref.ReturnType?.FullName != singletonType) continue;

                        var i1 = instrs[idx + 1];
                        if (i1.OpCode == OpCodes.Dup) continue;
                        if (i1.OpCode == OpCodes.Pop) continue;
                        if (i1.OpCode == OpCodes.Stloc || i1.OpCode == OpCodes.Stloc_0 ||
                            i1.OpCode == OpCodes.Stloc_1 || i1.OpCode == OpCodes.Stloc_2 ||
                            i1.OpCode == OpCodes.Stloc_3 || i1.OpCode == OpCodes.Stloc_S) continue;
                        if (i1.OpCode == OpCodes.Brfalse || i1.OpCode == OpCodes.Brfalse_S ||
                            i1.OpCode == OpCodes.Brtrue  || i1.OpCode == OpCodes.Brtrue_S) continue;

                        Instruction danger = null;
                        bool dangerProducesResult = false;
                        Instruction afterDanger = null;
                        for (int s = idx + 1; s <= Math.Min(idx + 8, instrs.Count - 1); s++)
                        {
                            var si = instrs[s];
                            if (si.OpCode != OpCodes.Callvirt && si.OpCode != OpCodes.Call) continue;
                            if (si.Operand is not MethodReference mref) break;

                            var declTy = mref.DeclaringType?.FullName ?? "";
                            bool onSingleton = declTy == singletonType;
                            var sname = mref.Name;
                            if (!onSingleton)
                            {
                                if (mref.ReturnType.FullName == "System.Void") break;
                                continue;
                            }
                            if (IsSkippableSink(sname))
                            {
                                danger = si;
                                dangerProducesResult = mref.ReturnType.FullName != "System.Void";
                                afterDanger = (s + 1 < instrs.Count) ? instrs[s + 1] : null;
                            }
                            break;
                        }
                        if (danger == null) continue;

                        bool storeAfter = afterDanger != null && (
                            afterDanger.OpCode == OpCodes.Stloc || afterDanger.OpCode == OpCodes.Stloc_0 ||
                            afterDanger.OpCode == OpCodes.Stloc_1 || afterDanger.OpCode == OpCodes.Stloc_2 ||
                            afterDanger.OpCode == OpCodes.Stloc_3 || afterDanger.OpCode == OpCodes.Stloc_S);

                        // SAFETY: if the danger callvirt produces a result that is NOT
                        // stored to a local immediately, the result is consumed by the
                        // next instruction (e.g. another callvirt on the returned object).
                        // On the null path our `pop; ldnull; <next>` would fail because:
                        //   - stloc-style: ldnull → stloc (target = stloc, OK)
                        //   - chained callvirt: ldnull → callvirt → NRE inside target
                        // Worse: when dangerProducesResult and !storeAfter, branching past
                        // the danger leaves the stack at the wrong depth for the next
                        // instruction → CLR rejects with InvalidProgramException
                        // (FuzzyWurmCrawler.AcidGoop hit this exact case 2026-05-06 when
                        // GetCreatureNode's result fed straight into Control::get_GlobalPosition).
                        // Refuse to patch this shape; the call site stays unguarded but the
                        // assembly stays verifiable.
                        if (dangerProducesResult && !storeAfter) continue;

                        Instruction target = (storeAfter && dangerProducesResult)
                            ? afterDanger
                            : (danger.Next ?? afterDanger);
                        if (target == null) continue;

                        var dup = il.Create(OpCodes.Dup);
                        var pop = il.Create(OpCodes.Pop);
                        var brSkip = il.Create(OpCodes.Br, target);
                        il.InsertAfter(i0, dup);
                        var brf = il.Create(OpCodes.Brfalse, pop);
                        il.InsertAfter(dup, brf);
                        il.InsertAfter(danger, brSkip);
                        il.InsertAfter(brSkip, pop);
                        if (storeAfter && dangerProducesResult)
                            il.InsertAfter(pop, il.Create(OpCodes.Ldnull));

                        patches++;
                        var shortSingleton = singletonType.Split('.').Last();
                        Console.WriteLine($"  Patched {nested.DeclaringType.Name}.{nested.Name} [{i0.Offset:X4}]: null-guard {shortSingleton} → {danger.Operand?.ToString()?.Split("::").Last().Split("(").First()}");
                        restart = true;
                        break;
                    }
                }
            }
        }
    }
}

// Patch 5: TalkCmd.Play → return null. Speech-bubble VFX has no scene root in headless
// and throws NullReferenceException synchronously, faulting the surrounding monster-move
// async task (e.g. KinPriest.RitualMove). Caller's downstream null derefs on the result
// are harmless (most callers drop the return value). Replacing the body with `ldnull;
// ret` skips the visual without affecting move logic.
foreach (var type in module.Types)
{
    if (type.FullName != "MegaCrit.Sts2.Core.Commands.TalkCmd") continue;
    foreach (var method in type.Methods)
    {
        if (method.Name != "Play" || method.Body == null) continue;
        if (method.ReturnType.FullName == "System.Void") continue;
        var il = method.Body.GetILProcessor();
        method.Body.Instructions.Clear();
        method.Body.Variables.Clear();
        method.Body.ExceptionHandlers.Clear();
        il.Emit(OpCodes.Ldnull);
        il.Emit(OpCodes.Ret);
        patches++;
        Console.WriteLine($"  Patched TalkCmd.Play({string.Join(",", method.Parameters.Select(p => p.ParameterType.Name))}) → null");
    }
}

// Patch 6: AssetCache.Get*(path) — replace `castclass T` with `isinst T` to prevent
// InvalidCastException when the cached object is the wrong type (e.g. a PackedScene
// stub injected at a path that the game's GetCompressedTexture2D expects).
// Without this, Vantom.DismemberMove and KinPriest VFX paths throw InvalidCastException
// inside the async task chain, faulting the task → ActionExecutor stalls → 10s
// boss-stuck signal → eval reports floor=17 hp>0 LOSS even though the agent could win.
{
    TypeDefinition? assetCache = null;
    foreach (var t in module.GetTypes())
    {
        if (t.Name == "AssetCache" && (t.Namespace?.Contains("Assets") ?? false))
        { assetCache = t; break; }
    }
    if (assetCache == null)
    {
        foreach (var t in module.GetTypes())
            if (t.Name == "AssetCache") { assetCache = t; break; }
    }
    if (assetCache != null)
    {
        foreach (var method in assetCache.Methods)
        {
            if (method.Body == null) continue;
            if (!method.Name.StartsWith("Get")) continue;
            if (method.ReturnType.FullName == "System.Void") continue;

            var il = method.Body.GetILProcessor();
            var instrs = method.Body.Instructions.ToList();
            bool changed = false;
            foreach (var ins in instrs)
            {
                if (ins.OpCode == OpCodes.Castclass)
                {
                    var newIns = il.Create(OpCodes.Isinst, (TypeReference)ins.Operand);
                    il.Replace(ins, newIns);
                    changed = true;
                }
            }
            if (changed)
            {
                patches++;
                Console.WriteLine($"  Patched AssetCache.{method.Name}: castclass → isinst (cast-safe)");
            }
        }
    }
    else
    {
        Console.WriteLine("  [WARN] AssetCache type not found — Patch 5 skipped");
    }
}

Console.WriteLine($"Applied {patches} patches");
var outPath = dllPath + ".patched";
module.Write(outPath);
module.Dispose();
File.Delete(dllPath);
File.Move(outPath, dllPath);
Console.WriteLine("Done!");
CSHARP

REPO_DIR="$(pwd)"
cd "$PATCH_DIR"
$DOTNET run -- "$REPO_DIR/lib/sts2.dll" 2>&1
cd "$REPO_DIR"
rm -rf "$PATCH_DIR"

# ── Build ──

echo ""
echo "🏗️ Building..."
$DOTNET build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -5

echo ""
echo "✅ Setup complete!"
echo ""
echo "To play:"
echo "  python3 python/play.py"
echo ""
echo "To run batch games:"
echo "  python3 python/play_full_run.py 10"
