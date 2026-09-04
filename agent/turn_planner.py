#!/usr/bin/env python3
"""turn_planner.py — Exhaustive 1-turn lookahead planner for real combat.

Strategy (Jun 11): replace the PPO policy's combat decisions with search.
At every combat_play decision:
  1. Build an exact CombatState from the observed JSON (real hand, real
     enemy intents — the game TELEGRAPHS what each enemy does this turn).
  2. DFS-enumerate legal card sequences within the energy budget.
  3. For each sequence: clone → play cards in sim → run sim's end_turn()
     (replays enemy attacks hit-by-hit with block absorption, strength,
     weak/vulnerable — exactly the tested sim math).
  4. Score = HP after enemy turn (dominant) + damage/kill/energy tiebreaks.
  5. Emit the FIRST action of the best sequence as an env action int.
     Re-plan every step (handles draws/discard shifts organically).

Action encoding (state_encoder.py): int k → hand_slot=k//4, target=k%4
(0-2 = enemy index, 3 = untargeted), 40 = end_turn.

Planner falls back to None (→ caller uses model.predict) when the hand has
no sim-known cards or anything fails. BROKEN_CARDS are never played.
"""
from __future__ import annotations

import os
import random
import re
import time

from agent.sim.combat_state import CombatState, Enemy
from agent.sim.combat_step import play_card, end_turn, get_card_data
from agent.card_scoring import BROKEN_CARDS, _card_id_norm

# Status cards that damage you at end of turn while held. Their text is fixed
# ("At the end of your turn, if this is in your Hand, take N damage"), so they
# do not need advisor vars to model.
_BURN_IN_HAND_CARDS = {"BURN", "INFECTION"}
_BURN_DAMAGE = 2

MAX_SEQUENCES = 500       # search budget per decision
DEADLINE_SEC = 0.8        # wall-clock budget per decision
NO_TARGET_SLOT = 3
END_TURN_ACTION = 40


def defense_override_enabled() -> bool:
    """Shared STS2_DEFENSE flag for intent_defense_override, read by both
    eval_rl.py's eval loop and rl_agent.py's live-play wrapper so the two
    paths cannot drift. Default ON (Aug 2026): confirmation run on 240 fixed
    seeds (ppo_defect_2248k, eval_fixed_3000..3239) paired STS2_DEFENSE=1 +
    STS2_ELITE_PREF=24 against baseline and found elite HP/fight 29.1 -> 24.2
    (paired -3.44 HP, se 1.03, p=0.0010) with elite fights 231 -> 358 and
    avg_floor 17.50 -> 18.14 — cheaper AND more fights, not fewer. Opt out
    with STS2_DEFENSE=0/false/off; any other value (including unset) keeps
    it on."""
    flag = os.environ.get("STS2_DEFENSE", "1").strip().lower()
    return flag not in {"0", "false", "off"}


# Hallway (non-elite, non-boss) block trigger for intent_defense_override.
#
# WAS max(12.0, 0.18*max_hp) until 2026-09-03. The override's original numbers
# were measured on ELITE fights only (see defense_override_enabled: elite
# HP/fight 29.1 -> 24.2). The hallway arm was never validated — and it is the
# arm carrying most of a run's HP cost. Ironclad at a1 loses 15.1 HP/normal
# fight over ~6 Act 1 hallway rooms (~90 HP against an 87 HP pool) versus ~42 HP
# for the single elite it may fight, and 97% of its runs die in Act 1 (n=2779,
# mean floor 10.3). Yet at max_hp 87 the old bar was 15.66 while the MEDIAN
# hallway attack in 151 recorded a1 combat_play states is 11 — so the override
# sat out the median hallway fight, i.e. exactly where the HP goes.
#
# Ironclad a1, 240 paired seeds/arm, dose-response with a turning point:
#   0.18/12 (old)  floor 10.537   hallway HP/fight 16.55   (baseline)
#   0.10/7         floor +0.133 (p=0.53)      15.39
#   0.06/5 (new)   floor +0.454 (p=0.031)     14.22
#   0.03/3         floor +0.500 (p=0.057)     13.94
#   0.00/0         floor -0.582 (p=0.0089)    16.05  <- always-block is WORSE
# The 0.00 arm is the guard rail: blocking on every incoming attack barely helps
# hallway HP (-0.48, p=0.084) because fights drag and total damage taken rises.
# 0.03 and 0.06 are statistically indistinguishable; 0.06 has the tighter floor p.
#
# Confirmed on all five characters at 240 paired seeds each before shipping,
# because the elite-pref default in map_planner.py was validated on Defect alone
# and made global, and ab_elite_chars had to go back and check the rest:
#   character    floor base -> d06     diff       p        hallway HP/fight
#   Ironclad     10.537 -> 10.992    +0.454   0.031     16.55 -> 14.22
#   Silent       11.688 -> 12.165    +0.477   0.043     12.60 -> 10.32
#   Necrobinder  11.556 -> 12.343    +0.787   0.021     10.74 ->  9.28
#   Regent       13.561 -> 14.418    +0.857   0.013     13.09 -> 11.62
#   Defect       19.449 -> 20.915    +1.466   0.0079    11.28 ->  9.55
# 5/5 positive on floor, 5/5 on combat_wins (all p<=0.01), and the mechanism
# metric (monster_hp_loss_per_fight) improves in all five at p<1e-4.
#
# Two honest caveats. (1) elite_hp_loss_per_fight and boss_hp_loss_per_fight
# RISE in most arms (e.g. Ironclad +3.67/+8.09). That is the entry-HP ceiling
# artifact documented in sts2-combat-hp-loss-metric — more marginal runs now
# survive to reach those rooms, and you cannot lose HP you never had — not
# evidence the override hurts elites, whose branch is untouched here. (2) The
# single Act 3 win in Defect's base arm did not recur in d06 (1/236 vs 0/236,
# p=0.32). At n=1 that is one seed's worth of noise in both directions, not a
# regression signal; Defect's *depth* gained 1.47 floors.
_HALLWAY_DANGER_FRAC = 0.06
_HALLWAY_DANGER_FLOOR = 5.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def hallway_danger_threshold(max_hp: float) -> float:
    """Unblocked damage at/above which the override blocks in a hallway fight."""
    return max(_env_float("STS2_DANGER_FLOOR", _HALLWAY_DANGER_FLOOR),
               _env_float("STS2_DANGER_FRAC", _HALLWAY_DANGER_FRAC) * max_hp)


