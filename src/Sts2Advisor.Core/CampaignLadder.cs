// src/Sts2Advisor.Core/CampaignLadder.cs
using System.Text;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>The character of the run that ended last and whether it was won.</summary>
public readonly record struct LastRun(string Character, bool Victory);

/// <summary>What the ladder needs to know about the game: each listed character's level (max unlocked ascension, capped
/// at 10), the characters that are done (a win at ascension 10 or more) and the run that ended last.</summary>
public sealed record LadderState(IReadOnlyDictionary<string, int> Levels, IReadOnlySet<string> Done, LastRun? Last);

/// <param name="Character">Null when the ladder is complete.</param>
/// <param name="Sticky">Picked again because its last run was a loss at the lowest level.</param>
public sealed record LadderPick(string? Character, int Level, bool Sticky, bool Complete);

/// <summary>campaign.json's <c>"strategy": "ladder"</c>: play the characters up one shared ladder, lowest level first.
/// A character is DONE when it has won a run at ascension 10 or more. Of the others the lowest level plays; if the
/// character that played the last ended run lost it and is still at that lowest level, it plays again ("keep playing
/// it until it clears"); otherwise the first listed character at the lowest level. A win moves the character up one
/// level, so it is never sticky after a win. Pure; the game reads (levels, history) live in CampaignControl.</summary>
public static class CampaignLadder
{
    public const string Name = "ladder";

    /// <summary>A win at this ascension or above finishes a character.</summary>
    public const int DoneAscension = CampaignPlan.GameMaxAscension;

    public static readonly IReadOnlyList<string> KnownCharacters = new[] { "IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER" };

    public static int LevelOf(IReadOnlyDictionary<string, int> levels, string character) =>
        levels.TryGetValue(character, out int l) ? Math.Clamp(l, 0, CampaignPlan.GameMaxAscension) : 0;

    public static LadderPick Pick(IReadOnlyList<string> characters, LadderState state)
    {
        var eligible = characters.Where(c => !state.Done.Contains(c)).ToList();
        if (eligible.Count == 0)
            return new LadderPick(null, 0, false, true);
        int min = eligible.Min(c => LevelOf(state.Levels, c));
        // sticky iff the last character is eligible AND its last run was a loss AND its level is the minimum level
        if (state.Last is { Victory: false } last
            && eligible.FirstOrDefault(c => string.Equals(c, last.Character, StringComparison.OrdinalIgnoreCase)) is { } again
            && LevelOf(state.Levels, again) == min)
            return new LadderPick(again, min, true, false);
        string first = eligible.First(c => LevelOf(state.Levels, c) == min);
        return new LadderPick(first, min, false, false);
    }

    /// <summary>"N9 R9 I10 done": first letters of the characters (they differ), the level, "done" when finished.</summary>
    public static string Summary(IReadOnlyList<string> characters, LadderState state)
    {
        var sb = new StringBuilder();
        foreach (var c in characters)
        {
            if (sb.Length > 0)
                sb.Append(' ');
            sb.Append(c.Length > 0 ? c[0] : '?').Append(LevelOf(state.Levels, c));
            if (state.Done.Contains(c))
                sb.Append(" done");
        }
        return sb.ToString();
    }

    /// <summary>The log line of a pick: "ladder pick NECROBINDER a9 (levels N9 R9 I10; sticky after loss)".</summary>
    public static string Describe(LadderPick pick, IReadOnlyList<string> characters, LadderState state)
    {
        if (pick.Complete)
            return "ladder complete";
        return $"ladder pick {pick.Character} a{pick.Level} (levels {Summary(characters, state)}; " +
               (pick.Sticky ? "sticky after loss" : "lowest level, first in order") + ")";
    }
}

/// <summary>The game's own run histories (&lt;profile&gt;/saves/history/&lt;start_time&gt;.run, the modded profile's while
/// mods are loaded) are the source of truth for "has this character won at ascension 10": <c>win</c>, <c>ascension</c>
/// and <c>players[0].character</c> ("CHARACTER.IRONCLAD").</summary>
public static class CampaignHistory
{
    public readonly record struct HistoryRun(string Character, bool Win, int Ascension);

    /// <summary>Null for anything that is not a single-player run history.</summary>
    public static HistoryRun? ParseRun(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !root.TryGetProperty("win", out var win) || win.ValueKind is not (JsonValueKind.True or JsonValueKind.False)
                || !root.TryGetProperty("ascension", out var asc) || !asc.TryGetInt32(out int level)
                || !root.TryGetProperty("players", out var players) || players.ValueKind != JsonValueKind.Array
                || players.GetArrayLength() != 1
                || players[0].ValueKind != JsonValueKind.Object
                || !players[0].TryGetProperty("character", out var c) || c.ValueKind != JsonValueKind.String)
                return null;
            string name = c.GetString()!.Trim().ToUpperInvariant();
            if (name.StartsWith("CHARACTER.", StringComparison.Ordinal))
                name = name["CHARACTER.".Length..];
            return name.Length == 0 ? null : new HistoryRun(name, win.GetBoolean(), level);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    public static IReadOnlySet<string> Cleared(IEnumerable<HistoryRun> runs, int minAscension = CampaignLadder.DoneAscension) =>
        runs.Where(r => r.Win && r.Ascension >= minAscension).Select(r => r.Character).ToHashSet(StringComparer.OrdinalIgnoreCase);
}

/// <summary>Reads the characters that have won at ascension 10 from a history folder. Each file is parsed once
/// (keyed by name, length and write time, so a file still being written is read again); the folder is listed on every
/// call, which is cheap. Never throws.</summary>
public sealed class CampaignHistoryScan
{
    private readonly Dictionary<string, (long Length, long Ticks, CampaignHistory.HistoryRun? Run)> _seen = new();

    public IReadOnlySet<string> Cleared(string directory)
    {
        try
        {
            if (!Directory.Exists(directory))
                return new HashSet<string>();
            var names = new HashSet<string>();
            foreach (var f in new DirectoryInfo(directory).EnumerateFiles("*.run"))
            {
                names.Add(f.Name);
                if (_seen.TryGetValue(f.Name, out var known) && known.Length == f.Length && known.Ticks == f.LastWriteTimeUtc.Ticks)
                    continue;
                CampaignHistory.HistoryRun? run = null;
                try { run = CampaignHistory.ParseRun(File.ReadAllText(f.FullName)); }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
                _seen[f.Name] = (f.Length, f.LastWriteTimeUtc.Ticks, run);
            }
            foreach (var gone in _seen.Keys.Where(k => !names.Contains(k)).ToList())
                _seen.Remove(gone);
            return CampaignHistory.Cleared(_seen.Values.Where(v => v.Run != null).Select(v => v.Run!.Value));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return new HashSet<string>();
        }
    }
}
