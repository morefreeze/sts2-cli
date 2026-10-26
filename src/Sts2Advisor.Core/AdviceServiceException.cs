namespace Sts2Advisor.Core;

/// <summary>The advice service was reached and refused the request (any non-2xx status, usually
/// 400 / 413 / 500 with an {"error": "..."} body). Unlike HttpRequestException it is not derived
/// from it: a caller that treats HttpRequestException as "offline, reconnect" must not catch this.</summary>
public sealed class AdviceServiceException : Exception
{
    public AdviceServiceException(int statusCode, string errorText)
        : base($"advice service answered HTTP {statusCode}: {errorText}")
    {
        StatusCode = statusCode;
        ErrorText = errorText;
    }

    public int StatusCode { get; }

    /// <summary>The body's "error" string, else the (truncated) raw body, else the status reason. Never empty.</summary>
    public string ErrorText { get; }
}
