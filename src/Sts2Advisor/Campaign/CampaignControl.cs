// src/Sts2Advisor/Campaign/CampaignControl.cs
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.AutoPilot;
using Sts2Advisor.Core;

namespace Sts2Advisor.Campaign;

/// <summary>Campaign mode: plays run after run unattended, steered by &lt;user dir&gt;/campaign.json (see
/// <see cref="CampaignFile"/>) and logging every run to campaign_runs.jsonl. Once a second it re-reads the file,
/// looks at the game (<see cref="CampaignObserver"/>), asks <see cref="CampaignPlan"/> what comes next and does it:
/// start full auto when a run is in progress, record the run when it ends and click through to the main menu, then
/// continue the saved run or start the next one. One action at a time; a multi-step one runs as an async flow on
/// the main thread (like the autopilot loop). Nothing is retried quickly: see <see cref="CampaignAttempts"/>.
/// Main thread only.</summary>
internal static class CampaignControl
{
    private const double TickSeconds = 1.0;
    private const double ErrorLogSeconds = 60;

    /// <summary>Watchdogs count ticks (frames the game really ran), not clock time, so a sleeping laptop trips nothing.
    /// A flow (the longest legitimate one is the game-over click-through) that has not finished after this many ticks
    /// is given up on ...</summary>
    private const int FlowWatchdogTicks = 900;

    /// <summary>... and so is a game that shows nothing the campaign recognizes (loading, fading) for this many.</summary>
    private const int UnknownWatchdogTicks = 300;

    private static readonly CampaignStats Stats = new();
    private static readonly CampaignAttempts Attempts = new();
    private static readonly HashSet<string> RestartedFloors = new();

    private static bool _initialized;
    private static string _filePath = "", _runsPath = "";
    private static AdvisorTraceLog? _trace;
    private static Func<double> _now = () => 0;

    private static CampaignRead _read = new(null, null, "");
    private static string? _lastRaw;
    private static double _nextTickAt;
    private static bool _busy;
    private static string _flowName = "";
    private static int _busyTicks, _unknownTicks;
    private static CancellationTokenSource? _cts;
    private static bool _flowIsQuit;

    private static string? _stuck;             // gave up; stays so until campaign.json changes
    private static bool _stopped;              // "command": "stop"; likewise
    private static string? _note;              // what the panel says it is doing or waiting for
    private static string? _lastIdleWhy;

    private static int _stopBaseline;          // autopilot stops up to here are not about the current run / file
    private static RunState? _trackedRun;
    private static RunState? _recordedRun;
    private static string? _lastRunCharacter;  // the run that ended last: where campaign.json's "rotation" goes on from
    private static LastRun? _lastRun;          // ... and whether it was won (the ladder repeats a character after a loss)
    private static readonly CampaignHistoryScan History = new();
    private static LadderState? _ladder;       // the ladder strategy's input, refreshed every tick while it is the strategy
    private static string _modalKey = "";
    private static double _modalSince;
    private static double _submenuSince = -1;
    private static string? _handBack;
    private static double _handBackSince;

    private static string? _errorKey;
    private static double _errorLoggedAt = double.NegativeInfinity;

    /// <summary>The file asks for a campaign, or is so broken that the panel should say so.</summary>
    public static bool Active => _initialized && (_read.Settings is { Enabled: true } || _read.Problem != null);

    public static void Init(string userDir, AdvisorTraceLog? trace, Func<double> now)
    {
        _filePath = Path.Combine(userDir, CampaignFile.FileName);
        _runsPath = Path.Combine(userDir, CampaignRunLog.FileName);
        _trace = trace;
        _now = now;
        _initialized = true;
        _lastRun = CampaignRunLog.LastRunOf(_runsPath);
        _lastRunCharacter = _lastRun?.Character;
        ModLog.Info($"campaign: control file {_filePath}, run log {_runsPath}; last recorded run {_lastRunCharacter ?? "none"}");
    }

