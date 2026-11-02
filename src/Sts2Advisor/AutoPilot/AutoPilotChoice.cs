// src/Sts2Advisor/AutoPilot/AutoPilotChoice.cs
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>One answer from the advice service for one settled screen.</summary>
internal sealed record CachedAdvice(Capture Capture, AdviceReply Reply, PlannedAction Plan, string Json);

/// <summary>Thrown out of a handler when it picks on a screen there is no advice for yet. The loop catches
/// it, asks the service (off the main thread), and reruns the handler, which reads the screen afresh.</summary>
internal sealed class NeedAdviceException(string screen, string fingerprint)
    : Exception($"autopilot needs advice for {screen}")
{
    public string Screen { get; } = screen;
    public string Fingerprint { get; } = fingerprint;
}

/// <summary>Stops the autopilot with a reason (the loop's way out).</summary>
internal sealed class AutoPilotStopException(StopReason reason, string? detail)
    : Exception($"autopilot stop: {reason} {detail}")
{
    public StopReason Reason { get; } = reason;
    public string? Detail { get; } = detail;
}

/// <summary>Thrown out of a room handler that is about to pick while an overlay is open (an event combat's
/// rewards, say): the loop deals with the overlay first and reruns the room handler later.</summary>
internal sealed class YieldToLoopException() : Exception("autopilot: an overlay is open, the loop handles it first");

/// <summary>What the handlers call back into (implemented by AutoPilotLoop).</summary>
internal interface IAutoPilotHost
{
    /// <summary>Whether the panel should say "combat" (an event's combat runs inside a room handler).</summary>
    bool InCombat { set; }

    /// <summary>A single step has executed its choice: whatever asks for another one ends the run.</summary>
    bool StepDone { get; }

    Task<CachedAdvice> FetchAdviceAsync(CancellationToken ct);
    void StepTaken(CachedAdvice advice);
    Task DrainUntilAsync(Task pending, CancellationToken ct);

    /// <summary>The belt is full and a potion reward is on the rewards screen: asks the service whether a held potion
    /// should make room for it and, when it says so, discards that one (the game's own potion-popup path), waits for
    /// the slot to open and records the step. True when a slot is free for the reward now; false (belt untouched, the
    /// reward skipped as before) for "keep the belt" and for every failure.</summary>
    Task<bool> MakeRoomForPotionAsync(Player me, PotionReward reward, CancellationToken ct);
}

/// <summary>The game object a UI node stands for, as Capture.Choices holds it.</summary>
internal static class UiModel
{
    public static object? Of(object node) => node switch
    {
        NCardHolder h => h.CardModel,  // NGridCardHolder too
        NEventOptionButton b => b.Option,
        NRestSiteButton b => b.Option,
        NCardBundle b => b.Bundle,
        NMapPoint p => p.Point,
        NMerchantSlot s => s.Entry,
        _ => null,
    };

    /// <summary>A grid may show a copy of a deck card: same card and upgrade level is the same choice.</summary>
    public static bool SameCard(object? a, object? b) =>
        a is CardModel x && b is CardModel y && x.Id == y.Id && x.CurrentUpgradeLevel == y.CurrentUpgradeLevel;
}

/// <summary>Replaces `random.NextItem(items)` inside the AutoSlay handlers (HandlerPatches rewrites the calls).
/// Main thread only, like the handlers. Outside the autopilot (the game's own AutoSlay) it is NextItem.</summary>
internal static class AutoPilotChoice
{
    private static LocLookup? _loc;
    private static IAutoPilotHost? _host;
    private static CachedAdvice? _advice;
    private static readonly List<int> Remaining = new();
    private static bool _stepRecorded;

    public static bool Active => _host != null;

    internal static IAutoPilotHost? Host => _host;

    public static void Begin(LocLookup loc, IAutoPilotHost host)
    {
        _loc = loc;
        _host = host;
        Clear();
    }

