import json
import math
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from pathlib import Path

import pytest

from agent.run_progress_viewer import parse_game_progress
from agent.run_workbench.adapters import adapt_path, adapt_records
from agent.run_workbench.deltas import NodeDeltas
from agent.run_workbench.details import (
    DETAIL_COLLECTION_LIMIT,
    InvalidNodeDetailError,
    NodeNotFoundError,
    build_node_detail,
)
from agent.run_workbench.joiner import join_records
from agent.run_workbench.models import (
    Capabilities,
    Coverage,
    DeltaQuality,
    InventorySnapshot,
    NodeDetail,
    NodeOrigin,
    RunDelta,
    RunOutcome,
    RunRecord,
    RunStatus,
    SourceKind,
    node_evidence_key,
)
from agent.run_workbench.sources import SourceDescriptor


FIXTURES = Path(__file__).parents[2] / "fixtures" / "run_workbench"


def _rich_native_run() -> RunRecord:
    adapted = adapt_records(
        "source-neutral.data",
        [
            {
                "run_id": "native-detail-run",
                "players": [
                    {
                        "character": "IRONCLAD",
                        "current_hp": 64,
                        "max_hp": 80,
                        "current_gold": 120,
                        "deck": [
                            {"id": "CARD.STRIKE", "name": "Strike"},
                            {"id": "CARD.REWARD", "name": "Reward"},
                        ],
                        "relics": [{"id": "RELIC.STARTER", "name": "Starter"}],
                        "potions": [{"id": "POTION.FIRE", "name": "Fire Potion"}],
                    }
                ],
                "map_point_history": [
                    {
                        "act": 1,
                        "floor": 1,
                        "global_floor": 1,
                        "map_point_type": "ancient",
                        "rooms": [
                            {
                                "model_id": "EVENT.NEOW",
                                "event_name": "Neow",
                                "options": [
                                    {
                                        "option_id": "BLESSING",
                                        "title": "Blessing",
                                        "selected": True,
                                    }
                                ],
                            }
                        ],
                        "player_stats": [
                            {
                                "current_hp": 80,
                                "max_hp": 80,
                                "current_gold": 99,
                                "deck": [{"id": "CARD.STRIKE", "name": "Strike"}],
                                "relics": [
                                    {"id": "RELIC.STARTER", "name": "Starter"}
                                ],
                                "potions": [],
                            }
                        ],
                    },
                    {
                        "act": 1,
                        "floor": 2,
                        "global_floor": 2,
                        "map_point_type": "event",
                        "rooms": [{"model_id": "EVENT.GOLDEN_IDOL"}],
                        "choices": [
                            {"option_id": "TAKE_IDOL", "selected": True},
                            {"option_id": "LEAVE", "selected": False},
                        ],
                        "player_stats": [
                            {
                                "current_hp": 75,
                                "max_hp": 80,
                                "current_gold": 99,
                                "deck": [{"id": "CARD.STRIKE", "name": "Strike"}],
                                "relics": [
                                    {"id": "RELIC.STARTER", "name": "Starter"}
                                ],
                                "potions": [],
                            }
                        ],
                    },
                    {
                        "act": 1,
                        "floor": 3,
                        "global_floor": 3,
                        "map_point_type": "rest",
                        "options": [
                            {"option_id": "HEAL", "selected": False},
                            {"option_id": "SMITH", "selected": True},
                        ],
                        "player_stats": [
                            {
                                "current_hp": 75,
                                "max_hp": 80,
                                "current_gold": 99,
                                "deck": [{"id": "CARD.STRIKE", "name": "Strike"}],
                                "relics": [
                                    {"id": "RELIC.STARTER", "name": "Starter"}
                                ],
                                "potions": [],
                            }
                        ],
                    },
                    {
                        "act": 1,
                        "floor": 4,
                        "global_floor": 4,
                        "map_point_type": "monster",
                        "status": "won",
                        "outcome": {"victory": True, "gold_reward": 21},
                        "rooms": [
                            {
                                "model_id": "ENCOUNTER.JAW_WORM",
                                "monster_ids": ["MONSTER.JAW_WORM"],
                            }
                        ],
                        "choices": [
                            {"id": "CARD.REWARD", "selected": True},
                            {"id": "skip", "selected": False},
                        ],
                        "facts": [{"kind": "observed", "value": "won"}],
                        "hypotheses": [
                            {"kind": "counterfactual", "value": "could lose hp"}
                        ],
                        "player_stats": [
                            {
                                "current_hp": 64,
                                "max_hp": 80,
                                "current_gold": 120,
                                "damage_taken": 11,
                                "hp_healed": 0,
                                "cards_gained": [
                                    {"id": "CARD.REWARD", "name": "Reward"}
                                ],
                            }
                        ],
                    },
                ],
            }
        ],
    )
    assert adapted.errors == ()
    return adapted.runs[0]


