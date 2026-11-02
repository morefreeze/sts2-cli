using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Sts2Advisor.Core;

/// <summary>Panel text for greedy trace keys. One entry per agent/greedy_trace.py TRACE_KEYS key
/// (tests/test_advisor_trace_text_keys.py keeps them in step). Rule templates take
/// {name} = a rule value and {name:%} = a ratio shown as a percentage; component
/// entries are just the label.</summary>
public static class TraceText
{
    public static readonly IReadOnlyDictionary<string, (string Zh, string En)> Templates =
        new Dictionary<string, (string Zh, string En)>
    {
        ["greedy.randomized"] = ("随机决策（STS2_RANDOMIZE 已设置）", "Random pick (STS2_RANDOMIZE is set)"),
        ["greedy.decision_advisor"] = ("由 DecisionAdvisor 决定（STS2_DECISION_ADVISOR 已设置）", "Picked by DecisionAdvisor (STS2_DECISION_ADVISOR is set)"),
        ["greedy.unhandled"] = ("greedy 没有处理这个决策", "greedy does not handle this decision"),

        ["card_reward.no_cards"] = ("没有可选的卡", "No cards offered"),
        ["card_reward.all_filtered"] = ("全部被阶段1后期质量门过滤 → 跳过", "All filtered by the late Act 1 quality gate -> skip"),
        ["card_reward.all_broken"] = ("全部是不可用的卡 → 跳过", "Every offered card is unusable -> skip"),
        ["card_reward.below_threshold"] = ("最高分 {best_score} < 跳过阈值 {threshold}（阶段{act}，牌组 {deck_size} 张）", "Best {best_score} < skip threshold {threshold} (act {act}, deck {deck_size})"),
        ["card_reward.pick_best"] = ("最高分 {best_score} ≥ 跳过阈值 {threshold}（阶段{act}，牌组 {deck_size} 张）", "Best {best_score} >= skip threshold {threshold} (act {act}, deck {deck_size})"),

        ["card.base"] = ("卡牌分", "Card score"),
        ["card.set_bonus"] = ("组合预测", "Set predictor"),
        ["card.tag_synergy"] = ("标签协同", "Tag synergy"),
        ["card.combat_type"] = ("战斗类型", "Combat mix"),
        ["card.boss_defense_gap"] = ("格挡缺口", "Block gap"),
        ["card.boss_readiness"] = ("Boss 准备度", "Boss readiness"),
        ["card.quality_lift"] = ("牌组质量", "Deck quality"),
        ["card.archetype"] = ("流派", "Archetype"),
        ["card.q_adjust"] = ("历史表现", "Track record"),
        ["card.score"] = ("卡牌分（不含牌组）", "Card score (deck-free)"),
        ["card.in_deck_score"] = ("卡牌分（含牌组协同）", "Card score (with deck synergy)"),

        ["rest.critical_heal"] = ("HP {hp_ratio:%}，必须休息线 {threshold:%}", "HP {hp_ratio:%}, must-rest line {threshold:%}"),
        ["rest.vantom_mid_boss"] = ("墨影幻灵规则：第 11–13 层且 HP < 70% 时休息（现第 {floor} 层，HP {hp_ratio:%}）", "Vantom rule: rest on floors 11-13 when HP < 70% (now floor {floor}, HP {hp_ratio:%})"),
        ["rest.pre_boss_heal"] = ("第 14 层起（Boss 前）：HP {hp_ratio:%}，休息阈值 {threshold:%}", "Floor 14+ (pre-boss): HP {hp_ratio:%}, rest threshold {threshold:%}"),
        ["rest.must_upgrade"] = ("有分数 ≥ {threshold} 的未升级卡时锻造", "Smith when an unupgraded card scores >= {threshold}"),
        ["rest.heal_threshold"] = ("HP {hp_ratio:%}，本层休息阈值 {threshold:%}", "HP {hp_ratio:%}, rest threshold here {threshold:%}"),
        ["rest.default_smith"] = ("默认：能锻造就锻造", "Default: Smith if available"),
        ["rest.fallback"] = ("没有适用的规则：能休息就休息，否则选第一个可用项", "No rule applied: Rest if possible, else the first available option"),
        ["rest.no_choice"] = ("没有可选项", "Nothing to choose"),

        ["event.pick_best"] = ("选分数最高的选项", "Highest-scoring option"),
        ["event.no_available"] = ("所有选项都已锁定 → 离开", "Every option is locked -> leave"),
        ["event.kw.lose_max_hp"] = ("失去最大生命值", "Lose Max HP"),
        ["event.kw.curse"] = ("诅咒", "Curse"),
        ["event.kw.lose_all_gold"] = ("失去全部金币", "Lose all gold"),
        ["event.kw.lose_gold"] = ("失去金币", "Lose gold"),
        ["event.kw.torment"] = ("涅奥的苦痛", "Neow's Torment"),
        ["event.kw.take_damage"] = ("受到伤害", "Take damage"),
        ["event.kw.lose_hp"] = ("失去生命", "Lose HP"),
        ["event.kw.downgrade"] = ("降级卡牌", "Downgrade a card"),
        ["event.kw.add_basic"] = ("加入打击或防御", "Add a Strike or Defend"),
        ["event.kw.rare_card"] = ("稀有卡", "Rare card"),
        ["event.kw.uncommon_card"] = ("罕见卡", "Uncommon card"),
        ["event.kw.card_reward"] = ("获得卡牌", "Card reward"),
        ["event.kw.remove_card"] = ("移除卡牌", "Remove a card"),
        ["event.kw.remove_card_hp_cost"] = ("移除卡牌要失去生命", "Removal costs HP"),
        ["event.kw.relic"] = ("遗物", "Relic"),
        ["event.kw.relic_with_downside"] = ("有代价的遗物", "Relic with a downside"),
        ["event.kw.upgrade"] = ("升级", "Upgrade"),
        ["event.kw.transform"] = ("变化", "Transform"),
        ["event.kw.gain_gold"] = ("获得金币", "Gain gold"),
        ["event.kw.gain_max_hp"] = ("获得最大生命值", "Gain Max HP"),
        ["event.kw.potion"] = ("药水", "Potion"),
        ["event.kw.heal"] = ("回复生命", "Heal"),
        ["event.kw.colorless_card"] = ("无色卡", "Colorless card"),
        ["event.kw.gain_strength"] = ("获得力量", "Gain Strength"),
        ["event.kw.enemy_strength"] = ("敌人获得力量", "Enemies gain Strength"),
        ["event.kw.gain_dexterity"] = ("获得敏捷", "Gain Dexterity"),
        ["event.kw.energy_each_turn"] = ("每回合能量", "Energy every turn"),
        ["event.kw.energy_once"] = ("一次性能量", "One-time energy"),

        ["bundle.single"] = ("只有一包", "Only one bundle"),
        ["bundle.pick_best"] = ("选总分最高的一包", "Highest total score"),
        ["bundle.card"] = ("包内卡", "Card"),

        // rest_best / combat_best always select exactly one card, whatever max_select says.
        ["card_select.rest_best"] = ("休息处锻造：选分数最高的卡", "Rest site Smith: highest-scoring card"),
        ["card_select.combat_best"] = ("选分数最高的卡", "Highest-scoring card"),
        ["card_select.top_scores"] = ("挑高分：选分数最高的 {max_select} 张（没有低于 3 分的卡，共 {n_cards} 张）", "Keep the best {max_select} (no card below 3, {n_cards} offered)"),
        ["card_select.lowest_scores"] = ("挑低分：选分数最低的 {max_select} 张（有低于 3 分的卡，或超过 10 张）", "Pick the worst {max_select} (a card below 3, or more than 10 offered)"),
        ["card_select.no_cards"] = ("没有可选的卡 → 跳过", "No cards -> skip"),

        // "needs X (now Y)": a skipped rule must not read as a true comparison.
        ["shop.heal_potion"] = ("回复药水：需 HP < 50%（现 {hp_ratio:%}）且持有药水 < {potion_slots}（现 {held_potions}/{potion_slots}）", "Heal potion: needs HP < 50% (now {hp_ratio:%}) and potions < {potion_slots} (now {held_potions}/{potion_slots})"),
        ["shop.remove_basic"] = ("移除基础牌：{n_basic} 张，需要 ≥ 3 张（{basic_scope}）", "Remove a basic card: {n_basic}, needs >= 3 ({basic_scope})"),
        ["shop.remove_value"] = ("移除卡牌价值 {removal_value}，最佳卡 {best_card_score}", "Card removal value {removal_value}, best card {best_card_score}"),
        ["shop.card_must_buy"] = ("最佳卡 {best_card_score}，必买线 {threshold}", "Best card {best_card_score}, must-buy line {threshold}"),
        ["shop.card_threshold"] = ("最佳卡 {best_card_score}，买卡阈值 {threshold}（阶段{act}）", "Best card {best_card_score}, buy threshold {threshold} (act {act})"),
        ["shop.relic"] = ("最佳遗物 {best_relic_score}，阈值 {threshold}，至少留 {gold_reserve} 金币", "Best relic {best_relic_score}, threshold {threshold}, keep {gold_reserve} gold"),
        ["shop.potion"] = ("最佳药水 {best_potion_score}，阈值 {threshold}，持有药水 {held_potions}/{potion_slots}", "Best potion {best_potion_score}, threshold {threshold}, held potions {held_potions}/{potion_slots}"),
        ["shop.leave"] = ("没有值得买的 → 离开", "Nothing worth buying -> leave"),
        ["shop.relic_score"] = ("遗物分", "Relic score"),
        ["shop.potion_score"] = ("药水分", "Potion score"),

        ["map.pick_min_priority"] = ("选优先级数值最小的节点（越小越好）", "Lowest priority number wins (lower is better)"),
        ["map.base_priority"] = ("房间类型", "Room type"),
        ["map.shop_low_gold"] = ("金币不够", "Not enough gold"),
        ["map.elite_early_floor"] = ("本阶段前两层", "First two floors of the act"),
        ["map.elite_hp_danger"] = ("HP < 40%", "HP < 40%"),
        ["map.elite_hp_low"] = ("HP < 60%", "HP < 60%"),
        ["map.elite_act2_healthy"] = ("阶段2起 HP ≥ 90%", "Act 2+, HP >= 90%"),
        ["map.elite_act2"] = ("阶段2起的精英", "Act 2+ elite"),
        ["map.elite_strong_midgame"] = ("HP ≥ 85% 且第 7–12 层", "HP >= 85%, floors 7-12"),
        ["map.elite_pre_boss"] = ("第 13 层起（Boss 前）", "Floor 13+ (pre-boss)"),
        ["map.elite_act2_boss_zone"] = ("阶段2起，第 13 层起", "Act 2+, floor 13+"),
        ["map.la_best_path"] = ("整条路线评估（共 {n_paths} 条路线）：{path}，{elites} 个精英，到 Boss 时 HP {hp_boss_ratio:%}；差别主要在{decider}", "Whole-path lookahead ({n_paths} routes): {path}, {elites} elites, HP at the boss {hp_boss_ratio:%}; decided by {decider}"),
        ["map.la_fallback"] = ("无法按整条路线评估（{reason}），改用优先级规则", "Cannot look ahead ({reason}); using the priority rules"),
        ["map.la_room_reward"] = ("房间收益", "Room rewards"),
        ["map.la_elite_reward"] = ("精英遗物收益", "Elite relic reward"),
        ["map.la_elite_danger"] = ("精英战死亡风险", "Elite fight death risk"),
        ["map.la_fight_danger"] = ("普通战斗死亡风险", "Normal fight death risk"),
        ["map.la_boss_danger"] = ("进 Boss 时 HP 不足的风险", "Boss entry death risk"),
        ["map.la_hp_end"] = ("到 Boss 时的 HP", "HP at the boss"),
    };

