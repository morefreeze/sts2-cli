# Sts2Advisor — 正式游戏内局外决策建议 mod Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在《杀戮尖塔 2》正式版里，用一个 mod 读取真实游戏状态，在右下角面板显示每个局外决策点的建议和依据。建议和依据都来自 A/B 验证过的 `greedy_action`，依据由它在做决定的同一段代码里记录。

**Architecture:** 三部分，前两部分互不依赖，可以并行。
- **Part A（Python）**：给 `greedy_action` 加可选的 trace 记录器，修地图启发式的楼层语义，新建本地 HTTP 服务 `agent/advisor_server.py`。
- **Part B（C# headless）**：把 `RunSimulator.cs` 里的"游戏模型 → 决策 JSON"纯函数抽进共享源文件 `src/Sts2Shared/`。用"重放同一份动作日志，状态逐字节一致"作为金标。
- **Part C（C# mod）**：新工程 `src/Sts2Advisor/`，编译时链接 Part B 的共享源文件；参照游戏自带 AutoSlay 的方式轮询当前界面，调 Part A 的服务，画面板。

**Tech Stack:** Python 3.11（`.venv/bin/python`）+ pytest + 标准库 `http.server`；C# .NET 9（`~/.dotnet-arm64/dotnet`）+ Krafs.Publicizer + xunit；游戏 v0.111.0 自带的 `sts2.dll`、`GodotSharp.dll`、`0Harmony.dll`。

**Spec:** `docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md`

---

## 背景（执行者必读）

- **始终用 `.venv/bin/python`**；构建用 `~/.dotnet-arm64/dotnet`。临时脚本放 `/tmp/sts2-cli/`，长期要留的输出放 `~/.sts2-train/`（Mac 会清 `/tmp`）。超过几分钟的命令用 `scripts/run_caffeinated.sh` 包住（Mac 空闲 1 分钟会睡眠）。
- **工作区里有用户未提交的改动**（`agent/run_workbench/game_detail.py`、`agent/run_workbench/replay.py` 和两个对应测试）。**不要 `git add -A`，不要 stash、checkout 或还原它们**。每次提交只 `git add` 本任务列出的文件。
- 提交信息结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- `greedy_action(state)`（`agent/combat_env.py:448`）只读入一个决策 JSON。但它**有一处隐藏状态**：`card_scoring._MC_CONTEXT`。card_reward 分支每次调用都会通过 `set_mc_context` 改写它，`pick_best_card` 和其他打分函数再读它（主要是 `floor`）。所以 card_select、商店的结果取决于这之前最后一次 card_reward。本计划的金标和测试都**按对局顺序重放**状态，并在每局开始时用 `reset_greedy_context()` 把它重置成模块初始值。这个隐藏状态保持原样，不修。
- 决策 JSON 里的名字和描述**只有英文**：`LocLookup.Lang` 默认是 `"en"`，日志里是 `"name": "Strike"`，不是 `{en, zh}`。所以面板上的中文名不能从 advisor 回复里拿，由 mod 自己从游戏取官方译名（Part C）。spec 第 1 段第 4 条里的 `label: {en, zh}` 据此删掉，在 Task A12 一并修正。
- 回归、A/B 都不经过 `HpAwareMapStrategy`（`play_full_run` 的路线是随机的）。所以 Task A9 的地图修复不会让任何已有实测结论失效。
- headless 引擎用 Krafs.Publicizer 访问 `sts2.dll` 的非公开成员（`src/Sts2Headless/Sts2Headless.csproj`）。被抽出来的序列化代码也依赖这一点，所以 mod 工程同样要用它。

## 文件结构

**Part A（Python）**
- Create `agent/greedy_trace.py`：`GreedyTrace` 记录器、`TRACE_KEYS`、`GREEDY_BEHAVIOR_ENV`、`reset_greedy_context()`。
- Modify `agent/card_scoring.py`：`pick_best_card` 增加 `trace`、`trace_indices` 参数。
- Modify `agent/strategy.py`：`rest_site_action` 增加 `trace`；`HpAwareMapStrategy.choose` 增加 `trace`，并修楼层语义。
- Modify `agent/combat_env.py`：`greedy_action(state, trace=None)`，`_score_event_option(opt, hits=None)`。
- Create `agent/advisor_server.py`：本地 HTTP 服务。
- Create `tools/build_advisor_corpus.py`：从保留的 batch 日志生成测试语料和金标。
- Create `tests/fixtures/advisor/ooc_corpus.jsonl.gz`（生成物，约 300 KB）。
- Create `tests/agent/advisor_corpus.py`（测试用加载器）、`tests/agent/test_greedy_trace_recorder.py`、`tests/agent/test_greedy_golden.py`、`tests/agent/test_greedy_trace.py`、`tests/agent/test_map_strategy_floors.py`、`tests/agent/test_advisor_server.py`。

**Part B（C# headless）**：见 Part B 开头的文件结构。

**Part C（C# mod）**：见 Part C 开头的文件结构。

---

# Part A — greedy trace、地图修复、advisor 服务（Python）

### Task A1: `GreedyTrace` 记录器和 key 表

**Files:**
- Create: `agent/greedy_trace.py`
- Test: `tests/agent/test_greedy_trace_recorder.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/agent/test_greedy_trace_recorder.py
"""GreedyTrace: the recorder greedy_action writes its reasons into."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

from agent import card_scoring
from agent.greedy_trace import (GREEDY_BEHAVIOR_ENV, TRACE_KEYS, GreedyTrace,
                                reset_greedy_context)


def test_option_and_rule_round_trip():
    t = GreedyTrace()
    t.option(2, kind="card", score=7.5,
             components=[("card.base", 6.0, "A"), ("card.archetype", 1.5, None)])
    t.option(0, kind="card", status="filtered")
    t.rule("card_reward.pick_best", "fired", best_index=2, threshold=5.5)
    d = t.to_dict()
    assert [o["index"] for o in d["options"]] == [0, 2]
    assert d["options"][1] == {
        "kind": "card", "index": 2, "score": 7.5, "status": "ok",
        "components": [{"key": "card.base", "value": 6.0, "detail": "A"},
                       {"key": "card.archetype", "value": 1.5}],
    }
    assert d["options"][0]["score"] is None and d["options"][0]["status"] == "filtered"
    assert d["rules"] == [{"key": "card_reward.pick_best", "outcome": "fired",
                           "values": {"best_index": 2, "threshold": 5.5}}]
    assert t.score_of("card", 2) == 7.5
    assert t.ok_scores("card") == [7.5]
    assert [r["key"] for r in t.fired()] == ["card_reward.pick_best"]


def test_unknown_key_raises():
    t = GreedyTrace()
    with pytest.raises(KeyError):
        t.rule("card_reward.typo", "fired")
    with pytest.raises(KeyError):
        t.option(0, kind="card", score=1.0, components=[("card.typo", 1.0, None)])


def test_bad_outcome_and_status_raise():
    t = GreedyTrace()
    with pytest.raises(ValueError):
        t.rule("card_reward.pick_best", "maybe")
    with pytest.raises(ValueError):
        t.option(0, kind="card", status="weird")


def test_behavior_env_covers_play_full_run_knobs():
    import play_full_run
    assert set(play_full_run.GREEDY_KNOBS) <= set(GREEDY_BEHAVIOR_ENV)
    assert {"STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL"} <= set(GREEDY_BEHAVIOR_ENV)


def test_reset_greedy_context_restores_module_default():
    card_scoring.set_mc_context(hp=12, max_hp=70, floor=14, relics=["X"])
    reset_greedy_context()
    assert card_scoring._MC_CONTEXT == {"hp": 80, "max_hp": 80, "floor": 5}


def test_event_keyword_keys_are_trace_keys():
    assert sum(1 for k in TRACE_KEYS if k.startswith("event.kw.")) == 28
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace_recorder.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'agent.greedy_trace'`

- [ ] **Step 3: 实现**

```python
# agent/greedy_trace.py
"""Evidence recorder for greedy_action -- the in-game Sts2Advisor's reasons.

greedy_action(state, trace=GreedyTrace()) records, in the code that makes each
decision, the numbers that decided it: per-option scores with the components
that were summed into them, and the ordered rule checks (fired / skipped).
trace=None -- every existing caller -- records nothing and runs the same code.

A key outside TRACE_KEYS raises KeyError, so a typo fails the advisor's tests
(and, at runtime, turns into "no advice") instead of reaching the panel.
"""
from __future__ import annotations

# Env vars that change what greedy_action picks: play_full_run.GREEDY_KNOBS plus
# three it does not stamp. The advisor warns when any is set; the corpus builder
# refuses to run.
GREEDY_BEHAVIOR_ENV = (
    "STS2_RANDOMIZE", "STS2_DECISION_ADVISOR", "STS2_CARD_THRESHOLD_LIFT",
    "STS2_BASIC_PURGE_ALL", "STS2_CARD_QUALITY_GATE",
    "STS2_CARD_PICK_TEMP", "STS2_MC_ROLLOUT", "STS2_VANTOM_REST_HEAL",
)

# card_scoring._MC_CONTEXT as the module defines it at import time.
MC_CONTEXT_START = {"hp": 80, "max_hp": 80, "floor": 5}

# One key per `if` branch of combat_env._score_event_option, in source order.
EVENT_KEYWORD_KEYS = (
    "event.kw.lose_max_hp", "event.kw.curse", "event.kw.lose_all_gold",
    "event.kw.lose_gold", "event.kw.torment", "event.kw.take_damage",
    "event.kw.lose_hp", "event.kw.downgrade", "event.kw.add_basic",
    "event.kw.rare_card", "event.kw.uncommon_card", "event.kw.card_reward",
    "event.kw.remove_card", "event.kw.remove_card_hp_cost", "event.kw.relic",
    "event.kw.relic_with_downside", "event.kw.upgrade", "event.kw.transform",
    "event.kw.gain_gold", "event.kw.gain_max_hp", "event.kw.potion",
    "event.kw.heal", "event.kw.colorless_card", "event.kw.gain_strength",
    "event.kw.enemy_strength", "event.kw.gain_dexterity",
    "event.kw.energy_each_turn", "event.kw.energy_once",
)

TRACE_KEYS = frozenset({
    # greedy_action itself
    "greedy.randomized", "greedy.decision_advisor", "greedy.unhandled",
    # card_reward rules
    "card_reward.no_cards", "card_reward.all_filtered", "card_reward.all_broken",
    "card_reward.below_threshold", "card_reward.pick_best",
    # card score components (pick_best_card sums the first nine, in this order)
    "card.base", "card.set_bonus", "card.tag_synergy", "card.combat_type",
    "card.boss_defense_gap", "card.boss_readiness", "card.quality_lift",
    "card.archetype", "card.q_adjust",
    "card.score", "card.in_deck_score",
    # rest_site rules, in rest_site_action's order
    "rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
    "rest.must_upgrade", "rest.heal_threshold", "rest.default_smith",
    "rest.fallback", "rest.no_choice",
    # event_choice
    "event.pick_best", "event.no_available", *EVENT_KEYWORD_KEYS,
    # bundle_select
    "bundle.single", "bundle.pick_best", "bundle.card",
    # card_select
    "card_select.rest_best", "card_select.combat_best", "card_select.top_scores",
    "card_select.lowest_scores", "card_select.no_cards",
    # shop rules, in greedy_action's order, and item score components
    "shop.heal_potion", "shop.remove_basic", "shop.remove_value",
    "shop.card_must_buy", "shop.card_threshold", "shop.relic", "shop.potion",
    "shop.leave", "shop.relic_score", "shop.potion_score",
    # map_select (HpAwareMapStrategy) rule and priority components
    "map.pick_min_priority", "map.base_priority", "map.shop_low_gold",
    "map.elite_early_floor", "map.elite_hp_danger", "map.elite_hp_low",
    "map.elite_act2_healthy", "map.elite_act2", "map.elite_strong_midgame",
    "map.elite_pre_boss", "map.elite_act2_boss_zone",
})

RULE_OUTCOMES = ("fired", "skipped")
OPTION_STATUSES = ("ok", "filtered", "locked", "unstocked", "unaffordable", "broken")


def _check_key(key: str) -> None:
    if key not in TRACE_KEYS:
        raise KeyError(f"unknown greedy trace key {key!r}")


def reset_greedy_context() -> None:
    """Put greedy's hidden per-process state back to its import-time value.

    card_reward rewrites card_scoring._MC_CONTEXT on every call and later
    decisions read it, so a replay that should match a fresh process must
    reset it at the start of each game."""
    from agent import card_scoring
    card_scoring._MC_CONTEXT.clear()
    card_scoring._MC_CONTEXT.update(MC_CONTEXT_START)


class GreedyTrace:
    """What one greedy_action call decided with. See the module docstring."""

    def __init__(self) -> None:
        self._options: dict[tuple[str, int], dict] = {}
        self.rules: list[dict] = []

    def option(self, index, *, kind: str, score=None, status: str = "ok",
               components=(), detail=None) -> None:
        """Record one option. `components` is [(key, value, detail-or-None)];
        for status "ok" their values sum (in order) to `score`."""
        if status not in OPTION_STATUSES:
            raise ValueError(f"unknown option status {status!r}")
        comps = []
        for key, value, extra in components:
            _check_key(key)
            item = {"key": key, "value": float(value)}
            if extra is not None:
                item["detail"] = extra
            comps.append(item)
        entry = {"kind": kind, "index": int(index),
                 "score": None if score is None else float(score),
                 "status": status, "components": comps}
        if detail is not None:
            entry["detail"] = detail
        self._options[(kind, int(index))] = entry

    def rule(self, key: str, outcome: str, **values) -> None:
        _check_key(key)
        if outcome not in RULE_OUTCOMES:
            raise ValueError(f"unknown rule outcome {outcome!r}")
        self.rules.append({"key": key, "outcome": outcome, "values": values})

    def score_of(self, kind: str, index) -> float | None:
        return self._options[(kind, int(index))]["score"]

    def ok_scores(self, kind: str) -> list[float]:
        return [o["score"] for (k, _), o in self._options.items()
                if k == kind and o["status"] == "ok" and o["score"] is not None]

    def fired(self) -> list[dict]:
        return [r for r in self.rules if r["outcome"] == "fired"]

    def to_dict(self) -> dict:
        return {"options": [self._options[k] for k in sorted(self._options)],
                "rules": list(self.rules)}
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace_recorder.py -q`
Expected: `6 passed`

- [ ] **Step 5: 提交**

```bash
git add agent/greedy_trace.py tests/agent/test_greedy_trace_recorder.py
git commit -m "feat(advisor): GreedyTrace recorder and trace key table

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A2: 测试语料和金标（在改 greedy 之前）

金标就是**现在**的 `greedy_action` 对每条真实状态给出的动作。后面每个任务都必须保持它不变（地图修复 Task A9 除外，它会有意改变一部分地图结果）。

**Files:**
- Create: `tools/build_advisor_corpus.py`
- Create: `tests/fixtures/advisor/ooc_corpus.jsonl.gz`（生成物）
- Create: `tests/agent/advisor_corpus.py`
- Test: `tests/agent/test_greedy_golden.py`

- [ ] **Step 1: 写生成脚本**

```python
# tools/build_advisor_corpus.py
"""Build tests/fixtures/advisor/ooc_corpus.jsonl.gz -- real out-of-combat
decision states from a kept run_batch, each with the action greedy_action
takes on it now. Those actions are the golden the greedy trace work must keep.

greedy has hidden per-process state (card_scoring._MC_CONTEXT, rewritten by
every card_reward), so each game's states are replayed in order and that state
is reset at every game start -- the tests replay the same way.

    .venv/bin/python tools/build_advisor_corpus.py \\
        --batch logs/batches/p3b-route-ab-20261012-160225
"""
import argparse
import glob
import gzip
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, reset_greedy_context  # noqa: E402

CHARACTERS = ("Ironclad", "Silent", "Defect", "Regent", "Necrobinder")
ADVISED = frozenset({"card_reward", "rest_site", "event_choice", "bundle_select",
                     "card_select", "shop", "map_select"})
DEFAULT_OUT = os.path.join(ROOT, "tests", "fixtures", "advisor", "ooc_corpus.jsonl.gz")


def _seed_number(path: str) -> int:
    return int(re.search(r"_(\d+)\.jsonl$", path).group(1))


def game_states(path: str):
    """Advised decision states of one game log, in play order."""
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            data = row.get("data")
            if (row.get("type") == "state" and isinstance(data, dict)
                    and data.get("type") == "decision"
                    and data.get("decision") in ADVISED):
                yield data


def lane_files(batch: str, character: str) -> list:
    pattern = os.path.join(batch, "games", f"{character}__random", "*.jsonl")
    return sorted(glob.glob(pattern), key=_seed_number)


def select_games(batch: str, per_character: int, bundle_games: int) -> list:
    picked = []
    for ch in CHARACTERS:
        picked += lane_files(batch, ch)[:per_character]
    # bundle_select is rare (15 in the whole p3b random arm): add games that have one.
    extra = []
    for ch in CHARACTERS:
        for path in lane_files(batch, ch):
            if len(extra) >= bundle_games:
                break
            if path not in picked and any(
                    s["decision"] == "bundle_select" for s in game_states(path)):
                extra.append(path)
    return picked + extra


def build_rows(files: list, greedy) -> list:
    rows = []
    for path in files:
        reset_greedy_context()
        game = os.path.basename(path)[: -len(".jsonl")]
        for seq, state in enumerate(game_states(path)):
            rows.append({"game": game, "seq": seq, "state": state,
                         "expected": greedy(state)})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", required=True, help="logs/batches/<id> with games/<Char>__random/")
    ap.add_argument("--per-character", type=int, default=2)
    ap.add_argument("--bundle-games", type=int, default=3)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    set_knobs = [k for k in GREEDY_BEHAVIOR_ENV if (os.environ.get(k) or "").strip()]
    if set_knobs:
        print(f"refusing: greedy behaviour env set: {set_knobs}", file=sys.stderr)
        return 2

    from agent.combat_env import greedy_action
    files = select_games(args.batch, args.per_character, args.bundle_games)
    rows = build_rows(files, greedy_action)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with gzip.open(args.out, "wt") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    counts = {}
    for row in rows:
        counts[row["state"]["decision"]] = counts.get(row["state"]["decision"], 0) + 1
    print(f"{len(files)} games, {len(rows)} states -> {args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 生成语料**

Run: `.venv/bin/python tools/build_advisor_corpus.py --batch logs/batches/p3b-route-ab-20261012-160225`
Expected：打印类似 `13 games, 1290 states -> .../ooc_corpus.jsonl.gz: {...}`。七种决策都要出现，`bundle_select` 至少 1 条。用 `ls -la tests/fixtures/advisor/` 确认文件不超过 1 MB（预估 300 KB）。如果 `bundle_select` 一条都没有，把 `--bundle-games` 调到 5 再跑；仍然没有就停下来报告。

- [ ] **Step 3: 写语料加载器**

```python
# tests/agent/advisor_corpus.py
"""Loader for tests/fixtures/advisor/ooc_corpus.jsonl.gz
(built by tools/build_advisor_corpus.py)."""
import gzip
import itertools
import json
import os

CORPUS = os.path.join(os.path.dirname(__file__), "..", "fixtures", "advisor",
                      "ooc_corpus.jsonl.gz")


def corpus_rows() -> list:
    with gzip.open(CORPUS, "rt") as f:
        return [json.loads(line) for line in f]


def corpus_games() -> list:
    """[(game, rows in play order)] in file order."""
    return [(game, list(rows)) for game, rows in
            itertools.groupby(corpus_rows(), key=lambda r: r["game"])]
```

- [ ] **Step 4: 写金标测试**

```python
# tests/agent/test_greedy_golden.py
"""greedy_action must keep choosing what it chose when the corpus was built
(tools/build_advisor_corpus.py). Replays each game in order -- greedy has
hidden state, see agent/greedy_trace.reset_greedy_context."""
import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, reset_greedy_context
from tests.agent.advisor_corpus import corpus_games, corpus_rows


@pytest.fixture(autouse=True)
def _default_greedy_env(monkeypatch):
    for key in GREEDY_BEHAVIOR_ENV:
        monkeypatch.delenv(key, raising=False)


def test_corpus_covers_every_advised_decision():
    seen = {row["state"]["decision"] for row in corpus_rows()}
    assert seen == {"card_reward", "rest_site", "event_choice", "bundle_select",
                    "card_select", "shop", "map_select"}


def test_greedy_matches_golden():
    mismatches = []
    for game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            got = greedy_action(row["state"])
            if got != row["expected"]:
                mismatches.append((game, row["seq"], row["state"]["decision"],
                                   row["expected"], got))
    assert not mismatches, mismatches[:5]
```

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_golden.py -q`
Expected: `2 passed`（这是刻画测试，生成后立刻就应该通过；不通过说明 greedy 本身不确定，停下来报告）

- [ ] **Step 6: 提交**

```bash
git add tools/build_advisor_corpus.py tests/fixtures/advisor/ooc_corpus.jsonl.gz tests/agent/advisor_corpus.py tests/agent/test_greedy_golden.py
git commit -m "test(advisor): real-state corpus and greedy golden actions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A3: `greedy_action` 加 trace 参数；卡牌奖励的依据

**Files:**
- Modify: `agent/combat_env.py:448-561`（`greedy_action` 开头和 card_reward 分支）、`agent/combat_env.py` 末尾的 `return {"cmd": "action", "action": "proceed"}`
- Modify: `agent/card_scoring.py:2483`（`pick_best_card`）
- Test: `tests/agent/test_greedy_trace.py`、`tests/agent/test_greedy_golden.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/agent/test_greedy_trace.py
"""greedy_action(state, trace=...) records the evidence it decided with, and the
evidence agrees with the choice. Replays the corpus in game order."""
import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, GreedyTrace, reset_greedy_context
from tests.agent.advisor_corpus import corpus_games


@pytest.fixture(autouse=True)
def _default_greedy_env(monkeypatch):
    for key in GREEDY_BEHAVIOR_ENV:
        monkeypatch.delenv(key, raising=False)


def traced_corpus(decision=None):
    """(row, action, trace) for every corpus state of `decision` (None = all).
    Every state is replayed (traced) so greedy's hidden context stays in step."""
    for _game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            trace = GreedyTrace()
            action = greedy_action(row["state"], trace=trace)
            if decision is None or row["state"]["decision"] == decision:
                yield row, action, trace


def _sums_match(trace):
    for opt in trace.to_dict()["options"]:
        if opt["status"] == "ok" and opt["components"]:
            total = sum(c["value"] for c in opt["components"])
            assert abs(total - opt["score"]) < 1e-9, opt


def test_trace_does_not_change_any_choice():
    for row, action, _trace in traced_corpus():
        assert action == row["expected"], (row["game"], row["seq"])


def test_card_reward_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("card_reward"):
        fired = trace.fired()
        assert len(fired) == 1, (row["game"], row["seq"], trace.rules)
        rule = fired[0]
        if action["action"] == "select_card_reward":
            idx = action["args"]["card_index"]
            assert rule["key"] == "card_reward.pick_best"
            assert trace.score_of("card", idx) == max(trace.ok_scores("card"))
            assert trace.score_of("card", idx) >= rule["values"]["threshold"]
        else:
            assert action["action"] == "skip_card_reward"
            assert rule["key"] in {"card_reward.below_threshold", "card_reward.all_filtered",
                                   "card_reward.all_broken", "card_reward.no_cards"}
            if rule["key"] == "card_reward.below_threshold":
                assert max(trace.ok_scores("card")) < rule["values"]["threshold"]
        _sums_match(trace)
        n += 1
    assert n > 0


def test_card_reward_components_are_the_nine_summed_terms():
    for _row, _action, trace in traced_corpus("card_reward"):
        for opt in trace.to_dict()["options"]:
            if opt["status"] == "ok":
                assert [c["key"] for c in opt["components"]] == [
                    "card.base", "card.set_bonus", "card.tag_synergy", "card.combat_type",
                    "card.boss_defense_gap", "card.boss_readiness", "card.quality_lift",
                    "card.archetype", "card.q_adjust"]
        break
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py -q`
Expected: FAIL，`TypeError: greedy_action() got an unexpected keyword argument 'trace'`

- [ ] **Step 3: 改 `pick_best_card`**

签名改为：

```python
def pick_best_card(cards: list[dict], threshold: float = 3.5,
                   deck: list[dict] = None, trace=None,
                   trace_indices: list[int] | None = None) -> int | None:
```

docstring 末尾加一段：

```python
    """...(原有内容不变)...

    trace (agent.greedy_trace.GreedyTrace) records each card's nine summed
    score terms -- the same lists `totals` is built from -- and BROKEN_CARDS
    rejections. trace_indices maps a position in `cards` to the index the
    caller reports (card_reward passes the reward's own indices after the
    quality gate filtered some out)."""
```

在 `valid = [...]` 这一行之后、`if not valid:` 之前插入：

```python
    def _reported(i: int) -> int:
        return trace_indices[i] if trace_indices is not None else i
    if trace is not None:
        valid_positions = {i for i, _ in valid}
        for i in range(len(cards)):
            if i not in valid_positions:
                trace.option(_reported(i), kind="card", status="broken")
```

在 `totals = [...]` 计算之后、`scored = sorted(...)` 之前插入（**不要改 `totals` 的算式**；trace 只记录已经算好的 9 个列表，顺序和 `totals` 求和的顺序相同，所以分项相加严格等于总分）：

```python
    if trace is not None:
        ratings = _load_advisor_ratings()
        for k, (orig_i, c) in enumerate(valid):
            tier = (ratings.get(_card_id_norm(c)) or {}).get("tier")
            trace.option(_reported(orig_i), kind="card", score=totals[k], components=[
                ("card.base", base[k], tier),
                ("card.set_bonus", set_bonus[k], None),
                ("card.tag_synergy", tag_bonus[k], None),
                ("card.combat_type", ct_bonus[k], None),
                ("card.boss_defense_gap", bd_bonus[k], None),
                ("card.boss_readiness", br_bonus[k], None),
                ("card.quality_lift", q_bonus[k], None),
                ("card.archetype", arch_bonus[k], None),
                ("card.q_adjust", q_adj[k], None),
            ])
```

- [ ] **Step 4: 改 `greedy_action` 开头**

`agent/combat_env.py:448` 起：

```python
def greedy_action(state: dict, trace=None) -> dict:
    """Greedy heuristic for non-combat decisions. Used during training and by coordinator.

    trace: optional agent.greedy_trace.GreedyTrace that records the evidence each
    decision was made with (the in-game advisor's reasons). None records nothing
    and takes the same code path."""
    decision = state.get("decision", "")
    if _RANDOMIZE_DECISIONS:
        _rnd = _random_choice_for(state)
        if _rnd is not None:
            if trace is not None:
                trace.rule("greedy.randomized", "fired", decision=decision)
            return _rnd
    if _decision_advisor_enabled():
        advised = _decision_advisor.choose(state)
        if advised is not None:
            if trace is not None:
                trace.rule("greedy.decision_advisor", "fired", decision=decision)
            return advised
```

函数最后一行 `return {"cmd": "action", "action": "proceed"}` 改为：

```python
    if trace is not None:
        trace.rule("greedy.unhandled", "fired", decision=decision)
    return {"cmd": "action", "action": "proceed"}
```

- [ ] **Step 5: 改 card_reward 分支**

1. `threshold += _card_threshold_lift()` 改为两行：

```python
            lift = _card_threshold_lift()
            threshold += lift
```

2. 原来的 `if _card_quality_gate_enabled() and late_act1:` 改为先算一个变量再判断（判断内容不变）：

```python
            gate_on = _card_quality_gate_enabled() and late_act1
            if gate_on:
```

3. 从 `eligible_cards = [card for _, card in indexed_cards]` 到分支末尾 `return {"cmd": "action", "action": "skip_card_reward"}` 整段替换为：

```python
            if trace is not None:
                kept = {i for i, _ in indexed_cards}
                for i in range(len(cards)):
                    if i not in kept:
                        trace.option(i, kind="card", status="filtered")
                reward_values = {"threshold": threshold, "lift": lift, "act": _act,
                                 "deck_size": deck_size, "quality_gate": gate_on}
            eligible_cards = [card for _, card in indexed_cards]
            if not eligible_cards:
                if trace is not None:
                    trace.rule("card_reward.all_filtered", "fired", **reward_values)
                return {"cmd": "action", "action": "skip_card_reward"}
            best_eligible = pick_best_card(
                eligible_cards, threshold=threshold, deck=deck,
                trace=trace, trace_indices=[i for i, _ in indexed_cards],
            )
            if best_eligible is not None:
                best = indexed_cards[best_eligible][0]
                if trace is not None:
                    trace.rule("card_reward.pick_best", "fired", best_index=best,
                               best_score=trace.score_of("card", best), **reward_values)
                return {"cmd": "action", "action": "select_card_reward",
                        "args": {"card_index": best}}
            if trace is not None:
                ok = trace.ok_scores("card")
                if ok:
                    trace.rule("card_reward.below_threshold", "fired",
                               best_score=max(ok), **reward_values)
                else:
                    trace.rule("card_reward.all_broken", "fired", **reward_values)
        elif trace is not None:
            trace.rule("card_reward.no_cards", "fired")
        return {"cmd": "action", "action": "skip_card_reward"}
```

注意最后的 `elif` 挂在 `if cards:` 上（缩进 8 格），`return` 仍是 card_reward 分支的最后一行（缩进 8 格），和原来一样。

- [ ] **Step 6: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_card_scoring.py tests/agent/test_combat_env.py -q`
Expected: 全部通过。`test_trace_does_not_change_any_choice` 此时就会跑全部语料；它通过说明给其他决策类型传 `trace` 不会出错（这些分支还没用到 trace）。

- [ ] **Step 7: 提交**

```bash
git add agent/combat_env.py agent/card_scoring.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): greedy_action(trace=...) records card reward evidence