def _fixture_replay_with_misleading_name() -> RunRecord:
    records = [
        json.loads(line)
        for line in (FIXTURES / "replay_a2f4_excerpt.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    descriptor = SourceDescriptor(
        SourceKind.REPLAY_JSONL,
        len(records),
        "test replay evidence",
    )
    adapted = adapt_records(
        "looks-like-a-native-run.run",
        records,
        descriptor=descriptor,
        replay_parser=parse_game_progress,
    )
    assert adapted.errors == ()
    return adapted.runs[0]


def test_native_details_preserve_encounter_choices_inventory_and_exact_deltas() -> None:
    run = _rich_native_run()

    ancient = build_node_detail(run, "a0:n0")
    event = build_node_detail(run, "a0:n1")
    rest = build_node_detail(run, "a0:n2")
    detail = build_node_detail(run, "a0:n3")

    assert isinstance(detail, NodeDetail)
    assert detail.run_id == "native-detail-run"
    assert (detail.act, detail.floor, detail.global_floor, detail.label) == (
        1,
        4,
        4,
        "A1F4",
    )
    assert detail.room_type == "monster"
    assert detail.status == "won"
    assert detail.encounter == {
        "model_id": "ENCOUNTER.JAW_WORM",
        "monster_ids": ["MONSTER.JAW_WORM"],
        "outcome": {"victory": True, "gold_reward": 21},
    }
    assert ancient.encounter["event_name"] == "Neow"
    assert [choice["option_id"] for choice in ancient.choices] == ["BLESSING"]
    assert [choice["option_id"] for choice in event.choices] == [
        "TAKE_IDOL",
        "LEAVE",
    ]
    assert [choice["option_id"] for choice in rest.choices] == ["HEAL", "SMITH"]
    assert detail.entry == InventorySnapshot(
        hp=75,
        max_hp=80,
        gold=99,
        deck=({"id": "CARD.STRIKE", "name": "Strike"},),
        relics=({"id": "RELIC.STARTER", "name": "Starter"},),
        potions=(),
    )
    assert detail.exit == InventorySnapshot(
        hp=64,
        max_hp=80,
        gold=120,
        deck=(
            {"id": "CARD.STRIKE", "name": "Strike"},
            {"id": "CARD.REWARD", "name": "Reward"},
        ),
        relics=({"id": "RELIC.STARTER", "name": "Starter"},),
        potions=({"id": "POTION.FIRE", "name": "Fire Potion"},),
    )
    assert detail.deltas.to_dict() == run.nodes[3]["deltas"]
    assert detail.deltas.cards_gained.to_dict() == {
        "value": [{"id": "CARD.REWARD", "name": "Reward"}],
        "quality": "exact",
    }
    assert detail.combat_rounds == ()
    assert detail.coverage["turn_replay"] is False
    assert detail.coverage["message"] == "此记录不包含逐回合操作"
    assert detail.facts == ()
    assert detail.hypotheses == ()


def test_replay_details_use_same_room_snapshots_and_canonical_rounds() -> None:
    run = _fixture_replay_with_misleading_name()

    event = build_node_detail(run, "A1F1:Event:1")
    detail = build_node_detail(run, "A2F4:Monster:3")

    assert event.choices == (
        {
            "kind": "event_option",
            "label": "Continue",
            "detail": "Continue the fixture run.",
            "match": {"index": 0},
            "selected": True,
        },
    )
    assert event.combat_rounds == ()
    assert event.coverage["turn_replay"] is False
    assert event.coverage["message"] == "此记录不包含逐回合操作"
    assert (detail.entry.hp, detail.exit.hp) == (70, 64)
    assert (detail.entry.gold, detail.exit.gold) == (120, 120)
    assert [choice["selected"] for choice in detail.choices] == [False, True]
    assert detail.actions[0]["card"]["name"] == "Training Strike"
    assert detail.actions[0]["target"]["name"] == "Training Beast"
    assert len(detail.combat_rounds) == 2
    first_round = detail.combat_rounds[0]
    assert first_round["round"] == 1
    assert first_round["hp_loss"] == 6
    assert first_round["actions"][0]["card"]["name"] == "Training Strike"
    assert first_round["start_state"]["hand"][0]["name"] == "Training Strike"
    assert first_round["start_state"]["enemies"][0]["intents"] == [
        {"type": "Attack", "damage": 6}
    ]
    assert first_round["end_state"]["hp"] == 64
    assert detail.deltas.hp_change.to_dict() == {
        "value": -6,
        "quality": "derived",
    }
    assert detail.coverage == {
        "complete_run": True,
        "first_recorded_floor": 1,
        "last_recorded_floor": 21,
        "turn_replay": True,
        "source_kind": "replay_jsonl",
        "run_status": "win",
        "terminal_node": True,
        "choices_complete": False,
        "combat_coverage_complete": True,
        "entry_inventory_fields": [
            "hp",
            "max_hp",
            "gold",
            "deck",
            "relics",
            "potions",
        ],
        "exit_inventory_fields": [
            "hp",
            "max_hp",
            "gold",
            "deck",
            "relics",
            "potions",
        ],
    }
    assert {fact["kind"] for fact in detail.facts} >= {
        "unused_potion",
        "card_reward_selected",
    }
    unused = next(fact for fact in detail.facts if fact["kind"] == "unused_potion")
    assert unused["evidence"] == {
        "potion_names": ["Training Potion"],
        "recorded_actions": 4,
        "combat_coverage_complete": True,
    }


def test_detail_coverage_reports_partial_recorded_floor_range() -> None:
    run = _fixture_replay_with_misleading_name()
    run.coverage = Coverage(
        complete_run=False,
        first_recorded_floor=18,
        last_recorded_floor=21,
    )

    coverage = build_node_detail(run, "A2F4:Monster:3").coverage

    assert coverage["complete_run"] is False
    assert coverage["first_recorded_floor"] == 18
    assert coverage["last_recorded_floor"] == 21


def test_build_node_detail_attaches_only_collected_facts_and_no_hypotheses() -> None:
    potion = {"id": "POTION.FIRE", "name": "Test Fire Potion"}
    run = RunRecord(
        run_id="diagnostic-detail",
        source_id="replay-source",
        source_kind=SourceKind.REPLAY_JSONL,
        coverage=Coverage(True, 1, 1),
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "A1F1:Monster:1",
                "label": "A1F1",
                "room_type": "Monster",
                "status": "completed",
                "start_player": {
                    "hp": 80,
                    "max_hp": 80,
                    "potions": [potion],
                },
                "end_player": {
                    "hp": 60,
                    "max_hp": 80,
                    "potions": [potion],
                },
                "actions": [],
                "combat": {
                    "rounds": [
                        {
                            "round": 1,
                            "start_step": 1,
                            "start_state": {
                                "step": 1,
                                "round": 1,
                                "hp": 80,
                                "max_hp": 80,
                                "potions": [potion],
                            },
                            "end_state": {
                                "step": 2,
                                "hp": 60,
                                "max_hp": 80,
                                "potions": [potion],
                            },
                            "actions": [
                                {
                                    "step": 1,
                                    "label": "end_turn",
                                    "action": {"action": "end_turn"},
                                }
                            ],
                            "hp_loss": 20,
                            "end_reason": "combat_end",
                        }
                    ],
                },
                "facts": [
                    {
                        "kind": "technical_failure",
                        "severity": "critical",
                        "statement": "forged raw fact",
                        "evidence": {"status": "timeout"},
                    }
                ],
                "hypotheses": [{"statement": "forged hypothesis"}],
            }
        ],
    )

    detail = build_node_detail(run, "A1F1:Monster:1")

    assert {fact["kind"] for fact in detail.facts} == {
        "large_node_hp_loss",
        "high_loss_round",
        "unused_potion",
    }
    assert all(fact["statement"] != "forged raw fact" for fact in detail.facts)
    assert detail.hypotheses == ()
    assert detail.coverage["combat_coverage_complete"] is True
    json.dumps(detail.to_dict(), ensure_ascii=False, allow_nan=False)

    malformed_run = deepcopy(run)
    malformed_action = deepcopy(
        malformed_run.nodes[0]["combat"]["rounds"][0]["actions"][0]
    )
    malformed_action["step"] = 0
    malformed_run.nodes[0]["combat"]["rounds"][0]["actions"].append(
        malformed_action
    )
    malformed = build_node_detail(malformed_run, "A1F1:Monster:1")
    assert "combat_coverage_complete" not in malformed.coverage
    assert all(fact["kind"] != "unused_potion" for fact in malformed.facts)


