using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>advisor_trace.jsonl: every decision JSON the mod sent and the reply it got
/// (plus an optional <c>autopilot</c> key on the line whose advice the autopilot executed).
/// The first in-game session's file (2026-10-28, Task C8) is saved gzipped as
/// tests/fixtures/advisor/real_game_trace.jsonl.gz, which tests/test_advisor_trace_contract.py
/// checks against the headless field shapes.
/// Logging is best effort: <see cref="Append"/> runs on a background thread after each reply and
/// never throws; a line whose JSON is invalid, or whose write fails, is dropped and counted.
/// The file is capped: once it is larger than the cap, the next append first moves it to
/// <c>advisor_trace.1.jsonl</c> (replacing the previous backup) and starts a fresh file, so the
/// log never takes more than about twice the cap on disk.</summary>
public sealed class AdvisorTraceLog
{
    private static readonly JsonWriterOptions WriterOptions =
        new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    /// <summary>Size above which the file is rotated (20 MB).</summary>
    public const long DefaultMaxBytes = 20L * 1024 * 1024;

    private readonly string _path;
    private readonly long _maxBytes;
    private readonly object _gate = new();
    private readonly Func<string?>? _runInfoJson;
    private int _dropped;

    /// <param name="runInfoJson">Optional provider of the run identity JSON written as <c>"run"</c> on every line
    /// (see <see cref="RunIdentity"/>). Append runs on a background thread, so the provider must only read a
    /// snapshot (a volatile string), never game state. Null, an exception or invalid JSON just omit the key.</param>
    public AdvisorTraceLog(string path, long maxBytes = DefaultMaxBytes, Func<string?>? runInfoJson = null)
    {
        _path = path;
        _maxBytes = maxBytes;
        _runInfoJson = runInfoJson;
        try { EnsureDirectory(); }
        catch (Exception) { /* retried on every Append; a missing log must not stop the mod loading */ }
    }

    /// <summary>Lines dropped so far (invalid JSON or a failed write).</summary>
    public int Dropped => Volatile.Read(ref _dropped);

    /// <summary><paramref name="autopilotStep"/>: set when the autopilot executed this advice as its step n;
    /// written as <c>"autopilot": {"executed": true, "step": n}</c>.</summary>
    public void Append(string decision, string stateJson, string? replyJson, string? error, int? autopilotStep = null)
    {
        try
        {
            var line = BuildLine(decision, stateJson, replyJson, error, autopilotStep, ReadRunInfo());
            lock (_gate)
            {
                EnsureDirectory();
                RotateIfTooBig();
                File.AppendAllText(_path, line);
            }
        }
        catch (Exception)
        {
            Interlocked.Increment(ref _dropped);
        }
    }

    private string? ReadRunInfo()
    {
        if (_runInfoJson == null) return null;
        try
        {
            var json = _runInfoJson();
            if (json == null) return null;
            using (JsonDocument.Parse(json)) { }  // invalid JSON -> no run key, the line is still written
            return json;
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>The previous file, e.g. advisor_trace.1.jsonl next to advisor_trace.jsonl.</summary>
    private string BackupPath =>
        Path.Combine(Path.GetDirectoryName(_path) ?? "",
                     Path.GetFileNameWithoutExtension(_path) + ".1" + Path.GetExtension(_path));

    /// <summary>Best effort: a failed move must not cost the line being appended, so it is swallowed
    /// (the file just keeps growing until a later append manages to rotate it).</summary>
    private void RotateIfTooBig()
    {
        try
        {
            var file = new FileInfo(_path);
            if (file.Exists && file.Length > _maxBytes)
                File.Move(_path, BackupPath, overwrite: true);
        }
        catch (Exception) { }
    }

    private void EnsureDirectory()
    {
        var dir = Path.GetDirectoryName(_path);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);
    }

    /// <summary>One compact line. The state and reply are parsed and re-written rather than pasted
    /// in raw, so invalid JSON throws here (before anything reaches the file) and a pretty-printed
    /// input still stays on one line.</summary>
    private static string BuildLine(string decision, string stateJson, string? replyJson, string? error, int? autopilotStep, string? runJson)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            w.WriteString("ts", DateTime.UtcNow.ToString("O"));
            w.WriteString("decision", decision);
            if (runJson != null)
            {
                w.WritePropertyName("run");
                WriteJson(w, runJson);
            }
            w.WritePropertyName("state");
            WriteJson(w, stateJson);
            w.WritePropertyName("reply");
            if (replyJson == null) w.WriteNullValue(); else WriteJson(w, replyJson);
            if (error != null) w.WriteString("error", error);
            if (autopilotStep is { } step)
            {
                w.WriteStartObject("autopilot");
                w.WriteBoolean("executed", true);
                w.WriteNumber("step", step);
                w.WriteEndObject();
            }
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.ToArray()) + "\n";
    }

    private static void WriteJson(Utf8JsonWriter w, string json)
    {
        using var doc = JsonDocument.Parse(json);
        doc.RootElement.WriteTo(w);
    }
}
