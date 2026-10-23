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
