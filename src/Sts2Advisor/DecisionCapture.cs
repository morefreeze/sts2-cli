// src/Sts2Advisor/DecisionCapture.cs
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

internal enum CaptureKind { Advise, Combat, NoAdvice }

/// <summary>One look at the current screen. Build() produces the decision JSON (the headless
/// engine's shape, via the shared DecisionSerializer); it must run on the main thread, in the
/// same tick as Detect(). Choices lists the game objects behind the JSON's option indices.</summary>
internal sealed class Capture
{
    public required CaptureKind Kind { get; init; }
    public required string Screen { get; init; }
    public required DecisionHeader Header { get; init; }
    public string Fingerprint { get; init; } = "";
    public Dictionary<string, string> Labels { get; init; } = new();
    public Func<Dictionary<string, object?>>? Build { get; init; }

    /// <summary>The options in the order the decision JSON lists them, so an advised index finds its
    /// game object: CardModel (card_reward, card_select), EventOption, RestSiteOption, MapPoint,
    /// IReadOnlyList&lt;CardModel&gt; (bundle_select), MerchantEntry (shop cards: character then colorless).</summary>
    public IReadOnlyList<object> Choices { get; init; } = Array.Empty<object>();

    /// <summary>Shop only: relics, potions and the removal are found here by index.</summary>
    public MerchantInventory? Inventory { get; init; }
}

/// <summary>Which decision the player faces, read the way the game's own screen resolver
/// (ActiveScreenContext) and its AutoSlay autoplayer see it. Main thread only. Reads, never writes.</summary>
internal static class DecisionCapture
{
    /// <summary>Null outside a run (main menu, loading).</summary>
    public static Capture? Detect(LocLookup loc)
    {
        var rs = RunManager.Instance?.DebugOnlyGetState();
        if (rs == null)
            return null;
        var player = LocalContext.GetMe(rs) ?? rs.Players.FirstOrDefault();
        if (player == null)
            return null;
        // The shared builders hard-code Players[0]: single-player only.
        if (rs.Players.Count != 1)
            return Simple(CaptureKind.NoAdvice, "multiplayer", rs, player);
        if (CombatManager.Instance?.IsInProgress == true)
            return Simple(CaptureKind.Combat, "combat", rs, player);

        var screen = ActiveScreenContext.Instance?.GetCurrentScreen();
        return screen switch
        {
            null => null,
            NMapScreen map when map.IsOpen && map.IsTravelEnabled => Map(rs, player, loc),
            NCardRewardSelectionScreen s => CardReward(s, rs, player, loc),
            NCardGridSelectionScreen g => GridSelect(g, rs, player, loc),
            NChooseACardSelectionScreen c => CardSelect(nameof(NChooseACardSelectionScreen), c._cards,
                                                        c._canSkip ? 0 : 1, 1, rs, player, loc),
            NChooseABundleSelectionScreen b => Bundle(b, rs, player, loc),
            NEventRoom when rs.CurrentRoom is EventRoom er => Event(er, rs, player, loc),
            NRestSiteRoom when rs.CurrentRoom is RestSiteRoom rr => Rest(rr, rs, player, loc),
            NMerchantRoom or NMerchantInventory when rs.CurrentRoom is MerchantRoom mr => Shop(mr, rs, player, loc),
            _ => Simple(CaptureKind.NoAdvice, screen.GetType().Name, rs, player),
        };
    }

    private static DecisionHeader Header(string decision, RunState rs, Player p) =>
        new(decision, rs.CurrentActIndex + 1, rs.ActFloor, rs.AscensionLevel,
            p.Creature?.CurrentHp ?? 0, p.Creature?.MaxHp ?? 0, p.Gold);

    /// <summary>Progress, HP, gold, deck and belt size: any change re-asks the advisor.</summary>
    private static string Common(RunState rs, Player p) =>
        $"{rs.CurrentActIndex}/{rs.ActFloor}/{p.Creature?.CurrentHp}/{p.Creature?.MaxHp}/{p.Gold}/" +
        $"{p.Deck?.Cards?.Count}/{p.Potions?.Count()}";

