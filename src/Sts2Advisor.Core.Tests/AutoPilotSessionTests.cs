// src/Sts2Advisor.Core.Tests/AutoPilotSessionTests.cs
using Sts2Advisor.Core;

public class AutoPilotSessionTests
{
    [Fact]
    public void StartsInAdviceModeAndStartsOnce()
    {
        var s = new AutoPilotSession();
        Assert.Equal(AutoPilotMode.Advice, s.Mode);
        Assert.True(s.Start());
        Assert.Equal(AutoPilotMode.Running, s.Mode);
        Assert.False(s.Start());
    }

    [Fact]
    public void StopRecordsTheReasonAndClearsTransientState()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.HandBack("NChooseARelicSelection");
        s.InCombat = true;
        s.Stop(StopReason.Stuck, "NRewardsScreen did not close");
        Assert.Equal(AutoPilotMode.Advice, s.Mode);
        Assert.Equal(StopReason.Stuck, s.LastStop);
        Assert.Equal("NRewardsScreen did not close", s.LastStopDetail);
        Assert.Null(s.HandBackScreen);
        Assert.False(s.InCombat);
    }

    [Fact]
    public void StartClearsTheLastRun()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null);
        s.Stop(StopReason.Hotkey, null);
        s.Start();
        Assert.Empty(s.Steps);
        Assert.Equal(0, s.StepCount);
        Assert.Null(s.LastStop);
    }

    [Fact]
    public void KeepsTheNewestStepsFirst()
    {
        var s = new AutoPilotSession();
        s.Start();
        for (int i = 1; i <= AutoPilotSession.MaxSteps + 2; i++)
            Assert.Equal(i, s.Record($"step {i}", i == 1 ? "why" : null));
        Assert.Equal(AutoPilotSession.MaxSteps, s.Steps.Count);
        Assert.Equal("step 8", s.Steps[0].Text);
        Assert.Equal(8, s.Steps[0].Number);
        Assert.Equal("step 3", s.Steps[^1].Text);
        Assert.Equal(8, s.StepCount);
    }

    [Fact]
    public void OfflineOnlyCountsWhenTenSecondsPass()
    {
        var s = new AutoPilotSession();
        Assert.False(s.Offline(100));
        Assert.False(s.Offline(109.9));
        Assert.True(s.Offline(110.1));
        s.Online();
        Assert.False(s.Offline(200));
    }

    [Fact]
    public void AdviceRetriesAreCappedPerFingerprintAndResetByAStep()
    {
        var s = new AutoPilotSession();
        for (int i = 0; i < AutoPilotSession.MaxAdviceRetries; i++)
            Assert.True(s.CountAdviceRetry("f1"));
        Assert.False(s.CountAdviceRetry("f1"));
        Assert.True(s.CountAdviceRetry("f2"));
        for (int i = 1; i < AutoPilotSession.MaxAdviceRetries; i++)
            s.CountAdviceRetry("f2");
        s.Record("picked", null);
        Assert.True(s.CountAdviceRetry("f2"));
    }

    [Fact]
    public void AlternatingFingerprintsStillHitTheTotalCapUntilAStep()
    {
        var s = new AutoPilotSession();
        for (int i = 0; i < AutoPilotSession.MaxTotalAdviceRetries; i++)
            Assert.True(s.CountAdviceRetry(i % 2 == 0 ? "f1" : "f2"));
        Assert.False(s.CountAdviceRetry("f1"));
        s.Record("picked", null);
        Assert.True(s.CountAdviceRetry("f2"));
    }

    [Fact]
    public void HandBackIsSetAndCleared()
    {
        var s = new AutoPilotSession();
        s.HandBack("NCrystalSphereScreen");
        Assert.Equal("NCrystalSphereScreen", s.HandBackScreen);
        s.ClearHandBack();
        Assert.Null(s.HandBackScreen);
    }

    [Fact]
    public void SingleStepIsDoneAfterOneStep()
    {
        var s = new AutoPilotSession();
        Assert.True(s.Start(singleStep: true));
        Assert.True(s.SingleStep);
        Assert.False(s.StepDone);
        s.Record("地图：问号", null);
        Assert.True(s.StepDone);
        s.Stop(StopReason.StepDone, null);
        Assert.True(s.Start());
        Assert.False(s.SingleStep);
        s.Record("x", null);
        Assert.False(s.StepDone);
    }

    [Fact]
    public void RecordKeepsTheDetailsOfEachStep()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null);
        s.Record("b", "why", new[] { "line 1", "line 2" });
        Assert.Equal(new[] { "line 1", "line 2" }, s.Steps[0].Details);
        Assert.Empty(s.Steps[1].Details);
    }

    [Fact]
    public void NoStepYetMeansNothingToHold()
    {
        var s = new AutoPilotSession();
        s.Start();
        Assert.Equal(0, s.HoldRemaining(100, 2.5));
    }

    [Fact]
    public void AStepIsHeldForThePauseAfterItsTime()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null, null, at: 100);
        Assert.Equal(2.5, s.HoldRemaining(100, 2.5), 6);
        Assert.Equal(1.0, s.HoldRemaining(101.5, 2.5), 6);
        Assert.Equal(0, s.HoldRemaining(102.5, 2.5));
        Assert.Equal(0, s.HoldRemaining(200, 2.5));
    }

    [Fact]
    public void ThePauseOfTheNewestStepCounts()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null, null, at: 100);
        s.Record("b", null, null, at: 110);
        Assert.Equal(2.0, s.HoldRemaining(110.5, 2.5), 6);
    }

    [Fact]
    public void AZeroOrNegativePauseNeverHolds()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null, null, at: 100);
        Assert.Equal(0, s.HoldRemaining(100, 0));
        Assert.Equal(0, s.HoldRemaining(100, -3));
    }

    [Fact]
    public void ASingleStepIsNeverHeld()
    {
        var s = new AutoPilotSession();
        s.Start(singleStep: true);
        s.Record("a", null, null, at: 100);
        Assert.Equal(0, s.HoldRemaining(100, 2.5));
    }

    [Fact]
    public void AnUntimedStepIsNeverHeldAndStartForgetsTheLastRun()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null);
        Assert.Equal(0, s.HoldRemaining(100, 2.5));
        s.Record("b", null, null, at: 100);
        s.Stop(StopReason.Hotkey, null);
        s.Start();
        Assert.Equal(0, s.HoldRemaining(100, 2.5));
    }

    [Fact]
    public void TheHoldNeverExceedsThePauseEvenIfTheClockMovesBack()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null, null, at: 100);
        Assert.Equal(2.5, s.HoldRemaining(50, 2.5), 6);
    }
}
