# Phase 3a — 局外决策策略移植 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 solver 评测 harness（`python/play_full_run.py`）的局外决策（选牌/篝火/事件/商店/bundle/局外 card_select）可以改由 `agent.combat_env.greedy_action` 驱动，并在 a1 上用配对 A/B 量出它的效果。

**Architecture:** 新增环境变量 `STS2_OOC_POLICY`（`naive`|`greedy`，默认 `naive`，A/B 通过后再翻成 `greedy`）。`greedy` 时，在 `play_run` 的决策分发链里、`combat_play` 分支之后插入一个分支，把上述 6 种决策交给 `greedy_action`；被引擎拒绝或 `greedy_action` 抛异常时回落到固定的兜底命令并计数。同时加 `--ascension`、`--seed-prefix`、`--keep-game-logs`（把每局的完整状态日志拷出来，躲开 `game_log.cleanup_old_logs()` 的 7 天清理——Phase 2 的对局日志已经被它删光了），结果行带上 `ascension`/`ooc_policy`/计数/`game_log` 路径，供 Phase 3b 离线拟合路线模型。

**Tech Stack:** Python 3.11（`.venv/bin/python`），pytest，C# 引擎不改。

**Spec:** `docs/superpowers/specs/2026-09-30-phase3-outofcombat-and-route-design.md`（Phase 3a 一节）。

---

## 背景（执行者必读）

- `python/play_full_run.py` 是 solver 评测 harness：战斗走 `plan_combat_turn`（ported Combat Solver），其余决策目前全是占位：`map_select` 用 `random.Random(seed)` 随机选；`card_reward` 永远拿第 0 张；`rest_site` 永远 HEAL；`shop` 直接 `leave_room`；`bundle_select` 选 0；`card_select` 选第 0 张；`event_choice` 选第一个未锁定选项（连续 3 次同页则 `leave_room`）。
- `agent/combat_env.py:448` 的 `greedy_action(state) -> dict` 是 RL harness 里调好的局外策略（`card_scoring` 选牌、`agent/strategy.py` 的 `rest_site_action`、商店买卖/删牌、事件打分）。它是纯函数、无随机性（`STS2_RANDOMIZE`/`STS2_DECISION_ADVISOR` 默认关），输入就是引擎的决策 JSON，和 `play_full_run` 收到的是同一种格式。**本计划不修改 `greedy_action` 本身。**
- `map_select` **不**交给 greedy（它内部会走 `HpAwareMapStrategy`），路线在 3a 保持随机，这样 A/B 只量局外策略这一个变量。`combat_play` 不变。
- import `agent.combat_env` 约 3 秒（gymnasium/numpy/卡牌数据），所以只在 `greedy` 时惰性导入。
- 引擎的 `start_run` 接受 `"ascension"`（`src/Sts2Headless/Program.cs:147`，缺省 0），所以显式发 `"ascension": 0` 与不发等价。
- 测试不需要游戏 DLL：`play_full_run.EngineProcess` 是 harness 与引擎之间的唯一接缝，测试用脚本化假引擎替换它（现成的 `_FakeEngine` 在 `tests/test_map_route_determinism.py`，按顺序回放预置回复、记录发出的命令）。
- 始终用 `.venv/bin/python`；临时脚本放 `/tmp/sts2-cli/`；长跑输出放 `~/.sts2-train/`（`/tmp` 会被清）；长跑用 `scripts/run_caffeinated.sh` 包住（Mac 空闲 1 分钟会睡眠）。
- 提交信息结尾加：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- 工作区里 `src/Sts2Headless/CombatSolverEngine/Engine/InCombat/Simulation/CombatPredictionSimulator.CardPile.cs` 有一处**未提交的 BUG-040 补丁**，由协调者另行提交。**不要 `git add -A`，不要 stash/checkout/还原它**——每次提交只 `git add` 本任务列出的文件。

## 文件结构

- Modify `python/play_full_run.py`：OOC 策略常量与 3 个纯函数（Task 1）；`play_run` 拆成包装层 + `_play_run`，加 ascension/日志保留/结果字段（Task 2）；分发分支（Task 3）；`summarize` 汇总行（Task 4）；`main` 新参数（Task 2）。
- Create `tests/test_ooc_policy.py`：本计划全部单测。
- Modify `CLAUDE.md`、`agent/bug.md`（仅 Task 8，视结果而定）。

---

### Task 1: OOC 策略解析与兜底命令（纯函数）

