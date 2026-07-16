from types import SimpleNamespace

import pytest

from agent.eval_rl import (
    _VerboseCombatEnv,
    _build_parser,
    classify_eval_result,
    format_floor_label,
    format_floor_labels,
    should_retry_invalid_result,
    summarize_eval_results,
    run_eval_verbose,
)


def test_format_floor_label_uses_act_relative_floor():
    assert format_floor_label(1) == "A1F1"
    assert format_floor_label(7) == "A1F7"
    assert format_floor_label(17) == "A1F17"
    assert format_floor_label(18) == "A2F1"
    assert format_floor_label(22) == "A2F5"
    assert format_floor_label(35) == "A3F1"


def test_format_floor_labels_sorts_numeric_floors_before_formatting():
    assert format_floor_labels([22, 7, 18]) == "[A1F7, A2F1, A2F5]"


def test_verbose_card_reward_log_uses_actual_greedy_command(monkeypatch):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl,
        "greedy_action",
        lambda state: {
            "cmd": "action",
            "action": "select_card_reward",
            "args": {"card_index": 7},
        },
    )
    env = object.__new__(_VerboseCombatEnv)
    env.room_log = []
    state = {
        "decision": "card_reward",
        "floor": 7,
        "player": {"hp": 34, "max_hp": 80},
        "cards": [
            {
                "index": 7,
                "id": "CARD.BASH",
                "cost": 2,
                "type": "Attack",
                "rarity": "Basic",
                "stats": {"damage": 8},
                "description": "Deal damage. Apply Vulnerable.",
            },
            {
                "index": 2,
                "id": "CARD.BLUDGEON",
                "cost": 3,
                "type": "Attack",
                "rarity": "Rare",
                "stats": {"damage": 32},
                "description": "Deal 32 damage.",
            },
        ],
    }

    env._log_room(state, "card_reward")

    assert env.room_log[-1].endswith("→ BASH")


@pytest.mark.parametrize(
    ("timed_out", "run_won", "info", "expected"),
    [
        (False, True, {}, "win"),
        (False, False, {}, "dead"),
        (False, False, {"crashed": True}, "crash"),
        (False, False, {"timeout": True}, "timeout"),
        (False, False, {"stuck": True}, "stuck"),
        (True, False, {"crashed": True}, "timeout"),
    ],
)
def test_classify_eval_result(timed_out, run_won, info, expected):
    assert classify_eval_result(
        timed_out=timed_out,
        run_won=run_won,
        info=info,
    ) == expected


def test_invalid_result_retries_only_within_budget():
    assert should_retry_invalid_result("crash", retries_used=0, retry_limit=1)
    assert not should_retry_invalid_result("crash", retries_used=1, retry_limit=1)
    assert not should_retry_invalid_result("dead", retries_used=0, retry_limit=1)


def test_eval_summary_excludes_invalid_attempts_from_performance_metrics():
    stats = summarize_eval_results(
        [
            {"seed": "s0", "status": "dead", "floor": 7, "combat_wins": 6},
            {"seed": "s1", "status": "crash", "floor": 1, "combat_wins": 0},
            {"seed": "s2", "status": "win", "floor": 18, "combat_wins": 17},
        ],
        requested_n=3,
        total_attempts=4,
        attempt_statuses=["dead", "crash", "crash", "win"],
    )

    assert stats["requested_n"] == 3
    assert stats["valid_n"] == stats["n"] == 2
    assert stats["invalid_n"] == 1
    assert stats["invalid_attempts"] == 2
    assert stats["attempts"] == 4
    assert stats["floors"] == [7, 18]
    assert stats["avg_floor"] == 12.5
    assert stats["max_floor"] == 18
    assert stats["win_rate"] == 0.5
    assert stats["avg_combat_wins"] == 11.5
    assert stats["status_counts"] == {"crash": 2, "dead": 1, "win": 1}


def test_eval_cli_requires_checkpoint_and_defaults_to_fixed_seeds():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    args = parser.parse_args(["checkpoints/model.zip"])
    assert args.checkpoint == "checkpoints/model.zip"
    assert args.fixed_seeds is True
    assert parser.parse_args(["checkpoints/model.zip", "--random-seeds"]).fixed_seeds is False
    assert args.invalid_retries == 1


def test_run_eval_retries_invalid_attempt_with_the_same_fixed_seed(monkeypatch):
    import agent.eval_rl as eval_rl

    seeds = []
    attempt_infos = [
        {"crashed": True, "floor": 7},
        {"floor": 7},
    ]

    class FakeEnv:
        def __init__(self, **kwargs):
            seeds.append(kwargs["seed"])
            self.info = attempt_infos.pop(0)
            self._current_floor = 7
            self._current_state = {
                "decision": "combat_play",
                "round": 2,
                "context": {},
                "player": {"hp": 0, "max_hp": 80},
            }

        def reset(self):
            return [0.0] * 161, {}

        def action_masks(self):
            return [True]

        def step(self, action):
            return [0.0] * 161, 0.0, True, False, self.info

        def close(self):
            pass

    class FakeModel:
        observation_space = SimpleNamespace(shape=(161,))

        def predict(self, obs, **kwargs):
            return 0, None

    monkeypatch.setattr(eval_rl, "CombatEnv", FakeEnv)
    monkeypatch.setattr(eval_rl, "ActionMasker", lambda env, mask_fn: env)
    monkeypatch.setattr(eval_rl.signal, "signal", lambda *args: None)
    monkeypatch.setattr(eval_rl.signal, "alarm", lambda *args: None)

    stats = run_eval_verbose(
        FakeModel(),
        "Ironclad",
        n_games=1,
        fixed_seeds=True,
        seed_offset=3,
        invalid_retries=1,
    )

    assert seeds == ["eval_fixed_3", "eval_fixed_3"]
    assert stats["attempts"] == 2
    assert stats["valid_n"] == 1
    assert stats["invalid_n"] == 0
    assert stats["invalid_attempts"] == 1
    assert stats["status_counts"] == {"crash": 1, "dead": 1}
    assert stats["results"][0]["status"] == "dead"
    assert stats["results"][0]["attempts"] == 2
