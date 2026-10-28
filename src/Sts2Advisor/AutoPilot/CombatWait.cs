// src/Sts2Advisor/AutoPilot/CombatWait.cs
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;

namespace Sts2Advisor.AutoPilot;

/// <summary>Combat belongs to CombatSolver (or the player, when it pauses full-auto): only wait. No timeout --
/// a time-budgeted solver turn can take minutes.</summary>
internal static class CombatWait
{
    public static async Task UntilOverAsync(CancellationToken ct)
    {
        while (CombatManager.Instance?.IsInProgress == true)
            await Task.Delay(250, ct);
    }

    /// <summary>Replaces EventRoomHandler.HandleEventCombat (HandlerPatches), which gives the player
    /// 100 Strength/Plating/Regen and kills the enemies outright.</summary>
    public static async Task ForEventCombat(EventRoomHandler handler, CancellationToken ct)
    {
        // The cheat only for the game's own AutoSlay; a stopped autopilot's old handler must not run it either.
        if (AutoSlayer.IsActive && !AutoPilotChoice.Active)
        {
            await handler.HandleEventCombat(ct);
            return;
        }
        var host = AutoPilotChoice.Host;
        if (host != null)
            host.InCombat = true;
        try
        {
            await WaitHelper.Until(() => CombatManager.Instance.IsInProgress, ct, AutoSlayConfig.nodeWaitTimeout, "Event combat not started");
            await UntilOverAsync(ct);
        }
        finally
        {
            if (host != null)
                host.InCombat = false;
        }
    }
}
