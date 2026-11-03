// src/Sts2Advisor.Core/CampaignPlan.cs
namespace Sts2Advisor.Core;

/// <summary>Where the game is, as far as the campaign cares. Run: a single-player run is loaded and playable
/// (no game-over screen, no fade). GameOver: the run just ended. MainMenu: the main menu itself, no submenu, no popup.
/// Submenu: something on the main menu's submenu stack (timeline, character select, settings ...).
/// Modal: a popup. Unknown: loading, fading, the logo.</summary>
public enum CampaignScreen { Unknown, Run, GameOver, MainMenu, Submenu, Modal }

/// <summary>How the autopilot last stopped. Player: someone took over (hotkey, panel button, single step).
/// Retriable: a failure that a restart may cure (restarted once per floor). Service: the advice service was
/// unreachable -- it is a separate process that may still be starting or restarting, so the restart is tried
/// again as often as <see cref="CampaignAttempts"/> allows. Fatal: it cannot run at all. None: it ended with the
/// run or never stopped.</summary>
public enum CampaignStop { None, Player, Retriable, Service, Fatal }

/// <param name="StopText">"Stuck: nothing to do for 30 s at ..." -- the last stop, for the log and the panel.</param>
/// <param name="Floor">"A2F5": the key of the once-per-floor restart.</param>
/// <param name="HandBackScreen">The screen the running autopilot has handed to the player (it has no advice for it:
/// choose a relic, crystal sphere ...), and for how long (<paramref name="HandBackSeconds"/>).</param>
/// <param name="LastRunCharacter">The character of the run that ended last (this session, else the last line of
/// campaign_runs.jsonl); where the file's "rotation" goes on from.</param>
/// <param name="Ladder">The ladder strategy's input (levels, done characters, the last run); only read when the file's
/// strategy is "ladder".</param>
public sealed record CampaignFacts(
    CampaignScreen Screen,
    bool AutopilotRunning = false,
    bool Multiplayer = false,
    bool HasSavedRun = false,
    CampaignStop LastStop = CampaignStop.None,
    string? StopText = null,
    double SecondsSinceStop = 0,
    bool RestartedThisFloor = false,
    string Floor = "",
    double ModalSeconds = 0,
    string ModalKey = "",
    double SubmenuSeconds = 0,
    string? HandBackScreen = null,
    double HandBackSeconds = 0,
    string? LastRunCharacter = null,
    LadderState? Ladder = null);

public enum CampaignStepKind { Idle, StartAutopilot, FinishRun, ContinueRun, StartRun, CloseModal, LeaveSubmenu, HandleScreen, Quit, StopAutopilot }

/// <param name="Why">What the step is for, or why there is nothing to do; goes to the log and the panel.</param>
/// <param name="Key">Identifies what a step acts on: repeating the same kind and key without effect is retried with
/// a growing delay and then given up (<see cref="CampaignAttempts"/>); a new key starts afresh.</param>
/// <param name="Target">StartRun: the run to start (from "rotation", else "next").</param>
public sealed record CampaignStep(CampaignStepKind Kind, string Why, string Key = "", CampaignTarget? Target = null);

public readonly record struct ResolvedAscension(int Level, bool Clamped);

/// <summary>The campaign's brain, apart from the game: the control file, a snapshot of the game and the autopilot's
/// last stop in; the next thing to do out. A run that has just ended is the GameOver screen: FinishRun records it
/// and clicks through to the main menu, where the next step is StartRun with the file's "rotation" entry after the
/// run that ended, else its "next" -- or ContinueRun when a saved run exists (a run is never abandoned for a new one).
/// Everything is read from the settings handed in, every time: an edit of the file is seen by the next plan.</summary>
public static class CampaignPlan
{
    /// <summary>A popup (a first-time tip, say) is the player's for this long before the campaign closes it.</summary>
    public const double ModalGraceSeconds = 5;