pick_best_card records the nine summed score terms per card, exactly the
lists totals is built from; card_reward records the deciding rule
(pick_best / below_threshold / all_filtered / all_broken / no_cards).
The corpus golden is unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A4: 篝火的依据

**Files:**
- Modify: `agent/strategy.py:128-224`（`rest_site_action`）
- Modify: `agent/combat_env.py`（rest_site 分支）
- Test: `tests/agent/test_greedy_trace.py`

- [ ] **Step 1: 写失败的测试**（追加到 `tests/agent/test_greedy_trace.py`）

```python
HEAL_RULES = {"rest.critical_heal", "rest.vantom_mid_boss", "rest.pre_boss_heal",
              "rest.heal_threshold"}
SMITH_RULES = {"rest.must_upgrade", "rest.default_smith"}


def test_rest_site_rules_stop_at_the_deciding_one():
    n = 0
    for row, action, trace in traced_corpus("rest_site"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        assert trace.rules[-1] is fired[0]
        assert all(r["outcome"] == "skipped" for r in trace.rules[:-1])
        key = fired[0]["key"]
        if action["action"] == "choose_option":
            idx = action["args"]["option_index"]
            opt = next(o for o in row["state"]["options"] if o["index"] == idx)
            if key in HEAL_RULES:
                assert "heal" in opt["option_id"].lower()
            elif key in SMITH_RULES:
                assert "smith" in opt["option_id"].lower()
            else:
                assert key == "rest.fallback"
        else:
            assert key == "rest.no_choice"
        n += 1
    assert n > 0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py::test_rest_site_rules_stop_at_the_deciding_one -q`
Expected: FAIL，`assert len(fired) == 1`（还没有记录任何规则）

- [ ] **Step 3: 改 `rest_site_action`**

`agent/strategy.py` 的 import 改为：

```python
from agent.card_scoring import SMITH_PRIORITY_THRESHOLD, best_smith_target
```

签名改为 `def rest_site_action(state: dict, options: list[dict], trace=None) -> dict:`。docstring 末尾加一句：`trace (agent.greedy_trace.GreedyTrace) records each rule checked, in order, up to the one that decided.`

把 `has_must_upgrade = smith is not None and best_smith_target(deck) is not None` 改为：

```python
    smith_target = best_smith_target(deck) if smith is not None else None
    has_must_upgrade = smith_target is not None
```

从 `if hp_ratio < CRITICAL_HEAL and heal is not None:` 到函数末尾整段替换为（条件和顺序与原来的 if/elif 链一一对应，全部是无副作用的布尔表达式）：

```python
    checks = (
        ("rest.critical_heal", hp_ratio < CRITICAL_HEAL and heal is not None, heal,
         {"hp_ratio": hp_ratio, "threshold": CRITICAL_HEAL, "floor": floor}),
        ("rest.vantom_mid_boss", vantom_mid_boss_low and heal is not None, heal,
         {"enabled": vantom_rest_heal_enabled, "hp_ratio": hp_ratio, "floor": floor}),
        ("rest.pre_boss_heal",
         pre_boss_zone and hp_ratio < heal_threshold and heal is not None, heal,
         {"hp_ratio": hp_ratio, "threshold": heal_threshold, "floor": floor}),
        ("rest.must_upgrade", has_must_upgrade, smith,
         {"deck_index": smith_target, "threshold": SMITH_PRIORITY_THRESHOLD}),
        ("rest.heal_threshold", hp_ratio < heal_threshold and heal is not None, heal,
         {"hp_ratio": hp_ratio, "threshold": heal_threshold, "floor": floor}),
        ("rest.default_smith", smith is not None, smith, {}),
    )
    choice = None
    for key, applies, option, values in checks:
        if applies:
            choice = option
            if trace is not None:
                trace.rule(key, "fired", **values)
            break
        if trace is not None:
            trace.rule(key, "skipped", **values)
    else:
        choice = heal or (enabled[0] if enabled else None)
        if trace is not None and choice:
            trace.rule("rest.fallback", "fired")

    if choice:
        return {"cmd": "action", "action": "choose_option",
                "args": {"option_index": choice["index"]}}
    if trace is not None:
        trace.rule("rest.no_choice", "fired")
    return {"cmd": "action", "action": "proceed"}
```

- [ ] **Step 4: greedy 里传 trace**

`agent/combat_env.py` 的 rest_site 分支：

```python
    elif decision == "rest_site":
        return rest_site_action(state, state.get("options", []), trace=trace)
```

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_combat_env.py -q`
Expected: 全部通过（`test_combat_env.py` 里有 4 个直接调用 `rest_site_action` 的测试）

- [ ] **Step 6: 提交**

```bash
git add agent/strategy.py agent/combat_env.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): rest_site_action records its rule chain

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A5: 事件的依据

**Files:**
- Modify: `agent/combat_env.py:246-337`（`_score_event_option`）和 event_choice 分支
- Test: `tests/agent/test_greedy_trace.py`

- [ ] **Step 1: 写失败的测试**（追加）

```python
def test_event_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("event_choice"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        if action["action"] == "choose_option":
            idx = action["args"]["option_index"]
            assert fired[0]["key"] == "event.pick_best"
            assert trace.score_of("option", idx) == max(trace.ok_scores("option"))
        else:
            assert fired[0]["key"] == "event.no_available"
        locked = {o["index"] for o in row["state"]["options"] if o.get("is_locked")}
        for opt in trace.to_dict()["options"]:
            assert (opt["status"] == "locked") == (opt["index"] in locked)
        _sums_match(trace)
        n += 1
    assert n > 0


def test_event_keyword_hits_are_recorded():
    from agent.combat_env import _score_event_option
    hits = []
    score = _score_event_option(
        {"title": "Obtain a Rare card", "description": "Lose 5 HP."}, hits)
    assert [h[0] for h in hits] == ["event.kw.lose_hp", "event.kw.rare_card"]
    assert score == sum(h[1] for h in hits) == 6.0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py -k event -q`
Expected: FAIL（`_score_event_option() takes 1 positional argument but 2 were given`）

- [ ] **Step 3: 改 `_score_event_option`**

在函数前加一个小工具函数：

```python
def _kw(score: float, hits, key: str, delta: float) -> float:
    """One _score_event_option keyword branch: add delta, note it in hits."""
    if hits is not None:
        hits.append((key, delta, None))
    return score + delta
```

签名改为 `def _score_event_option(opt: dict, hits: list | None = None) -> float:`，docstring 加一句 `hits: if a list, gets (trace key, delta, None) for every branch that fired.`

把函数里每一条 `score += X` / `score -= X` 改成 `score = _kw(score, hits, "<key>", ±X)`，条件判断一个字都不动。按源码顺序对应如下（第一列是原来的那行）：

| 原来 | 改成 |
|---|---|
| `score -= 10.0`（`_lose_max_hp`） | `score = _kw(score, hits, "event.kw.lose_max_hp", -10.0)` |
| `score -= 8.0`（curse） | `score = _kw(score, hits, "event.kw.curse", -8.0)` |
| `score -= 5.0`（lose all gold） | `score = _kw(score, hits, "event.kw.lose_all_gold", -5.0)` |
| `score -= 2.0`（elif lose gold） | `score = _kw(score, hits, "event.kw.lose_gold", -2.0)` |
| `score -= 5.0`（torment） | `score = _kw(score, hits, "event.kw.torment", -5.0)` |
| `score -= 2.0`（take damage） | `score = _kw(score, hits, "event.kw.take_damage", -2.0)` |
| `score -= 2.0`（lose hp） | `score = _kw(score, hits, "event.kw.lose_hp", -2.0)` |
| `score -= 4.0`（downgrade） | `score = _kw(score, hits, "event.kw.downgrade", -4.0)` |
| `score -= 3.0`（additional strike/defend） | `score = _kw(score, hits, "event.kw.add_basic", -3.0)` |
| `score += 8.0`（rare） | `score = _kw(score, hits, "event.kw.rare_card", 8.0)` |
| `score += 4.0`（elif uncommon） | `score = _kw(score, hits, "event.kw.uncommon_card", 4.0)` |
| `score += 2.0`（elif generic card） | `score = _kw(score, hits, "event.kw.card_reward", 2.0)` |
| `score += 6.0`（remove card） | `score = _kw(score, hits, "event.kw.remove_card", 6.0)` |
| `score -= 3.0`（remove + hp） | `score = _kw(score, hits, "event.kw.remove_card_hp_cost", -3.0)` |
| `score += 5.0`（relic, no add） | `score = _kw(score, hits, "event.kw.relic", 5.0)` |
| `score += 2.0`（elif relic） | `score = _kw(score, hits, "event.kw.relic_with_downside", 2.0)` |
| `score += 4.0`（upgrade） | `score = _kw(score, hits, "event.kw.upgrade", 4.0)` |
| `score += 3.0`（transform） | `score = _kw(score, hits, "event.kw.transform", 3.0)` |
| `score += 3.0`（gain gold） | `score = _kw(score, hits, "event.kw.gain_gold", 3.0)` |
| `score += 3.0`（max hp gain） | `score = _kw(score, hits, "event.kw.gain_max_hp", 3.0)` |
| `score += 2.0`（potion） | `score = _kw(score, hits, "event.kw.potion", 2.0)` |
| `score += 2.0`（heal） | `score = _kw(score, hits, "event.kw.heal", 2.0)` |
| `score += 2.0`（colorless） | `score = _kw(score, hits, "event.kw.colorless_card", 2.0)` |
| `score += 4.0`（strength gain） | `score = _kw(score, hits, "event.kw.gain_strength", 4.0)` |
| `score -= 3.0`（elif enemy strength） | `score = _kw(score, hits, "event.kw.enemy_strength", -3.0)` |
| `score += 3.0`（dexterity） | `score = _kw(score, hits, "event.kw.gain_dexterity", 3.0)` |
| `score += 5.0`（energy each turn） | `score = _kw(score, hits, "event.kw.energy_each_turn", 5.0)` |
| `score += 1.5`（elif energy gain） | `score = _kw(score, hits, "event.kw.energy_once", 1.5)` |

`x - y` 和 `x + (-y)` 在 IEEE 754 下结果完全相同，所以分数不变。

- [ ] **Step 4: 改 event_choice 分支**

```python
    elif decision == "event_choice":
        options = state.get("options", [])
        available = [o for o in options if not o.get("is_locked")]
        if trace is not None:
            for o in options:
                if o.get("is_locked"):
                    trace.option(o["index"], kind="option", status="locked")
        if available:
            def _event_key(o):
                hits = [] if trace is not None else None
                score = _score_event_option(o, hits)
                if trace is not None:
                    trace.option(o["index"], kind="option", score=score, components=hits)
                return score
            best = max(available, key=_event_key)
            if trace is not None:
                trace.rule("event.pick_best", "fired", best_index=best["index"],
                           n_options=len(options))
            return {"cmd": "action", "action": "choose_option",
                    "args": {"option_index": best["index"]}}
        if trace is not None:
            trace.rule("event.no_available", "fired", n_options=len(options))
        return {"cmd": "action", "action": "leave_room"}
```

`max` 对每个元素只调用一次 key，所以记录下来的分数就是 `max` 用来比较的那个值。

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_combat_env.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add agent/combat_env.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): event options record their keyword hits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A6: 卷轴盒和选牌的依据

**Files:**
- Modify: `agent/combat_env.py`（bundle_select、card_select 分支）
- Test: `tests/agent/test_greedy_trace.py`

- [ ] **Step 1: 写失败的测试**（追加）

```python
def test_bundle_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("bundle_select"):
        fired = trace.fired()
        assert len(fired) == 1
        idx = action["args"]["bundle_index"]
        if fired[0]["key"] == "bundle.pick_best":
            assert trace.score_of("bundle", idx) == max(trace.ok_scores("bundle"))
            _sums_match(trace)
        else:
            assert fired[0]["key"] == "bundle.single" and idx == 0
        n += 1
    assert n > 0


def test_card_select_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("card_select"):
        fired = trace.fired()
        assert len(fired) == 1, trace.rules
        key = fired[0]["key"]
        if action["action"] == "skip_select":
            assert key == "card_select.no_cards"
            continue
        picked = [int(i) for i in action["args"]["indices"].split(",")]
        scores = {o["index"]: o["score"] for o in trace.to_dict()["options"]
                  if o["status"] == "ok"}
        if key in {"card_select.rest_best", "card_select.combat_best"}:
            assert len(picked) == 1
            if scores:
                assert scores[picked[0]] == max(scores.values())
            else:
                assert picked == [0]
        else:
            assert key in {"card_select.top_scores", "card_select.lowest_scores"}
            ranked = sorted(scores, key=lambda i: scores[i],
                            reverse=(key == "card_select.top_scores"))
            want = [scores[i] for i in ranked[:len(picked)]]
            assert [scores[i] for i in picked] == want
        n += 1
    assert n > 0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py -k "bundle or card_select" -q`
Expected: FAIL（`assert len(fired) == 1`）

- [ ] **Step 3: 改 bundle_select 分支**

```python
    elif decision == "bundle_select":
        bundles = state.get("bundles", [])
        if len(bundles) >= 2:
            scores = []
            for bi, b in enumerate(bundles):
                card_scores = [score_card(c) for c in b.get("cards", [])]
                scores.append(sum(card_scores))
                if trace is not None:
                    trace.option(bi, kind="bundle", score=scores[-1], components=[
                        ("bundle.card", s, ci) for ci, s in enumerate(card_scores)])
            best_idx = scores.index(max(scores))
            if trace is not None:
                trace.rule("bundle.pick_best", "fired", best_index=best_idx)
        else:
            best_idx = 0
            if trace is not None:
                trace.rule("bundle.single", "fired", n_bundles=len(bundles))
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": best_idx}}
```

原来是 `sum(score_card(c) for c in ...)`，现在先放进列表再 `sum`：相加顺序相同，结果相同。

- [ ] **Step 4: 改 card_select 分支**

```python
    elif decision == "card_select":
        cards = state.get("cards", [])
        if cards:
            room_type = state.get("context", {}).get("room_type", "")
            max_sel = max(state.get("max_select", 1), 1)
            select_values = {"room_type": room_type, "n_cards": len(cards),
                             "max_select": max_sel}
            if "rest" in room_type.lower():
                best = pick_best_card(cards, threshold=0.0, trace=trace)
                idx = best if best is not None else 0
                if trace is not None:
                    trace.rule("card_select.rest_best", "fired", picked=idx, **select_values)
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            elif room_type in ("Boss", "Monster", "Elite", "CombatRoom") or not room_type:
                best = pick_best_card(cards, threshold=0.0, trace=trace)
                idx = best if best is not None else 0
                if trace is not None:
                    trace.rule("card_select.combat_best", "fired", picked=idx, **select_values)
                return {"cmd": "action", "action": "select_cards", "args": {"indices": str(idx)}}
            else:
                card_scores = [score_card(c) for c in cards]
                has_junk = any(s < 3.0 for s in card_scores)
                is_deck_selection = has_junk or len(cards) > 10
                if not is_deck_selection:
                    scored_by = sorted(enumerate(cards), key=lambda x: card_scores[x[0]], reverse=True)
                else:
                    scored_by = sorted(enumerate(cards), key=lambda x: card_scores[x[0]])
                selected = [str(scored_by[k][0]) for k in range(min(max_sel, len(scored_by)))]
                if trace is not None:
                    for i, s in enumerate(card_scores):
                        trace.option(i, kind="card", score=s, components=[("card.score", s, None)])
                    trace.rule("card_select.lowest_scores" if is_deck_selection
                               else "card_select.top_scores", "fired",
                               has_junk=has_junk, picked=[int(i) for i in selected],
                               **select_values)
                return {"cmd": "action", "action": "select_cards",
                        "args": {"indices": ",".join(selected)}}
        if trace is not None:
            trace.rule("card_select.no_cards", "fired")
        return {"cmd": "action", "action": "skip_select"}
```

原来 `has_junk` 和两处排序各自调用 `score_card`，现在共用一次算好的 `card_scores`。`score_card` 是纯函数，结果相同；`sorted` 是稳定排序，并列时顺序也不变。

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_combat_env.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add agent/combat_env.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): bundle and card_select record their scores and mode

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A7: 商店的依据

**Files:**
- Modify: `agent/combat_env.py`（shop 分支，从 `elif decision == "shop":` 到 `return {"cmd": "action", "action": "leave_room"}`）
- Test: `tests/agent/test_greedy_trace.py`

- [ ] **Step 1: 写失败的测试**（追加）

```python
SHOP_RULES = ["shop.heal_potion", "shop.remove_basic", "shop.remove_value",
              "shop.card_must_buy", "shop.card_threshold", "shop.relic",
              "shop.potion", "shop.leave"]
SHOP_ACTION = {"shop.heal_potion": "buy_potion", "shop.remove_basic": "remove_card",
               "shop.remove_value": "remove_card", "shop.card_must_buy": "buy_card",
               "shop.card_threshold": "buy_card", "shop.relic": "buy_relic",
               "shop.potion": "buy_potion", "shop.leave": "leave_room"}


def test_shop_rule_chain_matches_action():
    n = 0
    for row, action, trace in traced_corpus("shop"):
        keys = [r["key"] for r in trace.rules]
        fired = trace.fired()
        assert len(fired) == 1 and trace.rules[-1] is fired[0], keys
        assert keys == SHOP_RULES[:len(keys)]
        assert SHOP_ACTION[fired[0]["key"]] == action["action"]
        if action["action"] == "buy_card":
            idx = action["args"]["card_index"]
            assert trace.score_of("card", idx) == max(trace.ok_scores("card"))
        n += 1
    assert n > 0


def test_shop_remove_basic_reports_its_ironclad_only_scope():
    for row, _action, trace in traced_corpus("shop"):
        rule = next((r for r in trace.rules if r["key"] == "shop.remove_basic"), None)
        if rule is not None:
            assert rule["values"]["basic_scope"] == "ironclad_only"
            return
    pytest.fail("no shop state reached shop.remove_basic")
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py -k shop -q`
Expected: FAIL

- [ ] **Step 3: 替换 shop 分支**

整段替换为下面的代码。每条规则的条件、顺序、返回的命令都和原来一样；新增的只有 `_fire` / `_skip` 记录，以及把 max 的 key 换成"打分 + 记录"的包装函数（每个元素仍只打分一次）。

```python
    elif decision == "shop":
        gold = state.get("player", {}).get("gold", 0)
        player = state.get("player", {})
        hp_ratio = player.get("hp", 80) / max(player.get("max_hp", 80), 1)
        held_potions = len(player.get("potions", []))
        floor = state.get("floor") or state.get("context", {}).get("floor", 0)
        removal_cost = state.get("card_removal_cost")
        pre_boss = isinstance(floor, int) and floor >= 11

        def _skip(key, **values):
            if trace is not None:
                trace.rule(key, "skipped", **values)

        def _fire(key, cmd, **values):
            if trace is not None:
                trace.rule(key, "fired", **values)
            return cmd

        heal_potion = None
        if hp_ratio < 0.50 and held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            for sp in shop_potions:
                sp_name = (sp.get("name") or {})
                sp_name = (sp_name.get("en", "") if isinstance(sp_name, dict) else str(sp_name)).lower()
                sp_desc = (sp.get("description") or {})
                sp_desc = (sp_desc.get("en", "") if isinstance(sp_desc, dict) else str(sp_desc)).lower()
                if ("heal" in sp_name + sp_desc or "restore" in sp_name + sp_desc) and "curse" not in sp_name + sp_desc:
                    heal_potion = sp
                    break
        heal_values = {"hp_ratio": hp_ratio, "held_potions": held_potions}
        if heal_potion is not None:
            return _fire("shop.heal_potion",
                         {"cmd": "action", "action": "buy_potion",
                          "args": {"potion_index": heal_potion.get("index", 0)}},
                         potion_index=heal_potion.get("index", 0), **heal_values)
        _skip("shop.heal_potion", **heal_values)

        _act = state.get("act") or (state.get("context") or {}).get("act")
        in_act2 = isinstance(_act, int) and _act >= 2
        deck = state.get("player", {}).get("deck", []) or []

        purge_all = (os.environ.get("STS2_BASIC_PURGE_ALL", "").strip().lower()
                     in {"1", "true", "on"})
        _basic_ids = _BASIC_CARD_IDS if purge_all else ("STRIKE_IRONCLAD", "DEFEND_IRONCLAD")
        n_basic = sum(1 for c in deck if _card_id_norm(c) in _basic_ids)
        basic_values = {"n_basic": n_basic,
                        "basic_scope": "all" if purge_all else "ironclad_only",
                        "removal_cost": removal_cost, "gold": gold}
        if removal_cost and gold >= removal_cost and n_basic >= 3:
            return _fire("shop.remove_basic", {"cmd": "action", "action": "remove_card"},
                         **basic_values)
        _skip("shop.remove_basic", **basic_values)

        from agent.card_scoring import removal_value as _rv
        rv = _rv(deck)
        _cards_peek = [c for c in state.get("cards", [])
                        if c.get("is_stocked") and c.get("cost", 999) <= gold]
        if _cards_peek:
            _best_score = max(score_card_in_deck(c, deck) for c in _cards_peek)
        else:
            _best_score = 0.0
        rv_values = {"removal_value": rv, "best_card_score": _best_score,
                     "removal_cost": removal_cost, "gold": gold}
        if removal_cost and gold >= removal_cost and rv > max(_best_score, 3.0):
            return _fire("shop.remove_value", {"cmd": "action", "action": "remove_card"},
                         **rv_values)
        _skip("shop.remove_value", **rv_values)

        cards_avail = [c for c in state.get("cards", [])
                       if c.get("is_stocked") and c.get("cost", 999) <= gold]
        if trace is not None:
            for c in state.get("cards", []):
                if not c.get("is_stocked"):
                    trace.option(c.get("index", 0), kind="card", status="unstocked")
                elif c.get("cost", 999) > gold:
                    trace.option(c.get("index", 0), kind="card", status="unaffordable")

        def _card_key(c):
            s = score_card_in_deck(c, deck)
            if trace is not None:
                trace.option(c.get("index", 0), kind="card", score=s,
                             components=[("card.in_deck_score", s, None)])
            return s
        best_card = max(cards_avail, key=_card_key) if cards_avail else None
        best_score = score_card_in_deck(best_card, deck) if best_card else 0.0
        best_card_index = best_card.get("index", 0) if best_card else None
        if best_card and best_score >= 8.0:
            return _fire("shop.card_must_buy",
                         {"cmd": "action", "action": "buy_card",
                          "args": {"card_index": best_card.get("index", 0)}},
                         best_card_index=best_card_index, best_card_score=best_score,
                         threshold=8.0)
        _skip("shop.card_must_buy", best_card_index=best_card_index,
              best_card_score=best_score, threshold=8.0)
        if in_act2:
            card_buy_threshold = 4.5
        elif pre_boss:
            card_buy_threshold = 5.0
        else:
            card_buy_threshold = 5.5
        threshold_values = {"best_card_index": best_card_index, "best_card_score": best_score,
                            "threshold": card_buy_threshold, "act": _act, "pre_boss": pre_boss}
        if best_card and best_score >= card_buy_threshold:
            return _fire("shop.card_threshold",
                         {"cmd": "action", "action": "buy_card",
                          "args": {"card_index": best_card.get("index", 0)}},
                         **threshold_values)
        _skip("shop.card_threshold", **threshold_values)

        RELIC_GOLD_THRESHOLD = 25
        relics = [r for r in state.get("relics", [])
                  if r.get("is_stocked") and r.get("cost", 999) <= gold - RELIC_GOLD_THRESHOLD]
        if trace is not None:
            for r in state.get("relics", []):
                if not r.get("is_stocked"):
                    trace.option(r.get("index", 0), kind="relic", status="unstocked")
                elif r.get("cost", 999) > gold - RELIC_GOLD_THRESHOLD:
                    trace.option(r.get("index", 0), kind="relic", status="unaffordable")

        def _relic_key(r):
            s = _score_shop_relic(r)
            if trace is not None:
                trace.option(r.get("index", 0), kind="relic", score=s,
                             components=[("shop.relic_score", s, None)])
            return s
        relic_values = {"threshold": 4.0, "gold_reserve": RELIC_GOLD_THRESHOLD,
                        "n_affordable": len(relics)}
        if relics:
            best_relic = max(relics, key=_relic_key)
            relic_values["best_relic_index"] = best_relic.get("index", 0)
            relic_values["best_relic_score"] = _score_shop_relic(best_relic)
            if _score_shop_relic(best_relic) >= 4.0:
                return _fire("shop.relic",
                             {"cmd": "action", "action": "buy_relic",
                              "args": {"relic_index": best_relic.get("index", 0)}},
                             **relic_values)
        _skip("shop.relic", **relic_values)

        potion_values = {"threshold": 4.5, "held_potions": held_potions}
        if held_potions < 3:
            shop_potions = [p for p in state.get("potions", [])
                            if p.get("is_stocked") and p.get("cost", 999) <= gold]
            if shop_potions:
                def _potion_key(p):
                    s = _score_shop_potion(p)
                    if trace is not None:
                        trace.option(p.get("index", 0), kind="potion", score=s,
                                     components=[("shop.potion_score", s, None)])
                    return s
                best_potion = max(shop_potions, key=_potion_key)
                potion_values["best_potion_index"] = best_potion.get("index", 0)
                potion_values["best_potion_score"] = _score_shop_potion(best_potion)
                if _score_shop_potion(best_potion) >= 4.5:
                    return _fire("shop.potion",
                                 {"cmd": "action", "action": "buy_potion",
                                  "args": {"potion_index": best_potion.get("index", 0)}},
                                 **potion_values)
        _skip("shop.potion", **potion_values)
        return _fire("shop.leave", {"cmd": "action", "action": "leave_room"})
```