# Elite/boss arm of the same rule. Unlike the hallway arm this one WAS measured
# (elite HP/fight 29.1 -> 24.2) — but only on Defect, which has the cheapest
# elites in the roster. Post-hallway-fix elite HP/fight is Defect 29.4 against
# Ironclad 40.3, Necrobinder 36.8, Silent 35.1, Regent 34.5, and 47-62% of runs
# fight one, so the elite room is now the single largest HP line item for the
# four characters that cannot clear. Knobs mirror the hallway pair so the same
# bracketed sweep can be run; defaults reproduce the shipped max(6.0, 0.10*hp).
_ELITE_DANGER_FRAC = 0.10
_ELITE_DANGER_FLOOR = 6.0


def elite_danger_threshold(max_hp: float) -> float:
    """Unblocked damage at/above which the override blocks in an elite/boss fight."""
    return max(_env_float("STS2_ELITE_DANGER_FLOOR", _ELITE_DANGER_FLOOR),
               _env_float("STS2_ELITE_DANGER_FRAC", _ELITE_DANGER_FRAC) * max_hp)


# Status names as they appear in player_powers / enemy powers JSON → sim names
_STATUS_MAP = {
    "strength": "Strength", "dexterity": "Dexterity",
    "vulnerable": "Vulnerable", "weak": "Weak", "frail": "Frail",
    "thorns": "Thorns", "metallicize": "Metallicize",
    "plated armor": "PlatedArmor", "plating": "PlatedArmor",
    "barricade": "Barricade", "intangible": "Intangible",
    "ritual": "Ritual",
}


def _norm_status(name: str) -> str:
    n = (name or "").strip().lower()
    return _STATUS_MAP.get(n, name)


def _powers_to_statuses(powers) -> dict:
    out = {}
    for p in powers or []:
        name = p.get("name")
        if isinstance(name, dict):
            name = name.get("en", "")
        amt = p.get("amount", 0)
        if name and isinstance(amt, (int, float)) and amt != 0:
            out[_norm_status(str(name))] = int(amt)
    return out


# 100 / 200 / 300 at a1 — the 111/212/313 variants need AscensionLevel
# ToughEnemies, which is ascension 8, not 1.
_TEST_SUBJECT_FORM_HP = (100, 200, 300)


def _is_test_subject(state: dict, enemy: Enemy) -> bool:
    boss = ((state.get("context") or {}).get("boss") or {})
    boss_id = str(boss.get("id") if isinstance(boss, dict) else boss or "").upper()
    return "TEST_SUBJECT" in boss_id or "TEST_SUBJECT" in (enemy.id or "").upper()


def _test_subject_remaining_hp(enemy: Enemy) -> int:
    """Current form HP plus every form not yet fought.

    Which form we are on is inferred from max_hp: the engine sets it to the
    form's own pool on each revive, so max_hp identifies the form without the
    JSON having to expose a respawn counter.
    """
    try:
        idx = _TEST_SUBJECT_FORM_HP.index(int(enemy.max_hp))
    except ValueError:
        return int(enemy.hp)
    return int(enemy.hp) + sum(_TEST_SUBJECT_FORM_HP[idx + 1:])


