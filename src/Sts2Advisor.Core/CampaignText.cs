// src/Sts2Advisor.Core/CampaignText.cs
namespace Sts2Advisor.Core;

/// <param name="Enabled">The file turns the campaign on.</param>
/// <param name="Note">What the campaign is doing or waiting for right now (the plan's reason).</param>
/// <param name="FileProblem">Why campaign.json is unusable; shown even though no campaign runs.</param>
/// <param name="Stuck">Why the campaign gave up; it stays stopped until the file changes.</param>
public sealed record CampaignView(bool Enabled, string? Character, AscensionSpec? Ascension, int Runs, int Wins,
                                  string? Note, string? FileProblem, string? Stuck);

/// <summary>The one panel line of the campaign mode.</summary>
public static class CampaignText
{
    /// <summary>"a6", or "max" for the character's highest unlocked ascension.</summary>
    public static string Level(AscensionSpec? spec) => spec is { IsMax: false } s ? $"a{s.Level}" : "max";

    private static string T(bool zh, string zhText, string enText) => zh ? zhText : enText;

    /// <summary>Null when there is nothing to show (no campaign, no problem).</summary>
    public static PanelLine? Status(CampaignView v, bool zh)
    {
        if (v.FileProblem != null)
            return new PanelLine(T(zh, "战役：", "Campaign: ") + v.FileProblem, LineStyle.Warning);
        if (!v.Enabled)
            return null;
        if (v.Stuck != null)
            return new PanelLine(T(zh, $"战役已停止（卡住）：{v.Stuck} · 修改 campaign.json 后继续",
                                       $"Campaign STUCK: {v.Stuck} · edit campaign.json to resume"), LineStyle.Warning);

        string next = v.Character == null
            ? T(zh, "下一局：无", "next: none")
            : T(zh, $"下一局 {v.Character} {Level(v.Ascension)}", $"next {v.Character} {Level(v.Ascension)}");
        string session = T(zh, $"本次 {v.Runs} 局 {v.Wins} 胜",
                               $"{v.Runs} run{(v.Runs == 1 ? "" : "s")}, {v.Wins} win{(v.Wins == 1 ? "" : "s")}");
        string text = T(zh, "战役", "Campaign") + " · " + next + " · " + session;
        if (!string.IsNullOrEmpty(v.Note))
            text += " · " + v.Note;
        bool warn = v.Note != null && v.Note.StartsWith("needs attention", StringComparison.OrdinalIgnoreCase);
        return new PanelLine(text, warn ? LineStyle.Warning : LineStyle.Muted);
    }
}
