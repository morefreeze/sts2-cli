namespace Sts2Advisor.Core;

/// <summary>The advice service answered, but the reply is not what <see cref="AdviceReply.Parse"/>
/// expects (invalid JSON, a missing or wrongly typed field, a non-object root). That is a version
/// mismatch or a different program on the port, not an offline service, so callers show it
/// differently from a connection failure. <see cref="Path"/> names the field ("options[0].kind",
/// "$" for the root); InnerException is the original error when there was one.</summary>
public sealed class AdviceFormatException : Exception
{
    public AdviceFormatException(string path, string problem, Exception? inner = null)
        : base($"malformed advice reply at {path}: {problem}", inner)
    {
        Path = path;
    }

    public string Path { get; }
}
