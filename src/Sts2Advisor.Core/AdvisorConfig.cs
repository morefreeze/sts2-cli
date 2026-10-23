using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>User settings, read from &lt;game user dir&gt;/Sts2Advisor/config.json
/// (written by tools/install_advisor_mod.sh). Missing keys take the defaults.</summary>
public sealed record AdvisorConfig(string RepoPath, string Python, int Port, bool Autostart, string Hotkey)
{
    public const int DefaultPort = 8767;
    public const string DefaultHotkey = "F8";

    public static AdvisorConfig Defaults { get; } = new("", "", DefaultPort, true, DefaultHotkey);

    /// <summary>"python" if set, else the repo's .venv/bin/python, else "".</summary>
    public string ResolvedPython =>
        !string.IsNullOrWhiteSpace(Python) ? Python
        : string.IsNullOrWhiteSpace(RepoPath) ? ""
        : Path.Combine(RepoPath, ".venv", "bin", "python");

    /// <summary>Defaults when the file is absent; throws JsonException when it is not JSON.</summary>
    public static AdvisorConfig Load(string path)
    {
        if (!File.Exists(path))
            return Defaults;
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var root = doc.RootElement;
        return new AdvisorConfig(
            Str(root, "repo_path") ?? Defaults.RepoPath,
            Str(root, "python") ?? Defaults.Python,
            root.TryGetProperty("port", out var p) && p.TryGetInt32(out var port) ? port : DefaultPort,
            root.TryGetProperty("autostart", out var a) && a.ValueKind is JsonValueKind.True or JsonValueKind.False
                ? a.GetBoolean() : Defaults.Autostart,
            Str(root, "hotkey") ?? DefaultHotkey);
    }

    private static string? Str(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
}
