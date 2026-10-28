// src/Sts2Advisor.Core/AutoPilotSession.cs
namespace Sts2Advisor.Core;

public enum AutoPilotMode { Advice, Running }

/// <summary>Why the autopilot handed the run back. Hotkey: the player pressed it. GameOver: the run's
/// result screen. LeftRun: no run any more (main menu, loading). Multiplayer: more than one player.
/// HandlerFailed: a handler threw or the advice did not fit the screen. Stuck: no progress (a screen that
/// will not close, nothing to do for 30 s, no settled decision, the same advice asked for too often).
/// Offline: the advice service unreachable for more than 10 s. BadReply: it answered unusably.
/// Unsupported: game API mismatch. Disabled: autopilot could not start (patches or config missing).
/// StepDone: a single step executed its one advised choice and reached the next decision.</summary>
public enum StopReason { Hotkey, GameOver, LeftRun, Multiplayer, HandlerFailed, Stuck, Offline, BadReply, Unsupported, Disabled, StepDone }

public sealed record AutoPilotStep(int Number, string Text, string? Reason);

/// <summary>The autopilot's state, kept apart from the game so it can be tested. Main thread only.</summary>
public sealed class AutoPilotSession
{
    public const int MaxSteps = 6;
    public const double OfflineLimitSeconds = 10;
    public const int MaxAdviceRetries = 5;
    public const int MaxTotalAdviceRetries = MaxAdviceRetries * 2;

    private readonly List<AutoPilotStep> _steps = new();  // newest first
    private double? _offlineSince;
    private string? _retryFingerprint;
    private int _retries;
    private int _totalRetries;

    public AutoPilotMode Mode { get; private set; }
    public StopReason? LastStop { get; private set; }
    public string? LastStopDetail { get; private set; }
    /// <summary>The screen the player has been asked to finish by hand; null when none.</summary>
    public string? HandBackScreen { get; private set; }
    public bool InCombat { get; set; }
    public IReadOnlyList<AutoPilotStep> Steps => _steps;
    public int StepCount { get; private set; }

    /// <summary>Started as a single step: stop at the next decision once one advised choice is executed.</summary>
    public bool SingleStep { get; private set; }

    /// <summary>A single step has executed its choice; the next request for advice ends it.</summary>
    public bool StepDone => SingleStep && StepCount > 0;

    /// <summary>False when already running.</summary>
    public bool Start(bool singleStep = false)
    {
        if (Mode == AutoPilotMode.Running)
            return false;
        Mode = AutoPilotMode.Running;
        SingleStep = singleStep;
        _steps.Clear();
        StepCount = 0;
        LastStop = null;
        LastStopDetail = null;
        HandBackScreen = null;
        InCombat = false;
        _offlineSince = null;
        _retryFingerprint = null;
        _retries = 0;
        _totalRetries = 0;
        return true;
    }

    public void Stop(StopReason reason, string? detail)
    {
        Mode = AutoPilotMode.Advice;
        LastStop = reason;
        LastStopDetail = detail;
        HandBackScreen = null;
        InCombat = false;
    }

    /// <summary>Records an executed step; returns its number (1-based, over the whole session).</summary>
    public int Record(string text, string? reason)
    {
        StepCount++;
        _steps.Insert(0, new AutoPilotStep(StepCount, text, reason));
        if (_steps.Count > MaxSteps)
            _steps.RemoveAt(_steps.Count - 1);
        _retryFingerprint = null;
        _retries = 0;
        _totalRetries = 0;
        return StepCount;
    }

    public void HandBack(string screen) => HandBackScreen = screen;

    public void ClearHandBack() => HandBackScreen = null;

    /// <summary>One failed request at <paramref name="now"/> (seconds); true once the outage is longer than 10 s.</summary>
    public bool Offline(double now)
    {
        _offlineSince ??= now;
        return now - _offlineSince.Value > OfflineLimitSeconds;
    }

    public void Online() => _offlineSince = null;

    /// <summary>One more "ask for advice, then rerun the handler" on this decision; false past the cap
    /// for one decision (its advice never fits) or past the total cap since the last step (the screen
    /// keeps changing under the handler).</summary>
    public bool CountAdviceRetry(string fingerprint)
    {
        if (fingerprint != _retryFingerprint)
        {
            _retryFingerprint = fingerprint;
            _retries = 0;
        }
        _totalRetries++;
        return ++_retries <= MaxAdviceRetries && _totalRetries <= MaxTotalAdviceRetries;
    }
}
