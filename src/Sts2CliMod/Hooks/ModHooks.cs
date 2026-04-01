using Godot;
using Sts2CliMod.Server;

namespace Sts2CliMod.Hooks;

/// <summary>
/// Game event hooks that broadcast to the embedded server.
/// TODO: Extend AbstractModel when game types are available.
/// </summary>
public class ModHooks
{
    private static EmbeddedServer? _server;

    public static void SetServer(EmbeddedServer server) => _server = server;

    public static void OnBeforePlayerChoice(object choice)
    {
        MainFile.Logger.Info($"[BeforePlayerChoice] {choice.GetType().Name}");
        _server?.BroadcastDecisionPoint(new Dictionary<string, object?>
        {
            { "type", "player_choice" },
            { "choice_type", choice.GetType().Name },
            { "timestamp", DateTime.UtcNow.ToString("o") }
        });
    }

    public static void OnAfterCardPlayed(object card, object combat, object target)
    {
        MainFile.Logger.Info($"[AfterCardPlayed] Card played");
        _server?.BroadcastStateUpdate(new Dictionary<string, object?>
        {
            { "type", "card_played" },
            { "timestamp", DateTime.UtcNow.ToString("o") }
        });
    }

    public static void OnAfterTurnEnd(object combat)
    {
        MainFile.Logger.Info($"[AfterTurnEnd]");
        _server?.BroadcastStateUpdate(new Dictionary<string, object?>
        {
            { "type", "turn_end" },
            { "timestamp", DateTime.UtcNow.ToString("o") }
        });
    }

    public static void OnAfterRoomEntered(object room)
    {
        MainFile.Logger.Info($"[AfterRoomEntered]");
        _server?.BroadcastDecisionPoint(new Dictionary<string, object?>
        {
            { "type", "room_entered" },
            { "timestamp", DateTime.UtcNow.ToString("o") }
        });
    }

    public static void OnAfterCombatEnd(object combat, bool playerWon)
    {
        MainFile.Logger.Info($"[AfterCombatEnd] Won: {playerWon}");
        _server?.BroadcastStateUpdate(new Dictionary<string, object?>
        {
            { "type", "combat_end" },
            { "won", playerWon },
            { "timestamp", DateTime.UtcNow.ToString("o") }
        });
    }
}
