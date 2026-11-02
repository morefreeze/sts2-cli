// src/Sts2Advisor.Core.Tests/GridPickRouteTests.cs
using Sts2Advisor.Core;

public class GridPickRouteTests
{
    [Fact]
    public void AHolderShowingTheExactCardIsPressed()
    {
        Assert.Equal(GridPick.Holder, GridPickRoute.Choose(exactHolder: true, screenOffersCard: true, equalHolder: true));
        Assert.Equal(GridPick.Holder, GridPickRoute.Choose(exactHolder: true, screenOffersCard: false, equalHolder: false));
    }

    [Fact]
    public void ACardTheGridDoesNotShowIsSelectedThroughTheScreenApi()
    {
        // The grid only builds holders for the rows on (and just around) the screen: a deck of 29 upgradable cards
        // has no holder for card 25 until the grid is scrolled.
        Assert.Equal(GridPick.ScreenApi, GridPickRoute.Choose(exactHolder: false, screenOffersCard: true, equalHolder: false));
    }

    [Fact]
    public void TheScreenApiBeatsADifferentCardWithTheSameIdAndUpgradeLevel()
    {
        // An equal-looking holder is a different card object: the screen tracks its selection by object.
        Assert.Equal(GridPick.ScreenApi, GridPickRoute.Choose(exactHolder: false, screenOffersCard: true, equalHolder: true));
    }

    [Fact]
    public void AnEqualHolderIsTheLastResortForACardTheScreenDoesNotList()
    {
        Assert.Equal(GridPick.EqualHolder, GridPickRoute.Choose(exactHolder: false, screenOffersCard: false, equalHolder: true));
    }

    [Fact]
    public void ACardNeitherTheGridNorTheScreenKnowsCannotBePicked()
    {
        Assert.Equal(GridPick.None, GridPickRoute.Choose(exactHolder: false, screenOffersCard: false, equalHolder: false));
    }
}
