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

    // <- RunSimulator.MapSelectState(): the four identical {col,row,type} dicts
    public static Dictionary<string, object?> MapChoice(MapPoint point)
    {
        return new Dictionary<string, object?>
        {
            ["col"] = (int)point.coord.col,
            ["row"] = (int)point.coord.row,
            ["type"] = point.PointType.ToString(),
        };
    }

    // <- RunSimulator.MapSelectState() final return
    public static Dictionary<string, object?> MapSelect(RunState runState, List<Dictionary<string, object?>> choices, LocLookup loc)
    {
        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "map_select",
            ["context"] = RunContext(runState, loc),
            ["choices"] = choices,
            ["player"] = PlayerSummary(runState.Players[0], loc),
            ["act"] = runState.CurrentActIndex + 1,
            ["act_name"] = loc.Act(runState.Act?.Id.Entry ?? "OVERGROWTH"),
            ["floor"] = runState.ActFloor,
        };
    }

    // <- RunSimulator.EventChoiceState() from `var options = currentOptions`
    public static Dictionary<string, object?> EventChoice(EventModel localEvent, RunState runState, LocLookup loc)
    {
        var currentOptions = localEvent.CurrentOptions;
        var options = currentOptions
            .Select((opt, i) =>
            {
                // Try to resolve title via loc tables
                string? title = null;
                if (opt.Title != null)
                {
                    var t = loc.Bilingual(opt.Title.LocTable, opt.Title.LocEntryKey);
                    // Check if we actually found a translation (not just the key echoed back)
                    if (t != opt.Title.LocEntryKey)
                        title = t;
                }
                // Fallback: try to extract option ID from the key and look up as relic/card/potion
                if (title == null && opt.TextKey != null)
                {
                    // TextKey like "NEOW.pages.INITIAL.options.STONE_HUMIDIFIER" → extract "STONE_HUMIDIFIER"
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    // Try relic, then card, then just use the optionId
                    var relic = loc.Relic(optionId);
                    if (relic != optionId + ".title")
                        title = relic;
                    else
                    {
                        var card = loc.Card(optionId);
                        if (card != optionId + ".title")
                            title = card;
                        else
                            title = optionId.Replace("_", " ");
                    }
                }
                title ??= $"option_{i}";

                // Description: try loc table first
                string? optDesc = null;
                if (opt.Description != null && !string.IsNullOrEmpty(opt.Description.LocEntryKey))
                {
                    var d = loc.Bilingual(opt.Description.LocTable, opt.Description.LocEntryKey);
                    if (d != opt.Description.LocEntryKey)
                        optDesc = d;
                }
                // Fallback: try relic/card description
                if (optDesc == null && opt.TextKey != null)
                {
                    var parts = opt.TextKey.Split('.');
                    var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                    var rd = loc.Bilingual("relics", optionId + ".description");
                    if (rd != optionId + ".description")
                        optDesc = rd;
                }

                // Extract vars: try event's own DynamicVars first, then relic
                Dictionary<string, object?>? optVars = null;
                try
                {
                    // Event's DynamicVars (covers Gold, HpLoss, Heal, etc.)
                    if (localEvent.DynamicVars?.Values != null)
                    {
                        optVars = new Dictionary<string, object?>();
                        foreach (var dv in localEvent.DynamicVars.Values)
                            optVars[dv.Name] = (int)dv.BaseValue;
                    }
                }
                catch { }
                // Also try relic vars (for Neow options)
                if (opt.TextKey != null)
                {
                    try
                    {
                        var parts = opt.TextKey.Split('.');
                        var optionId = parts.Length > 0 ? parts[^1] : opt.TextKey;
                        var relicModel = ModelDb.GetById<RelicModel>(new ModelId("RELIC", optionId));
                        if (relicModel != null)
                        {
                            optVars ??= new Dictionary<string, object?>();
                            var mutable = relicModel.ToMutable();
                            foreach (var dv in mutable.DynamicVars.Values)
                                optVars[dv.Name] = (int)dv.BaseValue;
                        }
                    }
                    catch { }
                }

                // `RandomCard` (Slippery Bridge / Overcome) carries a *deck index*, but the
                // description template `{RandomCard}` should render that deck card's name. Resolve
                // it to the localized name so clients can substitute it (#58). Scoped to this var
                // name on purpose: other card vars (e.g. Wood Carvings' BirdCard/ToricCard) index
                // a transform-target pool, not the deck, so a generic rule would mis-resolve them.
                if (optVars != null && optVars.TryGetValue("RandomCard", out var rcVal) && rcVal is int rcIdx)
                {
                    try
                    {
                        var deck = runState.Players?[0]?.Deck?.Cards;
                        if (deck != null && rcIdx >= 0 && rcIdx < deck.Count && deck[rcIdx] != null)
                            optVars["RandomCard"] = loc.Card(deck[rcIdx].Id.Entry);
                    }
                    catch { }
                }

                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["title"] = title,
                    ["description"] = optDesc,
                    ["text_key"] = opt.TextKey,
                    ["is_locked"] = opt.IsLocked,
                    ["was_chosen"] = opt.WasChosen,
                    ["vars"] = optVars?.Count > 0 ? optVars : null,
                };
            }).ToList();

        // Resolve event name — try ancients table first (for Neow), then events
        var eventEntry = localEvent.Id?.Entry ?? localEvent.GetType().Name.ToUpperInvariant();
        var eventName = loc.Bilingual("ancients", eventEntry + ".title");
        if (eventName == eventEntry + ".title")
            eventName = loc.Event(eventEntry);

        // Resolve event description, suppress if key not found
        string? eventDesc = null;
        if (localEvent.Description != null)
        {
            var d = loc.Bilingual(localEvent.Description.LocTable, localEvent.Description.LocEntryKey);
            if (d != localEvent.Description.LocEntryKey)
                eventDesc = d;
        }

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "event_choice",
            ["context"] = RunContext(runState, loc),
            ["event_name"] = eventName,
            ["description"] = eventDesc,
            ["options"] = options,
            ["player"] = PlayerSummary(runState.Players[0], loc),
        };
    }

    // <- RunSimulator.RestSiteState() from `var optionList`
    public static Dictionary<string, object?> RestSite(IReadOnlyList<RestSiteOption> options, RunState runState, LocLookup loc)
    {
        var player = runState.Players[0];

        var optionList = options.Select((opt, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["option_id"] = opt.OptionId,
            ["name"] = opt.GetType().Name,
            ["is_enabled"] = opt.IsEnabled,
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "rest_site",
            ["context"] = RunContext(runState, loc),
            ["options"] = optionList,
            ["player"] = PlayerSummary(player, loc),
        };
    }

    // <- RunSimulator.ShopState() from `var cards = inv.CharacterCardEntries`
    public static Dictionary<string, object?> Shop(MerchantInventory inv, Player player, RunState runState, LocLookup loc)
    {
        var cards = inv.CharacterCardEntries.Concat(inv.ColorlessCardEntries)
            .Select((e, i) =>
            {
                var card = e.CreationResult?.Card;
                var entry = card?.Id.Entry ?? "?";
                var stats = new Dictionary<string, object?>();
                int cardCost = 0;
                try
                {
                    if (card != null)
                    {
                        cardCost = card.EnergyCost?.GetResolved() ?? 0;
                        // The shop entry's card can have uninitialized DynamicVars (stats: null
                        // while after_upgrade is populated, #68). Read base stats from a fresh
                        // ModelDb clone at the card's current upgrade level, like GetUpgradedInfo.
                        var fresh = ModelDb.GetById<CardModel>(card.Id).ToMutable();
                        for (int u = 0; u < card.CurrentUpgradeLevel; u++)
                        {
                            fresh.UpgradeInternal();
                            fresh.FinalizeUpgradeInternal();
                        }
                        foreach (var dv in fresh.DynamicVars.Values)
                            stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue;
                    }
                }
                catch { }
                var shopkws = card?.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["index"] = i,
                    ["name"] = loc.Card(entry),
                    ["type"] = card?.Type.ToString() ?? "?",
                    ["rarity"] = card?.Rarity.ToString() ?? "?",
                    ["card_cost"] = cardCost,
                    ["description"] = loc.Bilingual("cards", entry + ".description"),
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = shopkws?.Count > 0 ? shopkws : null,
                    ["after_upgrade"] = card != null ? UpgradedInfo(card, loc) : null,
                    ["cost"] = e.Cost,
                    ["is_stocked"] = e.IsStocked,
                    ["on_sale"] = e.IsOnSale,
                };
            }).ToList();

        var relics = inv.RelicEntries.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["name"] = loc.Relic(e.Model?.Id.Entry ?? "?"),
            ["description"] = loc.Bilingual("relics", (e.Model?.Id.Entry ?? "?") + ".description"),
            ["cost"] = e.Cost,
            ["is_stocked"] = e.IsStocked,
        }).ToList();

        var potions = inv.PotionEntries.Select((e, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["name"] = loc.Potion(e.Model?.Id.Entry ?? "?"),
            ["description"] = loc.Bilingual("potions", (e.Model?.Id.Entry ?? "?") + ".description"),
            ["cost"] = e.Cost,
            ["is_stocked"] = e.IsStocked,
        }).ToList();

        var removal = inv.CardRemovalEntry;

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "shop",
            ["context"] = RunContext(runState, loc),
            ["cards"] = cards,
            ["relics"] = relics,
            ["potions"] = potions,
            ["card_removal_cost"] = removal?.Cost,
            ["player"] = PlayerSummary(player, loc),
        };
    }

    // <- RunSimulator.CardRewardState() from `var cards = _pendingCardReward.Cards.Select`
    public static Dictionary<string, object?> CardReward(IEnumerable<CardModel> cards, bool canSkip, int? goldEarned, RunState runState, LocLookup loc)
    {
        var cardDicts = cards.Select((c, i) =>
        {
            var stats = new Dictionary<string, object?>();
            try { foreach (var dv in c.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
            var crkws = c.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
            return new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = c.Id.ToString(),
                ["name"] = loc.Card(c.Id.Entry),
                ["cost"] = c.EnergyCost?.GetResolved() ?? 0,
                ["type"] = c.Type.ToString(),
                ["rarity"] = c.Rarity.ToString(),
                ["description"] = loc.Bilingual("cards", c.Id.Entry + ".description"),
                ["stats"] = stats.Count > 0 ? stats : null,
                ["keywords"] = crkws?.Count > 0 ? crkws : null,
                ["after_upgrade"] = UpgradedInfo(c, loc),
            };
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "card_reward",
            ["context"] = RunContext(runState, loc),
            ["cards"] = cardDicts,
            ["can_skip"] = canSkip,
            ["gold_earned"] = goldEarned,
            ["player"] = PlayerSummary(runState.Players[0], loc),
        };
    }

    // <- RunSimulator.DetectDecisionPoint() `if (_cardSelector.HasPendingReward)` block
    public static Dictionary<string, object?> EventCardReward(IReadOnlyList<CardCreationResult> rewardCards, RunState runState, LocLookup loc)
    {
        var cards = rewardCards.Select((cr, i) =>
        {
            var stats = new Dictionary<string, object?>();
            try { foreach (var dv in cr.Card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
            var rrkws = cr.Card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
            return new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = cr.Card.Id.ToString(),
                ["name"] = loc.Card(cr.Card.Id.Entry),
                ["cost"] = cr.Card.EnergyCost?.GetResolved() ?? 0,
                ["type"] = cr.Card.Type.ToString(),
                ["rarity"] = cr.Card.Rarity.ToString(),
                ["description"] = loc.Bilingual("cards", cr.Card.Id.Entry + ".description"),
                ["stats"] = stats.Count > 0 ? stats : null,
                ["keywords"] = rrkws?.Count > 0 ? rrkws : null,
                ["after_upgrade"] = UpgradedInfo(cr.Card, loc),
            };
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "card_reward",
            ["context"] = RunContext(runState, loc),
            ["cards"] = cards,
            ["can_skip"] = true,
            ["from_event"] = true,
            ["player"] = PlayerSummary(runState.Players[0], loc),
        };
    }

    // <- RunSimulator.DetectDecisionPoint() `if (_cardSelector.HasPending && ...)` block
    public static Dictionary<string, object?> CardSelect(IReadOnlyList<CardModel> options, int minSelect, int maxSelect, Player player, RunState runState, LocLookup loc)
    {
        var opts = options.Select((card, i) =>
        {
            var stats = new Dictionary<string, object?>();
            try { foreach (var dv in card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
            var selkws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
            return new Dictionary<string, object?>
            {
                ["index"] = i,
                ["id"] = card.Id.ToString(),
                ["name"] = loc.Card(card.Id.Entry),
                ["cost"] = card.EnergyCost?.GetResolved() ?? 0,
                ["type"] = card.Type.ToString(),
                ["rarity"] = card.Rarity.ToString(),
                ["upgraded"] = card.IsUpgraded,
                ["stats"] = stats.Count > 0 ? stats : null,
                ["description"] = loc.Bilingual("cards", card.Id.Entry + ".description"),
                ["keywords"] = selkws?.Count > 0 ? selkws : null,
                ["after_upgrade"] = UpgradedInfo(card, loc),
            };
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "card_select",
            ["context"] = RunContext(runState, loc),
            ["cards"] = opts,
            ["min_select"] = minSelect,
            ["max_select"] = maxSelect,
            ["player"] = PlayerSummary(player, loc),
        };
    }

    // <- RunSimulator.DetectDecisionPoint() bundle block
    public static Dictionary<string, object?> BundleSelect(IReadOnlyList<IReadOnlyList<CardModel>> bundles, Player player, RunState runState, LocLookup loc)
    {
        var bundleDicts = bundles.Select((bundle, i) => new Dictionary<string, object?>
        {
            ["index"] = i,
            ["cards"] = bundle.Select(card =>
            {
                var stats = new Dictionary<string, object?>();
                try { foreach (var dv in card.DynamicVars.Values) stats[dv.Name.ToLowerInvariant()] = (int)dv.BaseValue; } catch { }
                var bkws = card.Keywords?.Where(k => k != CardKeyword.None).Select(k => k.ToString()).ToList();
                return new Dictionary<string, object?>
                {
                    ["name"] = loc.Card(card.Id.Entry),
                    ["cost"] = card.EnergyCost?.GetResolved() ?? 0,
                    ["type"] = card.Type.ToString(),
                    ["rarity"] = card.Rarity.ToString(),
                    ["description"] = loc.Bilingual("cards", card.Id.Entry + ".description"),
                    ["stats"] = stats.Count > 0 ? stats : null,
                    ["keywords"] = bkws?.Count > 0 ? bkws : null,
                };
            }).ToList(),
        }).ToList();

        return new Dictionary<string, object?>
        {
            ["type"] = "decision",
            ["decision"] = "bundle_select",
            ["context"] = RunContext(runState, loc),
            ["bundles"] = bundleDicts,
            ["player"] = PlayerSummary(player, loc),
        };
    }
}
