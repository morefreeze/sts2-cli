// src/Sts2Advisor.Core.Tests/CampaignCycleTests.cs
using Sts2Advisor.Core;

/// <summary>The campaign's loop, played against a toy game: plan -> attempt gate -> "execute" -> the game moves on.</summary>
public class CampaignCycleTests
{
    private static readonly CampaignSettings Settings =
        new(true, "IRONCLAD", AscensionSpec.Max, CampaignCommand.None);

    [Fact]
    public void ARunIsPlayedRecordedLeftAndTheNextOneStarted()
    {
        var attempts = new CampaignAttempts();
        var screen = CampaignScreen.Run;
        bool autopilot = false;
        var executed = new List<CampaignStepKind>();
        double now = 0;
        for (int tick = 0; tick < 12; tick++, now += 1)
        {
            var facts = new CampaignFacts(screen, AutopilotRunning: autopilot, Floor: "A1F1");
            var step = CampaignPlan.Next(Settings, facts);
            if (step.Kind == CampaignStepKind.Idle)
                continue;
            var verdict = attempts.Begin($"{step.Kind}:{step.Key}", now);
            Assert.Equal(AttemptVerdict.Go, verdict.Kind);
            executed.Add(step.Kind);
            switch (step.Kind)
            {
                case CampaignStepKind.StartAutopilot: autopilot = true; break;
                case CampaignStepKind.FinishRun: screen = CampaignScreen.MainMenu; break;
                case CampaignStepKind.StartRun: screen = CampaignScreen.Run; autopilot = false; break;
            }
            if (step.Kind == CampaignStepKind.StartAutopilot)
            {
                // ... the autopilot plays on, the player dies:
                screen = CampaignScreen.GameOver;
                autopilot = false;
            }
        }
        Assert.Equal(new[]
        {
            CampaignStepKind.StartAutopilot, CampaignStepKind.FinishRun, CampaignStepKind.StartRun,
            CampaignStepKind.StartAutopilot, CampaignStepKind.FinishRun, CampaignStepKind.StartRun,
            CampaignStepKind.StartAutopilot, CampaignStepKind.FinishRun, CampaignStepKind.StartRun,
            CampaignStepKind.StartAutopilot, CampaignStepKind.FinishRun, CampaignStepKind.StartRun,
        }, executed);
    }

    [Fact]
    public void AStartThatNeverTakesEffectIsRetriedWithBackoffThenGivenUp()
    {
        var attempts = new CampaignAttempts();
        var executed = new List<double>();
        double stuckAt = -1;
        for (double now = 0; now < 600 && stuckAt < 0; now += 1)
        {
            var step = CampaignPlan.Next(Settings, new CampaignFacts(CampaignScreen.MainMenu));
            Assert.Equal(CampaignStepKind.StartRun, step.Kind);
            var verdict = attempts.Begin($"{step.Kind}:{step.Key}", now);
            if (verdict.Kind == AttemptVerdict.Go)
                executed.Add(now);
            else if (verdict.Kind == AttemptVerdict.Stuck)
                stuckAt = now;
        }
        Assert.Equal(CampaignAttempts.MaxAttempts, executed.Count);
        Assert.True(stuckAt > 60 && stuckAt < 200, $"stuck declared at {stuckAt} s");
        for (int i = 1; i < executed.Count; i++)
            Assert.True(executed[i] - executed[i - 1] >= CampaignAttempts.DelayAfter(i));
    }

    [Fact]
    public void ANewRunOfTheSameCharacterIsNotMistakenForARetry()
    {
        var attempts = new CampaignAttempts();
        double now = 0;
        for (int run = 0; run < 20; run++)
        {
            // each run: start it, play it (> the forget time), lose, leave the game over screen
            var start = attempts.Begin("StartRun:IRONCLAD/max", now);
            Assert.Equal(AttemptVerdict.Go, start.Kind);
            Assert.Equal(1, start.Attempt);
            now += 5;
            Assert.Equal(AttemptVerdict.Go, attempts.Begin("StartAutopilot:A1F1", now).Kind);
            now += 1200;
            Assert.Equal(AttemptVerdict.Go, attempts.Begin("FinishRun:finish", now).Kind);
            now += 60;
        }
    }
}
