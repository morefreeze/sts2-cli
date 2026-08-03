import json
from enum import Enum

import pytest

from agent.run_workbench.models import (
    ActMap,
    Capabilities,
    Coverage,
    DeltaQuality,
    MapAlignment,
    MapEdge,
    MapNode,
    RunDelta,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)


def test_act_map_serializes_an_authoritative_aligned_route() -> None:
    result = ActMap(
        act_id="ACT.OVERGROWTH",
        full_map=True,
        visited_route=True,
        nodes=(
            MapNode(
                id="3:0",
                col=3,
                row=0,
                room_type="Ancient",
                visited=True,
                path_index=0,
            ),
            MapNode(
                id="1:1",
                col=1,
                row=1,
                room_type="Monster",
                visited=True,
                path_index=1,
            ),
            MapNode(id="3:1", col=3, row=1, room_type="Monster"),
        ),
        edges=(MapEdge(from_id="3:0", to_id="1:1"),),
        alignment=MapAlignment(
            ok=True,
            ambiguous=False,
            path_node_ids=("3:0", "1:1"),
        ),
    )

    payload = result.to_dict()

    assert payload["act_id"] == "ACT.OVERGROWTH"
    assert payload["full_map"] is True
    assert payload["visited_route"] is True
    assert payload["fallback_reason"] is None
    assert payload["alignment"] == {
        "ok": True,
        "ambiguous": False,
        "reason": None,
        "path_node_ids": ["3:0", "1:1"],
    }
    assert payload["edges"] == [{"from": "3:0", "to": "1:1"}]
    assert all(
        node["visited"] is False or node["path_index"] is not None
        for node in payload["nodes"]
    )


def test_map_models_are_immutable() -> None:
    node = MapNode(id="visited:0", col=0, row=0, room_type="Ancient")

    with pytest.raises(AttributeError):
        node.row = 2  # type: ignore[misc]


def test_unknown_deltas_preserve_none_values() -> None:
    numeric_delta = RunDelta()
    list_delta = RunDelta(value=None)

    assert numeric_delta.to_dict() == {"value": None, "quality": "unknown"}
    assert list_delta.to_dict() == {"value": None, "quality": "unknown"}


def test_only_technical_failures_are_marked_technical() -> None:
    technical_statuses = {
        RunStatus.CRASH,
        RunStatus.TIMEOUT,
        RunStatus.STUCK,
        RunStatus.RESET_FAILURE,
        RunStatus.INVALID,
    }

    assert all(status.is_technical for status in technical_statuses)
    assert not RunStatus.DEAD.is_technical
    assert not RunStatus.WIN.is_technical


def test_partial_record_serializes_coverage_and_capabilities() -> None:
    record = RunRecord(
        run_id="run-1",
        source_id="eval.jsonl:1",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(character="Ironclad", seed="eval_fixed_0"),
        outcome=RunOutcome(status=RunStatus.DEAD, max_global_floor=21),
        coverage=Coverage(
            complete_run=False,
            first_recorded_floor=18,
            last_recorded_floor=21,
        ),
        capabilities=Capabilities(visited_route=True),
    )

    payload = record.to_dict()

    assert payload["outcome"]["max_global_floor"] == 21
    assert payload["coverage"] == {
        "complete_run": False,
        "first_recorded_floor": 18,
        "last_recorded_floor": 21,
    }
    assert payload["capabilities"]["visited_route"] is True
    assert payload["capabilities"]["turn_replay"] is False


def test_model_output_is_json_safe_with_string_enum_values() -> None:
    record = RunRecord(
        run_id="run-2",
        source_id="replay.jsonl:2",
        source_kind=SourceKind.REPLAY_JSONL,
        metadata=RunMetadata(modifiers=("elite", "fast")),
        outcome=RunOutcome(status=RunStatus.IN_PROGRESS),
        acts=[{"kind": SourceKind.SUMMARY}],
        nodes=[{"delta": RunDelta(value=["card"], quality=DeltaQuality.DERIVED)}],
    )

    payload = record.to_dict()

    assert payload["source_kind"] == "replay_jsonl"
    assert payload["metadata"]["modifiers"] == ["elite", "fast"]
    assert payload["outcome"]["status"] == "in_progress"
    assert payload["acts"] == [{"kind": "summary"}]
    assert payload["nodes"] == [{"delta": {"value": ["card"], "quality": "derived"}}]
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize(
    ("value", "type_name"),
    [
        ({"card"}, "set"),
        (b"card", "bytes"),
        (complex(1, 2), "complex"),
    ],
)
def test_serialization_rejects_unsupported_nested_values(value: object, type_name: str) -> None:
    record = RunRecord(
        run_id="unsupported-value",
        source_id="summary:unsupported",
        source_kind=SourceKind.SUMMARY,
        nodes=[{"delta": RunDelta(value=value)}],
    )

    with pytest.raises(TypeError, match=type_name):
        record.to_dict()


def test_serialization_rejects_non_string_mapping_keys() -> None:
    record = RunRecord(
        run_id="unsupported-key",
        source_id="summary:unsupported",
        source_kind=SourceKind.SUMMARY,
        nodes=[{1: "not a public JSON key"}],
    )

    with pytest.raises(TypeError, match="dict key.*int"):
        record.to_dict()


def test_serialization_rejects_enum_with_unsupported_value() -> None:
    class UnsafeValue(Enum):
        BYTES = b"card"

    record = RunRecord(
        run_id="unsafe-enum",
        source_id="summary:unsafe-enum",
        source_kind=SourceKind.SUMMARY,
        nodes=[{"delta": RunDelta(value=UnsafeValue.BYTES)}],
    )

    with pytest.raises(TypeError, match="bytes"):
        record.to_dict()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_serialization_rejects_non_finite_floats(value: float) -> None:
    record = RunRecord(
        run_id="non-finite-float",
        source_id="summary:non-finite-float",
        source_kind=SourceKind.SUMMARY,
        nodes=[{"delta": RunDelta(value=value)}],
    )

    with pytest.raises(ValueError, match="non-finite float"):
        record.to_dict()


def test_serialized_global_floors_order_numerically_across_acts() -> None:
    records = [
        RunRecord("act-3", "summary:3", SourceKind.SUMMARY, outcome=RunOutcome(max_global_floor=51)),
        RunRecord("act-1", "summary:1", SourceKind.SUMMARY, outcome=RunOutcome(max_global_floor=17)),
        RunRecord("act-2", "summary:2", SourceKind.SUMMARY, outcome=RunOutcome(max_global_floor=34)),
    ]

    payloads = [record.to_dict() for record in records]
    ordered = sorted(payloads, key=lambda payload: payload["outcome"]["max_global_floor"])

    assert [payload["outcome"]["max_global_floor"] for payload in ordered] == [17, 34, 51]
    assert all(isinstance(payload["outcome"]["max_global_floor"], int) for payload in ordered)
