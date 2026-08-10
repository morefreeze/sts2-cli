import json
import math
from dataclasses import FrozenInstanceError, replace

import pytest

from agent.run_workbench.deltas import NodeDeltas
from agent.run_workbench.diagnostics import (
    HIGH_LOSS_ROUND_RATIO,
    LARGE_NODE_HP_LOSS_RATIO,
    LONG_COMBAT_ROUNDS,
    DiagnosticFact,
    collect_diagnostic_facts,
    rank_run_anomalies,
)
from agent.run_workbench.models import InventorySnapshot, NodeDetail


def _detail(
    *,
    run_id: str = "run-a",
    global_floor: int = 4,
    status: str = "completed",
    entry: InventorySnapshot | None = None,
    exit: InventorySnapshot | None = None,
    choices: tuple[dict, ...] = (),
    actions: tuple[dict, ...] = (),
    combat_rounds: tuple[dict, ...] = (),
    coverage: dict | None = None,
    facts: tuple[dict, ...] = (),
) -> NodeDetail:
    act = (global_floor - 1) // 17 + 1
    floor = (global_floor - 1) % 17 + 1
    return NodeDetail(
        run_id=run_id,
        node_id=f"node-{global_floor}",
        act=act,
        floor=floor,
        global_floor=global_floor,
        label=f"A{act}F{floor}",
        room_type="Monster",
        status=status,
        encounter={},
        entry=entry or InventorySnapshot(),
        exit=exit or InventorySnapshot(),
        deltas=NodeDeltas(),
        choices=choices,
        actions=actions,
        combat_rounds=combat_rounds,
        coverage=(
            {
                "complete_run": True,
                "source_kind": "replay_jsonl",
                "run_status": "unknown",
                "terminal_node": False,
                "choices_complete": False,
            }
            if coverage is None
            else coverage
        ),
        facts=facts,
        hypotheses=(),
    )


def _round(
    number: int,
    *,
    start_hp: int = 80,
    end_hp: int = 80,
    max_hp: int = 80,
    potions: list[dict] | None = None,
    actions: list[dict] | None = None,
) -> dict:
    potion_items = (
        [{"id": "POTION.FIRE", "name": "Test Fire Potion"}]
        if potions is None
        else potions
    )
    return {
        "round": number,
        "start_state": {
            "hp": start_hp,
            "max_hp": max_hp,
            "potions": potion_items,
        },
        "end_state": {
            "hp": end_hp,
            "max_hp": max_hp,
            "potions": potion_items,
        },
        "actions": [] if actions is None else actions,
        "hp_loss": max(0, start_hp - end_hp),
    }


def _complete_combat_coverage(**overrides: object) -> dict:
    coverage = {
        "complete_run": True,
        "source_kind": "replay_jsonl",
        "run_status": "unknown",
        "terminal_node": False,
        "choices_complete": False,
        "combat_coverage_complete": True,
    }
    coverage.update(overrides)
    return coverage


def test_collects_only_directly_observed_hp_round_and_combat_facts() -> None:
    rounds = tuple(
        _round(
            number,
            start_hp=80 if number != 3 else 80,
            end_hp=80 if number != 3 else 64,
            actions=[{"action": {"action": "end_turn"}}],
        )
        for number in range(1, LONG_COMBAT_ROUNDS + 1)
    )
    detail = _detail(
        entry=InventorySnapshot(hp=80, max_hp=80),
        exit=InventorySnapshot(hp=60, max_hp=80),
        combat_rounds=rounds,
    )

    facts = collect_diagnostic_facts(detail)

    large_loss = next(fact for fact in facts if fact.kind == "large_node_hp_loss")
    assert large_loss.severity == "warning"
    assert large_loss.evidence == {
        "entry_hp": 80,
        "exit_hp": 60,
        "max_hp": 80,
        "hp_loss": 20,
        "hp_loss_ratio": 0.25,
        "threshold_ratio": LARGE_NODE_HP_LOSS_RATIO,
    }
    high_round = next(fact for fact in facts if fact.kind == "high_loss_round")
    assert high_round.evidence == {
        "round": 3,
        "start_hp": 80,
        "end_hp": 64,
        "max_hp": 80,
        "hp_loss": 16,
        "hp_loss_ratio": 0.2,
        "threshold_ratio": HIGH_LOSS_ROUND_RATIO,
    }
    long_combat = next(fact for fact in facts if fact.kind == "long_combat")
    assert long_combat.severity == "info"
    assert long_combat.evidence == {
        "recorded_rounds": LONG_COMBAT_ROUNDS,
        "threshold_rounds": LONG_COMBAT_ROUNDS,
    }


