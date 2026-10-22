"""greedy_action must keep choosing what it chose when the corpus was built
(tools/build_advisor_corpus.py). Replays each game in order -- greedy has
hidden state, see agent/greedy_trace.reset_greedy_context. The last game,
"synthetic", holds rewritten real states for branches real runs never reach.

After a deliberate greedy change, `tools/build_advisor_corpus.py --check
--batch <batch from the corpus meta>` lists the golden actions that moved."""
import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_DATA_FILES, reset_greedy_context
from tests.agent.advisor_corpus import (corpus_games, corpus_meta, corpus_rows,
                                        fresh_greedy_state,
                                        require_same_greedy_data)


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def test_corpus_covers_every_advised_decision():
    seen = {row["state"]["decision"] for row in corpus_rows()}
    assert seen == {"card_reward", "rest_site", "event_choice", "bundle_select",
                    "card_select", "shop", "map_select"}


def test_corpus_has_a_synthetic_game_for_unreached_branches():
    games = dict(corpus_games())
    assert "synthetic" in games and len(games["synthetic"]) >= 50


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
                mismatches.append((game, row["seq"], row.get("variant"),
                                   row["state"]["decision"], row["expected"], got))
    assert not mismatches, mismatches[:5]
