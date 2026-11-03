// src/Sts2Advisor/GameText.cs
using System.Text.RegularExpressions;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

/// <summary>Names and descriptions as the game shows them, in the PANEL's language (config "panel_language"): by default
/// the game's current language, but "zh" over an English game still shows the official Simplified Chinese names and
/// descriptions, never our own translations. Main thread only. Never throws: a name that fails to resolve falls back
/// to its id.
///
/// How: the game's own formatter does the work. <see cref="Names"/> points LocManager at the target language's tables
/// (the ones the game itself loads from its pck, <see cref="LocSwap"/>) for the duration of a synchronous call, so
/// <c>card.Title</c>, <c>GetDescriptionForPile</c>, <c>DynamicDescription</c> ... resolve every variable, plural,
/// energy icon and highlight exactly as they do on screen, then puts the game's tables back. No UI callback fires.
/// If the tables cannot be swapped (a game update moved something), titles come from the repo's localization_zhs and
/// descriptions are left out ("title only"); a key missing from the target table shows the game's English text (the
/// table's own fallback) and is logged once.</summary>
internal static class GameText
{
    private static readonly Regex Markup = new(@"\[/?[a-zA-Z_][a-zA-Z0-9_=#]*\]");

    private static PanelLanguage _setting = PanelLanguage.Auto;
    private static LocLookup? _repoLoc;
    private static int _depth;
    private static bool _needsSwap, _swapped;
    private static readonly HashSet<string> Logged = new();

    public static void Configure(PanelLanguage setting, LocLookup? repoLoc)
    {
        _setting = setting;
        _repoLoc = repoLoc;
    }

    /// <summary>The game's own language ("eng", "zhs" ...).</summary>
    public static string? Language => LocManager.Instance?.Language;

    /// <summary>Does the panel speak Chinese? The one resolved setting every panel string follows.</summary>
    public static bool Zh => PanelLanguageRules.UsesChinese(_setting, Language);

    /// <summary>The language whose font the panel needs (see <see cref="PanelLanguageRules.FontLanguage"/>).</summary>
    public static string? FontLanguage => PanelLanguageRules.FontLanguage(_setting, Language);

    /// <summary>The tables are in the panel's language inside the scope, or no swap was needed.</summary>
    private static bool InPanelLanguage => !_needsSwap || _swapped;

    /// <summary>Reads inside the scope see the game's text in the panel's language. Nests; the outermost one swaps.</summary>
    public readonly struct NamesScope : IDisposable
    {
        public void Dispose() => Leave();
    }

    public static NamesScope Names()
    {
        Enter();
        return default;
    }

    private static void Enter()
    {
        if (_depth++ > 0)
            return;
        _needsSwap = _swapped = false;
        try
        {
            string? target = PanelLanguageRules.NamesLanguage(_setting, Language);
            if (target == null)
                return;
            _needsSwap = true;
            _swapped = LocSwap.Enter(target);
        }
        catch (Exception e)  // also a missing member: this is only text
        {
            _swapped = false;
            LogOnce("swap", $"cannot read the game's text in the panel language, falling back (logged once): {e}");
        }
    }

    private static void Leave()
    {
        if (--_depth > 0)
            return;
        _depth = 0;
        bool swapped = _swapped;
        _needsSwap = _swapped = false;
        if (!swapped)
            return;
        try
        {
            LocSwap.Leave();
        }
        catch (Exception e)
        {
            LogOnce("restore", $"could not put the game's tables back: {e}");
        }
    }

    public static string Card(CardModel card)
    {
        using var _ = Names();
        var key = card.TitleLocString;
        return Named(key.LocTable, key.LocEntryKey, card.Id.Entry, () => card.Title,
                     card.IsUpgraded ? (card.MaxUpgradeLevel > 1 ? $"+{card.CurrentUpgradeLevel}" : "+") : "");
    }

    public static string Relic(RelicModel? relic)
    {
        if (relic == null)
            return "?";
        using var _ = Names();
        var key = Safely(() => relic.Title, null);
        return key == null ? relic.Id.Entry
            : Named(key.LocTable, key.LocEntryKey, relic.Id.Entry, () => key.GetFormattedText(), "");
    }

    public static string Potion(PotionModel? potion)
    {
        if (potion == null)
            return "?";
        using var _ = Names();
        var key = potion.Title;
        return Named(key.LocTable, key.LocEntryKey, potion.Id.Entry, () => key.GetFormattedText(), "");
    }

    public static string Loc(LocString? text, string fallback)
    {
        if (text == null)
            return fallback;
        using var _ = Names();
        CheckLocal(text.LocTable, text.LocEntryKey);
        return Safe(text.GetFormattedText, fallback);
    }