原分支里买回血药水时，返回的是循环里**第一瓶**符合条件的药水；现在用 `break` 取第一瓶，行为相同。

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_combat_env.py tests/test_ooc_policy.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add agent/combat_env.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): shop records its rule chain and item scores

shop.remove_basic carries basic_scope (ironclad_only unless
STS2_BASIC_PURGE_ALL), shown as-is: that is the configuration the A/B
measured.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A8: 地图的依据（行为不变）

**Files:**
- Modify: `agent/strategy.py:59-124`（`HpAwareMapStrategy.choose`）
- Modify: `agent/combat_env.py`（map_select 分支）
- Test: `tests/agent/test_greedy_trace.py`

- [ ] **Step 1: 写失败的测试**（追加）

```python
def test_map_evidence_matches_choice():
    n = 0
    for row, action, trace in traced_corpus("map_select"):
        fired = trace.fired()
        assert [r["key"] for r in fired] == ["map.pick_min_priority"]
        args = action["args"]
        idx = next(i for i, c in enumerate(row["state"]["choices"])
                   if c["col"] == args["col"] and c["row"] == args["row"])
        assert trace.score_of("node", idx) == min(trace.ok_scores("node"))
        _sums_match(trace)
        n += 1
    assert n > 0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py::test_map_evidence_matches_choice -q`
Expected: FAIL

- [ ] **Step 3: 改 `HpAwareMapStrategy.choose`**

签名改为 `def choose(self, state: dict, choices: list[dict], trace=None) -> dict:`。循环体里每一处 `p += X` / `p -= X` 后面紧跟一行把分项记下来；最后记录选择。完整替换 `scored = []` 到方法末尾：

```python
        scored = []
        for i, c in enumerate(choices):
            base = self.PRIORITY.get(c.get("type", "Unknown"), 4)
            p = base
            comps = [("map.base_priority", base, c.get("type"))]
            # Shop value scales up in Act 2 — lower gold threshold
            act2_shop_threshold = 75 if in_act2 else self.SHOP_GOLD_THRESHOLD
            if c.get("type") == "Shop" and gold < act2_shop_threshold:
                p += 5
                comps.append(("map.shop_low_gold", 5, None))
            if c.get("type") == "Elite":
                # Floor-based early avoidance (inherited logic)
                if isinstance(floor, int) and floor < self.ELITE_AVOID_FLOOR:
                    p += 10
                    comps.append(("map.elite_early_floor", 10, None))
                # HP-based avoidance — takes priority over floor preference
                if hp_ratio < self.HP_DANGER:
                    p += 15
                    comps.append(("map.elite_hp_danger", 15, None))
                elif hp_ratio < self.HP_LOW:
                    p += 7
                    comps.append(("map.elite_hp_low", 7, None))
                elif in_act2:
                    # Act 2 elites: only seek them at 90%+ HP; otherwise avoid
                    if hp_ratio >= 0.90:
                        p -= 1.0  # healthy enough to risk Act 2 elite for relic
                        comps.append(("map.elite_act2_healthy", -1.0, None))
                    else:
                        p += 5  # Act 2 elites are significantly harder
                        comps.append(("map.elite_act2", 5, None))
                elif hp_ratio >= self.HP_STRONG and isinstance(floor, int) and floor > self.ELITE_PREFER_FLOOR and floor <= 12:
                    p -= 1.9  # (keep the original comment block here)
                    comps.append(("map.elite_strong_midgame", -1.9, None))
                elif isinstance(floor, int) and floor >= 13:
                    p += 3  # (keep the original comment block here)
                    comps.append(("map.elite_pre_boss", 3, None))
                if isinstance(floor, int) and floor >= self.ACT2_BOSS_ZONE:
                    p += 4  # pre-Act-2-boss: strongly avoid elites regardless of HP
                    comps.append(("map.elite_act2_boss_zone", 4, None))
            if trace is not None:
                trace.option(i, kind="node", score=p, components=comps,
                             detail={"col": c.get("col"), "row": c.get("row"),
                                     "type": c.get("type")})
            scored.append((p, i, c))
        scored.sort()
        best = scored[0][2]
        if trace is not None:
            trace.rule("map.pick_min_priority", "fired", best_index=scored[0][1],
                       hp_ratio=hp_ratio, gold=gold, floor=floor, in_act2=in_act2)
        return {"cmd": "action", "action": "select_map_node",
                "args": {"col": best["col"], "row": best["row"]}}
```

"keep the original comment block here" 指的是原来 `p -= 1.9` 和 `p += 3` 后面那几行注释，原样保留，不要删。`p` 从 `base` 开始逐项相加，`sum(components)` 从 0 开始按同样顺序相加，结果完全相同。

- [ ] **Step 4: greedy 里传 trace**

```python
    if decision == "map_select":
        choices = state.get("choices", [])
        if choices:
            if trace is not None:
                return _map_strategy.choose(state, choices, trace=trace)
            return _map_strategy.choose(state, choices)
```

（`set_map_strategy` 可以换成不接受 `trace` 的策略；不带 trace 的调用方式保持原样，所以不受影响。）

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_trace.py tests/agent/test_greedy_golden.py tests/agent/test_combat_env.py tests/test_route_policy.py -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add agent/strategy.py agent/combat_env.py tests/agent/test_greedy_trace.py
git commit -m "feat(advisor): map heuristic records each node's priority terms

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A9: 地图启发式修楼层语义（有意改变行为）

`context.floor` 是幕内楼层，`HpAwareMapStrategy` 却按全局楼层写：`floor >= 16` 当成第 2 幕，`floor >= 19` 当成第 2 幕 Boss 前。修复后用 `act` 判断幕数。

**Files:**
- Modify: `agent/strategy.py`（`HpAwareMapStrategy`）
- Modify: `tests/fixtures/advisor/ooc_corpus.jsonl.gz`（重新生成）
- Test: `tests/agent/test_map_strategy_floors.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/agent/test_map_strategy_floors.py
"""HpAwareMapStrategy reads the act from `act`, not from the act-local floor.
context.floor / state.floor are act-local (RunState.ActFloor, 1-based per act)."""
from agent.greedy_trace import GreedyTrace
from agent.strategy import HpAwareMapStrategy


def _state(act, floor, hp=60, max_hp=80, gold=0):
    return {"decision": "map_select", "act": act, "floor": floor,
            "context": {"act": act, "floor": floor},
            "player": {"hp": hp, "max_hp": max_hp, "gold": gold}}


def _components(state, choices):
    trace = GreedyTrace()
    HpAwareMapStrategy().choose(state, choices, trace=trace)
    return {o["index"]: [c["key"] for c in o["components"]]
            for o in trace.to_dict()["options"]}


ELITE_MONSTER = [{"col": 0, "row": 1, "type": "Elite"}, {"col": 1, "row": 1, "type": "Monster"}]


def test_act1_late_floors_use_act1_rules():
    comps = _components(_state(act=1, floor=16, hp=76), ELITE_MONSTER)[0]
    assert "map.elite_act2_healthy" not in comps and "map.elite_act2" not in comps
    assert "map.elite_pre_boss" in comps
    assert "map.elite_act2_boss_zone" not in comps


def test_act2_early_floor_uses_act2_rules():
    comps = _components(_state(act=2, floor=5, hp=56), ELITE_MONSTER)[0]
    assert "map.elite_act2" in comps


def test_act2_and_act3_boss_zone_from_floor_13():
    assert "map.elite_act2_boss_zone" in _components(_state(act=2, floor=13), ELITE_MONSTER)[0]
    assert "map.elite_act2_boss_zone" in _components(_state(act=3, floor=14), ELITE_MONSTER)[0]
    assert "map.elite_act2_boss_zone" not in _components(_state(act=2, floor=12), ELITE_MONSTER)[0]


def test_act1_floor_13_has_no_act2_boss_zone():
    assert "map.elite_act2_boss_zone" not in _components(_state(act=1, floor=13), ELITE_MONSTER)[0]


def test_shop_gold_threshold_follows_act():
    shop = [{"col": 0, "row": 1, "type": "Shop"}, {"col": 1, "row": 1, "type": "Monster"}]
    assert "map.shop_low_gold" not in _components(_state(act=3, floor=4, gold=80), shop)[0]
    assert "map.shop_low_gold" in _components(_state(act=1, floor=4, gold=80), shop)[0]


def test_missing_act_falls_back_to_act1_rules():
    state = _state(act=1, floor=5, hp=56)
    del state["act"]
    del state["context"]["act"]
    assert "map.elite_act2" not in _components(state, ELITE_MONSTER)[0]
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_map_strategy_floors.py -q`
Expected: 至少 `test_act1_late_floors_use_act1_rules`、`test_act2_early_floor_uses_act2_rules`、`test_act2_and_act3_boss_zone_from_floor_13`、`test_shop_gold_threshold_follows_act` 失败

- [ ] **Step 3: 修 `HpAwareMapStrategy`**

类常量：删掉 `ACT2_START = 16` 这一行，`ACT2_BOSS_ZONE` 改为

```python
    ACT2_BOSS_ZONE = 13      # act-local floor from which acts 2+ save HP for the boss
```

类 docstring 里的 `Act 2 (floor 16+) is handled separately` 改为 `Acts 2+ (from state["act"]; floors are act-local) are handled separately`。

`choose` 里 `in_act2 = isinstance(floor, int) and floor >= self.ACT2_START` 改为

```python
        act = state.get("act") or (state.get("context") or {}).get("act")
        in_act2 = isinstance(act, int) and not isinstance(act, bool) and act >= 2
```

循环里的 `if isinstance(floor, int) and floor >= self.ACT2_BOSS_ZONE:` 改为

```python
                if in_act2 and isinstance(floor, int) and floor >= self.ACT2_BOSS_ZONE:
```

再确认没有别处用到被删的常量：`grep -rn "ACT2_START\|ACT2_BOSS_ZONE" agent python tests tools`，只应剩 `agent/strategy.py` 里这两处。

- [ ] **Step 4: 运行新测试**

Run: `.venv/bin/python -m pytest tests/agent/test_map_strategy_floors.py -q`
Expected: `6 passed`

- [ ] **Step 5: 重新生成语料，并核对变化只出现在该变的地方**

```bash
cp tests/fixtures/advisor/ooc_corpus.jsonl.gz /tmp/sts2-cli/ooc_corpus_before_map_fix.jsonl.gz
.venv/bin/python tools/build_advisor_corpus.py --batch logs/batches/p3b-route-ab-20261012-160225
.venv/bin/python - <<'EOF'
import gzip, json
old = [json.loads(l) for l in gzip.open("/tmp/sts2-cli/ooc_corpus_before_map_fix.jsonl.gz", "rt")]
new = [json.loads(l) for l in gzip.open("tests/fixtures/advisor/ooc_corpus.jsonl.gz", "rt")]
assert [(r["game"], r["seq"]) for r in old] == [(r["game"], r["seq"]) for r in new]
changed = [(o, n) for o, n in zip(old, new) if o["expected"] != n["expected"]]
bad = [n for o, n in changed
       if n["state"]["decision"] != "map_select"
       or not ((n["state"].get("act") or 1) >= 2 or (n["state"].get("floor") or 0) >= 16)]
print(f"{len(changed)} expected actions changed; {len(bad)} outside act>=2 / floor>=16")
assert not bad, bad[:3]
EOF
```

Expected：打印 `N expected actions changed; 0 outside act>=2 / floor>=16`（N 可以是 0 或更多，都要记进提交信息）。

- [ ] **Step 6: 运行全部 advisor 测试**

Run: `.venv/bin/python -m pytest tests/agent/test_greedy_golden.py tests/agent/test_greedy_trace.py tests/agent/test_map_strategy_floors.py tests/agent/test_combat_env.py tests/test_route_policy.py -q`
Expected: 全部通过

- [ ] **Step 7: 提交**

```bash
git add agent/strategy.py tests/agent/test_map_strategy_floors.py tests/fixtures/advisor/ooc_corpus.jsonl.gz
git commit -m "fix(map): HpAwareMapStrategy reads the act from state.act

context.floor is act-local, so 'floor >= 16' fired the act-2 rules in the
last floors of every act and ACT2_BOSS_ZONE=19 never fired. Acts 2+ now
come from state.act; the boss zone is act >= 2 and floor >= 13 (the act-1
pre-boss threshold). Only CombatEnv / eval_rl / coordinator route maps
through this; play_full_run's regression and A/B batches use the random
route, so no measured result changes. Corpus golden regenerated: <N>
map_select actions changed, all at act >= 2 or floor >= 16.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

（把 `<N>` 换成 Step 5 打印的数字。）

### Task A10: 全语料可选检查（保留的 batch 日志全量）

固定语料只有十几局。这里再加一个**可选**测试：设了 `STS2_FULL_CORPUS=1` 时，遍历 `logs/batches/*/games/` 下所有日志，检查带 trace 和不带 trace 的选择完全一致、依据和选择一致。默认跳过，不拖慢日常测试。

**Files:**
- Test: `tests/agent/test_greedy_trace_full_corpus.py`

- [ ] **Step 1: 写测试**

```python
# tests/agent/test_greedy_trace_full_corpus.py
"""Opt-in (STS2_FULL_CORPUS=1): every kept batch game, traced vs untraced.
~25k states; minutes, not seconds."""
import glob
import os

import pytest

from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, GreedyTrace, reset_greedy_context

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GAMES = sorted(glob.glob(os.path.join(ROOT, "logs", "batches", "*", "games", "*", "*.jsonl")))

pytestmark = pytest.mark.skipif(
    os.environ.get("STS2_FULL_CORPUS") != "1" or not GAMES,
    reason="set STS2_FULL_CORPUS=1 (needs logs/batches/*/games)")


@pytest.fixture(autouse=True)
def _default_greedy_env(monkeypatch):
    for key in GREEDY_BEHAVIOR_ENV:
        monkeypatch.delenv(key, raising=False)


def test_trace_never_changes_a_choice_on_any_kept_game():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from build_advisor_corpus import game_states
    mismatches, n = [], 0
    for path in GAMES:
        states = list(game_states(path))
        reset_greedy_context()
        plain = [greedy_action(s) for s in states]
        reset_greedy_context()
        for s, want in zip(states, plain):
            trace = GreedyTrace()
            got = greedy_action(s, trace=trace)
            n += 1
            if got != want or len(trace.fired()) != 1:
                mismatches.append((path, s["decision"], want, got, trace.rules[-2:]))
    assert n > 0
    assert not mismatches, mismatches[:5]
```

- [ ] **Step 2: 跑一次全量**

Run: `STS2_FULL_CORPUS=1 scripts/run_caffeinated.sh .venv/bin/python -m pytest tests/agent/test_greedy_trace_full_corpus.py -q`
Expected: `1 passed`（耗时几分钟）。如果失败，把前 5 条 mismatch 贴进报告，回到对应的 Task 修。

再确认默认是跳过的：`.venv/bin/python -m pytest tests/agent/test_greedy_trace_full_corpus.py -q` → `1 skipped`。

- [ ] **Step 3: 提交**

```bash
git add tests/agent/test_greedy_trace_full_corpus.py
git commit -m "test(advisor): opt-in full-batch check that tracing never changes a pick

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task A11: `agent/advisor_server.py`

**Files:**
- Create: `agent/advisor_server.py`
- Test: `tests/agent/test_advisor_server.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/agent/test_advisor_server.py
"""agent/advisor_server.py: the in-game mod's local advice service."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from agent import advisor_server
from agent.combat_env import greedy_action
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, reset_greedy_context
from tests.agent.advisor_corpus import corpus_games


@pytest.fixture(autouse=True)
def _default_greedy_env(monkeypatch):
    for key in GREEDY_BEHAVIOR_ENV:
        monkeypatch.delenv(key, raising=False)


def test_advise_action_is_greedy_action_on_the_whole_corpus():
    for _game, rows in corpus_games():
        reset_greedy_context()
        for row in rows:
            reply = advisor_server.advise(row["state"], greedy_action, [])
            assert reply["action"] == row["expected"], (row["game"], row["seq"])
            assert reply["policy"] == "greedy"
            assert reply["unverified"] == (row["state"]["decision"] == "map_select")
            assert len([r for r in reply["rules"] if r["outcome"] == "fired"]) == 1


def test_advise_refuses_decisions_it_does_not_cover():
    reply = advisor_server.advise({"decision": "combat_play"}, greedy_action, [])
    assert reply["action"] is None
    assert reply["warnings"] == ["not_advised: combat_play"]


def test_advise_reports_greedy_failures_without_a_fallback():
    def boom(state, trace=None):
        raise ZeroDivisionError("x")
    reply = advisor_server.advise({"decision": "rest_site"}, boom, ["STS2_RANDOMIZE=1"])
    assert reply["action"] is None and reply["options"] == [] and reply["rules"] == []
    assert reply["warnings"] == ["STS2_RANDOMIZE=1", "greedy_raised: ZeroDivisionError"]
    reply = advisor_server.advise({"decision": "rest_site"}, lambda s, trace=None: None, [])
    assert reply["action"] is None and reply["warnings"] == ["greedy_none"]


def test_knob_warnings_lists_set_behaviour_env():
    env = {"STS2_MC_ROLLOUT": "1", "STS2_CARD_QUALITY_GATE": " ", "OTHER": "1"}
    assert advisor_server.knob_warnings(env) == ["STS2_MC_ROLLOUT=1"]


def test_http_round_trip():
    server = advisor_server.make_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/health", timeout=10) as r:
            health = json.load(r)
        assert health["ok"] is True and health["policy"] == "greedy"
        rest = next(row["state"] for _g, rows in corpus_games() for row in rows
                    if row["state"]["decision"] == "rest_site")
        req = urllib.request.Request(base + "/advise", data=json.dumps(rest).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            reply = json.load(r)
        assert reply["action"] == greedy_action(rest)
        bad = urllib.request.Request(base + "/advise", data=b"{not json")
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad, timeout=10)
        assert err.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


def test_server_binds_loopback_only():
    server = advisor_server.make_server(0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/agent/test_advisor_server.py -q`
Expected: FAIL，`ImportError: cannot import name 'advisor_server'`

- [ ] **Step 3: 实现**

```python
# agent/advisor_server.py
"""Local advice service for the in-game Sts2Advisor mod. Binds 127.0.0.1 only.

  POST /advise  body: one decision JSON (the headless engine's shape)
                reply: {"action", "options", "rules", "warnings", "unverified", "policy"}
  GET  /health  reply: {"ok", "policy", "commit", "warnings"}

`action` is exactly what play_full_run's greedy policy plays (same import path,
same defaults); `options` / `rules` are the GreedyTrace of that same call.
Requests are served one at a time: greedy has hidden per-process state
(see agent.greedy_trace.reset_greedy_context), which, as in a play_full_run
lane, carries from one decision to the next.

    .venv/bin/python agent/advisor_server.py --port 8767
"""
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import play_full_run  # noqa: E402
from agent.greedy_trace import GREEDY_BEHAVIOR_ENV, GreedyTrace  # noqa: E402

DEFAULT_PORT = 8767
ADVISED = frozenset(play_full_run.OOC_GREEDY_DECISIONS | {"map_select"})


def knob_warnings(env) -> list:
    """`KEY=value` for every greedy behaviour env var that is set (non-blank)."""
    return [f"{k}={env[k]}" for k in GREEDY_BEHAVIOR_ENV if (env.get(k) or "").strip()]


def advise(state: dict, greedy, warnings: list) -> dict:
    decision = state.get("decision")
    reply = {"action": None, "options": [], "rules": [], "warnings": list(warnings),
             "unverified": decision == "map_select", "policy": "greedy"}
    if decision not in ADVISED:
        reply["warnings"].append(f"not_advised: {decision}")
        return reply
    trace = GreedyTrace()
    try:
        action = greedy(state, trace=trace)
    except Exception as exc:  # a trace bug lands here too: no advice beats wrong reasons
        reply["warnings"].append(f"greedy_raised: {type(exc).__name__}")
        return reply
    if action is None:
        reply["warnings"].append("greedy_none")
        return reply
    reply["action"] = action
    reply.update(trace.to_dict())
    return reply


def _json_default(obj):
    if hasattr(obj, "item"):  # numpy scalars
        return obj.item()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


class _Handler(BaseHTTPRequestHandler):
    server_version = "Sts2Advisor/1"

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True, "policy": "greedy", "commit": self.server.commit,
                         "warnings": self.server.warnings})

    def do_POST(self):
        if self.path != "/advise":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            state = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if not isinstance(state, dict):
            return self._send(400, {"error": "body must be a decision object"})
        self._send(200, advise(state, self.server.greedy, self.server.warnings))

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, default=_json_default, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def make_server(port: int, greedy=None, env=None) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.greedy = greedy or play_full_run._load_greedy_action()
    server.warnings = knob_warnings(os.environ if env is None else env)
    server.commit = _git_commit()
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sts2Advisor local advice service")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    server = make_server(args.port)
    for w in server.warnings:
        print(f"WARNING: greedy behaviour env set, advice differs from the measured policy: {w}",
              file=sys.stderr)
    print(f"Sts2Advisor advice service on http://127.0.0.1:{server.server_address[1]} "
          f"(commit {server.commit})", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/agent/test_advisor_server.py -q`
Expected: `6 passed`

- [ ] **Step 5: 手动冒烟**

```bash
.venv/bin/python agent/advisor_server.py --port 8767 &
sleep 5; curl -s http://127.0.0.1:8767/health; echo
kill %1
```

Expected：`{"ok": true, "policy": "greedy", "commit": "...", "warnings": []}`

- [ ] **Step 6: 提交**

```bash
git add agent/advisor_server.py tests/agent/test_advisor_server.py
git commit -m "feat(advisor): local advice service for the in-game mod

POST /advise returns greedy_action's own action plus the GreedyTrace of
that same call; no naive fallback (a failure is 'no advice'). Loopback
only, one request at a time.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```


---

# Part B — 共享序列化（C# headless）

**目标**：把"游戏模型 → 决策 JSON"的纯函数从 `RunSimulator.cs` 抽进 `src/Sts2Shared/`，让 headless 和 mod 编译同一份代码。headless 的输出必须**逐字节不变**。

**金标做法**：用改动前编译的引擎重放 5 局真实对局的动作日志，存下每一条回复；每次改动后用新引擎重放同样的日志，逐行比较。`plan_combat_turn` 不重放：它只搜索、不改变游戏状态，而且搜索结果本身不确定。

**Part B 文件结构**
- Create `tools/replay_states.py`：重放和比较工具。Test: `tests/test_replay_states.py`。
- Create `src/Sts2Shared/LocLookup.cs`：从 `RunSimulator.cs` 原样搬出（命名空间改为 `Sts2Shared`），另加一个接收根目录的构造函数。
- Create `src/Sts2Shared/DecisionSerializer.cs`：`internal static class DecisionSerializer`，全部方法见 Task B4、B5。
- Modify `src/Sts2Headless/Sts2Headless.csproj`：链接 `..\Sts2Shared\*.cs`。
- Modify `src/Sts2Headless/RunSimulator.cs`：删除被搬走的代码，改为调用 `DecisionSerializer`；控制流（`ForceToMap`、待决的 selector 和 bundle、`_syncCtx.Pump()`、`Log(...)`）全部留在原处。

**规则**（B3–B5 每个任务都适用）
1. **代码原样搬运。** 不合并几处相似的卡牌字典，不调整键的顺序。键顺序就是 JSON 字节的一部分，金标会抓出来。
2. 搬进 `DecisionSerializer` 的方法体只做以下替换：
   - `_loc` → `loc`；
   - `_runState` / `_runState!` → `runState`（方法参数，非空）；
   - `GetUpgradedInfo(X)` → `UpgradedInfo(X, loc)`；
   - `PlayerSummary(X)` → `PlayerSummary(X, loc)`；
   - `RunContext()` → `RunContext(runState, loc)`。
3. `src/Sts2Shared/` 里**不能出现任何 `Godot` 命名空间的引用**。mod 和 headless 用的 GodotSharp 不同；只依赖 `sts2.dll` 的模型类型，两边都能编译。
4. 每个任务结束都要：`~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj` 0 个错误，并且 `replay_states.py --compare` 输出 `identical`。

### Task B1: 重放和比较工具

