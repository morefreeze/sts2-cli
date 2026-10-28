// src/Sts2Advisor/AutoPilot/ShopHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Opens the merchant, then asks greedy one step at a time (buy a card/relic/potion, remove a card,
/// leave) and does it, re-asking after every purchase. A removal opens the deck select screen, which the
/// loop's overlay handling answers from the advice (card_select).</summary>
internal sealed class ShopHandler(IAutoPilotHost host) : IHandler
{
    private const int MaxSteps = 30;

    public TimeSpan Timeout => TimeSpan.FromMinutes(5);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var root = ((SceneTree)Engine.GetMainLoop()).Root;
        var room = await WaitHelper.ForNode<NMerchantRoom>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/MerchantRoom", ct);
        if (!room.Inventory.IsOpen)
        {
            room.OpenInventory();
            await Task.Delay(500, ct);
        }
        for (int step = 0; ; step++)
        {
            if (step >= MaxSteps)
                throw new AutoPilotException($"shop: more than {MaxSteps} steps");
            var advice = await host.FetchAdviceAsync(ct);
            if (advice.Capture.Header.Decision != "shop")
                throw new AutoPilotException($"in the shop, but the settled decision is {advice.Capture.Header.Decision}");
            if (advice.Plan.Kind == PlanKind.Leave)
            {
                host.StepTaken(advice);
                break;
            }
            var entry = EntryFor(advice);
            var inventory = room.Inventory.Inventory ?? throw new AutoPilotException("shop: no inventory");
            host.StepTaken(advice);
            var purchase = entry.OnTryPurchaseWrapper(inventory);
            await host.DrainUntilAsync(purchase, ct);
            if (!await purchase)
                throw new AutoPilotException($"shop: {advice.Plan.Kind} was refused");
            await Task.Delay(300, ct);
        }
        var back = UiHelper.FindFirst<NBackButton>(room);
        if (back != null && room.Inventory.IsOpen)
        {
            await UiHelper.Click(back);
            await Task.Delay(300, ct);
        }
        await UiHelper.Click(room.ProceedButton);
    }

    private static MerchantEntry EntryFor(CachedAdvice advice)
    {
        var cap = advice.Capture;
        var inv = cap.Inventory ?? throw new AutoPilotException("shop capture without an inventory");
        int i = advice.Plan.Indices.Count > 0 ? advice.Plan.Indices[0] : -1;
        MerchantEntry? entry = advice.Plan.Kind switch
        {
            PlanKind.BuyCard => i < cap.Choices.Count ? cap.Choices[i] as MerchantEntry : null,
            PlanKind.BuyRelic => i < inv.RelicEntries.Count ? inv.RelicEntries[i] : null,
            PlanKind.BuyPotion => i < inv.PotionEntries.Count ? inv.PotionEntries[i] : null,
            PlanKind.RemoveCard => inv.CardRemovalEntry,
            _ => throw new AutoPilotException($"shop: unexpected plan {advice.Plan.Kind}"),
        };
        return entry is { IsStocked: true }
            ? entry
            : throw new AutoPilotException($"shop: {advice.Plan.Kind} {i} is not for sale");
    }
}
