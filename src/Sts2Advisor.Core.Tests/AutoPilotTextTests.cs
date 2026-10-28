// src/Sts2Advisor.Core.Tests/AutoPilotTextTests.cs
using Sts2Advisor.Core;

public class AutoPilotTextTests
{
    private static readonly Dictionary<string, string> Labels = new() { ["card:1"] = "痛击" };

    [Fact]
    public void StepTextNamesTheDecisionAndThePick()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        Assert.Equal("卡牌奖励：痛击", AutoPilotText.StepText("card_reward", reply, Labels, zh: true));
        Assert.Equal("Card reward: 痛击", AutoPilotText.StepText("card_reward", reply, Labels, zh: false));
    }

    [Fact]
    public void ReasonIsTheFirstFiredRule()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        Assert.Equal(TraceText.Rule(reply.Rules[0], true), AutoPilotText.Reason(reply, zh: true));
        var none = AdviceReply.Parse("""{"action": {"action": "leave_room"}, "rules": [{"key": "shop.leave", "outcome": "skipped", "values": {}}]}""");
        Assert.Null(AutoPilotText.Reason(none, zh: true));
    }

    [Fact]
    public void RunningShowsTitleStepsNewestFirstAndWarnings()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("地图：问号", null);
        s.Record("事件：离开", "因为");
        s.HandBack("NChooseARelicSelection");
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.FullAutoOff, zh: true);
        Assert.Equal(new PanelLine("全托管中 · F9 接管", LineStyle.Title), lines[0]);
        Assert.Equal(LineStyle.Warning, lines[1].Style);
        Assert.Contains("CombatSolver", lines[1].Text);
        Assert.Equal(LineStyle.Warning, lines[2].Style);
        Assert.Contains("手动", lines[2].Text);
        Assert.Equal(new PanelLine("事件：离开", LineStyle.Pick), lines[3]);
        Assert.Equal(new PanelLine("  因为", LineStyle.Muted), lines[4]);
        Assert.Equal(new PanelLine("地图：问号", LineStyle.Normal), lines[5]);
        Assert.Equal(6, lines.Count);
    }

    [Fact]
    public void RunningWithNothingYetSaysItIsWaiting()
    {
        var s = new AutoPilotSession();
        s.Start();
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.Ok, zh: false);
        Assert.Equal("Autopilot on · F9 to take over", lines[0].Text);
        Assert.Equal(LineStyle.Muted, lines[1].Style);
        Assert.Equal(2, lines.Count);
    }

    [Fact]
    public void RunningInCombatSaysSo()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.InCombat = true;
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.Ok, zh: true);
        Assert.Contains(lines, l => l.Text.Contains("CombatSolver") && l.Style == LineStyle.Muted);
    }

    [Fact]
    public void EveryStopReasonHasItsOwnTextInBothLanguages()
    {
        foreach (var r in Enum.GetValues<StopReason>())
        {
            string zh = AutoPilotText.StopReasonText(r, true), en = AutoPilotText.StopReasonText(r, false);
            Assert.False(string.IsNullOrWhiteSpace(zh));
            Assert.False(string.IsNullOrWhiteSpace(en));
            Assert.NotEqual(r.ToString(), en);
            Assert.NotEqual(zh, en);
        }
        Assert.Equal(Enum.GetValues<StopReason>().Length,
                     Enum.GetValues<StopReason>().Select(r => AutoPilotText.StopReasonText(r, true)).Distinct().Count());
    }

    [Fact]
    public void StopBannerCarriesReasonDetailAndHotkey()
    {
        var b = AutoPilotText.StopBanner(StopReason.Stuck, "NRewardsScreen", "F9", zh: true);
        Assert.Equal(LineStyle.Warning, b.Style);
        Assert.Contains(AutoPilotText.StopReasonText(StopReason.Stuck, true), b.Text);
        Assert.Contains("NRewardsScreen", b.Text);
        Assert.Contains("F9", b.Text);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.Hotkey, null, "F9", zh: true).Style);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.GameOver, null, "F9", zh: true).Style);
    }
}