    /// <summary>The panel's campaign line; null when there is no campaign.</summary>
    public static PanelLine? StatusLine(bool zh)
    {
        if (!_initialized)
            return null;
        var s = _read.Settings;
        string? note = _stopped ? "stopped by \"command\": \"stop\" until campaign.json changes" : _note;
        var next = CampaignPlan.NextTarget(s, _lastRunCharacter, _ladder);
        string? ladder = null;
        if (s is { Enabled: true, Ladder: { } characters } && _ladder != null)
            ladder = CampaignLadder.Pick(characters, _ladder).Complete ? "complete" : CampaignLadder.Summary(characters, _ladder);
        if (next != null && s?.Ladder != null && _ladder != null)  // the ladder starts at the character's level: say which
            next = next.Value with { Ascension = AscensionSpec.Of(CampaignLadder.LevelOf(_ladder.Levels, next.Value.Character)) };
        return CampaignText.Status(new CampaignView(s is { Enabled: true }, next == null ? null : GameText.Character(next.Value.Character), next?.Ascension, Stats.Runs, Stats.Wins,
                                                    note, _read.Problem, _stuck, ladder), zh);
    }

    /// <summary>Called every frame; acts about once a second.</summary>
    public static void Tick()
    {
        if (!_initialized)
            return;
        double now = _now();
        if (now < _nextTickAt)
            return;
        _nextTickAt = now + TickSeconds;
        try
        {
            TickInner(now);
        }
        catch (Exception e) when (e is TypeLoadException or MissingMemberException)
        {
            throw;  // the game changed: AdvisorRuntime.Tick switches the whole mod off
        }
        catch (Exception e)
        {
            LogTickError(e, now);
        }
    }

    private static void TickInner(double now)
    {
        var read = CampaignFile.Load(_filePath);
        if (read.Raw != _lastRaw)
            FileChanged(read);
        _read = read;
        if (read.Settings is not { Enabled: true } settings)
        {
            _note = null;
            _ladder = null;
            if (CampaignPlan.InterruptsFlow(read, _flowIsQuit))  // switched off; a half-written file does not count
                CancelFlow();
            return;
        }
        RefreshLadder(settings);
        if (_busy)
        {
            if (++_busyTicks > FlowWatchdogTicks)
            {
                Stuck($"{_flowName} has not finished after {FlowWatchdogTicks} s");
                CancelFlow();
                _cts = null;
                _busy = false;  // the orphaned flow can still finish; the stuck latch keeps the campaign from acting on it
            }
            else if (CampaignPlan.InterruptsFlow(read, _flowIsQuit))
            {
                // "quit" and "stop" preempt a flow; a new "next" or "rotation" does not (it is read when it is needed).
                // A cancelled flow ends at its next await; the tick after that acts on the command.
                CancelFlow();
            }
            return;
        }
        _busyTicks = 0;
        if (_stuck != null || _stopped)
            return;

        var obs = CampaignObserver.Observe();
        // A running autopilot has its own watchdogs (and a custom event screen can read as unknown for a while).
        if (obs.Screen != CampaignScreen.Unknown || AutoPilotControl.IsRunning)
            _unknownTicks = 0;
        else if (++_unknownTicks > UnknownWatchdogTicks)
        {
            Stuck($"the game has shown nothing the campaign recognizes for {UnknownWatchdogTicks} s (loading? a transition that never ends?)");
            return;
        }
        TrackRun(obs);
        var facts = FactsOf(obs, now);
        var step = CampaignPlan.Next(settings, facts);
        if (step.Kind == CampaignStepKind.Idle)
        {
            Idle(step.Why);
            return;
        }

        var attempt = Attempts.Begin($"{step.Kind}:{step.Key}", now);
        switch (attempt.Kind)
        {
            case AttemptVerdict.Wait:
                _note = $"{step.Why} (trying again in {Math.Ceiling(attempt.RetryAt - now):0} s)";
                return;
            case AttemptVerdict.Stuck:
                Stuck($"{step.Kind} ({step.Why}) had no effect after {attempt.Attempt} attempts");
                return;
        }
        _lastIdleWhy = null;
        _note = step.Why;
        Execute(step, facts, attempt.Attempt);
    }