    /// <summary>The autopilot's failure stops are restarted after this long, once per floor.</summary>
    public const double RestartDelaySeconds = 10;

    /// <summary>A submenu on the main menu (the timeline after a run) is left after this long.</summary>
    public const double SubmenuGraceSeconds = 3;

    /// <summary>A screen the autopilot hands to the player (no advice exists for it) waits this long for them; then,
    /// since nobody is there, AutoSlay's own handler for that screen plays it.</summary>
    public const double HandBackGraceSeconds = 60;

    /// <summary>AscensionManager.maxAscensionAllowed.</summary>
    public const int GameMaxAscension = 10;

    public static CampaignStep Next(CampaignSettings? s, CampaignFacts f)
    {
        if (s is not { Enabled: true })
            return Idle("campaign off");

        // The commands come first, on any screen: "quit" on the game-over screen too (the quit step records the ended
        // run first -- its data is on that screen -- and does not click through), "stop" likewise.
        if (s.Command == CampaignCommand.Quit)
            return new CampaignStep(CampaignStepKind.Quit, "command: quit", "quit");
        if (s.Command == CampaignCommand.Stop)
            return new CampaignStep(CampaignStepKind.StopAutopilot, "command: stop", "stop");

        switch (f.Screen)
        {
            case CampaignScreen.Modal:
                return f.ModalSeconds >= ModalGraceSeconds
                    ? new CampaignStep(CampaignStepKind.CloseModal, "a popup is open", f.ModalKey)
                    : Idle("a popup is open");

            case CampaignScreen.GameOver:
                return new CampaignStep(CampaignStepKind.FinishRun, "the run is over", "finish");

            case CampaignScreen.Run:
                return NextInRun(f);

            case CampaignScreen.MainMenu:
                if (f.HasSavedRun)
                    return new CampaignStep(CampaignStepKind.ContinueRun, "a saved run exists", "continue");
                if (s.Ladder != null)
                    return NextLadder(s.Ladder, f.Ladder);
                if (NextTarget(s, f.LastRunCharacter) is not { } target)
                    return Idle("no run in progress and campaign.json has no \"rotation\" or \"next\" to start");
                return new CampaignStep(CampaignStepKind.StartRun,
                                        $"no run in progress: start {target.Character} {CampaignText.Level(target.Ascension)}",
                                        $"{target.Character}/{target.Ascension}", target);

            case CampaignScreen.Submenu:
                return f.SubmenuSeconds >= SubmenuGraceSeconds
                    ? new CampaignStep(CampaignStepKind.LeaveSubmenu, "a menu screen is open", "submenu")
                    : Idle("a menu screen is open");

            default:
                return Idle("the game is loading");
        }
    }

    /// <summary>The ladder's pick (<see cref="CampaignLadder"/>), started at its level ("max"); "ladder complete" when
    /// every listed character has won at ascension 10.</summary>
    private static CampaignStep NextLadder(IReadOnlyList<string> characters, LadderState? state)
    {
        if (state == null)
            return Idle("the ladder has no game state to pick from");
        var pick = CampaignLadder.Pick(characters, state);
        if (pick.Character == null)
            return Idle("ladder complete");
        var target = new CampaignTarget(pick.Character, AscensionSpec.Max);
        return new CampaignStep(CampaignStepKind.StartRun, CampaignLadder.Describe(pick, characters, state),
                                $"{target.Character}/{target.Ascension}", target);
    }

