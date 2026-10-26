using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>advisor_trace.jsonl: every decision JSON the mod sent and the reply it got.
/// The first in-game session's file becomes tests/fixtures/advisor/real_game_trace.jsonl,
/// which tests/test_advisor_trace_contract.py checks against headless field shapes.
/// Logging is best effort: <see cref="Append"/> runs on a background thread after each reply and
/// never throws; a line whose JSON is invalid, or whose write fails, is dropped and counted.</summary>
public sealed class AdvisorTraceLog
{
    private static readonly JsonWriterOptions WriterOptions =
        new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private readonly string _path;
    private readonly object _gate = new();
    private int _dropped;

    public AdvisorTraceLog(string path)
    {
        _path = path;
        try { EnsureDirectory(); }
        catch (Exception) { /* retried on every Append; a missing log must not stop the mod loading */ }
    }

    /// <summary>Lines dropped so far (invalid JSON or a failed write).</summary>
    public int Dropped => Volatile.Read(ref _dropped);

    public void Append(string decision, string stateJson, string? replyJson, string? error)
    {
        try
        {
            var line = BuildLine(decision, stateJson, replyJson, error);
            lock (_gate)
            {
                EnsureDirectory();
                File.AppendAllText(_path, line);
            }
        }
        catch (Exception)
        {
            Interlocked.Increment(ref _dropped);
        }
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
    private static string BuildLine(string decision, string stateJson, string? replyJson, string? error)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            w.WriteString("ts", DateTime.UtcNow.ToString("O"));
            w.WriteString("decision", decision);
            w.WritePropertyName("state");
            WriteJson(w, stateJson);
            w.WritePropertyName("reply");
            if (replyJson == null) w.WriteNullValue(); else WriteJson(w, replyJson);
            if (error != null) w.WriteString("error", error);
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
