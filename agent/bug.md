# STS2-CLI Bug Tracker

## [FIXED] BUG-045: play_full_run reported the ACT-LOCAL floor to paired_eval and in avg_floor (2026-09-23, fixed 2026-09-23)
- **Decision type**: harness metrics (no gameplay effect)
- **Description**: The engine's game_over `floor` resets to 1 each act. `result_to_eval_row` (`--results-log`) and `summarize()`'s `avg_floor` used it directly, but `agent/paired_eval.py`'s floor metric is the global run floor `(act - 1) * 17 + floor` that `agent/eval_rl.py` writes (`global_floor_from_state`, eval_rl.py:109). A death at act 2 floor 16 scored below one at act 1 floor 17. The bias hit only runs that got past act 1, so in the Phase 2 A/B it fell almost entirely on the solver arm (54 deaths in act 2/3 vs 1 for the heuristic) and read Defect as -0.24 floors (p=0.79) when the true paired gain was +10.50 (p<0.0001).
- **Fix**: `global_floor()` in python/play_full_run.py, used by both `result_to_eval_row` (which also keeps the act-local value as `act_floor`) and `avg_floor`; a test pins it to eval_rl's own function. Commit a6472da. Every `avg_floor` this harness printed before it is only comparable between runs that died in the same act.

## [FIXED] BUG-044: a solver that silently fell back to the heuristic still reported "Completed: N/N" (2026-09-21, fixed 2026-09-21)
- **Decision type**: combat_play (harness diagnostics)
- **Description**: When `plan_combat_turn` returns an error, `play_full_run.py` falls back to its one-card-at-a-time heuristic -- by design -- but printed nothing and counted nothing. A 3-game Silent smoke test (BUG-043) therefore showed `Completed: 3/3, avg_floor=12.0` while all 502 solver calls had failed and the run measured only the heuristic.
- **Fix**: per-run `solver_plans`/`solver_errors` counters, the first engine error message printed once per run, `solver=<plans>/<attempts>` on each SUMMARY line, and a `SOLVER NEVER ENGAGED` banner when a solver-enabled character got zero plans (worded differently when zero calls were made at all). `Completed` accounting deliberately unchanged. Commits 26478ed, 4e046de.

## [FIXED] BUG-043: our own Neutralize.OnPlay Harmony patch made the solver refuse every Silent combat (2026-09-21, fixed 2026-09-21)
- **Decision type**: combat_play (solver path, Silent)
- **Description**: The vendored solver's `PredictionModPatchAudit.CaptureCardOnPlay` (`CombatSolverEngine/Prediction/PredictionModPatchAudit.cs:38-114`) inspects Harmony patches on every in-play card's `OnPlay` and throws `PredictionUnsupportedException` for any it cannot attribute to the base game or a registered mod. RunSimulator.cs patched `Neutralize.OnPlay` (a workaround for a DamageCmd NullRef, predating the v0.111 upgrade), and Neutralize is a Silent starter card -- so all 502 Silent `plan_combat_turn` calls failed with `Unknown Harmony patch ... NeutralizePrefix`.
- **Fix**: verified the NullRef no longer reproduces on v0.111 (Neutralize played 66 times across 3 games, zero exceptions) and deleted the patch plus `NeutralizePrefix`/`NeutralizeSafe` (commit 5e9a1c9). Capture failures 502 -> 0, real plans 0 -> 178. The replacement had also passed `DynamicVars.Damage.BaseValue`, dropping Strength and other modifiers, so this also restored Neutralize's real damage. A comment at the Harmony install site (26478ed) warns that any future patch on a card's `OnPlay` blinds the solver the same way.

