// src/Sts2Advisor/GameText.cs
using System.Text.RegularExpressions;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Advisor;

/// <summary>Names as the game shows them, in its current language (official translations).
/// Main thread only. Never throws: a name that fails to resolve falls back to its id.</summary>
internal static class GameText
{
    private static readonly Regex Markup = new(@"\[/?[a-zA-Z_][a-zA-Z0-9_=#]*\]");

    public static string? Language => LocManager.Instance?.Language;

    public static bool IsChinese => Language is "zhs" or "zht";

    public static string Card(CardModel card) => Safe(() => card.Title, card.Id.Entry);

    public static string Relic(RelicModel? relic) =>
        relic == null ? "?" : Safe(() => relic.Title.GetFormattedText(), relic.Id.Entry);

    public static string Potion(PotionModel? potion) =>
        potion == null ? "?" : Safe(() => potion.Title.GetFormattedText(), potion.Id.Entry);

    public static string Loc(LocString? text, string fallback) =>
        text == null ? fallback : Safe(text.GetFormattedText, fallback);

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
        string name = key == "" ? point.PointType.ToString()
            : Safe(() => new LocString("map", key + ".title").GetFormattedText(), point.PointType.ToString());
        return $"{name} ({point.coord.col},{point.coord.row})";
    }

    private static string Safe(Func<string> get, string fallback)
    {
        try
        {
            var text = get();
            return string.IsNullOrWhiteSpace(text) ? fallback : Markup.Replace(text, "");
        }
        catch (Exception)
        {
            return fallback;
        }
    }
}
