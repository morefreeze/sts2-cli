// src/Sts2Advisor.Core/CampaignFile.cs
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Sts2Advisor.Core;

/// <summary>"quit": quit the game the game's own way, then rewrite the file with command null. "stop": turn the
/// autopilot off and do nothing until the file changes.</summary>
public enum CampaignCommand { None, Quit, Stop }

/// <summary>next.ascension: "max" (the character's highest unlocked ascension) or a level.</summary>
public readonly record struct AscensionSpec(bool IsMax, int Level)
{
    public static AscensionSpec Max { get; } = new(true, 0);

    public static AscensionSpec Of(int level) => new(false, level);

    public override string ToString() => IsMax ? "max" : Level.ToString();
}

/// <summary>A run the campaign can start: the game's CharacterModel id without the CHARACTER. prefix, upper case
/// (IRONCLAD, SILENT, DEFECT, REGENT, NECROBINDER), and the ascension to start it at.</summary>
public readonly record struct CampaignTarget(string Character, AscensionSpec Ascension);

/// <param name="Character">"next".character; null when the file has no "next".</param>
/// <param name="Rotation">The "rotation" list; null when absent or empty. When present it decides the next run
/// (<see cref="CampaignRotation"/>) and "next" is not used.</param>
/// <param name="Ladder">The "characters" of <c>"strategy": "ladder"</c> (<see cref="CampaignLadder"/>, in tie-break
/// order); null for any other strategy. When set it decides the next run and "next" and "rotation" are not used.</param>
public sealed record CampaignSettings(bool Enabled, string? Character, AscensionSpec? Ascension, CampaignCommand Command,
                                      IReadOnlyList<CampaignTarget>? Rotation = null,
                                      IReadOnlyList<string>? Ladder = null);

/// <param name="Settings">Null: no campaign (no file, an empty file, or an unusable one).</param>
/// <param name="Problem">Why an existing file is unusable; null when the file is fine or absent.</param>
/// <param name="Raw">The file's text, to tell whether it changed since it was last looked at.</param>
public sealed record CampaignRead(CampaignSettings? Settings, string? Problem, string Raw);

/// <summary>&lt;user dir&gt;/campaign.json -- the control file of the campaign mode:
/// <c>{"enabled": true, "next": {"character": "IRONCLAD", "ascension": "max" | 6}, "rotation": [...], "command": null | "quit" | "stop"}</c>.
/// It is re-read whenever the mod needs it, so everything here is cheap and never throws: a missing, empty,
/// half-written or unusable file is "no campaign". A disabled file (enabled false or absent) is not validated
/// further. "next" may be absent (the campaign then cannot start a run, but still plays the one in progress and
/// obeys "command"); when present it must be complete. "rotation" is an optional list of characters, each either a
/// plain name (ascension max) or <c>{"character": ..., "ascension": "max" | n}</c> (ascension optional, max); when it
/// has entries it decides the next run instead of "next". <c>"strategy": "ladder"</c> with <c>"characters": [...]</c>
/// (known character ids, each once, in tie-break order) decides it instead of both.</summary>
public static class CampaignFile
{
    public const string FileName = "campaign.json";

    private static readonly Regex CharacterId = new("^[A-Z][A-Z0-9_]*$", RegexOptions.CultureInvariant);

    public static CampaignRead Load(string path)
    {
        string text;
        try
        {
            text = File.Exists(path) ? File.ReadAllText(path) : "";
        }
        catch (Exception e)
        {
            return new CampaignRead(null, $"{FileName} cannot be read: {e.Message}", "");
        }
        return Parse(text);
    }

