namespace Sts2Advisor.Core;

/// <summary>config.json "panel_language": which language the panel speaks. Auto follows the game's own language.</summary>
public enum PanelLanguage { Auto, Zh, En }

/// <summary>Which language the panel (our own UI strings and the game's names/descriptions it quotes) uses, given the
/// config setting and the game's current language ("eng", "zhs", "zht", "fra" ... as LocManager.Language).</summary>
public static class PanelLanguageRules
{
    public const string GameEnglish = "eng";
    public const string GameChinese = "zhs";

    /// <summary>"auto" / "zh" / "en", case-insensitive, plus the obvious spellings (zhs, zh-CN, chinese, eng, english).
    /// Anything else (including a non-string JSON value, passed as null) is Auto.</summary>
    public static PanelLanguage Parse(string? raw)
    {
        switch (raw?.Trim().ToLowerInvariant())
        {
            case "zh": case "zhs": case "zh-cn": case "zh_cn": case "zh-hans": case "cn": case "chinese": case "中文":
                return PanelLanguage.Zh;
            case "en": case "eng": case "en-us": case "en_us": case "english": case "英文":
                return PanelLanguage.En;
            default:
                return PanelLanguage.Auto;
        }
    }

    public static bool IsChineseGame(string? gameLanguage) => gameLanguage is "zhs" or "zht";

    /// <summary>Does the panel's own text (buttons, status, rule explanations) come in Chinese?</summary>
    public static bool UsesChinese(PanelLanguage setting, string? gameLanguage) => setting switch
    {
        PanelLanguage.Zh => true,
        PanelLanguage.En => false,
        _ => IsChineseGame(gameLanguage),
    };

    /// <summary>The game language its names and descriptions must be read in when that is not the game's current
    /// one, else null (leave the game's own text). Zh asks for the official Simplified Chinese ("zhs") unless the game
    /// is already Chinese (Traditional included); En for English.</summary>
    public static string? NamesLanguage(PanelLanguage setting, string? gameLanguage) => setting switch
    {
        PanelLanguage.Zh => IsChineseGame(gameLanguage) ? null : GameChinese,
        PanelLanguage.En => gameLanguage == GameEnglish ? null : GameEnglish,
        _ => null,
    };

    /// <summary>The language whose font the panel needs: Chinese glyphs for a Chinese panel over a non-Chinese game,
    /// else the game's own (the game substitutes fonts per language).</summary>
    public static string? FontLanguage(PanelLanguage setting, string? gameLanguage) =>
        setting == PanelLanguage.Zh && !IsChineseGame(gameLanguage) ? GameChinese : gameLanguage;
}
