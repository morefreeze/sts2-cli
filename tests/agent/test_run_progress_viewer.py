from agent.run_progress_viewer import (
    build_translation_catalog,
    extract_js_assignment_object,
)


def test_extract_js_assignment_object_handles_nested_strings():
    text = """
window.__CARDS_LOCALE__ = {"CARD.TEST": {"name": {"en": "Brace", "zh": "花括号"}, "desc": {"zh": "包含 } 字符"}}};
"""

    assert extract_js_assignment_object(text, "window.__CARDS_LOCALE__") == {
        "CARD.TEST": {
            "name": {"en": "Brace", "zh": "花括号"},
            "desc": {"zh": "包含 } 字符"},
        }
    }


def test_build_translation_catalog_maps_cards_and_relics_to_chinese():
    cards_locale = {
        "CARD.POMMEL_STRIKE": {
            "name": {"en": "Pommel Strike", "zh": "剑柄打击"},
            "desc": {"en": "Deal damage.", "zh": "造成伤害。"},
        },
        "CARD.SHRUG_IT_OFF+": {
            "name": {"en": "Shrug It Off+", "zh": "耸肩无视+"},
            "desc": {"en": "Gain Block.", "zh": "获得格挡。"},
        },
    }
    relic_info = {
        "RELIC.AKABEKO": {
            "name": "아카베코",
            "description": "매 전투 시작 시 활력을 얻습니다.",
            "name_loc": {"en": "Akabeko", "zh": "赤牛"},
            "desc_loc": {"en": "Gain Vigor.", "zh": "获得活力。"},
        }
    }

    catalog = build_translation_catalog(cards_locale, relic_info, lang="zh")

    assert catalog["cards"]["CARD.POMMEL_STRIKE"] == "剑柄打击"
    assert catalog["card_names"]["Pommel Strike"] == "剑柄打击"
    assert catalog["cards"]["CARD.SHRUG_IT_OFF+"] == "耸肩无视+"
    assert catalog["card_names"]["Shrug It Off+"] == "耸肩无视+"
    assert catalog["relics"]["RELIC.AKABEKO"] == "赤牛"
    assert catalog["relic_names"]["Akabeko"] == "赤牛"
