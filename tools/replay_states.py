# tools/replay_states.py
"""Replay recorded game logs through the headless engine; save every reply.

The golden for refactors that must not change engine output (Sts2Advisor Part B):
replay the same logs with the old and the new build, then --compare the two
output directories. plan_combat_turn is skipped -- it only searches (never
changes the game) and its plans are not deterministic. A dump that ends in an
engine hang is a truncated golden, so --compare fails on it even when both
builds hang at the same reply.

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

READY_TIMEOUT_S = 120   # engine start-up (first reply)
REPLY_TIMEOUT_S = 120   # per command


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
        ready = engine.read_json(READY_TIMEOUT_S)
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected first reply: {ready}")
        with open(out_path, "w", encoding="utf-8") as out:
            for cmd in recorded_commands(path):
                engine.write(cmd)
                try:
                    reply = engine.read_json(REPLY_TIMEOUT_S)
                except EngineHang:
                    out.write(json.dumps({"n": n, "cmd": cmd, "hang": True},
                                         ensure_ascii=False) + "\n")
                    break
                out.write(json.dumps({"n": n, "cmd": cmd, "reply": reply},
                                     ensure_ascii=False) + "\n")
                n += 1
    finally:
        _shutdown(engine)
    return n


def _read_lines(path: str) -> list:
    """The file's lines, split on "\\n" only.

    str.splitlines() would also cut at U+0085 / U+2028 / U+2029, which are legal raw
    characters inside a JSON string, and shift every later reply index.
    """
    with open(path, encoding="utf-8", newline="") as f:
        lines = f.read().split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _first_hang(lines: list):
    """Reply index of the first hang row (written by replay()), else None."""
    for i, line in enumerate(lines):
        if '"hang": true' not in line:  # cheap filter before parsing
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("hang") is True:
            n = row.get("n")
            return n if isinstance(n, int) else i
    return None


def _row(line: str) -> dict:
    try:
        row = json.loads(line)
    except ValueError:
        return {}
    return row if isinstance(row, dict) else {}


def _cmd_label(row: dict):
    cmd = row.get("cmd")
    if not isinstance(cmd, dict) or not cmd.get("cmd"):
        return None
    return f"{cmd['cmd']}/{cmd['action']}" if cmd.get("action") else str(cmd["cmd"])


def _decision_label(row: dict):
    if row.get("hang"):
        return "hang"
    reply = row.get("reply")
    if not isinstance(reply, dict):
        return None
    return reply.get("decision") or reply.get("type")


def _describe_difference(line_a: str, line_b: str) -> str:
    """" (cmd ...; decision A vs B)" naming what the first differing reply was, or "" if unknown."""
    ra, rb = _row(line_a), _row(line_b)
    parts = []
    cmd_a, cmd_b = _cmd_label(ra), _cmd_label(rb)
    if cmd_a or cmd_b:
        parts.append("cmd " + (cmd_a if cmd_a == cmd_b else f"{cmd_a or '?'} vs {cmd_b or '?'}"))
    dec_a, dec_b = _decision_label(ra), _decision_label(rb)
    if dec_a or dec_b:
        parts.append(f"decision {dec_a or '?'} vs {dec_b or '?'}")
    return f" ({'; '.join(parts)})" if parts else ""


def compare_with_counts(dir_a: str, dir_b: str):
    """(differences, files compared, replies in those files); no differences means identical.

    A file holding a hang row (either directory) is always reported: the dump was cut short
    there, so equal dumps would still mean the golden is incomplete.
    """
    diffs = []
    files = replies = 0
    names = sorted(set(os.listdir(dir_a)) | set(os.listdir(dir_b)))
    for name in names:
        pa, pb = os.path.join(dir_a, name), os.path.join(dir_b, name)
        la = _read_lines(pa) if os.path.exists(pa) else None
        lb = _read_lines(pb) if os.path.exists(pb) else None
        if la is None or lb is None:
            diffs.append(f"{name}: only in one directory")
        else:
            files += 1
            replies += len(la)
            for i, (x, y) in enumerate(zip(la, lb)):
                if x != y:
                    diffs.append(f"{name}: first difference at reply {i}{_describe_difference(x, y)}")
                    break
            else:
                if len(la) != len(lb):
                    diffs.append(f"{name}: {len(la)} vs {len(lb)} replies")
        hangs = [h for h in (_first_hang(lines) for lines in (la, lb) if lines is not None)
                 if h is not None]
        if hangs:
            diffs.append(f"{name}: engine hang at reply {min(hangs)} (golden truncated)")
    return diffs, files, replies


def compare(dir_a: str, dir_b: str) -> list:
    """Differences between two replay directories; [] means identical."""
    return compare_with_counts(dir_a, dir_b)[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"))
    args = ap.parse_args(argv)
    if args.compare:
        diffs, files, replies = compare_with_counts(*args.compare)
        if diffs:
            print("\n".join(diffs))
            return 1
        print(f"identical ({files} files, {replies} replies)")
        return 0
    if not args.logs or not args.out:
        ap.error("give LOG files and --out, or --compare DIR_A DIR_B")
    seen = {}
    for log in args.logs:  # dumps are named after the log, so equal basenames would overwrite each other
        key = os.path.basename(log).casefold()
        if key in seen:
            print(f"error: {seen[key]} and {log} share the basename {os.path.basename(log)!r}; "
                  "their dumps would overwrite each other -- replay them into separate --out dirs",
                  file=sys.stderr)
            return 2
        seen[key] = log
    os.makedirs(args.out, exist_ok=True)
    for log in args.logs:
        out = os.path.join(args.out, os.path.basename(log))
        n = replay(log, out)
        print(f"{os.path.basename(log)}: {n} replies", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
