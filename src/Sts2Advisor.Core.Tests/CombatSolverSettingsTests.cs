// src/Sts2Advisor.Core.Tests/CombatSolverSettingsTests.cs
using Sts2Advisor.Core;

public class CombatSolverSettingsTests
{
    private static string Write(string text)
    {
        var path = Path.Combine(Path.GetTempPath(), $"cs-{Guid.NewGuid()}.json");
        File.WriteAllText(path, text);
        return path;
    }

    [Fact]
    public void MissingFile() =>
        Assert.Equal(CombatSolverStatus.Missing,
                     CombatSolverSettings.Read(Path.Combine(Path.GetTempPath(), $"absent-{Guid.NewGuid()}.json")));

    [Fact]
    public void FullAutoOn() =>
        Assert.Equal(CombatSolverStatus.Ok, CombatSolverSettings.Read(Write("""{"solverDisabled": false, "autoEnableFullAuto": true}""")));

    [Theory]
    [InlineData("""{"autoEnableFullAuto": false}""")]
    [InlineData("""{"solverDisabled": false}""")]
    [InlineData("""{"autoEnableFullAuto": "true"}""")]
    [InlineData("""{"autoEnableFullAuto": true, "solverDisabled": true}""")]
    public void FullAutoOff(string json) =>
        Assert.Equal(CombatSolverStatus.FullAutoOff, CombatSolverSettings.Read(Write(json)));

    [Theory]
    [InlineData("not json")]
    [InlineData("[1, 2]")]
    public void Unreadable(string text) =>
        Assert.Equal(CombatSolverStatus.Unreadable, CombatSolverSettings.Read(Write(text)));
}
