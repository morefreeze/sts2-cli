// src/Sts2Advisor.Core.Tests/CampaignTextTests.cs
using Sts2Advisor.Core;

public class CampaignTextTests
{
    private static CampaignView View(string? note = null, string? problem = null, string? stuck = null, bool enabled = true,
                                     string? character = "IRONCLAD", AscensionSpec? asc = null, int runs = 3, int wins = 1) =>
        new(enabled, character, character == null ? null : asc ?? AscensionSpec.Of(6), runs, wins, note, problem, stuck);

    [Fact]
    public void AnOffCampaignHasNoLine()
    {
        Assert.Null(CampaignText.Status(View(enabled: false), zh: false));
    }

    [Fact]
    public void TheLineSaysWhatComesNextAndHowItWentSoFar()
    {
        var line = CampaignText.Status(View(), zh: false)!;
        Assert.Contains("Campaign", line.Text);
        Assert.Contains("IRONCLAD a6", line.Text);
        Assert.Contains("3 runs", line.Text);
        Assert.Contains("1 win", line.Text);
        Assert.DoesNotContain("1 wins", line.Text);
        Assert.Equal(LineStyle.Muted, line.Style);
    }

    [Fact]
    public void TheChineseLineUsesTheSameFacts()
    {
        var line = CampaignText.Status(View(), zh: true)!;
        Assert.Contains("战役", line.Text);
        Assert.Contains("IRONCLAD a6", line.Text);
        Assert.Contains("3 局", line.Text);
        Assert.Contains("1 胜", line.Text);
    }

    [Fact]
    public void AMaxAscensionIsShownAsMax()
    {
        var line = CampaignText.Status(View(asc: AscensionSpec.Max), zh: false)!;
        Assert.Contains("IRONCLAD max", line.Text);
    }

    [Fact]
    public void WhatTheCampaignIsWaitingForIsAppended()
    {
        var line = CampaignText.Status(View(note: "waiting: the game is loading"), zh: false)!;
        Assert.EndsWith("waiting: the game is loading", line.Text);
    }

    [Fact]
    public void WithoutANextTheLineSaysSo()
    {
        var line = CampaignText.Status(View(character: null), zh: false)!;
        Assert.Contains("next: none", line.Text);
    }

    [Fact]
    public void AStuckCampaignIsAWarningThatSaysHowToResume()
    {
        var line = CampaignText.Status(View(stuck: "StartRun did not take effect"), zh: false)!;
        Assert.Equal(LineStyle.Warning, line.Style);
        Assert.Contains("stuck", line.Text, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("StartRun did not take effect", line.Text);
        Assert.Contains("campaign.json", line.Text);
    }

    [Fact]
    public void ABrokenFileIsAWarningEvenThoughNoCampaignRuns()
    {
        var line = CampaignText.Status(View(enabled: false, problem: "campaign.json: not valid JSON"), zh: false)!;
        Assert.Equal(LineStyle.Warning, line.Style);
        Assert.Contains("not valid JSON", line.Text);
    }

    [Fact]
    public void ANeedsAttentionNoteIsAWarning()
    {
        var line = CampaignText.Status(View(note: "needs attention: the autopilot stopped again on A2F5"), zh: false)!;
        Assert.Equal(LineStyle.Warning, line.Style);
    }
}
