namespace Sts2Advisor.Core;

/// <summary>Cheap identity of a decision screen. The mod asks the advice service again
/// only when this changes (a purchase, a new event page, HP or gold moving).</summary>
public static class Fingerprint
{
    public static string Of(string decision, IEnumerable<object?> parts) =>
        decision + "|" + string.Join("|", parts.Select(p => p?.ToString() ?? "-"));
}
