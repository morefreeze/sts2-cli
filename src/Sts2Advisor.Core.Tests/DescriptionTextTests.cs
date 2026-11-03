// src/Sts2Advisor.Core.Tests/DescriptionTextTests.cs
using Sts2Advisor.Core;

public class DescriptionTextTests
{
    private const string Energy = "[img]res://images/packed/sprite_fonts/ironclad_energy_icon.png[/img]";
    private const string Star = "[img]res://images/packed/sprite_fonts/star_icon.png[/img]";

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   \n ")]
    public void NothingGivesAnEmptyString(string? raw) => Assert.Equal("", DescriptionText.Plain(raw, zh: false));

    [Fact]
    public void ColourTagsAreStrippedAndTheNumbersStay()
    {
        Assert.Equal("Deal 6 damage. Apply 2 Vulnerable.",
            DescriptionText.Plain("Deal [blue]6[/blue] damage.\nApply [gold]2[/gold] [gold]Vulnerable[/gold].", zh: false));
        Assert.Equal("造成9点伤害。", DescriptionText.Plain("造成[green]9[/green]点伤害。", zh: true));
    }

    [Fact]
    public void TagsWithArgumentsAndUnbalancedTagsAreStripped()
    {
        Assert.Equal("a b c d",
            DescriptionText.Plain("[color=#ff8800]a[/color] [font_size=40]b[/font_size] [wave amp=50.0 freq=7]c[/wave] [b]d", zh: false));
    }

    [Fact]
    public void LinesAndRunsOfWhitespaceBecomeSingleSpaces()
    {
        Assert.Equal("Deal 6 damage. Exhaust.", DescriptionText.Plain("  Deal 6\tdamage.\n\n  Exhaust.\r\n", zh: false));
    }

    [Fact]
    public void EscapedBracketsAreLiteralBrackets()
    {
        Assert.Equal("a [x] b", DescriptionText.Plain("a [lb]x[rb] [gold]b[/gold]", zh: false));
    }

    [Fact]
    public void ABracketThatIsNotATagStays()
    {
        Assert.Equal("a [ b ] 1", DescriptionText.Plain("a [ b ] 1", zh: false));
    }

    [Fact]
    public void ARunOfEnergyIconsBecomesItsCount()
    {
        Assert.Equal("Gain 2 Energy.", DescriptionText.Plain($"Gain {Energy}{Energy}.", zh: false));
        Assert.Equal("Gain 1 Energy.", DescriptionText.Plain($"Gain {Energy}.", zh: false));
        Assert.Equal("获得3能量。", DescriptionText.Plain($"获得{Energy}{Energy}{Energy}。", zh: true));
    }

    [Fact]
    public void ABigEnergyAmountIsADigitFollowedByOneIcon()
    {
        // EnergyIconsFormatter writes "5" + one icon from 4 up.
        Assert.Equal("Gain 5 Energy.", DescriptionText.Plain($"Gain 5{Energy}.", zh: false));
        Assert.Equal("获得5能量。", DescriptionText.Plain($"获得[blue]5[/blue]{Energy}。", zh: true));
    }

    [Fact]
    public void ARunOfStarIconsBecomesItsCount()
    {
        Assert.Equal("Gain 1 Star.", DescriptionText.Plain($"Gain {Star}.", zh: false));
        Assert.Equal("Gain 3 Stars.", DescriptionText.Plain($"Gain {Star}{Star}{Star}.", zh: false));
        Assert.Equal("获得2辉星。", DescriptionText.Plain($"获得{Star}{Star}。", zh: true));
    }

    [Fact]
    public void SeparateRunsAreCountedSeparately()
    {
        Assert.Equal("Pay 1 Energy or 2 Stars.", DescriptionText.Plain($"Pay {Energy} or {Star}{Star}.", zh: false));
    }

    [Fact]
    public void AnUnknownIconLeavesNoPathBehind()
    {
        string text = DescriptionText.Plain("Do [img]res://images/foo/bar.png[/img] it", zh: false);
        Assert.Equal("Do it", text);
        Assert.DoesNotContain("res://", text);
    }

    [Fact]
    public void ALongTextIsCutWithAnEllipsisWithinTheLimit()
    {
        string raw = new string('a', 141);
        string cut = DescriptionText.Plain(raw, zh: false);
        Assert.Equal(DescriptionText.DefaultMaxChars, cut.Length);
        Assert.Equal(new string('a', 139) + "…", cut);
        Assert.Equal(new string('a', 140), DescriptionText.Plain(new string('a', 140), zh: false));
        Assert.Equal("abc…", DescriptionText.Plain("abcdefgh", zh: false, maxChars: 4));
    }

