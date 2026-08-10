from types import SimpleNamespace
import json

import pytest

from agent.eval_rl import (
    _VerboseCombatEnv,
    _build_parser,
    _default_eval_batch_id,
    _resolve_combat_snapshot_config,
    _write_boss_deck_record,
    _write_combat_record,
    append_eval_result_row,
    classify_eval_result,
    format_floor_label,
    format_floor_labels,
    should_retry_invalid_result,
    summarize_eval_results,
    run_eval_verbose,
)


VERSION_FIELDS = {
    "game_version": "v0.103.2",
    "game_version_source": "cli",
}


def test_append_eval_result_row_writes_one_strict_json_object(tmp_path):
    path = tmp_path / "nested" / "eval_results.jsonl"
    row = {
        "event": "eval_result",
        "run_id": "eval-14000k-000",
        "score": 21,
    }

    append_eval_result_row(path, row)

    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert json.loads(path.read_text(encoding="utf-8")) == row


def test_append_eval_result_row_rejects_nonstandard_json_numbers(tmp_path):
    with pytest.raises(ValueError):
        append_eval_result_row(tmp_path / "results.jsonl", {"score": float("nan")})


def test_default_eval_batch_ids_do_not_collide_with_frozen_timestamp(monkeypatch):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(eval_rl.time, "strftime", lambda pattern: "20260803T120000")

    first = _default_eval_batch_id("checkpoints/model_14000k.zip", "Ironclad")
    second = _default_eval_batch_id("checkpoints/model_14000k.zip", "Ironclad")

    assert first.startswith("eval-model_14000k-20260803T120000-")
    assert second.startswith("eval-model_14000k-20260803T120000-")
    assert first != second


def test_format_floor_label_uses_act_relative_floor():
    assert format_floor_label(1) == "A1F1"
    assert format_floor_label(7) == "A1F7"
    assert format_floor_label(17) == "A1F17"
    assert format_floor_label(18) == "A2F1"
    assert format_floor_label(22) == "A2F5"
    assert format_floor_label(35) == "A3F1"


def test_format_floor_labels_sorts_numeric_floors_before_formatting():
    assert format_floor_labels([22, 7, 18]) == "[A1F7, A2F1, A2F5]"


def test_global_floor_uses_context_act_when_local_floor_resets():
    import agent.eval_rl as eval_rl

    state = {
        "floor": None,
        "context": {"act": 2, "floor": 4},
    }

    assert eval_rl.global_floor_from_state(state, fallback=17) == 21


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
        (False, True, {"crashed": True}, "crash"),
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
    assert args.results_log == "data/eval_results.jsonl"
    assert args.scenario == "full_run"
    assert args.game_version is None
    assert args.ascension == 0
    assert parser.parse_args(
        ["checkpoints/model.zip", "--ascension", "10"]
    ).ascension == 10


def test_eval_cli_leaves_environment_game_version_to_launch_resolver(monkeypatch):
    monkeypatch.setenv("STS2_GAME_VERSION", "v0.103.2")

    args = _build_parser().parse_args(["checkpoints/model.zip"])

    assert args.game_version is None


@pytest.mark.parametrize("game_version", [None, "", " \t", 103])
def test_run_eval_rejects_invalid_game_version_before_creating_an_env(
    monkeypatch, game_version
):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl,
        "CombatEnv",
        lambda **kwargs: pytest.fail("CombatEnv must not be created"),
    )

    with pytest.raises(ValueError, match="game_version"):
        run_eval_verbose(
            object(),
            "Ironclad",
            game_version=game_version,
            game_version_source="cli",
        )


@pytest.mark.parametrize("game_version_source", [None, "", "auto", 1])
def test_run_eval_rejects_invalid_version_source_before_creating_an_env(
    monkeypatch, game_version_source
):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl,
        "CombatEnv",
        lambda **kwargs: pytest.fail("CombatEnv must not be created"),
    )

    with pytest.raises(ValueError, match="game_version_source"):
        run_eval_verbose(
            object(),
            "Ironclad",
            game_version="v0.103.2",
            game_version_source=game_version_source,
        )


