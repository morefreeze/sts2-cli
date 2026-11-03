// src/Sts2Advisor.Core.Tests/CampaignLadderTests.cs
using Sts2Advisor.Core;

public class CampaignLadderTests
{
    private static readonly string[] Order = { "NECROBINDER", "REGENT", "IRONCLAD" };

    private static LadderState State(int necro, int regent, int ironclad, LastRun? last = null, params string[] done) =>
        new(new Dictionary<string, int> { ["NECROBINDER"] = necro, ["REGENT"] = regent, ["IRONCLAD"] = ironclad },
            done.ToHashSet(), last);

    private static LadderPick Pick(LadderState s) => CampaignLadder.Pick(Order, s);

    [Fact]
    public void ALossAtTheLowestLevelIsRepeated()
    {
        var p = Pick(State(9, 9, 10, new LastRun("NECROBINDER", false)));
        Assert.Equal("NECROBINDER", p.Character);
        Assert.Equal(9, p.Level);
        Assert.True(p.Sticky);
    }

    [Fact]
    public void ALossByTheSecondCharacterAtTheLowestLevelIsRepeatedToo()
    {
        var p = Pick(State(9, 9, 10, new LastRun("REGENT", false)));
        Assert.Equal("REGENT", p.Character);
        Assert.True(p.Sticky);
    }

    [Fact]
    public void AWinMovesOnToTheNextCharacterAtTheLowestLevel()
    {
        // necro won at 9 and is now 10: regent (9) is the lowest
        var p = Pick(State(10, 9, 10, new LastRun("NECROBINDER", true)));
        Assert.Equal("REGENT", p.Character);
        Assert.False(p.Sticky);
        Assert.Equal(9, p.Level);
    }

    [Fact]
    public void AfterAWinTheStickyRuleDoesNotApplyEvenWhenAllAreAtTheSameLevel()
    {
        // regent won at 9 -> everybody is at 10, none done: pick by order, not "regent again"
        var p = Pick(State(10, 10, 10, new LastRun("REGENT", true)));
        Assert.Equal("NECROBINDER", p.Character);
        Assert.False(p.Sticky);
        Assert.Equal(10, p.Level);
    }

    [Fact]
    public void ALossAtA10IsStickyUntilTheCharacterWins()
    {
        var p = Pick(State(10, 10, 10, new LastRun("REGENT", false)));
        Assert.Equal("REGENT", p.Character);
        Assert.True(p.Sticky);
        Assert.Equal(10, p.Level);
    }

    [Fact]
    public void AnA10WinMakesTheCharacterDoneAndTheNextOneIsPicked()
    {
        var p = Pick(State(10, 10, 10, new LastRun("REGENT", true), "REGENT"));
        Assert.Equal("NECROBINDER", p.Character);
        Assert.False(p.Sticky);
        var q = Pick(State(10, 10, 10, new LastRun("NECROBINDER", true), "NECROBINDER", "REGENT"));
        Assert.Equal("IRONCLAD", q.Character);
    }

    [Fact]
    public void ALossByADoneCharacterIsNotRepeated()
    {
        var p = Pick(State(10, 10, 10, new LastRun("NECROBINDER", false), "NECROBINDER"));
        Assert.Equal("REGENT", p.Character);
        Assert.False(p.Sticky);
    }

    [Fact]
    public void ALossAboveTheLowestLevelIsNotRepeated()
    {
        // ironclad lost at 10, but regent is still at 9: the lowest level goes first
        var p = Pick(State(10, 9, 10, new LastRun("IRONCLAD", false)));
        Assert.Equal("REGENT", p.Character);
        Assert.False(p.Sticky);
    }

    [Fact]
    public void TheLowestLevelBeatsTheOrder()
    {
        Assert.Equal("IRONCLAD", Pick(State(9, 9, 5)).Character);
        Assert.Equal("NECROBINDER", Pick(State(5, 5, 5)).Character);
    }

    [Fact]
    public void TheLevelOfTheLastCharacterIsItsCurrentLevelNotTheOneItLostAt()
    {
        // lost necro at 8 -> still 8: sticky; the same loss with necro now above the others is not
        Assert.True(Pick(State(8, 9, 9, new LastRun("NECROBINDER", false))).Sticky);
        Assert.Equal("REGENT", Pick(State(9, 8, 9, new LastRun("NECROBINDER", false))).Character);
    }

    [Fact]
    public void WhenEveryoneIsDoneTheLadderIsComplete()
    {
        var p = Pick(State(10, 10, 10, new LastRun("IRONCLAD", true), "NECROBINDER", "REGENT", "IRONCLAD"));
        Assert.True(p.Complete);
        Assert.Null(p.Character);
    }

    [Fact]
    public void ALevelAboveTheCapReadsAsTen()
    {
        Assert.Equal(10, CampaignLadder.LevelOf(State(12, 1, 1).Levels, "NECROBINDER"));
        Assert.Equal(0, CampaignLadder.LevelOf(State(1, 1, 1).Levels, "SILENT"));
    }

