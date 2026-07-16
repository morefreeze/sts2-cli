import pytest

from agent.train import _parse_hp_curriculum_values


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
