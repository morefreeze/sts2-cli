import json
from pathlib import Path

import pytest

from agent.run_progress_viewer import parse_game_progress
from agent.run_workbench.adapters import adapt_path
from agent.run_workbench.models import RunStatus, SourceKind


FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_workbench"


def test_native_run_preserves_identity_and_format_capabilities() -> None:
    adapted = adapt_path(FIXTURES / "native_run.json")

    assert adapted.descriptor.kind is SourceKind.NATIVE_RUN
    assert len(adapted.runs) == 1
    run = adapted.runs[0]
    assert run.metadata.game_version == "fixture-build"
    assert run.metadata.seed == "fixture-seed"
    assert run.metadata.character == "IRONCLAD"
    assert run.capabilities.visited_route is True
    assert run.capabilities.node_rewards is True
    assert run.capabilities.turn_replay is False
    assert run.nodes == [{"floor": 1}]


def test_invalid_run_suffix_does_not_fabricate_a_complete_capable_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.run"
    path.write_text("{}", encoding="utf-8")

    adapted = adapt_path(path)

    assert adapted.descriptor.kind is SourceKind.NATIVE_RUN
    assert adapted.runs == ()
    assert any("native run" in error for error in adapted.errors)


def test_replay_uses_injected_legacy_parser_and_records_observed_floor_range() -> None:
    adapted = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=parse_game_progress,
    )

    assert len(adapted.runs) == 1
    run = adapted.runs[0]
    assert run.capabilities.turn_replay is True
    assert run.capabilities.visited_route is True
    assert run.capabilities.decisions is True
    assert run.coverage.first_recorded_floor == 1
    assert run.coverage.last_recorded_floor == 1


def test_replay_nodes_are_exactly_the_injected_parser_rooms() -> None:
    calls: list[tuple[list[dict], str | None]] = []
    rooms = [{"id": "room-1", "global_floor": 3}]

    def parser(entries: list[dict], source_name: str | None = None) -> dict:
        calls.append((entries, source_name))
        return {
            "summary": {"seed": "parser-seed", "character": "Parser"},
            "rooms": rooms,
        }

    adapted = adapt_path(FIXTURES / "replay.jsonl", replay_parser=parser)

    assert calls and calls[0][1] == "replay.jsonl"
    assert adapted.runs[0].nodes == rooms
    assert adapted.runs[0].metadata.seed == "parser-seed"
    assert adapted.runs[0].metadata.character == "Parser"


def test_partial_replay_reports_only_observed_coverage() -> None:
    adapted = adapt_path(
        FIXTURES / "partial_replay.jsonl",
        replay_parser=parse_game_progress,
    )

    run = adapted.runs[0]
    assert run.coverage.complete_run is False
    assert run.coverage.first_recorded_floor == 12
    assert run.coverage.last_recorded_floor == 12


def test_replay_parser_is_a_dependency_and_is_not_required_for_safe_adaptation() -> None:
    adapted = adapt_path(FIXTURES / "replay.jsonl")

    assert len(adapted.runs) == 1
    assert adapted.runs[0].nodes == []
    assert adapted.runs[0].capabilities.turn_replay is False
    assert any("replay parser" in error for error in adapted.errors)


@pytest.mark.parametrize(
    "parser",
    [
        lambda entries, source_name=None: (_ for _ in ()).throw(RuntimeError("boom")),
        lambda entries, source_name=None: ["not", "an", "object"],
    ],
)
def test_failed_replay_parser_does_not_claim_turn_replay(parser) -> None:
    adapted = adapt_path(FIXTURES / "replay.jsonl", replay_parser=parser)

    assert len(adapted.runs) == 1
    assert adapted.runs[0].capabilities.turn_replay is False
    assert adapted.errors


def test_action_only_replay_has_decision_evidence_but_no_turn_replay(
    tmp_path: Path,
) -> None:
    path = tmp_path / "actions.jsonl"
    path.write_text(
        '{"type":"action","data":{"cmd":"action","action":"end_turn","args":{}}}\n',
        encoding="utf-8",
    )

    adapted = adapt_path(
        path,
        replay_parser=lambda entries, source_name=None: {"summary": {}, "rooms": []},
    )

    run = adapted.runs[0]
    assert run.capabilities.visited_route is False
    assert run.capabilities.decisions is True
    assert run.capabilities.turn_replay is False
    assert "__unassigned_actions__" in run.replay_by_node


