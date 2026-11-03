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

    [Fact]
    public void AutopilotHotkeyDefaultsToF9()
    {
        Assert.Equal("F9", AdvisorConfig.Defaults.AutopilotHotkey);
        var c = AdvisorConfig.Load(Write("""{"repo_path": "/r"}"""));
        Assert.Equal("F9", c.AutopilotHotkey);
    }

    [Fact]
    public void ReadsAutopilotHotkey()
    {
        var c = AdvisorConfig.Load(Write("""{"autopilot_hotkey": "F10"}"""));
        Assert.Equal("F10", c.AutopilotHotkey);
        Assert.Equal("F8", c.Hotkey);
    }

    [Fact]
    public void NonStringAutopilotHotkeyTakesTheDefault()
    {
        var c = AdvisorConfig.Load(Write("""{"autopilot_hotkey": 9}"""));
        Assert.Equal("F9", c.AutopilotHotkey);
    }

    [Fact]
    public void StepHotkeyDefaultsToF10AndIsRead()
    {
        Assert.Equal("F10", AdvisorConfig.Defaults.StepHotkey);
        Assert.Equal("F10", AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).StepHotkey);
        Assert.Equal("F11", AdvisorConfig.Load(Write("""{"step_hotkey": "F11"}""")).StepHotkey);
        Assert.Equal("F10", AdvisorConfig.Load(Write("""{"step_hotkey": 11}""")).StepHotkey);
    }

    [Fact]
    public void StepPauseDefaultsTo2_5SecondsAndIsRead()
    {
        Assert.Equal(2.5, AdvisorConfig.Defaults.StepPauseSeconds);
        Assert.Equal(2.5, AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).StepPauseSeconds);
        Assert.Equal(4, AdvisorConfig.Load(Write("""{"step_pause_seconds": 4}""")).StepPauseSeconds);
        Assert.Equal(0.75, AdvisorConfig.Load(Write("""{"step_pause_seconds": 0.75}""")).StepPauseSeconds);
        Assert.Equal(0, AdvisorConfig.Load(Write("""{"step_pause_seconds": 0}""")).StepPauseSeconds);
    }

    [Theory]
    [InlineData("-1", 0)]
    [InlineData("-0.5", 0)]
    [InlineData("10", 10)]
    [InlineData("10.5", 10)]
    [InlineData("999", 10)]
    [InlineData("1e9", 10)]
    [InlineData("1e400", 2.5)]
    public void StepPauseIsClampedToZeroToTen(string json, double expected)
    {
        Assert.Equal(expected, AdvisorConfig.Load(Write($$"""{"step_pause_seconds": {{json}}}""")).StepPauseSeconds);
    }

    [Theory]
    [InlineData("\"3\"")]
    [InlineData("true")]
    [InlineData("null")]
    [InlineData("[3]")]
    [InlineData("{}")]
    [InlineData("\"\"")]
    public void AnUnusableStepPauseTakesTheDefault(string json)
    {
        Assert.Equal(2.5, AdvisorConfig.Load(Write($$"""{"step_pause_seconds": {{json}}}""")).StepPauseSeconds);
    }

    [Fact]
    public void InstantModeDefaultsToFalseAndIsRead()
    {
        Assert.False(AdvisorConfig.Defaults.InstantMode);
        Assert.False(AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).InstantMode);
        Assert.True(AdvisorConfig.Load(Write("""{"instant_mode": true}""")).InstantMode);
        Assert.True(AdvisorConfig.Load(Write("""{"instant_mode": "TRUE"}""")).InstantMode);
        Assert.False(AdvisorConfig.Load(Write("""{"instant_mode": false}""")).InstantMode);
    }

    [Theory]
    [InlineData("1")]
    [InlineData("\"yes\"")]
    [InlineData("null")]
    [InlineData("[true]")]
    [InlineData("{}")]
    [InlineData("\"\"")]
    public void AnUnusableInstantModeTakesTheDefaultFalse(string json)
    {
        Assert.False(AdvisorConfig.Load(Write($$"""{"instant_mode": {{json}}}""")).InstantMode);
    }

    [Fact]
    public void PanelLanguageDefaultsToAuto()
    {
        Assert.Equal(PanelLanguage.Auto, AdvisorConfig.Defaults.PanelLanguage);
        Assert.Equal(PanelLanguage.Auto, AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).PanelLanguage);
    }

    [Theory]
    [InlineData("\"zh\"", PanelLanguage.Zh)]
    [InlineData("\"ZH-cn\"", PanelLanguage.Zh)]
    [InlineData("\"en\"", PanelLanguage.En)]
    [InlineData("\"English\"", PanelLanguage.En)]
    [InlineData("\"auto\"", PanelLanguage.Auto)]
    [InlineData("\"\"", PanelLanguage.Auto)]
    [InlineData("\"fr\"", PanelLanguage.Auto)]
    [InlineData("1", PanelLanguage.Auto)]
    [InlineData("true", PanelLanguage.Auto)]
    [InlineData("null", PanelLanguage.Auto)]
    [InlineData("[\"zh\"]", PanelLanguage.Auto)]
    [InlineData("{\"zh\": 1}", PanelLanguage.Auto)]
    public void PanelLanguageIsReadLeniently(string json, PanelLanguage expected)
    {
        Assert.Equal(expected, AdvisorConfig.Load(Write($$"""{"panel_language": {{json}}}""")).PanelLanguage);
    }

    [Fact]
    public void ABadPanelLanguageDoesNotSpoilTheOtherKeys()
    {
        var c = AdvisorConfig.Load(Write("""{"panel_language": 7, "port": 9000, "instant_mode": true}"""));
        Assert.Equal(PanelLanguage.Auto, c.PanelLanguage);
        Assert.Equal(9000, c.Port);
        Assert.True(c.InstantMode);
    }

    [Fact]
    public void MapPolicyDefaultsToHpAware()
    {
        Assert.Equal("hpaware", AdvisorConfig.Defaults.MapPolicy);
        Assert.Equal("hpaware", AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).MapPolicy);
    }

    [Theory]
    [InlineData("\"lookahead\"", "lookahead")]
    [InlineData("\" LookAhead \"", "lookahead")]
    [InlineData("\"look-ahead\"", "lookahead")]
    [InlineData("\"hpaware\"", "hpaware")]
    [InlineData("\"HpAware\"", "hpaware")]
    [InlineData("\"value\"", "hpaware")]
    [InlineData("\"\"", "hpaware")]
    [InlineData("true", "hpaware")]
    [InlineData("1", "hpaware")]
    [InlineData("null", "hpaware")]
    [InlineData("[\"lookahead\"]", "hpaware")]
    public void MapPolicyIsReadLeniently(string json, string expected)
    {
        Assert.Equal(expected, AdvisorConfig.Load(Write($$"""{"map_policy": {{json}}}""")).MapPolicy);
    }

    [Fact]
    public void ABadMapPolicyDoesNotSpoilTheOtherKeys()
    {
        var c = AdvisorConfig.Load(Write("""{"map_policy": 7, "port": 9000, "instant_mode": true}"""));
        Assert.Equal("hpaware", c.MapPolicy);
        Assert.Equal(9000, c.Port);
        Assert.True(c.InstantMode);
    }
}
