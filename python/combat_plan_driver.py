"""
Client-side driver for the plan_combat_turn JSON action: resolves the
solver's stable card/potion/enemy identities to live hand/potion/enemy
indices, applies bundled card_select sub-choices, and executes a plan's
actions up through the first end_turn.

See docs/superpowers/plans/2026-09-17-combatsolver-port-phase1.md for full
protocol context.
"""


def _norm_entity_id(eid):
    """Normalize a card/potion id for comparison between two different formats
    seen in this protocol: the ordinary combat_play/card_select JSON's "id"
    field is "CARD.<ENTRY>" for cards and "POTION.<ENTRY>" for potions
    (c.Id.ToString() / p.Id.ToString() in RunSimulator.cs), while
    plan_combat_turn's card_id/potion_id is the bare uppercase entry
    (card.Preview.Id.Entry / potion.Id.Entry in the vendored solver) -- same
    discrepancy agent/card_scoring.py's _card_id_norm already strips for
    card-override lookups (confirmed for potions too: live logs show e.g.
    "Player 1 obtained POTION.BLOCK_POTION from potion reward")."""
    if isinstance(eid, dict):
        eid = eid.get("en", str(eid))
    eid = str(eid).upper().strip()
    for prefix in ("CARD.", "POTION."):
        if eid.startswith(prefix):
            return eid[len(prefix):]
    return eid


def _resolve_card_index(hand, card_id, occurrence):
    """Resolve plan_combat_turn's (card_id, card_occurrence) -- a stable card
    identity -- to a live hand card_index (hand position), by walking the
    hand in order and counting matches the same way the vendored solver's
    own FindCardOccurrence (Search/CombatBeamSolver.Expansion.cs) counts
    occurrences when it originally assigned card_occurrence."""
    target = _norm_entity_id(card_id)
    seen = 0
    for c in hand:
        if _norm_entity_id(c.get("id", "")) == target:
            if seen == occurrence:
                return c["index"]
            seen += 1
    return None


def _resolve_potion_index(potions, potion_id):
    """Resolve a plan_combat_turn use_potion action's potion_id (stable
    identity) to a live potion_index -- the position in the CURRENT
    combat_play decision's player.potions list, i.e. the real game's
    player.Potions (RunSimulator.cs's DoUsePotion indexes potion_index into
    player.Potions.ToList()).

    Deliberately does NOT use the plan action's own "potion_slot": that is
    the solver's PHYSICAL belt slot (player.PotionSlots in the real game --
    fixed-size, with a null hole at every empty slot), which is a genuinely
    different numbering from player.Potions (the real game's own compacted
    enumerable that skips empty slots entirely -- confirmed by decompiling
    MegaCrit.Sts2.Core.Entities.Players.Player: `Potions => PotionSlots.Where(p
    => p != null)`). The two numberings only agree while no earlier belt slot
    has been consumed; once one has, PotionSlot no longer means the same thing
    as potion_index, which is exactly what caused a live "Invalid potion
    index" rejection (Task 5 postmortem, docs/superpowers/plans/
    2026-09-17-combatsolver-port-phase1.md). Resolving by identity instead
    (matching potion_id here the same way _resolve_card_index matches
    card_id) sidesteps the whole discrepancy. Potions have no upgrade/
    enchantment-style state to disambiguate duplicates by, so this returns
    the first live match -- unlike cards there is no occurrence field to
    thread through for potions."""
    target = _norm_entity_id(potion_id)
    for p in potions:
        if _norm_entity_id(p.get("id", "")) == target:
            return p["index"]
    return None


def _resolve_enemy_target_index(enemies, target_combat_id):
    """Resolve a plan_combat_turn action's target_combat_id -- the solver's
    stable per-enemy identity (the real game's Creature.CombatId, exposed on
    the ordinary combat_play decision's "enemies" list as "combat_id" since
    the Run 8 fix below) -- to a live target_index (position in the CURRENT
    combat_play decision's enemies list).

    Mirrors _resolve_card_index/_resolve_potion_index: trusting the plan's
    own target_index directly is unsafe not just cross-turn (which CLAUDE.md's
    Protocol notes already documented) but also SAME-turn -- an EARLIER action
    in the very same turn-plan can kill an enemy, and RunSimulator.cs's
    combat_play "enemies" list only ever contains IsAlive creatures, so it
    reindexes/shrinks the moment that happens. A LATER action's plan-time
    target_index (computed against the pre-kill enemy count) can then point
    past the live list's end, or silently hit the wrong creature. Confirmed
    live: 20-game Ironclad eval Run 8 (logs/20260917_192246_Ironclad_run_8.jsonl
    step 31-32) -- a play_card with target_index=2 was rejected with
    "'target_index' is required when multiple enemies are alive (2)", i.e.
    only 2 enemies were alive when the plan's target_index assumed a 3rd
    slot still existed.

    Returns None if no live enemy has this combat_id -- the target died
    (killed by an earlier action in this same plan), and the caller must
    skip the action rather than guess a substitute target.
    """
    if target_combat_id is None:
        return None
    for e in enemies:
        if e.get("combat_id") == target_combat_id:
            return e["index"]
    return None


