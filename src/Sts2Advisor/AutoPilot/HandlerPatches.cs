// src/Sts2Advisor/AutoPilot/HandlerPatches.cs
using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.Random;

namespace Sts2Advisor.AutoPilot;

/// <summary>Rewrites the AutoSlay handlers the autopilot reuses: each `random.NextItem(list)` becomes
/// AutoPilotChoice.Pick, and EventRoomHandler's `HandleEventCombat(ct)` becomes CombatWait.ForEventCombat.
/// A transpiler on the handlers' own async MoveNext, not a prefix on Rng.NextItem: nothing else in the
/// game is touched, and an inlined NextItem cannot slip past it. Both replacements are no-ops outside
/// the autopilot.</summary>
internal static class HandlerPatches
{
    private static readonly MethodInfo NextItem = AccessTools.Method(typeof(Rng), nameof(Rng.NextItem));
    private static readonly MethodInfo Pick = AccessTools.Method(typeof(AutoPilotChoice), nameof(AutoPilotChoice.Pick));
    private static readonly MethodInfo EventCombat = AccessTools.Method(typeof(EventRoomHandler), "HandleEventCombat");
    private static readonly MethodInfo EventCombatWait = AccessTools.Method(typeof(CombatWait), nameof(CombatWait.ForEventCombat));

    /// <summary>Handler, and how many NextItem / HandleEventCombat calls its HandleAsync must contain
    /// (2026-10-28, game v0.111.0). A different count means the game changed: autopilot stays off.</summary>
    private static readonly (Type Handler, int NextItems, int EventCombats)[] Targets =
    {
        (typeof(EventRoomHandler), 1, 2),
        (typeof(RestSiteRoomHandler), 1, 0),
        (typeof(DeckUpgradeScreenHandler), 1, 0),
        (typeof(DeckTransformScreenHandler), 1, 0),
        (typeof(DeckEnchantScreenHandler), 1, 0),
        (typeof(DeckCardSelectScreenHandler), 1, 0),
        (typeof(SimpleCardSelectScreenHandler), 1, 0),
        (typeof(ChooseACardScreenHandler), 1, 0),
        (typeof(ChooseABundleScreenHandler), 1, 0),
    };

    /// <summary>What the transpiler rewrote during the Patch call in progress (Patch runs it synchronously).</summary>
    private static (int NextItems, int EventCombats) _rewritten;

    /// <summary>Patches every target; null when all were rewritten as expected, else what went wrong.</summary>
    public static string? Apply(Harmony harmony)
    {
        var problems = new List<string>();
        foreach (var (handler, nextItems, eventCombats) in Targets)
        {
            var moveNext = AccessTools.AsyncMoveNext(AccessTools.Method(handler, "HandleAsync"));
            if (moveNext == null)
            {
                problems.Add($"{handler.Name}: no async HandleAsync");
                continue;
            }
            _rewritten = (0, 0);
            harmony.Patch(moveNext, transpiler: new HarmonyMethod(typeof(HandlerPatches), nameof(Redirect)));
            var got = _rewritten;
            if (got != (nextItems, eventCombats))
                problems.Add($"{handler.Name}: rewrote {got.NextItems} NextItem / {got.EventCombats} HandleEventCombat, expected {nextItems} / {eventCombats}");
        }
        return problems.Count == 0 ? null : string.Join("; ", problems);
    }

    private static IEnumerable<CodeInstruction> Redirect(IEnumerable<CodeInstruction> instructions)
    {
        int nextItems = 0, eventCombats = 0;
        var result = new List<CodeInstruction>();
        foreach (var ins in instructions)
        {
            if ((ins.opcode == OpCodes.Callvirt || ins.opcode == OpCodes.Call) && ins.operand is MethodInfo m)
            {
                // Stack [rng, items] -> static Pick<T>(Rng, IEnumerable<T>); [handler, ct] -> static ForEventCombat(handler, ct).
                if (m.IsGenericMethod && m.GetGenericMethodDefinition() == NextItem)
                {
                    ins.opcode = OpCodes.Call;
                    ins.operand = Pick.MakeGenericMethod(m.GetGenericArguments());
                    nextItems++;
                }
                else if (m == EventCombat)
                {
                    ins.opcode = OpCodes.Call;
                    ins.operand = EventCombatWait;
                    eventCombats++;
                }
            }
            result.Add(ins);
        }
        _rewritten = (nextItems, eventCombats);
        return result;
    }
}