## [OPEN] BUG-040: plan_combat_turn can hang forever -- one search worker spins inside a single node expansion, the 120s budget never gets a chance to stop it (2026-09-23)
- **Decision type**: combat_play (solver path, any character)
- **Description**: During the Phase 2 paired A/B, Ironclad seed `run_17` (ON arm, `STS2_SOLVER_THREADS=3`) sent `plan_combat_turn` at 2026-09-22 13:50 and never got a response. 24 h later the Sts2Headless process was at 98% of one core with 1403 min of CPU time. `sample` showed the main thread parked in `Monitor_Wait` (waiting to join the search) while exactly one of the three search workers (`Thread_88856000`) was busy in JIT'd managed code and the other two were idle in `__psynch_cvwait`. The soft time budget (`SolverSearchProfile.Default.SoftTimeBudgetMilliseconds = 120_000`) is only checked between node expansions (`CombatBeamSolver.Phases.cs:1496`, `:1811`; `CombatBeamSolver.NoveltySearch.cs:96`), so a worker that never returns from one expansion is unbounded. The harness has no per-call timeout either (`read_json_line()` blocks on `readline()`), so the lane silently stalled for a day -- 23 of Ironclad's 40 ON games were delayed behind it.
- **State at hang**: act 1 floor 8, Monster, round 3, HP 28/80, energy 3, hand `[Breakthrough, Defend, Defend, Strike, Bash]`, enemies two Nibbits (38 HP intent Attack 14; 44 HP intent Attack 8 + Defend). The previous action was a `select_cards` picking ARMAMENTS. Nothing exotic -- no custom mechanic in play.
- **Second occurrence (2026-09-23 14:37)**: Ironclad `run_32`, same A/B lane, same signature (main thread in `Monitor_Wait`, one busy search worker, last log line an unanswered `plan_combat_turn`). State: act 1 floor 17, **Boss** (Ceremonial Beast, 168 HP), round 3, HP 35/80, energy 3, hand `[Defend, Defend, Iron Wave, Pillage, Headbutt]`. Full dump + sample + log in `~/.sts2-train/hang_ironclad_run32_20260923/`. Different room, enemy and hand from the first hang; both are Ironclad, both round 3, both with two Defends in hand -- two samples is too few to treat that overlap as a lead. Final count once the batch finished: 2 hangs in 40 Ironclad solver games (5%) vs 0 in 160 games across the other four characters (an earlier note here said "~32", which was an interim count taken while the lane was still running). The remaining Ironclad games ran under an external auto-watchdog (kill the engine after 15 min with no log write; a healthy call never exceeds the 120 s budget).
- **Evidence preserved**: `~/.sts2-train/hang_ironclad_run17_20260923/` -- full core dump `core.18567.dmp` (8.4 GB, taken with the runtime's own `createdump --full` while the process was still spinning), the `sample` call graph, and the run's complete game log. Managed frame names need `dotnet-dump analyze` + SOS (`clrstack -all`) on the dump; `sample` alone cannot resolve JIT frames.
- **Workaround applied**: killed the Sts2Headless process; `play_run()` turned the resulting EOF into an `error` result (JSONL status `crash`, excluded from paired_eval pairing) and the lane continued with `run_18`.
- **Reproducible (2026-09-24)**: Ironclad `run_32` hung AGAIN at the same Act 1 boss (A1F17) in the Phase 2b-1 tier A/B, this time at a 30 s budget (`STS2_SOLVER_THREADS=3`, 6 lanes) -- so on that seed it is deterministic, not a race, consistent with the solver's play being deterministic (the Phase 2b-1 regression reproduced 25 Phase 2 games seed-for-seed). `run_17` did NOT hang again in that batch, so it is not a known reproducer; an earlier note here guessed it would be. To reproduce: `STS2_SOLVER_CHARS=Ironclad STS2_SOLVER_THREADS=3` and play Ironclad seed `run_32` to A1F17.
- **Mitigation (2026-09-23, commits ff8e4bb, a61a186)**: `python/play_full_run.py` now drives the engine through `python/engine_process.py`. Every reply has a deadline (`plan_combat_turn`: budget + 60 s; anything else: 120 s); a miss kills the engine's whole process group -- the real engine is a child of `dotnet run`, so killing only the wrapper would orphan it at 100% CPU -- and records the game as `HANG` (paired_eval status `stuck`, excluded from pairing) with act/floor/hp from the last decision. In the 240-game tier A/B it fired exactly once (the `run_32` repro above) and cost one game; in the 25-game regression it never fired. **Still unprotected**: `tests/conftest.py`'s Game fixture and `agent/sts2_bridge.py`.
- **Where it spins (2026-09-24, confirmed)**: reproduced `run_32` with `DOTNET_PerfMapEnabled=1` so the runtime wrote `/tmp/perf-<pid>.map`, then named the JIT frames in three `sample` call graphs taken 10 s apart (scripts `~/.sts2-train/bug040_repro.py` / `bug040_resolve.py`, evidence `~/.sts2-train/bug040_repro_20260924_124628/`). All three show the busy search worker inside `CardDrawCardMirrors.PillageOnPlay`'s `while (true)` draw loop (`CardDrawCardMirrors.cs:167-177`) -> `Draw` -> `AfterCardDrawn` -> `HandleHellraiserPower`, reached via `RunSupplementalAudits` -> `AuditSmartPotionUse` -> `SearchSmartPotionGradient` -> parallel expansion -> `ReplayAction`. The real state at the hang has **Hellraiser** in `player_powers` (played in round 1 of this fight; the power list lives in the decision's top-level `player_powers`, not under `player`). Mechanism: Pillage draws a Strike -> Hellraiser auto-plays it -> it leaves the hand -> Pillage only sees "drew an Attack" and draws again -> when the draw pile empties, the discard (where the Strikes went) is reshuffled in.
- **Ruled out by reading the code**: (1) a hand-size mismatch -- `Draw` and the loop use the same `GetMaxHandSize` (the captured `CardPile.MaxCardsInHand`); (2) the solver's Pillage being unfaithful -- the real game's `Pillage.OnPlay` (decompiled with `ilspycmd`, needs `DOTNET_ROLL_FORWARD=Major`) has the identical loop `do { Draw } while (drawn is Attack && hand < MaxCardsInHand)`, so the real game exits only because the Strikes kill the enemy; (3) auto-play being deferred -- `CombatPredictionSimulator.AutoPlay` resolves `OnPlayWrapper` immediately; (4) Hellraiser and the auto-play target resolution disagreeing about targets -- both use `State.HittableEnemies`; (5) a dead enemy staying targetable -- `IsHittable` rejects `!IsAlive`, and a dead boss would end the loop through `IsOverOrEnding` or Hellraiser's capped path.
- **What is still unexplained**: why the auto-played Strikes never kill the boss (Ceremonial Beast, 168 HP, Vulnerable 7; its `Plow` only stuns once at <= 150 HP and does not block damage). Remaining candidates: `HookMirrors.ShouldPlay` vetoing the auto-play so the card goes to its result pile unplayed (`CombatPredictionSimulator.AutoPlay.cs:36-42`), or damage that is zero / healed back. Telling these apart needs runtime state (enemy HP, whether the Strike was played or skipped) captured inside the loop.
- **Relevant code**: `src/Sts2Headless/RunSimulator.cs` `DoPlanCombatTurn` (~1148-1250); `python/play_full_run.py` `read_json_line()`

## [OPEN] BUG-041: Solve throws "Cannot fork with pending Power amount changes" (Regent, STRENGTH_POWER) (2026-09-23)
- **Decision type**: combat_play (solver path)
- **Description**: In the Phase 2 A/B, Regent's ON arm logged 4 `plan_combat_turn` errors: `CombatSearchCoordinator.Solve failed: InvalidOperationException: Cannot fork with pending Power amount changes: STRENGTH_POWER:1` (one instance lists `STRENGTH_POWER:1,STRENGTH_POWER:1`). The vendored engine refuses to fork its simulated state while a Power amount change is queued but not yet materialized. The harness fell back to the heuristic for those turns (logged once per run via the 26478ed diagnostics), so no run failed -- but those turns were not solver-played, a small dilution of the ON arm.
- **Possible lead, unconfirmed**: Phase 1 recorded that `PowerDynamicVarMaterializationGuardPatch` -- in the real mod a Harmony patch that fail-fasts when a background simulation materializes Power dynamic vars early -- is never installed here, because nothing in this headless build runs `Entry.Initialize()`. Worth checking whether the pending-change state that trips this fork check is one that guard would have prevented upstream. No evidence yet either way.
- **Relevant code**: `src/Sts2Headless/CombatSolverEngine/Search/SimulatedCombatState.Fork.cs:306` (the fork check; sibling guards at :299/:302 refuse pending turn-start and Knowledge Demon choices the same way); do not hand-edit vendored files -- see `VENDORED.md`.