def _resolve_choice_indices(pending_cards, choice):
    """Resolve a plan action's bundled card_select sub-choice (Discard/Exhaust/
    Transform/... -- PlanCardChoice in Search/CombatPlan.cs) to indices into
    the live card_select decision's offered "cards" list.

    Unlike _resolve_card_index (which matches card_id + card_occurrence the
    same way the vendored solver's CardOccurrence does -- Entry only, ignoring
    upgrade state, per CombatBeamSolver.Expansion.cs), this matches each
    PlanCardToken's card_id + upgrade_level + option_occurrence, mirroring the
    vendor's OptionOccurrence/HasStableTokenIdentity (Search/CardChoiceSupport.cs),
    which requires BOTH Entry AND CurrentUpgradeLevel to agree before two
    candidates count as "the same card" for occurrence-counting purposes.
    Confirmed as a real (not just theoretical) bug: with entry-only matching,
    a choice token identifying "the 2nd unupgraded Strike" in a candidate list
    that also contains an upgraded Strike+ would miscount the Strike+ as an
    occurrence of plain Strike and resolve to the wrong physical card. The
    live JSON only carries a bool ("upgraded" on each pending candidate,
    "upgrade_level" as an int on each token) rather than the vendor's richer
    state, so the comparison here is boolean upgraded-or-not -- as close to
    HasStableTokenIdentity as the wire format allows, not full parity with a
    CurrentUpgradeLevel int (e.g. it can't distinguish two different upgrade
    tracks/levels beyond upgraded-vs-not). Returns None if any token can't be
    resolved (a real mismatch to surface, not something to paper over)."""
    indices = []
    for token in choice.get("cards", []):
        target = _norm_entity_id(token.get("card_id", ""))
        target_upgraded = bool(token.get("upgrade_level", 0))
        occ = token.get("option_occurrence", 0)
        seen = 0
        found = None
        for c in pending_cards:
            if (_norm_entity_id(c.get("id", "")) == target
                    and bool(c.get("upgraded", False)) == target_upgraded):
                if seen == occ:
                    found = c["index"]
                    break
                seen += 1
        if found is None:
            return None
        indices.append(found)
    return indices


def _apply_action_choices(send, cur, action):
    """Resolve and answer any card_select sub-decisions bundled into a plan
    action (RunSimulator.cs's ConvertPlanActionsToJson attaches "choices" to
    ANY action kind that has one -- e.g. a potion like Ambrosia can trigger a
    card_select just as much as a Discard/Exhaust/Transform card can -- so
    this runs after play_card AND use_potion, not just play_card). Returns
    (state, ok).

    A bundled choice does NOT always surface as a live card_select: when
    there is at most one legal candidate to pick from (e.g. Ironclad's
    Headbutt -- "put a card from your discard pile on top of your draw
    pile" -- with exactly one card sitting in the discard), the real game
    auto-resolves it synchronously inside the same play_card/use_potion call
    and the decision never leaves combat_play. Confirmed live (Task 5
    follow-up regression): Thrash discarded this turn, Headbutt played with
    choices=[{"effect": "MoveToDrawTop", "cards": [{"card_id": "THRASH"}]}],
    decision stayed "combat_play" throughout, and the next state's
    draw_pile[0] was CARD.THRASH -- the solver's intended choice WAS
    applied, just with no round-trip. Treating that as a hard failure was
    the actual bug (this project's own CLAUDE.md documents the engine's
    complementary invariant: any path that can open a card_select "must
    yield as soon as _cardSelector.HasPending appears", so if the decision
    is NOT card_select, nothing is actually pending -- the choice already
    resolved one way or another). If the resulting decision is neither
    card_select nor combat_play (e.g. the card's damage ended the fight,
    moving to card_reward/game_over), there is equally nothing left to
    answer here -- the caller's outer loop handles what comes next. Only a
    genuinely unexpected decision, or an errored select_cards call, is
    treated as a real bug to surface.
    """
    for choice in action.get("choices", []):
        decision = cur.get("decision")
        if decision == "combat_play":
            continue  # auto-resolved synchronously; nothing to answer
        if decision != "card_select":
            # Left combat_play some other way (fight over, etc.) -- also
            # nothing to answer; let the caller's outer loop react to
            # whatever decision this now is.
            return cur, True
        idxs = _resolve_choice_indices(cur.get("cards", []), choice)
        if idxs is None:
            print(f"  !! plan_combat_turn: could not resolve choice tokens "
                  f"{choice.get('cards')} against pending cards "
                  f"{[c.get('id') for c in cur.get('cards', [])]}")
            return cur, False
        cur = send({"cmd": "action", "action": "select_cards",
                    "args": {"indices": ",".join(map(str, idxs))}})
        if cur.get("type") == "error":
            print(f"  !! plan_combat_turn: select_cards failed: {cur.get('message')}")
            return cur, False
    return cur, True


