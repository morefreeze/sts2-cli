import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from agent.run_progress_viewer import parse_game_progress
from agent.run_workbench.adapters import adapt_path, adapt_records
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
    InventorySnapshot,
    NodeDetail,
    RunRecord,
    SourceKind,
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

    native_detail = build_node_detail(joined, "native-node")
    replay_detail = build_node_detail(joined, "A1F2:Monster:7")

    assert joined.source_kind is SourceKind.NATIVE_RUN
    assert native_detail.exit.hp == 77
    assert native_detail.combat_rounds == ()
    assert replay_detail.entry.hp == 20
    assert replay_detail.exit.hp == 14
    assert len(replay_detail.combat_rounds) == 1
    assert replay_detail.coverage["turn_replay"] is True


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
