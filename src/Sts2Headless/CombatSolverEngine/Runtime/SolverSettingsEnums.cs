namespace CombatSolver;

// Hand-extracted from Runtime/SolverSettings.cs (upstream) -- only these two enums are needed;
// the rest of that file pulls in real Godot for mod-settings persistence, which this headless
// build doesn't want. If upstream changes these enums, re-sync by hand.

internal enum SolverPotionPolicy
{
    Disabled,
    Smart,
    RequireAtLeastOne,
}

internal enum BossHpStrategy
{
    ProgressionFirst,
    MinimizeHpLoss,
}
