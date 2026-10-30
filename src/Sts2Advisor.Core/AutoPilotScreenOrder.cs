// src/Sts2Advisor.Core/AutoPilotScreenOrder.cs
namespace Sts2Advisor.Core;

/// <summary>What the autopilot must deal with first when several screens are open at once.</summary>
public enum AutoPilotScreen { GameOver, Modal, Combat, Map, Overlay, Room }

/// <summary>Which screen is "on top", in the order the game itself resolves it. The game's
/// <c>ActiveScreenContext.GetCurrentScreen()</c> checks <c>NMapScreen.Instance.IsOpen</c> BEFORE
/// <c>NOverlayStack.Peek()</c>: after Proceed on the rewards screen the map opens while <c>NRewardsScreen</c>
/// stays in the overlay stack, and the map is the screen in front. AutoSlay's <c>DrainOverlayScreensAsync</c>
/// likewise stops on "Rewards screen handled and map is open". The autopilot therefore never touches an
/// overlay or the room while the map is open. Order: game over, a modal popup, combat, the open map, the
/// overlay stack, then the room itself.</summary>
public static class AutoPilotScreenOrder
{
    public static AutoPilotScreen Next(bool gameOver, bool modal, bool combat, bool mapOpen, bool overlay) =>
        gameOver ? AutoPilotScreen.GameOver
        : modal ? AutoPilotScreen.Modal
        : combat ? AutoPilotScreen.Combat
        : mapOpen ? AutoPilotScreen.Map
        : overlay ? AutoPilotScreen.Overlay
        : AutoPilotScreen.Room;
}
