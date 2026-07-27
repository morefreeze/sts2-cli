# Act 1 Marginal Card-Quality Gate Implementation Plan

> **For agentic workers:** Use the `subagent-driven-development` or
> `executing-plans` skill to implement this plan. In the current session,
> execute it inline with `executing-plans` because sub-agent work has not been
> authorized.

**Goal:** Preserve the `13955k` checkpoint's fixed-seed Act 1 progression,
produce leaner and no-worse-quality boss-entry decks, and enter Act 2 on at
least one of the same 20 seeds.

**Architecture:** Add a fail-open, pure eligibility predicate beside the
existing deck scoring functions. In `greedy_action`, filter only Act 1
card-reward offers, retain their original indices, and pass the remaining
cards to the unchanged `pick_best_card` ranking and threshold logic. Keep a
default-on environment switch for a one-variable A/B and immediate rollback.

**Tech Stack:** Python 3, pytest, Gymnasium, MaskablePPO, the existing
`agent.eval_rl` and `agent.train` CLIs.

**Approved design:**
`docs/superpowers/specs/2026-07-27-act1-card-quality-gate-design.md`

**Execution outcome:** The original implementation exposed the runtime act at
`state.context.act`, not `state.act`. Fixed-seed iterations then added a floor
12 activation threshold, a 16-card hard cap, and a `-0.01` premium dilution
floor. The static gate passed six of seven promotion checks but did not enter
Act 2; the one approved 2,048-step fine-tune regressed progression. The
updated design spec records the final evidence and overrides the initial
default-on and 18-card snippets below. The delivered switch is default-off.

---

## Task 1: Add the pure Act 1 eligibility predicate

**Files:**

- Modify: `agent/card_scoring.py`
- Modify: `tests/agent/test_card_scoring.py`

### Step 1: Write boundary and fail-open tests

Add the predicate to the test import:

```python
from agent.card_scoring import (
    deck_5turn_burst,
    deck_quality_metrics,
    is_act1_card_reward_eligible,
    pick_best_card,
    score_deck_dimensions,
    set_mc_context,
)
```

Add a small signal stub and the required boundary cases:

```python
def _stub_gate_signals(monkeypatch, *, delta, score=5.0, card_tags=()):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda cards: {"overall": 0.5 + (delta if len(cards) == 16 else 0.0)},
    )
    monkeypatch.setattr(scoring, "score_card_in_deck", lambda offered, deck: score)

    def fake_tags(value):
        if value.get("id") == "CARD.OFFER":
            return set(card_tags)
        return {"SCALING_PILLAR"} if value.get("pillar") else set()

    monkeypatch.setattr(scoring, "_card_tags", fake_tags)


def _gate_deck(size, *, pillars=0):
    return [
        {
            "id": f"CARD.DECK_{i}",
            "pillar": i < pillars,
        }
        for i in range(size)
    ]


def test_act1_card_quality_gate_is_inactive_outside_act1(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda deck: (_ for _ in ()).throw(AssertionError("gate should not score")),
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(18), act=2
    )


def test_act1_card_quality_gate_is_inactive_below_15_cards(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda deck: (_ for _ in ()).throw(AssertionError("gate should not score")),
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(14), act=1
    )


@pytest.mark.parametrize(
    ("delta", "expected"),
    [(0.0, False), (-0.001, False), (0.001, True)],
)
def test_act1_card_quality_gate_midrange_delta_boundary(
        monkeypatch, delta, expected):
    _stub_gate_signals(monkeypatch, delta=delta)
    assert (
        is_act1_card_reward_eligible(
            {"id": "CARD.OFFER"}, _gate_deck(15), act=1
        )
        is expected
    )


@pytest.mark.parametrize(
    ("score", "tags", "pillars"),
    [(9.5, (), 2), (5.0, ("SCALING_PILLAR",), 1)],
)
def test_act1_card_quality_gate_midrange_premium_exception(
        monkeypatch, score, tags, pillars):
    _stub_gate_signals(
        monkeypatch,
        delta=-0.001,
        score=score,
        card_tags=tags,
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(15, pillars=pillars), act=1
    )


@pytest.mark.parametrize(
    ("delta", "score", "expected"),
    [(-0.011, 10.0, False), (-0.010, 10.0, True), (0.005, 5.0, True)],
)
def test_act1_card_quality_gate_large_deck_boundaries(
        monkeypatch, delta, score, expected):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring,
        "deck_quality_metrics",
        lambda cards: {
            "overall": 0.5 + (delta if len(cards) == 19 else 0.0)
        },
    )
    monkeypatch.setattr(scoring, "score_card_in_deck", lambda offered, deck: score)
    monkeypatch.setattr(scoring, "_card_tags", lambda value: set())
    assert (
        is_act1_card_reward_eligible(
            {"id": "CARD.OFFER"}, _gate_deck(18), act=1
        )
        is expected
    )


@pytest.mark.parametrize(
    ("card", "deck", "act"),
    [
        ({}, _gate_deck(15), 1),
        ({"id": "CARD.OFFER"}, [{"name": "missing id"}] * 15, 1),
        ({"id": "CARD.OFFER"}, _gate_deck(15), None),
        ({"id": "CARD.OFFER"}, _gate_deck(15), "invalid"),
    ],
)
def test_act1_card_quality_gate_invalid_inputs_fail_open(card, deck, act):
    assert is_act1_card_reward_eligible(card, deck, act)


def test_act1_card_quality_gate_nonfinite_metric_fails_open(monkeypatch):
    import agent.card_scoring as scoring

    monkeypatch.setattr(
        scoring, "deck_quality_metrics", lambda cards: {"overall": float("nan")}
    )
    assert is_act1_card_reward_eligible(
        {"id": "CARD.OFFER"}, _gate_deck(15), act=1
    )
```

