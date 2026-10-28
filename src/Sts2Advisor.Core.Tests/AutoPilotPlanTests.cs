// src/Sts2Advisor.Core.Tests/AutoPilotPlanTests.cs
using Sts2Advisor.Core;

public class AutoPilotPlanTests
{
    private static AdviceReply Reply(string actionJson) =>
        AdviceReply.Parse($$"""{"action": {{actionJson}}, "options": [], "rules": [], "warnings": [], "unverified": false, "policy": "greedy"}""");

    private static PlannedAction Plan(string decision, string actionJson) => AutoPilotPlan.From(decision, Reply(actionJson));

    [Fact]
    public void MapNodeCarriesTheCoordinate()
    {
        var p = Plan("map_select", """{"action": "select_map_node", "args": {"col": 3, "row": 1}}""");
        Assert.Equal(PlanKind.MapNode, p.Kind);
        Assert.Equal((3, 1), (p.Col, p.Row));
        Assert.Empty(p.Indices);
    }

    [Theory]
    [InlineData("card_reward", "select_card_reward", "card_index", 2)]
    [InlineData("event_choice", "choose_option", "option_index", 0)]
    [InlineData("rest_site", "choose_option", "option_index", 1)]
    [InlineData("bundle_select", "select_bundle", "bundle_index", 2)]
    public void SinglePicks(string decision, string action, string arg, int index)
    {
        var p = Plan(decision, $$$"""{"action": "{{{action}}}", "args": {"{{{arg}}}": {{{index}}}}}""");
        Assert.Equal(PlanKind.Pick, p.Kind);
        Assert.Equal(new[] { index }, p.Indices);
    }

    [Fact]
    public void CardRewardSkip()
    {
        Assert.Equal(PlanKind.Skip, Plan("card_reward", """{"action": "skip_card_reward"}""").Kind);
    }

    [Fact]
    public void CardSelectParsesCommaIndicesInOrderWithoutDuplicates()
    {
        var p = Plan("card_select", """{"action": "select_cards", "args": {"indices": "4, 1,4"}}""");
        Assert.Equal(PlanKind.Pick, p.Kind);
        Assert.Equal(new[] { 4, 1 }, p.Indices);
    }

    [Fact]
    public void CardSelectAcceptsANumber()
    {
        Assert.Equal(new[] { 17 }, Plan("card_select", """{"action": "select_cards", "args": {"indices": 17}}""").Indices);
    }

    [Theory]
    [InlineData("""{"action": "buy_card", "args": {"card_index": 6}}""", PlanKind.BuyCard, 6)]
    [InlineData("""{"action": "buy_relic", "args": {"relic_index": 1}}""", PlanKind.BuyRelic, 1)]
    [InlineData("""{"action": "buy_potion", "args": {"potion_index": 0}}""", PlanKind.BuyPotion, 0)]
    public void ShopPurchases(string action, PlanKind kind, int index)
    {
        var p = Plan("shop", action);
        Assert.Equal(kind, p.Kind);
        Assert.Equal(new[] { index }, p.Indices);
    }

    [Fact]
    public void ShopRemoveAndLeave()
    {
        Assert.Equal(PlanKind.RemoveCard, Plan("shop", """{"action": "remove_card"}""").Kind);
        Assert.Equal(PlanKind.Leave, Plan("shop", """{"action": "leave_room"}""").Kind);
    }

    [Fact]
    public void NoAdviceThrows()
    {
        var reply = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_none"], "unverified": false, "policy": "greedy"}""");
        var e = Assert.Throws<AutoPilotException>(() => AutoPilotPlan.From("shop", reply));
        Assert.Contains("greedy_none", e.Message);
    }

    [Theory]
    [InlineData("shop", """{"action": "select_card_reward", "args": {"card_index": 0}}""")]   // wrong decision
    [InlineData("card_select", """{"action": "skip_select"}""")]                              // never emitted with cards; not executable
    [InlineData("rest_site", """{"action": "proceed"}""")]                                    // greedy's fallback
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {}}""")]          // missing index
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {"card_index": -1}}""")]
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {"card_index": "1"}}""")]
    [InlineData("card_select", """{"action": "select_cards", "args": {"indices": ""}}""")]
    [InlineData("card_select", """{"action": "select_cards", "args": {"indices": "1,x"}}""")]
    [InlineData("map_select", """{"action": "select_map_node", "args": {"col": 1}}""")]
    public void UnexecutableAdviceThrows(string decision, string action)
    {
        Assert.Throws<AutoPilotException>(() => Plan(decision, action));
    }
}
