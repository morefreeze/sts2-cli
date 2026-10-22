# tools/build_advisor_corpus.py
"""Build tests/fixtures/advisor/ooc_corpus.jsonl.gz -- real out-of-combat
decision states from a kept run_batch, each with the action greedy_action
takes on it now. Those actions are the golden the greedy trace work must keep.

greedy has hidden per-process state (card_scoring._MC_CONTEXT, rewritten by
every card_reward), so each game's states are replayed in order and that state
is reset at every game start -- the tests replay the same way.

It also writes ooc_corpus.meta.json next to the corpus: the batch it was built
from and the sha256 of every local data file greedy loads (GREEDY_DATA_FILES),
so the golden test can tell "a data file changed" from "greedy changed".

    .venv/bin/python tools/build_advisor_corpus.py \\
        --batch logs/batches/p3b-route-ab-20261012-160225
"""
import argparse
import glob
import gzip
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.greedy_trace import (  # noqa: E402
    GREEDY_BEHAVIOR_ENV, greedy_data_sha256, reset_greedy_context)

CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")
ADVISED = frozenset({"card_reward", "rest_site", "event_choice", "bundle_select",
                     "card_select", "shop", "map_select"})
DEFAULT_OUT = os.path.join(ROOT, "tests", "fixtures", "advisor", "ooc_corpus.jsonl.gz")


def meta_path(out: str) -> str:
    """ooc_corpus.jsonl.gz -> ooc_corpus.meta.json"""
    base = out[: -len(".jsonl.gz")] if out.endswith(".jsonl.gz") else out
    return base + ".meta.json"


def _seed_number(path: str) -> int:
    return int(re.search(r"_(\d+)\.jsonl$", path).group(1))


def game_states(path: str):
    """Advised decision states of one game log, in play order."""
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            data = row.get("data")
            if (row.get("type") == "state" and isinstance(data, dict)
                    and data.get("type") == "decision"
                    and data.get("decision") in ADVISED):
                yield data


def lane_files(batch: str, character: str) -> list:
    pattern = os.path.join(batch, "games", f"{character}__random", "*.jsonl")
    return sorted(glob.glob(pattern), key=_seed_number)


def select_games(batch: str, per_character: int, bundle_games: int) -> list:
    picked = []
    for ch in CHARACTERS:
        picked += lane_files(batch, ch)[:per_character]
    # bundle_select is rare (15 in the whole p3b random arm): add games that have one.
    extra = []
    for ch in CHARACTERS:
        for path in lane_files(batch, ch):
            if len(extra) >= bundle_games:
                break
            if path not in picked and any(
                    s["decision"] == "bundle_select" for s in game_states(path)):
                extra.append(path)
    return picked + extra


def build_rows(files: list, greedy) -> list:
    rows = []
    for path in files:
        reset_greedy_context()
        game = os.path.basename(path)[: -len(".jsonl")]
        for seq, state in enumerate(game_states(path)):
            rows.append({"game": game, "seq": seq, "state": state,
                         "expected": greedy(state)})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", required=True, help="logs/batches/<id> with games/<Char>__random/")
    ap.add_argument("--per-character", type=int, default=2)
    ap.add_argument("--bundle-games", type=int, default=3)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    set_knobs = [k for k in GREEDY_BEHAVIOR_ENV if (os.environ.get(k) or "").strip()]
    if set_knobs:
        print(f"refusing: greedy behaviour env set: {set_knobs}", file=sys.stderr)
        return 2

    from agent.combat_env import greedy_action
    files = select_games(args.batch, args.per_character, args.bundle_games)
    rows = build_rows(files, greedy_action)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with gzip.open(args.out, "wt") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    meta = {"batch": args.batch, "data_sha256": greedy_data_sha256()}
    with open(meta_path(args.out), "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)
        f.write("\n")
    counts = {}
    for row in rows:
        counts[row["state"]["decision"]] = counts.get(row["state"]["decision"], 0) + 1
    print(f"{len(files)} games, {len(rows)} states -> {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
