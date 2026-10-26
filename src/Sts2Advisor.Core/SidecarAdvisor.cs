using System.Text;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>agent/advisor_server.py on 127.0.0.1. 2 s timeout per request.
/// AdviseAsync throws: HttpRequestException / TaskCanceledException when the service cannot be
/// reached in time (offline), <see cref="AdviceServiceException"/> when it answers with a non-2xx
/// status, <see cref="AdviceFormatException"/> when a 2xx body is not a valid reply.</summary>
public sealed class SidecarAdvisor : IAdvisor, IDisposable
{
    /// <summary>Per-request timeout (a TimeSpan cannot be a const).</summary>
    public static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(2);

    private const int MaxErrorTextLength = 300;

    private readonly HttpClient _http;

    public SidecarAdvisor(int port, HttpMessageHandler? handler = null, TimeSpan? timeout = null)
    {
        _http = handler == null ? new HttpClient() : new HttpClient(handler);
        _http.BaseAddress = new Uri($"http://127.0.0.1:{port}/");
        _http.Timeout = timeout ?? DefaultTimeout;
    }

    /// <summary>The per-request timeout in force.</summary>
    public TimeSpan Timeout => _http.Timeout;

    public async Task<AdviceReply> AdviseAsync(string decisionJson, CancellationToken ct)
    {
        using var content = new StringContent(decisionJson, Encoding.UTF8, "application/json");
        using var response = await _http.PostAsync("advise", content, ct).ConfigureAwait(false);
        var body = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode)
            throw new AdviceServiceException((int)response.StatusCode, ErrorText(body, response));
        return AdviceReply.Parse(body);
    }

    /// <summary>True only for a 2xx {"ok": true}. Any other answer, an unreachable service and a
    /// timeout are false. A cancellation requested through <paramref name="ct"/> is rethrown.</summary>
    public async Task<bool> IsHealthyAsync(CancellationToken ct)
    {
        try
        {
            using var response = await _http.GetAsync("health", ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return false;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false));
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException or IOException)
        {
            return false;
        }
    }

    private static string ErrorText(string body, HttpResponseMessage response)
    {
        var text = body.Trim();
        if (text.Length == 0)
            return string.IsNullOrWhiteSpace(response.ReasonPhrase) ? $"HTTP {(int)response.StatusCode}" : response.ReasonPhrase!;
        try
        {
            using var doc = JsonDocument.Parse(text);
            if (doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(error.GetString()))
                return error.GetString()!;
        }
        catch (JsonException)
        {
            // not JSON: show the raw text below
        }
        return text.Length > MaxErrorTextLength ? text[..MaxErrorTextLength] + "…" : text;
    }

    public void Dispose() => _http.Dispose();
}
