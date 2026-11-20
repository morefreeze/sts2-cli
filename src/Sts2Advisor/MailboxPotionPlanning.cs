using System.Text.Json;
using HarmonyLib;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

/// <summary>Gives CombatSolver a small route-level credit for a planned Tiny Mailbox refill.</summary>
internal static class MailboxPotionPlanning
{
    private readonly record struct Hint(RunState Run, int Act, int Col, int Row);
    private static Hint? _hint;
    private static CombatState? _lastLogged;
    private static int _credit;
    private static bool _checked;
    private static double _nextCheck;

    public static void RecordMapAdvice(AdviceReply reply)
    {
        _hint = null;
        if (reply.Action is not { } action
            || !action.TryGetProperty("mailbox_restock", out var restock) || restock.ValueKind != JsonValueKind.True
            || !action.TryGetProperty("args", out var args) || args.ValueKind != JsonValueKind.Object
            || !TryCoordinate(args, "col", out int col) || !TryCoordinate(args, "row", out int row)
            || RunManager.Instance?.DebugOnlyGetState() is not { Players.Count: 1 } run)
            return;
        _hint = new Hint(run, run.CurrentActIndex, col, row);
    }

    private static bool TryCoordinate(JsonElement args, string key, out int coordinate)
    {
        coordinate = -1;
        return args.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.Number
            && value.TryGetInt32(out coordinate);
    }

    internal static void TryInstall(double now)
    {
        if (_checked || now < _nextCheck)
            return;
        _nextCheck = now + 2;
        if (AccessTools.TypeByName("CombatSolver.PotionRewardOutlook") is not { } outlook)
            return;
        _checked = true;
        var root = outlook.Assembly.GetType("CombatSolver.CombatRootSnapshot");
        var capture = root == null ? null : AccessTools.DeclaredMethod(root, "Capture",
            [typeof(CombatState), typeof(bool)]);
        var getter = AccessTools.DeclaredPropertyGetter(outlook, "ReplacementHpCredit");
        if (capture == null || getter?.ReturnType != typeof(int))
        {
            ModLog.Info("mailbox potion planning: CombatSolver reward outlook is incompatible");
            return;
        }

        var harmony = new Harmony("morefreeze.sts2advisor.mailbox");
        try
        {
            harmony.Patch(capture, prefix: Callback(nameof(CaptureCredit)));
            harmony.Patch(getter, postfix: Callback(nameof(FloorReplacementCredit)));
            ModLog.Info("mailbox potion planning: CombatSolver reward outlook connected");
        }
        catch (Exception e)
        {
            harmony.Unpatch(capture, HarmonyPatchType.All, harmony.Id);
            harmony.Unpatch(getter, HarmonyPatchType.All, harmony.Id);
            ModLog.Error($"mailbox potion planning: CombatSolver patch failed: {e.Message}");
        }
    }

    private static HarmonyMethod Callback(string name) => new(typeof(MailboxPotionPlanning), name);

    // The Solver captures the outlook on the game thread before starting its search workers.
    private static void CaptureCredit(CombatState state)
    {
        int credit = 0;
        if (_hint is { } hint && LocalContext.GetMe(state) is { } player
            && ReferenceEquals(player.RunState, hint.Run)
            && hint.Run.CurrentActIndex == hint.Act && hint.Run.CurrentMapCoord is { } here
            && here.col == hint.Col && here.row == hint.Row
            && state.Encounter?.RoomType is RoomType.Monster or RoomType.Elite)
        {
            credit = ActPlanningContext.MailboxRefillHpCredit(player);
            if (credit > 0 && !ReferenceEquals(_lastLogged, state))
            {
                _lastLogged = state;
                ModLog.Info($"mailbox potion planning: {credit} HP replacement credit, act {hint.Act + 1}, row {hint.Row}");
            }
        }
        Volatile.Write(ref _credit, credit);
    }

    private static void FloorReplacementCredit(ref int __result)
        => __result = Math.Max(__result, Volatile.Read(ref _credit));
}
