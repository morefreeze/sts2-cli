from agent.act_plan import fit_curve, rest_thresholds


def test_losses_raise_the_required_hp():
    prior = lambda hp: 0.55
    wins = fit_curve([(0.5, True, 1)] * 25, prior, 80)
    losses = fit_curve([(0.5, False, 1)] * 25, prior, 80)
    assert wins.probability(40) > losses.probability(40)
    assert wins.required_hp(0.8) < losses.required_hp(0.8)
    values = [wins.probability(hp) for hp in range(81)]
    assert values == sorted(values)


def test_rest_threshold_accounts_for_later_heal_but_requires_surviving_fights():
    kinds = ["RestSite", "Elite", "RestSite", "Monster"]
    assert rest_thresholds(kinds, [0, 20, 0, 10], 55, 24) == {0: 61, 2: 65}
    assert rest_thresholds(kinds, [0, 20, 0, 10], 3, 24) == {0: 21, 2: 13}


def test_map_and_actual_rest_use_one_plan(monkeypatch):
    from agent import act_plan
    from agent.greedy_trace import GreedyTrace
    from agent.strategy import LookaheadMapStrategy, rest_site_action

    monkeypatch.setenv("STS2_ACT_PLAN", "1")
    monkeypatch.setattr(act_plan, "_boss_curve",
                        lambda *args: act_plan.BossCurve(80, (0, 1), 40, 20))
    fight = {"type": "Monster", "col": 0, "row": 13, "children": [[0, 14]]}
    rest = {"type": "RestSite", "col": 0, "row": 14, "children": [[0, 15]]}
    boss = {"type": "Boss", "col": 0, "row": 15}
    planning = {"relic_ids": ["TINY_MAILBOX"], "potion_slots": 3, "rest_heal_hp": 24}
    whole_map = {"rows": [[fight], [rest]], "boss": boss, "act_planning": planning}
    player = {"hp": 42, "max_hp": 80, "potions": [{"id": "FIRE_POTION"}, {"id": "BLOCK_POTION"}]}
    state = {"player": player, "context": {"act": 1, "boss": {"id": "VANTOM_BOSS"}},
             "map": whole_map, "ascension": 9}
    route_trace = GreedyTrace()
    choice = LookaheadMapStrategy().choose(state, [fight], trace=route_trace)
    assert choice["mailbox_restock"] is True
    planned_floor, heal, arrival, threshold = route_trace.to_dict()["options"][0]["detail"]["rest_actions"][0]
    assert planned_floor == 15 and heal and threshold == 64
    blocked = {**state, "map": {**whole_map,
                                "act_planning": {**planning, "relic_ids": ["TINY_MAILBOX", "SOZU"]}}}
    assert "mailbox_restock" not in LookaheadMapStrategy().choose(blocked, [fight])

    rest_state = {**state, "player": {**player, "hp": arrival},
                  "map": {**whole_map, "current_coord": [0, 14]}}
    options = [{"index": 0, "option_id": "heal"}, {"index": 1, "option_id": "smith"}]
    assert rest_site_action(rest_state, options)["args"] == {"option_index": 0}
    rest_state["player"]["hp"] = 70
    assert rest_site_action(rest_state, options)["args"] == {"option_index": 1}


def test_history_does_not_treat_abandoned_boss_as_loss():
    from agent.act_plan import boss_observations

    rooms = [{"rooms": [], "player_stats": [{"current_hp": 30, "max_hp": 80}]},
             {"rooms": [{"room_type": "boss", "model_id": "ENCOUNTER.VANTOM_BOSS"}]}]
    run = {"players": [{"character": "CHARACTER.FOOL"}], "ascension": 9,
           "map_point_history": [rooms], "killed_by_encounter": "ENCOUNTER.VANTOM_BOSS"}
    only = list(boss_observations(run))
    assert len(only) == 1 and only[0].hp_ratio == 0.375 and not only[0].won
    assert list(boss_observations({**run, "was_abandoned": True})) == []
    assert list(boss_observations({**run, "win": True}))[0].won


def test_second_boss_uses_the_map_boss_instead_of_the_act_boss(monkeypatch):
    from agent import act_plan
    from agent.greedy_trace import GreedyTrace
    from agent.strategy import LookaheadMapStrategy

    monkeypatch.setenv("STS2_ACT_PLAN", "1")

    def curve(root, refresh, boss, *args):
        assert boss == "QUEEN_BOSS"
        return act_plan.BossCurve(80, (0, 1), 6, 3)

    monkeypatch.setattr(act_plan, "_boss_curve", curve)
    boss = {"id": "QUEEN_BOSS", "type": "Boss", "col": 3, "row": 17}
    state = {"player": {"hp": 60, "max_hp": 80}, "ascension": 10,
             "context": {"act": 3, "boss": {"id": "AEONGLASS_BOSS"}},
             "map": {"rows": [[{"type": "RestSite", "col": 3, "row": 15}]], "boss": boss}}
    trace = GreedyTrace()
    LookaheadMapStrategy().choose(state, [boss], trace=trace)
    assert next(r for r in trace.rules if r["key"] == "map.boss_plan")["values"]["samples"] == 3
