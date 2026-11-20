using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;

namespace Sts2Shared;

internal static class ActPlanningContext
{
    internal static Dictionary<string, object?> Capture(Player player)
        => new()
        {
            ["character"] = player.Character.Id.Entry,
            ["rest_heal_hp"] = (int)HealRestSiteOption.GetHealAmount(player),
            ["potion_slots"] = player.MaxPotionCount,
            ["relic_ids"] = player.Relics.Where(relic => !relic.IsMelted).Select(relic => relic.Id.Entry).ToArray(),
        };

    internal static int MailboxRefillHpCredit(Player player)
    {
        int open = player.PotionSlots.Count(slot => slot == null);
        return open <= 1 && player.Potions.Any()
            && player.Relics.Any(relic => !relic.IsMelted && relic.Id.Entry == "TINY_MAILBOX")
            && !player.Relics.Any(relic => !relic.IsMelted && relic.Id.Entry == "SOZU") ? 2 : 0;
    }
}
