# tests/agent/test_greedy_trace_recorder.py
"""GreedyTrace: the recorder greedy_action writes its reasons into."""
import json
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from agent import card_scoring
from agent.greedy_trace import (GREEDY_BEHAVIOR_DEFAULTS, GREEDY_BEHAVIOR_ENV,
                                TRACE_KEYS, GreedyTrace, behavior_env_is_default,
                                reset_greedy_context)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_option_and_rule_round_trip():
    t = GreedyTrace()
    t.option(2, kind="card", score=7.5,
             components=[("card.base", 6.0, "A"), ("card.archetype", 1.5, None)])
    t.option(0, kind="card", status="filtered")
    t.rule("card_reward.pick_best", "fired", best_index=2, threshold=5.5)
    d = t.to_dict()
    assert [o["index"] for o in d["options"]] == [0, 2]
    assert d["options"][1] == {
        "kind": "card", "index": 2, "score": 7.5, "status": "ok",
        "components": [{"key": "card.base", "value": 6.0, "detail": "A"},
                       {"key": "card.archetype", "value": 1.5}],
    }
    assert d["options"][0]["score"] is None and d["options"][0]["status"] == "filtered"
    assert d["rules"] == [{"key": "card_reward.pick_best", "outcome": "fired",
                           "values": {"best_index": 2, "threshold": 5.5}}]
    assert sum(c["value"] for c in d["options"][1]["components"]) == d["options"][1]["score"]
    assert t.score_of("card", 2) == 7.5
    assert t.ok_scores("card") == [7.5]
    assert [r["key"] for r in t.fired()] == ["card_reward.pick_best"]


def test_unknown_key_raises():
    t = GreedyTrace()
    with pytest.raises(KeyError):
        t.rule("card_reward.typo", "fired")
    with pytest.raises(KeyError):
        t.option(0, kind="card", score=1.0, components=[("card.typo", 1.0, None)])


def test_bad_outcome_and_status_raise():
    t = GreedyTrace()
    with pytest.raises(ValueError):
        t.rule("card_reward.pick_best", "maybe")
    with pytest.raises(ValueError):
        t.option(0, kind="card", status="weird")


def test_behavior_env_covers_play_full_run_knobs():
    import play_full_run
    assert set(play_full_run.GREEDY_KNOBS) <= set(GREEDY_BEHAVIOR_ENV)
    assert {"STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL"} <= set(GREEDY_BEHAVIOR_ENV)
    # read by agent/card_scoring.py on greedy_action's path
    assert {"STS2_ARCHETYPE", "STS2_BOSS_READINESS_LIFT", "STS2_CTX_WEIGHTS",
            "STS2_OVERRIDE_DELTA_CAP", "STS2_PREDICTOR_TARGET",
            "STS2_MC_SMART_THRESH"} <= set(GREEDY_BEHAVIOR_ENV)


# What the reading code resolves each variable to, read in a fresh interpreter
# (several are read at import time) whose environment has no GREEDY_BEHAVIOR_ENV
# variable but the ones a test passes.
_PROBE = """
import json
from agent import card_scoring as cs, combat_env as ce
print(json.dumps({
    "randomize": sorted(ce._RANDOMIZE_DECISIONS),
    "decision_advisor": ce._decision_advisor_enabled(),
    "card_threshold_lift": ce._card_threshold_lift(),
    "card_quality_gate": ce._card_quality_gate_enabled(),
    "mc_rollout": [cs._MC_ROLLOUT_ENABLED, cs._MC_MODE],
    "archetype": cs._ACTIVE_ARCHETYPE,
    "ctx_weights": cs.CTX_BLOCK_VALUE,
    "override_delta_cap": cs._OVERRIDE_DELTA_CAP,
    "predictor_target": cs.PREDICTOR_TARGET,
    "mc_smart_thresh": cs._MC_SMART_THRESHOLD,
    "map_policy": ce._map_policy(),
}))
"""


def _probe(extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in GREEDY_BEHAVIOR_ENV}
    env.update(extra_env or {})
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_behavior_defaults_cover_exactly_the_behavior_env():
    assert set(GREEDY_BEHAVIOR_DEFAULTS) == set(GREEDY_BEHAVIOR_ENV)


def test_behavior_env_is_default_rules():
    assert behavior_env_is_default("STS2_CARD_QUALITY_GATE", None)
    assert behavior_env_is_default("STS2_CARD_QUALITY_GATE", " 1 ")
    assert not behavior_env_is_default("STS2_CARD_QUALITY_GATE", "0")
    assert not behavior_env_is_default("STS2_CARD_QUALITY_GATE", "")   # set-but-blank = gate OFF
    assert behavior_env_is_default("STS2_MC_ROLLOUT", " ")             # blank default: blank is unset
    assert not behavior_env_is_default("STS2_MC_ROLLOUT", "smart")
    assert behavior_env_is_default("STS2_OVERRIDE_DELTA_CAP", "2")     # numeric spelling
    assert not behavior_env_is_default("STS2_OVERRIDE_DELTA_CAP", "2.5")
    assert behavior_env_is_default("STS2_PREDICTOR_TARGET", "Floor")
    assert not behavior_env_is_default("STS2_PREDICTOR_TARGET", "hp")


