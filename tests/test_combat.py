"""Tests for combat scenarios."""
from collections import Counter

import pytest


class TestCombatStructure:
    def test_combat_play_fields(self, game):
        state = game.start(seed="cs1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert state["decision"] == "combat_play"
        for key in ("round", "energy", "max_energy", "hand", "enemies",
                    "player", "draw_pile_count", "discard_pile_count", "player_powers",
                    "draw_pile", "discard_pile"):
            assert key in state, f"Missing: {key}"

    def test_card_fields(self, game):
        state = game.start(seed="cs2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for card in state["hand"]:
            assert isinstance(card["name"], str)
            assert "cost" in card
            assert "can_play" in card
            assert card["type"] in ("Attack", "Skill", "Power", "Status", "Curse")

    def test_pile_card_fields(self, game):
        """draw_pile/discard_pile entries need enough shape for the planner
        to identify, cost-check, and target-check a card without playing it
        (turn_planner.py reads id/cost; the next planned task -- multi-turn
        search -- is expected to need cost/type too, see RunSimulator.cs's
        comment above PileCardList)."""
        state = game.start(seed="cs4")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert state["draw_pile"], "expected a non-empty starting draw pile"
        for card in state["draw_pile"]:
            for key in ("index", "id", "name", "cost", "type", "target_type"):
                assert key in card, f"pile card missing {key}: {card}"
            assert isinstance(card["name"], str)
            assert card["type"] in ("Attack", "Skill", "Power", "Status", "Curse")

    def test_discard_pile_present_and_empty_at_combat_start(self, game):
        """Live-engine regression guard for the empty-vs-absent contract
        documented in RunSimulator.cs above PileCardList: a genuinely empty
        pile MUST be reported as `[]`, never collapsed to None/absent the
        way the player_powers field just below it collapses an empty list.
        At the very start of combat nothing has been played or discarded
        yet, so discard_pile==[] is a real, live example of "genuinely
        empty" straight from the engine (not a synthetic dict) -- if a
        future edit "cleans up" the draw_pile/discard_pile assignment to
        match the player_powers idiom (`X?.Count > 0 ? X : null`), this is
        the test that catches it.

        This does NOT exercise PileCardList's failure path (the try/catch
        that returns null on exception): nothing here forces an exception,
        so at combat start with an intact engine PileCardList never throws
        either way -- the old buggy exception-swallowing code and the
        current code both produce an identical `[]` here. The null-on-
        failure contract is presently guarded only by code review, not by
        an automated test.
        """
        state = game.start(seed="cs5")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        assert "discard_pile" in state
        assert state["discard_pile"] == [], (
            "discard_pile must be an empty list (genuinely empty) at combat "
            "start, not None/absent"
        )

    def test_draw_pile_order_matches_next_draw(self, game):
        """Live-engine proof of the ordering contract documented in
        RunSimulator.cs above PileCardList: draw_pile[0] is the top of the
        pile / the actual next card drawn, and the whole list is already in
        draw order -- not just trusting the comment, but confirming it
        against a real draw.

        Uses set_player(deck=...) to give the combat a bigger-than-normal,
        two-card-type deck. This matters because with the *default*
        starting deck, the draw pile remaining after the initial hand draw
        is exactly equal to the next turn's hand size (e.g. Ironclad: 10
        card deck - 5 card hand = 5 remaining, and the next end_turn draws
        exactly 5) -- so the *entire* remaining pile gets drawn regardless
        of its internal order, which would make this test pass even if the
        order convention were backwards. Sizing the deck so more cards
        remain than get drawn next turn makes it a real prefix draw, and
        segregating the forced order by card identity (all of one Strike
        vs Defend on top) makes the drawn hand's composition a clear,
        unambiguous signal of which end of the list was actually consumed.
        """
        state = game.start(character="Ironclad", seed="cs6")
        game.skip_neow(state)
        game.set_player(deck=["STRIKE_IRONCLAD"] * 10 + ["DEFEND_IRONCLAD"] * 10)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        hand_size = len(state["hand"])
        draw_pile = state["draw_pile"]
        assert len(draw_pile) > hand_size, (
            "test setup needs a surplus pile (more remaining than get "
            "redrawn) or a full-pile draw would pass regardless of order"
        )

        ids = [c["id"].split(".", 1)[-1] for c in draw_pile]
        counts = Counter(ids)
        # Put whichever card type has enough copies to fill a whole hand on
        # top, so the forced top-hand_size slice is a pure, unambiguous
        # composition to check against afterwards.
        top_type, top_n = counts.most_common(1)[0]
        assert top_n >= hand_size, (
            "test setup needs one card type with enough remaining copies "
            "to fill a full hand"
        )
        forced = [top_type] * counts[top_type]
        for other_type in [k for k in counts if k != top_type]:
            forced += [other_type] * counts[other_type]
        result = game.set_draw_order(forced)
        assert result["type"] == "ok"

        state = game.act("end_turn")
        assert state["decision"] == "combat_play"
        new_hand_ids = [c["id"].split(".", 1)[-1] for c in state["hand"]]
        assert Counter(new_hand_ids) == Counter([top_type] * hand_size), (
            f"draw_pile[0] must be the actual next card drawn; forced "
            f"{top_type} to the top of the pile but the next hand drawn "
            f"was {Counter(new_hand_ids)}"
        )

    def test_enemy_fields(self, game):
        state = game.start(seed="cs3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for e in state["enemies"]:
            assert isinstance(e["name"], str)
            assert e["hp"] > 0
            assert e["max_hp"] > 0
            assert "block" in e


class TestPlayCards:
    def test_play_card_costs_energy(self, game):
        state = game.start(seed="cp1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        energy_before = state["energy"]
        playable = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= energy_before]
        assert playable
        card = playable[0]
        args = {"card_index": card["index"]}
        if card.get("target_type") == "AnyEnemy":
            args["target_index"] = state["enemies"][0]["index"]
        state = game.act("play_card", **args)
        if state["decision"] == "combat_play":
            assert state["energy"] == energy_before - card["cost"]

    def test_play_attack_reduces_enemy_hp(self, game):
        state = game.start(seed="cp2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        target = state["enemies"][0]
        hp_before = target["hp"]
        attacks = [c for c in state["hand"] if c.get("can_play") and c["type"] == "Attack"
                   and c["cost"] <= state["energy"]]
        if not attacks:
            pytest.skip("No attacks in hand")
        card = attacks[0]
        args = {"card_index": card["index"]}
        if card.get("target_type") == "AnyEnemy":
            args["target_index"] = target["index"]
        state = game.act("play_card", **args)
        if state["decision"] == "combat_play":
            new_target = next((e for e in state["enemies"] if e["index"] == target["index"]), None)
            if new_target and target.get("block", 0) == 0:
                assert new_target["hp"] < hp_before

    def test_play_defend_adds_block(self, game):
        state = game.start(seed="cp3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        block_before = state["player"].get("block", 0)
        defends = [c for c in state["hand"] if c.get("can_play") and c["type"] == "Skill"
                   and c["cost"] <= state["energy"]]
        if not defends:
            pytest.skip("No skill cards")
        state = game.act("play_card", card_index=defends[0]["index"])
        if state["decision"] == "combat_play":
            assert state["player"].get("block", 0) >= block_before


class TestTurnFlow:
    def test_end_turn_advances_round(self, game):
        state = game.start(seed="tf1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        rnd = state["round"]
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert state["round"] == rnd + 1

    def test_end_turn_resets_energy(self, game):
        state = game.start(seed="tf2")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        max_e = state["max_energy"]
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert state["energy"] == max_e

    def test_end_turn_draws_new_hand(self, game):
        state = game.start(seed="tf3")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            assert len(state["hand"]) > 0


class TestCombatEnd:
    def test_win_combat_leads_to_reward(self, game):
        state = game.start(seed="cw1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        state = game.auto_play_combat(state)
        assert state["decision"] in ("card_reward", "map_select", "card_select", "bundle_select")

    def test_player_powers_after_enemy_debuff(self, game):
        """Shrinker Beetle applies Shrink debuff to player after its turn."""
        state = game.start(seed="ep1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        # End turn so beetle acts (applies Shrink to player)
        state = game.act("end_turn")
        if state["decision"] == "combat_play":
            pp = state.get("player_powers") or []
            assert len(pp) > 0, "Expected player debuff after Shrinker Beetle turn"
            for pw in pp:
                assert "name" in pw
                assert "amount" in pw
                assert "description" in pw


class TestCombatEdgeCases:
    def test_exhaust_all_and_end_turn(self, game):
        state = game.start(seed="ce1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        for _ in range(20):
            if state.get("decision") != "combat_play":
                break
            playable = [c for c in state["hand"] if c.get("can_play") and c["cost"] <= state["energy"]]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
        if state.get("decision") == "combat_play":
            state = game.act("end_turn")
            assert state.get("type") != "error"

    def test_many_cards_per_turn(self, game):
        """Play all playable cards in a single turn without errors."""
        state = game.start(seed="inf1")
        game.skip_neow(state)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        plays = 0
        for _ in range(20):
            if state.get("decision") != "combat_play":
                break
            playable = [c for c in state["hand"] if c.get("can_play")
                        and c["cost"] <= state["energy"] and c["type"] not in ("Status", "Curse")]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
            plays += 1
            assert state.get("type") != "error", f"Error after {plays} plays: {state.get('message')}"
        assert plays >= 2

    def test_infinite_card_loop(self, game):
        """Pommel Strike + Bloodletting infinite loop doesn't crash.

        Pommel Strike (1e): damage + draw 1
        Bloodletting (0e): lose HP + gain 2 energy
        Each cycle: net +1 energy, draws next card. Truly infinite.
        """
        state = game.start(seed="inf2")
        game.skip_neow(state)
        game.set_player(hp=80, max_hp=80, deck=["POMMEL_STRIKE"] * 5 + ["BLOODLETTING"] * 5)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")

        plays = 0
        for _ in range(60):
            if state.get("decision") != "combat_play":
                break
            hand = state.get("hand", [])
            energy = state.get("energy", 0)
            playable = [c for c in hand if c.get("can_play") and c["cost"] <= energy
                        and c["type"] not in ("Status", "Curse")]
            if not playable:
                break
            card = playable[0]
            args = {"card_index": card["index"]}
            if card.get("target_type") == "AnyEnemy" and state["enemies"]:
                args["target_index"] = state["enemies"][0]["index"]
            state = game.act("play_card", **args)
            plays += 1
            assert state.get("type") != "error", f"Error after {plays} plays: {state.get('message')}"

        # With Pommel Strike + Bloodletting, should play many cards before enemy dies
        assert plays >= 5, f"Expected infinite loop plays >= 5, got {plays}"

    def test_low_hp_death(self, game):
        """Player with 1 HP should die to any attack."""
        state = game.start(seed="ce2")
        game.skip_neow(state)
        game.set_player(hp=1)
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        # Just end turn, beetle will kill us
        state = game.act("end_turn")
        # Might need another turn
        for _ in range(10):
            if state.get("decision") == "game_over":
                break
            if state.get("decision") == "combat_play":
                state = game.act("end_turn")
            else:
                break
        assert state["decision"] == "game_over"
        assert state["victory"] is False


def _hand_card(state, card_id):
    """First card in `state["hand"]` with this id (e.g. "CARD.ANGER"), or fail loudly."""
    for card in state["hand"]:
        if card["id"] == card_id:
            return card
    raise AssertionError(f"{card_id} not in hand: {[c['id'] for c in state['hand']]}")


class TestReturnToHandCards:
    """BUG-050: a card that returns itself to the hand after resolving was reported as
    "Card could not be played (still in hand after action)". DoPlayCard's post-play check
    read "same hand size, same card at the same index" as "the play did nothing" -- which is
    exactly what such a card looks like once it has been played and put back."""

    def test_particle_wall_returns_to_hand_without_error(self, game):
        state = game.start(character="Regent", seed="bug050")
        game.skip_neow(state)
        # A 2-card deck puts both cards in the opening hand. Venerate (gain 2 stars) is played
        # first so Particle Wall (0 energy, 2 stars) ends up as the ONLY card in hand: it is then
        # re-added at the very index it was played from -- the shape that tripped the old check.
        game.set_player(hp=80, max_hp=80, deck=["VENERATE", "PARTICLE_WALL"])
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")

        state = game.act("play_card", card_index=_hand_card(state, "CARD.VENERATE")["index"])
        assert state.get("type") != "error", state
        assert state["decision"] == "combat_play"
        assert [c["id"] for c in state["hand"]] == ["CARD.PARTICLE_WALL"]
        stars_before = state["stars"]
        assert stars_before >= 2, "Venerate should have paid for Particle Wall's 2 star cost"
        block_before = state["player"]["block"]

        state = game.act("play_card", card_index=state["hand"][0]["index"])

        assert state.get("type") != "error", (
            f"a card that returns itself to the hand was played; got: {state}")
        assert state["decision"] == "combat_play"
        assert state["player"]["block"] > block_before, "Particle Wall's Block was not gained"
        assert state["stars"] == stars_before - 2, "the star cost was not paid"
        assert "CARD.PARTICLE_WALL" in [c["id"] for c in state["hand"]], (
            "Particle Wall must be back in the hand")


class TestChainedCardSelection:
    """BUG-049: resolving one card selection can run the game straight on into the NEXT
    selection before select_cards returns (Burning Pact's draw reshuffling into Stratagem's
    "choose a card from your draw pile"; Knowledge Demon's curse pick running into the next
    turn's reshuffle). HeadlessCardSelector.ResolvePending used to clear the pending state AFTER
    completing the TCS, wiping the newly opened selection: the reply was a combat_play the game
    could never leave (every play_card "still in hand", plan_combat_turn "only in the play
    phase", end_turn a no-op until the harness gave up STUCK)."""

    def test_selection_opened_by_the_continuation_is_surfaced(self, game):
        state = game.start(character="Ironclad", seed="bug049a")
        game.skip_neow(state)
        # 5-card deck = the whole deck in the opening hand, draw pile empty. Energy 3:
        #   Stratagem (1) -> Anger (0, leaves 2 Angers in the discard) -> Burning Pact (1).
        # Burning Pact asks which hand card to exhaust (selection #1); its draw then has to
        # reshuffle the 2-card discard into the empty draw pile, which fires Stratagem's
        # "choose a card from it" (selection #2) INSIDE the continuation of selection #1.
        game.set_player(hp=80, max_hp=80,
                        deck=["STRATAGEM", "BURNING_PACT", "ANGER", "ANGER", "STRIKE_IRONCLAD"])
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")

        state = game.act("play_card", card_index=_hand_card(state, "CARD.STRATAGEM")["index"])
        assert state.get("type") != "error", state
        state = game.act("play_card", card_index=_hand_card(state, "CARD.ANGER")["index"],
                         target_index=0)
        assert state.get("type") != "error", state
        assert state["discard_pile_count"] == 2, (
            "test setup: Anger and its copy should both be in the discard pile")

        # Selection #1: which card to exhaust.
        state = game.act("play_card", card_index=_hand_card(state, "CARD.BURNING_PACT")["index"])
        assert state.get("decision") == "card_select", state
        assert len(state["cards"]) >= 2

        # Resolving it opens selection #2 from within the continuation. It must come back as a
        # card_select over the reshuffled draw pile -- not as a dead combat_play.
        state = game.act("select_cards", indices="0")
        assert state.get("type") != "error", state
        assert state.get("decision") == "card_select", (
            "the selection opened while resolving the previous one was lost (BUG-049); "
            f"got {state.get('decision')!r}")
        assert [c["id"] for c in state["cards"]] == ["CARD.ANGER", "CARD.ANGER"]
        assert (state["min_select"], state["max_select"]) == (1, 1)

        # Answering it lets the game proceed: back in the play phase, cards play again.
        state = game.act("select_cards", indices="0")
        assert state.get("type") != "error", state
        assert state.get("decision") == "combat_play", state
        assert "CARD.ANGER" in [c["id"] for c in state["hand"]]
        state = game.act("play_card", card_index=_hand_card(state, "CARD.ANGER")["index"],
                         target_index=0)
        assert state.get("type") != "error", (
            f"combat is still parked on the lost selection (BUG-049): {state}")

    def test_turn_start_shuffle_selection_is_surfaced(self, game):
        """The un-chained sibling: Stratagem's pick opened by the NEXT TURN's draw must come
        back from end_turn as a card_select, and answering it must start the turn."""
        state = game.start(character="Ironclad", seed="bug049b")
        game.skip_neow(state)
        game.set_player(hp=80, max_hp=80,
                        deck=["STRATAGEM", "ANGER", "ANGER", "STRIKE_IRONCLAD", "STRIKE_IRONCLAD"])
        state = game.enter_room("combat", encounter="SHRINKER_BEETLE_WEAK")
        round_before = state["round"]

        state = game.act("play_card", card_index=_hand_card(state, "CARD.STRATAGEM")["index"])
        assert state.get("type") != "error", state

        # Turn 2's draw finds an empty draw pile, reshuffles the 4 discarded cards -> Stratagem.
        state = game.act("end_turn")
        assert state.get("decision") == "card_select", state
        assert len(state["cards"]) == 4

        state = game.act("select_cards", indices="0")
        assert state.get("type") != "error", state
        assert state.get("decision") == "combat_play", state
        assert state["round"] == round_before + 1
