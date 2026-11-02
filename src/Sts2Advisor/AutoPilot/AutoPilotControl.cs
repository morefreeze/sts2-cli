// src/Sts2Advisor/AutoPilot/AutoPilotControl.cs
using System.Text.Json;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>Switches the autopilot on and off (the hotkey) and says what it is doing. Main thread only.</summary>
internal static class AutoPilotControl
{
    private const double BannerSeconds = 15;

    private static readonly AutoPilotSession Session = new();
    private static IAdvisor? _advisor;
    private static LocLookup? _loc;
    private static AdvisorTraceLog? _trace;
    private static string _gameUserDir = "";
    private static Func<double> _now = () => 0;
    private static double _stepPauseSeconds = AdvisorConfig.DefaultStepPauseSeconds;
    private static string? _unavailable;
    private static CancellationTokenSource? _cts;
    private static Task? _runTask;
    private static bool _startPending;
    private static bool _pendingSingle;
    private static CombatSolverStatus _combatSolver = CombatSolverStatus.Ok;
    private static double _stoppedAt = double.NegativeInfinity;

    /// <summary>The loop hit a TypeLoad/MissingMember: the game changed, and the whole mod must go quiet.</summary>
    public static bool ApiMismatch { get; private set; }

    /// <summary>Whether a start would run (else it only shows why not): the panel hides its buttons otherwise.</summary>
    public static bool Available => _unavailable == null && _loc != null && _advisor != null && !ApiMismatch;

    public static bool IsRunning => Session.Mode == AutoPilotMode.Running;

    /// <summary>The screen the running autopilot has handed to the player, if any ("modal" for a popup).</summary>
    public static string? HandBackScreen => IsRunning ? Session.HandBackScreen : null;

    /// <summary>Why <see cref="Available"/> is false.</summary>
    public static string WhyUnavailable =>
        _unavailable ?? (ApiMismatch ? "game API mismatch" : _loc == null ? "no localization" : _advisor == null ? "no advice service" : "available");

    /// <summary>How many times the autopilot has stopped since the game started (the campaign tells new stops from old).</summary>
    public static int StopCount { get; private set; }

    /// <summary>Why it last stopped; null before the first stop and while a start is running.</summary>
    public static StopReason? LastStop => Session.LastStop;

    public static string? LastStopDetail => Session.LastStopDetail;

    public static double SecondsSinceStop => _now() - _stoppedAt;

    /// <summary>Starts full auto (as the hotkey does) unless it runs already. False when it cannot run: the autopilot
    /// is unavailable, or the previous run has not finished stopping.</summary>
    public static bool StartFullAuto()
    {
        if (IsRunning)
            return true;
        if (!Available || _runTask is { IsCompleted: false })
            return false;
        Start(singleStep: false);
        return IsRunning;
    }

    /// <summary><paramref name="unavailable"/>: why the autopilot cannot run (handlers not patched, no
    /// localization); null when it can.</summary>
    public static void Init(IAdvisor advisor, LocLookup? loc, AdvisorTraceLog? trace, string gameUserDir,
                            Func<double> now, double stepPauseSeconds, string? unavailable)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _gameUserDir = gameUserDir;
        _now = now;
        _stepPauseSeconds = stepPauseSeconds;
        _unavailable = unavailable;
    }

    public static void Toggle(bool singleStep = false)
    {
        if (IsRunning)
            Stop(StopReason.Hotkey, null);
        else if (_runTask is { IsCompleted: false })
        {
            // The previous run has not finished stopping: start when it has (a further press cancels that).
            if (_startPending && _pendingSingle != singleStep)  // the other mode: switch, keep waiting
            {
                _pendingSingle = singleStep;
                ModLog.Info($"autopilot: pending start is now {(singleStep ? "single step" : "full auto")}");
            }
            else
            {
                _startPending = !_startPending;
                _pendingSingle = singleStep;
                ModLog.Info(_startPending ? "autopilot: previous run still stopping, will start when it has" : "autopilot: pending start cancelled");
            }
        }
        else
            Start(singleStep);
    }

    /// <summary>A panel button: Step / Auto start that mode; Stop stops whichever runs.</summary>
    public static void Press(PanelAction action)
    {
        if (action == PanelAction.Stop) Stop(StopReason.Hotkey, null);
        else Toggle(singleStep: action == PanelAction.Step);
    }

    private static void Start(bool singleStep, bool previousRunDone = false)
    {
        if (ApiMismatch || _unavailable != null || _loc == null || _advisor == null)
        {
            Session.Stop(StopReason.Disabled, _unavailable ?? (ApiMismatch ? "game API mismatch" : "no localization"));
            _stoppedAt = _now();
            return;
        }
        if (!previousRunDone && _runTask is { IsCompleted: false })
        {
            ModLog.Info("autopilot: previous run still stopping");
            return;
        }
        if (!Session.Start(singleStep))
            return;
        _combatSolver = CombatSolverSettings.Read(Path.Combine(_gameUserDir, CombatSolverSettings.FileName));
        var cts = new CancellationTokenSource();
        _cts = cts;
        ModLog.Info($"autopilot: started ({(singleStep ? "single step" : "full auto")}, CombatSolver {_combatSolver})");
        var loop = new AutoPilotLoop(_advisor, _loc, _trace, Session, _now, _stepPauseSeconds);
        _runTask = RunAsync(loop, cts);
    }

    private static async Task RunAsync(AutoPilotLoop loop, CancellationTokenSource cts)
    {
        try
        {
            var (reason, detail) = await loop.RunAsync(cts.Token);
            if (reason == StopReason.Unsupported)
                ApiMismatch = true;
            if (ReferenceEquals(cts, _cts))  // a hotkey Stop has already finished this session
                Finish(reason, detail);
        }
        finally
        {
            cts.Dispose();
            if (_startPending)
            {
                _startPending = false;
                try { Start(_pendingSingle, previousRunDone: true); }
                catch (Exception e) { ModLog.Error($"autopilot: pending start failed: {e}"); }
            }
        }
    }

    public static void Stop(StopReason reason, string? detail)
    {
        _startPending = false;
        if (!IsRunning)
            return;
        var cts = _cts;
        Finish(reason, detail);
        try { cts?.Cancel(); } catch (ObjectDisposedException) { }
    }

    private static void Finish(StopReason reason, string? detail)
    {
        StopCount++;
        _cts = null;
        Session.Stop(reason, detail);
        _stoppedAt = _now();
        ModLog.Info($"autopilot: stopped ({reason}{(detail == null ? "" : ": " + detail)}) after {Session.StepCount} step(s)");
        try
        {
            _trace?.Append("autopilot_stop", JsonSerializer.Serialize(new { reason = reason.ToString(), detail, steps = Session.StepCount }),
                           null, reason.ToString());
        }
        catch (Exception e)
        {
            ModLog.Error($"autopilot: trace line for the stop failed: {e.Message}");
        }
    }

    public static IReadOnlyList<PanelLine> RunningLines(string autoKey, string stepKey, bool zh) =>
        AutoPilotText.Running(Session, autoKey, stepKey, _combatSolver, zh);

    /// <summary>The last stop, for 15 s after it (prepended to the advice panel).</summary>
    public static PanelLine? Banner(string autoKey, string stepKey, bool zh) =>
        !IsRunning && Session.LastStop is { } reason && _now() - _stoppedAt < BannerSeconds
            ? AutoPilotText.StopBanner(reason, Session.LastStopDetail, autoKey, stepKey, zh)
            : null;
}
