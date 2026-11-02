// src/Sts2Advisor.Core/GridPickRoute.cs
namespace Sts2Advisor.Core;

/// <summary>How the autopilot presses one advised card on a deck / card select screen.
/// Holder: the card holder that shows exactly that card object (the player's click path: Pressed -> the grid's
/// HolderPressed -> the screen's OnCardClicked(card)). ScreenApi: the screen's own OnCardClicked(card), which is
/// the end of that same path. EqualHolder: a holder showing another copy of the card (same id, same upgrade level).
/// None: the card is nowhere.</summary>
public enum GridPick { None, Holder, ScreenApi, EqualHolder }

/// <summary>NCardGrid is virtualised: it builds holders for the rows in (and a row around) the viewport only and
/// recycles them while scrolling (NCardGrid.InitGrid / AllocateCardHolders), so a deck of 29 cards has no holder for
/// card 25 until the grid is scrolled. Every NCardGridSelectionScreen keeps its selection as CardModels and its
/// click handler takes the CardModel, not the holder, so a card without a holder can still be selected.</summary>
public static class GridPickRoute
{
    /// <param name="exactHolder">A not yet pressed holder shows this very card object.</param>
    /// <param name="screenOffersCard">The screen's own card list contains this very card object.</param>
    /// <param name="equalHolder">A not yet pressed holder shows another card with the same id and upgrade level.</param>
    public static GridPick Choose(bool exactHolder, bool screenOffersCard, bool equalHolder) =>
        exactHolder ? GridPick.Holder
        : screenOffersCard ? GridPick.ScreenApi  // the screen tracks its selection by object: an equal copy is not the same pick
        : equalHolder ? GridPick.EqualHolder
        : GridPick.None;
}