## [OPEN] BUG-042: plan_combat_turn_execution_failed in 3 A/B games, and the ERROR row still loses act/floor (2026-09-23)
- **Decision type**: combat_play (solver path)
- **Description**: Defect `run_12`, `run_27` and Regent `run_6` (ON arm) ended with `error=plan_combat_turn_execution_failed` -- the live engine rejected an action mid-plan in a way `_execute_combat_plan_actions` treats as a hard failure (not one of the tolerated stale-plan / dead-target / auto-resolved-choice cases). The exact rejected action is in each run's game log and has not been inspected yet. Separately, all three SUMMARY rows read `act=None floor=None`: the plan-failure return path reads `state["context"]`, but when the failing response is an error dict that key is absent. The engine-error path already falls back to the last decision dict (BUG-039); this path does not.
- **Reproducible (2026-09-24)**: in the Phase 2b-1 tier A/B (30 s budget) Defect `run_12` and `run_27` and Regent `run_6` failed again with `plan_combat_turn_execution_failed` at EXACTLY the same step counts as in the Phase 2 A/B (125, 103, 123), so each is a deterministic repro. Defect `run_14` failed the same way for the first time at 30 s.
- **Relevant code**: `python/play_full_run.py` (plan-failure return in the `combat_play` branch); `python/combat_plan_driver.py` `_execute_combat_plan_actions`

## [FIXED] BUG-039: play_full_run.py combat fallback wedges on an engine-refused card; engine errors misreported as TIMEOUT (2026-09-20, fixed 2026-09-20)
- **Decision type**: combat_play (fallback heuristic, `play_run()` in python/play_full_run.py — the non-Ironclad path, since Ironclad is hard-gated to `plan_combat_turn`); top-level step loop error handling (all decision types)
- **Description**: Two related defects in `play_run()`, both in `python/play_full_run.py`:
  1. The fallback combat heuristic picked `playable[0]` unconditionally and assigned the response straight to `state`. When the engine refused the play (`DoPlayCard`'s "still in hand after action" check, RunSimulator.cs:988 — the same failure mode as BUG-004/BUG-006, where `can_play: true` and a runtime refusal are not contradictory because `CanPlay()` and the post-play verification are different checks), the loop re-picked the identical card forever, `state` became the error dict, and the run only ended when the STUCK detector's 20-iteration cap fired — reported as TIMEOUT. Observed live on Defect with `BeamCell [CARD.BEAM_CELL]`.
  2. Separately, the top-level step loop's `if state.get("type") == "error": ... break` fell into `return {..., "timeout": True}`, so ANY engine error (not just the exhausted-refusal case) was misreported as TIMEOUT in the SUMMARY table with `act=None floor=None` (the error dict carries neither `context` nor `player` — RunSimulator.cs's `Error()`/`ErrorWithTrace()`). Same dishonest-status class already fixed once for `plan_combat_turn` (see docs/superpowers/plans/2026-09-17-combatsolver-port-phase1.md's "问题 1" postmortem and `summarize()`'s docstring).
- **Fix**:
  1. Added a per-turn `refused_ids` set (function-scoped in `play_run()`) keyed by card **`id`** string, not hand index — a successful play of a different card re-indexes the hand, so a stored index would go stale and silently block the wrong card, while `id` is stable and a refusal is a property of the card's own behaviour (every copy sharing that id is equally suspect); this also guarantees termination since each refusal permanently removes >=1 id from candidacy. Turn key is `(context.get("act"), context.get("floor"), state.get("round"))`; the set resets whenever it changes. The fallback now runs an inner loop that resolves the whole refusal cascade for one outer-loop step: on a refusal it prints a one-line note (card id + engine message), adds the id to `refused_ids`, and deliberately leaves `state` untouched (a refusal changes nothing in the engine, so the pre-refusal decision dict is still exactly accurate — and there is no "get current state" command in the protocol to re-query it, see Program.cs's `HandleCommand`/RunSimulator.cs's `ExecuteAction`). Falls through to the existing end-turn/proceed path (unchanged) once no candidates remain.
  2. Replaced the top-level `break` with a `return {"victory": False, ..., "error": f"engine_error: {msg[:160]}"}` (never `"timeout"`), modelled on the existing plan-failure return. Since the error dict itself has no context/player, a `last_decision` local (updated whenever `state.get("type") == "decision"`, before the error check) supplies the fallback: `state.get("context") or last_decision.get("context", {})` / same for `player`.
- **Verification**: `python3 -m py_compile python/play_full_run.py`. Full CLAUDE.md regression gate (5 games x 5 characters) — `Completed: 5/5` for every character (Ironclad avg_floor=15.8, Silent 10.0, Defect 12.2, Regent 13.2, Necrobinder 8.8; no STUCK, no ERROR). 20-game Defect batch to exercise the intermittent refusal path — `Completed: 20/20`, and the recovery fired twice (both `CARD.CLAW`, seed `run_12`), each time falling straight through to the next hand candidate in the same step and reaching a genuine game_over. Exact before/after on one seed, isolated by temporarily giving the pre-fix code the same seeded map RNG so the route is identical: pre-fix `Run 12: TIMEOUT | seed=run_12 steps=107 act=None floor=None` and a false `Reached max steps (500)` line, `Completed: 11/12`; post-fix `Run 12: LOSS | seed=run_12 steps=140 act=1 floor=14`. Note the pre-fix mechanism: defect 2's `break` fired on the FIRST refusal, so the infinite re-pick of defect 1 was short-circuited — one refusal was enough to throw away a healthy run (hp 63/75, act 1) and label it TIMEOUT. Fixing only defect 2 would have made the abort honest (`ERROR | act=1 floor=12`) but still lost the game; both fixes are load-bearing. `summarize()` re-checked directly against a synthetic result dict of the new shape: renders `ERROR`, keeps real act/floor, excluded from `Completed`.
- **Relevant code**: python/play_full_run.py (`play_run()`, combat_play/else fallback ~line 244 pre-fix, and the top-level error check ~line 131 pre-fix); see also BUG-004 and BUG-006 for the "still in hand after action" lineage this recovers from.