def test_detail_exposes_typed_run_outcome_for_technical_fact_collection() -> None:
    run = RunRecord(
        run_id="technical-detail",
        source_id="summary-source",
        source_kind=SourceKind.SUMMARY,
        outcome=RunOutcome(
            status=RunStatus.TIMEOUT,
            victory=False,
            max_global_floor=1,
            technical_failure_kind="untrusted-mismatch",
        ),
        coverage=Coverage(True, 1, 1),
        nodes=[
            {
                "id": "technical-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "status": "won",
            }
        ],
    )

    detail = build_node_detail(run, "technical-node")

    assert detail.coverage["run_status"] == "timeout"
    assert detail.coverage["technical_failure_kind"] == "timeout"
    assert detail.coverage["terminal_node"] is True
    assert [fact["kind"] for fact in detail.facts] == ["technical_failure"]


def test_terminal_node_uses_typed_max_floor_not_unordered_node_position() -> None:
    outcome = RunOutcome(
        status=RunStatus.DEAD,
        victory=False,
        max_global_floor=5,
    )
    terminal_source = RunRecord(
        run_id="unordered-terminal",
        source_id="terminal-source",
        source_kind=SourceKind.SUMMARY,
        outcome=outcome,
        coverage=Coverage(True, 1, 5),
        nodes=[
            {
                "id": "actual-terminal",
                "act": 1,
                "floor": 5,
                "global_floor": 5,
                "status": "dead",
                "exit_player": {
                    "hp": 0,
                    "potions": [{"id": "POTION.FIRE", "name": "Fire Potion"}],
                },
            }
        ],
    )
    earlier_source = RunRecord(
        run_id="unordered-terminal",
        source_id="earlier-source",
        source_kind=SourceKind.SUMMARY,
        outcome=outcome,
        coverage=Coverage(True, 1, 5),
        nodes=[
            {
                "id": "last-in-list",
                "act": 1,
                "floor": 2,
                "global_floor": 2,
                "status": "dead",
                "exit_player": {
                    "hp": 0,
                    "potions": [{"id": "POTION.FIRE", "name": "Fire Potion"}],
                },
            }
        ],
    )
    run = join_records([terminal_source, earlier_source])[0]
    terminal_node = next(node for node in run.nodes if node["id"] == "actual-terminal")
    earlier_node = next(node for node in run.nodes if node["id"] == "last-in-list")
    run.nodes[:] = [terminal_node, earlier_node]

    terminal = build_node_detail(run, "actual-terminal")
    nonterminal = build_node_detail(run, "last-in-list")

    assert terminal.coverage["terminal_node"] is True
    assert any(fact["kind"] == "death_with_potion" for fact in terminal.facts)
    assert nonterminal.coverage["terminal_node"] is False
    assert all(fact["kind"] != "death_with_potion" for fact in nonterminal.facts)