### Step 2: Run the new tests and confirm the expected failure

Run:

```bash
../../.venv/bin/python -m pytest tests/agent/test_card_scoring.py \
  -k 'act1_card_quality_gate' -q
```

Expected: collection fails because
`is_act1_card_reward_eligible` does not exist.

### Step 3: Implement the fail-open predicate

Add `import math as _math` near the top of `agent/card_scoring.py`, then add:

```python
def is_act1_card_reward_eligible(
        card: dict, deck: list[dict], act: object) -> bool:
    """Whether an offered card may enter a late Act 1 deck.

    This is deliberately fail-open: state or scoring problems must preserve
    the pre-gate card reward behavior.
    """
    try:
        if isinstance(act, bool) or act is None:
            return True
        act_number = int(act)
        if isinstance(act, float) and not act.is_integer():
            return True
        if act_number != 1:
            return True
        if not isinstance(card, dict) or not _card_id_norm(card):
            return True
        if not isinstance(deck, list):
            return True
        if any(not isinstance(c, dict) or not _card_id_norm(c) for c in deck):
            return True
        if len(deck) < 15:
            return True

        before = float(deck_quality_metrics(deck)["overall"])
        after = float(deck_quality_metrics(deck + [card])["overall"])
        delta = after - before
        score = float(score_card_in_deck(card, deck))
        if not _math.isfinite(delta) or not _math.isfinite(score):
            return True

        tags = _card_tags(card)
        scaling_pillars = sum(
            1 for deck_card in deck
            if "SCALING_PILLAR" in _card_tags(deck_card)
        )
        premium_core = (
            score >= 9.5
            or ("SCALING_PILLAR" in tags and scaling_pillars < 2)
        )

        if len(deck) >= 18:
            return delta >= 0.005 or (premium_core and delta >= -0.01)
        return delta > 0.0 or premium_core
    except Exception:
        return True
```

Keep the broad exception handler: it is the approved fail-open contract, not
general error suppression.

### Step 4: Run focused and module tests

Run:

```bash
../../.venv/bin/python -m pytest tests/agent/test_card_scoring.py \
  -k 'act1_card_quality_gate' -q
../../.venv/bin/python -m pytest tests/agent/test_card_scoring.py -q
```

Expected: both commands pass.

### Step 5: Commit the pure predicate

```bash
git add agent/card_scoring.py tests/agent/test_card_scoring.py
git commit -m "feat(agent): add act 1 card quality eligibility"
```

---

## Task 2: Filter rewards and preserve original indices

**Files:**

- Modify: `agent/combat_env.py`
- Modify: `tests/agent/test_combat_env.py`

