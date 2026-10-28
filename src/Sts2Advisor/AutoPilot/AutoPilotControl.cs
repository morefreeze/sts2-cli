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
    private static string? _unavailable;
    private static CancellationTokenSource? _cts;
    private static Task? _runTask;
    private static CombatSolverStatus _combatSolver = CombatSolverStatus.Ok;
    private static double _stoppedAt = double.NegativeInfinity;

    public static bool IsRunning => Session.Mode == AutoPilotMode.Running;

    /// <summary><paramref name="unavailable"/>: why the autopilot cannot run (handlers not patched, no
    /// localization); null when it can.</summary>
    public static void Init(IAdvisor advisor, LocLookup? loc, AdvisorTraceLog? trace, string gameUserDir,
                            Func<double> now, string? unavailable)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _gameUserDir = gameUserDir;
        _now = now;
        _unavailable = unavailable;
    }

    public static void Toggle()
    {
        if (IsRunning) Stop(StopReason.Hotkey, null);
        else Start();
    }

    private static void Start()
    {
        if (_unavailable != null || _loc == null || _advisor == null)
        {
            Session.Stop(StopReason.Disabled, _unavailable ?? "no localization");
            _stoppedAt = _now();
            return;
        }
        if (_runTask is { IsCompleted: false })
        {
            ModLog.Info("autopilot: previous run still stopping");
            return;
        }
        if (!Session.Start())
            return;
        _combatSolver = CombatSolverSettings.Read(Path.Combine(_gameUserDir, CombatSolverSettings.FileName));
        var cts = new CancellationTokenSource();
        _cts = cts;
        ModLog.Info($"autopilot: started (CombatSolver {_combatSolver})");
        var loop = new AutoPilotLoop(_advisor, _loc, _trace, Session, _now);
        _runTask = RunAsync(loop, cts);
    }

    private static async Task RunAsync(AutoPilotLoop loop, CancellationTokenSource cts)
    {
        try
        {
            var (reason, detail) = await loop.RunAsync(cts.Token);
            if (ReferenceEquals(cts, _cts))  // a hotkey Stop has already finished this session
                Finish(reason, detail);
        }
        finally
        {
            cts.Dispose();
        }
    }

    public static void Stop(StopReason reason, string? detail)
    {
        if (!IsRunning)
            return;
        var cts = _cts;
        Finish(reason, detail);
        try { cts?.Cancel(); } catch (ObjectDisposedException) { }
    }

    private static void Finish(StopReason reason, string? detail)
    {
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

    public static IReadOnlyList<PanelLine> RunningLines(string hotkey, bool zh) =>
        AutoPilotText.Running(Session, hotkey, _combatSolver, zh);

    /// <summary>The last stop, for 15 s after it (prepended to the advice panel).</summary>
    public static PanelLine? Banner(string hotkey, bool zh) =>
        !IsRunning && Session.LastStop is { } reason && _now() - _stoppedAt < BannerSeconds
            ? AutoPilotText.StopBanner(reason, Session.LastStopDetail, hotkey, zh)
            : null;
}
