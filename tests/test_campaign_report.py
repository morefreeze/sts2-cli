"""tools/campaign_report.py on a synthetic user dir (no real game data needed)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import campaign_report as cr  # noqa: E402


def point(ptype, hp, max_hp, damage=0, healed=0, gold=100, gained=0, spent=0, picks=None,
          gained_cards=0, removed=0, rest=None, encounter=None, turns=3):
    ps = {"current_hp": hp, "max_hp": max_hp, "damage_taken": damage, "hp_healed": healed,
          "current_gold": gold, "gold_gained": gained, "gold_spent": spent}
    if picks is not None:
        ps["card_choices"] = [{"card": {"id": f"CARD.C{i}"}, "was_picked": i == 0 and picks}
                              for i in range(3)]
    if gained_cards:
        ps["cards_gained"] = [{"id": "CARD.X"}] * gained_cards
    if removed:
        ps["cards_removed"] = [{"id": "CARD.Y"}] * removed
    if rest:
        ps["rest_site_choices"] = [rest]
    out = {"map_point_type": ptype, "player_stats": [ps]}
    if encounter:
        out["rooms"] = [{"model_id": f"ENCOUNTER.{encounter}", "turns_taken": turns}]
    return out


def write_run(path, acts):
    path.write_text(json.dumps({"map_point_history": acts}))
    return str(path)


def row(start, char, asc, win, deck, hist, gold=50, floor=20, ts="2026-11-02T10:00:00Z"):
    return {"ts": ts, "character": char, "ascension": asc, "victory": win, "act": 1, "floor": 1,
            "total_floor": floor, "hp": 0, "max_hp": 80, "gold": gold, "deck": ["CARD.A"] * deck,
            "relics": [], "potions": [], "killed_by": "ENCOUNTER.B", "history_path": hist,
            "start_time": start}


def trace(decision, state, reply=None, run=None, ts="2026-11-02T10:00:00Z"):
    rec = {"ts": ts, "decision": decision, "state": state, "reply": reply or {}}
    if run:
        rec["run"] = run
    return rec


@pytest.fixture
def user_dir(tmp_path):
    # run 1000: a10 Necro loss. Act 1 fully played, boss beaten after a HEAL; dies in act 2.
    r1 = write_run(tmp_path / "1000.run", [
        [point("monster", 70, 80, damage=4, gained=10, picks=True, gained_cards=1, encounter="M1"),
         point("elite", 58, 80, damage=12, picks=False, spent=30),
         point("rest_site", 68, 80, healed=10, rest="HEAL"),
         point("boss", 40, 80, damage=20, gold=120, gained=40, picks=False, encounter="BOSS_A")],
        [point("monster", 0, 80, damage=40, gold=60, spent=60, removed=1, encounter="M2")]])
    # run 2000: a10 Regent loss at act 1 boss.
    r2 = write_run(tmp_path / "2000.run", [
        [point("monster", 60, 70, damage=10, encounter="M1"),
         point("rest_site", 70, 70, healed=10, rest="SMITH"),
         point("boss", 0, 70, damage=70, encounter="BOSS_A", turns=5)]])
    # run 3000: a2 Ironclad win; act 1 only.
    r3 = write_run(tmp_path / "3000.run", [
        [point("monster", 80, 80, damage=0, encounter="M1"),
         point("boss", 50, 80, damage=30, picks=True, gained_cards=1, encounter="BOSS_A")]])
    rows = [row(1000, "NECROBINDER", 10, False, deck=13, hist=r1, gold=60, floor=19),
            row(2000, "REGENT", 10, False, deck=12, hist=r2, gold=300, floor=17),
            row(3000, "IRONCLAD", 2, True, deck=12, hist=r3, gold=80, floor=48, ts="2026-10-01T00:00:00Z"),
            row(4000, "DEFECT", 10, False, deck=10, hist=str(tmp_path / "missing.run"))]
    (tmp_path / "campaign_runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    run1 = {"start_time": 1000, "seed": "S", "character": "NECROBINDER", "ascension": 10}
    la = {"key": "map.la_best_path", "outcome": "fired", "values": {"floor": 4, "hp_ratio": 0.9, "hp_boss_ratio": 0.9}}
    lines = [
        trace("map_select", {"context": {"act": 1, "floor": 4}, "player": {"hp": 70, "max_hp": 80},
                             "choices": [{"type": "Monster"}, {"type": "Elite"}]},
              {"rules": [la]}, run=run1),
        trace("map_select", {"context": {"act": 1, "floor": 7}, "player": {"hp": 40, "max_hp": 80},
                             "choices": [{"type": "Boss"}]}, {"rules": []}, run=run1),
        trace("event_choice",
              {"options": [{"index": 0, "title": "Mystery"}, {"index": 1, "title": "Known"}]},
              {"options": [{"kind": "option", "index": 0, "score": 0.0, "status": "ok", "components": []},
                           {"kind": "option", "index": 1, "score": 0.0, "status": "ok", "components": []}],
               "debug": {"event_unscored": ["Mystery", "Known"], "event_all_zero": True}}, run=run1),
        trace("event_choice",
              {"options": [{"index": 0, "title": "Fine"}]},
              {"options": [{"kind": "option", "index": 0, "score": 2.0, "status": "ok",
                            "components": [{"key": "event.kw.x", "value": 2.0}]}]}, run=run1),
        trace("card_reward", {"context": {"act": 1}, "player": {"deck_size": 12}},
              {"options": [{"kind": "card", "index": 0, "score": 9.0, "status": "ok",
                            "components": [{"key": "card.base", "value": 7.5},
                                           {"key": "card.archetype", "value": 0.0}]},
                           {"kind": "card", "index": 1, "score": 5.0, "status": "ok",
                            "components": [{"key": "card.base", "value": 4.0},
                                           {"key": "card.archetype", "value": 1.0}]}],
               "debug": {"card_reward": {"margin": 1.5}}}, run=run1),
        trace("combat_end", {"encounter": "M1", "room_type": "Monster", "act": 1, "floor": 2,
                             "won": True, "turns": 3, "hp_start": 80, "hp_end": 70, "damage_taken": 10},
              run=run1),
        # older, run-less lines: segment 0 ends at its run_end (names run 3000)
        trace("map_select", {"context": {"act": 1, "floor": 3}, "player": {"hp": 80, "max_hp": 80},
                             "choices": [{"type": "Monster"}, {"type": "Elite"}]},
              {"rules": [{"key": "map.la_best_path", "outcome": "fired",
                          "values": {"floor": 3, "hp_ratio": 1.0, "hp_boss_ratio": 1.0}}]},
              ts="2026-10-01T00:00:00Z"),
        trace("map_select", {"context": {"act": 1, "floor": 8}, "player": {"hp": 50, "max_hp": 80},
                             "choices": [{"type": "Boss"}]}, ts="2026-10-01T00:00:00Z"),
        trace("run_end", rows[2], ts="2026-10-01T00:00:00Z"),
    ]
    (tmp_path / "advisor_trace.1.jsonl").write_text("\n".join(json.dumps(x) for x in lines[:5]) + "\n")
    (tmp_path / "advisor_trace.jsonl").write_text("\n".join(json.dumps(x) for x in lines[5:]) + "\nnot json\n")
    return str(tmp_path)


@pytest.fixture
def report(user_dir):
    return cr.build_report(user_dir)


def test_missing_history_is_skipped_and_counted(report):
    _, data = report
    assert data["inputs"]["campaign_rows"] == 4
    assert data["inputs"]["runs_with_history"] == 3
    assert data["inputs"]["history_missing"] == 1


def test_runs_per_band_and_character(report):
    runs = report[1]["runs"]
    assert runs["total"] == {"n": 4, "wins": 1, "avg_floor": pytest.approx((19 + 17 + 48 + 20) / 4)}
    assert runs["by_band"]["a9-10"]["n"] == 3 and runs["by_band"]["a9-10"]["wins"] == 0
    assert runs["by_band"]["a0-5"]["wins"] == 1
    assert runs["by_character"]["REGENT"]["n"] == 1


def test_damage_per_band_act_and_room_type(report):
    dmg = report[1]["damage"]["a9-10"]
    monster = dmg["acts"][1]["types"]["monster"]
    assert monster["damage"]["n"] == 2 and monster["damage"]["mean"] == 7.0  # 4 and 10
    assert monster["rooms_per_run"] == 1.0
    assert dmg["acts"][1]["types"]["boss"]["damage"]["mean"] == 45.0  # 20 and 70
    assert dmg["acts"][2]["types"]["monster"]["damage"]["median"] == 40


def test_boss_hp_pass_fail_and_pre_boss_rest(report):
    boss = report[1]["boss"]["a9-10"]
    act1 = boss["by_act"][1]
    assert (act1["reached"], act1["passed"]) == (2, 1)
    assert act1["hp_in_pass"] == pytest.approx(60 / 80)  # 40 left + 20 damage
    assert act1["hp_in_fail"] == pytest.approx(70 / 70)
    assert boss["pre_boss_rest"]["HEAL"]["n"] == 1 and boss["pre_boss_rest"]["HEAL"]["passed"] == 1
    assert boss["pre_boss_rest"]["SMITH"]["passed"] == 0
    assert boss["pre_boss_rest"]["HEAL"]["hp_before"] == pytest.approx(58 / 80)


def test_gold_and_deck_size(report):
    gold = report[1]["gold"]["a9-10"]
    assert gold["gold_at_death"]["n"] == 2 and gold["gold_at_death"]["mean"] == 180
    assert gold["losses_with_300_plus"] == 1
    assert gold["spent_per_run"] == pytest.approx((90 + 0) / 2)
    assert gold["gold_at_act_end"][1]["mean"] == 120  # only run 1000 beat the act-1 boss
    cards = report[1]["cards"]["a9-10"]
    # run 1000: final deck 13, the act-2 room removed a card -> 14 entering the act-1 boss; run 2000: 12
    assert cards["deck_size_entering_boss"][1]["n"] == 2
    assert cards["deck_size_entering_boss"][1]["mean"] == 13.0
    assert cards["picks"]["A1 monster"] == {"pick": 1, "skip": 0}
    assert cards["picks"]["A1 boss"] == {"pick": 0, "skip": 1}


def test_trace_grouping_run_field_and_run_end_segments(user_dir):
    groups = cr.group_trace(cr.load_trace(user_dir))
    assert len(groups) == 2
    by_run = next(g for g in groups if g["key"] == ("t", 1000))
    assert len(by_run["recs"]) == 6 and by_run["meta"]["character"] == "NECROBINDER"
    seg = next(g for g in groups if g["key"][0] == "seg")
    assert seg["meta"]["character"] == "IRONCLAD" and seg["meta"]["ascension"] == 2
    assert [r["decision"] for r in seg["recs"]][-1] == "run_end"


def test_route_predicted_vs_actual(report):
    route = report[1]["route"]
    early = route["A1 early(<=6)"]
    assert early["n"] == 2  # one per run group
    assert early["predicted"] == pytest.approx((0.9 + 1.0) / 2)
    assert early["actual"] == pytest.approx((0.5 + 50 / 80) / 2)
    assert early["overpredict_gt_0.2"] == 1.0


def test_event_zero_titles_and_debug(report):
    events = report[1]["events"]
    assert events["decisions"] == 2 and events["all_options_zero"] == 1
    assert dict(events["zero_score_titles"]) == {"Mystery": 1, "Known": 1}
    assert events["debug"]["event_all_zero"] == 1
    assert dict(events["debug"]["titles"]) == {"Mystery": 1, "Known": 1}


def test_card_score_components_and_margin(report):
    a1 = report[1]["card_scores"]["A1"]
    assert a1["n"] == 1 and a1["best_score_median"] == 9.0 and a1["deck_size_median"] == 12
    assert a1["components"]["card.base"]["mean"] == pytest.approx(5.75)
    assert a1["components"]["card.archetype"]["frac_nonzero"] == 0.5
    assert a1["margin"]["mean"] == 1.5


def test_combat_end_section(report):
    text, data = report
    cell = data["combat_end"]["a9-10"]["A1 monster"]
    assert cell["n"] == 1 and cell["won"] == 1 and cell["damage"]["mean"] == 10
    assert "2b. Damage per room from combat_end" in text


def test_filters(user_dir):
    _, data = cr.build_report(user_dir, min_asc=9)
    assert data["runs"]["total"]["n"] == 3 and "a0-5" not in data["runs"]["by_band"]
    assert data["inputs"]["trace_groups"] == 1  # the run-less segment is ascension 2
    _, data = cr.build_report(user_dir, character="regent")
    assert data["runs"]["total"]["n"] == 1
    _, data = cr.build_report(user_dir, since="2026-11-01")
    assert data["runs"]["total"]["n"] == 3


def test_main_prints_and_writes_json(user_dir, tmp_path, capsys):
    out = tmp_path / "out.json"
    assert cr.main(["--user-dir", user_dir, "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "== 1. Runs ==" in printed and "== 8. Trace: card_reward score components ==" in printed
    assert json.loads(out.read_text())["runs"]["total"]["n"] == 4


def test_missing_user_dir_is_an_error(tmp_path, capsys):
    assert cr.main(["--user-dir", str(tmp_path / "nope")]) == 2


def test_empty_user_dir_still_reports(tmp_path):
    text, data = cr.build_report(str(tmp_path))
    assert data["runs"]["total"]["n"] == 0 and "== 6." in text
