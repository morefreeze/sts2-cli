using Sts2Advisor.Core;

/// <summary>The panel's own Chinese uses the game's words: every game term in a Core zh string is looked up in the
/// game's zhs tables, never guessed (CLAUDE.md, Localization).</summary>
public class PanelVocabularyTests
{
    // Each term is the game's own wording (table.key it comes from); the panel text must use it.
    [Theory]
    [InlineData("楼层")]     // static_hover_tips FLOOR.title
    [InlineData("生命值")]   // main_menu_ui HP_AMOUNT
    [InlineData("金币")]     // static_hover_tips MONEY_POUCH.title
    [InlineData("阶段")]     // gameplay_ui ACT_NUMBER
    [InlineData("休息处")]   // static_hover_tips ROOM_REST.title
    [InlineData("锻造")]     // rest_site_ui OPTION_SMITH.name
    [InlineData("商店")]     // static_hover_tips ROOM_MERCHANT.title
    [InlineData("精英")]     // static_hover_tips ROOM_ELITE.title
    [InlineData("事件")]     // static_hover_tips ROOM_EVENT.title
    [InlineData("地图")]     // map LEGEND_MAP.hoverTip.title
    [InlineData("卡牌奖励")] // static_hover_tips CARD_REWARD.title
    [InlineData("多人模式")] // main_menu_ui MULTIPLAYER
    [InlineData("选择一组")] // gameplay_ui CHOOSE_A_PACK (a bundle is a "组")
    [InlineData("药水栏位")] // static_hover_tips POTION_SLOT.title
    [InlineData("卡牌移除服务")] // merchant_room MERCHANT.cardRemovalService.title
    [InlineData("涅奥的苦痛")]   // relics NEOWS_TORMENT.title
    [InlineData("墨影幻灵")]     // monsters VANTOM.name
    [InlineData("无色牌")]   // card_library POOL_COLORLESS_TIP
    [InlineData("稀有牌")]   // relics ARCANE_SCROLL.description
    [InlineData("铁甲战士")] // characters IRONCLAD.title
    [InlineData("诅咒")]
    [InlineData("跳过")]
    [InlineData("离开")]
    [InlineData("能量")]
    [InlineData("辉星")]
    [InlineData("格挡")]
    [InlineData("遗物")]     // event.kw.relic / relics
    [InlineData("药水")]     // event.kw.potion
    [InlineData("最大生命值")] // event.kw.lose_max_hp / gain_max_hp
    [InlineData("力量")]     // event.kw.gain_strength
    [InlineData("敏捷")]     // event.kw.gain_dexterity
    [InlineData("升级")]     // event.kw.upgrade
    [InlineData("变化")]     // event.kw.transform
    [InlineData("移除")]     // event.kw.remove_card
    [InlineData("回复")]     // event.kw.heal
    [InlineData("每回合")]   // event.kw.energy_each_turn
    public void TheTermIsInTheGamesOwnChineseText(string term)
    {
        if (RepoLocalization.Dir == null)
            return;
        Assert.Contains(term, RepoLocalization.ZhCorpus);
    }

    [Fact]
    public void NoTemplateUsesTheOldInventedOrEnglishTerms()
    {
        foreach (var (key, t) in TraceText.Templates)
        {
            Assert.DoesNotContain("HP", t.Zh);         // 生命值
            Assert.DoesNotContain("联机", t.Zh);       // 多人模式
            Assert.DoesNotContain("层", t.Zh.Replace("楼层", "").Replace("层易伤", ""));  // 楼层 (the game's "N层" is a stack count)
            Assert.DoesNotContain("卡包", t.Zh);       // 组
            Assert.DoesNotContain("稀有卡", t.Zh);
            Assert.DoesNotContain("罕见卡", t.Zh);
            Assert.DoesNotContain("无色卡", t.Zh);
        }
        foreach (var d in new[] { "card_reward", "rest_site", "event_choice", "shop", "bundle_select", "card_select", "map_select", "combat", "multiplayer" })
        {
            string name = PanelText.DecisionName(d, zh: true);
            Assert.DoesNotContain("联机", name);
            Assert.DoesNotContain("卡包", name);
        }
        Assert.Equal("多人模式", PanelText.DecisionName("multiplayer", zh: true));
        Assert.Equal("选择一组", PanelText.DecisionName("bundle_select", zh: true));
        Assert.Contains("药水栏位", AutoPilotText.PotionSwapReason(1, 2, 0.5, zh: true));
        Assert.Contains("多人模式", AutoPilotText.StopReasonText(StopReason.Multiplayer, zh: true));
    }

    [Fact]
    public void EveryTermOfTheTemplatesThatNamesAGameThingIsTheGamesOwn()
    {
        if (RepoLocalization.Dir == null)
            return;
        // Words in the rule templates that name game things; each must occur in the game's zhs text.
        string[] gameThings = { "涅奥的苦痛", "墨影幻灵", "诅咒", "遗物", "药水", "金币", "最大生命值", "格挡", "力量", "敏捷", "升级", "变化", "锻造", "休息", "牌组", "打击", "防御", "能量" };
        var all = string.Join("\n", TraceText.Templates.Values.Select(t => t.Zh));
        foreach (var term in gameThings.Where(all.Contains))
            Assert.Contains(term, RepoLocalization.ZhCorpus);
    }
}
