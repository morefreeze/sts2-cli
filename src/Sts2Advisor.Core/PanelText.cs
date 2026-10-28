using System.Text.Json;

namespace Sts2Advisor.Core;

public enum LineStyle { Title, Pick, Normal, Fired, Muted, Warning }

public enum StatusKind { Thinking, Offline, Combat, NoAdvice, ReadFailed, Unsupported, ConfigMissing, BadReply }

public sealed record PanelLine(string Text, LineStyle Style);

/// <summary>What the mod read off the screen for the panel's title line.</summary>
public sealed record DecisionHeader(string Decision, int Act, int Floor, int Ascension, int Hp, int MaxHp, int Gold);

/// <summary>One advice reply -> the panel's lines. Labels are the game's own (localized) names,
/// keyed "card:3", "option:1", "rest:0", "node:2", "relic:0", "potion:1", "bundle:1",
/// "bundle_card:1:2" -- the decision JSON itself is English-only.</summary>
public static class PanelText
{
    /// <summary>Scored options listed per kind (cards, relics, potions, ...), the picked ones always on top of this.
    /// The cap is per kind so a shop's relic and potion picks are not pushed out by its cards.</summary>
    private const int MaxOptionsPerKind = 3;

    /// <summary>Components shown on an option's sub-line (the base score counts as one).</summary>
    private const int MaxComponents = 3;

    private static readonly Dictionary<string, (string Zh, string En)> DecisionNames = new()
    {
        ["card_reward"] = ("卡牌奖励", "Card reward"),
        ["rest_site"] = ("休息处", "Rest site"),
        ["event_choice"] = ("事件", "Event"),
        ["shop"] = ("商店", "Shop"),
        ["bundle_select"] = ("卡包选择", "Bundle choice"),
        ["card_select"] = ("选牌", "Card select"),
        ["map_select"] = ("地图", "Map"),
        ["combat"] = ("战斗", "Combat"),
        ["multiplayer"] = ("联机", "Multiplayer"),
    };

    private static readonly Dictionary<string, (string Zh, string En)> StatusNames = new()
    {
        ["filtered"] = ("已过滤（阶段1后期质量门）", "filtered (late Act 1 quality gate)"),
        ["locked"] = ("已锁定", "locked"),
        ["unstocked"] = ("已售出", "sold out"),
        ["unaffordable"] = ("买不起", "can't afford"),
        ["reserved"] = ("留作保底金（需留 25 金币）", "inside the 25-gold reserve"),
        ["broken"] = ("不可用", "unusable"),
    };

    private static string T(bool zh, string zhText, string enText) => zh ? zhText : enText;

    /// <summary>"卡牌奖励" / "Card reward"; the raw decision name when it has none.</summary>
    public static string DecisionName(string decision, bool zh) =>
        DecisionNames.TryGetValue(decision, out var n) ? (zh ? n.Zh : n.En) : decision;

    /// <summary>"卡牌奖励 · A1F13a1 · HP 52/80 · 金币 99". For a screen that is not a decision (the mod passes the
    /// screen class name, "event_finished", ...) there is no name to show, so the title starts at "A1F7a1".</summary>
    public static string Header(DecisionHeader h, bool zh)
    {
        var position = $"A{h.Act}F{h.Floor}a{h.Ascension} · HP {h.Hp}/{h.MaxHp} · {T(zh, "金币", "Gold")} {h.Gold}";
        return DecisionNames.TryGetValue(h.Decision, out var n) ? $"{(zh ? n.Zh : n.En)} · {position}" : position;
    }

    public static IReadOnlyList<PanelLine> Status(DecisionHeader? h, StatusKind kind, string? detail, bool zh)
    {
        var lines = new List<PanelLine>();
        if (h != null)
            lines.Add(new PanelLine(Header(h, zh), LineStyle.Title));
        lines.Add(kind switch
        {
            StatusKind.Thinking => new PanelLine(T(zh, "正在获取建议…", "Getting advice…"), LineStyle.Muted),
            StatusKind.Offline => new PanelLine(T(zh, "建议服务离线，正在重连", "Advice service offline, reconnecting"), LineStyle.Warning),
            StatusKind.Combat => new PanelLine(T(zh, "战斗中，交给 CombatSolver", "In combat: CombatSolver's job"), LineStyle.Muted),
            StatusKind.NoAdvice => new PanelLine(T(zh, "此处暂无建议", "No advice for this screen"), LineStyle.Muted),
            StatusKind.ReadFailed => new PanelLine(T(zh, "读取失败：", "Could not read: ") + detail, LineStyle.Warning),
            StatusKind.Unsupported => new PanelLine(T(zh, "游戏版本未适配，建议已停用", "Unsupported game version; advice is off"), LineStyle.Warning),
            StatusKind.ConfigMissing => new PanelLine(T(zh, "缺少配置：", "Missing config: ") + detail, LineStyle.Warning),
            StatusKind.BadReply => new PanelLine(T(zh, "建议服务回复格式不对（版本不匹配？）", "Advice service sent an unexpected reply (version mismatch?)"), LineStyle.Warning),
            _ => new PanelLine(kind.ToString(), LineStyle.Muted),
        });
        return lines;
    }

