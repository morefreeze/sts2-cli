// src/Sts2Advisor/AdvisorRuntime.cs
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using Godot;
using Sts2Advisor.AutoPilot;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

/// <summary>Per-frame driver (SceneTree.ProcessFrame, main thread). Every 0.3 s: read the screen,
/// and when its fingerprint changed, serialize it (main thread) and ask the advisor (background).
/// Replies are applied on a later frame, only if the screen has not changed meanwhile.
/// The autopilot hotkey (F9) hands the run to AutoPilot; while it runs, the panel shows its steps and the advice requests here pause.
/// F10 (step_hotkey) or the panel's "do this step" button runs a single step: one advised choice, then stop at the next decision.</summary>
internal static class AdvisorRuntime
{
    private const double PollSeconds = 0.3;
    private const int MaxScreenFailures = 3;
    private const double TickErrorLogSeconds = 30;
    private const string DetectScreen = "detect";

    // Same options as src/Sts2Headless/Program.cs's JsonOpts, so the JSON matches the headless engine's.
    internal static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    /// <summary>Advice: the service answered. BadReply: it answered, but not with a usable reply
    /// (non-2xx, or a malformed body) -- it is reachable, so no backoff. Offline: could not reach it.</summary>
    private enum Outcome { Advice, BadReply, Offline }

    private readonly record struct Result(string Fingerprint, Outcome Outcome, AdviceReply? Reply, string Message);

    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static readonly Backoff Reconnect = new();
    private static readonly ConcurrentQueue<Result> Results = new();

    private static IAdvisor? _advisor;
    private static LocLookup? _loc;
    private static AdvisorTraceLog? _trace;
    private static AdvisorPanel? _panel;
    private static Capture? _current;
    private static string? _requested;
    private static string? _language = "";
    private static string? _configProblem;
    private static string? _lastSeenFingerprint;
    private static string? _failScreen;
    private static string? _failFingerprint;
    private static int _failCount;
    private static int _loggedDropped;
    private static string? _tickErrorKey;
    private static double _tickErrorLoggedAt;
    private static int _tickErrorsSuppressed;
    private static double _nextPoll;
    private static bool _offline, _offlineLogged, _hotkeyDown, _disabled;
    private static bool _visible = true;
    private static Key _hotkey = Key.F8;
    private static Key _autoKey = Key.F9;
    private static string _autoKeyName = "F9";
    private static bool _autoKeyDown;
    private static Key _stepKey = Key.F10;
    private static string _stepKeyName = "F10";
    private static bool _stepKeyDown;

    private static bool Zh => GameText.IsChinese;

    public static void Start(AdvisorConfig config, string configPath, string userDir, string? autopilotProblem)
    {
        _advisor = new SidecarAdvisor(config.Port);
        _trace = new AdvisorTraceLog(System.IO.Path.Combine(userDir, "advisor_trace.jsonl"));
        if (!Enum.TryParse(config.Hotkey, ignoreCase: true, out _hotkey))
            _hotkey = Key.F8;
        if (!Enum.TryParse(config.AutopilotHotkey, ignoreCase: true, out _autoKey))
            _autoKey = Key.F9;
        _autoKeyName = _autoKey.ToString();
        if (!Enum.TryParse(config.StepHotkey, ignoreCase: true, out _stepKey))
            _stepKey = Key.F10;
        _stepKeyName = _stepKey.ToString();
        if (_stepKey == _hotkey || _stepKey == _autoKey)
            autopilotProblem ??= $"step_hotkey {_stepKeyName} clashes with hotkey/autopilot_hotkey ({configPath})";
        if (_autoKey == _hotkey)  // one press would both hide the panel and start the autopilot
            autopilotProblem ??= $"hotkey and autopilot_hotkey are both {_autoKeyName} ({configPath})";
        _loc = string.IsNullOrWhiteSpace(config.RepoPath) ? null : new LocLookup(config.RepoPath);
        bool locOk = _loc is { IsLoaded: true };
        string gameUserDir = System.IO.Path.GetDirectoryName(userDir) ?? userDir;
        AutoPilotControl.Init(_advisor, locOk ? _loc : null, _trace, gameUserDir, () => Clock.Elapsed.TotalSeconds,
                   autopilotProblem ?? (locOk ? null : $"repo_path ({configPath})"));
        if (!locOk)
        {
            _configProblem = $"repo_path ({configPath})";
            ModLog.Error($"no localization_eng/ under repo_path '{config.RepoPath}'; set repo_path in {configPath}");
            return;
        }
        var advisor = _advisor;
        _ = Task.Run(() => SidecarProcess.EnsureStartedAsync(config, advisor));
    }

