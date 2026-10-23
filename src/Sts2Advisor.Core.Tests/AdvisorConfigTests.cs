using System.Text.Json;
using Sts2Advisor.Core;

public class AdvisorConfigTests
{
    private static string Write(string json)
    {
        var path = Path.Combine(Path.GetTempPath(), $"sts2advisor-{Guid.NewGuid()}.json");
        File.WriteAllText(path, json);
        return path;
    }

    [Fact]
    public void MissingFileGivesDefaults()
    {
        var c = AdvisorConfig.Load(Path.Combine(Path.GetTempPath(), $"absent-{Guid.NewGuid()}.json"));
        Assert.Equal(AdvisorConfig.Defaults, c);
        Assert.Equal(8767, c.Port);
        Assert.True(c.Autostart);
        Assert.Equal("F8", c.Hotkey);
        Assert.Equal("", c.ResolvedPython);
    }

    [Fact]
    public void ReadsEveryKey()
    {
        var c = AdvisorConfig.Load(Write(
            """{"repo_path": "/r", "python": "/p/python", "port": 9000, "autostart": false, "hotkey": "F9"}"""));
        Assert.Equal(new AdvisorConfig("/r", "/p/python", 9000, false, "F9"), c);
        Assert.Equal("/p/python", c.ResolvedPython);
    }

    [Fact]
    public void PythonDefaultsToTheRepoVenv()
    {
        var c = AdvisorConfig.Load(Write("""{"repo_path": "/r"}"""));
        Assert.Equal(Path.Combine("/r", ".venv", "bin", "python"), c.ResolvedPython);
        Assert.Equal(8767, c.Port);
    }

    [Fact]
    public void MalformedJsonThrows()
    {
        Assert.ThrowsAny<JsonException>(() => AdvisorConfig.Load(Write("{not json")));
    }
}
