// src/Sts2Advisor.Core.Tests/AutoPilotScreenOrderTests.cs
using Sts2Advisor.Core;

public class AutoPilotScreenOrderTests
{
    private static AutoPilotScreen Next(bool gameOver = false, bool modal = false, bool combat = false,
                                        bool mapOpen = false, bool overlay = false) =>
        AutoPilotScreenOrder.Next(gameOver, modal, combat, mapOpen, overlay);

    [Fact]
    public void NothingOpenMeansTheRoom() => Assert.Equal(AutoPilotScreen.Room, Next());

    [Fact]
    public void AnOverlayAloneIsTheOverlay() => Assert.Equal(AutoPilotScreen.Overlay, Next(overlay: true));

    [Fact]
    public void AnOpenMapAloneIsTheMap() => Assert.Equal(AutoPilotScreen.Map, Next(mapOpen: true));

    [Fact]
    public void TheOpenMapComesBeforeTheOverlayStack()
    {
        // After Proceed on the rewards screen the game opens the map and leaves NRewardsScreen in the stack;
        // ActiveScreenContext.GetCurrentScreen() resolves that to the map.
        Assert.Equal(AutoPilotScreen.Map, Next(mapOpen: true, overlay: true));
    }

    [Fact]
    public void CombatComesBeforeTheMapTheOverlayAndTheRoom()
    {
        Assert.Equal(AutoPilotScreen.Combat, Next(combat: true));
        Assert.Equal(AutoPilotScreen.Combat, Next(combat: true, mapOpen: true));
        Assert.Equal(AutoPilotScreen.Combat, Next(combat: true, overlay: true));
        Assert.Equal(AutoPilotScreen.Combat, Next(combat: true, mapOpen: true, overlay: true));
    }

    [Fact]
    public void AModalComesBeforeCombatTheMapAndTheOverlay()
    {
        Assert.Equal(AutoPilotScreen.Modal, Next(modal: true));
        Assert.Equal(AutoPilotScreen.Modal, Next(modal: true, combat: true));
        Assert.Equal(AutoPilotScreen.Modal, Next(modal: true, mapOpen: true));
        Assert.Equal(AutoPilotScreen.Modal, Next(modal: true, overlay: true));
        Assert.Equal(AutoPilotScreen.Modal, Next(modal: true, combat: true, mapOpen: true, overlay: true));
    }

    [Fact]
    public void GameOverComesBeforeEverything()
    {
        Assert.Equal(AutoPilotScreen.GameOver, Next(gameOver: true));
        Assert.Equal(AutoPilotScreen.GameOver, Next(gameOver: true, overlay: true));
        Assert.Equal(AutoPilotScreen.GameOver, Next(gameOver: true, mapOpen: true));
        Assert.Equal(AutoPilotScreen.GameOver, Next(gameOver: true, combat: true));
        Assert.Equal(AutoPilotScreen.GameOver, Next(gameOver: true, modal: true));
        Assert.Equal(AutoPilotScreen.GameOver, Next(true, true, true, true, true));
    }

    [Fact]
    public void EveryCombinationFollowsTheOrderGameOverModalCombatMapOverlayRoom()
    {
        for (int bits = 0; bits < 32; bits++)
        {
            bool gameOver = (bits & 16) != 0, modal = (bits & 8) != 0, combat = (bits & 4) != 0,
                 mapOpen = (bits & 2) != 0, overlay = (bits & 1) != 0;
            var expected = gameOver ? AutoPilotScreen.GameOver
                : modal ? AutoPilotScreen.Modal
                : combat ? AutoPilotScreen.Combat
                : mapOpen ? AutoPilotScreen.Map
                : overlay ? AutoPilotScreen.Overlay
                : AutoPilotScreen.Room;
            Assert.Equal(expected, Next(gameOver, modal, combat, mapOpen, overlay));
        }
    }
}