**Files:**
- Create: `tools/replay_states.py`
- Test: `tests/test_replay_states.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_replay_states.py
"""tools/replay_states.py: replay a game log's commands, compare reply dumps."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import replay_states

ECHO_ENGINE = r'''
import json, sys
print("warming up")
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "quit":
        break
    print(json.dumps({"type": "decision", "echo": cmd}), flush=True)
'''


def _log(tmp_path, commands):
    path = tmp_path / "game.jsonl"
    rows = [{"type": "run_meta", "run_id": "x"}]
    for i, cmd in enumerate(commands):
        rows.append({"step": i, "type": "action", "data": cmd})
        rows.append({"step": i, "type": "state", "data": {"type": "decision"}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_recorded_commands_skip_plans_and_quit(tmp_path):
    log = _log(tmp_path, [
        {"cmd": "start_run", "character": "Ironclad", "seed": "s"},
        {"cmd": "action", "action": "plan_combat_turn"},
        {"cmd": "action", "action": "end_turn"},
        {"cmd": "quit"},
    ])
    assert list(replay_states.recorded_commands(str(log))) == [
        {"cmd": "start_run", "character": "Ironclad", "seed": "s"},
        {"cmd": "action", "action": "end_turn"},
    ]


def test_replay_writes_one_reply_per_command(tmp_path):
    engine = tmp_path / "engine.py"
    engine.write_text(ECHO_ENGINE)
    log = _log(tmp_path, [{"cmd": "start_run"}, {"cmd": "action", "action": "plan_combat_turn"},
                          {"cmd": "action", "action": "end_turn"}])
    out = tmp_path / "out.jsonl"
    n = replay_states.replay(str(log), str(out), argv=[sys.executable, str(engine)])
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert n == 2
    assert [l["reply"]["echo"] for l in lines] == [{"cmd": "start_run"},
                                                    {"cmd": "action", "action": "end_turn"}]


def test_compare_reports_first_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "g1.jsonl").write_text('{"n": 0}\n{"n": 1}\n')
    (b / "g1.jsonl").write_text('{"n": 0}\n{"n": 2}\n')
    (a / "g2.jsonl").write_text('{"n": 0}\n')
    (b / "g2.jsonl").write_text('{"n": 0}\n')
    (a / "g3.jsonl").write_text('{"n": 0}\n')
    assert replay_states.compare(str(a), str(b)) == [
        "g1.jsonl: first difference at reply 1",
        "g3.jsonl: only in one directory",
    ]
    assert replay_states.compare(str(a), str(a)) == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_replay_states.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'replay_states'`

- [ ] **Step 3: 实现**

```python
# tools/replay_states.py
"""Replay recorded game logs through the headless engine; save every reply.

The golden for refactors that must not change engine output (Sts2Advisor Part B):
replay the same logs with the old and the new build, then --compare the two
output directories. plan_combat_turn is skipped -- it only searches (never
changes the game) and its plans are not deterministic.

    .venv/bin/python tools/replay_states.py LOG.jsonl... --out DIR
    .venv/bin/python tools/replay_states.py --compare DIR_A DIR_B
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from engine_process import EngineHang, EngineProcess  # noqa: E402

REPLY_TIMEOUT_S = 120


def recorded_commands(path: str):
    """Commands the original run sent, in order, minus plan_combat_turn and quit."""
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            cmd = row.get("data")
            if row.get("type") != "action" or not isinstance(cmd, dict):
                continue
            if cmd.get("action") == "plan_combat_turn" or cmd.get("cmd") == "quit":
                continue
            yield cmd


def replay(path: str, out_path: str, argv=None) -> int:
    """Send every recorded command; write {"n", "cmd", "reply"} lines. Returns replies written."""
    if argv is None:
        import play_full_run  # sets the STS2_GAME_DIR default, like every harness run
        argv = play_full_run.engine_argv()
    engine = EngineProcess(argv, stderr=subprocess.DEVNULL)
    n = 0
    try:
        ready = engine.read_json(REPLY_TIMEOUT_S)
        if ready.get("type") != "ready":
            raise RuntimeError(f"unexpected first reply: {ready}")
        with open(out_path, "w") as out:
            for cmd in recorded_commands(path):
                engine.write(cmd)
                try:
                    reply = engine.read_json(REPLY_TIMEOUT_S)
                except EngineHang:
                    out.write(json.dumps({"n": n, "cmd": cmd, "hang": True}) + "\n")
                    break
                out.write(json.dumps({"n": n, "cmd": cmd, "reply": reply},
                                     ensure_ascii=False) + "\n")
                n += 1
    finally:
        engine.close()
    return n


def compare(dir_a: str, dir_b: str) -> list:
    """Differences between two replay directories; [] means identical."""
    diffs = []
    names = sorted(set(os.listdir(dir_a)) | set(os.listdir(dir_b)))
    for name in names:
        pa, pb = os.path.join(dir_a, name), os.path.join(dir_b, name)
        if not (os.path.exists(pa) and os.path.exists(pb)):
            diffs.append(f"{name}: only in one directory")
            continue
        la = open(pa).read().splitlines()
        lb = open(pb).read().splitlines()
        for i, (x, y) in enumerate(zip(la, lb)):
            if x != y:
                diffs.append(f"{name}: first difference at reply {i}")
                break
        else:
            if len(la) != len(lb):
                diffs.append(f"{name}: {len(la)} vs {len(lb)} replies")
    return diffs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--compare", nargs=2, metavar=("DIR_A", "DIR_B"))
    args = ap.parse_args(argv)
    if args.compare:
        diffs = compare(*args.compare)
        print("\n".join(diffs) if diffs else "identical")
        return 1 if diffs else 0
    if not args.logs or not args.out:
        ap.error("give LOG files and --out, or --compare DIR_A DIR_B")
    os.makedirs(args.out, exist_ok=True)
    for log in args.logs:
        out = os.path.join(args.out, os.path.basename(log))
        n = replay(log, out)
        print(f"{os.path.basename(log)}: {n} replies", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行测试**

Run: `.venv/bin/python -m pytest tests/test_replay_states.py -q`
Expected: `3 passed`

- [ ] **Step 5: 提交**

```bash
git add tools/replay_states.py tests/test_replay_states.py
git commit -m "feat(tools): replay_states -- replay game logs, compare engine replies

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task B2: 录基线，并确认重放本身是确定的

**Files:** 无代码改动；输出写在 `~/.sts2-train/advisor-golden/`。

- [ ] **Step 1: 用当前代码编译引擎**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -3`
Expected: `0 Error(s)`

- [ ] **Step 2: 选 5 局（每个角色一局）并重放两遍**

```bash
B=logs/batches/p3b-route-ab-20261012-160225/games
G=~/.sts2-train/advisor-golden
mkdir -p $G
LOGS="$B/Ironclad__random/Ironclad_greedy_a1_p3b_1.jsonl $B/Silent__random/Silent_greedy_a1_p3b_1.jsonl $B/Defect__random/Defect_greedy_a1_p3b_1.jsonl $B/Regent__random/Regent_greedy_a1_p3b_1.jsonl $B/Necrobinder__random/Necrobinder_greedy_a1_p3b_1.jsonl"
echo "$LOGS" > $G/logs.txt
scripts/run_caffeinated.sh .venv/bin/python tools/replay_states.py $LOGS --out $G/base1
scripts/run_caffeinated.sh .venv/bin/python tools/replay_states.py $LOGS --out $G/base2
.venv/bin/python tools/replay_states.py --compare $G/base1 $G/base2
```

Expected：每局打印几百条回复，最后一行 `identical`。

如果**不** identical：先用 `diff <(sed -n '<N+1>p' $G/base1/X.jsonl) <(sed -n '<N+1>p' $G/base2/X.jsonl)` 看是哪个字段在变。如果是时间戳之类和游戏状态无关的字段，就在 `compare` 里对该字段做归一化（加测试），再重跑这一步；如果是游戏状态本身在变，停下来报告，Part B 不能用这个金标。

再检查每局的最后一条回复：`tail -1 $G/base1/*.jsonl | cut -c1-200`。不应该是 `"hang": true`，也不应该从开局起就一直是 `"type": "error"`。录制日志之后引擎如果改过，重放可能在中途走岔，这不影响新旧对比；但如果全是错误回复，这份金标就什么也测不到，这时停下来报告。

- [ ] **Step 3: 记录**

不需要提交。把 `identical` 的结果和每局回复数写进给协调者的报告。

### Task B3: 把 `LocLookup` 搬到 `src/Sts2Shared/`

**Files:**
- Create: `src/Sts2Shared/LocLookup.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs`（删掉 `LocLookup` 类和它上面的 doc 注释，约 125 行；文件头加 `using Sts2Shared;`）
- Modify: `src/Sts2Headless/Sts2Headless.csproj`

- [ ] **Step 1: 建新文件**

`src/Sts2Shared/LocLookup.cs` 的内容是 `RunSimulator.cs` 里从 `/// <summary>` + `/// Bilingual localization lookup` 开始、到 `public bool IsLoaded => _eng.Count > 0;` 后面那个 `}` 为止的整段，原样搬过来。只改三处：

1. 文件头：

```csharp
// Shared by Sts2Headless and the Sts2Advisor mod (linked into both projects).
// No Godot references allowed here: the two projects bind different GodotSharp builds.
using System.Text.Json;

namespace Sts2Shared;
```

2. 原来的无参构造函数改成两个：

```csharp
    /// <summary>Headless default: localization_* dirs at the repo root, five levels above bin/.</summary>
    public LocLookup()
        : this(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."))
    {
    }

    /// <summary>Load localization_eng/ and localization_zhs/ under <paramref name="rootDir"/>.</summary>
    public LocLookup(string rootDir)
    {
        Load(Path.Combine(rootDir, "localization_eng"), _eng);
        Load(Path.Combine(rootDir, "localization_zhs"), _zhs);
    }
```

3. 类声明保持 `internal class LocLookup`。

- [ ] **Step 2: 从 `RunSimulator.cs` 删掉旧类，并加 using**

删掉 `RunSimulator.cs` 里整段 `LocLookup`（包括它上面的三行 doc 注释）。在 `using CombatSolver;` 后面加一行 `using Sts2Shared;`。

- [ ] **Step 3: 在 csproj 里链接共享目录**

在 `src/Sts2Headless/Sts2Headless.csproj` 的第一个 `<ItemGroup>` 前插入：

```xml
  <ItemGroup>
    <!-- Shared with the Sts2Advisor mod; see src/Sts2Shared/. -->
    <Compile Include="..\Sts2Shared\**\*.cs" Link="Sts2Shared\%(RecursiveDir)%(Filename)%(Extension)" />
  </ItemGroup>
```

- [ ] **Step 4: 编译并对比金标**

```bash
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -3
G=~/.sts2-train/advisor-golden
scripts/run_caffeinated.sh .venv/bin/python tools/replay_states.py $(cat $G/logs.txt) --out $G/after-b3
.venv/bin/python tools/replay_states.py --compare $G/base1 $G/after-b3
```

Expected：`0 Error(s)`，然后 `identical`

- [ ] **Step 5: 提交**

```bash
git add src/Sts2Shared/LocLookup.cs src/Sts2Headless/RunSimulator.cs src/Sts2Headless/Sts2Headless.csproj
git commit -m "refactor(engine): LocLookup moves to src/Sts2Shared for the advisor mod

Byte-identical replies on 5 replayed games (tools/replay_states.py).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task B4: `DecisionSerializer` 第一批：`RunContext`、`PlayerSummary`、`UpgradedInfo`

**Files:**
- Create: `src/Sts2Shared/DecisionSerializer.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs`（`GetUpgradedInfo`、`PlayerSummary`、`RunContext` 三个方法）

- [ ] **Step 1: 建文件，搬三个方法**

```csharp
// src/Sts2Shared/DecisionSerializer.cs
// Game model -> decision JSON (Dictionary<string, object?>), shared by Sts2Headless and the
// Sts2Advisor mod (linked into both). Pure: reads models, never advances the game.
// Moved verbatim from RunSimulator.cs -- key order is part of the byte-identical replay golden
// (tools/replay_states.py), so never reorder or merge the per-site card dicts.
// No Godot references allowed here.
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Entities.RestSite;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Shared;

internal static class DecisionSerializer
{
    // <- RunSimulator.RunContext()
    public static Dictionary<string, object?> RunContext(RunState runState, LocLookup loc)
    {
        // body: RunSimulator.RunContext's body from `var ctx = new Dictionary...` to `return ctx;`,
        // with the substitutions in "Part B 规则 2". Drop its first line
        // (`if (_runState == null) return new();`) -- that null check stays in RunSimulator.
    }

    // <- RunSimulator.PlayerSummary(Player)
    public static Dictionary<string, object?> PlayerSummary(Player player, LocLookup loc)
    {
        // body: RunSimulator.PlayerSummary's whole body, with the substitutions in 规则 2.
    }

    // <- RunSimulator.GetUpgradedInfo(CardModel)
    public static Dictionary<string, object?>? UpgradedInfo(CardModel card, LocLookup loc)
    {
        // body: RunSimulator.GetUpgradedInfo's whole body, with the substitutions in 规则 2.
    }
}
```

上面三处 `// body:` 注释都要换成真正搬过来的代码，**提交的文件里不能留这些注释**。

缺 `using` 时编译器会报具体是哪个类型，从 `RunSimulator.cs` 文件头抄对应的那一行过来（比如 `CardKeyword` 在 `MegaCrit.Sts2.Core.Entities.Cards`）。

- [ ] **Step 2: `RunSimulator` 改成调用共享代码**

三个方法原地保留为一行转发，这样 `RunSimulator` 里几十处调用点一个都不用改：

```csharp
    private Dictionary<string, object?>? GetUpgradedInfo(CardModel card)
        => DecisionSerializer.UpgradedInfo(card, _loc);

    private Dictionary<string, object?> PlayerSummary(Player player)
        => DecisionSerializer.PlayerSummary(player, _loc);

    /// <summary>Common context added to every decision point.</summary>
    private Dictionary<string, object?> RunContext()
        => _runState == null ? new() : DecisionSerializer.RunContext(_runState, _loc);
```

- [ ] **Step 3: 编译并对比金标**

```bash
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -3
G=~/.sts2-train/advisor-golden
scripts/run_caffeinated.sh .venv/bin/python tools/replay_states.py $(cat $G/logs.txt) --out $G/after-b4
.venv/bin/python tools/replay_states.py --compare $G/base1 $G/after-b4
```

Expected：`0 Error(s)`，然后 `identical`

- [ ] **Step 4: 提交**

```bash
git add src/Sts2Shared/DecisionSerializer.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor(engine): shared DecisionSerializer -- run context, player, upgrade info

Byte-identical replies on 5 replayed games.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task B5: `DecisionSerializer` 第二批：各决策的 JSON

**Files:**
- Modify: `src/Sts2Shared/DecisionSerializer.cs`
- Modify: `src/Sts2Headless/RunSimulator.cs`（`MapSelectState`、`EventChoiceState`、`RestSiteState`、`ShopState`、`CardRewardState`，以及 `DetectDecisionPoint` 里 bundle / 事件卡牌奖励 / card_select 三个分支）

每一项的做法都是：把下表"搬走的代码"原样移进 `DecisionSerializer` 的新方法（按规则 2 替换），在原处换成"原处改成"那一行。

| 新方法（加进 `DecisionSerializer`） | 搬走的代码 | 原处改成 |
|---|---|---|
| `public static Dictionary<string, object?> MapChoice(MapPoint point)` 返回 `new() { ["col"] = (int)point.coord.col, ["row"] = (int)point.coord.row, ["type"] = point.PointType.ToString() }` | `MapSelectState` 里四处一模一样的 `{col,row,type}` 字典 | `DecisionSerializer.MapChoice(child)`（`startPoint` 那一处传 `startPoint`） |
| `public static Dictionary<string, object?> MapSelect(RunState runState, List<Dictionary<string, object?>> choices, LocLookup loc)` | `MapSelectState` 最后的 `return new Dictionary<...> { ["type"] = "decision", ["decision"] = "map_select", ... ["floor"] = _runState.ActFloor, };` | `return DecisionSerializer.MapSelect(_runState!, choices, _loc);` |
| `public static Dictionary<string, object?> EventChoice(EventModel localEvent, RunState runState, LocLookup loc)` | `EventChoiceState` 里从 `var options = currentOptions` 到方法末尾的 `return`，并在方法开头加一行 `var currentOptions = localEvent.CurrentOptions;` | `return DecisionSerializer.EventChoice(localEvent, _runState!, _loc);`（它前面那段"事件已结束 / 没有选项"的处理留在原处，`var currentOptions = ...` 那行也留着，空选项检查要用） |
| `public static Dictionary<string, object?> RestSite(IReadOnlyList<RestSiteOption> options, RunState runState, LocLookup loc)` | `RestSiteState` 里从 `var optionList = options.Select(...)` 到方法末尾 | `return DecisionSerializer.RestSite(options, _runState!, _loc);`（原方法开头的 `var player = _runState!.Players[0];` 不再被用到，删掉；空选项时 `ForceToMap` 的分支留在原处）。新方法体第一行写 `var player = runState.Players[0];`，因为搬过去的 `return` 用到了 `player` |
| `public static Dictionary<string, object?> Shop(MerchantInventory inv, Player player, RunState runState, LocLookup loc)` | `ShopState` 里从 `var cards = inv.CharacterCardEntries...` 到方法末尾；其中 `var removal = merchantRoom.GetLocalInventory().CardRemovalEntry;` 改成 `var removal = inv.CardRemovalEntry;`（同一个对象） | `return DecisionSerializer.Shop(inv, player, _runState!, _loc);`（`inv == null` 的分支留在原处） |
| `public static Dictionary<string, object?> CardReward(IEnumerable<CardModel> cards, bool canSkip, int? goldEarned, RunState runState, LocLookup loc)` | `CardRewardState` 里从 `var cards = _pendingCardReward.Cards.Select(...)` 到方法末尾；字典里 `["can_skip"] = canSkip`、`["gold_earned"] = goldEarned` | `return DecisionSerializer.CardReward(_pendingCardReward.Cards, _pendingCardReward.CanSkip, _runState!.Players[0].Gold - _goldBeforeCombat, _runState!, _loc);` |
| `public static Dictionary<string, object?> EventCardReward(IReadOnlyList<CardCreationResult> rewardCards, RunState runState, LocLookup loc)` | `DetectDecisionPoint` 里 `if (_cardSelector.HasPendingReward)` 块中，从 `var cards = rewardCards.Select(...)` 到该块的 `return` | `return DecisionSerializer.EventCardReward(_cardSelector.PendingRewardCards!, _runState!, _loc);` |
| `public static Dictionary<string, object?> CardSelect(IReadOnlyList<CardModel> options, int minSelect, int maxSelect, Player player, RunState runState, LocLookup loc)` | `DetectDecisionPoint` 里 `if (_cardSelector.HasPending && _cardSelector.PendingOptions != null)` 块中，从 `var opts = _cardSelector.PendingOptions.Select(...)` 到该块的 `return`；`.PendingOptions` → `options`，`_cardSelector.PendingMinSelect` → `minSelect`，`_cardSelector.PendingMaxSelect` → `maxSelect` | `return DecisionSerializer.CardSelect(_cardSelector.PendingOptions, _cardSelector.PendingMinSelect, _cardSelector.PendingMaxSelect, player, _runState!, _loc);` |
| `public static Dictionary<string, object?> BundleSelect(IReadOnlyList<IReadOnlyList<CardModel>> bundles, Player player, RunState runState, LocLookup loc)` | `DetectDecisionPoint` 里 bundle 块中，从 `var bundles = _pendingBundles.Select(...)` 到该块的 `return`；`_pendingBundles.Select` → `bundles.Select`，并把局部变量改名为 `bundleDicts`，避免和参数重名 | `return DecisionSerializer.BundleSelect(_pendingBundles, player, _runState!, _loc);` |

注意几处原样保留的细节：
- 事件卡牌奖励和 card_reward 的 `player` 原来写的是 `PlayerSummary(_runState!.Players[0])`，搬过去后是 `PlayerSummary(runState.Players[0], loc)`。card_select 和 bundle 原来用的是局部变量 `player`，所以它们的方法多一个 `Player player` 参数。照原样，不要统一。
- `EventChoice` 里 `RandomCard` 那段读的是 `_runState?.Players?[0]?.Deck?.Cards`，搬过去后是 `runState.Players?[0]?.Deck?.Cards`。

- [ ] **Step 1: 按上表逐项搬运**

- [ ] **Step 2: 确认共享目录里没有 Godot**

Run: `grep -n "Godot" src/Sts2Shared/*.cs`
Expected：只命中两个文件头注释里的 "No Godot references allowed here"，没有 `using Godot` 或 `Godot.` 前缀。

- [ ] **Step 3: 编译并对比金标**

```bash
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -3
G=~/.sts2-train/advisor-golden
scripts/run_caffeinated.sh .venv/bin/python tools/replay_states.py $(cat $G/logs.txt) --out $G/after-b5
.venv/bin/python tools/replay_states.py --compare $G/base1 $G/after-b5
```

Expected：`0 Error(s)`，然后 `identical`

- [ ] **Step 4: 提交**

```bash
git add src/Sts2Shared/DecisionSerializer.cs src/Sts2Headless/RunSimulator.cs
git commit -m "refactor(engine): every out-of-combat decision JSON comes from DecisionSerializer

Map choices, event, rest site, shop, card reward (combat and event),
card select and bundle builders move verbatim; RunSimulator keeps its
control flow. Byte-identical replies on 5 replayed games.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task B6: 审计、JIT 探针、引擎测试、完整回归

**Files:** 无代码改动（除非检查失败）。

- [ ] **Step 1: Godot 桩审计和 JIT 探针**

```bash
.venv/bin/python tools/audit_godot_stub_refs.py 2>&1 | tail -3
.venv/bin/python tools/jit_probe.py 2>&1 | tail -3
```

Expected：审计输出 `TOTAL missing: 0`；JIT 探针没有新增失败。

- [ ] **Step 2: 需要游戏 DLL 的引擎测试**

Run: `scripts/run_caffeinated.sh .venv/bin/python -m pytest tests/test_card_reward.py tests/test_event.py tests/test_rest_site.py tests/test_shop.py tests/test_map.py tests/test_lang.py -q`
Expected：全部通过。`test_lang.py` 覆盖 `LocLookup.Lang = "zh"`，确认搬家没有影响中文输出。

- [ ] **Step 3: 完整回归（CLAUDE.md 要求）**

先确认没有别的 batch 在跑：`pgrep -f play_full_run`，应当没有输出。然后在后台运行：

```bash
scripts/run_caffeinated.sh .venv/bin/python python/run_batch.py --name regression --kind regression --games 5 --chars all
```

Expected：结尾汇总里每条 lane 都是 `Completed: 5/5`，没有 `INCOMPLETE`，也没有 `SOLVER NEVER ENGAGED`。把汇总原样贴进报告。

- [ ] **Step 4: 记录**

如果 Step 1–3 全部通过，不需要提交。有失败就回到 B3–B5 中对应的任务修，修完重跑本任务。

---

# Part C — 游戏内 mod（C#）

依赖 Part A（`advisor_server.py`、trace key 表）和 Part B（`src/Sts2Shared/`）。

## Part C 背景（已核实的游戏事实，v0.111.0 反编译）

- **加载方式**：游戏的 `ModManager` 递归扫描 `<可执行文件目录>/mods/` 下的 `*.json`。macOS 上是 `SlayTheSpire2.app/Contents/MacOS/mods/`（这个目录现在还不存在）。它加载 `<mod 目录>/<manifest.id>.dll`，放进 `sts2.dll` 所在的同一个 AssemblyLoadContext。所以 `id`、dll 文件名、程序集名必须都是 `Sts2Advisor`。
- **入口**：`[ModInitializer("Initialize")]` 标在类上，对应方法必须是 `static`、无参数，返回值会被忽略。用了这个属性，游戏就**不会**自动执行 `Harmony.PatchAll`；本 mod 也不需要任何 Harmony 补丁。
- **初始化时机很早**：在 `LocManager`、`ModelDb` 初始化之前，所以 `LocManager.Instance` 此时还是 null；`NGame.Instance` 已经存在。初始化函数里只做三件事：读配置、建日志、挂帧回调。其余所有和游戏有关的操作都放到第一帧之后。
- **工程形态**照抄已上架 mod 的做法（Torch1230/CombatSolver 的引用方式 + Gennadiyev/STS2MCP 的 plain SDK）：
  - 普通 `Microsoft.NET.Sdk`，`net9.0`，Krafs.Publicizer 2.3.0；
  - 引用游戏目录里**真实的** `sts2.dll`、`GodotSharp.dll`，`Private=false`；
  - **不继承任何 Godot 节点类**（继承的话需要 Godot 源码生成器）。只用内置节点，加上 `SceneTree.ProcessFrame` 信号。
- **识别当前界面**：用游戏自己的 `ActiveScreenContext.Instance.GetCurrentScreen()`（`MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext`），按返回节点的**类型**判断。不要用节点 `Name`：`NChooseARelicSelection` 的 Name 写错了。
  - 战斗中用 `CombatManager.Instance.IsInProgress` 判断，这时 mod 不出声。
  - `RunState` 从 `RunManager.Instance.DebugOnlyGetState()` 取，AutoSlay 也是这样做的。
- **界面背后的数据在私有字段里**，有 Publicizer 就能直接读：
  - `NCardRewardSelectionScreen._options`（`IReadOnlyList<CardCreationResult>`）、`._extraOptions`（`OptionId == "Skip"` 表示可以跳过）；
  - `NCardGridSelectionScreen._cards`（protected）；各个具体网格界面的 `_prefs`（`CardSelectorPrefs`，有 `MinSelect`/`MaxSelect`）；
  - `NChooseACardSelectionScreen._cards`、`._canSkip`；`NChooseABundleSelectionScreen._bundles`；
  - 事件：`EventRoom.LocalMutableEvent`；休息处：`RestSiteRoom.Options`，以及 `RunManager.Instance.RestSiteSynchronizer.GetChosenOptionIndex(player.NetId)`；商店：`MerchantRoom.GetLocalInventory()`；
  - 地图下一步可去的点：还没走过任何格子时是 `Map.StartingMapPoint`，否则是 `MapTravel.GetTravelablePointsFrom(rs, rs.CurrentMapPoint)`。
- **语言和字体**：
  - 游戏语言是 `LocManager.Instance.Language`，`"zhs"`/`"zht"` 表示中文，`"eng"` 表示英文。
  - 中文需要游戏自己的 CJK 替换字体：`control.ApplyLocaleFontSubstitution(FontType.Regular, "normal_font")`（`MegaCrit.Sts2.Core.Localization.Fonts`）。
- **显示名用游戏当前语言的官方译名**：`CardModel.Title`（string，已经带 `+`）、`RelicModel.Title` / `PotionModel.Title` / `EventOption.Title` / `RestSiteOption.Title`（都是 `LocString`，调 `.GetFormattedText()`）。地图节点用 `new LocString("map", "LEGEND_ELITE.title")` 这一组 key。
- **日志和配置放用户数据目录**：`OS.GetUserDataDir()`，macOS 上是 `~/Library/Application Support/SlayTheSpire2/`，放在其下的 `Sts2Advisor/`。不放在 dll 旁边，因为创意工坊下载的那份是只读的。spec 里"放在 dll 旁边"的写法在 Task D1 修正。
- 游戏 `data_sts2_macos_arm64/` 里已经带了 `System.Net.Http`、`System.Diagnostics.Process`、`System.Text.Json`。

## Part C 文件结构

- `src/Sts2Advisor.Core/`（`net9.0` 类库，**不引用游戏和 Godot**，可以单测）：
  - `AdvisorConfig.cs`：配置；
  - `Backoff.cs`：重连节奏；
  - `Fingerprint.cs`：决策指纹；
  - `AdviceReply.cs`：解析 `/advise` 的回复；
  - `IAdvisor.cs`、`SidecarAdvisor.cs`：建议来源接口和 HTTP 实现；
  - `TraceText.cs`：trace key → 中/英文案；
  - `PanelText.cs`：一次回复 → 面板上的若干行；
  - `AdvisorTraceLog.cs`：`advisor_trace.jsonl`。
- `src/Sts2Advisor.Core.Tests/`：xunit。
- `src/Sts2Advisor/`（mod 本体）：
  - `Sts2Advisor.csproj`：用 `<Compile Link>` 把 `../Sts2Shared/*.cs` 和 `../Sts2Advisor.Core/*.cs` 编进同一个 dll。mod 加载器只加载一个 dll，同目录里的其他依赖 dll 不保证能被找到。
  - `Sts2Advisor.json`（清单）、`ModEntry.cs`、`ModLog.cs`、`GameText.cs`、`DecisionCapture.cs`、`AdvisorPanel.cs`、`SidecarProcess.cs`、`AdvisorRuntime.cs`。
- `tools/install_advisor_mod.sh`：编译并安装到本机 Steam 版游戏。
- `tests/test_advisor_trace_text_keys.py`：保证 `TraceText.cs` 和 `TRACE_KEYS` 一一对应。
- `tests/test_advisor_trace_contract.py`（Task C8）：实机采集的 JSON 和 headless 字段一致。
- **不改** `src/sts2-cli.slnx`：mod 需要本机装着游戏才能编译，不应该影响现有的整解决方案构建。

### Task C1: Core 工程骨架；`AdvisorConfig`、`Backoff`、`Fingerprint`

**Files:**
- Create: `src/Sts2Advisor.Core/Sts2Advisor.Core.csproj`、`src/Sts2Advisor.Core/AdvisorConfig.cs`、`src/Sts2Advisor.Core/Backoff.cs`、`src/Sts2Advisor.Core/Fingerprint.cs`
- Create: `src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj`、`src/Sts2Advisor.Core.Tests/AdvisorConfigTests.cs`、`src/Sts2Advisor.Core.Tests/BackoffTests.cs`、`src/Sts2Advisor.Core.Tests/FingerprintTests.cs`

- [ ] **Step 1: 两个工程文件**

```xml
<!-- src/Sts2Advisor.Core/Sts2Advisor.Core.csproj -->
<!-- Game-free logic of the Sts2Advisor mod. The mod compiles these sources in by <Compile Link>;
     this project exists so they can be unit tested without the game. -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <RootNamespace>Sts2Advisor.Core</RootNamespace>
  </PropertyGroup>
</Project>
```

```xml
<!-- src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <IsPackable>false</IsPackable>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" />
    <PackageReference Include="xunit" Version="2.9.2" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.8.2" />
  </ItemGroup>
  <ItemGroup>
    <Using Include="Xunit" />
    <ProjectReference Include="..\Sts2Advisor.Core\Sts2Advisor.Core.csproj" />
  </ItemGroup>
</Project>
```

- [ ] **Step 2: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AdvisorConfigTests.cs
using System.Text.Json;
using Sts2Advisor.Core;

public class AdvisorConfigTests
{
    private static string Write(string json)
    {
        var path = Path.Combine(Path.GetTempPath(), $"sts2advisor-{Guid.NewGuid()}.json");
        File.WriteAllText(path, json);
        return path;
    }

    [Fact]
    public void MissingFileGivesDefaults()
    {
        var c = AdvisorConfig.Load(Path.Combine(Path.GetTempPath(), $"absent-{Guid.NewGuid()}.json"));
        Assert.Equal(AdvisorConfig.Defaults, c);
        Assert.Equal(8767, c.Port);
        Assert.True(c.Autostart);
        Assert.Equal("F8", c.Hotkey);
        Assert.Equal("", c.ResolvedPython);
    }

    [Fact]
    public void ReadsEveryKey()
    {
        var c = AdvisorConfig.Load(Write(
            """{"repo_path": "/r", "python": "/p/python", "port": 9000, "autostart": false, "hotkey": "F9"}"""));
        Assert.Equal(new AdvisorConfig("/r", "/p/python", 9000, false, "F9"), c);
        Assert.Equal("/p/python", c.ResolvedPython);
    }

    [Fact]
    public void PythonDefaultsToTheRepoVenv()
    {
        var c = AdvisorConfig.Load(Write("""{"repo_path": "/r"}"""));
        Assert.Equal(Path.Combine("/r", ".venv", "bin", "python"), c.ResolvedPython);
        Assert.Equal(8767, c.Port);
    }

    [Fact]
    public void MalformedJsonThrows()
    {
        Assert.ThrowsAny<JsonException>(() => AdvisorConfig.Load(Write("{not json")));
    }
}
```

```csharp
// src/Sts2Advisor.Core.Tests/BackoffTests.cs
using Sts2Advisor.Core;

public class BackoffTests
{
    [Fact]
    public void DoublesUpToTheCapThenResets()
    {
        var b = new Backoff(1, 30);
        Assert.True(b.Ready(0));
        var delays = new List<double>();
        for (int i = 0; i < 7; i++)
        {
            b.Failed(0);
            delays.Add(b.CurrentDelay);
        }
        Assert.Equal(new[] { 1.0, 2, 4, 8, 16, 30, 30 }, delays);
        b.Failed(100);
        Assert.False(b.Ready(129.9));
        Assert.True(b.Ready(130));
        b.Succeeded();
        Assert.True(b.Ready(0));
        b.Failed(0);
        Assert.Equal(1.0, b.CurrentDelay);
    }
}
```

```csharp
// src/Sts2Advisor.Core.Tests/FingerprintTests.cs
using Sts2Advisor.Core;

public class FingerprintTests
{
    [Fact]
    public void JoinsPartsAndMarksNulls()
    {
        Assert.Equal("shop|a|-|3", Fingerprint.Of("shop", new object?[] { "a", null, 3 }));
        Assert.NotEqual(Fingerprint.Of("shop", new object?[] { 1 }), Fingerprint.Of("shop", new object?[] { 2 }));
    }
}
```

- [ ] **Step 3: 运行，确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -5`
Expected：编译失败，`The type or namespace name 'AdvisorConfig' could not be found`（第一次运行会先从 nuget.org 还原 xunit，需要联网）

- [ ] **Step 4: 实现**

```csharp
// src/Sts2Advisor.Core/AdvisorConfig.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>User settings, read from &lt;game user dir&gt;/Sts2Advisor/config.json
/// (written by tools/install_advisor_mod.sh). Missing keys take the defaults.</summary>
public sealed record AdvisorConfig(string RepoPath, string Python, int Port, bool Autostart, string Hotkey)
{
    public const int DefaultPort = 8767;
    public const string DefaultHotkey = "F8";

    public static AdvisorConfig Defaults { get; } = new("", "", DefaultPort, true, DefaultHotkey);

    /// <summary>"python" if set, else the repo's .venv/bin/python, else "".</summary>
    public string ResolvedPython =>
        !string.IsNullOrWhiteSpace(Python) ? Python
        : string.IsNullOrWhiteSpace(RepoPath) ? ""
        : Path.Combine(RepoPath, ".venv", "bin", "python");

    /// <summary>Defaults when the file is absent; throws JsonException when it is not JSON.</summary>
    public static AdvisorConfig Load(string path)
    {
        if (!File.Exists(path))
            return Defaults;
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        var root = doc.RootElement;
        return new AdvisorConfig(
            Str(root, "repo_path") ?? Defaults.RepoPath,
            Str(root, "python") ?? Defaults.Python,
            root.TryGetProperty("port", out var p) && p.TryGetInt32(out var port) ? port : DefaultPort,
            root.TryGetProperty("autostart", out var a) && a.ValueKind is JsonValueKind.True or JsonValueKind.False
                ? a.GetBoolean() : Defaults.Autostart,
            Str(root, "hotkey") ?? DefaultHotkey);
    }

    private static string? Str(JsonElement root, string name) =>
        root.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
}
```

```csharp
// src/Sts2Advisor.Core/Backoff.cs
namespace Sts2Advisor.Core;

/// <summary>Reconnect schedule for the advice service: 1 s, 2 s, 4 s ... capped at 30 s.</summary>
public sealed class Backoff
{
    private readonly double _first;
    private readonly double _max;
    private double _readyAt;

    public Backoff(double firstSeconds = 1, double maxSeconds = 30)
    {
        _first = firstSeconds;
        _max = maxSeconds;
    }

    public double CurrentDelay { get; private set; }

    public bool Ready(double nowSeconds) => nowSeconds >= _readyAt;

    public void Failed(double nowSeconds)
    {
        CurrentDelay = CurrentDelay == 0 ? _first : Math.Min(CurrentDelay * 2, _max);
        _readyAt = nowSeconds + CurrentDelay;
    }

    public void Succeeded()
    {
        CurrentDelay = 0;
        _readyAt = 0;
    }
}
```

```csharp
// src/Sts2Advisor.Core/Fingerprint.cs
namespace Sts2Advisor.Core;

/// <summary>Cheap identity of a decision screen. The mod asks the advice service again
/// only when this changes (a purchase, a new event page, HP or gold moving).</summary>
public static class Fingerprint
{
    public static string Of(string decision, IEnumerable<object?> parts) =>
        decision + "|" + string.Join("|", parts.Select(p => p?.ToString() ?? "-"));
}
```

- [ ] **Step 5: 运行，确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3`
Expected: `Passed!  - Failed: 0, Passed: 6`

- [ ] **Step 6: 提交**

```bash
git add src/Sts2Advisor.Core src/Sts2Advisor.Core.Tests
git commit -m "feat(advisor-mod): Core library -- config, reconnect backoff, fingerprint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

（`git add` 目录前先用 `git status --short src/Sts2Advisor.Core src/Sts2Advisor.Core.Tests` 确认没有 `bin/`、`obj/` 混进去。仓库的 `.gitignore` 若没覆盖它们，在这一步给 `.gitignore` 加上 `src/Sts2Advisor*/bin/` 和 `src/Sts2Advisor*/obj/`，并一起提交。）

### Task C2: `AdviceReply`、`IAdvisor`、`SidecarAdvisor`

**Files:**
- Create: `src/Sts2Advisor.Core/AdviceReply.cs`、`src/Sts2Advisor.Core/IAdvisor.cs`、`src/Sts2Advisor.Core/SidecarAdvisor.cs`
- Test: `src/Sts2Advisor.Core.Tests/AdviceReplyTests.cs`、`src/Sts2Advisor.Core.Tests/SidecarAdvisorTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AdviceReplyTests.cs
using Sts2Advisor.Core;

