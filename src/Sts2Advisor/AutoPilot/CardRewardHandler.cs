// src/Sts2Advisor/AutoPilot/CardRewardHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Takes the advised card or presses Skip (AutoSlay's CardRewardScreenHandler always takes one).</summary>
internal sealed class CardRewardHandler(IAutoPilotHost host) : IScreenHandler
{
    public Type ScreenType => typeof(NCardRewardSelectionScreen);
    public TimeSpan Timeout => TimeSpan.FromSeconds(60);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var screen = AutoSlayer.GetCurrentScreen<NCardRewardSelectionScreen>();
        await Task.Delay(400, ct);
        var advice = await host.FetchAdviceAsync(ct);
        if (advice.Capture.Header.Decision != "card_reward")
            throw new AutoPilotException($"card reward screen, but the settled decision is {advice.Capture.Header.Decision}");
        if (advice.Plan.Kind == PlanKind.Skip)
        {
            int j = screen._extraOptions.ToList().FindIndex(a => string.Equals(a.OptionId, "Skip", StringComparison.OrdinalIgnoreCase));
            var buttons = screen._rewardAlternativesContainer.GetChildren().OfType<NCardRewardAlternativeButton>().ToList();
            if (j < 0 || j >= buttons.Count)
                throw new AutoPilotException("card reward: the advice skips but there is no skip button");
            host.StepTaken(advice);
            await UiHelper.Click(buttons[j]);
        }
        else
        {
            int i = advice.Plan.Indices[0];
            if (i >= advice.Capture.Choices.Count)
                throw new AutoPilotException($"card reward: index {i} is outside the {advice.Capture.Choices.Count} cards");
            var card = advice.Capture.Choices[i];
            var holder = UiHelper.FindAll<NCardHolder>(screen).FirstOrDefault(h => ReferenceEquals(h.CardModel, card))
                ?? throw new AutoPilotException($"card reward: no card holder shows card {i}");
            host.StepTaken(advice);
            holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
        }
        await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || !screen.IsVisibleInTree(),
                               ct, TimeSpan.FromSeconds(10), "Card reward screen did not close");
    }
}
