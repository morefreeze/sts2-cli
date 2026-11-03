// src/Sts2Advisor/LocSwap.cs
using System.Globalization;
using System.Reflection;
using MegaCrit.Sts2.Core.Localization;

namespace Sts2Advisor;

/// <summary>Points LocManager at another language's tables for a synchronous stretch of main-thread code, so the game's
/// own formatting (LocString.GetFormattedText: SmartFormat, DynamicVars, plural / diff() / energyIcons() formatters,
/// the language's CultureInfo) produces that language's text. The tables are loaded once, the way the game loads a
/// language (LocManager.LoadTablesFromPath reads res://localization/&lt;lang&gt;, English underneath as the fallback of
/// any missing key). Nothing the game listens to changes: SetLanguage and its locale-change callbacks are not used.
/// Not reentrant; <see cref="GameText"/> nests the scopes. Kept apart so that a game update that moves one of these
/// members fails here (caught by GameText: text falls back) rather than in the code that merely wants a name.</summary>
internal static class LocSwap
{
    private sealed record Loaded(Dictionary<string, LocTable> Tables, CultureInfo Culture);

    private static readonly Dictionary<string, Loaded?> Cache = new();
    private static Dictionary<string, LocTable>? _savedTables;
    private static CultureInfo? _savedCulture;
    private static bool _active;

    /// <summary>true when the tables are swapped (call <see cref="Leave"/> after); false when the language could not be loaded.</summary>
    public static bool Enter(string language)
    {
        var loc = LocManager.Instance;
        if (loc == null || _active)
            return false;
        var loaded = Load(loc, language);
        if (loaded == null)
            return false;
        _savedTables = loc._tables;
        _savedCulture = loc.CultureInfo;
        SetCulture(loc, loaded.Culture);  // may throw: nothing is swapped yet
        loc._tables = loaded.Tables;
        _active = true;
        return true;
    }

    public static void Leave()
    {
        var loc = LocManager.Instance;
        if (!_active || loc == null)
            return;
        _active = false;
        if (_savedTables != null)
            loc._tables = _savedTables;
        if (_savedCulture != null)
            SetCulture(loc, _savedCulture);
        _savedTables = null;
        _savedCulture = null;
    }

    /// <summary>The swapped-in table lacks this key itself and only has it through its English fallback, with text in it
    /// (an entry that is empty in English too, like a status card's description, is not a missing translation).</summary>
    public static bool IsMissingTranslation(string table, string key) =>
        LocManager.Instance is { } loc && loc._tables.TryGetValue(table, out var t)
        && !t.IsLocalKey(key) && t.HasEntry(key) && !string.IsNullOrWhiteSpace(t.GetRawText(key));

    private static Loaded? Load(LocManager loc, string language)
    {
        if (Cache.TryGetValue(language, out var cached))
            return cached;
        Loaded? loaded = null;
        try
        {
            var (tables, _, _) = LocManager.LoadTablesFromPath(language);
            if (!tables.ContainsKey("cards") || !tables.ContainsKey("relics"))
                throw new InvalidOperationException($"language '{language}' has no cards/relics table");
            loaded = new Loaded(tables, loc.CultureInfoFromThreeLetterCode(language));
            ModLog.Info($"GameText: loaded the game's '{language}' tables ({tables.Count}) for the panel");
        }
        catch (Exception e)
        {
            ModLog.Error($"GameText: could not load the game's '{language}' tables: {e}");
        }
        Cache[language] = loaded;
        return loaded;
    }

    // LocManager.CultureInfo has a private setter (compiler-generated, so the publicizer leaves it private).
    private static readonly MethodInfo? CultureSetter =
        typeof(LocManager).GetProperty(nameof(LocManager.CultureInfo))?.GetSetMethod(nonPublic: true);

    private static void SetCulture(LocManager loc, CultureInfo culture)
    {
        if (CultureSetter == null)
            throw new MissingMethodException("LocManager.CultureInfo has no setter");
        CultureSetter.Invoke(loc, new object[] { culture });
    }
}
