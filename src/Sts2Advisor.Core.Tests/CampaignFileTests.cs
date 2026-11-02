// src/Sts2Advisor.Core.Tests/CampaignFileTests.cs
using System.Text.Json;
using System.Text.Json.Nodes;
using Sts2Advisor.Core;

public class CampaignFileTests
{
    private static CampaignRead Parse(string json) => CampaignFile.Parse(json);

    private static string TempPath() => Path.Combine(Path.GetTempPath(), $"sts2campaign-{Guid.NewGuid()}.json");

    [Fact]
    public void ReadsAnEnabledFileWithAMaxAscension()
    {
        var r = Parse("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": "max"}, "command": null}""");
        Assert.Null(r.Problem);
        var s = Assert.IsType<CampaignSettings>(r.Settings);
        Assert.True(s.Enabled);
        Assert.Equal("IRONCLAD", s.Character);
        Assert.Equal(AscensionSpec.Max, s.Ascension);
        Assert.Equal(CampaignCommand.None, s.Command);
    }

    [Fact]
    public void ReadsANumericAscension()
    {
        var s = Parse("""{"enabled": true, "next": {"character": "SILENT", "ascension": 7}}""").Settings!;
        Assert.Equal(AscensionSpec.Of(7), s.Ascension);
        Assert.False(s.Ascension!.Value.IsMax);
        Assert.Equal(7, s.Ascension.Value.Level);
    }

    [Theory]
    [InlineData("Max")]
    [InlineData("MAX")]
    [InlineData(" max ")]
    public void MaxIsCaseInsensitive(string max)
    {
        var s = Parse("""{"enabled": true, "next": {"character": "DEFECT", "ascension": "@@"}}""".Replace("@@", max)).Settings!;
        Assert.True(s.Ascension!.Value.IsMax);
    }

    [Fact]
    public void ANumberInAStringIsAcceptedToo()
    {
        var s = Parse("""{"enabled": true, "next": {"character": "DEFECT", "ascension": "4"}}""").Settings!;
        Assert.Equal(AscensionSpec.Of(4), s.Ascension);
    }

    [Theory]
    [InlineData("ironclad", "IRONCLAD")]
    [InlineData("  Regent ", "REGENT")]
    [InlineData("CHARACTER.NECROBINDER", "NECROBINDER")]
    [InlineData("character.silent", "SILENT")]
    public void CharacterNamesAreNormalisedToTheGameIdsWithoutThePrefix(string given, string expected)
    {
        var s = Parse("""{"enabled": true, "next": {"character": "@@", "ascension": 0}}""".Replace("@@", given)).Settings!;
        Assert.Equal(expected, s.Character);
    }

    [Fact]
    public void ADisabledFileIsOffWhateverElseItSays()
    {
        var r = Parse("""{"enabled": false, "next": {"character": "IRONCLAD", "ascension": "max"}, "command": "quit"}""");
        Assert.Null(r.Problem);
        Assert.False(r.Settings!.Enabled);
        Assert.Equal(CampaignCommand.None, r.Settings.Command);  // a disabled campaign takes no orders
    }

    [Fact]
    public void AMissingEnabledKeyMeansOff()
    {
        var r = Parse("""{"next": {"character": "IRONCLAD", "ascension": "max"}}""");
        Assert.Null(r.Problem);
        Assert.False(r.Settings!.Enabled);
    }

    [Theory]
    [InlineData("quit", CampaignCommand.Quit)]
    [InlineData("QUIT", CampaignCommand.Quit)]
    [InlineData("stop", CampaignCommand.Stop)]
    [InlineData(" Stop ", CampaignCommand.Stop)]
    [InlineData("", CampaignCommand.None)]
    public void CommandsAreCaseInsensitive(string command, CampaignCommand expected)
    {
        var s = Parse("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": 1}, "command": "@@"}""".Replace("@@", command)).Settings!;
        Assert.Equal(expected, s.Command);
    }

    [Fact]
    public void AnUnknownCommandMakesTheFileInvalid()
    {
        // A typo for "quit" must not leave the campaign running as if nothing was asked.
        var r = Parse("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": 1}, "command": "qiut"}""");
        Assert.Null(r.Settings);
        Assert.Contains("command", r.Problem);
    }

    [Fact]
    public void NextMayBeMissingForACommand()
    {
        var s = Parse("""{"enabled": true, "command": "quit"}""").Settings!;
        Assert.Equal(CampaignCommand.Quit, s.Command);
        Assert.Null(s.Character);
        Assert.Null(s.Ascension);
    }

    [Theory]
    [InlineData("""{"enabled": true, "next": {"character": "IRON CLAD", "ascension": 1}}""")]
    [InlineData("""{"enabled": true, "next": {"character": 5, "ascension": 1}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": -1}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": 2.5}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": "high"}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": true}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD"}}""")]
    [InlineData("""{"enabled": true, "next": {"ascension": 3}}""")]
    [InlineData("""{"enabled": true, "next": 7}""")]
    [InlineData("""{"enabled": "yes", "next": {"character": "IRONCLAD", "ascension": 1}}""")]
    [InlineData("""{"enabled": true, "next": {"character": "IRONCLAD", "ascension": 1}, "command": 3}""")]
    public void AWrongTypeOrAHalfWrittenNextMakesTheFileInvalid(string json)
    {
        var r = Parse(json);
        Assert.Null(r.Settings);
        Assert.False(string.IsNullOrEmpty(r.Problem));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData(null)]
    public void NothingInTheFileIsNoCampaignAndNoProblem(string? text)
    {
        var r = CampaignFile.Parse(text);
        Assert.Null(r.Settings);
        Assert.Null(r.Problem);
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("[1, 2]")]
    [InlineData("7")]
    [InlineData("{\"enabled\": true,")]
    public void GarbageIsInvalidNotAnException(string text)
    {
        var r = Parse(text);
        Assert.Null(r.Settings);
        Assert.False(string.IsNullOrEmpty(r.Problem));
    }

    [Fact]
    public void LoadOfAMissingFileIsQuietlyOff()
    {
        var r = CampaignFile.Load(TempPath());
        Assert.Null(r.Settings);
        Assert.Null(r.Problem);
        Assert.Equal("", r.Raw);
    }

    [Fact]
    public void LoadKeepsTheRawTextToTellWhetherTheFileChanged()
    {
        var path = TempPath();
        const string text = """{"enabled": true, "next": {"character": "IRONCLAD", "ascension": 2}}""";
        File.WriteAllText(path, text);
        var r = CampaignFile.Load(path);
        Assert.Equal(text, r.Raw);
        Assert.Equal("IRONCLAD", r.Settings!.Character);
    }

    [Fact]
    public void LoadOfADirectoryOrAnUnreadablePathNeverThrows()
    {
        var r = CampaignFile.Load(Path.GetTempPath());
        Assert.Null(r.Settings);
    }

    [Fact]
    public void ClearCommandRewritesOnlyTheCommand()
    {
        var path = TempPath();
        File.WriteAllText(path, """{"enabled": true, "next": {"character": "REGENT", "ascension": "max"}, "command": "quit", "note": "keep me"}""");
        Assert.True(CampaignFile.ClearCommand(path));
        var node = JsonNode.Parse(File.ReadAllText(path))!.AsObject();
        Assert.Null(node["command"]);
        Assert.True(node.ContainsKey("command"));  // an explicit null, as in the documented shape
        Assert.Equal("keep me", (string?)node["note"]);
        Assert.Equal("REGENT", (string?)node["next"]!["character"]);
        var r = CampaignFile.Load(path);
        Assert.Equal(CampaignCommand.None, r.Settings!.Command);
        Assert.True(r.Settings.Enabled);
    }

    [Fact]
    public void ClearCommandLeavesNoTempFileBehind()
    {
        var path = TempPath();
        File.WriteAllText(path, """{"enabled": true, "command": "stop"}""");
        Assert.True(CampaignFile.ClearCommand(path));
        Assert.DoesNotContain(Directory.GetFiles(Path.GetDirectoryName(path)!, "*.tmp"), f => f.StartsWith(path));
    }

    [Fact]
    public void ClearCommandOnAMissingOrBrokenFileFailsWithoutThrowing()
    {
        Assert.False(CampaignFile.ClearCommand(TempPath()));
        var path = TempPath();
        File.WriteAllText(path, "not json");
        Assert.False(CampaignFile.ClearCommand(path));
        Assert.Equal("not json", File.ReadAllText(path));  // a file we cannot read is not overwritten
    }
}
