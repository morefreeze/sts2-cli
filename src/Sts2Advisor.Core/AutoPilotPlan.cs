// src/Sts2Advisor.Core/AutoPilotPlan.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>Pick: choose the options at Indices (one, or several for card_select). Skip: the card reward's skip.
/// MapNode: travel to (Col, Row). Buy*: the shop entry at Indices[0]. RemoveCard / Leave: the shop's removal / exit.</summary>
public enum PlanKind { Pick, Skip, MapNode, BuyCard, BuyRelic, BuyPotion, RemoveCard, Leave }

public sealed record PlannedAction(PlanKind Kind, IReadOnlyList<int> Indices, int Col = -1, int Row = -1);

/// <summary>Advice the autopilot cannot carry out, or a screen that does not match it. Stops the autopilot.</summary>
public sealed class AutoPilotException(string message) : Exception(message);

/// <summary>Turns greedy's engine action (agent/combat_env.py greedy_action) into what the autopilot clicks.
/// Indices point into the decision JSON's lists (DecisionSerializer order).</summary>
public static class AutoPilotPlan
{
    public static PlannedAction From(string decision, AdviceReply reply)
    {
        string action = reply.ActionName
            ?? throw new AutoPilotException($"{decision}: no advice ({string.Join("; ", reply.Warnings)})");
        return (decision, action) switch
        {
            ("map_select", "select_map_node") =>
                new(PlanKind.MapNode, Array.Empty<int>(), Int(reply, decision, "col"), Int(reply, decision, "row")),
            ("card_reward", "select_card_reward") => One(PlanKind.Pick, reply, decision, "card_index"),
            ("card_reward", "skip_card_reward") => new(PlanKind.Skip, Array.Empty<int>()),
            ("event_choice" or "rest_site", "choose_option") => One(PlanKind.Pick, reply, decision, "option_index"),
            ("card_select", "select_cards") => new(PlanKind.Pick, Indices(reply, decision)),
            ("bundle_select", "select_bundle") => One(PlanKind.Pick, reply, decision, "bundle_index"),
            ("shop", "buy_card") => One(PlanKind.BuyCard, reply, decision, "card_index"),
            ("shop", "buy_relic") => One(PlanKind.BuyRelic, reply, decision, "relic_index"),
            ("shop", "buy_potion") => One(PlanKind.BuyPotion, reply, decision, "potion_index"),
            ("shop", "remove_card") => new(PlanKind.RemoveCard, Array.Empty<int>()),
            ("shop", "leave_room") => new(PlanKind.Leave, Array.Empty<int>()),
            _ => throw new AutoPilotException($"{decision}: autopilot cannot execute '{action}'"),
        };
    }

    private static PlannedAction One(PlanKind kind, AdviceReply reply, string decision, string arg) =>
        new(kind, new[] { Int(reply, decision, arg) });

    private static int Int(AdviceReply reply, string decision, string arg) =>
        reply.Arg(arg) is { ValueKind: JsonValueKind.Number } v && v.TryGetInt32(out int i) && i >= 0
            ? i
            : throw new AutoPilotException($"{decision}: '{reply.ActionName}' needs a non-negative integer '{arg}'");

    /// <summary>"4, 1,4" -> [4, 1]: order kept, duplicates dropped. A bare number is one index.</summary>
    private static IReadOnlyList<int> Indices(AdviceReply reply, string decision)
    {
        var arg = reply.Arg("indices");
        if (arg is { ValueKind: JsonValueKind.Number } n && n.TryGetInt32(out int single) && single >= 0)
            return new[] { single };
        if (arg is not { ValueKind: JsonValueKind.String } s)
            throw new AutoPilotException($"{decision}: select_cards needs 'indices'");
        var result = new List<int>();
        foreach (var part in s.GetString()!.Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        {
            if (!int.TryParse(part, out int i) || i < 0)
                throw new AutoPilotException($"{decision}: bad index '{part}' in '{s.GetString()}'");
            if (!result.Contains(i))
                result.Add(i);
        }
        return result.Count > 0 ? result : throw new AutoPilotException($"{decision}: select_cards with no indices");
    }
}
