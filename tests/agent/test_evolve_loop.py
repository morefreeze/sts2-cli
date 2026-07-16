from agent.evolve_loop import parse_eval_reach


def test_parse_eval_reach_accepts_act_floor_labels():
    text = """
avg_floor      : 13.0
floor dist     : [A1F7, A1F17, A2F5]
"""

    assert parse_eval_reach(text) == (13.0, 2)


def test_parse_eval_reach_keeps_legacy_numeric_floor_dist():
    text = """
avg_floor      : 12.5
floor dist     : [7, 17, 22]
"""

    assert parse_eval_reach(text) == (12.5, 2)
