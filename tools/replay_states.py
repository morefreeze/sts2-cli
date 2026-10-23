# tools/replay_states.py
"""Replay recorded game logs through the headless engine; save every reply.

The golden for refactors that must not change engine output (Sts2Advisor Part B):
replay the same logs with the old and the new build, then --compare the two
output directories. plan_combat_turn is skipped -- it only searches (never
changes the game) and its plans are not deterministic.

    .venv/bin/python tools/replay_states.py LOG.jsonl... --out DIR
    .venv/bin/python tools/replay_states.py --compare DIR_A DIR_B
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from engine_process import EngineHang, EngineProcess  # noqa: E402

REPLY_TIMEOUT_S = 120


def recorded_commands(path: str):
    """Commands the original run sent, in order, minus plan_combat_turn and quit."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            cmd = row.get("data")
            if row.get("type") != "action" or not isinstance(cmd, dict):
                continue
            if cmd.get("action") == "plan_combat_turn" or cmd.get("cmd") == "quit":
                continue
            yield cmd


def _shutdown(engine) -> None:
    """Stop the engine for good: a failed (or interrupted) close() falls back to kill()."""
    try:
        engine.close()
    except Exception:
        engine.kill()
    except BaseException:  # KeyboardInterrupt mid-close: still don't leave the engine running
        engine.kill()
        raise


def replay(path: str, out_path: str, argv=None) -> int:
    """Send every recorded command; write {"n", "cmd", "reply"} lines. Returns replies written."""
    if argv is None:
        import play_full_run  # sets the STS2_GAME_DIR default, like every harness run
        argv = play_full_run.engine_argv()
    engine = EngineProcess(argv, stderr=subprocess.DEVNULL)
    n = 0
    try:
        ready = engine.read_json(REPLY_TIMEOUT_S)
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected first reply: {ready}")
        with open(out_path, "w", encoding="utf-8") as out:
            for cmd in recorded_commands(path):
                engine.write(cmd)
                try:
                    reply = engine.read_json(REPLY_TIMEOUT_S)
                except EngineHang:
                    out.write(json.dumps({"n": n, "cmd": cmd, "hang": True}) + "\n")
                    break
                out.write(json.dumps({"n": n, "cmd": cmd, "reply": reply},
                                     ensure_ascii=False) + "\n")
                n += 1
    finally:
        _shutdown(engine)
    return n


def compare(dir_a: str, dir_b: str) -> list:
    """Differences between two replay directories; [] means identical."""
    diffs = []
    names = sorted(set(os.listdir(dir_a)) | set(os.listdir(dir_b)))
    for name in names:
        pa, pb = os.path.join(dir_a, name), os.path.join(dir_b, name)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            diffs.append(f"{name}: only in one directory")
            continue
        with open(pa, encoding="utf-8") as fa:
            la = fa.read().splitlines()
        with open(pb, encoding="utf-8") as fb:
            lb = fb.read().splitlines()
        for i, (x, y) in enumerate(zip(la, lb)):
            if x != y:
                diffs.append(f"{name}: first difference at reply {i}")
                break
        else:
            if len(la) != len(lb):
                diffs.append(f"{name}: {len(la)} vs {len(lb)} replies")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"))
    args = ap.parse_args(argv)
    if args.compare:
        diffs = compare(*args.compare)
        print("\n".join(diffs) if diffs else "identical")
        return 1 if diffs else 0
    if not args.logs or not args.out:
        ap.error("give LOG files and --out, or --compare DIR_A DIR_B")
    os.makedirs(args.out, exist_ok=True)
    for log in args.logs:
        out = os.path.join(args.out, os.path.basename(log))
        n = replay(log, out)
        print(f"{os.path.basename(log)}: {n} replies", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