    public static IReadOnlyList<PanelLine> Render(DecisionHeader h, AdviceReply reply,
                                                  IReadOnlyDictionary<string, string> labels, bool zh)
    {
        var lines = new List<PanelLine> { new(Header(h, zh), LineStyle.Title) };
        if (reply.Action == null)
        {
            lines.Add(new PanelLine(T(zh, "无建议", "No advice"), LineStyle.Warning));
            lines.AddRange(reply.Warnings.Select(w => new PanelLine(w, LineStyle.Muted)));
            return lines;
        }

        lines.Add(new PanelLine(T(zh, "建议：", "Pick: ") + PickText(h.Decision, reply, labels, zh), LineStyle.Pick));

        // Lower is better on the map, and when a card_select picks the lowest cards (remove / transform).
        bool ascending = h.Decision == "map_select"
            || reply.Rules.Any(r => r.Key == "card_select.lowest_scores" && r.Outcome == "fired");
        var ok = Listed(reply, ascending);
        foreach (var o in ok)
        {
            lines.Add(new PanelLine($"{OptionLabel(o, labels)}  {TraceText.Num(o.Score!.Value)}",
                                    IsPicked(o, reply) ? LineStyle.Fired : LineStyle.Normal));
            // Event options are scored by keyword hits only; say so (or that none matched).
            bool isEventOption = o.Kind == "option";
            if (isEventOption && o.Components.Count == 0)
            {
                lines.Add(new PanelLine("  " + T(zh, "未命中关键词", "no keyword matched"), LineStyle.Muted));
                continue;
            }
            var parts = TopComponents(o, labels, zh);
            if (parts.Length > 0)
                lines.Add(new PanelLine("  " + (isEventOption ? T(zh, "关键词：", "Keywords: ") : "") + parts,
                                        LineStyle.Muted));
        }
        lines.AddRange(HiddenCounts(reply, ok, zh));
        // One line per reason: "买不起：A、B".
        foreach (var group in reply.Options.Where(o => o.Status != "ok").GroupBy(o => o.Status))
        {
            var status = StatusNames.TryGetValue(group.Key, out var s) ? (zh ? s.Zh : s.En) : group.Key;
            lines.Add(new PanelLine(status + T(zh, "：", ": ")
                + string.Join(T(zh, "、", ", "), group.Select(o => OptionLabel(o, labels))), LineStyle.Muted));
        }
        foreach (var r in reply.Rules.Where(r => !TraceText.IsHidden(r)))
            lines.Add(new PanelLine(TraceText.Rule(r, zh), r.Outcome == "fired" ? LineStyle.Fired : LineStyle.Muted));

        if (h.Decision == "event_choice")
            lines.Add(new PanelLine(T(zh, "按选项英文描述的关键词打分", "Scored by keywords in the option's English text"), LineStyle.Muted));
        if (h.Decision == "shop")
            lines.Add(new PanelLine(T(zh, "买完会重新评估下一步", "Re-evaluated after every purchase"), LineStyle.Muted));
        lines.Add(new PanelLine(reply.Unverified
            ? T(zh, "启发式，只看下一步，未经 A/B 验证", "Heuristic, next step only, not A/B tested")
            : T(zh, "greedy 启发式打分，不是概率", "greedy heuristic scores, not probabilities"), LineStyle.Muted));
        lines.AddRange(reply.Warnings.Select(w => new PanelLine(w, LineStyle.Warning)));
        return lines;
    }

    /// <summary>The scored options to list: per kind the best few (lowest few when ascending), plus every
    /// option the advice picks wherever it ranks. Cards first, then relics, potions, the rest.</summary>
    private static List<TraceOption> Listed(AdviceReply reply, bool ascending)
    {
        var ordered = reply.Options.Where(o => o.Status == "ok" && o.Score != null)
            .OrderBy(o => KindOrder(o.Kind))
            .ThenBy(o => ascending ? o.Score!.Value : -o.Score!.Value)
            .ToList();
        var listed = new List<TraceOption>();
        var perKind = new Dictionary<string, int>();
        foreach (var o in ordered)
        {
            perKind.TryGetValue(o.Kind, out var n);
            if (n >= MaxOptionsPerKind && !IsPicked(o, reply))
                continue;
            perKind[o.Kind] = n + 1;
            listed.Add(o);
        }
        return listed;
    }

    /// <summary>One muted line per kind for the scored options the per-kind cap left out.</summary>
    private static IEnumerable<PanelLine> HiddenCounts(AdviceReply reply, List<TraceOption> listed, bool zh)
    {
        foreach (var kind in reply.Options.Where(o => o.Status == "ok" && o.Score != null)
                     .GroupBy(o => o.Kind).OrderBy(g => KindOrder(g.Key)))
        {
            var hidden = kind.Count() - listed.Count(o => o.Kind == kind.Key);
            if (hidden > 0)
                yield return new PanelLine(MoreNotShown(kind.Key, hidden, zh), LineStyle.Muted);
        }
    }