    /// <summary>Adds the belt size to the decision's player: the shared serializer does not carry it
    /// (changing src/Sts2Shared needs a headless replay + regression), and greedy needs it because the
    /// belt shrinks at higher ascensions (Player.MaxPotionCount). Without it greedy assumes 3 slots and a
    /// shop buy the game refuses stops the autopilot. Also adds the top-level <c>ascension</c> to EVERY
    /// decision (rest_site's critical-heal threshold and the lookahead map policy scale by it; the shared
    /// serializer carries none).</summary>
    private static Func<Dictionary<string, object?>> WithPotionSlots(RunState rs, Player player, Func<Dictionary<string, object?>> build) =>
        () =>
        {
            var decision = build();
            if (decision.TryGetValue("player", out var p) && p is Dictionary<string, object?> playerDict)
                playerDict["potion_slots"] = player.MaxPotionCount;
            decision["ascension"] = (int)rs.AscensionLevel;
            return decision;
        };

    /// <summary>Adds <c>decision["map"]</c>: the whole act map in the shape of the headless engine's
    /// <c>get_map</c> reply (RunSimulator.GetFullMap), because the shared map_select decision carries only the
    /// next nodes and the lookahead map policy (STS2_MAP_POLICY=lookahead) plans the path to the boss.
    /// Mod-only on purpose (src/Sts2Shared stays byte-identical for the headless replay). A map that cannot be
    /// read just leaves the key out: the advisor then routes one step ahead, as before.</summary>
    private static Func<Dictionary<string, object?>> WithFullMap(RunState rs, LocLookup loc, Func<Dictionary<string, object?>> build) =>
        () =>
        {
            var decision = build();
            try
            {
                decision["map"] = FullMap(rs, loc);
            }
            catch (Exception e)
            {
                ModLog.Error($"reading the full map failed ({e.GetType().Name}: {e.Message}); advising one step ahead");
            }
            return decision;
        };

    private static Dictionary<string, object?> FullMap(RunState rs, LocLookup loc)
    {
        var map = rs.Map;
        var current = rs.CurrentMapCoord;
        var visited = rs.VisitedMapCoords;
        var rows = new List<List<Dictionary<string, object?>>>();
        for (int row = 0; row < map.GetRowCount(); row++)
        {
            var rowNodes = new List<Dictionary<string, object?>>();
            foreach (var point in map.GetPointsInRow(row))
            {
                if (point == null) continue;
                rowNodes.Add(new Dictionary<string, object?>
                {
                    ["col"] = (int)point.coord.col,
                    ["row"] = (int)point.coord.row,
                    ["type"] = point.PointType.ToString(),
                    ["children"] = point.Children?.Select(ch => new Dictionary<string, object?>
                    {
                        ["col"] = (int)ch.coord.col,
                        ["row"] = (int)ch.coord.row,
                    }).ToList(),
                    ["visited"] = visited?.Any(v => v.col == point.coord.col && v.row == point.coord.row) ?? false,
                    ["current"] = current.HasValue && current.Value.col == point.coord.col && current.Value.row == point.coord.row,
                });
            }
            if (rowNodes.Count > 0)
                rows.Add(rowNodes);
        }
        var boss = new Dictionary<string, object?>
        {
            ["col"] = (int)map.BossMapPoint.coord.col,
            ["row"] = (int)map.BossMapPoint.coord.row,
            ["type"] = map.BossMapPoint.PointType.ToString(),
        };
        var bossId = rs.Act?.BossEncounter?.Id?.Entry;
        if (!string.IsNullOrEmpty(bossId))
            boss["id"] = bossId;
        return new Dictionary<string, object?>
        {
            ["type"] = "map",
            ["context"] = DecisionSerializer.RunContext(rs, loc),
            ["rows"] = rows,
            ["boss"] = boss,
            ["current_coord"] = current.HasValue
                ? new Dictionary<string, object?> { ["col"] = (int)current.Value.col, ["row"] = (int)current.Value.row }
                : null,
        };
    }

