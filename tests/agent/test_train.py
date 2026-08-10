import pytest
from types import SimpleNamespace

from agent.train import (
    _apply_training_profile,
    _make_vec_env,
    _parse_hp_curriculum_values,
    _snapshot_curriculum_phase,
    _validate_training_profile,
    make_env,
    run_eval,
)


VERSION_FIELDS = {
    "game_version": "v0.103.2",
    "game_version_source": "cli",
}


def test_parse_hp_curriculum_values_uses_default_four_phase_anchors():
    assert _parse_hp_curriculum_values("100,90,80,72") == [
        (0.00, 100),
        (0.30, 90),
        (0.60, 80),
        (0.85, 72),
    ]


def test_parse_hp_curriculum_values_accepts_natural_phase():
    assert _parse_hp_curriculum_values("100,natural") == [
        (0.00, 100),
        (0.85, 0),
    ]


def test_parse_hp_curriculum_values_rejects_empty_phase():
    with pytest.raises(ValueError, match="empty"):
        _parse_hp_curriculum_values("100,,72")


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (0.00, (3, 0)),
        (0.34, (3, 0)),
        (0.35, (2, 1)),
        (0.69, (2, 1)),
        (0.70, (1, 2)),
        (0.89, (1, 2)),
        (0.90, (0, 3)),
        (1.00, (0, 3)),
    ],
)
def test_snapshot_curriculum_decays_snapshot_envs(progress, expected):
    assert _snapshot_curriculum_phase(progress, n_envs=4) == expected


def _profile_args(profile):
    return SimpleNamespace(
        profile=profile,
        n_envs=4,
        snapshot_curriculum=False,
        mix_save_envs=-1,
        hp_curriculum=False,
        hp_curriculum_values=None,
        ent_coef=None,
        eval_freq=50_000,
        save_dir=None,
    )


def test_midact_elite_profile_enables_decaying_snapshot_training():
    args = _profile_args("midact-elite")

    _apply_training_profile(args, explicit_options=set())

    assert args.snapshot_curriculum is True
    assert args.mix_save_envs == -1
    assert args.hp_curriculum is False
    assert args.ent_coef == 0.10
    assert args.eval_freq == 25_000
    assert args.save_dir == "checkpoints_midact_elite"


def test_act1_boss_profile_enables_mixed_hp_curriculum():
    args = _profile_args("act1-boss")

    _apply_training_profile(args, explicit_options=set())

    assert args.snapshot_curriculum is False
    assert args.mix_save_envs == 2
    assert args.hp_curriculum is True
    assert args.hp_curriculum_values == "100,90,80,natural"
    assert args.ent_coef == 0.15
    assert args.eval_freq == 25_000
    assert args.save_dir == "checkpoints_act1_boss"


def test_explicit_cli_values_override_training_profile_defaults():
    args = _profile_args("midact-elite")
    args.snapshot_curriculum = False
    args.mix_save_envs = 1
    args.ent_coef = 0.04
    args.eval_freq = 0
    args.save_dir = "custom_checkpoints"

    _apply_training_profile(
        args,
        explicit_options={
            "--mix-save-envs",
            "--ent-coef",
            "--eval-freq",
            "--save-dir",
        },
    )

    assert args.snapshot_curriculum is False
    assert args.mix_save_envs == 1
    assert args.ent_coef == 0.04
    assert args.eval_freq == 0
    assert args.save_dir == "custom_checkpoints"


def test_training_profile_requires_a_snapshot_pool():
    args = _profile_args("midact-elite")

    with pytest.raises(ValueError, match="save pool"):
        _validate_training_profile(args, load_saves=[])

    _validate_training_profile(args, load_saves=["floor7.save"])


def test_make_env_snapshots_run_context_before_factory_invocation(monkeypatch):
    import agent.train as train

    env_kwargs = []
    monkeypatch.setattr(
        train,
        "CombatEnv",
        lambda **kwargs: env_kwargs.append(kwargs) or SimpleNamespace(),
    )
    monkeypatch.setattr(train, "ActionMasker", lambda env, mask_fn: env)
    context = {
        "checkpoint": "source.zip",
        "evaluation_mode": "training",
        "scenario": "full_run",
        **VERSION_FIELDS,
    }

    factory = make_env(
        "Ironclad",
        10,
        worker_id=3,
        max_floor=7,
        run_context=context,
    )
    context["scenario"] = "mutated"
    factory()

    assert env_kwargs == [
        {
            "character": "Ironclad",
            "ascension": 10,
            "seed_prefix": "w3",
            "max_floor": 7,
            "native_save_path": None,
            "run_context": {
                "checkpoint": "source.zip",
                "evaluation_mode": "training",
                "scenario": "full_run",
                **VERSION_FIELDS,
            },
        }
    ]
    assert env_kwargs[0]["run_context"] is not context


