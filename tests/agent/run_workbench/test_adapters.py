import math
import json
from pathlib import Path

import pytest

from agent.run_progress_viewer import parse_game_progress
from agent.run_workbench.adapters import adapt_path, adapt_records
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
    assert run.metadata.is_multiplayer is None
    assert run.capabilities.visited_route is True
    assert run.capabilities.node_rewards is True
    assert run.capabilities.turn_replay is False
    assert run.nodes[0]["floor"] == 1
    assert run.nodes[0]["id"] == "a0:n0"
    assert run.nodes[0]["deltas"]["hp_change"] == {
        "value": None,
        "quality": "unknown",
    }


@pytest.mark.parametrize("is_multiplayer", [False, True])
def test_native_run_preserves_exact_multiplayer_flag(
    tmp_path: Path, is_multiplayer: bool
) -> None:
    path = tmp_path / "multiplayer.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "is_multiplayer": is_multiplayer,
                "map_point_history": [{"map_point_type": "ancient"}],
            }
        ),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert run.metadata.is_multiplayer is is_multiplayer
    assert run.to_dict()["metadata"]["is_multiplayer"] is is_multiplayer


def test_native_run_preserves_exact_string_modifiers(tmp_path: Path) -> None:
    path = tmp_path / "modifiers.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "modifiers": ["MODIFIER.BIG_GAME_HUNTER", "MODIFIER.DRAFT"],
                "map_point_history": [{"map_point_type": "ancient"}],
            }
        ),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert run.metadata.modifiers == (
        "MODIFIER.BIG_GAME_HUNTER",
        "MODIFIER.DRAFT",
    )
    assert run.to_dict()["metadata"]["modifiers"] == [
        "MODIFIER.BIG_GAME_HUNTER",
        "MODIFIER.DRAFT",
    ]


def test_native_run_rejects_a_mixed_modifier_list_conservatively(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-modifiers.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "modifiers": ["MODIFIER.BIG_GAME_HUNTER", 7],
                "map_point_history": [{"map_point_type": "ancient"}],
            }
        ),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert run.metadata.modifiers == ()


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


def test_replay_nodes_preserve_injected_parser_room_ids_and_add_deltas() -> None:
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
    assert adapted.runs[0].nodes[0]["id"] == "room-1"
    assert adapted.runs[0].nodes[0]["global_floor"] == 3
    assert adapted.runs[0].nodes[0]["deltas"]["hp_change"] == {
        "value": None,
        "quality": "unknown",
    }
    assert adapted.runs[0].metadata.seed == "parser-seed"
    assert adapted.runs[0].metadata.character == "Parser"