@pytest.mark.parametrize("ascension", [True, -1, 11, 1.0, "1"])
def test_run_eval_rejects_invalid_ascension_before_creating_an_env(
    monkeypatch, ascension
):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl,
        "CombatEnv",
        lambda **kwargs: pytest.fail("CombatEnv must not be created"),
    )

    with pytest.raises(ValueError, match=r"0\.\.10"):
        run_eval_verbose(
            object(),
            "Ironclad",
            ascension=ascension,
            **VERSION_FIELDS,
        )


def test_eval_launch_requires_version_before_checkpoint_load(monkeypatch, capsys):
    import agent.eval_rl as eval_rl

    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    monkeypatch.setattr(eval_rl.sys, "argv", ["eval_rl.py", "model.zip"])
    monkeypatch.setattr(
        eval_rl.MaskablePPO,
        "load",
        lambda *args, **kwargs: pytest.fail("checkpoint must not be loaded"),
    )

    with pytest.raises(SystemExit):
        eval_rl.main()

    error = capsys.readouterr().err
    assert "--game-version" in error
    assert "STS2_GAME_VERSION" in error


def test_eval_launch_rejects_out_of_range_ascension_before_checkpoint_load(
    monkeypatch, capsys
):
    import agent.eval_rl as eval_rl

    monkeypatch.setattr(
        eval_rl.sys,
        "argv",
        [
            "eval_rl.py",
            "model.zip",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "11",
        ],
    )
    monkeypatch.setattr(
        eval_rl.MaskablePPO,
        "load",
        lambda *args, **kwargs: pytest.fail("checkpoint must not be loaded"),
    )

    with pytest.raises(SystemExit):
        eval_rl.main()

    assert "ascension must be an integer in the range 0..10" in capsys.readouterr().err


def test_run_eval_logs_every_retry_attempt_with_stable_schema(monkeypatch, tmp_path):
    import agent.eval_rl as eval_rl

    behaviors = [
        ("crash", 7),
        ("timeout", 8),
        ("stuck", 9),
        ("dead", 11),
    ]
    contexts = []
    env_ascensions = []

    class FakeEnv:
        def __init__(self, **kwargs):
            contexts.append(kwargs["run_context"])
            env_ascensions.append(kwargs["ascension"])
            self.status, self._current_floor = behaviors.pop(0)
            self._current_state = {
                "decision": "combat_play",
                "round": 2,
                "context": {"floor": self._current_floor},
                "player": {"hp": 10, "max_hp": 80},
            }

        def reset(self):
            return [0.0] * 161, {}

        def action_masks(self):
            return [True]

        def step(self, action):
            if self.status == "timeout":
                raise eval_rl._GameTimeout()
            info = {"floor": self._current_floor}
            if self.status == "crash":
                info["crashed"] = True
            elif self.status == "stuck":
                info["stuck"] = True
            return [0.0] * 161, 0.0, True, False, info

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
    results_path = tmp_path / "eval_results.jsonl"

    stats = run_eval_verbose(
        FakeModel(),
        "Ironclad",
        n_games=1,
        fixed_seeds=True,
        invalid_retries=3,
        checkpoint_name="checkpoints/model_14000k.zip",
        batch_id="eval-14000k-20260803T120000",
        ascension=10,
        scenario="full_run",
        results_log_path=str(results_path),
        **VERSION_FIELDS,
    )

    rows = [json.loads(line) for line in results_path.read_text().splitlines()]
    assert [row["status"] for row in rows] == ["crash", "timeout", "stuck", "dead"]
    assert len({row["run_id"] for row in rows}) == 4
    assert [row["retrying"] for row in rows] == [True, True, True, False]
    assert [row["included_in_gameplay"] for row in rows] == [False, False, False, True]
    assert all(row["event"] == "eval_result" for row in rows)
    assert all(row["batch_id"] == "eval-14000k-20260803T120000" for row in rows)
    assert all(row["checkpoint"] == "model_14000k.zip" for row in rows)
    assert all(row["character"] == "Ironclad" for row in rows)
    assert all(row["game_version"] == "v0.103.2" for row in rows)
    assert all(row["game_version_source"] == "cli" for row in rows)
    assert all(row["ascension"] == 10 for row in rows)
    assert all(row["evaluation_mode"] == "fixed" for row in rows)
    assert all(row["scenario"] == "full_run" for row in rows)
    assert all(row["seed"] == "eval_fixed_0" for row in rows)
    assert [row["attempt_index"] for row in rows] == [1, 2, 3, 4]
    assert [context["run_id"] for context in contexts] == [row["run_id"] for row in rows]
    assert env_ascensions == [10, 10, 10, 10]
    assert all(
        context
        == {
            "run_id": row["run_id"],
            "checkpoint": "model_14000k.zip",
            "evaluation_mode": "fixed",
            "scenario": "full_run",
            "game_version": "v0.103.2",
            "game_version_source": "cli",
        }
        for context, row in zip(contexts, rows)
    )
    assert stats["attempt_results"] == rows
    assert len(stats["results"]) == 1
    assert stats["results"][0]["status"] == "dead"
    assert stats["results"][0]["attempts"] == 4
    assert stats["avg_floor"] == 11.0
    assert stats["floors"] == [11]
    assert stats["valid_n"] == 1
    assert stats["invalid_attempts"] == 3


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
        **VERSION_FIELDS,
    )

    assert seeds == ["eval_fixed_3", "eval_fixed_3"]
    assert stats["attempts"] == 2
    assert stats["valid_n"] == 1
    assert stats["invalid_n"] == 0
    assert stats["invalid_attempts"] == 1
    assert stats["status_counts"] == {"crash": 1, "dead": 1}
    assert [row["status"] for row in stats["results"]] == ["dead"]
    assert stats["results"][0]["attempts"] == 2
    assert [row["status"] for row in stats["attempt_results"]] == ["crash", "dead"]
    assert stats["attempt_results"][0]["included_in_gameplay"] is False
    assert stats["attempt_results"][1]["included_in_gameplay"] is True


