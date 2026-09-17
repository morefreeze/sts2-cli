using System.Reflection;
using MegaCrit.Sts2.Core.Models;

namespace CombatSolver.Engine.InCombat.Mirrors.Cards.OnPlay;

using CardOnPlayAction = Action<CardModel, CardOnPlayMirrorContext>;

/// <summary>
/// Infers simple, directly invoked vanilla command templates from an unregistered <see cref="CardModel.OnPlay" />.
/// </summary>
/// <remarks>
/// Upstream implements this by reading the override method's original (unpatched) IL via
/// <c>STS2RitsuLib.Utils.HarmonyIl</c> and pattern-matching straight-line Attack/Block/Draw
/// call sequences. This headless build never loads RitsuLib, so that IL reader is unavailable.
///
/// This is a best-effort fallback registered <em>after</em> the large hand-written per-card
/// mirror table in <see cref="CardOnPlayMirrors" /> (see its <c>RegisterStrictInferrer</c> /
/// <c>RegisterInferrer</c> calls): <see cref="MethodMirrorRegistry{TBase,TContext}.ResolveLookupUnderLock" />
/// only consults it for a card whose OnPlay override has no explicit registration. When both
/// inferrers return null, dispatch falls through to <c>MirrorDispatchKind.Unsupported</c>, which
/// records a "not mirrored" prediction risk instead of throwing -- the same degrade-gracefully
/// path already used for any other unmirrored override. Returning null here is therefore safe:
/// it trades best-effort inference for a handful of rarely-hit unregistered cards for a clean
/// build, at the cost of those cards' predictions being flagged as incomplete rather than
/// silently (and only approximately) inferred from IL.
/// </remarks>
internal static class CardOnPlayInferrer
{
    public static CardOnPlayAction? Infer(Type runtimeType, MethodInfo overrideMethod) => null;

    public static CardOnPlayAction? InferStrict(Type runtimeType, MethodInfo overrideMethod) => null;
}