public class AdviceReplyTests
{
    public const string CardRewardJson = """
    {"action": {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}},
     "options": [
       {"kind": "card", "index": 1, "score": 7.4, "status": "ok",
        "components": [{"key": "card.base", "value": 6.8, "detail": "A"},
                       {"key": "card.boss_defense_gap", "value": 0.6},
                       {"key": "card.archetype", "value": 0.0}]},
       {"kind": "card", "index": 0, "score": null, "status": "filtered", "components": []}],
     "rules": [{"key": "card_reward.pick_best", "outcome": "fired",
                "values": {"threshold": 5.5, "act": 1, "deck_size": 14, "best_score": 7.4}}],
     "warnings": ["STS2_MC_ROLLOUT=1"], "unverified": false, "policy": "greedy"}
    """;

    [Fact]
    public void ParsesEveryPart()
    {
        var r = AdviceReply.Parse(CardRewardJson);
        Assert.Equal("select_card_reward", r.ActionName);
        Assert.Equal(1, r.Arg("card_index")!.Value.GetInt32());
        Assert.Equal(2, r.Options.Count);
        Assert.Equal(7.4, r.Options[0].Score);
        Assert.Equal("A", r.Options[0].Components[0].Detail!.Value.GetString());
        Assert.Null(r.Options[1].Score);
        Assert.Equal("filtered", r.Options[1].Status);
        Assert.Equal(5.5, r.Rules[0].Values["threshold"].GetDouble());
        Assert.Equal(new[] { "STS2_MC_ROLLOUT=1" }, r.Warnings);
        Assert.False(r.Unverified);
        Assert.Equal("greedy", r.Policy);
        Assert.Equal(CardRewardJson, r.Raw);
    }

    [Fact]
    public void NullActionMeansNoAdvice()
    {
        var r = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_none"], "unverified": false, "policy": "greedy"}""");
        Assert.Null(r.Action);
        Assert.Null(r.ActionName);
        Assert.Null(r.Arg("card_index"));
    }
}
```

```csharp
// src/Sts2Advisor.Core.Tests/SidecarAdvisorTests.cs
using System.Net;
using System.Text;
using Sts2Advisor.Core;

public class SidecarAdvisorTests
{
    private sealed class StubHandler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, HttpResponseMessage> Respond = _ => new HttpResponseMessage(HttpStatusCode.OK);
        public readonly List<(string Method, string Path, string? Body)> Seen = new();

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Seen.Add((request.Method.Method, request.RequestUri!.AbsolutePath,
                      request.Content == null ? null : await request.Content.ReadAsStringAsync(ct)));
            return Respond(request);
        }
    }

    private static HttpResponseMessage Json(string body, HttpStatusCode code = HttpStatusCode.OK) =>
        new(code) { Content = new StringContent(body, Encoding.UTF8, "application/json") };

    [Fact]
    public async Task PostsTheDecisionAndParsesTheReply()
    {
        var stub = new StubHandler { Respond = _ => Json(AdviceReplyTests.CardRewardJson) };
        using var advisor = new SidecarAdvisor(8767, stub);
        var reply = await advisor.AdviseAsync("""{"decision": "card_reward"}""", CancellationToken.None);
        Assert.Equal("select_card_reward", reply.ActionName);
        Assert.Equal(("POST", "/advise", """{"decision": "card_reward"}"""), stub.Seen[0]);
    }

    [Fact]
    public async Task ServerErrorThrows()
    {
        var stub = new StubHandler { Respond = _ => Json("{}", HttpStatusCode.InternalServerError) };
        using var advisor = new SidecarAdvisor(8767, stub);
        await Assert.ThrowsAsync<HttpRequestException>(() => advisor.AdviseAsync("{}", CancellationToken.None));
    }

    [Fact]
    public async Task HealthyOnlyWhenTheServiceSaysOk()
    {
        var stub = new StubHandler { Respond = _ => Json("""{"ok": true, "policy": "greedy"}""") };
        using var advisor = new SidecarAdvisor(8767, stub);
        Assert.True(await advisor.IsHealthyAsync(CancellationToken.None));
        Assert.Equal(("GET", "/health", null), stub.Seen[0]);

        stub.Respond = _ => Json("""{"ok": false}""");
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
        stub.Respond = _ => Json("{}", HttpStatusCode.ServiceUnavailable);
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
        stub.Respond = _ => throw new HttpRequestException("refused");
        Assert.False(await advisor.IsHealthyAsync(CancellationToken.None));
    }
}
```

- [ ] **Step 2: 运行，确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -5`
Expected：编译失败，找不到 `AdviceReply` / `SidecarAdvisor`

- [ ] **Step 3: 实现**

```csharp
// src/Sts2Advisor.Core/AdviceReply.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

public sealed record TraceComponent(string Key, double Value, JsonElement? Detail);

public sealed record TraceOption(string Kind, int Index, double? Score, string Status,
                                 IReadOnlyList<TraceComponent> Components, JsonElement? Detail);

public sealed record TraceRule(string Key, string Outcome, IReadOnlyDictionary<string, JsonElement> Values);

/// <summary>agent/advisor_server.py's POST /advise reply: greedy's action plus the
/// GreedyTrace of the same call (options with summed components, ordered rules).</summary>
public sealed record AdviceReply(JsonElement? Action, IReadOnlyList<TraceOption> Options,
                                 IReadOnlyList<TraceRule> Rules, IReadOnlyList<string> Warnings,
                                 bool Unverified, string Policy, string Raw)
{
    public static AdviceReply Parse(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        JsonElement? action = root.TryGetProperty("action", out var a) && a.ValueKind == JsonValueKind.Object
            ? a.Clone() : null;

        var options = new List<TraceOption>();
        if (root.TryGetProperty("options", out var opts) && opts.ValueKind == JsonValueKind.Array)
        {
            foreach (var o in opts.EnumerateArray())
            {
                var comps = new List<TraceComponent>();
                if (o.TryGetProperty("components", out var cs) && cs.ValueKind == JsonValueKind.Array)
                    foreach (var c in cs.EnumerateArray())
                        comps.Add(new TraceComponent(
                            c.GetProperty("key").GetString()!, c.GetProperty("value").GetDouble(),
                            c.TryGetProperty("detail", out var cd) ? (JsonElement?)cd.Clone() : null));
                options.Add(new TraceOption(
                    o.GetProperty("kind").GetString()!, o.GetProperty("index").GetInt32(),
                    o.TryGetProperty("score", out var s) && s.ValueKind == JsonValueKind.Number ? s.GetDouble() : null,
                    o.GetProperty("status").GetString()!, comps,
                    o.TryGetProperty("detail", out var d) ? (JsonElement?)d.Clone() : null));
            }
        }

        var rules = new List<TraceRule>();
        if (root.TryGetProperty("rules", out var rs) && rs.ValueKind == JsonValueKind.Array)
        {
            foreach (var rule in rs.EnumerateArray())
            {
                var values = new Dictionary<string, JsonElement>();
                if (rule.TryGetProperty("values", out var vs) && vs.ValueKind == JsonValueKind.Object)
                    foreach (var p in vs.EnumerateObject())
                        values[p.Name] = p.Value.Clone();
                rules.Add(new TraceRule(rule.GetProperty("key").GetString()!,
                                        rule.GetProperty("outcome").GetString()!, values));
            }
        }

        var warnings = new List<string>();
        if (root.TryGetProperty("warnings", out var ws) && ws.ValueKind == JsonValueKind.Array)
            foreach (var w in ws.EnumerateArray())
                if (w.GetString() is { } text)
                    warnings.Add(text);

        return new AdviceReply(action, options, rules, warnings,
            root.TryGetProperty("unverified", out var u) && u.ValueKind == JsonValueKind.True,
            root.TryGetProperty("policy", out var pol) && pol.ValueKind == JsonValueKind.String ? pol.GetString()! : "",
            json);
    }

    /// <summary>The engine action name, e.g. "select_card_reward"; null when there is no advice.</summary>
    public string? ActionName =>
        Action is { } a && a.TryGetProperty("action", out var n) ? n.GetString() : null;

    public JsonElement? Arg(string name) =>
        Action is { } a && a.TryGetProperty("args", out var args) && args.ValueKind == JsonValueKind.Object
        && args.TryGetProperty(name, out var v) ? v : null;
}
```

```csharp
// src/Sts2Advisor.Core/IAdvisor.cs
namespace Sts2Advisor.Core;

/// <summary>Where advice comes from. SidecarAdvisor (local Python service) now; a C# port of
/// greedy_action later (spec "不做"), checked against the same corpus.</summary>
public interface IAdvisor
{
    /// <summary>Advice for one decision JSON. Throws when the source is unreachable or answers badly.</summary>
    Task<AdviceReply> AdviseAsync(string decisionJson, CancellationToken ct);

    Task<bool> IsHealthyAsync(CancellationToken ct);
}
```

```csharp
// src/Sts2Advisor.Core/SidecarAdvisor.cs
using System.Text;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>agent/advisor_server.py on 127.0.0.1. 2 s timeout per request.</summary>
public sealed class SidecarAdvisor : IAdvisor, IDisposable
{
    private readonly HttpClient _http;

    public SidecarAdvisor(int port, HttpMessageHandler? handler = null, TimeSpan? timeout = null)
    {
        _http = handler == null ? new HttpClient() : new HttpClient(handler);
        _http.BaseAddress = new Uri($"http://127.0.0.1:{port}/");
        _http.Timeout = timeout ?? TimeSpan.FromSeconds(2);
    }

    public async Task<AdviceReply> AdviseAsync(string decisionJson, CancellationToken ct)
    {
        using var content = new StringContent(decisionJson, Encoding.UTF8, "application/json");
        using var response = await _http.PostAsync("advise", content, ct).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        return AdviceReply.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false));
    }

    public async Task<bool> IsHealthyAsync(CancellationToken ct)
    {
        try
        {
            using var response = await _http.GetAsync("health", ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                return false;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false));
            return doc.RootElement.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException)
        {
            return false;
        }
    }

    public void Dispose() => _http.Dispose();
}
```

- [ ] **Step 4: 运行，确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3`
Expected: `Passed!  - Failed: 0, Passed: 11`

- [ ] **Step 5: 提交**

```bash
git add src/Sts2Advisor.Core/AdviceReply.cs src/Sts2Advisor.Core/IAdvisor.cs src/Sts2Advisor.Core/SidecarAdvisor.cs src/Sts2Advisor.Core.Tests/AdviceReplyTests.cs src/Sts2Advisor.Core.Tests/SidecarAdvisorTests.cs
git commit -m "feat(advisor-mod): IAdvisor and the HTTP sidecar client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C3: `TraceText`、`PanelText`，以及 key 对齐测试

**Files:**
- Create: `src/Sts2Advisor.Core/TraceText.cs`、`src/Sts2Advisor.Core/PanelText.cs`
- Test: `src/Sts2Advisor.Core.Tests/PanelTextTests.cs`、`tests/test_advisor_trace_text_keys.py`

中文文案里的游戏术语都用 `localization_zhs/` 里的官方译名：稀有（`gameplay_ui:CARD_RARITY.RARE`）、罕见、诅咒、力量（`powers:STRENGTH_POWER.title`）、敏捷、变化（`static_hover_tips:TRANSFORM.title`）、金币、能量、跳过（`card_reward_ui:OPTION_SKIP.name`）、离开、休息、锻造（`rest_site_ui:OPTION_SMITH.name`）、休息处（`static_hover_tips:ROOM_REST.title`）、商店（`ROOM_MERCHANT.title`）、事件、卡牌奖励（`static_hover_tips:CARD_REWARD.title`）、打击、防御。其余是 mod 自己的界面用语。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_advisor_trace_text_keys.py
"""Every greedy trace key has panel text in the mod, and the mod has no text for
keys greedy no longer emits (agent/greedy_trace.py TRACE_KEYS <-> TraceText.cs)."""
import os
import re

from agent.greedy_trace import TRACE_KEYS

SRC = os.path.join(os.path.dirname(__file__), "..", "src", "Sts2Advisor.Core", "TraceText.cs")


def test_trace_text_covers_exactly_the_trace_keys():
    with open(SRC, encoding="utf-8") as f:
        keys = set(re.findall(r'\["([a-z0-9_]+\.[a-z0-9_.]+)"\]\s*=\s*\(', f.read()))
    assert sorted(TRACE_KEYS - keys) == []
    assert sorted(keys - TRACE_KEYS) == []
```

```csharp
// src/Sts2Advisor.Core.Tests/PanelTextTests.cs
using System.Text.Json;
using Sts2Advisor.Core;

public class PanelTextTests
{
    private static TraceRule Rule(string key, string outcome, string valuesJson)
    {
        using var doc = JsonDocument.Parse(valuesJson);
        return new TraceRule(key, outcome,
            doc.RootElement.EnumerateObject().ToDictionary(p => p.Name, p => p.Value.Clone()));
    }

    private static readonly DecisionHeader CardHeader = new("card_reward", 1, 13, 1, 52, 80, 99);

    [Fact]
    public void RuleTemplatesFormatPercentagesAndNumbers()
    {
        var r = Rule("rest.heal_threshold", "fired", """{"hp_ratio": 0.5125, "threshold": 0.85, "floor": 11}""");
        Assert.Equal("HP 51%，本层休息阈值 85%", TraceText.Rule(r, zh: true));
        Assert.Equal("HP 51%, rest threshold here 85%", TraceText.Rule(r, zh: false));
    }