def build_sim_state(state: dict) -> tuple[CombatState | None, list[dict]]:
    """Construct an exact CombatState from a combat_play JSON state.

    Returns (sim_state, hand_meta) where hand_meta[i] mirrors sim hand slot i:
    {"slot": json_hand_slot, "id": normalized_id, "target_type": str,
     "cost": resolved_int, "known": bool}.
    Unknown/broken cards are EXCLUDED from the sim hand (they'd corrupt
    search); their slots simply can't be chosen by the planner.
    """
    if not state or state.get("decision") != "combat_play":
        return None, []
    player = state.get("player") or {}
    s = CombatState(
        hp=int(player.get("hp", 0) or 0),
        max_hp=int(player.get("max_hp", 80) or 80),
        block=int(player.get("block", 0) or 0),
        energy=int(state.get("energy", 0) or 0),
        max_energy=int(state.get("max_energy", 3) or 3),
        floor=int((state.get("context") or {}).get("floor", 1) or 1),
        rng_seed=1234,
    )
    s.statuses = _powers_to_statuses(state.get("player_powers"))
    # Burn-style status cards are absent from the sim card DB (build_card_db
    # iterates the advisor DB, which holds only draftable cards), so they get
    # dropped from the sim hand and their end-of-turn damage is invisible to
    # the planner. Count them so end_turn can charge for them: a1 Defect decks
    # accumulate a lot of these, and missing the damage makes the planner
    # under-estimate incoming damage exactly when it matters.
    _burns = 0
    for _c in (state.get("hand") or []):
        if _card_id_norm(_c) in _BURN_IN_HAND_CARDS:
            _burns += 1
    if _burns:
        s.statuses["_burn_in_hand"] = _burns
    # Relics: names only in JSON; map a few key ones by name match
    relic_names = []
    for r in player.get("relics") or []:
        nm = r.get("name")
        if isinstance(nm, dict):
            nm = nm.get("en", "")
        if nm:
            relic_names.append(str(nm).upper().replace(" ", "_").replace("-", "_"))
    s.relics = relic_names

    # intent_forecast (Task 2a): C#-side multi-turn lookahead, grouped by
    # round then filtered per enemy_index below using the SAME "first attack
    # intent, else zeroed debuff" reduction as the current-round intent above
    # — same normalized {"type","damage","hits"} shape, just one round later
    # each time. Stop at the first round with no entries for an enemy: that
    # signals the C# side couldn't (or didn't) forecast any further for it
    # (see "unsupported"), and a hole mid-list must not be misread as "does
    # nothing" — it must instead make the queue run out so
    # _advance_enemy_intents falls back to the wiki-scraped state machine,
    # exactly like an absent forecast would.
    #
    # Round 0 is deliberately skipped here: RunSimulator.cs builds it from
    # owner.Monster?.NextMove, which is BY CONSTRUCTION identical to the
    # per-enemy "intents" field already consumed above (round 0 exists on the
    # wire as a self-check, matching the reference implementation — see
    # RunSimulator.cs ~2581). Queuing it as forecast[0] would make
    # _advance_enemy_intents (combat_step.py) replay the move an enemy just
    # used instead of its actual next one — every later round would then be
    # one turn stale. Enemy.intent_forecast's contract (combat_state.py:42)
    # is explicit that [0] is the intent AFTER the upcoming one. Slicing
    # (rather than enumerate-and-skip) also means a short/absent forecast
    # ([] or a single round 0 only) degrades to an empty queue, not an
    # index error.
    forecast_rounds = ((state.get("intent_forecast") or {}).get("rounds") or [])[1:]

    # Enemies — in JSON order so target indices align
    for idx, e in enumerate(state.get("enemies") or []):
        intents = e.get("intents") or []
        atk = next((it for it in intents
                    if (it.get("type") or "").lower() == "attack"), None)
        if atk:
            intent = {"type": "attack",
                      "damage": int(atk.get("damage", 0) or 0),
                      "hits": int(atk.get("hits", 1) or 1)}
        else:
            intent = {"type": "debuff", "damage": 0, "hits": 0}
        name = e.get("name")
        if isinstance(name, dict):
            name = name.get("en", "")
        en = Enemy(
            id=str(name or "ENEMY").upper().replace(" ", "_"),
            name=str(name or ""),
            hp=int(e.get("hp", 1) or 1),
            max_hp=int(e.get("max_hp", 1) or 1),
            block=int(e.get("block", 0) or 0),
            intent=intent,
        )
        en.statuses = _powers_to_statuses(e.get("powers"))
        # Test Subject (Act 3 boss) is a three-form RESPAWN gauntlet:
        # DeadState = RESPAWN_MOVE, reviving at SecondFormHp then ThirdFormHp,
        # only truly dead at Respawns >= 2 (decompiled from lib/sts2.dll).
        # The sim has no respawn concept — combat_over() is true once every
        # enemy is at 0 HP — so it believes the fight ends when form 1 dies and
        # any rollout "wins" after ~100 damage. Folding the unfought forms into
        # the HP pool is an approximation (it does not model the per-respawn
        # PainfulStabs/Nemesis powers or the escalating Multi-Claw) but it at
        # least stops search planning against a fight 6x shorter than the real
        # one. See _TEST_SUBJECT_FORM_HP.
        if _is_test_subject(state, en):
            remaining = _test_subject_remaining_hp(en)
            if remaining > en.hp:
                en.hp = remaining
                en.max_hp = max(en.max_hp, remaining)
        enemy_index = e.get("index", idx)
        forecast: list[dict] = []
        for round_entries in forecast_rounds:
            matches = [it for it in (round_entries or [])
                       if it.get("enemy_index") == enemy_index]
            if not matches:
                break
            fatk = next((it for it in matches
                         if (it.get("type") or "").lower() == "attack"), None)
            if fatk:
                forecast.append({"type": "attack",
                                  "damage": int(fatk.get("damage", 0) or 0),
                                  "hits": int(fatk.get("hits", 1) or 1)})
            else:
                forecast.append({"type": "debuff", "damage": 0, "hits": 0})
        en.intent_forecast = forecast
        out_hp = en.hp
        if out_hp > 0:
            s.enemies.append(en)

    # Hand — only sim-known, playable, non-broken cards enter the sim hand.
    hand_meta: list[dict] = []
    for slot, c in enumerate(state.get("hand") or []):
        cid = _card_id_norm(c)
        known = (get_card_data(cid) is not None and cid not in BROKEN_CARDS
                 and bool(c.get("can_play", True)))
        cost_raw = c.get("cost")
        try:
            cost = int(cost_raw)
        except (TypeError, ValueError):
            cost = 1
        meta = {"slot": slot, "id": cid, "cost": cost,
                "target_type": str(c.get("target_type") or "").lower(),
                "known": known}
        hand_meta.append(meta)
        if known:
            s.hand.append(cid)
    s.draw_pile = _resolve_draw_pile(state, player, hand_meta)
    s.discard_pile = _resolve_discard_pile(state)

    return s, hand_meta