    private static string MoreNotShown(string kind, int n, bool zh) => kind switch
    {
        "card" => zh ? $"另有 {n} 张卡未列出" : $"{n} more {(n == 1 ? "card" : "cards")} not shown",
        "relic" => zh ? $"另有 {n} 件遗物未列出" : $"{n} more {(n == 1 ? "relic" : "relics")} not shown",
        "potion" => zh ? $"另有 {n} 瓶药水未列出" : $"{n} more {(n == 1 ? "potion" : "potions")} not shown",
        _ => zh ? $"另有 {n} 个未列出" : $"{n} more not shown",
    };

    private static int KindOrder(string kind) => kind switch
    {
        "card" => 0, "relic" => 1, "potion" => 2, _ => 3,
    };

    private static string Label(IReadOnlyDictionary<string, string> labels, string key, string fallback) =>
        labels.TryGetValue(key, out var v) ? v : fallback;

    private static string OptionLabel(TraceOption o, IReadOnlyDictionary<string, string> labels) =>
        Label(labels, $"{o.Kind}:{o.Index}", $"#{o.Index}");

    private static string TopComponents(TraceOption o, IReadOnlyDictionary<string, string> labels, bool zh)
    {
        var shown = o.Components.Where(c => c.Key == "card.base" || c.Key == "map.base_priority").ToList();
        var others = o.Components
            .Where(c => c.Key != "card.base" && c.Key != "map.base_priority" && Math.Abs(c.Value) > 1e-9)
            .OrderByDescending(c => Math.Abs(c.Value))
            .ToList();
        var room = Math.Max(0, MaxComponents - shown.Count);
        shown.AddRange(others.Take(room));
        var text = string.Join(" · ", shown.Select(c =>
            c.Key == "bundle.card" && c.Detail is { ValueKind: JsonValueKind.Number } ci
                ? $"{Label(labels, $"bundle_card:{o.Index}:{ci.GetInt32()}", "?")} {TraceText.Num(c.Value)}"
                : TraceText.Component(c, zh)));
        // Say so when components were left out, so the shown ones are not mistaken for the whole sum.
        return others.Count > room && text.Length > 0 ? text + " …" : text;
    }

    private static bool IsPicked(TraceOption o, AdviceReply reply)
    {
        int? Arg(string name) => reply.Arg(name) is { ValueKind: JsonValueKind.Number } v ? v.GetInt32() : null;
        return (reply.ActionName, o.Kind) switch
        {
            ("select_card_reward", "card") or ("buy_card", "card") => Arg("card_index") == o.Index,
            ("choose_option", "option") => Arg("option_index") == o.Index,
            ("buy_relic", "relic") => Arg("relic_index") == o.Index,
            ("buy_potion", "potion") => Arg("potion_index") == o.Index,
            ("select_bundle", "bundle") => Arg("bundle_index") == o.Index,
            ("select_cards", "card") => SelectedIndices(reply).Contains(o.Index),
            ("select_map_node", "node") => o.Detail is { } d
                && d.TryGetProperty("col", out var col) && d.TryGetProperty("row", out var row)
                && col.GetInt32() == Arg("col") && row.GetInt32() == Arg("row"),
            _ => false,
        };
    }

    private static IReadOnlyList<int> SelectedIndices(AdviceReply reply) =>
        reply.Arg("indices") is { ValueKind: JsonValueKind.String } s
            ? s.GetString()!.Split(',', StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToList()
            : Array.Empty<int>();

    public static string PickText(string decision, AdviceReply reply, IReadOnlyDictionary<string, string> labels, bool zh)
    {
        int Index(string name) => reply.Arg(name) is { ValueKind: JsonValueKind.Number } v ? v.GetInt32() : -1;
        switch (reply.ActionName)
        {
            case "select_card_reward":
            case "buy_card":
                return Label(labels, $"card:{Index("card_index")}", "?");
            case "skip_card_reward":
            case "skip_select":
                return T(zh, "跳过", "Skip");
            case "choose_option":
                return Label(labels, $"{(decision == "rest_site" ? "rest" : "option")}:{Index("option_index")}", "?");
            case "buy_relic":
                return Label(labels, $"relic:{Index("relic_index")}", "?");
            case "buy_potion":
                return Label(labels, $"potion:{Index("potion_index")}", "?");
            case "remove_card":
                return T(zh, "移除卡牌", "Remove a card");
            case "leave_room":
                return T(zh, "离开", "Leave");
            case "select_bundle":
                return Label(labels, $"bundle:{Index("bundle_index")}", "?");
            case "select_cards":
                return string.Join(T(zh, "、", ", "),
                    SelectedIndices(reply).Select(i => Label(labels, $"card:{i}", $"#{i}")));
            case "select_map_node":
                var node = reply.Options.FirstOrDefault(o => o.Kind == "node" && IsPicked(o, reply));
                return node != null ? OptionLabel(node, labels) : $"({Index("col")},{Index("row")})";
            default:
                return reply.ActionName ?? "?";
        }
    }
}
