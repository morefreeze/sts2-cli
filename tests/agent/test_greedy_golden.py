"""greedy_action must keep choosing what it chose when the corpus was built
(tools/build_advisor_corpus.py). Replays each game in order -- greedy has
hidden state, see agent/greedy_trace.reset_greedy_context."""
import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, GREEDY_DATA_FILES,
                                reset_greedy_context)
from tests.agent.advisor_corpus import (corpus_games, corpus_meta, corpus_rows,
                                        require_same_greedy_data)


@pytest.fixture(autouse=True)
def _default_greedy_env(monkeypatch):
    for key in GREEDY_BEHAVIOR_ENV:
        monkeypatch.delenv(key, raising=False)


def test_corpus_covers_every_advised_decision():
    seen = {row["state"]["decision"] for row in corpus_rows()}
    assert seen == {"card_reward", "rest_site", "event_choice", "bundle_select",
                    "card_select", "shop", "map_select"}


def test_corpus_meta_covers_every_greedy_data_file():
    assert set(corpus_meta()["data_sha256"]) == set(GREEDY_DATA_FILES)


def test_greedy_matches_golden():
    require_same_greedy_data()
    mismatches = []
    for game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            got = greedy_action(row["state"])
            if got != row["expected"]:
                mismatches.append((game, row["seq"], row["state"]["decision"],
                                   row["expected"], got))
    assert not mismatches, mismatches[:5]
