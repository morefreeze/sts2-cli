"""Opt-in (STS2_FULL_CORPUS=1): every kept batch game, traced vs untraced.

Per state: the traced pick equals the untraced pick, exactly one rule fired,
and the trace serialises under a strict JSON parser (the mod rejects NaN).
Every decision type greedy is advised on (build_advisor_corpus.ADVISED) must
appear in the corpus, so a thin set of kept logs cannot pass vacuously.

~44k states: about 100 s in one process (tests/conftest.py sets
OMP_NUM_THREADS=1; without it OpenMP oversubscription makes a card_reward call
~20x slower on a busy machine), 10-30 s spread over worker processes. Games
are independent (greedy's hidden state is reset at the start of each), so they
run in spawn workers; OMP_NUM_THREADS=1 is exported before the workers start
because the library reads it when it loads.

STS2_FULL_CORPUS_WORKERS sets the worker count: a positive integer, 1 =
in-process, blank/unset = min(4, cpu_count // 2). It is deliberately modest:
every worker is a busy core, and running this beside a regression or A/B batch
slows that batch's time-budgeted solver (it plays worse when starved of CPU).
Prefer a quiet machine, or STS2_FULL_CORPUS_WORKERS=1 under `nice`."""
import collections
import concurrent.futures
import glob
import json
import multiprocessing
import os
import sys
import time

import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GreedyTrace, reset_greedy_context
from tests.agent.advisor_corpus import fresh_greedy_state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Both batch layouts: A/B batches write games/<Character>__<arm>/<file>.jsonl,
# regression batches write games/<file>.jsonl flat.
_BATCH_GAMES = os.path.join(ROOT, "logs", "batches", "*", "games")
GAMES = sorted(set(glob.glob(os.path.join(_BATCH_GAMES, "*", "*.jsonl"))
                   + glob.glob(os.path.join(_BATCH_GAMES, "*.jsonl"))))

sys.path.insert(0, os.path.join(ROOT, "tools"))
from build_advisor_corpus import ADVISED, game_states  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("STS2_FULL_CORPUS") != "1" or not GAMES,
    reason="set STS2_FULL_CORPUS=1 (needs logs/batches/*/games)")


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def _init_worker() -> None:
    fresh_greedy_state()


def _check_game(path: str) -> dict:
    """One game, in play order: untraced pass, then traced pass."""
    states = list(game_states(path))
    mismatches, unserialisable = [], []
    per_decision = collections.Counter()
    reset_greedy_context()
    plain = [greedy_action(s) for s in states]
    reset_greedy_context()
    for seq, (s, want) in enumerate(zip(states, plain)):
        trace = GreedyTrace()
        got = greedy_action(s, trace=trace)
        per_decision[s["decision"]] += 1
        if got != want or len(trace.fired()) != 1:
            mismatches.append((path, seq, s["decision"], want, got, trace.rules[-2:]))
        try:
            json.dumps(trace.to_dict(), allow_nan=False)
        except (TypeError, ValueError) as exc:
            unserialisable.append((path, seq, s["decision"], repr(exc)))
    return {"n": len(states), "per_decision": per_decision,
            "mismatches": mismatches, "unserialisable": unserialisable}


def _workers() -> int:
    raw = (os.environ.get("STS2_FULL_CORPUS_WORKERS") or "").strip()
    if not raw:
        return max(1, min(4, (os.cpu_count() or 2) // 2))
    try:
        workers = int(raw)
    except ValueError:
        workers = 0
    if workers < 1:
        pytest.fail(f"STS2_FULL_CORPUS_WORKERS={raw!r}: expected a positive integer "
                    "(1 = run in-process; blank = default)")
    return workers


def test_trace_never_changes_a_choice_on_any_kept_game(monkeypatch):
    started = time.monotonic()
    workers = _workers()
    if workers == 1:
        results = map(_check_game, GAMES)
        pool = None
    else:
        # spawn, not fork: a forked child inherits the parent's OpenMP runtime state
        monkeypatch.setenv("OMP_NUM_THREADS", "1")
        pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
            initializer=_init_worker)
        results = pool.map(_check_game, GAMES)
    n, per_decision, mismatches, unserialisable = 0, collections.Counter(), [], []
    try:
        for done, res in enumerate(results, 1):  # GAMES order: stable mismatch order
            n += res["n"]
            per_decision.update(res["per_decision"])
            mismatches += res["mismatches"]
            unserialisable += res["unserialisable"]
            if done % 20 == 0:
                print(f"  {done}/{len(GAMES)} games, {n} states, "
                      f"{time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    print(f"\nfull corpus: {len(GAMES)} games, {n} states, {workers} worker(s), "
          f"{time.monotonic() - started:.0f}s; "
          + ", ".join(f"{k}={v}" for k, v in sorted(per_decision.items())))
    assert n > 0
    assert not set(ADVISED) - set(per_decision), (
        "decision types with no state in the kept logs: "
        f"{sorted(set(ADVISED) - set(per_decision))}")
    assert not mismatches, (len(mismatches), mismatches[:5])
    assert not unserialisable, (len(unserialisable), unserialisable[:5])