def test_native_nodes_receive_stable_ids_retain_details_and_exact_deltas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "detailed.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "acts": [{"act": 1}],
                "map_point_history": [
                    {
                        "act_index": 0,
                        "map_point_type": "monster",
                        "rooms": [
                            {
                                "model_id": "ENCOUNTER.JAW_WORM",
                                "monster_ids": ["MONSTER.JAW_WORM"],
                            }
                        ],
                        "choices": [{"id": "left"}],
                        "player_stats": [
                            {
                                "current_hp": 75,
                                "max_hp": 80,
                                "current_gold": 10,
                                "damage_taken": 5,
                                "hp_healed": 0,
                            }
                        ],
                    },
                    {
                        "act_index": 0,
                        "map_point_type": "shop",
                        "rooms": [{"model_id": "ROOM.SHOP"}],
                        "monster_ids": [],
                        "choices": [{"id": "buy-card"}],
                        "player_stats": [
                            {
                                "current_hp": 75,
                                "max_hp": 80,
                                "current_gold": 2,
                                "damage_taken": 0,
                                "hp_healed": 0,
                                "cards_gained": [{"id": "BASH"}],
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    nodes = adapt_path(path).runs[0].nodes

    assert [node["id"] for node in nodes] == ["a0:n0", "a0:n1"]
    assert nodes[1]["map_point_type"] == "shop"
    assert nodes[1]["rooms"] == [{"model_id": "ROOM.SHOP"}]
    assert nodes[0]["rooms"][0] == {
        "model_id": "ENCOUNTER.JAW_WORM",
        "monster_ids": ["MONSTER.JAW_WORM"],
    }
    assert nodes[1]["choices"] == [{"id": "buy-card"}]
    assert nodes[1]["deltas"]["gold_change"] == {
        "value": -8,
        "quality": "derived",
    }
    assert nodes[1]["deltas"]["cards_gained"] == {
        "value": [{"id": "BASH"}],
        "quality": "exact",
    }


def test_native_nested_act_history_uses_act_local_stable_node_ids(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested-acts.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "acts": [{"act": 1}, {"act": 2}],
                "map_point_history": [
                    [
                        {
                            "map_point_type": "monster",
                            "player_stats": [{"current_hp": 70}],
                        },
                        {
                            "map_point_type": "shop",
                            "player_stats": [{"current_hp": 70}],
                        },
                    ],
                    [
                        {
                            "map_point_type": "ancient",
                            "player_stats": [{"current_hp": 68}],
                        }
                    ],
                ],
            }
        ),
        encoding="utf-8",
    )

    nodes = adapt_path(path).runs[0].nodes

    assert [node["id"] for node in nodes] == ["a0:n0", "a0:n1", "a1:n0"]
    assert nodes[2]["map_point_type"] == "ancient"
    assert nodes[2]["deltas"]["hp_change"] == {
        "value": -2,
        "quality": "derived",
    }


def test_native_mixed_flat_and_nested_history_retains_all_nodes_and_coverage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mixed-history.run"
    path.write_text(
        json.dumps(
            {
                "players": [{"character": "IRONCLAD"}],
                "acts": [{"act": 1}, {"act": 2}],
                "map_point_history": [
                    {
                        "act_index": 0,
                        "floor": 1,
                        "player_stats": [{"current_hp": 80}],
                    },
                    [
                        {
                            "global_floor": 18,
                            "player_stats": [{"current_hp": 75}],
                        }
                    ],
                ],
            }
        ),
        encoding="utf-8",
    )

    run = adapt_path(path).runs[0]

    assert [node["id"] for node in run.nodes] == ["a0:n0", "a1:n0"]
    assert [node.get("global_floor", node.get("floor")) for node in run.nodes] == [
        1,
        18,
    ]
    assert run.coverage.first_recorded_floor == 1
    assert run.coverage.last_recorded_floor == 18


def test_replay_room_deltas_are_derived_within_each_room() -> None:
    rooms = [
        {
            "id": "legacy-room-1",
            "global_floor": 1,
            "start_player": {
                "hp": 80,
                "max_hp": 80,
                "gold": 0,
                "deck": [{"id": "STRIKE"}],
                "relic_items": [],
                "potion_items": [],
            },
            "end_player": {
                "hp": 70,
                "max_hp": 80,
                "gold": 10,
                "deck": [{"id": "STRIKE"}],
                "relic_items": [],
                "potion_items": [],
            },
        },
        {
            "id": "legacy-room-2",
            "global_floor": 2,
            "start_player": {
                "hp": 60,
                "max_hp": 80,
                "gold": 10,
                "deck": [{"id": "STRIKE"}],
                "relic_items": [],
                "potion_items": [],
            },
            "end_player": {
                "hp": 55,
                "max_hp": 80,
                "gold": 22,
                "deck": [{"id": "STRIKE"}, {"id": "BASH"}],
                "relic_items": [{"id": "ANCHOR"}],
                "potion_items": [],
            },
        },
    ]

    run = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": rooms,
        },
    ).runs[0]

    assert [node["id"] for node in run.nodes] == [
        "legacy-room-1",
        "legacy-room-2",
    ]
    assert run.nodes[0]["deltas"]["hp_change"] == {
        "value": -10,
        "quality": "derived",
    }
    assert run.nodes[1]["deltas"]["hp_change"] == {
        "value": -5,
        "quality": "derived",
    }
    assert run.nodes[1]["deltas"]["cards_gained"] == {
        "value": [{"id": "BASH"}],
        "quality": "derived",
    }
    assert run.replay_by_node["legacy-room-2"]["deltas"] == run.nodes[1]["deltas"]


def test_replay_room_with_missing_start_snapshot_has_unknown_changes() -> None:
    rooms = [
        {
            "id": "legacy-room-1",
            "end_player": {"hp": 70, "gold": 10},
        },
        {
            "id": "legacy-room-2",
            "end_player": {"hp": 65, "gold": 20},
        },
    ]

    run = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": rooms,
        },
    ).runs[0]

    assert run.nodes[1]["deltas"]["hp_change"] == {
        "value": None,
        "quality": "unknown",
    }
    assert run.nodes[1]["deltas"]["gold_change"] == {
        "value": None,
        "quality": "unknown",
    }


