using System.Diagnostics.CodeAnalysis;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.CompilerServices;
using HarmonyLib;

namespace STS2RitsuLib.Utils.HarmonyIl;

// Hand-written stand-in for the small used surface of RitsuLib's `STS2RitsuLib.Utils.HarmonyIl`
// -- NOT a vendored file, unlike everything else in this directory. RitsuLib is a Steam Workshop
// mod library that this headless build does not have (and cannot load: it is Godot/mod-loader
// coupled). The only consumer in the vendored tree is Engine/InCombat/Mirrors/Cards/OnPlay/
// CardOnPlayInferrer.cs, which is kept VERBATIM from upstream and pulls this namespace in with
// `using STS2RitsuLib.Utils.HarmonyIl;`. That is why the types below live in exactly that
// namespace and use exactly the names/signatures the inferrer calls -- so the vendored file
// compiles unchanged. Do not replace this with a copy of RitsuLib; if upstream's inferrer starts
// using more of the RitsuLib surface, extend this shim (and VENDORED.md) by hand.
//
// What the inferrer needs (and all this file provides):
//   - `MethodInfo.GetOriginalIl()` -> `HarmonyIlMethodBody { Instructions }`
//   - `HarmonyIl.TryGetCalledMethod`, `HarmonyIl.TryGetLocalLoadIndex`, `HarmonyIl.LoadsInt32`
//
// The inferrer was written against RitsuLib's semantics, and they matter:
//
//   * ORIGINAL IL. It must see the game's unpatched IL. This build applies Harmony patches to
//     some game methods, so the IL is read with `PatchProcessor.GetOriginalInstructions`
//     (the method body as compiled, not the patched/transpiled one).
//
//   * ASYNC MoveNext. Card `OnPlay` overrides are `async` methods: the method Reflection hands
//     over is only a stub that starts the compiler-generated state machine, and the real body
//     (the Attack/Block/Draw calls) is in the state machine's `MoveNext`. The inferrer's
//     heuristics are written for MoveNext IL: it ignores `stfld` to the [AsyncStateMachine]
//     type, skips conditional branches preceded by `get_IsCompleted`, and treats a
//     `ldloc.0; brfalse` near the start as the state switch ("local 0" is the cached state).
//     So for a method carrying [AsyncStateMachine] this returns MoveNext's IL; otherwise the
//     method's own IL.
//
//   * CALLS. `TryGetCalledMethod` only reports `call`/`callvirt` whose operand is a
//     `MethodInfo`. A `newobj`/base-constructor `call` carries a `ConstructorInfo` (a
//     `MethodBase`, not a `MethodInfo`) and is deliberately not reported: the inferrer matches
//     command methods (AttackCommand.Execute, CreatureCmd.GainBlock, CardPileCmd.Draw, getters)
//     and its strict mode rejects on any unrecognised game-namespace *method* call.
//
// This shim contains no algorithm logic -- it only decodes CodeInstructions. Every decision about
// what a card does lives in the verbatim CardOnPlayInferrer.

/// <summary>
/// The original (unpatched) IL of one method, as Harmony instructions.
/// </summary>
internal sealed class HarmonyIlMethodBody(MethodBase method, IReadOnlyList<CodeInstruction> instructions)
{
    /// <summary>The method whose IL was read (an async method's state machine <c>MoveNext</c>, not the async method).</summary>
    public MethodBase Method { get; } = method;

    public IReadOnlyList<CodeInstruction> Instructions { get; } = instructions;
}

internal static class HarmonyIlMethodExtensions
{
    private const BindingFlags MoveNextFlags =
        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly;

    /// <summary>
    /// Reads the original, unpatched IL that actually executes for <paramref name="method"/>: the state machine's
    /// <c>MoveNext</c> for an <c>async</c> method, the method itself otherwise. Throws when the IL cannot be read
    /// (for example an abstract or extern method); callers decide whether that is fatal.
    /// </summary>
    public static HarmonyIlMethodBody GetOriginalIl(this MethodInfo method)
    {
        ArgumentNullException.ThrowIfNull(method);

        MethodBase body = method;
        // inherit: false -- the state machine belongs to this exact method, not to one it overrides.
        Type? stateMachine = method.GetCustomAttribute<AsyncStateMachineAttribute>(inherit: false)?.StateMachineType;
        if (stateMachine is not null)
        {
            body = stateMachine.GetMethod("MoveNext", MoveNextFlags)
                ?? throw new InvalidOperationException(
                    $"Async state machine {stateMachine.FullName} of {method.DeclaringType?.FullName}.{method.Name} has no MoveNext method.");
        }

        return new HarmonyIlMethodBody(body, PatchProcessor.GetOriginalInstructions(body));
    }
}

