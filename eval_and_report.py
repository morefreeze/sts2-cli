#!/usr/bin/env python3
"""Evaluate an explicit RL checkpoint and generate a reproducible report.

This script:
1. Requires the checkpoint path from the caller
2. Evaluates it with a fixed seed set by default
3. Generates a formatted report
4. Output is auto-delivered to Telegram by the cron system
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

from agent.run_metadata import resolve_game_version, validate_ascension


def extract_steps(checkpoint_path: str) -> int:
    """Extract step count from checkpoint filename."""
    m = re.search(r"_(\d+)k\.zip$", checkpoint_path)
    return int(m.group(1)) if m else 0


def run_eval(checkpoint: str, n_games: int = 10,
             fixed_seeds: bool = True, invalid_retries: int = 1, *,
             ascension: int = 0, game_version: str = None,
             game_version_source: str = None) -> dict:
    """Run evaluation using eval_rl.py and parse output."""
    if type(game_version) is not str or not game_version.strip():
        raise ValueError("game_version must be a non-empty string")
    game_version = game_version.strip()
    if type(game_version_source) is not str or game_version_source not in {
        "cli",
        "environment",
    }:
        raise ValueError("game_version_source must be 'cli' or 'environment'")
    ascension = validate_ascension(ascension)

    # Import eval_rl module directly instead of subprocess
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.eval_rl import run_eval_verbose
    from sb3_contrib import MaskablePPO
    import torch

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = MaskablePPO.load(checkpoint, device=device)

    print(f"📊 STS2 RL Training Evaluation Report")
    print(f"{'='*50}")
    print(f"Checkpoint: {os.path.basename(checkpoint)}")
    print(f"Timestamp : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Device    : {device}")
    print(f"Games     : {n_games}")
    print(f"{'='*50}\n")

    stats = run_eval_verbose(
        model,
        character="Ironclad",
        n_games=n_games,
        fixed_seeds=fixed_seeds,
        invalid_retries=invalid_retries,
        verbose=False,
        checkpoint_name=checkpoint,
        ascension=ascension,
        game_version=game_version,
        game_version_source=game_version_source,
    )
    return stats


def format_report(stats: dict, checkpoint: str) -> str:
    """Format evaluation results into a readable report."""
    steps = extract_steps(checkpoint)

    report = []
    report.append(f"🎮 **STS2 RL Checkpoint Evaluation**")
    report.append(f"")
    report.append(f"📁 **Checkpoint**: `ppo_ironclad_{steps}k.zip`")
    report.append(f"📈 **Training Steps**: {steps}k ({steps*1000:,})")
    report.append(f"🕐 **Eval Time**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"")
    report.append(
        f"📊 **Performance ({stats['valid_n']}/{stats['requested_n']} valid games)**:"
    )
    report.append(f"  • **Win Rate**: {stats['win_rate']:.1%}")
    report.append(f"  • **Avg Floor**: {stats['avg_floor']:.1f}")
    report.append(f"  • **Max Floor**: {stats['max_floor']}")
    report.append(f"  • **Avg Combat Wins**: {stats['avg_combat_wins']:.1f}")
    report.append(f"  • **Invalid Seeds**: {stats['invalid_n']}")
    report.append(f"  • **Invalid Attempts**: {stats['invalid_attempts']}")
    report.append(f"  • **Attempts**: {stats['attempts']}")
    report.append(f"")
    report.append(f"📊 **Floor Distribution**: {sorted(stats['floors'])}")

    # Add trend indicator
    win_rate = stats['win_rate']
    if win_rate >= 0.7:
        emoji = "🚀"
        comment = "Excellent!"
    elif win_rate >= 0.5:
        emoji = "✅"
        comment = "Good progress"
    elif win_rate >= 0.3:
        emoji = "📈"
        comment = "Improving"
    else:
        emoji = "⚠️"
        comment = "Needs more training"

    report.append(f"")
    report.append(f"{emoji} {comment}")

    return "\n".join(report)


def _evaluation_key(checkpoint: str, *, n_games: int, fixed_seeds: bool,
                    invalid_retries: int, game_version: str,
                    ascension: int) -> str:
    return json.dumps({
        "checkpoint": os.path.abspath(checkpoint),
        "n_games": n_games,
        "fixed_seeds": fixed_seeds,
        "invalid_retries": invalid_retries,
        "game_version": game_version,
        "ascension": ascension,
    }, sort_keys=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", help="Explicit path to checkpoint zip")
    parser.add_argument("--n-games", type=int, default=10)
    parser.add_argument("--invalid-retries", type=int, default=1)
    parser.add_argument("--game-version", default=None)
    parser.add_argument("--ascension", type=int, default=0)
    parser.add_argument("--random-seeds", dest="fixed_seeds", action="store_false",
                        help="Use random seeds instead of the fixed comparison set")
    parser.set_defaults(fixed_seeds=True)
    return parser


def _resolve_launch_metadata(args):
    return resolve_game_version(args.game_version), validate_ascension(args.ascension)


def main():
    parser = _build_parser()
    args = parser.parse_args()
    try:
        resolved_game_version, ascension = _resolve_launch_metadata(args)
    except ValueError as exc:
        parser.error(str(exc))
    checkpoint = args.checkpoint
    evaluation_key = _evaluation_key(
        checkpoint,
        n_games=args.n_games,
        fixed_seeds=args.fixed_seeds,
        invalid_retries=args.invalid_retries,
        game_version=resolved_game_version.value,
        ascension=ascension,
    )

    # Check if we should skip (same checkpoint as last run)
    last_eval_file = "/tmp/sts2-cli/last_eval_checkpoint.txt"
    os.makedirs(os.path.dirname(last_eval_file), exist_ok=True)

    try:
        with open(last_eval_file) as f:
            last_evaluation_key = f.read().strip()
        if evaluation_key == last_evaluation_key:
            print(f"[SILENT]")
            return
    except FileNotFoundError:
        pass

    # Run evaluation
    stats = run_eval(
        checkpoint,
        n_games=args.n_games,
        fixed_seeds=args.fixed_seeds,
        invalid_retries=args.invalid_retries,
        ascension=ascension,
        game_version=resolved_game_version.value,
        game_version_source=resolved_game_version.source,
    )

    # Format and print report
    report = format_report(stats, checkpoint)
    print("\n" + report)

    # Save checkpoint as last evaluated
    with open(last_eval_file, "w") as f:
        f.write(evaluation_key)


if __name__ == "__main__":
    main()
