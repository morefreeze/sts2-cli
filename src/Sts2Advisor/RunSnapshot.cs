// src/Sts2Advisor/RunSnapshot.cs
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>The identity of the run in progress as a ready-made JSON string (<see cref="RunIdentity"/>), for the
/// <c>"run"</c> key of every advisor_trace.jsonl line. <see cref="Refresh"/> reads the game and runs on the main
/// thread only; <see cref="Current"/> is a volatile string, so the trace log's background thread never touches game
/// state. No run in progress = null. While the run is being cleaned up the previous snapshot is kept: the
/// campaign's run_end line is written then and must still say which run it was.</summary>
internal static class RunSnapshot
{
    private static volatile string? _json;
    private static (long, string?, string?, int)? _last;
    private static readonly HashSet<string> Logged = new();

    public static string? Current => _json;

    public static void Refresh()
    {
        try
        {
            var rm = RunManager.Instance;
            if (rm is not { IsInProgress: true })
            {
                _last = null;
                _json = null;
                return;
            }
            if (rm.IsCleaningUp) return;  // keep the previous snapshot
            var rs = rm.DebugOnlyGetState();
            if (rs == null) return;
            var player = Read("player", () => LocalContext.GetMe(rs) ?? rs.Players.First(), null);
            long startTime = Read("start time", () => rm._startTime, 0L);
            string? seed = Read("seed", () => rs.Rng.StringSeed, null);
            string? character = player == null ? null : Read("character", () => player.Character.Id.Entry, null);
            int ascension = Read("ascension", () => (int)rs.AscensionLevel, 0);
            var key = (startTime, seed, character, ascension);
            if (_last == key) return;
            _last = key;
            _json = RunIdentity.ToJson(startTime, seed, character, ascension);
        }
        catch (Exception ex) when (ex is TypeLoadException or MissingMemberException)
        {
            LogOnce("api", ex);
        }
        catch (Exception ex)
        {
            LogOnce("refresh", ex);
        }
    }

    private static T Read<T>(string what, Func<T> get, T fallback)
    {
        try
        {
            return get();
        }
        catch (Exception ex)
        {
            LogOnce(what, ex);
            return fallback;
        }
    }

    private static void LogOnce(string what, Exception ex)
    {
        if (Logged.Add(what))
            ModLog.Error($"run snapshot: cannot read {what}: {ex.GetType().Name}: {ex.Message}");
    }
}
