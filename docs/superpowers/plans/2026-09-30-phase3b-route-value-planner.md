# Phase 3b — Holistic route planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Unlike the 3a plan, the pure-Python tasks here give exact interfaces, formulas and test cases rather than full implementation code (the user prefers implementation on a cheaper model; the design decisions are all fixed below). Where this plan and the spec disagree, this plan wins.

**Goal:** Choose map routes in `python/play_full_run.py` by a holistic state-value model (HP, deck, potions, relics, gold) fitted from Phase 3a game logs, and measure it against the seeded random route in a paired a1 A/B.

**Architecture:** Three pure modules under `agent/` — `route_data.py` (game log → per-room transitions + state features), `route_model.py` (fit/load a value model V(state) = expected remaining global floors, and an empirical per-room transition model), `route_planner.py` (enumerate every path to the boss, propagate expected state and death risk, pick the child whose best path scores highest) — plus a fitting CLI `agent/fit_route_model.py` and a `STS2_ROUTE_POLICY` switch in the harness. The fitted model is a small JSON committed at `agent/data/route_model_a1.json`.

**Tech Stack:** Python 3.11 (`.venv/bin/python`), numpy, scikit-learn 1.8 (fitting only; the planner loads plain JSON), pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-phase3-outofcombat-and-route-design.md` (Phase 3b). **Depends on:** Phase 3a merged (harness has `--keep-game-logs`, `--ascension`, `STS2_OOC_POLICY`); Tasks 6+ need the 3a A/B's kept greedy-arm logs.

---

## Background (read first)

- Game log format (`python/game_log.py`): JSONL; first line may be `{"type": "run_meta", "seed", "character", "ascension", "experiment", ...}`; then `{"step", "ts", "type": "action", "data": <command>}` and `{"step", "ts", "type": "state", "data": <engine reply>}` in send order. Engine replies include decisions (`data.type == "decision"`), errors (`type == "error"`), combat plans (`type == "combat_plan"`), and — in value-arm logs — maps (`type == "map"`). Only `type == "decision"` states are game states.
- A `map_select` decision: `data.choices = [{"col", "row", "type"}]` with `type` in `Monster | Elite | Unknown | Shop | RestSite | Treasure | Boss`; `data.context = {"act", "floor", "room_type", "boss": {"id", "name"}}` (floor is ACT-LOCAL); `data.player = {"hp", "max_hp", "gold", "deck": [card], "relics": [relic], "potions": [potion], ...}`. Card: `{"id", "name", "type", "cost", "upgraded", ...}`. Relic: `{"name", "description", "vars"}` (no id). Potion: `{"index", "id", "name", ...}`.
- The command that picks a node is the action `{"cmd": "action", "action": "select_map_node", "args": {"col", "row"}}`.
- `game_over` decision: `data.victory`, `data.act`, `data.floor` (act-local), `data.player`.
- Global floor everywhere = `(act - 1) * 17 + floor` (`python/play_full_run.py` `global_floor`).
- `get_map` (`{"cmd": "get_map"}`) is a read-only query: reply `{"type": "map", "rows": [[node]], "boss": {"col", "row", "type": "Boss", "id", "name"}, "current_coord": ..., "context": ...}`, node = `{"col", "row", "type", "children": [{"col","row"} or [col,row]], "visited", "current"}`. Calling it at a `map_select` decision does not change the pending decision (`agent/combat_env.py` does the same).
- Measured map facts: Act 1 rows 1..15 then boss row 16; Act 2 rows 0..14 then boss 15 (row 0 = act-opening event). Fixed rows (every node one type): A1 row 1 Monster, 9 Treasure, 15 RestSite; A2 row 1 Monster, 8 Treasure, 14 RestSite. An Act 1 map has 32–374 start→boss paths — enumerate them all.
- Deck strength: `agent/map_planner.py::_deck_strength(deck) -> (strength 0..1, size)` (wraps `card_scoring.deck_5turn_burst`). Reuse it; do not reimplement.
- Rules: `.venv/bin/python` always; scratch in `/tmp/sts2-cli/`; long runs under `~/.sts2-train/` wrapped in `scripts/run_caffeinated.sh`; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; `git add` only the task's files (never `-A`).

## File structure

- Create `agent/route_data.py` — `state_features`, `extract_transitions`, `load_games`.
- Create `agent/route_model.py` — `RouteModel` (JSON load/save, `value`, `step`, `death_prob`), `fit_value_model`, `fit_transition_model`, `death_prob_from_samples`.
- Create `agent/route_planner.py` — `fixed_rows`, `enumerate_paths`, `score_path`, `choose_node`.
- Create `agent/fit_route_model.py` — CLI: logs → `route_model.json` + validation report.
- Create `agent/data/route_model_a1.json` (Task 6, fitted artefact).
- Modify `python/play_full_run.py` — `STS2_ROUTE_POLICY`, `STS2_ROUTE_MODEL`, map_select branch, counters, summary.
- Tests: `tests/test_route_data.py`, `tests/test_route_model.py`, `tests/test_route_planner.py`, `tests/test_route_policy.py`.

---

### Task 1: `agent/route_data.py` — features and transitions from game logs

**Interfaces:**

```python
FEATURE_KEYS = ("hp", "max_hp", "gold", "deck_strength", "deck_size", "n_upgraded",
                "n_relics", "n_potions")

