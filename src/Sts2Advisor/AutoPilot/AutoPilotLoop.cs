// src/Sts2Advisor/AutoPilot/AutoPilotLoop.cs
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.GameActions;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.Events.Custom;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>Plays the rest of the run from wherever the player switched the autopilot on. Every turn it looks
/// at the screen and does the one thing that screen needs: an overlay (rewards, card reward, deck select, ...),
/// combat (wait for CombatSolver), the map, or the room itself (event, rest site, shop, treasure, proceed).
/// Runs on the main thread (started from the frame tick; awaits resume there through Godot's
/// SynchronizationContext, as the game's own AutoSlay relies on). AutoSlayer.Start() is never called: it would
/// abandon the run, write unlocks to the save and quit the game.</summary>
internal sealed class AutoPilotLoop : IAutoPilotHost
{
    private const string RoomContainerPath = "/root/Game/RootSceneContainer/Run/RoomContainer";
    private const double StuckSeconds = 30;
    private const double FetchSeconds = 20;
    private const int MaxSameOverlay = 3;
    private const int MaxNoProgress = 3;
    /// <summary>How long an open map may take to enable travel (it animates in after Proceed) before it is taken for
    /// one the player opened to look at.</summary>
    private const int MapTravelWaitMs = 2000;
    private const int MapTravelPollMs = 100;
    /// <summary>An event can start a combat; CombatSolver may need minutes per turn.</summary>
    private static readonly TimeSpan EventTimeout = TimeSpan.FromHours(4);

    private readonly IAdvisor _advisor;
    private readonly LocLookup _loc;
    private readonly AdvisorTraceLog? _trace;
    private readonly AutoPilotSession _session;
    private readonly Func<double> _now;
    private readonly double _stepPauseSeconds;
    private readonly Rng _rng = new(0uL);  // never drawn from: every NextItem in the handlers is redirected
    private readonly Dictionary<Type, IScreenHandler> _screens;
    private readonly EventRoomHandler _event = new();
    private readonly RestSiteRoomHandler _rest = new();
    private readonly TreasureRoomHandler _treasure = new();
    private readonly MapHandler _map;
    private readonly ShopHandler _shop;
    private readonly HashSet<string> _doneRooms = new();
    private string _progressKey = "";
    private double _progressAt;
    private int _sameOverlay;
    private string _noProgressRoom = "";
    private int _noProgressCount;
    private (int Act, int Floor) _stepFloor;

