// src/Sts2Advisor/Campaign/CampaignActions.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Helpers;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Characters;
using MegaCrit.Sts2.Core.Multiplayer;
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Nodes.Audio;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events.Custom.CrystalSphere;
using MegaCrit.Sts2.Core.Nodes.Ftue;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.MainMenu;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Timeline;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Timeline.Epochs;
using Sts2Advisor.Core;

namespace Sts2Advisor.Campaign;

/// <summary>The campaign cannot go on and retrying will not help (an unknown character in the file, a saved run that
/// cannot be read). Latches the campaign as stuck at once.</summary>
internal sealed class CampaignStuckException(string why) : Exception(why);

/// <summary>What the campaign does to the game. Everything goes through the game's own buttons and calls -- the
/// ones its AutoSlay autoplayer and its own screens use -- and never through AutoSlayer.Start(), which abandons
/// the run, writes unlocks to the save and quits. Main thread only.</summary>
internal static class CampaignActions
{
    /// <summary>The game-over screen offers no button for this long: the attempt is given up (and retried with backoff).
    /// Restarted after every click, so a long victory summary (score lines, badges) is fine as long as it ends.</summary>
    private static readonly TimeSpan GameOverStageTimeout = TimeSpan.FromSeconds(120);
    private const int GameOverPollMs = 250;
    private static readonly TimeSpan GameOverReclickDelay = TimeSpan.FromSeconds(3);
    private static readonly TimeSpan TimelineTimeout = TimeSpan.FromSeconds(100);

    // -- game over -> main menu -------------------------------------------------------------------------------