### Step 1: Write integration tests

Add this module import at the top of the test file:

```python
import agent.combat_env as combat_env
```

Add a helper and four integration tests:

```python
def _late_card_reward_state():
    return {
        "decision": "card_reward",
        "act": 1,
        "floor": 12,
        "player": {
            "deck": [{"id": f"CARD.DECK_{i}"} for i in range(15)],
            "deck_size": 15,
        },
        "cards": [
            {"id": "CARD.OLD_TOP", "index": 0},
            {"id": "CARD.ELIGIBLE", "index": 1},
        ],
    }


def test_card_quality_gate_selects_eligible_card_at_original_index(monkeypatch):
    state = _late_card_reward_state()
    seen = {}
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: card["id"] == "CARD.ELIGIBLE",
    )

    def fake_pick(cards, *, threshold, deck):
        seen["ids"] = [card["id"] for card in cards]
        return 0

    monkeypatch.setattr(combat_env, "pick_best_card", fake_pick)
    action = greedy_action(state)
    assert seen["ids"] == ["CARD.ELIGIBLE"]
    assert action["args"]["card_index"] == 1


def test_card_quality_gate_skips_when_every_offer_is_ineligible(monkeypatch):
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: False,
    )
    monkeypatch.setattr(
        combat_env,
        "pick_best_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty eligible set should skip before ranking")
        ),
    )
    assert greedy_action(_late_card_reward_state()) == {
        "cmd": "action",
        "action": "skip_card_reward",
    }


def test_card_quality_gate_is_enabled_by_default(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.delenv("STS2_CARD_QUALITY_GATE", raising=False)
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: card["id"] == "CARD.ELIGIBLE",
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 1


def test_card_quality_gate_zero_restores_unfiltered_selection(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "0")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("disabled gate should not inspect offers")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 0
```

### Step 2: Run the integration tests and confirm the expected failure

Run:

```bash
../../.venv/bin/python -m pytest tests/agent/test_combat_env.py \
  -k 'card_quality_gate' -q
```

Expected: the monkeypatch fails because the predicate is not imported into
`agent.combat_env`.

### Step 3: Add the feature switch and filtering

Extend the existing scoring import:

```python
from agent.card_scoring import (
    score_card,
    score_card_in_deck,
    pick_best_card,
    pick_worst_card,
    deck_quality_score,
    is_act1_card_reward_eligible,
    _card_id_norm,
)
```

Add beside `_decision_advisor_enabled`:

```python
def _card_quality_gate_enabled() -> bool:
    flag = os.environ.get("STS2_CARD_QUALITY_GATE", "1").strip().lower()
    return flag not in {"0", "false", "off", "no"}
```

Replace the direct `pick_best_card(cards, ...)` section of `card_reward` with:

```python
            indexed_cards = list(enumerate(cards))
            if _card_quality_gate_enabled():
                act = state.get("act")
                indexed_cards = [
                    (original_index, card)
                    for original_index, card in indexed_cards
                    if is_act1_card_reward_eligible(card, deck, act)
                ]
            eligible_cards = [card for _, card in indexed_cards]
            best_eligible = pick_best_card(
                eligible_cards, threshold=threshold, deck=deck
            )
            if best_eligible is not None:
                best = indexed_cards[best_eligible][0]
                return {
                    "cmd": "action",
                    "action": "select_card_reward",
                    "args": {"card_index": best},
                }
```

Do not move threshold calculation, MC context, or broken-card filtering.

### Step 4: Run focused and module tests

Run:

```bash
../../.venv/bin/python -m pytest tests/agent/test_combat_env.py \
  -k 'card_quality_gate' -q
../../.venv/bin/python -m pytest tests/agent/test_combat_env.py -q
```

Expected: both commands pass, including the old card-reward test.

### Step 5: Commit the integration

```bash
git add agent/combat_env.py tests/agent/test_combat_env.py
git commit -m "feat(agent): gate late act 1 card rewards"
```

---

## Task 3: Verify the code path before live evaluation

**Files:** No expected changes.

### Step 1: Run the two directly affected modules together

```bash
../../.venv/bin/python -m pytest \
  tests/agent/test_card_scoring.py \
  tests/agent/test_combat_env.py -q
```

