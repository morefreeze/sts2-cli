// The CLI loads vanilla game models only and never initializes RitsuLib or BaseLib.
// These surfaces let the verbatim v0.113 Solver retain its optional-mod checks.
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Localization.DynamicVars;

namespace STS2RitsuLib
{
    internal static class RitsuLibFramework
    {
        // Native CardPileCmd.Draw's unmodified hand limit.
        internal static int GetMaxHandSize(Player _) => 10;
    }
}
namespace STS2RitsuLib.Cards.DynamicVars
{
    internal interface IComputedDynamicVar { }
    internal abstract class ComputedDynamicVar : IComputedDynamicVar { }
    internal static class DynamicVarTooltipRegistry
    {
        public static void CopyTo(DynamicVar _, DynamicVar __) { }
    }
}
namespace CombatSolver
{
    // Neither is installed. NativeModelCloneConcurrency only compares MethodInfo identities
    // after finding real BaseLib/Ritsu patch types, which are absent in this process.
    internal static class BaseLibDynamicVarCloneMetadataPatch { public static void Prefix() { } }
    internal static class RitsuDynamicVarCloneMetadataPatch { public static void Prefix() { } }
}
