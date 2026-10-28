// src/Sts2Advisor/AutoPilot/AutoPilotLoop.cs
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
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
    /// <summary>An event can start a combat; CombatSolver may need minutes per turn.</summary>
    private static readonly TimeSpan EventTimeout = TimeSpan.FromHours(4);

    private readonly IAdvisor _advisor;
    private readonly LocLookup _loc;
    private readonly AdvisorTraceLog? _trace;
    private readonly AutoPilotSession _session;
    private readonly Func<double> _now;
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

    public AutoPilotLoop(IAdvisor advisor, LocLookup loc, AdvisorTraceLog? trace, AutoPilotSession session, Func<double> now)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _session = session;
        _now = now;
        _map = new MapHandler(this);
        _shop = new ShopHandler(this);
        _screens = new Dictionary<Type, IScreenHandler>
        {
            [typeof(NRewardsScreen)] = new RewardsHandler(),
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
        var rs = RunManager.Instance?.DebugOnlyGetState() ?? throw new AutoPilotStopException(StopReason.LeftRun, null);
        if (rs.Players.Count != 1)
            throw new AutoPilotStopException(StopReason.Multiplayer, null);

        var overlay = NOverlayStack.Instance?.Peek();
        if (overlay is NGameOverScreen)
            throw new AutoPilotStopException(StopReason.GameOver, null);
        // A blocking popup (a first-time tip, say) is the player's, like an overlay without advice.
        if (ModalOpen())
        {
            await HandBackAsync("modal", ModalOpen, ct);
            return;
        }
        // Combat first: an overlay in combat (a pile select, say) belongs to CombatSolver or the player.
        if (CombatManager.Instance?.IsInProgress == true)
        {
            _session.InCombat = true;
            try { await CombatWait.UntilOverAsync(ct); }
            finally { _session.InCombat = false; }
            MarkProgress();
            return;
        }
        if (overlay != null)
        {
            await HandleOverlayAsync(overlay, ct);
            return;
        }
        _sameOverlay = 0;

        if (NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: true })
        {
            await RunHandlerAsync(_map, _map.Timeout, ct);
            MarkProgress();
            return;
        }

        var room = rs.CurrentRoom;
        string roomKey = $"{rs.CurrentActIndex}/{rs.TotalFloor}/{room?.RoomType}";
        switch (room)
        {
            case EventRoom er when !er.LocalMutableEvent.IsFinished && er.LocalMutableEvent.CurrentOptions.Any(o => !o.IsLocked):
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

    private async Task HandleOverlayAsync(IOverlayScreen overlay, CancellationToken ct)
    {
        if (!_screens.TryGetValue(overlay.GetType(), out var handler))
        {
            // No advice exists for it (choose a relic, crystal sphere, ...): the player does this one screen.
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

    public async Task<CachedAdvice> FetchAdviceAsync(CancellationToken ct)
    {
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

    public void StepTaken(CachedAdvice a)
    {
        bool zh = GameText.IsChinese;
        int n = _session.Record(AutoPilotText.StepText(a.Capture.Header.Decision, a.Reply, a.Capture.Labels, zh),
                                AutoPilotText.Reason(a.Reply, zh));
        _trace?.Append(a.Capture.Header.Decision, a.Json, a.Reply.Raw, null, autopilotStep: n);
        ModLog.Info($"autopilot step {n}: {a.Capture.Header.Decision} {a.Reply.ActionName}");
    }

    /// <summary>Answers the screens a pending action opens (AutoSlayer.DrainOverlayScreensUntilAsync).</summary>
    public async Task DrainUntilAsync(Task pending, CancellationToken ct)
    {
        while (!pending.IsCompleted)
        {
            ct.ThrowIfCancellationRequested();
            var overlay = NOverlayStack.Instance?.Peek();
            if (overlay != null && overlay is not NGameOverScreen && CombatManager.Instance?.IsInProgress != true)
                await HandleOverlayAsync(overlay, ct);
            await Task.Delay(50, ct);
        }
        await pending;
    }
}