def _resolve_draw_pile(state: dict, player: dict, hand_meta: list[dict]) -> list[str]:
    # NOTE on `cost` (not read by this function today, only `id` is): the C#
    # side (RunSimulator.cs PileCardList) reports a DIFFERENT cost semantic
    # for draw_pile/discard_pile entries than for `hand` entries. Hand card
    # `cost` is GetResolved() — live, relic/power-adjusted, correct for a
    # card that's actually playable this decision. Pile card `cost` is the
    # base/unresolved cost (upgrades still apply, but not local or global
    # in-combat modifiers) — a card sitting in a pile isn't playable now,
    # and by the time it's drawn the live modifiers will likely have
    # changed anyway, so resolving it here would be false precision at real
    # perf expense (GetResolved's Hook.ModifyEnergyCostInCombat call is ~24%
    # of CombatPlayState's serialization time). If a future change here
    # starts reading `cost` off draw_pile/discard_pile entries, do NOT
    # assume it's the same number as hand `cost` — it isn't.
    #
    # Draw pile: when the C# side reports the real ordered pile (RunSimulator.cs
    # "draw_pile" field, added for this planner), use it verbatim instead of
    # guessing — order is no longer unknowable. The C# pile is reported
    # top-first: index 0 is the next card drawn. This was confirmed two ways,
    # not assumed: (1) decompiling lib/sts2.dll — CardPile.MoveToTopInternal
    # does `_cards.Remove(card); _cards.Insert(0, card)` and
    # CardPileCmd's single-card-draw path reads
    # `card = drawPile.Cards.FirstOrDefault()` each iteration; (2) empirically,
    # ending turn 1 without playing a card (forcing a full redraw of the
    # then-reported draw_pile) reproduced the exact same order in the new hand.
    # The Python sim's CombatState.draw() pops from the END of the list
    # (`self.draw_pile.pop()`) — i.e. the sim treats the LAST element as top —
    # so the real order must be REVERSED here, or the sim would draw
    # bottom-up instead of top-down and silently invert every prediction.
    # Unknown/broken ids are kept (not filtered): this matches the pre-existing
    # deck-composition fallback below (which never filtered either), and it's
    # safe because a card only matters to the sim once it reaches hand and is
    # considered for play — dfs() in plan_action() already skips any hand card
    # with get_card_data(cid) is None, so an unknown id drawn mid-search just
    # becomes an inert, unplayable hand slot, exactly like today.
    real_draw_pile = state.get("draw_pile")
    if real_draw_pile is not None:
        # An empty list is a GENUINELY empty draw pile (everything is
        # currently in hand/discard) — it must stay empty, not fall back to
        # the deck-composition guess below, which would incorrectly
        # resurrect cards that are actually elsewhere right now.
        ids = [_card_id_norm(c) for c in real_draw_pile]
        return list(reversed(ids))
    # No real order in this state (older logs / replays predating the
    # "draw_pile" field): full deck composition is known (player.deck).
    # Fill the sim draw pile with deck − hand so draw effects (Pommel
    # Strike, Shrug It Off) pull real cards. Order is shuffled with a
    # fixed seed — exact order is unknowable but composition is exact.
    deck_ids = [_card_id_norm(c) for c in (player.get("deck") or [])]
    hand_ids = [m["id"] for m in hand_meta]
    pool = list(deck_ids)
    for hid in hand_ids:
        if hid in pool:
            pool.remove(hid)
    random.Random(99).shuffle(pool)
    return pool


def _resolve_discard_pile(state: dict) -> list[str]:
    # Discard pile: also real & exact when reported. Needed so a mid-search
    # reshuffle (CombatState.draw(): draw_pile empty + discard_pile non-empty
    # → shuffle discard back into draw_pile) has real cards to work with —
    # previously this was never set, so build_sim_state's sim always started
    # with an empty discard pile even mid-combat, and any reshuffle inside a
    # DFS sequence found nothing to shuffle back in. Order doesn't need
    # reversing (or any particular convention): CombatState.draw() always
    # rng.shuffle()s the discard pile before treating it as the new draw
    # pile, so only composition matters here, not sequence.
    real_discard_pile = state.get("discard_pile")
    if real_discard_pile is not None:
        return [_card_id_norm(c) for c in real_discard_pile]
    return []


def _predict_after_enemy_turn(sim: CombatState, rng: random.Random) -> CombatState:
    """Clone + run the sim's end_turn (enemy attacks resolve exactly as the
    tested sim math: block soak per hit, strength, weak, vulnerable, thorns,
    relic triggers)."""
    c = sim.clone() if hasattr(sim, "clone") else None
    if c is None:
        import copy
        c = copy.deepcopy(sim)
    try:
        end_turn(c, rng)
    except Exception:
        pass
    return c


def _future_value(after: CombatState, est_remaining_turns: float) -> float:
    """Multi-turn value of persistent player state — fixes 1-turn myopia.

    Without this the planner NEVER plays Powers (Inflame = 0 damage this
    turn → always loses to Strike) and never ramps. Each point of:
      Strength      ≈ +2.0 dmg/turn (≈2 attacks/turn average)
      Dexterity     ≈ +1.5 block/turn
      Metallicize   ≈ +1.0 block/turn (direct)
      Thorns        ≈ +1.0 dmg/turn vs attackers
      Ritual        — enemy-side; not player
    Persistent powers registered via trigger wrappers (Demon Form etc.) are
    visible as statuses after play; on_turn_start powers add value too but
    statuses capture most of it.
    """
    v = 0.0
    v += after.statuses.get("Strength", 0) * 2.0 * est_remaining_turns
    v += after.statuses.get("Dexterity", 0) * 1.5 * est_remaining_turns
    v += after.statuses.get("Metallicize", 0) * 1.0 * est_remaining_turns
    v += after.statuses.get("Thorns", 0) * 1.0 * est_remaining_turns
    # Power-card engines registered as sim powers (Demon Form / FNP / Dark
    # Embrace wrappers live in state.powers) — flat per-engine value.
    v += len(after.powers) * 3.0 * min(est_remaining_turns, 3.0)
    return v


