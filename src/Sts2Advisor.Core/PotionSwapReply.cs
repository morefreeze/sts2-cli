using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>agent/advisor_server.py's POST /potion_swap reply (agent/potion_swap.py). <see cref="Discard"/> is a
/// position in the held potions sent with the request, <see cref="Take"/> one in the offered list; both null
/// means "leave the belt as it is" (reason not_better / no_offer / no_slots). Reason free_slot has a Take and no
/// Discard (just claim it); swap has both. The ids echo the sent potions so the caller can check it still means
/// the same potion.</summary>
public sealed record PotionSwapReply(int? Discard, int? Take, string? DiscardId, string? TakeId, string Reason,
                                     double? DiscardScore, double? TakeScore, double Margin, string Raw)
{
    /// <summary>Parses a reply. Throws <see cref="AdviceFormatException"/> -- and nothing else -- for anything
    /// malformed: invalid JSON, a non-object root, a missing reason, an index that is not a non-negative integer.</summary>
    public static PotionSwapReply Parse(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                throw new AdviceFormatException("$", $"expected an object, got {root.ValueKind}");
            int? discard = Index(root, "discard"), take = Index(root, "take");
            return new PotionSwapReply(discard, take, Text(root, "discard_id"), Text(root, "take_id"),
                                       Text(root, "reason") ?? throw new AdviceFormatException("reason", "missing"),
                                       ScoreAt(root, "held_scores", discard), ScoreAt(root, "offered_scores", take),
                                       root.TryGetProperty("margin", out var m) && m.ValueKind == JsonValueKind.Number ? m.GetDouble() : 0,
                                       json);
        }
        catch (AdviceFormatException)
        {
            throw;
        }
        catch (JsonException e)
        {
            throw new AdviceFormatException("$", $"not valid JSON ({e.Message})", e);
        }
    }

    /// <summary>The score at <paramref name="index"/> of a score array; null when either is missing or unreadable
    /// (the scores are only shown, never acted on).</summary>
    private static double? ScoreAt(JsonElement root, string name, int? index) =>
        index is { } i && root.TryGetProperty(name, out var a) && a.ValueKind == JsonValueKind.Array && i < a.GetArrayLength()
        && a[i].ValueKind == JsonValueKind.Number ? a[i].GetDouble() : null;

    private static int? Index(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var e) || e.ValueKind == JsonValueKind.Null)
            return null;
        if (e.ValueKind != JsonValueKind.Number || !e.TryGetInt32(out int i) || i < 0)
            throw new AdviceFormatException(name, $"expected a non-negative integer or null, got {e.GetRawText()}");
        return i;
    }

    private static string? Text(JsonElement root, string name)
    {
        if (!root.TryGetProperty(name, out var e) || e.ValueKind == JsonValueKind.Null)
            return null;
        return e.ValueKind == JsonValueKind.String ? e.GetString()
            : throw new AdviceFormatException(name, $"expected a string or null, got {e.ValueKind}");
    }
}
