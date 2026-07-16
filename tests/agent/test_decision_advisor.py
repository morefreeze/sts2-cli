from agent.decision_advisor import DecisionAdvisor, evaluate_deck


def card(cid, *, cost=1, ctype="Attack", damage=0, block=0, draw=0, index=0):
    stats = {}
    if damage:
        stats["damage"] = damage
    if block:
        stats["block"] = block
    if draw:
        stats["cards"] = draw
    return {
        "id": cid,
        "name": {"en": cid},
        "cost": cost,
        "type": ctype,
        "rarity": "Common",
        "stats": stats,
        "description": "",
        "index": index,
    }


def base_state(decision, **extra):
    state = {
        "decision": decision,
        "floor": 5,
        "player": {"hp": 60, "max_hp": 80, "gold": 0, "deck": []},
    }
    state.update(extra)
    return state


def test_evaluate_deck_exposes_directional_axes():
    deck = [
        card("CARD.STRIKE_IRONCLAD", damage=6),
        card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5),
        card("CARD.SHRUG_IT_OFF", ctype="Skill", block=8, draw=1),
    ]

    scores = evaluate_deck(deck)

    assert set(scores) >= {"attack", "defense", "cycle", "energy", "boss_ready"}
    assert scores["defense"] > 0
    assert scores["cycle"] > 0


def test_card_reward_is_owned_by_shared_fallback_policy():
    state = base_state(
        "card_reward",
        cards=[card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0)],
    )

    assert DecisionAdvisor().choose(state) is None


def test_map_select_avoids_elite_at_low_hp():
    state = base_state(
        "map_select",
        choices=[
            {"type": "Elite", "col": 0, "row": 1},
            {"type": "Event", "col": 1, "row": 1},
        ],
    )
    state["player"]["hp"] = 20
    state["player"]["deck"] = [card("CARD.STRIKE_IRONCLAD", damage=6)]

    cmd = DecisionAdvisor().choose(state)

    assert cmd["action"] == "select_map_node"
    assert cmd["args"] == {"col": 1, "row": 1}


def test_rest_site_heals_at_critical_hp():
    state = base_state(
        "rest_site",
        options=[
            {"index": 0, "option_id": "SMITH", "is_enabled": True},
            {"index": 1, "option_id": "HEAL", "is_enabled": True},
        ],
    )
    state["player"]["hp"] = 18
    state["player"]["deck"] = [card("CARD.DEMON_FORM", ctype="Power", cost=3)]

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "choose_option", "args": {"option_index": 1}}


def test_combat_uses_lethal_planner_when_available(monkeypatch):
    import agent.decision_advisor as da

    monkeypatch.setattr(da, "plan_action", lambda state: 0)
    state = base_state(
        "combat_play",
        hand=[{"id": "CARD.STRIKE_IRONCLAD"}],
        enemies=[{"hp": 6, "intents": []}],
    )

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}


def test_greedy_action_uses_shared_card_reward_policy_when_advisor_enabled(monkeypatch):
    from agent import combat_env

    monkeypatch.setenv("STS2_DECISION_ADVISOR", "1")
    monkeypatch.setattr(combat_env, "pick_best_card", lambda *args, **kwargs: 1)
    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
    state = base_state(
        "card_reward",
        cards=[
            card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0),
            card("CARD.STRIKE_IRONCLAD", damage=6, index=1),
        ],
    )
    state["player"]["deck"] = deck

    assert combat_env.greedy_action(state)["args"]["card_index"] == 1


def test_greedy_action_can_disable_decision_advisor(monkeypatch):
    from agent import combat_env

    class FailingAdvisor:
        def choose(self, state):
            raise AssertionError("advisor should be bypassed")

    monkeypatch.setenv("STS2_DECISION_ADVISOR", "0")
    monkeypatch.setattr(combat_env, "_decision_advisor", FailingAdvisor())

    state = {
        "decision": "map_select",
        "choices": [
            {"col": 1, "row": 3, "type": "enemy"},
            {"col": 2, "row": 3, "type": "rest"},
        ],
    }

    assert combat_env.greedy_action(state)["action"] == "select_map_node"


def test_greedy_action_defaults_decision_advisor_off(monkeypatch):
    from agent import combat_env

    class FailingAdvisor:
        def choose(self, state):
            raise AssertionError("advisor should require explicit opt-in")

    monkeypatch.delenv("STS2_DECISION_ADVISOR", raising=False)
    monkeypatch.setattr(combat_env, "_decision_advisor", FailingAdvisor())

    state = {
        "decision": "map_select",
        "choices": [
            {"col": 1, "row": 3, "type": "enemy"},
            {"col": 2, "row": 3, "type": "rest"},
        ],
    }

    assert combat_env.greedy_action(state)["action"] == "select_map_node"


def test_greedy_action_enables_decision_advisor_explicitly(monkeypatch):
    from agent import combat_env

    advised = {"cmd": "action", "action": "leave_room"}

    class RecordingAdvisor:
        def choose(self, state):
            return advised

    monkeypatch.setenv("STS2_DECISION_ADVISOR", "1")
    monkeypatch.setattr(combat_env, "_decision_advisor", RecordingAdvisor())

    assert combat_env.greedy_action({"decision": "event_choice"}) == advised
