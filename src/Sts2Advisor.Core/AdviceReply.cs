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
    /// <summary>Parses a reply. Throws <see cref="AdviceFormatException"/> -- and nothing else --
    /// for anything malformed: invalid JSON, a non-object root, a missing or null required field
    /// (option kind / index / status, component key / value, rule key / outcome) or a field of the
    /// wrong JSON kind. Top-level parts that are absent or null (options, rules, warnings, policy,
    /// unverified) read as empty / false / "".</summary>
    public static AdviceReply Parse(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            return Build(doc.RootElement, json);
        }
        catch (AdviceFormatException)
        {
            throw;
        }
        catch (JsonException e)
        {
            throw new AdviceFormatException("$", $"not valid JSON ({e.Message})", e);
        }
        catch (Exception e) when (e is not OutOfMemoryException)
        {
            throw new AdviceFormatException("$", $"unreadable ({e.GetType().Name}: {e.Message})", e);
        }
    }

    private static AdviceReply Build(JsonElement root, string raw)
    {
        RequireObject(root, "$");

        JsonElement? action = null;
        if (root.TryGetProperty("action", out var a) && a.ValueKind != JsonValueKind.Null)
        {
            RequireObject(a, "action", "an object or null");
            if (a.TryGetProperty("action", out var name) && name.ValueKind is not (JsonValueKind.String or JsonValueKind.Null))
                throw Bad("action.action", $"expected a string, got {name.ValueKind}");
            action = a.Clone();
        }

        var options = new List<TraceOption>();
        foreach (var (o, path) in Items(root, "options", ""))
        {
            RequireObject(o, path);
            var kind = RequireString(o, "kind", path);
            var index = RequireInt(o, "index", path);
            var score = OptionalNumber(o, "score", path);
            var status = RequireString(o, "status", path);
            var components = new List<TraceComponent>();
            foreach (var (c, cpath) in Items(o, "components", path))
            {
                RequireObject(c, cpath);
                components.Add(new TraceComponent(
                    RequireString(c, "key", cpath), RequireNumber(c, "value", cpath),
                    c.TryGetProperty("detail", out var cd) ? cd.Clone() : null));
            }
            options.Add(new TraceOption(kind, index, score, status, components,
                o.TryGetProperty("detail", out var d) ? d.Clone() : null));
        }

        var rules = new List<TraceRule>();
        foreach (var (rule, path) in Items(root, "rules", ""))
        {
            RequireObject(rule, path);
            var key = RequireString(rule, "key", path);
            var outcome = RequireString(rule, "outcome", path);
            var values = new Dictionary<string, JsonElement>();
            if (rule.TryGetProperty("values", out var vs) && vs.ValueKind != JsonValueKind.Null)
            {
                RequireObject(vs, Join(path, "values"), "an object or null");
                foreach (var p in vs.EnumerateObject())
                    values[p.Name] = p.Value.Clone();
            }
            rules.Add(new TraceRule(key, outcome, values));
        }

        var warnings = new List<string>();
        foreach (var (w, path) in Items(root, "warnings", ""))
        {
            if (w.ValueKind == JsonValueKind.String)
                warnings.Add(w.GetString()!);
            else if (w.ValueKind != JsonValueKind.Null)
                throw Bad(path, $"expected a string, got {w.ValueKind}");
        }

        var unverified = false;
        if (root.TryGetProperty("unverified", out var u) && u.ValueKind != JsonValueKind.Null)
        {
            if (u.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                throw Bad("unverified", $"expected a boolean, got {u.ValueKind}");
            unverified = u.ValueKind == JsonValueKind.True;
        }

        var policy = "";
        if (root.TryGetProperty("policy", out var pol) && pol.ValueKind != JsonValueKind.Null)
        {
            if (pol.ValueKind != JsonValueKind.String)
                throw Bad("policy", $"expected a string, got {pol.ValueKind}");
            policy = pol.GetString()!;
        }

        return new AdviceReply(action, options, rules, warnings, unverified, policy, raw);
    }

    private static AdviceFormatException Bad(string path, string problem) => new(path, problem);

    private static string Join(string parent, string name) => parent.Length == 0 ? name : parent + "." + name;

    private static void RequireObject(JsonElement e, string path, string expected = "an object")
    {
        if (e.ValueKind != JsonValueKind.Object)
            throw Bad(path, $"expected {expected}, got {e.ValueKind}");
    }

    private static string RequireString(JsonElement obj, string name, string parent)
    {
        var path = Join(parent, name);
        if (!obj.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null)
            throw Bad(path, "missing");
        if (v.ValueKind != JsonValueKind.String)
            throw Bad(path, $"expected a string, got {v.ValueKind}");
        return v.GetString()!;
    }

    private static int RequireInt(JsonElement obj, string name, string parent)
    {
        var path = Join(parent, name);
        if (!obj.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null)
            throw Bad(path, "missing");
        if (v.ValueKind != JsonValueKind.Number || !v.TryGetInt32(out var n))
            throw Bad(path, $"expected an integer, got {v.ValueKind} {v.GetRawText()}");
        return n;
    }

    private static double RequireNumber(JsonElement obj, string name, string parent)
    {
        var path = Join(parent, name);
        if (!obj.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null)
            throw Bad(path, "missing");
        if (v.ValueKind != JsonValueKind.Number || !v.TryGetDouble(out var n))
            throw Bad(path, $"expected a number, got {v.ValueKind}");
        return n;
    }

    private static double? OptionalNumber(JsonElement obj, string name, string parent)
    {
        if (!obj.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null)
            return null;
        if (v.ValueKind != JsonValueKind.Number || !v.TryGetDouble(out var n))
            throw Bad(Join(parent, name), $"expected a number or null, got {v.ValueKind}");
        return n;
    }

    /// <summary>The elements of an optional array property with their paths ("options[0]"); absent or null = none.</summary>
    private static List<(JsonElement Item, string Path)> Items(JsonElement obj, string name, string parent)
    {
        var path = Join(parent, name);
        var items = new List<(JsonElement, string)>();
        if (!obj.TryGetProperty(name, out var v) || v.ValueKind == JsonValueKind.Null)
            return items;
        if (v.ValueKind != JsonValueKind.Array)
            throw Bad(path, $"expected an array, got {v.ValueKind}");
        var i = 0;
        foreach (var item in v.EnumerateArray())
            items.Add((item, $"{path}[{i++}]"));
        return items;
    }

    /// <summary>The engine action name, e.g. "select_card_reward"; null when there is no advice.</summary>
    public string? ActionName =>
        Action is { ValueKind: JsonValueKind.Object } a && a.TryGetProperty("action", out var n)
        && n.ValueKind == JsonValueKind.String ? n.GetString() : null;

    public JsonElement? Arg(string name) =>
        Action is { ValueKind: JsonValueKind.Object } a && a.TryGetProperty("args", out var args)
        && args.ValueKind == JsonValueKind.Object && args.TryGetProperty(name, out var v) ? v : null;
}
