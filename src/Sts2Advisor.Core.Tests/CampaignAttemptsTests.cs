// src/Sts2Advisor.Core.Tests/CampaignAttemptsTests.cs
using Sts2Advisor.Core;

public class CampaignAttemptsTests
{
    [Fact]
    public void TheFirstAttemptGoesAtOnce()
    {
        var a = new CampaignAttempts();
        var v = a.Begin("start/IRONCLAD", now: 100);
        Assert.Equal(AttemptVerdict.Go, v.Kind);
        Assert.Equal(1, v.Attempt);
    }

    [Fact]
    public void AnAttemptThatChangedNothingWaitsBeforeTheNext()
    {
        var a = new CampaignAttempts();
        a.Begin("k", 100);
        var soon = a.Begin("k", 101);
        Assert.Equal(AttemptVerdict.Wait, soon.Kind);
        Assert.True(soon.RetryAt > 101);
        var later = a.Begin("k", soon.RetryAt);
        Assert.Equal(AttemptVerdict.Go, later.Kind);
        Assert.Equal(2, later.Attempt);
    }

    [Fact]
    public void TheDelayGrowsWithEveryAttempt()
    {
        var a = new CampaignAttempts();
        double now = 0;
        double last = 0;
        for (int i = 1; i <= CampaignAttempts.MaxAttempts; i++)
        {
            var v = a.Begin("k", now);
            Assert.Equal(AttemptVerdict.Go, v.Kind);
            double gap = CampaignAttempts.DelayAfter(i);
            Assert.True(gap >= last, $"delay after attempt {i} ({gap}) is shorter than the one before ({last})");
            last = gap;
            now += gap;
        }
    }

    [Fact]
    public void WhenTheLastAttemptHasHadItsTimeTheCampaignIsStuck()
    {
        var a = new CampaignAttempts();
        double now = 0;
        for (int i = 1; i <= CampaignAttempts.MaxAttempts; i++)
        {
            Assert.Equal(AttemptVerdict.Go, a.Begin("k", now).Kind);
            now += CampaignAttempts.DelayAfter(i);
        }
        var v = a.Begin("k", now);
        Assert.Equal(AttemptVerdict.Stuck, v.Kind);
        Assert.Equal(CampaignAttempts.MaxAttempts, v.Attempt);
    }

    [Fact]
    public void BetweenTheLastAttemptAndTheVerdictItJustWaits()
    {
        var a = new CampaignAttempts();
        double now = 0;
        for (int i = 1; i <= CampaignAttempts.MaxAttempts; i++)
        {
            a.Begin("k", now);
            now += CampaignAttempts.DelayAfter(i);
        }
        Assert.Equal(AttemptVerdict.Wait, a.Begin("k", now - 1).Kind);
    }

    [Fact]
    public void ANewKeyStartsAfresh()
    {
        var a = new CampaignAttempts();
        a.Begin("modal#1", 0);
        a.Begin("modal#1", CampaignAttempts.DelayAfter(1));
        var v = a.Begin("modal#2", 1);
        Assert.Equal(AttemptVerdict.Go, v.Kind);
        Assert.Equal(1, v.Attempt);
    }

    [Fact]
    public void AttemptsOlderThanTenMinutesAreForgotten()
    {
        // StartRun for the same character comes round again after every run; it must not add up to "stuck".
        var a = new CampaignAttempts();
        double now = 0;
        for (int i = 0; i < CampaignAttempts.MaxAttempts; i++)
        {
            Assert.Equal(AttemptVerdict.Go, a.Begin("start/IRONCLAD", now).Kind);
            now += CampaignAttempts.DelayAfter(i + 1);
        }
        now += CampaignAttempts.ForgetAfterSeconds;
        var v = a.Begin("start/IRONCLAD", now);
        Assert.Equal(AttemptVerdict.Go, v.Kind);
        Assert.Equal(1, v.Attempt);
    }

    [Fact]
    public void ResetForgetsTheHistory()
    {
        var a = new CampaignAttempts();
        a.Begin("k", 0);
        a.Reset();
        var v = a.Begin("k", 1);
        Assert.Equal(AttemptVerdict.Go, v.Kind);
        Assert.Equal(1, v.Attempt);
    }

    [Fact]
    public void NeverFasterThanTheDelay()
    {
        // Whatever the caller does, two attempts at one key are never closer than the delay after the first one.
        var a = new CampaignAttempts();
        double lastGo = double.NegativeInfinity;
        int lastAttempt = 0;
        for (double now = 0; now < 1000; now += 0.5)
        {
            var v = a.Begin("k", now);
            if (v.Kind == AttemptVerdict.Go)
            {
                if (lastAttempt > 0)
                    Assert.True(now - lastGo >= CampaignAttempts.DelayAfter(lastAttempt));
                lastGo = now;
                lastAttempt = v.Attempt;
            }
        }
    }
}
