"""tools/build_advisor_corpus.py: game-log parsing, game selection, the guards
that keep it from writing a bad corpus, deterministic output, and --check.

Everything runs on tiny fake batches in a tmp dir (states copied from the
committed corpus), never on the real fixture or logs/."""
import gzip
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import build_advisor_corpus as bac  # noqa: E402
from tests.agent.advisor_corpus import corpus_rows, fresh_greedy_state  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_greedy():
    fresh_greedy_state()
    yield
    fresh_greedy_state()


def write_log(path, states, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "run_meta", "seed": "t"}),
             json.dumps({"step": 0, "type": "action", "data": {"cmd": "start_run"}})]
    lines += [json.dumps({"step": i + 1, "type": "state", "data": s})
              for i, s in enumerate(states)]
    path.write_text("\n".join(lines) + "\n" + tail, encoding="utf-8")


def one_state_per_decision():
    """First real corpus state of each advised decision type."""
    first = {}
    for row in corpus_rows():
        if row["game"] != bac.SYNTHETIC_GAME:
            first.setdefault(row["state"]["decision"], row["state"])
    return list(first.values())


def full_batch(tmp_path, tail=""):
    batch = tmp_path / "batch"
    write_log(batch / "games" / "Ironclad__random" / "Ironclad_greedy_a1_t_1.jsonl",
              one_state_per_decision(), tail=tail)
    return batch


def build_args(batch, out, *extra):
    return ["--batch", str(batch), "--out", str(out), "--per-character", "1",
            "--bundle-games", "0", *extra]


def state(decision):
    return {"type": "decision", "decision": decision}


# --- game_states --------------------------------------------------------------

def test_game_states_keeps_advised_decision_rows_and_skips_a_truncated_last_line(tmp_path):
    log = tmp_path / "Ironclad_greedy_a1_t_1.jsonl"
    write_log(log, [state("map_select"), state("combat_play"),
                    {"type": "other"}, state("card_reward")],
              tail='{"step": 9, "type": "sta')
    stats = {"bad_lines": 0}
    got = [s["decision"] for s in bac.game_states(str(log), stats)]
    assert got == ["map_select", "card_reward"]
    assert stats == {"bad_lines": 1}


def test_game_states_skips_json_that_is_not_an_object(tmp_path):
    log = tmp_path / "g_1.jsonl"
    log.write_text("42\n[1, 2]\n", encoding="utf-8")
    stats = {}
    assert list(bac.game_states(str(log), stats)) == []
    assert stats == {"bad_lines": 2}


# --- select_games -------------------------------------------------------------

def test_select_games_takes_files_in_seed_order_and_skips_names_without_a_seed(tmp_path, capsys):
    lane = tmp_path / "games" / "Ironclad__random"
    for name in ("Ironclad_greedy_a1_t_10", "Ironclad_greedy_a1_t_2",
                 "Ironclad_greedy_a1_t_1", "notes", "Ironclad_greedy_a1_t_x"):
        write_log(lane / f"{name}.jsonl", [state("map_select")])
    got = bac.select_games(str(tmp_path), per_character=2, bundle_games=0)
    assert [os.path.basename(p) for p in got] == [
        "Ironclad_greedy_a1_t_1.jsonl", "Ironclad_greedy_a1_t_2.jsonl"]
    # the four lanes with no logs at all are called out
    assert capsys.readouterr().err.count("warning:") == 4


def test_select_games_round_robins_bundle_games_across_characters(tmp_path):
    games = tmp_path / "games"
    for ch, seeds in (("Ironclad", (1, 3, 4, 5)), ("Silent", (1, 3)), ("Defect", (1, 3))):
        for seed in seeds:
            states = [state("bundle_select")] if seed >= 3 else [state("map_select")]
            write_log(games / f"{ch}__random" / f"{ch}_greedy_a1_t_{seed}.jsonl", states)
    got = bac.select_games(str(tmp_path), per_character=1, bundle_games=3)
    assert [os.path.basename(p) for p in got[3:]] == [
        "Ironclad_greedy_a1_t_3.jsonl", "Silent_greedy_a1_t_3.jsonl",
        "Defect_greedy_a1_t_3.jsonl"]


# --- main: guards ---------------------------------------------------------------

def test_main_refuses_a_missing_batch_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out" / "ooc_corpus.jsonl.gz"
    assert bac.main(build_args(tmp_path / "no-such-batch", out)) == 2
    assert not out.parent.exists()
    assert "no game logs" in capsys.readouterr().err


def test_main_refuses_when_an_advised_decision_is_missing(tmp_path, capsys):
    batch = tmp_path / "batch"
    write_log(batch / "games" / "Ironclad__random" / "Ironclad_greedy_a1_t_1.jsonl",
              [s for s in one_state_per_decision() if s["decision"] != "bundle_select"])
    out = tmp_path / "ooc_corpus.jsonl.gz"
    assert bac.main(build_args(batch, out)) == 2
    assert not out.exists() and not os.path.exists(bac.meta_path(str(out)))
    assert "bundle_select" in capsys.readouterr().err


