using System.Text.Json;
using Sts2Advisor.Core;

public class AdviceReplyTests
{
    public const string CardRewardJson = """
    {"action": {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}},
     "options": [
       {"kind": "card", "index": 1, "score": 7.4, "status": "ok",
        "components": [{"key": "card.base", "value": 6.8, "detail": "A"},
                       {"key": "card.boss_defense_gap", "value": 0.6},
                       {"key": "card.archetype", "value": 0.0}]},
       {"kind": "card", "index": 0, "score": null, "status": "filtered", "components": []}],
     "rules": [{"key": "card_reward.pick_best", "outcome": "fired",
                "values": {"threshold": 5.5, "act": 1, "deck_size": 14, "best_score": 7.4}}],
     "warnings": ["STS2_MC_ROLLOUT=1"], "unverified": false, "policy": "greedy"}
    """;

    [Fact]
    public void ParsesEveryPart()
    {
        var r = AdviceReply.Parse(CardRewardJson);
        Assert.Equal("select_card_reward", r.ActionName);
        Assert.Equal(1, r.Arg("card_index")!.Value.GetInt32());
        Assert.Equal(2, r.Options.Count);
        Assert.Equal(7.4, r.Options[0].Score);
        Assert.Equal("A", r.Options[0].Components[0].Detail!.Value.GetString());
        Assert.Null(r.Options[1].Score);
        Assert.Equal("filtered", r.Options[1].Status);
        Assert.Equal(5.5, r.Rules[0].Values["threshold"].GetDouble());
        Assert.Equal(new[] { "STS2_MC_ROLLOUT=1" }, r.Warnings);
        Assert.False(r.Unverified);
        Assert.Equal("greedy", r.Policy);
        Assert.Equal(CardRewardJson, r.Raw);
    }

    [Fact]
    public void NullActionMeansNoAdvice()
    {
        var r = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_none"], "unverified": false, "policy": "greedy"}""");
        Assert.Null(r.Action);
        Assert.Null(r.ActionName);
        Assert.Null(r.Arg("card_index"));
    }

    [Fact]
    public void ParsesEveryStatusTheServiceEmits()
    {
        var statuses = new[] { "ok", "filtered", "locked", "unstocked", "unaffordable", "reserved", "broken" };
        var options = string.Join(",", statuses.Select((s, i) =>
            $$"""{"kind": "shop_item", "index": {{i}}, "score": {{(s == "ok" ? "3.5" : "null")}}, "status": "{{s}}", "components": []}"""));
        var r = AdviceReply.Parse($$"""{"action": null, "options": [{{options}}], "rules": [], "warnings": [], "unverified": false, "policy": "greedy"}""");
        Assert.Equal(statuses, r.Options.Select(o => o.Status).ToArray());
        Assert.Equal(3.5, r.Options[0].Score);
        Assert.All(r.Options.Skip(1), o => Assert.Null(o.Score));
    }

    [Fact]
    public void RuleValuesKeepListsBoolsAndNulls()
    {
        var r = AdviceReply.Parse("""
            {"action": null, "options": [],
             "rules": [{"key": "shop.pick", "outcome": "fired",
                        "values": {"picked": [0, 2], "tied_best": [0, 1], "available": false,
                                   "best_card_score": null, "reason": "gold", "gold": 120}}],
             "warnings": [], "unverified": false, "policy": "greedy"}
            """);
        var v = r.Rules[0].Values;
        Assert.Equal(JsonValueKind.Array, v["picked"].ValueKind);
        Assert.Equal(new[] { 0, 2 }, v["picked"].EnumerateArray().Select(e => e.GetInt32()).ToArray());
        Assert.Equal(new[] { 0, 1 }, v["tied_best"].EnumerateArray().Select(e => e.GetInt32()).ToArray());
        Assert.Equal(JsonValueKind.False, v["available"].ValueKind);
        Assert.Equal(JsonValueKind.Null, v["best_card_score"].ValueKind);
        Assert.Equal(JsonValueKind.String, v["reason"].ValueKind);
        Assert.Equal(JsonValueKind.Number, v["gold"].ValueKind);
    }
}
