// src/Sts2Advisor/AutoPilot/StepDetails.cs
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>What a step chose, in the game's own words, for the panel: the description of the card, relic or
/// potion, as the game shows it (numbers resolved, current language). Event options, rest options and map nodes have
/// none. Main thread only, called just before the click while the choice is still on the screen. Display text must
/// never stop the autopilot: a description that cannot be read is left out (the first failure is logged).</summary>
internal static class StepDetails
{
    private static bool _failureLogged;

    public static IReadOnlyList<DetailItem> Of(CachedAdvice advice)
    {
        try
        {
            using var _ = GameText.Names();  // one swap for all the names and descriptions of the step
            return Collect(advice);
        }
        catch (Exception e)  // also a missing member: this is only text
        {
            LogOnce(e);
            return Array.Empty<DetailItem>();
        }
    }

    /// <summary>The descriptions of potions a swap names (the one discarded, the one taken), as the game shows them.</summary>
    public static IReadOnlyList<DetailItem> OfPotions(params PotionModel[] potions)
    {
        using var _ = GameText.Names();
        return potions.Select(p => PotionItem(p)).ToList();
    }

    private static DetailItem PotionItem(PotionModel p) =>
        Item(GameText.Potion(p), () => GameText.Description("potions", p.Id.Entry + ".description",
                                                            () => p.DynamicDescription.GetFormattedText()), false);

    private static DetailItem RelicItem(RelicModel r) =>
        Item(GameText.Relic(r), () => GameText.Description("relics", r.Id.Entry + ".description",
                                                           () => r.DynamicDescription.GetFormattedText()), false);

    private static List<DetailItem> Collect(CachedAdvice a)
    {
        var cap = a.Capture;
        var items = new List<DetailItem>();
        object? Choice(int i) => i >= 0 && i < cap.Choices.Count ? cap.Choices[i] : null;
        switch (a.Plan.Kind)
        {
            case PlanKind.Pick when cap.Header.Decision is "card_reward" or "card_select":
                // A smith shows the card as it will be; a removal or transform, the card that goes.
                bool upgrade = cap.Screen == nameof(NDeckUpgradeSelectScreen);
                foreach (int i in a.Plan.Indices)
                    if (Choice(i) is CardModel card)
                        items.Add(CardItem(card, upgrade));
                break;
            case PlanKind.Pick when cap.Header.Decision == "bundle_select":
                if (a.Plan.Indices.Count > 0 && Choice(a.Plan.Indices[0]) is IReadOnlyList<CardModel> bundle)
                    items.AddRange(bundle.Select(c => CardItem(c, false)));
                break;
            case PlanKind.BuyCard:
                if (a.Plan.Indices.Count > 0 && Choice(a.Plan.Indices[0]) is MerchantCardEntry { CreationResult: { } made })
                    items.Add(CardItem(made.Card, false));
                break;
            case PlanKind.BuyRelic:
                if (a.Plan.Indices.Count > 0 && cap.Inventory is { } ri && a.Plan.Indices[0] < ri.RelicEntries.Count
                    && ri.RelicEntries[a.Plan.Indices[0]].Model is { } relic)
                    items.Add(RelicItem(relic));
                break;
            case PlanKind.BuyPotion:
                if (a.Plan.Indices.Count > 0 && cap.Inventory is { } pi && a.Plan.Indices[0] < pi.PotionEntries.Count
                    && pi.PotionEntries[a.Plan.Indices[0]].Model is { } potion)
                    items.Add(PotionItem(potion));
                break;
        }
        return items;
    }

    private static DetailItem CardItem(CardModel card, bool afterUpgrade)
    {
        // A card that cannot be upgraded stays as it is.
        bool upgraded = afterUpgrade && card.IsUpgradable;
        return Item(GameText.Card(card),
                    () => GameText.Description("cards", card.Id.Entry + ".description", () => CardDescription(card, upgraded)),
                    upgraded);
    }

    /// <summary>The recipe of the game's own card library (NCardLibrary): the live card's description for the
    /// "no pile" case; for an upgrade, a clone that has been upgraded, read with GetDescriptionForUpgradePreview.
    /// The clone keeps the live card's dynamic vars untouched.</summary>
    private static string CardDescription(CardModel card, bool afterUpgrade)
    {
        if (!afterUpgrade)
            return card.GetDescriptionForPile(PileType.None);
        var clone = (CardModel)card.MutableClone();
        clone.UpgradeInternal();
        return clone.GetDescriptionForUpgradePreview();
    }

    private static DetailItem Item(string name, Func<string> description, bool afterUpgrade)
    {
        try
        {
            return new DetailItem(name, description(), afterUpgrade);
        }
        catch (Exception e)
        {
            LogOnce(e);
            return new DetailItem(name, "");
        }
    }

    private static void LogOnce(Exception e)
    {
        if (_failureLogged)
            return;
        _failureLogged = true;
        ModLog.Error($"autopilot: could not read a step description (the panel shows the step without it; logged once): {e}");
    }
}
