namespace Sts2Advisor.Core;

/// <summary>Where advice comes from. SidecarAdvisor (local Python service) now; a C# port of
/// greedy_action later (spec "不做"), checked against the same corpus.</summary>
public interface IAdvisor
{
    /// <summary>Advice for one decision JSON. Throws when the source is unreachable or answers badly.</summary>
    Task<AdviceReply> AdviseAsync(string decisionJson, CancellationToken ct);

    /// <summary>Which held potion to discard for an offered one when the belt is full (POST /potion_swap). The body
    /// is {"state": {"player": {"potions", "potion_slots"}}, "offered": [potion dicts]}. Throws like
    /// <see cref="AdviseAsync"/>.</summary>
    Task<PotionSwapReply> PotionSwapAsync(string requestJson, CancellationToken ct);

    Task<bool> IsHealthyAsync(CancellationToken ct);
}
