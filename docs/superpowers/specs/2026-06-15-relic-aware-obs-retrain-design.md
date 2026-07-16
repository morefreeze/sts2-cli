# Relic-aware observation + warm-start retrain — Design

**Date:** 2026-06-15
**Status:** Approved (brainstorm), pending implementation plan
**Author:** session continuation of the floor-15 attrition investigation

## Problem & motivation

Boss-reach is stuck at ~13-17% (best ckpt `13308k_RETRAIN_23pct`, 16.7%). Root
cause, established this session via the new `eval_rl --combat-snapshot-floors`
diagnostic: runs **bleed out across Act 1** and hit a **floor-15 wall** — they
arrive at floor 15 with a *good* deck (deck_quality ~6.2) against *low-burst*
routine fights but at **~46-53% HP**, and a normal fight finishes them. ~37% of
deaths cluster at floor 15, ~30% at floors 7-9.

Every attempt to fix this by making the policy play "safer" has been falsified
this session and prior:
- HP-preservation **reward** retrain (option d): falsified-to-marginal (+7pp fl15, noise).
- **heal-more** (rest-site blanket swap −1.0 floor; potion-heal boost −0.7): reverted.
- **block-more** (STS2_DEFENSE intent-defense): −0.5 floor, reach 4→2.

Unifying lesson: **in STS, killing fast IS the defense**; the PPO policy is at a
local optimum where racing prevents more damage than safer play saves. Inference
overrides that force caution trade away the offense that prevents damage.

The remaining lever with upside is to give the policy **information it does not
currently have**, so it can learn genuinely-more-efficient play (not forced
caution). The biggest information gap found in `state_encoder.py`: **the policy
is completely blind to its relics**, which heavily change optimal combat play
(block/energy/strength/heal effects). NOTE the policy already sees incoming
damage, hp, block, hand, and per-enemy intent — so relics are the gap, not threat
visibility.

## Scope / non-goals

- **In scope:** add a relic encoding to the combat observation; warm-start an
  expanded-obs policy from 13186k; chunked freeze-safe retrain with anti-regression
  gating; fixed-seed A/B to measure floor-15 attrition + reach.
- **Out of scope:** changing map/rest/potion heuristics (the RL policy only controls
  combat card-play); reward reshaping (falsified); the deck-scoring layers.
- The RL policy controls combat only — a combat-obs change affects within-combat play.

## Decisions (locked in brainstorm)

1. **Hypothesis:** encode relics (chosen over buff-identity / reconsider).
2. **Encoding scheme:** **C — full multi-hot over ~269 relics** (chosen over
   effect-based A / curated-50 B). Trivial to build, complete, reuses the existing
   269-relic vocab lineage. Accepted trade-off: worst learnability (rare relics seen
   too seldom). Mitigated by warm-start zero-init + a documented fallback to A/top-50.

## Design

### 1. Observation change
- Build a stable, sorted relic-id vocabulary from `data/relics.json`
  (`RELIC_VOCAB_SIZE`, default = full ~269; one constant, can be capped to top-50).
- Append the relic multi-hot in `combat_env.py::_encode`, as a new block AFTER the
  existing `extra_obs` 8 features (parallel to how `extra_obs` is appended at
  `_encode` today) — NOT inside `state_encoder.encode`, so the 161/169 encoder
  stays untouched and backward-compatible. `_encode` already concatenates extras.
- Vocab + the relic→index map live in `state_encoder.py` as a loaded constant
  (built once from `data/relics.json`); `_encode` calls it. `RELIC_VOCAB_SIZE =
  len(vocab)` (not a hardcoded literal). Relic ids via the existing
  `CombatEnv._state_relic_ids(state)` / `rid` normalization at `combat_env.py:125`
  (upper + `-`/space→`_`) — vocab keys MUST use the same normalization.
- New obs size = `enc.obs_size(161) + extra_obs(8) + RELIC_VOCAB_SIZE` (≈438 for full).
- **Gating:** detect via obs-size exactly like the existing `extra_obs` (161 vs
  169) path so `13186k` (169-dim) and all prior evals load unchanged. eval_rl and
  rl_agent already auto-detect obs size from the model — extend that ladder
  (161 / 169 / 169+relics).

### 2. Warm-start + VF-pretrain
- Reuse the existing `train.py::_expand_obs_checkpoint(...)`: loads 13186k,
  creates the new-obs model, copies policy weights, **zero-pads the relic columns**
  of the first linear layer, **reinits the value network** (reward scale shifts).
- Because policy weights are preserved and relic dims start at 0, the expanded
  model **behaves identically to 13186k at step 0** — safe warm-start, not scratch.
- **VF-pretrain: first ~2 chunks** let the reinit'd value net catch up before its
  advantage estimates are trusted (per Run 11 161→169 recipe). Do NOT change
  gamma/gae_lambda (see [[feedback_gamma_switch_regression]]).

### 3. Training loop (freeze-safe + anti-regression)
- Chunked, **40k steps/chunk**, checkpoint every chunk → a freeze loses ≤1 chunk
  (the box just rebooted under sustained training load — see
  [[feedback_compute_limits_freeze_risk]]). Confirm-before-launch; monitor load;
  never stack `Sts2Headless`.
- Orchestrate via the existing `evolve_loop` pattern. Per chunk after VF-pretrain:
  1. **HP=72 clutch sentinel** (`boss_retry` deterministic sweep, win ≥12/15).
  2. **Fixed-seed A/B vs 13186k** (n≥40, `--fixed-seeds`) using
     `--combat-snapshot-floors 7,9,11,13,15` to measure floor-15 HP-at-entry + reach
     + avg_floor. (HP metrics are seed-confounded — always fixed-seed, see
     [[feedback_fixed_seed_ab_for_hp_metrics]].)
  3. **Promote** to `checkpoints_best/` only if clutch holds AND it beats 13186k on
     reach/attrition.

### 4. Success criteria
A promoted relic-aware ckpt with HP=72 clutch ≥12/15 that, on fixed-seed A/B vs
13186k, enters floor 15 healthier (higher HP-at-entry median, n≥40) AND/OR reaches
the boss more often — without regressing avg_floor.

### 5. Risks & fallback
- **C learnability (primary risk):** rare relics seen too seldom to learn. Watch:
  if after ~3-4 post-VF chunks the fixed-seed A/B is flat (no attrition/reach gain),
  the relic dims aren't being used → **fall back** to effect-based encoding (A,
  ~12 dims) or cap vocab to top-50. One-constant / localized change.
- **Retrain regresses clutch** (historical): the sentinel blocks promotion of broken
  ckpts; 13186k/13308k stay protected.
- **Freeze:** small chunks + per-chunk checkpoints bound the loss; gate on load <12.
- **Obs-size drift breaks old tooling:** keep the obs-size ladder backward-compatible;
  all prior ckpts remain loadable.

## Key files
- `agent/state_encoder.py` — relic vocab + multi-hot encode (or in `_encode`).
- `agent/combat_env.py` — `_encode` append, obs-size/gating, `_state_relic_ids`.
- `agent/train.py` — `_expand_obs_checkpoint` (reuse), VF-pretrain phase, hyperparams.
- `agent/eval_rl.py`, `agent/rl_agent.py` — extend obs-size auto-detect ladder.
- `agent/evolve_loop.py` — orchestration (sentinel + fixed-seed A/B + promote).
- `data/relics.json` — relic vocab source.

## Open questions (resolved)
- Vocab size: full ~269 (user choice); knob to cap at 50.
- Warm-start: confirmed feasible via existing `_expand_obs_checkpoint`.
- Value net: reinit'd by the warm-start helper → VF-pretrain required.