    /// <param name="stepPauseSeconds">How long a step stays on the panel before the next screen is acted on
    /// (config step_pause_seconds); a single step is never held.</param>
    public AutoPilotLoop(IAdvisor advisor, LocLookup loc, AdvisorTraceLog? trace, AutoPilotSession session, Func<double> now,
                         double stepPauseSeconds)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _session = session;
        _now = now;
        _stepPauseSeconds = stepPauseSeconds;
        _map = new MapHandler(this);
        _shop = new ShopHandler(this);
        _screens = new Dictionary<Type, IScreenHandler>
        {
            [typeof(NRewardsScreen)] = new RewardsHandler(this),
            [typeof(NCardRewardSelectionScreen)] = new CardRewardHandler(this),
            // The grid handlers' own random picks are replaced by the advised cards (GridSelectHandler).
            [typeof(NDeckUpgradeSelectScreen)] = new GridSelectHandler(this, new DeckUpgradeScreenHandler(), GridPress.Holder),
            [typeof(NDeckTransformSelectScreen)] = new GridSelectHandler(this, new DeckTransformScreenHandler(), GridPress.Grid),
            [typeof(NDeckEnchantSelectScreen)] = new GridSelectHandler(this, new DeckEnchantScreenHandler(), GridPress.Holder),
            [typeof(NDeckCardSelectScreen)] = new GridSelectHandler(this, new DeckCardSelectScreenHandler(), GridPress.Holder),
            [typeof(NSimpleCardSelectScreen)] = new GridSelectHandler(this, new SimpleCardSelectScreenHandler(), GridPress.Grid),
            [typeof(NChooseACardSelectionScreen)] = new ChooseACardScreenHandler(),
            [typeof(NChooseABundleSelectionScreen)] = new ChooseABundleScreenHandler(),
        };
    }

    public bool InCombat { set => _session.InCombat = value; }

    /// <summary>Runs until something stops it; never throws.</summary>
    public async Task<(StopReason Reason, string? Detail)> RunAsync(CancellationToken ct)
    {
        AutoPilotChoice.Begin(_loc, this);
        try
        {
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                await StepAsync(ct);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return (StopReason.Hotkey, null);
        }
        catch (AutoPilotStopException e)
        {
            return (e.Reason, e.Detail);
        }
        catch (Exception e) when (e is TypeLoadException or MissingMemberException)
        {
            ModLog.Error($"autopilot: game API mismatch: {e}");
            return (StopReason.Unsupported, e.GetType().Name);
        }
        catch (Exception e)
        {
            ModLog.Error($"autopilot: {e}");
            return (StopReason.HandlerFailed, $"{e.GetType().Name}: {e.Message}");
        }
        finally
        {
            AutoPilotChoice.End(this);
            _session.InCombat = false;
        }
    }

    private async Task StepAsync(CancellationToken ct)
    {
        await HoldAsync(ct);
        var rs = RunManager.Instance?.DebugOnlyGetState() ?? throw new AutoPilotStopException(StopReason.LeftRun, null);
        if (rs.Players.Count != 1)
            throw new AutoPilotStopException(StopReason.Multiplayer, null);

        var overlay = NOverlayStack.Instance?.Peek();
        // The order is the game's own (ActiveScreenContext): the open map comes before the overlay stack.
        switch (AutoPilotScreenOrder.Next(overlay is NGameOverScreen, ModalOpen(), CombatManager.Instance?.IsInProgress == true,
                                          NMapScreen.Instance is { IsOpen: true }, overlay != null))
        {
            case AutoPilotScreen.GameOver:
                throw new AutoPilotStopException(StopReason.GameOver, null);
            case AutoPilotScreen.Modal:
                // A blocking popup (a first-time tip, say) is the player's, like an overlay without advice.
                await HandBackAsync("modal", ModalOpen, ct);
                return;
            case AutoPilotScreen.Combat:
                // An overlay in combat (a pile select, say) belongs to CombatSolver or the player.
                _session.InCombat = true;
                try { await CombatWait.UntilOverAsync(ct); }
                finally { _session.InCombat = false; }
                MarkProgress();
                return;
            case AutoPilotScreen.Map:
                await HandleMapAsync(ct);
                return;
            case AutoPilotScreen.Overlay:
                await HandleOverlayAsync(overlay!, ct);
                return;
        }
        _sameOverlay = 0;

        var room = rs.CurrentRoom;
        string roomKey = $"{rs.CurrentActIndex}/{rs.TotalFloor}/{room?.RoomType}";
        switch (room)
        {
            case EventRoom er when !er.LocalMutableEvent.IsFinished && er.LocalMutableEvent.CurrentOptions.Any(o => !o.IsLocked):
                // AutoSlay's EventRoomHandler walks through a FakeMerchant event and leaves it without asking
                // anyone. Full auto keeps that; a single step must not silently take a second decision.
                if (_session.StepDone && IsFakeMerchantEvent())
                    throw new AutoPilotStopException(StopReason.StepDone, null);
                await RunRoomAsync(_event, EventTimeout, roomKey, RoomDone.Never, ct);
                return;
            case RestSiteRoom rr when rr.Options.Any(o => o.IsEnabled) && !_doneRooms.Contains(roomKey):
                await RunRoomAsync(_rest, _rest.Timeout, roomKey, RoomDone.OnProgress, ct);
                return;
            case MerchantRoom when !_doneRooms.Contains(roomKey):
                await RunRoomAsync(_shop, _shop.Timeout, roomKey, RoomDone.OnProgress, ct);
                return;
            case TreasureRoom when !_doneRooms.Contains(roomKey):
                await RunRoomAsync(_treasure, _treasure.Timeout, roomKey, RoomDone.OnReturn, ct);
                return;
        }
        if (await ClickProceedAsync(ct))
            return;
        await IdleAsync(rs, room, ct);
    }

    /// <summary>Lets the newest step stay on the panel for step_pause_seconds after it was taken, before anything
    /// is done on the next screen. Cancelled by F9 / Stop like every wait here.</summary>
    private async Task HoldAsync(CancellationToken ct)
    {
        while (true)
        {
            double left = _session.HoldRemaining(_now(), _stepPauseSeconds);
            if (left <= 0)
                return;
            await Task.Delay(TimeSpan.FromSeconds(Math.Min(left, 0.1)), ct);
        }
    }

    /// <summary>The map is open, so it is the screen in front -- whatever the overlay stack still holds (the rewards
    /// screen stays in it after Proceed). Travel is usually enabled at once; right after Proceed the map may still be
    /// animating in, so give it a moment. A map that stays closed to travel is one the player opened to look at.</summary>
    private async Task HandleMapAsync(CancellationToken ct)
    {
        _sameOverlay = 0;  // the overlay under the map is not being retried
        for (int waited = 0; waited < MapTravelWaitMs && MapOpenWithoutTravel(); waited += MapTravelPollMs)
            await Task.Delay(MapTravelPollMs, ct);
        if (NMapScreen.Instance is not { IsOpen: true })
            return;  // closed in the meantime: the next step looks again
        if (MapOpenWithoutTravel())
        {
            if (_session.StepDone)  // a decision for the player, like an overlay without advice
                throw new AutoPilotStopException(StopReason.StepDone, null);
            await HandBackAsync(nameof(NMapScreen), MapOpenWithoutTravel, ct);
            return;
        }
        await RunHandlerAsync(_map, _map.Timeout, ct);
        MarkProgress();
    }

    private static bool MapOpenWithoutTravel() => NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: false };

    private async Task HandleOverlayAsync(IOverlayScreen overlay, CancellationToken ct)
    {
        if (!_screens.TryGetValue(overlay.GetType(), out var handler))
        {
            // No advice exists for it (choose a relic, crystal sphere, ...): the player does this one screen.
            if (_session.StepDone)  // that is a decision too: a single step ends here
                throw new AutoPilotStopException(StopReason.StepDone, null);
            await HandBackAsync(overlay.GetType().Name, () => NOverlayStack.Instance?.Peek() == overlay, ct);
            return;
        }
        int countBefore = NOverlayStack.Instance!.ScreenCount;
        await RunHandlerAsync(handler, handler.Timeout, ct);
        var stack = NOverlayStack.Instance;
        if (stack != null && stack.ScreenCount == countBefore && stack.Peek() == overlay)
        {
            if (++_sameOverlay >= MaxSameOverlay)
                throw new AutoPilotStopException(StopReason.Stuck, $"{overlay.GetType().Name} did not close after {MaxSameOverlay} attempts");
        }
        else
        {
            _sameOverlay = 0;
        }
        MarkProgress();
        await Task.Delay(100, ct);
    }

    /// <summary>A popup is open and still alive (a freed or detached one must not park the autopilot).</summary>
    private static bool ModalOpen() =>
        NModalContainer.Instance?.OpenModal is GodotObject o && GodotObject.IsInstanceValid(o) && (o as Node)?.IsInsideTree() != false;

    private async Task HandBackAsync(string name, Func<bool> stillOpen, CancellationToken ct)
    {
        _session.HandBack(name);
        ModLog.Info($"autopilot: handing {name} back to the player");
        while (stillOpen())
            await Task.Delay(200, ct);  // no timeout: the player takes as long as they like
        _session.ClearHandBack();
        MarkProgress();
    }

    /// <summary>When a room's handler has done its job: never (an event runs until it is finished), on its first
    /// step or overlay (rest site, shop: an early return with nothing done, such as "no clickable rest site
    /// buttons", is retried), or whenever it returns (treasure clicks proceed itself and records no step).</summary>
    private enum RoomDone { Never, OnProgress, OnReturn }

    /// <summary>Runs a room's handler. A room that is done is not run again; a handler that returns again and again
    /// without a step or an overlay is not getting anywhere.</summary>
    private async Task RunRoomAsync(IHandler handler, TimeSpan timeout, string roomKey, RoomDone done, CancellationToken ct)
    {
        int stepsBefore = _session.StepCount;
        bool finished = await RunHandlerAsync(handler, timeout, ct);
        MarkProgress();
        if (!finished)
            return;
        bool progressed = _session.StepCount != stepsBefore || NOverlayStack.Instance?.Peek() != null;
        if (done == RoomDone.OnReturn || (done == RoomDone.OnProgress && progressed))
            _doneRooms.Add(roomKey);
        if (progressed || done == RoomDone.OnReturn)
        {
            _noProgressCount = 0;
            return;
        }
        if (roomKey != _noProgressRoom)
        {
            _noProgressRoom = roomKey;
            _noProgressCount = 0;
        }
        if (++_noProgressCount >= MaxNoProgress)
            throw new AutoPilotStopException(StopReason.Stuck, $"{roomKey}: {handler.GetType().Name} made no progress");
    }

    /// <summary>Runs a handler; when it stops for want of advice, asks for it and runs the handler again.</summary>
    /// <summary>False when the handler yielded to the loop (an overlay is open) instead of finishing.</summary>
    private async Task<bool> RunHandlerAsync(IHandler handler, TimeSpan timeout, CancellationToken ct)
    {
        while (true)
        {
            try
            {
                await WaitHelper.WithTimeout(t => handler.HandleAsync(_rng, t), timeout, ct);
                AutoPilotChoice.WarnUnusedPicks(handler.GetType().Name);
                return true;
            }
            catch (YieldToLoopException)
            {
                return false;
            }
            catch (NeedAdviceException e)
            {
                if (!_session.CountAdviceRetry(e.Fingerprint))
                    throw new AutoPilotStopException(StopReason.Stuck,
                        $"{e.Screen}: asked for advice {AutoPilotSession.MaxAdviceRetries} times without settling");
                await FetchAdviceAsync(ct);
            }
            catch (AutoPilotException e)
            {
                throw new AutoPilotStopException(StopReason.HandlerFailed, e.Message);
            }
        }
    }

    /// <summary>Event proceed option, or an enabled Proceed button in the room. True when one was clicked.</summary>
    private async Task<bool> ClickProceedAsync(CancellationToken ct)
    {
        var container = ((SceneTree)Engine.GetMainLoop()).Root.GetNodeOrNull(RoomContainerPath);
        if (container == null)
            return false;
        NClickableControl? button = UiHelper.FindAll<NEventOptionButton>(container)
            .FirstOrDefault(o => o.Option.IsProceed && !o.Option.IsLocked && o.IsEnabled && o.IsVisibleInTree());
        button ??= UiHelper.FindAll<NProceedButton>(container).FirstOrDefault(b => b.IsEnabled && b.IsVisibleInTree());
        if (button == null)
            return false;
        await UiHelper.Click(button);
        await Task.Delay(300, ct);
        MarkProgress();
        return true;
    }

    private async Task IdleAsync(RunState rs, AbstractRoom? room, CancellationToken ct)
    {
        string key = $"{rs.CurrentActIndex}/{rs.TotalFloor}/{room?.RoomType}/{NMapScreen.Instance?.IsOpen}/{NOverlayStack.Instance?.ScreenCount}";
        double now = _now();
        if (key != _progressKey)
        {
            _progressKey = key;
            _progressAt = now;
        }
        else if (now - _progressAt > StuckSeconds)
        {
            throw new AutoPilotStopException(StopReason.Stuck, $"nothing to do for {StuckSeconds:0} s at {key}");
        }
        await Task.Delay(200, ct);
    }

    private void MarkProgress() => _progressKey = "";

    public bool StepDone => _session.StepDone;

    public async Task<CachedAdvice> FetchAdviceAsync(CancellationToken ct)
    {
        // A tip popup over the screen comes first: it must not end a step whose follow-up select it covers.
        if (ModalOpen())
            await HandBackAsync("modal", ModalOpen, ct);
        // A single step stops at the next decision, except the card select / bundle its own choice opened
        // (shop removal, smith, an event's "remove a card"): that is the second half of the same step.
        if (_session.StepDone && !OpenedByTheStep())
            throw new AutoPilotStopException(StopReason.StepDone, null);
        // Handlers that decide several times in a row (shop purchases, an event's pages) come back here, not to StepAsync.
        await HoldAsync(ct);
        string? last = null;
        double deadline = _now() + FetchSeconds;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            if (ModalOpen())  // a tip popup opened over the screen: the screen does not read as a decision until it is closed
            {
                await HandBackAsync("modal", ModalOpen, ct);
                last = null;
                deadline = _now() + FetchSeconds;  // the player's time does not count
                continue;
            }
            var cap = DecisionCapture.Detect(_loc);
            bool advisable = cap is { Kind: CaptureKind.Advise };
            // Screens animate in: ask only about a fingerprint seen on two polls in a row (as the advice panel does).
            if (advisable && cap!.Fingerprint == last)
            {
                var advice = await AskAsync(cap, ct);
                if (advice != null)
                    return advice;
                last = null;
                deadline = _now() + FetchSeconds;  // an outage is timed by the session, not by this deadline
                continue;
            }
            last = advisable ? cap!.Fingerprint : null;
            if (_now() > deadline)
                throw new AutoPilotStopException(StopReason.Stuck, $"no settled decision on {cap?.Screen ?? "no screen"}");
            await Task.Delay(300, ct);
        }
    }

    /// <summary>Null when the service could not be reached (retry); throws when the reply is unusable.</summary>
    private async Task<CachedAdvice?> AskAsync(Capture cap, CancellationToken ct)
    {
        string decision = cap.Header.Decision;
        string json = JsonSerializer.Serialize(cap.Build!(), AdvisorRuntime.JsonOpts);
        AdviceReply reply;
        try
        {
            reply = await _advisor.AdviseAsync(json, ct);
        }
        catch (Exception e) when (e is AdviceFormatException or AdviceServiceException)
        {
            _trace?.Append(decision, json, null, e.GetType().Name);
            throw new AutoPilotStopException(StopReason.BadReply, e.Message);
        }
        catch (Exception e) when (!ct.IsCancellationRequested)
        {
            _trace?.Append(decision, json, null, e.GetType().Name);
            if (_session.Offline(_now()))
                throw new AutoPilotStopException(StopReason.Offline, e.Message);
            await Task.Delay(1000, ct);
            return null;
        }
        _session.Online();
        PlannedAction plan;
        try
        {
            plan = AutoPilotPlan.From(decision, reply);
        }
        catch (AutoPilotException e)
        {
            _trace?.Append(decision, json, reply.Raw, "autopilot_plan");
            throw new AutoPilotStopException(StopReason.HandlerFailed, e.Message);
        }
        var advice = new CachedAdvice(cap, reply, plan, json);
        AutoPilotChoice.Offer(advice);
        return advice;
    }

    private static bool IsFakeMerchantEvent()
    {
        var container = ((SceneTree)Engine.GetMainLoop()).Root.GetNodeOrNull(RoomContainerPath);
        return container != null && UiHelper.FindFirst<NFakeMerchant>(container) != null;
    }

    private static (int Act, int Floor) CurrentFloor()
    {
        var rs = RunManager.Instance?.DebugOnlyGetState();
        return rs == null ? (-1, -1) : (rs.CurrentActIndex, rs.TotalFloor);
    }

    /// <summary>The screen is a card/bundle select on the floor where the last step was taken.</summary>
    private bool OpenedByTheStep() =>
        DecisionCapture.Detect(_loc) is { Kind: CaptureKind.Advise } cap
        && cap.Header.Decision is "card_select" or "bundle_select"
        && CurrentFloor() == _stepFloor;

    public void StepTaken(CachedAdvice a)
    {
        _stepFloor = CurrentFloor();
        bool zh = GameText.IsChinese;
        int n = _session.Record(AutoPilotText.StepText(a.Capture.Header.Decision, a.Reply, a.Capture.Labels, zh),
                                AutoPilotText.Reason(a.Reply, zh),
                                AutoPilotText.DetailLines(StepDetails.Of(a), zh),
                                _now());
        _trace?.Append(a.Capture.Header.Decision, a.Json, a.Reply.Raw, null, autopilotStep: n);
        ModLog.Info($"autopilot step {n}: {a.Capture.Header.Decision} {a.Reply.ActionName}");
    }

    private const double PotionDiscardSeconds = 5;
    private bool _potionSwapFailureLogged;

    public async Task<bool> MakeRoomForPotionAsync(Player me, PotionReward reward, CancellationToken ct)
    {
        string request = "";
        try
        {
            if (reward.Potion is not { } offered)
                return false;
            var held = me.Potions.ToList();
            // The same potion dicts the decision JSON carries (Sts2Shared), in a minimal state: greedy's scorer
            // reads player.potions / potion_slots only.
            var state = new Dictionary<string, object?>
            {
                ["decision"] = "potion_swap",
                ["player"] = new Dictionary<string, object?>
                {
                    ["potions"] = held.Select((p, i) => DecisionSerializer.PotionDict(p, i, _loc)).ToList(),
                    ["potion_slots"] = me.MaxPotionCount,
                },
            };
            var offeredList = new List<Dictionary<string, object?>> { DecisionSerializer.PotionDict(offered, 0, _loc) };
            request = JsonSerializer.Serialize(new Dictionary<string, object?> { ["state"] = state, ["offered"] = offeredList },
                                               AdvisorRuntime.JsonOpts);
            var reply = await _advisor.PotionSwapAsync(request, ct);
            _trace?.Append("potion_swap", request, reply.Raw, null);
            if (reply.Reason != "swap")
            {
                // "free_slot" cannot happen on a full belt; anything but a swap leaves things as they were.
                ModLog.Info($"autopilot: potion swap: keeping the belt ({reply.Reason}) over {GameText.Potion(offered)}");
                return false;
            }
            if (reply.Discard is not { } d || reply.Take != 0 || d >= held.Count
                || (reply.DiscardId != null && reply.DiscardId != held[d].Id.ToString())
                || (reply.TakeId != null && reply.TakeId != offered.Id.ToString()))
                throw new AutoPilotException($"potion_swap reply does not match the belt: {reply.Raw}");
            var doomed = held[d];
            int slot = -1;
            for (int i = 0; i < me.PotionSlots.Count; i++)
                if (ReferenceEquals(me.PotionSlots[i], doomed))
                    slot = i;
            if (slot < 0)
                throw new AutoPilotException("the potion to discard is not in the belt");

            bool zh = GameText.IsChinese;
            string doomedName = GameText.Potion(doomed), takenName = GameText.Potion(offered);
            int n = _session.Record(AutoPilotText.PotionSwapText(doomedName, takenName, zh),
                                    reply.DiscardScore is { } ds && reply.TakeScore is { } ts
                                        ? AutoPilotText.PotionSwapReason(ds, ts, reply.Margin, zh) : null,
                                    AutoPilotText.DetailLines(StepDetails.OfPotions(doomed, offered), zh), _now());
            _trace?.Append("potion_swap", request, reply.Raw, null, autopilotStep: n);
            ModLog.Info($"autopilot: potion swap: discard {doomedName}, take {takenName}");

            // The game's own discard (NPotionPopup.OnDiscardButtonPressed): an action in the queue.
            RunManager.Instance.ActionQueueSynchronizer.RequestEnqueue(
                new DiscardPotionGameAction(me, (uint)slot, CombatManager.Instance?.IsInProgress == true));
            await WaitHelper.Until(() => me.HasOpenPotionSlots, ct, TimeSpan.FromSeconds(PotionDiscardSeconds),
                                   $"the belt still has no free slot {PotionDiscardSeconds:0} s after discarding {doomedName}");
            await Task.Delay(200, ct);
            return true;
        }
        catch (Exception e) when (!ct.IsCancellationRequested)
        {
            // Never stops the autopilot: the reward is skipped, as it was before swaps existed.
            if (!_potionSwapFailureLogged)
            {
                _potionSwapFailureLogged = true;
                ModLog.Error($"autopilot: potion swap failed, skipping the potion reward (logged once): {e}");
            }
            _trace?.Append("potion_swap", request == "" ? "{}" : request, null, e.GetType().Name);
            return false;
        }
    }

    /// <summary>Answers the screens a pending action opens (AutoSlayer.DrainOverlayScreensUntilAsync).</summary>
    public async Task DrainUntilAsync(Task pending, CancellationToken ct)
    {
        while (!pending.IsCompleted)
        {
            ct.ThrowIfCancellationRequested();
            var overlay = NOverlayStack.Instance?.Peek();
            // modal: false -- a tip popup is answered by the overlay's own handler (FetchAdviceAsync hands it back).
            if (overlay != null && AutoPilotScreenOrder.Next(overlay is NGameOverScreen, false, CombatManager.Instance?.IsInProgress == true,
                                                             NMapScreen.Instance is { IsOpen: true }, true) == AutoPilotScreen.Overlay)
                await HandleOverlayAsync(overlay, ct);
            await Task.Delay(50, ct);
        }
        await pending;
    }
}
