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

Five deliberate exceptions to "verbatim file copy" in this directory:

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

- `RitsuLibHarmonyIlShim.cs` is **hand-written, not vendored from any single
  upstream file** (2026-09-30, agent/bug.md BUG-048). It stands in for the small
  used surface of RitsuLib's `STS2RitsuLib.Utils.HarmonyIl` -- the extension
  `MethodInfo.GetOriginalIl()`, `HarmonyIlMethodBody.Instructions`, and
  `HarmonyIl.{TryGetCalledMethod,TryGetLocalLoadIndex,LoadsInt32}` -- so that
  `Engine/InCombat/Mirrors/Cards/OnPlay/CardOnPlayInferrer.cs` can be vendored
  **verbatim** again (its `using STS2RitsuLib.Utils.HarmonyIl;` compiles
  unchanged because the shim declares exactly that namespace). RitsuLib is a Steam
  Workshop mod library this headless build does not have. The port (09311aa) had
  instead stubbed the inferrer's `Infer`/`InferStrict` to `=> null`, which silently
  removed the generic Attack/Block/Draw simulation of every card without a
  hand-written mirror (~486 card classes; BUG-048). The shim reads the ORIGINAL
  (unpatched) IL with `HarmonyLib.PatchProcessor.GetOriginalInstructions` -- our
  build applies Harmony patches to some game methods -- and, because card `OnPlay`
  overrides are `async`, reads the compiler-generated state machine's `MoveNext`
  (found through `[AsyncStateMachine]`), which is the IL the inferrer's heuristics
  were written against (it ignores `stfld` to the state-machine type, skips branches
  preceded by `get_IsCompleted`, and reads "local 0 near the start of MoveNext" as
  the async state switch). `TryGetCalledMethod` reports only `call`/`callvirt` with
  a `MethodInfo` operand (constructor calls carry a `ConstructorInfo`). The shim
  contains no algorithm logic: it only decodes `CodeInstruction`s; every decision
  about what a card does stays in the verbatim inferrer. It also carries the
  `STS2_SOLVER_INFERRER` on/off switch (`InferrerSwitch`, read once per process):
  `off` makes `GetOriginalIl` throw, which the inferrer catches and turns into
  `return null` -- exactly the old stub -- so a paired A/B needs no edit to any
  vendored file (still no algorithm logic). If upstream's inferrer
  starts using more of RitsuLib, extend the shim by hand.

NOT vendored: Runtime/ (remainder, including SolverSettings.cs and
SolverController.cs as wholes, and Entry.cs), UI/, Api/, Diagnostics/,
Replay/, Testing/ — these are the live-mod / RitsuLib / overlay glue this
headless integration does not need. See
docs/superpowers/specs/2026-09-17-combatsolver-port-design.md.

Do not hand-edit ported files' algorithm logic. If upstream fixes a bug we
need, re-vendor the affected file(s) from a fresh clone instead of patching
by hand, so we don't silently diverge from upstream. The five exceptions
above (two hand-extracted enum files, one RitsuLib-touchpoint file with its
mod-patcher interface stripped, and two hand-written shims -- logging and the
HarmonyIl IL reader) are the only deliberate departures from "verbatim file
copy" in this directory, apart from the local patches listed below.

## Local patches to vendored algorithm files

Two deliberate, user-approved departures from "never hand-edit ported
algorithm logic" (both 2026-09-30). (A third, a Scourge draw in
`CardEffectSpecRegistry.cs`, was added and then removed the same week with
the user's approval: once BUG-048 restored the IL inferrer, the inferrer
simulates Scourge's draw itself, so the patch would have drawn twice. That
file is verbatim upstream again.) Each is marked `LOCAL PATCH` in the source
and must be re-applied (or dropped, if upstream fixed it) on any re-vendor.

- `Engine/InCombat/Simulation/CombatPredictionSimulator.CardPile.cs` --
  `MaxSimulatedCardDraws = 1000`: `ContinueDrawExecution` stops drawing once
  one simulated line has drawn 1000 cards and records
  `PredictionRiskReason.CardDrawLimitExceeded` (an existing upstream enum value
  that upstream never raised; the count is upstream's own O(1)
  `CombatPredictionCardDrawnEntry` counter). Without it a search worker spins
  forever on a loop the real game also never exits -- Pillage's "draw until a
  non-Attack" while Hellraiser auto-plays each drawn Strike and Ringing vetoes
  the auto-play (agent/bug.md BUG-040). Same shape as upstream's own
  `MaxSimulatedChanneledOrbs` / `OrbChannelLimitExceeded` cap in
  `CombatPredictionSimulator.Orb.cs`. It only changes lines that draw 1000+
  cards, i.e. lines that are already non-terminating in the real game.

- `Search/CombatBeamSolver.BeamRetentionPolicy.cs` -- `appliedAdmissionClaims`
  (in `AddOrderedMutationPortfolio`) is now `new(ReferenceEqualityComparer.Instance)`
  instead of `[]`. `OrderedMutationAdmissionClaim` is a record holding
  `SearchNode Candidate` (and a `Packet` holding SearchNodes), so the default comparer
  hashed it with the compiler-generated record `GetHashCode`, which walks
  `SearchNode.Parent` chains and every `CycleSearchState.PriorCycleEndpoint` chain
  without memoization -- exponential in the number of cycle endpoints on a line, so a
  long repeating combo line (Creative AI / Reboot loops) hangs `plan_combat_turn`
  deterministically (agent/bug.md BUG-047). Every other `SearchNode` set in the
  search code already uses reference equality; claims are only ever re-added as the
  same object (built once by `CoalesceOrderedMutationAdmissionClaims`, then reached
  via `admissionClaims`, `work.Claim` and `work.AliasedClaims`) and every claim owns a
  distinct `Reasons` set instance, so reference semantics cannot change which claims
  count as already applied. Upstream (Torch1230/CombatSolver @ 7236330) has the same
  code at `src/Search/CombatBeamSolver.BeamRetentionPolicy.OrderedMutation.cs:485`.
