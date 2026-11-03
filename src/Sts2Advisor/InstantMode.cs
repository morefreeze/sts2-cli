// src/Sts2Advisor/InstantMode.cs
using MegaCrit.Sts2.Core.Nodes;
using MegaCrit.Sts2.Core.Runs;
using MegaCrit.Sts2.Core.Saves;
using MegaCrit.Sts2.Core.Settings;

namespace Sts2Advisor;

/// <summary>Config <c>instant_mode</c>: keeps the game's Instant speed on. Release builds reset
/// <c>PrefsSave.FastMode</c> from Instant to Fast in NGame's init; only the dev console's
/// <c>instant</c> command sets it again, which is the one line applied here. Main thread only.
/// Never touches anything when the option is false.</summary>
internal static class InstantMode
{
    private const double CheckSeconds = 3;

    private static bool _enabled, _failed, _loggedOn;
    private static double _nextCheck;

    public static void Init(bool enabled) => _enabled = enabled;

    public static void Tick(double now)
    {
        if (!_enabled || _failed || now < _nextCheck) return;
        _nextCheck = now + CheckSeconds;
        try
        {
            // The startup reset has run once the main menu (or a run) exists.
            var game = NGame.Instance;
            if (game == null) return;
            bool up = game.MainMenu != null || RunManager.Instance is { IsInProgress: true };
            if (!up) return;
            var prefs = SaveManager.Instance?.PrefsSave;
            if (prefs == null || prefs.FastMode == FastModeType.Instant) return;
            var before = prefs.FastMode;
            prefs.FastMode = FastModeType.Instant;
            ModLog.Info(_loggedOn ? $"instant mode re-applied (game had set {before})" : "instant mode on");
            _loggedOn = true;
        }
        catch (Exception ex)
        {
            _failed = true;  // do not retry (and re-log) a broken API every few seconds
            ModLog.Error($"instant_mode disabled: {ex}");
        }
    }
}