def test_main_refuses_when_a_greedy_env_var_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("STS2_RANDOMIZE", "1")
    out = tmp_path / "ooc_corpus.jsonl.gz"
    assert bac.main(build_args(full_batch(tmp_path), out)) == 2
    assert not out.exists()


# --- main: output -------------------------------------------------------------

def test_main_writes_corpus_meta_and_a_synthetic_game_and_leaves_no_tmp_files(tmp_path, capsys):
    out = tmp_path / "out" / "ooc_corpus.jsonl.gz"
    batch = full_batch(tmp_path, tail='{"step": 99, "type": "sta')  # killed lane
    assert bac.main(build_args(batch, out)) == 0
    assert "unparsable" in capsys.readouterr().err
    assert sorted(os.listdir(out.parent)) == ["ooc_corpus.jsonl.gz", "ooc_corpus.meta.json"]
    rows = bac.read_corpus(str(out))
    assert {r["state"]["decision"] for r in rows} == bac.ADVISED
    synthetic = [r for r in rows if r["game"] == bac.SYNTHETIC_GAME]
    assert len(synthetic) >= 50 and rows[-len(synthetic):] == synthetic
    meta = json.loads((out.parent / "ooc_corpus.meta.json").read_text(encoding="utf-8"))
    assert meta["batch"] == str(batch)
    assert set(meta["data_sha256"]) and list(meta) == sorted(meta)


def test_main_output_is_byte_identical_for_identical_input(tmp_path):
    batch = full_batch(tmp_path)
    a, b = tmp_path / "a" / "c.jsonl.gz", tmp_path / "b" / "c.jsonl.gz"
    assert bac.main(build_args(batch, a)) == 0
    assert bac.main(build_args(batch, b)) == 0
    assert a.read_bytes() == b.read_bytes()


def test_synthetic_variants_reach_the_branches_real_states_never_do(tmp_path):
    out = tmp_path / "c.jsonl.gz"
    assert bac.main(build_args(full_batch(tmp_path), out)) == 0
    by_variant = {r["variant"]: r["expected"]
                  for r in bac.read_corpus(str(out)) if r["game"] == bac.SYNTHETIC_GAME}
    proceed = {"cmd": "action", "action": "proceed"}
    assert by_variant["rest none-enabled"] == proceed
    assert by_variant["map no-choices"] == proceed
    assert by_variant["event all-locked"]["action"] == "leave_room"
    assert by_variant["card_select empty"]["action"] == "skip_select"
    assert by_variant["bundle single"]["args"] == {"bundle_index": 0}
    assert by_variant["shop heal-potion hp30%"]["action"] == "buy_potion"
    assert by_variant["shop broke leave"]["action"] == "leave_room"
    # an Elite beside a Monster: avoided on floor 1, taken at floor 8 with 95% hp
    assert by_variant["map elite-vs-monster act1 f1 hp1.0"]["args"] != \
        by_variant["map elite-vs-monster act1 f8 hp0.95"]["args"]


# --- main --check ---------------------------------------------------------------

def test_check_passes_on_an_unchanged_corpus_and_writes_nothing(tmp_path, capsys):
    batch = full_batch(tmp_path)
    out = tmp_path / "out" / "ooc_corpus.jsonl.gz"
    assert bac.main(build_args(batch, out)) == 0
    before = {p.name: p.read_bytes() for p in out.parent.iterdir()}
    capsys.readouterr()
    assert bac.main(build_args(batch, out, "--check")) == 0
    assert "0 expected action(s) changed" in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in out.parent.iterdir()} == before


def test_check_fails_and_names_the_decision_after_an_expected_action_changes(tmp_path, capsys):
    batch = full_batch(tmp_path)
    out = tmp_path / "out" / "ooc_corpus.jsonl.gz"
    assert bac.main(build_args(batch, out)) == 0
    rows = bac.read_corpus(str(out))
    victim = next(r for r in rows if r["state"]["decision"] == "card_reward")
    victim["expected"] = {"cmd": "action", "action": "skip_card_reward"}
    os.replace(bac.write_corpus(rows, str(out)), str(out))
    before = out.read_bytes()
    capsys.readouterr()
    assert bac.main(build_args(batch, out, "--check")) == 1
    printed = capsys.readouterr().out
    assert "1 expected action(s) changed" in printed
    assert "card_reward: 1 of" in printed and "e.g." in printed
    assert out.read_bytes() == before  # --check never rewrites the corpus


def test_check_fails_when_the_corpus_is_missing(tmp_path):
    out = tmp_path / "nope.jsonl.gz"
    assert bac.main(build_args(full_batch(tmp_path), out, "--check")) == 2
    assert not out.exists()
