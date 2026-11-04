using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>The <c>"run"</c> object on every advisor_trace.jsonl line: which run the line belongs to.
/// <c>start_time</c> is the game's own RunManager._startTime, i.e. the file name of the run's
/// saves/history/&lt;start_time&gt;.run, so a trace line joins the history without guessing.</summary>
public static class RunIdentity
{
    private static readonly JsonWriterOptions WriterOptions =
        new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    /// <summary>{"start_time", "seed", "character", "ascension"}; start_time is null when 0 (not known).</summary>
    public static string ToJson(long startTime, string? seed, string? character, int ascension)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            if (startTime == 0) w.WriteNull("start_time"); else w.WriteNumber("start_time", startTime);
            if (seed == null) w.WriteNull("seed"); else w.WriteString("seed", seed);
            if (character == null) w.WriteNull("character"); else w.WriteString("character", character);
            w.WriteNumber("ascension", ascension);
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.ToArray());
    }
}