    public static void Tick()
    {
        if (_disabled) return;
        if (AutoPilotControl.ApiMismatch)
        {
            DisableForApiMismatch("the autopilot hit a game API mismatch", null);
            return;
        }
        try
        {
            TickInner();
        }
        catch (Exception ex) when (ex is TypeLoadException or MissingMemberException)
        {
            DisableForApiMismatch("game API mismatch", ex);
        }
        catch (Exception ex)
        {
            LogTickFailure(ex);
        }
    }

    private static void DisableForApiMismatch(string why, Exception? ex)
    {
        _disabled = true;
        ModLog.Error($"{why}, advisor disabled{(ex == null ? "" : $": {ex}")}");
        try { AutoPilotControl.Stop(StopReason.Unsupported, ex?.GetType().Name); } catch (Exception) { }
        // The missing member may be LocManager itself: fall back to English, never throw out of the tick.
        try
        {
            bool zh = false;
            try { zh = Zh; } catch (Exception) { }
            Show(PanelText.Status(null, StatusKind.Unsupported, null, zh));
        }
        catch (Exception) { }
    }

    private static void TickInner()
    {
        double now = Clock.Elapsed.TotalSeconds;
        PollHotkey();
        // While the autopilot runs a late advice reply must not paint over its panel: drop them.
        DrainResults(now, discard: AutoPilotControl.IsRunning);
        if (now < _nextPoll) return;
        _nextPoll = now + PollSeconds;

        if (_panel == null || !_panel.Alive)
        {
            _panel = AdvisorPanel.TryCreate();
            if (_panel == null) return;
        }
        if (!_visible)
        {
            _panel.Hide();
            return;
        }
        var language = GameText.Language;
        if (language != _language)
        {
            _language = language;
            _requested = null;  // re-render labels in the new language
        }
        if (_configProblem != null)
        {
            Show(PanelText.Status(null, StatusKind.ConfigMissing, _configProblem, Zh));
            return;
        }

        if (AutoPilotControl.IsRunning)
        {
            // The autopilot asks for its own advice; the advice panel would only ask twice.
            _requested = null;
            _lastSeenFingerprint = null;
            Show(AutoPilotControl.RunningLines(_autoKeyName, _stepKeyName, Zh));
            return;
        }

        Capture? cap;
        try
        {
            cap = DecisionCapture.Detect(_loc!);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed(DetectScreen, null, ex);
            return;
        }
        // A parked screen is forgiven as soon as the player is on anything else (combat, a menu, another
        // screen class, or a different state of the same class), so leaving and coming back retries it.
        if (_failScreen != null && (cap?.Screen != _failScreen || cap.Fingerprint != _failFingerprint))
            ResetFailures();
        _current = cap;
        if (cap == null)
        {
            _requested = null;
            _lastSeenFingerprint = null;
            _panel.Hide();
            return;
        }
        if (cap.Kind != CaptureKind.Advise)
        {
            _requested = null;
            _lastSeenFingerprint = null;
            Show(PanelText.Status(cap.Header, cap.Kind == CaptureKind.Combat ? StatusKind.Combat : StatusKind.NoAdvice, null, Zh));
            return;
        }
        if (cap.Screen == _failScreen && _failCount >= MaxScreenFailures)
        {
            Show(PanelText.Status(null, StatusKind.ReadFailed, cap.Screen, Zh));  // dedupes identical text
            return;
        }
        if (cap.Fingerprint == _requested) return;
        // Screens animate in and their state settles over a frame or two: ask only about a fingerprint
        // seen on two polls in a row. The first sighting just records it.
        bool settled = cap.Fingerprint == _lastSeenFingerprint;
        _lastSeenFingerprint = cap.Fingerprint;
        if (_offline && !Reconnect.Ready(now))
        {
            Show(PanelText.Status(cap.Header, StatusKind.Offline, null, Zh));
            return;
        }
        if (!settled)
        {
            Show(PanelText.Status(cap.Header, StatusKind.Thinking, null, Zh));  // not the last screen's advice
            return;
        }

        string json;
        try
        {
            json = JsonSerializer.Serialize(cap.Build!(), JsonOpts);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed(cap.Screen, cap.Fingerprint, ex);
            return;
        }
        if (cap.Screen == _failScreen)
            ResetFailures();
        _requested = cap.Fingerprint;
        Show(PanelText.Status(cap.Header, StatusKind.Thinking, null, Zh));
        string fingerprint = cap.Fingerprint, decision = cap.Header.Decision;
        var advisor = _advisor!;
        var trace = _trace;
        _ = Task.Run(async () =>
        {
            try
            {
                var reply = await advisor.AdviseAsync(json, CancellationToken.None).ConfigureAwait(false);
                trace?.Append(decision, json, reply.Raw, null);
                Results.Enqueue(new Result(fingerprint, Outcome.Advice, reply, ""));
            }
            catch (Exception ex)
            {
                trace?.Append(decision, json, null, ex.GetType().Name);
                // The service answered, just not usefully: a bug or a version mismatch, not an outage.
                // Anything else (refused connection, timeout, ...) means it is not there.
                bool answered = ex is AdviceFormatException or AdviceServiceException;
                if (answered)
                    ModLog.Error($"bad advice reply for {decision}: {ex.GetType().Name}: {ex.Message}");
                Results.Enqueue(new Result(fingerprint, answered ? Outcome.BadReply : Outcome.Offline, null, ex.Message));
            }
        });
    }

