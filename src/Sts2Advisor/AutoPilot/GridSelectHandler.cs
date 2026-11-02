// src/Sts2Advisor/AutoPilot/GridSelectHandler.cs
using System.Reflection;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>How an AutoSlay grid handler presses a card: the holder's own Pressed signal, or the grid's HolderPressed.</summary>
internal enum GridPress { Holder, Grid }

/// <summary>Presses exactly the advised cards on a deck/card select screen, then lets the AutoSlay handler for that
/// screen do the rest (preview, confirm). The AutoSlay handlers stop picking as soon as Confirm is enabled, which
/// is after MinSelect cards: with an advised range (Min &lt; Max) they would never reach the advised count, and
/// with Min 0 they would confirm an empty selection.
/// NCardGrid is virtualised (it builds holders for the rows around the viewport only), so a big deck has no holder
/// for a card further down; such a card is selected through the screen's own OnCardClicked(CardModel), the method the
/// grid's HolderPressed signal ends in (see <see cref="GridPickRoute"/>).</summary>
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

        var cardScreen = screen as NCardGridSelectionScreen
            ?? throw new AutoPilotException($"{ScreenType.Name} is not a card grid selection screen");
        var pressed = new HashSet<NGridCardHolder>();
        host.StepTaken(advice);
        foreach (int i in advice.Plan.Indices)
        {
            ct.ThrowIfCancellationRequested();
            if (i >= advice.Capture.Choices.Count)
                throw new AutoPilotException($"{ScreenType.Name}: advised index {i} is outside the {advice.Capture.Choices.Count} cards");
            object model = advice.Capture.Choices[i];
            holders = UiHelper.FindAll<NGridCardHolder>(screen);
            var exact = holders.FirstOrDefault(h => !pressed.Contains(h) && ReferenceEquals(h.CardModel, model));
            var equal = exact != null ? null
                : holders.FirstOrDefault(h => !pressed.Contains(h) && UiModel.SameCard(h.CardModel, model));
            bool offered = model is CardModel && cardScreen._cards.Any(c => ReferenceEquals(c, model));
            switch (GridPickRoute.Choose(exact != null, offered, equal != null))
            {
                case GridPick.Holder:
                case GridPick.EqualHolder:
                    var holder = (exact ?? equal)!;
                    pressed.Add(holder);
                    if (grid != null)
                        grid.EmitSignal(NCardGrid.SignalName.HolderPressed, holder);
                    else
                        holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
                    break;
                case GridPick.ScreenApi:
                    ModLog.Info($"autopilot: {ScreenType.Name} card {i} has no holder on screen (the grid builds only the rows in view); selecting it through the screen");
                    SelectThroughScreen(cardScreen, (CardModel)model);
                    break;
                default:
                    throw new AutoPilotException($"{ScreenType.Name}: advised card {i} is neither shown by a card holder nor listed by the screen");
            }
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

    /// <summary>NCardGridSelectionScreen.OnCardClicked(CardModel) is protected abstract (the Publicizer leaves virtual
    /// members alone), so it is called by reflection; the subclass's override runs.</summary>
    private static readonly MethodInfo? OnCardClicked =
        typeof(NCardGridSelectionScreen).GetMethod("OnCardClicked", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                                                   null, new[] { typeof(CardModel) }, null);

    private static void SelectThroughScreen(NCardGridSelectionScreen screen, CardModel card)
    {
        if (OnCardClicked == null)
            throw new AutoPilotException("NCardGridSelectionScreen.OnCardClicked(CardModel) not found: the game changed");
        try
        {
            OnCardClicked.Invoke(screen, new object[] { card });
        }
        catch (TargetInvocationException e) when (e.InnerException != null)
        {
            throw new AutoPilotException($"{screen.GetType().Name}.OnCardClicked failed: {e.InnerException.GetType().Name}: {e.InnerException.Message}");
        }
    }
}
