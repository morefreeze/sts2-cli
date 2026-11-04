using System.Text.Json;
using Sts2Advisor.Core;

public class RunIdentityTests
{
    [Fact]
    public void WritesAllFourFields()
    {
        using var doc = JsonDocument.Parse(RunIdentity.ToJson(1730000000123, "SEED1", "NECROBINDER", 9));
        var r = doc.RootElement;
        Assert.Equal(1730000000123, r.GetProperty("start_time").GetInt64());
        Assert.Equal("SEED1", r.GetProperty("seed").GetString());
        Assert.Equal("NECROBINDER", r.GetProperty("character").GetString());
        Assert.Equal(9, r.GetProperty("ascension").GetInt32());
    }

    [Fact]
    public void ZeroStartTimeAndNullStringsAreNull()
    {
        using var doc = JsonDocument.Parse(RunIdentity.ToJson(0, null, null, 0));
        var r = doc.RootElement;
        Assert.Equal(JsonValueKind.Null, r.GetProperty("start_time").ValueKind);
        Assert.Equal(JsonValueKind.Null, r.GetProperty("seed").ValueKind);
        Assert.Equal(JsonValueKind.Null, r.GetProperty("character").ValueKind);
        Assert.Equal(0, r.GetProperty("ascension").GetInt32());
    }

    [Fact]
    public void IsSingleLineAndKeepsNonAscii()
    {
        var json = RunIdentity.ToJson(1, "种子", "IRONCLAD", 1);
        Assert.DoesNotContain("\n", json);
        Assert.Contains("种子", json);
    }
}