    private static string CardKey(CardModel c) => c.Id.Entry + (c.IsUpgraded ? "+" + c.CurrentUpgradeLevel : "");

    private static Capture Simple(CaptureKind kind, string screen, RunState rs, Player p) =>
        new() { Kind = kind, Screen = screen, Header = Header(kind == CaptureKind.Combat ? "combat" : screen, rs, p) };

    private static Capture Map(RunState rs, Player player, LocLookup loc)
    {
        IEnumerable<MapPoint> next = rs.VisitedMapCoords.Count == 0
            ? new[] { rs.Map.StartingMapPoint }
            : MapTravel.GetTravelablePointsFrom(rs, rs.CurrentMapPoint!);
        var points = next.ToList();
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < points.Count; i++)
            labels[$"node:{i}"] = GameText.MapNode(points[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NMapScreen), Header = Header("map_select", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("map_select",
                new object?[] { Common(rs, player) }.Concat(points.Select(p => $"{p.coord.col},{p.coord.row}"))),
            Build = WithFullMap(rs, loc, WithPotionSlots(rs, player,
                () => DecisionSerializer.MapSelect(rs, points.Select(DecisionSerializer.MapChoice).ToList(), loc))),
            Choices = points.Cast<object>().ToList(),
        };
    }

    private static Capture CardReward(NCardRewardSelectionScreen s, RunState rs, Player player, LocLookup loc)
    {
        // The screen lingers for a moment after a pick or a skip: its choice is already made.
        if (s._completionSource?.Task.IsCompleted == true)
            return Simple(CaptureKind.NoAdvice, "card_reward_done", rs, player);
        var cards = s._options.Select(o => o.Card).ToList();
        bool canSkip = s._extraOptions.Any(a => string.Equals(a.OptionId, "Skip", StringComparison.OrdinalIgnoreCase));
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cards.Count; i++)
            labels[$"card:{i}"] = GameText.Card(cards[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NCardRewardSelectionScreen),
            Header = Header("card_reward", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("card_reward",
                new object?[] { Common(rs, player), canSkip }.Concat(cards.Select(CardKey))),
            // gold_earned: the real game gives no pre-combat gold snapshot; greedy does not read it.
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.CardReward(cards, canSkip, null, rs, loc)),
            Choices = cards.Cast<object>().ToList(),
        };
    }

    private static Capture GridSelect(NCardGridSelectionScreen g, RunState rs, Player player, LocLookup loc)
    {
        CardSelectorPrefs? prefs = g switch
        {
            NDeckUpgradeSelectScreen s => s._prefs,
            NDeckTransformSelectScreen s => s._prefs,
            NDeckEnchantSelectScreen s => s._prefs,
            NDeckCardSelectScreen s => s._prefs,
            NSimpleCardSelectScreen s => s._prefs,
            _ => null,  // NCombatPileCardSelectScreen: in combat, never reached here
        };
        if (prefs == null)
            return Simple(CaptureKind.NoAdvice, g.GetType().Name, rs, player);
        return CardSelect(g.GetType().Name, g._cards, prefs.Value.MinSelect, prefs.Value.MaxSelect, rs, player, loc);
    }

    private static Capture CardSelect(string screen, IReadOnlyList<CardModel> cards, int min, int max,
                                      RunState rs, Player player, LocLookup loc)
    {
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cards.Count; i++)
            labels[$"card:{i}"] = GameText.Card(cards[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = screen, Header = Header("card_select", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("card_select",
                new object?[] { screen, min, max, Common(rs, player) }.Concat(cards.Select(CardKey))),
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.CardSelect(cards, min, max, player, rs, loc)),
            Choices = cards.Cast<object>().ToList(),
        };
    }

    private static Capture Bundle(NChooseABundleSelectionScreen b, RunState rs, Player player, LocLookup loc)
    {
        var bundles = b._bundles;
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < bundles.Count; i++)
        {
            labels[$"bundle:{i}"] = GameText.Bundle(i + 1);
            for (int j = 0; j < bundles[i].Count; j++)
                labels[$"bundle_card:{i}:{j}"] = GameText.Card(bundles[i][j]);
        }
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NChooseABundleSelectionScreen),
            Header = Header("bundle_select", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("bundle_select",
                new object?[] { Common(rs, player) }.Concat(bundles.Select(x => string.Join(",", x.Select(CardKey))))),
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.BundleSelect(bundles, player, rs, loc)),
            Choices = bundles.Cast<object>().ToList(),
        };
    }

    private static Capture Event(EventRoom er, RunState rs, Player player, LocLookup loc)
    {
        var ev = er.LocalMutableEvent;
        var options = ev.CurrentOptions;
        if (ev.IsFinished || options.Count == 0)
            return Simple(CaptureKind.NoAdvice, "event_finished", rs, player);
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < options.Count; i++)
            labels[$"option:{i}"] = GameText.Option(options[i].Title, options[i].TextKey);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NEventRoom), Header = Header("event_choice", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("event_choice",
                new object?[] { Common(rs, player), ev.Id.Entry, ev.Description?.LocEntryKey }
                    .Concat(options.Select(o => $"{o.TextKey}#{o.IsLocked}#{o.WasChosen}"))),
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.EventChoice(ev, rs, loc)),
            Choices = options.Cast<object>().ToList(),
        };
    }

    private static Capture Rest(RestSiteRoom rr, RunState rs, Player player, LocLookup loc)
    {
        var options = rr.Options;
        if (options.Count == 0)
            return Simple(CaptureKind.NoAdvice, "rest_site_done", rs, player);
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < options.Count; i++)
            labels[$"rest:{i}"] = GameText.Option(options[i].Title, options[i].OptionId);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NRestSiteRoom), Header = Header("rest_site", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("rest_site",
                new object?[] { Common(rs, player) }.Concat(options.Select(o => $"{o.OptionId}#{o.IsEnabled}"))),
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.RestSite(options, rs, loc)),
            Choices = options.Cast<object>().ToList(),
        };
    }

    private static Capture Shop(MerchantRoom mr, RunState rs, Player player, LocLookup loc)
    {
        var inv = mr.GetLocalInventory();
        var cardEntries = inv.CharacterCardEntries.Concat(inv.ColorlessCardEntries).ToList();
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cardEntries.Count; i++)
            labels[$"card:{i}"] = cardEntries[i].CreationResult?.Card is { } c ? GameText.Card(c) : "—";
        for (int i = 0; i < inv.RelicEntries.Count; i++)
            labels[$"relic:{i}"] = GameText.Relic(inv.RelicEntries[i].Model);
        for (int i = 0; i < inv.PotionEntries.Count; i++)
            labels[$"potion:{i}"] = GameText.Potion(inv.PotionEntries[i].Model);
        var stock = cardEntries.Select(e => $"c{e.IsStocked}{e.Cost}")
            .Concat(inv.RelicEntries.Select(e => $"r{e.IsStocked}{e.Cost}"))
            .Concat(inv.PotionEntries.Select(e => $"p{e.IsStocked}{e.Cost}"))
            .Append($"x{inv.CardRemovalEntry?.IsStocked}{inv.CardRemovalEntry?.Cost}");
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NMerchantRoom), Header = Header("shop", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("shop", new object?[] { Common(rs, player) }.Concat(stock)),
            Build = WithPotionSlots(rs, player, () => DecisionSerializer.Shop(inv, player, rs, loc)),
            Choices = cardEntries.Cast<object>().ToList(), Inventory = inv,
        };
    }
}