**Files:**
- Modify: `python/play_full_run.py`（在 `solver_call_timeout_s` 定义之后、`_find_dotnet` 之前插入；顶部 import 加 `shutil`）
- Test: `tests/test_ooc_policy.py`（新建）

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_ooc_policy.py`：

```python
"""Phase 3a: STS2_OOC_POLICY -- out-of-combat decisions via agent.combat_env.greedy_action.

No game DLLs: play_full_run.EngineProcess is replaced by the scripted
_FakeEngine from test_map_route_determinism, and the greedy policy itself is
replaced through the play_full_run._load_greedy_action seam, so these tests pin
the harness's dispatch/fallback logic, not card_scoring's judgement.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run
from tests.test_map_route_determinism import _FakeEngine


def test_ooc_policy_defaults_to_naive():
    assert play_full_run.ooc_policy({}) == "naive"


def test_ooc_policy_blank_means_default():
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": "  "}) == "naive"


@pytest.mark.parametrize("raw,expected", [("greedy", "greedy"), ("NAIVE", "naive"), (" Greedy ", "greedy")])
def test_ooc_policy_accepts_known_values_case_insensitively(raw, expected):
    assert play_full_run.ooc_policy({"STS2_OOC_POLICY": raw}) == expected


def test_ooc_policy_rejects_unknown_values_rather_than_silently_defaulting():
    with pytest.raises(ValueError, match="STS2_OOC_POLICY"):
        play_full_run.ooc_policy({"STS2_OOC_POLICY": "smart"})


def test_greedy_decisions_exclude_map_and_combat():
    assert "map_select" not in play_full_run.OOC_GREEDY_DECISIONS
    assert "combat_play" not in play_full_run.OOC_GREEDY_DECISIONS
    assert play_full_run.OOC_GREEDY_DECISIONS == {
        "card_reward", "rest_site", "event_choice", "bundle_select", "card_select", "shop"}


@pytest.mark.parametrize("state,expected", [
    ({"decision": "card_reward"}, {"cmd": "action", "action": "skip_card_reward"}),
    ({"decision": "bundle_select"}, {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}),
    ({"decision": "card_select", "cards": [{"index": 0}]},
     {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}),
    ({"decision": "card_select", "cards": []}, {"cmd": "action", "action": "skip_select"}),
    ({"decision": "rest_site"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "event_choice"}, {"cmd": "action", "action": "leave_room"}),
    ({"decision": "shop"}, {"cmd": "action", "action": "leave_room"}),
])
def test_fallback_command_per_decision(state, expected):
    assert play_full_run.ooc_fallback_command(state) == expected


def test_load_greedy_action_returns_the_real_combat_env_function():
    fn = play_full_run._load_greedy_action()
    assert callable(fn) and fn.__name__ == "greedy_action"
    assert fn.__module__ == "agent.combat_env"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py -q`
Expected: FAIL，`AttributeError: module 'play_full_run' has no attribute 'ooc_policy'`（及同类）。

- [ ] **Step 3: 最小实现**

`python/play_full_run.py` 顶部 import 区把 `import random` 那组改为（加 `shutil`）：

```python
import argparse
import json
import subprocess
import sys
import random
import os
import shutil
```

在 `def solver_call_timeout_s(...)` 函数体结束之后、`def _find_dotnet():` 之前插入：

```python
# Out-of-combat decision policy (Phase 3a,
# docs/superpowers/specs/2026-09-30-phase3-outofcombat-and-route-design.md).
# "naive" is the original placeholder policy (card 0, always HEAL, never shop,
# first unlocked event option); "greedy" hands those decisions to
# agent.combat_env.greedy_action, the tuned policy the RL harness uses.
# map_select (seeded random route) and combat_play (solver) are never
# delegated, so an A/B between the two isolates the out-of-combat policy.
OOC_POLICIES = ("naive", "greedy")
OOC_POLICY_DEFAULT = "naive"
OOC_GREEDY_DECISIONS = frozenset({"card_reward", "rest_site", "event_choice",
                                  "bundle_select", "card_select", "shop"})
# A greedy shop visit normally ends in a few buys and a leave_room; this cap
# only exists so a buy the engine accepts without changing anything cannot
# spin until the global STUCK detector kills the run.
SHOP_ACTION_CAP = 20


def ooc_policy(env=None) -> str:
    """Resolve STS2_OOC_POLICY ("naive" | "greedy"; blank/unset = default).

    An unknown value raises instead of silently running the default, for the
    same reason solver_characters() does: an A/B arm that quietly measured the
    wrong policy still prints "Completed: N/N".
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_OOC_POLICY") or "").strip().lower()
    if not raw:
        return OOC_POLICY_DEFAULT
    if raw not in OOC_POLICIES:
        raise ValueError(f"STS2_OOC_POLICY={raw!r} is not one of {OOC_POLICIES}")
    return raw


