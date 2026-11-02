// src/Sts2Advisor.Core.Tests/CampaignPlanTests.cs
using Sts2Advisor.Core;

public class CampaignPlanTests
{
    private static CampaignSettings On(CampaignCommand command = CampaignCommand.None, string? character = "IRONCLAD",
                                       AscensionSpec? ascension = null) =>
        new(true, character, character == null ? null : ascension ?? AscensionSpec.Max, command);

    private static CampaignStep Next(CampaignSettings? s, CampaignFacts f) => CampaignPlan.Next(s, f);

    private static CampaignFacts At(CampaignScreen screen) => new(screen);

    // -- off ---------------------------------------------------------------------------------------------------

    [Fact]
    public void WithoutAFileTheCampaignDoesNothing()
    {
        var step = Next(null, At(CampaignScreen.MainMenu));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
    }

    [Fact]
    public void ADisabledCampaignDoesNothingOnAnyScreen()
    {
        var off = new CampaignSettings(false, "IRONCLAD", AscensionSpec.Max, CampaignCommand.None);
        foreach (var screen in Enum.GetValues<CampaignScreen>())
            Assert.Equal(CampaignStepKind.Idle, Next(off, At(screen)).Kind);
    }

    // -- run in progress ---------------------------------------------------------------------------------------

