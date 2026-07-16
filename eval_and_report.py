#!/usr/bin/env python3
"""eval_and_report.py - Evaluate latest RL checkpoint and generate report.

This script:
1. Finds the latest checkpoint in checkpoints/
2. Evaluates it with 10 games
3. Generates a formatted report
4. Output is auto-delivered to Telegram by the cron system
"""
import glob, os, re, subprocess, sys
from datetime import datetime

def latest_checkpoint(checkpoints_dir: str = "checkpoints") -> str:
    """Find the latest checkpoint by step count."""
    zips = glob.glob(os.path.join(checkpoints_dir, "ppo_ironclad_*.zip"))
    if not zips:
        raise FileNotFoundError(f"No checkpoints found in {checkpoints_dir}/")
    def _steps(p):
        m = re.search(r"_(\d+)k\.zip$", p)
        return int(m.group(1)) if m else 0
    return max(zips, key=_steps)

def extract_steps(checkpoint_path: str) -> int:
    """Extract step count from checkpoint filename."""
    m = re.search(r"_(\d+)k\.zip$", checkpoint_path)
    return int(m.group(1)) if m else 0

def run_eval(checkpoint: str, n_games: int = 10) -> dict:
    """Run evaluation using eval_rl.py and parse output."""
    # Import eval_rl module directly instead of subprocess
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.eval_rl import run_eval_verbose, _latest_checkpoint
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
        fixed_seeds=False,
        verbose=False
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
    report.append(f"📊 **Performance ({stats['n']} games)**:")
    report.append(f"  • **Win Rate**: {stats['win_rate']:.1%}")
    report.append(f"  • **Avg Floor**: {stats['avg_floor']:.1f}")
    report.append(f"  • **Max Floor**: {stats['max_floor']}")
    report.append(f"  • **Avg Combat Wins**: {stats['avg_combat_wins']:.1f}")
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

def main():
    checkpoint = latest_checkpoint()

    # Check if we should skip (same checkpoint as last run)
    last_eval_file = "/tmp/sts2-cli/last_eval_checkpoint.txt"
    os.makedirs(os.path.dirname(last_eval_file), exist_ok=True)

    try:
        with open(last_eval_file) as f:
            last_checkpoint = f.read().strip()
        if os.path.basename(checkpoint) == last_checkpoint:
            print(f"[SILENT]")
            return
    except FileNotFoundError:
        pass

    # Run evaluation
    stats = run_eval(checkpoint, n_games=10)

    # Format and print report
    report = format_report(stats, checkpoint)
    print("\n" + report)

    # Save checkpoint as last evaluated
    with open(last_eval_file, "w") as f:
        f.write(os.path.basename(checkpoint))

if __name__ == "__main__":
    main()
