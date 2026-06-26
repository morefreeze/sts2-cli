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