def state_features(state: dict) -> dict:
    """Numeric features of one decision state (map_select or game_over).
    Returns FEATURE_KEYS plus "act" (int, default 1), "floor" (act-local int, default 0),
    "gf" (global floor = (act-1)*17 + floor). deck_strength via map_planner._deck_strength;
    n_upgraded = cards with upgraded true; n_potions counts non-null potion entries."""

def extract_transitions(rows: list[dict]) -> dict | None:
    """One game log (already json-parsed lines) -> {
         "character", "ascension", "seed",        # from run_meta (None if absent)
         "won": bool, "final_gf": int,
         "snapshots": [ {"features": {...}, "choice_type": str} ],   # every map_select at which a
                                                                     # select_map_node was sent
         "transitions": [ {"act", "room_type", "pre": {...}, "post": {...} | None, "died": bool} ]
       }
    or None if the log has no game_over decision (crash / hang / truncated: excluded, like paired_eval)."""

def load_games(paths: list[str]) -> list[dict]:
    """extract_transitions over files; skips None; never raises on one bad file (prints a warning)."""
```

**Semantics (must hold):**
- Walk entries in order. Track `last_map_select` (the most recent `map_select` decision state). On an action `select_map_node` with (col,row): the chosen type is the `choices` entry with that col,row in `last_map_select` (if missing: skip this move). That state is the move's `pre`.
- The move's `post` is the next decision state that is a `map_select` (or `game_over`); intermediate decisions (combat, rewards, events, rest, shop) are inside the room.
- If a `game_over` with `victory == false` arrives before the next `map_select`: `died = True`, `post = state_features(game_over)` (its hp is 0).
- If the next `map_select` is in a different act than `pre` (boss moves), keep the transition with `room_type = "Boss"`; the model ignores Boss transitions but the analysis uses them.
- A `map_select` that repeats without an intervening `select_map_node` just replaces `last_map_select`.
- `snapshots` get one entry per `select_map_node`, with the pre features and the chosen type.
- `final_gf` = global floor of the `game_over` state; `won` = its `victory`.

**Tests (`tests/test_route_data.py`)** — build tiny synthetic logs in the test (helper `_ms(act, floor, hp, choices, deck=..., relics=..., potions=...)`, `_act(col,row)`, `_over(act, floor, victory)`):
1. `state_features` on a state with deck `[{"id":"A","type":"Attack","upgraded":True}, {"id":"B","type":"Skill","upgraded":False}]`, 2 relics, potions `[{"id":"P"}, None]` → deck_size 2, n_upgraded 1, n_relics 2, n_potions 1, gf for act 2 floor 3 == 20.
2. Two moves then death: ms(A1F1, hp 80) → select (1,1) Monster → ms(A1F2, hp 70) → select (1,2) Elite → game_over(A1F2, victory false). Transitions: [Monster pre.hp 80 post.hp 70 died False], [Elite pre.hp 70 died True post.hp 0]; final_gf 2; won False; 2 snapshots with choice types Monster, Elite.
3. Act crossing: ms(A1F16) select Boss → event_choice decision (A2) → ms(A2F1) → transition room_type "Boss", post act 2.
4. Log without game_over → `extract_transitions` returns None; `load_games` skips it.
5. Non-decision state entries (`{"type":"map"}`, `{"type":"combat_plan"}`, `{"type":"error"}`) between entries are ignored.
6. Repeated map_select (error in between) → the later one is the `pre`.

Commit: `feat(route): extract per-room transitions and state features from game logs`.

---

### Task 2: `agent/route_model.py` — value model, transition model, JSON

**Value model.** Samples = every snapshot of every game in `load_games` output (only games with final_gf). Target `y = final_gf - features["gf"]`. Two feature sets (constants in the module):

```python
FULL_FEATURES = ["hp", "max_hp", "hp_frac", "gold", "deck_strength", "deck_size", "n_upgraded",
                 "n_relics", "n_potions", "floor", "act2", "act3",
                 "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]
HP_ONLY_FEATURES = ["hp", "max_hp", "hp_frac", "floor", "act2", "act3",
                    "char_Ironclad", "char_Silent", "char_Defect", "char_Regent", "char_Necrobinder"]
```

`hp_frac = hp / max(max_hp, 1)`; `act2 = act == 2`, `act3 = act >= 3`; character one-hot from the game's character.

```python
def feature_vector(features: dict, character: str, names: list[str]) -> list[float]

def fit_value_model(games: list[dict], alpha: float = 1.0, folds: int = 5, seed: int = 0) -> dict:
    """StandardScaler + Ridge(alpha) on FULL and on HP_ONLY. GroupKFold by game (group = index of the
    game), `folds` folds (min(folds, n_games)). Returns {
      "chosen": "full" | "hp_only",   # full only if its mean CV MAE < hp_only's AND it wins on
                                      # >= ceil(folds/2) folds; else hp_only
      "cv": {"full": [mae per fold], "hp_only": [...]},
      "model": {"features": names, "mean": [...], "scale": [...], "coef": [...], "intercept": float},
      "n_games": int, "n_samples": int }
    The returned "model" is the chosen set refit on ALL samples."""
```

**Transition model.** From all transitions whose `room_type` is not Boss:

- Cell key `f"{act}|{room_type}|{bucket}"`. `bucket`: for Monster and Elite, `"lo"`/`"hi"` by `pre.deck_strength < split` where `split` = median `deck_strength` over all Monster pre states; for RestSite, `"lo"`/`"hi"` by `pre hp_frac < 0.5`; otherwise `"all"`.
- Per cell store: `n`, `n_died`; `mean_delta` over SURVIVING samples for each of `hp, max_hp, gold, deck_strength, deck_size, n_upgraded, n_relics, n_potions` (0.0 if no survivors); `losses` = sorted list of `max(0, pre.hp - post.hp)` for survivors; `death_hp` = sorted list of `pre.hp` for deaths.
- Fallback when a cell has `n < min_n` (default 20): `f"*|{room_type}|{bucket}"` (pooled over acts) → `f"*|{room_type}|all"` → a zero cell (no delta, no death risk). Build the pooled cells at fit time.

```python
def death_prob_from_samples(losses: list[int], death_hp: list[int], hp: float) -> float:
    """Censoring-aware P(loss >= hp): a death at pre-hp d means loss >= d, so it is informative only
    for thresholds hp <= d. Estimate = (#losses >= hp + #death_hp >= hp) / (#losses + #death_hp >= hp);
    0.0 if the denominator is 0. hp <= 0 -> 1.0."""

def fit_transition_model(games: list[dict], min_n: int = 20) -> dict   # {"split", "min_n", "cells": {...}}
```

**RouteModel** (plain-JSON runtime object; no sklearn at runtime):

```python
class RouteModel:
    def __init__(self, data: dict)            # {"version": 1, "value": <model dict>, "transitions": <dict>,
                                              #  "meta": {...}, "validation": {...}}
    @classmethod
    def load(cls, path: str) -> "RouteModel"
    def save(self, path: str) -> None          # json, indent 1, sorted keys
    def value(self, features: dict, character: str) -> float           # ridge prediction, >= 0
    def cell(self, act: int, room_type: str, features: dict) -> dict    # with the fallback chain
    def death_prob(self, act: int, room_type: str, features: dict) -> float
    def step(self, act: int, room_type: str, features: dict) -> dict:
        """Expected features after SURVIVING the room: add the cell's mean_delta; clamp hp to
        [1, max_hp], max_hp >= 1, gold >= 0, deck_strength to [0, 1], counts >= 0, n_potions <= 3;
        floor += 1, gf += 1; act unchanged."""
```

**Tests (`tests/test_route_model.py`)** — synthetic games built with Task 1's shapes (write a helper that emits `load_games`-style dicts directly):
1. `death_prob_from_samples([0, 5, 10], [], 6)` == 1/3; `([0, 5], [8], 6)` == 2/3 (the death at 8 counts); `([0, 5], [3], 6)` == 0.0 (death at 3 uninformative for hp 6, denominator 2, numerator 0); `hp=0` → 1.0; all empty → 0.0.
2. `fit_value_model` on synthetic data where `y = 30 - floor + 0.3*deck_strength*100` (deck matters, noise small, 40 games): chosen == "full" and its mean CV MAE < hp_only's. On data where y depends only on hp and floor: chosen == "hp_only" (full does not win on a majority of folds or ties).
3. Round-trip: `RouteModel(data).save(p)`; `RouteModel.load(p).value(f, "Defect")` equals the pre-save value to 1e-9.
4. Cell fallback: a model with only `*|Elite|hi` present returns that cell for `(2, "Elite", hi-features)`; unknown room type returns the zero cell (death_prob 0, step changes only floor/gf).
5. `step` clamps: hp + delta above max_hp → max_hp; deck_strength stays in [0,1]; n_potions capped at 3.
6. `fit_transition_model` puts RestSite samples with pre hp_frac 0.3 in `|lo` and 0.8 in `|hi`; Monster split uses the Monster median.

Commit: `feat(route): value model (ridge, grouped CV vs HP-only) and empirical transition model`.

---

### Task 3: `agent/route_planner.py` — enumerate and score paths

```python
def node_key(node_or_child) -> tuple[int, int]      # accepts {"col","row"} or [col,row]
def fixed_rows(map_json: dict) -> dict[int, str]     # rows (>=1 node) whose nodes all share one type
def enumerate_paths(map_json: dict, start: tuple[int, int]) -> list[list[dict]]:
    """Every path of map nodes from `start` (inclusive) following `children` until a node with no
    children inside `rows` (the boss is not in `rows`). Depth-first, children in (col,row) order.
    Raises ValueError if more than 20000 paths (not expected: measured max 374 per map)."""

def score_path(path: list[dict], features: dict, character: str, model: "RouteModel",
               checkpoint_rows: frozenset[int] = frozenset()) -> dict:
    """Walk the path from the current state `features` (the map_select state's features):
        p_alive = 1.0; death_term = 0.0; s = dict(features)
        for k, node in enumerate(path, 1):
            q = model.death_prob(s["act"], node["type"], s)
            death_term += p_alive * q * (features["gf"] + k)
            p_alive *= (1 - q)
            s = model.step(s["act"], node["type"], s)
        score = death_term + p_alive * (s["gf"] + model.value(s, character))
    Returns {"score", "p_alive", "end": s, "checkpoints": {row: s right after stepping the node on
    that row, for every node whose row is in checkpoint_rows}}. choose_node passes
    frozenset(fixed_rows(map_json))."""

def choose_node(map_json: dict, state: dict, character: str, model) -> dict:
    """Among state["choices"] (only those present as nodes in map_json["rows"]), return
    {"col", "row", "score", "p_alive", "paths": n_paths_scored,
     "checkpoints": {str(row): {"hp", "max_hp", "deck_strength", "n_relics", "n_potions", "gold"}}}
    for the choice whose best path has the highest score. Ties: lower col, then lower row.
    A choice with type "Boss" (or no enumerable path) is scored as the empty path (score = gf + V)."""
```

**Tests (`tests/test_route_planner.py`)** — hand-built maps and a hand-built `RouteModel` (construct its data dict directly: value model with features `["hp"]`, coef chosen so V = hp/10; transition cells chosen per test):
1. `fixed_rows` on a 3-row map where row 2 is all Treasure → `{2: "Treasure"}` (plus any other uniform rows).
2. `enumerate_paths` on a diamond (1 → {2a, 2b} → 3) → 2 paths; children given as `[col,row]` lists also work.
3. Choice between a path with a Monster (death_prob 0, Δhp −10) and a path with a RestSite (Δhp +20): with V = hp/10 the RestSite child wins.
4. Holistic trade-off: Elite (Δhp −15, Δn_relics +1) vs Monster (Δhp −5); value model with coef on n_relics = 3.0 and hp = 0.1 → Elite path wins; with coef on n_relics = 0.5 → Monster wins.
5. Death risk dominates: a path through a cell whose death_prob is 0.9 at the current hp loses to a safe path even if its end value is higher.
6. Tie → lower col.
7. `choose_node` returns checkpoints keyed by the fixed rows the chosen path crosses.

Commit: `feat(route): path enumeration and expected-value route choice`.

---

### Task 4: `agent/fit_route_model.py` — CLI

```
.venv/bin/python -m agent.fit_route_model --logs DIR_OR_GLOB [--logs ...] --ascension 1 \
    --out agent/data/route_model_a1.json [--alpha 1.0] [--min-n 20]
```

- Collect `*.jsonl` under each `--logs` (dir → all jsonl inside; else glob). Keep games whose run_meta ascension equals `--ascension` (drop others, print the count dropped).
- Fit value + transitions; save `RouteModel`; `meta` = {"n_games", "n_transitions", "characters": {name: n}, "ascension", "sources": [...], "fitted_at": iso date}; `validation` = the fit's `cv` + chosen.
- Print a report: games per character; CV MAE mean ± se for full and hp_only and the chosen set; the chosen coefficients as a table (feature, coef in target units per 1 SD); cell table (key, n, n_died, mean Δhp, mean Δrelics, mean Δdeck_strength) for Act 1 and Act 2 cells.
- Test (`tests/test_route_model.py::test_cli_end_to_end`): write 3 synthetic game logs (JSONL with run_meta ascension 1) and 1 with ascension 0 into tmp_path; run `main([...])`; the output JSON loads with `RouteModel.load`, `meta.n_games == 3`.

Commit: `feat(route): fit_route_model CLI with validation report`.

---

### Task 5: harness wiring — `STS2_ROUTE_POLICY`

In `python/play_full_run.py`:

- `ROUTE_POLICIES = ("random", "value")`, default `"random"`; `route_policy(env=None)` like `ooc_policy` (blank → default; unknown raises ValueError mentioning `STS2_ROUTE_POLICY`).
- `ROUTE_MODEL_DEFAULT = <repo>/agent/data/route_model_a1.json`; `route_model_path(env=None)` = `STS2_ROUTE_MODEL` or the default.
- `_load_route_model(path)` (cached per path) imports `agent.route_model.RouteModel` lazily (repo root on sys.path, like `_load_greedy_action`).
- `main()`: under `value`, load the model once before any game (fail fast) and print `Route policy: value (model <path>, n_games <meta.n_games>, value <chosen>)`.
- `play_run` wrapper: stats gain `route_policy`, `route_plans`, `route_fallbacks`; stamped on results and in `result_to_eval_row`.
- `_play_run` map_select branch: **under `random` the existing code runs unchanged** (the random arm must stay bit-identical). Under `value`:
  ```
  reply = send({"cmd": "get_map"})           # do NOT assign to `state`
  if reply.get("type") == "map":
      try: pick = route_planner.choose_node(reply, state, character, model)
      except Exception as exc: print("!! route planner raised ..." + traceback first time); pick = None
  else: pick = None
  if pick is None or (pick["col"], pick["row"]) not in {(c["col"], c["row"]) for c in choices}:
      route_fallbacks += 1; choice = rng.choice(choices)
  else:
      route_plans += 1; choice = pick; print("  [route] " + json.dumps({act, floor, choice, score, p_alive, checkpoints}))
  send select_map_node as before
  ```
- `summarize`: when any result has `route_policy`, print `Route policy: <p> -- N planned, M fallbacks`; banner `!! ROUTE PLANNER NEVER ENGAGED` if the policy is value and N == 0; banner `!! route planner fell back M times` if M > 0 under value.

**Tests (`tests/test_route_policy.py`)**, fake engine + a stub model/planner through a seam `play_full_run._load_route_model` and monkeypatching `agent.route_planner.choose_node` (or a seam `_route_choose`):
1. `route_policy` default/blank/case/unknown-raises.
2. Under random (unset), no `get_map` is ever sent and `_load_route_model` is never called (monkeypatch it to raise); `tests/test_map_route_determinism.py` must still pass unchanged (it pins the random route sequence per seed).
3. Under value, `get_map` is sent at each map_select and the planner's pick is the `select_map_node` sent; `route_plans` counts it.
4. Planner raises / returns a node not in `choices` / `get_map` reply not a map → rng fallback, `route_fallbacks` counts it, run continues.
5. The map reply does not replace `state`: the next command after the map is `select_map_node` with the pick.
6. Summary line and both banners.

Commit: `feat(play_full_run): STS2_ROUTE_POLICY=value routes by the fitted route model`.

---

### Task 6: fit on the Phase 3a data (after the 3a A/B finishes)

```bash
cd /Users/bytedance/mygit/sts2-cli
AB=$(cat ~/.sts2-train/last_phase3a_ab_dir.txt)
.venv/bin/python -m agent.fit_route_model --logs "$AB/games_greedy" --ascension 1 \
    --out agent/data/route_model_a1.json | tee "$AB/route_model_fit_report.txt"
```

Record in this file under Task 6: games per character, CV MAE full vs hp_only, the chosen set, the coefficient table, and the Act 1 cell table. **Sanity gates** (stop and report to the controller if any fails): value model predicts non-negative remaining floors on every training snapshot; Elite Act 1 cells have mean Δn_relics > 0.5; RestSite `lo` cells have mean Δhp > 0; Monster cells have mean Δhp < 0.

Commit `agent/data/route_model_a1.json` + this file: `feat(route): route model fitted on Phase 3a a1 greedy games`.

### Task 7: smoke + regression (value arm)

Smoke: 2 games × 5 characters, `STS2_OOC_POLICY=greedy STS2_ROUTE_POLICY=value --ascension 1 --seed-prefix p3b_smoke_`, `STS2_SOLVER_THREADS=3`, 5 lanes; expect Completed 2/2, `Route policy: value -- N planned, 0 fallbacks`, `[route]` lines present. Then the CLAUDE.md regression with `STS2_ROUTE_POLICY=value STS2_OOC_POLICY=greedy` (5 × 5, Completed 5/5 each). Record both here.

### Task 8: paired A/B and verdict

Arms: `random` vs `value`, both `STS2_OOC_POLICY=greedy`, `--ascension 1 --seed-prefix p3b_`, 40 seeds × 5 characters, 5 lanes × `STS2_SOLVER_THREADS=3`, `--keep-game-logs "$AB/games_${arm}"`, results `$AB/${char}_${arm}.jsonl` (same launcher shape as Phase 3a Task 7). Report per character: paired global-floor diff, t, p, A1/A2 boss pass rates, wins, route fallbacks. Also a **checkpoint drift** table from the value arm's `[route]` lines: predicted vs actual hp / relics / deck_strength at the Treasure and RestSite rows (mean error, mean absolute error). Verdict: ship `value` as the default if the mean paired diff across characters is > 0 and no character is significantly worse (p < 0.05); otherwise keep `random` and report. Update `CLAUDE.md` Protocol notes with `STS2_ROUTE_POLICY` / `STS2_ROUTE_MODEL` and the verdict.