    [Fact]
    public void MissingValuesRenderAsDash()
    {
        var r = Rule("shop.relic", "skipped", """{"threshold": 4.0, "gold_reserve": 25, "n_affordable": 0}""");
        Assert.Equal("最佳遗物 —，阈值 4，至少留 25 金币", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void WordValuesAreTranslated()
    {
        var r = Rule("shop.remove_basic", "skipped", """{"n_basic": 4, "basic_scope": "ironclad_only"}""");
        Assert.Contains("只统计 Ironclad 的打击和防御（实测配置）", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void UnknownKeysShowTheKeyAndValues()
    {
        var r = Rule("x.brand_new", "fired", """{"a": 1}""");
        Assert.Equal("x.brand_new a=1", TraceText.Rule(r, zh: true));
    }

    [Fact]
    public void CardRewardPanel()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        var labels = new Dictionary<string, string> { ["card:0"] = "打击", ["card:1"] = "燃烧" };
        var lines = PanelText.Render(CardHeader, reply, labels, zh: true);
        Assert.Equal(new PanelLine("卡牌奖励 · A1F13a1 · HP 52/80 · 金币 99", LineStyle.Title), lines[0]);
        Assert.Equal(new PanelLine("建议：燃烧", LineStyle.Pick), lines[1]);
        Assert.Contains(new PanelLine("燃烧  7.4", LineStyle.Fired), lines);
        Assert.Contains(new PanelLine("  基础评级 A 6.8 · 格挡缺口 +0.6", LineStyle.Muted), lines);
        Assert.Contains(new PanelLine("打击  已过滤（第 1 幕后期质量门）", LineStyle.Muted), lines);
        Assert.Contains(new PanelLine("最高分 7.4 ≥ 跳过阈值 5.5（第 1 幕，牌组 14 张）", LineStyle.Fired), lines);
        Assert.Equal(new PanelLine("STS2_MC_ROLLOUT=1", LineStyle.Warning), lines[^1]);
        Assert.Equal(new PanelLine("greedy 启发式打分，不是概率", LineStyle.Muted), lines[^2]);
    }

    [Fact]
    public void MapPanelSortsAscendingAndSaysUnverified()
    {
        var reply = AdviceReply.Parse("""
        {"action": {"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 4}},
         "options": [
           {"kind": "node", "index": 0, "score": 5, "status": "ok", "detail": {"col": 1, "row": 4, "type": "Monster"},
            "components": [{"key": "map.base_priority", "value": 5, "detail": "Monster"}]},
           {"kind": "node", "index": 1, "score": 4.1, "status": "ok", "detail": {"col": 3, "row": 4, "type": "Elite"},
            "components": [{"key": "map.base_priority", "value": 6, "detail": "Elite"},
                           {"key": "map.elite_strong_midgame", "value": -1.9}]}],
         "rules": [{"key": "map.pick_min_priority", "outcome": "fired", "values": {}}],
         "warnings": [], "unverified": true, "policy": "greedy"}
        """);
        var labels = new Dictionary<string, string> { ["node:0"] = "敌人 (1,4)", ["node:1"] = "精英 (3,4)" };
        var lines = PanelText.Render(new DecisionHeader("map_select", 1, 8, 1, 75, 80, 40), reply, labels, zh: true);
        Assert.Equal("建议：精英 (3,4)", lines[1].Text);
        Assert.Equal(new PanelLine("精英 (3,4)  4.1", LineStyle.Fired), lines[2]);
        Assert.Equal("敌人 (1,4)  5.0", lines.First(l => l.Text.StartsWith("敌人")).Text);
        Assert.Contains(new PanelLine("  房间类型 6.0 · HP ≥ 85% 且第 7–12 层 -1.9", LineStyle.Muted), lines);
        Assert.Equal(new PanelLine("启发式，只看下一步，未经 A/B 验证", LineStyle.Muted), lines[^1]);
    }

    [Fact]
    public void NoActionShowsNoAdviceAndWarnings()
    {
        var reply = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_raised: KeyError"], "unverified": false, "policy": "greedy"}""");
        var lines = PanelText.Render(new DecisionHeader("shop", 2, 4, 1, 60, 80, 182), reply,
                                     new Dictionary<string, string>(), zh: true);
        Assert.Equal(new PanelLine("无建议", LineStyle.Warning), lines[1]);
        Assert.Equal(new PanelLine("greedy_raised: KeyError", LineStyle.Muted), lines[2]);
    }

    [Fact]
    public void StatusLines()
    {
        Assert.Equal("战斗中，交给 CombatSolver",
                     PanelText.Status(CardHeader with { Decision = "combat" }, StatusKind.Combat, null, zh: true)[^1].Text);
        Assert.Equal("Advice service offline, reconnecting",
                     PanelText.Status(null, StatusKind.Offline, null, zh: false)[^1].Text);
        Assert.Equal("读取失败：NEventRoom",
                     PanelText.Status(null, StatusKind.ReadFailed, "NEventRoom", zh: true)[^1].Text);
    }
}
```

- [ ] **Step 2: 运行，确认失败**

```bash
~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3
.venv/bin/python -m pytest tests/test_advisor_trace_text_keys.py -q
```

Expected：C# 编译失败（找不到 `TraceText`/`PanelText`）；Python `FileNotFoundError`

- [ ] **Step 3: 实现 `TraceText`**

```csharp
// src/Sts2Advisor.Core/TraceText.cs
using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Sts2Advisor.Core;

/// <summary>Panel text for greedy trace keys. One entry per agent/greedy_trace.py TRACE_KEYS key
/// (tests/test_advisor_trace_text_keys.py keeps them in step). Rule templates take
/// {name} = a rule value and {name:%} = a ratio shown as a percentage; component
/// entries are just the label.</summary>
public static class TraceText
{
    public static readonly IReadOnlyDictionary<string, (string Zh, string En)> Templates =
        new Dictionary<string, (string Zh, string En)>
    {
        ["greedy.randomized"] = ("随机决策（STS2_RANDOMIZE 已设置）", "Random pick (STS2_RANDOMIZE is set)"),
        ["greedy.decision_advisor"] = ("由 DecisionAdvisor 决定（STS2_DECISION_ADVISOR 已设置）", "Picked by DecisionAdvisor (STS2_DECISION_ADVISOR is set)"),
        ["greedy.unhandled"] = ("greedy 没有处理这个决策", "greedy does not handle this decision"),

        ["card_reward.no_cards"] = ("没有可选的卡", "No cards offered"),
        ["card_reward.all_filtered"] = ("全部被第 1 幕后期质量门过滤 → 跳过", "All filtered by the late Act 1 quality gate -> skip"),
        ["card_reward.all_broken"] = ("全部是不可用的卡 → 跳过", "Every offered card is unusable -> skip"),
        ["card_reward.below_threshold"] = ("最高分 {best_score} < 跳过阈值 {threshold}（第 {act} 幕，牌组 {deck_size} 张）", "Best {best_score} < skip threshold {threshold} (act {act}, deck {deck_size})"),
        ["card_reward.pick_best"] = ("最高分 {best_score} ≥ 跳过阈值 {threshold}（第 {act} 幕，牌组 {deck_size} 张）", "Best {best_score} >= skip threshold {threshold} (act {act}, deck {deck_size})"),

        ["card.base"] = ("基础评级", "Base rating"),
        ["card.set_bonus"] = ("组合预测", "Set predictor"),
        ["card.tag_synergy"] = ("标签协同", "Tag synergy"),
        ["card.combat_type"] = ("战斗类型", "Combat mix"),
        ["card.boss_defense_gap"] = ("格挡缺口", "Block gap"),
        ["card.boss_readiness"] = ("Boss 准备度", "Boss readiness"),
        ["card.quality_lift"] = ("牌组质量", "Deck quality"),
        ["card.archetype"] = ("流派", "Archetype"),
        ["card.q_adjust"] = ("历史表现", "Track record"),
        ["card.score"] = ("卡牌分", "Card score"),
        ["card.in_deck_score"] = ("卡牌分（含牌组协同）", "Card score (with deck synergy)"),

        ["rest.critical_heal"] = ("HP {hp_ratio:%}，必须休息线 {threshold:%}", "HP {hp_ratio:%}, must-rest line {threshold:%}"),
        ["rest.vantom_mid_boss"] = ("Vantom 规则：HP {hp_ratio:%}（STS2_VANTOM_REST_HEAL）", "Vantom rule: HP {hp_ratio:%} (STS2_VANTOM_REST_HEAL)"),
        ["rest.pre_boss_heal"] = ("Boss 前（第 {floor} 层）：HP {hp_ratio:%}，休息阈值 {threshold:%}", "Pre-boss (floor {floor}): HP {hp_ratio:%}, rest threshold {threshold:%}"),
        ["rest.must_upgrade"] = ("有分数 ≥ {threshold} 的未升级卡时锻造", "Smith when an unupgraded card scores >= {threshold}"),
        ["rest.heal_threshold"] = ("HP {hp_ratio:%}，本层休息阈值 {threshold:%}", "HP {hp_ratio:%}, rest threshold here {threshold:%}"),
        ["rest.default_smith"] = ("默认：锻造", "Default: Smith"),
        ["rest.fallback"] = ("没有休息或锻造，选第一个可用项", "No Rest or Smith: first available option"),
        ["rest.no_choice"] = ("没有可选项", "Nothing to choose"),

        ["event.pick_best"] = ("选分数最高的选项", "Highest-scoring option"),
        ["event.no_available"] = ("所有选项都已锁定 → 离开", "Every option is locked -> leave"),
        ["event.kw.lose_max_hp"] = ("失去最大生命值", "Lose Max HP"),
        ["event.kw.curse"] = ("诅咒", "Curse"),
        ["event.kw.lose_all_gold"] = ("失去全部金币", "Lose all gold"),
        ["event.kw.lose_gold"] = ("失去金币", "Lose gold"),
        ["event.kw.torment"] = ("折磨", "Torment"),
        ["event.kw.take_damage"] = ("受到伤害", "Take damage"),
        ["event.kw.lose_hp"] = ("失去生命", "Lose HP"),
        ["event.kw.downgrade"] = ("降级卡牌", "Downgrade a card"),
        ["event.kw.add_basic"] = ("加入打击或防御", "Add a Strike or Defend"),
        ["event.kw.rare_card"] = ("稀有卡", "Rare card"),
        ["event.kw.uncommon_card"] = ("罕见卡", "Uncommon card"),
        ["event.kw.card_reward"] = ("获得卡牌", "Card reward"),
        ["event.kw.remove_card"] = ("删牌", "Remove a card"),
        ["event.kw.remove_card_hp_cost"] = ("删牌要失去生命", "Removal costs HP"),
        ["event.kw.relic"] = ("遗物", "Relic"),
        ["event.kw.relic_with_downside"] = ("有代价的遗物", "Relic with a downside"),
        ["event.kw.upgrade"] = ("升级", "Upgrade"),
        ["event.kw.transform"] = ("变化", "Transform"),
        ["event.kw.gain_gold"] = ("获得金币", "Gain gold"),
        ["event.kw.gain_max_hp"] = ("获得最大生命值", "Gain Max HP"),
        ["event.kw.potion"] = ("药水", "Potion"),
        ["event.kw.heal"] = ("回复生命", "Heal"),
        ["event.kw.colorless_card"] = ("无色卡", "Colorless card"),
        ["event.kw.gain_strength"] = ("获得力量", "Gain Strength"),
        ["event.kw.enemy_strength"] = ("敌人获得力量", "Enemies gain Strength"),
        ["event.kw.gain_dexterity"] = ("获得敏捷", "Gain Dexterity"),
        ["event.kw.energy_each_turn"] = ("每回合能量", "Energy every turn"),
        ["event.kw.energy_once"] = ("一次性能量", "One-time energy"),

        ["bundle.single"] = ("只有一包", "Only one bundle"),
        ["bundle.pick_best"] = ("选总分最高的一包", "Highest total score"),
        ["bundle.card"] = ("包内卡", "Card"),

        ["card_select.rest_best"] = ("休息处锻造：选分数最高的卡", "Rest site Smith: highest-scoring card"),
        ["card_select.combat_best"] = ("选分数最高的卡", "Highest-scoring card"),
        ["card_select.top_scores"] = ("挑高分：选分数最高的 {max_select} 张（没有低于 3 分的卡，共 {n_cards} 张）", "Keep the best {max_select} (no card below 3, {n_cards} offered)"),
        ["card_select.lowest_scores"] = ("挑低分：选分数最低的 {max_select} 张（有低于 3 分的卡，或超过 10 张）", "Pick the worst {max_select} (a card below 3, or more than 10 offered)"),
        ["card_select.no_cards"] = ("没有可选的卡 → 跳过", "No cards -> skip"),

        ["shop.heal_potion"] = ("HP {hp_ratio:%} < 50% 且药水 {held_potions}/3 时买回复药水", "Heal potion when HP {hp_ratio:%} < 50% and potions {held_potions}/3"),
        ["shop.remove_basic"] = ("删基础牌：{n_basic} 张，需要 ≥ 3 张（{basic_scope}）", "Remove a basic card: {n_basic}, needs >= 3 ({basic_scope})"),
        ["shop.remove_value"] = ("删牌价值 {removal_value}，最佳卡 {best_card_score}", "Removal value {removal_value}, best card {best_card_score}"),
        ["shop.card_must_buy"] = ("最佳卡 {best_card_score}，必买线 {threshold}", "Best card {best_card_score}, must-buy line {threshold}"),
        ["shop.card_threshold"] = ("最佳卡 {best_card_score}，买卡阈值 {threshold}（第 {act} 幕）", "Best card {best_card_score}, buy threshold {threshold} (act {act})"),
        ["shop.relic"] = ("最佳遗物 {best_relic_score}，阈值 {threshold}，至少留 {gold_reserve} 金币", "Best relic {best_relic_score}, threshold {threshold}, keep {gold_reserve} gold"),
        ["shop.potion"] = ("最佳药水 {best_potion_score}，阈值 {threshold}，药水 {held_potions}/3", "Best potion {best_potion_score}, threshold {threshold}, potions {held_potions}/3"),
        ["shop.leave"] = ("没有值得买的 → 离开", "Nothing worth buying -> leave"),
        ["shop.relic_score"] = ("遗物分", "Relic score"),
        ["shop.potion_score"] = ("药水分", "Potion score"),

        ["map.pick_min_priority"] = ("选优先级数值最小的节点（越小越好）", "Lowest priority number wins (lower is better)"),
        ["map.base_priority"] = ("房间类型", "Room type"),
        ["map.shop_low_gold"] = ("金币不够", "Not enough gold"),
        ["map.elite_early_floor"] = ("本幕前两层", "First two floors of the act"),
        ["map.elite_hp_danger"] = ("HP < 40%", "HP < 40%"),
        ["map.elite_hp_low"] = ("HP < 60%", "HP < 60%"),
        ["map.elite_act2_healthy"] = ("第 2 幕起 HP ≥ 90%", "Act 2+, HP >= 90%"),
        ["map.elite_act2"] = ("第 2 幕起的精英", "Act 2+ elite"),
        ["map.elite_strong_midgame"] = ("HP ≥ 85% 且第 7–12 层", "HP >= 85%, floors 7-12"),
        ["map.elite_pre_boss"] = ("第 13 层起（Boss 前）", "Floor 13+ (pre-boss)"),
        ["map.elite_act2_boss_zone"] = ("第 2 幕起，第 13 层起", "Act 2+, floor 13+"),
    };

    /// <summary>Rule values that are words, not numbers.</summary>
    private static readonly Dictionary<string, (string Zh, string En)> ValueWords = new()
    {
        ["ironclad_only"] = ("只统计 Ironclad 的打击和防御（实测配置）", "Ironclad's Strike/Defend only (the measured config)"),
        ["all"] = ("统计全部角色的打击和防御", "every character's Strike/Defend"),
    };

    /// <summary>Components shown without a sign: they are a score, not an adjustment.</summary>
    private static readonly HashSet<string> Unsigned = new()
    {
        "card.base", "card.score", "card.in_deck_score", "shop.relic_score", "shop.potion_score",
        "map.base_priority", "bundle.card",
    };

    private static readonly Regex Placeholder = new(@"\{(\w+)(:%)?\}");

    public static string Rule(TraceRule rule, bool zh)
    {
        if (!Templates.TryGetValue(rule.Key, out var t))
            return rule.Values.Count == 0
                ? rule.Key
                : rule.Key + " " + string.Join(" ", rule.Values.Select(kv => $"{kv.Key}={Value(kv.Value, false, zh)}"));
        return Placeholder.Replace(zh ? t.Zh : t.En, m =>
            rule.Values.TryGetValue(m.Groups[1].Value, out var v) ? Value(v, m.Groups[2].Success, zh) : "—");
    }

    public static string Label(string key, bool zh) =>
        Templates.TryGetValue(key, out var t) ? (zh ? t.Zh : t.En) : key;

    /// <summary>"基础评级 A 6.8" / "格挡缺口 +0.6".</summary>
    public static string Component(TraceComponent c, bool zh)
    {
        var detail = c.Key == "card.base" && c.Detail is { ValueKind: JsonValueKind.String } d ? " " + d.GetString() : "";
        var number = Unsigned.Contains(c.Key) ? Num(c.Value) : Signed(c.Value);
        return $"{Label(c.Key, zh)}{detail} {number}";
    }

    public static string Num(double v) => v.ToString("0.0", CultureInfo.InvariantCulture);

    public static string Signed(double v) => (v >= 0 ? "+" : "") + Num(v);

    private static string Value(JsonElement v, bool percent, bool zh) => v.ValueKind switch
    {
        JsonValueKind.Number when percent =>
            Math.Round(v.GetDouble() * 100).ToString("0", CultureInfo.InvariantCulture) + "%",
        JsonValueKind.Number => v.GetDouble().ToString("0.##", CultureInfo.InvariantCulture),
        JsonValueKind.String => ValueWords.TryGetValue(v.GetString()!, out var w) ? (zh ? w.Zh : w.En) : v.GetString()!,
        JsonValueKind.True => zh ? "是" : "yes",
        JsonValueKind.False => zh ? "否" : "no",
        JsonValueKind.Null => "—",
        _ => v.GetRawText(),
    };
}
```

- [ ] **Step 4: 实现 `PanelText`**

```csharp
// src/Sts2Advisor.Core/PanelText.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

public enum LineStyle { Title, Pick, Normal, Fired, Muted, Warning }

public enum StatusKind { Thinking, Offline, Combat, NoAdvice, ReadFailed, Unsupported, ConfigMissing }

public sealed record PanelLine(string Text, LineStyle Style);

/// <summary>What the mod read off the screen for the panel's title line.</summary>
public sealed record DecisionHeader(string Decision, int Act, int Floor, int Ascension, int Hp, int MaxHp, int Gold);

/// <summary>One advice reply -> the panel's lines. Labels are the game's own (localized) names,
/// keyed "card:3", "option:1", "rest:0", "node:2", "relic:0", "potion:1", "bundle:1",
/// "bundle_card:1:2" -- the decision JSON itself is English-only.</summary>
public static class PanelText
{
    private const int MaxOptions = 5;

    private static readonly Dictionary<string, (string Zh, string En)> DecisionNames = new()
    {
        ["card_reward"] = ("卡牌奖励", "Card reward"),
        ["rest_site"] = ("休息处", "Rest site"),
        ["event_choice"] = ("事件", "Event"),
        ["shop"] = ("商店", "Shop"),
        ["bundle_select"] = ("卡包选择", "Bundle choice"),
        ["card_select"] = ("选牌", "Card select"),
        ["map_select"] = ("地图", "Map"),
        ["combat"] = ("战斗", "Combat"),
    };

    private static readonly Dictionary<string, (string Zh, string En)> StatusNames = new()
    {
        ["filtered"] = ("已过滤（第 1 幕后期质量门）", "filtered (late Act 1 quality gate)"),
        ["locked"] = ("已锁定", "locked"),
        ["unstocked"] = ("已售出", "sold out"),
        ["unaffordable"] = ("买不起", "can't afford"),
        ["broken"] = ("不可用", "unusable"),
    };

    private static string T(bool zh, string zhText, string enText) => zh ? zhText : enText;

    public static string Header(DecisionHeader h, bool zh)
    {
        var name = DecisionNames.TryGetValue(h.Decision, out var n) ? (zh ? n.Zh : n.En) : h.Decision;
        return $"{name} · A{h.Act}F{h.Floor}a{h.Ascension} · HP {h.Hp}/{h.MaxHp} · {T(zh, "金币", "Gold")} {h.Gold}";
    }

    public static IReadOnlyList<PanelLine> Status(DecisionHeader? h, StatusKind kind, string? detail, bool zh)
    {
        var lines = new List<PanelLine>();
        if (h != null)
            lines.Add(new PanelLine(Header(h, zh), LineStyle.Title));
        lines.Add(kind switch
        {
            StatusKind.Thinking => new PanelLine(T(zh, "正在获取建议…", "Getting advice…"), LineStyle.Muted),
            StatusKind.Offline => new PanelLine(T(zh, "建议服务离线，正在重连", "Advice service offline, reconnecting"), LineStyle.Warning),
            StatusKind.Combat => new PanelLine(T(zh, "战斗中，交给 CombatSolver", "In combat: CombatSolver's job"), LineStyle.Muted),
            StatusKind.NoAdvice => new PanelLine(T(zh, "此处暂无建议", "No advice for this screen"), LineStyle.Muted),
            StatusKind.ReadFailed => new PanelLine(T(zh, "读取失败：", "Could not read: ") + detail, LineStyle.Warning),
            StatusKind.Unsupported => new PanelLine(T(zh, "游戏版本未适配，建议已停用", "Unsupported game version; advice is off"), LineStyle.Warning),
            StatusKind.ConfigMissing => new PanelLine(T(zh, "缺少配置：", "Missing config: ") + detail, LineStyle.Warning),
            _ => new PanelLine(kind.ToString(), LineStyle.Muted),
        });
        return lines;
    }

    public static IReadOnlyList<PanelLine> Render(DecisionHeader h, AdviceReply reply,
                                                  IReadOnlyDictionary<string, string> labels, bool zh)
    {
        var lines = new List<PanelLine> { new(Header(h, zh), LineStyle.Title) };
        if (reply.Action == null)
        {
            lines.Add(new PanelLine(T(zh, "无建议", "No advice"), LineStyle.Warning));
            lines.AddRange(reply.Warnings.Select(w => new PanelLine(w, LineStyle.Muted)));
            return lines;
        }

        lines.Add(new PanelLine(T(zh, "建议：", "Pick: ") + PickText(h.Decision, reply, labels, zh), LineStyle.Pick));

        bool ascending = h.Decision == "map_select";
        var ok = reply.Options.Where(o => o.Status == "ok" && o.Score != null)
            .OrderBy(o => KindOrder(o.Kind))
            .ThenBy(o => ascending ? o.Score!.Value : -o.Score!.Value)
            .Take(MaxOptions);
        foreach (var o in ok)
        {
            lines.Add(new PanelLine($"{OptionLabel(o, labels)}  {TraceText.Num(o.Score!.Value)}",
                                    IsPicked(o, reply) ? LineStyle.Fired : LineStyle.Normal));
            var parts = TopComponents(o, labels, zh);
            if (parts.Length > 0)
                lines.Add(new PanelLine("  " + parts, LineStyle.Muted));
        }
        foreach (var o in reply.Options.Where(o => o.Status != "ok"))
        {
            var status = StatusNames.TryGetValue(o.Status, out var s) ? (zh ? s.Zh : s.En) : o.Status;
            lines.Add(new PanelLine($"{OptionLabel(o, labels)}  {status}", LineStyle.Muted));
        }
        foreach (var r in reply.Rules)
            lines.Add(new PanelLine(TraceText.Rule(r, zh), r.Outcome == "fired" ? LineStyle.Fired : LineStyle.Muted));

        if (h.Decision == "event_choice")
            lines.Add(new PanelLine(T(zh, "按选项英文描述的关键词打分", "Scored by keywords in the option's English text"), LineStyle.Muted));
        if (h.Decision == "shop")
            lines.Add(new PanelLine(T(zh, "买完会重新评估下一步", "Re-evaluated after every purchase"), LineStyle.Muted));
        lines.Add(new PanelLine(reply.Unverified
            ? T(zh, "启发式，只看下一步，未经 A/B 验证", "Heuristic, next step only, not A/B tested")
            : T(zh, "greedy 启发式打分，不是概率", "greedy heuristic scores, not probabilities"), LineStyle.Muted));
        lines.AddRange(reply.Warnings.Select(w => new PanelLine(w, LineStyle.Warning)));
        return lines;
    }

    private static int KindOrder(string kind) => kind switch
    {
        "card" => 0, "relic" => 1, "potion" => 2, _ => 3,
    };

    private static string Label(IReadOnlyDictionary<string, string> labels, string key, string fallback) =>
        labels.TryGetValue(key, out var v) ? v : fallback;

    private static string OptionLabel(TraceOption o, IReadOnlyDictionary<string, string> labels) =>
        Label(labels, $"{o.Kind}:{o.Index}", $"#{o.Index}");

    private static string TopComponents(TraceOption o, IReadOnlyDictionary<string, string> labels, bool zh)
    {
        var shown = o.Components.Where(c => c.Key == "card.base" || c.Key == "map.base_priority").ToList();
        shown.AddRange(o.Components
            .Where(c => c.Key != "card.base" && c.Key != "map.base_priority" && Math.Abs(c.Value) > 1e-9)
            .OrderByDescending(c => Math.Abs(c.Value))
            .Take(3 - shown.Count));
        return string.Join(" · ", shown.Select(c =>
            c.Key == "bundle.card" && c.Detail is { ValueKind: JsonValueKind.Number } ci
                ? $"{Label(labels, $"bundle_card:{o.Index}:{ci.GetInt32()}", "?")} {TraceText.Num(c.Value)}"
                : TraceText.Component(c, zh)));
    }

    private static bool IsPicked(TraceOption o, AdviceReply reply)
    {
        int? Arg(string name) => reply.Arg(name) is { ValueKind: JsonValueKind.Number } v ? v.GetInt32() : null;
        return (reply.ActionName, o.Kind) switch
        {
            ("select_card_reward", "card") or ("buy_card", "card") => Arg("card_index") == o.Index,
            ("choose_option", "option") => Arg("option_index") == o.Index,
            ("buy_relic", "relic") => Arg("relic_index") == o.Index,
            ("buy_potion", "potion") => Arg("potion_index") == o.Index,
            ("select_bundle", "bundle") => Arg("bundle_index") == o.Index,
            ("select_cards", "card") => SelectedIndices(reply).Contains(o.Index),
            ("select_map_node", "node") => o.Detail is { } d
                && d.TryGetProperty("col", out var col) && d.TryGetProperty("row", out var row)
                && col.GetInt32() == Arg("col") && row.GetInt32() == Arg("row"),
            _ => false,
        };
    }

    private static IReadOnlyList<int> SelectedIndices(AdviceReply reply) =>
        reply.Arg("indices") is { ValueKind: JsonValueKind.String } s
            ? s.GetString()!.Split(',', StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToList()
            : Array.Empty<int>();

    private static string PickText(string decision, AdviceReply reply, IReadOnlyDictionary<string, string> labels, bool zh)
    {
        int Index(string name) => reply.Arg(name) is { ValueKind: JsonValueKind.Number } v ? v.GetInt32() : -1;
        switch (reply.ActionName)
        {
            case "select_card_reward":
            case "buy_card":
                return Label(labels, $"card:{Index("card_index")}", "?");
            case "skip_card_reward":
            case "skip_select":
                return T(zh, "跳过", "Skip");
            case "choose_option":
                return Label(labels, $"{(decision == "rest_site" ? "rest" : "option")}:{Index("option_index")}", "?");
            case "buy_relic":
                return Label(labels, $"relic:{Index("relic_index")}", "?");
            case "buy_potion":
                return Label(labels, $"potion:{Index("potion_index")}", "?");
            case "remove_card":
                return T(zh, "删一张牌", "Remove a card");
            case "leave_room":
                return T(zh, "离开", "Leave");
            case "select_bundle":
                return Label(labels, $"bundle:{Index("bundle_index")}", "?");
            case "select_cards":
                return string.Join(T(zh, "、", ", "),
                    SelectedIndices(reply).Select(i => Label(labels, $"card:{i}", $"#{i}")));
            case "select_map_node":
                var node = reply.Options.FirstOrDefault(o => o.Kind == "node" && IsPicked(o, reply));
                return node != null ? OptionLabel(node, labels) : $"({Index("col")},{Index("row")})";
            default:
                return reply.ActionName ?? "?";
        }
    }
}
```

- [ ] **Step 5: 运行，确认通过**

```bash
~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3
.venv/bin/python -m pytest tests/test_advisor_trace_text_keys.py -q
```

Expected：`Passed!  - Failed: 0, Passed: 19`；Python `1 passed`

如果 `CardRewardPanel` 的某一行对不上，先打印 `lines` 对照看。期望值是按上面 `Render` 的规则手算出来的（options → 非 ok 选项 → rules → 页脚 → warnings），不要为了让测试通过去改期望值，除非确认是手算错了。

- [ ] **Step 6: 提交**

```bash
git add src/Sts2Advisor.Core/TraceText.cs src/Sts2Advisor.Core/PanelText.cs src/Sts2Advisor.Core.Tests/PanelTextTests.cs tests/test_advisor_trace_text_keys.py
git commit -m "feat(advisor-mod): panel text from the greedy trace, zh and en

Every TRACE_KEYS entry has a template (a Python test keeps the two in
step); game terms use the official localization_zhs names.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C4: `AdvisorTraceLog`

**Files:**
- Create: `src/Sts2Advisor.Core/AdvisorTraceLog.cs`
- Test: `src/Sts2Advisor.Core.Tests/AdvisorTraceLogTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AdvisorTraceLogTests.cs
using System.Text.Json;
using Sts2Advisor.Core;

public class AdvisorTraceLogTests
{
    [Fact]
    public void WritesOneJsonLinePerDecisionWithTheRawStateAndReply()
    {
        var path = Path.Combine(Path.GetTempPath(), $"trace-{Guid.NewGuid()}", "advisor_trace.jsonl");
        var log = new AdvisorTraceLog(path);
        log.Append("rest_site", """{"decision": "rest_site", "options": []}""", """{"action": null}""", null);
        log.Append("shop", """{"decision": "shop"}""", null, "HttpRequestException");
        var lines = File.ReadAllLines(path);
        Assert.Equal(2, lines.Length);
        using var first = JsonDocument.Parse(lines[0]);
        Assert.Equal("rest_site", first.RootElement.GetProperty("state").GetProperty("decision").GetString());
        Assert.Equal(JsonValueKind.Null, first.RootElement.GetProperty("reply").GetProperty("action").ValueKind);
        using var second = JsonDocument.Parse(lines[1]);
        Assert.Equal(JsonValueKind.Null, second.RootElement.GetProperty("reply").ValueKind);
        Assert.Equal("HttpRequestException", second.RootElement.GetProperty("error").GetString());
    }
}
```

- [ ] **Step 2: 运行，确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3`
Expected：编译失败，找不到 `AdvisorTraceLog`

- [ ] **Step 3: 实现**

```csharp
// src/Sts2Advisor.Core/AdvisorTraceLog.cs
using System.Text;
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>advisor_trace.jsonl: every decision JSON the mod sent and the reply it got.
/// The first in-game session's file becomes tests/fixtures/advisor/real_game_trace.jsonl,
/// which tests/test_advisor_trace_contract.py checks against headless field shapes.</summary>
public sealed class AdvisorTraceLog
{
    private readonly string _path;
    private readonly object _gate = new();

    public AdvisorTraceLog(string path)
    {
        _path = path;
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
    }

    public void Append(string decision, string stateJson, string? replyJson, string? error)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteString("ts", DateTime.UtcNow.ToString("O"));
            w.WriteString("decision", decision);
            w.WritePropertyName("state");
            w.WriteRawValue(stateJson);
            w.WritePropertyName("reply");
            if (replyJson == null) w.WriteNullValue(); else w.WriteRawValue(replyJson);
            if (error != null) w.WriteString("error", error);
            w.WriteEndObject();
        }
        var line = Encoding.UTF8.GetString(buffer.ToArray()) + "\n";
        lock (_gate)
            File.AppendAllText(_path, line);
    }
}
```

- [ ] **Step 4: 运行，确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -3`
Expected: `Passed!  - Failed: 0, Passed: 20`

- [ ] **Step 5: 提交**

```bash
git add src/Sts2Advisor.Core/AdvisorTraceLog.cs src/Sts2Advisor.Core.Tests/AdvisorTraceLogTests.cs
git commit -m "feat(advisor-mod): advisor_trace.jsonl writer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C5: mod 工程骨架、清单、入口、安装脚本

**Files:**
- Create: `src/Sts2Advisor/Sts2Advisor.csproj`、`src/Sts2Advisor/Sts2Advisor.json`、`src/Sts2Advisor/ModEntry.cs`、`src/Sts2Advisor/ModLog.cs`
- Create: `src/Sts2Advisor/AdvisorRuntime.cs`（本任务只放一个空的 `Start`/`Tick`，Task C7 再补全）
- Create: `tools/install_advisor_mod.sh`

- [ ] **Step 1: 工程文件**

```xml
<!-- src/Sts2Advisor/Sts2Advisor.csproj -->
<!-- The in-game Sts2Advisor mod. Builds against the REAL game assemblies (not src/GodotStubs, not the
     IL-patched lib/sts2.dll). Shared serializer and Core sources are compiled in: the game's
     ModManager loads exactly one <id>.dll per mod. -->
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net9.0</TargetFramework>
    <OutputType>Library</OutputType>
    <ImplicitUsings>enable</ImplicitUsings>
    <Nullable>enable</Nullable>
    <LangVersion>13.0</LangVersion>
    <AssemblyName>Sts2Advisor</AssemblyName>
    <RootNamespace>Sts2Advisor</RootNamespace>
    <EnableDynamicLoading>true</EnableDynamicLoading>
    <Sts2DataDir Condition="'$(Sts2DataDir)' == ''">$(HOME)/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64</Sts2DataDir>
    <NoWarn>$(NoWarn);CS0436</NoWarn>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="Krafs.Publicizer" Version="2.3.0" PrivateAssets="all" />
    <Publicize Include="sts2" IncludeVirtualMembers="false" IncludeCompilerGeneratedMembers="false" />
  </ItemGroup>

