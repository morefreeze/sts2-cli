using System.Text.Json;
using Sts2Advisor.Core;

public class PanelTextTests
{
    private static TraceRule Rule(string key, string outcome, string valuesJson)
    {
        using var doc = JsonDocument.Parse(valuesJson);
        return new TraceRule(key, outcome,
            doc.RootElement.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.Clone()));
    }

    private static TraceComponent Comp(string key, double value, string? detailJson = null)
    {
        JsonElement? detail = null;
        if (detailJson != null)
        {
            using var doc = JsonDocument.Parse(detailJson);
            detail = doc.RootElement.Clone();
        }
        return new TraceComponent(key, value, detail);
    }

    private static readonly DecisionHeader CardHeader = new("card_reward", 1, 13, 1, 52, 80, 99);

    [Fact]
    public void RuleTemplatesFormatPercentagesAndNumbers()
    {
        var r = Rule("rest.heal_threshold", "fired", """{"hp_ratio": 0.5125, "threshold": 0.85, "floor": 11}""");
        Assert.Equal("HP 51%，本层休息阈值 85%", TraceText.Rule(r, zh: true));
        Assert.Equal("HP 51%, rest threshold here 85%", TraceText.Rule(r, zh: false));
    }

    [Fact]
    public void MissingValuesRenderAsDash()
    {
        var r = Rule("shop.relic", "skipped", """{"threshold": 4.0, "gold_reserve": 25, "n_affordable": 0}""");
        Assert.Equal("最佳遗物 —，阈值 4，至少留 25 金币", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void WordValuesAreTranslated()
    {
        var r = Rule("shop.remove_basic", "skipped", """{"n_basic": 4, "basic_scope": "ironclad_only"}""");
        Assert.Contains("只统计 Ironclad 的打击和防御（实测配置）", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void UnknownKeysShowTheKeyAndValues()
    {
        var r = Rule("x.brand_new", "fired", """{"a": 1}""");
        Assert.Equal("x.brand_new a=1", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void ListAndNullValuesNeverCrashATemplate()
    {
        // picked / tied_best are lists, best_card_score may be null: lists render comma-joined, null as a dash.
        var unknown = Rule("x.brand_new", "fired", """{"picked": [0, 2], "tied_best": [1], "none": null, "empty": []}""");
        Assert.Equal("x.brand_new picked=0, 2 tied_best=1 none=— empty=—", TraceText.Rule(unknown, zh: true));

        var known = Rule("card_select.top_scores", "fired", """{"max_select": [1, 2], "n_cards": null, "picked": [0]}""");
        Assert.Equal("挑高分：选分数最高的 1, 2 张（没有低于 3 分的卡，共 — 张）", TraceText.Rule(known, zh: true));

        var shop = Rule("shop.remove_value", "skipped", """{"removal_value": 4.5, "best_card_score": null}""");
        Assert.Equal("删牌价值 4.5，最佳卡 —", TraceText.Rule(shop, zh: true));
    }

    [Fact]
    public void CardBaseIsTheCardScoreWithItsTierAsDetail()
    {
        var baseWithTier = Comp("card.base", 6.8, "\"A\"");
        Assert.Equal("卡牌分（评级 A） 6.8", TraceText.Component(baseWithTier, zh: true));
        Assert.Equal("Card score (tier A) 6.8", TraceText.Component(baseWithTier, zh: false));
        Assert.Equal("卡牌分 6.8", TraceText.Component(Comp("card.base", 6.8), zh: true));
        Assert.Equal("Card score 6.8", TraceText.Component(Comp("card.base", 6.8, "null"), zh: false));
    }

    [Fact]
    public void CardScoreLabelsStayDistinct()
    {
        var labels = new[] { "card.base", "card.score", "card.in_deck_score" }
            .Select(k => TraceText.Label(k, zh: true)).ToArray();
        Assert.Equal(new[] { "卡牌分", "卡牌分（不含牌组）", "卡牌分（含牌组协同）" }, labels);
        Assert.Equal(3, labels.Distinct().Count());
        Assert.Equal("Card score (deck-free)", TraceText.Label("card.score", zh: false));
        Assert.Equal("Card score (with deck synergy)", TraceText.Label("card.in_deck_score", zh: false));
        Assert.Equal("Card score", TraceText.Label("card.base", zh: false));
        Assert.Equal("卡牌分（不含牌组） 3.0", TraceText.Component(Comp("card.score", 3.0), zh: true));
        Assert.Equal("卡牌分（含牌组协同） 4.5", TraceText.Component(Comp("card.in_deck_score", 4.5), zh: true));
    }

    [Theory]
    [InlineData("rest.critical_heal")]
    [InlineData("rest.vantom_mid_boss")]
    [InlineData("rest.pre_boss_heal")]
    [InlineData("rest.heal_threshold")]
    [InlineData("rest.must_upgrade")]
    public void RestRulesSayWhenTheirOptionIsMissing(string key)
    {
        var missing = Rule(key, "skipped", """{"hp_ratio": 0.4, "threshold": 0.5, "floor": 9, "available": false}""");
        Assert.EndsWith("（没有该选项）", TraceText.Rule(missing, zh: true));
        Assert.EndsWith(" (option unavailable)", TraceText.Rule(missing, zh: false));

        var present = Rule(key, "fired", """{"hp_ratio": 0.4, "threshold": 0.5, "floor": 9, "available": true}""");
        Assert.DoesNotContain("没有该选项", TraceText.Rule(present, zh: true));
        Assert.DoesNotContain("unavailable", TraceText.Rule(present, zh: false));
        var absent = Rule(key, "fired", """{"hp_ratio": 0.4, "threshold": 0.5, "floor": 9}""");
        Assert.DoesNotContain("没有该选项", TraceText.Rule(absent, zh: true));
    }

    [Fact]
    public void RestRuleSuffixIsAppendedToTheFormattedLine()
    {
        var r = Rule("rest.heal_threshold", "skipped", """{"hp_ratio": 0.5, "threshold": 0.85, "floor": 11, "available": false}""");
        Assert.Equal("HP 50%，本层休息阈值 85%（没有该选项）", TraceText.Rule(r, zh: true));
        Assert.Equal("HP 50%, rest threshold here 85% (option unavailable)", TraceText.Rule(r, zh: false));
    }

    [Fact]
    public void ShopHealPotionSaysWhenNoneIsForSale()
    {
        var r = Rule("shop.heal_potion", "skipped",
            """{"hp_ratio": 0.3, "held_potions": 1, "slots_free": true, "available": false}""");
        Assert.Equal("HP 30% < 50% 且药水 1/3 时买回复药水（没有可买的回复药水）", TraceText.Rule(r, zh: true));
        Assert.Equal("Heal potion when HP 30% < 50% and potions 1/3 (no heal potion for sale)", TraceText.Rule(r, zh: false));

        // Each condition is needed: HP not low, no free slot, or a potion on sale.
        foreach (var json in new[]
        {
            """{"hp_ratio": 0.6, "held_potions": 1, "slots_free": true, "available": false}""",
            """{"hp_ratio": 0.5, "held_potions": 1, "slots_free": true, "available": false}""",
            """{"hp_ratio": 0.3, "held_potions": 3, "slots_free": false, "available": false}""",
            """{"hp_ratio": 0.3, "held_potions": 1, "slots_free": true, "available": true}""",
            """{"hp_ratio": 0.3, "held_potions": 1}""",
        })
        {
            var other = Rule("shop.heal_potion", "skipped", json);
            Assert.DoesNotContain("没有可买", TraceText.Rule(other, zh: true));
            Assert.DoesNotContain("no heal potion", TraceText.Rule(other, zh: false));
        }
    }

    [Theory]
    [InlineData("card_select.rest_best", "休息处锻造：选分数最高的卡")]
    [InlineData("card_select.combat_best", "选分数最高的卡")]
    public void CardSelectBestSaysWhenItDefaultedToTheFirstCard(string key, string zhBase)
    {
        var fell = Rule(key, "fired", """{"picked": [0], "fallback": true, "room_type": "Monster", "n_cards": 2, "max_select": 1}""");
        Assert.Equal(zhBase + "（没有可用的卡，默认第 1 张）", TraceText.Rule(fell, zh: true));
        Assert.EndsWith(" (no usable card; defaulted to the first)", TraceText.Rule(fell, zh: false));

        var normal = Rule(key, "fired", """{"picked": [1], "fallback": false, "room_type": "Monster", "n_cards": 2, "max_select": 1}""");
        Assert.Equal(zhBase, TraceText.Rule(normal, zh: true));
    }

    [Theory]
    [InlineData("card_select.rest_best")]
    [InlineData("card_select.combat_best")]
    public void CardSelectBestNeverClaimsToPickUpToN(string key)
    {
        // Both always select exactly one card, whatever max_select says.
        var r = Rule(key, "fired", """{"picked": [2], "fallback": false, "room_type": "Monster", "n_cards": 5, "max_select": 3}""");
        var zh = TraceText.Rule(r, zh: true);
        var en = TraceText.Rule(r, zh: false);
        Assert.DoesNotContain("3", zh);
        Assert.DoesNotContain("3", en);
        Assert.DoesNotContain("最多", zh);
        Assert.DoesNotContain("up to", en, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("best 3", en);
    }

    [Fact]
    public void CardRewardGateFallbackIsSaid()
    {
        var r = Rule("card_reward.pick_best", "fired",
            """{"best_index": 0, "best_score": 5.1, "threshold": 4.5, "act": 1, "deck_size": 20, "quality_gate": true, "gate_fallback": true}""");
        Assert.Equal("最高分 5.1 ≥ 跳过阈值 4.5（第 1 幕，牌组 20 张）（质量门过滤了全部且不能跳过，改从全部卡中选）",
                     TraceText.Rule(r, zh: true));
        Assert.EndsWith(" (quality gate filtered all and skip is not allowed; picked from all)", TraceText.Rule(r, zh: false));

        var plain = Rule("card_reward.pick_best", "fired",
            """{"best_index": 0, "best_score": 5.1, "threshold": 4.5, "act": 1, "deck_size": 20, "quality_gate": true, "gate_fallback": false}""");
        Assert.DoesNotContain("质量门", TraceText.Rule(plain, zh: true));
    }

    [Fact]
    public void MapTieIsSaidOnlyForTwoOrMoreBest()
    {
        var tie = Rule("map.pick_min_priority", "fired", """{"best_index": 0, "tied_best": [0, 2], "hp_ratio": 0.9}""");
        Assert.Equal("选优先级数值最小的节点（越小越好）（并列，取列表中第一个）", TraceText.Rule(tie, zh: true));
        Assert.Equal("Lowest priority number wins (lower is better) (tie: first listed wins)", TraceText.Rule(tie, zh: false));

        var single = Rule("map.pick_min_priority", "fired", """{"best_index": 1, "tied_best": [1]}""");
        Assert.Equal("选优先级数值最小的节点（越小越好）", TraceText.Rule(single, zh: true));
        var none = Rule("map.pick_min_priority", "fired", "{}");
        Assert.Equal("Lowest priority number wins (lower is better)", TraceText.Rule(none, zh: false));
    }

    [Fact]
    public void CardRewardPanel()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        var labels = new Dictionary<string, string> { ["card:0"] = "打击", ["card:1"] = "燃烧" };
        var lines = PanelText.Render(CardHeader, reply, labels, zh: true);
        Assert.Equal(new PanelLine("卡牌奖励 · A1F13a1 · HP 52/80 · 金币 99", LineStyle.Title), lines[0]);
        Assert.Equal(new PanelLine("建议：燃烧", LineStyle.Pick), lines[1]);
        Assert.Contains(new PanelLine("燃烧  7.4", LineStyle.Fired), lines);
        Assert.Contains(new PanelLine("  卡牌分（评级 A） 6.8 · 格挡缺口 +0.6", LineStyle.Muted), lines);
        Assert.Contains(new PanelLine("打击  已过滤（第 1 幕后期质量门）", LineStyle.Muted), lines);
        Assert.Contains(new PanelLine("最高分 7.4 ≥ 跳过阈值 5.5（第 1 幕，牌组 14 张）", LineStyle.Fired), lines);
        Assert.Equal(new PanelLine("STS2_MC_ROLLOUT=1", LineStyle.Warning), lines[^1]);
        Assert.Equal(new PanelLine("greedy 启发式打分，不是概率", LineStyle.Muted), lines[^2]);
    }

    [Fact]
    public void CardRewardPanelInEnglish()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        var labels = new Dictionary<string, string> { ["card:0"] = "Strike", ["card:1"] = "Inflame" };
        var lines = PanelText.Render(CardHeader, reply, labels, zh: false);
        Assert.Contains(new PanelLine("  Card score (tier A) 6.8 · Block gap +0.6", LineStyle.Muted), lines);
    }

    [Fact]
    public void ReservedShopRelicIsExplained()
    {
        var reply = AdviceReply.Parse("""
        {"action": {"cmd": "action", "action": "leave_room", "args": {}},
         "options": [
           {"kind": "relic", "index": 0, "score": null, "status": "reserved", "components": []},
           {"kind": "relic", "index": 1, "score": null, "status": "unaffordable", "components": []}],
         "rules": [{"key": "shop.leave", "outcome": "fired", "values": {}}],
         "warnings": [], "unverified": false, "policy": "greedy"}
        """);
        var labels = new Dictionary<string, string> { ["relic:0"] = "炼金术", ["relic:1"] = "壶" };
        var zh = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 60), reply, labels, zh: true);
        Assert.Contains(new PanelLine("炼金术  留作保底金（需留 25 金币）", LineStyle.Muted), zh);
        Assert.Contains(new PanelLine("壶  买不起", LineStyle.Muted), zh);
        var en = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 60), reply, labels, zh: false);
        Assert.Contains(new PanelLine("炼金术  inside the 25-gold reserve", LineStyle.Muted), en);
    }

    [Fact]
    public void EventOptionsShowKeywordsOrSayNoneMatched()
    {
        var reply = AdviceReply.Parse("""
        {"action": {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}},
         "options": [
           {"kind": "option", "index": 0, "score": -2.0, "status": "ok",
            "components": [{"key": "event.kw.lose_hp", "value": -2.0}]},
           {"kind": "option", "index": 1, "score": 0.0, "status": "ok", "components": []},
           {"kind": "option", "index": 2, "score": null, "status": "locked", "components": []}],
         "rules": [{"key": "event.pick_best", "outcome": "fired", "values": {"best_index": 1, "n_options": 3}}],
         "warnings": [], "unverified": false, "policy": "greedy"}
        """);
        var labels = new Dictionary<string, string> { ["option:0"] = "祈祷", ["option:1"] = "离开", ["option:2"] = "献祭" };
        var zh = PanelText.Render(new DecisionHeader("event_choice", 1, 5, 1, 60, 80, 30), reply, labels, zh: true);
        var i1 = zh.ToList().IndexOf(new PanelLine("离开  0.0", LineStyle.Fired));
        Assert.True(i1 > 0);
        Assert.Equal(new PanelLine("  未命中关键词", LineStyle.Muted), zh[i1 + 1]);
        Assert.Equal(new PanelLine("祈祷  -2.0", LineStyle.Normal), zh[i1 + 2]);
        Assert.Equal(new PanelLine("  关键词：失去生命 -2.0", LineStyle.Muted), zh[i1 + 3]);
        Assert.Contains(new PanelLine("献祭  已锁定", LineStyle.Muted), zh);

        var en = PanelText.Render(new DecisionHeader("event_choice", 1, 5, 1, 60, 80, 30), reply, labels, zh: false);
        Assert.Contains(new PanelLine("  no keyword matched", LineStyle.Muted), en);
        Assert.Contains(new PanelLine("  Keywords: Lose HP -2.0", LineStyle.Muted), en);
    }

    [Fact]
    public void KeywordPrefixIsOnlyForEventOptions()
    {
        // A card option with components keeps the plain "a · b" sub-line.
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        var lines = PanelText.Render(CardHeader, reply, new Dictionary<string, string>(), zh: true);
        Assert.DoesNotContain(lines, l => l.Text.Contains("关键词") || l.Text.Contains("未命中"));
    }

    [Fact]
    public void MapPanelSortsAscendingAndSaysUnverified()
    {
        var reply = AdviceReply.Parse("""
        {"action": {"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 4}},
         "options": [
           {"kind": "node", "index": 0, "score": 5, "status": "ok", "detail": {"col": 1, "row": 4, "type": "Monster"},
            "components": [{"key": "map.base_priority", "value": 5, "detail": "Monster"}]},
           {"kind": "node", "index": 1, "score": 4.1, "status": "ok", "detail": {"col": 3, "row": 4, "type": "Elite"},
            "components": [{"key": "map.base_priority", "value": 6, "detail": "Elite"},
                           {"key": "map.elite_strong_midgame", "value": -1.9}]}],
         "rules": [{"key": "map.pick_min_priority", "outcome": "fired", "values": {}}],
         "warnings": [], "unverified": true, "policy": "greedy"}
        """);
        var labels = new Dictionary<string, string> { ["node:0"] = "敌人 (1,4)", ["node:1"] = "精英 (3,4)" };
        var lines = PanelText.Render(new DecisionHeader("map_select", 1, 8, 1, 75, 80, 40), reply, labels, zh: true);
        Assert.Equal("建议：精英 (3,4)", lines[1].Text);
        Assert.Equal(new PanelLine("精英 (3,4)  4.1", LineStyle.Fired), lines[2]);
        Assert.Equal("敌人 (1,4)  5.0", lines.First(l => l.Text.StartsWith("敌人")).Text);
        Assert.Contains(new PanelLine("  房间类型 6.0 · HP ≥ 85% 且第 7–12 层 -1.9", LineStyle.Muted), lines);
        Assert.Equal(new PanelLine("启发式，只看下一步，未经 A/B 验证", LineStyle.Muted), lines[^1]);
    }

    [Fact]
    public void NoActionShowsNoAdviceAndWarnings()
    {
        var reply = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_raised: KeyError"], "unverified": false, "policy": "greedy"}""");
        var lines = PanelText.Render(new DecisionHeader("shop", 2, 4, 1, 60, 80, 182), reply,
                                     new Dictionary<string, string>(), zh: true);
        Assert.Equal(new PanelLine("无建议", LineStyle.Warning), lines[1]);
        Assert.Equal(new PanelLine("greedy_raised: KeyError", LineStyle.Muted), lines[2]);
    }

    [Fact]
    public void StatusLines()
    {
        Assert.Equal("战斗中，交给 CombatSolver",
                     PanelText.Status(CardHeader with { Decision = "combat" }, StatusKind.Combat, null, zh: true)[^1].Text);
        Assert.Equal("Advice service offline, reconnecting",
                     PanelText.Status(null, StatusKind.Offline, null, zh: false)[^1].Text);
        Assert.Equal("读取失败：NEventRoom",
                     PanelText.Status(null, StatusKind.ReadFailed, "NEventRoom", zh: true)[^1].Text);
    }
}
