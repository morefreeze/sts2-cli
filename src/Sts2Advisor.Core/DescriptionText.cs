// src/Sts2Advisor.Core/DescriptionText.cs
using System.Text;
using System.Text.RegularExpressions;

namespace Sts2Advisor.Core;

/// <summary>The game's card / relic / potion description (BBCode-flavoured, numbers already resolved by the game's
/// own LocString) -> one line of plain text for the panel. The panel escapes every "[" it is given, so the markup
/// could not render there anyway: tags are dropped, icons become words, the length is capped.</summary>
public static class DescriptionText
{
    public const int DefaultMaxChars = 140;

    // An icon run: "[img]res://.../x_energy_icon.png[/img]" repeated. EnergyIconsFormatter writes one icon per
    // point up to 3 and "N" + one icon from 4 up, so a digit (and the tags closing around it) may come first.
    private static readonly Regex IconRun = new(
        @"(?<pre>[0-9](?:\[/[^\[\]]*\])*)?(?<run>(?:\[img[^\]]*\][^\[]*\[/img\])+)", RegexOptions.CultureInvariant);
    private static readonly Regex Icon = new(@"\[img[^\]]*\]([^\[]*)\[/img\]", RegexOptions.CultureInvariant);
    private static readonly Regex Tag = new(@"\[/?[a-zA-Z_][^\[\]]*\]", RegexOptions.CultureInvariant);
    private static readonly Regex Spaces = new(@"\s+", RegexOptions.CultureInvariant);

    // A template variable the game's formatter left in ("{Damage:diff()}", also a nested "{Skills:choose(1):a|{:diff()}}"):
    // it returns the raw template when it fails.
    private static readonly Regex Unresolved = new(@"\{[A-Za-z_]", RegexOptions.CultureInvariant);

    /// <summary>The text still holds a template variable, i.e. the game could not format it.</summary>
    public static bool HasUnresolvedVariable(string? text) => !string.IsNullOrEmpty(text) && Unresolved.IsMatch(text);

    private enum IconKind { Unknown, Energy, Star }

    /// <summary>"Deal [blue]8[/blue] damage.\nExhaust." -> "Deal 8 damage. Exhaust."; at most
    /// <paramref name="maxChars"/> characters of visible text, an ellipsis in place of the cut part. Text that still
    /// holds a raw "{Var}" is dropped (empty): half a template is worse than none.</summary>
    public static string Plain(string? raw, bool zh, int maxChars = DefaultMaxChars)
    {
        if (string.IsNullOrWhiteSpace(raw) || maxChars < 1 || HasUnresolvedVariable(raw))
            return "";
        string s = IconRun.Replace(raw, m => IconText(m, zh));
        // [lb] / [rb] are BBCode's escaped brackets: literal in the result, so they are put back after the tags go.
        s = s.Replace("[lb]", "\u0001").Replace("[rb]", "\u0002");
        s = Tag.Replace(s, "");
        s = s.Replace('\u0001', '[').Replace('\u0002', ']');
        s = Spaces.Replace(s, " ").Trim();
        if (s.Length <= maxChars)
            return s;
        int cut = maxChars - 1;
        if (cut > 0 && char.IsHighSurrogate(s[cut - 1]))
            cut--;
        return s[..cut].TrimEnd() + "…";
    }

    private static string IconText(Match m, bool zh)
    {
        var kinds = Icon.Matches(m.Groups["run"].Value).Select(i => KindOf(i.Groups[1].Value)).ToList();
        var sb = new StringBuilder(m.Groups["pre"].Value);
        bool afterDigit = m.Groups["pre"].Success;
        for (int i = 0; i < kinds.Count;)
        {
            int j = i;
            while (j < kinds.Count && kinds[j] == kinds[i])
                j++;
            int n = j - i;
            string word = Word(kinds[i], afterDigit ? 2 : n, zh);
            if (word != "")
                sb.Append(afterDigit ? (zh ? "" : " ") + word : zh ? $"{n}{word}" : $"{n} {word}");
            afterDigit = false;
            i = j;
        }
        return sb.ToString();
    }

    private static IconKind KindOf(string path) =>
        path.Contains("energy", StringComparison.OrdinalIgnoreCase) ? IconKind.Energy
        : path.Contains("star", StringComparison.OrdinalIgnoreCase) ? IconKind.Star
        : IconKind.Unknown;

    // The game's own words (localization static_hover_tips ENERGY.title / STAR_COUNT.title).
    private static string Word(IconKind kind, int n, bool zh) => kind switch
    {
        IconKind.Energy => zh ? "能量" : "Energy",
        IconKind.Star => zh ? "辉星" : n == 1 ? "Star" : "Stars",
        _ => "",
    };
}