  <ItemGroup>
    <Reference Include="sts2">
      <HintPath>$(Sts2DataDir)/sts2.dll</HintPath>
      <Private>false</Private>
    </Reference>
    <Reference Include="GodotSharp">
      <HintPath>$(Sts2DataDir)/GodotSharp.dll</HintPath>
      <Private>false</Private>
    </Reference>
  </ItemGroup>

  <ItemGroup>
    <Compile Include="..\Sts2Shared\**\*.cs" Link="Sts2Shared\%(RecursiveDir)%(Filename)%(Extension)" />
    <Compile Include="..\Sts2Advisor.Core\**\*.cs"
             Exclude="..\Sts2Advisor.Core\bin\**;..\Sts2Advisor.Core\obj\**"
             Link="Core\%(RecursiveDir)%(Filename)%(Extension)" />
    <None Update="Sts2Advisor.json" CopyToOutputDirectory="PreserveNewest" />
  </ItemGroup>

  <Target Name="CheckGameInstall" BeforeTargets="ResolveAssemblyReferences">
    <Error Condition="!Exists('$(Sts2DataDir)/sts2.dll')"
           Text="sts2.dll not found under Sts2DataDir=$(Sts2DataDir). Install the game or pass -p:Sts2DataDir=..." />
  </Target>
</Project>
```

- [ ] **Step 2: 清单**

```json
{
  "id": "Sts2Advisor",
  "name": "Sts2Advisor",
  "author": "morefreeze",
  "description": "Out-of-combat advice (card rewards, rest sites, events, shops, maps) with the reasons behind each pick. Combat is left to CombatSolver.",
  "version": "0.1.0",
  "has_pck": false,
  "has_dll": true,
  "affects_gameplay": false,
  "min_game_version": "0.111.0",
  "dependencies": []
}
```

- [ ] **Step 3: `ModLog.cs`**

```csharp
// src/Sts2Advisor/ModLog.cs
namespace Sts2Advisor;

/// <summary>&lt;game user dir&gt;/Sts2Advisor/Sts2Advisor.log, plus the game's own log.</summary>
internal static class ModLog
{
    private static readonly object Gate = new();
    private static string _path = "";

    public static void Init(string dir)
    {
        Directory.CreateDirectory(dir);
        _path = Path.Combine(dir, "Sts2Advisor.log");
    }

    public static void Info(string message) => Write("INFO", message);

    public static void Error(string message) => Write("ERROR", message);

    private static void Write(string level, string message)
    {
        try
        {
            lock (Gate)
                File.AppendAllText(_path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {level} {message}\n");
        }
        catch (Exception) { /* logging must never take the game down */ }
        try
        {
            if (level == "ERROR") MegaCrit.Sts2.Core.Logging.Log.Error("[Sts2Advisor] " + message);
            else MegaCrit.Sts2.Core.Logging.Log.Info("[Sts2Advisor] " + message);
        }
        catch (Exception) { }
    }
}
```

- [ ] **Step 4: `ModEntry.cs` 和占位的 `AdvisorRuntime.cs`**

```csharp
// src/Sts2Advisor/ModEntry.cs
using Godot;
using MegaCrit.Sts2.Core.Modding;
using Sts2Advisor.Core;

namespace Sts2Advisor;

[ModInitializer(nameof(Initialize))]
public static class ModEntry
{
    public const string ModId = "Sts2Advisor";

    /// <summary>Called by the game's ModManager very early -- before LocManager and ModelDb exist.
    /// Only reads config, opens the log and hooks the frame loop; everything that touches the game
    /// waits for AdvisorRuntime.Tick.</summary>
    public static void Initialize()
    {
        string userDir = System.IO.Path.Combine(OS.GetUserDataDir(), ModId);
        ModLog.Init(userDir);
        string configPath = System.IO.Path.Combine(userDir, "config.json");
        AdvisorConfig config;
        try
        {
            config = AdvisorConfig.Load(configPath);
        }
        catch (Exception ex)
        {
            ModLog.Error($"bad {configPath}: {ex.Message}; using defaults");
            config = AdvisorConfig.Defaults;
        }
        ModLog.Info($"loaded; config {configPath}: repo={config.RepoPath} port={config.Port} autostart={config.Autostart} hotkey={config.Hotkey}");
        AdvisorRuntime.Start(config, configPath, userDir);
        var tree = (SceneTree)Engine.GetMainLoop();
        tree.Connect(SceneTree.SignalName.ProcessFrame, Callable.From(AdvisorRuntime.Tick));
    }
}
```

```csharp
// src/Sts2Advisor/AdvisorRuntime.cs  (Task C5 placeholder; Task C7 replaces this whole file)
using Sts2Advisor.Core;

namespace Sts2Advisor;

internal static class AdvisorRuntime
{
    private static bool _firstTickLogged;

    public static void Start(AdvisorConfig config, string configPath, string userDir) { }

    public static void Tick()
    {
        if (_firstTickLogged) return;
        _firstTickLogged = true;
        ModLog.Info("first frame tick");
    }
}
```

（在 `using Godot;` 的文件里，`Path`、`Environment` 等名字可能和 Godot 的类型冲突，所以 `ModEntry` 里写全名 `System.IO.Path`。）

- [ ] **Step 5: 安装脚本**

```bash
#!/bin/bash
# tools/install_advisor_mod.sh -- build the Sts2Advisor mod and install it into the local Steam copy.
# Re-run after a Steam update or "verify integrity of game files" (either can remove mods/).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GAME_APP="${STS2_GAME_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app}"
DATA_DIR="$GAME_APP/Contents/Resources/data_sts2_macos_arm64"
MODS_DIR="$GAME_APP/Contents/MacOS/mods/Sts2Advisor"
USER_DIR="${STS2_USER_DIR:-$HOME/Library/Application Support/SlayTheSpire2}/Sts2Advisor"
DOTNET="${DOTNET:-$HOME/.dotnet-arm64/dotnet}"

"$DOTNET" build "$ROOT/src/Sts2Advisor/Sts2Advisor.csproj" -c Release -p:Sts2DataDir="$DATA_DIR"
OUT="$ROOT/src/Sts2Advisor/bin/Release/net9.0"
mkdir -p "$MODS_DIR"
cp "$OUT/Sts2Advisor.dll" "$OUT/Sts2Advisor.json" "$MODS_DIR/"

mkdir -p "$USER_DIR"
if [ ! -f "$USER_DIR/config.json" ]; then
  cat > "$USER_DIR/config.json" <<EOF
{
  "repo_path": "$ROOT",
  "python": "$ROOT/.venv/bin/python",
  "port": 8767,
  "autostart": true,
  "hotkey": "F8"
}
EOF
  echo "wrote $USER_DIR/config.json"
fi
echo "installed to $MODS_DIR"
ls -la "$MODS_DIR"
```

`chmod +x tools/install_advisor_mod.sh`

- [ ] **Step 6: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -5`
Expected: `0 Error(s)`。然后确认输出目录里**没有**游戏的 dll：`ls src/Sts2Advisor/bin/Release/net9.0/`，应当只有 `Sts2Advisor.dll`、`Sts2Advisor.pdb`、`Sts2Advisor.deps.json`、`Sts2Advisor.json`，没有 `sts2.dll`、`GodotSharp.dll`。

编译错误几乎都来自链接进来的 `src/Sts2Shared/*.cs`：
- 它们在 headless 里是对着 `lib/` 下 IL 打过补丁的 `sts2.dll` 编译的，这里是原版。
- 如果某个成员只在补丁版里存在，停下来报告是哪个成员，不要在 mod 里另写一份序列化。
- 如果只是缺 `using`，按编译器提示补上，并且在 headless 里同样能编译。
- 如果报 `CS0012`（某类型定义在未引用的程序集里，比如 `SmartFormat`），就在 csproj 的引用组里照 `sts2` 的写法加上那个程序集：`<HintPath>$(Sts2DataDir)/<名字>.dll</HintPath>`、`<Private>false</Private>`。

- [ ] **Step 7: 安装并做第一次加载检查（需要用户操作）**

```bash
tools/install_advisor_mod.sh
```

然后请用户：
1. 从 Steam 启动游戏；
2. 第一次出现"是否加载 mod"弹窗时选同意，游戏可能要求重启；
3. 进到主菜单后退出。

再检查：

```bash
cat "$HOME/Library/Application Support/SlayTheSpire2/Sts2Advisor/Sts2Advisor.log"
grep -n "Sts2Advisor" "$HOME/Library/Application Support/SlayTheSpire2/logs/godot.log" | tail -20
```

Expected：`Sts2Advisor.log` 里有 `loaded; config ...` 和 `first frame tick` 两行，`godot.log` 里没有 `Sts2Advisor` 相关的 `MOD_ERROR` 或异常。

如果 `Sts2Advisor.log` 不存在，说明用户数据目录不是预期的路径：在 `godot.log` 里搜 `Sts2Advisor` 看加载结果，并在报告里写明实际路径。之后 `STS2_USER_DIR` 要指向那里。

- [ ] **Step 8: 提交**

```bash
git add src/Sts2Advisor/Sts2Advisor.csproj src/Sts2Advisor/Sts2Advisor.json src/Sts2Advisor/ModEntry.cs src/Sts2Advisor/ModLog.cs src/Sts2Advisor/AdvisorRuntime.cs tools/install_advisor_mod.sh
git commit -m "feat(advisor-mod): mod project, manifest, entry point, install script

Plain Microsoft.NET.Sdk against the real game assemblies (Private=false),
Krafs.Publicizer, Sts2Shared + Core compiled in. Loads in the real game
(Sts2Advisor.log: 'loaded', 'first frame tick').

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C6: `GameText` 和 `DecisionCapture`

**Files:**
- Create: `src/Sts2Advisor/GameText.cs`、`src/Sts2Advisor/DecisionCapture.cs`

这两个文件直接读游戏对象，没法离线单测；在 Task C8 的实机验收里验证。代码要保持很薄：识别界面、取数据、调 `DecisionSerializer`，不做任何判断逻辑。

- [ ] **Step 1: `GameText.cs`**

```csharp
// src/Sts2Advisor/GameText.cs
using System.Text.RegularExpressions;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;

namespace Sts2Advisor;

/// <summary>Names as the game shows them, in its current language (official translations).
/// Main thread only. Never throws: a name that fails to resolve falls back to its id.</summary>
internal static class GameText
{
    private static readonly Regex Markup = new(@"\[/?[a-zA-Z_][a-zA-Z0-9_=#]*\]");

    public static string? Language => LocManager.Instance?.Language;

    public static bool IsChinese => Language is "zhs" or "zht";

    public static string Card(CardModel card) => Safe(() => card.Title, card.Id.Entry);

    public static string Relic(RelicModel? relic) =>
        relic == null ? "?" : Safe(() => relic.Title.GetFormattedText(), relic.Id.Entry);

    public static string Potion(PotionModel? potion) =>
        potion == null ? "?" : Safe(() => potion.Title.GetFormattedText(), potion.Id.Entry);

    public static string Loc(LocString? text, string fallback) =>
        text == null ? fallback : Safe(text.GetFormattedText, fallback);

    /// <summary>"精英 (3,4)": the map legend's own name for the room type.</summary>
    public static string MapNode(MapPoint point)
    {
        string key = point.PointType switch
        {
            MapPointType.Monster => "LEGEND_ENEMY",
            MapPointType.Elite => "LEGEND_ELITE",
            MapPointType.RestSite => "LEGEND_REST",
            MapPointType.Shop => "LEGEND_MERCHANT",
            MapPointType.Treasure => "LEGEND_TREASURE",
            MapPointType.Unknown => "LEGEND_UNKNOWN",
            MapPointType.Boss => "LEGEND_BOSS",
            MapPointType.Ancient => "LEGEND_ANCIENT",
            _ => "",
        };
        string name = key == "" ? point.PointType.ToString()
            : Safe(() => new LocString("map", key + ".title").GetFormattedText(), point.PointType.ToString());
        return $"{name} ({point.coord.col},{point.coord.row})";
    }

    private static string Safe(Func<string> get, string fallback)
    {
        try
        {
            var text = get();
            return string.IsNullOrWhiteSpace(text) ? fallback : Markup.Replace(text, "");
        }
        catch (Exception)
        {
            return fallback;
        }
    }
}
```

- [ ] **Step 2: `DecisionCapture.cs`**

```csharp
// src/Sts2Advisor/DecisionCapture.cs
using MegaCrit.Sts2.Core.CardSelection;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.ScreenContext;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

internal enum CaptureKind { Advise, Combat, NoAdvice }

/// <summary>One look at the current screen. Build() produces the decision JSON (the headless
/// engine's shape, via the shared DecisionSerializer); it must run on the main thread, in the
/// same tick as Detect().</summary>
internal sealed class Capture
{
    public required CaptureKind Kind { get; init; }
    public required string Screen { get; init; }
    public required DecisionHeader Header { get; init; }
    public string Fingerprint { get; init; } = "";
    public Dictionary<string, string> Labels { get; init; } = new();
    public Func<Dictionary<string, object?>>? Build { get; init; }
}

/// <summary>Which decision the player faces, read the way the game's own screen resolver
/// (ActiveScreenContext) and its AutoSlay autoplayer see it. Main thread only. Reads, never writes.</summary>
internal static class DecisionCapture
{
    /// <summary>Null outside a run (main menu, loading).</summary>
    public static Capture? Detect(LocLookup loc)
    {
        var rs = RunManager.Instance?.DebugOnlyGetState();
        if (rs == null)
            return null;
        var player = LocalContext.GetMe(rs) ?? rs.Players.FirstOrDefault();
        if (player == null)
            return null;
        if (CombatManager.Instance?.IsInProgress == true)
            return Simple(CaptureKind.Combat, "combat", rs, player);

        var screen = ActiveScreenContext.Instance?.GetCurrentScreen();
        return screen switch
        {
            null => null,
            NMapScreen map when map.IsOpen && map.IsTravelEnabled => Map(rs, player, loc),
            NCardRewardSelectionScreen s => CardReward(s, rs, player, loc),
            NCardGridSelectionScreen g => GridSelect(g, rs, player, loc),
            NChooseACardSelectionScreen c => CardSelect(nameof(NChooseACardSelectionScreen), c._cards,
                                                        c._canSkip ? 0 : 1, 1, rs, player, loc),
            NChooseABundleSelectionScreen b => Bundle(b, rs, player, loc),
            NEventRoom when rs.CurrentRoom is EventRoom er => Event(er, rs, player, loc),
            NRestSiteRoom when rs.CurrentRoom is RestSiteRoom rr => Rest(rr, rs, player, loc),
            NMerchantRoom or NMerchantInventory when rs.CurrentRoom is MerchantRoom mr => Shop(mr, rs, player, loc),
            _ => Simple(CaptureKind.NoAdvice, screen.GetType().Name, rs, player),
        };
    }

    private static DecisionHeader Header(string decision, RunState rs, Player p) =>
        new(decision, rs.CurrentActIndex + 1, rs.ActFloor, rs.AscensionLevel,
            p.Creature?.CurrentHp ?? 0, p.Creature?.MaxHp ?? 0, p.Gold);

    /// <summary>Progress, HP, gold, deck and belt size: any change re-asks the advisor.</summary>
    private static string Common(RunState rs, Player p) =>
        $"{rs.CurrentActIndex}/{rs.ActFloor}/{p.Creature?.CurrentHp}/{p.Creature?.MaxHp}/{p.Gold}/" +
        $"{p.Deck?.Cards?.Count}/{p.Potions?.Count()}";

    private static string CardKey(CardModel c) => c.Id.Entry + (c.IsUpgraded ? "+" + c.CurrentUpgradeLevel : "");

    private static Capture Simple(CaptureKind kind, string screen, RunState rs, Player p) =>
        new() { Kind = kind, Screen = screen, Header = Header(kind == CaptureKind.Combat ? "combat" : screen, rs, p) };

    private static Capture Map(RunState rs, Player player, LocLookup loc)
    {
        IEnumerable<MapPoint> next = rs.VisitedMapCoords.Count == 0
            ? new[] { rs.Map.StartingMapPoint }
            : MapTravel.GetTravelablePointsFrom(rs, rs.CurrentMapPoint!);
        var points = next.ToList();
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < points.Count; i++)
            labels[$"node:{i}"] = GameText.MapNode(points[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NMapScreen), Header = Header("map_select", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("map_select",
                new object?[] { Common(rs, player) }.Concat(points.Select(p => $"{p.coord.col},{p.coord.row}"))),
            Build = () => DecisionSerializer.MapSelect(rs, points.Select(DecisionSerializer.MapChoice).ToList(), loc),
        };
    }

    private static Capture CardReward(NCardRewardSelectionScreen s, RunState rs, Player player, LocLookup loc)
    {
        var cards = s._options.Select(o => o.Card).ToList();
        bool canSkip = s._extraOptions.Any(a => string.Equals(a.OptionId, "Skip", StringComparison.OrdinalIgnoreCase));
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cards.Count; i++)
            labels[$"card:{i}"] = GameText.Card(cards[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NCardRewardSelectionScreen),
            Header = Header("card_reward", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("card_reward",
                new object?[] { Common(rs, player), canSkip }.Concat(cards.Select(CardKey))),
            // gold_earned: the real game gives no pre-combat gold snapshot; greedy does not read it.
            Build = () => DecisionSerializer.CardReward(cards, canSkip, null, rs, loc),
        };
    }

    private static Capture GridSelect(NCardGridSelectionScreen g, RunState rs, Player player, LocLookup loc)
    {
        CardSelectorPrefs? prefs = g switch
        {
            NDeckUpgradeSelectScreen s => s._prefs,
            NDeckTransformSelectScreen s => s._prefs,
            NDeckEnchantSelectScreen s => s._prefs,
            NDeckCardSelectScreen s => s._prefs,
            NSimpleCardSelectScreen s => s._prefs,
            _ => null,  // NCombatPileCardSelectScreen: in combat, never reached here
        };
        if (prefs == null)
            return Simple(CaptureKind.NoAdvice, g.GetType().Name, rs, player);
        return CardSelect(g.GetType().Name, g._cards, prefs.Value.MinSelect, prefs.Value.MaxSelect, rs, player, loc);
    }

    private static Capture CardSelect(string screen, IReadOnlyList<CardModel> cards, int min, int max,
                                      RunState rs, Player player, LocLookup loc)
    {
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cards.Count; i++)
            labels[$"card:{i}"] = GameText.Card(cards[i]);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = screen, Header = Header("card_select", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("card_select",
                new object?[] { screen, min, max, Common(rs, player) }.Concat(cards.Select(CardKey))),
            Build = () => DecisionSerializer.CardSelect(cards, min, max, player, rs, loc),
        };
    }

    private static Capture Bundle(NChooseABundleSelectionScreen b, RunState rs, Player player, LocLookup loc)
    {
        var bundles = b._bundles;
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < bundles.Count; i++)
        {
            labels[$"bundle:{i}"] = GameText.IsChinese ? $"第 {i + 1} 包" : $"Bundle {i + 1}";
            for (int j = 0; j < bundles[i].Count; j++)
                labels[$"bundle_card:{i}:{j}"] = GameText.Card(bundles[i][j]);
        }
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NChooseABundleSelectionScreen),
            Header = Header("bundle_select", rs, player), Labels = labels,
            Fingerprint = Fingerprint.Of("bundle_select",
                new object?[] { Common(rs, player) }.Concat(bundles.Select(x => string.Join(",", x.Select(CardKey))))),
            Build = () => DecisionSerializer.BundleSelect(bundles, player, rs, loc),
        };
    }

    private static Capture Event(EventRoom er, RunState rs, Player player, LocLookup loc)
    {
        var ev = er.LocalMutableEvent;
        var options = ev.CurrentOptions;
        if (ev.IsFinished || options.Count == 0)
            return Simple(CaptureKind.NoAdvice, "event_finished", rs, player);
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < options.Count; i++)
            labels[$"option:{i}"] = GameText.Loc(options[i].Title, options[i].TextKey);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NEventRoom), Header = Header("event_choice", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("event_choice",
                new object?[] { Common(rs, player), ev.Id.Entry, ev.Description?.LocEntryKey }
                    .Concat(options.Select(o => $"{o.TextKey}#{o.IsLocked}#{o.WasChosen}"))),
            Build = () => DecisionSerializer.EventChoice(ev, rs, loc),
        };
    }

    private static Capture Rest(RestSiteRoom rr, RunState rs, Player player, LocLookup loc)
    {
        var options = rr.Options;
        if (options.Count == 0 || RunManager.Instance.RestSiteSynchronizer.GetChosenOptionIndex(player.NetId) != null)
            return Simple(CaptureKind.NoAdvice, "rest_site_done", rs, player);
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < options.Count; i++)
            labels[$"rest:{i}"] = GameText.Loc(options[i].Title, options[i].OptionId);
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NRestSiteRoom), Header = Header("rest_site", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("rest_site",
                new object?[] { Common(rs, player) }.Concat(options.Select(o => $"{o.OptionId}#{o.IsEnabled}"))),
            Build = () => DecisionSerializer.RestSite(options, rs, loc),
        };
    }

    private static Capture Shop(MerchantRoom mr, RunState rs, Player player, LocLookup loc)
    {
        var inv = mr.GetLocalInventory();
        var cardEntries = inv.CharacterCardEntries.Concat(inv.ColorlessCardEntries).ToList();
        var labels = new Dictionary<string, string>();
        for (int i = 0; i < cardEntries.Count; i++)
            labels[$"card:{i}"] = cardEntries[i].CreationResult?.Card is { } c ? GameText.Card(c) : "—";
        for (int i = 0; i < inv.RelicEntries.Count; i++)
            labels[$"relic:{i}"] = GameText.Relic(inv.RelicEntries[i].Model);
        for (int i = 0; i < inv.PotionEntries.Count; i++)
            labels[$"potion:{i}"] = GameText.Potion(inv.PotionEntries[i].Model);
        var stock = cardEntries.Select(e => $"c{e.IsStocked}{e.Cost}")
            .Concat(inv.RelicEntries.Select(e => $"r{e.IsStocked}{e.Cost}"))
            .Concat(inv.PotionEntries.Select(e => $"p{e.IsStocked}{e.Cost}"))
            .Append($"x{inv.CardRemovalEntry?.IsStocked}{inv.CardRemovalEntry?.Cost}");
        return new Capture
        {
            Kind = CaptureKind.Advise, Screen = nameof(NMerchantRoom), Header = Header("shop", rs, player),
            Labels = labels,
            Fingerprint = Fingerprint.Of("shop", new object?[] { Common(rs, player) }.Concat(stock)),
            Build = () => DecisionSerializer.Shop(inv, player, rs, loc),
        };
    }
}
```

`_options`、`_extraOptions`、`_cards`、`_prefs`、`_canSkip`、`_bundles` 都是私有或受保护字段，靠 Publicizer 才能编译。如果编译器说某个字段不存在，用 `/tmp/sts2-cli/decomp/_all/` 里对应的反编译文件（或重新跑 `ilspycmd -t <类型全名>`）确认真实字段名再改，不要改成反射。

- [ ] **Step 3: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -5`
Expected: `0 Error(s)`

