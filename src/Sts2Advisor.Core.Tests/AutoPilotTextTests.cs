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
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.FullAutoOff, zh: true);
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
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: false);
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
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true);
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
        var b = AutoPilotText.StopBanner(StopReason.Stuck, "NRewardsScreen", "F9", "F10", zh: true);
        Assert.Equal(LineStyle.Warning, b.Style);
        Assert.Contains(AutoPilotText.StopReasonText(StopReason.Stuck, true), b.Text);
        Assert.Contains("NRewardsScreen", b.Text);
        Assert.Contains("F9", b.Text);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.Hotkey, null, "F9", "F10", zh: true).Style);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.GameOver, null, "F9", "F10", zh: true).Style);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.LeftRun, null, "F9", "F10", zh: true).Style);
    }

    [Fact]
    public void SingleStepTitleNamesTheStepKey()
    {
        var s = new AutoPilotSession();
        s.Start(singleStep: true);
        Assert.Equal("单步执行中 · F10 停止", AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true)[0].Text);
        Assert.Equal("Single step · F10 to stop", AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: false)[0].Text);
    }

    [Fact]
    public void StepDoneBannerIsMutedAndOffersTheNextStep()
    {
        var b = AutoPilotText.StopBanner(StopReason.StepDone, null, "F9", "F10", zh: true);
        Assert.Equal(LineStyle.Muted, b.Style);
        Assert.Contains("F10", b.Text);
        Assert.DoesNotContain("F9", b.Text);
    }

    [Fact]
    public void ButtonsOfferStepAndFullAutoOrStop()
    {
        var idle = AutoPilotText.Buttons(running: false, "F10", "F9", zh: true);
        Assert.Equal(new[] { PanelAction.Step, PanelAction.Auto }, idle.Select(b => b.Action));
        Assert.Equal("执行这一步 (F10)", idle[0].Text);
        Assert.Equal("全自动 (F9)", idle[1].Text);
        var running = AutoPilotText.Buttons(running: true, "F10", "F9", zh: false);
        Assert.Equal(PanelAction.Stop, Assert.Single(running).Action);
        Assert.Equal("Stop", running[0].Text);
    }

    [Fact]
    public void AnEventStepShowsEveryOptionItWeighedUnderTheNewestStepOnly()
    {
        var reply = AdviceReply.Parse("""
        {"action": {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}},
         "options": [
           {"kind": "option", "index": 0, "score": 1.5, "status": "ok", "components": [{"key": "event.kw.energy_once", "value": 1.5}]},
           {"kind": "option", "index": 1, "score": 7.0, "status": "ok", "components": [{"key": "event.kw.card_reward", "value": 2.0}, {"key": "event.kw.relic", "value": 5.0}]},
           {"kind": "option", "index": 2, "score": 0.0, "status": "ok", "components": []}],
         "rules": [{"key": "event.pick_best", "outcome": "fired", "values": {"best_index": 1, "n_options": 3}}],
         "warnings": [], "unverified": false, "policy": "greedy"}
        """);
        var labels = new Dictionary<string, string> { ["option:0"] = "佩尔之泪", ["option:1"] = "佩尔之翼", ["option:2"] = "佩尔之眼" };
        var s = new AutoPilotSession();
        s.Start();
        s.Record("older", null, options: new[] { new PanelLine("never shown", LineStyle.Normal) });
        s.Record(AutoPilotText.StepText("event_choice", reply, labels, zh: true), AutoPilotText.Reason(reply, zh: true),
                 options: PanelText.OptionLines("event_choice", reply, labels, zh: true));
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true).Select(l => l.Text).ToList();
        Assert.Contains("事件：佩尔之翼", lines);
        Assert.Contains("  → 佩尔之翼  7.0", lines);
        Assert.Contains("    → 关键词：获得遗物 +5.0 · 卡牌奖励 +2.0", lines);
        Assert.Contains("    佩尔之泪  1.5", lines);
        Assert.Contains("      关键词：获得一次性能量 +1.5", lines);
        Assert.Contains("    佩尔之眼  0.0", lines);
        Assert.Contains("      无加分项", lines);
        Assert.Contains("  选分数最高的选项", lines);
        Assert.DoesNotContain("never shown", lines.Select(l => l.Trim()));
    }

    private static readonly string EnergyIcon = "[img]res://images/packed/sprite_fonts/ironclad_energy_icon.png[/img]";

    [Fact]
    public void ADetailLineIsTheCardsPlainDescriptionWhenOneCardWasChosen()
    {
        var lines = AutoPilotText.DetailLines(new[] { new DetailItem("Bash", "Deal [blue]8[/blue] damage.\nApply [gold]2[/gold] [gold]Vulnerable[/gold].") }, zh: false);
        Assert.Equal(new[] { "Deal 8 damage. Apply 2 Vulnerable." }, lines);
    }

    [Fact]
    public void ADetailLineSaysWhenItIsTheCardAfterTheUpgrade()
    {
        var item = new DetailItem("痛击", "造成[green]10[/green]点伤害。", AfterUpgrade: true);
        Assert.Equal(new[] { "升级后：造成10点伤害。" }, AutoPilotText.DetailLines(new[] { item }, zh: true));
        Assert.Equal(new[] { "After upgrade: 造成10点伤害。" }, AutoPilotText.DetailLines(new[] { item }, zh: false));
    }

    [Fact]
    public void SeveralChosenItemsEachNameThemselves()
    {
        var items = new[]
        {
            new DetailItem("打击", "造成6点伤害。"),
            new DetailItem("防御", "获得5点[gold]格挡[/gold]。"),
        };
        Assert.Equal(new[] { "打击：造成6点伤害。", "防御：获得5点格挡。" }, AutoPilotText.DetailLines(items, zh: true));
        Assert.Equal(new[] { "Strike: Deal 6 damage.", "Defend: Gain 5 Block." },
            AutoPilotText.DetailLines(new[] { new DetailItem("Strike", "Deal 6 damage."), new DetailItem("Defend", "Gain 5 [gold]Block[/gold].") }, zh: false));
    }

    [Fact]
    public void AnItemWithoutADescriptionAddsNoLineWhenAlone()
    {
        Assert.Empty(AutoPilotText.DetailLines(new[] { new DetailItem("Bash", "") }, zh: false));
        Assert.Empty(AutoPilotText.DetailLines(Array.Empty<DetailItem>(), zh: false));
    }

    [Fact]
    public void AnItemWithoutADescriptionStillNamesItselfAmongSeveral()
    {
        var items = new[] { new DetailItem("A", ""), new DetailItem("B", "text") };
        Assert.Equal(new[] { "A", "B: text" }, AutoPilotText.DetailLines(items, zh: false));
    }

    [Fact]
    public void OnlyTheFirstFewItemsGetALine()
    {
        var items = Enumerable.Range(1, AutoPilotText.MaxDetailItems + 3).Select(i => new DetailItem($"c{i}", $"d{i}")).ToList();
        var lines = AutoPilotText.DetailLines(items, zh: false);
        Assert.Equal(AutoPilotText.MaxDetailItems, lines.Count);
        Assert.Equal("c1: d1", lines[0]);
    }

    [Fact]
    public void ALongDescriptionIsCutInTheDetailLine()
    {
        var line = Assert.Single(AutoPilotText.DetailLines(new[] { new DetailItem("x", new string('a', 300)) }, zh: false));
        Assert.Equal(DescriptionText.DefaultMaxChars, line.Length);
        Assert.EndsWith("…", line);
    }

    [Fact]
    public void EnergyIconsInADetailLineBecomeText()
    {
        var line = Assert.Single(AutoPilotText.DetailLines(new[] { new DetailItem("Offering", $"Gain {EnergyIcon}{EnergyIcon}.") }, zh: false));
        Assert.Equal("Gain 2 Energy.", line);
    }

    [Fact]
    public void TheLatestStepShowsItsDetailsOnMutedLinesUnderItAndEarlierStepsDoNot()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("地图：问号", null, new[] { "ignored: this is not the newest step" });
        s.Record("卡牌奖励：痛击", "因为", new[] { "造成8点伤害。", "施加2层易伤。" });
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true);
        Assert.Equal(new PanelLine("全托管中 · F9 接管", LineStyle.Title), lines[0]);
        Assert.Equal(new PanelLine("卡牌奖励：痛击", LineStyle.Pick), lines[1]);
        Assert.Equal(new PanelLine("  造成8点伤害。", LineStyle.Muted), lines[2]);
        Assert.Equal(new PanelLine("  施加2层易伤。", LineStyle.Muted), lines[3]);
        Assert.Equal(new PanelLine("  因为", LineStyle.Muted), lines[4]);
        Assert.Equal(new PanelLine("地图：问号", LineStyle.Normal), lines[5]);
        Assert.Equal(6, lines.Count);
        Assert.DoesNotContain(lines, l => l.Text.Contains("ignored"));
    }

    [Fact]
    public void AStepWithoutDetailsRendersAsBefore()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("事件：离开", "因为");
        var lines = AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true);
        Assert.Equal(new PanelLine("事件：离开", LineStyle.Pick), lines[1]);
        Assert.Equal(new PanelLine("  因为", LineStyle.Muted), lines[2]);
        Assert.Equal(3, lines.Count);
    }
}
