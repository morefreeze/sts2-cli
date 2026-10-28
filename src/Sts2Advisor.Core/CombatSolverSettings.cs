// src/Sts2Advisor.Core/CombatSolverSettings.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

public enum CombatSolverStatus { Ok, Missing, FullAutoOff, Unreadable }

/// <summary>Whether the CombatSolver Workshop mod will play combats by itself: its settings file
/// (&lt;game user dir&gt;/combat_solver_settings.json) has "autoEnableFullAuto": true and not
/// "solverDisabled": true. The autopilot only warns when it is not; it never writes the file.</summary>
public static class CombatSolverSettings
{
    public const string FileName = "combat_solver_settings.json";

    public static CombatSolverStatus Read(string path)
    {
        try
        {
            if (!File.Exists(path))
                return CombatSolverStatus.Missing;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return CombatSolverStatus.Unreadable;
            bool disabled = root.TryGetProperty("solverDisabled", out var d) && d.ValueKind == JsonValueKind.True;
            bool fullAuto = root.TryGetProperty("autoEnableFullAuto", out var a) && a.ValueKind == JsonValueKind.True;
            return fullAuto && !disabled ? CombatSolverStatus.Ok : CombatSolverStatus.FullAutoOff;
        }
        catch (Exception)
        {
            return CombatSolverStatus.Unreadable;
        }
    }
}
