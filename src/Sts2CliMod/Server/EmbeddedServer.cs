using System.Net;
using System.Text.Json;
using Sts2HeadlessCore.Core;

namespace Sts2CliMod.Server;

public class EmbeddedServer
{
    private readonly int _port;
    private HttpListener? _listener;
    private readonly List<WebSocketConnection> _clients = new();
    private readonly InputLock _inputLock = new();
    private readonly CancellationTokenSource _cts = new();

    public bool IsRunning { get; private set; }
    public int ConnectedClients => _clients.Count;
    private RunSimulator? _simulator;

    public void SetSimulator(RunSimulator simulator) => _simulator = simulator;
    public RunSimulator? GetSimulator() => _simulator;

    public EmbeddedServer(int port = 12580) => _port = port;

    public void Start()
    {
        if (IsRunning) return;
        try
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{_port}/");
            _listener.Start();
            IsRunning = true;
            MainFile.Logger.Info($"EmbeddedServer started on port {_port}");
            Task.Run(AcceptConnectionsAsync);
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Failed to start: {ex.Message}");
        }
    }

    public void Stop()
    {
        if (!IsRunning) return;
        _cts.Cancel();
        _listener?.Stop();
        _listener?.Close();
        foreach (var client in _clients) client.Disconnect();
        _clients.Clear();
        IsRunning = false;
    }

    private async Task AcceptConnectionsAsync()
    {
        while (!_cts.IsCancellationRequested && _listener?.IsListening == true)
        {
            try
            {
                var context = await _listener.GetContextAsync();
                _ = Task.Run(() => HandleConnectionAsync(context));
            }
            catch (Exception ex)
            {
                if (!_cts.IsCancellationRequested)
                    MainFile.Logger.Error($"Accept error: {ex.Message}");
            }
        }
    }

    private async Task HandleConnectionAsync(HttpListenerContext context)
    {
        try
        {
            var request = context.Request;
            var response = context.Response;

            if (request.Url?.PathAndQuery == "/health")
            {
                await WriteJsonResponse(response, new { status = "ok", mod = "Sts2CliMod", version = "0.1.0" });
                return;
            }
            if (request.Url?.PathAndQuery == "/api/command")
            {
                await HandleCommand(request, response);
                return;
            }
            response.StatusCode = 404;
            response.Close();
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Handle error: {ex.Message}");
        }
    }

    private async Task HandleCommand(HttpListenerRequest request, HttpListenerResponse response)
    {
        if (request.HttpMethod != "POST") { response.StatusCode = 405; response.Close(); return; }
        if (!_inputLock.TryAcquire(InputLock.InputSource.CLI))
        {
            await WriteJsonResponse(response, new { type = "error", message = "Input locked" });
            return;
        }
        try
        {
            string body;
            using (var reader = new StreamReader(request.InputStream))
                body = await reader.ReadToEndAsync();
            var cmdData = JsonSerializer.Deserialize<JsonElement>(body);
            var cmd = cmdData.GetProperty("cmd").GetString() ?? "";
            MainFile.Logger.Info($"Command: {cmd}");
            await WriteJsonResponse(response, new { type = "ack", command = cmd, status = "not_implemented" });
        }
        catch (Exception ex)
        {
            await WriteJsonResponse(response, new { type = "error", message = ex.Message }, 500);
        }
        finally
        {
            _inputLock.Release(InputLock.InputSource.CLI);
        }
    }

    private async Task WriteJsonResponse(HttpListenerResponse response, object data, int statusCode = 200)
    {
        response.StatusCode = statusCode;
        response.ContentType = "application/json";
        response.Headers.Add("Access-Control-Allow-Origin", "*");
        var json = JsonSerializer.Serialize(data);
        var buffer = System.Text.Encoding.UTF8.GetBytes(json);
        await response.OutputStream.WriteAsync(buffer, 0, buffer.Length);
        response.Close();
    }

    public void BroadcastDecisionPoint(Dictionary<string, object?> data)
        => MainFile.Logger.Debug($"Decision: {data.GetValueOrDefault("type")}");
    public void BroadcastStateUpdate(Dictionary<string, object?> data)
        => MainFile.Logger.Debug($"State: {data.Count} fields");
    public Dictionary<string, object?>? GetCurrentState() => null;
}

internal class WebSocketConnection
{
    public void Disconnect() { }
}
