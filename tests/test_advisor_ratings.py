from agent.build_advisor_ratings import build_card_db, parse_cards


def _row(**attrs) -> str:
    """Build a single `<tr class="card-row" ...>` fixture row.

    Every real row carries the same fixed set of data-* attributes,
    so give every fixture row sane defaults and let callers override.
    """
    defaults = dict(
        card_id="CARD.PHANTOM_BLADES",
        card_name="팬텀 블레이드",
        card_axes="SCALING,SHIV",
        card_tier="B",
        card_cost="1",
        star_cost="0",
        card_ev="9.7",
        card_tags="",
        card_desc="desc line 1\ndesc line 2",
        upg_desc="upgraded desc",
        upg_cost="1",
        upg_axes="SCALING,SHIV,UPGRADED",
        card_damage="-1",
        card_block="-1",
        card_vars="",
        upg_damage="-1",
        upg_block="-1",
        upg_vars="",
        card_type="Power",
        card_rarity="Uncommon",
        card_versions="v0.103.2,v0.103.3",
        anchor_tier="2",
        anchor_score="9.7",
        anchor_axis="SHIV",
        sim_best_scenario="",
        owner_char="사일런트",
        origin_char="사일런트",
    )
    defaults.update(attrs)
    attr_str = " ".join(f'data-{k.replace("_", "-")}="{v}"' for k, v in defaults.items())
    return (
        f'<tr class="card-row" {attr_str}>'
        f'<td>row body, never parsed</td></tr>'
    )


def test_parse_cards_extracts_and_dedupes():
    # base row wins over a same-id "+"-suffixed (upgraded) row, regardless
    # of document order.
    html = (
        _row(card_id="CARD.PHANTOM_BLADES", card_name="팬텀 블레이드+", card_tier="A")
        + _row(card_id="CARD.PHANTOM_BLADES", card_name="팬텀 블레이드", card_tier="B")
        + _row(card_id="CARD.AGGRESSION", card_name="어그레션", card_tier="B",
               owner_char="아이언클래드", origin_char="아이언클래드",
               card_axes="SCALING,RANDOM", anchor_score="8.0")
    )
    cards = parse_cards(html)
    # normalized keys (CARD. stripped, upper-cased), deduped by id
    assert set(cards) == {"PHANTOM_BLADES", "AGGRESSION"}
    pb = cards["PHANTOM_BLADES"]
    assert pb["tier"] == "B"  # base row wins, not the "+" row seen first
    assert pb["axes"] == ["SCALING", "SHIV"]
    assert pb["character"] == "SILENT"
    assert pb["anchor_score"] == 9.7


def test_parse_cards_keeps_upg_row_when_no_base_row_exists():
    html = _row(card_id="CARD.SOLO_UPG", card_name="솔로+", card_tier="A")
    cards = parse_cards(html)
    assert "SOLO_UPG" in cards
    assert cards["SOLO_UPG"]["tier"] == "A"


def test_parse_cards_skips_blank_tier():
    html = _row(card_tier="")
    assert parse_cards(html) == {}


def test_parse_cards_filters_hangul_axes():
    html = _row(card_axes="SCALING,강화 가치,SHIV")
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["axes"] == ["SCALING", "SHIV"]


def test_parse_cards_maps_korean_character_names():
    cases = [
        ("아이언클래드", "IRONCLAD"),
        ("사일런트", "SILENT"),
        ("디펙트", "DEFECT"),
        ("네크로바인더", "NECROBINDER"),
        ("리젠트", "REGENT"),
        ("공용", "SHARED"),
    ]
    for kr, en in cases:
        html = _row(card_id=f"CARD.T_{en}", owner_char=kr, origin_char=kr)
        cards = parse_cards(html)
        assert cards[f"T_{en}"]["character"] == en


def test_parse_cards_uses_origin_not_owner_char():
    # Shared cards are rendered once per character *context*: six identical
    # rows whose data-owner-char names the context. Only data-origin-char
    # says the card is shared, so the character must come from origin —
    # otherwise a shared card is filed under whichever context sorted first.
    html = "".join(
        _row(card_id="CARD.SHARED_ONE", owner_char=kr, origin_char="공용")
        for kr in ("사일런트", "아이언클래드", "리젠트")
    )
    cards = parse_cards(html)
    assert cards["SHARED_ONE"]["character"] == "SHARED"


