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
}