def _send_and_apply_choices(send, cur, action, action_name, index_key, idx):
    """Shared post-resolution part of a play_card/use_potion plan action, run
    once the primary index (card_index/potion_index) has already been
    resolved by the caller. Resolves target_combat_id to a live target_index
    (or falls back to a raw target_index), sends the action, checks for a
    live-engine error, applies any bundled card_select choices, and checks
    whether the decision left combat_play.

    Returns (cur, ok, done). ok=False is a real live-engine failure the
    caller should propagate as (cur, False). done=True means the caller
    should return (cur, True) -- either the action fully resolved and left
    combat_play, or a hard failure already returned. done=False means the
    target was no longer alive (skipped) and the caller's for-loop should
    continue to the next planned action."""
    args = {index_key: idx}
    if "target_combat_id" in action:
        t_idx = _resolve_enemy_target_index(cur.get("enemies", []), action["target_combat_id"])
        if t_idx is None:
            # index_key is "card_index"/"potion_index"; the action's matching
            # stable-identity field is "card_id"/"potion_id" -- same prefix.
            id_key = index_key.replace("_index", "_id")
            print(f"  .. plan_combat_turn: target {action.get('target_name')} "
                  f"(combat_id={action.get('target_combat_id')}) for "
                  f"{id_key}={action.get(id_key)} is no longer alive -- "
                  f"an earlier action in this same plan must have killed it; "
                  f"skipping this action")
            return cur, True, False
        args["target_index"] = t_idx
    elif "target_index" in action:
        # No stable id available -- shouldn't happen for AnyEnemy cards per
        # ConvertPlanActionsToJson, but keep the raw index as a fallback.
        args["target_index"] = action["target_index"]
    cur = send({"cmd": "action", "action": action_name, "args": args})
    if cur.get("type") == "error":
        print(f"  !! plan_combat_turn: {action_name} failed: {cur.get('message')}")
        return cur, False, True
    cur, ok = _apply_action_choices(send, cur, action)
    if not ok:
        return cur, False, True
    if cur.get("decision") != "combat_play":
        return cur, True, True
    return cur, True, False