## [FIXED] BUG-038: AssetCache.GetCompressedTexture2D InvalidCastException — boss STUCK at floor 17 (2026-05-06, fixed 2026-05-06)
- **Decision type**: combat_play (during Vantom Round 3 DismemberMove, KinPriest Round 4, possibly others)
- **Description**: `MegaCrit.Sts2.Core.Assets.AssetCache.GetCompressedTexture2D(string path)` does an internal `castclass CompressedTexture2D` that throws `InvalidCastException: Unable to cast object of type 'Godot.PackedScene' to type 'Godot.CompressedTexture2D'`. The cached entry is a PackedScene (likely from `PreloadVfxAssets` or game-side loading), and the unchecked cast faults the surrounding async task. The faulted task leaves ActionExecutor in a broken state on the enemy turn, the game never returns to PlayPhase, and after 10s the engine emits the boss-stuck signal → coordinator reports `STUCK` (floor=17, hp>0, no win).
- **Evidence**: `/tmp/coord_engine.log` contained ~25 `InvalidCastException ... at AssetCache.GetCompressedTexture2D` entries each followed by `[WARN] Unobserved task exception` and `[SIM] EndTurn stuck after 10s(boss) — Round=3, Enemies=[Vantom(hp=166)]` (and similar for KinPriest Round 4). 20-game eval pre-fix: 2/20 STUCK at floor=17 hp>0; post-fix: 0/20 STUCK, boss combat completes (genuine wins or genuine deaths instead).
- **Fix**: IL-patch all `AssetCache.Get*(path)` methods (`GetScene`, `GetTexture2D`, `GetMaterial`, `GetCompressedTexture2D`) by replacing every `castclass T` instruction with `isinst T`. `isinst` returns null on type mismatch instead of throwing — downstream NREs are already swallowed by `InlineSyncCtx.Pump` (BUG-029). Persisted as Patch 5 in setup.sh.
- **Relevant code**: setup.sh (Patch 5 block, after Patch 4); lib/sts2.dll patched in-place

## [FIXED] BUG-037: Vantom.DismemberMove NullRef on NGame.DoHitStop — boss cr=2-3% (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (end_turn during Vantom boss Round 3)
- **Description**: After Cmd.Wait IL patch fixed the deadlock (BUG-014/BUG-030), `Vantom.DismemberMove` still throws `NullReferenceException` at `callvirt NGame.DoHitStop(2, 1)`. The game code correctly null-checks NCombatRoom.get_Instance() and NGame.get_Instance() elsewhere (RadialBlur, ScreenShake) but forgot the guard before DoHitStop. In headless, NGame.Instance=null → callvirt on null → NullRef → faulted task → ActionExecutor dies → TurnStarted never fires → 10s boss timeout → stuck signal → cr.
- **Evidence**: All cr events in Run 16 training show `Enemies=[Vantom(hp=16X)]`, Round=3, consistent NullRef stack trace pointing to `Vantom.DismemberMove`.
- **Fix**: IL-patch `Vantom/<DismemberMove>d__34::MoveNext` to add `dup; brfalse SKIP; ldc.i4.2; ldc.i4.1; callvirt DoHitStop; br AFTER; SKIP: pop; AFTER:` null guard pattern (same as the RadialBlur/ScreenShake guards already in the same method). Applied standalone (not part of setup.sh Patch 4 addition) at 2026-04-30 16:49.
- **Relevant code**: setup.sh Patch 4; lib/sts2.dll patched in-place (mtime Apr 30 16:49)

## [FIXED] BUG-036: Shop buy_potion fails with NullRef — purchase never completes (2026-04-30, fixed 2026-04-30)
- **Decision type**: shop (buy_potion)
- **Description**: `DoBuyPotion` calls `entry.OnTryPurchaseWrapper(...).GetAwaiter().GetResult()` which blocks the main thread. UI animation code runs and throws `NullReferenceException` (missing VFX slot reference). Exception is caught+logged, but potion is NOT added to player inventory. Same root cause in `DoBuyCard` and `DoBuyRelic`.
- **Evidence**: Repeated "Buy potion failed: Object reference not set to an instance of an object." in crash_stderr.log across all Run 16 training episodes.
- **Fix**: Replaced `GetAwaiter().GetResult()` with `SuppressYield=true` + pump-while-wait loop (same as BUG-031 fix). Also logs exception type for future diagnosis. Applied to DoBuyCard, DoBuyRelic, DoBuyPotion.
- **Relevant code**: RunSimulator.cs (DoBuyCard ~line 1182, DoBuyRelic ~line 1208, DoBuyPotion ~line 1234)

## [FIXED] BUG-035: card_select "add N from pool" picks WORST cards instead of BEST (2026-04-30, fixed 2026-04-30)
- **Decision type**: card_select (event room with multi-card pool)
- **Description**: Events like "满屋芝士" (add 2 from 8 commons) have max_sel=2, pool=8. The card_select handler's condition `len(cards) <= 5 and max_sel == 1` only catches single-card discoveries. Pools with max_sel>=2 or len>5 fell into the "large pool = removal" branch and picked the WORST cards. Example: cheese event → picked Blood Wall (4.0) + Tremble (4.5) instead of best 2 powers.
- **Fix**: Changed condition to `max_sel >= 2 or len(cards) <= 10` → picks BEST N cards. Only large single-select pools (len > 10, max_sel = 1) use the "remove worst" path.
- **Relevant code**: agent/combat_env.py (card_select handler, lines ~265-276)

## [FIXED] BUG-034: Whirlwind NullRef + round-stale stuck causing cr=6% in Run 16 (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (play_card WHIRLWIND; end_turn after any fast-path enemy turn)
- **Description**: Two related crash causes contributing ~3-4% each to cr=6%:
  1. `Whirlwind.OnPlay()` throws `NullReferenceException` in headless (missing Godot creature reference). DoPlayCard catches it and returns GameOverState(false), but the card should simply not be acquired.
  2. `PumpStartOfTurnSetup` exits when `energy > 0` before `RoundNumber` increments. `DetectDecisionPoint()` returns `combat_play` with stale round=N. Python stuck detection sees end_turn returning same round+HP → sends 5 proceeds → kills process → `crashed=True`.
- **Fix**:
  1. Lowered `WHIRLWIND` score from 7.0 to 1.0 in `card_scoring.py` so agent stops acquiring it.
  2. Added `prevRound` parameter to `PumpStartOfTurnSetup` — keeps pumping until both turn data ready AND `RoundNumber > prevRound`.
- **Relevant code**: RunSimulator.cs (PumpStartOfTurnSetup); agent/card_scoring.py (WHIRLWIND)

