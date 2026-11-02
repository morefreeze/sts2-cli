// src/Sts2Advisor.Core.Tests/CampaignRunLogTests.cs
using System.Text.Json;
using Sts2Advisor.Core;

public class CampaignRunLogTests
{
    private static CampaignRunRecord Sample(bool victory = false) => new(
        Character: "REGENT", Ascension: 6, Victory: victory, Act: 3, Floor: 14, TotalFloor: 48, Hp: 0, MaxHp: 75, Gold: 589,
        Deck: new[] { "CARD.STRIKE_REGENT", "CARD.GLOW+1" },
        Relics: new[] { "RELIC.DIVINE_RIGHT", "RELIC.BING_BONG" },
        Potions: new[] { "POTION.SWIFT_POTION" },
        RunTimeSeconds: 2346, Seed: "ABCD1234EFGH", KilledBy: victory ? null : "ENCOUNTER.TEST_SUBJECT_BOSS", KilledByEvent: null,
        KilledByMonsters: victory ? Array.Empty<string>() : new[] { "MONSTER.TEST_SUBJECT" },
        HistoryPath: "/x/history/1793158122.run", StartTime: 1793158122);

    private static string TempPath() => Path.Combine(Path.GetTempPath(), $"sts2runs-{Guid.NewGuid()}", "campaign_runs.jsonl");

    [Fact]
    public void TheLineCarriesEveryFieldTheCampaignPromises()
    {
        string json = CampaignRunLog.ToJson(Sample(), new DateTime(2026, 11, 2, 10, 30, 0, DateTimeKind.Utc));
        using var doc = JsonDocument.Parse(json);
        var r = doc.RootElement;
        Assert.Equal("2026-11-02T10:30:00.0000000Z", r.GetProperty("ts").GetString());
        Assert.Equal("REGENT", r.GetProperty("character").GetString());
        Assert.Equal(6, r.GetProperty("ascension").GetInt32());
        Assert.False(r.GetProperty("victory").GetBoolean());
        Assert.Equal(3, r.GetProperty("act").GetInt32());
        Assert.Equal(14, r.GetProperty("floor").GetInt32());
        Assert.Equal(48, r.GetProperty("total_floor").GetInt32());
        Assert.Equal(0, r.GetProperty("hp").GetInt32());
        Assert.Equal(75, r.GetProperty("max_hp").GetInt32());
        Assert.Equal(589, r.GetProperty("gold").GetInt32());
        Assert.Equal("CARD.GLOW+1", r.GetProperty("deck")[1].GetString());
        Assert.Equal("RELIC.BING_BONG", r.GetProperty("relics")[1].GetString());
        Assert.Equal("POTION.SWIFT_POTION", r.GetProperty("potions")[0].GetString());
        Assert.Equal(2346, r.GetProperty("run_time").GetInt64());
        Assert.Equal("ABCD1234EFGH", r.GetProperty("seed").GetString());
        Assert.Equal("ENCOUNTER.TEST_SUBJECT_BOSS", r.GetProperty("killed_by").GetString());
        Assert.Equal("MONSTER.TEST_SUBJECT", r.GetProperty("killed_by_monsters")[0].GetString());
        Assert.Equal("/x/history/1793158122.run", r.GetProperty("history_path").GetString());
        Assert.Equal(1793158122, r.GetProperty("start_time").GetInt64());
    }

    [Fact]
    public void AVictoryHasNoKiller()
    {
        using var doc = JsonDocument.Parse(CampaignRunLog.ToJson(Sample(victory: true), DateTime.UtcNow));
        Assert.True(doc.RootElement.GetProperty("victory").GetBoolean());
        Assert.Equal(JsonValueKind.Null, doc.RootElement.GetProperty("killed_by").ValueKind);
        Assert.Equal(0, doc.RootElement.GetProperty("killed_by_monsters").GetArrayLength());
    }

    [Fact]
    public void MissingPiecesAreExplicitNullsSoTheSchemaIsStable()
    {
        var bare = Sample() with { RunTimeSeconds = null, Seed = null, HistoryPath = null, StartTime = null, KilledBy = null };
        using var doc = JsonDocument.Parse(CampaignRunLog.ToJson(bare, DateTime.UtcNow));
        foreach (var key in new[] { "run_time", "seed", "history_path", "start_time", "killed_by", "killed_by_event" })
            Assert.Equal(JsonValueKind.Null, doc.RootElement.GetProperty(key).ValueKind);
    }

    [Fact]
    public void TheLineIsOneLine()
    {
        string json = CampaignRunLog.ToJson(Sample() with { Seed = "a\nb" }, DateTime.UtcNow);
        Assert.DoesNotContain('\n', json);
    }

    [Theory]
    [InlineData("CARD.STRIKE_REGENT", 0, "CARD.STRIKE_REGENT")]
    [InlineData("CARD.GLOW", 1, "CARD.GLOW+1")]
    [InlineData("CARD.SEARING_BLOW", 3, "CARD.SEARING_BLOW+3")]
    public void CardsCarryTheirUpgradeLevel(string id, int upgrade, string expected)
    {
        Assert.Equal(expected, CampaignRunLog.CardId(id, upgrade));
    }

    [Fact]
    public void AppendAddsOneLinePerRunAndCreatesTheDirectory()
    {
        string path = TempPath();
        Assert.True(CampaignRunLog.Append(path, CampaignRunLog.ToJson(Sample(), DateTime.UtcNow)));
        Assert.True(CampaignRunLog.Append(path, CampaignRunLog.ToJson(Sample(victory: true), DateTime.UtcNow)));
        var lines = File.ReadAllLines(path);
        Assert.Equal(2, lines.Length);
        Assert.False(JsonDocument.Parse(lines[0]).RootElement.GetProperty("victory").GetBoolean());
        Assert.True(JsonDocument.Parse(lines[1]).RootElement.GetProperty("victory").GetBoolean());
    }

    [Fact]
    public void AppendKeepsEarlierLinesAndNeverMergesTwoRunsOntoOneLine()
    {
        string path = TempPath();
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, "{\"old\":1}");  // a file whose last line lost its newline
        Assert.True(CampaignRunLog.Append(path, "{\"new\":2}"));
        Assert.Equal(new[] { "{\"old\":1}", "{\"new\":2}" }, File.ReadAllLines(path));
    }

    [Fact]
    public void AppendNeverThrows()
    {
        Assert.False(CampaignRunLog.Append(Path.GetTempPath(), "{}"));  // a directory, not a file
        Assert.False(CampaignRunLog.Append(Path.Combine(Path.GetTempPath(), "x\0y", "f.jsonl"), "{}"));
    }

    [Fact]
    public void AppendRefusesALineThatIsNotOneLineOfJson()
    {
        string path = TempPath();
        Assert.False(CampaignRunLog.Append(path, "not json"));
        Assert.False(CampaignRunLog.Append(path, "{\"a\":1}\n{\"b\":2}"));
        Assert.False(File.Exists(path));
    }
}
