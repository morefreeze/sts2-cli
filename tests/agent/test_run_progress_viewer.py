from agent.run_progress_viewer import (
    _build_cohort_tree,
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


def _tree_cohort(cohort_id, version, latest_at, character="Ironclad"):
    return {
        "cohort_id": cohort_id,
        "label": cohort_id,
        "run_count": 1,
        "technical_count": 0,
        "latest_at": latest_at,
        "filters": {"game_version": version, "character": character},
    }


def _tree_versions(cohorts):
    return [entry["game_version"] for entry in _build_cohort_tree(cohorts)]


def test_tree_orders_version_groups_by_newest_latest_at_null_bucket_included():
    # The unarchived (null-version) bucket holds the newest batch, so it leads.
    assert _tree_versions(
        [
            _tree_cohort("a", "v0.111.0", 100.0),
            _tree_cohort("b", None, 300.0),
            _tree_cohort("c", "v0.50.0", 200.0),
        ]
    ) == [None, "v0.50.0", "v0.111.0"]
    # ... and with only older batches it stays below the newer versioned group.
    assert _tree_versions(
        [
            _tree_cohort("a", "v0.111.0", 100.0),
            _tree_cohort("b", None, 50.0),
            _tree_cohort("c", "v0.50.0", 200.0),
        ]
    ) == ["v0.50.0", "v0.111.0", None]


def test_tree_group_uses_its_newest_cohort_and_undated_groups_go_last():
    assert _tree_versions(
        [
            _tree_cohort("old", None, 10.0),
            _tree_cohort("newer", None, 400.0, character="Silent"),
            _tree_cohort("v", "v1", 200.0),
            _tree_cohort("undated-v", "v2", None),
            _tree_cohort("undated-v3", "v3", None),
        ]
    ) == [None, "v1", "v2", "v3"]


def test_tree_ties_put_versioned_before_null_then_version_string():
    cohorts = [
        _tree_cohort("n", None, 100.0),
        _tree_cohort("b", "v-b", 100.0),
        _tree_cohort("a", "v-a", 100.0),
    ]
    assert _tree_versions(cohorts) == ["v-a", "v-b", None]
    assert _tree_versions(list(reversed(cohorts))) == ["v-a", "v-b", None]
    # Groups without any latest_at tie as well, with the same tiebreak.
    undated = [
        _tree_cohort("n", None, None),
        _tree_cohort("z", "v-z", None),
        _tree_cohort("a", "v-a", None),
    ]
    assert _tree_versions(undated) == ["v-a", "v-z", None]
