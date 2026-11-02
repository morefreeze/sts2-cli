using System.Net;
using System.Text;
using Sts2Advisor.Core;

public class PotionSwapTests
{
    private const string SwapJson = """
        {"discard": 1, "take": 0, "discard_id": "POTION.FIRE_POTION", "take_id": "POTION.FAIRY_IN_A_BOTTLE",
         "held_scores": [6.0, 3.0, 6.0], "offered_scores": [17.0], "margin": 2.0, "reason": "swap"}
        """;

    private sealed class Stub : HttpMessageHandler
    {
        public HttpResponseMessage Response = new(HttpStatusCode.OK);
        public string? Path, Body;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Path = request.RequestUri!.AbsolutePath;
            Body = request.Content == null ? null : await request.Content.ReadAsStringAsync(ct);
            return Response;
        }
    }

    private static HttpResponseMessage Json(string body, HttpStatusCode code = HttpStatusCode.OK) =>
        new(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

    [Fact]
    public void ParsesASwap()
    {
        var r = PotionSwapReply.Parse(SwapJson);
        Assert.Equal((1, 0, "swap"), (r.Discard, r.Take, r.Reason));
        Assert.Equal("POTION.FIRE_POTION", r.DiscardId);
        Assert.Equal("POTION.FAIRY_IN_A_BOTTLE", r.TakeId);
        Assert.Equal(3.0, r.DiscardScore);
        Assert.Equal(17.0, r.TakeScore);
        Assert.Equal(2.0, r.Margin);
    }

    [Fact]
    public void ParsesAKeepAndAFreeSlot()
    {
        var keep = PotionSwapReply.Parse("""{"discard": null, "take": null, "reason": "not_better", "held_scores": [3.0], "offered_scores": [3.0]}""");
        Assert.Null(keep.Discard);
        Assert.Null(keep.Take);
        Assert.Null(keep.DiscardScore);
        var free = PotionSwapReply.Parse("""{"discard": null, "take": 2, "reason": "free_slot", "offered_scores": [3.0]}""");
        Assert.Equal(2, free.Take);
        Assert.Null(free.TakeScore);  // index 2 is not in the array: scores are only shown
    }

    [Theory]
    [InlineData("")]
    [InlineData("[]")]
    [InlineData("""{"take": 0}""")]                                   // no reason
    [InlineData("""{"reason": "swap", "take": -1}""")]
    [InlineData("""{"reason": "swap", "take": "0"}""")]
    [InlineData("""{"reason": "swap", "take": 0.5}""")]
    [InlineData("""{"reason": 5}""")]
    [InlineData("""{"reason": "swap", "discard_id": 3}""")]
    public void MalformedRepliesAreFormatErrors(string body)
    {
        Assert.Throws<AdviceFormatException>(() => PotionSwapReply.Parse(body));
    }

    [Fact]
    public async Task PostsToPotionSwapAndParses()
    {
        var stub = new Stub { Response = Json(SwapJson) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var r = await advisor.PotionSwapAsync("""{"state": {}, "offered": []}""", CancellationToken.None);
        Assert.Equal("/potion_swap", stub.Path);
        Assert.Equal("""{"state": {}, "offered": []}""", stub.Body);
        Assert.Equal("swap", r.Reason);
    }

    [Fact]
    public async Task ErrorsMapLikeAdvise()
    {
        var stub = new Stub { Response = Json("""{"error": "offered must be a list of potion objects"}""", HttpStatusCode.BadRequest) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var e = await Assert.ThrowsAsync<AdviceServiceException>(() => advisor.PotionSwapAsync("{}", CancellationToken.None));
        Assert.Equal(400, e.StatusCode);
        stub.Response = Json("oops");
        await Assert.ThrowsAsync<AdviceFormatException>(() => advisor.PotionSwapAsync("{}", CancellationToken.None));
    }

    [Fact]
    public void StepAndReasonText()
    {
        Assert.Equal("换药水：丢弃 火焰药水，拿 瓶中精灵", AutoPilotText.PotionSwapText("火焰药水", "瓶中精灵", zh: true));
        Assert.Equal("Swap potion: discard Fire Potion, take Fairy in a Bottle",
                     AutoPilotText.PotionSwapText("Fire Potion", "Fairy in a Bottle", zh: false));
        Assert.Equal("Belt full: the offered potion scores 17, at least 2 above the worst held one (3)",
                     AutoPilotText.PotionSwapReason(3.0, 17.0, 2.0, zh: false));
        Assert.Contains("17", AutoPilotText.PotionSwapReason(3.0, 17.0, 2.0, zh: true));
    }
}