    /// <summary>Rule values that are words, not numbers.</summary>
    private static readonly Dictionary<string, (string Zh, string En)> ValueWords = new()
    {
        ["ironclad_only"] = ("只统计铁甲战士的打击和防御，实测配置", "Ironclad's Strike/Defend only, the measured config"),
        ["all"] = ("统计全部角色的打击和防御", "every character's Strike/Defend"),
        // map.la_best_path `decider` (the route score term that separates the pick from the runner-up)
        ["room_reward"] = ("房间收益", "room rewards"),
        ["elite_reward"] = ("精英遗物收益", "the elite relic reward"),
        ["elite_danger"] = ("精英战死亡风险", "elite fight death risk"),
        ["fight_danger"] = ("普通战斗死亡风险", "normal fight death risk"),
        ["boss_danger"] = ("进 Boss 时 HP 不足的风险", "boss entry death risk"),
        ["hp_end"] = ("到 Boss 时的 HP", "HP at the boss"),
        ["no_gap"] = ("没有明显差别", "no clear gap"),
        ["tied"] = ("并列（按优先级）", "a tie (priority decides)"),
        // map.la_fallback `reason`
        ["no_map"] = ("没有地图数据", "no map data"),
        ["choice_not_on_map"] = ("选项不在地图上", "choice not on the map"),
        ["too_many_paths"] = ("路线太多", "too many routes"),
    };

