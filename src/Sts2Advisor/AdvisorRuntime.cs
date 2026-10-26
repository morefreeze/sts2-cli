// src/Sts2Advisor/AdvisorRuntime.cs  (Task C5 placeholder; Task C7 replaces this whole file)
using Sts2Advisor.Core;

namespace Sts2Advisor;

internal static class AdvisorRuntime
{
    private static bool _firstTickLogged;

    public static void Start(AdvisorConfig config, string configPath, string userDir) { }

    public static void Tick()
    {
        if (_firstTickLogged) return;
        _firstTickLogged = true;
        ModLog.Info("first frame tick");
    }
}
