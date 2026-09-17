namespace CombatSolver;

// Hand-written stand-in for upstream Runtime/Entry.cs -- NOT a vendored file, unlike everything
// else in this directory. Do not replace this with a verbatim copy of Entry.cs.
//
// Upstream Entry.cs is the mod's [ModInitializer] entry point: on load it wires up Godot
// scene-tree signals (CombatManager.Instance.TurnStarted, NGame.TreeExiting, ...), installs the
// entire RitsuLib Harmony patcher (every RegisterPatch<T>() call, including the other 4-5
// touchpoints elsewhere in this port), and owns the SolverController/overlay UI lifecycle. None
// of that exists or runs in this headless build -- it is exactly the "mod loader glue" this port
// deliberately excludes (see VENDORED.md and docs/superpowers/specs/2026-09-17-combatsolver-port-design.md).
//
// Its `Logger` property returns a `CombatSolverLog`, whose real implementation
// (Runtime/CombatSolverLog.cs, not vendored) writes to a Godot user-data-dir file via
// Runtime/CombatDiagnosticJournal.cs (not vendored) and to src/Diagnostics/PerformanceRecording.cs
// (an entire top-level directory this port excludes). Vendoring that chain would pull in real
// Godot file I/O and the excluded Diagnostics/ tree for what is, in every call site actually
// reachable from this vendored engine, a fire-and-forget diagnostic log line -- never a value
// that influences search/prediction control flow (grep the tree for `Entry\.` to confirm: every
// use is `Entry.Logger.Info/Warn(...)` or the `Entry.ModId` constant).
//
// This shim reproduces only that surface. Logging is a no-op: the headless build speaks JSON over
// stdin/stdout (see agent/sts2_bridge.py), so writing arbitrary log lines to Console would corrupt
// that protocol, and there is no vendored file-logging destination to write to instead. If
// CombatSolver's own diagnostics are ever needed, wire this up to whatever logging
// src/Sts2Headless already uses, rather than reintroducing Entry.cs's Godot-based journal.
internal static class Entry
{
    public const string ModId = "CombatSolver";

    public static readonly CombatSolverLog Logger = new();
}

internal sealed class CombatSolverLog
{
    public void Info(string message)
    {
    }

    public void Debug(string message)
    {
    }

    public void Warn(string message)
    {
    }

    public void Error(string message)
    {
    }
}
