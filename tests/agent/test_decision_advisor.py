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


def test_card_reward_prefers_card_that_fills_weak_defense_axis():
    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
    cards = [
        card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0),
        card("CARD.STRIKE_IRONCLAD", damage=6, index=1),
    ]
    state = base_state("card_reward", cards=cards)
    state["player"]["deck"] = deck

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {
        "cmd": "action",
        "action": "select_card_reward",
        "args": {"card_index": 0},
    }


def test_card_reward_skips_when_candidate_worsens_large_deck():
    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(20)]
    bad = card("CARD.WOUND", ctype="Status", index=0)
    state = base_state("card_reward", cards=[bad])
    state["player"]["deck"] = deck

    cmd = DecisionAdvisor().choose(state)

    assert cmd == {"cmd": "action", "action": "skip_card_reward"}


def test_card_reward_falls_back_for_unscorable_fixture_cards():
    state = base_state("card_reward", cards=[{"index": 0}])

    cmd = DecisionAdvisor().choose(state)

    assert cmd is None


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


def test_greedy_action_uses_decision_advisor_for_card_rewards():
    from agent.combat_env import greedy_action

    deck = [card("CARD.STRIKE_IRONCLAD", damage=6) for _ in range(5)]
    state = base_state(
        "card_reward",
        cards=[
            card("CARD.DEFEND_IRONCLAD", ctype="Skill", block=5, index=0),
            card("CARD.STRIKE_IRONCLAD", damage=6, index=1),
        ],
    )
    state["player"]["deck"] = deck

    assert greedy_action(state)["args"]["card_index"] == 0