Expected: all tests pass.

### Step 2: Run the complete Python suite

```bash
../../.venv/bin/python -m pytest -q
```

Expected: all tests pass. If an unrelated pre-existing test fails, record the
exact node ID and prove it also fails at `f919338` before proceeding; do not
silently weaken or skip it.

### Step 3: Review the code-only diff

```bash
git diff f919338..HEAD --check
git diff f919338..HEAD -- \
  agent/card_scoring.py agent/combat_env.py \
  tests/agent/test_card_scoring.py tests/agent/test_combat_env.py
git status --short
```

Expected: no whitespace errors, no generated runtime artifacts, and only the
planned code, tests, spec, and plan are tracked.

---

## Task 4: Run the paired 20-seed A/B

**Files:**

- Create runtime evidence only under ignored top-level `logs/` and
  `data/snapshots/`; do not commit it.

### Step 1: Define immutable inputs and unique output paths

Run from the worktree:

```bash
MODEL=../../checkpoints/act1_boss_13933_smoke_20260727/ppo_ironclad_13955k.zip
PY=../../.venv/bin/python
BASE_LOG=../../logs/eval_13955k_gate0_fixed20_20260727_v1.log
GATE_LOG=../../logs/eval_13955k_gate1_fixed20_20260727_v1.log
BASE_DECKS=../../data/eval_decks_13955k_gate0_fixed20_20260727_v1.jsonl
GATE_DECKS=../../data/eval_decks_13955k_gate1_fixed20_20260727_v1.jsonl
BASE_SNAPS=../../data/snapshots/boss_eval_13955k_gate0_fixed20_20260727_v1
GATE_SNAPS=../../data/snapshots/boss_eval_13955k_gate1_fixed20_20260727_v1
test -f "$MODEL"
test ! -e "$BASE_LOG"
test ! -e "$GATE_LOG"
test ! -e "$BASE_DECKS"
test ! -e "$GATE_DECKS"
test ! -e "$BASE_SNAPS"
test ! -e "$GATE_SNAPS"
mkdir -p ../../logs "$BASE_SNAPS" "$GATE_SNAPS"
```

If a `v1` path exists, increment both lanes to the same unused version rather
than deleting or appending to old evidence.

### Step 2: Run the baseline lane with only the gate disabled

```bash
env \
  DECK_HISTORY_PATH= \
  STS2_CARD_QUALITY_GATE=0 \
  STS2_DECISION_ADVISOR=0 \
  STS2_PLANNER=0 \
  STS2_BOSS_PLANNER_MASK=0 \
  STS2_BOSS_READINESS_LIFT=0 \
  "$PY" -m agent.eval_rl "$MODEL" \
    --n-games 20 \
    --fixed-seeds \
    --invalid-retries 2 \
    --deck-log "$BASE_DECKS" \
    --boss-snapshot-dir "$BASE_SNAPS" \
    --boss-snapshot-min-hp 0 \
  2>&1 | tee "$BASE_LOG"
```

Expected baseline reference: 20 valid games, zero invalid attempts,
`avg_floor=15.0`, boss reach `11/20`, and Act 2 `0/20`. If it differs, retain
the new result but stop promotion analysis until the environment/checkpoint
drift is explained.

### Step 3: Run the candidate lane with only the gate enabled

```bash
env \
  DECK_HISTORY_PATH= \
  STS2_CARD_QUALITY_GATE=1 \
  STS2_DECISION_ADVISOR=0 \
  STS2_PLANNER=0 \
  STS2_BOSS_PLANNER_MASK=0 \
  STS2_BOSS_READINESS_LIFT=0 \
  "$PY" -m agent.eval_rl "$MODEL" \
    --n-games 20 \
    --fixed-seeds \
    --invalid-retries 2 \
    --deck-log "$GATE_DECKS" \
    --boss-snapshot-dir "$GATE_SNAPS" \
    --boss-snapshot-min-hp 0 \
  2>&1 | tee "$GATE_LOG"
```

### Step 4: Produce the exact progression and deck comparison

Run this analysis without modifying either JSONL:

