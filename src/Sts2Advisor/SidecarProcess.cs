// src/Sts2Advisor/SidecarProcess.cs
using System.Diagnostics;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>Starts agent/advisor_server.py once per game launch, unless a healthy service already
/// answers on the port (then it is reused). Its stderr goes to Sts2Advisor.log.</summary>
internal static class SidecarProcess
{
    private static Process? _process;
    private static volatile bool _stopping;

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
                // A pipe we never write to and never close: when the game process dies, however it dies,
                // the service sees EOF on it and exits instead of lingering as an orphan.
                RedirectStandardInput = true,
            };
            // The advisor's routing policy: the env var greedy_action reads per call. Always set (also for the
            // default), so a STS2_MAP_POLICY left in the game's own environment cannot override the config.
            info.Environment["STS2_MAP_POLICY"] = config.MapPolicy;
            info.ArgumentList.Add(script);
            info.ArgumentList.Add("--port");
            info.ArgumentList.Add(config.Port.ToString());
            info.ArgumentList.Add("--exit-on-stdin-eof");
            // Hot reload: the service re-executes itself (same pipe, same env) when a policy commit lands
            // in the repo, so the running game picks it up without a restart.
            info.ArgumentList.Add("--reload-on-commit");
            _process = Process.Start(info)!;
            _process.EnableRaisingEvents = true;
            _process.Exited += OnExited;
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

    /// <summary>A Python crash at startup would otherwise leave no trace but an "offline" panel.</summary>
    private static void OnExited(object? sender, EventArgs e)
    {
        try
        {
            var process = (Process)sender!;
            // WaitForExit() also waits for the redirected streams to drain, so the traceback reaches the
            // log before this line. Bounded: a grandchild holding the pipe open must not swallow the message.
            Task.Run(process.WaitForExit).Wait(2000);
            string message = $"sidecar exited with code {process.ExitCode}";
            if (_stopping) ModLog.Info(message);
            else ModLog.Error(message);
        }
        catch (Exception) { }
    }

    public static void Stop()
    {
        _stopping = true;
        try
        {
            if (_process is { HasExited: false })
                _process.Kill(entireProcessTree: true);
        }
        catch (Exception) { }
    }
}
