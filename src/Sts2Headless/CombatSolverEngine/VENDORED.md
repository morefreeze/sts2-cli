# Vendored from Combat Solver

Source: https://github.com/Torch1230/CombatSolver (MIT license)
Vendored: 2026-09-17, from `main` branch, commit at clone time.
Scope: Engine/, Search/, Prediction/, Strategy/, and Runtime/ (14 files,
vendored verbatim/unmodified):
  - CombatRootSnapshot.cs
  - SolverProgress.cs
  - BattleDamageTracker.cs
  - ContinuationStamp.cs
  - LiveCombatStamp.cs
  - SearchGcLifecycleMetrics.cs
  - SearchMemoryPressureSignal.cs
  - SolverDisplayNames.cs
  - SolverControllerSessions.cs
  - CombatBugReportDescription.cs
  - SimulationNotificationIsolation.cs
  - SolverDiagnostics.cs
  - PowerDynamicVarWarmup.cs
  - CardDynamicVarWarmup.cs

Original scope (Runtime/CombatRootSnapshot.cs only) turned out to be
incomplete: Task 3's compile pass found Engine/Search/Prediction transitively
depend on many more Runtime-only types (SolverProgress, BattleDamageSnapshot,
ContinuationStamp, LiveCombatStamp, SearchGcLifecycleAttribution/Snapshot,
SearchMemoryPressureSignal, SolverDisplayNames, SolverInterimResult,
SearchInteractionState, SolverPotionPolicy, BossHpStrategy,
SearchProgressDisplayState, ReplanCause, CombatBugReportIssueLedger, and the
Entry.Logger/SimulationNotificationIsolation/PowerDynamicVarWarmup/
CardDynamicVarWarmup call sites below). The 13 files above (beyond the
original CombatRootSnapshot.cs) were vendored unmodified to supply them, each
confirmed clean of Godot/RitsuLib coupling before vendoring.

Four deliberate exceptions to "verbatim file copy" in this directory:

- `SolverSettingsEnums.cs` hand-extracts only the `SolverPotionPolicy` and
  `BossHpStrategy` enum declarations out of upstream `Runtime/SolverSettings.cs`.
  That file's remaining ~700 lines implement mod-settings JSON persistence and
  pull in real `Godot` (not GodotStubs), which this headless build does not
  want and does not otherwise need. If upstream changes either enum, re-sync
  by hand.

- `SolverControllerEnums.cs` hand-extracts only the `ReplanCause` enum
  declaration out of upstream `Runtime/SolverController.cs`. That file
  (3400+ lines) is the mod's top-level Godot-facing controller (settings,
  overlay UI, save/load, multiplayer, RitsuLib patch registration) — the
  canonical "mod loader glue" this port excludes. If upstream changes this
  enum, re-sync by hand.

- `PowerDynamicVarMaterializationGuardPatch.cs` is a **5th RitsuLib
  touchpoint** (the original triage in Task 3's brief only enumerated 4; this
  one only became visible once the Runtime files above were vendored and the
  build reached this file). Upstream implements it as an `IPatchMethod`
  (`STS2RitsuLib.Patching.Models`) that RitsuLib's patcher installs as a real
  Harmony patch via `Entry.cs`. This build never runs `Entry.Initialize()`, so
  the patch is never installed; the only consumer in this tree
  (`Engine/Common/NativeModelCloneConcurrency.HasDefaultPowerInitialization`)
  only takes `AccessTools.Method(typeof(...), "Prefix")` to compare against
  `Harmony.GetPatchInfo(...)`, which is always empty here regardless of this
  class's contents. The vendored copy drops the `IPatchMethod` interface and
  its RitsuLib-only members (`PatchId`, `Description`, `GetTargets()`) and
  keeps only the same-signature `Prefix` method, preserving that outcome
  exactly. See the comment at the top of the file for the full reasoning.

- `EntryLoggingShim.cs` is **hand-written, not vendored from any single
  upstream file**. It stands in for the small used surface of upstream
  `Runtime/Entry.cs` (`Entry.ModId`, `Entry.Logger.{Info,Warn,Debug,Error}`).
  `Entry.cs` itself is the mod's `[ModInitializer]` — Godot scene-tree wiring,
  the full RitsuLib patcher, and the SolverController/UI lifecycle — none of
  which this headless build runs. Its `Logger` further chains into
  `Runtime/CombatSolverLog.cs` → `Runtime/CombatDiagnosticJournal.cs` (both
  unvendored) and `src/Diagnostics/PerformanceRecording.cs` (a whole
  top-level directory this port excludes). Every `Entry.Logger` call site in
  this vendored tree is diagnostics-only (never control flow); the shim's
  logger methods are no-ops, since writing to Console would corrupt this
  build's JSON stdin/stdout protocol and there is no vendored file-logging
  destination to write to instead. `Entry.ModId` is different -- it's
  compared against a patch's owning mod id in two files to gate an
  exception/foreign-patch path, which is currently unreachable here (no
  Harmony patches are ever installed) but is control flow, not diagnostics;
  the shim keeps the real constant value ("CombatSolver") so that stays
  correct if this ever changes. See the comment at the top of the file for
  the full reasoning.

NOT vendored: Runtime/ (remainder, including SolverSettings.cs and
SolverController.cs as wholes, and Entry.cs), UI/, Api/, Diagnostics/,
Replay/, Testing/ — these are the live-mod / RitsuLib / overlay glue this
headless integration does not need. See
docs/superpowers/specs/2026-09-17-combatsolver-port-design.md.

Do not hand-edit ported files' algorithm logic. If upstream fixes a bug we
need, re-vendor the affected file(s) from a fresh clone instead of patching
by hand, so we don't silently diverge from upstream. The four exceptions
above (two hand-extracted enum files, one RitsuLib-touchpoint file with its
mod-patcher interface stripped, and one hand-written logging shim) are the
only deliberate departures from "verbatim file copy" in this directory.