    /// <summary>The ladder strategy's input: the levels and the characters that already won at ascension 10 (from the
    /// game's run histories). A failure leaves it null, and the plan then waits rather than guessing.</summary>
    private static void RefreshLadder(CampaignSettings settings)
    {
        if (settings.Ladder is not { } characters)
        {
            _ladder = null;
            return;
        }
        try
        {
            _ladder = new LadderState(CampaignActions.LadderLevels(characters), History.Cleared(CampaignActions.HistoryDirectory()), _lastRun);
        }
        catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
        {
            _ladder = null;
            LogTickError(e, _now());
        }
    }

    // -- the file -----------------------------------------------------------------------------------------

    private static void FileChanged(CampaignRead read)
    {
        _lastRaw = read.Raw;
        _stuck = null;
        _stopped = false;
        _lastIdleWhy = null;
        _stopBaseline = AutoPilotControl.StopCount;  // an old hand stop does not outlive an edit of the file
        Attempts.Reset();
        if (read.Problem != null)
            ModLog.Error($"campaign: {read.Problem}; the campaign is off until it is fixed");
        else if (read.Settings is not { Enabled: true })
            ModLog.Info(read.Raw.Length == 0 ? "campaign: no control file content: off" : "campaign: disabled in the control file: off");
        else
        {
            var s = read.Settings;
            if (s.Ladder is { } ladder)
            {
                ModLog.Info($"campaign: enabled; strategy ladder over {string.Join(", ", ladder)} (\"next\" and \"rotation\" are ignored); command {s.Command}");
                return;
            }
            string rotation = s.Rotation is { Count: > 0 } r
                ? $"; rotation {string.Join(", ", r.Select(t => $"{t.Character} {CampaignText.Level(t.Ascension)}"))} (next {CampaignPlan.NextTarget(s, _lastRunCharacter)?.Character})"
                : "";
            ModLog.Info($"campaign: enabled; next {(s.Character == null ? "none" : $"{s.Character} {CampaignText.Level(s.Ascension)}")}{rotation}; command {s.Command}");
        }
    }

    // -- facts --------------------------------------------------------------------------------------------

    private static void TrackRun(Observation obs)
    {
        if (obs.Run != null && !ReferenceEquals(obs.Run, _trackedRun))
        {
            _trackedRun = obs.Run;
            _stopBaseline = AutoPilotControl.StopCount;
            RestartedFloors.Clear();
            Attempts.Reset();
            ModLog.Info($"campaign: run in progress at {obs.Floor}a{obs.Run.AscensionLevel}");
        }
        else if (obs.Run == null && obs.Screen is CampaignScreen.MainMenu or CampaignScreen.Submenu)
        {
            _trackedRun = null;
        }
    }

    private static CampaignFacts FactsOf(Observation obs, double now)
    {
        if (obs.Screen == CampaignScreen.Modal)
        {
            if (obs.ModalKey != _modalKey)
            {
                _modalKey = obs.ModalKey;
                _modalSince = now;
            }
        }
        else
        {
            _modalKey = "";
        }
        if (obs.Screen == CampaignScreen.Submenu)
        {
            if (_submenuSince < 0)
                _submenuSince = now;
        }
        else
        {
            _submenuSince = -1;
        }

        string? handBack = AutoPilotControl.HandBackScreen;
        if (handBack == "modal")  // popups are the campaign's CloseModal, not a screen for AutoSlay
            handBack = null;
        if (handBack != _handBack)
        {
            _handBack = handBack;
            _handBackSince = now;
        }

        var stop = CampaignStop.None;
        string? stopText = null;
        if (AutoPilotControl.StopCount > _stopBaseline && !AutoPilotControl.IsRunning && AutoPilotControl.LastStop is { } reason)
        {
            stop = CampaignPlan.Classify(reason);
            stopText = AutoPilotControl.LastStopDetail is { Length: > 0 } d ? $"{reason}: {d}" : reason.ToString();
        }
        return new CampaignFacts(
            obs.Screen,
            AutopilotRunning: AutoPilotControl.IsRunning,
            Multiplayer: obs.Multiplayer,
            HasSavedRun: obs.HasSavedRun,
            LastStop: stop,
            StopText: stopText,
            SecondsSinceStop: AutoPilotControl.SecondsSinceStop,
            RestartedThisFloor: RestartedFloors.Contains(obs.Floor),
            Floor: obs.Floor,
            ModalSeconds: obs.Screen == CampaignScreen.Modal ? now - _modalSince : 0,
            ModalKey: obs.ModalKey,
            SubmenuSeconds: _submenuSince < 0 ? 0 : now - _submenuSince,
            HandBackScreen: handBack,
            HandBackSeconds: handBack == null ? 0 : now - _handBackSince,
            LastRunCharacter: _lastRunCharacter,
            Ladder: _ladder);
    }

