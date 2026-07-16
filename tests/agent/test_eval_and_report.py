import pytest

from eval_and_report import _build_parser


def test_report_cli_requires_explicit_checkpoint_and_defaults_to_fixed_seeds():
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    args = parser.parse_args(["checkpoints/model.zip"])
    assert args.checkpoint == "checkpoints/model.zip"
    assert args.fixed_seeds is True
    assert args.n_games == 10
