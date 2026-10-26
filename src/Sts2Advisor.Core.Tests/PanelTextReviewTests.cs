using System.Text.Json;
using System.Text.RegularExpressions;
using Sts2Advisor.Core;

/// <summary>Display defects found by rendering all 1696 real advisor replies through PanelText.</summary>
public class PanelTextReviewTests
{
    private static Dictionary<string, object?> Opt(string kind, int index, double? score, string status = "ok",
                                                   params (string Key, double Value)[] comps) => new()
    {
        ["kind"] = kind, ["index"] = index, ["score"] = score, ["status"] = status,
        ["components"] = comps.Select(c => new Dictionary<string, object?> { ["key"] = c.Key, ["value"] = c.Value }).ToList(),
    };

    private static Dictionary<string, object?> RuleObj(string key, string outcome, object? values = null) => new()
    {
        ["key"] = key, ["outcome"] = outcome, ["values"] = values ?? new Dictionary<string, object?>(),
    };

    private static AdviceReply Reply(object? action, IEnumerable<object> options, IEnumerable<object> rules, bool unverified = false) =>
        AdviceReply.Parse(JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["action"] = action, ["options"] = options, ["rules"] = rules,
            ["warnings"] = Array.Empty<string>(), ["unverified"] = unverified, ["policy"] = "greedy",
        }));

    private static object SelectCards(string indices) =>
        new { cmd = "action", action = "select_cards", args = new { indices } };

    private static TraceRule ParsedRule(string key, string outcome, object values)
    {
        var reply = Reply(null, Array.Empty<object>(), new object[] { RuleObj(key, outcome, values) });
        return reply.Rules[0];
    }

    private static Dictionary<string, string> Labels(string prefix, int n, string name = "C") =>
        Enumerable.Range(0, n).ToDictionary(i => $"{prefix}:{i}", i => $"{name}{i}");

    private static List<string> Texts(IEnumerable<PanelLine> lines) => lines.Select(l => l.Text).ToList();

    // ---- 1. removal / transform panels list the lowest cards -------------------------------------------------

    [Fact]
    public void LowestScoresPanelSortsAscendingAndHighlightsThePickedCards()
    {
        var options = Enumerable.Range(0, 8).Select(i => (object)Opt("card", i, 8 - i)).ToList();
        var rules = new object[]
        {
            RuleObj("card_select.lowest_scores", "fired",
                new { has_junk = true, picked = new[] { 7, 6 }, room_type = "Shop", n_cards = 8, max_select = 2 }),
        };
        var lines = PanelText.Render(new DecisionHeader("card_select", 1, 5, 1, 60, 80, 99),
                                     Reply(SelectCards("7,6"), options, rules), Labels("card", 8), zh: true);
        Assert.Equal("建议：C7、C6", lines[1].Text);
        var cardLines = lines.Where(l => Regex.IsMatch(l.Text, @"^C\d  ")).ToList();
        Assert.Equal(new[]
        {
            new PanelLine("C7  1.0", LineStyle.Fired),
            new PanelLine("C6  2.0", LineStyle.Fired),
            new PanelLine("C5  3.0", LineStyle.Normal),
        }, cardLines);
    }

    [Fact]
    public void EveryPickedOptionIsShownEvenBeyondTheCap()
    {
        var options = Enumerable.Range(0, 8).Select(i => (object)Opt("card", i, 8 - i)).ToList();
        var rules = new object[]
        {
            RuleObj("card_select.top_scores", "fired", new { has_junk = false, picked = new[] { 0, 7 }, n_cards = 8, max_select = 2 }),
        };
        var lines = PanelText.Render(new DecisionHeader("card_select", 1, 5, 1, 60, 80, 99),
                                     Reply(SelectCards("0,7"), options, rules), Labels("card", 8), zh: true);
        // top_scores keeps the descending order; the lowest card is picked, so it is shown after the top three.
        Assert.Equal(new[]
        {
            new PanelLine("C0  8.0", LineStyle.Fired),
            new PanelLine("C1  7.0", LineStyle.Normal),
            new PanelLine("C2  6.0", LineStyle.Normal),
            new PanelLine("C7  1.0", LineStyle.Fired),
        }, lines.Where(l => Regex.IsMatch(l.Text, @"^C\d  ")).ToArray());
    }

    // ---- 2. the cap is per kind -----------------------------------------------------------------------------

    [Fact]
    public void ShopCapsEachKindSeparatelyAndAlwaysShowsThePickedRelic()
    {
        var options = new List<object>();
        for (int i = 0; i < 5; i++) options.Add(Opt("card", i, 9 - i, "ok", ("card.in_deck_score", 9 - i)));
        for (int i = 0; i < 4; i++) options.Add(Opt("relic", i, 6 - i, "ok", ("shop.relic_score", 6 - i)));
        for (int i = 0; i < 2; i++) options.Add(Opt("potion", i, 5 - i, "ok", ("shop.potion_score", 5 - i)));
        var action = new { cmd = "action", action = "buy_relic", args = new { relic_index = 3 } };
        var labels = Labels("card", 5, "Card").Concat(Labels("relic", 4, "Relic")).Concat(Labels("potion", 2, "Potion"))
            .ToDictionary(kv => kv.Key, kv => kv.Value);
        var lines = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 400),
                                     Reply(action, options, new object[] { RuleObj("shop.relic", "fired", new { threshold = 4.0, gold_reserve = 25, n_affordable = 4, best_relic_score = 6.0 }) }),
                                     labels, zh: true);
        var optionLines = lines.Where(l => Regex.IsMatch(l.Text, @"^(Card|Relic|Potion)\d  ")).ToList();
        Assert.Equal(new[]
        {
            new PanelLine("Card0  9.0", LineStyle.Normal), new PanelLine("Card1  8.0", LineStyle.Normal), new PanelLine("Card2  7.0", LineStyle.Normal),
            new PanelLine("Relic0  6.0", LineStyle.Normal), new PanelLine("Relic1  5.0", LineStyle.Normal), new PanelLine("Relic2  4.0", LineStyle.Normal),
            new PanelLine("Relic3  3.0", LineStyle.Fired),
            new PanelLine("Potion0  5.0", LineStyle.Normal), new PanelLine("Potion1  4.0", LineStyle.Normal),
        }, optionLines);
    }

    [Fact]
    public void ARelicOrPotionBuyIsNeverHiddenBehindFiveCards()
    {
        var options = new List<object>();
        for (int i = 0; i < 6; i++) options.Add(Opt("card", i, 3 + i));
        options.Add(Opt("potion", 0, 4.6));
        var action = new { cmd = "action", action = "buy_potion", args = new { potion_index = 0 } };
        var labels = Labels("card", 6, "Card");
        labels["potion:0"] = "Fire Potion";
        var lines = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 400),
                                     Reply(action, options, Array.Empty<object>()), labels, zh: true);
        Assert.Contains(new PanelLine("Fire Potion  4.6", LineStyle.Fired), lines);
    }

    // ---- 3. conditional rule templates must not read as false statements ------------------------------------

    [Fact]
    public void HealPotionStatesTheRequirementNextToTheCurrentValues()
    {
        var r = ParsedRule("shop.heal_potion", "skipped", new { hp_ratio = 1.0, held_potions = 2, slots_free = true, available = false });
        Assert.Equal("回复药水：需 HP < 50%（现 100%）且持有药水 < 3（现 2/3）", TraceText.Rule(r, zh: true));
        Assert.Equal("Heal potion: needs HP < 50% (now 100%) and potions < 3 (now 2/3)", TraceText.Rule(r, zh: false));
    }

    [Fact]
    public void NoTemplateComparesAPlaceholderWithALiteral()
    {
        // "HP {hp_ratio:%} < 50%" is false whenever the rule was skipped. A requirement is worded as
        // "needs X < 50% (now {hp_ratio:%})" instead. Placeholder-vs-placeholder text belongs to fired-only rules.
        var comparison = new Regex(@"\}\s*(<|>|≥|≤|>=|<=)\s*\d");
        foreach (var (key, t) in TraceText.Templates)
        {
            Assert.False(comparison.IsMatch(t.Zh), $"{key} (zh): {t.Zh}");
            Assert.False(comparison.IsMatch(t.En), $"{key} (en): {t.En}");
        }
    }

    [Fact]
    public void RestDefaultSmithIsAConditionNotAnAssertion()
    {
        var r = ParsedRule("rest.default_smith", "skipped", new { });
        Assert.Equal("默认：能锻造就锻造", TraceText.Rule(r, zh: true));
        Assert.Equal("Default: Smith if available", TraceText.Rule(r, zh: false));
    }

    // ---- 4. rest.fallback -----------------------------------------------------------------------------------

    [Fact]
    public void RestFallbackDescribesTheRealLogic()
    {
        var r = ParsedRule("rest.fallback", "fired", new { });
        Assert.Equal("没有适用的规则：能休息就休息，否则选第一个可用项", TraceText.Rule(r, zh: true));
        Assert.Equal("No rule applied: Rest if possible, else the first available option", TraceText.Rule(r, zh: false));
    }

    // ---- 5. rest.pre_boss_heal states its condition ---------------------------------------------------------

    [Theory]
    [InlineData(14)]
    [InlineData(15)]
    [InlineData(16)]
    public void PreBossHealShowsItsNumbersOnceTheFloorIsReached(int floor)
    {
        var r = ParsedRule("rest.pre_boss_heal", "skipped", new { hp_ratio = 1.0, threshold = 0.95, floor, available = true });
        Assert.Equal("第 14 层起（Boss 前）：HP 100%，休息阈值 95%", TraceText.Rule(r, zh: true));
        Assert.Equal("Floor 14+ (pre-boss): HP 100%, rest threshold 95%", TraceText.Rule(r, zh: false));
    }

    [Theory]
    [InlineData(1)]
    [InlineData(7)]
    [InlineData(13)]
    public void PreBossHealBelowFloor14ShowsNoNumbers(int floor)
    {
        // Below floor 14 the recorded threshold is the current floor's, not the pre-boss one.
        var r = ParsedRule("rest.pre_boss_heal", "skipped", new { hp_ratio = 0.8125, threshold = 0.8, floor, available = true });
        Assert.Equal("第 14 层起（Boss 前）休息", TraceText.Rule(r, zh: true));
        Assert.Equal("Floor 14+ (pre-boss): rest", TraceText.Rule(r, zh: false));

        var missingOption = ParsedRule("rest.pre_boss_heal", "skipped", new { hp_ratio = 0.8125, threshold = 0.8, floor, available = false });
        Assert.Equal("第 14 层起（Boss 前）休息（没有该选项）", TraceText.Rule(missingOption, zh: true));
    }

    [Fact]
    public void PreBossHealWithoutAFloorKeepsTheFullLine()
    {
        var r = ParsedRule("rest.pre_boss_heal", "skipped", new { hp_ratio = 1.0, threshold = 0.95 });
        Assert.Equal("第 14 层起（Boss 前）：HP 100%，休息阈值 95%", TraceText.Rule(r, zh: true));
    }

    // ---- 6. removal rules say why they were skipped when gold is short --------------------------------------

    [Theory]
    [InlineData("shop.remove_basic")]
    [InlineData("shop.remove_value")]
    public void RemovalRulesSayWhenGoldIsShort(string key)
    {
        var short_ = ParsedRule(key, "skipped",
            new { n_basic = 5, basic_scope = "ironclad_only", removal_value = 6.0, best_card_score = 2.0, removal_cost = 75, gold = 60 });
        Assert.EndsWith("（需 75 金币，现 60）", TraceText.Rule(short_, zh: true));
        Assert.EndsWith(" (needs 75 gold, have 60)", TraceText.Rule(short_, zh: false));

        foreach (var values in new object[]
        {
            new { n_basic = 5, removal_value = 6.0, best_card_score = 2.0, removal_cost = 75, gold = 75 },
            new { n_basic = 5, removal_value = 6.0, best_card_score = 2.0, removal_cost = 75, gold = 400 },
            new { n_basic = 5, removal_value = 6.0, best_card_score = 2.0, removal_cost = (int?)null, gold = 60 },
            new { n_basic = 5, removal_value = 6.0, best_card_score = 2.0, gold = 60 },
        })
        {
            var enough = ParsedRule(key, "skipped", values);
            Assert.DoesNotContain("需 ", TraceText.Rule(enough, zh: true).Replace("需要 ≥ 3", ""));
            Assert.DoesNotContain("needs 7", TraceText.Rule(enough, zh: false));
        }
    }

    [Theory]
    [InlineData(3, true)]
    [InlineData(5, true)]
    [InlineData(2, false)]
    [InlineData(0, false)]
    public void RemoveBasicBlamesGoldOnlyWhenEnoughBasicCardsWereHeld(int nBasic, bool blamesGold)
    {
        // Below 3 basic cards the rule was skipped for that reason, whatever the gold.
        var r = ParsedRule("shop.remove_basic", "skipped",
            new { n_basic = nBasic, basic_scope = "ironclad_only", removal_cost = 250, gold = 141 });
        Assert.Equal(blamesGold, TraceText.Rule(r, zh: true).EndsWith("（需 250 金币，现 141）"));
        Assert.Equal(blamesGold, TraceText.Rule(r, zh: false).EndsWith(" (needs 250 gold, have 141)"));
    }

    [Fact]
    public void RemoveBasicWithoutACountDoesNotBlameGold()
    {
        var r = ParsedRule("shop.remove_basic", "skipped", new { removal_cost = 250, gold = 141 });
        Assert.DoesNotContain("需 250", TraceText.Rule(r, zh: true));
        var nullCount = ParsedRule("shop.remove_basic", "skipped", new { n_basic = (int?)null, removal_cost = 250, gold = 141 });
        Assert.DoesNotContain("需 250", TraceText.Rule(nullCount, zh: true));
    }

    [Fact]
    public void RemoveValueStillBlamesShortGoldWhateverTheBasicCount()
    {
        var r = ParsedRule("shop.remove_value", "skipped", new { removal_value = 2.2, best_card_score = 2.97, removal_cost = 250, gold = 141 });
        Assert.EndsWith("（需 250 金币，现 141）", TraceText.Rule(r, zh: true));
    }

    // ---- 7. one number format for scores ---------------------------------------------------------------------

    [Fact]
    public void ScoresShareOneFormatBetweenOptionsAndRules()
    {
        var options = new object[] { Opt("card", 0, 5.45, "ok", ("card.base", 5.45)) };
        var rules = new object[]
        {
            RuleObj("card_reward.below_threshold", "fired", new { best_score = 5.45, threshold = 5.5, act = 1, deck_size = 14 }),
        };
        var lines = PanelText.Render(new DecisionHeader("card_reward", 1, 4, 1, 70, 80, 50),
                                     Reply(new { cmd = "action", action = "skip_card_reward" }, options, rules),
                                     new Dictionary<string, string> { ["card:0"] = "X" }, zh: true);
        Assert.Contains(new PanelLine("X  5.45", LineStyle.Normal), lines);
        Assert.Contains(new PanelLine("最高分 5.45 < 跳过阈值 5.5（阶段1，牌组 14 张）", LineStyle.Fired), lines);
        Assert.Contains(new PanelLine("  卡牌分 5.45", LineStyle.Muted), lines);
    }

    [Fact]
    public void NumberFormatKeepsOneDecimalForIntegralScoresAndTwoOtherwise()
    {
        Assert.Equal("5.0", TraceText.Num(5));
        Assert.Equal("5.5", TraceText.Num(5.5));
        Assert.Equal("5.45", TraceText.Num(5.45));
        Assert.Equal("0.0", TraceText.Num(-0.0004));
        Assert.Equal("+0.05", TraceText.Signed(0.05));
        Assert.Equal("-1.9", TraceText.Signed(-1.9));
        Assert.Equal("+0.0", TraceText.Signed(-0.0004));
        // Counts stay plain integers; score-like values use the score format.
        var r = ParsedRule("shop.relic", "skipped", new { threshold = 4, gold_reserve = 25, n_affordable = 2, best_relic_score = 3.95 });
        Assert.Equal("最佳遗物 3.95，阈值 4.0，至少留 25 金币", TraceText.Rule(r, zh: true));
    }

    // ---- 8. percentages never round across a threshold --------------------------------------------------------

    [Theory]
    [InlineData(0.4999, "49.9%")]
    [InlineData(0.5, "50%")]
    [InlineData(0.5125, "51.2%")]
    [InlineData(0.85, "85%")]
    [InlineData(0.57, "57%")]
    [InlineData(0.7, "70%")]
    [InlineData(0.3, "30%")]
    [InlineData(0.9999, "99.9%")]
    [InlineData(1.0, "100%")]
    public void PercentagesAreFlooredToOneDecimal(double ratio, string expected)
    {
        Assert.Equal(expected, TraceText.Percent(ratio));
    }

    [Fact]
    public void AHpJustUnderTheThresholdNeverReadsAsTheThreshold()
    {
        var r = ParsedRule("rest.critical_heal", "skipped", new { hp_ratio = 0.4999, threshold = 0.5, floor = 5, available = true });
        Assert.Equal("HP 49.9%，必须休息线 50%", TraceText.Rule(r, zh: true));
    }

    // ---- 9. official zh names ---------------------------------------------------------------------------------

    [Fact]
    public void OfficialNamesReplaceTheInventedOnes()
    {
        Assert.Equal("涅奥的苦痛", TraceText.Label("event.kw.torment", zh: true));
        Assert.Equal("Neow's Torment", TraceText.Label("event.kw.torment", zh: false));
        Assert.Equal("移除卡牌", TraceText.Label("event.kw.remove_card", zh: true));
        Assert.Equal("移除卡牌要失去生命", TraceText.Label("event.kw.remove_card_hp_cost", zh: true));
        Assert.Contains("墨影幻灵", TraceText.Rule(ParsedRule("rest.vantom_mid_boss", "skipped",
            new { enabled = true, is_vantom = true, hp_ratio = 0.8, floor = 7, available = true }), zh: true));
        var removal = Reply(new { cmd = "action", action = "remove_card" }, Array.Empty<object>(), Array.Empty<object>());
        var header = new DecisionHeader("shop", 1, 6, 1, 70, 80, 400);
        Assert.Equal("建议：移除卡牌", PanelText.Render(header, removal, new Dictionary<string, string>(), zh: true)[1].Text);
        Assert.Equal("Pick: Remove a card", PanelText.Render(header, removal, new Dictionary<string, string>(), zh: false)[1].Text);
    }

    [Fact]
    public void ActsAreWrittenTheOfficialWayAndNoInventedTermsRemain()
    {
        foreach (var (key, t) in TraceText.Templates)
        {
            Assert.DoesNotContain("幕", t.Zh);   // 阶段{actNumber} is the official act wording
            Assert.DoesNotContain("删牌", t.Zh);
            Assert.DoesNotContain("删基础牌", t.Zh);
            Assert.DoesNotContain("Vantom", t.Zh);
        }
        var r = ParsedRule("shop.card_threshold", "skipped", new { best_card_score = 3.0, threshold = 4.5, act = 2, pre_boss = false });
        Assert.Equal("最佳卡 3.0，买卡阈值 4.5（阶段2）", TraceText.Rule(r, zh: true));
        Assert.Equal("Best card 3.0, buy threshold 4.5 (act 2)", TraceText.Rule(r, zh: false));
        var lines = PanelText.Render(new DecisionHeader("card_reward", 1, 14, 1, 70, 80, 50),
                                     Reply(new { cmd = "action", action = "skip_card_reward" }, new object[] { Opt("card", 0, null, "filtered") }, Array.Empty<object>()),
                                     new Dictionary<string, string> { ["card:0"] = "X" }, zh: true);
        Assert.Contains(new PanelLine("已过滤（阶段1后期质量门）：X", LineStyle.Muted), lines);
    }

    // ---- 10. minor display fixes ------------------------------------------------------------------------------

    [Fact]
    public void TheVantomRuleIsHiddenWhenItsSwitchIsOff()
    {
        object Rules(bool enabled) => new[]
        {
            RuleObj("rest.critical_heal", "skipped", new { hp_ratio = 1.0, threshold = 0.3, floor = 7, available = true }),
            RuleObj("rest.vantom_mid_boss", "skipped", new { enabled, is_vantom = true, hp_ratio = 1.0, floor = 7, available = true }),
        };
        var header = new DecisionHeader("rest_site", 1, 7, 1, 80, 80, 99);
        var off = PanelText.Render(header, Reply(new { cmd = "action", action = "proceed" }, Array.Empty<object>(), (IEnumerable<object>)Rules(false)),
                                   new Dictionary<string, string>(), zh: true);
        Assert.DoesNotContain(off, l => l.Text.Contains("墨影幻灵"));
        Assert.Contains(off, l => l.Text.StartsWith("HP 100%，必须休息线"));
        var on = PanelText.Render(header, Reply(new { cmd = "action", action = "proceed" }, Array.Empty<object>(), (IEnumerable<object>)Rules(true)),
                                  new Dictionary<string, string>(), zh: true);
        Assert.Contains(on, l => l.Text.Contains("墨影幻灵"));
    }

    [Theory]
    [InlineData(true, true, false)]    // switch on and a Vantom boss: shown
    [InlineData(false, true, true)]    // switch off: hidden
    [InlineData(true, false, true)]    // switch on but the next boss is not Vantom: hidden
    [InlineData(false, false, true)]
    public void TheVantomRuleShowsOnlyWhenEnabledAndTheBossIsVantom(bool enabled, bool isVantom, bool hidden)
    {
        var rules = new object[]
        {
            RuleObj("rest.critical_heal", "skipped", new { hp_ratio = 1.0, threshold = 0.3, floor = 12, available = true }),
            RuleObj("rest.vantom_mid_boss", "skipped", new { enabled, is_vantom = isVantom, hp_ratio = 1.0, floor = 12, available = true }),
        };
        var lines = PanelText.Render(new DecisionHeader("rest_site", 1, 12, 1, 80, 80, 99),
                                     Reply(new { cmd = "action", action = "proceed" }, Array.Empty<object>(), rules),
                                     new Dictionary<string, string>(), zh: true);
        Assert.Equal(hidden, lines.All(l => !l.Text.Contains("墨影幻灵")));
        Assert.Contains(lines, l => l.Text.StartsWith("HP 100%，必须休息线"));
    }

    [Fact]
    public void TheVantomRuleStaysVisibleWhenItsFlagsAreMissing()
    {
        var rules = new object[] { RuleObj("rest.vantom_mid_boss", "skipped", new { hp_ratio = 1.0, floor = 12 }) };
        var lines = PanelText.Render(new DecisionHeader("rest_site", 1, 12, 1, 80, 80, 99),
                                     Reply(new { cmd = "action", action = "proceed" }, Array.Empty<object>(), rules),
                                     new Dictionary<string, string>(), zh: true);
        Assert.Contains(lines, l => l.Text.Contains("墨影幻灵"));
    }

    [Fact]
    public void TheCapSaysHowManyCardsItLeftOut()
    {
        var options = Enumerable.Range(0, 8).Select(i => (object)Opt("card", i, 8 - i)).ToList();
        var rules = new object[] { RuleObj("card_select.top_scores", "fired", new { has_junk = false, picked = new[] { 0 }, n_cards = 8, max_select = 1 }) };
        var reply = Reply(SelectCards("0"), options, rules);
        var header = new DecisionHeader("card_select", 1, 5, 1, 60, 80, 99);
        var zh = PanelText.Render(header, reply, Labels("card", 8), zh: true);
        Assert.Contains(new PanelLine("另有 5 张卡未列出", LineStyle.Muted), zh);
        var en = PanelText.Render(header, reply, Labels("card", 8), zh: false);
        Assert.Contains(new PanelLine("5 more cards not shown", LineStyle.Muted), en);

        // It comes right after the listed options and before the rule lines.
        var texts = Texts(zh);
        var lastOption = texts.FindLastIndex(t => Regex.IsMatch(t, @"^C\d  "));
        Assert.Equal("另有 5 张卡未列出", texts[lastOption + 1]);
        Assert.Equal(1, zh.Count(l => l.Text.StartsWith("另有")));
    }

    [Fact]
    public void APickedOptionBeyondTheCapIsNotCountedAsHidden()
    {
        var options = Enumerable.Range(0, 8).Select(i => (object)Opt("card", i, 8 - i)).ToList();
        var rules = new object[] { RuleObj("card_select.top_scores", "fired", new { has_junk = false, picked = new[] { 0, 7 }, n_cards = 8, max_select = 2 }) };
        var lines = PanelText.Render(new DecisionHeader("card_select", 1, 5, 1, 60, 80, 99),
                                     Reply(SelectCards("0,7"), options, rules), Labels("card", 8), zh: true);
        Assert.Contains(new PanelLine("另有 4 张卡未列出", LineStyle.Muted), lines);   // 8 scored, 4 listed
    }

    [Fact]
    public void TheCapCountsEachKindSeparately()
    {
        var options = new List<object>();
        for (int i = 0; i < 4; i++) options.Add(Opt("card", i, 9 - i));
        for (int i = 0; i < 5; i++) options.Add(Opt("relic", i, 7 - i));
        for (int i = 0; i < 4; i++) options.Add(Opt("potion", i, 6 - i));
        var action = new { cmd = "action", action = "buy_card", args = new { card_index = 0 } };
        var labels = Labels("card", 4, "Card").Concat(Labels("relic", 5, "Relic")).Concat(Labels("potion", 4, "Potion"))
            .ToDictionary(kv => kv.Key, kv => kv.Value);
        var header = new DecisionHeader("shop", 1, 6, 1, 70, 80, 400);
        var zh = PanelText.Render(header, Reply(action, options, Array.Empty<object>()), labels, zh: true);
        Assert.Contains(new PanelLine("另有 1 张卡未列出", LineStyle.Muted), zh);
        Assert.Contains(new PanelLine("另有 2 件遗物未列出", LineStyle.Muted), zh);
        Assert.Contains(new PanelLine("另有 1 瓶药水未列出", LineStyle.Muted), zh);
        var en = PanelText.Render(header, Reply(action, options, Array.Empty<object>()), labels, zh: false);
        Assert.Contains(new PanelLine("1 more card not shown", LineStyle.Muted), en);
        Assert.Contains(new PanelLine("2 more relics not shown", LineStyle.Muted), en);
        Assert.Contains(new PanelLine("1 more potion not shown", LineStyle.Muted), en);
        // Cards, then relics, then potions.
        var texts = Texts(zh);
        Assert.True(texts.IndexOf("另有 1 张卡未列出") < texts.IndexOf("另有 2 件遗物未列出"));
        Assert.True(texts.IndexOf("另有 2 件遗物未列出") < texts.IndexOf("另有 1 瓶药水未列出"));
    }

    [Fact]
    public void OtherKindsGetAGenericCount()
    {
        var options = Enumerable.Range(0, 5).Select(i => (object)Opt("node", i, 5 + i)).ToList();
        var action = new { cmd = "action", action = "select_map_node", args = new { col = 1, row = 1 } };
        var header = new DecisionHeader("map_select", 1, 1, 1, 80, 80, 99);
        var reply = Reply(action, options, Array.Empty<object>());
        Assert.Contains(new PanelLine("另有 2 个未列出", LineStyle.Muted), PanelText.Render(header, reply, new Dictionary<string, string>(), zh: true));
        Assert.Contains(new PanelLine("2 more not shown", LineStyle.Muted), PanelText.Render(header, reply, new Dictionary<string, string>(), zh: false));
    }

    [Fact]
    public void NothingIsSaidWhenNothingWasLeftOutOrOnlyUnavailableOptionsAreMissing()
    {
        var options = new List<object> { Opt("card", 0, 7.0), Opt("card", 1, 6.0), Opt("card", 2, 5.0) };
        for (int i = 3; i < 8; i++) options.Add(Opt("card", i, null, "unaffordable"));
        var action = new { cmd = "action", action = "buy_card", args = new { card_index = 0 } };
        var lines = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 400),
                                     Reply(action, options, Array.Empty<object>()), Labels("card", 8), zh: true);
        Assert.DoesNotContain(lines, l => l.Text.StartsWith("另有"));
        Assert.Contains(lines, l => l.Text.StartsWith("买不起："));
    }

    [Fact]
    public void UnavailableOptionsFoldIntoOneLinePerStatus()
    {
        var options = new object[]
        {
            Opt("card", 0, 7.0), Opt("card", 1, null, "unaffordable"), Opt("card", 2, null, "unstocked"),
            Opt("card", 3, null, "unaffordable"), Opt("card", 4, null, "unaffordable"),
        };
        var labels = Labels("card", 5, "C");
        var action = new { cmd = "action", action = "buy_card", args = new { card_index = 0 } };
        var zh = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 400), Reply(action, options, Array.Empty<object>()), labels, zh: true);
        Assert.Contains(new PanelLine("买不起：C1、C3、C4", LineStyle.Muted), zh);
        Assert.Contains(new PanelLine("已售出：C2", LineStyle.Muted), zh);
        Assert.Equal(1, zh.Count(l => l.Text.StartsWith("买不起")));
        var en = PanelText.Render(new DecisionHeader("shop", 1, 6, 1, 70, 80, 400), Reply(action, options, Array.Empty<object>()), labels, zh: false);
        Assert.Contains(new PanelLine("can't afford: C1, C3, C4", LineStyle.Muted), en);
    }

    [Fact]
    public void HiddenComponentsAreMarkedWithAnEllipsis()
    {
        var many = Opt("card", 0, 9.0, "ok", ("card.base", 5.0), ("card.set_bonus", 1.0), ("card.tag_synergy", 1.5),
                       ("card.combat_type", 2.0), ("card.q_adjust", 0.5));
        var fits = Opt("card", 1, 8.0, "ok", ("card.base", 5.0), ("card.set_bonus", 1.0), ("card.tag_synergy", 1.5), ("card.archetype", 0.0));
        var lines = PanelText.Render(new DecisionHeader("card_reward", 1, 4, 1, 70, 80, 50),
                                     Reply(new { cmd = "action", action = "select_card_reward", args = new { card_index = 0 } }, new object[] { many, fits }, Array.Empty<object>()),
                                     Labels("card", 2), zh: true);
        Assert.Contains(new PanelLine("  卡牌分 5.0 · 战斗类型 +2.0 · 标签协同 +1.5 …", LineStyle.Muted), lines);
        Assert.Contains(new PanelLine("  卡牌分 5.0 · 标签协同 +1.5 · 组合预测 +1.0", LineStyle.Muted), lines);
    }

    [Fact]
    public void HiddenEventKeywordsAreMarkedToo()
    {
        var option = Opt("option", 0, 3.0, "ok", ("event.kw.gain_gold", 2.0), ("event.kw.heal", 1.0), ("event.kw.potion", 0.5), ("event.kw.relic", 0.25));
        var lines = PanelText.Render(new DecisionHeader("event_choice", 1, 4, 1, 70, 80, 50),
                                     Reply(new { cmd = "action", action = "choose_option", args = new { option_index = 0 } }, new object[] { option }, Array.Empty<object>()),
                                     new Dictionary<string, string> { ["option:0"] = "O" }, zh: false);
        Assert.Contains(new PanelLine("  Keywords: Gain gold +2.0 · Heal +1.0 · Potion +0.5 …", LineStyle.Muted), lines);
    }

    [Fact]
    public void ShopPotionRuleSaysHeldPotions()
    {
        var r = ParsedRule("shop.potion", "skipped", new { threshold = 4.5, held_potions = 3, best_potion_score = 4.0 });
        Assert.Equal("最佳药水 4.0，阈值 4.5，持有药水 3/3", TraceText.Rule(r, zh: true));
        Assert.Equal("Best potion 4.0, threshold 4.5, held potions 3/3", TraceText.Rule(r, zh: false));
    }
}
