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

    [Fact]
    public void OptionDetailSurvivesTheParsedDocumentBeingDisposed()
    {
        // Parse disposes its JsonDocument; every JsonElement kept in the reply must have been cloned.
        var r = AdviceReply.Parse("""
            {"action": {"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 4}},
             "options": [{"kind": "node", "index": 1, "score": 4.1, "status": "ok",
                          "detail": {"col": 3, "row": 4, "type": "Elite"},
                          "components": [{"key": "map.base_priority", "value": 6, "detail": "Elite"}]}],
             "rules": [{"key": "map.pick_min_priority", "outcome": "fired", "values": {"tied_best": [1]}}],
             "warnings": [], "unverified": true, "policy": "greedy"}
            """);
        GC.Collect();
        var detail = r.Options[0].Detail!.Value;
        Assert.Equal(3, detail.GetProperty("col").GetInt32());
        Assert.Equal("Elite", detail.GetProperty("type").GetString());
        Assert.Equal("Elite", r.Options[0].Components[0].Detail!.Value.GetString());
        Assert.Equal(4, r.Arg("row")!.Value.GetInt32());
        Assert.Equal(1, r.Rules[0].Values["tied_best"][0].GetInt32());
    }

    [Fact]
    public void ParsesAnUnverifiedReply()
    {
        var r = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": [], "unverified": true, "policy": "greedy"}""");
        Assert.True(r.Unverified);
        var verified = AdviceReply.Parse("""{"action": null, "unverified": false}""");
        Assert.False(verified.Unverified);
    }

    [Fact]
    public void MissingTopLevelPartsAreEmptyNotErrors()
    {
        var r = AdviceReply.Parse("{}");
        Assert.Null(r.Action);
        Assert.Empty(r.Options);
        Assert.Empty(r.Rules);
        Assert.Empty(r.Warnings);
        Assert.False(r.Unverified);
        Assert.Equal("", r.Policy);
        var nulls = AdviceReply.Parse("""{"action": null, "options": null, "rules": null, "warnings": null, "unverified": null, "policy": null}""");
        Assert.Empty(nulls.Options);
        Assert.Equal("", nulls.Policy);
    }

    [Theory]
    [InlineData("""{"options": 5}""", "options")]
    [InlineData("""{"options": [5]}""", "options[0]")]
    [InlineData("""{"options": [{"index": 0, "status": "ok"}]}""", "options[0].kind")]
    [InlineData("""{"options": [{"kind": null, "index": 0, "status": "ok"}]}""", "options[0].kind")]
    [InlineData("""{"options": [{"kind": 7, "index": 0, "status": "ok"}]}""", "options[0].kind")]
    [InlineData("""{"options": [{"kind": "card", "status": "ok"}]}""", "options[0].index")]
    [InlineData("""{"options": [{"kind": "card", "index": null, "status": "ok"}]}""", "options[0].index")]
    [InlineData("""{"options": [{"kind": "card", "index": "1", "status": "ok"}]}""", "options[0].index")]
    [InlineData("""{"options": [{"kind": "card", "index": 1.5, "status": "ok"}]}""", "options[0].index")]
    [InlineData("""{"options": [{"kind": "card", "index": 1}]}""", "options[0].status")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": null}]}""", "options[0].status")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": 3}]}""", "options[0].status")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "score": "high"}]}""", "options[0].score")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": {}}]}""", "options[0].components")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": [3]}]}""", "options[0].components[0]")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": [{"value": 1}]}]}""", "options[0].components[0].key")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": [{"key": "card.base"}]}]}""", "options[0].components[0].value")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": [{"key": "card.base", "value": null}]}]}""", "options[0].components[0].value")]
    [InlineData("""{"options": [{"kind": "card", "index": 1, "status": "ok", "components": [{"key": "card.base", "value": "6.8"}]}]}""", "options[0].components[0].value")]
    [InlineData("""{"options": [{"kind": "card", "index": 0, "status": "ok"}, {"kind": "card", "status": "ok"}]}""", "options[1].index")]
    [InlineData("""{"rules": {}}""", "rules")]
    [InlineData("""{"rules": [{"outcome": "fired"}]}""", "rules[0].key")]
    [InlineData("""{"rules": [{"key": null, "outcome": "fired"}]}""", "rules[0].key")]
    [InlineData("""{"rules": [{"key": "a.b"}]}""", "rules[0].outcome")]
    [InlineData("""{"rules": [{"key": "a.b", "outcome": null}]}""", "rules[0].outcome")]
    [InlineData("""{"rules": [{"key": "a.b", "outcome": "fired", "values": [1]}]}""", "rules[0].values")]
    [InlineData("""{"warnings": "x"}""", "warnings")]
    [InlineData("""{"warnings": [5]}""", "warnings[0]")]
    [InlineData("""{"unverified": "yes"}""", "unverified")]
    [InlineData("""{"policy": 5}""", "policy")]
    [InlineData("""{"action": "select_card_reward"}""", "action")]
    [InlineData("""{"action": []}""", "action")]
    [InlineData("""{"action": {"cmd": "action", "action": 5}}""", "action.action")]
    public void MalformedRepliesThrowOnlyAdviceFormatExceptionNamingTheField(string json, string path)
    {
        var e = Assert.Throws<AdviceFormatException>(() => AdviceReply.Parse(json));
        Assert.Equal(path, e.Path);
        Assert.Contains(path, e.Message);
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("42")]
    [InlineData("null")]
    [InlineData("true")]
    [InlineData("\"select_card_reward\"")]
    public void AReplyThatIsNotAnObjectIsAFormatError(string json)
    {
        var e = Assert.Throws<AdviceFormatException>(() => AdviceReply.Parse(json));
        Assert.Equal("$", e.Path);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("{not json")]
    [InlineData("<html>502 Bad Gateway</html>")]
    [InlineData("""{"action": null, "options": [""")]
    public void InvalidJsonIsAFormatErrorWithTheOriginalAsInner(string json)
    {
        var e = Assert.Throws<AdviceFormatException>(() => AdviceReply.Parse(json));
        Assert.IsAssignableFrom<JsonException>(e.InnerException);
        Assert.Contains("not valid JSON", e.Message);
    }

    [Fact]
    public void ANullStringIsAFormatErrorToo()
    {
        var e = Assert.Throws<AdviceFormatException>(() => AdviceReply.Parse(null!));
        Assert.NotNull(e.InnerException);
    }

    [Fact]
    public void ActionNameAndArgNeverThrowOnOddShapes()
    {
        // Built by hand, bypassing Parse: the accessors must not throw either.
        using var doc = JsonDocument.Parse("""{"action": 5, "args": "x"}""");
        var odd = new AdviceReply(doc.RootElement.Clone(), Array.Empty<TraceOption>(), Array.Empty<TraceRule>(),
                                  Array.Empty<string>(), false, "greedy", "{}");
        Assert.Null(odd.ActionName);
        Assert.Null(odd.Arg("card_index"));
    }
}