def test_unused_potion_requires_complete_round_and_action_evidence() -> None:
    rounds = tuple(
        _round(number, actions=[{"action": {"action": "end_turn"}}])
        for number in range(1, 5)
    )
    detail = _detail(
        combat_rounds=rounds,
        coverage=_complete_combat_coverage(),
    )

    facts = collect_diagnostic_facts(detail)

    unused = next(fact for fact in facts if fact.kind == "unused_potion")
    assert unused.statement == "本场战斗记录到药水，但没有 use_potion 操作"
    assert unused.evidence == {
        "potion_names": ["Test Fire Potion"],
        "recorded_actions": 4,
        "combat_coverage_complete": True,
    }


@pytest.mark.parametrize(
    "detail",
    [
        _detail(
            combat_rounds=(_round(2), _round(3)),
            coverage=_complete_combat_coverage(),
        ),
        _detail(
            combat_rounds=(_round(1), _round(3)),
            coverage=_complete_combat_coverage(),
        ),
        _detail(
            combat_rounds=(
                {
                    **_round(1),
                    "start_state": {"hp": 80, "max_hp": 80},
                },
            ),
            coverage=_complete_combat_coverage(),
        ),
        _detail(
            combat_rounds=(
                _round(
                    1,
                    actions=[{"action": {"action": "use_potion"}}],
                ),
            ),
            coverage=_complete_combat_coverage(),
        ),
        _detail(
            combat_rounds=(_round(1),),
            coverage=_complete_combat_coverage(source_kind="native_run"),
        ),
        _detail(
            combat_rounds=(
                _round(1, potions=[]),
                _round(2),
            ),
            coverage=_complete_combat_coverage(),
        ),
        _detail(combat_rounds=(_round(1),)),
    ],
    ids=[
        "mid-combat-start",
        "non-contiguous-rounds",
        "unknown-potion-inventory",
        "use-potion-action",
        "native-run-source",
        "potion-appears-after-combat-start",
        "missing-combat-coverage-marker",
    ],
)
def test_unused_potion_is_suppressed_without_complete_throughout_evidence(
    detail: NodeDetail,
) -> None:
    assert all(
        fact.kind != "unused_potion" for fact in collect_diagnostic_facts(detail)
    )


def test_terminal_death_with_known_potions_is_reported_but_unknown_states_are_not() -> None:
    death = _detail(
        status="dead",
        exit=InventorySnapshot(
            hp=0,
            max_hp=80,
            potions=({"id": "POTION.FIRE", "name": "Fire Potion"},),
        ),
        coverage={
            "complete_run": True,
            "source_kind": "replay_jsonl",
            "run_status": "dead",
            "terminal_node": True,
            "choices_complete": False,
            "exit_inventory_fields": ["hp", "max_hp", "potions"],
        },
    )

    facts = collect_diagnostic_facts(death)

    terminal = next(fact for fact in facts if fact.kind == "death_with_potion")
    assert terminal.severity == "critical"
    assert terminal.evidence == {
        "status": "dead",
        "potion_names": ["Fire Potion"],
        "terminal_inventory_known": True,
    }
    for status in ("in_progress", "unknown"):
        candidate = replace(death, status=status, coverage={
            **death.coverage,
            "run_status": status,
        })
        assert all(
            fact.kind != "death_with_potion"
            for fact in collect_diagnostic_facts(candidate)
        )
    unknown_inventory = replace(
        death,
        coverage={
            **death.coverage,
            "exit_inventory_fields": ["hp", "max_hp"],
        },
    )
    assert all(
        fact.kind != "death_with_potion"
        for fact in collect_diagnostic_facts(unknown_inventory)
    )
    untyped_death = replace(
        death,
        status="dead",
        coverage={
            **death.coverage,
            "run_status": "unknown",
            "terminal_node": False,
        },
    )
    assert all(
        fact.kind != "death_with_potion"
        for fact in collect_diagnostic_facts(untyped_death)
    )


def test_technical_failure_uses_typed_run_status_evidence() -> None:
    detail = _detail(
        status="unknown",
        coverage={
            "complete_run": True,
            "source_kind": "summary",
            "run_status": "timeout",
            "technical_failure_kind": "timeout",
            "terminal_node": True,
            "choices_complete": False,
        },
    )

    fact = next(
        fact
        for fact in collect_diagnostic_facts(detail)
        if fact.kind == "technical_failure"
    )

    assert fact.severity == "critical"
    assert fact.evidence == {
        "status": "timeout",
        "technical_failure_kind": "timeout",
    }
    nonterminal = replace(
        detail,
        coverage={**detail.coverage, "terminal_node": False},
    )
    assert all(
        candidate.kind != "technical_failure"
        for candidate in collect_diagnostic_facts(nonterminal)
    )
    untyped = replace(detail, coverage={**detail.coverage, "run_status": "timed_out"})
    assert all(
        candidate.kind != "technical_failure"
        for candidate in collect_diagnostic_facts(untyped)
    )