    /// <summary>The game's description text for a model, BBCode and all (Core's DescriptionText makes it plain), read
    /// in the panel's language. Empty when it cannot be: the panel then shows the title alone. A template variable the
    /// game left unresolved counts as failure -- a raw "{Damage}" must never reach the panel.</summary>
    public static string Description(string table, string key, Func<string> get)
    {
        using var _ = Names();
        if (!InPanelLanguage)
            return "";
        CheckLocal(table, key);
        try
        {
            string text = get();
            if (DescriptionText.HasUnresolvedVariable(text))
            {
                LogOnce("unresolved:" + table + "." + key, $"description {table}.{key} still has a template variable after formatting; showing the title only: {text}");
                return "";
            }
            return text ?? "";
        }
        catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
        {
            LogOnce("describe:" + table + "." + key, $"could not format {table}.{key}; showing the title only: {e.Message}");
            return "";
        }
    }

    /// <summary>A campaign character id ("IRONCLAD") as the game names it ("铁甲战士"); the id itself unless the panel is Chinese.</summary>
    public static string Character(string id)
    {
        if (!Zh)
            return id;
        using var _ = Names();
        CheckLocal("characters", id + ".title");
        return Safe(() => new LocString("characters", id + ".title").GetFormattedText(), id);
    }

    /// <summary>"第 2 组": a bundle of cards (the game's own word for it is "组", CHOOSE_A_PACK).</summary>
    public static string Bundle(int number) => Zh ? $"第 {number} 组" : $"Bundle {number}";

    /// <summary>"精英 (3,4)": the map legend's own name for the room type.</summary>
    public static string MapNode(MapPoint point)
    {
        string key = point.PointType switch
        {
            MapPointType.Monster => "LEGEND_ENEMY",
            MapPointType.Elite => "LEGEND_ELITE",
            MapPointType.RestSite => "LEGEND_REST",
            MapPointType.Shop => "LEGEND_MERCHANT",
            MapPointType.Treasure => "LEGEND_TREASURE",
            MapPointType.Unknown => "LEGEND_UNKNOWN",
            MapPointType.Boss => "LEGEND_BOSS",
            MapPointType.Ancient => "LEGEND_ANCIENT",
            _ => "",
        };
        string name;
        if (key == "")
        {
            name = point.PointType.ToString();
        }
        else
        {
            using var _ = Names();
            CheckLocal("map", key + ".title");
            name = Safe(() => new LocString("map", key + ".title").GetFormattedText(), point.PointType.ToString());
        }
        return $"{name} ({point.coord.col},{point.coord.row})";
    }

    /// <summary>A model's name in the panel language: the game's formatter inside the scope, else (swap unavailable and
    /// the panel Chinese) the repo's zhs title, else whatever the game gives. <paramref name="suffix"/> is the upgrade
    /// mark the repo title lacks.</summary>
    private static string Named(string table, string key, string idEntry, Func<string> get, string suffix)
    {
        CheckLocal(table, key);
        if (!InPanelLanguage && RepoTitle(table, key) is { } repo)
            return repo + suffix;
        return Safe(get, idEntry);
    }

    private static string? RepoTitle(string table, string key)
    {
        if (PanelLanguageRules.NamesLanguage(_setting, Language) != PanelLanguageRules.GameChinese)
            return null;
        string? zh = _repoLoc?.Zh(table, key);
        return string.IsNullOrWhiteSpace(zh) || DescriptionText.HasUnresolvedVariable(zh) ? null : Markup.Replace(zh, "");
    }

    /// <summary>Inside a swapped scope: log once when the target language has no entry of its own (the game then shows
    /// its English text for that key).</summary>
    private static void CheckLocal(string table, string key)
    {
        if (!_swapped)
            return;
        try
        {
            if (LocSwap.IsMissingTranslation(table, key))
                LogOnce("missing:" + table + "." + key, $"no {PanelLanguageRules.NamesLanguage(_setting, Language)} entry for {table}.{key}; showing the game's fallback text");
        }
        catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
        {
            // the table is absent: GetFormattedText below fails and Safe() falls back to the id
        }
    }

    private static T? Safely<T>(Func<T> get, T? fallback)
    {
        try
        {
            return get();
        }
        catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
        {
            return fallback;
        }
    }

    private static string Safe(Func<string> get, string fallback)
    {
        try
        {
            var text = get();
            if (string.IsNullOrWhiteSpace(text) || DescriptionText.HasUnresolvedVariable(text))
                return fallback;
            return Markup.Replace(text, "");
        }
        catch (Exception)
        {
            return fallback;
        }
    }

    private static void LogOnce(string key, string message)
    {
        if (Logged.Add(key))
            ModLog.Error("GameText: " + message);
    }
}
