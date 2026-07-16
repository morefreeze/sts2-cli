# Relic-aware Observation + Warm-start Retrain — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the combat PPO policy a relic-aware observation (full multi-hot over the 272-relic DB) and warm-start a freeze-safe retrain from `13186k`, so it can learn HP-efficient play to break the floor-15 attrition wall.

**Architecture:** Add a relic multi-hot block to the combat observation, appended in `combat_env._encode` after the existing `extra_obs` features (the 161/169 `StateEncoder` stays untouched). Training reuses the *existing* `train.py --obs-expand 169 --vf-pretrain-chunks 2` warm-start path (copies policy weights, zero-inits relic columns, reinits + pretrains the value net). All eval paths auto-detect obs size from the loaded model, so prior checkpoints keep working.

**Tech Stack:** Python, numpy, Gymnasium `Box` space, sb3-contrib MaskablePPO, pytest.

**Spec:** `docs/superpowers/specs/2026-06-15-relic-aware-obs-retrain-design.md`

---

## File Structure

- `agent/state_encoder.py` — NEW: relic vocab loader (`build_relic_vocab`, `RELIC_VOCAB`, `RELIC_VOCAB_SIZE`) + `encode_relics(ids)`. Pure functions, no game dependency.
- `agent/combat_env.py` — MODIFY: `CombatEnv.__init__` relic-obs flag + `observation_space`; `_encode` appends the relic block; reuse existing `_state_relic_ids`.
- `agent/eval_rl.py`, `agent/rl_agent.py`, `agent/boss_retry.py` — MODIFY: obs-size→flags auto-detect ladder (161 / 169 / 169+vocab).
- `tests/test_relic_obs.py` — NEW: unit tests for vocab + encode_relics + the obs-flags helper (pure Python, fast, no game DLLs).
- `agent/train.py` — NO CODE CHANGE (uses existing `--obs-expand` / `--vf-pretrain-chunks`); training driven by `STS2_RELIC_OBS=1` env var read in `combat_env`.
- Training itself = a gated runbook (Phase B), not auto-run.

**Key data facts (verified):**
- `data/relics.json` = `{"relics": [272 dicts], "n": 272}`. Each entry has `runtime_id` (uppercase, e.g. `"AKABEKO"`, `"ANCHOR"`) and `id` (kebab, e.g. `"akabeko"`).
- `CombatEnv._state_relic_ids(state)` returns uppercase snake-case ids (`.upper().replace("-","_").replace(" ","_")`) — matches `runtime_id` after the same normalization.
- `combat_env.py`: `_EXTRA_OBS = 8 if extra_obs else 0`; `observation_space` shape = `enc.obs_size + _EXTRA_OBS` (line ~539); `_encode` (line ~887) appends the 8 extras.
- `train.py`: `--obs-expand <old_size>` (line 477) triggers `_expand_obs_checkpoint`; `--vf-pretrain-chunks` (line 479) already exists.

---

## PHASE A — Relic-aware observation (code, fully testable without training)

### Task 1: Relic vocabulary loader

**Files:**
- Modify: `agent/state_encoder.py` (add at module level, after imports)
- Test: `tests/test_relic_obs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relic_obs.py
import numpy as np
from agent.state_encoder import build_relic_vocab, RELIC_VOCAB, RELIC_VOCAB_SIZE

def test_relic_vocab_built_from_db():
    vocab = build_relic_vocab("data/relics.json")
    # 272 relics in the DB
    assert len(vocab) == 272
    # canonical uppercase ids present, matching _state_relic_ids output
    assert "AKABEKO" in vocab
    assert "ANCHOR" in vocab
    # deterministic, contiguous 0..N-1 indices
    assert sorted(vocab.values()) == list(range(272))
    # module constants mirror the loaded vocab
    assert RELIC_VOCAB_SIZE == len(RELIC_VOCAB) == 272

def test_relic_vocab_cap():
    vocab = build_relic_vocab("data/relics.json", cap=50)
    assert len(vocab) == 50
    assert sorted(vocab.values()) == list(range(50))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py::test_relic_vocab_built_from_db -v`
