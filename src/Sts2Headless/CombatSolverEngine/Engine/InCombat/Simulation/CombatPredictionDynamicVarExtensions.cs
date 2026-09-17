using HarmonyLib;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using CombatSolver.Engine.Common;
using MegaCrit.Sts2.Core.Modding;

namespace CombatSolver.Engine.InCombat.Simulation;

internal static class CombatPredictionDynamicVarExtensions
{
    private delegate DynamicVar GetDynamicVarDelegate(CalculatedVar calculatedVar);

    private static readonly GetDynamicVarDelegate GetBaseVar =
        AccessTools.Method(typeof(CalculatedVar), "GetBaseVar").CreateDelegate<GetDynamicVarDelegate>();

    private static readonly GetDynamicVarDelegate GetExtraVar =
        AccessTools.Method(typeof(CalculatedVar), "GetExtraVar").CreateDelegate<GetDynamicVarDelegate>();

    public static decimal InvokeCalculate(
        this DynamicVar dynamicVar,
        CombatPredictionSimulator simulator,
        PredictedCard card,
        Creature? target)
    {
        // Upstream also matches STS2RitsuLib.Cards.DynamicVars.IComputedDynamicVar here, for
        // RitsuLib-authored custom cards' computed dynamic values. This headless build never
        // loads RitsuLib, so no DynamicVar instance can ever implement that interface; the
        // branch is unreachable dead code here and is dropped rather than stubbed. Any
        // non-CalculatedVar (including what would have been a computed var) already falls
        // back to BaseValue below, which is the same effective behavior.
        return dynamicVar switch
        {
            CalculatedVar calculatedVar =>
                calculatedVar.InvokeCalculate(simulator, card, target),
            _ => dynamicVar.BaseValue
        };
    }

    public static decimal InvokeCalculate(
        this CalculatedVar calculatedVar,
        CombatPredictionSimulator simulator,
        PredictedCard card,
        Creature? target)
    {
        using var _ = simulator.PushActionSource(card.Original, PredictionActionKind.DynamicVariableCalculation);
        if (CalculatedVarSpecRegistry.TryCalculate(calculatedVar, simulator, card, target, out decimal value))
            return value;
        simulator.History.RecordRisk(PredictionRiskReason.MethodMirrorIncomplete);
        var mod = AssemblyInfo.ModForType(card.Preview.GetType(), out bool isBaseGame);
        if (!isBaseGame && mod?.manifest?.id is { Length: > 0 } modId)
            throw new IncompatibleGameplayModException(modId, mod.manifest.name ?? modId,
                $"card {card.Preview.Id.Entry}: calculated variable has no branch-local specification", "combat");
        throw new NotSupportedException(
            $"Card {card.Preview.Id.Entry} has no branch-local calculated variable specification.");
    }
}