def test_result_log_failure_is_warned_and_does_not_stop_later_games(monkeypatch):
    import agent.eval_rl as eval_rl

    logged_run_ids = []
    write_calls = 0

    class FakeEnv:
        def __init__(self, **kwargs):
            self._current_floor = 7
            self._current_state = {
                "decision": "combat_play",
                "context": {"floor": 7},
                "player": {"hp": 1, "max_hp": 80},
            }

        def reset(self):
            return [0.0] * 161, {}

        def action_masks(self):
            return [True]

        def step(self, action):
            return [0.0] * 161, 0.0, True, False, {"floor": 7}

        def close(self):
            pass

    class FakeModel:
        observation_space = SimpleNamespace(shape=(161,))

        def predict(self, obs, **kwargs):
            return 0, None

    def flaky_append(path, row):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            raise OSError("disk unavailable")
        logged_run_ids.append(row["run_id"])

    monkeypatch.setattr(eval_rl, "CombatEnv", FakeEnv)
    monkeypatch.setattr(eval_rl, "ActionMasker", lambda env, mask_fn: env)
    monkeypatch.setattr(eval_rl, "append_eval_result_row", flaky_append)
    monkeypatch.setattr(eval_rl.signal, "signal", lambda *args: None)
    monkeypatch.setattr(eval_rl.signal, "alarm", lambda *args: None)

    with pytest.warns(RuntimeWarning, match="disk unavailable"):
        stats = run_eval_verbose(
            FakeModel(),
            "Ironclad",
            n_games=2,
            invalid_retries=0,
            batch_id="eval-log-failure",
            results_log_path="unused.jsonl",
            **VERSION_FIELDS,
        )

    assert stats["valid_n"] == 2
    assert len(stats["results"]) == 2
    assert len(stats["attempt_results"]) == 2
    assert len(stats["result_log_errors"]) == 1
    assert "disk unavailable" in stats["result_log_errors"][0]
    assert logged_run_ids == ["eval-log-failure-001-a02"]


