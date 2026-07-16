import pytest

from eval_and_report import _build_parser, _evaluation_key


def test_report_cli_requires_explicit_checkpoint_and_defaults_to_fixed_seeds():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    args = parser.parse_args(["checkpoints/model.zip"])
    assert args.checkpoint == "checkpoints/model.zip"
    assert args.fixed_seeds is True
    assert args.n_games == 10


def test_evaluation_key_distinguishes_path_and_seed_configuration(tmp_path):
    first = tmp_path / "first" / "ppo_ironclad_100k.zip"
    second = tmp_path / "second" / "ppo_ironclad_100k.zip"

    fixed_key = _evaluation_key(
        str(first), n_games=10, fixed_seeds=True, invalid_retries=1)

    assert fixed_key != _evaluation_key(
        str(second), n_games=10, fixed_seeds=True, invalid_retries=1)
    assert fixed_key != _evaluation_key(
        str(first), n_games=10, fixed_seeds=False, invalid_retries=1)
    assert fixed_key != _evaluation_key(
        str(first), n_games=20, fixed_seeds=True, invalid_retries=1)