def _rollout_turns() -> int:
    """Turn budget for rollout scoring, from STS2_PLANNER_ROLLOUT. 0 = off.

    The 1-turn planner scores a candidate by HP after ONE enemy turn. That is
    blind to the Act 3 boss, which is three forms totalling 600 HP with a
    self-looping Multi-Claw whose hit count grows every use: winning needs
    block/damage sequenced across many turns. Measured there, 1-turn lookahead
    scored 0/24 at scoring weights 0.10 / 0.5 / 1.0 / 2.0 against the bare
    policy's 4/24 — the horizon, not the objective, is the limit.

    With this set, each candidate first-action is rolled forward to terminal
    with `combat_simulator.heuristic_policy` and scored on the OUTCOME (win,
    surviving HP) instead of one turn of HP. Off by default: rollouts cost
    real wall-clock inside a live decision, and the planner itself is off.
    """
    raw = os.environ.get("STS2_PLANNER_ROLLOUT")
    if raw is None or raw == "":
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _rollout_score(sim: CombatState, turns: int, rng: random.Random) -> float:
    """Play `sim` forward to terminal and score the outcome.

    Scored on the result of the fight rather than the next turn: a win is worth
    far more than any amount of preserved HP, and surviving HP breaks ties among
    losses so the search still prefers lasting longer.
    """
    from agent.sim.combat_simulator import simulate_combat, heuristic_policy
    clone = sim.clone()
    try:
        out = simulate_combat(clone, policy=heuristic_policy,
                              max_turns=turns, rng=rng)
    except Exception:
        return float(sim.hp)  # a sim failure must never break live play
    return (1000.0 if out.get("won") else 0.0) + float(out.get("final_hp", 0))


def _score(after: CombatState, before_seq: CombatState,
           dmg_dealt: int, kills: int, energy_left: int,
           unsupported_delta: int, est_remaining_turns: float = 3.0) -> float:
    """HP-centric: surviving HP after enemy turn dominates; small bonuses
    push damage/kills (anti-turtle, per block_reward collapse lesson);
    future-value term rewards ramping (Powers/Strength) by remaining length."""
    if after.hp <= 0:
        return -1000.0
    return (float(after.hp)
            # 0.10 against a surviving-HP weight of 1.0 means 1 HP saved is
            # worth 10 damage dealt — deliberately turtle-biased, and correct
            # for hallway fights. Suspected wrong at the Act 3 boss (3 forms,
            # 600 HP, self-escalating Multi-Claw), so it was made tunable and
            # swept there: at forced HP 190 over 12 stochastic fights x 2
            # banked snapshots, the planner scored 0/24 at weights 0.10, 0.5,
            # 1.0 AND 2.0, against the bare policy's 4/24. The weighting is not
            # the problem; 1-turn lookahead just cannot play this fight. Knob
            # removed rather than left as dead config.
            + 0.10 * dmg_dealt
            + 2.0 * kills
            + 0.30 * energy_left
            - 2.0 * unsupported_delta
            + _future_value(after, est_remaining_turns))


