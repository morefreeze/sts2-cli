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

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("[]")]
    [InlineData("[1, 2]")]
    [InlineData("42")]
    [InlineData("null")]
    [InlineData("true")]
    [InlineData("\"port\"")]
    [InlineData("{\"port\": 9000")]
    public void AnUnusableFileThrowsExactlyJsonException(string content)
    {
        // Callers catch JsonException only; nothing else may escape.
        var e = Record.Exception(() => AdvisorConfig.Load(Write(content)));
        Assert.NotNull(e);
        Assert.Equal(typeof(JsonException), e!.GetType());
    }

    [Fact]
    public void AnUnreadableFileThrowsJsonExceptionWithTheOriginalInside()
    {
        if (OperatingSystem.IsWindows())
            return;
        var path = Write("""{"port": 9000}""");
        File.SetUnixFileMode(path, UnixFileMode.None);
        try
        {
            try
            {
                File.ReadAllText(path);
                return; // running with rights that ignore file modes (root): nothing to test
            }
            catch (UnauthorizedAccessException) { }
            var e = Record.Exception(() => AdvisorConfig.Load(path));
            Assert.Equal(typeof(JsonException), e!.GetType());
            Assert.IsType<UnauthorizedAccessException>(e.InnerException);
        }
        finally
        {
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
        }
    }

    [Theory]
    [InlineData("1", 1)]
    [InlineData("8767", 8767)]
    [InlineData("65535", 65535)]
    [InlineData("0", 8767)]
    [InlineData("-5", 8767)]
    [InlineData("65536", 8767)]
    [InlineData("99999999999", 8767)]
    [InlineData("9000.5", 8767)]
    [InlineData("\"9000\"", 8767)]
    [InlineData("true", 8767)]
    [InlineData("null", 8767)]
    [InlineData("[9000]", 8767)]
    public void PortMustBeANumberInTheValidRange(string portJson, int expected)
    {
        Assert.Equal(expected, AdvisorConfig.Load(Write($$"""{"port": {{portJson}}}""")).Port);
    }

    [Theory]
    [InlineData("true", true)]
    [InlineData("false", false)]
    [InlineData("\"true\"", true)]
    [InlineData("\"false\"", false)]
    [InlineData("\"False\"", false)]
    [InlineData("\"FALSE\"", false)]
    [InlineData("\"TRUE\"", true)]
    [InlineData("\"yes\"", true)]
    [InlineData("\"no\"", true)]
    [InlineData("\"\"", true)]
    [InlineData("0", true)]
    [InlineData("1", true)]
    [InlineData("null", true)]
    [InlineData("[]", true)]
    [InlineData("{}", true)]
    public void AutostartTakesBoolsAndTrueFalseStringsElseTheDefault(string autostartJson, bool expected)
    {
        Assert.Equal(expected, AdvisorConfig.Load(Write($$"""{"autostart": {{autostartJson}}}""")).Autostart);
    }

    [Fact]
    public void WronglyTypedStringKeysFallBackToTheDefaults()
    {
        var c = AdvisorConfig.Load(Write("""{"repo_path": 5, "python": null, "hotkey": ["F9"], "port": "9000", "autostart": 1}"""));
        Assert.Equal(AdvisorConfig.Defaults, c);
    }

    [Fact]
    public void UnknownKeysAreIgnored()
    {
        var c = AdvisorConfig.Load(Write("""{"repo_path": "/r", "future_option": {"a": 1}}"""));
        Assert.Equal("/r", c.RepoPath);
    }
}