    /// <summary>Components shown without a sign: they are a score, not an adjustment.</summary>
    private static readonly HashSet<string> Unsigned = new()
    {
        "card.base", "card.score", "card.in_deck_score", "shop.relic_score", "shop.potion_score",
        "map.base_priority", "bundle.card",
    };

    /// <summary>Rest-site rules whose `available` value says whether the option they would pick exists.</summary>
    private static readonly HashSet<string> RestOptionRules = new()
    {
        "rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
        "rest.heal_threshold", "rest.must_upgrade",
    };

    /// <summary>Rule values that are scores, shown in the same format as the option scores
    /// (so "5.45 &lt; 5.5" is never rounded into "5.5 &lt; 5.5"). Everything else is a count or an id.</summary>
    private static readonly HashSet<string> ScoreValues = new()
    {
        "best_score", "best_card_score", "removal_value", "best_relic_score", "best_potion_score", "threshold",
    };

    /// <summary>rest.pre_boss_heal below its floor: the recorded threshold is the current floor's, not the
    /// pre-boss one, so the numbers are left out.</summary>
    private const int PreBossFloor = 14;

    /// <summary>Rule values an older greedy did not record. Before `potion_slots` greedy assumed a belt of 3, so a
    /// trace without it (a sidecar started before the change) still reads right.</summary>
    private static readonly Dictionary<string, double> ValueDefaults = new() { ["potion_slots"] = 3 };