    private static void DrainResults(double now, bool discard)
    {
        int dropped = _trace?.Dropped ?? 0;
        if (dropped > _loggedDropped)
        {
            ModLog.Error($"advisor_trace.jsonl: {dropped - _loggedDropped} more line(s) dropped ({dropped} so far)");
            _loggedDropped = dropped;
        }
        while (Results.TryDequeue(out var result))
        {
            if (discard)
                continue;
            if (_current == null || result.Fingerprint != _current.Fingerprint)
                continue;  // the screen moved on; the next poll asks again
            if (result.Outcome == Outcome.Offline)
            {
                _offline = true;
                Reconnect.Failed(now);
                _requested = null;
                if (!_offlineLogged)  // once per outage, not once per retry
                {
                    _offlineLogged = true;
                    ModLog.Info($"advice service unreachable: {result.Message}");
                }
                Show(PanelText.Status(_current.Header, StatusKind.Offline, null, Zh));
                continue;
            }
            if (_offlineLogged)
            {
                _offlineLogged = false;
                ModLog.Info("advice service reachable");
            }
            _offline = false;
            Reconnect.Succeeded();
            if (result.Outcome == Outcome.BadReply)
            {
                // _requested stays set: this screen is not re-asked until its fingerprint changes.
                Show(PanelText.Status(_current.Header, StatusKind.BadReply, null, Zh));
                continue;
            }
            Show(PanelText.Render(_current.Header, result.Reply!, _current.Labels, Zh));
        }
    }

    private static void ReadFailed(string screen, string? fingerprint, Exception ex)
    {
        if (screen == _failScreen && fingerprint == _failFingerprint) _failCount++;
        else
        {
            _failScreen = screen;
            _failFingerprint = fingerprint;
            _failCount = 1;
        }
        if (_failCount <= MaxScreenFailures)  // a persistent failure must not flood the log at 3 Hz
            ModLog.Error($"reading {screen} failed ({_failCount}/{MaxScreenFailures}): {ex}");
        Show(PanelText.Status(null, StatusKind.ReadFailed, screen, Zh));
    }

