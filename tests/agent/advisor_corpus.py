# tests/agent/advisor_corpus.py
"""Loader for tests/fixtures/advisor/ooc_corpus.jsonl.gz
(built by tools/build_advisor_corpus.py), plus the guard that tells a changed
local data file apart from a changed greedy."""
import gzip
import itertools
import json
import os

import pytest

from agent.greedy_trace import greedy_data_sha256

CORPUS = os.path.join(os.path.dirname(__file__), "..", "fixtures", "advisor",
                      "ooc_corpus.jsonl.gz")
META = os.path.join(os.path.dirname(__file__), "..", "fixtures", "advisor",
                    "ooc_corpus.meta.json")


def corpus_rows() -> list:
    with gzip.open(CORPUS, "rt") as f:
        return [json.loads(line) for line in f]


def corpus_games() -> list:
    """[(game, rows in play order)] in file order."""
    return [(game, list(rows)) for game, rows in
            itertools.groupby(corpus_rows(), key=lambda r: r["game"])]


def corpus_meta() -> dict:
    """{"batch": ..., "data_sha256": {GREEDY_DATA_FILES entry: hex or None}}"""
    with open(META) as f:
        return json.load(f)


def _short(digest) -> str:
    return "absent" if digest is None else digest[:12]


def data_drift() -> list:
    """One human-readable line per GREEDY_DATA_FILES entry whose current sha256
    (or absence) differs from the one the corpus was built with."""
    built = corpus_meta()["data_sha256"]
    now = greedy_data_sha256()
    return [f"{rel}: built {_short(built.get(rel))}, now {_short(now[rel])}"
            for rel in sorted(now) if built.get(rel) != now[rel]]


def require_same_greedy_data() -> None:
    drift = data_drift()
    if drift:
        pytest.fail("greedy's local data files changed since the corpus was built: "
                    + "; ".join(drift)
                    + "; rebuild with tools/build_advisor_corpus.py --batch "
                    + corpus_meta()["batch"])