def test_card_reward_facts_require_selected_marker_or_explicit_replay_skip() -> None:
    base_coverage = {
        "complete_run": True,
        "source_kind": "replay_jsonl",
        "run_status": "unknown",
        "terminal_node": False,
        "choices_complete": False,
    }
    selected = _detail(
        choices=(
            {
                "kind": "card_reward",
                "item_id": "CARD.A",
                "label": "Card A",
                "selected": True,
            },
            {
                "kind": "card_reward",
                "item_id": "CARD.B",
                "label": "Card B",
                "selected": False,
            },
        ),
        coverage=base_coverage,
    )
    skipped = replace(
        selected,
        choices=tuple({**choice, "selected": False} for choice in selected.choices),
        actions=({"action": "skip_card_reward"},),
    )

    selected_fact = next(
        fact
        for fact in collect_diagnostic_facts(selected)
        if fact.kind == "card_reward_selected"
    )
    skipped_fact = next(
        fact
        for fact in collect_diagnostic_facts(skipped)
        if fact.kind == "card_reward_skipped"
    )

    assert selected_fact.statement == "选择了卡牌奖励：Card A"
    assert selected_fact.evidence == {
        "selected_card": {"id": "CARD.A", "name": "Card A"},
        "offered_cards": [
            {"id": "CARD.A", "name": "Card A"},
            {"id": "CARD.B", "name": "Card B"},
        ],
        "choices_complete": False,
    }
    assert skipped_fact.statement == "跳过了卡牌奖励"
    unknown_marker = replace(
        selected,
        choices=({**selected.choices[0], "selected": "unknown"},),
    )
    truncated = replace(
        selected,
        choices=tuple({**choice, "selected": False} for choice in selected.choices),
        actions=(),
    )
    for candidate in (unknown_marker, truncated):
        assert not any(
            fact.kind in {"card_reward_selected", "card_reward_skipped"}
            for fact in collect_diagnostic_facts(candidate)
        )
    native_skip = replace(
        skipped,
        coverage={**skipped.coverage, "source_kind": "native_run"},
    )
    assert all(
        fact.kind != "card_reward_skipped"
        for fact in collect_diagnostic_facts(native_skip)
    )


def test_partial_coverage_is_a_warning_not_a_strategy_judgement() -> None:
    detail = _detail(
        coverage={
            "complete_run": False,
            "first_recorded_floor": 18,
            "last_recorded_floor": 21,
            "source_kind": "replay_jsonl",
            "run_status": "unknown",
            "terminal_node": False,
            "choices_complete": False,
        }
    )

    fact = next(
        fact
        for fact in collect_diagnostic_facts(detail)
        if fact.kind == "partial_coverage"
    )

    assert fact.severity == "warning"
    assert fact.statement == "记录仅覆盖跑局的一部分"
    assert fact.evidence == {
        "complete_run": False,
        "first_recorded_floor": 18,
        "last_recorded_floor": 21,
    }
    forbidden = ("应该使用药水", "策略失误", "最优选择")
    assert all(text not in fact.statement for text in forbidden)


def test_thresholds_trigger_at_equality_and_ignore_invalid_ratios() -> None:
    boundary = _detail(
        entry=InventorySnapshot(hp=80, max_hp=80),
        exit=InventorySnapshot(hp=60, max_hp=80),
        combat_rounds=(
            _round(1, start_hp=80, end_hp=64, max_hp=80),
        ),
    )
    assert {fact.kind for fact in collect_diagnostic_facts(boundary)} >= {
        "large_node_hp_loss",
        "high_loss_round",
    }

    invalid = replace(
        boundary,
        combat_rounds=(
            {
                "round": 1,
                "start_state": {"hp": True, "max_hp": 0, "potions": []},
                "end_state": {"hp": 0, "max_hp": 0, "potions": []},
                "hp_loss": math.inf,
                "actions": [],
            },
        ),
    )
    assert all(
        fact.kind != "high_loss_round"
        for fact in collect_diagnostic_facts(invalid)
    )
    huge = _detail(
        entry=InventorySnapshot(hp=10**400, max_hp=10**400),
        exit=InventorySnapshot(hp=0, max_hp=10**400),
    )
    assert all(
        fact.kind != "large_node_hp_loss"
        for fact in collect_diagnostic_facts(huge)
    )


