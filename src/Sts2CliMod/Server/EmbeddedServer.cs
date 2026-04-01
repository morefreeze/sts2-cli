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
    private Dictionary<string, object?>? _currentState;

    public void SetSimulator(RunSimulator simulator) => _simulator = simulator;
    public RunSimulator? GetSimulator() => _simulator;
    public void SetCurrentState(Dictionary<string, object?> state) => _currentState = state;

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
            if (request.Url?.PathAndQuery == "/state")
            {
                await HandleGetState(response);
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

            var result = ExecuteCommand(cmd, cmdData);
            await WriteJsonResponse(response, result);
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

    private object ExecuteCommand(string cmd, JsonElement cmdData)
    {
        if (_simulator == null)
        {
            return new { type = "error", message = "No simulator instance" };
        }

        try
        {
            return cmd switch
            {
                "start_run" => ExecuteStartRun(cmdData),
                "get_map" => _simulator.GetFullMap(),
                "get_state" => GetCurrentState() ?? new Dictionary<string, object?> { ["type"] = "error", ["message"] = "No state available" },
                "action" => ExecuteAction(cmdData),
                _ => new { type = "ack", command = cmd, status = "unknown_command" }
            };
        }
        catch (Exception ex)
        {
            MainFile.Logger.Error($"Command execution failed: {ex.Message}");
            return new { type = "error", message = ex.Message };
        }
    }

    private object ExecuteStartRun(JsonElement cmdData)
    {
        var character = cmdData.TryGetProperty("character", out var charEl) ? charEl.GetString() ?? "Ironclad" : "Ironclad";
        var ascension = cmdData.TryGetProperty("ascension", out var ascEl) ? ascEl.GetInt32() : 0;
        var seed = cmdData.TryGetProperty("seed", out var seedEl) ? seedEl.GetString() : null;
        var lang = cmdData.TryGetProperty("lang", out var langEl) ? langEl.GetString() ?? "en" : "en";

        MainFile.Logger.Info($"Starting run: {character} A{ascension} seed={seed}");
        var result = _simulator!.StartRun(character, ascension, seed, lang);
        SetCurrentState(result);
        BroadcastDecisionPoint(result);
        return result;
    }

    private object ExecuteAction(JsonElement cmdData)
    {
        if (!cmdData.TryGetProperty("action", out var actionEl))
        {
            return new { type = "error", message = "Missing 'action' parameter" };
        }

        var action = actionEl.GetString() ?? "";
        Dictionary<string, object?>? args = null;

        if (cmdData.TryGetProperty("args", out var argsEl))
        {
            args = new Dictionary<string, object?>();
            foreach (var prop in argsEl.EnumerateObject())
            {
                args[prop.Name] = prop.Value.ValueKind switch
                {
                    JsonValueKind.String => prop.Value.GetString(),
                    JsonValueKind.Number => prop.Value.GetInt32(),
                    JsonValueKind.True => true,
                    JsonValueKind.False => false,
                    JsonValueKind.Null => null,
                    _ => prop.Value.ToString()
                };
            }
        }

        MainFile.Logger.Info($"Executing action: {action}");
        var result = _simulator!.ExecuteAction(action, args);
        SetCurrentState(result);
        BroadcastDecisionPoint(result);
        return result;
    }

    private async Task HandleGetState(HttpListenerResponse response)
    {
        var state = GetCurrentState();
        if (state == null)
        {
            state = new Dictionary<string, object?>
            {
                ["type"] = "state",
                ["message"] = "No state available"
            };
        }
        await WriteJsonResponse(response, state);
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
    {
        SetCurrentState(data);
        MainFile.Logger.Info($"Decision: {data.GetValueOrDefault("type")}");
        // TODO: Send to WebSocket clients when implemented
    }

    public void BroadcastStateUpdate(Dictionary<string, object?> data)
    {
        MainFile.Logger.Debug($"State: {data.Count} fields");
        // TODO: Send to WebSocket clients when implemented
    }

    public Dictionary<string, object?>? GetCurrentState() => _currentState;
}

internal class WebSocketConnection
{
    public void Disconnect() { }
}