## [FIXED] BUG-033: BloodPotion crash — TargetType.None self-heal potion needs player.Creature target in combat (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (use_potion during combat)
- **Description**: `BloodPotion` has `TargetType.None` but the game engine's `UsePotionAction.ExecuteAction()` internally classifies it as a "single target" potion. When `target=null` (the BUG-002 fix), the game throws `InvalidOperationException: Attempted to execute UsePotionAction with single target potion during combat, but the target ID is null!`
- **Fix**: Added fallback: when `target == null && CombatManager.Instance.IsInProgress`, set `target = player.Creature`.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoUsePotion, line ~1401)

## [FIXED] BUG-001: Potion index shifts after use, causing invalid index errors (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play
- **Description**: After using a potion at index 0, remaining potions shift indices but the old indices are still referenced.
- **Fix**: Changed verification from index-based check to reference-based `Contains(potion)` check in `DoUsePotion`.
- **Relevant code**: Sts2Headless/RunSimulator.cs:~775

## [FIXED] BUG-002: Potion use_potion fails silently for some potion types (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play
- **Description**: Potions with TargetType.None/All (Attack Potion, Fortifier, Lucky Tonic) had incorrect auto-targeting.
- **Fix**: Removed catch-all else branch that forced `target = player.Creature`. Now only Self/AnyEnemy get auto-targets; others correctly leave target as null.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoUsePotion auto-targeting)

## [FIXED] BUG-014: Vantom R3 end_turn deadlock from Cmd.Wait in StatusCard intent (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play (end_turn during Vantom boss Round 3)
- **Description**: Vantom's DISMEMBER_MOVE adds 3 Wound status cards via CardPileCmd.AddToCombatAndPreview(), which calls Cmd.Wait(1f) for UI preview animation. In headless mode, Cmd.Wait never completes (no Godot scene tree), blocking the ActionExecutor, preventing WaitUntilQueueIsEmpty from completing, preventing StartTurn from firing, causing _turnStarted event to never set.
- **Fix**: Harmony patch on both Cmd.Wait() overloads to return Task.CompletedTask immediately (no-op in headless mode).
- **Relevant code**: Sts2Headless/RunSimulator.cs (PatchCmdWait, YieldPatches.CmdWaitPrefix)

## [FIXED] BUG-015: Self-targeting potions (Flex, Fortifier) applied to enemies when target_index provided (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play (use_potion)
- **Description**: DoUsePotion checked target_index before TargetType, so Self-targeting potions like Flex Potion would target enemy at index 0 instead of the player.
- **Fix**: Check potion.TargetType first — Self/TargetedNoCreature always targets player regardless of target_index.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoUsePotion)

## [FIXED] BUG-016: Rest site HEAL creates infinite rest_site loop (2026-03-22, fixed 2026-03-22)
- **Decision type**: rest_site (choose_option for HEAL)
- **Description**: After choosing HEAL, rest site options didn't clear, so DetectDecisionPoint returned rest_site again.
- **Fix**: After non-Smith rest options, force transition to map via ForceToMap().
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoChooseOption rest site handler)

## [WONTFIX] BUG-003: EOF crash during Leaf Slime combat (2026-03-22)
- **Decision type**: combat_play
- **Description**: Simulator occasionally crashes (returns EOF) during combat with Leaf Slime groups, possibly related to slime splitting mechanics or card interactions during split.
- **Repro**: Fight Leaf Slime group with seed=silent_run_3
- **Reported by**: Silent agent
- **Resolution**: Process-level crash (EOF) cannot be fixed in RunSimulator.cs. Requires EOF recovery in the bridge layer (sts2_bridge.py). The root cause is likely an unhandled exception in the game engine during slime split that kills the process.
- **Relevant code**: Sts2Headless/RunSimulator.cs (combat resolution)

## [FIXED] BUG-022: Self-targeting cards fail when target_index provided (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play (play_card)
- **Description**: DoPlayCard checked target_index BEFORE card.TargetType. When a Self/None/All card (Defend, Powers) was played with target_index:0, it resolved to an enemy target instead of null, causing PlayCardAction to fail silently.
- **Fix**: Check card.TargetType first — only AnyEnemy cards use target_index. All other cards get target=null (game handles targeting internally).
- **Impact**: This was likely the root cause of most "Card could not be played" errors throughout 10+ iterations.
- **Verified**: Using log replay (step 8 of seed 970fd80347ca) — Defend with target_index:0 now succeeds.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoPlayCard target resolution)

## [FIXED] BUG-004: Cards reported as can_play=true infinitely, causing infinite play loop (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play
- **Description**: PlayCardAction failing silently left card in hand, causing infinite play attempts.
- **Fix**: Added post-play verification in DoPlayCard — if card is still in hand at same index after action, returns error "Card could not be played" instead of looping.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoPlayCard)

## [FIXED] BUG-005: game_over state reports hp == max_hp even when player died (2026-03-22, fixed 2026-03-22)
- **Decision type**: game_over
- **Description**: The game_over JSON response shows player.hp equal to player.max_hp (e.g. 80/80) even when the player died. The engine resets CurrentHp after death.
- **Fix**: Added `_lastKnownHp` field, updated every combat_play state and room transition. In GameOverState, when `!isVictory`, override hp to 0 (since the player is dead and _lastKnownHp > 0 confirms they were alive before).
- **Reported by**: Ironclad agent (iteration 2)
- **Relevant code**: Sts2Headless/RunSimulator.cs (GameOverState, CombatPlayState, DoMapSelect)

## [NEEDS_VERIFY] BUG-006: Regent Particle Wall card can_play=true but fails to play (2026-03-22)
- **Decision type**: combat_play
- **Description**: Particle Wall (Regent card) reports can_play=true but when played returns "Card could not be played (still in hand after action)". May require special target or condition not captured by can_play.
- **Reported by**: Regent agent (iteration 2)
- **Status**: BUG-022 fix (target_index handling for non-AnyEnemy cards) likely resolved the root cause. Also improved error message to include card name/ID for future debugging. Needs verification with a Regent run.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoPlayCard error message now includes card name)

