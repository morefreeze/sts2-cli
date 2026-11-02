// src/Sts2Advisor/Campaign/CampaignObserver.cs
using Godot;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using Sts2Advisor.Core;

namespace Sts2Advisor.Campaign;

/// <summary>What the game shows right now, in the campaign's terms. <paramref name="Floor"/> is "A2F5" (act, act-local
/// floor) while a run is loaded, else "". <paramref name="Run"/> is the loaded run, if any.</summary>
internal readonly record struct Observation(CampaignScreen Screen, bool Multiplayer, string ModalKey, string Floor,
                                            bool HasSavedRun, RunState? Run);

/// <summary>Reads the screen. Main thread only; reads, never writes.</summary>
internal static class CampaignObserver
{
    public static Observation Observe()
    {
        var game = NGame.Instance;
        if (game == null)
            return new Observation(CampaignScreen.Unknown, false, "", "", false, null);

        var rm = RunManager.Instance;
        RunState? rs = rm is { IsInProgress: true, IsCleaningUp: false } ? rm.DebugOnlyGetState() : null;
        bool inRun = rs != null && NRun.Instance != null;
        string floor = rs == null ? "" : $"A{rs.CurrentActIndex + 1}F{rs.ActFloor}";
        bool saved = HasSavedRun();

        // A popup blocks everything under it, in a run too (a first-time tip over the map, say).
        if (NModalContainer.Instance?.OpenModal is GodotObject modal && GodotObject.IsInstanceValid(modal)
            && (modal as Node)?.IsInsideTree() != false)
            return new Observation(CampaignScreen.Modal, false, ModalKey(modal), floor, saved, rs);

        if (inRun)
        {
            if (NOverlayStack.Instance?.Peek() is NGameOverScreen)
                return new Observation(CampaignScreen.GameOver, false, "", floor, saved, rs);
            if (rs!.Players.Count != 1)
                return new Observation(CampaignScreen.Run, true, "", floor, saved, rs);
            // A run that is still being set up has no room yet: not a place to start the autopilot.
            return new Observation(rs.CurrentRoom == null ? CampaignScreen.Unknown : CampaignScreen.Run, false, "", floor, saved, rs);
        }

        if (game.MainMenu is { SubmenuStack: { } stack })
        {
            if (game.Transition.InTransition)
                return new Observation(CampaignScreen.Unknown, false, "", "", saved, null);
            return new Observation(stack.SubmenusOpen ? CampaignScreen.Submenu : CampaignScreen.MainMenu,
                                   false, "", "", saved, null);
        }
        return new Observation(CampaignScreen.Unknown, false, "", floor, saved, rs);
    }

    /// <summary>The single-player run save the main menu would offer to continue.</summary>
    public static bool HasSavedRun()
    {
        try { return SaveManager.Instance?.HasRunSave == true; }
        catch (Exception) { return false; }
    }

    public static string ModalKey(GodotObject modal) => $"{modal.GetType().Name}#{modal.GetInstanceId()}";

    public static NGameOverScreen? GameOver() => NOverlayStack.Instance?.Peek() as NGameOverScreen;
}
