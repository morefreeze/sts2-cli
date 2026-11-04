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

    private static string BackupOf(string path) =>
        Path.Combine(Path.GetDirectoryName(path)!, "advisor_trace.1.jsonl");

    private static int Number(string line)
    {
        using var doc = JsonDocument.Parse(line);
        return doc.RootElement.GetProperty("state").GetProperty("n").GetInt32();
    }

    [Fact]
    public void TheDefaultCapIs20MegabytesAndASmallFileIsNeverRotated()
    {
        Assert.Equal(20L * 1024 * 1024, AdvisorTraceLog.DefaultMaxBytes);
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        for (int n = 0; n < 20; n++)
            log.Append("shop", $$"""{"n": {{n}}}""", null, null);
        Assert.Equal(20, File.ReadAllLines(path).Length);
        Assert.False(File.Exists(BackupOf(path)));
    }

    [Fact]
    public void AFileOverTheCapIsMovedToTheBackupAndAFreshOneStarted()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path, maxBytes: 500);
        for (int n = 0; n < 12; n++)
            log.Append("shop", $$"""{"n": {{n}}}""", null, null);

        Assert.True(File.Exists(BackupOf(path)));
        var backup = File.ReadAllLines(BackupOf(path));
        var current = File.ReadAllLines(path);
        Assert.NotEmpty(backup);
        Assert.NotEmpty(current);
        // Nothing lost, nothing duplicated, in order, every line whole.
        var numbers = backup.Concat(current).Select(Number).ToList();
        Assert.Equal(Enumerable.Range(0, 12), numbers);
        Assert.Equal(0, log.Dropped);
        // The live file only passes the cap by the line that crossed it.
        Assert.True(new FileInfo(path).Length <= 500 + current.Max(l => l.Length + 1));
    }

    [Fact]
    public void ASecondRotationReplacesTheBackupInsteadOfPilingUpFiles()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path, maxBytes: 300);
        for (int n = 0; n < 60; n++)
            log.Append("shop", $$"""{"n": {{n}}}""", null, null);

        Assert.Equal(2, Directory.GetFiles(Path.GetDirectoryName(path)!).Length);
        var backup = File.ReadAllLines(BackupOf(path)).Select(Number).ToList();
        var current = File.ReadAllLines(path).Select(Number).ToList();
        Assert.Equal(59, current[^1]);
        Assert.Equal(current[0] - 1, backup[^1]);  // the backup is the file right before the live one
        Assert.True(backup[0] > 0);                // older lines are gone
        Assert.Equal(0, log.Dropped);
    }

    [Fact]
    public void AFailedRotationNeverThrowsAndNeverCostsTheLine()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path, maxBytes: 100);
        // A non-empty directory where the backup should go: the move cannot succeed.
        Directory.CreateDirectory(Path.Combine(BackupOf(path), "keep"));
        for (int n = 0; n < 5; n++)
            log.Append("shop", $$"""{"n": {{n}}}""", null, null);
        Assert.Equal(Enumerable.Range(0, 5), File.ReadAllLines(path).Select(Number));
        Assert.Equal(0, log.Dropped);
    }

    [Fact]
    public void AutopilotStepsCarryTheirMarker()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("shop", """{"decision": "shop"}""", """{"action": null}""", null, autopilotStep: 3);
        log.Append("shop", """{"decision": "shop"}""", """{"action": null}""", null);
        var lines = File.ReadAllLines(path);
        using var first = JsonDocument.Parse(lines[0]);
        var ap = first.RootElement.GetProperty("autopilot");
        Assert.True(ap.GetProperty("executed").GetBoolean());
        Assert.Equal(3, ap.GetProperty("step").GetInt32());
        using var second = JsonDocument.Parse(lines[1]);
        Assert.False(second.RootElement.TryGetProperty("autopilot", out _));
    }

    private static JsonDocument OnlyLine(string path)
    {
        var lines = File.ReadAllLines(path);
        Assert.Single(lines);
        return JsonDocument.Parse(lines[0]);
    }

    [Fact]
    public void RunProviderJsonIsWrittenAsTheRunObjectAfterDecision()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path, runInfoJson: () => RunIdentity.ToJson(1730000000, "ABC123", "IRONCLAD", 7));
        log.Append("shop", """{"decision": "shop"}""", null, null);
        using var doc = OnlyLine(path);
        var run = doc.RootElement.GetProperty("run");
        Assert.Equal(1730000000, run.GetProperty("start_time").GetInt64());
        Assert.Equal("ABC123", run.GetProperty("seed").GetString());
        Assert.Equal("IRONCLAD", run.GetProperty("character").GetString());
        Assert.Equal(7, run.GetProperty("ascension").GetInt32());
        var names = doc.RootElement.EnumerateObject().Select(p => p.Name).ToList();
        Assert.Equal(names.IndexOf("decision") + 1, names.IndexOf("run"));
    }

    [Fact]
    public void NoRunKeyWithoutAProviderOrWhenItReturnsNull()
    {
        var withoutProvider = NewPath();
        new AdvisorTraceLog(withoutProvider).Append("shop", """{"a": 1}""", null, null);
        using (var doc = OnlyLine(withoutProvider))
            Assert.False(doc.RootElement.TryGetProperty("run", out _));

        var nullProvider = NewPath();
        new AdvisorTraceLog(nullProvider, runInfoJson: () => null).Append("shop", """{"a": 1}""", null, null);
        using (var doc = OnlyLine(nullProvider))
            Assert.False(doc.RootElement.TryGetProperty("run", out _));
    }

    [Fact]
    public void ThrowingOrInvalidProviderStillWritesTheLineWithoutRun()
    {
        var thrower = NewPath();
        var log1 = new AdvisorTraceLog(thrower, runInfoJson: () => throw new InvalidOperationException("boom"));
        log1.Append("shop", """{"a": 1}""", null, null);
        using (var doc = OnlyLine(thrower))
        {
            Assert.False(doc.RootElement.TryGetProperty("run", out _));
            Assert.Equal(1, doc.RootElement.GetProperty("state").GetProperty("a").GetInt32());
        }
        Assert.Equal(0, log1.Dropped);

        var invalid = NewPath();
        var log2 = new AdvisorTraceLog(invalid, runInfoJson: () => "{not json");
        log2.Append("shop", """{"a": 1}""", null, null);
        using (var doc = OnlyLine(invalid))
            Assert.False(doc.RootElement.TryGetProperty("run", out _));
        Assert.Equal(0, log2.Dropped);
    }

    [Fact]
    public void PrettyPrintedRunJsonStaysOnOneLine()
    {
        var path = NewPath();
        new AdvisorTraceLog(path, runInfoJson: () => "{\n \"seed\": \"X\"\n}").Append("shop", """{"a": 1}""", null, null);
        using var doc = OnlyLine(path);
        Assert.Equal("X", doc.RootElement.GetProperty("run").GetProperty("seed").GetString());
    }
}