    private static readonly Regex Placeholder = new(@"\{(\w+)(:%)?\}");

    public static string Rule(TraceRule rule, bool zh)
    {
        if (rule.Key == "rest.pre_boss_heal"
            && rule.Values.TryGetValue("floor", out var floor) && floor.ValueKind == JsonValueKind.Number
            && floor.GetDouble() < PreBossFloor)
            return (zh ? "第 14 层起（Boss 前）休息" : "Floor 14+ (pre-boss): rest") + RuleSuffix(rule, zh);
        if (!Templates.TryGetValue(rule.Key, out var t))
            return rule.Values.Count == 0
                ? rule.Key
                : rule.Key + " " + string.Join(" ", rule.Values.Select(kv => $"{kv.Key}={Value(kv.Key, kv.Value, false, zh)}"));
        return Placeholder.Replace(zh ? t.Zh : t.En, m =>
            rule.Values.TryGetValue(m.Groups[1].Value, out var v) ? Value(m.Groups[1].Value, v, m.Groups[2].Success, zh)
            : ValueDefaults.TryGetValue(m.Groups[1].Value, out var d) ? d.ToString("0.##", CultureInfo.InvariantCulture)
            : "—")
            + RuleSuffix(rule, zh);
    }

    /// <summary>Rules the panel leaves out: the Vantom rest rule is noise while its env switch is off, and
    /// when the next boss is not Vantom.</summary>
    public static bool IsHidden(TraceRule rule) =>
        rule.Key == "rest.vantom_mid_boss"
        && (rule.Values.TryGetValue("enabled", out var enabled) && enabled.ValueKind == JsonValueKind.False
            || rule.Values.TryGetValue("is_vantom", out var isVantom) && isVantom.ValueKind == JsonValueKind.False);

