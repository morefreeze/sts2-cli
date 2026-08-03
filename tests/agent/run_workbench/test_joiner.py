from agent.run_workbench.joiner import join_records
from agent.run_workbench.models import (
    Capabilities,
    Coverage,
    RunMetadata,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
)


def test_exact_nonempty_run_id_joins_deck_and_eval_records() -> None:
    deck = RunRecord(
        run_id="shared-run",
        source_id="deck.jsonl:shared-run",
        source_kind=SourceKind.DECK_HISTORY,
        metadata=RunMetadata(seed="fixed-1"),
        outcome=RunOutcome(status=RunStatus.WIN, victory=True, max_global_floor=34),
        coverage=Coverage(complete_run=True),
        capabilities=Capabilities(visited_route=True, decisions=True),
        nodes=[{"kind": "card_pick"}],
    )
    evaluation = RunRecord(
        run_id="shared-run",
        source_id="eval.jsonl:1",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(checkpoint="model_14000k.zip"),
        outcome=RunOutcome(status=RunStatus.WIN, victory=True, max_global_floor=34),
        capabilities=Capabilities(turn_replay=True),
    )

    joined = join_records([evaluation, deck])

    assert len(joined) == 1
    run = joined[0]
    assert run.metadata.checkpoint == "model_14000k.zip"
    assert run.metadata.seed == "fixed-1"
    assert run.capabilities.visited_route is True
    assert run.capabilities.decisions is True
    assert run.capabilities.turn_replay is True
    assert "deck.jsonl:shared-run" in run.source_id
    assert "eval.jsonl:1" in run.source_id


def test_conflicting_non_null_metadata_is_deterministic_and_warned() -> None:
    later_source = RunRecord(
        run_id="same",
        source_id="z-eval",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(character="Silent", seed="seed-z"),
    )
    earlier_source = RunRecord(
        run_id="same",
        source_id="a-deck",
        source_kind=SourceKind.DECK_HISTORY,
        metadata=RunMetadata(character="Ironclad", seed="seed-a"),
    )

    forward = join_records([later_source, earlier_source])[0]
    reverse = join_records([earlier_source, later_source])[0]

    assert forward.metadata == reverse.metadata
    assert forward.metadata.character == "Ironclad"
    assert forward.metadata.seed == "seed-a"
    assert any("conflicting metadata character" in warning for warning in forward.warnings)
    assert any("conflicting metadata seed" in warning for warning in forward.warnings)


def test_conflicting_gameplay_outcomes_keep_status_and_victory_coherent() -> None:
    won = RunRecord(
        run_id="same",
        source_id="a-win",
        source_kind=SourceKind.EVAL_RESULTS,
        outcome=RunOutcome(status=RunStatus.WIN, victory=True),
    )
    dead = RunRecord(
        run_id="same",
        source_id="b-dead",
        source_kind=SourceKind.DECK_HISTORY,
        outcome=RunOutcome(
            status=RunStatus.DEAD,
            victory=False,
            technical_failure_kind="stale-technical-kind",
        ),
    )

    outcome = join_records([dead, won])[0].outcome

    assert outcome.status is RunStatus.WIN
    assert outcome.victory is True
    assert outcome.technical_failure_kind is None


def test_technical_outcome_precedence_sets_false_victory_and_matching_kind() -> None:
    gameplay = RunRecord(
        run_id="same",
        source_id="a-gameplay",
        source_kind=SourceKind.DECK_HISTORY,
        outcome=RunOutcome(status=RunStatus.WIN, victory=True),
    )
    technical = RunRecord(
        run_id="same",
        source_id="b-technical",
        source_kind=SourceKind.EVAL_RESULTS,
        outcome=RunOutcome(status=RunStatus.TIMEOUT, victory=None),
    )

    run = join_records([technical, gameplay])[0]

    assert run.outcome.status is RunStatus.TIMEOUT
    assert run.outcome.victory is False
    assert run.outcome.technical_failure_kind == "timeout"
    assert any("conflicting outcome status" in warning for warning in run.warnings)


def test_missing_run_ids_never_merge_on_matching_seed_and_timestamp() -> None:
    deck = RunRecord(
        run_id="",
        source_id="old-deck:1",
        source_kind=SourceKind.DECK_HISTORY,
        metadata=RunMetadata(seed="old-seed", started_at=100.0, ended_at=120.0),
    )
    replay = RunRecord(
        run_id="",
        source_id="old-replay:1",
        source_kind=SourceKind.REPLAY_JSONL,
        metadata=RunMetadata(seed="old-seed", started_at=100.0, ended_at=120.0),
    )

    joined = join_records([deck, replay])

    assert len(joined) == 2
    assert {run.source_id for run in joined} == {"old-deck:1", "old-replay:1"}
    assert all(
        any(
            "ambiguous historical identity" in warning
            and ("old-replay:1" in warning if run.source_id == "old-deck:1" else "old-deck:1" in warning)
            for warning in run.warnings
        )
        for run in joined
    )