def test_replay_empty_player_objects_use_room_level_phase_snapshots() -> None:
    room = {
        "id": "room-with-fallbacks",
        "start_player": {},
        "end_player": {},
        "start_hp": 20,
        "end_hp": 15,
        "start_max_hp": 70,
        "end_max_hp": 72,
        "start_gold": 5,
        "end_gold": 7,
        "start_deck": [],
        "end_deck": [{"id": "CARD.BASH"}],
        "start_relic_items": [],
        "end_relic_items": [],
        "start_potion_items": [],
        "end_potion_items": [],
    }

    run = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": [room],
        },
    ).runs[0]
    deltas = run.nodes[0]["deltas"]

    assert deltas["hp_change"] == {"value": -5, "quality": "derived"}
    assert deltas["max_hp_change"] == {"value": 2, "quality": "derived"}
    assert deltas["gold_change"] == {"value": 2, "quality": "derived"}
    assert deltas["cards_gained"] == {
        "value": [{"id": "CARD.BASH"}],
        "quality": "derived",
    }
    assert deltas["relics_gained"] == {"value": [], "quality": "derived"}
    assert deltas["potions_gained"] == {"value": [], "quality": "derived"}


def test_replay_empty_player_objects_without_fallbacks_stay_unknown() -> None:
    run = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": [
                {"id": "empty-room", "start_player": {}, "end_player": {}}
            ],
        },
    ).runs[0]
    deltas = run.nodes[0]["deltas"]

    assert deltas["hp_change"] == {"value": None, "quality": "unknown"}
    assert deltas["cards_gained"] == {"value": None, "quality": "unknown"}
    assert deltas["relics_gained"] == {"value": None, "quality": "unknown"}


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_nonfinite_delta_measurements_do_not_drop_native_or_replay_runs(
    invalid: float,
) -> None:
    native = adapt_records(
        "nonfinite.run",
        [
            {
                "players": [{"character": "IRONCLAD"}],
                "map_point_history": [
                    {
                        "player_stats": [
                            {"current_hp": invalid, "damage_taken": invalid}
                        ]
                    }
                ],
            }
        ],
    )
    replay = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": [
                {
                    "id": "nonfinite-room",
                    "start_player": {"hp": 20},
                    "end_player": {"hp": invalid},
                }
            ],
        },
    )

    assert len(native.runs) == 1
    assert native.runs[0].nodes[0]["deltas"]["hp_after"] == {
        "value": None,
        "quality": "unknown",
    }
    assert native.runs[0].nodes[0]["deltas"]["damage_taken"] == {
        "value": None,
        "quality": "unknown",
    }
    assert len(replay.runs) == 1
    assert replay.runs[0].nodes[0]["deltas"]["hp_change"] == {
        "value": None,
        "quality": "unknown",
    }


def test_nested_nonfinite_delta_lists_do_not_drop_native_or_replay_runs() -> None:
    native = adapt_records(
        "nested-nonfinite.run",
        [
            {
                "players": [{"character": "IRONCLAD"}],
                "map_point_history": [
                    {
                        "player_stats": [
                            {
                                "cards_gained": [
                                    {"id": "CARD.X", "score": math.nan}
                                ],
                                "potion_choices": [
                                    {
                                        "choice": "POTION.X",
                                        "was_picked": True,
                                        "score": math.inf,
                                    }
                                ],
                            }
                        ]
                    }
                ],
            }
        ],
    )
    replay = adapt_path(
        FIXTURES / "replay.jsonl",
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": [
                {
                    "id": "nested-nonfinite-room",
                    "start_player": {"deck": []},
                    "end_player": {
                        "deck": [{"id": "CARD.X", "score": math.inf}]
                    },
                }
            ],
        },
    )

    assert len(native.runs) == 1
    assert native.runs[0].nodes[0]["deltas"]["cards_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert native.runs[0].nodes[0]["deltas"]["potions_gained"] == {
        "value": None,
        "quality": "unknown",
    }
    assert len(replay.runs) == 1
    assert replay.runs[0].nodes[0]["deltas"]["cards_gained"] == {
        "value": None,
        "quality": "unknown",
    }


def test_replay_top_level_run_id_wins_over_nested_conflict_with_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "conflicting-id.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "action",
                        "run_id": "top-level",
                        "data": {"cmd": "start_run", "run_id": "nested"},
                    }
                ),
                json.dumps(
                    {
                        "type": "state",
                        "data": {
                            "run_id": "nested",
                            "context": {"act": 1, "floor": 1},
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    run = adapt_path(
        path,
        replay_parser=lambda entries, source_name=None: {
            "summary": {},
            "rooms": [],
        },
    ).runs[0]

    assert run.run_id == "top-level"
    assert any(
        "conflicting replay run_id" in warning
        and "top-level" in warning
        and "nested" in warning
        for warning in run.warnings
    )


def test_replay_start_metadata_requires_integer_ascension(tmp_path: Path) -> None:
    path = tmp_path / "typed-ascension.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "action",
                        "data": {
                            "cmd": "start_run",
                            "run_id": "typed-run",
                            "ascension": 3.0,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "state",
                        "status": "dead",
                        "data": {"run_id": "typed-run"},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    run = adapt_path(path, replay_parser=parse_game_progress).runs[0]

    assert run.metadata.ascension is None


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
