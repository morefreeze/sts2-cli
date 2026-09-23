"""Regression test for the map-routing determinism bug.

play_full_run.py's play_run() passes a seed string to the C# engine's
start_run, but the harness's OWN map_select decisions used to be drawn from
the unseeded global `random` module. That meant the SAME seed could route
differently between runs, because whatever else had touched the global
random module before/during the run (order of test execution, other library
calls, etc.) changed the sequence handed to random.choice().

Confirmed pre-fix evidence (see the plan this test accompanies): Defect
seed "run_1" reached floor 12 on one run and floor 7 on an immediate re-run
of the identical seed, with completely different select_map_node routes.

The fix seeds a dedicated `random.Random(seed)` instance inside play_run()
(never `random.seed()`, which would reseed the shared global module and
change behavior for every other component using it). This test proves that
by perturbing the global `random` module's state between two invocations of
the same seed and asserting the chosen map route is unchanged -- if play_run
ever regresses to reading global `random` state again, this test will start
failing without needing the real game engine.

No game DLLs / subprocess involved: play_full_run.EngineProcess (the seam
play_run() drives the engine through) is monkeypatched to a scripted fake so
this stays fast and hermetic.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run


class _FakeEngine:
    """Stands in for engine_process.EngineProcess, the seam play_run() drives.

    Feeds back a pre-scripted sequence of engine responses (one per
    read_json()) and records every command written so the test can inspect
    exactly which select_map_node actions the harness chose. (This used to
    fake subprocess.Popen; play_run() now talks to the engine only through
    EngineProcess, whose reader thread iterates stdout -- a readline()-only
    Popen fake would silently starve it and wait out the reply timeout.)
    """

    def __init__(self, script):
        self._script = list(script)
        self._idx = 0
        self.sent = []

    def write(self, obj):
        self.sent.append(json.dumps(obj))

    def read_json(self, timeout, on_skip=None):
        reply = self._script[self._idx]
        self._idx += 1
        return reply

    def kill(self):
        pass

    def close(self):
        pass


def _map_select_script(n_steps=5):
    """ready -> n_steps x map_select (4 choices each) -> game_over."""
    script = [{"type": "ready"}]
    for i in range(n_steps):
        script.append({
            "type": "decision",
            "decision": "map_select",
            "choices": [{"col": c, "row": i + 1} for c in range(4)],
        })
    script.append({
        "type": "decision",
        "decision": "game_over",
        "victory": False,
        "act": 1,
        "floor": n_steps,
        "player": {"hp": 10, "max_hp": 10, "gold": 0, "deck_size": 10},
    })
    return script


def _run_and_capture_map_choices(monkeypatch, seed):
    created = []

    def _fake_engine(argv, **kwargs):
        engine = _FakeEngine(_map_select_script())
        created.append(engine)
        return engine

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake_engine)
    play_full_run.play_run(seed, character="Ironclad", verbose=False, log=False)

    engine = created[0]
    picks = []
    for line in engine.sent:
        cmd = json.loads(line)
        if cmd.get("action") == "select_map_node":
            picks.append((cmd["args"]["col"], cmd["args"]["row"]))
    return picks


def test_map_choice_sequence_immune_to_global_random_state(monkeypatch):
    saved_state = random.getstate()
    try:
        random.seed(111)
        first = _run_and_capture_map_choices(monkeypatch, "det_seed_1")

        random.seed(999999)  # different global state -- must not matter
        second = _run_and_capture_map_choices(monkeypatch, "det_seed_1")

        assert first == second, (
            "same seed produced different map routes after the global "
            "random state changed -- play_run() is reading global random "
            "state again instead of a seed-derived random.Random instance"
        )
        # Sanity: the fake harness actually exercised branching (multiple
        # choices per step), not a script with only one option per decision.
        assert len(first) == 5
    finally:
        random.setstate(saved_state)


def test_different_seeds_can_pick_different_routes(monkeypatch):
    """Guards against a fix that accidentally hardcodes a single outcome
    (e.g. always picking choices[0]) which would trivially pass the
    determinism test above without actually being seeded by `seed`.
    """
    saved_state = random.getstate()
    try:
        routes = {
            seed: tuple(_run_and_capture_map_choices(monkeypatch, seed))
            for seed in ("seed_a", "seed_b", "seed_c", "seed_d")
        }
        assert len(set(routes.values())) > 1, (
            "different seeds all produced the identical route -- map "
            "selection may not actually be keyed off `seed`"
        )
    finally:
        random.setstate(saved_state)
