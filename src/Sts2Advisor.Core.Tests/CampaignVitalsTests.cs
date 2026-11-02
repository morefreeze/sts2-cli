// src/Sts2Advisor.Core.Tests/CampaignVitalsTests.cs
using Sts2Advisor.Core;

public class CampaignVitalsTests
{
    // The live player of a victory is already dead (the Architect's WinRun kills them after OnEnded), and the game's
    // second OnEnded (CreatureCmd.Kill -> all players dead) rewrites the in-memory last map point entry with hp 0.
    private static readonly RunVitals Dead = new(0, 75, 38);
    private static readonly RunVitals Saved = new(30, 75, 38);   // what the first OnEnded wrote into <start>.run
    private static readonly RunVitals Zeroed = new(0, 75, 38);   // the in-memory entry after the second OnEnded

    [Fact]
    public void AVictoryTakesTheSavedHistoryFilesLastEntryNotTheDeadPlayersZero()
    {
        Assert.Equal(Saved, CampaignVitals.Pick(victory: true, Dead, Saved, Zeroed));
    }

    [Fact]
    public void AVictoryFallsBackToTheInMemoryEntryWhenTheFileCannotBeRead()
    {
        var entry = new RunVitals(41, 80, 120);
        Assert.Equal(entry, CampaignVitals.Pick(true, Dead, null, entry));
    }

    [Fact]
    public void AVictoryWhereEverySourceReadsZeroKeepsTheLiveValues()
    {
        Assert.Equal(Dead, CampaignVitals.Pick(true, Dead, Zeroed, Zeroed));
        Assert.Equal(Dead, CampaignVitals.Pick(true, Dead, null, null));
    }

    [Fact]
    public void TheFirstSourceWithHpWins()
    {
        var a = new RunVitals(12, 70, 5);
        var b = new RunVitals(30, 75, 38);
        Assert.Equal(a, CampaignVitals.Pick(true, Dead, a, b));
        Assert.Equal(b, CampaignVitals.Pick(true, Dead, Zeroed, b));
    }

    [Fact]
    public void ADefeatIsTheLivePlayerWhateverTheEntriesSay()
    {
        var died = new RunVitals(0, 70, 99);
        Assert.Equal(died, CampaignVitals.Pick(victory: false, died, new RunVitals(30, 75, 38), new RunVitals(30, 75, 38)));
    }

    // -- the game's history file ----------------------------------------------------------------------------------

    // Trimmed from a real <start_time>.run of a Defect a4 victory: three acts, the last map point is the boss.
    private const string HistoryFile = """
        {"acts":["ACT.OVERGROWTH"],"ascension":4,"win":true,"players":[{"id":1,"character":"CHARACTER.DEFECT"}],
         "map_point_history":[
           [{"map_point_type":"monster","player_stats":[{"current_gold":99,"current_hp":70,"damage_taken":5,"max_hp":75,"player_id":1}],"rooms":[]}],
           [{"map_point_type":"rest_site","player_stats":[{"current_gold":38,"current_hp":75,"damage_taken":0,"max_hp":75,"player_id":1}],"rooms":[]},
            {"map_point_type":"boss","player_stats":[{"current_gold":38,"current_hp":30,"damage_taken":45,"max_hp":75,"player_id":1}],
             "rooms":[{"model_id":"ENCOUNTER.TEST_SUBJECT_BOSS","room_type":"boss","turns_taken":10}]}]],
         "run_time":1392,"seed":"X","start_time":1793592522}
        """;

    [Fact]
    public void TheHistoryFilesLastMapPointEntryHasTheVictorysHp()
    {
        Assert.Equal(Saved, CampaignVitals.FromHistoryJson(HistoryFile, 1));
    }

    [Fact]
    public void WithoutAPlayerIdTheFirstPlayerCounts()
    {
        Assert.Equal(Saved, CampaignVitals.FromHistoryJson(HistoryFile, null));
    }

    [Fact]
    public void ThePlayerIsFoundByIdAmongSeveral()
    {
        const string two = """
            {"map_point_history":[[{"player_stats":[
              {"current_gold":1,"current_hp":2,"max_hp":3,"player_id":1},
              {"current_gold":4,"current_hp":5,"max_hp":6,"player_id":2}]}]]}
            """;
        Assert.Equal(new RunVitals(5, 6, 4), CampaignVitals.FromHistoryJson(two, 2));
        Assert.Equal(new RunVitals(2, 3, 1), CampaignVitals.FromHistoryJson(two, 1));
        Assert.Equal(new RunVitals(2, 3, 1), CampaignVitals.FromHistoryJson(two, 9));  // unknown id: the first player
    }

    [Fact]
    public void AnEmptyLastActFallsBackToTheLastActThatHasEntries()
    {
        const string json = """
            {"map_point_history":[[{"player_stats":[{"current_gold":7,"current_hp":8,"max_hp":9,"player_id":1}]}],[]]}
            """;
        Assert.Equal(new RunVitals(8, 9, 7), CampaignVitals.FromHistoryJson(json, 1));
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("[1]")]
    [InlineData("{}")]
    [InlineData("""{"map_point_history": []}""")]
    [InlineData("""{"map_point_history": [[]]}""")]
    [InlineData("""{"map_point_history": [[{"player_stats": []}]]}""")]
    [InlineData("""{"map_point_history": [[{"player_stats": [{"current_hp": "x"}]}]]}""")]
    [InlineData("""{"map_point_history": [[{"player_stats": [{"current_gold": 1, "max_hp": 2}]}]]}""")]
    public void AFileThatHasNoUsableLastEntryGivesNull(string json)
    {
        Assert.Null(CampaignVitals.FromHistoryJson(json, 1));
    }

    [Fact]
    public void AFileOnDiskIsReadAndAMissingOneIsQuietlyNull()
    {
        var path = Path.Combine(Path.GetTempPath(), $"sts2hist-{Guid.NewGuid()}.run");
        Assert.Null(CampaignVitals.FromHistoryFile(path, 1));
        File.WriteAllText(path, HistoryFile);
        Assert.Equal(Saved, CampaignVitals.FromHistoryFile(path, 1));
        File.Delete(path);
    }
}
