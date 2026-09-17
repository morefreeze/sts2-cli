using HarmonyLib;
using MegaCrit.Sts2.Core.Localization.DynamicVars;
using MegaCrit.Sts2.Core.Models;

namespace CombatSolver;

// A 5th RitsuLib touchpoint, discovered while vendoring the rest of Runtime/ for Task 3 (the
// original triage only found 4). Upstream implements this as an `IPatchMethod` (from
// STS2RitsuLib.Patching.Models) that RitsuLib's patcher installs as a real Harmony patch on
// PowerModel.get_DynamicVars via Entry.cs's `patcher.RegisterPatch<PowerDynamicVarMaterializationGuardPatch>()`.
// This headless build never runs Entry.Initialize() and never installs that patch.
//
// The only consumer in this vendored tree is
// Engine/Common/NativeModelCloneConcurrency.HasDefaultPowerInitialization, which does
// `AccessTools.Method(typeof(PowerDynamicVarMaterializationGuardPatch), "Prefix")` purely to get a
// MethodInfo to compare against `Harmony.GetPatchInfo(...)`'s prefix list -- it never installs or
// invokes the patch either. Since no patch is ever actually applied in this build,
// `Harmony.GetPatchInfo` on `PowerModel.get_DynamicVars` returns null/empty, so that comparison
// always evaluates to "not patched" regardless of what this class's body does. Dropping the
// `IPatchMethod` interface (and PatchId/Description/GetTargets, which only RitsuLib's patcher
// reads) and keeping just a same-signature `Prefix` method preserves that outcome exactly while
// removing the RitsuLib dependency.
internal sealed class PowerDynamicVarMaterializationGuardPatch
{
    private static readonly AccessTools.FieldRef<PowerModel, DynamicVarSet?> DynamicVarsField =
        AccessTools.FieldRefAccess<PowerModel, DynamicVarSet?>("_dynamicVars");

    [HarmonyPriority(Priority.First)]
    public static void Prefix(PowerModel __instance)
    {
        if (SimulationNotificationIsolation.IsActive && DynamicVarsField(__instance) == null)
        {
            throw new InvalidOperationException(
                $"后台模拟尝试惰性创建 Power 显示变量：power={__instance.Id.Entry}；" +
                "该实例必须在主线程根捕获阶段完成物化。");
        }
    }
}
