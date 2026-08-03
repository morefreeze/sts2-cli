"""Canonical, dependency-free records for training and evaluation runs."""

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
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
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


@dataclass(frozen=True)
class RunDelta:
    value: Any = None
    quality: DeltaQuality = DeltaQuality.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return _serialize(self)


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