```bash
"$PY" - "$BASE_LOG" "$BASE_DECKS" "$GATE_LOG" "$GATE_DECKS" <<'PY'
import json
import re
import statistics
import sys

from agent.card_scoring import deck_quality_metrics

BASICS = {"STRIKE_IRONCLAD", "DEFEND_IRONCLAD"}


def load_lane(log_path, deck_path):
    log = open(log_path).read()
    valid = re.search(
        r"valid games\s*:\s*(\d+)/(\d+).*invalid_attempts=(\d+)", log
    )
    avg = re.search(r"avg_floor\s*:\s*([0-9.]+)", log)
    if not valid or not avg:
        raise SystemExit(f"missing evaluation summary in {log_path}")

    decks = []
    results = {}
    for line in open(deck_path):
        row = json.loads(line)
        key = (row.get("seed"), row.get("game_index"))
        if row.get("event") == "result":
            results[key] = row
        elif row.get("act") == 1 and row.get("cards"):
            decks.append(row)

    sizes = [len(row["cards"]) for row in decks]
    qualities = [
        deck_quality_metrics(
            [{"id": f"CARD.{card['id']}"} for card in row["cards"]]
        )["overall"]
        for row in decks
    ]
    basic_counts = [
        sum(card["id"].upper().removeprefix("CARD.") in BASICS
            for card in row["cards"])
        for row in decks
    ]
    act2 = sum(bool(row.get("boss_beaten")) for row in results.values())
    return {
        "valid": int(valid.group(1)),
        "requested": int(valid.group(2)),
        "invalid_attempts": int(valid.group(3)),
        "avg_floor": float(avg.group(1)),
        "boss_reach": len(decks),
        "act2": act2,
        "median_deck_size": statistics.median(sizes),
        "avg_quality": statistics.fmean(qualities),
        "avg_basics": statistics.fmean(basic_counts),
    }


baseline = load_lane(sys.argv[1], sys.argv[2])
candidate = load_lane(sys.argv[3], sys.argv[4])
checks = {
    "valid_20_zero_invalid": (
        candidate["valid"] == candidate["requested"] == 20
        and candidate["invalid_attempts"] == 0
    ),
    "avg_floor_at_least_15": candidate["avg_floor"] >= 15.0,
    "boss_reach_at_least_11": candidate["boss_reach"] >= 11,
    "act2_at_least_1": candidate["act2"] >= 1,
    "median_deck_one_smaller": (
        candidate["median_deck_size"] <= baseline["median_deck_size"] - 1
    ),
    "quality_not_lower": candidate["avg_quality"] >= baseline["avg_quality"],
    "starter_basics_not_higher": (
        candidate["avg_basics"] <= baseline["avg_basics"]
    ),
}
print(json.dumps(
    {"baseline": baseline, "candidate": candidate, "checks": checks},
    indent=2,
    sort_keys=True,
))
raise SystemExit(0 if all(checks.values()) else 1)
PY
```

Save the printed comparison alongside the logs when executing the plan.

### Step 5: Apply the approved decision

- If all seven checks pass, keep the default-on implementation and continue to
  Task 6.
- If checks 1, 2, 3, 5, 6, and 7 pass but `act2_at_least_1` fails, continue to
  the conditional Task 5.
- If progression or deck composition regresses, do not train. Set the code
  default to disabled before any merge, inspect the rejected offers on the
  failing fixed seeds, change exactly one threshold or premium exception,
  add a boundary regression test, and rerun Tasks 3 and 4 with a new evidence
  version. Do not make multiple simultaneous rule changes.

---

## Task 5: Conditionally run one natural-HP diverse boss fine-tune

Execute this task only when the static gate passes every criterion except Act 2
entry.

**Files:**

- Create runtime evidence only under ignored top-level `data/snapshots/`,
  `checkpoints/`, and `logs/`; do not commit it.

### Step 1: Build a diverse immutable Act 1 boss snapshot pool

Use both lanes so the combat learner is not fitted only to gated states:

```bash
POOL=../../data/snapshots/boss_gate_diverse_13955k_20260727_v1
test ! -e "$POOL"
mkdir -p "$POOL"
for src in "$BASE_SNAPS"/*.save; do
  cp "$src" "$POOL/base_$(basename "$src")"
done
for src in "$GATE_SNAPS"/*.save; do
  cp "$src" "$POOL/gate_$(basename "$src")"
done
test "$(find "$POOL" -name '*.save' | wc -l | tr -d ' ')" -ge 11
```

