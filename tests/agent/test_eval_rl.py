from agent.eval_rl import format_floor_label, format_floor_labels


def test_format_floor_label_uses_act_relative_floor():
    assert format_floor_label(1) == "A1F1"
    assert format_floor_label(7) == "A1F7"
    assert format_floor_label(17) == "A1F17"
    assert format_floor_label(18) == "A2F1"
    assert format_floor_label(22) == "A2F5"
    assert format_floor_label(35) == "A3F1"


def test_format_floor_labels_sorts_numeric_floors_before_formatting():
    assert format_floor_labels([22, 7, 18]) == "[A1F7, A2F1, A2F5]"
