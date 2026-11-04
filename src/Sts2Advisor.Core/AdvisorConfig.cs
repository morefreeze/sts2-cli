using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>User settings, read from &lt;game user dir&gt;/Sts2Advisor/config.json
/// (written by tools/install_advisor_mod.sh / .ps1). Missing or unusable keys take the defaults.</summary>
public sealed record AdvisorConfig(string RepoPath, string Python, int Port, bool Autostart, string Hotkey,
                                   string AutopilotHotkey = AdvisorConfig.DefaultAutopilotHotkey,
                                   string StepHotkey = AdvisorConfig.DefaultStepHotkey,
                                   double StepPauseSeconds = AdvisorConfig.DefaultStepPauseSeconds,
                                   bool InstantMode = false,
                                   PanelLanguage PanelLanguage = PanelLanguage.Auto,
                                   string MapPolicy = AdvisorConfig.DefaultMapPolicy)
{
    public const string DefaultMapPolicy = "lookahead";
    public const string LookaheadMapPolicy = "lookahead";
    public const string HpAwareMapPolicy = "hpaware";

    public const int DefaultPort = 8767;
    public const string DefaultHotkey = "F8";
    public const string DefaultAutopilotHotkey = "F9";
    public const string DefaultStepHotkey = "F10";
    public const double DefaultStepPauseSeconds = 2.5;
    public const double MaxStepPauseSeconds = 10;

    public static AdvisorConfig Defaults { get; } = new("", "", DefaultPort, true, DefaultHotkey);

    /// <summary>"python" if set, else the repo's virtualenv interpreter: <c>.venv/bin/python</c> (macOS/Linux) if it exists,
    /// else <c>.venv/Scripts/python.exe</c> (Windows) if it exists, else the bin path (so the "cannot start" log names
    /// the conventional location); "" when neither is set.</summary>
    public string ResolvedPython
    {
        get
        {
            if (!string.IsNullOrWhiteSpace(Python)) return Python;
            if (string.IsNullOrWhiteSpace(RepoPath)) return "";
            string unix = Path.Combine(RepoPath, ".venv", "bin", "python");
            if (File.Exists(unix)) return unix;
            string windows = Path.Combine(RepoPath, ".venv", "Scripts", "python.exe");
            return File.Exists(windows) ? windows : unix;
        }
    }

    /// <summary>Defaults when the file is absent. Throws <see cref="JsonException"/> -- and only
    /// that -- when the file exists but is unusable: unreadable (the original exception is the
    /// inner one), not JSON, or JSON whose root is not an object. Inside a usable file, a key with
    /// the wrong type takes its default; "port" must be a JSON number in 1..65535 (else 8767) and
    /// "autostart" a boolean or the string "true"/"false" in any case (else true).
    /// "autopilot_hotkey" is a Godot Key name like "hotkey" (else "F9"); "step_hotkey" likewise (else "F10").
    /// "step_pause_seconds" -- how long the autopilot leaves a step on the panel before acting on the next screen --
    /// must be a JSON number, clamped to 0..10 (0 = no pause), else 2.5.
    /// "instant_mode" (apply the game's Instant speed once it has started) is read like "autostart" but defaults to false.
    /// "panel_language" is "auto" (follow the game's language; the default), "zh" or "en" -- see <see cref="PanelLanguageRules.Parse"/>;
    /// anything else, or a non-string, is "auto".
    /// "map_policy" is "lookahead" (the default since 2026-11-04: plan the whole path to the boss; the sidecar gets it
    /// as STS2_MAP_POLICY) or "hpaware" (route one step ahead) -- see <see cref="ParseMapPolicy"/>; a missing key,
    /// anything else, or a non-string, is "lookahead".</summary>
    public static AdvisorConfig Load(string path)
    {
        try
        {
            if (!File.Exists(path))
                return Defaults;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                throw new JsonException($"the root must be a JSON object, not {root.ValueKind}");
            return new AdvisorConfig(
                Str(root, "repo_path") ?? Defaults.RepoPath,
                Str(root, "python") ?? Defaults.Python,
                ReadPort(root),
                ReadBool(root, "autostart", Defaults.Autostart),
                Str(root, "hotkey") ?? DefaultHotkey,
                Str(root, "autopilot_hotkey") ?? DefaultAutopilotHotkey,
                Str(root, "step_hotkey") ?? DefaultStepHotkey,
                ReadStepPause(root),
                ReadBool(root, "instant_mode", false),
                PanelLanguageRules.Parse(Str(root, "panel_language")),
                ParseMapPolicy(Str(root, "map_policy")));
        }
        catch (Exception e)
        {
            throw new JsonException($"unusable config file {path}: {e.Message}", e);
        }
    }

    /// <summary>"hpaware" (case-insensitive, trimmed) is the one-step opt-out; anything else, null included, is the
    /// default "lookahead" (which also covers "look-ahead"/"look_ahead"). Never throws: a typo must not break the mod.</summary>
    public static string ParseMapPolicy(string? raw) =>
        raw?.Trim().ToLowerInvariant() is "hpaware"
            ? HpAwareMapPolicy : DefaultMapPolicy;

    private static int ReadPort(JsonElement root) =>
        root.TryGetProperty("port", out var p) && p.ValueKind == JsonValueKind.Number
        && p.TryGetInt32(out var port) && port is >= 1 and <= 65535
            ? port : DefaultPort;

    private static double ReadStepPause(JsonElement root) =>
        root.TryGetProperty("step_pause_seconds", out var p) && p.ValueKind == JsonValueKind.Number
        && p.TryGetDouble(out var seconds) && double.IsFinite(seconds)
            ? Math.Clamp(seconds, 0, MaxStepPauseSeconds) : DefaultStepPauseSeconds;

    private static bool ReadBool(JsonElement root, string name, bool fallback)
    {
        if (!root.TryGetProperty(name, out var a))
            return fallback;
        return a.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.String when bool.TryParse(a.GetString(), out var parsed) => parsed,
            _ => fallback,
        };
    }

    private static string? Str(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
}
