// src/Sts2Advisor.Core/CampaignGameOver.cs
namespace Sts2Advisor.Core;

/// <summary>What the game-over screen offers right now. <paramref name="ScreenOpen"/>: it is still the top overlay.
/// <paramref name="ContinueEnabled"/>: the Continue button (to the run summary) can be pressed.
/// <paramref name="MainMenuReady"/>: the return-to-main-menu button is shown and enabled (after the summary played).</summary>
public readonly record struct GameOverView(bool ScreenOpen, bool ContinueEnabled, bool MainMenuReady);

public enum GameOverAction { Done, ClickContinue, ClickMainMenu, Wait }

/// <summary>The click-through of the game-over screen (victory or defeat) as a decision over what the screen offers,
/// not as a script: whoever asks -- the first attempt, or one resuming after an interrupted attempt -- gets the next
/// click that is needed. A button that was already pressed is disabled (Continue stays so, the main menu button is
/// disabled on press), so a resumed attempt waits for what comes next instead of for a button that is gone.</summary>
public static class CampaignGameOver
{
    public static GameOverAction Next(GameOverView v)
    {
        if (!v.ScreenOpen)
            return GameOverAction.Done;       // the screen is gone: on its way to the timeline / main menu
        if (v.MainMenuReady)
            return GameOverAction.ClickMainMenu;
        if (v.ContinueEnabled)
            return GameOverAction.ClickContinue;
        return GameOverAction.Wait;           // animating in, the summary playing, or the exit transition
    }
}