@pytest.mark.parametrize("unsafe_value", [{"bad"}, float("nan"), float("inf")])
def test_unsafe_injected_replay_data_is_reported_and_not_returned(
    unsafe_value: object,
) -> None:
    def parser(entries: list[dict], source_name: str | None = None) -> dict:
        return {
            "summary": {},
            "rooms": [{"id": "unsafe-room", "score": unsafe_value}],
        }

    adapted = adapt_path(FIXTURES / "replay.jsonl", replay_parser=parser)

    assert adapted.runs == ()
    assert any("JSON-safe" in error for error in adapted.errors)


def test_deck_history_produces_one_outcome_per_exact_run_id() -> None:
    adapted = adapt_path(FIXTURES / "deck_history.jsonl")

    assert len(adapted.runs) == 1
    run = adapted.runs[0]
    assert run.run_id == "training-1"
    assert run.outcome.status is RunStatus.WIN
    assert run.outcome.victory is True
    assert run.metadata.character is None
    assert run.metadata.seed is None
    assert run.metadata.checkpoint is None
    assert all(node["_workbench_evidence_kind"] == "deck_history_event" for node in run.nodes)
    assert all(node["_workbench_provenance"] for node in run.nodes)


def test_deck_history_does_not_group_empty_run_ids(tmp_path: Path) -> None:
    path = tmp_path / "old_deck_history.jsonl"
    path.write_text(
        '\n'.join(
            [
                json.dumps({"event": "milestone", "floor_crossed": 5}),
                json.dumps({"event": "outcome", "status": "dead", "max_floor": 7}),
            ]
        ),
        encoding="utf-8",
    )

    adapted = adapt_path(path)

    assert len(adapted.runs) == 2
    assert all(run.run_id == "" for run in adapted.runs)
    assert [run.metadata.seed for run in adapted.runs] == [None, None]
    assert all(any("missing run_id" in warning for warning in run.warnings) for run in adapted.runs)
    assert any("missing run_id" in warning for warning in adapted.errors)


def test_eval_results_produce_one_record_per_row(tmp_path: Path) -> None:
    path = tmp_path / "eval_results.jsonl"
    rows = [
        {
            "event": "eval_result",
            "run_id": "eval-1",
            "status": "dead",
            "floor": 9,
            "checkpoint": "model_13000k.zip",
        },
        {
            "event": "eval_result",
            "run_id": "eval-2",
            "status": "win",
            "floor": 34,
            "checkpoint": "model_14000k.zip",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    adapted = adapt_path(path)

    assert [run.run_id for run in adapted.runs] == ["eval-1", "eval-2"]
    assert [run.outcome.status for run in adapted.runs] == [RunStatus.DEAD, RunStatus.WIN]
    assert adapted.runs[1].metadata.checkpoint == "model_14000k.zip"


@pytest.mark.parametrize(
    "status",
    ["crash", "timeout", "stuck", "reset_failure", "invalid"],
)
def test_technical_eval_statuses_remain_technical(tmp_path: Path, status: str) -> None:
    path = tmp_path / "technical.jsonl"
    path.write_text(
        json.dumps({"event": "eval_result", "run_id": status, "status": status}),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert run.outcome.status.value == status
    assert run.outcome.status.is_technical
    assert run.outcome.victory is False
    assert run.outcome.technical_failure_kind == status


@pytest.mark.parametrize(
    ("status", "raw_victory", "expected_victory"),
    [
        ("win", False, True),
        ("dead", True, False),
        ("in_progress", True, None),
    ],
)
def test_explicit_eval_status_is_authoritative_over_contradictory_victory(
    tmp_path: Path,
    status: str,
    raw_victory: bool,
    expected_victory: bool | None,
) -> None:
    path = tmp_path / "contradictory_eval.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "eval_result",
                "run_id": status,
                "status": status,
                "victory": raw_victory,
            }
        ),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert run.outcome.status.value == status
    assert run.outcome.victory is expected_victory


def test_summary_source_returns_summary_without_fabricating_a_run() -> None:
    adapted = adapt_path(FIXTURES / "summary.jsonl")

    assert adapted.descriptor.kind is SourceKind.SUMMARY
    assert adapted.runs == ()
    assert adapted.summary is not None
    assert adapted.summary["record_count"] == 2
    assert adapted.summary["records"][0]["checkpoint"] == "boss"


def test_unknown_shape_is_safe_and_does_not_fabricate_runs(tmp_path: Path) -> None:
    path = tmp_path / "unknown.json"
    path.write_text('{"event":"other"}', encoding="utf-8")

    adapted = adapt_path(path)

    assert adapted.descriptor.kind is SourceKind.UNKNOWN
    assert adapted.runs == ()
    assert adapted.summary is None
    assert adapted.errors


def test_adapter_output_remains_json_safe() -> None:
    run = adapt_path(FIXTURES / "native_run.json").runs[0]

    json.dumps(run.to_dict(), allow_nan=False)
