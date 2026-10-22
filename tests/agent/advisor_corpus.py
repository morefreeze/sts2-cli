# tests/agent/advisor_corpus.py
"""Loader for tests/fixtures/advisor/ooc_corpus.jsonl.gz
(built by tools/build_advisor_corpus.py), plus the guard that tells a changed
local data file apart from a changed greedy."""
import gzip
import itertools
import json
import os

import pytest

from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, greedy_data_drift,
                                reset_greedy_context)

CORPUS = os.path.join(os.path.dirname(__file__), "..", "fixtures", "advisor",
                      "ooc_corpus.jsonl.gz")
META = os.path.join(os.path.dirname(__file__), "..", "fixtures", "advisor",
                    "ooc_corpus.meta.json")


def corpus_rows() -> list:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def corpus_games() -> list:
    """[(game, rows in play order)] in file order."""
    return [(game, list(rows)) for game, rows in
            itertools.groupby(corpus_rows(), key=lambda r: r["game"])]


def corpus_meta() -> dict:
    """{"batch": ..., "data_sha256": {GREEDY_DATA_FILES entry: hex or None}}"""
    with open(META, encoding="utf-8") as f:
        return json.load(f)


def data_drift() -> list:
    """One human-readable line per GREEDY_DATA_FILES entry whose current sha256
    (or absence) differs from the one the corpus was built with."""
    return greedy_data_drift(corpus_meta()["data_sha256"])


def require_same_greedy_data() -> None:
    drift = data_drift()
    if drift:
        pytest.fail("greedy's local data files changed since the corpus was built: "
                    + "; ".join(drift)
                    + "; rebuild with tools/build_advisor_corpus.py --batch "
                    + corpus_meta()["batch"])


def fresh_greedy_state() -> None:
    """Put greedy in the state a fresh process has, for a hermetic test.

    - fails naming any GREEDY_BEHAVIOR_ENV variable that is set: several are
      read when agent.card_scoring is imported, so unsetting one inside a test
      cannot undo it;
    - makes card_scoring reload its Q table from data/card_q_values.json (an
      earlier test may have called update_card_q);
    - resets card_scoring._MC_CONTEXT (reset_greedy_context)."""
    set_knobs = [k for k in GREEDY_BEHAVIOR_ENV if (os.environ.get(k) or "").strip()]
    if set_knobs:
        pytest.fail(f"greedy behaviour env vars are set: {set_knobs}; unset them "
                    "(they are read at import time, so a test cannot undo them)")
    from agent import card_scoring
    card_scoring._CARD_Q = {}
    card_scoring._CARD_Q_LOADED = False
    reset_greedy_context()
