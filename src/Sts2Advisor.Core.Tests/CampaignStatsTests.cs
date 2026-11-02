// src/Sts2Advisor.Core.Tests/CampaignStatsTests.cs
using Sts2Advisor.Core;

public class CampaignStatsTests
{
    [Fact]
    public void CountsRunsAndWins()
    {
        var s = new CampaignStats();
        Assert.Equal(0, s.Runs);
        s.Record(victory: false);
        s.Record(victory: true);
        s.Record(victory: false);
        Assert.Equal(3, s.Runs);
        Assert.Equal(1, s.Wins);
    }
}
