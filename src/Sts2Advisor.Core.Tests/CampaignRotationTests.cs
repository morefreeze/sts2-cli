// src/Sts2Advisor.Core.Tests/CampaignRotationTests.cs
using Sts2Advisor.Core;

public class CampaignRotationTests
{
    private static readonly CampaignTarget[] Five =
    {
        new("IRONCLAD", AscensionSpec.Max), new("SILENT", AscensionSpec.Of(3)), new("DEFECT", AscensionSpec.Max),
        new("REGENT", AscensionSpec.Max), new("NECROBINDER", AscensionSpec.Max),
    };

    [Fact]
    public void TheEntryAfterTheCharacterThatJustEndedIsNext()
    {
        Assert.Equal(Five[1], CampaignRotation.Next(Five, "IRONCLAD"));
        Assert.Equal(Five[3], CampaignRotation.Next(Five, "DEFECT"));
    }

    [Fact]
    public void ItCyclesBackToTheFirstEntry()
    {
        Assert.Equal(Five[0], CampaignRotation.Next(Five, "NECROBINDER"));
    }

    [Fact]
    public void ACharacterThatIsNotInTheListStartsAtTheFirstEntry()
    {
        Assert.Equal(Five[0], CampaignRotation.Next(Five, "WATCHER"));
    }

    [Fact]
    public void NoRunEndedYetStartsAtTheFirstEntry()
    {
        Assert.Equal(Five[0], CampaignRotation.Next(Five, null));
        Assert.Equal(Five[0], CampaignRotation.Next(Five, ""));
    }

    [Fact]
    public void TheMatchIgnoresCase()
    {
        Assert.Equal(Five[2], CampaignRotation.Next(Five, "silent"));
    }

    [Fact]
    public void AOneEntryRotationRepeatsThatEntry()
    {
        var one = new[] { new CampaignTarget("REGENT", AscensionSpec.Max) };
        Assert.Equal(one[0], CampaignRotation.Next(one, "REGENT"));
        Assert.Equal(one[0], CampaignRotation.Next(one, "SILENT"));
    }

    [Fact]
    public void ARepeatedCharacterMatchesItsFirstEntry()
    {
        var twice = new[]
        {
            new CampaignTarget("IRONCLAD", AscensionSpec.Of(10)), new CampaignTarget("IRONCLAD", AscensionSpec.Of(5)),
            new CampaignTarget("SILENT", AscensionSpec.Max),
        };
        Assert.Equal(twice[1], CampaignRotation.Next(twice, "IRONCLAD"));
    }

    [Fact]
    public void AnEmptyOrMissingRotationHasNoNextEntry()
    {
        Assert.Null(CampaignRotation.Next(Array.Empty<CampaignTarget>(), "IRONCLAD"));
        Assert.Null(CampaignRotation.Next(null, "IRONCLAD"));
    }

    // -- how the plan uses it -----------------------------------------------------------------------------------

    private static CampaignSettings Settings(string? nextCharacter, params CampaignTarget[] rotation) =>
        new(true, nextCharacter, nextCharacter == null ? null : AscensionSpec.Of(1), CampaignCommand.None,
            rotation.Length == 0 ? null : rotation);

    [Fact]
    public void TheMenuStartsTheRotationsNextEntryAndNotNext()
    {
        var s = Settings("IRONCLAD", Five);
        var step = CampaignPlan.Next(s, new CampaignFacts(CampaignScreen.MainMenu, LastRunCharacter: "SILENT"));
        Assert.Equal(CampaignStepKind.StartRun, step.Kind);
        Assert.Equal(Five[2], step.Target);
        Assert.Equal("DEFECT/max", step.Key);
    }

    [Fact]
    public void WithoutARotationNextIsStartedWhateverRanLast()
    {
        var s = Settings("IRONCLAD");
        var step = CampaignPlan.Next(s, new CampaignFacts(CampaignScreen.MainMenu, LastRunCharacter: "SILENT"));
        Assert.Equal(CampaignStepKind.StartRun, step.Kind);
        Assert.Equal(new CampaignTarget("IRONCLAD", AscensionSpec.Of(1)), step.Target);
        Assert.Equal("IRONCLAD/1", step.Key);
    }

    [Fact]
    public void WithNeitherThereIsNothingToStart()
    {
        var step = CampaignPlan.Next(Settings(null), new CampaignFacts(CampaignScreen.MainMenu));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Contains("rotation", step.Why);
    }

    [Fact]
    public void ASavedRunIsContinuedBeforeTheRotationIsConsulted()
    {
        var step = CampaignPlan.Next(Settings(null, Five), new CampaignFacts(CampaignScreen.MainMenu, HasSavedRun: true, LastRunCharacter: "IRONCLAD"));
        Assert.Equal(CampaignStepKind.ContinueRun, step.Kind);
    }

    [Fact]
    public void TheTargetIsReadFromTheLatestSettingsEachTime()
    {
        // An edit of "rotation" between two plans changes the answer; nothing is remembered from the first.
        var facts = new CampaignFacts(CampaignScreen.MainMenu, LastRunCharacter: "IRONCLAD");
        var before = CampaignPlan.Next(Settings(null, Five), facts);
        var edited = CampaignPlan.Next(Settings(null, new CampaignTarget("IRONCLAD", AscensionSpec.Max), new CampaignTarget("REGENT", AscensionSpec.Max)), facts);
        Assert.Equal("SILENT", before.Target!.Value.Character);
        Assert.Equal("REGENT", edited.Target!.Value.Character);
    }

    [Fact]
    public void NextTargetIsWhatThePanelShows()
    {
        Assert.Equal(Five[1], CampaignPlan.NextTarget(Settings("DEFECT", Five), "IRONCLAD"));
        Assert.Equal(new CampaignTarget("DEFECT", AscensionSpec.Of(1)), CampaignPlan.NextTarget(Settings("DEFECT"), "IRONCLAD"));
        Assert.Null(CampaignPlan.NextTarget(Settings(null), "IRONCLAD"));
        Assert.Null(CampaignPlan.NextTarget(null, "IRONCLAD"));
    }
}