def test_run_eval_marks_act1_boss_beaten_after_entering_act2(monkeypatch, tmp_path):
    import agent.eval_rl as eval_rl

    class FakeEnv:
        def __init__(self, **kwargs):
            self._current_floor = 17
            self._current_state = {
                "decision": "combat_play",
                "round": 1,
                "floor": None,
                "context": {
                    "act": 1,
                    "floor": 17,
                    "room_type": "Boss",
                    "boss": {"id": "CEREMONIAL_BEAST_BOSS"},
                },
                "player": {"hp": 85, "max_hp": 90, "deck": []},
            }
            self._reset_count = 0
            self._step_count = 0

        def reset(self):
            self._reset_count += 1
            if self._reset_count == 2:
                self._current_floor = 4
                self._current_state = {
                    "decision": "combat_play",
                    "round": 1,
                    "floor": None,
                    "context": {
                        "act": 2,
                        "floor": 4,
                        "room_type": "Monster",
                    },
                    "player": {"hp": 20, "max_hp": 90},
                }
            return [0.0] * 161, {}

        def action_masks(self):
            return [True]

        def step(self, action):
            self._step_count += 1
            if self._step_count == 1:
                return [0.0] * 161, 0.0, True, False, {
                    "floor": 17,
                    "combat_won": True,
                }
            return [0.0] * 161, 0.0, True, False, {"floor": 4}

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

    deck_log = tmp_path / "boss.jsonl"
    stats = run_eval_verbose(
        FakeModel(),
        "Ironclad",
        n_games=1,
        invalid_retries=0,
        boss_deck_log_path=str(deck_log),
        **VERSION_FIELDS,
    )

    result = json.loads(deck_log.read_text().strip())
    assert stats["results"][0]["floor"] == 21
    assert stats["per_boss"]["CEREMONIAL_BEAST"]["won"] == 1
    assert result["max_floor"] == 21
    assert result["boss_beaten"] is True


def test_midact_elite_snapshot_preset_targets_floor_6_and_7():
    snapshot_dir, floors = _resolve_combat_snapshot_config(
        preset="midact-elite",
        snapshot_dir=None,
        floors_spec=None,
    )

    assert snapshot_dir == "data/snapshots/midact_elite"
    assert floors == {6, 7}


def test_snapshot_config_requires_directory_and_floors_together():
    with pytest.raises(ValueError, match="both"):
        _resolve_combat_snapshot_config(
            preset=None,
            snapshot_dir="data/snapshots/custom",
            floors_spec=None,
        )


def test_combat_snapshot_record_retains_explicit_checkpoint_path(tmp_path):
    checkpoint = tmp_path / "nested" / "ppo_ironclad_13418k.zip"
    record_path = tmp_path / "records.jsonl"
    state = {
        "player": {"hp": 34, "max_hp": 80, "deck": [], "relics": []},
        "context": {"act": 1, "floor": 7, "room_type": "Elite"},
        "enemies": [{"name": "Byrdonis", "hp": 86, "max_hp": 86, "intents": []}],
    }

    _write_combat_record(
        state,
        floor=7,
        checkpoint=str(checkpoint),
        character="Ironclad",
        game_seed="eval_fixed_0",
        game_index=0,
        jsonl_path=str(record_path),
    )

    record = json.loads(record_path.read_text().strip())
    assert record["checkpoint"] == checkpoint.name
    assert record["checkpoint_path"] == str(checkpoint.resolve())
    assert record["seed"] == "eval_fixed_0"
    assert record["floor"] == 7
    assert record["act"] == 1
    assert record["room_type"] == "Elite"


def test_boss_deck_record_uses_context_progress_fields(tmp_path):
    record_path = tmp_path / "boss.jsonl"
    state = {
        "floor": None,
        "player": {
            "hp": 55,
            "max_hp": 80,
            "deck": [{"id": "CARD.STRIKE_IRONCLAD", "name": "Strike"}],
            "relics": [],
        },
        "context": {
            "act": 1,
            "floor": 17,
            "room_type": "Boss",
            "boss": {"id": "CEREMONIAL_BEAST_BOSS"},
        },
    }

    _write_boss_deck_record(
        state,
        checkpoint="checkpoints/model.zip",
        character="Ironclad",
        game_seed="eval_fixed_0",
        game_index=0,
        jsonl_path=str(record_path),
    )

    record = json.loads(record_path.read_text().strip())
    assert record["act"] == 1
    assert record["floor"] == 17
