using System.Net;
using System.Text;
using Sts2Advisor.Core;

public class SidecarAdvisorTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Respond = _ => new HttpResponseMessage(HttpStatusCode.OK);
        public readonly List<(string Method, string Path, string? Body)> Seen = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Seen.Add((request.Method.Method, request.RequestUri!.AbsolutePath,
                      request.Content == null ? null : await request.Content.ReadAsStringAsync(ct)));
            return Respond(request);
        }
    }

    private static HttpResponseMessage Json(string body, HttpStatusCode code = HttpStatusCode.OK) =>
        new(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

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
    public async Task ServerErrorThrows()
    {
        var stub = new StubHandler { Respond = _ => Json("{}", HttpStatusCode.InternalServerError) };
        using var advisor = new SidecarAdvisor(8767, stub);
        await Assert.ThrowsAsync<HttpRequestException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
    }

    [Theory]
    [InlineData(HttpStatusCode.BadRequest)]
    [InlineData(HttpStatusCode.RequestEntityTooLarge)]
    [InlineData(HttpStatusCode.InternalServerError)]
    public async Task ErrorBodyWithErrorStatusThrows(HttpStatusCode code)
    {
        // The service answers a bad request with {"error": "..."} and 400 / 413 / 500.
        var stub = new StubHandler { Respond = _ => Json("""{"error": "bad request"}""", code) };
        using var advisor = new SidecarAdvisor(8767, stub);
        await Assert.ThrowsAsync<HttpRequestException>(() => advisor.AdviseAsync("""{"x": 1}""", CancellationToken.None));
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
}
