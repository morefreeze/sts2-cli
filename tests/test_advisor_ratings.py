from agent.build_advisor_ratings import parse_cards

SAMPLE_HTML = '''
<script>
const ANCHOR_BY_BUILD = {"build A": [
{"id": "CARD.PHANTOM_BLADES", "title": "x", "character": "SILENT", "type": "Power",
 "cost": 1, "rarity": "Uncommon", "tier": "B", "axes": ["SCALING", "SHIV"],
 "anchor_score": 9.7, "signals": {"S1_multiplier": 3.0}},
{"id": "CARD.AGGRESSION", "character": "IRONCLAD", "type": "Power", "cost": 1,
 "rarity": "Rare", "tier": "B", "axes": ["SCALING", "RANDOM"], "anchor_score": 8.0},
{"id": "CARD.PHANTOM_BLADES", "character": "SILENT", "tier": "B", "axes": ["SCALING", "SHIV"],
 "anchor_score": 9.7}
]};
</script>
'''


def test_parse_cards_extracts_and_dedupes():
    cards = parse_cards(SAMPLE_HTML)
    # normalized keys (CARD. stripped, upper-cased), deduped by id
    assert set(cards) == {"PHANTOM_BLADES", "AGGRESSION"}
    pb = cards["PHANTOM_BLADES"]
    assert pb["tier"] == "B"
    assert pb["axes"] == ["SCALING", "SHIV"]
    assert pb["character"] == "SILENT"
    assert pb["anchor_score"] == 9.7


def test_parse_cards_skips_blank_tier():
    html = '<script>x = {"id": "CARD.FOO", "tier": "", "axes": []};</script>'
    assert parse_cards(html) == {}


from agent import card_scoring


def test_advisor_ratings_loader_returns_dict():
    ratings = card_scoring._load_advisor_ratings()
    assert isinstance(ratings, dict)
    # generated in Task 2; a known Ironclad card should be present
    assert "AGGRESSION" in ratings
    assert ratings["AGGRESSION"]["tier"] in {"S", "A", "B", "C", "D"}


def test_tier_base_monotonic():
    tb = card_scoring._TIER_BASE
    assert tb["S"] > tb["A"] > tb["B"] > tb["C"] > tb["D"]


def test_context_bonus_rewards_draw_and_energy():
    # a skill that draws 2 and gives 1 energy → positive context bonus
    card = {"id": "CARD.TEST", "type": "skill", "cost": 1,
            "stats": {"cards": 2, "energy": 1}, "description": ""}
    assert card_scoring._context_bonus(card) > 0


def test_rated_card_uses_tier_base(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS",
                        {"ZED": {"tier": "D", "axes": [], "character": "SILENT"}})
    # A D-tier card with big raw damage must NOT score high — tier dominates the base.
    card = {"id": "CARD.ZED", "type": "attack", "cost": 1,
            "stats": {"damage": 40}, "description": ""}
    score = card_scoring.score_card(card)
    assert score <= card_scoring._TIER_BASE["D"] + 0.01  # base 2.0, no context bonus


def test_override_becomes_bounded_delta(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS",
                        {"ZED": {"tier": "C", "axes": [], "character": "IRONCLAD"}})
    monkeypatch.setitem(card_scoring.OVERRIDES, "ZED", 10.0)  # huge absolute override
    card = {"id": "CARD.ZED", "type": "skill", "cost": 1, "stats": {}, "description": ""}
    score = card_scoring.score_card(card)
    # base C=4.0; delta capped at +2.0 → 6.0, NOT the raw 10.0
    assert abs(score - (card_scoring._TIER_BASE["C"] + card_scoring._OVERRIDE_DELTA_CAP)) < 0.01


def test_unrated_card_unchanged(monkeypatch):
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS", {})  # nothing rated
    card = {"id": "CARD.UNRATED", "type": "attack", "cost": 1,
            "stats": {"damage": 6}, "description": ""}
    # heuristic path: Strike-like 6 dmg/1 cost → positive score, unchanged
    assert card_scoring.score_card(card) > 0