- [ ] **Step 4: 提交**

```bash
git add src/Sts2Advisor/GameText.cs src/Sts2Advisor/DecisionCapture.cs
git commit -m "feat(advisor-mod): read the current decision from the live game

ActiveScreenContext + RunState, as the game's own AutoSlay does; decision
JSON comes from the shared DecisionSerializer, labels from the game's own
localized titles. Read-only.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C7: 面板、sidecar 进程、运行循环

**Files:**
- Create: `src/Sts2Advisor/AdvisorPanel.cs`、`src/Sts2Advisor/SidecarProcess.cs`
- Modify: `src/Sts2Advisor/AdvisorRuntime.cs`（整个文件替换）

- [ ] **Step 1: `AdvisorPanel.cs`**

```csharp
// src/Sts2Advisor/AdvisorPanel.cs
using Godot;
using MegaCrit.Sts2.Core.Localization.Fonts;
using MegaCrit.Sts2.Core.Nodes;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>Bottom-right overlay. Stock Godot nodes only -- no C# node subclasses, so no Godot
/// source generators or script registration. Ignores the mouse: it never blocks a click.</summary>
internal sealed class AdvisorPanel
{
    private readonly CanvasLayer _layer;
    private readonly PanelContainer _panel;
    private readonly RichTextLabel _text;
    private string? _fontLanguage = "";

    private AdvisorPanel(CanvasLayer layer, PanelContainer panel, RichTextLabel text)
    {
        _layer = layer;
        _panel = panel;
        _text = text;
    }

    public bool Alive => GodotObject.IsInstanceValid(_layer) && GodotObject.IsInstanceValid(_text);

    /// <summary>Null until the game's root node exists.</summary>
    public static AdvisorPanel? TryCreate()
    {
        var host = NGame.Instance;
        if (host == null || !GodotObject.IsInstanceValid(host))
            return null;
        var layer = new CanvasLayer { Name = "Sts2AdvisorOverlay", Layer = 110 };
        var panel = new PanelContainer { Name = "Panel", MouseFilter = Control.MouseFilterEnum.Ignore };
        var box = new StyleBoxFlat { BgColor = new Color(0.06f, 0.06f, 0.08f, 0.82f) };
        box.SetCornerRadiusAll(8);
        box.SetContentMarginAll(12);
        panel.AddThemeStyleboxOverride("panel", box);
        var text = new RichTextLabel
        {
            Name = "Text",
            BbcodeEnabled = true,
            FitContent = true,
            ScrollActive = false,
            MouseFilter = Control.MouseFilterEnum.Ignore,
            AutowrapMode = TextServer.AutowrapMode.WordSmart,
            CustomMinimumSize = new Vector2(380, 0),
        };
        text.AddThemeFontSizeOverride("normal_font_size", 15);
        text.AddThemeFontSizeOverride("bold_font_size", 15);
        panel.AddChild(text);
        layer.AddChild(panel);
        panel.SetAnchorsAndOffsetsPreset(Control.LayoutPreset.BottomRight, Control.LayoutPresetMode.Minsize, 16);
        panel.GrowHorizontal = Control.GrowDirection.Begin;
        panel.GrowVertical = Control.GrowDirection.Begin;
        panel.Visible = false;
        host.CallDeferred(Node.MethodName.AddChild, layer);
        host.TreeExiting += SidecarProcess.Stop;
        return new AdvisorPanel(layer, panel, text);
    }

    public void Show(IReadOnlyList<PanelLine> lines, string? language)
    {
        if (!Alive) return;
        if (language != _fontLanguage)
        {
            _fontLanguage = language;
            // The game's CJK substitute font; a no-op for languages that need none.
            _text.ApplyLocaleFontSubstitution(FontType.Regular, "normal_font");
            _text.ApplyLocaleFontSubstitution(FontType.Bold, "bold_font");
        }
        _text.Text = string.Join("\n", lines.Select(Bbcode));
        _panel.Visible = true;
    }

    public void Hide()
    {
        if (Alive) _panel.Visible = false;
    }

    private static string Bbcode(PanelLine line)
    {
        string t = line.Text.Replace("[", "[lb]");
        return line.Style switch
        {
            LineStyle.Title => $"[color=#a8a8b3][font_size=13]{t}[/font_size][/color]",
            LineStyle.Pick => $"[b][color=#7fc8ff]{t}[/color][/b]",
            LineStyle.Fired => $"[color=#ffffff]{t}[/color]",
            LineStyle.Normal => $"[color=#d9d9e0]{t}[/color]",
            LineStyle.Muted => $"[color=#8a8a96]{t}[/color]",
            LineStyle.Warning => $"[color=#ffc66b]{t}[/color]",
            _ => t,
        };
    }
}
```

（Godot 的枚举成员名如果和这里写的不一样，比如 `LayoutPresetMode.Minsize`，编译器会给出正确的名字，照改即可。）

- [ ] **Step 2: `SidecarProcess.cs`**

```csharp
// src/Sts2Advisor/SidecarProcess.cs
using System.Diagnostics;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>Starts agent/advisor_server.py once per game launch, unless a healthy service already
/// answers on the port (then it is reused). Its stderr goes to Sts2Advisor.log.</summary>
internal static class SidecarProcess
{
    private static Process? _process;

    public static async Task EnsureStartedAsync(AdvisorConfig config, IAdvisor advisor)
    {
        try
        {
            if (await advisor.IsHealthyAsync(CancellationToken.None).ConfigureAwait(false))
            {
                ModLog.Info($"advice service already running on port {config.Port}; reusing it");
                return;
            }
            if (!config.Autostart)
            {
                ModLog.Info("autostart is off; waiting for an advice service");
                return;
            }
            string script = Path.Combine(config.RepoPath, "agent", "advisor_server.py");
            string python = config.ResolvedPython;
            if (!File.Exists(script) || !File.Exists(python))
            {
                ModLog.Error($"cannot start the advice service: python={python} script={script}");
                return;
            }
            var info = new ProcessStartInfo(python)
            {
                WorkingDirectory = config.RepoPath,
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
            };
            info.ArgumentList.Add(script);
            info.ArgumentList.Add("--port");
            info.ArgumentList.Add(config.Port.ToString());
            _process = Process.Start(info)!;
            _process.ErrorDataReceived += (_, e) => { if (e.Data != null) ModLog.Info("[advisor_server] " + e.Data); };
            _process.OutputDataReceived += (_, e) => { if (e.Data != null) ModLog.Info("[advisor_server] " + e.Data); };
            _process.BeginErrorReadLine();
            _process.BeginOutputReadLine();
            AppDomain.CurrentDomain.ProcessExit += (_, _) => Stop();
            ModLog.Info($"started advice service, pid {_process.Id}");
        }
        catch (Exception ex)
        {
            ModLog.Error($"starting the advice service failed: {ex}");
        }
    }

    public static void Stop()
    {
        try
        {
            if (_process is { HasExited: false })
                _process.Kill(entireProcessTree: true);
        }
        catch (Exception) { }
    }
}
```

- [ ] **Step 3: `AdvisorRuntime.cs`（整个文件替换）**

```csharp
// src/Sts2Advisor/AdvisorRuntime.cs
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Serialization;
using Godot;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor;

/// <summary>Per-frame driver (SceneTree.ProcessFrame, main thread). Every 0.3 s: read the screen,
/// and when its fingerprint changed, serialize it (main thread) and ask the advisor (background).
/// Replies are applied on a later frame, only if the screen has not changed meanwhile.</summary>
internal static class AdvisorRuntime
{
    private const double PollSeconds = 0.3;
    private const int MaxScreenFailures = 3;

    // Same options as src/Sts2Headless/Program.cs's JsonOpts, so the JSON matches the headless engine's.
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = false,
    };

    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static readonly Backoff Reconnect = new();
    private static readonly ConcurrentQueue<(string Fingerprint, AdviceReply? Reply, string? Error)> Results = new();

    private static IAdvisor? _advisor;
    private static LocLookup? _loc;
    private static AdvisorTraceLog? _trace;
    private static AdvisorPanel? _panel;
    private static Capture? _current;
    private static string? _requested;
    private static string? _language = "";
    private static string? _configProblem;
    private static string? _failScreen;
    private static int _failCount;
    private static double _nextPoll;
    private static bool _offline, _hotkeyDown, _disabled;
    private static bool _visible = true;
    private static Key _hotkey = Key.F8;

    private static bool Zh => GameText.IsChinese;

    public static void Start(AdvisorConfig config, string configPath, string userDir)
    {
        _advisor = new SidecarAdvisor(config.Port);
        _trace = new AdvisorTraceLog(System.IO.Path.Combine(userDir, "advisor_trace.jsonl"));
        if (!Enum.TryParse(config.Hotkey, ignoreCase: true, out _hotkey))
            _hotkey = Key.F8;
        _loc = string.IsNullOrWhiteSpace(config.RepoPath) ? null : new LocLookup(config.RepoPath);
        if (_loc == null || !_loc.IsLoaded)
        {
            _configProblem = $"repo_path ({configPath})";
            ModLog.Error($"no localization_eng/ under repo_path '{config.RepoPath}'; set repo_path in {configPath}");
            return;
        }
        var advisor = _advisor;
        _ = Task.Run(() => SidecarProcess.EnsureStartedAsync(config, advisor));
    }

    public static void Tick()
    {
        if (_disabled) return;
        try
        {
            TickInner();
        }
        catch (Exception ex) when (ex is TypeLoadException or MissingMemberException)
        {
            _disabled = true;
            ModLog.Error($"game API mismatch, advisor disabled: {ex}");
            Show(PanelText.Status(null, StatusKind.Unsupported, null, Zh));
        }
        catch (Exception ex)
        {
            ModLog.Error($"tick failed: {ex}");
        }
    }

    private static void TickInner()
    {
        double now = Clock.Elapsed.TotalSeconds;
        PollHotkey();
        DrainResults(now);
        if (now < _nextPoll) return;
        _nextPoll = now + PollSeconds;

        if (_panel == null || !_panel.Alive)
        {
            _panel = AdvisorPanel.TryCreate();
            if (_panel == null) return;
        }
        if (!_visible)
        {
            _panel.Hide();
            return;
        }
        var language = GameText.Language;
        if (language != _language)
        {
            _language = language;
            _requested = null;  // re-render labels in the new language
        }
        if (_configProblem != null)
        {
            Show(PanelText.Status(null, StatusKind.ConfigMissing, _configProblem, Zh));
            return;
        }

        Capture? cap;
        try
        {
            cap = DecisionCapture.Detect(_loc!);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed("detect", ex);
            return;
        }
        _current = cap;
        if (cap == null)
        {
            _requested = null;
            _panel.Hide();
            return;
        }
        if (cap.Kind != CaptureKind.Advise)
        {
            _requested = null;
            Show(PanelText.Status(cap.Header, cap.Kind == CaptureKind.Combat ? StatusKind.Combat : StatusKind.NoAdvice, null, Zh));
            return;
        }
        if (cap.Screen == _failScreen && _failCount >= MaxScreenFailures) return;
        if (cap.Fingerprint == _requested) return;
        if (_offline && !Reconnect.Ready(now))
        {
            Show(PanelText.Status(cap.Header, StatusKind.Offline, null, Zh));
            return;
        }

        string json;
        try
        {
            json = JsonSerializer.Serialize(cap.Build!(), JsonOpts);
        }
        catch (Exception ex) when (ex is not (TypeLoadException or MissingMemberException))
        {
            ReadFailed(cap.Screen, ex);
            return;
        }
        if (cap.Screen == _failScreen)
        {
            _failScreen = null;
            _failCount = 0;
        }
        _requested = cap.Fingerprint;
        Show(PanelText.Status(cap.Header, StatusKind.Thinking, null, Zh));
        string fingerprint = cap.Fingerprint, decision = cap.Header.Decision;
        var advisor = _advisor!;
        var trace = _trace;
        _ = Task.Run(async () =>
        {
            try
            {
                var reply = await advisor.AdviseAsync(json, CancellationToken.None).ConfigureAwait(false);
                trace?.Append(decision, json, reply.Raw, null);
                Results.Enqueue((fingerprint, reply, null));
            }
            catch (Exception ex)
            {
                trace?.Append(decision, json, null, ex.GetType().Name);
                Results.Enqueue((fingerprint, null, ex.Message));
            }
        });
    }

    private static void DrainResults(double now)
    {
        while (Results.TryDequeue(out var result))
        {
            if (_current == null || result.Fingerprint != _current.Fingerprint)
                continue;  // the screen moved on; the next poll asks again
            if (result.Reply == null)
            {
                _offline = true;
                Reconnect.Failed(now);
                _requested = null;
                ModLog.Info($"advice service unreachable: {result.Error}");
                Show(PanelText.Status(_current.Header, StatusKind.Offline, null, Zh));
                continue;
            }
            _offline = false;
            Reconnect.Succeeded();
            Show(PanelText.Render(_current.Header, result.Reply, _current.Labels, Zh));
        }
    }

    private static void ReadFailed(string screen, Exception ex)
    {
        if (screen == _failScreen) _failCount++;
        else
        {
            _failScreen = screen;
            _failCount = 1;
        }
        ModLog.Error($"reading {screen} failed ({_failCount}/{MaxScreenFailures}): {ex}");
        Show(PanelText.Status(null, StatusKind.ReadFailed, screen, Zh));
    }

    private static void PollHotkey()
    {
        bool down = Input.IsKeyPressed(_hotkey);
        if (down && !_hotkeyDown)
        {
            _visible = !_visible;
            _requested = null;
            if (!_visible) _panel?.Hide();
        }
        _hotkeyDown = down;
    }

    private static void Show(IReadOnlyList<PanelLine> lines) => _panel?.Show(lines, _language);
}
```

- [ ] **Step 4: 编译并安装**

```bash
~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -3
~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests/Sts2Advisor.Core.Tests.csproj 2>&1 | tail -2
tools/install_advisor_mod.sh
```

Expected：`0 Error(s)`；Core 测试全部通过；安装脚本打印 `installed to ...`

- [ ] **Step 5: 提交**

```bash
git add src/Sts2Advisor/AdvisorPanel.cs src/Sts2Advisor/SidecarProcess.cs src/Sts2Advisor/AdvisorRuntime.cs
git commit -m "feat(advisor-mod): overlay panel, sidecar autostart, poll loop

0.3 s poll on the main thread, advice off it, stale replies dropped by
fingerprint; offline backoff 1..30 s; 3 read failures park a screen; a
game API mismatch disables the mod instead of throwing into the game.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task C8: 实机验收和字段契约测试（需要用户打一局）

**Files:**
- Create: `tests/test_advisor_trace_contract.py`
- Create: `tests/fixtures/advisor/real_game_trace.jsonl`（实机采集，Step 4 生成）

- [ ] **Step 1: 写契约测试（先用合成数据测检查函数本身）**

```python
# tests/test_advisor_trace_contract.py
"""Real-game decision JSON (the mod's advisor_trace.jsonl, saved as a fixture after
the first in-game session) has the fields and JSON types the headless engine
produces for the same decision type. greedy silently mis-scores a field that is
missing or has another type, so this is the check the mod's serializer needs."""
import json
import os

import pytest

from tests.agent.advisor_corpus import corpus_rows

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "advisor", "real_game_trace.jsonl")

# (decision, top-level key) the real game legitimately lacks or leaves null.
ALLOWED_MISSING = {
    ("card_reward", "gold_earned"),  # DecisionCapture passes null: no pre-combat gold snapshot
    ("card_reward", "from_event"),   # headless-only marker for event card rewards
}


def _type(v):
    return {dict: "object", list: "array", str: "string", bool: "bool",
            type(None): "null"}.get(type(v), "number")


def paths(obj) -> dict:
    """{key path: set of JSON types}; list items share the path segment `[]`."""
    out = {}

    def walk(v, p):
        out.setdefault(p, set()).add(_type(v))
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{p}.{k}" if p else k)
        elif isinstance(v, list):
            for x in v:
                walk(x, f"{p}[]")
    walk(obj, "")
    return out


def contract_problems(headless_states: list, game_states: list, decision: str) -> list:
    """Paths every headless sample has but a game sample lacks, and types the game
    uses that headless never does (null is always allowed)."""
    required = None
    seen_types = {}
    for s in headless_states:
        ps = paths(s)
        required = set(ps) if required is None else required & set(ps)
        for p, t in ps.items():
            seen_types.setdefault(p, set()).update(t)
    problems = []
    for g in game_states:
        gp = paths(g)
        for p in sorted(required or ()):
            top = p.split(".")[0].split("[")[0]
            if p not in gp and (decision, top) not in ALLOWED_MISSING:
                problems.append(f"{decision}: missing {p}")
        for p, types in gp.items():
            if p in seen_types:
                extra = types - seen_types[p] - {"null"}
                if extra:
                    problems.append(f"{decision}: {p} is {sorted(extra)}, headless uses {sorted(seen_types[p])}")
    return sorted(set(problems))


def test_checker_flags_missing_and_retyped_fields():
    headless = [{"decision": "x", "player": {"hp": 5, "deck": [{"id": "A"}]}, "cards": []},
                {"decision": "x", "player": {"hp": 7, "deck": [{"id": "B"}]}, "cards": [{"n": 1}]}]
    assert contract_problems(headless, [headless[0]], "x") == []
    game = [{"decision": "x", "player": {"hp": "5", "deck": []}, "cards": []}]
    assert contract_problems(headless, game, "x") == [
        "x: missing player.deck[]", "x: missing player.deck[].id",
        "x: player.hp is ['string'], headless uses ['number']"]
    assert contract_problems([{"gold_earned": 3}], [{}], "card_reward") == []


@pytest.mark.skipif(not os.path.exists(FIXTURE),
                    reason="record tests/fixtures/advisor/real_game_trace.jsonl in-game first (Task C8)")
def test_real_game_json_matches_headless_shapes():
    with open(FIXTURE) as f:
        game = [json.loads(line)["state"] for line in f if line.strip()]
    by_decision = {}
    for s in game:
        by_decision.setdefault(s["decision"], []).append(s)
    headless = {}
    for row in corpus_rows():
        headless.setdefault(row["state"]["decision"], []).append(row["state"])
    problems = []
    for decision, states in sorted(by_decision.items()):
        assert decision in headless, f"real game produced {decision!r}, which headless never does"
        problems += contract_problems(headless[decision], states, decision)
    assert not problems, "\n".join(problems)
```

Run: `.venv/bin/python -m pytest tests/test_advisor_trace_contract.py -q`
Expected：`1 passed, 1 skipped`

- [ ] **Step 2: 请用户实机打一局（a1，任选角色）**

开始前：
```bash
tools/install_advisor_mod.sh
rm -f "$HOME/Library/Application Support/SlayTheSpire2/Sts2Advisor/advisor_trace.jsonl"
```

请用户启动游戏、开一局，尽量走到以下每一种界面，并记下每一项是否满足：
- [ ] Neow 和普通事件：面板显示建议、各选项分数和命中的关键词；
- [ ] 地图：建议的节点和各节点优先级，最后一行写着"未经 A/B 验证"；
- [ ] 战斗中：面板只有"战斗中，交给 CombatSolver"；
- [ ] 卡牌奖励：建议卡（或跳过）、分数、分项、跳过阈值；
- [ ] 休息处：休息或锻造，规则链里决定动作的那条高亮；锻造之后的升级选牌界面也有建议；
- [ ] 商店：每买一样东西，建议都会刷新；
- [ ] 奖励总览、宝箱这类界面：显示"此处暂无建议"；
- [ ] 按 `F8` 能隐藏和显示面板；
- [ ] 中文界面下面板里没有方块乱码；
- [ ] 中途在终端里杀掉 sidecar（`pkill -f advisor_server.py`），面板显示"建议服务离线"。mod 不会再自动拉起它（每次启动游戏只拉起一次）；用户手动运行 `.venv/bin/python agent/advisor_server.py --port 8767` 后，面板应在 30 秒内恢复；
- [ ] 整局游戏没有崩溃，也没有卡顿。

- [ ] **Step 3: 检查日志**

```bash
U="$HOME/Library/Application Support/SlayTheSpire2/Sts2Advisor"
grep -c . "$U/advisor_trace.jsonl"
grep -n "ERROR" "$U/Sts2Advisor.log" | head -20
.venv/bin/python - "$U/advisor_trace.jsonl" <<'EOF'
import collections, json, sys
rows = [json.loads(l) for l in open(sys.argv[1])]
print(collections.Counter(r["decision"] for r in rows))
print("errors:", collections.Counter(r.get("error") for r in rows if r.get("error")))
print("no advice:", sum(1 for r in rows if r["reply"] and r["reply"]["action"] is None))
EOF
```

Expected：
- 决策类型至少包括 `event_choice`、`map_select`、`card_reward`、`rest_site`；
- `errors` 为空，或只有用户手动杀 sidecar 那段时间的 `HttpRequestException`；
- `no advice` 为 0；
- `Sts2Advisor.log` 里的 ERROR 都能解释（比如 sidecar 被杀）。不能解释的 ERROR，回到 C6/C7 修。

- [ ] **Step 4: 存为测试夹具并跑契约测试**

```bash
cp "$HOME/Library/Application Support/SlayTheSpire2/Sts2Advisor/advisor_trace.jsonl" tests/fixtures/advisor/real_game_trace.jsonl
.venv/bin/python -m pytest tests/test_advisor_trace_contract.py -q
```

Expected：`2 passed`

如果有 `missing ...` 或类型不符：
- 先判断是 mod 的序列化读错了数据（修 `DecisionCapture`），还是实机本来就不同（比如字段确实不存在）。
- 后一种情况把 `(decision, key)` 加进 `ALLOWED_MISSING`，在注释里写明原因，并在报告里列出来。
- **greedy 会读的字段**（`id`、`name`、`stats`、`cost`、`type`、`rarity`、`keywords`、`player.hp/max_hp/gold/deck/potions`、`context.act/floor/room_type`、事件的 `title/description/is_locked`、商店的 `is_stocked/cost`、`card_removal_cost`）不允许放进 `ALLOWED_MISSING`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_advisor_trace_contract.py tests/fixtures/advisor/real_game_trace.jsonl
git commit -m "test(advisor-mod): real-game decision JSON matches headless field shapes

Fixture recorded in one in-game a1 run with the mod (advisor_trace.jsonl).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

# Part D — 文档

### Task D1: CLAUDE.md、spec 修正

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md`

- [ ] **Step 1: CLAUDE.md 加一节**（放在 "## Key Files" 之前）

```markdown
## Sts2Advisor (in-game advice mod)

A real-game mod (`src/Sts2Advisor/`) that shows out-of-combat advice plus its reasons in a bottom-right panel; combat is left to the CombatSolver Workshop mod. Spec `docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md`, plan `docs/superpowers/plans/2026-10-21-sts2-advisor-mod.md`.

- **Install / update**: `tools/install_advisor_mod.sh` (builds against the real Steam `sts2.dll`/`GodotSharp.dll`, copies into `SlayTheSpire2.app/Contents/MacOS/mods/Sts2Advisor/`, writes `~/Library/Application Support/SlayTheSpire2/Sts2Advisor/config.json` once). Re-run after a Steam update or file verification. Log: `.../Sts2Advisor/Sts2Advisor.log`; every decision sent and reply received: `.../Sts2Advisor/advisor_trace.jsonl`.
- **Advice** comes from `agent/advisor_server.py` (127.0.0.1:8767, started by the mod): `greedy_action(state, trace=GreedyTrace())` — the same policy and defaults as `play_full_run` — plus the trace of that same call. `agent/greedy_trace.py` `TRACE_KEYS` and `src/Sts2Advisor.Core/TraceText.cs` must list the same keys (`tests/test_advisor_trace_text_keys.py`). A greedy change must keep `tests/agent/test_greedy_golden.py` passing; an intended behaviour change regenerates `tests/fixtures/advisor/ooc_corpus.jsonl.gz` with `tools/build_advisor_corpus.py` and says why in the commit.
- **Decision JSON is shared**: `src/Sts2Shared/` (`LocLookup`, `DecisionSerializer`) is compiled into both Sts2Headless and the mod — no Godot references there. A change to it must replay byte-identical (`tools/replay_states.py ... --out DIR`, then `--compare` against a pre-change dump) and pass the regression.
- `HpAwareMapStrategy` reads acts from `state.act` since 2026-10-21 (it used to treat act-local `floor >= 16` as act 2). Map advice stays labelled unverified: it has never been A/B'd against the random route.
```

- [ ] **Step 2: 修正 spec**（文档写好之后发现的几处出入）

在 `docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md` 里：
1. 第 1 段第 4 条，回复格式里的 `label: {en, zh}` 删掉。在下面加一句：决策 JSON 只有英文（`LocLookup.Lang` 默认 `en`），选项名由 mod 从游戏对象取当前语言的官方译名（`GameText`），按 `kind:index` 对应。
2. 第 1 段第 4 条 `options[].status`：加上 `unstocked`（商店已售出）。
3. 第 1 段第 5 条里"`pick_best_card` 改成先构建一张表再按行求和"改成：`pick_best_card` 不改算式，在 `totals` 算好之后记录已经算好的 9 个列表，顺序与求和顺序相同。
4. 第 1 段第 7 条（生命周期）和第 3 段：
   - 配置文件改为 `<游戏用户目录>/Sts2Advisor/config.json`，日志改为同目录下的 `Sts2Advisor.log`；macOS 上游戏用户目录是 `~/Library/Application Support/SlayTheSpire2/`。
   - 原因：创意工坊下载的 mod 目录是只读的。
5. 第 2 段第 1 条：识别界面改为用 `ActiveScreenContext.Instance.GetCurrentScreen()`（游戏自己的当前界面解析器）。第 6 条：不需要 Harmony postfix，私有字段靠 Publicizer 直接读，所以 mod 里没有任何 Harmony 补丁。
6. 第 3 段"错误处理"：遇到游戏 API 不匹配（`TypeLoadException`、`MissingMemberException`）时停用 mod，并在面板显示"游戏版本未适配"。
7. 第 1 段第 3 条：不需要单独的 `ILocSource` 接口。headless 和 mod 共用同一个 `Sts2Shared.LocLookup` 类；mod 用 `new LocLookup(repo_path)` 读取同一份 `localization_*` 目录。

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md
git commit -m "docs: Sts2Advisor in CLAUDE.md; spec follows what implementation found

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## 执行顺序与并行

- Part A（A1→A11）和 Part B（B1→B6）互不依赖，可以由两个子代理并行执行。B6 的完整回归要跑很久，期间不要再起别的 batch。
- Part C 依赖 A11（服务）和 B5（共享序列化）：C1–C4 只依赖 Part A 的 key 表（C3 的 Python 测试要用），可以在 B 进行时先做；C5 起需要 B5 完成。
- C5 Step 7 和 C8 需要用户在游戏里操作，协调者到这两步时要停下来请用户配合，不能由子代理代做。
- D1 最后做。
