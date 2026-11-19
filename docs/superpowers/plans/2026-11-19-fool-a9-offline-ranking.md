# Fool A9 CLI restoration, Solver branches and offline ranking

**Goal:** Run the installed v0.113.1 Fool at A9 entirely in isolated CLI processes; prove exact reward restoration, label all reward options with Solver outcomes, and fit a small offline ranking model. Live campaign, policy, model files and saves remain unchanged.

**Architecture:** A dedicated worktree has its own copied/IL-patched game libraries and generated stubs. Persist complete command/observation prefixes and verified pre-room checkpoints. Every branch starts a fresh process, verifies the entire decision observation and engine version/hash, then takes exactly one option. All battles must execute usable Solver plans; failures are technical errors, never losses or training rows. Train on within-state option differences, split by seed, and keep the resulting artifact out of all advisor import paths.

**Execution:** Use subagent-driven development because C#/DLL compatibility and Python collection/model training have separate file ownership and review boundaries. The parent owns integration and end-to-end state-identity checks.

## 1. Isolated CLI compatibility

- Copy current Steam v0.113.1 dependencies into this worktree's lib; never link writable libraries to the live checkout.
- Run setup/IL checks on these copies. Fix API and generated Godot stub gaps; preserve old-character support.
- Register Fool and add a smoke regression that starts Fool A9, checks its character/ascension, starter deck, and deterministic seed.
- Surface native Fool mechanics to the vendored Solver; verify actual plan execution, not only a successful search response.

## 2. Exact restoration contract

- Freeze each card-reward observation with seed, game/library hashes, full action prefix and canonical SHA256.
- Replay into independent CLI processes and compare the complete JSON observation, including reward order/upgrades, HP, deck, relics, gold, potion belt, room/floor and future RNG progression.
- A checkpoint shortcut is usable only after matching full-prefix replay and future continuation; otherwise retain exact full-prefix replay.
- Test both taking a card and skipping, so restoration cannot pass while omitting the consequence of selection.

## 3. Small collection

- Target 24 reward decisions across at least 6 seeds, 3 repetitions per legal option including skip, first at 3500ms Solver search budget with 1 worker; increase to 12000ms on ambiguous or incomplete labels.
- Keep continuations comparable, use the same future route/policy within a case, stop after the next combat for the initial labels and record next-Boss evidence separately where available.
- Record restore hash, selected card, plans/actions, combat outcome, HP delta, duration, expansions/search budget and all failures. Do not mix events into this reward dataset.
- Report per-option stability; discard technical errors, unverifiable restores and cross-seed/unequal-state comparisons.

## 4. Offline ranker and validation

- Build pairwise examples only inside one verified decision, with confidence from repeated Solver branches.
- Use transparent deck/candidate/HP/relic/phase features and a regularized small ranking model. No seed ID or future-result leakage in features.
- Split train/validation/test by seed. Report state-level top-option agreement, pairwise accuracy and outcome regret against current heuristic, random choice and always-skip baselines.
- A model trained with insufficient/indistinguishable labels must be reported as unvalidated; never claim increased full-run win rate from next-combat labels.

## 5. Integration boundary and required checks

- Review all changes; run the project-required full regression, 5 completed games per supported character with Solver, plus targeted Fool/replay tests. Run only one batch from this checkout.
- Integrate CLI and offline tooling to mainline only after checks. Do not move the running feature checkout HEAD, install/restart the advisor, load learned weights online, or change the hourly monitoring automation.
- Verify the live /health commit and campaign configuration match the pre-work snapshots, and the hourly heartbeat remains active.
