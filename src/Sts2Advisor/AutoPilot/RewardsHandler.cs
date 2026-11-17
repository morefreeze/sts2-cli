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
            var buttons = UiHelper.FindAll<NRewardButton>(screen).Where(b => b.IsEnabled && !_attempted.Contains(b)).ToList();
            var button = buttons.FirstOrDefault(b => b.Reward is not PotionReward || hasPotionSlots);
            var swapButton = !hasPotionSlots && CombatManager.Instance?.IsInProgress != true
                ? buttons.FirstOrDefault(b => !_swapAsked.Contains(b) && b.Reward is PotionReward)
                : null;
            string pendingAction = swapButton != null ? "swap_potion" : button != null ? "claim_reward" : "leave_rewards";
            if (await host.TryUsePotionAsync(pendingAction, ct))
                continue;  // Re-read the belt and the eligible rewards after the potion's effect.
            if (me != null && swapButton?.Reward is PotionReward potionReward)
            {
                _swapAsked.Add(swapButton);
                if (await host.MakeRoomForPotionAsync(me, potionReward, ct))
                    button = swapButton;
            }
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
