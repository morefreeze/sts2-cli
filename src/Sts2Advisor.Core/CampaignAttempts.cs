// src/Sts2Advisor.Core/CampaignAttempts.cs
namespace Sts2Advisor.Core;

public enum AttemptVerdict { Go, Wait, Stuck }

/// <param name="Attempt">Go: the number of this attempt (1-based). Stuck / Wait: attempts made so far.</param>
/// <param name="RetryAt">Wait: the clock time (seconds) the next attempt may start.</param>
public readonly record struct AttemptResult(AttemptVerdict Kind, int Attempt, double RetryAt = 0);

/// <summary>"Retry a few times with backoff, then stuck; never loop fast." Every time the campaign wants to do
/// something (start a run, close a popup ...) it asks <see cref="Begin"/>: the first time at once, then no sooner
/// than <see cref="DelayAfter"/> seconds after the attempt before, and after <see cref="MaxAttempts"/> attempts at
/// the same thing that never made the plan move on -- stuck. A different key (another popup, another floor) is
/// a different thing and starts afresh. Main thread only.</summary>
public sealed class CampaignAttempts
{
    public const int MaxAttempts = 5;

    /// <summary>Attempts at one thing older than this are forgotten: a run later, the same step is a new thing.</summary>
    public const double ForgetAfterSeconds = 600;

    private string? _key;
    private int _attempts;
    private double _nextAt;
    private double _lastGoAt;

    /// <summary>Seconds before attempt n + 1 may start: 4, 8, 16, 32, 60.</summary>
    public static double DelayAfter(int attempt) => Math.Min(4 * Math.Pow(2, Math.Max(attempt, 1) - 1), 60);

    public AttemptResult Begin(string key, double now)
    {
        if (key != _key || (_attempts > 0 && now - _lastGoAt > ForgetAfterSeconds))
        {
            _key = key;
            _attempts = 0;
            _nextAt = 0;
        }
        if (now < _nextAt)
            return new AttemptResult(AttemptVerdict.Wait, _attempts, _nextAt);
        if (_attempts >= MaxAttempts)
            return new AttemptResult(AttemptVerdict.Stuck, _attempts);
        _attempts++;
        _lastGoAt = now;
        _nextAt = now + DelayAfter(_attempts);
        return new AttemptResult(AttemptVerdict.Go, _attempts, _nextAt);
    }

    public void Reset()
    {
        _key = null;
        _attempts = 0;
        _nextAt = 0;
    }
}

/// <summary>Runs and wins since the game started (shown on the panel).</summary>
public sealed class CampaignStats
{
    public int Runs { get; private set; }
    public int Wins { get; private set; }

    public void Record(bool victory)
    {
        Runs++;
        if (victory)
            Wins++;
    }
}