    /// <summary>What a rule line adds when its values show a special case; "" otherwise.</summary>
    public static string RuleSuffix(TraceRule rule, bool zh)
    {
        var values = rule.Values;
        bool Is(string name, JsonValueKind kind) => values.TryGetValue(name, out var v) && v.ValueKind == kind;

        if (RestOptionRules.Contains(rule.Key))
            return Is("available", JsonValueKind.False) ? (zh ? "（没有该选项）" : " (option unavailable)") : "";

        switch (rule.Key)
        {
            case "shop.heal_potion":
                return Is("available", JsonValueKind.False) && Is("slots_free", JsonValueKind.True)
                       && values.TryGetValue("hp_ratio", out var hp) && hp.ValueKind == JsonValueKind.Number
                       && hp.GetDouble() < 0.5
                    ? (zh ? "（没有可买的回复药水）" : " (no heal potion for sale)") : "";
            case "card_select.rest_best":
            case "card_select.combat_best":
                return Is("fallback", JsonValueKind.True)
                    ? (zh ? "（没有达标的卡，默认第 1 张）" : " (no card qualified; defaulted to the first)") : "";
            case "card_reward.pick_best":
                return Is("gate_fallback", JsonValueKind.True)
                    ? (zh ? "（质量门过滤了全部且不能跳过，改从全部卡中选）"
                          : " (quality gate filtered all and skip is not allowed; picked from all)") : "";
            case "shop.remove_basic":
            case "shop.remove_value":
                // Say the gold is short only when that is why the rule was skipped: remove_basic needs 3 basic
                // cards first, and with fewer the gold is beside the point.
                if (rule.Key == "shop.remove_basic"
                    && !(values.TryGetValue("n_basic", out var basics) && basics.ValueKind == JsonValueKind.Number
                         && basics.GetDouble() >= 3))
                    return "";
                return values.TryGetValue("removal_cost", out var cost) && cost.ValueKind == JsonValueKind.Number
                       && values.TryGetValue("gold", out var gold) && gold.ValueKind == JsonValueKind.Number
                       && gold.GetDouble() < cost.GetDouble()
                    ? (zh ? $"（需 {Value("removal_cost", cost, false, zh)} 金币，现 {Value("gold", gold, false, zh)}）"
                          : $" (needs {Value("removal_cost", cost, false, zh)} gold, have {Value("gold", gold, false, zh)})") : "";
            case "map.pick_min_priority":
                return values.TryGetValue("tied_best", out var tied) && tied.ValueKind == JsonValueKind.Array
                       && tied.GetArrayLength() > 1
                    ? (zh ? "（并列，取列表中第一个）" : " (tie: first listed wins)") : "";
            default:
                return "";
        }
    }

    public static string Label(string key, bool zh) =>
        Templates.TryGetValue(key, out var t) ? (zh ? t.Zh : t.En) : key;

    /// <summary>"卡牌分（评级 A） 6.8" / "格挡缺口 +0.6".</summary>
    public static string Component(TraceComponent c, bool zh)
    {
        var detail = "";
        if (c.Key == "card.base" && c.Detail is { ValueKind: JsonValueKind.String } d && !string.IsNullOrEmpty(d.GetString()))
            detail = zh ? $"（评级 {d.GetString()}）" : $" (tier {d.GetString()})";
        var number = Unsigned.Contains(c.Key) ? Num(c.Value) : Signed(c.Value);
        return $"{Label(c.Key, zh)}{detail} {number}";
    }

    /// <summary>The one score format: one decimal, two when the value needs them ("5.0", "5.5", "5.45").
    /// Rounded away from zero first, so a tiny negative never prints as "-0.0".</summary>
    public static string Num(double v)
    {
        var rounded = Math.Round(v, 2, MidpointRounding.AwayFromZero);
        return (rounded == 0 ? 0.0 : rounded).ToString("0.0#", CultureInfo.InvariantCulture);
    }

    /// <summary>A ratio as a percentage, floored to one decimal: an HP of 49.99% reads "49.9%", never
    /// the "50%" threshold it is below.</summary>
    public static string Percent(double ratio) =>
        (Math.Floor(ratio * 1000 + 1e-6) / 10).ToString("0.#", CultureInfo.InvariantCulture) + "%";

    /// <summary>"+0.6" / "-1.9"; a value that rounds to zero is "+0.0", whatever its sign.</summary>
    public static string Signed(double v)
    {
        var n = Num(v);
        return n.StartsWith('-') ? n : "+" + n;
    }

    private static string Value(string name, JsonElement v, bool percent, bool zh) => v.ValueKind switch
    {
        JsonValueKind.Number when percent => Percent(v.GetDouble()),
        JsonValueKind.Number when ScoreValues.Contains(name) => Num(v.GetDouble()),
        JsonValueKind.Number => v.GetDouble().ToString("0.##", CultureInfo.InvariantCulture),
        JsonValueKind.String => ValueWords.TryGetValue(v.GetString()!, out var w) ? (zh ? w.Zh : w.En) : v.GetString()!,
        JsonValueKind.True => zh ? "是" : "yes",
        JsonValueKind.False => zh ? "否" : "no",
        JsonValueKind.Null => "—",
        JsonValueKind.Array => v.GetArrayLength() == 0
            ? "—"
            : string.Join(", ", v.EnumerateArray().Select(e => Value(name, e, false, zh))),
        _ => v.GetRawText(),
    };
}
