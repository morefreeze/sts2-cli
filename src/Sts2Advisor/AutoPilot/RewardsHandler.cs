// src/Sts2Advisor/AutoPilot/RewardsHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Advisor.AutoPilot;

/// <summary>AutoSlay's RewardsScreenHandler, except that the buttons already clicked on this rewards screen
/// are remembered across calls: a skipped card reward leaves its button enabled, and must not be reopened.</summary>
internal sealed class RewardsHandler(IAutoPilotHost host) : IScreenHandler
{
    private NRewardsScreen? _screen;
    private readonly HashSet<NRewardButton> _attempted = new();
    private readonly HashSet<NRewardButton> _swapAsked = new();

    public Type ScreenType => typeof(NRewardsScreen);
    public TimeSpan Timeout => TimeSpan.FromSeconds(30);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var screen = AutoSlayer.GetCurrentScreen<NRewardsScreen>();
        if (!ReferenceEquals(screen, _screen))
        {
            _screen = screen;
            _attempted.Clear();
            _swapAsked.Clear();
        }
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var me = LocalContext.GetMe(RunManager.Instance.DebugOnlyGetState());
            bool hasPotionSlots = me?.HasOpenPotionSlots ?? false;
            NRewardButton? button = null;
            if (me != null && !hasPotionSlots && CombatManager.Instance?.IsInProgress != true)
            {
                // A full belt used to leave the potion on the floor, Fairy in a Bottle included. Ask once per
                // button whether a held potion should make room; if so the reward is claimed right away.
                var potionButton = UiHelper.FindAll<NRewardButton>(screen).FirstOrDefault(b =>
                    b.IsEnabled && !_attempted.Contains(b) && !_swapAsked.Contains(b) && b.Reward is PotionReward);
                if (potionButton?.Reward is PotionReward potionReward)
                {
                    _swapAsked.Add(potionButton);
                    if (await host.MakeRoomForPotionAsync(me, potionReward, ct))
                    {
                        hasPotionSlots = true;
                        button = potionButton;
                    }
                }
            }
            button ??= UiHelper.FindAll<NRewardButton>(screen).FirstOrDefault(b =>
                b.IsEnabled && !_attempted.Contains(b) && (b.Reward is not PotionReward || hasPotionSlots));
            if (button == null)
                break;
            _attempted.Add(button);
            await UiHelper.Click(button);
            await Task.Delay(500, ct);
            var top = NOverlayStack.Instance?.Peek();
            if (top != null && top != screen)
                return;  // a card reward (or another child screen) opened: the loop handles it, then comes back
        }
        var proceed = UiHelper.FindFirst<NProceedButton>(screen);
        if (proceed != null)
        {
            await UiHelper.Click(proceed);
            await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || NOverlayStack.Instance?.Peek() != screen
                                         || (NMapScreen.Instance?.IsOpen ?? false),
                                   ct, TimeSpan.FromSeconds(10), "Rewards screen did not close after proceed");
        }
    }
}