## [FIXED] BUG-007: Regent Astral Pulse StarCostTooHigh despite can_play not checking (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play
- **Description**: Astral Pulse reports StarCostTooHigh error. The engine's `card.CanPlay()` doesn't check star cost, so cards with star_cost > 0 showed can_play=true even when player lacked stars.
- **Fix**: In CombatPlayState hand card serialization, when a card has star_cost > 0 and `pcs.Stars < starCost`, override `can_play` to false. This prevents the agent from attempting to play cards it can't afford.
- **Reported by**: Regent agent (iteration 2)
- **Relevant code**: Sts2Headless/RunSimulator.cs (CombatPlayState hand card serialization)

## [FIXED] BUG-008: Map/context missing boss encounter name (2026-03-22, fixed 2026-03-22)
- **Decision type**: map_select / all decisions
- **Description**: Boss node in map data and RunContext had no boss name/type info, only "Boss" label.
- **Fix**: Added boss encounter extraction from Act.BossEncounter, with localized name via Monster() lookup. Boss info now in both get_map response and every decision's context field.
- **Relevant code**: Sts2Headless/RunSimulator.cs (GetFullMap, RunContext)

## [FIXED] BUG-009: BBCode tags in card/relic descriptions (2026-03-22, fixed 2026-03-22)
- **Decision type**: all
- **Description**: Card and relic descriptions contained raw BBCode tags like [gold], [/blue], [b], [sine].
- **Fix**: Added StripBBCode() to LocLookup.Bilingual() that strips all BBCode tags.
- **Relevant code**: Sts2Headless/RunSimulator.cs (LocLookup class)

## [FIXED] BUG-011: NullReferenceException on select_map_node after leaving shop (2026-03-22, fixed 2026-03-22)
- **Decision type**: map_select
- **Description**: After leaving shop, selecting an Elite node causes NullReferenceException.
- **Fix**: 4 changes: (1) DoMapSelect uses direct EnterMapCoord instead of action executor, (2) null check for player.Creature in DetectDecisionPoint, (3) null check for map.GetPoint in MapSelectState, (4) WaitForActionExecutor after EnterRoom in DoLeaveRoom.
- **Relevant code**: Sts2Headless/RunSimulator.cs

## [FIXED] BUG-012: Boss name/ID empty in context.boss throughout entire run (2026-03-22, fixed 2026-03-22)
- **Decision type**: all decisions
- **Description**: `context.boss.name` was empty because code used non-existent `BossId` property.
- **Fix**: Changed `_runState.Act?.BossId.Entry` to `_runState.Act?.BossEncounter?.Id?.Entry` in both RunContext and GetFullMap. Also restructured output to `{id, name}` dict.
- **Relevant code**: Sts2Headless/RunSimulator.cs (lines ~1990, ~2670)

## [CANNOT_REPRODUCE] BUG-017: Silent Slice card deals 0 damage (2026-03-22)
- **Decision type**: combat_play
- **Description**: Slice card (0-cost Attack) deals 0 damage consistently. Multiple agents confirmed.
- **Status**: Likely a game data issue — the card's DynamicVars may not have a "damage" entry, or the headless mode card model doesn't define damage correctly. Without a game log showing Slice in hand with its stats, cannot reproduce or diagnose further. The displayed stats come from DynamicVars.BaseValue which may differ from actual resolved damage.
- **Workaround**: Never pick Slice.

## [NOT_A_BUG] BUG-018: Precise Cut displays wrong damage (2026-03-22)
- **Decision type**: combat_play
- **Description**: Precise Cut shows 13 damage in stats but only deals 3-5 actual damage.
- **Resolution**: The displayed stats are DynamicVars.BaseValue (base stats before combat modifiers). Actual damage at play time is calculated with Strength, Vulnerable, and other modifiers. The discrepancy between displayed base stats and actual resolved damage is expected behavior — the simulator shows base values, not combat-resolved values. This is consistent with how all other cards work.
- **Workaround**: Don't rely on displayed stats as exact damage values; they are base stats only.

## [CANNOT_REPRODUCE] BUG-019: Phantom Blades unreliable auto-trigger (2026-03-22)
- **Decision type**: combat_play
- **Description**: Phantom Blades power doesn't trigger reliably on enemy attack turns.
- **Status**: Event-driven power triggers may not fire correctly in headless mode due to missing event subscriptions or timing issues with the InlineSynchronizationContext. Without a game log showing Phantom Blades active and failing to trigger, cannot diagnose further.
- **Workaround**: Don't pick this card.

## [CANNOT_REPRODUCE] BUG-020: Danse Macabre end-of-turn damage doesn't apply (2026-03-22)
- **Decision type**: combat_play
- **Description**: Danse Macabre power supposed to deal end-of-turn damage but confirmed unreliable in simulator.
- **Status**: Same class of issue as BUG-019 — event-driven end-of-turn effects may not process correctly in headless mode. Needs a game log with Danse Macabre active to diagnose.
- **Workaround**: Don't rely on this for damage scaling.

## [CANNOT_REPRODUCE] BUG-021: Doom Potion doesn't tick damage (2026-03-22)
- **Decision type**: combat_play
- **Description**: Doom Potion applies 33 Doom but damage never ticks during boss fight.
- **Status**: Same class of issue as BUG-019/020 — Doom is a debuff whose damage tick is event-driven. May not fire in headless mode. Needs a game log with Doom applied to diagnose.
- **Workaround**: Don't rely on Doom for boss kills.

## [FIXED] BUG-013: Relic picking session conflict on room transition (2026-03-22, fixed 2026-03-22)
- **Decision type**: map_select
- **Description**: "InvalidOperationException: Attempted to start new relic picking session while one was already occurring!" on floor 12→13 transition. Caused by entering a new room (especially Treasure) before the previous relic picking session completes.
- **Fix**: (1) Added WaitForActionExecutor + Pump before EnterMapCoord in DoMapSelect to ensure pending sessions complete. (2) Added WaitForActionExecutor + Pump before treasure reward collection in TreasureState. (3) Added try/catch for InvalidOperationException with "relic picking session" message that waits and retries once.
- **Reported by**: Ironclad agent (iteration 3)
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoMapSelect, TreasureState)

