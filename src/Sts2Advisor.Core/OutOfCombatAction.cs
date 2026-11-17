namespace Sts2Advisor.Core;

/// <summary>Orders optional potion use against the action the autopilot would otherwise take.</summary>
public sealed record OutOfCombatAction(string Name, int? PotionIndex, int Priority)
{
    public static IReadOnlyList<OutOfCombatAction> Rank(string pendingAction, IReadOnlyList<string> usablePotionIds)
    {
        var candidates = new List<OutOfCombatAction> { new(pendingAction, null, 0) };
        for (int i = 0; i < usablePotionIds.Count; i++)
        {
            // Fruit Juice permanently adds max HP and frees a slot. Unknown effects stay in the belt.
            int priority = usablePotionIds[i] == "POTION.FRUIT_JUICE" ? 1 : -1;
            candidates.Add(new OutOfCombatAction("use_potion", i, priority));
        }
        return candidates.OrderByDescending(action => action.Priority).ToList();
    }
}
