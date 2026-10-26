// src/Sts2Advisor/SidecarProcess.cs
using System.Diagnostics;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>Starts agent/advisor_server.py once per game launch, unless a healthy service already
/// answers on the port (then it is reused). Its stderr goes to Sts2Advisor.log.</summary>
internal static class SidecarProcess
{
    private static Process? _process;

    public static async Task EnsureStartedAsync(AdvisorConfig config, IAdvisor advisor)
    {
        try
        {
            if (await advisor.IsHealthyAsync(CancellationToken.None).ConfigureAwait(false))
            {
                ModLog.Info($"advice service already running on port {config.Port}; reusing it");
                return;
            }
            if (!config.Autostart)
            {
                ModLog.Info("autostart is off; waiting for an advice service");
                return;
            }
            string script = Path.Combine(config.RepoPath, "agent", "advisor_server.py");
            string python = config.ResolvedPython;
            if (!File.Exists(script) || !File.Exists(python))
            {
                ModLog.Error($"cannot start the advice service: python={python} script={script}");
                return;
            }
            var info = new ProcessStartInfo(python)
            {
                WorkingDirectory = config.RepoPath,
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
            };
            info.ArgumentList.Add(script);
            info.ArgumentList.Add("--port");
            info.ArgumentList.Add(config.Port.ToString());
            _process = Process.Start(info)!;
            _process.ErrorDataReceived += (_, e) => { if (e.Data != null) ModLog.Info("[advisor_server] " + e.Data); };
            _process.OutputDataReceived += (_, e) => { if (e.Data != null) ModLog.Info("[advisor_server] " + e.Data); };
            _process.BeginErrorReadLine();
            _process.BeginOutputReadLine();
            AppDomain.CurrentDomain.ProcessExit += (_, _) => Stop();
            ModLog.Info($"started advice service, pid {_process.Id}");
        }
        catch (Exception ex)
        {
            ModLog.Error($"starting the advice service failed: {ex}");
        }
    }

    public static void Stop()
    {
        try
        {
            if (_process is { HasExited: false })
                _process.Kill(entireProcessTree: true);
        }
        catch (Exception) { }
    }
}
