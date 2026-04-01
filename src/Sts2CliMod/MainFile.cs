using Godot;
using MegaCrit.Sts2.Core.Modding;
using Sts2CliMod.Server;

namespace Sts2CliMod;

[ModInitializer(nameof(Initialize))]
public partial class MainFile : Node
{
    public const string ModId = "Sts2CliMod";
    public const int DefaultPort = 12580;

    public static MegaCrit.Sts2.Core.Logging.Logger Logger { get; } = new(
        ModId,
        MegaCrit.Sts2.Core.Logging.LogType.Generic
    );

    private static EmbeddedServer? _server;

    public static void Initialize()
    {
        Logger.Info("Sts2CliMod initializing...");
        _server = new EmbeddedServer(DefaultPort);
        _server.Start();
        Logger.Info($"Sts2CliMod initialized. Version 0.1.0");
    }

    public override void _ExitTree()
    {
        Logger.Info("Sts2CliMod shutting down...");
        _server?.Stop();
    }
}
