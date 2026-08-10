"""Canonical, dependency-free records for training and evaluation runs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deltas import NodeDeltas


_CANONICAL_NODE_COLLECTION_LIMIT = 256
_CANONICAL_ACT_LIMIT = 4
_CANONICAL_FLOORS_PER_ACT = 17


class _FrozenMapping(Mapping[str, Any]):
    """Tuple-backed JSON mapping with no mutable-builtin escape hatch."""

    __slots__ = ("_items",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_items", tuple(value.items()))

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
        raise TypeError("canonical detail snapshots are immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("canonical detail snapshots are immutable")

    def __copy__(self) -> _FrozenMapping:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenMapping:
        return self

    def __repr__(self) -> str:
        return repr(dict(self.items()))


class _FrozenSequence(Sequence[Any]):
    """Tuple-backed JSON sequence that compares like an ordinary list."""

    __slots__ = ("_items",)

    def __init__(self, value: Sequence[Any]) -> None:
        object.__setattr__(self, "_items", tuple(value))

    def __getitem__(self, index: int | slice) -> Any:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("canonical detail snapshots are immutable")

    def __delattr__(self, _name: str) -> None:
        raise TypeError("canonical detail snapshots are immutable")

    def __copy__(self) -> _FrozenSequence:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenSequence:
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence) and not isinstance(other, (str, bytes)):
            return tuple(self) == tuple(other)
        return False

    def __repr__(self) -> str:
        return repr(list(self))


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
    if isinstance(value, Mapping):
        snapshot: dict[str, Any] = {}
        for key, item in list(value.items())[:_CANONICAL_NODE_COLLECTION_LIMIT]:
            if isinstance(key, str):
                snapshot[key] = _snapshot_json(item, depth=depth + 1)
        return _FrozenMapping(snapshot)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _FrozenSequence(
            [
                _snapshot_json(item, depth=depth + 1)
                for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
            ]
        )
    return None


def _snapshot_dict_sequence(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(
        _snapshot_json(item)
        for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
        if isinstance(item, Mapping)
    )


def _optional_contract_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _safe_snapshot_value(value: Any, *, depth: int = 0) -> bool:
    if depth >= 16:
        return False
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Enum):
        return _safe_snapshot_value(value.value, depth=depth + 1)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str)
            and _safe_snapshot_value(item, depth=depth + 1)
            for key, item in list(value.items())[:_CANONICAL_NODE_COLLECTION_LIMIT]
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return all(
            _safe_snapshot_value(item, depth=depth + 1)
            for item in value[:_CANONICAL_NODE_COLLECTION_LIMIT]
        )
    return False


def _snapshot_deltas(value: Any) -> NodeDeltas:
    """Return a deep immutable NodeDeltas contract or all-unknown deltas."""

    # Local import avoids the models <-> deltas module-loading cycle.
    from .deltas import NodeDeltas

    if type(value) is not NodeDeltas:
        return NodeDeltas()
    resolved: dict[str, RunDelta] = {}
    for item in fields(NodeDeltas):
        delta = getattr(value, item.name, None)
        if (
            not isinstance(delta, RunDelta)
            or not isinstance(delta.quality, DeltaQuality)
            or delta.value is None
            or not _safe_snapshot_value(delta.value)
        ):
            resolved[item.name] = RunDelta()
            continue
        resolved[item.name] = RunDelta(
            value=_snapshot_json(delta.value),
            quality=delta.quality,
        )
    try:
        return NodeDeltas(**resolved)
    except (AttributeError, TypeError, ValueError):
        return NodeDeltas()


class SourceKind(str, Enum):
    NATIVE_RUN = "native_run"
    REPLAY_JSONL = "replay_jsonl"
    DECK_HISTORY = "deck_history"
    EVAL_RESULTS = "eval_results"
    SUMMARY = "summary"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NodeOrigin:
    """Typed internal provenance established by canonicalization boundaries."""

    source_kind: SourceKind
    source_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("NodeOrigin source_kind must be a SourceKind")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("NodeOrigin source_id must be a non-empty string")


def node_evidence_key(nodes: Sequence[Any], index: int) -> str:
    """Return a stable key for one node, including duplicate occurrence order."""

    if not 0 <= index < len(nodes):
        raise IndexError(index)
    node = nodes[index]
    identity = "anonymous"
    if isinstance(node, Mapping):
        for field_name in ("id", "node_id"):
            candidate = node.get(field_name)
            if isinstance(candidate, str) and candidate:
                identity = f"{field_name}:{candidate}"
                break
    occurrence = 0
    for candidate in nodes[:index]:
        candidate_identity = "anonymous"
        if isinstance(candidate, Mapping):
            for field_name in ("id", "node_id"):
                candidate_value = candidate.get(field_name)
                if isinstance(candidate_value, str) and candidate_value:
                    candidate_identity = f"{field_name}:{candidate_value}"
                    break
        if candidate_identity == identity:
            occurrence += 1
    return f"{len(identity)}:{identity}:{occurrence}"


def _snapshot_node_provenance_index(
    value: Any,
) -> Mapping[str, tuple[NodeOrigin, ...]]:
    if not isinstance(value, Mapping):
        return _FrozenMapping({})
    snapshot: dict[str, tuple[NodeOrigin, ...]] = {}
    for key, raw_origins in list(value.items())[:_CANONICAL_NODE_COLLECTION_LIMIT]:
        if (
            not isinstance(key, str)
            or not isinstance(raw_origins, Sequence)
            or isinstance(raw_origins, (str, bytes))
        ):
            continue
        origins: list[NodeOrigin] = []
        for origin in raw_origins[:_CANONICAL_NODE_COLLECTION_LIMIT]:
            if (
                type(origin) is NodeOrigin
                and isinstance(origin.source_kind, SourceKind)
                and isinstance(origin.source_id, str)
                and origin.source_id
                and origin not in origins
            ):
                origins.append(origin)
        if origins:
            snapshot[key] = tuple(origins)
    return _FrozenMapping(snapshot)


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
            if item.metadata.get("serialize", True)
        }
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(
                    "Run record serialization requires string dict keys; "
                    f"got {type(key).__name__}"
                )
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
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
    game_version_source: str | None = None
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
        coordinate_limits = {
            "act": _CANONICAL_ACT_LIMIT,
            "floor": _CANONICAL_FLOORS_PER_ACT,
            "global_floor": (
                _CANONICAL_ACT_LIMIT * _CANONICAL_FLOORS_PER_ACT
            ),
        }
        for name, limit in coordinate_limits.items():
            if not 1 <= getattr(self, name) <= limit:
                raise ValueError(f"NodeDetail {name} is out of range")
        for name in ("label", "room_type", "status"):
            value = getattr(self, name)
            object.__setattr__(self, name, value if isinstance(value, str) else "unknown")
        object.__setattr__(
            self,
            "encounter",
            _snapshot_json(
                self.encounter if isinstance(self.encounter, Mapping) else {}
            ),
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
            _snapshot_json(
                self.coverage if isinstance(self.coverage, Mapping) else {}
            ),
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
    _node_provenance_index: Mapping[str, tuple[NodeOrigin, ...]] = field(
        default_factory=dict,
        repr=False,
        compare=False,
        metadata={"serialize": False},
    )

    def __post_init__(self) -> None:
        self._node_provenance_index = _snapshot_node_provenance_index(
            self._node_provenance_index
        )

    def node_origins(self, index: int) -> tuple[NodeOrigin, ...]:
        key = node_evidence_key(self.nodes, index)
        return self._node_provenance_index.get(key, ())

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)
