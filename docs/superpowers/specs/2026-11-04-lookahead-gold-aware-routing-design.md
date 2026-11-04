# Gold-aware lookahead routing (LookaheadMapStrategy v2) — design

Date: 2026-11-04. Status: approved by the user in chat (approach A: extend the existing lookahead).

## Why

Review of 88 real-game campaign runs (2026-11-02..04, `campaign_runs.jsonl` + the game's `.run` history +
`advisor_trace*.jsonl`; a9-10: 54 runs, 5 wins):

- HP on entering the boss separates passes from losses at a9-10: act 2 0.90 vs 0.58, act 3 0.88 vs 0.65.
- `map.la_best_path` predicted HP at the boss is calibrated in acts 1-2 (0.92 predicted vs 0.90-0.93 real) but
  not in act 3 (0.92 predicted, 0.71 real). The a8+ act-3 costs are low: monster 8.2 vs 10.7 real (survivors,
  n=105), elite 27.6 vs 34.3 (n=9).
- Characters differ a lot: survivors' damage relative to the all-character mean of the same (act, room type),
  a8+: Ironclad 1.70 (n=168), Defect 0.86 (130), Regent 0.89 (113), Necrobinder 0.76 (337), Silent 0.75 (20).
- Gold is not modelled beyond "a shop reached with >= 75-100 gold is worth 10 points, then gold = 0". Runs died
  holding 900-1146 gold (act-3 Ancient Signet Ring +888..1110, a treasure +632); at A3F2 with 1061 gold the
  planner took a shopless route over one with two shops. 13/47 act-3 boss entries held >= 300 gold.
- A shop visit spends ~70% of the gold walked in with (62-79% by band) and at 350+ gold ~340-380 per visit.

The user's requirements: plan the whole act from its start, consider candidate routes for rich and poor
cases, spend gold before the boss, re-plan every floor, adjust room weights dynamically, avoid elites at low
HP, prefer shops when rich. Whole map, every-route enumeration, re-planning at every fork and HP-dependent
elite risk already exist; this design adds what is missing.

## Scope

Phase 1 (this spec, Python only plus the panel strings): items 1-4 below.
Phase 2 (separate spec, after the debug-info session lands its mod-side room history): per-run dynamic cost
adjustment from the damage this run has actually taken, and showing candidate routes on the panel.

All phase-1 behaviour sits behind `STS2_LOOKAHEAD_MODEL` = `v1` (default; today's planner, bit-identical) |
`v2` (this design). Blank/unset = `v1`; anything else raises `ValueError`. It is added to
`GREEDY_BEHAVIOR_ENV` so experiment metadata records it. The default flips to `v2` only after a paired A/B
(see Validation) — the in-game advisor follows the same default (no `advisor_env_defaults` override).

## 1. Gold along the route (v2)

`evaluate_path` carries `gold` room by room.

- Gold gained per room (v2 table, a8+ real data, survivors' mean `gold_gained`; a treasure's 632-gold
  outlier is excluded): Monster 12, Elite 30, Treasure 40, Unknown 12 (acts 1-2) / 7 (act 3). v1 keeps
  `GOLD_GAIN` exactly.
- Shop: if `gold >= SHOP_MIN_GOLD` (= the v1 thresholds: `SHOP_GOLD` act 1, `SHOP_GOLD_ACT2` acts 2+),
  `spend = min(SHOP_SPEND_FRAC * gold, SHOP_SPEND_CAP)` with `SHOP_SPEND_FRAC = 0.70`,
  `SHOP_SPEND_CAP = 400`; `gold -= spend`; value `shop_value(spend) = 0.08 * min(spend, 150) +
  0.04 * max(spend - 150, 0)` points (150 gold ~ 12 points ~ one elite relic; 400 gold ~ 22). Each shop on
  the route is valued on its own, so a second shop is worth what the first left. Paid with the survival
  probability `p` like every other reward. Recorded as part `shop` (trace component `map.la_shop`).
- Leftover gold: at the end of the route, `p * LEFTOVER_GOLD_VALUE[act] * gold` with
  `LEFTOVER_GOLD_VALUE = (0.03, 0.03, 0.0)` (acts 1, 2, 3+). Part `gold_left` (`map.la_gold_left`). The rate
  is below every shop rate, so spending beats carrying; in act 3 carried gold is worth nothing.
- In v2 the shop is no longer in `room_reward`; v1 is unchanged.

Death risk (400 points x death probability) is untouched, so a shop behind a low-HP elite still loses.

## 2. Costs (v2)

- a8+ band, act 3: monster 10.7, elite 34.3 (acts 1-2 and the boss row unchanged). a6-7 unchanged.
- Character multiplier on monster and elite costs (not the boss), shrunk toward 1 by n/(n+50):
  IRONCLAD 1.54, SILENT 0.93, DEFECT 0.90, REGENT 0.92, NECROBINDER 0.79. Applied only in the a6-7 and a8+
  bands (the base band is the a5-A/B-validated table). Character from `state.player.name` ("The Ironclad",
  "The Silent", "The Defect", "The Regent", "The Necrobinder"; case-insensitive substring) or a
  `character` key if present; unknown = 1.0.
- The death curves are keyed on HP / cost, so a higher cost raises the risk automatically.

## 3. Candidate routes in the trace (v2)

`map.la_best_path` values gain `candidates`: the 3 best routes over all offered nodes (by value), each
`{path, first: [col,row], hp_boss_ratio, gold_spent, gold_left, elites, shops, value}`, and the chosen
route's `gold_spent`, `gold_left`, `shops`, `character_mult`, `model: "v2"`. Option `detail` gains the same
per-option fields. `PathEval` gains `gold_spent`, `gold_left`, `shops`.

`TRACE_KEYS` (agent/greedy_trace.py) and `src/Sts2Advisor.Core/TraceText.cs` get `map.la_shop`
("商店消费" / "Shop spending") and `map.la_gold_left` ("剩余金币" / "Gold left at the boss") and the
`decider` names `shop` / `gold_left`. TraceText.cs is edited only after the debug-info session has
committed (it edits the same file).

## 4. Headless ascension for the A/B

`python/play_full_run.py` `_route_via_greedy` attaches `ascension` (the run's `--ascension`) to the copy of
the map decision, as the mod does, so a headless a6+ lookahead arm uses its band. This changes existing
headless lookahead arms at a6+ (they wrongly used the base table) — intended; noted in the commit.

## Validation

- Unit tests (tests/agent/): v1 bit-identical to today on fixed maps; with 1000 gold a two-shop route beats a
  shopless one of equal risk; act-3 leftover gold earns 0, act-1 earns 0.03/gold; at low HP an act-3 shop route
  through an elite still loses to a safe shopless route; spend cap and fraction; character multiplier raises
  Ironclad's elite risk and lowers Necrobinder's; candidates list ordered and capped at 3; bad
  `STS2_LOOKAHEAD_MODEL` raises.
- `tests/agent/test_greedy_golden.py` passes unchanged (default v1; golden uses HpAware).
- CLAUDE.md regression (`run_batch.py --kind regression --games 5 --chars all`) at night.
- Paired A/B at night (22:00-08:00), a10, 5 characters, `STS2_ROUTE_POLICY=lookahead` both arms:
  `--arm v1:STS2_LOOKAHEAD_MODEL=v1 --arm v2:STS2_LOOKAHEAD_MODEL=v2`. Success = floors not worse and act-3
  boss entries with less gold; flip the default only on a positive or clearly non-negative result.

## Out of scope

Ancient/event relic scoring, Lord's Parasol, shop purchase logic itself, per-run dynamic costs (phase 2).