def _execute_combat_plan_actions(send, state, actions):
    """Execute a plan_combat_turn action list up through (and including) the
    first end_turn -- never the whole multi-turn plan blindly, per CLAUDE.md's
    "Protocol notes" on plan_combat_turn: later turns assume draws and enemy
    intents the protocol never exposes, so the caller must re-call
    plan_combat_turn fresh for the next turn instead. (Targets themselves are
    resolvable on any turn -- each enemy in a combat_play decision carries the
    same combat_id the plan's target_combat_id refers to; see
    _resolve_enemy_target_index.) Returns (state, ok); ok=False means the LIVE engine
    itself rejected an action, or a bundled card_select choice couldn't be
    resolved -- a real bug to surface, not "the solver predicts a loss". An
    unresolved card_id/potion_id is deliberately NOT one of those cases (see
    "Stale-plan recovery" below) -- it reports ok=True instead, since it
    reflects the solver's own prediction going stale, not a live-engine
    disagreement.

    Also stops (successfully) the moment any action leaves the "combat_play"
    decision for a reason OTHER than the plan's own end_turn -- e.g. a card's
    damage happens to end the fight mid-turn, jumping straight to
    card_reward/game_over. Trying to resolve the next planned action's
    card_id against a hand that no longer exists would misreport a real
    "fight ended early" as a false unresolved-card failure; the caller's
    outer decision loop is what should react to whatever decision this now
    is, not this function.

    Same-turn target resolution (Run 8 fix): an action's target_index is only
    valid against the enemy list AS OF PLAN TIME. If an EARLIER action in this
    same plan killed an enemy, the live combat_play decision's enemies list
    (RunSimulator.cs only ever includes IsAlive creatures) reindexes/shrinks,
    so a later action's raw target_index can point past the live list's end
    or hit the wrong creature. Every play_card/use_potion action that targets
    an enemy also carries target_combat_id (the stable Creature.CombatId,
    now also exposed on the live enemies list as "combat_id" -- see
    RunSimulator.cs's enemy serialization). _resolve_enemy_target_index
    re-derives the CURRENT target_index from that stable id each time,
    instead of trusting the plan's stale one. If the intended target is no
    longer alive at all (killed earlier this turn), resolution fails and the
    action is skipped rather than guessing a substitute target -- the next
    action still gets a chance (nothing else about state resolution depends
    on this one action having run).

    Stale-plan recovery (Run 15 fix): if a LATER action's card_id/potion_id
    itself can't be found in the live hand/potions at all (not a target
    problem -- the card the plan wants to play was simply never there),
    that is different from an engine rejection: it means the solver's
    PREDICTION of this turn's state diverged from what the live engine
    actually did, so the rest of this precomputed plan is unsafe to keep
    executing blindly. Confirmed root cause for one concrete case (20-game
    Ironclad eval Run 15, logs/20260917_194053_Ironclad_run_15.jsonl step
    148-150): the solver predicted Bash (damage + Vulnerable) would trigger
    Ironclad's Vicious power ("whenever you apply Vulnerable, draw 1 card")
    and planned a follow-up play_card for the drawn card (True Grit) --
    but Bash's hit was lethal, and the real game's Vulnerable application
    (decompiled MegaCrit.Sts2.Core.Models.Cards.Bash.OnPlay in lib/sts2.dll)
    silently no-ops against an already-dead target, so Vicious never fired
    and no card was drawn. The vendored solver's generic post-damage power
    effect application (CombatSolverEngine/Prediction/CardEffectSpecRegistry.cs's
    CardEffectTarget.Target case) has no such IsAlive guard before applying
    the power and recording the triggering PowerAmountChange that
    PowerLifecycleSupport.ResolvePowerAmountChanges later fires Vicious's
    draw from -- a genuine vendored-engine simulation gap, but one with wide
    blast radius (any lethal "damage + debuff" card), so fixing the engine
    itself is out of scope here (see docs/superpowers/plans/
    2026-09-17-combatsolver-port-phase1.md's postmortem). The safe, narrow
    mitigation available at this layer: stop executing the rest of THIS
    stale plan and report ok=True with the decision still "combat_play" --
    the caller's outer loop (play_full_run.py's play_run()) then naturally
    re-enters the combat_play branch and calls plan_combat_turn again fresh,
    which plans off the ACTUAL live hand instead of the solver's stale
    prediction. This trades a slightly weaker turn (the remaining planned
    actions don't execute) for correctness, matching this project's
    established "re-plan when stale" philosophy (CLAUDE.md's Protocol notes)
    and the sanctioned Run 8 fallback ("skip the action, or trigger a fresh
    re-plan, don't just guess a random target").
    """
    cur = state
    for action in actions:
        kind = action.get("action")
        if kind == "play_card":
            hand = cur.get("hand", [])
            idx = _resolve_card_index(hand, action.get("card_id", ""), action.get("card_occurrence", 0))
            if idx is None:
                print(f"  ~~ plan_combat_turn: card_id={action.get('card_id')} "
                      f"occurrence={action.get('card_occurrence')} not found in live hand "
                      f"{[c.get('id') for c in hand]} -- the plan has gone stale (a "
                      f"predicted mid-turn side effect, e.g. a power-triggered draw, "
                      f"didn't actually happen live; see Run 15 fix note above). "
                      f"Stopping this plan early and letting the caller re-plan from "
                      f"the current live state.")
                return cur, True
            cur, ok, done = _send_and_apply_choices(
                send, cur, action, "play_card", "card_index", idx)
            if done:
                return cur, ok
        elif kind == "use_potion":
            potions = cur.get("player", {}).get("potions", [])
            idx = _resolve_potion_index(potions, action.get("potion_id", ""))
            if idx is None:
                print(f"  ~~ plan_combat_turn: potion_id={action.get('potion_id')} "
                      f"(solver's physical slot={action.get('potion_slot')}) not found in "
                      f"live potions {[p.get('id') for p in potions]} -- the plan has gone "
                      f"stale (see Run 15 fix note above). Stopping this plan early and "
                      f"letting the caller re-plan from the current live state.")
                return cur, True
            cur, ok, done = _send_and_apply_choices(
                send, cur, action, "use_potion", "potion_index", idx)
            if done:
                return cur, ok
        elif kind == "end_turn":
            cur = send({"cmd": "action", "action": "end_turn"})
            if cur.get("type") == "error":
                print(f"  !! plan_combat_turn: end_turn failed: {cur.get('message')}")
                return cur, False
            return cur, True
        else:
            # Unreachable today (PlanActionKind only has PlayCard/UsePotion/
            # EndTurn), but every other unexpected condition in this function
            # is a hard failure (unresolved card_id/potion_id/choice, any
            # send() error) -- silently skipping an action kind we don't
            # recognize would half-execute a plan without saying so if this
            # enum ever grows. Fail loudly instead, matching that convention.
            print(f"  !! plan_combat_turn: unknown action kind {kind!r}")
            return cur, False
    return cur, True
