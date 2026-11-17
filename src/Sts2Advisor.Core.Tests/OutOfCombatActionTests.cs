using Sts2Advisor.Core;

public class OutOfCombatActionTests
{
    [Theory]
    [InlineData("claim_reward")]
    [InlineData("swap_potion")]
    [InlineData("leave_rewards")]
    [InlineData("shop")]
    [InlineData("map")]
    [InlineData("room")]
    public void FruitJuiceGoesBeforeThePendingAction(string pending)
    {
        var ranked = OutOfCombatAction.Rank(pending, ["POTION.FOUL_POTION", "POTION.FRUIT_JUICE"]);
        Assert.Equal(("use_potion", 1), (ranked[0].Name, ranked[0].PotionIndex));
        Assert.Equal(pending, ranked[1].Name);
        Assert.Equal(("use_potion", 0), (ranked[2].Name, ranked[2].PotionIndex));
    }

    [Fact]
    public void WithoutAKnownBenefitThePendingActionWins()
    {
        Assert.Equal("map", OutOfCombatAction.Rank("map", [])[0].Name);
        Assert.Equal("shop", OutOfCombatAction.Rank("shop", ["POTION.FOUL_POTION"])[0].Name);
    }

    [Fact]
    public void MultipleBottlesAreRankedInBeltOrder()
    {
        Assert.Equal(new int?[] { 0, 1, null },
                     OutOfCombatAction.Rank("shop", ["POTION.FRUIT_JUICE", "POTION.FRUIT_JUICE"])
                                      .Select(a => a.PotionIndex));
    }
}
