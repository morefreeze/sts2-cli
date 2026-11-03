// src/Sts2Advisor/ModEntry.cs
using Godot;
using HarmonyLib;
using MegaCrit.Sts2.Core.Modding;
using Sts2Advisor.AutoPilot;
using Sts2Advisor.Core;

namespace Sts2Advisor;

[ModInitializer(nameof(Initialize))]
public static class ModEntry
{
    public const string ModId = "Sts2Advisor";

    /// <summary>Called by the game's ModManager very early -- before LocManager and ModelDb exist.
    /// Only reads config, opens the log and hooks the frame loop; everything that touches the game
    /// waits for AdvisorRuntime.Tick.</summary>
    public static void Initialize()
    {
        string userDir = System.IO.Path.Combine(OS.GetUserDataDir(), ModId);
        ModLog.Init(userDir);
        string configPath = System.IO.Path.Combine(userDir, "config.json");
        AdvisorConfig config;
        try
        {
            config = AdvisorConfig.Load(configPath);
        }
        catch (Exception ex)
        {
            ModLog.Error($"bad {configPath}: {ex.Message}; using defaults");
            config = AdvisorConfig.Defaults;
        }
        ModLog.Info($"loaded; config {configPath}: repo={config.RepoPath} port={config.Port} autostart={config.Autostart} hotkey={config.Hotkey} autopilot_hotkey={config.AutopilotHotkey} step_hotkey={config.StepHotkey} instant_mode={config.InstantMode}");
        string? patchProblem;
        try
        {
            patchProblem = HandlerPatches.Apply(new Harmony("morefreeze.sts2advisor.autopilot"));
        }
        catch (Exception ex)
        {
            patchProblem = $"{ex.GetType().Name}: {ex.Message}";
        }
        if (patchProblem != null)
            ModLog.Error($"autopilot disabled, AutoSlay handlers not patched: {patchProblem}");
        else
            ModLog.Info("autopilot: AutoSlay handlers patched");
        AdvisorRuntime.Start(config, configPath, userDir, patchProblem);
        var tree = (SceneTree)Engine.GetMainLoop();
        tree.Connect(SceneTree.SignalName.ProcessFrame, Callable.From(AdvisorRuntime.Tick));
    }
}
