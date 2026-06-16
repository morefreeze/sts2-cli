import numpy as np
from agent.state_encoder import build_relic_vocab, RELIC_VOCAB, RELIC_VOCAB_SIZE


def test_relic_vocab_built_from_db():
    vocab = build_relic_vocab("data/relics.json")
    assert len(vocab) == 272
    assert "AKABEKO" in vocab
    assert "ANCHOR" in vocab
    assert sorted(vocab.values()) == list(range(272))
    assert RELIC_VOCAB_SIZE == len(RELIC_VOCAB) == 272


def test_relic_vocab_cap():
    vocab = build_relic_vocab("data/relics.json", cap=50)
    assert len(vocab) == 50
    assert sorted(vocab.values()) == list(range(50))