## [FIXED] BUG-010: Vantom boss EndTurn deadlock (2026-03-22, fixed 2026-03-22)
- **Decision type**: combat_play (end_turn during Vantom boss fight)
- **Description**: Simulator deadlocked during EndTurn in Vantom boss fight. Task.Yield() posted continuations to ThreadPool that never completed.
- **Fix**: Re-enabled PatchTaskYield() Harmony patch (was commented out). Added targeted SuppressYield=true only during EndTurn (try/finally), disabled during map navigation. This forces Task.Yield() continuations to run inline synchronously during enemy turn processing.
- **Verification**: 15/15 regression runs pass (all 5 characters × 3 runs), 0 timeouts.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoEndTurn, EnsureModelDbInitialized, YieldPatches)

## [FIXED] BUG-024: Tools of Trade power blocks play_card at start of turn (2026-03-23, fixed 2026-03-23)
- **Decision type**: combat_play
- **Description**: When Tools of the Trade power is active (draw 1 + discard 1 at start of turn), the start-of-turn discard creates a pending card_select state. However, the bridge reports combat_play decision instead of card_select because the HasPending check at line ~1154 runs BEFORE the Pump() at line ~1210 that processes start-of-turn effects.
- **Fix**: Added a re-check for `_cardSelector.HasPending` AFTER the `Pump()` + `WaitForActionExecutor()` in the combat room section of DetectDecisionPoint. If a pending card selection appeared during pump (from start-of-turn powers), it now correctly jumps back to the card_select handler via goto label.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DetectDecisionPoint, combat room section ~line 1210)

## [FIXED] BUG-023: Shop card removal causes NullReferenceException in PlayerSummary (2026-03-23, fixed 2026-03-23)
- **Decision type**: shop (remove_card → select_cards)
- **Description**: After removing a card via shop card removal, returning to ShopState triggers NullReferenceException in PlayerSummary's deck card serialization. The removed card's model becomes null in the deck list, but PlayerSummary iterates all cards without null-checking.
- **Fix**: Added `.Where(c => c != null)` filter before `.Select()` in PlayerSummary deck serialization. Also fixed `deck_size` to use `.Count(c => c != null)` to exclude null entries.
- **Relevant code**: Sts2Headless/RunSimulator.cs (PlayerSummary, lines ~2090-2091)

## [FIXED] BUG-025: Eradicate can_play=true at 0 energy blocks turn end (2026-03-23, fixed 2026-03-23)
- **Decision type**: combat_play
- **Description**: Eradicate has base cost 0 with Retain keyword. Its mechanic deals 11 damage × current energy. At 0 energy, `CanPlay()` returns true (cost 0 ≤ 0 energy), but playing it fails because the card action requires energy > 0. This blocks the game: the bridge won't allow end_turn when "playable" cards exist, and the card can't actually be played.
- **Fix**: Added special-case override in CombatPlayState hand card serialization: when card is ERADICATE and player energy is 0, set can_play to false. Same pattern as BUG-007 (Astral Pulse star cost).
- **Relevant code**: Sts2Headless/RunSimulator.cs (CombatPlayState hand card serialization, ~line 1414)

## [FIXED] BUG-027: DEF-mode enemy causes energy=0 + empty hand stuck (2026-04-24, fixed 2026-04-24)
- **Decision type**: combat_play (end_turn when hand is empty, energy=0, vs DEF-mode enemy)
- **Description**: When a DEF-mode enemy (e.g. Nibbit, Vine Shambler) processes its turn instantaneously inside Pump(), IsPlayPhase flips back to true before SuppressYield is cleared. The new player turn's start-of-turn setup (energy reset + card draw) contains Task.Yield() continuations that — when SuppressYield=false — get posted to ThreadPool and never complete. Result: every subsequent `end_turn` sees energy=0 + empty hand (round never advances).
- **Root cause**: `TurnStarted` event fires at the START of player turn, BEFORE draw/energy reset actions complete. All DoEndTurn fallback loops ran with SuppressYield=false, so Task.Yield() in start-of-turn actions posted to ThreadPool and hung.
- **Fix**: Added `PumpStartOfTurnSetup(Player player)` helper that sets SuppressYield=true, then pumps until energy>0 or cards appear in hand (or deck exhausted). Called in 4 locations in DoEndTurn: (1) immediately in the initial try-block when IsPlayPhase is already true after Pump(), (2) after the first fallback loop when _turnStarted.IsSet, (3) after the cancel+retry fallback loop, (4) after the nuclear fallback succeeds.
- **Verification**: strategic_play.py confirmed correct round advancement (Energy:3 + new hand) after end_turn vs DEF-mode Nibbit. HP changes between turns confirm turns are advancing.
- **Relevant code**: RunSimulator.cs (DoEndTurn, PumpStartOfTurnSetup ~line 2558)

## [OPEN] BUG-028: Nuclear fallback fails for Wrigglers and AssassinRubyRaider (2026-04-24)
- **Decision type**: combat_play (end_turn during Wriggler/AssassinRubyRaider fights)
- **Description**: Wrigglers (potentially split mechanic) and AssassinRubyRaider cause `Nuclear fallback FAILED` — ActionExecutor.IsRunning=False but IsPlayPhase stays False. The enemy turn never completes. Pre-existing issue (confirmed by baseline test without BUG-027 fix — same failure).
- **Status**: Not reproduced cleanly enough to diagnose. Likely related to special enemy mechanics (Wriggler split, Assassin steal) that create intermediate game states not handled by headless mode.
- **Relevant code**: RunSimulator.cs (DoEndTurn nuclear fallback, ~line 1000)

## [FIXED] BUG-029: VFX NullRef kills ActionExecutor during boss fights (2026-04-29, fixed 2026-04-29)
- **Decision type**: combat_play (end_turn during KinPriest / any enemy with PlayerHurtVignette)
- **Description**: `KinPriest.BeamMove()` damages the player → `PlayerHurtVignetteHelper.Play()` → `NLowHpBorderVfx.Create()` calls `AssetCache.Get("res://scenes/vfx/ui/vfx_low_hp_border.tscn")` → returns null in headless → NullReferenceException. The NRE throws from inside a continuation posted to `InlineSynchronizationContext.Post()`. This causes the async infrastructure to propagate the NRE into the ActionExecutor's task chain, killing `IsRunning` → fight reported as `crash/stuck`.
- **Evidence**: 35,000+ `TaskHelper.LogTaskExceptions` log entries in crash_stderr.log. Floor=17 (KinPriest boss) always showed `crash/stuck` in eval before fix.
- **Fix**: Catch `NullReferenceException` and `InvalidOperationException` per-callback in `InlineSynchronizationContext.Post()` drain loop and `Pump()`. VFX failures are non-fatal in headless mode — the exception is logged to stderr but execution continues normally.
- **Verification**: 20-game eval shows game 14 (floor=17 seed) now ends as `[dead]` instead of `crash/stuck`. No crashes in 20 games.
- **Relevant code**: RunSimulator.cs (`InlineSynchronizationContext.Post/Pump`, ~line 44)

