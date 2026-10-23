// src/Sts2Shared/DecisionSerializer.cs
// Game model -> decision JSON (Dictionary<string, object?>), shared by Sts2Headless and the
// Sts2Advisor mod (linked into both). Pure: reads models, never advances the game.
// Moved verbatim from RunSimulator.cs -- key order is part of the byte-identical replay golden
// (tools/replay_states.py), so never reorder or merge the per-site card dicts.
// No Godot references allowed here.
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Shared;

internal static class DecisionSerializer
{
    // <- RunSimulator.RunContext()
    public static Dictionary<string, object?> RunContext(RunState runState, LocLookup loc)
    {
        var ctx = new Dictionary<string, object?>
        {
            ["act"] = runState.CurrentActIndex + 1,
            ["act_name"] = loc.Act(runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = runState.ActFloor,
            ["room_type"] = runState.CurrentRoom?.RoomType.ToString(),
        };

        // Boss encounter info — use BossEncounter?.Id?.Entry
        try
        {
            var bossIdEntry = runState.Act?.BossEncounter?.Id?.Entry;
            if (!string.IsNullOrEmpty(bossIdEntry))
            {
                var monsterKey = bossIdEntry.EndsWith("_BOSS") ? bossIdEntry[..^5] : bossIdEntry;
                // Handle special mappings
                if (monsterKey == "THE_KIN") monsterKey = "KIN_PRIEST";
                ctx["boss"] = new Dictionary<string, object?>
                {
                    ["id"] = bossIdEntry,
                    ["name"] = loc.BossName(bossIdEntry, monsterKey),
                };
            }
        }
        catch { }

        return ctx;
    }

    // <- RunSimulator.PlayerSummary(Player)
    public static Dictionary<string, object?> PlayerSummary(Player player, LocLookup loc)
    {
        return new Dictionary<string, object?>
        {
            ["name"] = loc.Bilingual("characters", (player.Character?.Id.Entry ?? "IRONCLAD") + ".title"),
            ["hp"] = player.Creature?.CurrentHp ?? 0,
            ["max_hp"] = player.Creature?.MaxHp ?? 0,
            ["block"] = player.Creature?.Block ?? 0,
            ["gold"] = player.Gold,
            ["relics"] = player.Relics?.Select(r =>
            {
                var vars = new Dictionary<string, object?>();
                try { foreach (var dv in r.DynamicVars.Values) vars[dv.Name] = (int)dv.BaseValue; } catch { }
                return new Dictionary<string, object?>
                {
                    ["name"] = loc.Relic(r.Id.Entry),
                    ["description"] = loc.Bilingual("relics", r.Id.Entry + ".description"),
                    ["vars"] = vars.Count > 0 ? vars : null,
                };
            }).ToList(),
            ["potions"] = player.Potions?.Select((p, i) =>
            {
                if (p == null) return null;
                var pvars = new Dictionary<string, object?>();
                try { foreach (var dv in p.DynamicVars.Values) pvars[dv.Name] = (int)dv.BaseValue; } catch { }
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    // Stable identity, matching the "id" field already exposed for hand cards
                    // (c.Id.ToString() there too) -- needed so a plan_combat_turn use_potion
                    // action's PotionId (a bare entry like "FIRE_POTION"; see
                    // Search/CombatBeamSolver.Expansion.cs's `PotionId: potion.Id.Entry`) can be
                    // resolved to this live "index" by identity instead of assuming any numeric
                    // relationship to the solver's PotionSlot. PotionSlot addresses the physical
                    // belt (player.PotionSlots, fixed-size, holes for empty slots); this "index"
                    // is a position in player.Potions, the real game's OWN compacted enumerable
                    // (Where(p => p != null) over PotionSlots) that DoUsePotion's potion_index
                    // argument already indexes into -- the two numberings diverge the moment any
                    // earlier belt slot is consumed, which is exactly the bug this "id" field
                    // exists to let callers route around (see docs/superpowers/plans/
                    // 2026-09-17-combatsolver-port-phase1.md's Task 5 postmortem).
                    ["id"] = p.Id.ToString(),
                    ["name"] = loc.Potion(p.Id.Entry),
                    ["description"] = loc.Bilingual("potions", p.Id.Entry + ".description"),
                    ["vars"] = pvars.Count > 0 ? pvars : null,
                    ["target_type"] = p.TargetType.ToString(),
                };
            }).Where(x => x != null).ToList(),
            ["deck_size"] = player.Deck?.Cards?.Count(c => c != null) ?? 0,
            ["deck"] = player.Deck?.Cards?.Where(c => c != null).Select(c =>
            {
                var dstats = new Dictionary<string, object?>();
                try { foreach (var dv in c.DynamicVars.Values) dstats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                var dkws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                var dcard = new Dictionary<string, object?>
                {
                    ["id"] = c.Id.ToString(),
                    ["name"] = loc.Card(c.Id.Entry),
                    ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
                    ["type"] = c.Type.ToString(),
                    ["upgraded"] = c.IsUpgraded,
                    ["description"] = loc.Bilingual("cards", c.Id.Entry + ".description"),
                    ["stats"] = dstats.Count > 0 ? dstats : null,
                    ["keywords"] = dkws?.Count > 0 ? dkws : null,
                    ["after_upgrade"] = UpgradedInfo(c, loc),
                };
                // Enchantment/affliction metadata, matching the combat hand export so clients
                // can see e.g. Slither applied to a deck card after an event (#76).
                if (c.Enchantment != null)
                {
                    dcard["enchantment"] = loc.Bilingual("enchantments", c.Enchantment.Id.Entry + ".title");
                    try { if (c.Enchantment.Amount != 0) dcard["enchantment_amount"] = c.Enchantment.Amount; } catch { }
                }
                if (c.Affliction != null)
                {
                    dcard["affliction"] = loc.Bilingual("afflictions", c.Affliction.Id.Entry + ".title");
                    try { if (c.Affliction.Amount != 0) dcard["affliction_amount"] = c.Affliction.Amount; } catch { }
                }
                return dcard;
            }).ToList(),
        };
    }

    // <- RunSimulator.GetUpgradedInfo(CardModel)
    public static Dictionary<string, object?>? UpgradedInfo(CardModel card, LocLookup loc)
    {
        if (!card.IsUpgradable) return null;
        try
        {
            var clone = ModelDb.GetById<CardModel>(card.Id).ToMutable();
            // Apply existing upgrades first
            for (int i = 0; i < card.CurrentUpgradeLevel; i++)
            {
                clone.UpgradeInternal();
                clone.FinalizeUpgradeInternal();
            }
            // Apply one more upgrade
            clone.UpgradeInternal();
            clone.FinalizeUpgradeInternal();

            var stats = new Dictionary<string, object?>();
            try { foreach (var dv in clone.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }

            // Compare keywords before/after upgrade
            var oldKws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var newKws = clone.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToHashSet() ?? new();
            var addedKws = newKws.Except(oldKws).ToList();
            var removedKws = oldKws.Except(newKws).ToList();

            return new Dictionary<string, object?>
            {
                ["cost"] = clone.EnergyCost?.GetResolved() ?? 0,
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = loc.Bilingual("cards", card.Id.Entry + ".description"),
                ["added_keywords"] = addedKws.Count > 0 ? addedKws : null,
                ["removed_keywords"] = removedKws.Count > 0 ? removedKws : null,
            };
        }
        catch { return null; }
    }
}