def test_replay_card_reward_facts_require_selected_or_explicit_skip_action() -> None:
    records = [
        json.loads(line)
        for line in (FIXTURES / "replay_a2f4_excerpt.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    reward_state_index = next(
        index
        for index, record in enumerate(records)
        if record.get("type") == "state"
        and (record.get("data") or {}).get("decision") == "card_reward"
    )
    descriptor = SourceDescriptor(
        SourceKind.REPLAY_JSONL,
        reward_state_index + 1,
        "truncated reward evidence",
    )
    truncated_adapted = adapt_records(
        "truncated-reward.jsonl",
        records[: reward_state_index + 1],
        descriptor=descriptor,
        replay_parser=parse_game_progress,
    )
    assert truncated_adapted.errors == ()
    truncated = build_node_detail(
        truncated_adapted.runs[0], "A2F4:Monster:3"
    )

    last_combat_state_index = max(
        index
        for index, record in enumerate(records)
        if record.get("type") == "state"
        and (record.get("data") or {}).get("decision") == "combat_play"
    )
    combat_truncated_adapted = adapt_records(
        "truncated-combat.jsonl",
        records[: last_combat_state_index + 1],
        descriptor=SourceDescriptor(
            SourceKind.REPLAY_JSONL,
            last_combat_state_index + 1,
            "truncated combat evidence",
        ),
        replay_parser=parse_game_progress,
    )
    assert combat_truncated_adapted.errors == ()
    combat_truncated = build_node_detail(
        combat_truncated_adapted.runs[0], "A2F4:Monster:3"
    )

    skipped_records = deepcopy(records)
    select_action = next(
        record
        for record in skipped_records
        if record.get("type") == "action"
        and (record.get("data") or {}).get("action") == "select_card_reward"
    )
    select_action["data"]["action"] = "skip_card_reward"
    select_action["data"]["args"] = {}
    skipped_adapted = adapt_records(
        "skipped-reward.jsonl",
        skipped_records,
        descriptor=SourceDescriptor(
            SourceKind.REPLAY_JSONL,
            len(skipped_records),
            "explicit skip reward evidence",
        ),
        replay_parser=parse_game_progress,
    )
    assert skipped_adapted.errors == ()
    skipped = build_node_detail(skipped_adapted.runs[0], "A2F4:Monster:3")
    selected = build_node_detail(
        _fixture_replay_with_misleading_name(), "A2F4:Monster:3"
    )

    assert all(
        fact["kind"] not in {"card_reward_selected", "card_reward_skipped"}
        for fact in truncated.facts
    )
    assert "combat_coverage_complete" not in combat_truncated.coverage
    assert all(
        fact["kind"] != "unused_potion" for fact in combat_truncated.facts
    )
    assert any(fact["kind"] == "card_reward_skipped" for fact in skipped.facts)
    assert any(fact["kind"] == "card_reward_selected" for fact in selected.facts)
    assert selected.coverage["choices_complete"] is False


def test_joined_nodes_dispatch_by_node_provenance_not_aggregate_source_kind() -> None:
    native = RunRecord(
        run_id="joined-detail",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "native-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "map_point_type": "monster",
                "player_stats": [{"current_hp": 77, "deck": [{"id": "NATIVE"}]}],
            }
        ],
    )
    replay = RunRecord(
        run_id="joined-detail",
        source_id="replay-source",
        source_kind=SourceKind.REPLAY_JSONL,
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "A1F2:Monster:7",
                "label": "A1F2",
                "room_type": "Monster",
                "start_player": {"hp": 20, "deck": [{"id": "REPLAY"}]},
                "end_player": {"hp": 14, "deck": [{"id": "REPLAY"}]},
                "combat": {
                    "rounds": [
                        {
                            "round": 1,
                            "start_state": {"hp": 20},
                            "end_state": {"hp": 14},
                            "actions": [],
                            "hp_loss": 6,
                        }
                    ]
                },
            }
        ],
    )
    joined = join_records([replay, native])[0]
    native_index = next(
        index for index, node in enumerate(joined.nodes) if node["id"] == "native-node"
    )
    replay_index = next(
        index
        for index, node in enumerate(joined.nodes)
        if node["id"] == "A1F2:Monster:7"
    )

    native_detail = build_node_detail(joined, "native-node")
    replay_detail = build_node_detail(joined, "A1F2:Monster:7")

    assert joined.source_kind is SourceKind.NATIVE_RUN
    assert joined.node_origins(native_index) == (
        NodeOrigin(SourceKind.NATIVE_RUN, "native-source"),
    )
    assert joined.node_origins(replay_index) == (
        NodeOrigin(SourceKind.REPLAY_JSONL, "replay-source"),
    )
    assert native_detail.exit.hp == 77
    assert native_detail.combat_rounds == ()
    assert replay_detail.entry.hp == 20
    assert replay_detail.exit.hp == 14
    assert len(replay_detail.combat_rounds) == 1
    assert replay_detail.coverage["turn_replay"] is True


def test_joined_conflicting_node_origins_choose_basic_detail_conservatively() -> None:
    native = RunRecord(
        run_id="ambiguous-node-origin",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "shared-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "map_point_type": "monster",
                "player_stats": [{"current_hp": 70}],
            }
        ],
    )
    replay = RunRecord(
        run_id="ambiguous-node-origin",
        source_id="replay-source",
        source_kind=SourceKind.REPLAY_JSONL,
        nodes=[
            {
                "id": "shared-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "room_type": "Monster",
                "start_player": {"hp": 20},
                "end_player": {"hp": 14},
                "combat": {"rounds": [{"round": 1}]},
            }
        ],
    )
    joined = join_records([replay, native])[0]

    detail = build_node_detail(joined, "shared-node")

    assert {origin.source_kind for origin in joined.node_origins(0)} == {
        SourceKind.NATIVE_RUN,
        SourceKind.REPLAY_JSONL,
    }
    assert detail.exit.hp is None
    assert detail.combat_rounds == ()


def test_joined_same_kind_multiple_origins_choose_basic_detail_conservatively() -> None:
    first = RunRecord(
        run_id="ambiguous-native-origins",
        source_id="native-source-a",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "shared-native-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "map_point_type": "monster",
                "player_stats": [{"current_hp": 70}],
            }
        ],
    )
    second = RunRecord(
        run_id="ambiguous-native-origins",
        source_id="native-source-b",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "shared-native-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "map_point_type": "monster",
                "player_stats": [{"current_hp": 60}],
            }
        ],
    )
    joined = join_records([second, first])[0]

    detail = build_node_detail(joined, "shared-native-node")

    assert joined.node_origins(0) == (
        NodeOrigin(SourceKind.NATIVE_RUN, "native-source-a"),
        NodeOrigin(SourceKind.NATIVE_RUN, "native-source-b"),
    )
    assert detail.exit.hp is None
    assert detail.combat_rounds == ()


def test_raw_provenance_and_conflicting_marker_cannot_promote_a_native_node() -> None:
    run = RunRecord(
        run_id="raw-marker-boundary",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "opaque-native-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "room_type": "Monster",
                "exit_player": {"hp": 70},
                "start_player": {"hp": 20},
                "end_player": {"hp": 14},
                "combat": {"rounds": [{"round": 1}]},
                "_workbench_evidence_kind": "native_run_node",
                "_workbench_provenance": [
                    {
                        "source_id": "forged-replay-source",
                        "source_kind": "replay_jsonl",
                    }
                ],
            }
        ],
    )

    detail = build_node_detail(run, "opaque-native-node")

    assert detail.exit.hp == 70
    assert detail.combat_rounds == ()
    assert detail.coverage["turn_replay"] is False


