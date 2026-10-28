// src/Sts2Advisor/AutoPilot/GridSelectHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>How an AutoSlay grid handler presses a card: the holder's own Pressed signal, or the grid's HolderPressed.</summary>
internal enum GridPress { Holder, Grid }

/// <summary>Presses exactly the advised cards on a deck/card select screen, then lets the AutoSlay handler for that
/// screen do the rest (preview, confirm). The AutoSlay handlers stop picking as soon as Confirm is enabled, which
/// is after MinSelect cards: with an advised range (Min &lt; Max) they would never reach the advised count, and
/// with Min 0 they would confirm an empty selection.</summary>
internal sealed class GridSelectHandler(IAutoPilotHost host, IScreenHandler inner, GridPress press) : IScreenHandler
{
    public Type ScreenType => inner.ScreenType;
    public TimeSpan Timeout => inner.Timeout + TimeSpan.FromSeconds(30);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var screen = NOverlayStack.Instance?.Peek() as Node;
        if (screen == null || !ScreenType.IsInstanceOfType(screen))
            throw new AutoPilotException($"{ScreenType.Name} is not on top of the overlay stack");
        var holders = new List<NGridCardHolder>();
        await WaitHelper.Until(() =>
        {
            holders = UiHelper.FindAll<NGridCardHolder>(screen);
            return holders.Count > 0;
        }, ct, TimeSpan.FromSeconds(5), "No cards found in card select screen");
        await Task.Delay(300, ct);

        var advice = await host.FetchAdviceAsync(ct);
        if (advice.Capture.Header.Decision != "card_select")
            throw new AutoPilotException($"{ScreenType.Name}, but the settled decision is {advice.Capture.Header.Decision}");
        if (advice.Plan.Kind != PlanKind.Pick)
            throw new AutoPilotException($"{ScreenType.Name}: the advice is {advice.Plan.Kind}, autopilot can only pick cards here");
        var grid = press == GridPress.Grid
            ? UiHelper.FindFirst<NCardGrid>(screen) ?? throw new AutoPilotException($"{ScreenType.Name}: no card grid")
            : null;

        var pressed = new HashSet<NGridCardHolder>();
        host.StepTaken(advice);
        foreach (int i in advice.Plan.Indices)
        {
            ct.ThrowIfCancellationRequested();
            if (i >= advice.Capture.Choices.Count)
                throw new AutoPilotException($"{ScreenType.Name}: advised index {i} is outside the {advice.Capture.Choices.Count} cards");
            object model = advice.Capture.Choices[i];
            holders = UiHelper.FindAll<NGridCardHolder>(screen);
            var holder = holders.FirstOrDefault(h => !pressed.Contains(h) && ReferenceEquals(h.CardModel, model))
                ?? holders.FirstOrDefault(h => !pressed.Contains(h) && UiModel.SameCard(h.CardModel, model))
                ?? throw new AutoPilotException($"{ScreenType.Name}: no card holder shows advised card {i}");
            pressed.Add(holder);
            if (grid != null)
                grid.EmitSignal(NCardGrid.SignalName.HolderPressed, holder);
            else
                holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
            await Task.Delay(200, ct);
        }
        AutoPilotChoice.Consume();
        // A fixed-count select closes itself on the last press (NSimpleCardSelectScreen.CheckIfSelectionComplete);
        // the delegated handler would then throw for want of a screen.
        if (!OnTop())
            return;
        await inner.HandleAsync(random, ct);
        // Still there: the loop must not rerun this handler, whose presses would toggle the same cards off.
        for (int i = 0; i < 20 && OnTop(); i++)
            await Task.Delay(100, ct);
        if (OnTop())
            throw new AutoPilotException($"{ScreenType.Name} did not close after the advised picks");

        bool OnTop() => GodotObject.IsInstanceValid(screen) && NOverlayStack.Instance?.Peek() == (object)screen;
    }
}
