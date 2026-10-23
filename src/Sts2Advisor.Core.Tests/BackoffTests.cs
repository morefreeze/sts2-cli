using Sts2Advisor.Core;

public class BackoffTests
{
    [Fact]
    public void DoublesUpToTheCapThenResets()
    {
        var b = new Backoff(1, 30);
        Assert.True(b.Ready(0));
        var delays = new List<double>();
        for (int i = 0; i < 7; i++)
        {
            b.Failed(0);
            delays.Add(b.CurrentDelay);
        }
        Assert.Equal(new[] { 1.0, 2, 4, 8, 16, 30, 30 }, delays);
        b.Failed(100);
        Assert.False(b.Ready(129.9));
        Assert.True(b.Ready(130));
        b.Succeeded();
        Assert.True(b.Ready(0));
        b.Failed(0);
        Assert.Equal(1.0, b.CurrentDelay);
    }
}
