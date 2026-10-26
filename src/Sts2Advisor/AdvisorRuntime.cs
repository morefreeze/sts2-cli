// src/Sts2Advisor/AdvisorRuntime.cs
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using Godot;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

/// <summary>Per-frame driver (SceneTree.ProcessFrame, main thread). Every 0.3 s: read the screen,
/// and when its fingerprint changed, serialize it (main thread) and ask the advisor (background).
/// Replies are applied on a later frame, only if the screen has not changed meanwhile.</summary>
internal static class AdvisorRuntime
{
    private const double PollSeconds = 0.3;
    private const int MaxScreenFailures = 3;
    private const double TickErrorLogSeconds = 30;
    private const string DetectScreen = "detect";

    // Same options as src/Sts2Headless/Program.cs's JsonOpts, so the JSON matches the headless engine's.
    private static readonly JsonSerializerOptions JsonOpts = new()
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
    private static string? _failScreen;
    private static int _failCount;
    private static int _loggedDropped;
    private static string? _tickErrorKey;
    private static double _tickErrorLoggedAt;
    private static int _tickErrorsSuppressed;
    private static double _nextPoll;
    private static bool _offline, _offlineLogged, _hotkeyDown, _disabled;
    private static bool _visible = true;
    private static Key _hotkey = Key.F8;

    private static bool Zh => GameText.IsChinese;

    public static void Start(AdvisorConfig config, string configPath, string userDir)
    {
        _advisor = new SidecarAdvisor(config.Port);
        _trace = new AdvisorTraceLog(System.IO.Path.Combine(userDir, "advisor_trace.jsonl"));
        if (!Enum.TryParse(config.Hotkey, ignoreCase: true, out _hotkey))
            _hotkey = Key.F8;
        _loc = string.IsNullOrWhiteSpace(config.RepoPath) ? null : new LocLookup(config.RepoPath);
        if (_loc == null || !_loc.IsLoaded)
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
        try
        {
            TickInner();
        }
        catch (Exception ex) when (ex is TypeLoadException or MissingMemberException)
        {
            _disabled = true;
            ModLog.Error($"game API mismatch, advisor disabled: {ex}");
            Show(PanelText.Status(null, StatusKind.Unsupported, null, Zh));
        }
        catch (Exception ex)
        {
            LogTickFailure(ex);
        }
    }

    private static void TickInner()
    {
        double now = Clock.Elapsed.TotalSeconds;
        PollHotkey();
        DrainResults(now);
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

        Capture? cap;
        try
        {
            cap = DecisionCapture.Detect(_loc!);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed(DetectScreen, ex);
            return;
        }
        if (_failScreen == DetectScreen)
            ResetFailures();
        _current = cap;
        if (cap == null)
        {
            _requested = null;
            _panel.Hide();
            return;
        }
        if (cap.Kind != CaptureKind.Advise)
        {
            _requested = null;
            Show(PanelText.Status(cap.Header, cap.Kind == CaptureKind.Combat ? StatusKind.Combat : StatusKind.NoAdvice, null, Zh));
            return;
        }
        if (cap.Screen == _failScreen && _failCount >= MaxScreenFailures) return;
        if (cap.Fingerprint == _requested) return;
        if (_offline && !Reconnect.Ready(now))
        {
            Show(PanelText.Status(cap.Header, StatusKind.Offline, null, Zh));
            return;
        }

        string json;
        try
        {
            json = JsonSerializer.Serialize(cap.Build!(), JsonOpts);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed(cap.Screen, ex);
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

    private static void DrainResults(double now)
    {
        int dropped = _trace?.Dropped ?? 0;
        if (dropped > _loggedDropped)
        {
            ModLog.Error($"advisor_trace.jsonl: {dropped - _loggedDropped} more line(s) dropped ({dropped} so far)");
            _loggedDropped = dropped;
        }
        while (Results.TryDequeue(out var result))
        {
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

    private static void ReadFailed(string screen, Exception ex)
    {
        if (screen == _failScreen) _failCount++;
        else
        {
            _failScreen = screen;
            _failCount = 1;
        }
        if (_failCount <= MaxScreenFailures)  // a persistent failure must not flood the log at 3 Hz
            ModLog.Error($"reading {screen} failed ({_failCount}/{MaxScreenFailures}): {ex}");
        Show(PanelText.Status(null, StatusKind.ReadFailed, screen, Zh));
    }

    private static void ResetFailures()
    {
        _failScreen = null;
        _failCount = 0;
    }

    /// <summary>An unexpected exception can recur every frame; log a new one at once, a repeat at most every 30 s.</summary>
    private static void LogTickFailure(Exception ex)
    {
        double now = Clock.Elapsed.TotalSeconds;
        string key = ex.GetType().FullName + ":" + ex.Message;
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
    }

    private static void Show(IReadOnlyList<PanelLine> lines) => _panel?.Show(lines, _language);
}