    // -- doing --------------------------------------------------------------------------------------------

    private static void Execute(CampaignStep step, CampaignFacts facts, int attempt)
    {
        switch (step.Kind)
        {
            case CampaignStepKind.StartAutopilot:
                bool restart = facts.LastStop is CampaignStop.Retriable or CampaignStop.Service;
                if (AutoPilotControl.StartFullAuto())
                {
                    ModLog.Info($"campaign: {(restart ? "restarting" : "starting")} the autopilot at {facts.Floor}");
                    if (facts.LastStop == CampaignStop.Retriable)  // once per floor; an unreachable service is retried by attempts
                        RestartedFloors.Add(facts.Floor);
                }
                else
                {
                    ModLog.Info($"campaign: the autopilot did not start ({AutoPilotControl.WhyUnavailable}); attempt {attempt}");
                }
                break;

            case CampaignStepKind.StopAutopilot:
                AutoPilotControl.Stop(StopReason.Hotkey, "campaign.json: stop");
                _stopped = true;
                ModLog.Info("campaign: command stop: the autopilot is off; nothing happens until campaign.json changes");
                break;

            case CampaignStepKind.CloseModal:
                try
                {
                    ModLog.Info($"campaign: closing a popup that has been open for a while: {CampaignActions.CloseModal()}");
                }
                catch (CampaignStuckException e)
                {
                    Stuck(e.Message);
                }
                catch (Exception e)
                {
                    ModLog.Error($"campaign: cannot close the popup (attempt {attempt}): {e.Message}");
                }
                break;

            case CampaignStepKind.FinishRun:
                StartFlow("finish run", FinishRunAsync);
                break;
            case CampaignStepKind.ContinueRun:
                StartFlow("continue run", CampaignActions.ContinueRunAsync);
                break;
            case CampaignStepKind.StartRun:
                var target = step.Target!.Value;  // "rotation"'s entry after the run that ended, else "next"
                if (step.Why.StartsWith("ladder pick", StringComparison.Ordinal))
                    ModLog.Info($"campaign: {step.Why}");
                StartFlow("start run", ct => CampaignActions.StartRunAsync(target.Character, target.Ascension, ct));
                break;
            case CampaignStepKind.LeaveSubmenu:
                StartFlow("leave menu screen", CampaignActions.LeaveSubmenuAsync);
                break;
            case CampaignStepKind.HandleScreen:
                StartFlow("handle a screen nobody answered", CampaignActions.HandleScreenAsync);
                break;
            case CampaignStepKind.Quit:
                // On the game-over screen the ended run is recorded first (its data is gone once the screen is left);
                // the quit itself needs no click-through.
                try
                {
                    RecordEndedRun();
                }
                catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
                {
                    ModLog.Error($"campaign: could not record the ended run before quitting: {e.Message}");  // never blocks the quit
                }
                AutoPilotControl.Stop(StopReason.Hotkey, "campaign.json: quit");
                StartFlow("quit", ct => CampaignActions.QuitAsync(_filePath, ct), isQuit: true);
                break;
        }
    }

    /// <summary>Resumable: the run is recorded once (a later attempt finds it recorded), and the click-through looks at
    /// the screen each time (<see cref="CampaignActions.LeaveGameOverAsync"/>), so an attempt that follows an
    /// interrupted one carries on from wherever the screen is.</summary>
    private static async Task FinishRunAsync(CancellationToken ct)
    {
        RecordEndedRun();
        if (AutoPilotControl.IsRunning)
            AutoPilotControl.Stop(StopReason.GameOver, null);
        await CampaignActions.LeaveGameOverAsync(ct);
    }

