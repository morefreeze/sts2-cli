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


from agent.state_encoder import encode_relics


def test_encode_relics_multihot():
    vec = encode_relics(["AKABEKO", "ANCHOR"])
    assert vec.dtype == np.float32
    assert vec.shape == (RELIC_VOCAB_SIZE,)
    assert vec.sum() == 2.0
    assert vec[RELIC_VOCAB["AKABEKO"]] == 1.0
    assert vec[RELIC_VOCAB["ANCHOR"]] == 1.0


def test_encode_relics_unknown_ignored():
    vec = encode_relics(["NOT_A_REAL_RELIC_XYZ"])
    assert vec.shape == (RELIC_VOCAB_SIZE,)
    assert vec.sum() == 0.0


def test_encode_relics_empty():
    assert encode_relics([]).sum() == 0.0