def test_anonymous_and_identified_overlap_remain_separate_and_warned() -> None:
    anonymous = RunRecord(
        run_id="",
        source_id="old-deck:1",
        source_kind=SourceKind.DECK_HISTORY,
        metadata=RunMetadata(seed="shared-seed", started_at=100.0, ended_at=120.0),
    )
    identified = RunRecord(
        run_id="known-run",
        source_id="eval:1",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(seed="shared-seed", started_at=110.0, ended_at=130.0),
    )

    joined = join_records([anonymous, identified])

    assert len(joined) == 2
    assert {run.run_id for run in joined} == {"", "known-run"}
    assert all(
        any("ambiguous historical identity" in warning for warning in run.warnings)
        for run in joined
    )


def test_missing_run_ids_without_seed_and_time_evidence_remain_unwarned() -> None:
    first = RunRecord("", "one", SourceKind.DECK_HISTORY)
    second = RunRecord("", "two", SourceKind.REPLAY_JSONL)

    joined = join_records([first, second])

    assert len(joined) == 2
    assert all(not run.warnings for run in joined)


def test_distinct_nonempty_run_ids_remain_distinct() -> None:
    first = RunRecord("one", "eval:1", SourceKind.EVAL_RESULTS)
    second = RunRecord("two", "eval:2", SourceKind.EVAL_RESULTS)

    assert [run.run_id for run in join_records([second, first])] == ["one", "two"]


def test_join_sort_key_never_serializes_full_run_evidence(
    monkeypatch,
) -> None:
    first = RunRecord(
        "same",
        "same-source",
        SourceKind.DECK_HISTORY,
        metadata=RunMetadata(character="Ironclad", seed="seed-b"),
        nodes=[{"payload": "x" * 1_000_000}],
    )
    second = RunRecord(
        "same",
        "same-source",
        SourceKind.DECK_HISTORY,
        metadata=RunMetadata(character="Silent", seed="seed-a"),
        nodes=[{"payload": "y" * 1_000_000}],
    )

    def fail_to_dict(self):
        raise AssertionError("join sort key serialized full evidence")

    monkeypatch.setattr(RunRecord, "to_dict", fail_to_dict)

    forward = join_records([first, second])[0]
    reverse = join_records([second, first])[0]

    assert forward.metadata == reverse.metadata
    assert forward.metadata.character == "Ironclad"
    assert forward.metadata.seed == "seed-b"


def test_equal_provenance_keys_use_content_tiebreaker_for_metadata_conflicts() -> None:
    ironclad = RunRecord(
        run_id="same",
        source_id="same-source",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(character="Ironclad", seed="seed-b"),
    )
    silent = RunRecord(
        run_id="same",
        source_id="same-source",
        source_kind=SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(character="Silent", seed="seed-a"),
    )

    forward = join_records([ironclad, silent])[0]
    reverse = join_records([silent, ironclad])[0]

    assert forward.to_dict() == reverse.to_dict()
    assert any("conflicting metadata character" in warning for warning in forward.warnings)
    assert any("conflicting metadata seed" in warning for warning in forward.warnings)


def test_conflicting_stable_evidence_is_deduplicated_with_provenance() -> None:
    native = RunRecord(
        run_id="same",
        source_id="native.run",
        source_kind=SourceKind.NATIVE_RUN,
        acts=[{"act": 1, "name": "native-act"}],
        nodes=[
            {"id": "shared-node", "gold": 10},
            {"room_type": "Event", "description": "native-only"},
        ],
    )
    replay = RunRecord(
        run_id="same",
        source_id="replay.jsonl",
        source_kind=SourceKind.REPLAY_JSONL,
        acts=[{"act": 1, "name": "replay-act"}],
        nodes=[
            {"id": "shared-node", "gold": 20},
            {"room_type": "Event", "description": "replay-only"},
        ],
    )

    run = join_records([replay, native])[0]

    assert len(run.nodes) == 3
    shared = next(node for node in run.nodes if node.get("id") == "shared-node")
    assert shared["gold"] == 10
    assert {item["source_kind"] for item in shared["_workbench_provenance"]} == {
        "native_run",
        "replay_jsonl",
    }
    assert shared["_workbench_conflicting_evidence"][0]["payload"]["gold"] == 20
    assert all(node["_workbench_provenance"] for node in run.nodes)
    assert len(run.acts) == 1
    assert run.acts[0]["name"] == "native-act"
    assert run.acts[0]["_workbench_conflicting_evidence"][0]["payload"]["name"] == "replay-act"
    assert any("conflicting node payload" in warning for warning in run.warnings)
    assert any("conflicting act payload" in warning for warning in run.warnings)


def test_join_does_not_mutate_input_records() -> None:
    first = RunRecord(
        "same",
        "one",
        SourceKind.DECK_HISTORY,
        metadata=RunMetadata(seed="a"),
    )
    second = RunRecord(
        "same",
        "two",
        SourceKind.EVAL_RESULTS,
        metadata=RunMetadata(seed="b"),
    )

    join_records([first, second])

    assert first.warnings == []
    assert second.warnings == []