    /// <summary>Records the run on the game-over screen, if it is up and the run was not recorded yet.</summary>
    private static void RecordEndedRun()
    {
        var obs = CampaignObserver.Observe();
        if (obs.Run != null && CampaignObserver.GameOver() != null && !ReferenceEquals(obs.Run, _recordedRun))
            RecordRun(obs.Run);
    }

    /// <summary>One line in campaign_runs.jsonl and a run_end line in the advisor trace, once per run. The run's data
    /// is read off the game-over screen's run (gone once the screen is left).</summary>
    private static void RecordRun(RunState run)
    {
        _recordedRun = run;
        CampaignRunRecord record;
        try
        {
            record = RunEndRecorder.Capture();
        }
        catch (Exception e) when (e is not (TypeLoadException or MissingMemberException))
        {
            ModLog.Error($"campaign: could not read the finished run: {e}");
            return;
        }
        Stats.Record(record.Victory);
        _lastRunCharacter = record.Character;
        _lastRun = new LastRun(record.Character, record.Victory);
        string json = CampaignRunLog.ToJson(record, DateTime.UtcNow);
        bool written = CampaignRunLog.Append(_runsPath, json);
        _trace?.Append("run_end", json, null, written ? null : "campaign_runs_append_failed");
        ModLog.Info($"campaign: run ended victory={record.Victory.ToString().ToLowerInvariant()} A{record.Act}F{record.Floor}a{record.Ascension} " +
                    $"({record.Character}, total floor {record.TotalFloor}, hp {record.Hp}/{record.MaxHp}, killed by {record.KilledBy ?? record.KilledByEvent ?? "-"})" +
                    (written ? "" : $"; WRITING {_runsPath} FAILED"));
    }

    private static void StartFlow(string name, Func<CancellationToken, Task> flow, bool isQuit = false)
    {
        _busy = true;
        _busyTicks = 0;
        _flowName = name;
        _flowIsQuit = isQuit;
        var cts = new CancellationTokenSource();
        _cts = cts;
        _ = RunFlowAsync(name, flow, cts);
    }

    private static async Task RunFlowAsync(string name, Func<CancellationToken, Task> flow, CancellationTokenSource cts)
    {
        try
        {
            await flow(cts.Token);
        }
        catch (OperationCanceledException) when (cts.IsCancellationRequested)
        {
            ModLog.Info($"campaign: {name} cancelled");
        }
        catch (CampaignStuckException e)
        {
            Stuck(e.Message);
        }
        catch (Exception e) when (e is TypeLoadException or MissingMemberException)
        {
            Stuck($"{name}: the game changed under the campaign ({e.GetType().Name}: {e.Message})");
        }
        catch (Exception e)
        {
            ModLog.Error($"campaign: {name} failed: {e.GetType().Name}: {e.Message}");
            _note = $"{name} failed: {e.Message}";
        }
        finally
        {
            if (ReferenceEquals(cts, _cts))
            {
                _cts = null;
                _busy = false;
            }
            cts.Dispose();
        }
    }

    private static void CancelFlow()
    {
        try { _cts?.Cancel(); } catch (ObjectDisposedException) { }
    }

    // -- saying -------------------------------------------------------------------------------------------

    private static void Idle(string why)
    {
        _note = why;
        if (why == _lastIdleWhy)
            return;
        _lastIdleWhy = why;
        ModLog.Info(why.StartsWith("needs attention", StringComparison.Ordinal) || why == "ladder complete" ? $"campaign: {why}" : $"campaign: waiting: {why}");
    }

    private static void Stuck(string why)
    {
        _stuck = why;
        ModLog.Error($"campaign: stuck: {why}");
    }

    private static void LogTickError(Exception e, double now)
    {
        string key = e.GetType().Name + e.Message;
        if (key == _errorKey && now - _errorLoggedAt < ErrorLogSeconds)
            return;
        _errorKey = key;
        _errorLoggedAt = now;
        ModLog.Error($"campaign: tick failed: {e}");
    }
}
