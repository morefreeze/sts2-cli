using System.Text.Json;

namespace Sts2Advisor.Core;

public sealed record TraceComponent(string Key, double Value, JsonElement? Detail);

public sealed record TraceOption(string Kind, int Index, double? Score, string Status,
                                 IReadOnlyList<TraceComponent> Components, JsonElement? Detail);

public sealed record TraceRule(string Key, string Outcome, IReadOnlyDictionary<string, JsonElement> Values);

/// <summary>agent/advisor_server.py's POST /advise reply: greedy's action plus the
/// GreedyTrace of the same call (options with summed components, ordered rules).</summary>
public sealed record AdviceReply(JsonElement? Action, IReadOnlyList<TraceOption> Options,
                                 IReadOnlyList<TraceRule> Rules, IReadOnlyList<string> Warnings,
                                 bool Unverified, string Policy, string Raw)
{
    public static AdviceReply Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        JsonElement? action = root.TryGetProperty("action", out var a) && a.ValueKind == JsonValueKind.Object
            ? a.Clone() : null;

        var options = new List<TraceOption>();
        if (root.TryGetProperty("options", out var opts) && opts.ValueKind == JsonValueKind.Array)
        {
            foreach (var o in opts.EnumerateArray())
            {
                var comps = new List<TraceComponent>();
                if (o.TryGetProperty("components", out var cs) && cs.ValueKind == JsonValueKind.Array)
                    foreach (var c in cs.EnumerateArray())
                        comps.Add(new TraceComponent(
                            c.GetProperty("key").GetString()!, c.GetProperty("value").GetDouble(),
                            c.TryGetProperty("detail", out var cd) ? (JsonElement?)cd.Clone() : null));
                options.Add(new TraceOption(
                    o.GetProperty("kind").GetString()!, o.GetProperty("index").GetInt32(),
                    o.TryGetProperty("score", out var s) && s.ValueKind == JsonValueKind.Number ? s.GetDouble() : null,
                    o.GetProperty("status").GetString()!, comps,
                    o.TryGetProperty("detail", out var d) ? (JsonElement?)d.Clone() : null));
            }
        }

        var rules = new List<TraceRule>();
        if (root.TryGetProperty("rules", out var rs) && rs.ValueKind == JsonValueKind.Array)
        {
            foreach (var rule in rs.EnumerateArray())
            {
                var values = new Dictionary<string, JsonElement>();
                if (rule.TryGetProperty("values", out var vs) && vs.ValueKind == JsonValueKind.Object)
                    foreach (var p in vs.EnumerateObject())
                        values[p.Name] = p.Value.Clone();
                rules.Add(new TraceRule(rule.GetProperty("key").GetString()!,
                                        rule.GetProperty("outcome").GetString()!, values));
            }
        }

        var warnings = new List<string>();
        if (root.TryGetProperty("warnings", out var ws) && ws.ValueKind == JsonValueKind.Array)
            foreach (var w in ws.EnumerateArray())
                if (w.GetString() is { } text)
                    warnings.Add(text);

        return new AdviceReply(action, options, rules, warnings,
            root.TryGetProperty("unverified", out var u) && u.ValueKind == JsonValueKind.True,
            root.TryGetProperty("policy", out var pol) && pol.ValueKind == JsonValueKind.String ? pol.GetString()! : "",
            json);
    }

    /// <summary>The engine action name, e.g. "select_card_reward"; null when there is no advice.</summary>
    public string? ActionName =>
        Action is { } a && a.TryGetProperty("action", out var n) ? n.GetString() : null;

    public JsonElement? Arg(string name) =>
        Action is { } a && a.TryGetProperty("args", out var args) && args.ValueKind == JsonValueKind.Object
        && args.TryGetProperty(name, out var v) ? v : null;
}
