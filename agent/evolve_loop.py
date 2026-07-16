#!/usr/bin/env python3
"""evolve_loop.py — Evolutionary training loop with clutch-sentinel selection.

Methodology discovered Jun 10: PPO fine-tuning randomly destroys the HP=72
boss-clutch within 25-75k steps (gradient drift, not entropy). But some short
runs survive AND gain boss-reach (13255k→13308k: reach 13.3%→16.7%, clutch
intact). So: evolutionary hill climbing —

    round:
      1. train CHUNK_STEPS from current best (fresh random seeds each round)
      2. sentinel: hp-sweep clutch check (HP=72 must win ≥ THRESHOLD/15)
      3. quick reach eval (n=N_EVAL games, count floor-17 reaches)
      4. survivor (clutch pass + reach ≥ best) → promote to best
      5. repeat

Run:  .venv/bin/python -m agent.evolve_loop --rounds 12
Stop: touch /tmp/sts2-cli/evolve_stop
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(REPO, ".venv", "bin", "python")
ENV = {
    **os.environ,
    "PATH": os.path.expanduser("~/.dotnet-arm64") + ":" + os.environ.get("PATH", ""),
    "DOTNET_ROOT": os.path.expanduser("~/.dotnet-arm64"),
    "STS2_MC_ROLLOUT": "smart",
}
SNAPSHOT = os.path.join(
    REPO, "data", "boss_snapshots_13186k", "eval_r0e093a_65_fl17_hp80.save")
STOP_FILE = "/tmp/sts2-cli/evolve_stop"
STATE_FILE = os.path.join(REPO, "checkpoints_evolve", "evolve_state.json")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list[str], timeout: int, log_path: str) -> int:
    with open(log_path, "w") as f:
        try:
            p = subprocess.run(cmd, cwd=REPO, env=ENV, stdout=f,
                               stderr=subprocess.STDOUT, timeout=timeout)
            return p.returncode
        except subprocess.TimeoutExpired:
            return -9


def kill_orphans() -> None:
    subprocess.run(["pkill", "-9", "-f", "Sts2Headless"], capture_output=True)
    time.sleep(2)


def latest_ckpt(dirpath: str) -> str | None:
    zips = [f for f in os.listdir(dirpath) if f.endswith(".zip")] if os.path.isdir(dirpath) else []
    if not zips:
        return None
    def steps(name):
        m = re.search(r"_(\d+)k", name)
        return int(m.group(1)) if m else 0
    return os.path.join(dirpath, max(zips, key=steps))


def train_chunk(base_ckpt: str, out_dir: str, steps: int, ent: float,
                round_idx: int) -> str | None:
    """Train one chunk; returns path of the newest ckpt or None."""
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    log(f"  train: {steps} steps from {os.path.basename(base_ckpt)} (ent={ent})")
    rc = run([PY, "-m", "agent.train", "--character", "Ironclad",
              "--steps", str(steps), "--checkpoint", base_ckpt,
              "--ent-coef", str(ent), "--save-dir", out_dir,
              "--eval-freq", "0"],
             timeout=3600, log_path=f"/tmp/sts2-cli/evolve_r{round_idx}_train.log")
    kill_orphans()
    ck = latest_ckpt(out_dir)
    if rc != 0 and ck is None:
        log(f"  train FAILED rc={rc}")
        return None
    return ck


def sentinel_clutch(ckpt: str, round_idx: int, threshold: int = 12) -> bool:
    """HP=72 deterministic sweep; must win ≥ threshold of 15."""
    log_path = f"/tmp/sts2-cli/evolve_r{round_idx}_sentinel.log"
    rc = run([PY, "-m", "agent.boss_retry", ckpt, SNAPSHOT,
              "--hp-sweep", "72", "--n-deterministic", "15",
              "--n-stochastic", "0"],
             timeout=1800, log_path=log_path)
    kill_orphans()
    try:
        with open(log_path) as f:
            text = f.read()
        m = re.search(r"hp=\s*72.*?win\s+(\d+)/15", text)
        wins = int(m.group(1)) if m else 0
    except Exception:
        wins = 0
    log(f"  sentinel: HP=72 clutch {wins}/15 ({'PASS' if wins >= threshold else 'FAIL'})")
    return wins >= threshold


def _floor_token_to_int(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    m = re.fullmatch(r"A(\d+)F(\d+)", token, flags=re.IGNORECASE)
    if not m:
        return None
    act = int(m.group(1))
    floor = int(m.group(2))
    if act <= 0 or floor <= 0:
        return None
    return (act - 1) * 17 + floor


def parse_eval_reach(text: str) -> tuple[float, int]:
    avg_floor, reach = 0.0, 0
    m = re.search(r"avg_floor\s*:\s*([\d.]+)", text)
    if m:
        avg_floor = float(m.group(1))
    md = re.search(r"floor dist\s*:\s*\[([^\]]*)\]", text)
    if md:
        floors = []
        for token in md.group(1).split(","):
            floor = _floor_token_to_int(token)
            if floor is not None:
                floors.append(floor)
        reach = sum(1 for f_ in floors if f_ >= 17)
    return avg_floor, reach


def eval_reach(ckpt: str, n_games: int, round_idx: int) -> tuple[float, int]:
    """Quick eval; returns (avg_floor, boss_reach_count)."""
    log_path = f"/tmp/sts2-cli/evolve_r{round_idx}_eval.log"
    rc = run([PY, "-m", "agent.eval_rl", ckpt, "--n-games", str(n_games),
              "--character", "Ironclad"],
             timeout=3600, log_path=log_path)
    kill_orphans()
    avg_floor, reach = 0.0, 0
    try:
        with open(log_path) as f:
            text = f.read()
        avg_floor, reach = parse_eval_reach(text)
    except Exception:
        pass
    log(f"  eval: avg_floor={avg_floor:.1f} reach={reach}/{n_games}")
    return avg_floor, reach


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rounds", type=int, default=12)
    p.add_argument("--chunk-steps", type=int, default=15000)
    p.add_argument("--ent", type=float, default=0.08)
    p.add_argument("--n-eval", type=int, default=15)
    p.add_argument("--best", default=os.path.join(
        REPO, "checkpoints_best", "ppo_ironclad_13308k_RETRAIN_23pct.zip"))
    p.add_argument("--best-reach", type=int, default=2,
                   help="current best reach count over --n-eval games "
                        "(13308k = 16.7%% → ~2.5/15)")
    args = p.parse_args()

    os.makedirs("/tmp/sts2-cli", exist_ok=True)
    os.makedirs(os.path.join(REPO, "checkpoints_evolve"), exist_ok=True)

    best = args.best
    best_reach = args.best_reach
    history = []

    log(f"evolve start: best={os.path.basename(best)} best_reach={best_reach}/{args.n_eval}")
    for r in range(1, args.rounds + 1):
        if os.path.exists(STOP_FILE):
            log("stop file found — exiting")
            break
        log(f"=== round {r}/{args.rounds} ===")
        work = os.path.join(REPO, "checkpoints_evolve", f"round_{r}")
        ck = train_chunk(best, work, args.chunk_steps, args.ent, r)
        if ck is None:
            history.append({"round": r, "result": "train_failed"})
            continue
        if not sentinel_clutch(ck, r):
            history.append({"round": r, "ckpt": ck, "result": "clutch_fail"})
            continue
        avg_floor, reach = eval_reach(ck, args.n_eval, r)
        rec = {"round": r, "ckpt": ck, "result": "clutch_pass",
               "avg_floor": avg_floor, "reach": reach}
        if reach > best_reach:
            promoted = os.path.join(
                REPO, "checkpoints_best",
                f"evolve_r{r}_reach{reach}of{args.n_eval}.zip")
            shutil.copy(ck, promoted)
            best = promoted
            best_reach = reach
            rec["result"] = "PROMOTED"
            log(f"  *** PROMOTED: reach {reach}/{args.n_eval} → new best {os.path.basename(promoted)}")
        history.append(rec)
        with open(STATE_FILE, "w") as f:
            json.dump({"best": best, "best_reach": best_reach,
                       "history": history}, f, indent=1)

    log("evolve done")
    log(f"final best: {best} (reach {best_reach}/{args.n_eval})")
    for h in history:
        log(f"  {h}")


if __name__ == "__main__":
    main()
