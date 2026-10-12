"""Per-game detail (route, deck, combat legal actions + solver plans, choices).

Two kinds of logs: small hand-built ones (every rule gets a log that isolates
it) and three trimmed real play_full_run logs in tests/fixtures/run_workbench/
(``game_detail_*.jsonl``).  The real ones are verbatim GameLogger rows cut down
to what the readers use: rows copied by index range from

* ``logs/20261008_115655_Ironclad_run_5.jsonl`` rows 0-5, 6-41, 182-219, 382-434
  (a whole small game: an event, a fight, a card_select event, SMITH, a shop
  with removals and a purchase, and the boss fight it died in) -> ``full_run``;
* ``logs/20261009_164129_Ironclad_p3x_27.jsonl`` rows 0-1, 500-571 (an Elite
  fight with a potion + bundled card choice, and a plan that was played to the
  end yet the turn went on, so the harness re-planned) -> ``replan``;
* ``logs/20261011_072319_Regent_p3x_38.jsonl`` rows 0-1, 370-420 (the solver
  failing twice and the heuristic playing a card before the re-plan) ->
  ``plan_error``.

Dropped from every state: draw/discard piles, the intent forecast, card and
relic descriptions/stats, stack traces, and the deck of all but the first and
non-combat states of a room.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import agent.run_progress_viewer as viewer
from agent.run_progress_viewer import make_viewer_handler
from agent.run_workbench.catalog import CatalogError, CatalogNotFoundError, RunCatalog
from agent.run_workbench.game_detail import build_game_detail, load_game_records
from agent.run_workbench.localization import Localizer
from agent.run_workbench.replay import parse_game_progress

FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_workbench"
REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# A hand-built game log
# ---------------------------------------------------------------------------


def _card(index, name, cost=1, *, can_play=True, target="AnyEnemy", kind="Attack", cid=None):
    entry_id = cid or name.upper().replace(" ", "_")
    return {"index": index, "id": f"CARD.{entry_id}", "name": name, "cost": cost, "type": kind,
            "rarity": "Common", "can_play": can_play, "target_type": target}


def _enemy(index, combat_id, name="Louse", hp=30, block=0, intents=None, powers=None):
    return {"index": index, "combat_id": combat_id, "name": name, "hp": hp, "max_hp": 40,
            "block": block, "intents": intents or [{"type": "Attack", "damage": 7}], "powers": powers}


def _deck(*cards):
    return [{"id": f"CARD.{n.upper().replace(' ', '_')}", "name": n, "type": "Attack", "upgraded": up}
            for n, up in cards]


class Log:
    """GameLogger rows with the real step bookkeeping: an action carries the
    step the previous state announced, the reply to it the next one."""

    def __init__(self, *, character="Ironclad", seed="t1", meta=None) -> None:
        self.rows: list[dict] = [
            {"type": "run_meta", "ts": 1_800_000_000.0, "character": character, "seed": seed,
             "ascension": 0, "experiment": "run:ooc=greedy", **(meta or {})}
        ]
        self.step = 0
        self.hp = 80
        self.gold = 99
        self.deck = _deck(("Strike", False), ("Strike", False), ("Defend", False), ("Bash", False))
        self.relics = [{"name": "Burning Blood"}]
        self.potions: list[dict] = []
        self.act, self.floor, self.room = 1, 1, "Event"
        self.action({"cmd": "start_run", "character": character, "seed": seed, "ascension": 0})

    # -- rows ------------------------------------------------------------

    def _row(self, kind, data):
        self.rows.append({"step": self.step, "ts": f"2026-10-12T13:00:{len(self.rows) % 60:02d}",
                          "type": kind, "data": data})

    def action(self, data):
        self._row("action", data)
        self.step += 1

    def do(self, name, **args):
        self.action({"cmd": "action", "action": name, "args": args} if args
                    else {"cmd": "action", "action": name})

    def player(self):
        return {"name": "The Ironclad", "hp": self.hp, "max_hp": 80, "block": 0, "gold": self.gold,
                "deck_size": len(self.deck), "deck": deepcopy(self.deck),
                "relics": deepcopy(self.relics), "potions": deepcopy(self.potions)}

    def state(self, decision, *, room=None, floor=None, **extra):
        if floor is not None:
            self.floor = floor
        if room is not None:
            self.room = room
        data = {"type": "decision", "decision": decision,
                "context": {"act": self.act, "act_name": "Overgrowth", "floor": self.floor,
                            "room_type": self.room, "boss": {"id": "B", "name": "Boss"}},
                "player": self.player(), **extra}
        self._row("state", data)

    def reply(self, data):
        self._row("state", data)

    # -- decisions -------------------------------------------------------

    def map_select(self, choices, take):
        self.state("map_select", room="Map", choices=[{"col": c, "row": r, "type": t} for c, r, t in choices])
        col, row, _kind = choices[take]
        self.do("select_map_node", col=col, row=row)

    def combat(self, rnd, hand, enemies, *, energy=3, block=0, floor=None, room="Monster"):
        # the engine renumbers the hand and the enemy list after every play
        hand = [{**card, "index": position} for position, card in enumerate(hand)]
        enemies = [{**enemy, "index": position} for position, enemy in enumerate(enemies)]
        self.state("combat_play", room=room, floor=floor, round=rnd, energy=energy, max_energy=3,
                   hand=hand, enemies=enemies, draw_pile_count=5, discard_pile_count=0,
                   player_powers=None)
        self.rows[-1]["data"]["player"]["block"] = block

    def plan(self, actions, *, hp=70, dead=True, odr=False, elapsed=1200):
        self.do("plan_combat_turn")
        self.reply({"type": "combat_plan", "only_death_routes_found": odr, "action_count": len(actions),
                    "score": 1, "projected_player_hp": hp, "all_enemies_dead": dead, "actions": actions,
                    "search": {"budget_ms": 120000, "elapsed_ms": elapsed, "boundary": "None",
                               "expanded_nodes": 10, "total_expanded_nodes": 40, "inferrer": "on"}})

    def error(self, message):
        self.reply({"type": "error", "message": message})

    def play(self, card_index, target_index=None):
        if target_index is None:
            self.do("play_card", card_index=card_index)
        else:
            self.do("play_card", card_index=card_index, target_index=target_index)


def _pa(turn, card_id, target=None, **extra):
    action = {"turn": turn, "action": "play_card", "card_id": card_id, "card_title": f"{card_id}.title",
              "card_upgrade_level": 0, "card_occurrence": 0}
    if target is not None:
        action.update({"target_index": 0, "target_combat_id": target, "target_name": "LOUSE.name"})
    action.update(extra)
    return action


def _end(turn):
    return {"turn": turn, "action": "end_turn"}


def _detail(log: Log, **kwargs) -> dict:
    return build_game_detail(log.rows, **kwargs)


def _combat_room(detail: dict, label: str | None = None) -> dict:
    rooms = [r for r in detail["rooms"] if r["combat"] and (label is None or r["label"] == label)]
    return rooms[0]


def _two_turn_fight(log: Log) -> Log:
    """A1F2: turn 1 plays Bash, Strike then ends; turn 2 plays Strike (kills)."""
    log.map_select([(1, 1, "Monster"), (2, 1, "Shop")], 0)
    hand = [_card(0, "Strike"), _card(1, "Bash", 2), _card(2, "Defend", target="Self", kind="Skill"),
            _card(3, "Dazed", 0, can_play=False, target="None", kind="Status")]
    log.combat(1, hand, [_enemy(0, 7, hp=30)], floor=2)
    log.plan([_pa(1, "BASH", 7), _pa(1, "STRIKE", 7), _end(1), _pa(2, "STRIKE", 7)])
    log.play(1, 0)
    log.combat(1, [hand[0], hand[2], hand[3]], [_enemy(0, 7, hp=22)], energy=1)
    log.play(0, 0)
    log.combat(1, [hand[2], hand[3]], [_enemy(0, 7, hp=16)], energy=0)
    log.do("end_turn")
    return log


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def test_route_lists_the_options_offered_and_the_one_taken():
    log = Log()
    log.state("event_choice", event_name="Neow", options=[{"index": 0, "title": "Boon"}])
    log.do("choose_option", option_index=0)
    log.map_select([(1, 1, "Monster"), (3, 1, "Unknown"), (5, 1, "Elite")], 1)
    log.state("event_choice", room="Event", floor=2, event_name="Dense Vegetation",
              options=[{"index": 0, "title": "Rest"}])
    log.do("choose_option", option_index=0)
    log.map_select([(2, 2, "Monster")], 0)
    log.combat(1, [_card(0, "Strike")], [_enemy(0, 1)], floor=3)

    detail = _detail(log)

    first, second = detail["route"]
    assert (first["act"], first["floor"], first["label"]) == (1, 1, "A1F1")
    assert [(o["col"], o["row"], o["type"]) for o in first["options"]] == [
        (1, 1, "Monster"), (3, 1, "Unknown"), (5, 1, "Elite")]
    assert first["taken"] == {"col": 3, "row": 1, "type": "Unknown"}
    assert first["taken_index"] == 1
    # the "?" node became an event: the strip shows what the choice turned into
    assert first["entered"]["room_type"] == "Event" and first["entered"]["label"] == "A1F2"
    assert second["label"] == "A1F2" and second["taken_index"] == 0
    assert second["entered"]["room_type"] == "Monster"


def test_a_node_that_opens_no_room_leads_nowhere_not_to_the_next_room_logged():
    log = Log()
    log.map_select([(1, 1, "Treasure"), (2, 1, "Monster")], 0)
    log.map_select([(1, 2, "Monster")], 0)          # the chest asked nothing: next choice at once
    log.combat(1, [_card(0, "Strike")], [_enemy(0, 1)], floor=3)

    chest, to_fight = _detail(log)["route"]
    assert chest["taken"]["type"] == "Treasure" and chest["entered"] is None
    assert to_fight["entered"]["room_type"] == "Monster" and to_fight["entered"]["label"] == "A1F3"


def test_a_pending_map_choice_has_no_taken_node():
    log = Log()
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 1, "type": "Monster"}])
    (entry,) = _detail(log)["route"]
    assert entry["taken"] is None and entry["taken_index"] is None and entry["entered"] is None


# ---------------------------------------------------------------------------
# Deck
# ---------------------------------------------------------------------------


def _deck_room_log() -> Log:
    log = Log()
    log.state("card_reward", room="Monster", floor=2,
              cards=[{"index": 0, "id": "CARD.CLEAVE", "name": "Cleave", "cost": 1, "type": "Attack",
                      "rarity": "Common"}], can_skip=True)
    log.do("select_card_reward", card_index=0)
    log.deck = log.deck + _deck(("Cleave", False))
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 3, "type": "RestSite"}])
    log.do("select_map_node", col=1, row=3)
    log.state("rest_site", room="RestSite", floor=3,
              options=[{"index": 0, "option_id": "HEAL", "name": "Heal", "is_enabled": True},
                       {"index": 1, "option_id": "SMITH", "name": "Smith", "is_enabled": True}])
    log.do("choose_option", option_index=1)
    log.state("card_select", cards=[
        {"index": i, "id": c["id"], "name": c["name"], "type": "Attack", "upgraded": c["upgraded"]}
        for i, c in enumerate(log.deck)], min_select=0, max_select=1)
    log.do("select_cards", indices="1")
    log.deck = _deck(("Strike", False), ("Strike", True), ("Defend", False), ("Bash", False), ("Cleave", False))
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 4, "type": "Monster"}])
    return log


def test_deck_delta_per_room_names_added_removed_and_upgraded_cards():
    detail = _detail(_deck_room_log())

    reward_room, rest_room = detail["rooms"]
    assert [c["name"] for c in reward_room["deck_delta"]["added"]] == ["Cleave"]
    assert reward_room["deck_delta"]["removed"] == [] and reward_room["deck_delta"]["upgraded"] == []
    # one of the two plain Strikes became Strike+: an upgrade, not an add + a remove
    assert [(c["name"], c["upgraded"]) for c in rest_room["deck_delta"]["upgraded"]] == [("Strike", True)]
    assert rest_room["deck_delta"]["added"] == [] and rest_room["deck_delta"]["removed"] == []


def test_deck_delta_is_a_multiset_diff_and_reports_removals():
    log = Log()
    log.state("shop", room="Shop", floor=2, cards=[], relics=[], potions=[], card_removal_cost=75)
    log.deck = _deck(("Strike", False), ("Defend", False), ("Bash", False))  # one Strike gone
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 3, "type": "Monster"}])

    (room,) = _detail(log)["rooms"]
    assert [c["name"] for c in room["deck_delta"]["removed"]] == ["Strike"]
    assert room["deck_delta"]["added"] == []


def test_final_deck_groups_duplicates_and_marks_upgrades():
    detail = _detail(_deck_room_log())

    deck = detail["deck"]
    assert deck["size"] == 5
    groups = {(g["name"], g["upgraded"]): g["count"] for g in deck["final"]}
    assert groups == {("Strike", False): 1, ("Strike", True): 1, ("Defend", False): 1,
                      ("Bash", False): 1, ("Cleave", False): 1}
    assert deck["by_type"] == {"Attack": 5}
    assert detail["summary"]["deck_size"] == 5


def test_relics_and_potions_gained_and_used_per_room():
    log = Log()
    log.potions = [{"index": 0, "id": "POTION.FIRE_POTION", "name": "Fire Potion", "target_type": "AnyEnemy"}]
    log.map_select([(1, 1, "Monster")], 0)
    hand = [_card(0, "Strike")]
    log.combat(1, hand, [_enemy(0, 1, hp=30)], floor=2)
    log.plan([{"turn": 1, "action": "use_potion", "potion_id": "FIRE_POTION", "potion_slot": 0,
               "target_index": 0, "target_combat_id": 1, "target_name": "LOUSE.name"}, _end(1)])
    log.do("use_potion", potion_index=0, target_index=0)
    log.potions = []
    log.relics = log.relics + [{"name": "Anchor"}]
    log.state("card_reward", cards=[], can_skip=True)

    room = _combat_room(_detail(log))
    assert room["potions_used"] == ["Fire Potion"]
    assert room["relics_gained"] == ["Anchor"]
    assert room["potions_gained"] == []


# ---------------------------------------------------------------------------
# Legal actions
# ---------------------------------------------------------------------------


def test_legal_actions_are_the_playable_affordable_cards_potions_and_end_turn():
    log = Log()
    log.potions = [{"index": 0, "id": "POTION.FIRE_POTION", "name": "Fire Potion", "target_type": "AnyEnemy"},
                   {"index": 1, "id": "POTION.BLOCK_POTION", "name": "Block Potion", "target_type": "AnyPlayer"}]
    hand = [
        _card(0, "Strike"),
        _card(1, "Bash", 2),
        _card(2, "Defend", target="Self", kind="Skill"),
        _card(3, "Dazed", 0, can_play=False, target="None", kind="Status"),
        _card(4, "Whirlwind", 3),               # can_play is true but 3 > 2 energy
        _card(5, "Cleave", 1, target="AllEnemies"),
    ]
    log.combat(1, hand, [_enemy(0, 7), _enemy(1, 8, name="Cultist")], energy=2, floor=2)
    log.play(0, 1)

    step = _combat_room(_detail(log))["combat"]["turns"][0]["steps"][0]

    legal = step["legal"]
    cards = [(e["card_index"], e.get("targets")) for e in legal if e["kind"] == "play_card"]
    assert cards == [(0, [0, 1]), (1, [0, 1]), (2, None), (5, None)]
    potions = [(e["potion_index"], e.get("targets")) for e in legal if e["kind"] == "use_potion"]
    assert potions == [(0, [0, 1]), (1, None)]
    assert legal[-1] == {"kind": "end_turn"}
    # the hand still shows every card, with the unplayable ones flagged
    playable = {c["name"]: c["playable"] for c in step["state"]["hand"]}
    assert playable == {"Strike": True, "Bash": True, "Defend": True, "Dazed": False,
                        "Whirlwind": False, "Cleave": True}
    assert step["state"]["energy"] == 2
    assert [e["combat_id"] for e in step["state"]["enemies"]] == [7, 8]


def test_the_taken_action_is_marked_with_its_target():
    log = Log()
    log.potions = [{"index": 0, "id": "POTION.FIRE_POTION", "name": "Fire Potion", "target_type": "AnyEnemy"}]
    hand = [_card(0, "Strike"), _card(1, "Defend", target="Self", kind="Skill")]
    enemies = [_enemy(0, 7), _enemy(1, 8, name="Cultist")]
    log.combat(1, hand, enemies, floor=2)
    log.play(0, 1)
    log.combat(1, hand[1:], enemies, energy=2)
    log.do("use_potion", potion_index=0, target_index=0)
    log.combat(1, hand[1:], enemies[:1], energy=2)
    log.do("end_turn")

    steps = _combat_room(_detail(log))["combat"]["turns"][0]["steps"]

    play, potion, end = steps
    assert play["kind"] == "play_card"
    assert play["legal"][play["taken_legal"]] == {"kind": "play_card", "card_index": 0, "targets": [0, 1]}
    assert play["taken"]["card"]["name"] == "Strike"
    assert (play["taken"]["target"]["index"], play["taken"]["target"]["combat_id"],
            play["taken"]["target"]["name"]) == (1, 8, "Cultist")
    assert potion["legal"][potion["taken_legal"]]["kind"] == "use_potion"
    assert potion["taken"]["potion"]["name"] == "Fire Potion"
    assert potion["taken"]["target"]["combat_id"] == 7
    assert end["legal"][end["taken_legal"]] == {"kind": "end_turn"}


def test_a_lone_enemy_is_targeted_without_a_target_index():
    log = Log()
    log.combat(1, [_card(0, "Strike")], [_enemy(0, 7, name="Louse")], floor=2)
    log.play(0)  # the engine auto-targets a single enemy

    (step,) = _combat_room(_detail(log))["combat"]["turns"][0]["steps"]
    assert step["taken"]["target"] == {"index": 0, "combat_id": 7, "name": "Louse", "auto": True}


def test_a_card_that_needs_no_target_has_none_even_with_one_enemy_alive():
    log = Log()
    log.combat(1, [_card(0, "Defend", target="Self", kind="Skill")], [_enemy(0, 7)], floor=2)
    log.play(0)

    (step,) = _combat_room(_detail(log))["combat"]["turns"][0]["steps"]
    assert step["taken"]["target"] is None


def test_turn_start_shows_enemies_with_intents_and_powers():
    log = Log()
    enemy = _enemy(0, 7, hp=30, block=4, intents=[{"type": "Attack", "damage": 6, "hits": 2, "total_damage": 12}],
                   powers=[{"name": "Strength", "amount": 2, "description": "x"}])
    log.combat(1, [_card(0, "Strike")], [enemy], floor=2, block=5)
    log.play(0, 0)

    turn = _combat_room(_detail(log))["combat"]["turns"][0]
    start = turn["start"]
    assert (start["energy"], start["max_energy"], start["block"], start["hp"]) == (3, 3, 5, 80)
    assert start["enemies"][0]["intents"] == [{"type": "Attack", "damage": 6, "hits": 2, "total_damage": 12}]
    assert start["enemies"][0]["powers"] == [{"name": "Strength", "amount": 2}]
    assert (start["enemies"][0]["hp"], start["enemies"][0]["block"]) == (30, 4)


# ---------------------------------------------------------------------------
# Plans, aligned with turns
# ---------------------------------------------------------------------------


def test_a_plan_belongs_to_the_turn_it_was_requested_in_and_lists_only_that_turns_actions():
    detail = _detail(_two_turn_fight(Log()))

    (turn,) = _combat_room(detail)["combat"]["turns"]
    (plan,) = turn["solver_plans"]
    assert plan["ok"] is True
    # the reply covers two turns; the driver stops at the first end_turn
    assert [a["kind"] for a in plan["actions"]] == ["play_card", "play_card", "end_turn"]
    assert [a.get("card_id") for a in plan["actions"]] == ["BASH", "STRIKE", None]
    assert plan["later_actions"] == 1 and plan["turns_planned"] == [1, 2]
    assert plan["projected_player_hp"] == 70 and plan["all_enemies_dead"] is True
    assert plan["only_death_routes_found"] is False
    assert plan["search"] == {"budget_ms": 120000, "elapsed_ms": 1200, "boundary": "None",
                              "expanded_nodes": 10, "total_expanded_nodes": 40, "inferrer": "on"}
    assert plan["action_step"] is not None and plan["step"] == plan["action_step"] + 1
    # names are read off the hand the plan was asked in
    assert plan["actions"][0]["card_name"] == "Bash"
    assert plan["actions"][0]["target"] == {"combat_id": 7, "index": 0, "name": "Louse"}


def test_executed_steps_are_tied_to_their_planned_action():
    turn = _combat_room(_detail(_two_turn_fight(Log())))["combat"]["turns"][0]

    steps = turn["steps"]
    assert [s["kind"] for s in steps] == ["play_card", "play_card", "end_turn"]
    assert [s["source"] for s in steps] == ["plan", "plan", "plan"]
    assert [s["plan"] for s in steps] == [{"plan": 0, "pos": 0}, {"plan": 0, "pos": 1}, {"plan": 0, "pos": 2}]
    plan = turn["solver_plans"][0]
    assert [a["status"] for a in plan["actions"]] == ["executed"] * 3
    assert [a["step_n"] for a in plan["actions"]] == [0, 1, 2]
    assert plan["executed"] == 3 and plan["end_reason"] == "completed"
    assert turn["divergence"] == []
    assert turn["ended"] == "end_turn"


def test_each_round_gets_its_own_plan():
    log = _two_turn_fight(Log())
    log.combat(2, [_card(0, "Strike")], [_enemy(0, 7, hp=10)], energy=3)
    log.plan([_pa(2, "STRIKE", 7)], hp=65)
    log.play(0, 0)
    log.state("card_reward", cards=[], can_skip=True)

    turns = _combat_room(_detail(log))["combat"]["turns"]
    assert [t["round"] for t in turns] == [1, 2]
    assert [len(t["solver_plans"]) for t in turns] == [1, 1]
    assert turns[1]["solver_plans"][0]["projected_player_hp"] == 65
    assert turns[1]["ended"] == "combat_end"
    # the fight ended on the last planned card: nothing diverged
    assert turns[1]["divergence"] == [] and turns[1]["solver_plans"][0]["end_reason"] == "completed"


def test_a_plan_error_is_kept_and_the_heuristic_step_after_it_is_marked():
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Defend", target="Self", kind="Skill")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    log.do("plan_combat_turn")
    log.error("CombatSearchCoordinator.Solve failed: InvalidOperationException: boom\n   at X.Y()")
    log.play(0, 0)
    log.combat(1, hand[1:], [_enemy(0, 7, hp=24)], energy=2)
    log.plan([_pa(1, "DEFEND"), _end(1)])
    log.play(0)
    log.combat(1, [], [_enemy(0, 7, hp=24)], energy=1)
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    bad, good = turn["solver_plans"]
    assert bad["ok"] is False and bad["error"].startswith("CombatSearchCoordinator.Solve failed")
    assert "\n" not in bad["error"]
    assert good["ok"] is True
    assert [s["source"] for s in turn["steps"]] == ["heuristic", "plan", "plan"]
    kinds = [d["kind"] for d in turn["divergence"]]
    assert kinds == ["plan_error", "replan"]
    assert turn["divergence"][1]["after"] == "error"


def test_replanning_after_a_plan_that_was_played_out_but_did_not_end_the_turn():
    """The solver predicted the fight over; it was not -> 'exhausted', then a replan."""
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Strike")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    log.plan([_pa(1, "STRIKE", 7), _pa(1, "STRIKE", 7)], dead=True)
    log.play(0, 0)
    log.combat(1, hand[1:], [_enemy(0, 7, hp=24)], energy=2)
    log.play(0, 0)
    log.combat(1, [], [_enemy(0, 7, hp=18)], energy=1)   # still alive
    log.plan([_end(1)])
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    first, second = turn["solver_plans"]
    assert first["end_reason"] == "exhausted" and first["executed"] == 2
    assert second["end_reason"] == "completed"
    assert [d["kind"] for d in turn["divergence"]] == ["exhausted", "replan"]
    assert turn["divergence"][1]["after"] == "exhausted"
    assert [s["plan"]["plan"] for s in turn["steps"]] == [0, 0, 1]


def test_a_plan_dropped_because_its_card_is_not_in_hand_is_truncated_with_that_reason():
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Defend", target="Self", kind="Skill")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    # the solver counted on drawing Bash after the Strike; it never came
    log.plan([_pa(1, "STRIKE", 7), _pa(1, "BASH", 7), _end(1)])
    log.play(0, 0)
    log.combat(1, hand[1:], [_enemy(0, 7, hp=24)], energy=2)
    log.plan([_pa(1, "DEFEND"), _end(1)])
    log.play(0)
    log.combat(1, [], [_enemy(0, 7, hp=24)], energy=1)
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    first, second = turn["solver_plans"]
    assert first["end_reason"] == "truncated" and first["reason"] == "card_missing"
    assert first["executed"] == 1
    assert [a["status"] for a in first["actions"]] == ["executed", "not_executed", "not_executed"]
    kinds = [d["kind"] for d in turn["divergence"]]
    assert kinds == ["truncated", "replan"]
    truncated = turn["divergence"][0]
    assert (truncated["executed"], truncated["planned"], truncated["reason"]) == (1, 3, "card_missing")
    assert truncated["next"]["card_id"] == "BASH"
    assert turn["divergence"][1]["reason"] == "card_missing"


def test_an_engine_refusal_of_a_planned_card_is_a_refused_truncation():
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Bash", 2)]
    log.combat(1, hand, [_enemy(0, 7)], floor=2, energy=3)
    log.plan([_pa(1, "STRIKE", 7), _pa(1, "BASH", 7), _end(1)])
    log.play(0, 0)
    log.combat(1, hand[1:], [_enemy(0, 7, hp=24)], energy=2)
    log.play(0, 0)
    log.error("Cannot play card Bash: EnergyCostTooHigh")
    log.plan([_end(1)])
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    refused_step = turn["steps"][1]
    assert refused_step["rejected"] == "Cannot play card Bash: EnergyCostTooHigh"
    assert refused_step["plan"] == {"plan": 0, "pos": 1}
    first = turn["solver_plans"][0]
    assert first["end_reason"] == "truncated" and first["reason"] == "refused"
    assert first["executed"] == 1
    assert [a["status"] for a in first["actions"]] == ["executed", "refused", "not_executed"]
    kinds = [d["kind"] for d in turn["divergence"]]
    assert "refused" in kinds and "truncated" in kinds and "replan" in kinds


def test_an_executed_card_that_is_not_the_plans_next_is_a_mismatch():
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Defend", target="Self", kind="Skill")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    log.plan([_pa(1, "STRIKE", 7), _end(1)])
    log.play(1)                                    # Defend instead of the planned Strike
    log.combat(1, hand[:1], [_enemy(0, 7)], energy=2)
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    assert turn["steps"][0]["source"] == "off_plan" and turn["steps"][0]["plan"] is None
    (mismatch,) = [d for d in turn["divergence"] if d["kind"] == "mismatch"]
    assert mismatch["expected"]["card_id"] == "STRIKE"
    assert mismatch["actual"]["card_id"] == "DEFEND"
    assert mismatch["step_n"] == 0


def test_a_planned_card_whose_target_died_is_skipped_not_a_mismatch():
    log = Log()
    hand = [_card(0, "Strike"), _card(1, "Strike"), _card(2, "Defend", target="Self", kind="Skill")]
    log.combat(1, hand, [_enemy(0, 7, hp=5), _enemy(1, 8, name="Cultist")], floor=2)
    log.plan([_pa(1, "STRIKE", 7), _pa(1, "STRIKE", 7), _pa(1, "DEFEND"), _end(1)])
    log.play(0, 0)                                  # kills enemy 7
    log.combat(1, hand[1:], [_enemy(0, 8, name="Cultist")], energy=2)
    log.play(1)                                     # the 2nd Strike is skipped: no target
    log.combat(1, hand[1:2] + [], [_enemy(0, 8, name="Cultist")], energy=1)
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    plan = turn["solver_plans"][0]
    assert [a["status"] for a in plan["actions"]] == ["executed", "skipped", "executed", "executed"]
    skipped = [d for d in turn["divergence"] if d["kind"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["planned"]["card_id"] == "STRIKE"
    assert not [d for d in turn["divergence"] if d["kind"] == "mismatch"]
    assert plan["end_reason"] == "completed"


def test_steps_without_any_plan_in_a_solver_game_are_flagged_heuristic():
    log = Log()
    hand = [_card(0, "Strike")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    log.plan([_pa(1, "STRIKE", 7), _end(1)])
    log.play(0, 0)
    log.combat(1, [], [_enemy(0, 7, hp=24)], energy=2)
    log.do("end_turn")
    # next round: the harness never asked for a plan (stale guard)
    log.combat(2, [_card(0, "Strike")], [_enemy(0, 7, hp=24)])
    log.play(0, 0)
    log.combat(2, [], [_enemy(0, 7, hp=18)], energy=2)
    log.do("end_turn")

    turns = _combat_room(_detail(log))["combat"]["turns"]
    assert turns[1]["solver_plans"] == []
    assert [s["source"] for s in turns[1]["steps"]] == ["heuristic", "heuristic"]
    assert [d["kind"] for d in turns[1]["divergence"]] == ["heuristic"]


def test_a_game_without_the_solver_has_no_plans_and_no_divergence_noise():
    log = Log()
    log.combat(1, [_card(0, "Strike")], [_enemy(0, 7)], floor=2)
    log.play(0, 0)
    log.combat(1, [], [_enemy(0, 7, hp=24)], energy=2)
    log.do("end_turn")

    detail = _detail(log)

    turn = _combat_room(detail)["combat"]["turns"][0]
    assert turn["solver_plans"] == [] and turn["divergence"] == []
    assert detail["summary"]["solver"]["used"] is False
    assert [s["source"] for s in turn["steps"]] == ["heuristic", "heuristic"]


def test_a_bundled_card_choice_is_its_own_step_inside_the_plan():
    log = Log()
    hand = [_card(0, "Headbutt", target="AnyEnemy"), _card(1, "Strike")]
    log.combat(1, hand, [_enemy(0, 7)], floor=2)
    log.plan([_pa(1, "HEADBUTT", 7, choices=[{"effect": "MoveToDrawTop", "cards": [{"card_id": "STRIKE"}]}]),
              _end(1)])
    log.play(0, 0)
    log.state("card_select", cards=[{"index": 0, "id": "CARD.STRIKE", "name": "Strike", "type": "Attack",
                                     "upgraded": False}], min_select=1, max_select=1)
    log.do("select_cards", indices="0")
    log.combat(1, hand[1:], [_enemy(0, 7, hp=24)], energy=2)
    log.do("end_turn")

    turn = _combat_room(_detail(log))["combat"]["turns"][0]

    assert [s["kind"] for s in turn["steps"]] == ["play_card", "select_cards", "end_turn"]
    choice = turn["steps"][1]
    assert choice["source"] == "choice" and choice["legal"] == []
    assert choice["taken"] == {"indices": [0]}
    assert [c["name"] for c in choice["state"]["offered"]] == ["Strike"]
    assert turn["solver_plans"][0]["actions"][0]["choices"] == [
        {"effect": "MoveToDrawTop", "cards": ["STRIKE"]}]
    assert turn["divergence"] == []


def test_solver_totals_roll_up_into_the_summary():
    log = _two_turn_fight(Log())
    detail = _detail(log)
    assert detail["summary"]["solver"] == {
        "used": True, "plans": 1, "errors": 0, "replans": 0, "diverged_turns": 0,
        "heuristic_steps": 0, "search_ms": 1200}
    combat = _combat_room(detail)["combat"]
    assert (combat["plans"], combat["plan_errors"], combat["rounds"], combat["steps"]) == (1, 0, 1, 3)
    assert combat["enemies"] == ["Louse"]


# ---------------------------------------------------------------------------
# Out-of-combat choices
# ---------------------------------------------------------------------------


def test_card_reward_shows_offered_cards_and_the_pick_or_the_skip():
    log = Log()
    cards = [{"index": i, "id": f"CARD.{n.upper()}", "name": n, "cost": 1, "type": "Attack", "rarity": "Common"}
             for i, n in enumerate(["Cleave", "Anger", "Clash"])]
    log.state("card_reward", room="Monster", floor=2, cards=cards, can_skip=True, gold_earned=17)
    log.do("select_card_reward", card_index=2)
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 3, "type": "Monster"}])
    log.do("select_map_node", col=1, row=3)
    log.state("card_reward", room="Monster", floor=3, cards=cards, can_skip=True)
    log.do("skip_card_reward")

    picked_room, skipped_room = _detail(log)["rooms"]
    picked = picked_room["choices"][0]
    assert picked["type"] == "card_reward" and picked["source"] == "combat"
    assert [c["name"] for c in picked["offered"]] == ["Cleave", "Anger", "Clash"]
    assert picked["picked"] == 2 and picked["picked_card"]["name"] == "Clash"
    assert picked["skipped"] is False and picked["gold_earned"] == 17
    skipped = skipped_room["choices"][0]
    assert skipped["picked"] is None and skipped["picked_card"] is None and skipped["skipped"] is True


def test_event_pages_show_the_options_and_the_chosen_one():
    log = Log()
    log.state("event_choice", event_name="Big Fish", description="A fish.", options=[
        {"index": 0, "title": "Banana", "description": "Heal {Heal} HP.", "is_locked": False,
         "was_chosen": False, "vars": {"Heal": 5}},
        {"index": 1, "title": "Donut", "description": "Max HP.", "is_locked": True}])
    log.do("choose_option", option_index=0)
    log.state("event_choice", event_name="Big Fish", options=[{"index": 0, "title": "Leave"}])
    log.do("leave_room")

    (room,) = _detail(log)["rooms"]
    first, second = room["choices"]
    assert first["type"] == "event" and first["event_name"] == "Big Fish"
    assert [(o["index"], o["title"], o["is_locked"]) for o in first["options"]] == [
        (0, "Banana", False), (1, "Donut", True)]
    assert first["chosen"] == 0 and first["left"] is False
    assert second["chosen"] is None and second["left"] is True


def test_shop_shows_the_stock_and_each_purchase_and_removal():
    log = Log()
    stock = dict(
        cards=[{"index": 0, "name": "Whirlwind", "type": "Attack", "rarity": "Uncommon", "card_cost": 0,
                "cost": 78, "is_stocked": True, "on_sale": False},
               {"index": 1, "name": "Anger", "type": "Attack", "rarity": "Common", "card_cost": 0,
                "cost": 49, "is_stocked": True, "on_sale": True}],
        relics=[{"index": 0, "name": "Pear", "cost": 244, "is_stocked": True}],
        potions=[{"index": 0, "name": "Flex Potion", "cost": 50, "is_stocked": True}],
        card_removal_cost=75)
    log.gold = 300
    log.state("shop", room="Shop", floor=2, **stock)
    log.do("buy_card", card_index=1)
    log.gold = 251
    log.deck = log.deck + _deck(("Anger", False))
    log.state("shop", **stock)
    log.do("remove_card")
    log.state("card_select", cards=[{"index": 0, "id": "CARD.STRIKE", "name": "Strike", "type": "Attack",
                                     "upgraded": False}], min_select=1, max_select=1)
    log.do("select_cards", indices="0")
    log.gold = 176
    log.deck = log.deck[1:]
    log.state("shop", **stock)
    log.do("buy_potion", potion_index=0)
    log.gold = 126
    log.state("shop", **stock)
    log.do("leave_room")

    (room,) = _detail(log)["rooms"]
    (shop,) = room["choices"]
    assert shop["type"] == "shop"
    assert [c["name"] for c in shop["stock"]["cards"]] == ["Whirlwind", "Anger"]
    assert shop["stock"]["cards"][1]["price"] == 49 and shop["stock"]["cards"][1]["on_sale"] is True
    assert [r["name"] for r in shop["stock"]["relics"]] == ["Pear"]
    assert shop["stock"]["card_removal_cost"] == 75
    assert [(p["kind"], (p["item"] or {}).get("name"), p["price"]) for p in shop["purchases"]] == [
        ("card", "Anger", 49), ("remove", "Strike", 75), ("potion", "Flex Potion", 50)]
    assert (shop["gold_start"], shop["gold_end"]) == (300, 126)


def test_rest_site_shows_options_chosen_and_the_smithed_card():
    detail = _detail(_deck_room_log())
    rest = detail["rooms"][1]["choices"][0]
    assert rest["type"] == "rest"
    assert [(o["option_id"], o["is_enabled"]) for o in rest["options"]] == [("HEAL", True), ("SMITH", True)]
    assert rest["chosen"] == 1 and rest["chosen_id"] == "SMITH"
    assert rest["upgraded_card"]["name"] == "Strike"
    # the card_select that picked the card is folded into the rest entry, not listed twice
    assert [c["type"] for c in detail["rooms"][1]["choices"]] == ["rest"]


def test_a_rest_heal_reports_the_hp_before_and_after():
    log = Log()
    log.hp = 40
    log.state("rest_site", room="RestSite", floor=2,
              options=[{"index": 0, "option_id": "HEAL", "name": "Heal", "is_enabled": True}])
    log.do("choose_option", option_index=0)
    log.hp = 64
    log.state("map_select", room="Map", choices=[{"col": 1, "row": 3, "type": "Monster"}])

    (rest,) = _detail(log)["rooms"][0]["choices"]
    assert (rest["chosen_id"], rest["hp_before"], rest["hp_after"], rest["upgraded_card"]) == ("HEAL", 40, 64, None)


def test_a_card_select_in_an_event_and_a_bundle_choice_are_listed():
    log = Log()
    log.state("event_choice", event_name="Neow", options=[{"index": 0, "title": "Scrolls"}])
    log.do("choose_option", option_index=0)
    log.state("bundle_select", bundles=[
        {"index": 0, "cards": [{"name": "Cleave", "cost": 1, "type": "Attack", "rarity": "Common"}]},
        {"index": 1, "cards": [{"name": "Anger", "cost": 0, "type": "Attack", "rarity": "Common"}]}])
    log.do("select_bundle", bundle_index=1)
    log.state("card_select", cards=[{"index": 0, "id": "CARD.STRIKE", "name": "Strike", "type": "Attack",
                                     "upgraded": False}], min_select=0, max_select=1)
    log.do("select_cards", indices="0")

    (room,) = _detail(log)["rooms"]
    event, bundle, select = room["choices"]
    assert event["type"] == "event"
    assert bundle["type"] == "bundle_select" and bundle["chosen"] == 1
    assert [c["name"] for c in bundle["bundles"][1]["cards"]] == ["Anger"]
    assert select["type"] == "card_select" and select["after"] == "bundle_select"
    assert select["chosen"] == [0] and select["chosen_cards"][0]["name"] == "Strike"


def test_a_refused_ooc_action_is_skipped_for_the_fallback_that_took_effect():
    log = Log()
    log.state("event_choice", event_name="X", options=[{"index": 0, "title": "A"}, {"index": 1, "title": "B"}])
    log.do("choose_option", option_index=1)
    log.error("option locked")
    log.do("choose_option", option_index=0)

    (event,) = _detail(log)["rooms"][0]["choices"]
    assert event["chosen"] == 0


# ---------------------------------------------------------------------------
# Summary, localisation, robustness
# ---------------------------------------------------------------------------


def test_summary_describes_the_game():
    log = Log(meta={"harness": "play_full_run", "batch_id": "reg-1", "lane": "Ironclad",
                    "game_index": 2, "games_total": 5, "run_id": "pfr-abc", "game_version": "v0.1"})
    _two_turn_fight(log)
    log.state("game_over", victory=False)

    summary = _detail(log)["summary"]

    assert summary["seed"] == "t1" and summary["character"] == "Ironclad" and summary["ascension"] == 0
    assert summary["experiment"] == "run:ooc=greedy" and summary["policy"] == "greedy"
    assert isinstance(summary["ended_at"], float)
    assert (summary["batch_id"], summary["lane"], summary["game_index"], summary["games_total"]) == (
        "reg-1", "Ironclad", 2, 5)
    assert summary["run_id"] == "pfr-abc" and summary["game_version"] == "v0.1"
    assert summary["outcome"] == "dead" and summary["victory"] is False and summary["complete"] is True
    assert summary["room_count"] == 2 and summary["combat_count"] == 1
    assert summary["max_global_floor"] == 2 and summary["end_label"] == "A1F2"
    assert summary["started_at"] == 1_800_000_000.0
    assert summary["final_error"] is None


def test_run_info_from_the_catalog_overrides_the_outcome():
    log = _two_turn_fight(Log())
    detail = _detail(log, run_info={"status": "stuck", "state": "finished", "source_id": "s1"})
    assert detail["summary"]["outcome"] == "stuck" and detail["summary"]["state"] == "finished"
    assert detail["source_id"] == "s1"


def test_a_log_that_ends_on_an_engine_error_reports_it():
    log = _two_turn_fight(Log())
    log.error("Engine crashed: NullReferenceException\n  at A.B()")
    summary = _detail(log)["summary"]
    assert summary["final_error"] == "Engine crashed: NullReferenceException"
    assert summary["outcome"] == "in_progress"


def test_names_are_localised_from_the_repository_tables():
    log = _two_turn_fight(Log())
    detail = _detail(log, localizer=Localizer())
    loc = detail["loc"]
    assert loc["card"]["Bash"] == "痛击" and loc["card"]["Strike"] == "打击"
    assert loc["card_id"]["BASH"] == "痛击"
    log2 = Log()
    log2.combat(1, [_card(0, "Strike")], [_enemy(0, 1, name="Shrinker Beetle")], floor=2)
    log2.play(0, 0)
    assert _detail(log2, localizer=Localizer())["loc"]["monster"]["Shrinker Beetle"] == "缩小甲虫"


def test_localisation_reads_the_tables_it_is_given(tmp_path: Path):
    for directory, title in (("localization_zhs", "测试牌"), ("localization_eng", "Strike")):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "cards.json").write_text(
            json.dumps({"STRIKE.title": title}), encoding="utf-8")
    localizer = Localizer(tmp_path)
    assert localizer.card("CARD.STRIKE") == "测试牌"
    assert localizer.card(None, "Strike+") == "测试牌"
    assert localizer.card("CARD.NOPE", "Nope") is None
    assert localizer.monster("Nobody") is None   # a missing table is just "no translation"
    assert Localizer(tmp_path / "missing").card("CARD.STRIKE") is None


def test_a_log_without_state_rows_is_rejected():
    with pytest.raises(ValueError):
        build_game_detail([{"type": "run_meta", "ts": 1.0}])


def test_rows_the_replay_parser_rejects_make_the_detail_refuse_the_log():
    """Garbage inside a state / action row is the parser's contract: ValueError
    (the endpoint answers 400) -- never a half-built payload."""
    log = _two_turn_fight(Log())
    log.rows.insert(5, {"step": 2, "type": "state", "data": "garbage"})
    with pytest.raises(ValueError):
        _detail(log)


def test_rows_of_unknown_types_and_shapes_outside_the_game_are_ignored():
    log = _two_turn_fight(Log())
    log.rows.insert(5, {"type": "note", "text": "hello"})
    log.rows.insert(6, {"step": 3, "type": "state", "data": {"type": "mystery"}})
    detail = _detail(log)
    assert len(_combat_room(detail)["combat"]["turns"]) == 1


def test_load_game_records_skips_blank_and_torn_lines(tmp_path: Path):
    path = tmp_path / "g.jsonl"
    path.write_text('{"type":"run_meta"}\n\n{"step":1,"type":"action","data":{}}\n{"step":2,"typ', encoding="utf-8")
    assert [r.get("type") for r in load_game_records(path)] == ["run_meta", "action"]


def test_the_payload_is_json_serialisable_and_has_no_stack_traces():
    log = _two_turn_fight(Log())
    log.do("plan_combat_turn")
    log.reply({"type": "error", "message": "boom", "stack_trace": "   at Secret.Path()"})
    text = json.dumps(_detail(log), allow_nan=False)
    assert "Secret.Path" not in text


# ---------------------------------------------------------------------------
# Real (trimmed) logs
# ---------------------------------------------------------------------------


def _fixture_detail(name: str) -> dict:
    records = load_game_records(FIXTURES / f"game_detail_{name}.jsonl")
    return build_game_detail(records, source_name=f"game_detail_{name}.jsonl", localizer=Localizer())


def test_fixtures_are_small():
    total = sum(path.stat().st_size for path in FIXTURES.glob("game_detail_*.jsonl"))
    assert total < 300_000


def test_real_full_run_route_choices_deck_and_outcome():
    detail = _fixture_detail("full_run")

    summary = detail["summary"]
    assert (summary["character"], summary["seed"], summary["outcome"]) == ("Ironclad", "run_5", "dead")
    assert summary["complete"] is True and summary["end_label"] == "A1F17"
    assert [r["label"] for r in detail["rooms"]] == [
        "A1F1", "A1F2", "A1F6", "A1F7", "A1F8", "A1F9", "A1F17"]
    # the first map choice offered three Monster nodes and took (6, 1)
    first = detail["route"][0]
    assert [(o["col"], o["row"]) for o in first["options"]] == [(2, 1), (3, 1), (6, 1)]
    assert first["taken"] == {"col": 6, "row": 1, "type": "Monster"}
    assert first["entered"]["label"] == "A1F2"
    assert all(entry["taken"] is not None for entry in detail["route"])
    # deck: the three removals and the purchase in the shop, the SMITH in the rest site
    by_label = {r["label"]: r for r in detail["rooms"]}
    shop = by_label["A1F9"]
    assert [c["name"] for c in shop["deck_delta"]["removed"]] == ["Strike", "Strike", "Strike"]
    assert [c["name"] for c in shop["deck_delta"]["added"]] == ["Forgotten Ritual"]
    assert [c["name"] for c in by_label["A1F8"]["deck_delta"]["upgraded"]] == ["Colossus"]
    assert detail["deck"]["size"] == sum(g["count"] for g in detail["deck"]["final"])
    assert detail["deck"]["size"] == 13  # the deck the game_over state reports
    assert detail["deck"]["relics"] and detail["loc"]["card"]["Colossus"] == "巨像"


def test_real_full_run_out_of_combat_choices():
    rooms = {r["label"]: r for r in _fixture_detail("full_run")["rooms"]}

    reward = rooms["A1F2"]["choices"][0]
    assert reward["type"] == "card_reward" and reward["source"] == "combat"
    assert reward["picked_card"]["name"] == "Cruelty" and not reward["skipped"]
    assert len(reward["offered"]) == 3
    event, select, event2 = rooms["A1F7"]["choices"]
    assert (event["type"], select["type"], event2["type"]) == ("event", "card_select", "event")
    assert select["after"] == "event" and select["chosen_cards"][0]["name"] == "Strike"
    rest = rooms["A1F8"]["choices"][0]
    assert rest["chosen_id"] == "SMITH" and rest["upgraded_card"]["name"] == "Colossus"
    (shop,) = rooms["A1F9"]["choices"]
    assert [(p["kind"], (p["item"] or {}).get("name")) for p in shop["purchases"]] == [
        ("remove", "Strike"), ("remove", "Strike"), ("remove", "Strike"), ("card", "Forgotten Ritual")]
    assert shop["purchases"][3]["price"] == 37
    assert shop["stock"]["card_removal_cost"] == 75 and shop["gold_start"] > shop["gold_end"]
    # event options carry the official Chinese title where the game has one
    assert any(o["title_zh"] for o in event["options"])


def test_real_full_run_combat_every_executed_card_was_a_legal_action_and_planned():
    detail = _fixture_detail("full_run")
    boss = next(r for r in detail["rooms"] if r["room_type"] == "Boss")["combat"]

    assert boss["rounds"] == 6 and boss["plans"] == 6 and boss["plan_errors"] == 0
    for combat in (r["combat"] for r in detail["rooms"] if r["combat"]):
        for turn in combat["turns"]:
            assert len(turn["solver_plans"]) == 1 and turn["divergence"] == []
            for step in turn["steps"]:
                assert step["taken_legal"] is not None, (turn["round"], step["n"])
                assert step["legal"][step["taken_legal"]]["kind"] == step["kind"]
                assert step["source"] == "plan" and step["plan"]["plan"] == 0
            plan = turn["solver_plans"][0]
            assert plan["search"]["budget_ms"] == 120000 and plan["executed"] >= 1
    # the death: HP projected by the plan vs what happened
    last_turn = boss["turns"][-1]
    assert last_turn["solver_plans"][0]["projected_player_hp"] is not None


def test_real_replan_turn_is_played_out_then_replanned():
    detail = _fixture_detail("replan")
    combat = next(r for r in detail["rooms"] if r["combat"])["combat"]

    turn = next(t for t in combat["turns"] if t["round"] == 5)
    first, second = turn["solver_plans"]
    assert first["end_reason"] == "exhausted" and first["executed"] == 5
    assert [a["card_id"] for a in first["actions"]] == ["HAVOC", "EVIL_EYE", "BASH", "MAKE_IT_SO", "PECK"]
    assert first["all_enemies_dead"] is True and first["later_actions"] == 0
    assert [a["kind"] for a in second["actions"]] == ["end_turn"]
    assert second["later_actions"] == 1 and second["end_reason"] == "completed"
    assert [d["kind"] for d in turn["divergence"]] == ["exhausted", "replan"]
    assert [s["plan"]["plan"] for s in turn["steps"]] == [0, 0, 0, 0, 0, 1]
    assert combat["replans"] == 1 and combat["diverged_turns"] == 1
    # the other turns followed their plans
    assert all(t["divergence"] == [] for t in combat["turns"] if t["round"] != 5)


def test_real_replan_potion_and_bundled_card_choice():
    combat = next(r for r in _fixture_detail("replan")["rooms"] if r["combat"])["combat"]

    turn = combat["turns"][0]
    kinds = [s["kind"] for s in turn["steps"]]
    assert kinds == ["play_card", "play_card", "play_card", "use_potion", "select_cards",
                     "play_card", "play_card", "end_turn"]
    potion = turn["steps"][3]
    assert potion["taken"]["potion"]["id"] == "POTION.ATTACK_POTION"
    assert potion["legal"][potion["taken_legal"]]["kind"] == "use_potion"
    choice = turn["steps"][4]
    assert choice["source"] == "choice" and choice["taken"]["indices"] == [2]
    assert [c["name"] for c in choice["state"]["offered"]] == ["Molten Fist", "Bludgeon", "Mangle"]
    # the plan listed the potion between the cards, and the playable Mangle came from it
    plan = turn["solver_plans"][0]
    assert [a["kind"] for a in plan["actions"]] == [
        "play_card", "play_card", "play_card", "use_potion", "play_card", "play_card", "end_turn"]
    assert plan["actions"][3]["potion_id"] == "ATTACK_POTION"
    assert plan["projected_player_hp"] == 67 and plan["only_death_routes_found"] is False
    assert plan["search"]["elapsed_ms"] == 20071
    # unplayable cards stay in the hand listing but not in the legal actions
    first = turn["steps"][0]
    names = {c["name"]: c["playable"] for c in first["state"]["hand"]}
    assert names["Astral Pulse"] is False
    assert all(first["state"]["hand"][e["card_index"]]["playable"] for e in first["legal"] if e["kind"] == "play_card")


def test_real_plan_error_falls_back_to_the_heuristic_then_replans():
    combat = next(r for r in _fixture_detail("plan_error")["rooms"] if r["combat"])["combat"]

    assert combat["plan_errors"] == 2 and combat["replans"] == 2 and combat["heuristic_steps"] == 2
    turn = next(t for t in combat["turns"] if t["round"] == 6)
    bad, good = turn["solver_plans"]
    assert bad["ok"] is False and "Cannot fork with pending Power amount changes" in bad["error"]
    assert good["ok"] is True and good["end_reason"] == "completed"
    assert [s["source"] for s in turn["steps"][:2]] == ["heuristic", "plan"]
    assert turn["steps"][0]["taken"]["card"]["name"] == "Sovereign Blade"
    assert [d["kind"] for d in turn["divergence"]] == ["plan_error", "replan"]
    # a plan that completed cleanly has no markers
    clean = next(t for t in combat["turns"] if t["round"] == 5)
    assert clean["divergence"] == [] and clean["solver_plans"][0]["end_reason"] == "completed"


def test_real_logs_round_trip_through_json():
    for name in ("full_run", "replan", "plan_error"):
        json.dumps(_fixture_detail(name), allow_nan=False)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@contextmanager
def _server(catalog: RunCatalog):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_viewer_handler(catalog))
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urlopen(base + path, timeout=10) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _catalog(root: Path) -> RunCatalog:
    return RunCatalog([root], replay_parser=parse_game_progress, include_policy="workbench",
                      clock=lambda: 1_900_000_000.0)


def _place(tmp_path: Path, name: str, relative: str) -> Path:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((FIXTURES / f"game_detail_{name}.jsonl").read_bytes())
    return target


def _source_id(catalog: RunCatalog, filename: str) -> str:
    return next(s["source_id"] for s in catalog.list_sources() if s["display_name"].endswith(filename))


def test_game_detail_endpoint_by_source(tmp_path: Path):
    _place(tmp_path, "full_run", "20261008_115655_Ironclad_run_5.jsonl")
    catalog = _catalog(tmp_path)
    source_id = _source_id(catalog, "20261008_115655_Ironclad_run_5.jsonl")

    with _server(catalog) as base:
        status, payload = _get(base, f"/api/game-detail?source={source_id}")

    assert status == 200 and payload["view"] == "game_detail"
    assert payload["source_id"] == source_id and payload["regression"] is True
    assert payload["live"] is False
    summary = payload["summary"]
    assert summary["seed"] == "run_5" and summary["outcome"] == "dead" and summary["state"] == "finished"
    assert len(payload["route"]) >= 5 and len(payload["rooms"]) == 7
    assert payload["loc"]["card"]["Colossus"] == "巨像"
    assert payload["source_name"] == "20261008_115655_Ironclad_run_5.jsonl"
    # no absolute path leaks into the payload
    assert str(tmp_path) not in json.dumps(payload)


def test_game_detail_endpoint_by_run_id(tmp_path: Path):
    rows = load_game_records(FIXTURES / "game_detail_replan.jsonl")
    rows[0] = {**rows[0], "run_id": "pfr-aaaa1111", "harness": "play_full_run"}
    path = tmp_path / "20261009_164129_Ironclad_p3x_27.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    catalog = _catalog(tmp_path)

    with _server(catalog) as base:
        status, payload = _get(base, "/api/game-detail?run=pfr-aaaa1111")
        missing_status, missing = _get(base, "/api/game-detail?run=pfr-nope")

    assert status == 200 and payload["summary"]["run_id"] == "pfr-aaaa1111"
    assert payload["regression"] is True
    assert missing_status == 404 and "pfr-nope" in missing["error"]


def test_game_detail_endpoint_same_run_in_logs_and_in_the_kept_copy_resolves_to_the_original(tmp_path: Path):
    rows = load_game_records(FIXTURES / "game_detail_replan.jsonl")
    rows[0] = {**rows[0], "run_id": "pfr-bbbb2222", "harness": "play_full_run"}
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (tmp_path / "20261009_164129_Ironclad_p3x_27.jsonl").write_text(text, encoding="utf-8")
    kept = tmp_path / "batches" / "b1" / "games" / "20261009_164129_Ironclad_p3x_27.jsonl"
    kept.parent.mkdir(parents=True)
    kept.write_text(text, encoding="utf-8")
    catalog = _catalog(tmp_path)

    ref = catalog.resolve_game_log(run_id="pfr-bbbb2222")
    assert ref.path == (tmp_path / "20261009_164129_Ironclad_p3x_27.jsonl").resolve()
    assert ref.regression is True


@pytest.mark.parametrize(
    "query, status",
    [
        ("", 400),
        ("?source=", 400),
        ("?source=a&run=b", 400),
        ("?bogus=1", 400),
        ("?source=a&source=b", 400),
        ("?run=a&run=b", 400),
        ("?source=nope", 404),
    ],
)
def test_game_detail_endpoint_validates_its_query(tmp_path: Path, query: str, status: int):
    with _server(_catalog(tmp_path)) as base:
        got, payload = _get(base, "/api/game-detail" + query)
    assert got == status and "error" in payload


def test_game_detail_endpoint_rejects_a_source_that_is_not_a_game_log(tmp_path: Path):
    (tmp_path / "deck_history.jsonl").write_text(
        json.dumps({"deck": ["Strike"], "floor": 3}) + "\n", encoding="utf-8")
    catalog = _catalog(tmp_path)
    source_id = _source_id(catalog, "deck_history.jsonl")
    with _server(catalog) as base:
        status, payload = _get(base, f"/api/game-detail?source={source_id}")
    assert status == 400 and "game log" in payload["error"]


def test_game_detail_endpoint_reports_a_log_the_parser_rejects(tmp_path: Path):
    rows = [{"type": "run_meta", "ts": 1.0, "experiment": "ooc=greedy", "seed": "x", "character": "Ironclad"},
            {"step": 0, "ts": "2026-10-12T13:00:00", "type": "action",
             "data": {"cmd": "start_run", "character": "Ironclad", "seed": "x"}},
            {"step": 1, "ts": "2026-10-12T13:00:01", "type": "action",
             "data": {"cmd": "start_run", "character": "Ironclad", "seed": "x"}}]
    (tmp_path / "bad.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    catalog = _catalog(tmp_path)
    source_id = _source_id(catalog, "bad.jsonl")
    with _server(catalog) as base:
        status, payload = _get(base, f"/api/game-detail?source={source_id}")
    assert status == 400 and "multiple start_run records" in payload["error"]


def test_game_detail_is_cached_per_file_version_and_follows_a_growing_log(tmp_path: Path, monkeypatch):
    path = _place(tmp_path, "replan", "20261009_164129_Ironclad_p3x_27.jsonl")
    catalog = _catalog(tmp_path)
    source_id = _source_id(catalog, path.name)
    calls = []
    real = viewer.build_game_detail

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(viewer, "build_game_detail", counting)
    viewer._game_detail_cache.clear()
    with _server(catalog) as base:
        _get(base, f"/api/game-detail?source={source_id}")
        _get(base, f"/api/game-detail?source={source_id}")
        assert len(calls) == 1
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"step": 999, "ts": "2026-10-12T13:00:00", "type": "action",
                                     "data": {"cmd": "action", "action": "end_turn"}}) + "\n")
        _get(base, f"/api/game-detail?source={source_id}")
    assert len(calls) == 2


def test_game_detail_of_a_live_game_says_so(tmp_path: Path):
    path = _place(tmp_path, "replan", "20261009_164129_Ironclad_p3x_27.jsonl")
    import os
    os.utime(path, (1_900_000_000.0 - 5, 1_900_000_000.0 - 5))
    catalog = _catalog(tmp_path)
    source_id = _source_id(catalog, path.name)
    with _server(catalog) as base:
        status, payload = _get(base, f"/api/game-detail?source={source_id}")
    assert status == 200 and payload["live"] is True
    assert payload["summary"]["outcome"] == "in_progress" and payload["summary"]["state"] == "live"


def test_resolve_game_log_argument_errors(tmp_path: Path):
    catalog = _catalog(tmp_path)
    with pytest.raises(CatalogError):
        catalog.resolve_game_log()
    with pytest.raises(CatalogError):
        catalog.resolve_game_log(source_id="a", run_id="b")
    with pytest.raises(CatalogNotFoundError):
        catalog.resolve_game_log(source_id="nope")
    with pytest.raises(CatalogNotFoundError):
        catalog.resolve_game_log(run_id="pfr-nope")


def test_game_detail_script_is_a_served_utf8_asset(tmp_path: Path):
    from agent.run_workbench.catalog import RunCatalog as _Catalog

    assert viewer.STATIC_ASSETS["/static/game-detail.js"] == (
        "game-detail.js", "text/javascript; charset=utf-8")
    assert "/static/game-detail.js" not in viewer.QUERY_ALLOWED_ASSETS
    with _server(_Catalog([tmp_path], replay_parser=parse_game_progress)) as base:
        with urlopen(base + "/static/game-detail.js", timeout=5) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "text/javascript; charset=utf-8"