    /// <summary>The game-over screen's two buttons -- Continue (to the run summary, NGameOverContinueButton), then the
    /// return to the main menu (NReturnToMainMenuButton, shown and enabled once the summary has played) -- pressed the
    /// way AutoSlay's GameOverScreenHandler presses them, but as a loop that looks at the screen every time instead of
    /// a script that waits for the buttons in order: whichever button is usable is pressed, a button that an earlier
    /// (interrupted) attempt already pressed is simply not there to wait for, and the flow ends when the screen is
    /// gone. The game then goes to the timeline if the run discovered epochs, else to the main menu
    /// (NGameOverScreen.OnMainMenuButtonPressed). The decision itself is <see cref="CampaignGameOver"/>.</summary>
    public static async Task LeaveGameOverAsync(CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + GameOverStageTimeout;
        GameOverAction? logged = null, lastClick = null;
        var lastClickAt = DateTime.MinValue;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var screen = CampaignObserver.GameOver();
            bool open = screen != null && GodotObject.IsInstanceValid(screen);
            var view = new GameOverView(open, open && ContinueEnabled(screen!), open && MainMenuReady(screen!));
            var action = CampaignGameOver.Next(view);
            // A pressed button disables itself at once; one that is still enabled a moment later was not pressed after
            // all, but it is not hammered every poll either.
            if (action is GameOverAction.ClickContinue or GameOverAction.ClickMainMenu && action == lastClick
                && DateTime.UtcNow - lastClickAt < GameOverReclickDelay)
                action = GameOverAction.Wait;
            if (action != logged)
            {
                logged = action;
                ModLog.Info($"campaign: game over screen: {action} (continue enabled {view.ContinueEnabled}, main menu button ready {view.MainMenuReady})");
            }
            switch (action)
            {
                case GameOverAction.Done:
                    return;
                case GameOverAction.ClickContinue:
                    await UiHelper.Click(screen!._continueButton);
                    lastClick = action;
                    lastClickAt = DateTime.UtcNow;
                    deadline = lastClickAt + GameOverStageTimeout;
                    logged = null;  // say what comes after the click
                    break;
                case GameOverAction.ClickMainMenu:
                    await UiHelper.Click(screen!._mainMenuButton);
                    lastClick = action;
                    lastClickAt = DateTime.UtcNow;
                    deadline = lastClickAt + GameOverStageTimeout;
                    logged = null;
                    break;
            }
            if (DateTime.UtcNow > deadline)
                throw new TimeoutException($"the game-over screen offered no button for {GameOverStageTimeout.TotalSeconds:0} s");
            await Task.Delay(GameOverPollMs, ct);
        }
    }

    private static bool ContinueEnabled(NGameOverScreen screen) =>
        GodotObject.IsInstanceValid(screen._continueButton) && screen._continueButton.IsEnabled;

    /// <summary>What the handler waits for before pressing the main menu button: it exists, is shown and is enabled.</summary>
    private static bool MainMenuReady(NGameOverScreen screen) =>
        GodotObject.IsInstanceValid(screen._mainMenuButton) && screen._mainMenuButton.Visible && screen._mainMenuButton.IsEnabled;

    // -- main menu -> new run ---------------------------------------------------------------------------------

    /// <summary>Starts a single-player standard run the way the character-select Embark ends: the same
    /// <c>NGame.StartNewSingleplayerRun</c> call with a random seed, the seed's act list, no modifiers and the ascension
    /// the lobby would have clamped to. It skips the menus (and with them their first-time popups) but keeps the
    /// transition and music handling of NCharacterSelectScreen.BeginRun / StartNewSingleplayerRun.</summary>
    public static async Task StartRunAsync(string characterId, AscensionSpec spec, CancellationToken ct)
    {
        var game = NGame.Instance ?? throw new InvalidOperationException("no NGame");
        if (game.MainMenu == null)
            throw new InvalidOperationException("not on the main menu");
        var character = ModelDb.AllCharacters.FirstOrDefault(c => string.Equals(c.Id.Entry, characterId, StringComparison.OrdinalIgnoreCase))
            ?? throw new CampaignStuckException(
                $"campaign.json: unknown character {characterId} (this game has {string.Join(", ", ModelDb.AllCharacters.Select(c => c.Id.Entry))})");
        if (CampaignObserver.HasSavedRun())
            throw new InvalidOperationException("a saved run exists: it is continued, not replaced");

        int unlocked = UnlockedMaxAscension(character);
        var ascension = CampaignPlan.ResolveAscension(spec, unlocked);
        if (ascension.Clamped)
            ModLog.Info($"campaign: ascension {spec} is above what {character.Id.Entry} has unlocked ({unlocked}); using a{ascension.Level}");

        string seed = SeedHelper.GetRandomSeed();
        // StartRunLobby.BeginRunLocally: the act list comes from the seed.
        var acts = ActModel.GetRandomList(new Rng(StringHelper.GetDeterministicHashCode(seed), "act_selection"),
                                          SaveManager.Instance.GenerateUnlockStateFromProgress(), false).ToList();
        ModLog.Info($"campaign: starting {character.Id.Entry} a{ascension.Level} (seed {seed})");

        ct.ThrowIfCancellationRequested();
        NAudioManager.Instance?.StopMusic();
        // NMainMenu's Continue does this for a single-player run (a new run from the menus gets it from the lobby).
        game.ReactionContainer.InitializeNetworking(new NetSingleplayerGameService());
        SfxCmd.Play(character.CharacterTransitionSfx);
        await game.Transition.FadeOut(0.8f, character.CharacterSelectTransitionPath);
        try
        {
            await game.StartNewSingleplayerRun(character, true, acts, Array.Empty<ModifierModel>(), seed, GameMode.Standard, ascension.Level);
        }
        catch (Exception e)
        {
            ModLog.Error($"campaign: starting the run failed: {e}");
            try { await game.ReturnToMainMenuWithInternalError(e); }  // what the character-select screen does
            catch (Exception e2) { ModLog.Error($"campaign: returning to the main menu failed too: {e2.Message}"); }
            throw;
        }
    }

    /// <summary>The highest ascension the character may start at, as StartRunLobby shows it: its CharacterStats'
    /// MaxAscension, or 0 until the timeline has revealed the character's ascension epoch.</summary>
    public static int UnlockedMaxAscension(CharacterModel character)
    {
        var stats = SaveManager.Instance.Progress.GetOrCreateCharacterStats(character.Id);
        return AscensionEpochRevealed(character) ? stats.MaxAscension : 0;
    }

    private static bool AscensionEpochRevealed(CharacterModel c)
    {
        var save = SaveManager.Instance;
        if (c.Id == ModelDb.GetId<Ironclad>()) return save.IsEpochRevealed<Ironclad4Epoch>();
        if (c.Id == ModelDb.GetId<Silent>()) return save.IsEpochRevealed<Silent4Epoch>();
        if (c.Id == ModelDb.GetId<Regent>()) return save.IsEpochRevealed<Regent4Epoch>();
        if (c.Id == ModelDb.GetId<Defect>()) return save.IsEpochRevealed<Defect4Epoch>();
        if (c.Id == ModelDb.GetId<Necrobinder>()) return save.IsEpochRevealed<Necrobinder4Epoch>();
        return true;
    }

    // -- main menu -> saved run -------------------------------------------------------------------------------

    /// <summary>The main menu's own Continue button (NMainMenu.OnContinueButtonPressed loads the save it read in
    /// RefreshButtons). A save that cannot be read is never replaced: the game would only show its error popup.</summary>
    public static async Task ContinueRunAsync(CancellationToken ct)
    {
        var menu = NGame.Instance?.MainMenu ?? throw new InvalidOperationException("not on the main menu");
        var read = menu._readRunSaveResult ?? throw new InvalidOperationException("the main menu has not read the saved run yet");
        if (read is not { Success: true, SaveData: { } save })
            throw new CampaignStuckException($"the saved run cannot be read (status {read.Status}); the campaign does not abandon it");
        if (!menu._continueButton.IsEnabled)
            throw new InvalidOperationException("the Continue button is not enabled yet");
        ModLog.Info($"campaign: continuing saved run ({save.Players.FirstOrDefault()?.CharacterId?.Entry} a{save.Ascension})");
        ct.ThrowIfCancellationRequested();
        await UiHelper.Click(menu._continueButton);
        // The click starts an async load that ends with the menu scene replaced by the run's; stay busy until then so
        // that the next tick does not click a Continue button that is already disabled.
        await WaitHelper.Until(() => NGame.Instance?.MainMenu == null, ct, TimeSpan.FromSeconds(90), "the saved run did not load");
    }

    // -- popups -----------------------------------------------------------------------------------------------

    /// <summary>Closes the open popup if it is one of the harmless kinds (first-time tips, error popups, the
    /// early-access note); returns what it did. Any other popup (the quit and abandon confirmations, the mod
    /// loading question) is not clicked blindly.</summary>
    public static string CloseModal()
    {
        var modal = NModalContainer.Instance?.OpenModal as Node ?? throw new InvalidOperationException("no popup is open");
        string name = modal.GetType().Name;
        switch (modal)
        {
            case NAcceptTutorialsFtue:
                VerticalPopupOf(modal).NoButton.ForceClick();
                return $"{name}: declined";
            case NFtue:
                (UiHelper.FindFirst<NFtueConfirmButton>(modal) ?? throw new InvalidOperationException($"{name}: no confirm button")).ForceClick();
                return $"{name}: confirmed";
            case NErrorPopup:
                VerticalPopupOf(modal).YesButton.ForceClick();  // its OK (or report-a-bug) button
                return $"{name}: dismissed";
            case NEarlyAccessDisclaimer disclaimer:
                TaskHelper.RunSafely(disclaimer.CloseScreen());
                return $"{name}: closed";
            default:
                throw new InvalidOperationException($"the campaign does not close {name} popups by itself");
        }
    }

    /// <summary>The popup's own NVerticalPopup child with the Yes/No buttons (NErrorPopup and NAcceptTutorialsFtue both
    /// hold one at "VerticalPopup"; the node class NErrorPopup derives from NVerticalPopup, but its buttons are the child's).</summary>
    private static NVerticalPopup VerticalPopupOf(Node modal) =>
        modal.GetNodeOrNull<NVerticalPopup>("VerticalPopup")
        ?? UiHelper.FindAll<NVerticalPopup>(modal).LastOrDefault()
        ?? throw new InvalidOperationException($"{modal.GetType().Name}: no popup buttons");

    // -- submenus ---------------------------------------------------------------------------------------------

    /// <summary>Gets back to the bare main menu: the timeline (which the game opens after a run that discovered
    /// epochs) is worked through -- reveal the epoch slots, confirm the unlock screens -- and any other submenu is
    /// popped, as its back button would.</summary>
    public static async Task LeaveSubmenuAsync(CancellationToken ct)
    {
        var stack = NGame.Instance?.MainMenu?.SubmenuStack ?? throw new InvalidOperationException("not on the main menu");
        switch (stack.Peek())
        {
            case null:
                return;
            case NTimelineScreen timeline:
                await LeaveTimelineAsync(timeline, ct);
                return;
            case var other:
                ModLog.Info($"campaign: closing {other.GetType().Name}");
                stack.Pop();
                return;
        }
    }

    private static async Task LeaveTimelineAsync(NTimelineScreen timeline, CancellationToken ct)
    {
        var stack = NGame.Instance!.MainMenu!.SubmenuStack;
        var deadline = DateTime.UtcNow + TimelineTimeout;
        ModLog.Info($"campaign: timeline open ({SaveManager.Instance.GetDiscoveredEpochCount()} epoch(s) to reveal)");
        while (DateTime.UtcNow < deadline)
        {
            ct.ThrowIfCancellationRequested();
            if (!GodotObject.IsInstanceValid(timeline) || stack.Peek() != timeline)
                return;
            if (timeline.CurrentUnlockScreen is { } unlock)
            {
                if (UiHelper.FindFirst<NUnlockConfirmButton>(unlock) is { IsEnabled: true } confirm)
                {
                    ModLog.Info($"campaign: timeline: confirming {unlock.GetType().Name}");
                    await UiHelper.Click(confirm, 300);
                }
                await Task.Delay(800, ct);
                continue;
            }
            if (timeline._inputBlocker.Visible)  // the screen is animating; EnableInput hides the blocker
            {
                await Task.Delay(500, ct);
                continue;
            }
            if (timeline._inspectScreen.IsVisibleInTree())
            {
                timeline._inspectScreen.Close();
                await Task.Delay(800, ct);
                continue;
            }
            var slot = UiHelper.FindAll<NEpochSlot>(timeline).FirstOrDefault(s => s.State == EpochSlotState.Obtained && s.IsEnabled);
            if (slot != null)
            {
                ModLog.Info($"campaign: timeline: revealing {slot.model.GetType().Name}");
                await UiHelper.Click(slot, 300);
                await Task.Delay(1500, ct);
                continue;
            }
            if (SaveManager.Instance.GetDiscoveredEpochCount() == 0)
            {
                stack.Pop();  // what the timeline's back button does once everything is revealed
                return;
            }
            await Task.Delay(1000, ct);
        }
        ModLog.Error("campaign: timeline: gave up revealing epochs; leaving it (runs start from the menu regardless)");
        if (GodotObject.IsInstanceValid(timeline) && stack.Peek() == timeline)
            stack.Pop();
    }

    // -- a screen the autopilot has no advice for ---------------------------------------------------------------

    /// <summary>The autopilot hands a screen it has no advice for (choose a relic, the crystal sphere) to the player and
    /// waits. When nobody comes, AutoSlay's own handler for that screen plays it (a random pick), so an unattended
    /// campaign is not parked there for the night.</summary>
    public static async Task HandleScreenAsync(CancellationToken ct)
    {
        var overlay = NOverlayStack.Instance?.Peek();
        IScreenHandler handler = overlay switch
        {
            NChooseARelicSelection => new ChooseARelicScreenHandler(),
            NCrystalSphereScreen => new CrystalSphereScreenHandler(),
            _ => throw new InvalidOperationException($"AutoSlay has no handler for {overlay?.GetType().Name ?? "this screen (not an overlay)"}"),
        };
        ModLog.Info($"campaign: nobody answered {overlay.GetType().Name}; letting AutoSlay's {handler.GetType().Name} play it");
        var rng = new Rng(0uL);
        await WaitHelper.WithTimeout(t => handler.HandleAsync(rng, t), handler.Timeout, ct);
    }

    // -- quit -------------------------------------------------------------------------------------------------

    /// <summary>Quits the way the main menu's quit button does after its confirmation: NGame.Quit() saves the
    /// settings, preferences, progress and profile and ends the game. A run in progress is saved by the game on
    /// every room entry; a write in flight is waited for. The file's command is cleared BEFORE the quit and the
    /// quit is not done if that fails: a "quit" left in the file would quit the game again on every start.</summary>
    public static async Task QuitAsync(string campaignFilePath, CancellationToken ct)
    {
        for (int i = 0; i < 60 && SaveManager.Instance.CurrentRunSaveTask is { IsCompleted: false }; i++)
            await Task.Delay(250, ct);
        if (!CampaignFile.ClearCommand(campaignFilePath))
            throw new CampaignStuckException($"{CampaignFile.FileName}: cannot rewrite it with command null, so the game does not quit (it would quit again on every start)");
        ModLog.Info("campaign: command quit: quitting the game");
        (NGame.Instance ?? throw new InvalidOperationException("no NGame")).Quit();
    }
}
