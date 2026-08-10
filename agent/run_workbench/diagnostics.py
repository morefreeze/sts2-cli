"""Conservative, deterministic facts derived from canonical node details."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
import json
import math
from typing import Any

from .models import NodeDetail


LARGE_NODE_HP_LOSS_RATIO = 0.25
HIGH_LOSS_ROUND_RATIO = 0.20
LONG_COMBAT_ROUNDS = 8

_EVIDENCE_COLLECTION_LIMIT = 256
_EVIDENCE_DEPTH_LIMIT = 16
_STATEMENT_LENGTH_LIMIT = 512
_FACT_COLLECTION_LIMIT = 256

_DIAGNOSTIC_KINDS = frozenset(
    {
        "technical_failure",
        "large_node_hp_loss",
        "high_loss_round",
        "long_combat",
        "unused_potion",
        "death_with_potion",
        "card_reward_selected",
        "card_reward_skipped",
        "partial_coverage",
    }
)
_DIAGNOSTIC_SEVERITIES = frozenset({"critical", "warning", "info"})
_TECHNICAL_STATUSES = frozenset(
    {"crash", "timeout", "stuck", "reset_failure", "invalid"}
)
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class _FrozenEvidenceMapping(Mapping[str, Any]):
    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_items", tuple(items.items()))

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("diagnostic evidence is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("diagnostic evidence is immutable")

    def __copy__(self) -> _FrozenEvidenceMapping:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenEvidenceMapping:
        return self

    def __repr__(self) -> str:
        return repr(dict(self.items()))


class _FrozenEvidenceSequence(Sequence[Any]):
    __slots__ = ("_items",)

    def __init__(self, items: Sequence[Any]) -> None:
        object.__setattr__(self, "_items", tuple(items))

    def __getitem__(self, index: int | slice) -> Any:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("diagnostic evidence is immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("diagnostic evidence is immutable")

    def __copy__(self) -> _FrozenEvidenceSequence:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenEvidenceSequence:
        return self

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, (str, bytes))
            and tuple(self) == tuple(other)
        )

    def __repr__(self) -> str:
        return repr(list(self))


def _freeze_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth >= _EVIDENCE_DEPTH_LIMIT:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        snapshot: dict[str, Any] = {}
        for key, item in list(value.items())[:_EVIDENCE_COLLECTION_LIMIT]:
            if isinstance(key, str):
                snapshot[key] = _freeze_evidence(item, depth=depth + 1)
        return _FrozenEvidenceMapping(snapshot)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _FrozenEvidenceSequence(
            [
                _freeze_evidence(item, depth=depth + 1)
                for item in value[:_EVIDENCE_COLLECTION_LIMIT]
            ]
        )
    return None


def _thaw_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_evidence(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_thaw_evidence(item) for item in value]
    return value


@dataclass(frozen=True)
class DiagnosticFact:
    """One allowlisted, JSON-safe observation with immutable evidence."""

    kind: str
    severity: str
    statement: str
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _DIAGNOSTIC_KINDS:
            raise ValueError("DiagnosticFact kind is not supported")
        if (
            type(self.severity) is not str
            or self.severity not in _DIAGNOSTIC_SEVERITIES
        ):
            raise ValueError("DiagnosticFact severity is not supported")
        if (
            type(self.statement) is not str
            or not self.statement
            or len(self.statement) > _STATEMENT_LENGTH_LIMIT
        ):
            raise ValueError("DiagnosticFact statement is invalid")
        if not isinstance(self.evidence, Mapping):
            raise ValueError("DiagnosticFact evidence must be a mapping")
        object.__setattr__(self, "evidence", _freeze_evidence(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "statement": self.statement,
            "evidence": _thaw_evidence(self.evidence),
        }


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None
    return None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _display_number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _max_hp(detail: NodeDetail, *states: Mapping[str, Any]) -> float | None:
    candidates: list[float] = []
    for state in states:
        value = _number(state.get("max_hp"))
        if value is not None and value > 0:
            candidates.append(value)
    for value in (detail.entry.max_hp, detail.exit.max_hp):
        numeric = _number(value)
        if numeric is not None and numeric > 0:
            candidates.append(numeric)
    if not candidates or any(value != candidates[0] for value in candidates[1:]):
        return None
    return candidates[0]


def _technical_failure_fact(detail: NodeDetail) -> DiagnosticFact | None:
    status = detail.coverage.get("run_status")
    if (
        status not in _TECHNICAL_STATUSES
        or detail.coverage.get("terminal_node") is not True
    ):
        return None
    technical_kind = detail.coverage.get("technical_failure_kind", status)
    if technical_kind != status:
        return None
    return DiagnosticFact(
        kind="technical_failure",
        severity="critical",
        statement=f"跑局因技术故障结束：{status}",
        evidence={
            "status": status,
            "technical_failure_kind": technical_kind,
        },
    )


def _large_node_hp_loss_fact(detail: NodeDetail) -> DiagnosticFact | None:
    entry_hp = _number(detail.entry.hp)
    exit_hp = _number(detail.exit.hp)
    max_hp = _max_hp(detail)
    if entry_hp is None or exit_hp is None or max_hp is None:
        return None
    hp_loss = entry_hp - exit_hp
    ratio = hp_loss / max_hp
    if hp_loss <= 0 or ratio < LARGE_NODE_HP_LOSS_RATIO:
        return None
    return DiagnosticFact(
        kind="large_node_hp_loss",
        severity="warning",
        statement=(
            f"本节点损失 {_display_number(hp_loss)} HP"
            f"（最大生命的 {ratio:.1%}）"
        ),
        evidence={
            "entry_hp": _display_number(entry_hp),
            "exit_hp": _display_number(exit_hp),
            "max_hp": _display_number(max_hp),
            "hp_loss": _display_number(hp_loss),
            "hp_loss_ratio": ratio,
            "threshold_ratio": LARGE_NODE_HP_LOSS_RATIO,
        },
    )


def _high_loss_round_facts(detail: NodeDetail) -> tuple[DiagnosticFact, ...]:
    facts: list[DiagnosticFact] = []
    for round_info in detail.combat_rounds:
        if not isinstance(round_info, Mapping):
            continue
        round_number = _integer(round_info.get("round"))
        start = round_info.get("start_state")
        end = round_info.get("end_state")
        if (
            round_number is None
            or not isinstance(start, Mapping)
            or not isinstance(end, Mapping)
        ):
            continue
        start_hp = _number(start.get("hp"))
        end_hp = _number(end.get("hp"))
        max_hp = _max_hp(detail, start, end)
        if start_hp is None or end_hp is None or max_hp is None:
            continue
        hp_loss = start_hp - end_hp
        ratio = hp_loss / max_hp
        if hp_loss <= 0 or ratio < HIGH_LOSS_ROUND_RATIO:
            continue
        facts.append(
            DiagnosticFact(
                kind="high_loss_round",
                severity="warning",
                statement=(
                    f"第 {round_number} 回合损失 {_display_number(hp_loss)} HP"
                    f"（最大生命的 {ratio:.1%}）"
                ),
                evidence={
                    "round": round_number,
                    "start_hp": _display_number(start_hp),
                    "end_hp": _display_number(end_hp),
                    "max_hp": _display_number(max_hp),
                    "hp_loss": _display_number(hp_loss),
                    "hp_loss_ratio": ratio,
                    "threshold_ratio": HIGH_LOSS_ROUND_RATIO,
                },
            )
        )
        if len(facts) >= _FACT_COLLECTION_LIMIT:
            break
    return tuple(facts)


def _long_combat_fact(detail: NodeDetail) -> DiagnosticFact | None:
    recorded_rounds = len(detail.combat_rounds)
    if recorded_rounds < LONG_COMBAT_ROUNDS:
        return None
    return DiagnosticFact(
        kind="long_combat",
        severity="info",
        statement=f"本场战斗记录到 {recorded_rounds} 个回合",
        evidence={
            "recorded_rounds": recorded_rounds,
            "threshold_rounds": LONG_COMBAT_ROUNDS,
        },
    )


def _action_name(action: Mapping[str, Any]) -> str | None:
    nested = action.get("action")
    if isinstance(nested, Mapping):
        value = nested.get("action")
        return value if isinstance(value, str) else None
    return nested if isinstance(nested, str) else None


def _potion_identity(potion: Any) -> tuple[str, str] | None:
    if not isinstance(potion, Mapping):
        return None
    potion_id = potion.get("id")
    name = potion.get("name")
    if isinstance(potion_id, str) and potion_id:
        return ("id", potion_id)
    if isinstance(name, str) and name:
        return ("name", name)
    return None


def _potion_name(potion: Mapping[str, Any]) -> str | None:
    for key in ("name", "id"):
        value = potion.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _unused_potion_fact(detail: NodeDetail) -> DiagnosticFact | None:
    if (
        detail.coverage.get("source_kind") != "replay_jsonl"
        or detail.coverage.get("combat_coverage_complete") is not True
        or not detail.combat_rounds
    ):
        return None
    first_round = detail.combat_rounds[0]
    if not isinstance(first_round, Mapping) or _integer(first_round.get("round")) != 1:
        return None

    common_identities: set[tuple[str, str]] | None = None
    identity_names: dict[tuple[str, str], str] = {}
    recorded_actions = 0
    all_actions: list[Mapping[str, Any]] = []
    for expected_round, round_info in enumerate(detail.combat_rounds, start=1):
        if not isinstance(round_info, Mapping):
            return None
        if _integer(round_info.get("round")) != expected_round:
            return None
        actions = round_info.get("actions")
        if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
            return None
        round_actions = [action for action in actions if isinstance(action, Mapping)]
        if len(round_actions) != len(actions):
            return None
        recorded_actions += len(round_actions)
        all_actions.extend(round_actions)
        for state_key in ("start_state", "end_state"):
            state = round_info.get(state_key)
            if not isinstance(state, Mapping) or "potions" not in state:
                return None
            potions = state.get("potions")
            if not isinstance(potions, Sequence) or isinstance(potions, (str, bytes)):
                return None
            identities: set[tuple[str, str]] = set()
            for potion in potions:
                identity = _potion_identity(potion)
                if identity is None or not isinstance(potion, Mapping):
                    return None
                identities.add(identity)
                name = _potion_name(potion)
                if name is not None:
                    identity_names.setdefault(identity, name)
            common_identities = (
                identities
                if common_identities is None
                else common_identities.intersection(identities)
            )
    all_actions.extend(
        action for action in detail.actions if isinstance(action, Mapping)
    )
    if any(_action_name(action) == "use_potion" for action in all_actions):
        return None
    if not common_identities:
        return None
    potion_names = sorted(
        identity_names.get(identity, identity[1]) for identity in common_identities
    )
    return DiagnosticFact(
        kind="unused_potion",
        severity="warning",
        statement="本场战斗记录到药水，但没有 use_potion 操作",
        evidence={
            "potion_names": potion_names,
            "recorded_actions": recorded_actions,
            "combat_coverage_complete": True,
        },
    )


def _inventory_potion_names(detail: NodeDetail) -> list[str]:
    names: list[str] = []
    for potion in detail.exit.potions:
        if isinstance(potion, Mapping):
            name = _potion_name(potion)
            if name is not None and name not in names:
                names.append(name)
    return names


def _death_with_potion_fact(detail: NodeDetail) -> DiagnosticFact | None:
    run_status = detail.coverage.get("run_status")
    known_exit_fields = detail.coverage.get("exit_inventory_fields")
    if (
        run_status != "dead"
        or detail.coverage.get("terminal_node") is not True
        or not isinstance(known_exit_fields, Sequence)
        or isinstance(known_exit_fields, (str, bytes))
        or "potions" not in known_exit_fields
    ):
        return None
    potion_names = _inventory_potion_names(detail)
    if not potion_names:
        return None
    return DiagnosticFact(
        kind="death_with_potion",
        severity="critical",
        statement="终态为死亡，且终局库存仍有药水",
        evidence={
            "status": "dead",
            "potion_names": potion_names,
            "terminal_inventory_known": True,
        },
    )


def _card_summary(choice: Mapping[str, Any]) -> dict[str, str] | None:
    card_id = next(
        (
            value
            for key in ("item_id", "card_id", "id")
            if isinstance((value := choice.get(key)), str) and value
        ),
        None,
    )
    name = next(
        (
            value
            for key in ("label", "name")
            if isinstance((value := choice.get(key)), str) and value
        ),
        card_id,
    )
    if name is None:
        return None
    summary = {"name": name}
    if card_id is not None:
        summary = {"id": card_id, "name": name}
    return summary


def _card_reward_fact(detail: NodeDetail) -> DiagnosticFact | None:
    reward_choices = [
        choice
        for choice in detail.choices
        if isinstance(choice, Mapping) and choice.get("kind") == "card_reward"
    ]
    if not reward_choices or any(
        type(choice.get("selected")) is not bool for choice in reward_choices
    ):
        return None
    offered_cards = [_card_summary(choice) for choice in reward_choices]
    if any(card is None for card in offered_cards):
        return None
    selected = [choice for choice in reward_choices if choice["selected"] is True]
    if len(selected) > 1:
        return None
    evidence = {
        "offered_cards": offered_cards,
        "choices_complete": False,
    }
    if selected:
        selected_card = _card_summary(selected[0])
        if selected_card is None:
            return None
        return DiagnosticFact(
            kind="card_reward_selected",
            severity="info",
            statement=f"选择了卡牌奖励：{selected_card['name']}",
            evidence={"selected_card": selected_card, **evidence},
        )
    if detail.coverage.get("source_kind") != "replay_jsonl" or not any(
        _action_name(action) == "skip_card_reward"
        for action in detail.actions
        if isinstance(action, Mapping)
    ):
        return None
    return DiagnosticFact(
        kind="card_reward_skipped",
        severity="info",
        statement="跳过了卡牌奖励",
        evidence=evidence,
    )


def _partial_coverage_fact(detail: NodeDetail) -> DiagnosticFact | None:
    first_floor = _integer(detail.coverage.get("first_recorded_floor"))
    last_floor = _integer(detail.coverage.get("last_recorded_floor"))
    if (
        detail.coverage.get("complete_run") is not False
        or first_floor is None
        or last_floor is None
        or first_floor < 1
        or last_floor < first_floor
    ):
        return None
    return DiagnosticFact(
        kind="partial_coverage",
        severity="warning",
        statement="记录仅覆盖跑局的一部分",
        evidence={
            "complete_run": False,
            "first_recorded_floor": first_floor,
            "last_recorded_floor": last_floor,
        },
    )


def collect_diagnostic_facts(detail: NodeDetail) -> tuple[DiagnosticFact, ...]:
    """Derive facts from a canonical detail, ignoring any pre-existing facts."""

    if type(detail) is not NodeDetail:
        return ()
    facts: list[DiagnosticFact] = []
    single_fact_collectors = (
        _technical_failure_fact,
        _large_node_hp_loss_fact,
        _long_combat_fact,
        _unused_potion_fact,
        _death_with_potion_fact,
        _card_reward_fact,
        _partial_coverage_fact,
    )
    for collector in single_fact_collectors:
        fact = collector(detail)
        if fact is not None:
            facts.append(fact)
    facts.extend(_high_loss_round_facts(detail))
    return tuple(facts[:_FACT_COLLECTION_LIMIT])


def rank_run_anomalies(
    details: Iterable[NodeDetail],
) -> tuple[DiagnosticFact, ...]:
    """Return stable, de-duplicated facts ordered by severity then floor."""

    ranked: list[tuple[int, int, int, DiagnosticFact]] = []
    seen: set[tuple[str, str, int, str]] = set()
    ordinal = 0
    for detail in details:
        if type(detail) is not NodeDetail:
            continue
        for fact in collect_diagnostic_facts(detail):
            fingerprint = json.dumps(
                fact.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            key = (detail.run_id, detail.node_id, detail.global_floor, fingerprint)
            if key in seen:
                continue
            seen.add(key)
            located_fact = DiagnosticFact(
                kind=fact.kind,
                severity=fact.severity,
                statement=fact.statement,
                evidence={
                    **fact.to_dict()["evidence"],
                    "locator": {
                        "run_id": detail.run_id,
                        "node_id": detail.node_id,
                        "global_floor": detail.global_floor,
                    },
                },
            )
            ranked.append(
                (
                    _SEVERITY_ORDER[fact.severity],
                    detail.global_floor,
                    ordinal,
                    located_fact,
                )
            )
            ordinal += 1
    ranked.sort(key=lambda item: item[:3])
    return tuple(item[3] for item in ranked)


__all__ = [
    "HIGH_LOSS_ROUND_RATIO",
    "LARGE_NODE_HP_LOSS_RATIO",
    "LONG_COMBAT_ROUNDS",
    "DiagnosticFact",
    "collect_diagnostic_facts",
    "rank_run_anomalies",
]
