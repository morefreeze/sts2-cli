using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>One finished combat, written to advisor_trace.jsonl as the state of a <c>combat_end</c> line.
/// Every field is optional: a value the mod could not read is null, never a guess.
/// <see cref="DamageTaken"/> is HpStart - HpEnd; HpEnd is read after end-of-combat relic effects (Burning Blood ...).
/// <see cref="PotionsUsed"/> is the multiset difference start - end; potions gained mid-combat are ignored.</summary>
public sealed record CombatRecord
{
    private static readonly JsonWriterOptions WriterOptions =
        new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public string? Encounter { get; init; }
    public string? RoomType { get; init; }
    /// <summary>1-based.</summary>
    public int? Act { get; init; }
    /// <summary>Act-local floor.</summary>
    public int? Floor { get; init; }
    public bool Won { get; init; }
    public int? Turns { get; init; }
    public int? HpStart { get; init; }
    public int? MaxHpStart { get; init; }
    public int? BlockStart { get; init; }
    public int? HpEnd { get; init; }
    public int? MaxHpEnd { get; init; }
    public int? BlockEnd { get; init; }
    public int? GoldStart { get; init; }
    public int? GoldEnd { get; init; }
    public IReadOnlyList<string>? PotionsStart { get; init; }
    public IReadOnlyList<string>? PotionsEnd { get; init; }

    public int? DamageTaken => HpStart is { } a && HpEnd is { } b ? a - b : null;

    /// <summary>Potions in start that are not in end (each end potion cancels one equal start potion); null if either is unknown.</summary>
    public IReadOnlyList<string>? PotionsUsed
    {
        get
        {
            if (PotionsStart == null || PotionsEnd == null) return null;
            var left = new List<string>(PotionsEnd);
            var used = new List<string>();
            foreach (var p in PotionsStart)
            {
                int i = left.IndexOf(p);
                if (i >= 0) left.RemoveAt(i); else used.Add(p);
            }
            return used;
        }
    }

    public string ToJson()
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            Str(w, "encounter", Encounter);
            Str(w, "room_type", RoomType);
            Num(w, "act", Act);
            Num(w, "floor", Floor);
            w.WriteBoolean("won", Won);
            Num(w, "turns", Turns);
            Num(w, "hp_start", HpStart);
            Num(w, "max_hp_start", MaxHpStart);
            Num(w, "block_start", BlockStart);
            Num(w, "hp_end", HpEnd);
            Num(w, "max_hp_end", MaxHpEnd);
            Num(w, "block_end", BlockEnd);
            Num(w, "damage_taken", DamageTaken);
            Num(w, "gold_start", GoldStart);
            Num(w, "gold_end", GoldEnd);
            Arr(w, "potions_start", PotionsStart);
            Arr(w, "potions_end", PotionsEnd);
            Arr(w, "potions_used", PotionsUsed);
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.ToArray());
    }

    private static void Str(Utf8JsonWriter w, string name, string? v)
    {
        if (v == null) w.WriteNull(name); else w.WriteString(name, v);
    }

    private static void Num(Utf8JsonWriter w, string name, int? v)
    {
        if (v is { } n) w.WriteNumber(name, n); else w.WriteNull(name);
    }

    private static void Arr(Utf8JsonWriter w, string name, IReadOnlyList<string>? v)
    {
        if (v == null) { w.WriteNull(name); return; }
        w.WriteStartArray(name);
        foreach (var s in v) w.WriteStringValue(s);
        w.WriteEndArray();
    }
}