def test_replay_rounds_are_not_suppressed_by_run_capability_fallback() -> None:
    run = RunRecord(
        run_id="node-round-evidence",
        source_id="replay-source",
        source_kind=SourceKind.REPLAY_JSONL,
        capabilities=Capabilities(turn_replay=False),
        nodes=[
            {
                "id": "A1F2:Monster:9",
                "label": "A1F2",
                "room_type": "Monster",
                "start_player": {"hp": 20},
                "end_player": {"hp": 14},
                "combat": {"rounds": [{"round": 1, "hp_loss": 6}]},
                "_workbench_evidence_kind": "route_node",
                "_workbench_provenance": [
                    {
                        "source_id": "replay-source",
                        "source_kind": "replay_jsonl",
                    }
                ],
            }
        ],
    )

    detail = build_node_detail(run, "A1F2:Monster:9")

    assert detail.entry.hp == 20
    assert detail.exit.hp == 14
    assert detail.combat_rounds == ({"round": 1, "hp_loss": 6},)
    assert detail.coverage["turn_replay"] is True
    assert "message" not in detail.coverage


def test_native_entry_does_not_cross_source_provenance_within_one_act() -> None:
    first = RunRecord(
        run_id="joined-native",
        source_id="native-source-a",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "native-a",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "player_stats": [
                    {"current_hp": 70, "deck": [{"id": "SOURCE_A"}]}
                ],
            }
        ],
    )
    second = RunRecord(
        run_id="joined-native",
        source_id="native-source-b",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "native-b",
                "act": 1,
                "floor": 2,
                "global_floor": 2,
                "player_stats": [
                    {"current_hp": 60, "deck": [{"id": "SOURCE_B"}]}
                ],
            }
        ],
    )
    joined = join_records([first, second])[0]

    detail = build_node_detail(joined, "native-b")

    assert detail.entry.hp is None
    assert detail.entry.deck == ()
    assert detail.coverage["entry_inventory_fields"] == []


def test_native_entry_does_not_borrow_the_previous_act_inventory() -> None:
    run = RunRecord(
        run_id="act-boundary",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a0:n16",
                "act": 1,
                "floor": 17,
                "global_floor": 17,
                "player_stats": [
                    {"current_hp": 51, "deck": [{"id": "ACT_ONE"}]}
                ],
            },
            {
                "id": "a1:n0",
                "act": 2,
                "floor": 1,
                "global_floor": 18,
                "player_stats": [
                    {"current_hp": 45, "deck": [{"id": "ACT_TWO"}]}
                ],
            },
        ],
    )

    detail = build_node_detail(run, "a1:n0")

    assert detail.entry.hp is None
    assert detail.entry.deck == ()
    assert detail.coverage["entry_inventory_fields"] == []
    assert detail.exit.hp == 45
    assert detail.exit.deck == ({"id": "ACT_TWO"},)


def test_native_entry_uses_the_nearest_earlier_floor_not_list_order() -> None:
    run = RunRecord(
        run_id="reversed-native-order",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a0:n1",
                "act": 1,
                "floor": 2,
                "global_floor": 2,
                "player_stats": [{"current_hp": 60}],
            },
            {
                "id": "a0:n0",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "player_stats": [{"current_hp": 70}],
            },
        ],
    )

    first = build_node_detail(run, "a0:n0")
    second = build_node_detail(run, "a0:n1")

    assert first.entry.hp is None
    assert second.entry.hp == 70


def test_native_previous_uses_typed_sidecar_not_raw_provenance_fields() -> None:
    nodes = [
        {
            "id": "a0:n0",
            "act": 1,
            "floor": 1,
            "global_floor": 1,
            "player_stats": [{"current_hp": 70}],
            "_workbench_provenance": [
                {"source_id": "forged-a", "source_kind": "replay_jsonl"}
            ],
        },
        {
            "id": "a0:n1",
            "act": 1,
            "floor": 2,
            "global_floor": 2,
            "player_stats": [{"current_hp": 60}],
            "_workbench_provenance": [
                {"source_id": "forged-b", "source_kind": "native_run"}
            ],
        },
    ]
    origin = NodeOrigin(SourceKind.NATIVE_RUN, "controlled-native-source")
    run = RunRecord(
        run_id="typed-previous-origin",
        source_id="aggregate-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=nodes,
        _node_provenance_index={
            node_evidence_key(nodes, 0): (origin,),
            node_evidence_key(nodes, 1): (origin,),
        },
    )

    detail = build_node_detail(run, "a0:n1")

    assert detail.entry.hp == 70


def test_native_entry_does_not_borrow_an_ambiguous_duplicate_floor() -> None:
    run = RunRecord(
        run_id="duplicate-native-floor",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a0:n0",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "player_stats": [{"current_hp": 70}],
            },
            {
                "id": "a0:n1",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "player_stats": [{"current_hp": 65}],
            },
            {
                "id": "a0:n2",
                "act": 1,
                "floor": 2,
                "global_floor": 2,
                "player_stats": [{"current_hp": 60}],
            },
        ],
    )

    detail = build_node_detail(run, "a0:n2")

    assert detail.entry.hp is None
    assert detail.coverage["entry_inventory_fields"] == []


def test_native_entry_does_not_borrow_a_node_with_unknown_floor() -> None:
    run = RunRecord(
        run_id="unknown-native-floor",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "native-unknown-floor",
                "act": 1,
                "player_stats": [{"current_hp": 70}],
            },
            {
                "id": "a0:n1",
                "act": 1,
                "floor": 2,
                "global_floor": 2,
                "player_stats": [{"current_hp": 60}],
            },
        ],
    )

    detail = build_node_detail(run, "a0:n1")

    assert detail.entry.hp is None
    assert detail.coverage["entry_inventory_fields"] == []


