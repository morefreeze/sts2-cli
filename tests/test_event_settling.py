"""Event tasks must reach a real input boundary before publishing a decision."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from engine_process import EngineProcess
from .conftest import DOTNET


def test_neow_selection_continuation_settles_before_next_option(tmp_path):
    """The completed upgrade must already be visible when select_cards replies."""
    engine = Path(__file__).resolve().parents[1] / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll"
    prefix = [
        {"cmd": "start_run", "character": "Fool", "seed": "FOOLA9OFFLINE01", "ascension": 9, "lang": "en"},
        {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}},
        {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}},
    ]
    expected = ["event_choice", "card_select", "map_select"]

    def replay(attempt):
        with (tmp_path / f"native-{attempt}.log").open("w") as stderr:
            process = EngineProcess([DOTNET, str(engine)], stderr=stderr)
            try:
                assert process.read_json(45)["type"] == "ready"
                observations = []
                for index, command in enumerate(prefix):
                    process.write(command)
                    state = process.read_json(45)
                    assert state.get("decision") == expected[index], (attempt, index, state)
                    observations.append(state)
                assert observations[2]["player"]["deck_size"] == 11
                assert sum(c["upgraded"] for c in observations[2]["player"]["deck"]) == 1
                return observations
            finally:
                process.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(replay, range(60)))
    assert all(result == results[0] for result in results)