Expected: FAIL — `ImportError: cannot import name 'build_relic_vocab'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent/state_encoder.py — add after the existing imports (json, numpy already imported)
import os

_RELIC_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "relics.json")

def _norm_relic_id(rid: str) -> str:
    return str(rid).upper().replace("-", "_").replace(" ", "_")

def build_relic_vocab(path: str = _RELIC_DB_PATH, cap: int | None = None) -> dict:
    """relic runtime_id (normalized, uppercase snake) -> contiguous index.
    Sorted for determinism. `cap` keeps the first N (alphabetical) ids."""
    with open(path) as f:
        relics = json.load(f)["relics"]
    ids = sorted({_norm_relic_id(r.get("runtime_id") or r.get("id") or "")
                  for r in relics} - {""})
    if cap is not None:
        ids = ids[:cap]
    return {rid: i for i, rid in enumerate(ids)}

# Built once at import. To cap the vocab (e.g. top-50), set STS2_RELIC_VOCAB_CAP.
_cap = os.environ.get("STS2_RELIC_VOCAB_CAP")
RELIC_VOCAB = build_relic_vocab(cap=int(_cap) if _cap else None)
RELIC_VOCAB_SIZE = len(RELIC_VOCAB)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py -k vocab -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/state_encoder.py tests/test_relic_obs.py
git commit -m "feat(obs): relic vocabulary loader from 272-relic DB"
```

---

### Task 2: Relic multi-hot encoder

**Files:**
- Modify: `agent/state_encoder.py`
- Test: `tests/test_relic_obs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relic_obs.py — append
from agent.state_encoder import encode_relics

def test_encode_relics_multihot():
    vec = encode_relics(["AKABEKO", "ANCHOR"])
    assert vec.dtype == np.float32
    assert vec.shape == (RELIC_VOCAB_SIZE,)
    assert vec.sum() == 2.0
    assert vec[RELIC_VOCAB["AKABEKO"]] == 1.0
    assert vec[RELIC_VOCAB["ANCHOR"]] == 1.0

def test_encode_relics_unknown_ignored():
    vec = encode_relics(["NOT_A_REAL_RELIC_XYZ"])
    assert vec.shape == (RELIC_VOCAB_SIZE,)
    assert vec.sum() == 0.0

def test_encode_relics_empty():
    assert encode_relics([]).sum() == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py -k encode_relics -v`
Expected: FAIL — `ImportError: cannot import name 'encode_relics'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent/state_encoder.py — add after RELIC_VOCAB_SIZE
def encode_relics(relic_ids) -> np.ndarray:
    """Multi-hot over RELIC_VOCAB. Unknown ids are ignored. Caller passes the
    output of CombatEnv._state_relic_ids (already uppercase snake-case)."""
    vec = np.zeros(RELIC_VOCAB_SIZE, dtype=np.float32)
    for rid in relic_ids or []:
        i = RELIC_VOCAB.get(rid)
        if i is not None:
            vec[i] = 1.0
    return vec
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py -k encode_relics -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/state_encoder.py tests/test_relic_obs.py
git commit -m "feat(obs): relic multi-hot encoder"
```

---

### Task 3: obs-size → flags auto-detect helper

**Files:**
- Modify: `agent/state_encoder.py` (add helper next to the vocab)
- Test: `tests/test_relic_obs.py`

This helper is the single source of truth for "given a model's obs width and the base encoder size, which feature blocks are on?" Used by every eval path (Task 4) so they never disagree.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_relic_obs.py — append
from agent.state_encoder import obs_flags_for_size

def test_obs_flags_ladder():
    # base encoder is 161 dims
    assert obs_flags_for_size(161, 161) == (False, False)          # legacy
    assert obs_flags_for_size(169, 161) == (True, False)           # +8 extra_obs
    assert obs_flags_for_size(161 + 8 + RELIC_VOCAB_SIZE, 161) == (True, True)  # +relics