### Step 2: Run exactly 2,048 steps from `13955k`

```bash
TRAIN_OUT=../../checkpoints/act1_gate_13955_natural_20260727_v1
TRAIN_LOG=../../logs/train_act1_gate_13955_natural_2048_20260727_v1.log
test ! -e "$TRAIN_OUT"
mkdir -p "$TRAIN_OUT"
env \
  DECK_HISTORY_PATH= \
  STS2_CARD_QUALITY_GATE=1 \
  STS2_DECISION_ADVISOR=0 \
  STS2_PLANNER=0 \
  STS2_BOSS_PLANNER_MASK=0 \
  STS2_BOSS_READINESS_LIFT=0 \
  "$PY" -m agent.train \
    --profile act1-boss \
    --steps 2048 \
    --n-envs 4 \
    --checkpoint "$MODEL" \
    --load-save-dir "$POOL" \
    --mix-save-envs 2 \
    --hp-curriculum-values natural \
    --eval-freq 0 \
    --save-dir "$TRAIN_OUT" \
  2>&1 | tee "$TRAIN_LOG"
```

If the interactive runner cannot retain the process, launch this exact command
as a one-shot macOS LaunchAgent and continue polling the PID, newest
checkpoint, and log; do not start a duplicate trainer.

### Step 3: Verify the checkpoint artifact

```bash
TRAINED_MODEL="$(find "$TRAIN_OUT" -name 'ppo_ironclad_*.zip' -print | sort -V | tail -1)"
test -n "$TRAINED_MODEL"
"$PY" - "$TRAINED_MODEL" <<'PY'
import sys
from sb3_contrib import MaskablePPO

model = MaskablePPO.load(sys.argv[1], device="cpu")
print({"checkpoint": sys.argv[1], "num_timesteps": model.num_timesteps})
assert model.num_timesteps >= 13955000
PY
```

### Step 4: Evaluate the combined checkpoint with the same gate and seeds

Use new `gate1_trained` log, deck JSONL, and snapshot paths, but otherwise run
the exact candidate command from Task 4 with `"$TRAINED_MODEL"`.

Compare it against the Task 4 gate-off baseline with the same analysis script.
The combined checkpoint is promoted only if all seven criteria pass. Do not
substitute boss-retry wins for a full-run Act 2 entry.

---

## Task 6: Final verification, review, and promotion state

**Files:**

- Modify `agent/combat_env.py` only if promotion failed and the default must be
  disabled.
- Runtime evidence remains untracked.

### Step 1: Set the final switch default from measured evidence

If every promotion criterion passed, retain:

```python
flag = os.environ.get("STS2_CARD_QUALITY_GATE", "1").strip().lower()
```

If the final candidate failed any criterion, change the default to rollback:

```python
flag = os.environ.get("STS2_CARD_QUALITY_GATE", "0").strip().lower()
```

Update the default-state integration test to match the measured promotion
state. Never merge a failed candidate as silently default-on.

### Step 2: Run fresh verification

```bash
../../.venv/bin/python -m pytest \
  tests/agent/test_card_scoring.py \
  tests/agent/test_combat_env.py -q
../../.venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: all tests pass, no whitespace errors, and no runtime artifacts are
tracked.

### Step 3: Request a code review

Use the `requesting-code-review` skill against the diff from `f919338` to
`HEAD`. Address correctness findings before the final commit; do not change the
approved evaluation thresholds during review.

### Step 4: Commit any final promotion-state change

Only if Task 6 Step 1 changed tracked files:

```bash
git add agent/combat_env.py tests/agent/test_combat_env.py
git commit -m "fix(agent): align card gate default with evaluation"
```

### Step 5: Record the proof object

Report:

- exact checkpoint path and internal timestep;
- both fixed-20 log and boss-deck JSONL paths;
- valid/invalid counts, average floor, boss reach, and Act 2 entries;
- median boss-entry deck size, average operational quality, and average
  starter-basic count for both lanes;
- final feature-switch default;
- commits included in the branch.

Then use the `finishing-a-development-branch` skill to integrate only after the
measured candidate meets the approved promotion criteria.