## [FIXED] BUG-030: Vantom DISMEMBER_MOVE deadlocks boss fight via Cmd.Wait (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (end_turn during Vantom boss Round 3)
- **Description**: `Cmd.Wait(float, bool)` and `Cmd.Wait(float, CancellationToken, bool)` are used for UI animation delays (e.g. AddToCombatAndPreview for Vantom's Wound cards). In headless mode, these create timers that never fire, causing the ActionExecutor to hang indefinitely.
- **Root cause**: All Harmony runtime patches (including the existing Cmd.Wait patch) fail on CoreCLR 10.0.5 with "CoreCLR version X is not supported". The runtime patch was never actually applied.
- **Evidence**: 365 "EndTurn stuck after 10s(boss)" entries in crash_stderr.log, all at Round=3, Enemies=[Vantom]. Harmony "[WARN] Failed to patch Cmd.Wait" appears on every game startup.
- **Fix**: IL-patch `lib/sts2.dll` via Mono.Cecil to replace both Cmd.Wait() overloads with `return Task.CompletedTask`. Added to setup.sh (Patch 3) so future installs apply the fix permanently.
- **Relevant code**: setup.sh (Patch 3), lib/sts2.dll

## [FIXED] BUG-031: DetectPostCombatState deadlock — GenerateWithoutOffering blocks main thread with SuppressYield=false (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (play_card with hand=1 enemies=1 — killing the last enemy)
- **Description**: `DetectPostCombatState()` calls `rewardsSet.GenerateWithoutOffering().GetAwaiter().GetResult()` without SuppressYield=true. The async method contains `await Task.Yield()` calls (IL-patched in sts2.dll). With SuppressYield=false, continuations post to `InlineSyncCtx`, but main thread is blocked on `.GetResult()` → permanent deadlock. Same applies to `OnSelectWrapper().GetResult()`.
- **Evidence**: All 9+ crash log entries have `hand=1 enemies=1 play_card` or `end_turn` pattern — all represent the combat-ending action where the last enemy dies and rewards are generated. cr=11% steady.
- **Fix**: Wrapped `GenerateWithoutOffering().GetResult()` and `OnSelectWrapper().GetResult()` in `DetectPostCombatState` with `YieldPatches.SuppressYield = true` / finally block. Also added `SuppressYield=true` to `DoPlayCard`'s `WaitForActionExecutor()` call so on-death async effects complete inline.
- **Relevant code**: RunSimulator.cs (`DetectPostCombatState` ~line 2083, `DoPlayCard` ~line 899)

## [FIXED] BUG-032: KinPriest crash — GpuParticles2D.Amount missing from GodotStubs (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (end_turn during KinPriest boss fight)
- **Description**: KinPriest (and KinFollower) emit VFX via `GpuParticles2D.Amount` property setter. This method was not stubbed in GodotStubs, causing "Method not found: 'Void Godot.GpuParticles2D.set_Amount(Int32)'" exceptions that killed the ActionExecutor.
- **Evidence**: 87 "EndTurn stuck after Ns — KinFollower/KinPriest" entries in crash_stderr.log. Error message "Method not found: 'Void Godot.GpuParticles2D.set_Amount'" in log.
- **Fix**: Added `Amount`, `Lifetime`, `LifetimeRandomness`, `OneShot`, `LocalCoords`, `SpeedScale`, `Explosiveness`, `Restart()` to GpuParticles2D and CpuParticles2D in GodotStubs/UI.cs and ExtraGodotTypes.cs.
- **Relevant code**: src/GodotStubs/UI.cs (GpuParticles2D), src/GodotStubs/ExtraGodotTypes.cs (CpuParticles2D)

## [FIXED] BUG-033: BloodPotion crash — TargetType.None self-heal potion needs player.Creature target in combat (2026-04-30, fixed 2026-04-30)
- **Decision type**: combat_play (use_potion during combat)
- **Description**: `BloodPotion` has `TargetType.None` but the game engine's `UsePotionAction.ExecuteAction()` internally classifies it as a "single target" potion. When `target=null` (the BUG-002 fix), the game throws `InvalidOperationException: Attempted to execute UsePotionAction with single target potion during combat, but the target ID is null!`. This unobserved async exception is rethrown by the .NET finalizer thread, crashing the process. Evidence: ~7% cr rate in Run 16, 2x per crash_stderr.log entry.
- **Root cause**: BUG-002 fix removed the catch-all `target = player.Creature` fallback. BloodPotion has `TargetType.None` but still requires a creature target in combat.
- **Fix**: Added fallback: when `target == null && CombatManager.Instance.IsInProgress`, set `target = player.Creature`. This covers all TargetType.None potions used in combat (BloodPotion, Fortifier, etc.) which should all target the player.
- **Relevant code**: Sts2Headless/RunSimulator.cs (DoUsePotion, line ~1401)

## [FIXED] BUG-026: Attack Potion card_select deadlocks combat — all cards unplayable (2026-03-23, fixed 2026-03-23)
- **Decision type**: combat_play (use_potion with Attack Potion)
- **Description**: When Attack Potion is used, it triggers a card_select (choose from 3 attack cards). The game engine's async method awaits GetSelectedCards(), which creates a pending TaskCompletionSource. WaitForActionExecutor loops 1000 pumps but the executor can never finish because it's awaiting the user's card selection. After WaitForActionExecutor gives up (with IsRunning still true), all subsequent card plays and end_turn fail because the executor remains permanently "stuck".
- **Fix**: Added early exit in WaitForActionExecutor when `_cardSelector.HasPending` or `_cardSelector.HasPendingReward` is true. The executor can't progress until the user resolves the selection, so there's no point waiting. This allows DoUsePotion to return the card_select decision immediately. When DoSelectCards later resolves the selection, the executor chain completes normally.
- **Relevant code**: Sts2Headless/RunSimulator.cs (WaitForActionExecutor, ~line 1990)
