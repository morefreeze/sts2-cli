using System.Text;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>agent/advisor_server.py on 127.0.0.1. 2 s timeout per request.</summary>
public sealed class SidecarAdvisor : IAdvisor, IDisposable
{
    private readonly HttpClient _http;

    public SidecarAdvisor(int port, HttpMessageHandler? handler = null, TimeSpan? timeout = null)
    {
        _http = handler == null ? new HttpClient() : new HttpClient(handler);
        _http.BaseAddress = new Uri($"http://127.0.0.1:{port}/");
        _http.Timeout = timeout ?? TimeSpan.FromSeconds(2);
    }

    public async Task<AdviceReply> AdviseAsync(string decisionJson, CancellationToken ct)
    {
        using var content = new StringContent(decisionJson, Encoding.UTF8, "application/json");
        using var response = await _http.PostAsync("advise", content, ct).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return AdviceReply.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false));
    }

    public async Task<bool> IsHealthyAsync(CancellationToken ct)
    {
        try
        {
            using var response = await _http.GetAsync("health", ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return false;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false));
            return doc.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            return false;
        }
    }

    public void Dispose() => _http.Dispose();
}
