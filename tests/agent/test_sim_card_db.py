"""The sim card DB must cover more than Ironclad.

`turn_planner`'s DFS skips any card `get_card_data` does not know, so a DB
holding only Ironclad makes the planner structurally inert for the other four
characters — it returns None and the PPO policy plays instead.
"""
from agent.sim.combat_step import get_card_data


def test_ironclad_cards_still_resolve():
    """The wiki-derived Ironclad entries stay authoritative — no regression."""
    bash = get_card_data("BASH")

    assert bash is not None
    kinds = [e["kind"] for e in bash["parsed"]["normal"]]
    assert kinds == ["deal_damage", "apply_status"]


def test_defect_cards_resolve():
    """Defect cards must be known, or the planner cannot drive Defect at all."""
    assert get_card_data("ZAP") is not None
    assert get_card_data("BALL_LIGHTNING") is not None


def test_defect_card_carries_parsed_effects():
    ball = get_card_data("BALL_LIGHTNING")

    assert any(e["kind"] == "deal_damage" for e in ball["parsed"]["normal"])


def test_extra_card_db_can_be_disabled(monkeypatch):
    """The expansion must be switchable: it changes DEFAULT-ON behaviour.

    get_card_data backs intent_defense_override's block/damage fallbacks, so
    adding characters is not a no-op for live play and has to be A/B-able.
    """
    import agent.sim.combat_step as cs

    monkeypatch.setenv("STS2_SIM_CARD_DB_EXTRA", "0")
    monkeypatch.setattr(cs, "_CARD_DB", None)
    assert cs.get_card_data("ZAP") is None
    assert cs.get_card_data("BASH") is not None

    monkeypatch.setenv("STS2_SIM_CARD_DB_EXTRA", "1")
    monkeypatch.setattr(cs, "_CARD_DB", None)
    assert cs.get_card_data("ZAP") is not None
