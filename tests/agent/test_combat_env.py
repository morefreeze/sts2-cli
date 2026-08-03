import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import json
import pytest
import agent.combat_env as combat_env
from agent.combat_env import CombatEnv, greedy_action
from agent.strategy import rest_site_action

CARDS_JSON = os.path.join(os.path.dirname(__file__), '..', '..', 'localization_eng', 'cards.json')


def test_env_action_space_size():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    assert env.action_space.n == 41


def test_env_observation_space_shape():
    env = CombatEnv(cards_json=CARDS_JSON, character="Ironclad", dry_run=True)
    expected = env.enc.obs_size + env._EXTRA_OBS + env._RELIC_OBS
    assert env.observation_space.shape == (expected,)


def test_reward_combat_win():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._combat_start_player_max_hp = 80
    assert env._combat_win_reward({"player": {"hp": 80, "max_hp": 80}}) == pytest.approx(3.0)
    assert env._combat_win_reward({"player": {"hp": 40, "max_hp": 80}}) == pytest.approx(0.75)


def test_reward_terminal():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    assert env._terminal_reward({"victory": False}) == -2.0
    assert env._terminal_reward({"victory": True}) == 2.0


def test_shaping_reward_damage():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._prev_enemy_hp = 30
    env._prev_player_hp = 80
    env._combat_start_enemy_hp = 30
    env._combat_start_player_max_hp = 80
    r = env._shaping_reward({"enemies": [{"hp": 20}], "player": {"hp": 80}})
    assert r > 0
    assert r == pytest.approx(0.15 * 10 / 30 - 0.003)


def _boss_reward_env(room_type="Boss"):
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._prev_enemy_hp = 250
    env._prev_player_hp = 80
    env._combat_start_enemy_hp = 250
    env._combat_start_player_max_hp = 80
    env._current_floor = 17
    env._current_combat_room_type = room_type
    return env


def test_boss_dense_reward_adds_progress_signal(monkeypatch):
    next_state = {"enemies": [{"hp": 200}], "player": {"hp": 80}}

    monkeypatch.delenv("STS2_BOSS_DENSE", raising=False)
    baseline = _boss_reward_env()._shaping_reward(next_state)

    monkeypatch.setenv("STS2_BOSS_DENSE", "1")
    dense = _boss_reward_env()._shaping_reward(next_state)

    assert dense >= baseline + 0.09


def test_boss_dense_reward_does_not_affect_non_boss_combats(monkeypatch):
    next_state = {"enemies": [{"hp": 200}], "player": {"hp": 80}}

    monkeypatch.delenv("STS2_BOSS_DENSE", raising=False)
    baseline = _boss_reward_env(room_type="Monster")._shaping_reward(next_state)

    monkeypatch.setenv("STS2_BOSS_DENSE", "1")
    dense = _boss_reward_env(room_type="Monster")._shaping_reward(next_state)

    assert dense == baseline


def test_reset_returns_correct_obs_shape():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    obs, info = env.reset()
    assert obs.shape == env.observation_space.shape


def test_step_dry_run_terminates():
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env.reset()
    obs, reward, terminated, truncated, info = env.step(40)  # end_turn
    assert terminated  # dry_run always terminates


def _read_history_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _recording_env(monkeypatch, tmp_path, **kwargs):
    history_path = tmp_path / "deck_history.jsonl"
    monkeypatch.setenv("DECK_HISTORY_PATH", str(history_path))
    context = {
        "run_id": "eval-14000k-000",
        "checkpoint": "model_14000k.zip",
        "evaluation_mode": "fixed",
        "scenario": "full_run",
        "game_version": "v0.103.2",
    }
    context.update(kwargs.pop("run_context", {}))
    env = CombatEnv(
        cards_json=CARDS_JSON,
        character="Ironclad",
        ascension=3,
        dry_run=True,
        run_context=context,
        **kwargs,
    )
    env._run_seed = "eval_fixed_0"
    return env, history_path


