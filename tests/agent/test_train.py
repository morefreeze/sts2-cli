import pytest

from agent.train import _parse_hp_curriculum_values, _snapshot_curriculum_phase


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
