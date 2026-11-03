using Sts2Advisor.Core;

public class PanelLanguageTests
{
    [Theory]
    [InlineData("auto", PanelLanguage.Auto)]
    [InlineData("AUTO", PanelLanguage.Auto)]
    [InlineData("zh", PanelLanguage.Zh)]
    [InlineData("ZH", PanelLanguage.Zh)]
    [InlineData(" zh-CN ", PanelLanguage.Zh)]
    [InlineData("zhs", PanelLanguage.Zh)]
    [InlineData("Chinese", PanelLanguage.Zh)]
    [InlineData("中文", PanelLanguage.Zh)]
    [InlineData("en", PanelLanguage.En)]
    [InlineData("En", PanelLanguage.En)]
    [InlineData("eng", PanelLanguage.En)]
    [InlineData("English", PanelLanguage.En)]
    [InlineData("", PanelLanguage.Auto)]
    [InlineData("   ", PanelLanguage.Auto)]
    [InlineData("klingon", PanelLanguage.Auto)]
    [InlineData("fr", PanelLanguage.Auto)]
    [InlineData(null, PanelLanguage.Auto)]
    public void ParseIsLenientAndFallsBackToAuto(string? raw, PanelLanguage expected) =>
        Assert.Equal(expected, PanelLanguageRules.Parse(raw));

    [Theory]
    [InlineData(PanelLanguage.Auto, "eng", false)]
    [InlineData(PanelLanguage.Auto, "zhs", true)]
    [InlineData(PanelLanguage.Auto, "zht", true)]
    [InlineData(PanelLanguage.Auto, "fra", false)]
    [InlineData(PanelLanguage.Auto, null, false)]
    [InlineData(PanelLanguage.Zh, "eng", true)]
    [InlineData(PanelLanguage.Zh, "fra", true)]
    [InlineData(PanelLanguage.Zh, null, true)]
    [InlineData(PanelLanguage.Zh, "zhs", true)]
    [InlineData(PanelLanguage.En, "zhs", false)]
    [InlineData(PanelLanguage.En, "eng", false)]
    public void TheOwnTextFollowsTheSettingThenTheGame(PanelLanguage setting, string? game, bool zh) =>
        Assert.Equal(zh, PanelLanguageRules.UsesChinese(setting, game));

    [Theory]
    // auto never swaps: the game's own text, whatever it is
    [InlineData(PanelLanguage.Auto, "eng", null)]
    [InlineData(PanelLanguage.Auto, "zhs", null)]
    // zh over a non-Chinese game reads the official Simplified Chinese; over a Chinese game it leaves the game's own
    [InlineData(PanelLanguage.Zh, "eng", "zhs")]
    [InlineData(PanelLanguage.Zh, "fra", "zhs")]
    [InlineData(PanelLanguage.Zh, null, "zhs")]
    [InlineData(PanelLanguage.Zh, "zhs", null)]
    [InlineData(PanelLanguage.Zh, "zht", null)]
    [InlineData(PanelLanguage.En, "eng", null)]
    [InlineData(PanelLanguage.En, "zhs", "eng")]
    [InlineData(PanelLanguage.En, "fra", "eng")]
    public void NamesAreSwappedOnlyWhenTheGameSpeaksAnotherLanguage(PanelLanguage setting, string? game, string? swapTo) =>
        Assert.Equal(swapTo, PanelLanguageRules.NamesLanguage(setting, game));

    [Theory]
    [InlineData(PanelLanguage.Auto, "eng", "eng")]
    [InlineData(PanelLanguage.Auto, "zhs", "zhs")]
    [InlineData(PanelLanguage.Zh, "eng", "zhs")]
    [InlineData(PanelLanguage.Zh, "zht", "zht")]
    [InlineData(PanelLanguage.En, "zhs", "zhs")]
    public void ChineseGlyphsAreNeededOnlyForAChinesePanel(PanelLanguage setting, string? game, string? font) =>
        Assert.Equal(font, PanelLanguageRules.FontLanguage(setting, game));

    [Fact]
    public void AChineseSettingGivesTheChineseHeaderOverAnEnglishGame()
    {
        // A Chinese panel over an English game: the header, a stop banner and the buttons carry no English UI text.
        var header = new DecisionHeader("card_reward", 1, 13, 1, 52, 80, 99);
        Assert.Equal("卡牌奖励 · A1F13a1 · 生命值 52/80 · 金币 99", PanelText.Header(header, PanelLanguageRules.UsesChinese(PanelLanguage.Zh, "eng")));
        Assert.Equal("Card reward · A1F13a1 · HP 52/80 · Gold 99", PanelText.Header(header, PanelLanguageRules.UsesChinese(PanelLanguage.Auto, "eng")));
    }
}
