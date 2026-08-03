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