    private static CampaignStep NextInRun(CampaignFacts f)
    {
        if (f.Multiplayer)
            return Idle("a multiplayer run is not played by the campaign");
        if (f.AutopilotRunning)
        {
            if (f.HandBackScreen != null && f.HandBackSeconds >= HandBackGraceSeconds)
                return new CampaignStep(CampaignStepKind.HandleScreen,
                                        $"the autopilot has waited {f.HandBackSeconds:0} s for the player on {f.HandBackScreen}", f.HandBackScreen);
            return Idle("autopilot running");
        }
        switch (f.LastStop)
        {
            case CampaignStop.Player:
                return Idle("the autopilot was stopped by hand; start it again to resume");
            case CampaignStop.Fatal:
                return Idle($"needs attention: the autopilot cannot run ({f.StopText})");
            case CampaignStop.Service:
            case CampaignStop.Retriable:
                if (f.LastStop == CampaignStop.Retriable && f.RestartedThisFloor)
                    return Idle($"needs attention: the autopilot stopped again on {f.Floor} after a restart ({f.StopText})");
                if (f.SecondsSinceStop < RestartDelaySeconds)
                    return Idle($"the autopilot stopped ({f.StopText}); restarting it {RestartDelaySeconds:0} s after the stop");
                return new CampaignStep(CampaignStepKind.StartAutopilot, $"restarting the autopilot after: {f.StopText}", f.Floor);
            default:
                return new CampaignStep(CampaignStepKind.StartAutopilot, "a run is in progress", f.Floor);
        }
    }

    /// <summary>The run the campaign starts next: the "rotation" entry after <paramref name="lastRunCharacter"/> when
    /// the file has a rotation, else "next", else none. With the ladder strategy: the ladder's pick.</summary>
    public static CampaignTarget? NextTarget(CampaignSettings? s, string? lastRunCharacter, LadderState? ladder = null)
    {
        if (s == null)
            return null;
        if (s.Ladder != null)
            return ladder != null && CampaignLadder.Pick(s.Ladder, ladder).Character is { } who ? new CampaignTarget(who, AscensionSpec.Max) : null;
        if (CampaignRotation.Next(s.Rotation, lastRunCharacter) is { } fromRotation)
            return fromRotation;
        return s.Character != null && s.Ascension != null ? new CampaignTarget(s.Character, s.Ascension.Value) : null;
    }

    /// <summary>Whether this reading of campaign.json interrupts a flow that is under way (finish run, start run,
    /// continue ...): only switching the campaign off and the "stop" and "quit" commands do. A new "next" or
    /// "rotation" does not: the flow finishes and the new values are read when they are needed. A half-written file
    /// interrupts nothing. The quit flow itself is not interrupted by a command (it started with one).</summary>
    public static bool InterruptsFlow(CampaignRead read, bool flowIsQuit)
    {
        if (read.Problem != null)
            return false;
        if (read.Settings is not { Enabled: true } s)
            return true;
        return !flowIsQuit && s.Command is CampaignCommand.Quit or CampaignCommand.Stop;
    }

    /// <summary>"max" is the highest ascension the character has unlocked; a level above that is clamped down to it
    /// (and the caller logs that). Never above the game's own cap of 10.</summary>
    public static ResolvedAscension ResolveAscension(AscensionSpec spec, int unlockedMax)
    {
        int max = Math.Clamp(unlockedMax, 0, GameMaxAscension);
        if (spec.IsMax)
            return new ResolvedAscension(max, false);
        return spec.Level > max ? new ResolvedAscension(max, true) : new ResolvedAscension(spec.Level, false);
    }

    public static CampaignStop Classify(StopReason? reason) => reason switch
    {
        null or StopReason.GameOver or StopReason.LeftRun => CampaignStop.None,
        StopReason.Hotkey or StopReason.StepDone => CampaignStop.Player,
        StopReason.Offline => CampaignStop.Service,
        StopReason.Stuck or StopReason.HandlerFailed or StopReason.BadReply => CampaignStop.Retriable,
        StopReason.Multiplayer or StopReason.Unsupported or StopReason.Disabled => CampaignStop.Fatal,
        _ => throw new ArgumentOutOfRangeException(nameof(reason), reason, "a stop reason the campaign has no policy for"),
    };

    private static CampaignStep Idle(string why) => new(CampaignStepKind.Idle, why);
}