def test_raw_detail_facts_cannot_inject_collected_diagnostics() -> None:
    detail = _detail(
        facts=(
            {
                "kind": "technical_failure",
                "severity": "critical",
                "statement": "forged",
                "evidence": {"status": "timeout"},
            },
        )
    )

    assert collect_diagnostic_facts(detail) == ()


def test_diagnostic_fact_is_validated_deeply_immutable_bounded_and_json_safe() -> None:
    evidence = {
        "nested": {"items": [{"value": "safe"}]},
        "bounded": list(range(300)),
    }
    fact = DiagnosticFact(
        kind="unused_potion",
        severity="warning",
        statement="observed",
        evidence=evidence,
    )
    evidence["nested"]["items"][0]["value"] = "mutated"

    assert fact.evidence["nested"]["items"][0]["value"] == "safe"
    assert len(fact.evidence["bounded"]) < 300
    with pytest.raises((FrozenInstanceError, TypeError)):
        fact.statement = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        fact.evidence["nested"]["items"][0]["value"] = "mutated"  # type: ignore[index]
    json.dumps(fact.to_dict(), ensure_ascii=False, allow_nan=False)
    with pytest.raises(ValueError):
        DiagnosticFact("invented", "warning", "bad", {})
    with pytest.raises(ValueError):
        DiagnosticFact("unused_potion", "urgent", "bad", {})


def test_rank_orders_by_severity_then_floor_and_deduplicates_only_same_node_fact() -> None:
    info = _detail(
        run_id="run-a",
        global_floor=2,
        combat_rounds=tuple(_round(index) for index in range(1, 9)),
    )
    warning = _detail(
        run_id="run-a",
        global_floor=5,
        entry=InventorySnapshot(hp=80, max_hp=80),
        exit=InventorySnapshot(hp=60, max_hp=80),
    )
    same_kind_other_floor = replace(
        warning,
        node_id="node-6",
        floor=6,
        global_floor=6,
        label="A1F6",
    )
    same_kind_other_run = replace(
        warning,
        run_id="run-b",
        node_id="run-b-node-5",
    )
    critical = _detail(
        run_id="run-a",
        global_floor=9,
        coverage={
            "complete_run": True,
            "source_kind": "summary",
            "run_status": "crash",
            "technical_failure_kind": "crash",
            "terminal_node": True,
            "choices_complete": False,
        },
    )

    ranked = rank_run_anomalies(
        [warning, info, critical, warning, same_kind_other_floor, same_kind_other_run]
    )

    assert [fact.severity for fact in ranked] == [
        "critical",
        "warning",
        "warning",
        "warning",
        "info",
    ]
    assert [fact.kind for fact in ranked] == [
        "technical_failure",
        "large_node_hp_loss",
        "large_node_hp_loss",
        "large_node_hp_loss",
        "long_combat",
    ]


def test_rank_does_not_drop_later_runs_before_sorting() -> None:
    warning = _detail(
        entry=InventorySnapshot(hp=80, max_hp=80),
        exit=InventorySnapshot(hp=60, max_hp=80),
    )
    many_runs = [
        replace(
            warning,
            run_id=f"warning-run-{index}",
            node_id=f"warning-node-{index}",
        )
        for index in range(257)
    ]
    critical = _detail(
        run_id="critical-run",
        coverage={
            "complete_run": True,
            "source_kind": "summary",
            "run_status": "crash",
            "technical_failure_kind": "crash",
            "terminal_node": True,
            "choices_complete": False,
        },
    )

    ranked = rank_run_anomalies([*many_runs, critical])

    assert len(ranked) == 258
    assert ranked[0].kind == "technical_failure"


def test_rank_adds_distinct_stable_locators_to_identical_node_facts() -> None:
    first = _detail(
        run_id="same-run",
        global_floor=5,
        entry=InventorySnapshot(hp=80, max_hp=80),
        exit=InventorySnapshot(hp=60, max_hp=80),
    )
    second = replace(
        first,
        node_id="other-node",
        floor=6,
        global_floor=6,
        label="A1F6",
    )

    ranked = rank_run_anomalies([first, second])

    assert [fact.evidence["locator"] for fact in ranked] == [
        {"run_id": "same-run", "node_id": "node-5", "global_floor": 5},
        {"run_id": "same-run", "node_id": "other-node", "global_floor": 6},
    ]
    assert ranked[0].to_dict() != ranked[1].to_dict()
    assert "locator" not in collect_diagnostic_facts(first)[0].evidence
