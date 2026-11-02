// src/Sts2Advisor.Core/CampaignRotation.cs
namespace Sts2Advisor.Core;

/// <summary>campaign.json's "rotation": the entry after the one whose character matches the run that just ended,
/// cycling. A function of the character alone (no counter), so a saved run that is continued, a game restart or an
/// edit of the list never loses the place. A character that is not in the list -- or no run ended yet -- starts at the
/// first entry. A character that appears twice matches its first entry, so list each character once.</summary>
public static class CampaignRotation
{
    /// <summary>Null when there is no rotation (null or empty).</summary>
    public static CampaignTarget? Next(IReadOnlyList<CampaignTarget>? rotation, string? lastRunCharacter)
    {
        if (rotation == null || rotation.Count == 0)
            return null;
        if (string.IsNullOrEmpty(lastRunCharacter))
            return rotation[0];
        for (int i = 0; i < rotation.Count; i++)
        {
            if (string.Equals(rotation[i].Character, lastRunCharacter, StringComparison.OrdinalIgnoreCase))
                return rotation[(i + 1) % rotation.Count];
        }
        return rotation[0];
    }
}
