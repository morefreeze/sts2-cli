"""Kaiser Crab boss (Crusher + Rocket) -- BUG-055.

`Crusher.BeforeDeath` and `Rocket.BeforeDeath` call `NAudioManager.Instance.PlayOneShot(...)`
unguarded (every other audio call goes through `SfxCmd`, which `NonInteractiveMode` guards).
`NAudioManager.Instance` is `NGame.Instance?.AudioManager`, null headless, so EVERY death of
an arm threw a NullReferenceException inside `CreatureCmd.KillWithoutCheckingWinCondition` --
after the HP was zeroed but before `Died` / `AfterDeath` / removal ran:

* an arm dying on the ENEMY's turn (Thorns / poison retaliating against its own attack) threw
  through the monster's move into the combat turn loop, which died; the EndTurn "nuclear
  fallback" then forced a `game_over` tagged `technical_failure_kind: "stuck"`;
* an arm dying on the player's turn only aborted that card's action, so the kill was
  half-done: `Crab Rage` ("when an ally dies, gain Strength and 99 Block") never fired and
  the surviving arm was far easier than in the real game.

These tests are real fights against the boss (hp 999 so the player outlives the long fights).
"""

import pytest

# Caltrops gives Thorns 3 (two of them -> 6); Bludgeon (3 energy, 32 dmg) does the bulk of the
# damage and Strike (1 energy, 6 dmg) trims the last few HP so the arm is left at 1..Thorns.
THORNS_NEEDED = 6
KILL_DECK = ["CALTROPS"] * 3 + ["BLUDGEON"] * 12 + ["STRIKE_IRONCLAD"] * 10
DAMAGE = {"BLUDGEON": 32, "STRIKE_IRONCLAD": 6}


def _enemy(state, name):
    return next((e for e in state.get("enemies", []) if e["name"] == name), None)


def _power(creature, name):
    return sum(p["amount"] for p in creature.get("powers", []) if p["name"] == name)


def _thorns(state):
    return sum(p["amount"] for p in state.get("player_powers", []) if p["name"] == "Thorns")


def _card_key(card):
    return card["id"].split(".", 1)[-1]


def _enter_kaiser_crab(game, deck):
    state = game.skip_neow(game.start(character="Ironclad", seed="kaiser_crab_bug055"))
    assert state["decision"] == "map_select"
    assert game.set_player(hp=999, max_hp=999, deck=deck)["type"] == "ok"
    state = game.enter_room("combat", encounter="KAISER_CRAB_BOSS")
    assert state["decision"] == "combat_play", state
    crusher, rocket = _enemy(state, "Crusher"), _enemy(state, "Rocket")
    assert crusher and rocket, state["enemies"]
    # BUG-052: Crusher.AfterAddedToRoom must run to the end (it applies these two powers).
    for arm in (crusher, rocket):
        assert _power(arm, "Crab Rage") == 1 and _power(arm, "Back Attack") == 1, arm
    return state


def _play(game, state, card, target=None):
    args = {"card_index": card["index"]}
    if target is not None:
        args["target_index"] = target["index"]
    new = game.act("play_card", **args)
    assert new.get("type") != "error", new
    return new


def _fight_to_the_end(game, state, target_name, max_steps=400):
    """Attack `target_name` with everything until the combat is over; return the last state."""
    for _ in range(max_steps):
        if state["decision"] != "combat_play":
            return state
        target = _enemy(state, target_name) or (state["enemies"] or [None])[0]
        attacks = [c for c in state["hand"] if c.get("can_play") and _card_key(c) in DAMAGE
                   and c["cost"] <= state["energy"]]
        if attacks and target is not None:
            state = _play(game, state, max(attacks, key=lambda c: DAMAGE[_card_key(c)]), target)
        else:
            state = game.act("end_turn")
    raise AssertionError("the Kaiser Crab fight did not end")


