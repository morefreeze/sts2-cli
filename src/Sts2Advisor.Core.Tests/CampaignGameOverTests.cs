// src/Sts2Advisor.Core.Tests/CampaignGameOverTests.cs
using Sts2Advisor.Core;

/// <summary>The game-over click-through is a state machine over what the screen offers, so that an attempt that
/// resumes after an interrupted one does the next needed click and never waits for a button that is already pressed.</summary>
public class CampaignGameOverTests
{
    private static GameOverAction Next(bool screen = true, bool cont = false, bool menu = false) =>
        CampaignGameOver.Next(new GameOverView(screen, cont, menu));

    [Fact]
    public void ANewlyOpenedScreenWaitsForTheContinueButton()
    {
        Assert.Equal(GameOverAction.Wait, Next());
    }

    [Fact]
    public void AnEnabledContinueButtonIsPressed()
    {
        Assert.Equal(GameOverAction.ClickContinue, Next(cont: true));
    }

    [Fact]
    public void AfterContinueWasPressedTheSummaryPlaysAndNothingIsWaitedFor()
    {
        // Continue is disabled again, the main menu button is not ready yet: the resumed attempt just waits.
        Assert.Equal(GameOverAction.Wait, Next(cont: false, menu: false));
    }

    [Fact]
    public void TheMainMenuButtonIsPressedWhenItIsReady()
    {
        Assert.Equal(GameOverAction.ClickMainMenu, Next(menu: true));
    }

    [Fact]
    public void TheMainMenuButtonComesBeforeContinue()
    {
        Assert.Equal(GameOverAction.ClickMainMenu, Next(cont: true, menu: true));
    }

    [Fact]
    public void AfterTheMainMenuButtonWasPressedBothAreDisabledAndTheScreenFadesOut()
    {
        Assert.Equal(GameOverAction.Wait, Next(cont: false, menu: false));
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(true, true)]
    [InlineData(false, true)]
    public void WhenTheScreenIsGoneItIsDoneWhateverTheButtonsSay(bool cont, bool menu)
    {
        Assert.Equal(GameOverAction.Done, Next(screen: false, cont: cont, menu: menu));
    }

    [Fact]
    public void AVictoryThatIsInterruptedAtEveryStageResumesWithoutAWrongClick()
    {
        // The stages a victory's screen goes through, and what a fresh attempt does at each.
        var stages = new[]
        {
            (new GameOverView(true, false, false), GameOverAction.Wait),         // animating in
            (new GameOverView(true, true, false), GameOverAction.ClickContinue), // continue offered
            (new GameOverView(true, false, false), GameOverAction.Wait),         // continue pressed: score lines, badges ...
            (new GameOverView(true, false, true), GameOverAction.ClickMainMenu), // summary done
            (new GameOverView(true, false, false), GameOverAction.Wait),         // main menu pressed: transition
            (new GameOverView(false, false, false), GameOverAction.Done),        // gone
        };
        foreach (var (view, expected) in stages)
            Assert.Equal(expected, CampaignGameOver.Next(view));
    }
}