internal static class HarmonyIl
{
    private static readonly OpCode[] LoadSmallInt32 =
    [
        OpCodes.Ldc_I4_0, OpCodes.Ldc_I4_1, OpCodes.Ldc_I4_2, OpCodes.Ldc_I4_3, OpCodes.Ldc_I4_4,
        OpCodes.Ldc_I4_5, OpCodes.Ldc_I4_6, OpCodes.Ldc_I4_7, OpCodes.Ldc_I4_8,
    ];

    private static readonly OpCode[] LoadShortLocal =
        [OpCodes.Ldloc_0, OpCodes.Ldloc_1, OpCodes.Ldloc_2, OpCodes.Ldloc_3];

    /// <summary>
    /// True for a <c>call</c>/<c>callvirt</c> whose operand is a <see cref="MethodInfo"/>. Constructor calls
    /// (<c>newobj</c>, base-constructor <c>call</c>) have a <c>ConstructorInfo</c> operand and return false.
    /// </summary>
    public static bool TryGetCalledMethod(CodeInstruction instruction, [NotNullWhen(true)] out MethodInfo? calledMethod)
    {
        if ((instruction.opcode == OpCodes.Call || instruction.opcode == OpCodes.Callvirt)
            && instruction.operand is MethodInfo method)
        {
            calledMethod = method;
            return true;
        }

        calledMethod = null;
        return false;
    }

    /// <summary>
    /// True for a local-variable load (<c>ldloc.0</c>..<c>ldloc.3</c>, <c>ldloc.s</c>, <c>ldloc</c>) and yields the
    /// local's index. Harmony gives the long forms either a <c>LocalBuilder</c> or a raw integer operand, and both
    /// are accepted. <c>ldloca</c> (load the local's address) is deliberately not a load of the local: the only
    /// use is recognising the async state switch, <c>ldloc.0; brfalse</c>, which is always a value load.
    /// </summary>
    public static bool TryGetLocalLoadIndex(CodeInstruction instruction, out int localIndex)
    {
        int shortIndex = Array.IndexOf(LoadShortLocal, instruction.opcode);
        if (shortIndex >= 0)
        {
            localIndex = shortIndex;
            return true;
        }

        if (instruction.opcode == OpCodes.Ldloc_S || instruction.opcode == OpCodes.Ldloc)
        {
            switch (instruction.operand)
            {
                case LocalVariableInfo local: // LocalBuilder derives from LocalVariableInfo
                    localIndex = local.LocalIndex;
                    return true;
                case byte index:
                    localIndex = index;
                    return true;
                case ushort index:
                    localIndex = index;
                    return true;
                case short index:
                    localIndex = index;
                    return true;
                case int index:
                    localIndex = index;
                    return true;
            }
        }

        localIndex = -1;
        return false;
    }

    /// <summary>
    /// True when the instruction pushes the constant <paramref name="value"/> as an int32:
    /// <c>ldc.i4.m1</c>, <c>ldc.i4.0</c>..<c>ldc.i4.8</c>, <c>ldc.i4.s</c> or <c>ldc.i4</c>.
    /// </summary>
    public static bool LoadsInt32(CodeInstruction instruction, int value)
    {
        OpCode opcode = instruction.opcode;
        if (opcode == OpCodes.Ldc_I4_M1)
            return value == -1;

        int small = Array.IndexOf(LoadSmallInt32, opcode);
        if (small >= 0)
            return value == small;

        if (opcode == OpCodes.Ldc_I4_S)
        {
            return instruction.operand switch
            {
                sbyte operand => operand == value,
                byte operand => operand == value,
                short operand => operand == value,
                int operand => operand == value,
                _ => false,
            };
        }

        return opcode == OpCodes.Ldc_I4 && instruction.operand is int wide && wide == value;
    }
}