def _leave_arm_to_thorns(game, state, victim, max_steps=400):
    """Play Caltrops, then damage `victim` down to 1..THORNS_NEEDED HP and keep ending turns
    until it dies of its own attack (Thorns retaliating on the enemy's turn). Returns the state
    after the end_turn in which the victim died."""
    for _ in range(max_steps):
        assert state["decision"] == "combat_play", (
            "the combat left combat_play before the victim died", state.get("decision"),
            state.get("technical_failure_kind"))
        arm = _enemy(state, victim)
        if arm is None:
            return state
        hand = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
        caltrops = [c for c in hand if _card_key(c) == "CALTROPS"]
        if _thorns(state) < THORNS_NEEDED and caltrops:
            state = _play(game, state, caltrops[0])
            continue
        if _thorns(state) >= THORNS_NEEDED and arm["hp"] > THORNS_NEEDED:
            # never overshoot: assume every point of damage lands on HP, none on Block
            fits = [c for c in hand if _card_key(c) in DAMAGE and arm["hp"] - DAMAGE[_card_key(c)] >= 1]
            if fits:
                state = _play(game, state, max(fits, key=lambda c: DAMAGE[_card_key(c)]), arm)
                continue
        state = game.act("end_turn")
        assert state.get("type") != "error", state
        if state["decision"] == "combat_play" and _enemy(state, victim) is None:
            return state
    raise AssertionError(f"{victim} never died of Thorns")


@pytest.mark.parametrize("victim,survivor", [("Crusher", "Rocket"), ("Rocket", "Crusher")])
def test_arm_killed_by_thorns_on_the_enemy_turn_does_not_kill_the_turn_loop(game, victim, survivor):
    state = _enter_kaiser_crab(game, KILL_DECK)

    state = _leave_arm_to_thorns(game, state, victim)

    # the turn loop survived the kill: still a normal combat_play, not an engine-forced game_over
    assert state["decision"] == "combat_play", (state.get("decision"), state.get("technical_failure_kind"))
    assert "technical_failure_kind" not in state
    assert _enemy(state, victim) is None
    # the death hooks ran to completion, so Crab Rage fired on the arm that is still alive
    other = _enemy(state, survivor)
    assert other is not None
    assert other["block"] >= 99, ("Crab Rage did not give the surviving arm its Block", other)
    assert _power(other, "Strength") >= 5, ("Crab Rage did not give the surviving arm Strength", other)

    # ...and the rest of the boss fight plays out normally to a victory
    state = _fight_to_the_end(game, state, survivor)
    assert state["decision"] != "combat_play" and state["decision"] != "game_over", state.get("decision")
    log = game.stderr_text()
    assert "turn loop died" not in log
    assert "Nuclear fallback" not in log
    assert "[ERROR]" not in log, "the engine logged an exception during the boss fight"


def test_arm_killed_on_the_players_turn_still_runs_its_death_hooks(game):
    state = _enter_kaiser_crab(game, ["BLUDGEON"] * 12)

    # Kill Rocket (199 HP) with Bludgeons, one per turn, without letting Crusher die.
    for _ in range(100):
        assert state["decision"] == "combat_play", state.get("decision")
        if _enemy(state, "Rocket") is None:
            break
        bludgeons = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
        state = (_play(game, state, bludgeons[0], _enemy(state, "Rocket"))
                 if bludgeons else game.act("end_turn"))
    else:
        raise AssertionError("Rocket never died")

    crusher = _enemy(state, "Crusher")
    assert crusher is not None
    # Crab Rage is an AfterDeath hook: before BUG-055 the kill threw before AfterDeath ran
    assert crusher["block"] >= 99, ("Crab Rage did not fire for a kill made on the player's turn", crusher)
    assert _power(crusher, "Strength") >= 5, crusher
    log = game.stderr_text()
    # (the startup banner mentions "Rocket.BeforeDeath()"; a stack frame reads "BeforeDeath(Creature")
    assert "Rocket.BeforeDeath(Creature" not in log, "an exception escaped Rocket.BeforeDeath"
    assert "NullReferenceException" not in log
    assert "[ERROR]" not in log, "the engine logged an exception during the boss fight"
