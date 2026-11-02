"""The Advisor Play tab's data: the Advisor mod's real-game runs (``advisor_play.py``).

Everything runs on small files written under tmp dirs -- the real saves are never read.
The shapes copied here are the game's: ``saves/history/<start_time>.run`` (``win``,
``ascension``, ``killed_by_*``, ``players[0]``, ``map_point_history`` = acts of floors with
``map_point_type`` / ``rooms`` / ``player_stats``), ``current_run.save`` (same, plus
``character_id`` / ``current_hp`` on the player), ``progress.save`` (``character_stats``),
the mod's ``campaign_runs.jsonl`` / ``campaign.json`` / ``Sts2Advisor.log``.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench import advisor_play as ap
from agent.run_workbench.catalog import RunCatalog

BASE = 1_793_200_000  # after ADVISOR_SINCE (2026-10-28 00:00 Asia/Shanghai = 1793116800)
NOW = BASE + 10_000


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def point(map_type, name, dmg, hp, max_hp=80, room_type=None, turns=3, rooms=None):
    return {
        "map_point_type": map_type,
        "rooms": rooms or [{"model_id": name, "room_type": room_type or map_type, "turns_taken": turns}],
        "player_stats": [{"current_hp": hp, "max_hp": max_hp, "damage_taken": dmg, "current_gold": 99}],
    }


def standard_acts(final_hp=0):
    """Act 1: ancient, monster, ? (a fight), elite, rest, boss.  Act 2: ancient, elite, boss."""
    return [
        [
            point("ancient", "EVENT.NEOW", 0, 70, room_type="event"),
            point("monster", "ENCOUNTER.NIBBITS_WEAK", 10, 60),                    # A1F2
            point("unknown", "ENCOUNTER.TOADPOLES_WEAK", 4, 56, room_type="monster"),  # A1F3: a "?" fight
            point("elite", "ENCOUNTER.BYGONE_EFFIGY_ELITE", 30, 26),                 # A1F4: starts at 56/80 = 70%
            point("rest_site", "REST", 0, 70, room_type="rest_site"),                # A1F5
            point("boss", "ENCOUNTER.VANTOM_BOSS", 22, 48),                          # A1F6
        ],
        [
            point("ancient", "EVENT.TANX", 0, 80, room_type="event"),                # A2F1: healed to full
            point("elite", "ENCOUNTER.SOUL_NEXUS_ELITE", 50, 30),                    # A2F2: starts at 80/80
            point("boss", "ENCOUNTER.AEONGLASS_BOSS", 30, final_hp),                 # A2F3: dies here (final_hp 0)
        ],
    ]


def run_file(start, character="IRONCLAD", ascension=3, win=False, killed="ENCOUNTER.AEONGLASS_BOSS",
             abandoned=False, acts=None, deck=None, relics=None, final_hp=0):
    deck = deck if deck is not None else (
        [{"id": "CARD.STRIKE_IRONCLAD"}] * 3 + [{"id": "CARD.BASH", "current_upgrade_level": 1}] * 2)
    relics = relics if relics is not None else [{"id": "RELIC.BURNING_BLOOD"}, {"id": "RELIC.ANCHOR"}]
    return {
        "acts": ["ACT.OVERGROWTH", "ACT.HIVE", "ACT.GLORY"],
        "ascension": ascension,
        "killed_by_encounter": "NONE.NONE" if win else killed,
        "killed_by_event": "NONE.NONE",
        "map_point_history": acts if acts is not None else standard_acts(final_hp),
        "players": [{"character": f"CHARACTER.{character}", "deck": deck, "relics": relics, "potions": [], "id": 1}],
        "run_time": 1800,
        "seed": f"SEED{start}",
        "start_time": start,
        "was_abandoned": abandoned,
        "win": win,
    }


class Env:
    """A fake modded profile + advisor dir + journal under tmp_path."""

    def __init__(self, root: Path) -> None:
        self.saves = root / "saves"
        self.advisor = root / "Sts2Advisor"
        self.journal = root / "journal.txt"
        (self.saves / "history").mkdir(parents=True)
        self.advisor.mkdir()
        self.paths = ap.AdvisorPlayPaths.from_dirs(self.saves, self.advisor, self.journal)

    def add_run(self, start, **kwargs):
        run = run_file(start, **kwargs)
        (self.saves / "history" / f"{start}.run").write_text(json.dumps(run), encoding="utf-8")
        return run

    def write(self, name, data, where="advisor"):
        target = (self.advisor if where == "advisor" else self.saves) / name
        target.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def payload(self, **kwargs):
        return ap.build_payload(self.paths, now=kwargs.pop("now", NOW), **kwargs)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def progress(**ascensions):
    return {"character_stats": [
        {"id": f"CHARACTER.{name}", "max_ascension": asc, "total_wins": asc * 2, "total_losses": asc * 3}
        for name, asc in ascensions.items()
    ] + [{"id": "CHARACTER.RANDOM_CHARACTER", "max_ascension": 0, "total_wins": 0, "total_losses": 0}]}


def log_line(ts: dt.datetime, text: str) -> str:
    return f"{ts:%Y-%m-%d %H:%M:%S}.123 INFO {text}"


# ---------------------------------------------------------------------------
# What counts as an Advisor run
# ---------------------------------------------------------------------------


def test_advisor_since_is_the_first_advisor_day_in_shanghai():
    shanghai = dt.timezone(dt.timedelta(hours=8))
    assert ap.ADVISOR_SINCE == int(dt.datetime(2026, 10, 28, tzinfo=shanghai).timestamp()) == 1_793_116_800
    assert ap.AdvisorPlayPaths.from_dirs(Path("s"), Path("a"), Path("j")).since == ap.ADVISOR_SINCE


def test_only_runs_since_the_first_advisor_day_are_listed_and_old_files_are_never_opened(env: Env):
    env.add_run(ap.ADVISOR_SINCE - 5, character="SILENT")                 # the day before: not an Advisor run
    (env.saves / "history" / f"{ap.ADVISOR_SINCE - 100}.run").write_text("{ not json", encoding="utf-8")
    env.add_run(ap.ADVISOR_SINCE, character="DEFECT")                    # exactly on the boundary: counts
    env.add_run(BASE, character="IRONCLAD")
    payload = env.payload()
    assert [r["char"] for r in payload["runs"]] == ["IRONCLAD", "DEFECT"]       # newest first
    assert payload["problems"] == []   # the unreadable old file was skipped by name, not opened


def test_the_unmodded_profile_is_never_read(tmp_path: Path):
    game = tmp_path / "Library" / "Application Support" / "SlayTheSpire2" / "steam" / "123"
    modded = game / "modded" / "profile1" / "saves" / "history"
    vanilla = game / "profile1" / "saves" / "history"
    modded.mkdir(parents=True)
    vanilla.mkdir(parents=True)
    (modded / f"{BASE}.run").write_text(json.dumps(run_file(BASE, character="IRONCLAD")), encoding="utf-8")
    (vanilla / f"{BASE + 1}.run").write_text(json.dumps(run_file(BASE + 1, character="SILENT")), encoding="utf-8")
    (vanilla.parent / "progress.save").write_text(json.dumps(progress(SILENT=10)), encoding="utf-8")

    paths = ap.AdvisorPlayPaths.default(home=tmp_path)
    inside_modded = [paths.history_dir, paths.current_run, paths.progress]
    assert all("modded" in p.parts for p in inside_modded)
    assert all(vanilla.parent not in p.parents and p != vanilla.parent for p in vars(paths).values() if isinstance(p, Path))
    payload = ap.build_payload(paths, now=NOW)
    assert [r["char"] for r in payload["runs"]] == ["IRONCLAD"]
    assert next(c for c in payload["characters"] if c["character"] == "SILENT")["max_ascension"] == 0


# ---------------------------------------------------------------------------
# One run: the numbers
# ---------------------------------------------------------------------------


def test_summarise_run_reads_the_scalar_fields_the_way_analyze_runs_does(env: Env):
    env.add_run(BASE, character="REGENT", ascension=5, killed="ENCOUNTER.AEONGLASS_BOSS")
    (run,) = env.payload()["runs"]
    assert (run["char"], run["asc"], run["result"]) == ("REGENT", 5, "loss")
    assert run["end"] == "A2F3" and run["total_floor"] == 9          # the last floor reached, all floors counted
    assert (run["hp"], run["max_hp"]) == (0, 80)                     # the last floor's HP
    assert run["killed_by"] == "AEONGLASS_BOSS"                      # prefix dropped
    assert (run["deck"], run["upgraded"], run["relics"]) == (5, 2, 2)
    assert run["relic_names"] == ["BURNING_BLOOD", "ANCHOR"]
    assert run["run_time"] == 1800 and run["start"] == BASE and run["acts"] == 2


def test_a_win_has_no_killer_and_an_abandoned_run_is_neither_win_nor_loss(env: Env):
    env.add_run(BASE, win=True)
    env.add_run(BASE + 1, abandoned=True)
    payload = env.payload()
    abandoned, won = payload["runs"]
    assert (won["result"], won["killed_by"]) == ("win", None)
    assert abandoned["result"] == "abandoned"
    assert payload["summary"]["wins"] == 1 and payload["summary"]["losses"] == 0 and payload["summary"]["abandoned"] == 1
    assert payload["summary"]["win_rate"] == 1.0


def test_a_killing_event_is_the_killer_when_no_encounter_killed(env: Env):
    run = run_file(BASE)
    run["killed_by_encounter"] = "NONE.NONE"
    run["killed_by_event"] = "EVENT.DROWNING_BEACON"
    (env.saves / "history" / f"{BASE}.run").write_text(json.dumps(run), encoding="utf-8")
    assert env.payload()["runs"][0]["killed_by"] == "DROWNING_BEACON"


def test_floors_carry_kind_hp_after_and_hp_before(env: Env):
    env.add_run(BASE)
    floors = env.payload()["runs"][0]["floors"]
    assert [(f["act"], f["floor"], f["kind"]) for f in floors[:6]] == [
        (1, 1, "ancient"), (1, 2, "monster"), (1, 3, "unknown_fight"), (1, 4, "elite"), (1, 5, "rest_site"), (1, 6, "boss"),
    ]
    elite = floors[3]
    assert (elite["elite"], elite["boss"], elite["dmg"], elite["hp"], elite["max"]) == (True, False, 30, 26, 80)
    assert (elite["hp_before"], elite["max_before"]) == (56, 80)       # the previous floor's HP after
    assert floors[0]["hp_before"] is None and floors[6]["hp_before"] is None     # an act's first floor
    assert floors[5]["boss"] is True and floors[5]["name"] == "VANTOM_BOSS"
    assert floors[0]["name"] == "NEOW"


def test_a_question_mark_node_that_leads_to_an_event_is_not_a_fight(env: Env):
    acts = [[point("unknown", "EVENT.SLIPPERY_BRIDGE", 7, 63, room_type="event")]]
    env.add_run(BASE, acts=acts)
    (floor,) = env.payload()["runs"][0]["floors"]
    assert floor["kind"] == "unknown_other" and floor["type"] == "unknown"


def test_a_run_with_odd_or_missing_pieces_does_not_break_the_page(env: Env):
    run = run_file(BASE, acts=[[{"map_point_type": "monster"}, "junk", {"rooms": "x", "player_stats": []}], "also junk"])
    run["players"] = [{"character": "CHARACTER.WATCHER-WATCHER"}]
    (env.saves / "history" / f"{BASE}.run").write_text(json.dumps(run), encoding="utf-8")
    payload = env.payload()
    assert payload["error"] is None
    (row,) = payload["runs"]
    assert row["char"] == "WATCHER-WATCHER" and row["deck"] == 0
    assert {c["id"]: c["count"] for c in payload["character_options"]}["OTHER"] == 1


# ---------------------------------------------------------------------------
# The run in progress
# ---------------------------------------------------------------------------


def current_save(start, character="IRONCLAD", ascension=7, hp=61, max_hp=80):
    return {
        "ascension": ascension, "current_act_index": 0, "start_time": start, "save_time": start + 600,
        "run_time": 600, "win_time": 0,
        "map_point_history": [standard_acts()[0][:3]],
        "players": [{"character_id": f"CHARACTER.{character}", "current_hp": hp, "max_hp": max_hp,
                     "deck": [{"id": "CARD.STRIKE_IRONCLAD"}], "relics": [{"id": "RELIC.BURNING_BLOOD"}]}],
    }


def test_the_run_in_progress_is_reported_apart_from_the_finished_runs(env: Env):
    env.add_run(BASE)
    env.write("current_run.save", current_save(BASE + 5000), where="saves")
    payload = env.payload()
    cur = payload["current"]
    assert (cur["char"], cur["asc"], cur["result"], cur["origin"]) == ("IRONCLAD", 7, "running", "current")
    assert (cur["end"], cur["hp"], cur["max_hp"], cur["number"]) == ("A1F3", 61, 80, 2)
    assert payload["summary"]["runs"] == 1 and len(payload["runs"]) == 1       # not a finished run
    assert payload["current_selected"] is True
    ladder = next(c for c in payload["characters"] if c["character"] == "IRONCLAD")
    assert ladder["ascensions"][6]["running"] == 1                             # but it shows in the ladder


def test_a_run_that_just_finished_is_not_listed_twice(env: Env):
    env.add_run(BASE + 5000)
    env.write("current_run.save", current_save(BASE + 5000), where="saves")   # same start_time as the history file
    payload = env.payload()
    assert payload["current"] is None and len(payload["runs"]) == 1


def test_a_current_run_from_before_the_advisor_or_with_a_win_time_is_ignored(env: Env):
    env.write("current_run.save", current_save(ap.ADVISOR_SINCE - 10), where="saves")
    assert env.payload()["current"] is None
    done = current_save(BASE)
    done["win_time"] = BASE + 100
    env.write("current_run.save", done, where="saves")
    assert env.payload()["current"] is None


# ---------------------------------------------------------------------------
# progress.save, the goal and the ladder
# ---------------------------------------------------------------------------


def test_goal_and_ladder_read_progress_save_and_the_runs(env: Env):
    env.write("progress.save", progress(IRONCLAD=7, SILENT=10, DEFECT=6, REGENT=4), where="saves")
    env.add_run(BASE, character="IRONCLAD", ascension=6, win=True)
    env.add_run(BASE + 1, character="IRONCLAD", ascension=7)
    env.add_run(BASE + 2, character="IRONCLAD", ascension=7, abandoned=True)
    payload = env.payload()
    assert payload["goal"] == {"ascension": 10, "levels": 7 + 10 + 6 + 4 + 0, "levels_goal": 50}
    by_name = {c["character"]: c for c in payload["characters"]}
    assert list(by_name) == ["IRONCLAD", "SILENT", "DEFECT", "REGENT", "NECROBINDER"]
    iron = by_name["IRONCLAD"]
    assert (iron["max_ascension"], iron["left"], iron["total_wins"], iron["total_losses"]) == (7, 3, 14, 21)
    a6, a7, a8 = iron["ascensions"][5], iron["ascensions"][6], iron["ascensions"][7]
    assert (a6["wins"], a6["unlocked"], a6["top"]) == (1, True, False)
    assert (a7["losses"], a7["abandoned"], a7["top"]) == (1, 1, True)
    assert (a8["unlocked"], a8["wins"] + a8["losses"]) == (False, 0)
    assert by_name["SILENT"]["left"] == 0 and by_name["NECROBINDER"]["known"] is False
    assert by_name["NECROBINDER"]["max_ascension"] == 0


def test_levels_cap_at_ten_per_character(env: Env):
    env.write("progress.save", progress(IRONCLAD=12), where="saves")
    assert env.payload()["goal"]["levels"] == 10


# ---------------------------------------------------------------------------
# campaign.json, rotation, the advisor log
# ---------------------------------------------------------------------------


def test_rotation_next_follows_the_mods_rule():
    rot = [{"character": c, "ascension": "max"} for c in ("IRONCLAD", "DEFECT", "REGENT")]
    assert ap.rotation_next(rot, "IRONCLAD")["character"] == "DEFECT"
    assert ap.rotation_next(rot, "REGENT")["character"] == "IRONCLAD"          # wraps
    assert ap.rotation_next(rot, "SILENT")["character"] == "IRONCLAD"          # not in the list: the first
    assert ap.rotation_next(rot, None)["character"] == "IRONCLAD"
    assert ap.rotation_next([], "IRONCLAD") is None


def test_campaign_json_names_the_rotation_the_next_run_and_who_is_out(env: Env):
    env.write("campaign.json", {"enabled": True, "rotation": ["IRONCLAD", {"character": "defect", "ascension": 4}, "REGENT"],
                                "next": {"character": "REGENT", "ascension": "max"}, "command": None})
    env.write("progress.save", progress(DEFECT=6, IRONCLAD=7), where="saves")
    env.add_run(BASE, character="IRONCLAD")
    env.write("campaign_runs.jsonl", json.dumps({"character": "IRONCLAD", "start_time": BASE, "victory": False,
                                                 "ascension": 3}) + "\n")
    campaign = env.payload()["campaign"]
    assert campaign["ok"] and campaign["enabled"] is True and campaign["command"] is None
    assert [e["character"] for e in campaign["rotation"]] == ["IRONCLAD", "DEFECT", "REGENT"]
    assert campaign["not_in_rotation"] == ["SILENT", "NECROBINDER"]
    # the entry after the last logged run's character (IRONCLAD); an explicit ascension is kept
    assert campaign["next"]["character"] == "DEFECT" and campaign["next"]["resolved_ascension"] == 4
    assert campaign["next_after_current"] is False
    # with a run in progress, "next" follows that run's character instead
    env.write("current_run.save", current_save(BASE + 100, character="DEFECT"), where="saves")
    after = env.payload()["campaign"]
    assert after["next"]["character"] == "REGENT" and after["next_after_current"] is True
    assert after["next"]["max_ascension"] is None


def test_campaign_json_missing_or_broken_is_reported_not_raised(env: Env):
    payload = env.payload()
    assert payload["campaign"]["ok"] is False and payload["campaign"]["rotation"] == []
    env.write("campaign.json", "{ nope")
    payload = env.payload()
    assert payload["campaign"]["ok"] is False
    assert any("campaign.json" in p for p in payload["problems"])


def test_the_advisor_log_tail_says_when_the_autopilot_last_stepped(env: Env):
    start = dt.datetime.fromtimestamp(NOW - 90)
    env.write("Sts2Advisor.log", "\n".join([
        log_line(start - dt.timedelta(seconds=300), "campaign: starting REGENT a3 (seed X)"),
        log_line(start - dt.timedelta(seconds=40), "autopilot step 7: card_reward select_card_reward"),
        "a continuation line without a timestamp",
        log_line(start, "autopilot step 8: map_select select_map_node"),
        log_line(start + dt.timedelta(seconds=2), "campaign: waiting: autopilot running"),
    ]) + "\n")
    activity = env.payload()["activity"]
    assert activity["state"] == "active"
    assert activity["last_step_n"] == 8 and activity["last_step_text"] == "map_select select_map_node"
    assert activity["last_step_ts"] == start.timestamp()
    assert activity["age_seconds"] == pytest.approx(90, abs=1.5)
    assert activity["last_line_ts"] == (start + dt.timedelta(seconds=2)).timestamp()
    assert activity["last_start"]["character"] == "REGENT" and activity["last_start"]["ascension"] == 3


@pytest.mark.parametrize("age, state", [(60, "active"), (299, "active"), (301, "quiet"), (29 * 60, "quiet"), (31 * 60, "stalled")])
def test_activity_state_by_age_of_the_last_step(env: Env, age, state):
    when = dt.datetime.fromtimestamp(NOW - age)
    env.write("Sts2Advisor.log", log_line(when, "autopilot step 1: x y") + "\n")
    assert env.payload()["activity"]["state"] == state


def test_no_log_means_unknown_activity(env: Env):
    activity = env.payload()["activity"]
    assert activity["state"] == "unknown" and activity["last_step_ts"] is None and activity["age_seconds"] is None


def test_only_the_tail_of_a_huge_log_is_read_and_the_last_30_lines_are_kept(env: Env):
    filler = "\n".join(log_line(dt.datetime(2026, 11, 1, 0, 0, 0), f"filler line {i} " + "x" * 80) for i in range(8000))
    last = log_line(dt.datetime.fromtimestamp(NOW - 10), "autopilot step 99: shop leave_room")
    env.write("Sts2Advisor.log", filler + "\n" + last + "\n")
    payload = env.payload()
    assert len(payload["log_tail"]) == 30 and payload["log_tail"][-1] == last
    assert payload["activity"]["last_step_n"] == 99


def test_journal_shows_the_last_15_lines_verbatim(env: Env):
    env.journal.write_text("\n".join(f"line {i} <b>&amp;" for i in range(1, 21)) + "\n", encoding="utf-8")
    journal = env.payload()["journal"]
    assert len(journal) == 15 and journal[0] == "line 6 <b>&amp;" and journal[-1] == "line 20 <b>&amp;"


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_character_filter_scopes_the_runs_and_the_numbers_but_not_the_goal(env: Env):
    env.write("progress.save", progress(IRONCLAD=7, SILENT=3), where="saves")
    env.add_run(BASE, character="IRONCLAD", win=True)
    env.add_run(BASE + 1, character="SILENT")
    env.add_run(BASE + 2, character="SILENT", killed="ENCOUNTER.TEST_SUBJECT_BOSS")
    env.add_run(BASE + 3, character="WATCHER-WATCHER")
    env.write("current_run.save", current_save(BASE + 100, character="IRONCLAD"), where="saves")

    everything = env.payload()
    assert everything["summary"]["runs"] == 4 and everything["filter"] == {"character": None, "shown": 30}
    options = {o["id"]: o["count"] for o in everything["character_options"]}
    assert options == {"IRONCLAD": 1, "SILENT": 2, "DEFECT": 0, "REGENT": 0, "NECROBINDER": 0, "OTHER": 1}

    silent = env.payload(character="silent")
    assert silent["filter"]["character"] == "SILENT"
    assert [r["char"] for r in silent["runs"]] == ["SILENT", "SILENT"]
    assert [r["number"] for r in silent["runs"]] == [2, 1]
    assert silent["summary"] == {"runs": 2, "wins": 0, "losses": 2, "abandoned": 0, "win_rate": 0.0, "mean_run_time": 1800}
    assert [d["encounter"] for d in silent["aggregates"]["deaths"]] == ["AEONGLASS_BOSS", "TEST_SUBJECT_BOSS"]
    # the filter does not hide the campaign: goal, unlocked ascensions, the run in progress
    assert silent["goal"]["levels"] == everything["goal"]["levels"] == 10
    assert silent["current"]["char"] == "IRONCLAD" and silent["current_selected"] is False
    iron = next(c for c in silent["characters"] if c["character"] == "IRONCLAD")
    assert iron["max_ascension"] == 7 and iron["ascensions"][6]["running"] == 0   # results follow the filter

    other = env.payload(character="OTHER")
    assert [r["char"] for r in other["runs"]] == ["WATCHER-WATCHER"]


def test_shown_limits_the_hp_curves_not_the_list_or_the_aggregates(env: Env):
    for i in range(5):
        env.add_run(BASE + i, character="IRONCLAD")
    payload = env.payload(shown=2)
    assert len(payload["runs"]) == 5 and payload["runs_with_floors"] == 2
    assert [r["floors"] is not None for r in payload["runs"]] == [True, True, False, False, False]
    assert payload["runs"][4]["end"] == "A2F3" and payload["runs"][4]["hp"] == 0      # listed in full otherwise
    # aggregates saw all 5 runs' floors
    elites = payload["aggregates"]["elites"]
    assert elites["total"] == 10


@pytest.mark.parametrize("kwargs", [{"shown": 0}, {"shown": 501}, {"shown": "abc"}, {"character": "bad name!"}, {"character": "x" * 41}])
def test_a_bad_filter_is_a_value_error(kwargs):
    with pytest.raises(ValueError):
        ap.parse_filter(**kwargs)


def test_filter_defaults_and_all():
    assert ap.parse_filter() == {"character": None, "shown": 30}
    assert ap.parse_filter("all", "") == {"character": None, "shown": 30}
    assert ap.parse_filter("other", "60") == {"character": "OTHER", "shown": 60}


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def test_damage_by_room_kind_and_act_averages_every_floor_zeros_included(env: Env):
    env.add_run(BASE)
    env.add_run(BASE + 1)
    rows = {r["kind"]: r for r in env.payload()["aggregates"]["damage"]}
    monster = rows["monster"]["acts"]
    assert (monster[0]["n"], monster[0]["mean"], monster[0]["max"]) == (2, 10.0, 10)
    assert monster[1]["n"] == 0 and monster[1]["mean"] is None
    assert rows["elite"]["acts"][0]["mean"] == 30.0 and rows["elite"]["acts"][1]["mean"] == 50.0
    assert rows["boss"]["acts"][1]["mean"] == 30.0 and rows["boss"]["n"] == 4 and rows["boss"]["total"] == 104
    assert rows["unknown_fight"]["mean"] == 4.0
    assert rows["rest_site"]["mean"] == 0.0 and "treasure" not in rows       # kinds nobody visited are absent
    assert [r["kind"] for r in env.payload()["aggregates"]["damage"]][:3] == ["monster", "elite", "boss"]


def test_deaths_group_by_the_killing_encounter(env: Env):
    env.add_run(BASE, character="IRONCLAD", ascension=7, killed="ENCOUNTER.SOUL_FYSH_BOSS")
    env.add_run(BASE + 1, character="REGENT", ascension=3, killed="ENCOUNTER.SOUL_FYSH_BOSS")
    env.add_run(BASE + 2, character="SILENT", ascension=10, killed="ENCOUNTER.TEST_SUBJECT_BOSS")
    env.add_run(BASE + 3, win=True)
    env.add_run(BASE + 4, abandoned=True, killed="ENCOUNTER.NIBBITS_WEAK")
    deaths = env.payload()["aggregates"]["deaths"]
    assert [(d["encounter"], d["count"]) for d in deaths] == [("SOUL_FYSH_BOSS", 2), ("TEST_SUBJECT_BOSS", 1)]
    assert [(r["character"], r["ascension"], r["end"]) for r in deaths[0]["runs"]] == [
        ("IRONCLAD", 7, "A2F3"), ("REGENT", 3, "A2F3")]


def test_elite_fights_are_bucketed_by_the_hp_they_started_with(env: Env):
    low_acts = [
        [point("ancient", "EVENT.NEOW", 0, 80), point("monster", "ENCOUNTER.A", 50, 30),
         point("elite", "ENCOUNTER.LOW_ELITE", 20, 10)],          # 30/80 = 37.5%: low
        [point("elite", "ENCOUNTER.ACT_START_ELITE", 5, 75)],       # first floor of an act: no HP before, skipped
    ]
    env.add_run(BASE, acts=low_acts)
    env.add_run(BASE + 1)                                          # 56/80 = 70% and 80/80 = 100%
    elites = env.payload()["aggregates"]["elites"]
    assert elites["total"] == 3 and elites["low"] == 1 and elites["threshold"] == 0.5
    assert [(b["label"], b["n"], b["mean_dmg"]) for b in elites["buckets"]] == [
        ("<50%", 1, 20.0), ("50-75%", 1, 30.0), (">=75%", 1, 50.0)]
    (low,) = elites["low_fights"]
    assert (low["hp_before"], low["max_before"], low["ratio"], low["dmg"], low["hp_after"]) == (30, 80, 0.375, 20, 10)
    assert (low["act"], low["floor"], low["name"], low["character"]) == (1, 3, "LOW_ELITE", "IRONCLAD")


def test_with_no_runs_everything_is_empty_not_an_error(env: Env):
    payload = env.payload()
    assert payload["error"] is None and payload["runs"] == [] and payload["current"] is None
    assert payload["summary"] == {"runs": 0, "wins": 0, "losses": 0, "abandoned": 0, "win_rate": None, "mean_run_time": None}
    assert payload["aggregates"]["damage"] == [] and payload["aggregates"]["deaths"] == []
    assert payload["aggregates"]["elites"]["total"] == 0 and payload["goal"]["levels"] == 0


# ---------------------------------------------------------------------------
# campaign_runs.jsonl against the histories
# ---------------------------------------------------------------------------


def log_row(start, **kwargs):
    row = {"ts": "2026-11-02T03:28:06Z", "character": "REGENT", "ascension": 2, "victory": True, "act": 3, "floor": 16,
           "total_floor": 48, "hp": 0, "max_hp": 75, "deck": ["CARD.A", "CARD.B+1"], "relics": ["RELIC.X"],
           "killed_by": None, "run_time": 2716, "seed": "S", "start_time": start}
    row.update(kwargs)
    return json.dumps(row)


def test_the_log_is_cross_checked_and_a_run_missing_its_history_file_is_still_listed(env: Env):
    env.add_run(BASE, character="REGENT", ascension=2, win=True, final_hp=44)
    env.add_run(BASE + 1, character="IRONCLAD", ascension=6, win=False)                # history says loss
    env.write("campaign_runs.jsonl", "\n".join([
        log_row(BASE, hp=0),                                                            # hp 0 on a victory
        log_row(BASE + 1, character="IRONCLAD", ascension=6, victory=True, hp=10),      # log says win: mismatch
        log_row(BASE + 2, character="DEFECT", ascension=4, victory=False, killed_by="ENCOUNTER.THE_KIN_BOSS", hp=0),
        "not json",
        log_row(ap.ADVISOR_SINCE - 1),                                                  # before the Advisor: ignored
    ]) + "\n")
    payload = env.payload()
    checks = payload["checks"]
    assert checks["log_rows"] == 3 and checks["history_runs"] == 2
    assert checks["missing_in_history"] == [BASE + 2] and checks["missing_in_log"] == []
    assert checks["mismatched"] == [BASE + 1] and checks["zero_hp_victories"] == 1
    listed = {r["start"]: r for r in payload["runs"]}
    only_log = listed[BASE + 2]
    assert (only_log["origin"], only_log["char"], only_log["asc"], only_log["result"]) == ("campaign_runs", "DEFECT", 4, "loss")
    assert (only_log["end"], only_log["killed_by"], only_log["deck"], only_log["upgraded"], only_log["floors"]) == (
        "A3F16", "THE_KIN_BOSS", 2, 1, [])
    assert listed[BASE]["origin"] == "history" and listed[BASE]["hp"] == 44        # the history's HP, not the log's hp=0
    assert [r["number"] for r in payload["runs"]] == [3, 2, 1]


def test_a_history_run_the_log_never_recorded_is_noted_not_dropped(env: Env):
    env.add_run(BASE)                                       # played before campaign mode wrote the log
    env.write("campaign_runs.jsonl", "")
    payload = env.payload()
    assert payload["checks"]["missing_in_log"] == [BASE] and len(payload["runs"]) == 1


# ---------------------------------------------------------------------------
# Never throws, JSON-safe
# ---------------------------------------------------------------------------


def test_unreadable_inputs_become_problems_not_exceptions(env: Env):
    env.add_run(BASE)
    (env.saves / "history" / f"{BASE + 1}.run").write_text("{ broken", encoding="utf-8")
    (env.saves / "history" / f"{BASE + 2}.run").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    env.write("progress.save", "not json at all", where="saves")
    env.write("current_run.save", "[]", where="saves")
    payload = env.payload()
    assert payload["error"] is None and len(payload["runs"]) == 1
    assert any(f"{BASE + 1}.run" in p for p in payload["problems"])
    assert any("progress.save" in p for p in payload["problems"])
    assert payload["goal"]["levels"] == 0
    json.dumps(payload, allow_nan=False)


def test_a_missing_everything_still_yields_a_payload(tmp_path: Path):
    paths = ap.AdvisorPlayPaths.from_dirs(tmp_path / "nope" / "saves", tmp_path / "nope" / "adv", tmp_path / "nope.txt")
    payload = ap.build_payload(paths, now=NOW)
    assert payload["error"] is None and payload["runs"] == [] and payload["journal"] == [] and payload["log_tail"] == []
    assert len(payload["characters"]) == 5
    json.dumps(payload, allow_nan=False)


def test_an_unexpected_failure_is_the_error_field(env: Env, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ap, "load_runs", boom)
    payload = env.payload()
    assert payload["error"] == "RuntimeError: kaboom"
    assert payload["runs"] == [] and payload["goal"]["levels_goal"] == 50
    json.dumps(payload, allow_nan=False)


def test_a_full_payload_is_json_safe_and_parsed_history_files_are_cached(env: Env, monkeypatch):
    env.add_run(BASE, win=True)
    first = env.payload()
    json.dumps(first, allow_nan=False)
    calls = []
    real = ap.summarise_run
    monkeypatch.setattr(ap, "summarise_run", lambda *a, **k: calls.append(1) or real(*a, **k))
    env.payload()
    assert calls == []                                       # unchanged file: served from the cache
    env.add_run(BASE, win=False)                             # rewritten (size/mtime change): parsed again
    env.payload()
    assert calls == [1]


def test_the_payloads_runs_are_copies_so_filters_never_leak_into_the_cache(env: Env):
    env.add_run(BASE)
    env.add_run(BASE + 1)
    env.payload(shown=1)                                     # strips floors from run 2 in its own copy
    again = env.payload(shown=30)
    assert all(r["floors"] for r in again["runs"])


# ---------------------------------------------------------------------------
# GET /api/advisor-play
# ---------------------------------------------------------------------------


@contextmanager
def advisor_server(env: Env, tmp_path: Path):
    catalog = RunCatalog([tmp_path / "logs"], replay_parser=lambda rows, name=None: {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_viewer_handler(catalog, advisor_play_paths=env.paths))
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def fetch(base: str, path: str):
    try:
        with urlopen(base + path, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), json.loads(response.read())
    except HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), json.loads(error.read())


def test_the_endpoint_serves_the_payload_read_on_request(env: Env, tmp_path: Path):
    env.add_run(BASE, character="REGENT", win=True)
    env.write("progress.save", progress(REGENT=4), where="saves")
    with advisor_server(env, tmp_path) as base:
        status, content_type, payload = fetch(base, "/api/advisor-play")
        assert status == 200 and content_type == "application/json; charset=utf-8"
        assert payload["error"] is None and [r["char"] for r in payload["runs"]] == ["REGENT"]
        assert payload["goal"]["levels"] == 4 and payload["filter"] == {"character": None, "shown": 30}
        # the files are read on every request: a new run shows up without restarting anything
        env.add_run(BASE + 1, character="SILENT")
        _, _, again = fetch(base, "/api/advisor-play")
        assert [r["char"] for r in again["runs"]] == ["SILENT", "REGENT"]
        _, _, scoped = fetch(base, "/api/advisor-play?character=REGENT&shown=1")
        assert [r["char"] for r in scoped["runs"]] == ["REGENT"] and scoped["filter"] == {"character": "REGENT", "shown": 1}


def test_the_endpoint_never_throws_for_broken_inputs_and_rejects_a_bad_query(env: Env, tmp_path: Path):
    env.write("progress.save", "garbage", where="saves")
    (env.saves / "history" / f"{BASE}.run").write_text("{", encoding="utf-8")
    with advisor_server(env, tmp_path) as base:
        status, _, payload = fetch(base, "/api/advisor-play")
        assert status == 200 and payload["error"] is None and len(payload["problems"]) == 2
        for query, fragment in (
            ("?foo=1", "unexpected advisor-play query parameter: foo"),
            ("?shown=abc", "shown must be an integer"),
            ("?shown=999", "between 1 and 500"),
            ("?character=%3Cscript%3E", "invalid character"),
        ):
            status, _, error = fetch(base, "/api/advisor-play" + query)
            assert status == 400 and fragment in error["error"], query
        status, _, error = fetch(base, "/api/advisor-play/")
        assert status == 404
