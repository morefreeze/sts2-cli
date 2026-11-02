// src/Sts2Advisor.Core/CampaignRunLog.cs
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>One finished run, as campaign_runs.jsonl keeps it. The deck lists cards as "CARD.GLOW+1" (the game's id,
/// "+n" for an upgrade level above 0).</summary>
/// <param name="Floor">Act-local floor; <paramref name="TotalFloor"/> counts over the acts.</param>
/// <param name="RunTimeSeconds">The game's own run clock.</param>
/// <param name="KilledBy">The encounter the player died in (ENCOUNTER.X); null for a victory or when unknown.</param>
/// <param name="KilledByMonsters">The monsters of that encounter.</param>
/// <param name="HistoryPath">The game's own history file for this run (&lt;StartTime&gt;.run), if it could be worked out.</param>
public sealed record CampaignRunRecord(
    string Character, int Ascension, bool Victory, int Act, int Floor, int TotalFloor, int Hp, int MaxHp, int Gold,
    IReadOnlyList<string> Deck, IReadOnlyList<string> Relics, IReadOnlyList<string> Potions,
    long? RunTimeSeconds, string? Seed, string? KilledBy, string? KilledByEvent, IReadOnlyList<string> KilledByMonsters,
    string? HistoryPath, long? StartTime);

/// <summary>&lt;user dir&gt;/campaign_runs.jsonl: one line per run the campaign saw end.</summary>
public static class CampaignRunLog
{
    public const string FileName = "campaign_runs.jsonl";

    private static readonly JsonWriterOptions WriterOptions = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    private static readonly object Gate = new();

    public static string CardId(string id, int upgradeLevel) => upgradeLevel > 0 ? $"{id}+{upgradeLevel}" : id;

    public static string ToJson(CampaignRunRecord r, DateTime utcNow)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            w.WriteString("ts", utcNow.ToUniversalTime().ToString("O"));
            w.WriteString("character", r.Character);
            w.WriteNumber("ascension", r.Ascension);
            w.WriteBoolean("victory", r.Victory);
            w.WriteNumber("act", r.Act);
            w.WriteNumber("floor", r.Floor);
            w.WriteNumber("total_floor", r.TotalFloor);
            w.WriteNumber("hp", r.Hp);
            w.WriteNumber("max_hp", r.MaxHp);
            w.WriteNumber("gold", r.Gold);
            Strings(w, "deck", r.Deck);
            Strings(w, "relics", r.Relics);
            Strings(w, "potions", r.Potions);
            Number(w, "run_time", r.RunTimeSeconds);
            Text(w, "seed", r.Seed);
            Text(w, "killed_by", r.KilledBy);
            Text(w, "killed_by_event", r.KilledByEvent);
            Strings(w, "killed_by_monsters", r.KilledByMonsters);
            Text(w, "history_path", r.HistoryPath);
            Number(w, "start_time", r.StartTime);
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.ToArray());
    }

    /// <summary>Appends <paramref name="jsonLine"/> and a newline in one write, so a reader never sees half a line
    /// (and a file whose last line lost its newline does not swallow the new one). False -- never an exception --
    /// when the line is not one line of JSON or the write fails.</summary>
    public static bool Append(string path, string jsonLine)
    {
        try
        {
            if (jsonLine.Contains('\n') || jsonLine.Contains('\r'))
                return false;
            using (JsonDocument.Parse(jsonLine)) { }
            lock (Gate)
            {
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir))
                    Directory.CreateDirectory(dir);
                bool needsBreak = false;
                var info = new FileInfo(path);
                if (info.Exists && info.Length > 0)
                {
                    using var read = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                    read.Seek(-1, SeekOrigin.End);
                    needsBreak = read.ReadByte() != '\n';
                }
                var bytes = Encoding.UTF8.GetBytes((needsBreak ? "\n" : "") + jsonLine + "\n");
                using var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                stream.Write(bytes, 0, bytes.Length);
                stream.Flush(true);
            }
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static void Text(Utf8JsonWriter w, string name, string? value)
    {
        if (value == null) w.WriteNull(name); else w.WriteString(name, value);
    }

    private static void Number(Utf8JsonWriter w, string name, long? value)
    {
        if (value is { } v) w.WriteNumber(name, v); else w.WriteNull(name);
    }

    private static void Strings(Utf8JsonWriter w, string name, IReadOnlyList<string> values)
    {
        w.WriteStartArray(name);
        foreach (var v in values)
            w.WriteStringValue(v);
        w.WriteEndArray();
    }
}