    public static CampaignRead Parse(string? text)
    {
        text ??= "";
        if (string.IsNullOrWhiteSpace(text))
            return new CampaignRead(null, null, text);
        try
        {
            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return Invalid(text, $"the root must be a JSON object, not {root.ValueKind}");
            bool enabled = false;
            if (root.TryGetProperty("enabled", out var e))
            {
                if (e.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                    return Invalid(text, "\"enabled\" must be true or false");
                enabled = e.GetBoolean();
            }
            if (!enabled)
                return new CampaignRead(new CampaignSettings(false, null, null, CampaignCommand.None), null, text);

            string? character = null;
            AscensionSpec? ascension = null;
            if (root.TryGetProperty("next", out var next) && next.ValueKind != JsonValueKind.Null)
            {
                if (next.ValueKind != JsonValueKind.Object)
                    return Invalid(text, "\"next\" must be an object with \"character\" and \"ascension\"");
                string? problem = null;
                if (!next.TryGetProperty("character", out var c))
                    problem = "\"next.character\" is missing";
                else if (!TryCharacter(c, out character))
                    problem = "\"next.character\" must be a character id such as IRONCLAD";
                else if (!next.TryGetProperty("ascension", out var a))
                    problem = "\"next.ascension\" is missing";
                else if (!TryAscension(a, out var spec))
                    problem = "\"next.ascension\" must be \"max\" or a whole number from 0";
                else
                    ascension = spec;
                if (problem != null)
                    return Invalid(text, problem);
            }

            IReadOnlyList<CampaignTarget>? rotation = null;
            if (root.TryGetProperty("rotation", out var rot) && rot.ValueKind != JsonValueKind.Null)
            {
                if (rot.ValueKind != JsonValueKind.Array)
                    return Invalid(text, "\"rotation\" must be a list of characters, e.g. [\"IRONCLAD\", {\"character\": \"SILENT\", \"ascension\": 5}]");
                var entries = new List<CampaignTarget>();
                foreach (var item in rot.EnumerateArray())
                {
                    if (!TryTarget(item, out var target, out string? why))
                        return Invalid(text, $"\"rotation\" entry {entries.Count + 1}: {why}");
                    entries.Add(target);
                }
                if (entries.Count > 0)
                    rotation = entries;
            }

            IReadOnlyList<string>? ladder = null;
            if (root.TryGetProperty("strategy", out var strat) && strat.ValueKind != JsonValueKind.Null)
            {
                string? name = strat.ValueKind == JsonValueKind.String ? strat.GetString()!.Trim().ToLowerInvariant() : null;
                if (name == CampaignLadder.Name)
                {
                    if (!root.TryGetProperty("characters", out var chars) || chars.ValueKind != JsonValueKind.Array || chars.GetArrayLength() == 0)
                        return Invalid(text, "\"strategy\": \"ladder\" needs \"characters\": a list such as [\"NECROBINDER\", \"REGENT\", \"IRONCLAD\"]");
                    var list = new List<string>();
                    foreach (var item in chars.EnumerateArray())
                    {
                        if (!TryCharacter(item, out var who) || !CampaignLadder.KnownCharacters.Contains(who!))
                            return Invalid(text, $"\"characters\" entry {list.Count + 1}: unknown character {Describe(item)} (one of {string.Join(", ", CampaignLadder.KnownCharacters)})");
                        if (list.Contains(who!))
                            return Invalid(text, $"\"characters\": {who} is listed twice");
                        list.Add(who!);
                    }
                    ladder = list;
                }
                else if (name != "" && name != "rotation")
                    return Invalid(text, $"unknown \"strategy\" {Describe(strat)} (\"ladder\", or leave it out)");
            }

            var command = CampaignCommand.None;
            if (root.TryGetProperty("command", out var cmd) && cmd.ValueKind != JsonValueKind.Null)
            {
                if (cmd.ValueKind != JsonValueKind.String)
                    return Invalid(text, "\"command\" must be null, \"quit\" or \"stop\"");
                switch (cmd.GetString()!.Trim().ToLowerInvariant())
                {
                    case "": break;
                    case "quit": command = CampaignCommand.Quit; break;
                    case "stop": command = CampaignCommand.Stop; break;
                    default: return Invalid(text, $"unknown \"command\" '{cmd.GetString()}' (null, \"quit\" or \"stop\")");
                }
            }
            return new CampaignRead(new CampaignSettings(true, character, ascension, command, rotation, ladder), null, text);
        }
        catch (JsonException e)
        {
            return Invalid(text, $"not valid JSON: {e.Message}");
        }
    }

    /// <summary>Rewrites the file with <c>"command": null</c> and everything else as it was (atomically: a temp file
    /// next to it, then a move). False when the file cannot be read, parsed or written; it is then left untouched.</summary>
    public static bool ClearCommand(string path)
    {
        string temp = path + "." + Guid.NewGuid().ToString("N")[..8] + ".tmp";
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
            if (node == null)
                return false;
            node["command"] = null;
            File.WriteAllText(temp, node.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n");
            File.Move(temp, path, overwrite: true);
            return true;
        }
        catch (Exception)
        {
            try { if (File.Exists(temp)) File.Delete(temp); } catch (Exception) { }
            return false;
        }
    }

    private static string Describe(JsonElement e) => e.ValueKind == JsonValueKind.String ? $"'{e.GetString()}'" : e.GetRawText();

    private static CampaignRead Invalid(string text, string why) => new(null, $"{FileName}: {why}", text);

    private static bool TryTarget(JsonElement e, out CampaignTarget target, out string? why)
    {
        target = default;
        why = null;
        string? character;
        var ascension = AscensionSpec.Max;
        switch (e.ValueKind)
        {
            case JsonValueKind.String:
                if (!TryCharacter(e, out character))
                    why = "the character must be an id such as IRONCLAD";
                break;
            case JsonValueKind.Object:
                character = null;
                if (!e.TryGetProperty("character", out var c))
                    why = "\"character\" is missing";
                else if (!TryCharacter(c, out character))
                    why = "\"character\" must be a character id such as IRONCLAD";
                else if (e.TryGetProperty("ascension", out var a) && a.ValueKind != JsonValueKind.Null && !TryAscension(a, out ascension))
                    why = "\"ascension\" must be \"max\" or a whole number from 0";
                break;
            default:
                character = null;
                why = "an entry is a character id or {\"character\": ..., \"ascension\": ...}";
                break;
        }
        if (why != null)
            return false;
        target = new CampaignTarget(character!, ascension);
        return true;
    }

    private static bool TryCharacter(JsonElement e, out string? character)
    {
        character = null;
        if (e.ValueKind != JsonValueKind.String)
            return false;
        string s = e.GetString()!.Trim().ToUpperInvariant();
        if (s.StartsWith("CHARACTER.", StringComparison.Ordinal))
            s = s["CHARACTER.".Length..];
        if (!CharacterId.IsMatch(s))
            return false;
        character = s;
        return true;
    }

    private static bool TryAscension(JsonElement e, out AscensionSpec spec)
    {
        spec = default;
        switch (e.ValueKind)
        {
            case JsonValueKind.String:
                string s = e.GetString()!.Trim();
                if (string.Equals(s, "max", StringComparison.OrdinalIgnoreCase))
                {
                    spec = AscensionSpec.Max;
                    return true;
                }
                if (s.Length > 0 && s.All(char.IsAsciiDigit) && int.TryParse(s, out int fromText))
                {
                    spec = AscensionSpec.Of(fromText);
                    return true;
                }
                return false;
            case JsonValueKind.Number:
                if (e.TryGetInt32(out int level) && level >= 0)
                {
                    spec = AscensionSpec.Of(level);
                    return true;
                }
                return false;
            default:
                return false;
        }
    }
}