def plan_action(state: dict, masks=None, lethal_only: bool = False) -> int | None:
    """Search this turn's best card sequence; return env action int for the
    FIRST step (or 40=end_turn). None → caller should fall back to policy.

    lethal_only=True (hybrid mode): only return an action when the best
    sequence KILLS ALL enemies this turn — provably correct intervention;
    everything else stays with the PPO policy. Full-planner mode (False)
    underperformed the policy in n=30 evals (0/30 reach vs ~10%): 1-turn
    lookahead can't see multi-turn block/sequencing patterns the policy knows."""
    t0 = time.time()
    sim0, hand_meta = build_sim_state(state)
    if sim0 is None:
        return None
    known_metas = [m for m in hand_meta if m["known"]]
    if not known_metas:
        return None  # nothing we can reason about — let the policy act

    rng = random.Random(0xC0FFEE)
    rollout_turns = _rollout_turns()
    enemy_hp0 = sum(e.hp for e in sim0.enemies)
    unsup0 = sim0.statuses.get("_unsupported_effects", 0)
    # Estimate remaining combat length: enemy HP pool / rough deck DPT (8).
    est_turns = max(1.0, min(6.0, enemy_hp0 / 8.0))

    best_score = None
    best_first: tuple[str, int] | None = None   # (card_id, sim_target_idx)
    lethal_first: tuple[str, int] | None = None  # first action of an all-kill sequence
    counter = {"n": 0}

    def evaluate(sim: CombatState, first: tuple[str, int] | None):
        nonlocal best_score, best_first, lethal_first
        all_dead = all(e.hp <= 0 for e in sim.enemies)
        if all_dead and first is not None and lethal_first is None:
            lethal_first = first
        after = _predict_after_enemy_turn(sim, rng)
        dmg = enemy_hp0 - sum(max(0, e.hp) for e in sim.enemies)
        kills = sum(1 for e in sim.enemies if e.hp <= 0)
        unsup = sim.statuses.get("_unsupported_effects", 0) - unsup0
        if rollout_turns:
            sc = _rollout_score(sim, rollout_turns, rng)
        else:
            sc = _score(after, sim, dmg, kills, sim.energy, max(0, unsup),
                        est_remaining_turns=est_turns)
        if best_score is None or sc > best_score:
            best_score = sc
            best_first = first

    def dfs(sim: CombatState, first: tuple[str, int] | None):
        if counter["n"] >= MAX_SEQUENCES or time.time() - t0 > DEADLINE_SEC:
            return
        counter["n"] += 1
        evaluate(sim, first)  # ending the turn here is always an option
        seen: set[tuple[str, int]] = set()
        for idx, cid in enumerate(list(sim.hand)):
            data = get_card_data(cid)
            if data is None:
                continue
            cost_raw = data.get("cost", "1")
            try:
                cost = int(cost_raw)
            except (TypeError, ValueError):
                cost = sim.energy if cost_raw == "X" else 1
            if cost > sim.energy:
                continue
            alive = [i for i, e in enumerate(sim.enemies) if e.hp > 0]
            if not alive:
                continue
            is_attack = data.get("type") == "Attack"
            targets = alive if (is_attack and len(alive) > 1) else [alive[0]]
            for t in targets:
                key = (cid, t)
                if key in seen:
                    continue  # duplicate card id + same target → symmetric
                seen.add(key)
                child = sim.clone() if hasattr(sim, "clone") else None
                if child is None:
                    import copy
                    child = copy.deepcopy(sim)
                ok = False
                try:
                    ok = play_card(child, idx, t, rng)
                except Exception:
                    ok = False
                if not ok:
                    continue
                dfs(child, first or key)

    dfs(sim0, None)

    if lethal_only:
        if lethal_first is None:
            return None  # no provable kill — let the policy play
        best_first = lethal_first

    if best_first is None:
        return END_TURN_ACTION

    cid, sim_target = best_first
    # Map back to a JSON hand slot for this card id (first matching playable slot)
    slot = next((m["slot"] for m in hand_meta
                 if m["known"] and m["id"] == cid), None)
    if slot is None or slot >= 10:
        return None
    # Target slot: anyenemy cards use the enemy index; everything else 3.
    meta = next(m for m in hand_meta if m["slot"] == slot)
    if meta["target_type"] == "anyenemy":
        target_slot = min(max(sim_target, 0), 2)
    else:
        target_slot = NO_TARGET_SLOT
    action = slot * 4 + target_slot
    if masks is not None:
        try:
            if not bool(masks[action]):
                # try the untargeted variant, then any masked variant of slot
                alt = slot * 4 + NO_TARGET_SLOT
                if bool(masks[alt]):
                    return alt
                for ts in range(4):
                    if bool(masks[slot * 4 + ts]):
                        return slot * 4 + ts
                return None
        except Exception:
            return None
    return action


# ─── Intent-aware defense override (Jun 13) ────────────────────────────────
# Narrow intervention discovered from boss replays: the policy NEVER blocks
# against telegraphed ATK intents (took 64 unblocked HP from Ceremonial Beast)
# yet wastes a Defend on a Debuff turn. Fix only the "should-block-didn't"
# error; leave the policy's (good) attack decisions alone. Never blocks when
# no attack is incoming → avoids the block_reward "block forever" collapse.

def _incoming_attack_damage(state: dict) -> int:
    """Total HP lost this turn from enemy attack intents. The engine's
    intent.damage is the resolved displayed value (already includes enemy
    Strength and player Vulnerable) — verified from replay traces where HP
    loss exactly matched the shown ATK number."""
    total = 0
    for e in state.get("enemies") or []:
        if (e.get("hp") or 0) <= 0:
            continue
        for it in e.get("intents") or []:
            if (it.get("type") or "").lower() == "attack":
                total += int(it.get("damage") or 0) * int(it.get("hits") or 1)
    return total


def _enemy_attack_damage(enemy: dict) -> int:
    total = 0
    for it in enemy.get("intents") or []:
        if (it.get("type") or "").lower() == "attack":
            total += int(it.get("damage") or 0) * int(it.get("hits") or 1)
    return total


def _card_cost(card: dict, energy: int) -> int:
    cost_raw = card.get("cost")
    try:
        return int(cost_raw)
    except (TypeError, ValueError):
        return energy if cost_raw == "X" else 1


def _card_damage_amount(card: dict) -> int:
    """Conservative single-hit damage estimate for kill-as-defense checks."""
    stats = card.get("stats") or {}
    dmg = int(stats.get("damage", 0) or 0)
    if dmg > 0:
        return dmg
    data = get_card_data(_card_id_norm(card))
    if data:
        for eff in data.get("parsed", {}).get("normal", []):
            kind = eff.get("kind")
            if kind in ("deal_damage", "deal_aoe"):
                dmg += int(eff.get("amount", 0) or 0)
            elif kind == "multi_hit":
                dmg += int(eff.get("amount", 0) or 0) * int(eff.get("hits", 1) or 1)
    return dmg


def _card_block_amount(card: dict) -> int:
    """Block a card grants. Reads JSON stats.block, falls back to sim parse."""
    stats = card.get("stats") or {}
    blk = int(stats.get("block", 0) or 0)
    if blk > 0:
        return blk
    data = get_card_data(_card_id_norm(card))
    if data:
        for eff in data.get("parsed", {}).get("normal", []):
            if eff.get("kind") == "gain_block":
                blk += int(eff.get("amount", 0) or 0)
    return blk


def _action_is_defense(state: dict, action_int: int) -> bool:
    """Is the chosen action playing a block-granting card?"""
    if action_int == END_TURN_ACTION:
        return False
    slot = action_int // 4
    hand = state.get("hand") or []
    if slot >= len(hand):
        return False
    return _card_block_amount(hand[slot]) > 0