def test_run_outcome_retains_identity_and_is_idempotent(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env._run_milestone_records = [{"event": "milestone", "floor": 7}]
    env._run_max_floor = 21

    env._emit_run_outcome({}, victory=False, status="dead")
    env._emit_run_outcome({}, victory=False, status="dead")

    rows = _read_history_rows(history_path)
    assert len(rows) == 2
    outcome = rows[-1]
    assert outcome["event"] == "outcome"
    assert outcome["run_id"] == "eval-14000k-000"
    assert outcome["max_floor"] == 21
    assert outcome["won"] is False
    assert outcome["seed"] == "eval_fixed_0"
    assert outcome["character"] == "Ironclad"
    assert outcome["ascension"] == 3
    assert outcome["checkpoint"] == "model_14000k.zip"
    assert outcome["evaluation_mode"] == "fixed"
    assert outcome["scenario"] == "full_run"
    assert outcome["game_version"] == "v0.103.2"
    assert outcome["status"] == "dead"
    assert outcome["technical_failure_kind"] is None


@pytest.mark.parametrize("status", ["crash", "timeout", "stuck", "reset_failure", "invalid"])
def test_technical_run_outcome_is_never_a_win(monkeypatch, tmp_path, status):
    env, history_path = _recording_env(monkeypatch, tmp_path)

    env._emit_run_outcome({}, victory=True, status=status)

    outcome = _read_history_rows(history_path)[-1]
    assert outcome["status"] == status
    assert outcome["won"] is False
    assert outcome["technical_failure_kind"] == status


def test_dead_process_step_emits_crash_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._game_alive = False

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["crashed"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "crash"


def test_combat_step_limit_emits_timeout_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._game_alive = True
    env._combat_steps = env.max_combat_steps

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["timeout"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "timeout"


def test_ignored_end_turn_emits_stuck_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    env._current_state = combat_env._dummy_combat_state()
    env._current_state["round"] = 2
    env._game_alive = True
    monkeypatch.setattr(env.enc, "decode", lambda action, state: {"action": "end_turn"})
    monkeypatch.setattr(env, "_send", lambda command: env._current_state)
    monkeypatch.setattr(env, "_kill_proc", lambda: None)

    _, _, terminated, _, info = env.step(40)

    assert terminated
    assert info["stuck"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "stuck"


def test_failed_start_emits_reset_failure_outcome(monkeypatch, tmp_path):
    env, history_path = _recording_env(monkeypatch, tmp_path)
    env.dry_run = False
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)
    monkeypatch.setattr(env, "_send", lambda command: {"type": "error", "message": "nope"})

    _, info = env.reset()

    assert info["reset_failure"] is True
    assert _read_history_rows(history_path)[-1]["status"] == "reset_failure"


def test_run_outcome_logging_failure_does_not_escape(monkeypatch, tmp_path):
    env, _ = _recording_env(monkeypatch, tmp_path)
    env._deck_history_path = str(tmp_path)

    env._emit_run_outcome({}, victory=False, status="crash")

    assert env._run_outcome_emitted is True


def test_reset_uses_updated_state_after_hp_override(monkeypatch):
    load_state = {
        "decision": "combat_play",
        "context": {"floor": 17, "room_type": "Boss"},
        "player": {"hp": 80, "max_hp": 80},
        "enemies": [{"hp": 100, "max_hp": 100}],
    }
    hp_state = {
        **load_state,
        "player": {"hp": 72, "max_hp": 80},
    }
    calls = []

    env = CombatEnv(cards_json=CARDS_JSON, native_save_path="boss.save", set_hp_after_load=72)
    monkeypatch.setattr(env, "_kill_proc", lambda: None)
    monkeypatch.setattr(env, "_start_proc", lambda: None)

    def fake_send(cmd):
        calls.append(cmd)
        if cmd["cmd"] == "load_save":
            return load_state
        if cmd["cmd"] == "set_player":
            return hp_state
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(env, "_send", fake_send)
    monkeypatch.setattr(env, "_advance_to_combat", lambda state: state)

    env.reset()

    assert calls == [
        {"cmd": "load_save", "path": "boss.save", "lang": "en"},
        {"cmd": "set_player", "hp": 72},
    ]
    assert env._current_state["player"]["hp"] == 72
    assert env._combat_entry_hp_ratio == pytest.approx(0.9)


def test_greedy_use_potions_refreshes_indices_after_use(monkeypatch):
    vulnerable_0 = {
        "index": 0,
        "name": {"en": "Vulnerable Potion"},
        "description": {"en": "Apply Vulnerable."},
        "target_type": "AnyEnemy",
    }
    blood_1 = {
        "index": 1,
        "name": {"en": "Blood Potion"},
        "description": {"en": "Heal."},
        "target_type": "AnyPlayer",
    }
    vulnerable_2 = {
        "index": 2,
        "name": {"en": "Vulnerable Potion"},
        "description": {"en": "Apply Vulnerable."},
        "target_type": "AnyEnemy",
    }

    def state_with(potions):
        return {
            "decision": "combat_play",
            "context": {"floor": 8, "room_type": "Elite"},
            "player": {"hp": 87, "max_hp": 87, "block": 0, "potions": potions},
            "enemies": [
                {
                    "name": "Bygone Effigy",
                    "hp": 127,
                    "max_hp": 127,
                    "intents": [{"type": "Sleep"}],
                }
            ],
        }

    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_floor = 8
    used_indices = []

    def fake_send(cmd):
        idx = cmd["args"]["potion_index"]
        used_indices.append(idx)
        if idx == 0:
            return state_with([
                {**blood_1, "index": 0},
                {**vulnerable_2, "index": 1},
            ])
        if idx == 1:
            return state_with([{**blood_1, "index": 0}])
        return {"type": "error", "message": f"Invalid potion index {idx}"}

    monkeypatch.setattr(env, "_send", fake_send)

    result = env._greedy_use_potions(state_with([vulnerable_0, blood_1, vulnerable_2]))

    assert used_indices == [0, 1]
    assert result["decision"] == "combat_play"
    assert result.get("type") != "error"


def _vantom_slippery_mask_state():
    return {
        "decision": "combat_play",
        "energy": 3,
        "player": {"hp": 80, "max_hp": 80, "block": 0},
        "hand": [
            {
                "index": 0,
                "id": "CARD.CINDER",
                "cost": 2,
                "type": "Attack",
                "can_play": True,
                "target_type": "AnyEnemy",
                "stats": {"damage": 17},
                "description": "Deal 17 damage.",
            },
            {
                "index": 1,
                "id": "CARD.STRIKE_IRONCLAD",
                "cost": 1,
                "type": "Attack",
                "can_play": True,
                "target_type": "AnyEnemy",
                "stats": {"damage": 6},
                "description": "Deal 6 damage.",
            },
            {
                "index": 2,
                "id": "CARD.DEFEND_IRONCLAD",
                "cost": 1,
                "type": "Skill",
                "can_play": True,
                "target_type": "Self",
                "stats": {"block": 5},
                "description": "Gain 5 Block.",
            },
        ],
        "enemies": [
            {
                "name": "Vantom",
                "hp": 173,
                "max_hp": 173,
                "intents": [{"type": "Attack", "damage": 7}],
                "powers": [{"name": "Slippery", "amount": 9}],
            }
        ],
    }


def test_action_masks_leave_vantom_slippery_filter_off_by_default(monkeypatch):
    monkeypatch.delenv("STS2_VANTOM_SLIPPERY_MASK", raising=False)
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _vantom_slippery_mask_state()

    masks = env.action_masks()

    assert masks[0]
    assert masks[4]
    assert masks[11]
    assert masks[40]


def test_action_masks_apply_vantom_slippery_strip_filter(monkeypatch):
    monkeypatch.setenv("STS2_VANTOM_SLIPPERY_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _vantom_slippery_mask_state()

    masks = env.action_masks()

    assert not masks[0]
    assert masks[4]
    assert masks[11]
    assert masks[40]


def _boss_planner_mask_state():
    strike = {
        "index": 0,
        "id": "CARD.STRIKE_IRONCLAD",
        "cost": 1,
        "type": "Attack",
        "can_play": True,
        "target_type": "AnyEnemy",
        "stats": {"damage": 6},
        "description": "Deal 6 damage.",
    }
    defend = {
        "index": 1,
        "id": "CARD.DEFEND_IRONCLAD",
        "cost": 1,
        "type": "Skill",
        "can_play": True,
        "target_type": "Self",
        "stats": {"block": 5},
        "description": "Gain 5 Block.",
    }
    return {
        "decision": "combat_play",
        "energy": 3,
        "max_energy": 3,
        "round": 1,
        "context": {"floor": 17, "room_type": "Boss"},
        "player": {
            "hp": 30,
            "max_hp": 80,
            "block": 0,
            "deck": [strike, defend],
            "relics": [],
        },
        "player_powers": None,
        "hand": [strike, defend],
        "enemies": [
            {
                "name": "Boss",
                "hp": 6,
                "max_hp": 50,
                "block": 0,
                "intents": [{"type": "attack", "damage": 12, "hits": 1}],
                "powers": None,
            }
        ],
    }


def test_action_masks_can_force_boss_planner_choice(monkeypatch):
    monkeypatch.setenv("STS2_BOSS_PLANNER_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    env._current_state = _boss_planner_mask_state()

    masks = env.action_masks()

    assert masks.sum() == 1
    assert masks[0]


def test_action_masks_can_force_elite_planner_choice(monkeypatch):
    monkeypatch.setenv("STS2_BOSS_PLANNER_MASK", "1")
    env = CombatEnv(cards_json=CARDS_JSON, dry_run=True)
    state = _boss_planner_mask_state()
    state["context"]["room_type"] = "Elite"
    env._current_state = state

    masks = env.action_masks()

    assert masks.sum() == 1
    assert masks[0]


def test_greedy_action_map_select():
    state = {
        "decision": "map_select",
        "choices": [
            {"col": 1, "row": 3, "type": "rest"},
            {"col": 2, "row": 3, "type": "enemy"},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "select_map_node"
    # col and row must come from the same node (not independently sampled)
    col = action["args"]["col"]
    row = action["args"]["row"]
    valid_pairs = {(c["col"], c["row"]) for c in state["choices"]}
    assert (col, row) in valid_pairs


def test_greedy_action_card_reward():
    state = {
        "decision": "card_reward",
        "player": {"deck": []},
        "cards": [{
            "index": 0,
            "id": "CARD.BLUDGEON",
            "cost": 3,
            "type": "Attack",
            "rarity": "Rare",
            "stats": {"damage": 32},
            "description": "Deal 32 damage.",
        }],
    }
    action = greedy_action(state)
    assert action["action"] == "select_card_reward"


def _late_card_reward_state():
    return {
        "decision": "card_reward",
        "act": 1,
        "floor": 12,
        "player": {
            "deck": [{"id": f"CARD.DECK_{i}"} for i in range(15)],
            "deck_size": 15,
        },
        "cards": [
            {"id": "CARD.OLD_TOP", "index": 0},
            {"id": "CARD.ELIGIBLE", "index": 1},
        ],
    }


def test_card_quality_gate_selects_eligible_card_at_original_index(monkeypatch):
    state = _late_card_reward_state()
    seen = {}
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: card["id"] == "CARD.ELIGIBLE",
    )

    def fake_pick(cards, *, threshold, deck):
        seen["ids"] = [card["id"] for card in cards]
        return 0

    monkeypatch.setattr(combat_env, "pick_best_card", fake_pick)
    action = greedy_action(state)
    assert seen["ids"] == ["CARD.ELIGIBLE"]
    assert action["args"]["card_index"] == 1


def test_card_quality_gate_reads_act_from_runtime_context(monkeypatch):
    state = _late_card_reward_state()
    state.pop("act")
    state["context"] = {"act": 1}
    seen = {}
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")

    def fake_eligibility(card, deck, act):
        seen["act"] = act
        return card["id"] == "CARD.ELIGIBLE"

    monkeypatch.setattr(
        combat_env, "is_act1_card_reward_eligible", fake_eligibility
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert seen["act"] == 1
    assert action["args"]["card_index"] == 1


def test_card_quality_gate_bypasses_early_inflated_deck(monkeypatch):
    state = _late_card_reward_state()
    state["floor"] = 7
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("early Act 1 rewards should remain unfiltered")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert action["args"]["card_index"] == 0


def test_card_quality_gate_skips_when_every_offer_is_ineligible(monkeypatch):
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: False,
    )
    monkeypatch.setattr(
        combat_env,
        "pick_best_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty eligible set should skip before ranking")
        ),
    )
    assert greedy_action(_late_card_reward_state()) == {
        "cmd": "action",
        "action": "skip_card_reward",
    }


def test_card_quality_gate_falls_back_for_forced_reward(monkeypatch):
    state = _late_card_reward_state()
    state["can_skip"] = False
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "1")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda card, deck, act: False,
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )

    action = greedy_action(state)

    assert action["action"] == "select_card_reward"
    assert action["args"]["card_index"] == 0


def test_card_quality_gate_is_enabled_by_default_after_promotion(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.delenv("STS2_CARD_QUALITY_GATE", raising=False)
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: False,
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["action"] == "skip_card_reward"


@pytest.mark.parametrize("flag", ["", "garbage"])
def test_card_quality_gate_requires_recognized_opt_in(monkeypatch, flag):
    state = _late_card_reward_state()
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", flag)
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("unknown flag value must not enable the gate")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 0


def test_card_quality_gate_zero_restores_unfiltered_selection(monkeypatch):
    state = _late_card_reward_state()
    monkeypatch.setenv("STS2_CARD_QUALITY_GATE", "0")
    monkeypatch.setattr(
        combat_env,
        "is_act1_card_reward_eligible",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("disabled gate should not inspect offers")
        ),
    )
    monkeypatch.setattr(
        combat_env, "pick_best_card", lambda cards, *, threshold, deck: 0
    )
    assert greedy_action(state)["args"]["card_index"] == 0


def test_greedy_action_rest_heal():
    state = {
        "decision": "rest_site",
        "options": [
            {"index": 0, "option_id": "SMITH", "is_enabled": True},
            {"index": 1, "option_id": "HEAL", "is_enabled": True},
        ]
    }
    action = greedy_action(state)
    assert action["action"] == "choose_option"
    assert action["args"]["option_index"] == 1  # HEAL preferred


def _rest_options():
    return [
        {"index": 0, "option_id": "SMITH", "is_enabled": True},
        {"index": 1, "option_id": "HEAL", "is_enabled": True},
    ]


def _must_smith_deck():
    return [
        {
            "id": "CARD.INFLAME",
            "type": "Power",
            "rarity": "Uncommon",
            "cost": 1,
            "description": "Gain 2 Strength.",
        }
    ]


def test_vantom_mid_act_rest_keeps_must_smith_override_by_default(monkeypatch):
    monkeypatch.delenv("STS2_VANTOM_REST_HEAL", raising=False)
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "VANTOM_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 0


def test_vantom_mid_act_rest_heals_below_seventy_when_enabled(monkeypatch):
    monkeypatch.setenv("STS2_VANTOM_REST_HEAL", "1")
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "VANTOM_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 1


def test_non_vantom_mid_act_rest_keeps_must_smith_override():
    state = {
        "decision": "rest_site",
        "floor": 12,
        "context": {"boss": {"id": "CEREMONIAL_BEAST_BOSS"}},
        "player": {"hp": 54, "max_hp": 80, "deck": _must_smith_deck()},
    }

    action = rest_site_action(state, _rest_options())

    assert action["args"]["option_index"] == 0
