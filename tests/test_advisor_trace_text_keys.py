"""Every greedy trace key has panel text in the mod, and the mod has no text for
keys greedy no longer emits (agent/greedy_trace.py TRACE_KEYS <-> TraceText.cs)."""
import os
import re

from agent.greedy_trace import TRACE_KEYS

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "Sts2Advisor.Core", "TraceText.cs")


def test_trace_text_covers_exactly_the_trace_keys():
    with open(SRC, encoding="utf-8") as f:
        keys = set(re.findall(r'\["([a-z0-9_]+\.[a-z0-9_.]+)"\]\s*=\s*\(', f.read()))
    assert sorted(TRACE_KEYS - keys) == []
    assert sorted(keys - TRACE_KEYS) == []


def test_every_event_keyword_the_scorer_can_fire_has_panel_text_in_both_languages():
    """_score_event_option's event.kw.* keys (agent/combat_env.py) are what an event option's reasons show; each must
    be a trace key with a Chinese and an English template (not the raw key, which is what the panel falls back to)."""
    env = os.path.join(os.path.dirname(__file__), "..", "agent", "combat_env.py")
    with open(env, encoding="utf-8") as f:
        fired = set(re.findall(r'_kw\(score, hits, "(event\.kw\.[a-z_]+)"', f.read()))
    assert len(fired) >= 20
    assert sorted(fired - TRACE_KEYS) == []
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    for key in sorted(fired):
        m = re.search(r'\["%s"\]\s*=\s*\("([^"]+)",\s*"([^"]+)"\)' % re.escape(key), text)
        assert m, key
        zh, en = m.groups()
        assert re.search(r"[一-鿿]", zh), (key, zh)
        assert zh != en and "event.kw" not in zh + en, key
