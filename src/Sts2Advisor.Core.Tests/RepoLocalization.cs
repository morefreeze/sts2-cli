using System.Text.Json;

/// <summary>The repo's official localization tables (localization_eng / localization_zhs, extracted from the game),
/// found by walking up from the test binary. Tests that need them return early when the checkout has none.</summary>
internal static class RepoLocalization
{
    public static string? Dir { get; } = Find();

    private static string? Find()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            if (Directory.Exists(Path.Combine(d.FullName, "localization_zhs")) && Directory.Exists(Path.Combine(d.FullName, "localization_eng")))
                return d.FullName;
        return null;
    }

    public static Dictionary<string, string> Table(string language, string table)
    {
        var path = Path.Combine(Dir!, $"localization_{language}", table + ".json");
        return JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(path))!;
    }

    /// <summary>Every zhs string of every table, to look a word up.</summary>
    public static string ZhCorpus { get; } = Dir == null ? "" : string.Join("\n",
        Directory.GetFiles(Path.Combine(Dir, "localization_zhs"), "*.json")
            .SelectMany(f => JsonSerializer.Deserialize<Dictionary<string, string>>(File.ReadAllText(f))!.Values));
}
