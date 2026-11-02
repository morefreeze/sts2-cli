// src/Sts2Advisor/Campaign/RunEndRecorder.cs
using Godot;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using Sts2Advisor.Core;

namespace Sts2Advisor.Campaign;

/// <summary>Collects the record of a run that has just ended, from the live RunState (still intact while the
/// game-over screen is up) and the history the game wrote in RunManager.OnEnded. Every field is read on its own: a
/// field that cannot be read is logged and left empty, the record is always written. Main thread only.</summary>
internal static class RunEndRecorder
{
    public static CampaignRunRecord Capture()
    {
        var rm = RunManager.Instance ?? throw new InvalidOperationException("no RunManager");
        var rs = rm.DebugOnlyGetState() ?? throw new InvalidOperationException("no run state");
        var player = LocalContext.GetMe(rs) ?? rs.Players.First();
        long startTime = Read("start time", () => rm._startTime, 0L);
        RunHistory? history = Read("run history", () => rm.History, null);
        // RunManager keeps the last run's history until the next run ends; only this run's counts.
        if (history != null && startTime != 0 && history.StartTime != startTime)
        {
            ModLog.Error($"campaign: run record: the game's history is from another run (start {history.StartTime}, this run {startTime}); not using it");
            history = null;
        }
        bool victory = history?.Win ?? Read("victory room", () => rs.CurrentRoom?.IsVictoryRoom ?? false, false);

        string? killedBy = null, killedByEvent = null;
        var monsters = new List<string>();
        if (!victory)
        {
            if (history != null && history.KilledByEncounter != ModelId.none)
                killedBy = history.KilledByEncounter.ToString();
            if (history != null && history.KilledByEvent != ModelId.none)
                killedByEvent = history.KilledByEvent.ToString();
            if (rs.CurrentRoom is CombatRoom combat)
            {
                killedBy ??= Read("encounter", () => combat.Encounter.Id.ToString(), null);
                monsters.AddRange(Read("monsters", () => combat.Encounter.MonstersWithSlots.Select(m => m.Item1.Id.ToString()).ToList(),
                                       new List<string>())!);
            }
        }

        // The Architect's WinRun kills the player to end the run: after a victory the live HP reads 0. The map point history
        // was updated (RunManager.UpdatePlayerStatsInMapPointHistory) just before that.
        var stats = victory ? Read("final stats", () => rs.CurrentMapPointHistoryEntry?.GetEntry(player.NetId), null) : null;

        return new CampaignRunRecord(
            Character: Read("character", () => player.Character.Id.Entry, "?")!,
            Ascension: Read("ascension", () => rs.AscensionLevel, 0),
            Victory: victory,
            Act: Read("act", () => rs.CurrentActIndex + 1, 0),
            Floor: Read("floor", () => rs.ActFloor, 0),
            TotalFloor: Read("total floor", () => rs.TotalFloor, 0),
            Hp: stats?.CurrentHp ?? Read("hp", () => player.Creature?.CurrentHp ?? 0, 0),
            MaxHp: stats?.MaxHp ?? Read("max hp", () => player.Creature?.MaxHp ?? 0, 0),
            Gold: stats?.CurrentGold ?? Read("gold", () => player.Gold, 0),
            Deck: Read("deck", () => player.Deck.Cards.Select(c => CampaignRunLog.CardId(c.Id.ToString(), c.CurrentUpgradeLevel)).ToList(),
                       new List<string>())!,
            Relics: Read("relics", () => player.Relics.Select(r => r.Id.ToString()).ToList(), new List<string>())!,
            Potions: Read("potions", () => player.Potions.Select(p => p.Id.ToString()).ToList(), new List<string>())!,
            RunTimeSeconds: history != null ? (long)history.RunTime : Read<long?>("run time", () => rm.RunTime, null),
            Seed: history?.Seed is { Length: > 0 } hs ? hs : Read("seed", () => rs.Rng.StringSeed, null),
            KilledBy: killedBy,
            KilledByEvent: killedByEvent,
            KilledByMonsters: monsters,
            HistoryPath: startTime == 0 ? null : Read("history path", () => HistoryPath(startTime), null),
            StartTime: startTime == 0 ? null : startTime);
    }

    /// <summary>Where RunManager.OnEnded -> RunHistoryUtilities.CreateRunHistoryEntry saved this run: the history
    /// folder of the current profile (the modded one while mods are loaded), &lt;StartTime&gt;.run.</summary>
    private static string HistoryPath(long startTime)
    {
        string scoped = SaveManager.Instance.GetProfileScopedPath($"saves/history/{startTime}.run");
        return ProjectSettings.GlobalizePath(scoped);
    }

    private static T Read<T>(string what, Func<T> get, T fallback)
    {
        try
        {
            return get();
        }
        catch (Exception e)
        {
            ModLog.Error($"campaign: run record: cannot read {what}: {e.GetType().Name}: {e.Message}");
            return fallback;
        }
    }
}