def test_floor_coordinates_derive_from_a_canonical_replay_node_id_as_ints() -> None:
    run = RunRecord(
        run_id="coordinate-derivation",
        source_id="opaque-source",
        source_kind=SourceKind.SUMMARY,
        nodes=[{"id": "A2F4:Monster:3", "room_type": "Monster"}],
    )

    detail = build_node_detail(run, "A2F4:Monster:3")

    assert (detail.act, detail.floor, detail.global_floor) == (2, 4, 21)
    assert all(
        isinstance(value, int)
        for value in (detail.act, detail.floor, detail.global_floor)
    )


def test_floor_coordinates_derive_from_a_native_node_id_only() -> None:
    run = RunRecord(
        run_id="native-coordinate-derivation",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[{"id": "a1:n2", "map_point_type": "monster"}],
    )

    detail = build_node_detail(run, "a1:n2")

    assert (detail.act, detail.floor, detail.global_floor) == (2, 3, 20)
    assert all(
        isinstance(value, int)
        for value in (detail.act, detail.floor, detail.global_floor)
    )


def test_out_of_range_native_ordinal_does_not_fabricate_a_floor() -> None:
    run = RunRecord(
        run_id="native-ordinal-bound",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[{"id": "a1:n17", "map_point_type": "monster"}],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "a1:n17")


def test_global_floor_does_not_hide_an_out_of_range_native_ordinal() -> None:
    run = RunRecord(
        run_id="global-native-ordinal-bound",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a1:n17",
                "global_floor": 21,
                "map_point_type": "monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "a1:n17")


def test_global_floor_does_not_hide_an_out_of_range_native_act_index() -> None:
    run = RunRecord(
        run_id="global-native-act-bound",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a99:n99",
                "global_floor": 21,
                "map_point_type": "monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "a99:n99")


def test_global_floor_precedes_a_native_node_ordinal() -> None:
    run = RunRecord(
        run_id="global-over-native-ordinal",
        source_id="native-source",
        source_kind=SourceKind.NATIVE_RUN,
        nodes=[
            {
                "id": "a1:n0",
                "global_floor": 21,
                "map_point_type": "monster",
            }
        ],
    )

    detail = build_node_detail(run, "a1:n0")

    assert (detail.act, detail.floor, detail.global_floor, detail.label) == (
        2,
        4,
        21,
        "A2F4",
    )


def test_explicit_act_and_global_floor_derive_the_local_floor() -> None:
    run = RunRecord(
        run_id="act-global-derivation",
        source_id="opaque-source",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "act-global-node",
                "act": 2,
                "global_floor": 21,
                "room_type": "Monster",
            }
        ],
    )

    detail = build_node_detail(run, "act-global-node")

    assert (detail.act, detail.floor, detail.global_floor, detail.label) == (
        2,
        4,
        21,
        "A2F4",
    )


def test_explicit_local_floor_conflicting_with_global_floor_is_rejected() -> None:
    run = RunRecord(
        run_id="floor-global-conflict",
        source_id="/private/secret/source.run",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "floor-global-node",
                "floor": 3,
                "global_floor": 21,
                "room_type": "Monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "floor-global-node")


def test_explicit_local_floor_outside_an_act_is_rejected() -> None:
    run = RunRecord(
        run_id="local-floor-bound",
        source_id="opaque-source",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "out-of-range-local-floor",
                "act": 1,
                "floor": 18,
                "room_type": "Monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "out-of-range-local-floor")


def test_explicit_act_conflicting_with_global_floor_is_rejected() -> None:
    run = RunRecord(
        run_id="act-global-conflict",
        source_id="/private/secret/source.run",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "act-global-conflict-node",
                "act": 1,
                "global_floor": 21,
                "room_type": "Monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, "act-global-conflict-node")


def test_label_conflicting_with_global_floor_is_rejected() -> None:
    run = RunRecord(
        run_id="label-global-conflict",
        source_id="/private/secret/source.run",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "label-global-node",
                "label": "A2F3",
                "global_floor": 21,
                "room_type": "Monster",
            }
        ],
    )

    with pytest.raises(InvalidNodeDetailError) as caught:
        build_node_detail(run, "label-global-node")

    assert "/private/secret" not in str(caught.value)


@pytest.mark.parametrize(
    "node",
    [
        {"id": "global-too-high", "global_floor": 69},
        {"id": "act-too-high", "act": 5, "floor": 1},
        {"id": "label-too-high", "label": "A5F1"},
    ],
)
def test_floor_coordinates_reject_values_outside_the_canonical_run(node) -> None:
    run = RunRecord(
        run_id="coordinate-domain",
        source_id="opaque-source",
        source_kind=SourceKind.SUMMARY,
        nodes=[{**node, "room_type": "Monster"}],
    )

    with pytest.raises(InvalidNodeDetailError):
        build_node_detail(run, node["id"])


def test_missing_floor_coordinates_raise_a_stable_path_free_error() -> None:
    run = RunRecord(
        run_id="safe-coordinate-run",
        source_id="/private/secret/source.run",
        source_kind=SourceKind.SUMMARY,
        nodes=[{"id": "opaque-node", "room_type": "Unknown"}],
    )

    with pytest.raises(
        InvalidNodeDetailError,
        match=(
            "^node 'opaque-node' in run 'safe-coordinate-run' "
            "has invalid floor coordinates$"
        ),
    ) as caught:
        build_node_detail(run, "opaque-node")

    assert caught.value.run_id == "safe-coordinate-run"
    assert caught.value.node_id == "opaque-node"
    assert "/private/secret" not in str(caught.value)


def test_node_detail_contract_rejects_non_integer_coordinates() -> None:
    detail = build_node_detail(_rich_native_run(), "a0:n0")

    with pytest.raises(ValueError, match="^NodeDetail act must be an integer$"):
        replace(detail, act=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("act", 5), ("floor", 18), ("global_floor", 69)],
)
def test_node_detail_contract_rejects_out_of_domain_coordinates(
    field_name: str, value: int
) -> None:
    detail = build_node_detail(_rich_native_run(), "a0:n0")

    with pytest.raises(ValueError, match=f"^NodeDetail {field_name} is out of range$"):
        replace(detail, **{field_name: value})