def test_make_vec_env_isolates_snapshot_and_fresh_worker_scenarios(monkeypatch):
    import agent.train as train

    calls = []

    def fake_make_env(character, ascension, worker_id=0, max_floor=0,
                      native_save_path=None, run_context=None):
        calls.append(
            {
                "native_save_path": native_save_path,
                "run_context": run_context,
            }
        )
        return lambda: None

    monkeypatch.setattr(train, "make_env", fake_make_env)
    monkeypatch.setattr(train, "SubprocVecEnv", lambda makers: makers)
    base_context = {
        "checkpoint": "source.zip",
        "evaluation_mode": "training",
        "scenario": "full_run",
        **VERSION_FIELDS,
    }

    _make_vec_env(
        "Ironclad",
        10,
        2,
        0,
        load_saves=["floor7.save"],
        mix_save_envs=1,
        run_context=base_context,
    )

    assert calls[0]["native_save_path"] == "floor7.save"
    assert calls[0]["run_context"]["scenario"] == "native_save"
    assert calls[1]["native_save_path"] is None
    assert calls[1]["run_context"]["scenario"] == "full_run"
    assert calls[0]["run_context"] is not calls[1]["run_context"]
    assert base_context["scenario"] == "full_run"


def test_training_eval_passes_versioned_fixed_run_context(monkeypatch):
    import agent.train as train

    env_kwargs = []

    class FakeEnv:
        def __init__(self, **kwargs):
            env_kwargs.append(kwargs)

        def reset(self):
            return [0.0], {}

        def action_masks(self):
            return [True]

        def step(self, action):
            return [0.0], 0.0, True, False, {"floor": 7}

        def close(self):
            pass

    class FakeModel:
        def predict(self, obs, **kwargs):
            return 0, None

    monkeypatch.setattr(train, "CombatEnv", FakeEnv)
    monkeypatch.setattr(train, "ActionMasker", lambda env, mask_fn: env)

    stats = run_eval(
        FakeModel(),
        "Ironclad",
        n_games=1,
        ascension=10,
        checkpoint="ppo_ironclad_25k.zip",
        **VERSION_FIELDS,
    )

    assert stats["n"] == 1
    assert env_kwargs == [
        {
            "character": "Ironclad",
            "ascension": 10,
            "seed": "eval_fixed_0",
            "seed_prefix": "eval_0",
            "max_floor": 0,
            "run_context": {
                "checkpoint": "ppo_ironclad_25k.zip",
                "evaluation_mode": "fixed",
                "scenario": "full_run",
                "game_version": "v0.103.2",
                "game_version_source": "cli",
            },
        }
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"game_version": None},
        {"game_version": ""},
        {"game_version": 103},
        {"game_version_source": None},
        {"game_version_source": "auto"},
        {"game_version_source": 1},
        {"ascension": True},
        {"ascension": -1},
        {"ascension": 11},
        {"ascension": 1.0},
        {"ascension": "1"},
    ],
)
def test_training_eval_rejects_invalid_metadata_before_creating_an_env(
    monkeypatch, overrides
):
    import agent.train as train

    monkeypatch.setattr(
        train,
        "CombatEnv",
        lambda **kwargs: pytest.fail("CombatEnv must not be created"),
    )
    kwargs = {
        "ascension": 0,
        "checkpoint": "model.zip",
        **VERSION_FIELDS,
        **overrides,
    }

    with pytest.raises(ValueError):
        run_eval(object(), "Ironclad", n_games=1, **kwargs)


def test_training_launch_requires_version_before_env_or_model(monkeypatch, capsys):
    import agent.train as train

    monkeypatch.delenv("STS2_GAME_VERSION", raising=False)
    monkeypatch.setattr(train.sys, "argv", ["train.py", "--steps", "1"])
    monkeypatch.setattr(
        train,
        "_make_vec_env",
        lambda *args, **kwargs: pytest.fail("env must not be created"),
    )
    monkeypatch.setattr(
        train.MaskablePPO,
        "load",
        lambda *args, **kwargs: pytest.fail("model must not be loaded"),
    )

    with pytest.raises(SystemExit):
        train.main()

    error = capsys.readouterr().err
    assert "--game-version" in error
    assert "STS2_GAME_VERSION" in error


def test_training_launch_rejects_bad_ascension_before_env_or_model(
    monkeypatch, capsys
):
    import agent.train as train

    monkeypatch.setattr(
        train.sys,
        "argv",
        [
            "train.py",
            "--steps",
            "1",
            "--game-version",
            "v0.103.2",
            "--ascension",
            "11",
        ],
    )
    monkeypatch.setattr(
        train,
        "_make_vec_env",
        lambda *args, **kwargs: pytest.fail("env must not be created"),
    )
    monkeypatch.setattr(
        train.MaskablePPO,
        "load",
        lambda *args, **kwargs: pytest.fail("model must not be loaded"),
    )

    with pytest.raises(SystemExit):
        train.main()

    assert "ascension must be an integer in the range 0..10" in capsys.readouterr().err
