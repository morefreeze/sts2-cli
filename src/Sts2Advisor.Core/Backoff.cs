namespace Sts2Advisor.Core;

/// <summary>Reconnect schedule for the advice service: 1 s, 2 s, 4 s ... capped at 30 s.</summary>
public sealed class Backoff
{
    private readonly double _first;
    private readonly double _max;
    private double _readyAt;

    public Backoff(double firstSeconds = 1, double maxSeconds = 30)
    {
        _first = firstSeconds;
        _max = maxSeconds;
    }

    public double CurrentDelay { get; private set; }

    public bool Ready(double nowSeconds) => nowSeconds >= _readyAt;

    public void Failed(double nowSeconds)
    {
        CurrentDelay = CurrentDelay == 0 ? _first : Math.Min(CurrentDelay * 2, _max);
        _readyAt = nowSeconds + CurrentDelay;
    }

    public void Succeeded()
    {
        CurrentDelay = 0;
        _readyAt = 0;
    }
}