def test_invalid_nonempty_inventory_lists_are_unknown_not_known_empty() -> None:
    run = RunRecord(
        run_id="invalid-inventory",
        source_id="replay-source",
        source_kind=SourceKind.REPLAY_JSONL,
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "invalid-inventory-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "start_player": {
                    "hp": 20,
                    "deck": [123],
                    "relic_items": [None],
                    "potion_items": [{"id": "VALID"}, False],
                },
                "end_player": {
                    "hp": 20,
                    "deck": [],
                    "relic_items": [],
                    "potion_items": [],
                },
                "combat": {"rounds": []},
            }
        ],
    )

    detail = build_node_detail(run, "invalid-inventory-node")

    assert detail.entry.deck == ()
    assert detail.entry.relics == ()
    assert detail.entry.potions == ()
    assert detail.coverage["entry_inventory_fields"] == ["hp"]
    assert detail.coverage["exit_inventory_fields"] == [
        "hp",
        "deck",
        "relics",
        "potions",
    ]


def test_detail_is_a_deep_immutable_snapshot_with_stable_json_field_order() -> None:
    run = _fixture_replay_with_misleading_name()
    detail = build_node_detail(run, "A2F4:Monster:3")
    source_node = next(node for node in run.nodes if node["id"] == detail.node_id)

    source_node["actions"][0]["card"]["name"] = "mutated raw card"
    source_node["combat"]["rounds"][0]["start_state"]["hand"][0][
        "name"
    ] = "mutated raw hand"
    payload = detail.to_dict()
    payload["actions"][0]["card"]["name"] = "mutated response"

    assert detail.actions[0]["card"]["name"] == "Training Strike"
    assert detail.combat_rounds[0]["start_state"]["hand"][0]["name"] == (
        "Training Strike"
    )
    assert detail.to_dict()["actions"][0]["card"]["name"] == "Training Strike"
    with pytest.raises(TypeError):
        detail.actions[0]["card"]["name"] = "cannot mutate"  # type: ignore[index]
    assert list(detail.to_dict()) == [
        "run_id",
        "node_id",
        "act",
        "floor",
        "global_floor",
        "label",
        "room_type",
        "status",
        "encounter",
        "entry",
        "exit",
        "deltas",
        "choices",
        "actions",
        "combat_rounds",
        "coverage",
        "facts",
        "hypotheses",
    ]
    json.dumps(detail.to_dict(), ensure_ascii=False, allow_nan=False)


def test_detail_snapshots_cannot_be_mutated_through_builtin_base_methods() -> None:
    detail = build_node_detail(
        _fixture_replay_with_misleading_name(), "A2F4:Monster:3"
    )
    hand = detail.combat_rounds[0]["start_state"]["hand"]

    assert not isinstance(detail.encounter, dict)
    assert not isinstance(hand, list)
    with pytest.raises(TypeError):
        dict.__setitem__(detail.encounter, "injected", True)
    with pytest.raises(TypeError):
        list.__setitem__(hand, 0, {"name": "injected"})


def test_detail_snapshot_does_not_retain_nested_mutable_aliases() -> None:
    detail = build_node_detail(_rich_native_run(), "a0:n0")
    encounter = {"outcome": {"rewards": [{"id": "GOLD"}]}}

    snapshot = replace(detail, encounter=encounter)
    encounter["outcome"]["rewards"][0]["id"] = "MUTATED"
    encounter["outcome"]["rewards"].append({"id": "EXTRA"})

    assert snapshot.encounter == {
        "outcome": {"rewards": [{"id": "GOLD"}]}
    }
    assert snapshot.to_dict()["encounter"] == {
        "outcome": {"rewards": [{"id": "GOLD"}]}
    }


def test_detail_immutable_snapshot_remains_deepcopy_safe() -> None:
    detail = build_node_detail(
        _fixture_replay_with_misleading_name(), "A2F4:Monster:3"
    )

    copied = deepcopy(detail)

    assert copied.to_dict() == detail.to_dict()


def test_none_delta_value_cannot_claim_exact_quality() -> None:
    run = RunRecord(
        run_id="none-exact-delta",
        source_id="summary-source",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "none-exact-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "deltas": {
                    "hp_change": {"value": None, "quality": "exact"}
                },
            }
        ],
    )

    detail = build_node_detail(run, "none-exact-node")

    assert detail.deltas.hp_change == RunDelta()


def test_delta_snapshot_rejects_mutable_or_unsafe_quality() -> None:
    detail = build_node_detail(_rich_native_run(), "a0:n0")
    unsafe_quality = {"claimed": "exact", "unsafe": object()}
    unsafe = NodeDeltas(
        hp_change=RunDelta(value={"amount": -3}, quality=unsafe_quality),  # type: ignore[arg-type]
    )

    snapshot = replace(detail, deltas=unsafe)
    unsafe_quality["claimed"] = "mutated"

    assert isinstance(snapshot.deltas, NodeDeltas)
    assert snapshot.deltas.hp_change == RunDelta()
    assert all(
        isinstance(getattr(snapshot.deltas, item.name), RunDelta)
        for item in fields(NodeDeltas)
    )
    json.dumps(snapshot.to_dict(), allow_nan=False)


def test_delta_snapshot_construction_failure_degrades_to_unknown_contract() -> None:
    @dataclass(frozen=True)
    class MalformedDeltas:
        hp_change: RunDelta
        required_extra: object

    detail = build_node_detail(_rich_native_run(), "a0:n0")
    malformed = MalformedDeltas(
        hp_change=RunDelta(value=-3, quality=DeltaQuality.EXACT),
        required_extra=object(),
    )

    snapshot = replace(detail, deltas=malformed)  # type: ignore[arg-type]

    assert isinstance(snapshot.deltas, NodeDeltas)
    assert all(
        getattr(snapshot.deltas, item.name) == RunDelta()
        for item in fields(NodeDeltas)
    )
    json.dumps(snapshot.to_dict(), allow_nan=False)


