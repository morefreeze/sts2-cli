// src/Sts2Advisor/CombatRecorder.cs
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Rooms;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>One <c>combat_end</c> line in advisor_trace.jsonl per combat, from CombatManager's CombatBegan /
/// TurnStarted / CombatWon / CombatEnded events (all raised on the main thread). Local player only. No handler may
/// throw into the game's event: each body is wrapped, and each field is read on its own so one bad read nulls just
/// that field. Every failure is logged once.</summary>
internal static class CombatRecorder
{
    private sealed class Start
    {
        public string? Encounter, RoomType;
        public int? Act, Floor, Hp, MaxHp, Block, Gold;
        public List<string>? Potions;
    }

    private static bool _subscribed;
    private static AdvisorTraceLog? _trace;
    private static Start? _start;
    private static bool _won;
    private static int _rounds;
    private static readonly HashSet<string> Logged = new();

    public static void Init(AdvisorTraceLog? trace)
    {
        _trace = trace;
        if (_subscribed) return;
        _subscribed = true;
        try
        {
            var cm = CombatManager.Instance;
            cm.CombatBegan += OnBegan;
            cm.TurnStarted += OnTurnStarted;
            cm.CombatWon += OnWon;
            cm.CombatEnded += OnEnded;
        }
        catch (Exception ex)
        {
            ModLog.Error($"combat recorder: cannot subscribe: {ex.GetType().Name}: {ex.Message}");
        }
    }

    private static void OnBegan(CombatState state)
    {
        try
        {
            _won = false;
            _rounds = Read("rounds", () => state.RoundNumber, 0);
            var s = new Start();
            s.Encounter = Read("encounter", () => state.Encounter?.Id.ToString(), null);
            s.RoomType = Read("room type", () => state.Encounter?.RoomType.ToString(), null);
            s.Act = Read<int?>("act", () => state.RunState.CurrentActIndex + 1, null);
            s.Floor = Read<int?>("floor", () => state.RunState.ActFloor, null);
            var player = Me(state);
            Vitals(player, out s.Hp, out s.MaxHp, out s.Block, out s.Gold, out s.Potions);
            _start = s;
        }
        catch (Exception ex) { LogOnce("began", ex); }
    }

    private static void OnTurnStarted(CombatState state)
    {
        try
        {
            _rounds = Math.Max(_rounds, state.RoundNumber);
        }
        catch (Exception ex) { LogOnce("turn", ex); }
    }

    private static void OnWon(CombatRoom room)
    {
        _won = true;
    }

    private static void OnEnded(CombatRoom room)
    {
        try
        {
            var start = _start;
            _start = null;
            var state = Read("combat state", () => room.CombatState, null);
            var player = Me(state);
            Vitals(player, out var hp, out var maxHp, out var block, out var gold, out var potions);
            var record = new CombatRecord
            {
                Encounter = start?.Encounter ?? Read("end encounter", () => room.Encounter?.Id.ToString(), null),
                RoomType = start?.RoomType ?? Read("end room type", () => room.RoomType.ToString(), null),
                Act = start?.Act ?? (state == null ? null : Read<int?>("end act", () => state.RunState.CurrentActIndex + 1, null)),
                Floor = start?.Floor ?? (state == null ? null : Read<int?>("end floor", () => state.RunState.ActFloor, null)),
                Won = _won,
                Turns = _rounds > 0 ? _rounds : null,
                HpStart = start?.Hp, MaxHpStart = start?.MaxHp, BlockStart = start?.Block,
                HpEnd = hp, MaxHpEnd = maxHp, BlockEnd = block,
                GoldStart = start?.Gold, GoldEnd = gold,
                PotionsStart = start?.Potions, PotionsEnd = potions,
            };
            _trace?.Append("combat_end", record.ToJson(), null, null);
        }
        catch (Exception ex) { LogOnce("ended", ex); }
        finally
        {
            _won = false;
            _rounds = 0;
        }
    }

    /// <summary>The local player, from the combat or else the run: after a win the combat's player creatures may be gone
    /// (it is over), while the run still has the player.</summary>
    private static Player? Me(CombatState? state)
    {
        var p = state == null ? null : Read("local player", () => LocalContext.GetMe(state) ?? state.Players.FirstOrDefault(), null);
        if (p != null) return p;
        return Read("run player", () =>
        {
            var rs = MegaCrit.Sts2.Core.Runs.RunManager.Instance?.DebugOnlyGetState();
            return rs == null ? null : LocalContext.GetMe(rs) ?? rs.Players.FirstOrDefault();
        }, null);
    }

    private static void Vitals(Player? player, out int? hp, out int? maxHp, out int? block, out int? gold, out List<string>? potions)
    {
        hp = maxHp = block = gold = null;
        potions = null;
        if (player == null) return;
        var p = player;
        hp = Read<int?>("hp", () => p.Creature.CurrentHp, null);
        maxHp = Read<int?>("max hp", () => p.Creature.MaxHp, null);
        block = Read<int?>("block", () => p.Creature.Block, null);
        gold = Read<int?>("gold", () => p.Gold, null);
        potions = Read<List<string>?>("potions", () => p.Potions.Select(x => x.Id.ToString()).ToList(), null);
    }

    private static T Read<T>(string what, Func<T> get, T fallback)
    {
        try
        {
            return get();
        }
        catch (Exception ex)
        {
            LogOnce(what, ex);
            return fallback;
        }
    }

    private static void LogOnce(string what, Exception ex)
    {
        if (Logged.Add(what))
            ModLog.Error($"combat recorder: cannot read {what}: {ex.GetType().Name}: {ex.Message}");
    }
}