def test_behavior_defaults_match_what_the_reading_code_resolves():
    unset = _probe()
    # the shipped values the map's non-blank defaults stand for
    assert unset["card_quality_gate"] is True
    assert unset["card_threshold_lift"] == 0.0
    assert unset["override_delta_cap"] == 2.0
    assert unset["predictor_target"] == "floor"
    assert unset["mc_smart_thresh"] == 0.15
    # every variable set to its default (blank defaults: set but blank) resolves
    # exactly as the same variable unset
    assert _probe(dict(GREEDY_BEHAVIOR_DEFAULTS)) == unset
    # ...and the probe sees every one of them: a non-default value moves its entry
    moved = _probe({"STS2_RANDOMIZE": "card_reward", "STS2_DECISION_ADVISOR": "1",
                    "STS2_CARD_THRESHOLD_LIFT": "1.5", "STS2_CARD_QUALITY_GATE": "0",
                    "STS2_MC_ROLLOUT": "smart", "STS2_ARCHETYPE": "orbs",
                    "STS2_CTX_WEIGHTS": "legacy", "STS2_OVERRIDE_DELTA_CAP": "3",
                    "STS2_PREDICTOR_TARGET": "hp", "STS2_MC_SMART_THRESH": "0.5",
                    "STS2_MAP_POLICY": "lookahead"})
    assert {k for k in unset if moved[k] != unset[k]} == set(unset)


def test_behavior_defaults_match_the_literal_defaults_in_the_source():
    """Every `environ.get("STS2_X", "<literal>")` on greedy's path agrees with the
    map (this is also the only check of STS2_CARD_PICK_TEMP, read per call, and of
    the blank-default flags read per call); a read with a computed default
    (STS2_OVERRIDE_DELTA_CAP) is covered by the probe above."""
    pattern = re.compile(r'get\(\s*"(STS2_[A-Z0-9_]+)"\s*(?:,\s*(?:"([^"]*)"|[^)]*))?\)')
    seen = {}
    for rel in ("agent/card_scoring.py", "agent/combat_env.py", "agent/strategy.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            for m in pattern.finditer(f.read()):
                if m.group(1) in GREEDY_BEHAVIOR_DEFAULTS:
                    # group 2 is None when the default is not a string literal
                    seen.setdefault(m.group(1), []).append((rel, m.group(2)))
    # every variable is read somewhere this scan looks (a rename would drop it)
    assert set(seen) == set(GREEDY_BEHAVIOR_ENV)
    for key, reads in seen.items():
        for rel, literal in reads:
            if literal is not None:    # computed / absent defaults: see the probe
                assert behavior_env_is_default(key, literal), (key, rel, literal)


def test_reset_greedy_context_restores_module_default():
    card_scoring.set_mc_context(hp=12, max_hp=70, floor=14, relics=["X"])
    reset_greedy_context()
    assert card_scoring._MC_CONTEXT == {"hp": 80, "max_hp": 80, "floor": 5}


def test_event_keyword_keys_are_trace_keys():
    assert sum(1 for k in TRACE_KEYS if k.startswith("event.kw.")) == 28


def test_rule_key_and_outcome_are_positional_only():
    t = GreedyTrace()
    t.rule("card_reward.pick_best", "fired", key=1)
    assert t.rules == [{"key": "card_reward.pick_best", "outcome": "fired",
                        "values": {"key": 1}}]


def test_option_recorded_twice_raises():
    t = GreedyTrace()
    t.option(1, kind="card", score=1.0)
    with pytest.raises(ValueError, match="option card:1 recorded twice"):
        t.option(1, kind="card", score=2.0)
    assert t.score_of("card", 1) == 1.0
    # same index under another kind is a different option
    t.option(1, kind="relic", score=3.0)
    assert t.score_of("relic", 1) == 3.0


def test_option_detail_appears_in_to_dict():
    t = GreedyTrace()
    t.option(0, kind="shop", score=4.0, detail={"price": 75})
    t.option(1, kind="shop", score=2.0)
    d = t.to_dict()
    assert d["options"][0]["detail"] == {"price": 75}
    assert "detail" not in d["options"][1]


def test_to_dict_orders_options_by_kind_then_index():
    t = GreedyTrace()
    t.option(0, kind="relic", score=1.0)
    t.option(2, kind="card", score=1.0)
    t.option(0, kind="card", score=1.0)
    assert [(o["kind"], o["index"]) for o in t.to_dict()["options"]] == [
        ("card", 0), ("card", 2), ("relic", 0)]
