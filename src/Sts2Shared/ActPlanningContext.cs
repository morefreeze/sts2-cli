using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

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

    internal static (MapPoint Point, EncounterModel? Encounter) UpcomingBoss(RunState run)
    {
        var first = run.Map.BossMapPoint;
        if (run.Map.SecondBossMapPoint is { } second && run.CurrentMapCoord is { } here
            && (here.row > first.coord.row || here.row == first.coord.row && here.col == first.coord.col))
            return (second, run.Act?.SecondBossEncounter);
        return (first, run.Act?.BossEncounter);
    }

    internal static int MailboxRefillHpCredit(Player player)
    {
        int open = player.PotionSlots.Count(slot => slot == null);
        return open <= 1 && player.Potions.Any()
            && player.Relics.Any(relic => !relic.IsMelted && relic.Id.Entry == "TINY_MAILBOX")
            && !player.Relics.Any(relic => !relic.IsMelted && relic.Id.Entry == "SOZU") ? 2 : 0;
    }
}