    [Fact]
    public void TheLastCharacterMatchesIgnoringCase()
    {
        Assert.True(Pick(State(9, 9, 10, new LastRun("regent", false))).Sticky);
    }

    [Fact]
    public void ThePickIsDescribedForTheLog()
    {
        var s = State(9, 9, 10, new LastRun("NECROBINDER", false));
        Assert.Equal("ladder pick NECROBINDER a9 (levels N9 R9 I10; sticky after loss)", CampaignLadder.Describe(Pick(s), Order, s));
        var t = State(10, 9, 10, new LastRun("NECROBINDER", true));
        Assert.Equal("ladder pick REGENT a9 (levels N10 R9 I10; lowest level, first in order)", CampaignLadder.Describe(Pick(t), Order, t));
        var d = State(10, 10, 10, null, "NECROBINDER", "REGENT", "IRONCLAD");
        Assert.Equal("ladder complete", CampaignLadder.Describe(Pick(d), Order, d));
        Assert.Equal("N10 done R10 done I10 done", CampaignLadder.Summary(Order, d));
    }

    // -- plan ---------------------------------------------------------------------------------------------

    private static CampaignSettings Ladder() =>
        new(true, "IRONCLAD", AscensionSpec.Of(3), CampaignCommand.None, new[] { new CampaignTarget("SILENT", AscensionSpec.Max) }, Order);

    [Fact]
    public void ThePlanStartsTheLaddersPickAtItsLevelAndIgnoresNextAndRotation()
    {
        var step = CampaignPlan.Next(Ladder(), new CampaignFacts(CampaignScreen.MainMenu, LastRunCharacter: "SILENT",
                                                                  Ladder: State(9, 9, 10, new LastRun("NECROBINDER", false))));
        Assert.Equal(CampaignStepKind.StartRun, step.Kind);
        Assert.Equal(new CampaignTarget("NECROBINDER", AscensionSpec.Max), step.Target);
        Assert.StartsWith("ladder pick NECROBINDER a9", step.Why);
    }

    [Fact]
    public void ACompleteLadderStartsNothing()
    {
        var step = CampaignPlan.Next(Ladder(), new CampaignFacts(CampaignScreen.MainMenu,
            Ladder: State(10, 10, 10, null, "NECROBINDER", "REGENT", "IRONCLAD")));
        Assert.Equal(CampaignStepKind.Idle, step.Kind);
        Assert.Equal("ladder complete", step.Why);
    }

    [Fact]
    public void WithoutGameStateTheLadderWaits()
    {
        Assert.Equal(CampaignStepKind.Idle, CampaignPlan.Next(Ladder(), new CampaignFacts(CampaignScreen.MainMenu)).Kind);
    }

    [Fact]
    public void ASavedRunIsStillContinuedUnderTheLadder()
    {
        var step = CampaignPlan.Next(Ladder(), new CampaignFacts(CampaignScreen.MainMenu, HasSavedRun: true));
        Assert.Equal(CampaignStepKind.ContinueRun, step.Kind);
    }

    [Fact]
    public void NextTargetFollowsTheLadder()
    {
        var t = CampaignPlan.NextTarget(Ladder(), "SILENT", State(10, 9, 10, new LastRun("NECROBINDER", true)));
        Assert.Equal("REGENT", t!.Value.Character);
        Assert.Null(CampaignPlan.NextTarget(Ladder(), "SILENT", null));
    }

    // -- panel --------------------------------------------------------------------------------------------

    [Fact]
    public void ThePanelLineShowsTheLadder()
    {
        var line = CampaignText.Status(new CampaignView(true, "Regent", AscensionSpec.Of(9), 2, 0, null, null, null, "N10 R9 I10 done"), false)!;
        Assert.Contains("ladder N10 R9 I10 done", line.Text);
        Assert.Contains("next Regent a9", line.Text);
        var zh = CampaignText.Status(new CampaignView(true, null, null, 0, 0, null, null, null, "complete"), true)!;
        Assert.Contains("天梯已完成", zh.Text);
    }

    // -- history ------------------------------------------------------------------------------------------

    private static string Run(string character, bool win, int ascension, int players = 1) =>
        "{\"win\":" + (win ? "true" : "false") + ",\"ascension\":" + ascension + ",\"players\":[" +
        string.Join(",", Enumerable.Repeat("{\"character\":\"CHARACTER." + character + "\"}", players)) + "]}";

    [Fact]
    public void ARunHistoryYieldsCharacterWinAndAscension()
    {
        Assert.Equal(new CampaignHistory.HistoryRun("IRONCLAD", true, 10), CampaignHistory.ParseRun(Run("IRONCLAD", true, 10)));
        Assert.Null(CampaignHistory.ParseRun(Run("IRONCLAD", true, 10, players: 2)));
        Assert.Null(CampaignHistory.ParseRun("{\"win\":true}"));
        Assert.Null(CampaignHistory.ParseRun("not json"));
    }