def _load_greedy_action():
    """Import agent.combat_env.greedy_action lazily -- the import pulls in
    gymnasium/numpy and the card data (~3 s), which the naive policy never needs.
    Also the seam tests replace to script the policy's answers."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from agent.combat_env import greedy_action
    return greedy_action


def ooc_fallback_command(state: dict) -> dict:
    """What to send when a greedy command is refused, or greedy_action raises.

    Chosen to always make progress: skip a card reward, take the first bundle,
    select the first card of a mandatory card_select, and otherwise leave the
    room (the same escape the naive branches use after an engine error).
    """
    decision = state.get("decision", "")
    if decision == "card_reward":
        return {"cmd": "action", "action": "skip_card_reward"}
    if decision == "bundle_select":
        return {"cmd": "action", "action": "select_bundle", "args": {"bundle_index": 0}}
    if decision == "card_select":
        if state.get("cards"):
            return {"cmd": "action", "action": "select_cards", "args": {"indices": "0"}}
        return {"cmd": "action", "action": "skip_select"}
    return {"cmd": "action", "action": "leave_room"}
```

（`shutil` 在 Task 2 使用；本任务先导入。）

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py -q`
Expected: 全部 PASS（`test_load_greedy_action_returns_the_real_combat_env_function` 会真实导入 `agent.combat_env`，约 3 秒）。

- [ ] **Step 5: 提交**

```bash
git add python/play_full_run.py tests/test_ooc_policy.py
git commit -m "feat(play_full_run): STS2_OOC_POLICY resolver and greedy fallback commands

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `--ascension` / `--seed-prefix` / `--keep-game-logs` 与结果字段

**Files:**
- Modify: `python/play_full_run.py`（`play_run` 定义处 `:151` 起；`start_run` 发送处约 `:231`；`logger = GameLogger(...)` 约 `:175`；`finally:` 块约 `:583`；`result_to_eval_row`；`main`）
- Test: `tests/test_ooc_policy.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_ooc_policy.py`：

```python
def _game_over(act=1, floor=3):
    return {"type": "decision", "decision": "game_over", "victory": False,
            "act": act, "floor": floor,
            "player": {"hp": 0, "max_hp": 80, "gold": 0, "deck_size": 10}}


def _run(monkeypatch, script, seed="s1", **kwargs):
    """play_run() against a scripted engine; returns (result, sent commands)."""
    created = []

    def _fake(argv, **kw):
        created.append(_FakeEngine(script))
        return created[-1]

    monkeypatch.setattr(play_full_run, "EngineProcess", _fake)
    kwargs.setdefault("verbose", False)
    kwargs.setdefault("log", False)
    result = play_full_run.play_run(seed, character="Ironclad", **kwargs)
    return result, [json.loads(line) for line in created[0].sent]


def test_start_run_carries_the_requested_ascension(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()], ascension=1)
    start = sent[0]
    assert start["cmd"] == "start_run" and start["ascension"] == 1
    assert result["ascension"] == 1


def test_ascension_defaults_to_zero(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, sent = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert sent[0]["ascension"] == 0 and result["ascension"] == 0


def test_result_carries_policy_and_zero_counters_under_naive(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()])
    assert result["ooc_policy"] == "naive"
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 0
    assert result["game_log"] is None  # log=False writes no game log


def test_keep_game_logs_copies_the_log_and_records_the_kept_path(monkeypatch, tmp_path):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)
    # game_log.LOG_DIR is read at call time by GameLogger and cleanup_old_logs,
    # so pointing it at tmp_path keeps the test out of the real logs/.
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr("game_log.LOG_DIR", str(logs_dir))
    keep = tmp_path / "kept"
    result, _ = _run(monkeypatch, [{"type": "ready"}, _game_over()], seed="p3a_7",
                     ascension=1, log=True, keep_log_dir=str(keep))
    kept = keep / "Ironclad_naive_a1_p3a_7.jsonl"
    assert result["game_log"] == str(kept)
    lines = [json.loads(l) for l in kept.read_text().splitlines()]
    meta = lines[0]
    assert meta["type"] == "run_meta"
    assert meta["seed"] == "p3a_7" and meta["character"] == "Ironclad"
    assert meta["ascension"] == 1 and meta["experiment"] == "ooc=naive"
    assert any(l.get("type") == "state" for l in lines[1:])


def test_eval_row_carries_phase3a_fields():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "p3a_1", "act": 1, "floor": 9, "steps": 50,
         "solver_plans": 3, "solver_errors": 0, "ascension": 1,
         "ooc_policy": "greedy", "ooc_greedy": 12, "ooc_fallbacks": 1,
         "game_log": "/x/Ironclad_p3a_1.jsonl"}, "Ironclad")
    assert row["ascension"] == 1 and row["ooc_policy"] == "greedy"
    assert row["ooc_greedy"] == 12 and row["ooc_fallbacks"] == 1
    assert row["game_log"] == "/x/Ironclad_p3a_1.jsonl"
    assert row["status"] == "dead" and row["floor"] == 9
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py -q`
Expected: 新增 5 个测试 FAIL（`play_run() got an unexpected keyword argument 'ascension'`、`KeyError: 'ascension'` 等）；Task 1 的测试仍 PASS。

- [ ] **Step 3: 实现**

(a) 把 `def play_run(seed: str, character: str = "Ironclad", verbose: bool = True, log: bool = True):` 这一行及其 docstring 行 `"""Play a complete run and return the result."""` 替换为包装层 + 改名后的原函数头：

```python
def play_run(seed: str, character: str = "Ironclad", verbose: bool = True, log: bool = True,
             ascension: int = 0, keep_log_dir: str | None = None):
    """Play a complete run and return the result.

    Thin wrapper over _play_run() that stamps the fields every result needs
    regardless of which of _play_run's many return paths produced it:
    ascension, the out-of-combat policy and its counters, and the path of the
    game's full state log. With keep_log_dir, that log is copied out of
    logs/ first -- game_log.cleanup_old_logs() deletes anything older than
    7 days, which already erased every Phase 2 game, and Phase 3b fits its
    route model from these logs.
    """
    stats = {"ooc_policy": ooc_policy(), "ooc_greedy": 0, "ooc_fallbacks": 0,
             "game_log": None}
    result = _play_run(seed, character, verbose, log, ascension, stats)
    game_log = stats.pop("game_log")
    if keep_log_dir and game_log and os.path.exists(game_log):
        os.makedirs(keep_log_dir, exist_ok=True)
        kept = os.path.join(keep_log_dir,
                            f"{character}_{ooc_policy()}_a{ascension}_{str(seed).replace('/', '_')}.jsonl")
        shutil.copy2(game_log, kept)
        game_log = kept
    result.update(stats)
    result["ascension"] = ascension
    result["game_log"] = game_log
    return result


def _play_run(seed: str, character: str, verbose: bool, log: bool, ascension: int,
              stats: dict):
    """The run loop. `stats` is play_run()'s per-run dict: this function bumps
    stats["ooc_greedy"]/["ooc_fallbacks"] and sets stats["game_log"]."""
```

（原函数体保持原样紧接在后面。）

(b) 把 `logger = GameLogger(character, seed, enabled=log)` 改为：

```python
    logger = GameLogger(character, seed, enabled=log,
                        run_context={"seed": seed, "character": character,
                                     "ascension": ascension,
                                     "experiment": f"ooc={stats['ooc_policy']}"})
```

(c) 把 `state = send({"cmd": "start_run", "character": character, "seed": seed})` 改为：

```python
        state = send({"cmd": "start_run", "character": character, "seed": seed,
                      "ascension": ascension})
```

(d) 在 `finally:` 块里，`logger.close()` 之后加一行：

```python
        stats["game_log"] = logger.path
```

(e) `result_to_eval_row` 的返回 dict 末尾（`"solver_budget_s": solver_budget_seconds(),` 之后）加：

```python
        "ascension": result.get("ascension"),
        "ooc_policy": result.get("ooc_policy"),
        "ooc_greedy": result.get("ooc_greedy"),
        "ooc_fallbacks": result.get("ooc_fallbacks"),
        "game_log": result.get("game_log"),
```

(f) `main()`：在 `--results-log` 的 `add_argument` 之后加：

```python
    parser.add_argument("--ascension", type=int, default=0,
                        help="Ascension level sent with start_run (default 0).")
    parser.add_argument("--seed-prefix", default="run_",
                        help="Seeds are <prefix><i> for i = 1..num_runs (default run_). "
                             "Use a fresh prefix per experiment so a verdict never "
                             "runs on seeds an earlier phase tuned on.")
    parser.add_argument("--keep-game-logs", default=None, metavar="DIR",
                        help="Copy each game's full state log to "
                             "DIR/<character>_<policy>_a<ascension>_<seed>.jsonl "
                             "(logs/ is purged after 7 days) and record that path in the "
                             "results row.")
```

在 `if args.num_runs <= 0:` 检查之后加：

```python
    if args.ascension < 0:
        parser.error(f"--ascension must be >= 0, got {args.ascension}")
    policy = ooc_policy()  # fail on a bad STS2_OOC_POLICY before any game starts
```

把 `print(f"Playing {num_runs} runs as {character}")` 改为：

```python
    print(f"Playing {num_runs} runs as {character} "
          f"(ascension {args.ascension}, ooc policy {policy})")
```

把循环里的 `seed = f"run_{i+1}"` 改为 `seed = f"{args.seed_prefix}{i+1}"`，把 `result = play_run(seed, character, verbose=True)` 改为：

```python
        result = play_run(seed, character, verbose=True, ascension=args.ascension,
                          keep_log_dir=args.keep_game_logs)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py tests/test_map_route_determinism.py tests/test_solver_gate.py tests/test_engine_process.py tests/test_plan_combat_turn_resolution.py -q`
Expected: 全部 PASS（旧的 67 个 + 新增的）。

- [ ] **Step 5: 提交**

```bash
git add python/play_full_run.py tests/test_ooc_policy.py
git commit -m "feat(play_full_run): --ascension, --seed-prefix, --keep-game-logs; stamp policy fields on results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: greedy 分发分支（含兜底、商店上限、事件卡页保护）

**Files:**
- Modify: `python/play_full_run.py`（`_play_run` 内：循环前初始化；在 `elif decision == "event_choice":`（唯一一处，约 `:486`）**之前**插入新分支）
- Test: `tests/test_ooc_policy.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_ooc_policy.py`：

```python
def _decision(kind, **extra):
    d = {"type": "decision", "decision": kind,
         "context": {"act": 1, "floor": 5, "room_type": extra.pop("room_type", "")},
         "player": {"hp": 50, "max_hp": 80, "gold": extra.pop("gold", 100), "deck_size": 10}}
    d.update(extra)
    return d


def _greedy_env(monkeypatch, policy_fn):
    monkeypatch.setenv("STS2_OOC_POLICY", "greedy")
    monkeypatch.setattr(play_full_run, "_load_greedy_action", lambda: policy_fn)


def test_greedy_policy_answers_card_reward(monkeypatch):
    seen = []

    def policy(state):
        seen.append(state["decision"])
        return {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}}

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert seen == ["card_reward"]
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}} in sent
    assert result["ooc_policy"] == "greedy"
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 0


def test_naive_policy_never_loads_greedy(monkeypatch):
    monkeypatch.delenv("STS2_OOC_POLICY", raising=False)

    def boom():
        raise AssertionError("naive policy must not import greedy_action")

    monkeypatch.setattr(play_full_run, "_load_greedy_action", boom)
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert {"cmd": "action", "action": "select_card_reward", "args": {"card_index": 0}} in sent
    assert result["ooc_greedy"] == 0


def test_refused_greedy_command_falls_back_and_is_counted(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "select_card_reward",
                                        "args": {"card_index": 1}})
    script = [{"type": "ready"},
              _decision("card_reward", cards=[{"index": 0}, {"index": 1}]),
              {"type": "error", "message": "nope"},
              _game_over()]
    result, sent = _run(monkeypatch, script)
    actions = [c.get("action") for c in sent]
    assert actions[1:3] == ["select_card_reward", "skip_card_reward"]
    assert result["ooc_greedy"] == 1 and result["ooc_fallbacks"] == 1
    assert "error" not in result  # the fallback recovered; the run ended normally


def test_greedy_exception_falls_back_without_counting_a_greedy_decision(monkeypatch):
    def policy(state):
        raise RuntimeError("scoring blew up")

    _greedy_env(monkeypatch, policy)
    script = [{"type": "ready"}, _decision("rest_site", options=[]), _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1] == {"cmd": "action", "action": "leave_room"}
    assert result["ooc_greedy"] == 0 and result["ooc_fallbacks"] == 1


def test_map_and_combat_are_never_delegated(monkeypatch):
    def policy(state):
        raise AssertionError(f"greedy called for {state['decision']}")

    _greedy_env(monkeypatch, policy)
    monkeypatch.setenv("STS2_SOLVER_CHARS", "none")
    script = [{"type": "ready"},
              _decision("map_select", choices=[{"col": 0, "row": 1}]),
              _game_over()]
    result, sent = _run(monkeypatch, script)
    assert sent[1]["action"] == "select_map_node"
    assert result["ooc_greedy"] == 0


def test_shop_visit_is_capped(monkeypatch):
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    cap = play_full_run.SHOP_ACTION_CAP
    # gold changes every step so the global STUCK detector stays quiet
    shops = [_decision("shop", gold=1000 - i) for i in range(cap + 1)]
    script = [{"type": "ready"}, *shops, _game_over()]
    result, sent = _run(monkeypatch, script)
    actions = [c.get("action") for c in sent[1:]]
    assert actions == ["buy_card"] * cap + ["leave_room"]
    assert result["ooc_greedy"] == cap and result["ooc_fallbacks"] == 1


def test_shop_cap_resets_for_a_new_shop(monkeypatch):
    # Cap of 1: a second action in the SAME visit would be forced to leave_room,
    # so two buys with zero fallbacks proves the counter reset at the new floor.
    monkeypatch.setattr(play_full_run, "SHOP_ACTION_CAP", 1)
    _greedy_env(monkeypatch, lambda s: {"cmd": "action", "action": "buy_card",
                                        "args": {"card_index": 0}})
    first = _decision("shop", gold=100)
    second = _decision("shop", gold=90)
    second["context"] = {"act": 1, "floor": 11, "room_type": ""}
    script = [{"type": "ready"}, first, second, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert [c.get("action") for c in sent[1:]] == ["buy_card", "buy_card"]
    assert result["ooc_greedy"] == 2 and result["ooc_fallbacks"] == 0


def test_event_page_stuck_guard_applies_under_greedy(monkeypatch):
    calls = []

    def policy(state):
        calls.append(1)
        return {"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}

    _greedy_env(monkeypatch, policy)
    page = _decision("event_choice",
                     options=[{"index": 0, "is_locked": False, "was_chosen": True}])
    script = [{"type": "ready"}, page, page, page, page, _game_over()]
    result, sent = _run(monkeypatch, script)
    assert len(calls) == 3
    assert sent[-1] == {"cmd": "action", "action": "leave_room"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py -q`
Expected: 新增的 greedy 测试 FAIL（greedy 从未被调用，走了 naive 分支）；`test_naive_policy_never_loads_greedy` 和 `test_map_and_combat_are_never_delegated` 可能已经 PASS——没关系。

- [ ] **Step 3: 实现**

(a) 在 `_play_run` 里 `refused_turn_key = None` 这一行之后（`while step < max_steps:` 之前）加：

```python
        # Phase 3a: out-of-combat decisions go to agent.combat_env.greedy_action
        # when STS2_OOC_POLICY=greedy (see OOC_GREEDY_DECISIONS). None = naive.
        greedy = _load_greedy_action() if stats["ooc_policy"] == "greedy" else None
        shop_visit = None
        shop_actions = 0
```

(b) 在 `            elif decision == "event_choice":` 这一行**之前**插入（缩进与其它 `elif decision == ...` 分支一致，12 个空格）：

```python
            elif greedy is not None and decision in OOC_GREEDY_DECISIONS:
                context = state.get("context") or {}
                if decision == "shop":
                    visit = (context.get("act"), context.get("floor"))
                    if visit != shop_visit:
                        shop_visit, shop_actions = visit, 0
                    shop_actions += 1
                if decision == "event_choice" and stuck_count >= 3:
                    # Same guard as the naive event branch below: a page that keeps
                    # coming back unchanged means the chosen option is a latched
                    # no-op (was_chosen) -- leave rather than spin to STUCK.
                    print("  event_choice stuck on same page, leaving room")
                    state = send({"cmd": "action", "action": "leave_room"})
                elif decision == "shop" and shop_actions > SHOP_ACTION_CAP:
                    print(f"  !! shop visit exceeded {SHOP_ACTION_CAP} actions, leaving")
                    stats["ooc_fallbacks"] += 1
                    state = send({"cmd": "action", "action": "leave_room"})
                else:
                    try:
                        cmd = greedy(state)
                    except Exception as exc:
                        print(f"  !! greedy_action raised on {decision}: {exc!r} -- using fallback")
                        cmd = None
                    if cmd is None:
                        stats["ooc_fallbacks"] += 1
                        state = send(ooc_fallback_command(state))
                    else:
                        stats["ooc_greedy"] += 1
                        reply = send(cmd)
                        if reply.get("type") == "error":
                            print(f"  !! greedy {cmd.get('action')} refused on {decision}: "
                                  f"{reply.get('message', 'unknown')} -- using fallback")
                            stats["ooc_fallbacks"] += 1
                            reply = send(ooc_fallback_command(state))
                        state = reply
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py tests/test_map_route_determinism.py tests/test_solver_gate.py tests/test_engine_process.py tests/test_plan_combat_turn_resolution.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add python/play_full_run.py tests/test_ooc_policy.py
git commit -m "feat(play_full_run): route out-of-combat decisions through greedy_action under STS2_OOC_POLICY=greedy

Refused commands and greedy_action exceptions fall back to a progress-making
command and are counted; a shop visit is capped at SHOP_ACTION_CAP actions;
the naive event-page stuck guard also applies. map_select and combat_play
are never delegated.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: SUMMARY 汇报 OOC 策略与回落次数

**Files:**
- Modify: `python/play_full_run.py`（`summarize`，在 `Solver engagement` 那一行 append 之后）
- Test: `tests/test_ooc_policy.py`

- [ ] **Step 1: 写失败的测试**

```python
def _r(policy, greedy, fallbacks, **extra):
    r = {"victory": False, "seed": "s", "steps": 10, "act": 1, "floor": 5,
         "solver_plans": 1, "solver_errors": 0,
         "ooc_policy": policy, "ooc_greedy": greedy, "ooc_fallbacks": fallbacks}
    r.update(extra)
    return r


def test_summary_reports_ooc_policy_and_counts():
    text = play_full_run.summarize([_r("greedy", 10, 1), _r("greedy", 7, 0)], 2,
                                   "Ironclad", solver_chars={"Ironclad"})
    assert "OOC policy: greedy -- 17 greedy decisions, 1 fallbacks" in text
    assert "OOC POLICY NEVER ENGAGED" not in text


def test_summary_banner_when_greedy_never_engaged():
    text = play_full_run.summarize([_r("greedy", 0, 3)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "!! OOC POLICY NEVER ENGAGED" in text


def test_summary_naive_prints_policy_without_banner():
    text = play_full_run.summarize([_r("naive", 0, 0)], 1, "Ironclad",
                                   solver_chars={"Ironclad"})
    assert "OOC policy: naive" in text
    assert "NEVER ENGAGED" not in text


def test_summary_tolerates_results_without_ooc_fields():
    text = play_full_run.summarize([{"victory": False, "seed": "s", "act": 1, "floor": 5,
                                     "solver_plans": 1, "solver_errors": 0}], 1,
                                   "Ironclad", solver_chars={"Ironclad"})
    assert "OOC policy" not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py -q -k summary`
Expected: 前三个 FAIL（文本里没有 `OOC policy`），第四个 PASS。

- [ ] **Step 3: 实现**

在 `summarize` 中 `lines.append(f"Solver engagement: ...")` 那条语句之后插入：

```python
    policies = sorted({r.get("ooc_policy") for r in results if r and r.get("ooc_policy")})
    if policies:
        total_greedy = sum(r.get("ooc_greedy") or 0 for r in results if r)
        total_fallbacks = sum(r.get("ooc_fallbacks") or 0 for r in results if r)
        lines.append(f"OOC policy: {'/'.join(policies)} -- {total_greedy} greedy decisions, "
                     f"{total_fallbacks} fallbacks")
        # Same blindness the solver banner guards against: a greedy arm whose
        # policy never answered anything is secretly the naive arm.
        if policies == ["greedy"] and total_greedy == 0:
            lines.append("!! OOC POLICY NEVER ENGAGED -- STS2_OOC_POLICY=greedy but "
                         "greedy_action answered no decision; these results measure "
                         "the fallback commands")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py tests/test_solver_gate.py -q`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add python/play_full_run.py tests/test_ooc_policy.py
git commit -m "feat(play_full_run): SUMMARY reports the OOC policy, greedy/fallback counts, never-engaged banner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

#### Review fixes (2026-09-30)

代码评审（34e1a47..b889568）后追加的一次提交。上面 Task 1-4 的代码块是**原始版本**；与本小节冲突处，以本小节和已提交的代码为准。

- `ooc_greedy` 只数引擎**接受**的 greedy 命令；被拒/抛异常/返回 None 的不计入。
- 回落按原因计数：`ooc_fallback_causes = {raised, refused, none, shop_cap}`，`ooc_fallbacks` = 四项之和；事件页卡死退出（naive 也这样做）单独计入 `ooc_event_stuck`，不算回落。
- 回落命令 = **naive 的选择**：新增 `naive_ooc_command(state)`（逐条对应 `_play_run` 里 naive 分支的首条命令，有 15 组状态的一致性测试，跑真实的 naive 臂来对照）；它的回复若是 error，再发 `ooc_escape_command(state)`（原 `ooc_fallback_command` 改名，行为不变）。
- 可见性：每种决策首次 `greedy_action` 抛异常时打印完整 traceback（之后只打一行）；SUMMARY 行带分类与 event-stuck 数：`OOC policy: greedy -- N greedy decisions, M fallbacks (raised a, refused b, none c, shop_cap d), E event-stuck exits`；`M / (N + M) > OOC_FALLBACK_RATE_WARN`（0.05）时加 `!! OOC FALLBACK RATE x.x% -- the greedy arm is partly running the naive policy` 横幅。
- 结果行额外带 `ooc_fallback_causes`、`ooc_event_stuck`、`ooc_knobs`。
- 日志拷贝失败（`OSError`）不再丢掉整局结果：打印 `!! could not keep game log ...`，`game_log` 保持 `logs/` 原路径，结果里加 `game_log_error`。
- 保留的日志文件名改为 `<character>_<policy>_a<ascension>_<seed>.jsonl`（naive/greedy 两臂、a0/a1 不会互相覆盖）。
- `GameLogger` 用 `"x"` 模式开文件；同名冲突时改用带 pid 后缀的文件名（不再截断/交错另一个进程正在写的日志）。
- `main()` 在 greedy 时先 `_load_greedy_action()`（导入坏了立刻中止，而不是 N 局 `crash`），并对显式设置的 `STS2_RANDOMIZE` / `STS2_DECISION_ADVISOR` / `STS2_CARD_THRESHOLD_LIFT` / `STS2_BASIC_PURGE_ALL` / `STS2_CARD_QUALITY_GATE` 打印 `!! greedy_action knob set: NAME=value`，并把它们记入每局结果的 `ooc_knobs`（naive 下为 `{}`）。
- 补测试：greedy 返回 None、naive 回落也被拒（走 escape）、bundle_select 走 greedy、错误返回路径也带政策字段、商店无变化的重复状态被上限截断（不是 STUCK）、map/combat 测试同时断言 `ooc_fallbacks == 0`。

---

### Task 5: 真引擎冒烟（greedy，a1，每角色 2 局）

**前置：** `pgrep -f "python/play_full_run.py"` 没有输出（协调者的 BUG-040 回归跑完之前**不要**启动，避免抢 CPU 把 solver 测弱）。

- [ ] **Step 1: 跑冒烟**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
OUT=~/.sts2-train/phase3a_smoke_$(date +%Y%m%d_%H%M%S); mkdir -p "$OUT"; echo "$OUT" > ~/.sts2-train/last_phase3a_smoke_dir.txt
for char in Ironclad Silent Defect Regent Necrobinder; do
  STS2_OOC_POLICY=greedy STS2_SOLVER_THREADS=3 scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py 2 "$char" \
      --ascension 1 --seed-prefix p3a_smoke_ --keep-game-logs "$OUT/games" \
      --results-log "$OUT/${char}.jsonl" > "$OUT/${char}.log" 2>&1 &
done
wait
grep -H -E "Wins: |OOC policy|NEVER ENGAGED|OOC FALLBACK RATE|Run [0-9]+: (TIMEOUT|ERROR|HANG)" "$OUT"/*.log
```

Expected（观察项）：每个角色 `Completed: 2/2`；`OOC policy: greedy -- N greedy decisions` 且 N > 0（N 只数引擎**接受**的 greedy 命令）；没有 NEVER ENGAGED，没有 `OOC FALLBACK RATE`（回落率 > 5% 就出这条横幅）。

- [ ] **Step 2: 检查回落与保留日志**

```bash
OUT=$(cat ~/.sts2-train/last_phase3a_smoke_dir.txt)
grep -h -E "!! greedy|!! shop visit|!! naive fallback" "$OUT"/*.log | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -20
grep -h -B1 -A14 "^Traceback" "$OUT"/*.log | head -60   # 每局、每种决策首次 greedy_action 抛异常时的完整 traceback
grep -h "^OOC policy" "$OUT"/*.log      # 回落按 raised/refused/none/shop_cap 分类；event-stuck 退出不算回落
ls "$OUT/games" | wc -l            # 期望 10
head -1 "$OUT/games/"*_p3a_smoke_1.jsonl | head -5   # 期望 run_meta 行，ascension=1, experiment=ooc=greedy
grep -h '"action": "buy_\|"action": "remove_card"\|SMITH' "$OUT"/*.log | wc -l   # 期望 > 0：商店/锻造确实发生了
```

判定：回落是**某一种命令被系统性拒绝**（同一条 `!! greedy <action> refused` 反复出现）或 **`greedy_action` 抛异常**（`raised` > 0：贴出 traceback）→ 这是 greedy 与 solver harness 的协议差异或 greedy 自身的 bug，停下来把日志行贴给协调者，不要自己改 `greedy_action`。零星几次回落可以接受（回落时走的是 **naive 的选择**，不是"随便什么"）。

- [ ] **Step 3: 把冒烟结果写进本文件**

在本 Task 标题下加"冒烟结果（实测）"小节：每角色完成数、greedy 决策数、回落数及其类别、是否见到买/删牌/锻造。然后：

```bash
git add docs/superpowers/plans/2026-09-30-phase3a-ooc-policy-port.md
git commit -m "docs: Phase 3a smoke results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: 全量回归（CLAUDE.md 口径，两种策略各一遍）

- [ ] **Step 1: 两种策略的回归并行跑（各一条 lane，默认 8 线程，合计 16 ≤ 18 核）**

naive 证明默认路径没被改坏；greedy 覆盖新代码。

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
OUT=~/.sts2-train/phase3a_gate_$(date +%Y%m%d_%H%M%S); mkdir -p "$OUT"; echo "$OUT" > ~/.sts2-train/last_phase3a_gate_dir.txt
for arm in naive greedy; do
  STS2_OOC_POLICY=$arm scripts/run_caffeinated.sh bash -c 'for char in Ironclad Silent Defect Regent Necrobinder; do
      echo "===== $char ====="; .venv/bin/python -u python/play_full_run.py 5 "$char"; done' \
      > "$OUT/regression_${arm}.log" 2>&1 &
done
wait
grep -H -E "^===== |Wins: |OOC policy|NEVER ENGAGED|OOC FALLBACK RATE|Run [0-9]+: (TIMEOUT|ERROR|HANG)" "$OUT"/regression_*.log
```

Expected: 两个日志里 5 个角色全部 `Completed: 5/5`；naive 日志 `OOC policy: naive -- 0 greedy decisions, 0 fallbacks (raised 0, refused 0, none 0, shop_cap 0), 0 event-stuck exits`；greedy 日志 greedy 决策数 > 0、没有 NEVER ENGAGED、没有 `OOC FALLBACK RATE`。**任一角色不到 5/5 就停**：用同 seed 重跑定位（路线由 `random.Random(seed)` 锁定，可复现），修好再从 Step 1 重来。

- [ ] **Step 2: （已并入 Step 1）**

- [ ] **Step 3: 记录结果到本文件并提交**

```bash
git add docs/superpowers/plans/2026-09-30-phase3a-ooc-policy-port.md
git commit -m "docs: Phase 3a regression gate results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: 配对 A/B（a1，naive vs greedy，40 新种子 × 5 角色）

- [ ] **Step 1: 启动（后台，~1 天）**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
AB=~/.sts2-train/phase3a_ab_$(date +%Y%m%d_%H%M%S); mkdir -p "$AB"; echo "$AB" > ~/.sts2-train/last_phase3a_ab_dir.txt
for char in Ironclad Silent Defect Regent Necrobinder; do
  ( for arm in naive greedy; do
      STS2_OOC_POLICY=$arm STS2_SOLVER_THREADS=3 scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py 40 "$char" \
          --ascension 1 --seed-prefix p3a_ --keep-game-logs "$AB/games_${arm}" \
          --results-log "$AB/${char}_${arm}.jsonl" > "$AB/${char}_${arm}.log" 2>&1
    done ) &
done
wait
```

5 条 lane 并行（每 lane 一个角色、两条臂先后跑），每个 solver 进程 3 线程：5×3=15 ≤ 18 核，与 Phase 2 同一口径（CPU 饥饿会把限时搜索测弱，e7c4ed8）。

- [ ] **Step 2: 盯停滞（协调者负责）**

每条 lane 的当前日志 15 分钟没写入就报告（BUG-040 教训：只等"完成"会让一个挂死的 lane 白等 24 小时）。引擎单次回复超时已由 `python/engine_process.py` 的看门狗处理（记为 HANG/`stuck`，不计入配对）。

- [ ] **Step 3: 配对报告**

```bash
cd /Users/bytedance/mygit/sts2-cli
AB=$(cat ~/.sts2-train/last_phase3a_ab_dir.txt)
for char in Ironclad Silent Defect Regent Necrobinder; do
    echo "########## $char ##########"
    .venv/bin/python -m agent.paired_eval "$AB/${char}_naive.jsonl" "$AB/${char}_greedy.jsonl" \
        --label-a "naive" --label-b "greedy"
    grep -h -E "Wins: |OOC policy|NEVER ENGAGED|OOC FALLBACK RATE" "$AB/${char}_naive.log" "$AB/${char}_greedy.log"
done
```

同时算每臂的 A1/A2/A3 boss 通过率与胜场（全局层 >17 / >34 / 胜利）：

```bash
AB=$(cat ~/.sts2-train/last_phase3a_ab_dir.txt)
.venv/bin/python - "$AB" <<'EOF'
import glob, json, os, sys
ab = sys.argv[1]
for path in sorted(glob.glob(os.path.join(ab, "*_*.jsonl"))):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    valid = [r for r in rows if r.get("status") in ("win", "dead")]
    n = len(valid) or 1
    gf = [r.get("floor") or 0 for r in valid]
    print(f"{os.path.basename(path):28s} valid={len(valid):2d}/{len(rows):2d} "
          f"passA1={sum(f > 17 for f in gf)/n:4.0%} passA2={sum(f > 34 for f in gf)/n:4.0%} "
          f"wins={sum(r['status'] == 'win' for r in valid)} "
          f"fallbacks={sum(r.get('ooc_fallbacks') or 0 for r in rows)}")
EOF
```

- [ ] **Step 4: 把实测数字写进本文件**

在本 Task 下加"配对评测结果（实测）"小节：逐角色（五个都写）有效配对数、naive → greedy 全局层均值、配对差值、t、p、两臂 boss 通过率与胜场、greedy 臂回落数。

---

### Task 8: 判定与收尾

- [ ] **Step 1: 判定**

- 所有角色配对差值 ≥ 0，或为负但 p ≥ 0.05 → 把 `OOC_POLICY_DEFAULT` 改为 `"greedy"`，并把 `test_ooc_policy_defaults_to_naive`/`test_ooc_policy_blank_means_default` 的期望改成 `"greedy"`（测试名同步改为 `..._defaults_to_greedy`）；`test_ascension_defaults_to_zero`、`test_result_carries_policy_and_zero_counters_under_naive`、`test_naive_policy_never_loads_greedy` 里的 `monkeypatch.delenv("STS2_OOC_POLICY", ...)` 改成 `monkeypatch.setenv("STS2_OOC_POLICY", "naive")`。
- 任一角色差值为负且 p < 0.05 → **不翻默认**，停下来把该角色的数字交给协调者（可能需要按角色分策略，那是新的决策，不在本计划内）。

- [ ] **Step 2: 更新 CLAUDE.md 协议说明**

在 `CLAUDE.md` 的 "Protocol notes" 末尾加一条（按实际判定结果改写括号内内容）：

```markdown
- `python/play_full_run.py` out-of-combat decisions (card_reward, rest_site, event_choice, bundle_select, card_select, shop) follow `STS2_OOC_POLICY`: `greedy` = `agent.combat_env.greedy_action` (default since Phase 3a — a1 paired A/B, see `docs/superpowers/plans/2026-09-30-phase3a-ooc-policy-port.md`), `naive` = the old placeholders (card 0, always HEAL, never shop), kept as the control arm; unknown values raise. `map_select` stays a seeded random route and `combat_play` stays on the solver. `--ascension N`, `--seed-prefix P` (default `run_`) and `--keep-game-logs DIR` exist for experiments; **`logs/` is purged after 7 days** (`game_log.cleanup_old_logs`), so any game log an analysis needs must be kept with `--keep-game-logs`.
```

- [ ] **Step 3: 全部单测**

Run: `.venv/bin/python -m pytest tests/test_ooc_policy.py tests/test_map_route_determinism.py tests/test_solver_gate.py tests/test_engine_process.py tests/test_plan_combat_turn_resolution.py -q`
Expected: 全部 PASS。

- [ ] **Step 4: 提交**

```bash
git add python/play_full_run.py tests/test_ooc_policy.py CLAUDE.md docs/superpowers/plans/2026-09-30-phase3a-ooc-policy-port.md
git commit -m "feat: greedy out-of-combat policy is the play_full_run default (Phase 3a A/B)

<逐角色 naive -> greedy 配对差值与 p 值>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
