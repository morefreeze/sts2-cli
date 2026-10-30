// src/Sts2Advisor.Core/AutoPilotText.cs
namespace Sts2Advisor.Core;

public enum PanelAction { Step, Auto, Stop }

public sealed record PanelButton(PanelAction Action, string Text);

/// <summary>One thing a step chose, for the panel's description line: its name, the game's raw description text
/// (BBCode and all; <see cref="DescriptionText"/> makes it plain) and whether that is the card as it will be after
/// the upgrade being picked.</summary>
public sealed record DetailItem(string Name, string Raw, bool AfterUpgrade = false);

/// <summary>The panel while the autopilot runs, and the one-line banner after it stops.</summary>
public static class AutoPilotText
{
    private static string T(bool zh, string zhText, string enText) => zh ? zhText : enText;

    /// <summary>"卡牌奖励：痛击".</summary>
    public static string StepText(string decision, AdviceReply reply, IReadOnlyDictionary<string, string> labels, bool zh) =>
        PanelText.DecisionName(decision, zh) + T(zh, "：", ": ") + PanelText.PickText(decision, reply, labels, zh);

    /// <summary>The first fired, shown rule of the same reply; null when none fired.</summary>
    public static string? Reason(AdviceReply reply, bool zh) =>
        reply.Rules.FirstOrDefault(r => r.Outcome == "fired" && !TraceText.IsHidden(r)) is { } rule
            ? TraceText.Rule(rule, zh) : null;

    /// <summary>Items shown at most: a deck select or a bundle holds a few cards, never a screenful.</summary>
    public const int MaxDetailItems = 4;

    /// <summary>The newest step's description lines: the plain description when one thing was chosen (its name is
    /// already in the step text), "name: description" for each of several. Nothing when there is no description.</summary>
    public static IReadOnlyList<string> DetailLines(IReadOnlyList<DetailItem> items, bool zh)
    {
        var lines = new List<string>();
        foreach (var item in items.Take(MaxDetailItems))
        {
            string text = DescriptionText.Plain(item.Raw, zh);
            if (text != "" && item.AfterUpgrade)
                text = T(zh, "升级后：", "After upgrade: ") + text;
            if (items.Count == 1)
            {
                if (text != "")
                    lines.Add(text);
            }
            else
            {
                lines.Add(text == "" ? item.Name : item.Name + T(zh, "：", ": ") + text);
            }
        }
        return lines;
    }

    public static IReadOnlyList<PanelLine> Running(AutoPilotSession s, string autoKey, string stepKey, CombatSolverStatus combat, bool zh)
    {
        var lines = new List<PanelLine>
        {
            s.SingleStep
                ? new(T(zh, $"单步执行中 · {stepKey} 停止", $"Single step · {stepKey} to stop"), LineStyle.Title)
                : new(T(zh, $"全托管中 · {autoKey} 接管", $"Autopilot on · {autoKey} to take over"), LineStyle.Title),
        };
        if (combat != CombatSolverStatus.Ok)
            lines.Add(new PanelLine(CombatWarning(combat, zh), LineStyle.Warning));
        if (s.HandBackScreen != null)
            lines.Add(new PanelLine(T(zh, "请手动完成此界面，完成后自动继续",
                                          "Please finish this screen yourself; autopilot resumes after it"), LineStyle.Warning));
        if (s.InCombat)
            lines.Add(new PanelLine(T(zh, "战斗中（CombatSolver）", "In combat (CombatSolver)"), LineStyle.Muted));
        for (int i = 0; i < s.Steps.Count; i++)
        {
            var step = s.Steps[i];
            lines.Add(new PanelLine(step.Text, i == 0 ? LineStyle.Pick : LineStyle.Normal));
            if (i == 0)  // what was chosen, in the game's words; earlier steps keep the short form
                lines.AddRange(step.Details.Select(d => new PanelLine("  " + d, LineStyle.Muted)));
            if (step.Reason != null)
                lines.Add(new PanelLine("  " + step.Reason, LineStyle.Muted));
        }
        if (s.Steps.Count == 0 && s.HandBackScreen == null && !s.InCombat)
            lines.Add(new PanelLine(T(zh, "等待下一个决策…", "Waiting for the next decision…"), LineStyle.Muted));
        return lines;
    }

    public static PanelLine StopBanner(StopReason reason, string? detail, string autoKey, string stepKey, bool zh)
    {
        string text = T(zh, "全托管已停止：", "Autopilot stopped: ") + StopReasonText(reason, zh)
            + (string.IsNullOrEmpty(detail) ? "" : T(zh, "（", " (") + detail + T(zh, "）", ")"))
            + (reason == StopReason.StepDone
                ? T(zh, $" · {stepKey} 下一步", $" · {stepKey} for the next step")
                : T(zh, $" · {autoKey} 重新开启", $" · {autoKey} to restart"));
        return new PanelLine(text, reason is StopReason.Hotkey or StopReason.GameOver or StopReason.LeftRun or StopReason.StepDone
            ? LineStyle.Muted : LineStyle.Warning);
    }

    public static string StopReasonText(StopReason reason, bool zh) => reason switch
    {
        StopReason.Hotkey => T(zh, "玩家接管", "taken over by the player"),
        StopReason.GameOver => T(zh, "本局结束", "the run is over"),
        StopReason.LeftRun => T(zh, "已离开本局", "left the run"),
        StopReason.Multiplayer => T(zh, "联机不支持", "multiplayer is not supported"),
        StopReason.HandlerFailed => T(zh, "执行失败", "an action failed"),
        StopReason.Stuck => T(zh, "界面卡住", "the screen is stuck"),
        StopReason.Offline => T(zh, "建议服务离线超过 10 秒", "advice service offline for over 10 s"),
        StopReason.BadReply => T(zh, "建议服务回复格式不对", "unexpected reply from the advice service"),
        StopReason.Unsupported => T(zh, "游戏版本未适配", "unsupported game version"),
        StopReason.StepDone => T(zh, "单步完成", "step done"),
        StopReason.Disabled => T(zh, "全托管不可用", "autopilot unavailable"),
        _ => reason.ToString(),
    };

    /// <summary>The panel's buttons: "do this step" and "full auto" while advising; one "stop" while running.</summary>
    public static IReadOnlyList<PanelButton> Buttons(bool running, string stepKey, string autoKey, bool zh) =>
        running
            ? new[] { new PanelButton(PanelAction.Stop, T(zh, "停止", "Stop")) }
            : new[]
            {
                new PanelButton(PanelAction.Step, T(zh, $"执行这一步 ({stepKey})", $"Do this step ({stepKey})")),
                new PanelButton(PanelAction.Auto, T(zh, $"全自动 ({autoKey})", $"Full auto ({autoKey})")),
            };

    private static string CombatWarning(CombatSolverStatus status, bool zh) => status switch
    {
        CombatSolverStatus.Missing => T(zh, "没找到 CombatSolver 设置，战斗不会自动进行",
                                            "CombatSolver settings not found: combats will not play themselves"),
        CombatSolverStatus.FullAutoOff => T(zh, "CombatSolver 未开启“每场战斗开始时自动开启全自动”，战斗不会自动进行",
                                                "CombatSolver auto full-auto is off: combats will not play themselves"),
        _ => T(zh, "CombatSolver 设置读不了，战斗可能不会自动进行",
                   "CombatSolver settings unreadable: combats may not play themselves"),
    };
}
