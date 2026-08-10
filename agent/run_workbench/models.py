"""Canonical, dependency-free records for training and evaluation runs."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deltas import NodeDeltas


_CANONICAL_NODE_COLLECTION_LIMIT = 256


class _FrozenDict(dict[str, Any]):
    """A JSON-shaped mapping that cannot mutate after contract construction."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("canonical detail snapshots are immutable")

    __delitem__ = _immutable
    __ior__ = _immutable
    __setitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable

    def __copy__(self) -> _FrozenDict:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenDict:
        return self


class _FrozenList(list[Any]):
    """A list-shaped immutable value that retains ordinary JSON equality."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("canonical detail snapshots are immutable")

    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    __setitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self) -> _FrozenList:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenList:
        return self


def _snapshot_json(value: Any, *, depth: int = 0) -> Any:
    """Freeze one bounded JSON value, degrading unsafe leaves to ``None``."""

    if depth >= 16:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return _snapshot_json(value.value, depth=depth + 1)
    if isinstance(value, dict):
        snapshot: dict[str, Any] = {}
        for key, item in list(value.items())[:_CANONICAL_NODE_COLLECTION_LIMIT]:
            if isinstance(key, str):
                snapshot[key] = _snapshot_json(item, depth=depth + 1)
        return _FrozenDict(snapshot)
    if isinstance(value, list):
        return _FrozenList(
            _snapshot_json(item, depth=depth + 1)
            for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
        )
    if isinstance(value, tuple):
        return tuple(
            _snapshot_json(item, depth=depth + 1)
            for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
        )
    return None


def _snapshot_dict_sequence(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        _snapshot_json(item)
        for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
        if isinstance(item, dict)
    )


def _optional_contract_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _snapshot_deltas(value: Any) -> Any:
    """Deep-freeze RunDelta values without importing deltas at runtime."""

    if not is_dataclass(value) or isinstance(value, type):
        return value
    try:
        return type(value)(
            **{
                item.name: type(delta)(
                    value=_snapshot_json(delta.value),
                    quality=delta.quality,
                )
                for item in fields(value)
                if is_dataclass(delta := getattr(value, item.name))
                and hasattr(delta, "value")
                and hasattr(delta, "quality")
            }
        )
    except (AttributeError, TypeError):
        return value


class SourceKind(str, Enum):
    NATIVE_RUN = "native_run"
    REPLAY_JSONL = "replay_jsonl"
    DECK_HISTORY = "deck_history"
    EVAL_RESULTS = "eval_results"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


class RunStatus(str, Enum):
    WIN = "win"
    DEAD = "dead"
    CRASH = "crash"
    TIMEOUT = "timeout"
    STUCK = "stuck"
    RESET_FAILURE = "reset_failure"
    INVALID = "invalid"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"

    @property
    def is_technical(self) -> bool:
        return self in {
            RunStatus.CRASH,
            RunStatus.TIMEOUT,
            RunStatus.STUCK,
            RunStatus.RESET_FAILURE,
            RunStatus.INVALID,
        }


class DeltaQuality(str, Enum):
    EXACT = "exact"
    DERIVED = "derived"
    UNKNOWN = "unknown"


def _serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return _serialize(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _serialize(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(
                    "Run record serialization requires string dict keys; "
                    f"got {type(key).__name__}"
                )
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Run record serialization does not support non-finite float values")
        return value
    raise TypeError(
        "Run record serialization does not support "
        f"{type(value).__name__} values"
    )


@dataclass(frozen=True)
class RunDelta:
    value: Any = None
    quality: DeltaQuality = DeltaQuality.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(frozen=True)
class MapNode:
    id: str
    col: int
    row: int
    room_type: str
    visited: bool = False
    path_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(frozen=True)
class MapEdge:
    from_id: str
    to_id: str

    def to_dict(self) -> dict[str, str]:
        return {"from": self.from_id, "to": self.to_id}


@dataclass(frozen=True)
class MapAlignment:
    ok: bool
    ambiguous: bool = False
    reason: str | None = None
    path_node_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(frozen=True)
class ActMap:
    act_id: str
    nodes: tuple[MapNode, ...] = ()
    edges: tuple[MapEdge, ...] = ()
    alignment: MapAlignment = field(default_factory=lambda: MapAlignment(ok=False))
    full_map: bool = False
    visited_route: bool = False
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "act_id": self.act_id,
            "full_map": self.full_map,
            "visited_route": self.visited_route,
            "fallback_reason": self.fallback_reason,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "alignment": self.alignment.to_dict(),
        }


@dataclass(frozen=True)
class RunMetadata:
    character: str | None = None
    seed: str | None = None
    game_version: str | None = None
    checkpoint: str | None = None
    evaluation_mode: str | None = None
    scenario: str | None = None
    ascension: int | None = None
    modifiers: tuple[str, ...] = ()
    is_multiplayer: bool | None = None
    started_at: float | None = None
    ended_at: float | None = None


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus = RunStatus.UNKNOWN
    victory: bool | None = None
    max_global_floor: int | None = None
    max_floor_label: str | None = None
    technical_failure_kind: str | None = None


@dataclass(frozen=True)
class Coverage:
    complete_run: bool = False
    first_recorded_floor: int | None = None
    last_recorded_floor: int | None = None


@dataclass(frozen=True)
class Capabilities:
    full_map: bool = False
    visited_route: bool = False
    node_rewards: bool = False
    final_inventory: bool = False
    decisions: bool = False
    turn_replay: bool = False


@dataclass(frozen=True)
class InventorySnapshot:
    hp: int | None = None
    max_hp: int | None = None
    gold: int | None = None
    deck: tuple[dict, ...] = ()
    relics: tuple[dict, ...] = ()
    potions: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hp", _optional_contract_int(self.hp))
        object.__setattr__(self, "max_hp", _optional_contract_int(self.max_hp))
        object.__setattr__(self, "gold", _optional_contract_int(self.gold))
        object.__setattr__(self, "deck", _snapshot_dict_sequence(self.deck))
        object.__setattr__(self, "relics", _snapshot_dict_sequence(self.relics))
        object.__setattr__(self, "potions", _snapshot_dict_sequence(self.potions))

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass(frozen=True)
class NodeDetail:
    run_id: str
    node_id: str
    act: int
    floor: int
    global_floor: int
    label: str
    room_type: str
    status: str
    encounter: dict
    entry: InventorySnapshot
    exit: InventorySnapshot
    deltas: NodeDeltas
    choices: tuple[dict, ...]
    actions: tuple[dict, ...]
    combat_rounds: tuple[dict, ...]
    coverage: dict
    facts: tuple[dict, ...] = ()
    hypotheses: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", self.run_id if isinstance(self.run_id, str) else "")
        object.__setattr__(self, "node_id", self.node_id if isinstance(self.node_id, str) else "")
        for name in ("act", "floor", "global_floor"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"NodeDetail {name} must be an integer")
        for name in ("label", "room_type", "status"):
            value = getattr(self, name)
            object.__setattr__(self, name, value if isinstance(value, str) else "unknown")
        object.__setattr__(
            self,
            "encounter",
            _snapshot_json(self.encounter if isinstance(self.encounter, dict) else {}),
        )
        object.__setattr__(self, "deltas", _snapshot_deltas(self.deltas))
        for name in (
            "choices",
            "actions",
            "combat_rounds",
            "facts",
            "hypotheses",
        ):
            object.__setattr__(self, name, _snapshot_dict_sequence(getattr(self, name)))
        object.__setattr__(
            self,
            "coverage",
            _snapshot_json(self.coverage if isinstance(self.coverage, dict) else {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


@dataclass
class RunRecord:
    run_id: str
    source_id: str
    source_kind: SourceKind
    metadata: RunMetadata = field(default_factory=RunMetadata)
    outcome: RunOutcome = field(default_factory=RunOutcome)
    coverage: Coverage = field(default_factory=Coverage)
    capabilities: Capabilities = field(default_factory=Capabilities)
    acts: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    replay_by_node: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)
