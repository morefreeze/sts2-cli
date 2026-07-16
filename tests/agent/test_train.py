import pytest
from types import SimpleNamespace

from agent.train import (
    _apply_training_profile,
    _parse_hp_curriculum_values,
    _snapshot_curriculum_phase,
    _validate_training_profile,
)


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