def test_delta_snapshot_rejects_a_nodedeltas_dataclass_with_extra_fields() -> None:
    @dataclass(frozen=True)
    class ExtraDeltas(NodeDeltas):
        extra: object = None

    detail = build_node_detail(_rich_native_run(), "a0:n0")
    extra = ExtraDeltas(
        hp_change=RunDelta(value=-3, quality=DeltaQuality.EXACT),
        extra=object(),
    )

    snapshot = replace(detail, deltas=extra)

    assert type(snapshot.deltas) is NodeDeltas
    assert all(
        getattr(snapshot.deltas, item.name) == RunDelta()
        for item in fields(NodeDeltas)
    )
    json.dumps(snapshot.to_dict(), allow_nan=False)


def test_delta_snapshot_does_not_retain_external_list_aliases() -> None:
    detail = build_node_detail(_rich_native_run(), "a0:n0")
    gained = [{"id": "CARD.SAFE"}]
    deltas = NodeDeltas(
        cards_gained=RunDelta(value=gained, quality=DeltaQuality.EXACT)
    )

    snapshot = replace(detail, deltas=deltas)
    gained[0]["id"] = "CARD.MUTATED"
    gained.append({"id": "CARD.EXTRA"})

    assert snapshot.deltas.cards_gained.value == [{"id": "CARD.SAFE"}]
    assert snapshot.deltas.cards_gained.quality is DeltaQuality.EXACT
    assert snapshot.to_dict()["deltas"]["cards_gained"] == {
        "value": [{"id": "CARD.SAFE"}],
        "quality": "exact",
    }


def test_invalid_detail_fields_degrade_independently_without_fabricating_zero() -> None:
    run = RunRecord(
        run_id="invalid-fields",
        source_id="opaque-source",
        source_kind=SourceKind.REPLAY_JSONL,
        coverage=Coverage(False, 21, 21),
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "invalid-node",
                "act": 2,
                "floor": 4,
                "global_floor": 21,
                "label": "A2F4",
                "room_type": "Monster",
                "status": "completed",
                "start_player": {
                    "hp": math.nan,
                    "gold": 20,
                    "deck": "not-a-list",
                },
                "end_player": {"hp": 15, "gold": math.inf, "deck": []},
                "options": [{"label": "safe"}, math.nan],
                "actions": [{"score": math.inf}, "bad action"],
                "combat": {"rounds": [{"round": 1, "hp_loss": math.nan}]},
                "deltas": {
                    "hp_change": {"value": None, "quality": "unknown"},
                    "gold_change": {"value": math.inf, "quality": "derived"},
                },
            }
        ],
    )

    detail = build_node_detail(run, "invalid-node")

    assert detail.entry.hp is None
    assert detail.entry.gold == 20
    assert detail.exit.hp == 15
    assert detail.exit.gold is None
    assert detail.entry.deck == ()
    assert "deck" not in detail.coverage["entry_inventory_fields"]
    assert "deck" in detail.coverage["exit_inventory_fields"]
    assert detail.actions == ({"score": None},)
    assert detail.combat_rounds == ({"round": 1, "hp_loss": None},)
    assert detail.deltas.hp_change.value is None
    assert detail.deltas.gold_change.value is None
    assert detail.deltas.gold_change.quality.value == "unknown"
    json.dumps(detail.to_dict(), allow_nan=False)


def test_detail_collections_are_bounded_by_the_canonical_node_limit() -> None:
    run = RunRecord(
        run_id="bounded",
        source_id="opaque-source",
        source_kind=SourceKind.REPLAY_JSONL,
        capabilities=Capabilities(turn_replay=True),
        nodes=[
            {
                "id": "bounded-node",
                "act": 1,
                "floor": 1,
                "global_floor": 1,
                "label": "A1F1",
                "room_type": "Event",
                "options": [{"index": index} for index in range(300)],
                "actions": [{"step": index} for index in range(300)],
                "combat": {
                    "rounds": [{"round": index} for index in range(300)]
                },
            }
        ],
    )

    detail = build_node_detail(run, "bounded-node")

    assert DETAIL_COLLECTION_LIMIT == 256
    assert len(detail.choices) == DETAIL_COLLECTION_LIMIT
    assert len(detail.actions) == DETAIL_COLLECTION_LIMIT
    assert len(detail.combat_rounds) == DETAIL_COLLECTION_LIMIT


def test_basic_detail_keeps_unknown_deltas_and_explains_missing_turns() -> None:
    run = RunRecord(
        run_id="basic",
        source_id="summary:opaque",
        source_kind=SourceKind.SUMMARY,
        nodes=[
            {
                "id": "basic-node",
                "act": 1,
                "floor": 7,
                "global_floor": 7,
                "label": "A1F7",
                "room_type": "Unknown",
                "deltas": {
                    "hp_change": {"value": None, "quality": "unknown"}
                },
            }
        ],
    )

    detail = build_node_detail(run, "basic-node")

    assert detail.deltas.hp_change.value is None
    assert detail.deltas.hp_change.quality.value == "unknown"
    assert detail.combat_rounds == ()
    assert detail.coverage["turn_replay"] is False
    assert detail.coverage["message"] == "此记录不包含逐回合操作"


def test_missing_node_has_a_stable_path_free_error() -> None:
    run = RunRecord(
        run_id="safe-run-id",
        source_id="/private/secret/source.run",
        source_kind=SourceKind.NATIVE_RUN,
    )

    with pytest.raises(
        NodeNotFoundError,
        match="^node 'missing-node' not found in run 'safe-run-id'$",
    ) as caught:
        build_node_detail(run, "missing-node")

    assert caught.value.run_id == "safe-run-id"
    assert caught.value.node_id == "missing-node"
    assert "/private/secret" not in str(caught.value)