def test_parse_cards_drops_unknown_origin_char():
    # the advisor page occasionally leaves an unresolved template literal
    # in the character attributes; such rows must be dropped, not given a
    # bogus character.
    html = _row(origin_char="${CSS.escape(char)}")
    assert parse_cards(html) == {}


def test_parse_cards_na_damage_block_become_none():
    html = _row(card_damage="-1", card_block="-1")
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["damage"] is None
    assert cards["PHANTOM_BLADES"]["block"] is None


def test_parse_cards_damage_block_present():
    html = _row(card_damage="5", card_block="8")
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["damage"] == 5
    assert cards["PHANTOM_BLADES"]["block"] == 8


def test_parse_cards_parses_vars():
    html = _row(card_vars="Cards:1,Damage:5")
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["vars"] == {"Cards": 1, "Damage": 5}


def test_parse_cards_empty_vars_is_empty_dict():
    html = _row(card_vars="")
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["vars"] == {}


def test_parse_cards_extra_fields():
    html = _row(card_ev="14.4", card_cost="2", card_type="Skill", card_rarity="Rare",
                card_versions="v0.103.2,v0.103.3,v0.107.1")
    cards = parse_cards(html)
    card = cards["PHANTOM_BLADES"]
    assert card["ev"] == 14.4
    assert card["cost"] == 2
    assert card["type"] == "Skill"
    assert card["rarity"] == "Rare"
    assert card["versions"] == ["v0.103.2", "v0.103.3", "v0.107.1"]


def test_parse_cards_unparseable_cost_is_none():
    html = _row(card_cost="X")  # the advisor uses "X" for variable-cost cards
    cards = parse_cards(html)
    assert cards["PHANTOM_BLADES"]["cost"] is None


# --- build_card_db (data/card_db_advisor.json) ----------------------------

def test_build_card_db_has_full_field_set():
    html = _row(card_id="CARD.PHANTOM_BLADES", card_axes="SCALING,SHIV",
                upg_axes="SCALING,SHIV,UPGRADED", card_cost="1", upg_cost="0",
                card_damage="-1", upg_damage="8", card_block="-1", upg_block="-1")
    db = build_card_db(html)
    card = db["PHANTOM_BLADES"]
    assert set(card) == {
        "cost", "type", "rarity", "tier", "ev", "damage", "block", "vars",
        "axes", "upg_axes", "upg_cost", "upg_damage", "upg_block",
        "character", "versions",
    }
    assert card["axes"] == ["SCALING", "SHIV"]
    assert card["upg_axes"] == ["SCALING", "SHIV", "UPGRADED"]
    assert card["cost"] == 1
    assert card["upg_cost"] == 0
    assert card["damage"] is None      # -1 sentinel -> None
    assert card["upg_damage"] == 8
    assert card["block"] is None
    assert card["upg_block"] is None   # -1 sentinel -> None


def test_build_card_db_dedupes_base_row_wins_like_parse_cards():
    html = (
        _row(card_id="CARD.PHANTOM_BLADES", card_name="팬텀 블레이드+", card_tier="A",
             upg_axes="UPGRADED_ONLY")
        + _row(card_id="CARD.PHANTOM_BLADES", card_name="팬텀 블레이드", card_tier="B",
               upg_axes="BASE_ROW_UPG")
    )
    db = build_card_db(html)
    assert db["PHANTOM_BLADES"]["tier"] == "B"  # base row wins, same as parse_cards
    assert db["PHANTOM_BLADES"]["upg_axes"] == ["BASE_ROW_UPG"]


def test_build_card_db_skips_blank_tier_and_unknown_char_same_as_parse_cards():
    assert build_card_db(_row(card_tier="")) == {}
    assert build_card_db(_row(origin_char="${CSS.escape(char)}")) == {}


def test_build_card_db_and_parse_cards_agree_on_card_set():
    # Both are built from the same _dedupe_rows() pass over the same rows,
    # so the set of surviving card ids must always match exactly.
    html = (
        _row(card_id="CARD.PHANTOM_BLADES")
        + _row(card_id="CARD.AGGRESSION", owner_char="아이언클래드", origin_char="아이언클래드")
        + _row(card_tier="")  # dropped
    )
    assert set(build_card_db(html)) == set(parse_cards(html))


def test_build_card_db_filters_hangul_from_upg_axes():
    html = _row(upg_axes="SCALING,강화 가치,SHIV")
    db = build_card_db(html)
    assert db["PHANTOM_BLADES"]["upg_axes"] == ["SCALING", "SHIV"]