    [Fact]
    public void TheCutDoesNotLeaveTrailingSpaceBeforeTheEllipsis()
    {
        Assert.Equal("ab…", DescriptionText.Plain("ab cdefgh", zh: false, maxChars: 4));
    }

    [Fact]
    public void TheCutDoesNotSplitASurrogatePair()
    {
        string cut = DescriptionText.Plain("ab😀cd", zh: false, maxChars: 4);
        Assert.Equal("ab…", cut);
        Assert.All(cut, c => Assert.False(char.IsSurrogate(c)));
    }

    [Fact]
    public void TheLengthLimitCountsTheVisibleTextNotTheTags()
    {
        string raw = "[gold]" + new string('a', 140) + "[/gold]";
        Assert.Equal(new string('a', 140), DescriptionText.Plain(raw, zh: false));
    }

    // ---- the game formats the description (its own LocString, with the language's table); Core only flattens it ----

    [Fact]
    public void ChineseCardsAsTheGameFormatsThemFlattenToPlainText()
    {
        // Bash, Defend, Offering (energy icons), an upgraded Bash: the shapes GetDescriptionForPile produces from the zhs tables.
        Assert.Equal("造成8点伤害。 给予2层易伤。",
            DescriptionText.Plain("造成[blue]8[/blue]点伤害。\n给予[blue]2[/blue]层[gold]易伤[/gold]。", zh: true));
        Assert.Equal("获得5点格挡。", DescriptionText.Plain("获得[blue]5[/blue]点[gold]格挡[/gold]。", zh: true));
        Assert.Equal("失去6点生命。 获得2能量。 抽3张牌。",
            DescriptionText.Plain($"失去[blue]6[/blue]点生命。\n获得{Energy}{Energy}。\n抽[blue]3[/blue]张牌。", zh: true));
        Assert.Equal("造成10点伤害。 给予3层易伤。",
            DescriptionText.Plain("造成[green]10[/green]点伤害。\n给予[green]3[/green]层[gold]易伤[/gold]。", zh: true));
        Assert.Equal("将这张牌的一张0能量复制品添加到你的弃牌堆。",
            DescriptionText.Plain($"将这张牌的一张0{Energy}复制品添加到你的[gold]弃牌堆[/gold]。", zh: true));
    }

    [Theory]
    [InlineData("造成{Damage:diff()}点伤害。")]
    [InlineData("Deal {Damage} damage.")]
    [InlineData("获得{Energy:energyIcons()}。")]
    [InlineData("a {InCombat:\n（造成{CalculatedDamage:diff()}点伤害）|} b")]
    public void ARawTemplateVariableNeverReachesThePanel(string raw)
    {
        Assert.True(DescriptionText.HasUnresolvedVariable(raw));
        Assert.Equal("", DescriptionText.Plain(raw, zh: true));
        Assert.Equal("", DescriptionText.Plain(raw, zh: false));
    }

    [Theory]
    [InlineData("造成[blue]8[/blue]点伤害。")]
    [InlineData("Deal 8 damage. [lb]x[rb]")]
    [InlineData("a { b } c")]
    public void FormattedTextIsNotMistakenForATemplate(string raw) => Assert.False(DescriptionText.HasUnresolvedVariable(raw));

    [Fact]
    public void EveryRealChineseCardTemplateWithAVariableIsRefusedUnformatted()
    {
        if (RepoLocalization.Dir == null)
            return;
        var zh = RepoLocalization.Table("zhs", "cards");
        var descriptions = zh.Where(kv => kv.Key.EndsWith(".description")).ToList();
        Assert.True(descriptions.Count > 500);
        foreach (var (key, raw) in descriptions)
        {
            bool hasVariable = raw.Contains('{');
            Assert.Equal(hasVariable, DescriptionText.HasUnresolvedVariable(raw));
            if (hasVariable)
                Assert.Equal("", DescriptionText.Plain(raw, zh: true));   // a failed format shows the title only
        }
    }

    [Fact]
    public void EveryOfficialChineseTitleOfACardRelicPotionExists()
    {
        if (RepoLocalization.Dir == null)
            return;
        foreach (var table in new[] { "cards", "relics", "potions" })
        {
            var eng = RepoLocalization.Table("eng", table);
            var zh = RepoLocalization.Table("zhs", table);
            foreach (var key in eng.Keys.Where(k => k.EndsWith(".title")))
                Assert.True(zh.ContainsKey(key) && zh[key].Length > 0, $"{table}.{key} has no official zhs title");
        }
    }
}
