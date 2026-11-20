"""Boss-entry survival estimates and backwards HP budgets for the rest of an act."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from time import monotonic


def enabled() -> bool:
    return os.environ.get("STS2_ACT_PLAN") == "1"


def target_win_rate() -> float:
    try:
        target = float(os.environ.get("STS2_BOSS_WIN_TARGET", "0.8"))
    except ValueError:
        return 0.8
    return target if 0 < target < 1 else 0.8


@dataclass(frozen=True)
class BossCurve:
    max_hp: int
    values: tuple[float, ...]
    samples: int
    matching: int

    def probability(self, hp: float) -> float:
        bucket = min(max(hp / self.max_hp, 0), 1) * (len(self.values) - 1)
        left = int(bucket)
        right = min(left + 1, len(self.values) - 1)
        return self.values[left] + (bucket - left) * (self.values[right] - self.values[left])

    def required_hp(self, target: float) -> int:
        # max_hp + 1 means the requested confidence is unreachable even at full HP.
        return next((hp for hp in range(1, self.max_hp + 1) if self.probability(hp) >= target),
                    self.max_hp + 1)


def fit_curve(samples, prior, max_hp: int, matching: int = 0) -> BossCurve:
    """Local, weighted win/loss estimates with a pooled prior and monotonic smoothing."""
    blocks = []
    steps = 10
    for i in range(steps + 1):
        ratio = i / steps
        weight = 2.0
        wins = weight * prior(ratio * max_hp)
        for observed, won, relevance in samples:
            support = relevance * max(0, 1 - abs(ratio - observed) / 0.2)
            weight += support
            wins += support * won
        blocks.append((1, weight, wins))
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][1] > blocks[-1][2] / blocks[-1][1]:
            n, weight, wins = blocks.pop()
            prev_n, prev_weight, prev_wins = blocks.pop()
            blocks.append((n + prev_n, weight + prev_weight, wins + prev_wins))
    values = tuple(wins / weight for n, weight, wins in blocks for _ in range(n))
    return BossCurve(max_hp, values, len(samples), matching)


def entry_id(raw) -> str:
    return str(raw or "").upper().split(".")[-1]


@dataclass(frozen=True)
class BossObservation:
    boss: str
    act: int
    character: str
    ascension: int
    build: str
    hp_ratio: float
    won: bool


def boss_observations(run: dict):
    """The previous room's exit HP is the history proxy for Boss entry HP."""
    players = run["players"]
    if len(players) != 1 or run.get("is_custom_game") or run.get("modifiers"):
        return
    character = entry_id(players[0]["character"])
    ascension = run["ascension"]
    if type(ascension) is not int:
        return
    acts = run["map_point_history"]
    for act_idx, rooms in enumerate(acts):
        for i, point in enumerate(rooms[1:], 1):
            combats = [room for room in point["rooms"] if room.get("room_type") == "boss"]
            if not combats:
                continue
            boss = entry_id(combats[0].get("model_id"))
            previous = rooms[i - 1]["player_stats"][0]
            hp, max_hp = previous["current_hp"], previous["max_hp"]
            if not boss or not 0 < hp <= max_hp:
                continue
            later_act = any(acts[act_idx + 1:])
            reached_later_room = i + 1 < len(rooms) or later_act
            if reached_later_room or run.get("win"):
                won = True
            elif not run.get("was_abandoned") and entry_id(run.get("killed_by_encounter")) == boss:
                won = False
            elif len(combats) > 1 and entry_id(run.get("killed_by_encounter")) == entry_id(combats[1].get("model_id")):
                won = True
            else:
                continue
            yield BossObservation(boss, act_idx + 1, character, ascension, str(run.get("build_id", "")),
                                  hp / max_hp, won)


@lru_cache(maxsize=2)
def _load_history(root: str, refresh: int) -> tuple[BossObservation, ...]:
    samples = []
    for path in Path(root).glob("**/saves/history/*.run"):
        try:
            samples.extend(boss_observations(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
    return tuple(samples)


def _minor_build(version: str) -> tuple[int, ...]:
    return tuple(map(int, re.findall(r"\d+", version)[:2]))


@lru_cache(maxsize=128)
def _boss_curve(root, refresh, boss, act, character, ascension, build, max_hp, prior_hp, prior):
    history = _load_history(root, refresh) if root else ()
    if not build:
        build = max((row.build for row in history), key=_minor_build, default="")
    samples = []
    matching = 0
    for row in history:
        if row.act != act:
            continue
        version_weight = 1 if _minor_build(row.build) == _minor_build(build) else 0.25
        asc_weight = (0.42 ** abs(row.ascension - ascension)) if ascension is not None else 0.35
        encounter_weight = 1 if row.boss == boss else 0.12
        character_weight = 1 if row.character == character else 0.55
        samples.append((row.hp_ratio, row.won, version_weight * asc_weight * encounter_weight * character_weight))
        matching += row.boss == boss and version_weight == 1 and asc_weight >= 0.42
    return fit_curve(samples, lambda hp: prior(hp / prior_hp), max_hp, matching)


def boss_curve(state, map_json, act, character, ascension, max_hp, prior_hp, prior):
    if not enabled():
        return None
    boss = (state.get("context") or {}).get("boss") or {}
    boss_id = entry_id((map_json.get("boss") or {}).get("id") or (boss.get("id") if isinstance(boss, dict) else boss))
    if not boss_id:
        return None
    return _boss_curve(os.environ.get("STS2_BOSS_HISTORY_ROOT"), int(monotonic() // 300),
                       boss_id, act, entry_id(character), ascension,
                       state.get("game_version") or os.environ.get("STS2_BOSS_GAME_VERSION", ""),
                       int(max_hp), prior_hp, prior)


def rest_thresholds(kinds, costs, boss_hp, heal_hp) -> dict[int, float]:
    """HP needed to upgrade at each rest given the option of healing further ahead."""
    target = boss_hp
    thresholds = {}
    for index in range(len(kinds) - 1, -1, -1):
        if kinds[index] == "RestSite":
            thresholds[index] = target
            target = max(1, target - heal_hp)
        else:
            target += costs[index]
    return thresholds


def rest_should_heal(hp, threshold, max_hp, heal_hp) -> bool:
    # Spend the rest only if it improves the suffix budget. The threshold may
    # exceed max HP when no route can reach the desired Boss win rate.
    return hp < threshold and min(max_hp, hp + heal_hp) > hp


def normal_heal(max_hp: float) -> int:
    return int(0.30 * max_hp)
