#!/usr/bin/env python3
"""Replay executed Fool reward observations; performs no game/API actions.

Example (match the live advisor's environment explicitly):
  STS2_CARD_QUALITY_GATE=0 .venv/bin/python tools/eval_fool_drafting.py \
    --policy-root /path/to/checkout --input saved-trace.jsonl --output replay.jsonl

Comparing action changes is NOT an outcome evaluation. Solver labels / full
paired runs are needed before interpreting changes as win-rate improvements.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--input', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ascension', type=int, default=9)
    args = parser.parse_args()
    root = args.policy_root.resolve()
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path[:0] = [str(root), str(root / 'python')]
    from agent.combat_env import greedy_action
    from agent.greedy_trace import (GreedyTrace, reset_greedy_context,
                                    GREEDY_BEHAVIOR_ENV, greedy_data_sha256)
    rows, seen = [], set()
    for path in args.input:
        with path.open() as source:
            for line in source:
                row = json.loads(line)
                run = row.get('run') or {}
                if (run.get('character') != 'FOOL'
                        or run.get('ascension') != args.ascension
                        or row.get('decision') != 'card_reward'
                        or not (row.get('autopilot') or {}).get('executed')):
                    continue
                identity = json.dumps([run, row['state']], sort_keys=True)
                if identity in seen:
                    continue
                seen.add(identity)
                reset_greedy_context()
                trace = GreedyTrace()
                action = greedy_action(row['state'], trace=trace)
                rows.append({'seed': run['seed'], 'ts': row['ts'],
                             'state_sha256': hashlib.sha256(identity.encode()).hexdigest(),
                             'state': row['state'], 'logged': row['reply']['action'],
                             'action': action, **trace.to_dict()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w') as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + '\n')
    metadata = {
        'policy_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'policy_dirty': bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True).strip()),
        'inputs': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.input},
        'data_sha256': greedy_data_sha256(str(root)),
        'behavior_environment': {k: os.environ[k] for k in GREEDY_BEHAVIOR_ENV if k in os.environ},
        'decisions': len(rows), 'runs': len({r['seed'] for r in rows}),
        'changed_from_logged': sum(r['action'] != r['logged'] for r in rows),
        'outcomes_evaluated': False,
    }
    args.output.with_suffix('.meta.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print(json.dumps({k: metadata[k] for k in ('decisions','runs','changed_from_logged','outcomes_evaluated')}))


if __name__ == '__main__':
    main()