    [Fact]
    public void ARunInProgressWithTheAutopilotOffStartsFullAuto()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, Floor: "A1F3"));
        Assert.Equal(CampaignStepKind.StartAutopilot, step.Kind);
    }

    [Fact]
    public void ARunningAutopilotIsLeftAlone()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, AutopilotRunning: true));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
    }

    [Fact]
    public void AScreenTheAutopilotHandedToThePlayerIsPlayedByAutoSlayAfterAMinute()
    {
        var early = Next(On(), new CampaignFacts(CampaignScreen.Run, AutopilotRunning: true, HandBackScreen: "NChooseARelicSelection", HandBackSeconds: 30));
        Assert.Equal(CampaignStepKind.Idle, early.Kind);
        var late = Next(On(), new CampaignFacts(CampaignScreen.Run, AutopilotRunning: true, HandBackScreen: "NChooseARelicSelection", HandBackSeconds: 60));
        Assert.Equal(CampaignStepKind.HandleScreen, late.Kind);
        Assert.Equal("NChooseARelicSelection", late.Key);
    }

    [Fact]
    public void NothingIsHandledWhenNothingWasHandedBack()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, AutopilotRunning: true, HandBackSeconds: 500));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
    }

    [Fact]
    public void AMultiplayerRunIsNotTouched()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, Multiplayer: true));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Contains("multiplayer", step.Why, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void WhenThePlayerStoppedTheAutopilotTheCampaignLeavesItStopped()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Player, SecondsSinceStop: 600));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
    }

    [Fact]
    public void AFailureStopIsRestartedAfterTenSeconds()
    {
        var early = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Retriable, SecondsSinceStop: 3, Floor: "A2F5"));
        Assert.Equal(CampaignStepKind.Idle, early.Kind);
        var late = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Retriable, SecondsSinceStop: 10, Floor: "A2F5"));
        Assert.Equal(CampaignStepKind.StartAutopilot, late.Kind);
    }

    [Fact]
    public void ASecondFailureOnTheSameFloorNeedsAttention()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Retriable, SecondsSinceStop: 60,
                                                RestartedThisFloor: true, Floor: "A2F5", StopText: "Stuck: nothing to do"));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Contains("needs attention", step.Why);
        Assert.Contains("A2F5", step.Why);
    }

    [Fact]
    public void AnOfflineAdviceServiceIsWaitedOutNotCountedAgainstTheFloor()
    {
        // The advice service is a separate process that may take a while to come up after the game starts.
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Service, SecondsSinceStop: 12,
                                                RestartedThisFloor: true, Floor: "A1F1"));
        Assert.Equal(CampaignStepKind.StartAutopilot, step.Kind);
        var early = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Service, SecondsSinceStop: 2));
        Assert.Equal(CampaignStepKind.Idle, early.Kind);
    }

    [Fact]
    public void AFatalStopIsNotRetried()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.Run, LastStop: CampaignStop.Fatal, SecondsSinceStop: 60, StopText: "Unsupported"));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Contains("needs attention", step.Why);
    }

    [Fact]
    public void TheRestartKeyIsTheFloorSoTheAttemptCounterStartsOverOnTheNextFloor()
    {
        var a = Next(On(), new CampaignFacts(CampaignScreen.Run, Floor: "A1F3"));
        var b = Next(On(), new CampaignFacts(CampaignScreen.Run, Floor: "A1F4"));
        Assert.NotEqual(a.Key, b.Key);
    }

    // -- run over ----------------------------------------------------------------------------------------------

    [Fact]
    public void TheGameOverScreenIsFinishedWhateverTheAutopilotIsDoing()
    {
        Assert.Equal(CampaignStepKind.FinishRun, Next(On(), At(CampaignScreen.GameOver)).Kind);
        Assert.Equal(CampaignStepKind.FinishRun, Next(On(), new CampaignFacts(CampaignScreen.GameOver, AutopilotRunning: true)).Kind);
    }

    // -- main menu ---------------------------------------------------------------------------------------------

    [Fact]
    public void TheMainMenuStartsTheNextRunOfTheFile()
    {
        var step = Next(On(character: "REGENT", ascension: AscensionSpec.Of(6)), At(CampaignScreen.MainMenu));
        Assert.Equal(CampaignStepKind.StartRun, step.Kind);
        Assert.Contains("REGENT", step.Key);
        Assert.Contains("6", step.Key);
        Assert.Contains("REGENT a6", step.Why);
    }

    [Fact]
    public void AMaxAscensionReadsAsMaxNotAsA()
    {
        var step = Next(On(), At(CampaignScreen.MainMenu));
        Assert.Contains("IRONCLAD max", step.Why);
    }

    [Fact]
    public void ASavedRunIsContinuedNeverAbandonedForANewOne()
    {
        var step = Next(On(), new CampaignFacts(CampaignScreen.MainMenu, HasSavedRun: true));
        Assert.Equal(CampaignStepKind.ContinueRun, step.Kind);
    }

    [Fact]
    public void WithoutANextTheMainMenuCannotStartARun()
    {
        var step = Next(On(character: null), At(CampaignScreen.MainMenu));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Contains("next", step.Why);
    }

    [Fact]
    public void WithoutANextASavedRunIsStillContinued()
    {
        var step = Next(On(character: null), new CampaignFacts(CampaignScreen.MainMenu, HasSavedRun: true));
        Assert.Equal(CampaignStepKind.ContinueRun, step.Kind);
    }

    // -- popups and submenus -----------------------------------------------------------------------------------

    [Fact]
    public void APopupIsGivenAMomentBeforeItIsClosed()
    {
        Assert.Equal(CampaignStepKind.Idle, Next(On(), new CampaignFacts(CampaignScreen.Modal, ModalSeconds: 1)).Kind);
        var step = Next(On(), new CampaignFacts(CampaignScreen.Modal, ModalSeconds: 5, ModalKey: "NFtue#42"));
        Assert.Equal(CampaignStepKind.CloseModal, step.Kind);
        Assert.Equal("NFtue#42", step.Key);
    }

    [Fact]
    public void ASubmenuIsLeftAfterAShortWait()
    {
        Assert.Equal(CampaignStepKind.Idle, Next(On(), new CampaignFacts(CampaignScreen.Submenu, SubmenuSeconds: 1)).Kind);
        Assert.Equal(CampaignStepKind.LeaveSubmenu, Next(On(), new CampaignFacts(CampaignScreen.Submenu, SubmenuSeconds: 3)).Kind);
    }

    [Fact]
    public void AnUnknownScreenIsWaitedOut()
    {
        var step = Next(On(), At(CampaignScreen.Unknown));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.False(string.IsNullOrEmpty(step.Why));
    }

    // -- commands ----------------------------------------------------------------------------------------------

    [Theory]
    [InlineData(CampaignScreen.Run)]
    [InlineData(CampaignScreen.GameOver)]
    [InlineData(CampaignScreen.MainMenu)]
    [InlineData(CampaignScreen.Modal)]
    [InlineData(CampaignScreen.Submenu)]
    [InlineData(CampaignScreen.Unknown)]
    public void QuitComesBeforeEverythingOnAnyScreen(CampaignScreen screen)
    {
        // Including the game-over screen: the control records the run (its data is on that screen) as part of the quit step.
        Assert.Equal(CampaignStepKind.Quit, Next(On(CampaignCommand.Quit), At(screen)).Kind);
    }

    [Fact]
    public void QuitBeatsAnAutopilotThatIsStillRunningAndASavedRun()
    {
        Assert.Equal(CampaignStepKind.Quit,
            Next(On(CampaignCommand.Quit), new CampaignFacts(CampaignScreen.MainMenu, AutopilotRunning: true, HasSavedRun: true)).Kind);
    }

    [Theory]
    [InlineData(CampaignScreen.Run)]
    [InlineData(CampaignScreen.GameOver)]
    [InlineData(CampaignScreen.MainMenu)]
    [InlineData(CampaignScreen.Unknown)]
    public void StopComesBeforeEverything(CampaignScreen screen)
    {
        Assert.Equal(CampaignStepKind.StopAutopilot, Next(On(CampaignCommand.Stop), At(screen)).Kind);
    }

    // -- what interrupts a flow that is under way ----------------------------------------------------------------

    private static CampaignRead Read(string json) => CampaignFile.Parse(json);

    [Theory]
    [InlineData("""{"enabled": true, "next": {"character": "REGENT", "ascension": "max"}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "NECROBINDER", "ascension": 3}, "command": null}""")]
    [InlineData("""{"enabled": true, "rotation": ["REGENT", "SILENT"]}""")]
    [InlineData("""{"enabled": true, "rotation": [], "next": {"character": "DEFECT", "ascension": 1}}""")]
    public void ChangingNextOrRotationNeverInterruptsAFlow(string json)
    {
        Assert.False(CampaignPlan.InterruptsFlow(Read(json), flowIsQuit: false));
    }

    [Theory]
    [InlineData("""{"enabled": true, "command": "quit"}""")]
    [InlineData("""{"enabled": true, "next": {"character": "REGENT", "ascension": "max"}, "command": "stop"}""")]
    [InlineData("""{"enabled": false}""")]
    [InlineData("")]
    public void QuitStopAndSwitchingTheCampaignOffInterruptAFlow(string json)
    {
        Assert.True(CampaignPlan.InterruptsFlow(Read(json), flowIsQuit: false));
    }

    [Fact]
    public void AHalfWrittenFileDoesNotInterruptAFlow()
    {
        var half = Read("{\"enabled\": true,");
        Assert.NotNull(half.Problem);
        Assert.False(CampaignPlan.InterruptsFlow(half, flowIsQuit: false));
    }

    [Fact]
    public void TheQuitFlowIsNotInterruptedByTheCommandThatStartedIt()
    {
        Assert.False(CampaignPlan.InterruptsFlow(Read("""{"enabled": true, "command": "quit"}"""), flowIsQuit: true));
        Assert.False(CampaignPlan.InterruptsFlow(Read("""{"enabled": true, "command": "stop"}"""), flowIsQuit: true));
        Assert.True(CampaignPlan.InterruptsFlow(Read("""{"enabled": false}"""), flowIsQuit: true));  // switched off: no quit after all
    }

    // -- ascension ---------------------------------------------------------------------------------------------

    [Fact]
    public void MaxIsTheCharactersHighestUnlockedAscension()
    {
        Assert.Equal(new ResolvedAscension(6, false), CampaignPlan.ResolveAscension(AscensionSpec.Max, 6));
    }

    [Fact]
    public void ALevelWithinTheUnlockedRangeIsKept()
    {
        Assert.Equal(new ResolvedAscension(3, false), CampaignPlan.ResolveAscension(AscensionSpec.Of(3), 6));
        Assert.Equal(new ResolvedAscension(6, false), CampaignPlan.ResolveAscension(AscensionSpec.Of(6), 6));
        Assert.Equal(new ResolvedAscension(0, false), CampaignPlan.ResolveAscension(AscensionSpec.Of(0), 6));
    }

    [Fact]
    public void ALevelAboveTheUnlockedMaxIsClampedDownAndSaysSo()
    {
        Assert.Equal(new ResolvedAscension(4, true), CampaignPlan.ResolveAscension(AscensionSpec.Of(9), 4));
    }

    [Fact]
    public void NothingIsEverAboveTheGamesOwnCap()
    {
        Assert.Equal(new ResolvedAscension(10, false), CampaignPlan.ResolveAscension(AscensionSpec.Max, 12));
        Assert.Equal(new ResolvedAscension(10, true), CampaignPlan.ResolveAscension(AscensionSpec.Of(11), 12));
    }

    [Fact]
    public void ACharacterWithoutAscensionsOnlyPlaysLevelZero()
    {
        Assert.Equal(new ResolvedAscension(0, false), CampaignPlan.ResolveAscension(AscensionSpec.Max, 0));
        Assert.Equal(new ResolvedAscension(0, true), CampaignPlan.ResolveAscension(AscensionSpec.Of(2), 0));
    }

    // -- stops -------------------------------------------------------------------------------------------------

    [Theory]
    [InlineData(null, CampaignStop.None)]
    [InlineData(StopReason.GameOver, CampaignStop.None)]
    [InlineData(StopReason.LeftRun, CampaignStop.None)]
    [InlineData(StopReason.Hotkey, CampaignStop.Player)]
    [InlineData(StopReason.StepDone, CampaignStop.Player)]
    [InlineData(StopReason.Stuck, CampaignStop.Retriable)]
    [InlineData(StopReason.HandlerFailed, CampaignStop.Retriable)]
    [InlineData(StopReason.Offline, CampaignStop.Service)]
    [InlineData(StopReason.BadReply, CampaignStop.Retriable)]
    [InlineData(StopReason.Multiplayer, CampaignStop.Fatal)]
    [InlineData(StopReason.Unsupported, CampaignStop.Fatal)]
    [InlineData(StopReason.Disabled, CampaignStop.Fatal)]
    public void EveryStopReasonIsClassified(StopReason? reason, CampaignStop expected)
    {
        Assert.Equal(expected, CampaignPlan.Classify(reason));
    }

    [Fact]
    public void NoStopReasonIsLeftUnclassified()
    {
        foreach (var reason in Enum.GetValues<StopReason>())
            _ = CampaignPlan.Classify(reason);  // throws on a new reason nobody thought about
    }
}