    [Fact]
    public void OnlyWinsAtAscensionTenOrMoreClearACharacter()
    {
        var cleared = CampaignHistory.Cleared(new[]
        {
            new CampaignHistory.HistoryRun("IRONCLAD", true, 10),
            new CampaignHistory.HistoryRun("REGENT", true, 9),
            new CampaignHistory.HistoryRun("NECROBINDER", false, 10),
            new CampaignHistory.HistoryRun("SILENT", true, 11),
        });
        Assert.Equal(new[] { "IRONCLAD", "SILENT" }, cleared.OrderBy(x => x));
    }

    [Fact]
    public void TheScanReadsAFolderAndPicksUpNewFiles()
    {
        string dir = Path.Combine(Path.GetTempPath(), "sts2hist-" + Guid.NewGuid());
        Directory.CreateDirectory(dir);
        try
        {
            var scan = new CampaignHistoryScan();
            Assert.Empty(scan.Cleared(dir));
            File.WriteAllText(Path.Combine(dir, "1.run"), Run("REGENT", true, 9));
            File.WriteAllText(Path.Combine(dir, "2.run"), Run("IRONCLAD", true, 10));
            File.WriteAllText(Path.Combine(dir, "3.run"), "{ half written");
            Assert.Equal(new[] { "IRONCLAD" }, scan.Cleared(dir));
            File.WriteAllText(Path.Combine(dir, "4.run"), Run("NECROBINDER", true, 10));
            Assert.Equal(new[] { "IRONCLAD", "NECROBINDER" }, scan.Cleared(dir).OrderBy(x => x));
            File.Delete(Path.Combine(dir, "2.run"));
            Assert.Equal(new[] { "NECROBINDER" }, scan.Cleared(dir));
            Assert.Empty(scan.Cleared(Path.Combine(dir, "missing")));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void TheRunLogGivesTheLastRunAndItsResult()
    {
        string path = Path.Combine(Path.GetTempPath(), "sts2runs-" + Guid.NewGuid() + ".jsonl");
        try
        {
            Assert.Null(CampaignRunLog.LastRunOf(path));
            File.WriteAllText(path, "{\"character\":\"REGENT\",\"victory\":false}\n{\"character\":\"NECROBINDER\",\"victory\":true}\nnoise\n");
            Assert.Equal(new LastRun("NECROBINDER", true), CampaignRunLog.LastRunOf(path));
            Assert.Equal("NECROBINDER", CampaignRunLog.LastCharacter(path));
        }
        finally
        {
            File.Delete(path);
        }
    }

    // -- file ---------------------------------------------------------------------------------------------

    [Fact]
    public void TheFileParsesTheLadder()
    {
        var r = CampaignFile.Parse("""{"enabled": true, "strategy": "ladder", "characters": ["necrobinder", "REGENT", "CHARACTER.IRONCLAD"]}""");
        Assert.Null(r.Problem);
        Assert.Equal(Order, r.Settings!.Ladder);
        Assert.Null(r.Settings.Character);
    }

    [Theory]
    [InlineData("""{"enabled": true, "strategy": "ladder", "characters": ["WATCHER"]}""", "unknown character")]
    [InlineData("""{"enabled": true, "strategy": "ladder"}""", "needs \"characters\"")]
    [InlineData("""{"enabled": true, "strategy": "ladder", "characters": []}""", "needs \"characters\"")]
    [InlineData("""{"enabled": true, "strategy": "ladder", "characters": ["REGENT", "REGENT"]}""", "listed twice")]
    [InlineData("""{"enabled": true, "strategy": "ladder", "characters": [3]}""", "unknown character 3")]
    [InlineData("""{"enabled": true, "strategy": "zigzag", "characters": ["REGENT"]}""", "unknown \"strategy\"")]
    [InlineData("""{"enabled": true, "strategy": 5}""", "unknown \"strategy\"")]
    public void ABadLadderMakesTheFileInvalid(string json, string problem)
    {
        var r = CampaignFile.Parse(json);
        Assert.Null(r.Settings);
        Assert.Contains(problem, r.Problem);
    }

    [Fact]
    public void NoStrategyOrRotationStrategyKeepsTheOldBehaviour()
    {
        Assert.Null(CampaignFile.Parse("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": "max"}}""").Settings!.Ladder);
        Assert.Null(CampaignFile.Parse("""{"enabled": true, "strategy": null, "next": {"character": "IRONCLAD", "ascension": "max"}}""").Settings!.Ladder);
        Assert.Null(CampaignFile.Parse("""{"enabled": true, "strategy": "rotation", "rotation": ["SILENT"]}""").Settings!.Ladder);
    }

    [Fact]
    public void ADisabledLadderFileIsNotValidatedFurther()
    {
        var r = CampaignFile.Parse("""{"enabled": false, "strategy": "ladder", "characters": ["WATCHER"]}""");
        Assert.Null(r.Problem);
        Assert.False(r.Settings!.Enabled);
    }
}
