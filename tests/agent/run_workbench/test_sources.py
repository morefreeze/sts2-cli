import json
from pathlib import Path

import pytest

from agent.run_workbench.models import SourceKind
from agent.run_workbench.sources import (
    SourceFormatError,
    classify_path,
    classify_records,
    read_json_records,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_workbench"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("native_run.json", SourceKind.NATIVE_RUN),
        ("replay.jsonl", SourceKind.REPLAY_JSONL),
        ("partial_replay.jsonl", SourceKind.REPLAY_JSONL),
        ("deck_history.jsonl", SourceKind.DECK_HISTORY),
        ("eval_results.jsonl", SourceKind.EVAL_RESULTS),
        ("summary.jsonl", SourceKind.SUMMARY),
    ],
)
def test_classifies_supported_shapes(name: str, expected: SourceKind) -> None:
    assert classify_path(FIXTURES / name).kind is expected


def test_malformed_jsonl_names_the_file_and_line() -> None:
    with pytest.raises(SourceFormatError, match=r"malformed.jsonl:2"):
        read_json_records(FIXTURES / "malformed.jsonl")


def test_run_suffix_classifies_one_object_as_native(tmp_path: Path) -> None:
    path = tmp_path / "history.run"
    path.write_text(json.dumps({"anything": "allowed"}), encoding="utf-8")

    assert classify_path(path).kind is SourceKind.NATIVE_RUN


@pytest.mark.parametrize(
    "payload, expected_count",
    [
        ({"event": "eval_result"}, 1),
        ([{"event": "eval_result"}, {"event": "eval_result"}], 2),
    ],
)
def test_json_accepts_one_object_or_a_top_level_list(
    tmp_path: Path, payload: object, expected_count: int
) -> None:
    path = tmp_path / "records.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    records = read_json_records(path)

    assert len(records) == expected_count
    assert all(isinstance(record, dict) for record in records)


def test_run_rejects_a_top_level_list(tmp_path: Path) -> None:
    path = tmp_path / "invalid.run"
    path.write_text('[{"players": []}]', encoding="utf-8")

    with pytest.raises(SourceFormatError, match=r"invalid.run.*top-level object"):
        read_json_records(path)


def test_jsonl_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text('\n {"event": "eval_result"}\n\n', encoding="utf-8")

    assert read_json_records(path) == [{"event": "eval_result"}]


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
def test_invalid_utf8_is_reported_as_a_source_format_error(
    tmp_path: Path, suffix: str
) -> None:
    path = tmp_path / f"invalid_utf8{suffix}"
    path.write_bytes(b"\x80")

    with pytest.raises(SourceFormatError, match=rf"{path.name}.*invalid UTF-8.*byte 0") as error:
        read_json_records(path)

    assert isinstance(error.value.__cause__, UnicodeDecodeError)


@pytest.mark.parametrize(
    ("name", "contents", "location"),
    [
        ("scalar.json", '"not an object"', "scalar.json:top-level"),
        ("items.json", '[{"event": "ok"}, 2]', "items.json:2"),
        ("record.jsonl", '["not an object"]\n', "record.jsonl:1"),
    ],
)
def test_non_object_records_name_the_file_and_location(
    tmp_path: Path, name: str, contents: str, location: str
) -> None:
    path = tmp_path / name
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(SourceFormatError, match=location):
        read_json_records(path)


def test_unknown_valid_shape_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "unknown.json"
    path.write_text('{"event": "other"}', encoding="utf-8")

    descriptor = classify_path(path)

    assert descriptor.kind is SourceKind.UNKNOWN
    assert descriptor.message == "unsupported JSON shape"


def test_action_only_gamelogger_records_are_a_replay(tmp_path: Path) -> None:
    path = tmp_path / "actions.jsonl"
    path.write_text(
        '{"type":"action","data":{"cmd":"action","action":"end_turn","args":{}}}\n',
        encoding="utf-8",
    )

    assert classify_path(path).kind is SourceKind.REPLAY_JSONL


def test_action_with_non_mapping_data_is_not_a_replay(tmp_path: Path) -> None:
    path = tmp_path / "not_a_replay.jsonl"
    path.write_text('{"type":"action","data":"not a command object"}\n', encoding="utf-8")

    assert classify_path(path).kind is SourceKind.UNKNOWN


def test_classification_precedence_is_deterministic() -> None:
    native_and_everything = [
        {
            "players": [],
            "map_point_history": [],
            "type": "state",
            "event": "milestone",
        }
    ]
    replay_and_deck = [{"type": "state", "event": "milestone"}]
    deck_and_eval = [{"event": "milestone"}, {"event": "eval_result"}]
    eval_and_summary = [{"event": "eval_result"}, {"event": "result"}]

    assert classify_records(native_and_everything, suffix=".json").kind is SourceKind.NATIVE_RUN
    assert classify_records(replay_and_deck, suffix=".jsonl").kind is SourceKind.REPLAY_JSONL
    assert classify_records(deck_and_eval, suffix=".jsonl").kind is SourceKind.DECK_HISTORY
    assert classify_records(eval_and_summary, suffix=".jsonl").kind is SourceKind.EVAL_RESULTS


def test_classification_normalizes_suffix_case() -> None:
    assert classify_records([{"event": "other"}], suffix=".RUN").kind is SourceKind.NATIVE_RUN


def test_unsupported_suffix_names_the_file_and_suffix(tmp_path: Path) -> None:
    path = tmp_path / "records.txt"
    path.write_text('{"event": "eval_result"}', encoding="utf-8")

    with pytest.raises(SourceFormatError, match=r"records.txt.*\.txt"):
        read_json_records(path)
