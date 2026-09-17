namespace CombatSolver;

// Hand-extracted from Runtime/SolverController.cs (upstream) -- only ReplanCause is needed;
// the rest of that 3400+ line file is the mod's top-level Godot-facing controller (settings,
// overlay, save/load, multiplayer, etc.), which this headless build doesn't want. If upstream
// changes this enum, re-sync by hand.

internal enum ReplanCause
{
    InitialSearch,
    StateMismatch,
    ManualDivergence,
    ContinuationMissing,
    DeploymentDrift,
    PlanExhausted,
    ExplicitRequest,
}