    /// <summary>Only the loop that began the session may end it: a stopped loop can finish after a new one began.</summary>
    public static void End(IAutoPilotHost host)
    {
        if (!ReferenceEquals(_host, host))
            return;
        _host = null;
        Clear();
    }

    public static void Offer(CachedAdvice advice)
    {
        _advice = advice;
        Remaining.Clear();
        Remaining.AddRange(advice.Plan.Indices);
        _stepRecorded = false;
    }

    /// <summary>A handler that acted on the whole advice itself (GridSelectHandler): nothing is left of it, but the
    /// advice stays, so a later pick fails with "more picks than the advice has".</summary>
    public static void Consume()
    {
        Remaining.Clear();
        _stepRecorded = true;
    }

    private static void Clear()
    {
        _advice = null;
        Remaining.Clear();
        _stepRecorded = false;
    }

    public static T? Pick<T>(Rng random, IEnumerable<T> items)
    {
        if (!Active)
        {
            // Never pick randomly for a stopped autopilot (its old handler may still be running): only the
            // game's own AutoSlay wants that.
            if (AutoSlayer.IsActive)
                return random.NextItem(items);
            throw new OperationCanceledException("autopilot is not running");
        }
        var list = items as IReadOnlyList<T> ?? items.ToList();
        // A finished event offers one synthetic Proceed option, and the screen no longer reads as a decision.
        if (list.Count > 0 && list.All(n => n is NEventOptionButton { Option.IsProceed: true }))
            return list[0];
        if ((typeof(T) == typeof(NEventOptionButton) || typeof(T) == typeof(NRestSiteButton))
            && NOverlayStack.Instance?.Peek() != null)
            throw new YieldToLoopException();
        var cap = DecisionCapture.Detect(_loc!);
        if (cap is not { Kind: CaptureKind.Advise })
            throw new AutoPilotException($"a handler picks among {list.Count} {typeof(T).Name} but the screen reads as {cap?.Screen ?? "nothing"}");
        if (_advice == null || _advice.Capture.Fingerprint != cap.Fingerprint)
            throw new NeedAdviceException(cap.Screen, cap.Fingerprint);
        if (_advice.Plan.Kind != PlanKind.Pick)
            throw new AutoPilotException($"{cap.Screen}: the handler picks an option but the advice is {_advice.Plan.Kind}");

        foreach (int i in Remaining)
        {
            if (i >= cap.Choices.Count)
                throw new AutoPilotException($"{cap.Screen}: advised index {i} is outside the {cap.Choices.Count} options");
            object model = cap.Choices[i];
            var node = list.FirstOrDefault(n => n != null && ReferenceEquals(UiModel.Of(n), model));
            if (node == null)
            {
                node = list.FirstOrDefault(n => n != null && UiModel.SameCard(UiModel.Of(n), model));
                if (node != null)
                    ModLog.Info($"autopilot: {cap.Screen} option {i} matched by card id, not by object");
            }
            if (node == null)
                continue;
            Remaining.Remove(i);
            if (!_stepRecorded)
            {
                _stepRecorded = true;
                _host!.StepTaken(_advice);
            }
            return node;
        }
        if (Remaining.Count == 0 && _host!.StepDone)
            throw new AutoPilotStopException(StopReason.StepDone, null);
        throw new AutoPilotException(Remaining.Count == 0
            ? $"{cap.Screen}: the handler wants more picks than the advice has ({_advice.Plan.Indices.Count})"
            : $"{cap.Screen}: no advised option ({string.Join(",", Remaining)}) is among the {list.Count} the screen offers"
              + (typeof(T) == typeof(NEventOptionButton) ? " (the handler drops locked options and options that would kill the player)" : ""));
    }

    /// <summary>After a handler returns: advised picks it never asked for (a screen that confirmed early).</summary>
    public static void WarnUnusedPicks(string handler)
    {
        if (_advice != null && _stepRecorded && Remaining.Count > 0)
            ModLog.Info($"autopilot: {handler} finished with advised picks {string.Join(",", Remaining)} unused");
    }
}