def _masked_action_ok(masks, action: int) -> bool:
    if masks is None:
        return True
    try:
        return bool(masks[action])
    except Exception:
        return False


def _is_boss_or_elite_room(state: dict) -> bool:
    context = state.get("context") or {}
    room_type = str(
        state.get("room_type")
        or context.get("room_type")
        or context.get("room")
        or ""
    ).lower()
    return "boss" in room_type or "elite" in room_type


def _slippery_target(state: dict) -> tuple[int, int] | None:
    for idx, enemy in enumerate((state.get("enemies") or [])[:3]):
        if (enemy.get("hp") or 0) <= 0:
            continue
        for power in enemy.get("powers") or []:
            name = power.get("name")
            if isinstance(name, dict):
                name = name.get("en", "")
            if str(name or "").strip().lower() != "slippery":
                continue
            amount = int(power.get("amount", 0) or 0)
            if amount > 0:
                return idx, amount
    return None


def _card_hit_count(card: dict) -> int:
    stats = card.get("stats") or {}
    for key in ("hits", "hit_count", "times"):
        try:
            hits = int(stats.get(key, 0) or 0)
        except (TypeError, ValueError):
            hits = 0
        if hits > 0:
            return hits

    text = str(card.get("description") or "")
    data = get_card_data(_card_id_norm(card))
    if data:
        text = f"{text}\n{data.get('normal_text') or ''}"
        for eff in data.get("parsed", {}).get("normal", []):
            if eff.get("kind") == "multi_hit":
                try:
                    hits = int(eff.get("times", 0) or 0)
                except (TypeError, ValueError):
                    hits = 0
                if hits > 0:
                    return hits

    low = text.lower()
    if "twice" in low:
        return 2
    m = re.search(r"\b(\d+)\s+times\b", low)
    if m:
        return max(1, int(m.group(1)))
    return 1


def _attack_action_for_target(card: dict, slot: int, target_idx: int) -> int | None:
    target_type = str(card.get("target_type") or "").lower()
    if "all" in target_type and "enem" in target_type:
        return slot * 4 + NO_TARGET_SLOT
    if "anyenemy" in target_type or "enem" in target_type:
        return slot * 4 + target_idx
    return None


def _slippery_strip_options(state: dict, masks=None) -> list[tuple[tuple, int]]:
    target = _slippery_target(state)
    if target is None:
        return []
    target_idx, slippery = target
    energy = int(state.get("energy", 0) or 0)
    options: list[tuple[tuple, int]] = []
    for slot, card in enumerate((state.get("hand") or [])[:10]):
        if not card.get("can_play", True):
            continue
        if str(card.get("type") or "").lower() != "attack":
            continue
        cost = _card_cost(card, energy)
        if cost > energy:
            continue
        action = _attack_action_for_target(card, slot, target_idx)
        if action is None or not _masked_action_ok(masks, action):
            continue
        damage = _card_damage_amount(card)
        hits = _card_hit_count(card)
        if damage <= 0 or hits <= 0:
            continue
        strips = min(slippery, hits)
        waste = max(0, damage - 1) * strips
        score = (strips, -waste, -cost, -slot)
        options.append((score, action))
    options.sort(reverse=True)
    return options


def vantom_slippery_override(state: dict, policy_action: int,
                             masks=None) -> int | None:
    """Replace a wasteful attack into Vantom's Slippery with a better stripper.

    Returns None when the policy chose defense/end_turn, when no Slippery enemy
    is present, or when the chosen attack is already the best Slippery-strip
    action. The caller can use this as a narrow safety layer around raw policy
    actions.
    """
    if not state or state.get("decision") != "combat_play":
        return None
    if policy_action == END_TURN_ACTION:
        return None
    hand = state.get("hand") or []
    slot = int(policy_action) // 4
    if slot >= len(hand):
        return None
    chosen = hand[slot]
    if str(chosen.get("type") or "").lower() != "attack":
        return None

    options = _slippery_strip_options(state, masks)
    if not options:
        return None
    best_score, best_action = options[0]
    current_score = next((score for score, action in options
                          if action == int(policy_action)), None)
    if current_score is None:
        return best_action
    if best_action != int(policy_action) and best_score > current_score:
        return best_action
    return None


def apply_vantom_slippery_mask(state: dict, masks):
    """Mask wasteful Slippery attacks while leaving defense/end_turn legal."""
    if masks is None or not state or state.get("decision") != "combat_play":
        return masks
    options = _slippery_strip_options(state, masks)
    if len(options) <= 1:
        return masks
    adjusted = masks.copy() if hasattr(masks, "copy") else list(masks)
    best_score, best_action = options[0]
    for score, action in options[1:]:
        if action != best_action and score < best_score:
            adjusted[action] = False
    adjusted[best_action] = True
    return adjusted


def _kill_attacker_override(state: dict, masks, energy: int) -> int | None:
    """Return an attack that removes incoming damage by killing an attacker."""
    candidates = []
    enemies = state.get("enemies") or []
    for target_idx, enemy in enumerate(enemies[:3]):
        if (enemy.get("hp") or 0) <= 0:
            continue
        prevented = _enemy_attack_damage(enemy)
        if prevented <= 0:
            continue
        lethal_needed = int(enemy.get("hp", 0) or 0) + int(enemy.get("block", 0) or 0)
        for slot, card in enumerate((state.get("hand") or [])[:10]):
            if not card.get("can_play", True):
                continue
            cost = _card_cost(card, energy)
            if cost > energy:
                continue
            damage = _card_damage_amount(card)
            if damage < lethal_needed:
                continue
            target_type = str(card.get("target_type") or "").lower()
            if "all" in target_type and "enem" in target_type:
                action = slot * 4 + NO_TARGET_SLOT
            elif "anyenemy" in target_type or "enem" in target_type:
                action = slot * 4 + target_idx
            else:
                continue
            if _masked_action_ok(masks, action):
                candidates.append((prevented, damage, -cost, -slot, action))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][-1]


