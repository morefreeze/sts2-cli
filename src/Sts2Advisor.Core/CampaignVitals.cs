// src/Sts2Advisor.Core/CampaignVitals.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

public readonly record struct RunVitals(int Hp, int MaxHp, int Gold);

/// <summary>The player's HP, max HP and gold at the end of a run, for campaign_runs.jsonl.
/// After a victory the live player is dead: the Architect's WinRun runs RunManager.OnEnded(true) (which snapshots the
/// last map point history entry and saves &lt;start_time&gt;.run) and then kills the player, and killing the last player
/// runs OnEnded(false), whose UpdatePlayerStatsInMapPointHistory overwrites that same in-memory entry with HP 0. So
/// the live HP and the in-memory entry both read 0; the saved history file still has the HP of the victory.</summary>
public static class CampaignVitals
{
    /// <summary>A defeat is the live player (HP 0). A victory is the first of <paramref name="recorded"/> (best source
    /// first) that still has HP, else the live values.</summary>
    public static RunVitals Pick(bool victory, RunVitals live, params RunVitals?[] recorded)
    {
        if (!victory)
            return live;
        foreach (var r in recorded)
        {
            if (r is { Hp: > 0 } v)
                return v;
        }
        return live;
    }

    /// <summary>The last map point history entry's stats of the player with <paramref name="playerId"/> (the first
    /// player when that id is not there, or null) in a run history file's JSON; null when the file has no such entry.</summary>
    public static RunVitals? FromHistoryJson(string json, ulong? playerId)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.ValueKind != JsonValueKind.Object
                || !doc.RootElement.TryGetProperty("map_point_history", out var acts)
                || acts.ValueKind != JsonValueKind.Array)
                return null;
            JsonElement last = default;
            foreach (var act in acts.EnumerateArray())
            {
                if (act.ValueKind == JsonValueKind.Array && act.GetArrayLength() > 0)
                    last = act[act.GetArrayLength() - 1];
            }
            if (last.ValueKind != JsonValueKind.Object
                || !last.TryGetProperty("player_stats", out var stats)
                || stats.ValueKind != JsonValueKind.Array || stats.GetArrayLength() == 0)
                return null;
            JsonElement chosen = stats[0];
            if (playerId is { } id)
            {
                foreach (var p in stats.EnumerateArray())
                {
                    if (p.ValueKind == JsonValueKind.Object && p.TryGetProperty("player_id", out var pid)
                        && pid.ValueKind == JsonValueKind.Number && pid.TryGetUInt64(out var n) && n == id)
                    {
                        chosen = p;
                        break;
                    }
                }
            }
            if (chosen.ValueKind == JsonValueKind.Object
                && Int(chosen, "current_hp") is { } hp && Int(chosen, "max_hp") is { } maxHp && Int(chosen, "current_gold") is { } gold)
                return new RunVitals(hp, maxHp, gold);
            return null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary><see cref="FromHistoryJson"/> of a file; null when it is missing or unreadable.</summary>
    public static RunVitals? FromHistoryFile(string path, ulong? playerId)
    {
        try
        {
            return File.Exists(path) ? FromHistoryJson(File.ReadAllText(path), playerId) : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static int? Int(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out int n) ? n : null;
}
