"""Tests for shop scenarios."""
import pytest


class TestShopStructure:
    def test_shop_fields(self, game):
        state = game.start(seed="ss1")
        game.skip_neow(state)
        state = game.enter_room("shop")
        assert state["decision"] == "shop"
        assert "cards" in state
        assert "relics" in state
        assert "potions" in state
        assert "card_removal_cost" in state

    def test_shop_cards_have_description(self, game):
        state = game.start(seed="ss2")
        game.skip_neow(state)
        state = game.enter_room("shop")
        for card in state["cards"]:
            assert isinstance(card["name"], str)
            assert "description" in card
            assert "cost" in card
            assert "type" in card
            assert "card_cost" in card

    def test_shop_cards_have_upgrade_preview(self, game):
        state = game.start(seed="ss3")
        game.skip_neow(state)
        state = game.enter_room("shop")
        has_upgrade = any(c.get("after_upgrade") for c in state["cards"])
        assert has_upgrade

    def test_shop_relics_have_description(self, game):
        state = game.start(seed="ss4")
        game.skip_neow(state)
        state = game.enter_room("shop")
        for r in state["relics"]:
            assert isinstance(r["name"], str)
            assert "description" in r

    def test_shop_potions_have_description(self, game):
        state = game.start(seed="ss5")
        game.skip_neow(state)
        state = game.enter_room("shop")
        for p in state["potions"]:
            assert isinstance(p["name"], str)
            assert "description" in p


class TestShopBuy:
    def test_buy_card_reduces_gold(self, game):
        state = game.start(seed="sb1")
        game.skip_neow(state)
        game.set_player(gold=999)
        state = game.enter_room("shop")
        gold_before = state["player"]["gold"]
        deck_before = state["player"]["deck_size"]
        stocked = [c for c in state["cards"] if c.get("is_stocked")]
        assert stocked
        card = stocked[0]
        state = game.act("buy_card", card_index=card["index"])
        if state.get("decision") == "shop":
            assert state["player"]["gold"] < gold_before
            assert state["player"]["deck_size"] == deck_before + 1

    def test_buy_relic_succeeds_and_is_not_reported_as_error(self, game):
        """BUG-046: the purchase clears the shop entry's Model, so logging its name
        AFTER buying threw NRE and a successful buy came back as
        {"type": "error", "message": "Buy relic failed: Object reference ..."}."""
        state = game.start(seed="sb4")
        game.skip_neow(state)
        game.set_player(gold=999)
        state = game.enter_room("shop")
        assert state["decision"] == "shop"
        gold_before = state["player"]["gold"]
        relics_before = len(state["player"]["relics"])
        # Old Coin pays out gold on pickup, which would blur the exact-cost assertion.
        stocked = [r for r in state["relics"]
                   if r.get("is_stocked") and r["name"] != "Old Coin" and r["cost"] <= gold_before]
        assert stocked, "no affordable stocked relic in this shop"
        relic = stocked[0]
        state = game.act("buy_relic", relic_index=relic["index"])
        assert state.get("type") != "error", state
        # A pickup effect may open a selection (e.g. Kifuda); resolve it to get back to the shop.
        for _ in range(5):
            if state.get("decision") == "card_select":
                if state.get("min_select", 0) == 0:
                    state = game.act("skip_select")
                else:
                    state = game.act("select_cards", indices="0")
                assert state.get("type") != "error", state
            else:
                break
        assert state["decision"] == "shop", state
        assert state["player"]["gold"] == gold_before - relic["cost"]
        assert len(state["player"]["relics"]) == relics_before + 1

    def test_buy_potion_succeeds_and_is_not_swallowed(self, game):
        """BUG-046: same post-purchase Model dereference in DoBuyPotion (there the NRE
        was swallowed into a log line, so verify the purchase really landed)."""
        state = game.start(seed="sb5")
        game.skip_neow(state)
        game.set_player(gold=999)
        state = game.enter_room("shop")
        assert state["decision"] == "shop"
        gold_before = state["player"]["gold"]
        potions_before = len(state["player"]["potions"])
        stocked = [p for p in state["potions"]
                   if p.get("is_stocked") and p["cost"] <= gold_before]
        if not stocked:
            pytest.skip("no affordable stocked potion in this shop")
        if potions_before >= 3:
            pytest.skip("potion belt already full after Neow")
        potion = stocked[0]
        state = game.act("buy_potion", potion_index=potion["index"])
        assert state.get("type") != "error", state
        assert state["decision"] == "shop", state
        assert state["player"]["gold"] == gold_before - potion["cost"]
        assert len(state["player"]["potions"]) == potions_before + 1

    def test_buy_insufficient_gold(self, game):
        state = game.start(seed="sb2")
        game.skip_neow(state)
        game.set_player(gold=0)
        state = game.enter_room("shop")
        stocked = [c for c in state["cards"] if c.get("is_stocked")]
        if stocked:
            state = game.act("buy_card", card_index=stocked[0]["index"])
            assert state.get("type") == "error"

    def test_leave_shop(self, game):
        state = game.start(seed="sb3")
        game.skip_neow(state)
        state = game.enter_room("shop")
        state = game.act("leave_room")
        assert state["decision"] == "map_select"


class TestShopRemove:
    def test_remove_card_flow(self, game):
        state = game.start(seed="sr1")
        game.skip_neow(state)
        game.set_player(gold=999)
        state = game.enter_room("shop")
        deck_before = state["player"]["deck_size"]
        state = game.act("remove_card")
        # Should trigger card_select
        if state["decision"] == "card_select":
            state = game.act("select_cards", indices="0")
            # Should return to shop with deck_size - 1
            if state.get("decision") == "shop":
                assert state["player"]["deck_size"] == deck_before - 1