def intent_defense_override(state: dict, masks=None,
                             danger_frac: float | None = None) -> int | None:
    """If the enemy telegraphs an attack dealing significant UNBLOCKED damage
    and the policy isn't defending, return the action for the most efficient
    block card. None = no intervention.

    Caller must already know the policy action is NOT a defense (check via
    _action_is_defense) — this function assumes intervention is wanted."""
    player = state.get("player") or {}
    hp = int(player.get("hp", 0) or 0)
    max_hp = int(player.get("max_hp", 80) or 80)
    block = int(player.get("block", 0) or 0)
    energy = int(state.get("energy", 0) or 0)
    incoming = _incoming_attack_damage(state)
    if incoming <= 0:
        return None  # no attack this turn — never block (anti-collapse)
    unblocked = max(0, incoming - block)
    if _is_boss_or_elite_room(state):
        danger = elite_danger_threshold(max_hp)
    elif danger_frac is not None:
        danger = max(_HALLWAY_DANGER_FLOOR, danger_frac * max_hp)
    else:
        danger = hallway_danger_threshold(max_hp)
    # 0.08 is NOT worth sweeping: made configurable and driven over 801 recorded
    # combat_play states, 0.02 and 0.20 both changed 0 decisions. The clause is
    # dominated by the `unblocked < danger` term ahead of it, so this guard is
    # effectively unreachable. Knob removed rather than left as dead config.
    critical_hp = max(5.0, 0.08 * max_hp)
    if unblocked < danger and unblocked < hp and (hp - unblocked) > critical_hp:
        return None  # hit is survivable as-is; let the policy attack
    kill_action = _kill_attacker_override(state, masks, energy)
    if kill_action is not None:
        return kill_action
    # Find playable block cards; prefer highest block.
    opts = []
    for slot, c in enumerate(state.get("hand") or []):
        if not c.get("can_play", True):
            continue
        cost = _card_cost(c, energy)
        if cost > energy:
            continue
        blk = _card_block_amount(c)
        if blk > 0:
            opts.append((blk, slot, cost))
    if not opts:
        return None
    # STS2_BLOCK_PICK: "max" (default, shipped) takes the biggest block card;
    # "efficient" takes the best block-per-energy, leaving energy for attacks.
    # opts entries are (block, slot, cost). MEASURED 2026-09-03 and kept default
    # "max": the knob binds (12.5% of 801 recorded combat_play decisions change)
    # but the outcome is mixed and tiny -- Ironclad 10.992 -> 11.179 (+0.188,
    # p=0.042), Regent 14.574 -> 14.414 (-0.160, p=0.44), combined ~ +0.01. Not
    # shipped, for the same reason as the shop basic purge: a change whose sign
    # flips between characters is not an improvement.
    if os.environ.get("STS2_BLOCK_PICK", "").strip().lower() == "efficient":
        opts.sort(key=lambda o: (o[0] / max(o[2], 1), o[0]), reverse=True)
    else:
        opts.sort(reverse=True)
    _, slot, _ = opts[0]
    action = slot * 4 + NO_TARGET_SLOT
    if masks is not None:
        try:
            if not bool(masks[action]):
                return None
        except Exception:
            return None
    return action


# ─── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Synthetic combat_play state: 3 Strikes + 1 Defend, 1 enemy attacking 12.
    fake = {
        "decision": "combat_play",
        "energy": 3, "max_energy": 3,
        "player": {"hp": 30, "max_hp": 80, "block": 0, "relics": []},
        "player_powers": None,
        "context": {"floor": 5},
        "hand": [
            {"index": 0, "id": "CARD.STRIKE_IRONCLAD", "cost": 1, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
            {"index": 1, "id": "CARD.STRIKE_IRONCLAD", "cost": 1, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
            {"index": 2, "id": "CARD.DEFEND_IRONCLAD", "cost": 1, "type": "Skill",
             "can_play": True, "target_type": "Self"},
            {"index": 3, "id": "CARD.BASH", "cost": 2, "type": "Attack",
             "can_play": True, "target_type": "AnyEnemy"},
        ],
        "enemies": [
            {"name": "JAW WORM", "hp": 11, "max_hp": 44, "block": 0,
             "intents": [{"type": "attack", "damage": 12, "hits": 1}],
             "powers": None},
        ],
    }
    t0 = time.time()
    a = plan_action(fake)
    dt = time.time() - t0
    print(f"action={a} ({dt*1000:.0f}ms)")
    # Enemy at 11 HP: two strikes (6+6) kill it → no incoming damage.
    # Expect: play a Strike (slot 0 or 1, target 0) → action 0 or 4.
    assert a in (0, 4), f"expected strike→enemy0 (0/4), got {a}"
    print("✓ planner kills the 11-HP enemy instead of blocking")

    # Variant: enemy 40 HP (can't kill) — blocking is better.
    fake["enemies"][0]["hp"] = 40
    a2 = plan_action(fake)
    print(f"action={a2}")
    # Best play: defend (12 incoming, 5 block) + strikes with rest.
    # Defend = slot 2 untargeted → 2*4+3 = 11
    print("✓ planner output for unkillable enemy:", a2)