def test_build_card_db_real_advisor_html_matches_established_facts():
    # Established facts from the task spec (already-downloaded advisor.html
    # snapshot): 507 unique cards; IRONCLAD 85 + SHARED 79 = 164; 92 distinct
    # axes among IRONCLAD+SHARED cards.
    import os as _os2
    path = _os2.path.expanduser("~/.sts2-train/data/advisor.html")
    if not _os2.path.exists(path):
        import pytest as _pytest
        _pytest.skip("no local advisor.html snapshot")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    db = build_card_db(html)
    assert len(db) == 507
    ironclad_shared = {cid: r for cid, r in db.items()
                       if r["character"] in ("IRONCLAD", "SHARED")}
    assert len(ironclad_shared) == 164
    axis_vocab = {a for r in ironclad_shared.values() for a in r["axes"]}
    assert len(axis_vocab) == 92
    assert set(db) == set(parse_cards(html))


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


def test_card_tags_fall_back_to_advisor(monkeypatch):
    # Ironclad hand-tuned map has NO entry for this Silent card...
    monkeypatch.setattr(card_scoring, "_CARD_TAGS", {})
    monkeypatch.setattr(card_scoring, "_ADVISOR_TAGS",
                        {"PHANTOM_BLADES": ["SCALING", "SHIV"]})
    tags = card_scoring._card_tags({"id": "CARD.PHANTOM_BLADES"})
    assert "SHIV" in tags and "SCALING" in tags


def test_card_tags_prefers_handtuned(monkeypatch):
    # When both exist, hand-tuned Ironclad tags win (union with advisor).
    monkeypatch.setattr(card_scoring, "_CARD_TAGS", {"AGGRESSION": ["SCALING_PILLAR"]})
    monkeypatch.setattr(card_scoring, "_ADVISOR_TAGS", {"AGGRESSION": ["RANDOM"]})
    tags = card_scoring._card_tags({"id": "CARD.AGGRESSION"})
    assert "SCALING_PILLAR" in tags  # hand-tuned preserved


import sys as _sys, os as _os_t
_sys.path.insert(0, _os_t.path.join(_os_t.path.dirname(__file__), "..", "python"))
import play_full_run


def test_summarize_reports_avg_floor():
    results = [
        {"victory": False, "seed": "run_1", "floor": 12, "act": 1, "steps": 50},
        {"victory": True,  "seed": "run_2", "floor": 17, "act": 3, "steps": 90},
        {"victory": False, "seed": "run_3", "floor": "?", "act": 1, "steps": 40},
    ]
    out = play_full_run.summarize(results, num_runs=3, character="Silent")
    assert "Wins: 1/3" in out
    assert "Completed: 3/3" in out
    # avg over numeric floors (12, 17) → 14.5
    assert "avg_floor=14.5" in out


def test_broken_card_beats_advisor_rating():
    # WHIRLWIND is in BOTH BROKEN_CARDS and the advisor ratings. The BROKEN check
    # runs before the advisor branch, so it must still score 0.0 (locks invariant:
    # broken-before-advisor ordering). Uses the real committed data (no monkeypatch).
    assert "WHIRLWIND" in card_scoring.BROKEN_CARDS
    assert "WHIRLWIND" in card_scoring._load_advisor_ratings()
    card = {"id": "CARD.WHIRLWIND", "type": "attack", "cost": 1,
            "stats": {"damage": 5}, "description": ""}
    assert card_scoring.score_card(card) == 0.0


def test_override_negative_delta_uncapped(monkeypatch):
    # A rated S-tier card whose OVERRIDE is BELOW the tier base → negative delta,
    # within the ±cap so it is NOT clamped. Confirms the lower/uncapped delta path.
    monkeypatch.setattr(card_scoring, "_ADVISOR_RATINGS",
                        {"ZED": {"tier": "S", "axes": [], "character": "IRONCLAD"}})
    monkeypatch.setitem(card_scoring.OVERRIDES, "ZED", 8.0)  # below S base 9.5
    card = {"id": "CARD.ZED", "type": "skill", "cost": 1, "stats": {}, "description": ""}
    score = card_scoring.score_card(card)
    # base S=9.5; delta = 8.0 - 9.5 = -1.5 (within ±2.0) → 8.0, no context bonus
    assert abs(score - 8.0) < 0.01
