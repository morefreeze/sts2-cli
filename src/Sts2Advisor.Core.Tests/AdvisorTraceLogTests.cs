using System.Text.Json;
using Sts2Advisor.Core;

public class AdvisorTraceLogTests
{
    private static string NewPath() =>
        Path.Combine(Path.GetTempPath(), $"trace-{Guid.NewGuid()}", "advisor_trace.jsonl");

    [Fact]
    public void WritesOneJsonLinePerDecisionWithTheRawStateAndReply()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("rest_site", """{"decision": "rest_site", "options": []}""", """{"action": null}""", null);
        log.Append("shop", """{"decision": "shop"}""", null, "HttpRequestException");
        var lines = File.ReadAllLines(path);
        Assert.Equal(2, lines.Length);
        using var first = JsonDocument.Parse(lines[0]);
        Assert.Equal("rest_site", first.RootElement.GetProperty("state").GetProperty("decision").GetString());
        Assert.Equal(JsonValueKind.Null, first.RootElement.GetProperty("reply").GetProperty("action").ValueKind);
        using var second = JsonDocument.Parse(lines[1]);
        Assert.Equal(JsonValueKind.Null, second.RootElement.GetProperty("reply").ValueKind);
        Assert.Equal("HttpRequestException", second.RootElement.GetProperty("error").GetString());
    }

    [Fact]
    public void InvalidJsonDropsTheLineAndNeverThrows()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("shop", """{"decision": "shop"}""", """{"action": null}""", null);

        // None of these may throw into the caller, and none may leave a half-written line behind.
        log.Append("shop", "{not json", null, null);
        log.Append("shop", "", null, null);
        log.Append("shop", """{"decision": "shop"} trailing""", null, null);
        log.Append("shop", """{"decision": "shop"}""", "{broken", null);
        log.Append("shop", null!, null, null);

        var lines = File.ReadAllLines(path);
        Assert.Single(lines);
        foreach (var line in lines)
            using (JsonDocument.Parse(line)) { }
        Assert.Equal(5, log.Dropped);
    }

    [Fact]
    public void InvalidJsonOnAFreshLogLeavesNoFileOrOnlyValidLines()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("shop", "{not json", null, null);
        if (File.Exists(path))
            foreach (var line in File.ReadAllLines(path))
                using (JsonDocument.Parse(line)) { }
    }

    [Fact]
    public void PrettyPrintedInputStillMakesExactlyOneLine()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("event_choice", "{\n  \"decision\": \"event_choice\",\n  \"options\": [\n    {\"index\": 0}\n  ]\n}",
                   "{\r\n \"action\": null\r\n}", null);
        var lines = File.ReadAllLines(path);
        Assert.Single(lines);
        using var doc = JsonDocument.Parse(lines[0]);
        Assert.Equal(0, doc.RootElement.GetProperty("state").GetProperty("options")[0].GetProperty("index").GetInt32());
    }

    [Fact]
    public void NonAsciiTextSurvives()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("shop", """{"name": "打击"}""", null, "读取失败");
        using var doc = JsonDocument.Parse(File.ReadAllLines(path)[0]);
        Assert.Equal("打击", doc.RootElement.GetProperty("state").GetProperty("name").GetString());
        Assert.Equal("读取失败", doc.RootElement.GetProperty("error").GetString());
    }

    [Fact]
    public void WriteFailureIsSwallowed()
    {
        // The "file" is a directory, so every write fails.
        var dir = Path.Combine(Path.GetTempPath(), $"trace-dir-{Guid.NewGuid()}");
        Directory.CreateDirectory(dir);
        var log = new AdvisorTraceLog(dir);
        log.Append("shop", """{"decision": "shop"}""", null, null);
        Assert.Equal(1, log.Dropped);
    }

    [Fact]
    public void AnUncreatableDirectoryNeverThrowsFromTheConstructorOrAppend()
    {
        // A directory cannot be created below a regular file.
        var file = Path.Combine(Path.GetTempPath(), $"trace-file-{Guid.NewGuid()}");
        File.WriteAllText(file, "x");
        var log = new AdvisorTraceLog(Path.Combine(file, "sub", "advisor_trace.jsonl"));
        log.Append("shop", """{"decision": "shop"}""", null, null);
        Assert.Equal(1, log.Dropped);
    }

    [Fact]
    public async Task ConcurrentAppendsAllLandAsWholeLines()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        await Task.WhenAll(Enumerable.Range(0, 8).Select(t => Task.Run(() =>
        {
            for (int n = 0; n < 50; n++)
                log.Append("card_reward", $$"""{"t": {{t}}, "n": {{n}}}""", $$"""{"action": null, "t": {{t}}}""", null);
        })).ToArray());

        var lines = File.ReadAllLines(path);
        Assert.Equal(400, lines.Length);
        var seen = new HashSet<(int, int)>();
        foreach (var line in lines)
        {
            using var doc = JsonDocument.Parse(line);
            var state = doc.RootElement.GetProperty("state");
            seen.Add((state.GetProperty("t").GetInt32(), state.GetProperty("n").GetInt32()));
        }
        Assert.Equal(400, seen.Count);
        Assert.Equal(0, log.Dropped);
    }
}
