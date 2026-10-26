using System.Net;
using System.Text;
using Sts2Advisor.Core;

public class SidecarAdvisorTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Respond = _ => new HttpResponseMessage(HttpStatusCode.OK);
        public Func<CancellationToken, Task>? Delay;
        public readonly List<(string Method, string Path, string? Body)> Seen = new();
        public readonly List<(string? MediaType, string? CharSet)> ContentTypes = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Seen.Add((request.Method.Method, request.RequestUri!.AbsolutePath,
                      request.Content == null ? null : await request.Content.ReadAsStringAsync(ct)));
            ContentTypes.Add((request.Content?.Headers.ContentType?.MediaType, request.Content?.Headers.ContentType?.CharSet));
            if (Delay != null)
                await Delay(ct);
            return Respond(request);
        }
    }

    private static HttpResponseMessage Json(string body, HttpStatusCode code = HttpStatusCode.OK) =>
        new(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

    private static Task Forever(CancellationToken ct) => Task.Delay(Timeout.Infinite, ct);

    [Fact]
    public async Task PostsTheDecisionAndParsesTheReply()
    {
        var stub = new StubHandler { Respond = _ => Json(AdviceReplyTests.CardRewardJson) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var reply = await advisor.AdviseAsync("""{"decision": "card_reward"}""", CancellationToken.None);
        Assert.Equal("select_card_reward", reply.ActionName);
        Assert.Equal(("POST", "/advise", """{"decision": "card_reward"}"""), stub.Seen[0]);
    }

    [Fact]
    public async Task SendsTheDecisionAsUtf8Json()
    {
        var stub = new StubHandler { Respond = _ => Json(AdviceReplyTests.CardRewardJson) };
        using var advisor = new SidecarAdvisor(8767, stub);
        await advisor.AdviseAsync("""{"decision": "card_reward"}""", CancellationToken.None);
        Assert.Equal(("application/json", "utf-8"), stub.ContentTypes[0]);
    }

    [Fact]
    public async Task ServerErrorThrows()
    {
        var stub = new StubHandler { Respond = _ => Json("{}", HttpStatusCode.InternalServerError) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
        Assert.Equal(500, e.StatusCode);
    }

    [Theory]
    [InlineData(HttpStatusCode.BadRequest, 400)]
    [InlineData(HttpStatusCode.RequestEntityTooLarge, 413)]
    [InlineData(HttpStatusCode.InternalServerError, 500)]
    public async Task ErrorBodyWithErrorStatusThrowsAServiceException(HttpStatusCode code, int number)
    {
        // The service answers a bad request with {"error": "..."} and 400 / 413 / 500.
        var stub = new StubHandler { Respond = _ => Json("""{"error": "bad request: no decision"}""", code) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.AdviseAsync("""{"x": 1}""", CancellationToken.None));
        Assert.Equal(number, e.StatusCode);
        Assert.Equal("bad request: no decision", e.ErrorText);
        Assert.Contains("bad request: no decision", e.Message);
        Assert.Contains(number.ToString(), e.Message);
    }

    [Fact]
    public async Task ServiceExceptionIsNotAConnectionError()
    {
        // A caller that treats HttpRequestException as "offline" must not swallow a rejected request.
        var stub = new StubHandler { Respond = _ => Json("""{"error": "nope"}""", HttpStatusCode.BadRequest) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Record.ExceptionAsync(() => advisor.AdviseAsync("{}", CancellationToken.None));
        Assert.IsType<AdviceServiceException>(e);
        Assert.False(e is HttpRequestException);
    }

    [Theory]
    [InlineData("<html>Bad Gateway</html>", "<html>Bad Gateway</html>")]
    [InlineData("""{"error": 5}""", """{"error": 5}""")]
    [InlineData("""{"detail": "x"}""", """{"detail": "x"}""")]
    [InlineData("""["a"]""", """["a"]""")]
    public async Task ErrorTextFallsBackToTheRawBody(string body, string expected)
    {
        var stub = new StubHandler { Respond = _ => Json(body, HttpStatusCode.BadGateway) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
        Assert.Equal(502, e.StatusCode);
        Assert.Equal(expected, e.ErrorText);
    }

    [Fact]
    public async Task ErrorTextIsTruncatedAndNeverEmpty()
    {
        var stub = new StubHandler { Respond = _ => Json(new string('x', 5000), HttpStatusCode.InternalServerError) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
        Assert.InRange(e.ErrorText.Length, 1, 400);

        stub.Respond = _ => new HttpResponseMessage(HttpStatusCode.ServiceUnavailable);
        var empty = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
        Assert.Equal(503, empty.StatusCode);
        Assert.False(string.IsNullOrWhiteSpace(empty.ErrorText));
    }

    [Theory]
    [InlineData("")]
    [InlineData("oops")]
    [InlineData("[]")]
    [InlineData("""{"options": 5}""")]
    [InlineData("""{"options": [{"index": 0, "status": "ok"}]}""")]
    public async Task ASuccessfulStatusWithAMalformedBodyIsAFormatError(string body)
    {
        var stub = new StubHandler { Respond = _ => Json(body) };
        using var advisor = new SidecarAdvisor(8767, stub);
        await Assert.ThrowsAsync<AdviceFormatException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
    }

    [Fact]
    public async Task ConnectionFailuresKeepThrowingHttpRequestException()
    {
        var stub = new StubHandler { Respond = _ => throw new HttpRequestException("refused") };
        using var advisor = new SidecarAdvisor(8767, stub);
        await Assert.ThrowsAsync<HttpRequestException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
    }

    [Fact]
    public async Task ATimeoutKeepsThrowingTaskCanceledException()
    {
        var stub = new StubHandler { Delay = Forever };
        using var advisor = new SidecarAdvisor(8767, stub, TimeSpan.FromMilliseconds(50));
        await Assert.ThrowsAnyAsync<TaskCanceledException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
    }

    [Fact]
    public void DefaultTimeoutIsTwoSeconds()
    {
        Assert.Equal(TimeSpan.FromSeconds(2), SidecarAdvisor.DefaultTimeout);
        using var byDefault = new SidecarAdvisor(8767, new StubHandler());
        Assert.Equal(SidecarAdvisor.DefaultTimeout, byDefault.Timeout);
        using var custom = new SidecarAdvisor(8767, new StubHandler(), TimeSpan.FromSeconds(7));
        Assert.Equal(TimeSpan.FromSeconds(7), custom.Timeout);
    }

    [Fact]
    public async Task HealthyOnlyWhenTheServiceSaysOk()
    {
        var stub = new StubHandler { Respond = _ => Json("""{"ok": true, "policy": "greedy"}""") };
        using var advisor = new SidecarAdvisor(8767, stub);
        Assert.True(await advisor.IsHealthyAsync(CancellationToken.None));
        Assert.Equal(("GET", "/health", null), stub.Seen[0]);

        stub.Respond = _ => Json("""{"ok": false}""");
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
        stub.Respond = _ => Json("{}", HttpStatusCode.ServiceUnavailable);
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
        stub.Respond = _ => throw new HttpRequestException("refused");
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
    }

    [Theory]
    [InlineData("")]
    [InlineData("not json")]
    [InlineData("[]")]
    [InlineData("[true]")]
    [InlineData("42")]
    [InlineData("null")]
    [InlineData("true")]
    [InlineData("\"ok\"")]
    [InlineData("""{"ok": "true"}""")]
    [InlineData("""{"ok": 1}""")]
    [InlineData("""{"status": "ok"}""")]
    public async Task AMalformedHealthBodyIsUnhealthyNotAnException(string body)
    {
        var stub = new StubHandler { Respond = _ => Json(body) };
        using var advisor = new SidecarAdvisor(8767, stub);
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
    }

    [Fact]
    public async Task ATimedOutHealthCheckIsUnhealthy()
    {
        var stub = new StubHandler { Delay = Forever };
        using var advisor = new SidecarAdvisor(8767, stub, TimeSpan.FromMilliseconds(50));
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
    }

    [Fact]
    public async Task ACancelledHealthCheckRethrowsInsteadOfReportingUnhealthy()
    {
        var stub = new StubHandler { Delay = Forever };
        using var advisor = new SidecarAdvisor(8767, stub);
        using var cts = new CancellationTokenSource();
        cts.CancelAfter(50);
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => advisor.IsHealthyAsync(cts.Token));
    }
}
