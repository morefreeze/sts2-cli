"""Canonical, dependency-free records for training and evaluation runs."""

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
import math
from typing import Any


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
        return _serialize(asdict(value))
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