    private static void ResetFailures()
    {
        _failScreen = null;
        _failFingerprint = null;
        _failCount = 0;
    }

    /// <summary>An unexpected exception can recur every frame; log a new one at once, a repeat at most every 30 s.</summary>
    private static void LogTickFailure(Exception ex)
    {
        double now = Clock.Elapsed.TotalSeconds;
        string key = TickFailureKey(ex);
        if (key == _tickErrorKey && now - _tickErrorLoggedAt < TickErrorLogSeconds)
        {
            _tickErrorsSuppressed++;
            return;
        }
        string repeats = _tickErrorsSuppressed > 0 ? $" ({_tickErrorsSuppressed} similar suppressed)" : "";
        ModLog.Error($"tick failed{repeats}: {ex}");
        _tickErrorKey = key;
        _tickErrorLoggedAt = now;
        _tickErrorsSuppressed = 0;
    }

    /// <summary>Exception type + where it was thrown, never its message (messages often carry ids that
    /// change every frame). The first frame in this mod's own code is preferred: a KeyNotFoundException
    /// from two call sites is two bugs, but the same top frame inside Dictionary.</summary>
    private static string TickFailureKey(Exception ex)
    {
        string type = ex.GetType().FullName ?? ex.GetType().Name;
        try
        {
            var frames = new StackTrace(ex, false).GetFrames();
            var methods = frames.Select(f => f.GetMethod()).Where(m => m != null).ToList();
            var method = methods.FirstOrDefault(m => m!.DeclaringType?.Namespace?.StartsWith("Sts2") == true) ?? methods.FirstOrDefault();
            return method == null ? type : $"{type}@{method.DeclaringType?.FullName}.{method.Name}";
        }
        catch (Exception)
        {
            return type;
        }
    }

    private static void PollHotkey()
    {
        bool down = Input.IsKeyPressed(_hotkey);
        if (down && !_hotkeyDown)
        {
            _visible = !_visible;
            _requested = null;
            if (!_visible) _panel?.Hide();
        }
        _hotkeyDown = down;
        // A clash is logged at start and disables the autopilot; the panel toggle wins, and a key shared by
        // the autopilot and the step is not polled twice (one press must not act twice).
        if (_autoKey != _hotkey)
        {
            bool autoDown = Input.IsKeyPressed(_autoKey);
            if (autoDown && !_autoKeyDown)
            {
                AutoPilotControl.Toggle();
                _requested = null;
            }
            _autoKeyDown = autoDown;
        }
        if (_stepKey != _hotkey && _stepKey != _autoKey)
        {
            bool stepDown = Input.IsKeyPressed(_stepKey);
            if (stepDown && !_stepKeyDown)
            {
                AutoPilotControl.Toggle(singleStep: true);
                _requested = null;
            }
            _stepKeyDown = stepDown;
        }
    }

    /// <summary>Nothing is shown while the player has the panel hidden (F8): a reply that lands late must not undo that.</summary>
    private static void Show(IReadOnlyList<PanelLine> lines)
    {
        if (!_visible) return;
        var banner = AutoPilotControl.Banner(_autoKeyName, _stepKeyName, Zh);
        // No buttons while the mod cannot act (config missing, game version unsupported).
        var buttons = _configProblem != null || _disabled || !AutoPilotControl.Available
            ? Array.Empty<PanelButton>()
            : AutoPilotText.Buttons(AutoPilotControl.IsRunning, _stepKeyName, _autoKeyName, Zh);
        _panel?.Show(banner == null ? lines : lines.Prepend(banner).ToList(), _language, buttons, OnPanelButton);
    }

    private static void OnPanelButton(PanelAction action)
    {
        // Not inside the GUI click dispatch: the loop starts (and may touch the scene) on the next idle moment.
        Callable.From(() =>
        {
            try
            {
                AutoPilotControl.Press(action);
                _requested = null;
            }
            catch (Exception ex)
            {
                LogTickFailure(ex);  // a deferred call must not throw
            }
        }).CallDeferred();
    }
}