def test_obs_flags_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        obs_flags_for_size(200, 161)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py -k obs_flags -v`
Expected: FAIL — `ImportError: cannot import name 'obs_flags_for_size'`.

- [ ] **Step 3: Write minimal implementation**

```python
# agent/state_encoder.py — add after encode_relics
def obs_flags_for_size(model_obs_size: int, base_obs_size: int) -> tuple:
    """Map a model's observation width to (extra_obs, relic_obs) flags.
    base_obs_size is StateEncoder.obs_size (161). Raises on an unknown width."""
    if model_obs_size == base_obs_size:
        return (False, False)
    if model_obs_size == base_obs_size + 8:
        return (True, False)
    if model_obs_size == base_obs_size + 8 + RELIC_VOCAB_SIZE:
        return (True, True)
    raise ValueError(
        f"obs width {model_obs_size} not in ladder "
        f"{{{base_obs_size}, {base_obs_size+8}, {base_obs_size+8+RELIC_VOCAB_SIZE}}}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_relic_obs.py -k obs_flags -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agent/state_encoder.py tests/test_relic_obs.py
git commit -m "feat(obs): obs-size->flags ladder helper"
```

---

### Task 4: Wire relic block into CombatEnv

**Files:**
- Modify: `agent/combat_env.py` — `__init__` (~line 521-540), `_encode` (~line 887-899)

`_encode` currently early-returns `base` when `_EXTRA_OBS == 0`. Restructure so the extras list is built and concatenated whenever EITHER extra_obs OR relic_obs is on.

- [ ] **Step 1: Add the constructor flag + observation_space**

In `CombatEnv.__init__`, find (line ~538-540):

```python
        self._EXTRA_OBS = 8 if extra_obs else 0
        self.observation_space = Box(low=0.0, high=1.0,
                                     shape=(self.enc.obs_size + self._EXTRA_OBS,), dtype=np.float32)
```

Replace with:

```python
        import os as _os_env
        relic_obs = _os_env.environ.get("STS2_RELIC_OBS") == "1" if relic_obs is None else relic_obs
        self._EXTRA_OBS = 8 if extra_obs else 0
        from agent.state_encoder import RELIC_VOCAB_SIZE
        self._RELIC_OBS = RELIC_VOCAB_SIZE if relic_obs else 0
        self.observation_space = Box(
            low=0.0, high=1.0,
            shape=(self.enc.obs_size + self._EXTRA_OBS + self._RELIC_OBS,), dtype=np.float32)
```

Add `relic_obs: bool = None` to the `__init__` signature (next to `extra_obs: bool = True` at line ~521).

- [ ] **Step 2: Update `_encode` to append the relic block**

Replace `_encode` (lines ~887-899) with:

```python
    def _encode(self, state: dict) -> np.ndarray:
        """Encode state + optional extra (8) + optional relic multi-hot."""
        base = self.enc.encode(state)
        if self._EXTRA_OBS == 0 and self._RELIC_OBS == 0:
            return base
        parts = [base]
        if self._EXTRA_OBS:
            floor_norm = min(self._current_floor / 17.0, 1.0)
            enemies = state.get("enemies", [])
            extra = [floor_norm, self._combat_entry_hp_ratio]
            for slot in range(3):
                e = enemies[slot] if slot < len(enemies) else {}
                extra.append(min(_enemy_power_amount(e, "Vulnerable") / 10.0, 1.0))
                extra.append(min(_enemy_power_amount(e, "Weak") / 10.0, 1.0))
            parts.append(np.array(extra, dtype=np.float32))
        if self._RELIC_OBS:
            from agent.state_encoder import encode_relics
            parts.append(encode_relics(CombatEnv._state_relic_ids(state)))
        return np.concatenate(parts)
```

- [ ] **Step 3: Smoke-verify obs shape end-to-end** (requires game DLLs)

Run:
```bash
STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" \
PATH="$HOME/.dotnet-arm64:$PATH" DOTNET_ROOT="$HOME/.dotnet-arm64" \
STS2_RELIC_OBS=1 .venv/bin/python -c "
from agent.combat_env import CombatEnv
from agent.state_encoder import RELIC_VOCAB_SIZE
e = CombatEnv(character='Ironclad', seed='shape_test', max_floor=0)
print('obs shape:', e.observation_space.shape, 'expected:', 161+8+RELIC_VOCAB_SIZE)
assert e.observation_space.shape == (161+8+RELIC_VOCAB_SIZE,)
print('OK')
"
```
Expected: `obs shape: (441,) expected: 441` then `OK`. Then `pkill -9 -f Sts2Headless`.

- [ ] **Step 4: Verify backward compat (relic off → 169)**

Run:
```bash
.venv/bin/python -c "
from agent.combat_env import CombatEnv
e = CombatEnv(character='Ironclad', seed='compat', max_floor=0, relic_obs=False)
assert e.observation_space.shape == (169,), e.observation_space.shape
print('backward-compat 169 OK')
" ; pkill -9 -f Sts2Headless 2>/dev/null
```
Expected: `backward-compat 169 OK`.

- [ ] **Step 5: Commit**

```bash
git add agent/combat_env.py
git commit -m "feat(obs): append relic multi-hot block in CombatEnv (STS2_RELIC_OBS)"
```

---

### Task 5: Auto-detect relic obs in all eval paths

**Files:**
- Modify: `agent/eval_rl.py` (~line 331-333), `agent/rl_agent.py` (~line 13-15), `agent/boss_retry.py` (where it builds CombatEnv)

Every eval path must set `relic_obs` from the *model's* obs width (model is source of truth), so a relic-aware ckpt gets a 441-dim env and a 169 ckpt gets a 169-dim env — regardless of `STS2_RELIC_OBS`.

- [ ] **Step 1: eval_rl.py — replace the obs-size detection**

Find (line ~331-333):
```python
    model_obs_size = model.observation_space.shape[0]
    extra_obs = (model_obs_size > 161)
```
Replace:
```python
    from agent.state_encoder import obs_flags_for_size
    model_obs_size = model.observation_space.shape[0]
    extra_obs, relic_obs = obs_flags_for_size(model_obs_size, 161)
```
Then thread `relic_obs=relic_obs` into the `env_kwargs` dict (next to `extra_obs=extra_obs`, line ~287):
```python
        env_kwargs = dict(character=character, seed=game_seed,
                          seed_prefix=f"eval_{i}", max_floor=0,
                          extra_obs=extra_obs, relic_obs=relic_obs)
```

- [ ] **Step 2: rl_agent.py — same detection**

Find (line ~13-15):
```python
        model_obs_size = self.model.observation_space.shape[0]
        self._extra_obs = model_obs_size > self.enc.obs_size
        self._extra_dim = model_obs_size - self.enc.obs_size
```
Replace:
```python
        from agent.state_encoder import obs_flags_for_size
        model_obs_size = self.model.observation_space.shape[0]
        self._extra_obs, self._relic_obs = obs_flags_for_size(model_obs_size, self.enc.obs_size)
        self._extra_dim = model_obs_size - self.enc.obs_size
```
If rl_agent builds a CombatEnv, pass `relic_obs=self._relic_obs`. If it encodes states directly, ensure it uses `CombatEnv._encode` semantics (append extras + relics). NOTE: inspect rl_agent.py first; if it builds its own obs vector inline, mirror the `_encode` append logic there using `encode_relics(CombatEnv._state_relic_ids(state))`.

- [ ] **Step 3: boss_retry.py — pass relic_obs when constructing CombatEnv**

Inspect `agent/boss_retry.py` for `CombatEnv(...)` construction. Add the same detection from the loaded model and pass `relic_obs=`. (If boss_retry loads the env via eval_rl helpers, this may be inherited — verify.)

- [ ] **Step 4: Regression — existing 169 ckpt still evaluates**

Run (n=2, fast):
```bash
PATH="$HOME/.dotnet-arm64:$PATH" DOTNET_ROOT="$HOME/.dotnet-arm64" STS2_MC_ROLLOUT=smart \
  .venv/bin/python -m agent.eval_rl checkpoints_best/ppo_ironclad_13186k.zip \
  --n-games 2 --character Ironclad --deck-log none 2>&1 | grep -E "avg_floor|Error|Traceback"
pkill -9 -f Sts2Headless 2>/dev/null
```
Expected: prints `avg_floor`, NO Error/Traceback (169 ckpt → flags (True, False) → 169 env, unchanged behavior).

- [ ] **Step 5: Commit**

```bash
git add agent/eval_rl.py agent/rl_agent.py agent/boss_retry.py
git commit -m "feat(obs): auto-detect relic obs from model width in all eval paths"
```

---

### Task 6: Warm-start determinism check (verification only — proves the retrain is safe)

This proves the expanded model behaves identically to `13186k` at step 0 (relic columns are zero-init, so they contribute 0 to the forward pass regardless of relic input). No new product code — this is the go/no-go gate before any training.

**Files:**
- Create (scratch): `/tmp/sts2-cli/check_warmstart.py`

- [ ] **Step 1: Write the check script**

```python
# /tmp/sts2-cli/check_warmstart.py
import os, numpy as np, torch
from sb3_contrib import MaskablePPO
from agent.state_encoder import RELIC_VOCAB_SIZE
NEW = 161 + 8 + RELIC_VOCAB_SIZE            # 441
old = MaskablePPO.load("checkpoints_best/ppo_ironclad_13186k.zip", device="cpu")
# random 169-dim obs; pad to 441 with arbitrary relic bits
rng = np.random.default_rng(0)
o169 = rng.random((5, 169), dtype=np.float32)
relic = (rng.random((5, RELIC_VOCAB_SIZE)) > 0.9).astype(np.float32)
o441 = np.concatenate([o169, relic], axis=1)
# Expand a fresh policy the same way _expand_obs_checkpoint does, in-process:
import agent.train as T
# Build a 441-obs dummy vec_env spec via the helper is heavy; instead expand weights directly:
import torch.nn as nn
newp = MaskablePPO("MlpPolicy",  # placeholder env not needed for forward check
    __import__("gymnasium").spaces  # noqa
    , device="cpu")  # NOTE: replace with the actual _expand_obs_checkpoint call in Step 3
print("placeholder")
```

(Step 1 is a sketch — the real determinism check runs through `train.py`'s own expand path, see Step 3. The point is to compare actions.)

- [ ] **Step 2: Produce the expanded seed via the real path** (gated — confirm with user first; short, ~2 chunks)

This is the FIRST training command and belongs to Phase B. Do not run it as part of code review; it is the runbook entry below. After it produces `checkpoints_relic/ppo_ironclad_*.zip`, run Step 3.

- [ ] **Step 3: Compare expanded-seed actions vs 13186k on matched states**

After the seed exists, run a fixed-seed n=10 A/B (eval_rl `--fixed-seeds`) of the *pre-training* expanded seed vs 13186k. Because the VF-pretrain only touches the value net (policy net + zero relic columns preserved), `avg_floor` and `floor dist` should be ~identical (within seed noise). Expected: avg_floor within ±0.3 of 13186k. If it diverges sharply, the warm-start is wrong — STOP and debug before the long run.

---

## PHASE B — Training runbook (USER-GATED; do NOT auto-run)

> Each command is freeze-safe: short chunks, checkpoint per chunk, `pkill -9 -f Sts2Headless` between phases, gate on system load `< 12` first (`sysctl -n vm.loadavg`). Confirm with the user before each launch. See the compute-freeze lesson in agent memory.

- [ ] **B1: Expand + VF-pretrain → relic seed** (~2 chunks, user-gated)

```bash
cd ~/mygit/sts2-cli && pkill -9 -f Sts2Headless; sleep 2
STS2_RELIC_OBS=1 STS2_MC_ROLLOUT=smart \
PATH="$HOME/.dotnet-arm64:$PATH" DOTNET_ROOT="$HOME/.dotnet-arm64" \
nohup .venv/bin/python -m agent.train --character Ironclad \
  --checkpoint checkpoints_best/ppo_ironclad_13186k.zip \
  --obs-expand 169 --vf-pretrain-chunks 2 \
  --steps 80000 --save-dir checkpoints_relic --eval-freq 0 \
  > /tmp/sts2-cli/relic_seed.log 2>&1 &
```
Produces `checkpoints_relic/ppo_ironclad_*.zip` (441-dim). Then run Task 6 Step 3 (sanity A/B vs 13186k).

- [ ] **B2: Evolve from the relic seed** (user-gated, freeze-safe chunks)

```bash
cd ~/mygit/sts2-cli && pkill -9 -f Sts2Headless; sleep 2
STS2_RELIC_OBS=1 nohup .venv/bin/python -m agent.evolve_loop \
  --rounds 8 --chunk-steps 40000 --n-eval 30 \
  --best checkpoints_relic/<seed_ckpt>.zip --best-reach 3 \
  > /tmp/sts2-cli/relic_evolve.log 2>&1 &
```
Per round: HP=72 clutch sentinel (≥12/15) + fixed-seed reach eval; promote to `checkpoints_best/` only if clutch holds AND reach > base. (evolve_loop's env propagates `STS2_RELIC_OBS=1` to the train subprocess; sentinel/eval auto-detect obs width from the 441-dim ckpt via Task 5.)

- [ ] **B3: Final fixed-seed A/B — did it break the wall?**

```bash
# base vs best relic ckpt, IDENTICAL seeds, measure fl15 attrition + reach
for ck in checkpoints_best/ppo_ironclad_13186k.zip checkpoints_best/<relic_best>.zip; do
  pkill -9 -f Sts2Headless; sleep 1
  PATH="$HOME/.dotnet-arm64:$PATH" DOTNET_ROOT="$HOME/.dotnet-arm64" STS2_MC_ROLLOUT=smart \
   .venv/bin/python -m agent.eval_rl "$ck" --n-games 40 --fixed-seeds --deck-log none \
   --combat-snapshot-dir /tmp/sts2-cli/relic_ab_$(basename $ck) --combat-snapshot-floors 7,9,11,13,15
done
# compare avg_floor, reach, and HP-at-fl15 (analyze_fl_snaps.py)
```
SUCCESS = relic_best enters fl15 healthier and/or reaches more, clutch intact, avg_floor not regressed.
WATCH (C learnability): if flat after ~3-4 post-VF chunks, set `STS2_RELIC_VOCAB_CAP=50` (top-50) or fall back to effect-based encoding — see spec §5.

---

## Self-Review

**Spec coverage:**
- Obs change / encoding (spec §1) → Tasks 1-4. ✓
- Warm-start + VF-pretrain (spec §2) → existing `--obs-expand`/`--vf-pretrain-chunks`, gate Task 6 + B1. ✓
- Training loop / freeze-safe / anti-regression (spec §3) → B1-B3. ✓
- Success criteria (spec §4) → B3. ✓
- Risks & fallback (spec §5) → B3 WATCH note + `STS2_RELIC_VOCAB_CAP`. ✓
- Backward compatibility → Task 4 Step 4, Task 5 Step 4. ✓

**Placeholder scan:** Task 6 Step 1 is explicitly a sketch superseded by Step 3 (real path) — acceptable because the determinism guarantee is verified end-to-end in Step 3, not via the sketch. All product-code tasks (1-5) have complete code. `<seed_ckpt>` / `<relic_best>` are runtime filenames filled in when the ckpts exist (unavoidable).

**Type consistency:** `build_relic_vocab`, `RELIC_VOCAB`, `RELIC_VOCAB_SIZE`, `encode_relics`, `obs_flags_for_size(model_obs_size, base_obs_size) -> (extra_obs, relic_obs)` used consistently across Tasks 1-5. `relic_obs` constructor param + `_RELIC_OBS` + `STS2_RELIC_OBS` env var consistent. Obs width 441 = 161+8+272 consistent throughout.

**Open risk noted:** Task 5 Step 2 — must inspect `rl_agent.py` to see whether it builds a CombatEnv or encodes inline; the plan covers both branches.
