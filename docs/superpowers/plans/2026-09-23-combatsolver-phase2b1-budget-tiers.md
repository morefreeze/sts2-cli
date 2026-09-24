# Combat Solver Phase 2b-1：搜索时间档位 + 挂死看门狗 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `plan_combat_turn` 的单次搜索时间可以在 **30 / 60 / 120 / 180 / 300 秒**几个档位里选，给每次搜索加上"为什么停"的遥测，用看门狗把 BUG-040 的永久挂死降级成"只损失一局"，然后用配对评测回答"训练该用哪一档"。

**Architecture:** 时间档位不改 vendored 引擎——`SearchPolicySnapshot.BudgetOverrideMilliseconds` 已经会被 `CombatSearchCoordinator.cs:237-238` 原样套成 `profile with { SoftTimeBudgetMilliseconds = ... }`，我们只把环境变量 `STS2_SOLVER_BUDGET` 接到这个字段上。遥测读的是 `Solve` 返回之后 `SolverResult` 上已有的字段，**不碰 `progressCallback`**（它非空时协调器会切到额外维护 UI 预览的路径，在按墙钟计的预算下会吃掉搜索时间，污染档位对比）。看门狗是 harness 侧的：新模块 `python/engine_process.py` 用读线程 + 队列给每次回复设超时，超时就按**进程组**杀掉引擎（只杀 `dotnet run` 会让真正在死转的 Sts2Headless 子进程变成孤儿继续占满一个核）。

**Tech Stack:** C# / .NET 9（`src/Sts2Headless/RunSimulator.cs`，只动我们自己的代码）、Python 3.11（`.venv/bin/python`）、pytest、`agent/paired_eval.py`。

---

## 范围

**做：** 时间档位（C# + Python 两侧）、每次搜索的遥测、看门狗、完整回归、档位配对评测、文档。

**不做（各自单独立项）：**
- **Phase 2b-2**：把 solver 接进 `agent/combat_env.py` / `agent/eval_rl.py`，以及退休 `agent/turn_planner.py`。本计划的档位结论是它的**输入**（训练吞吐取决于用哪一档），所以排在后面。
- **BUG-040 根因**：两份 core dump 已存（`~/.sts2-train/hang_ironclad_run{17,32}_20260923/`），但解析托管栈要装 `dotnet-dump`（从 NuGet 下载），需要先问用户。看门狗是缓解，不是修复。
- BUG-041（Regent 分叉异常）、BUG-042（execution_failed 与 ERROR 行缺 act/floor）。

## 依据（写计划前实测过，不是猜测）

**用 Phase 2 回归里 1 409 次真实搜索的耗时推算各档（8 线程、无争用）：**

| 档位 | 会被截断的搜索 | 平均每次搜索耗时 | 相对 120 s |
|---|---|---|---|
| 30 s | 6.9% | 6.73 s | 73% |
| 60 s | 2.9% | 8.09 s | 88% |
| 120 s（现默认，Phase 2 已验证） | 1.3% | 9.22 s | 100% |
| 180 / 300 s | 只影响现在顶到 120 s 的那 1.3% | ≥ 9.22 s | ≥ 100%（数据被 120 s 截断，外推不了） |

分角色的"被截断比例"（30 s / 60 s / 120 s）：Necrobinder **20.4% / 12.1% / 6.9%**、Regent 9.8% / 1.8% / 0%、Defect 4.2% / 0.4% / 0%、Ironclad 3.1% / 0.6% / 0%、Silent 0.3% / 0% / 0%。

由此推出评测怎么设计：
1. **30 s 只省约 27% 的时间**——中位数搜索本来只要 3.4 s，大多数搜索自己就收敛了。所以问题不是"30 s 快多少"，而是"30 s 伤不伤棋力"。
2. **120 s 以上的档位只可能改变 Necrobinder**（其余四个角色在 120 s 下被截断的比例都是 0%）。所以 300 s 只对 Necrobinder 测。
3. 180/300 s 可能根本用不上：`MaxExpandedNodes = 120 000` 是停止条件，节点先用完的话多给时间也没用。Task 1 的遥测就是用来看清这一点的。

**`SolverResult.BoundaryReason` 的语义（`CombatBeamSolver.Phases.cs:474-484`）：** 它是**被选中那条路线**的边界。只有在那条路线本身没有自然边界（`None`）时，才会被改写成 `TimeLimit` / `NodeLimit` / `TurnLimit`。一次搜索即使用完了时间，只要它的最优路线停在洗牌点，报的就是 `Shuffle` 而不是 `TimeLimit`。**所以 `TimeLimit` 会少算被时间截断的搜索**。分析时要同时看我们自己计的墙钟 `elapsed_ms ≥ budget_ms − 1000`。

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/Sts2Headless/RunSimulator.cs` | 解析 `STS2_SOLVER_BUDGET`、接到 `BudgetOverrideMilliseconds`、在 `combat_plan` 回复里加 `search` 遥测 | 修改 |
| `python/play_full_run.py` | 档位表与校验、看门狗超时计算、接入 `EngineProcess`、HANG 结果、结果行记录档位 | 修改 |
| `python/engine_process.py` | 引擎子进程：进程组启动、读线程、带超时读回复、按进程组杀 | **新建** |
| `tests/fake_engine.py` | 假引擎脚本（不是测试模块），模拟 JSON 协议和"孙子进程" | **新建** |
| `tests/test_engine_process.py` | `EngineProcess` 单测 | **新建** |
| `tests/test_solver_gate.py` | 档位解析、结果行、HANG 渲染、`play_run` 挂死集成测试 | 修改 |

---

### Task 1: 引擎侧时间档位 + 搜索遥测（C#）

**Files:**
- Modify: `src/Sts2Headless/RunSimulator.cs`（`SolverThreads` 字段附近约 1084 行加新字段和解析函数；约 1157 行 `BudgetOverrideMilliseconds: null`；约 1246-1265 行 `Solve` 调用与 `combat_plan` 回复）

这个仓库的 C# 没有单测工程，这个 Task 靠构建 + 一局真实对局验证（和 `STS2_SOLVER_THREADS` 那次同一种做法）。

- [ ] **Step 1: 加档位表和解析函数**

在 `RunSimulator.cs` 里 `private static readonly int SolverThreads = ResolveSolverThreadsAndLog();` 这一行**之前**加入下面整段。**顺序很重要**：C# 的静态字段初始化器按源码文本顺序执行，`SolverBudgetTiersSeconds` 必须写在 `SolverBudgetMilliseconds` 前面，否则后者初始化时数组还是 `null`，会让整个 `RunSimulator` 类型初始化失败。

```csharp
    /// <summary>
    /// The only per-call solver budgets a batch may select, in SECONDS. 120 is the vendored
    /// SolverSearchProfile.Default budget that Phase 2's paired A/B validated; 30/60/180/300 are the
    /// tiers under study in Phase 2b-1 (docs/superpowers/plans/2026-09-23-combatsolver-phase2b1-budget-tiers.md).
    /// Mirrors python/play_full_run.py SOLVER_BUDGET_TIERS_S -- keep the two in sync. play_full_run
    /// cross-checks the budget this engine reports on every plan (combat_plan.search.budget_ms) and
    /// fails the run loudly on a mismatch, so drift cannot silently mislabel an A/B arm.
    /// Must stay ABOVE SolverBudgetMilliseconds: static initializers run in textual order.
    /// </summary>
    internal static readonly int[] SolverBudgetTiersSeconds = { 30, 60, 120, 180, 300 };

    /// <summary>
    /// Per-call soft time budget for plan_combat_turn in milliseconds, or null to keep the profile's
    /// own default (SolverSearchProfile.Default.SoftTimeBudgetMilliseconds = 120 000). Resolved once
    /// from STS2_SOLVER_BUDGET. It is passed as SearchPolicySnapshot.BudgetOverrideMilliseconds, which
    /// CombatSearchCoordinator.cs:237-238 applies as `profile with { SoftTimeBudgetMilliseconds = ... }`
    /// -- no vendored code changes. The budget is wall-clock and only checked between node expansions
    /// (CombatBeamSolver.Phases.cs:1496/1811, NoveltySearch.cs:96), so it bounds the search, not a
    /// single runaway expansion -- that is what the harness watchdog is for (agent/bug.md BUG-040).
    /// </summary>
    private static readonly int? SolverBudgetMilliseconds = ResolveSolverBudgetAndLog();

    private static int? ResolveSolverBudgetAndLog()
    {
        var raw = Environment.GetEnvironmentVariable("STS2_SOLVER_BUDGET");
        var resolved = ResolveSolverBudget(raw, out var warning);
        if (warning != null)
            Console.Error.WriteLine(warning);
        var effectiveSeconds = (resolved ?? SolverSearchProfile.Default.SoftTimeBudgetMilliseconds) / 1000;
        Console.Error.WriteLine(
            $"[Sts2Headless] Solver time budget: {effectiveSeconds}s " +
            $"(STS2_SOLVER_BUDGET={(string.IsNullOrWhiteSpace(raw) ? "<unset>" : raw)})");
        return resolved;
    }

    /// <summary>
    /// Pure parse for STS2_SOLVER_BUDGET (seconds). Unset/blank -> null (keep the profile default).
    /// Anything not on <see cref="SolverBudgetTiersSeconds"/> -> null plus a warning naming the bad
    /// value, same policy as <see cref="ResolveSolverThreads"/>: a typo must not silently change what a
    /// multi-hour paired A/B measures. (The Python harness also rejects it before any game starts.)
    /// </summary>
    internal static int? ResolveSolverBudget(string? raw, out string? warning)
    {
        warning = null;
        if (string.IsNullOrWhiteSpace(raw))
            return null;
        if (int.TryParse(raw.Trim(), out var seconds) && Array.IndexOf(SolverBudgetTiersSeconds, seconds) >= 0)
            return seconds * 1000;
        warning = $"[WARN] STS2_SOLVER_BUDGET='{raw}' is not one of the supported tiers " +
                  $"({string.Join("/", SolverBudgetTiersSeconds)} s); using the default " +
                  $"{SolverSearchProfile.Default.SoftTimeBudgetMilliseconds / 1000}s instead";
        return null;
    }
```

- [ ] **Step 2: 接到搜索策略上**

在 `DoPlanCombatTurn` 构造 `SearchPolicySnapshot` 的地方，把

```csharp
            BudgetOverrideMilliseconds: null,
```

改成

```csharp
            BudgetOverrideMilliseconds: SolverBudgetMilliseconds,
```

- [ ] **Step 3: 给 `Solve` 计时，并在回复里加 `search` 遥测**

把现在的

```csharp
        SolverResult result;
        try
        {
            result = CombatSearchCoordinator.Solve(
                snapshot, displayNames, battleDamage, policy,
                CancellationToken.None, progressCallback: null);
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("CombatSearchCoordinator.Solve failed", ex);
        }

        return new Dictionary<string, object?>
        {
            ["type"] = "combat_plan",
            ["only_death_routes_found"] = result.OnlyDeathRoutesFound,
            ["action_count"] = result.BestNode.ActionCount,
            ["score"] = result.BestNode.Score,
            ["projected_player_hp"] = result.Snapshot.ProjectedPlayerHp,
            ["all_enemies_dead"] = result.Snapshot.AllEnemiesDead,
            ["actions"] = ConvertPlanActionsToJson(result.BestNode.Actions),
        };
```

改成

```csharp
        SolverResult result;
        var solveClock = System.Diagnostics.Stopwatch.StartNew();
        try
        {
            // progressCallback stays null on purpose: when it is non-null the coordinator switches to
            // an "enriched" path that also maintains UI route previews (CombatSearchCoordinator.cs:79-
            // 133), extra work that -- under a wall-clock budget -- would come out of search time and
            // contaminate any budget-tier comparison. Everything reported below is read from the
            // SolverResult after Solve returns, so it cannot perturb the search.
            result = CombatSearchCoordinator.Solve(
                snapshot, displayNames, battleDamage, policy,
                CancellationToken.None, progressCallback: null);
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("CombatSearchCoordinator.Solve failed", ex);
        }
        solveClock.Stop();

        return new Dictionary<string, object?>
        {
            ["type"] = "combat_plan",
            ["only_death_routes_found"] = result.OnlyDeathRoutesFound,
            ["action_count"] = result.BestNode.ActionCount,
            ["score"] = result.BestNode.Score,
            ["projected_player_hp"] = result.Snapshot.ProjectedPlayerHp,
            ["all_enemies_dead"] = result.Snapshot.AllEnemiesDead,
            ["actions"] = ConvertPlanActionsToJson(result.BestNode.Actions),
            // Why and how far this search went. `boundary` is the CHOSEN LINE's boundary: it only
            // becomes TimeLimit/NodeLimit/TurnLimit when that line had no natural stopping point of its
            // own (CombatBeamSolver.Phases.cs:474-484), so a search that ran out of time but whose best
            // line stops at a Shuffle reports "Shuffle". Compare elapsed_ms with budget_ms to catch every
            // time-capped search.
            ["search"] = new Dictionary<string, object?>
            {
                ["budget_ms"] = SolverBudgetMilliseconds ?? SolverSearchProfile.Default.SoftTimeBudgetMilliseconds,
                ["elapsed_ms"] = solveClock.ElapsedMilliseconds,
                ["boundary"] = result.BoundaryReason.ToString(),
                ["expanded_nodes"] = result.ExpandedNodes,
                ["total_expanded_nodes"] = result.TotalExpandedNodes,
            },
        };
```

- [ ] **Step 4: 构建**

```bash
cd /Users/bytedance/mygit/sts2-cli
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | grep -E "Error\(s\)|error CS"
```

Expected: `0 Error(s)`。

- [ ] **Step 5: 一局真实对局，确认档位生效、遥测有值**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
STS2_SOLVER_BUDGET=30 .venv/bin/python -u python/play_full_run.py 1 Ironclad 2>&1 | grep -E "Solver time budget|Solver engagement"
.venv/bin/python - <<'EOF'
import glob, json, os
path = max(glob.glob("logs/*_Ironclad_run_1.jsonl"), key=os.path.getmtime)
searches = []
for line in open(path):
    e = json.loads(line) if line.strip() else {}
    d = e.get("data") or {}
    if e.get("type") == "state" and d.get("type") == "combat_plan":
        searches.append(d.get("search"))
print(len(searches), "plans; first three search dicts:")
for s in searches[:3]:
    print(" ", s)
print("max elapsed_ms:", max(s["elapsed_ms"] for s in searches))
print("boundaries:", sorted({s["boundary"] for s in searches}))
EOF
STS2_SOLVER_BUDGET=45 .venv/bin/python -u python/play_full_run.py 1 Ironclad 2>&1 | grep -E "WARN\] STS2_SOLVER_BUDGET|Solver time budget" | head -2
```

Expected：
- 第一局 stderr 有 `Solver time budget: 30s (STS2_SOLVER_BUDGET=30)`。
- 每个 `search` 都有 `budget_ms: 30000`；`total_expanded_nodes > 0` 且 `≥ expanded_nodes`；`max elapsed_ms` 不超过约 31 000。
- 第二局（`45` 不在档位表里）先打 `[WARN] STS2_SOLVER_BUDGET='45' is not one of the supported tiers ...`，然后 `Solver time budget: 120s`。

如果 `total_expanded_nodes` 恒为 0，说明这条路径不填这个字段——在报告里写明，分析时改用 `expanded_nodes`，**不要**去改 vendored 代码补它。

- [ ] **Step 6: Commit**

```bash
git add src/Sts2Headless/RunSimulator.cs
git commit -m "feat: selectable solver time budget tiers and per-search telemetry

STS2_SOLVER_BUDGET picks the plan_combat_turn soft budget from 30/60/120/
180/300 s (unset keeps the Phase 2-validated 120 s). Wired through the
existing SearchPolicySnapshot.BudgetOverrideMilliseconds, which the
vendored coordinator already applies to the profile -- no vendored edits.
A value off the tier list warns and falls back rather than silently
changing what a batch measures.

Every combat_plan reply now carries search.{budget_ms, elapsed_ms,
boundary, expanded_nodes, total_expanded_nodes}, read from SolverResult
after Solve returns. progressCallback stays null: when set, the
coordinator also maintains UI previews, which under a wall-clock budget
would eat into search time and skew any tier comparison.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: harness 侧档位配置（Python，TDD）

**Files:**
- Modify: `python/play_full_run.py`（`solver_characters()` 之后加常量和函数；`result_to_eval_row()`）
- Test: `tests/test_solver_gate.py`（追加）

- [ ] **Step 1: 写失败的测试**

在 `tests/test_solver_gate.py` 末尾追加：

```python
# --- solver time budget tiers (Phase 2b-1) ------------------------------------

def test_solver_budget_defaults_to_the_validated_120s():
    assert play_full_run.solver_budget_seconds({}) == 120


def test_blank_solver_budget_means_default():
    assert play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": "  "}) == 120


@pytest.mark.parametrize("tier", [30, 60, 120, 180, 300])
def test_solver_budget_accepts_each_tier(tier):
    assert play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": str(tier)}) == tier


@pytest.mark.parametrize("bad", ["45", "0", "-30", "abc", "30s", "120.0", "+30"])
def test_solver_budget_rejects_anything_off_the_tier_list(bad):
    # Raise before any game starts: a typo that silently ran the default would
    # label a 120 s run as some other tier in a multi-hour A/B.
    with pytest.raises(ValueError, match="STS2_SOLVER_BUDGET"):
        play_full_run.solver_budget_seconds({"STS2_SOLVER_BUDGET": bad})


def test_solver_call_watchdog_sits_well_above_every_budget():
    # The budget is soft (checked between node expansions); the worst overrun in
    # 1,409 measured solves was 0.4 s. The watchdog must never fire on a
    # legitimately slow search, only on a BUG-040 hang.
    for tier in play_full_run.SOLVER_BUDGET_TIERS_S:
        assert play_full_run.solver_call_timeout_s(tier) >= tier + 60


def test_result_row_records_the_budget_tier(monkeypatch):
    monkeypatch.setenv("STS2_SOLVER_BUDGET", "30")
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "act": 1, "floor": 5}, "Silent")
    assert row["solver_budget_s"] == 30
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q 2>&1 | tail -3
```

Expected: 新测试 FAIL，`AttributeError: module 'play_full_run' has no attribute 'solver_budget_seconds'`。

- [ ] **Step 3: 实现**

在 `python/play_full_run.py` 的 `solver_characters()` 函数定义结束之后加入：

```python
# Selectable soft time budgets for one plan_combat_turn call, in seconds. 120
# is the vendored SolverSearchProfile.Default budget that Phase 2's paired A/B
# validated; the others are the tiers under study in Phase 2b-1. Mirrors
# RunSimulator.cs SolverBudgetTiersSeconds -- play_run() cross-checks the
# budget the engine reports on every plan, so a drift fails the run loudly.
SOLVER_BUDGET_TIERS_S = (30, 60, 120, 180, 300)
SOLVER_BUDGET_DEFAULT_S = 120
# How far past its budget a plan_combat_turn reply may run before the watchdog
# declares the engine hung (agent/bug.md BUG-040). The budget is soft -- only
# checked between node expansions -- but the worst overrun in 1,409 measured
# solves was 0.4 s, so 60 s is ~150x headroom while still costing one game
# instead of a whole day.
SOLVER_WATCHDOG_MARGIN_S = 60
# Every other engine reply arrives in well under a second; 120 s only fires on
# a genuine stall.
ENGINE_REPLY_TIMEOUT_S = 120


def solver_budget_seconds(env=None) -> int:
    """Resolve STS2_SOLVER_BUDGET to one of SOLVER_BUDGET_TIERS_S.

    Unset/blank means the validated default. Anything else must be a tier
    written as plain digits; otherwise raise, for the same reason
    solver_characters() does -- a typo must not silently make a run measure a
    different budget than the one it is labelled with.
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_SOLVER_BUDGET") or "").strip()
    if not raw:
        return SOLVER_BUDGET_DEFAULT_S
    if raw.isdigit() and int(raw) in SOLVER_BUDGET_TIERS_S:
        return int(raw)
    raise ValueError(
        f"STS2_SOLVER_BUDGET={raw!r} is not a supported tier; use one of "
        f"{', '.join(str(t) for t in SOLVER_BUDGET_TIERS_S)} (seconds)")


def solver_call_timeout_s(budget_s: int) -> float:
    """Watchdog deadline for one plan_combat_turn reply."""
    return budget_s + SOLVER_WATCHDOG_MARGIN_S
```

在 `result_to_eval_row()` 返回的 dict 里，`"solver": sorted(solver_characters()),` 之后加一行：

```python
        "solver_budget_s": solver_budget_seconds(),
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py tests/test_plan_combat_turn_resolution.py tests/test_map_route_determinism.py -q 2>&1 | tail -2
```

Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add python/play_full_run.py tests/test_solver_gate.py
git commit -m "feat: harness-side solver budget tiers and watchdog deadline

SOLVER_BUDGET_TIERS_S mirrors the engine's tier table; an off-list
STS2_SOLVER_BUDGET raises before any game starts. The watchdog deadline for
a plan_combat_turn reply is budget + 60 s -- 150x the worst overrun seen in
1,409 measured solves. Result rows now record the tier.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `EngineProcess`——带超时读回复、按进程组杀（Python，TDD）

**Files:**
- Create: `python/engine_process.py`
- Create: `tests/fake_engine.py`（脚本，不以 `test_` 开头，pytest 不会收集它）
- Create: `tests/test_engine_process.py`

- [ ] **Step 1: 写假引擎**

创建 `tests/fake_engine.py`：

```python
"""Stand-in for the Sts2Headless engine, for the watchdog tests. Not a test module.

Speaks just enough of the JSON line protocol: a non-JSON warm-up line, then
{"type": "ready"}, then one reply per command. `start_run` returns a
combat_play decision; `plan_combat_turn` returns a combat_plan whose
search.budget_ms is --plan-budget-ms; the command or action named by
--hang-on never gets a reply (the BUG-040 shape); `quit` exits.

It also starts a long-sleeping child and writes its pid to --pidfile. That child
stands in for the real engine process that `dotnet run` spawns, so tests can
prove the watchdog kills the whole process group -- killing only the wrapper
would leave a real BUG-040 hang spinning a core forever.
"""
import argparse
import json
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--pidfile", required=True)
parser.add_argument("--hang-on", default="hang")
parser.add_argument("--plan-budget-ms", type=int, default=120_000)
args = parser.parse_args()

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(args.pidfile, "w") as fh:
    fh.write(str(child.pid))

COMBAT = {
    "type": "decision", "decision": "combat_play", "round": 3, "energy": 3,
    "context": {"act": 1, "floor": 8, "room_type": "Monster"},
    "player": {"hp": 28, "max_hp": 80, "gold": 0},
    "hand": [], "enemies": [{"index": 0, "hp": 38, "combat_id": 1}],
}

print("warming up, not JSON", flush=True)
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd.get("cmd") == "quit":
        break
    if args.hang_on in (cmd.get("cmd"), cmd.get("action")):
        time.sleep(600)
    if cmd.get("cmd") == "start_run":
        reply = COMBAT
    elif cmd.get("action") == "plan_combat_turn":
        reply = {"type": "combat_plan", "actions": [],
                 "search": {"budget_ms": args.plan_budget_ms, "elapsed_ms": 1,
                            "boundary": "None", "expanded_nodes": 1,
                            "total_expanded_nodes": 1}}
    else:
        reply = {"type": "decision", "decision": "echo", "echo": cmd}
    print(json.dumps(reply), flush=True)
child.kill()
```

- [ ] **Step 2: 写失败的测试**

创建 `tests/test_engine_process.py`：

```python
"""EngineProcess: per-reply watchdog and process-group kill (agent/bug.md BUG-040)."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
from engine_process import EngineHang, EngineProcess

FAKE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _spawn(tmp_path, hang_on="hang"):
    pidfile = tmp_path / "grandchild.pid"
    engine = EngineProcess([sys.executable, FAKE, "--pidfile", str(pidfile), "--hang-on", hang_on])
    return engine, pidfile


def _grandchild_pid(pidfile):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = pidfile.read_text().strip() if pidfile.exists() else ""
        if text:
            return int(text)
        time.sleep(0.05)
    raise AssertionError("fake engine never wrote its child's pid")


def _gone(pid, within=5.0):
    # A killed process lingers as a zombie until reaped; once its parent is gone
    # too, launchd reaps it -- poll instead of checking once.
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_reads_ready_and_reports_skipped_non_json_lines(tmp_path):
    engine, _ = _spawn(tmp_path)
    skipped = []
    try:
        assert engine.read_json(10, on_skip=skipped.append) == {"type": "ready"}
        assert skipped == ["warming up, not JSON"]
    finally:
        engine.close()


def test_request_round_trip(tmp_path):
    engine, _ = _spawn(tmp_path)
    try:
        engine.read_json(10)
        engine.write({"cmd": "action", "action": "end_turn"})
        assert engine.read_json(10)["echo"]["action"] == "end_turn"
    finally:
        engine.close()


def test_a_hung_reply_raises_engine_hang_instead_of_blocking_forever(tmp_path):
    engine, _ = _spawn(tmp_path)
    try:
        engine.read_json(10)
        engine.write({"cmd": "action", "action": "hang"})
        started = time.monotonic()
        with pytest.raises(EngineHang):
            engine.read_json(0.5)
        assert time.monotonic() - started < 5
    finally:
        engine.kill()


def test_kill_takes_down_the_engine_behind_the_wrapper(tmp_path):
    # In BUG-040 the spinning process was the real engine, a CHILD of
    # `dotnet run`. Killing only the wrapper would orphan it at 100% CPU.
    engine, pidfile = _spawn(tmp_path)
    engine.read_json(10)
    grandchild = _grandchild_pid(pidfile)
    engine.write({"cmd": "action", "action": "hang"})
    with pytest.raises(EngineHang):
        engine.read_json(0.5)
    engine.kill()
    assert _gone(grandchild), "the engine behind the wrapper survived the watchdog kill"


def test_engine_exit_raises_eof_not_engine_hang(tmp_path):
    engine, _ = _spawn(tmp_path)
    engine.read_json(10)
    engine.write({"cmd": "quit"})
    with pytest.raises(RuntimeError, match="EOF"):
        engine.read_json(10)
    engine.close()


def test_close_after_a_normal_run_leaves_nothing_behind(tmp_path):
    engine, pidfile = _spawn(tmp_path)
    engine.read_json(10)
    grandchild = _grandchild_pid(pidfile)
    engine.close()
    assert _gone(grandchild)


def test_close_after_kill_is_harmless(tmp_path):
    engine, _ = _spawn(tmp_path)
    engine.read_json(10)
    engine.kill()
    engine.close()   # must not raise
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_engine_process.py -q 2>&1 | tail -3
```

Expected: FAIL，`ModuleNotFoundError: No module named 'engine_process'`。

- [ ] **Step 4: 实现**

创建 `python/engine_process.py`：

```python
"""The headless engine as a subprocess, with a per-reply watchdog.

agent/bug.md BUG-040: a plan_combat_turn call can hang forever -- one search
worker spins inside a single node expansion, where the solver's time budget is
never checked -- and a bare `proc.stdout.readline()` then blocks the harness
for as long as the engine spins (24 h, the first time). Here a reader thread
feeds a queue and every read has a deadline.

The engine runs in its own process group (start_new_session=True, the same
pattern agent/combat_env.py uses), because `dotnet run` is only a wrapper: the
process that actually spins is its child, and killing the wrapper alone would
orphan it at 100% CPU.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time


class EngineHang(Exception):
    """The engine did not reply within the watchdog deadline.

    Deliberately NOT a RuntimeError, so an engine that exited (EOF,
    RuntimeError) can never be mistaken for one that hung.
    """


_EOF = object()


class EngineProcess:
    def __init__(self, argv, *, stderr=None):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._lines = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(_EOF)

    def write(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def read_json(self, timeout: float, on_skip=None) -> dict:
        """Next JSON reply, skipping non-JSON lines (build warnings etc.).

        Raises EngineHang if nothing JSON arrives within `timeout` seconds, and
        RuntimeError on EOF (the engine exited).
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineHang(f"no reply within {timeout:g}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise EngineHang(f"no reply within {timeout:g}s") from None
            if line is _EOF:
                raise RuntimeError("No response from simulator (EOF)")
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
            if line and on_skip is not None:
                on_skip(line)

    def kill(self) -> None:
        """Kill the whole process group -- the wrapper AND the engine under it."""
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        """Ask the engine to quit; if it doesn't, kill it. Safe after kill()."""
        if self.proc.poll() is None:
            try:
                self.write({"cmd": "quit"})
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.kill()
        # Sweep the group either way: a child can outlive a wrapper that exited.
        # Safe from pid reuse -- a pgid cannot be recycled while any member of
        # the group is alive, and if none is, killpg just raises.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_engine_process.py -q 2>&1 | tail -3
```

Expected: 7 passed，且总耗时在几秒内（没有测试会等满假引擎的 600 秒睡眠）。

- [ ] **Step 6: Commit**

```bash
git add python/engine_process.py tests/fake_engine.py tests/test_engine_process.py
git commit -m "feat: EngineProcess -- engine subprocess with a per-reply watchdog

A reader thread feeds a queue so every read has a deadline (EngineHang),
and the engine runs in its own process group so a kill takes down the
real engine under the \`dotnet run\` wrapper, not just the wrapper. Tested
against a fake engine that hangs on demand and owns a child standing in for
the real engine.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `play_run()` 接入看门狗、HANG 结果、档位核对（Python，TDD）

**Files:**
- Modify: `python/play_full_run.py`
- Test: `tests/test_solver_gate.py`（追加）

- [ ] **Step 1: 写失败的测试**

在 `tests/test_solver_gate.py` 末尾追加：

```python
# --- play_run watchdog integration (Phase 2b-1) --------------------------------

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _fake_engine(monkeypatch, tmp_path, *extra):
    pidfile = tmp_path / "engine.pid"
    monkeypatch.setattr(play_full_run, "engine_argv",
                        lambda: [sys.executable, FAKE_ENGINE, "--pidfile", str(pidfile), *extra])
    monkeypatch.delenv("STS2_SOLVER_CHARS", raising=False)
    monkeypatch.delenv("STS2_SOLVER_BUDGET", raising=False)


def test_play_run_turns_a_solver_hang_into_a_hang_result(monkeypatch, tmp_path):
    _fake_engine(monkeypatch, tmp_path, "--hang-on", "plan_combat_turn")
    monkeypatch.setattr(play_full_run, "solver_call_timeout_s", lambda budget_s: 0.5)
    result = play_full_run.play_run("seed_x", "Ironclad", verbose=False, log=False)
    assert result["hang"] is True
    assert result["error"].startswith("engine_hang")
    # act/floor/hp come from the last decision seen before the hang -- the
    # error path must not report None/None like BUG-042's rows do.
    assert (result["act"], result["floor"], result["hp"]) == (1, 8, 28)
    assert play_full_run.result_to_eval_row(result, "Ironclad")["status"] == "stuck"


def test_play_run_fails_loudly_if_engine_and_harness_disagree_on_the_budget(monkeypatch, tmp_path):
    # Harness resolves 120 s (unset); the fake engine claims 30 s.
    _fake_engine(monkeypatch, tmp_path, "--plan-budget-ms", "30000")
    result = play_full_run.play_run("seed_x", "Ironclad", verbose=False, log=False)
    assert "solver budget" in result["error"]
    assert not result.get("hang")


def test_summarize_labels_a_hang_as_hang_and_not_completed():
    out = play_full_run.summarize(
        [{"victory": False, "seed": "s", "act": 1, "floor": 8, "steps": 40,
          "error": "engine_hang: no reply within 180s", "hang": True,
          "solver_plans": 3, "solver_errors": 0}],
        1, character="Ironclad", solver_chars={"Ironclad"})
    assert "Run 1: HANG" in out
    assert "Completed: 0/1" in out
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q -k "hang or budget_s or disagree" 2>&1 | tail -4
```

Expected: FAIL，`AttributeError: ... has no attribute 'engine_argv'`（以及 HANG 渲染断言失败）。

- [ ] **Step 3: 加 `engine_argv()` 和 import**

在 `python/play_full_run.py` 顶部 import 区，`from game_log import GameLogger` 之后加：

```python
from engine_process import EngineHang, EngineProcess
```

在 `PROJECT = ...` 定义之后加：

```python
def engine_argv() -> list:
    """Command line that starts the headless engine. A function (not a constant)
    so tests can point play_run() at tests/fake_engine.py."""
    return [DOTNET, "run", "--no-build", "--project", PROJECT]
```

- [ ] **Step 4: `play_run()` 开头解析档位**

把 `play_run()` 里的

```python
    rng = random.Random(seed)
    solver_chars = solver_characters()
```

改成

```python
    rng = random.Random(seed)
    solver_chars = solver_characters()
    solver_budget_s = solver_budget_seconds()
```

- [ ] **Step 5: 用 `EngineProcess` 替换 `Popen` / `read_json_line` / `send`**

把

```python
    logger = GameLogger(character, seed, enabled=log)
    proc = subprocess.Popen(
        [DOTNET, "run", "--no-build", "--project", PROJECT],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if not verbose else None,
        text=True,
        bufsize=1,
    )

    def read_json_line() -> dict:
        """Read a line from stdout, skipping non-JSON lines (build warnings etc.)"""
        while True:
            resp_line = proc.stdout.readline().strip()
            if not resp_line:
                raise RuntimeError("No response from simulator (EOF)")
            if resp_line.startswith("{"):
                return json.loads(resp_line)
            # Skip non-JSON lines (build warnings, etc.)
            if verbose:
                print(f"  [skip] {resp_line[:120]}")

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()
        resp = read_json_line()
        logger.log_state(resp)
```

改成

```python
    logger = GameLogger(character, seed, enabled=log)
    # stderr: inherit when verbose, otherwise DEVNULL -- never an unread PIPE,
    # which fills up and blocks the engine's Console.Error.WriteLine (the stderr
    # deadlock described in RunSimulator.cs DoPlanCombatTurn's diagnostics note).
    engine = EngineProcess(engine_argv(), stderr=None if verbose else subprocess.DEVNULL)
    solver_call_timeout = solver_call_timeout_s(solver_budget_s)

    def read_json_line(timeout: float = ENGINE_REPLY_TIMEOUT_S) -> dict:
        """Next JSON reply, skipping non-JSON lines (build warnings etc.).
        Raises EngineHang if nothing arrives in time (agent/bug.md BUG-040)."""
        on_skip = (lambda text: print(f"  [skip] {text[:120]}")) if verbose else None
        return engine.read_json(timeout, on_skip=on_skip)

    def send(cmd: dict) -> dict:
        line = json.dumps(cmd)
        if verbose:
            print(f"  > {line[:200]}")
        logger.log_action(cmd)
        engine.write(cmd)
        timeout = (solver_call_timeout if cmd.get("action") == "plan_combat_turn"
                   else ENGINE_REPLY_TIMEOUT_S)
        resp = read_json_line(timeout)
        logger.log_state(resp)
```

（`send()` 后面打印 verbose 摘要、`return resp` 的部分不变。）

- [ ] **Step 6: 把 `last_decision` 挪到 `try:` 之前**

`last_decision = {}` 现在在 `try:` 里面（它上方有一段 4 行的注释 "Most recent state with type=="decision" ..."）。把**这段注释和这一行一起**移到 `step = 0` / `try:` 之前，这样挂死发生在任何位置时，`except EngineHang` 都能读到它（`try` 里还没执行到赋值就挂死的话，它会是未绑定变量）。`try` 里原来那一行删掉，循环里的 `last_decision = state` 更新保持不变。

- [ ] **Step 7: 档位核对**

在 `combat_play` 分支里，把

```python
                    else:
                        solver_plans += 1
```

改成

```python
                    else:
                        solver_plans += 1
                        # The engine resolves STS2_SOLVER_BUDGET itself (RunSimulator.cs
                        # ResolveSolverBudget); this harness resolves it independently for
                        # the watchdog and the results row. If their tier tables ever
                        # drift, fail the run loudly rather than label a 30 s run as a
                        # 120 s one in an A/B.
                        reported = (plan.get("search") or {}).get("budget_ms")
                        if reported is not None and reported != solver_budget_s * 1000:
                            raise RuntimeError(
                                f"engine solver budget {reported} ms != STS2_SOLVER_BUDGET "
                                f"resolved here as {solver_budget_s} s")
```

- [ ] **Step 8: 加 `except EngineHang`，并让 `finally` 用 `engine.close()`**

在 `play_run()` 的 `except Exception as e:` **之前**加：

```python
    except EngineHang as e:
        context = last_decision.get("context") or {}
        player = last_decision.get("player") or {}
        print(f"  !! ENGINE HANG: {e} -- killing the engine process group (agent/bug.md BUG-040)")
        engine.kill()
        return {"victory": False, "seed": seed, "steps": step,
                "act": context.get("act"), "floor": context.get("floor"),
                "hp": player.get("hp"), "max_hp": player.get("max_hp"),
                "error": f"engine_hang: {e}", "hang": True,
                "solver_plans": solver_plans, "solver_errors": solver_errors}
```

把 `finally:` 里

```python
        try:
            proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            proc.stdin.flush()
        except:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except:
            proc.kill()
```

改成

```python
        engine.close()
```

- [ ] **Step 9: HANG 的渲染和结果行状态**

`summarize()` 里：

```python
            if r.get("victory"):
                status = "WIN"
            elif r.get("timeout"):
```

改成

```python
            if r.get("victory"):
                status = "WIN"
            elif r.get("hang"):
                status = "HANG"
            elif r.get("timeout"):
```

`result_to_eval_row()` 里：

```python
    if result.get("victory"):
        status = "win"
    elif result.get("timeout"):
```

改成

```python
    if result.get("victory"):
        status = "win"
    elif result.get("hang"):
        # "stuck" is in eval_rl's technical-status set, so paired_eval drops the
        # seed from pairing instead of averaging a killed run in as a death.
        status = "stuck"
    elif result.get("timeout"):
```

（HANG 结果带 `error` 键，所以 `summarize()` 现有的 `Completed` 计数已经会把它排除，不用改。）

- [ ] **Step 10: 跑全部相关测试**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py tests/test_engine_process.py tests/test_plan_combat_turn_resolution.py tests/test_map_route_determinism.py -q 2>&1 | tail -2
grep -n "proc\." python/play_full_run.py
```

Expected: 全部 PASS；`grep` 没有输出（`play_run()` 里不应再有直接操作 `proc` 的代码）。

- [ ] **Step 11: 一局真实对局冒烟**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
.venv/bin/python -u python/play_full_run.py 1 Silent 2>&1 | tail -6
pgrep -fl Sts2Headless || echo "no engine left running"
```

Expected: `Completed: 1/1`、`Solver engagement: N/N`（N > 0）；最后一行是 `no engine left running`（`close()` 没有留下孤儿进程）。

- [ ] **Step 12: Commit**

```bash
git add python/play_full_run.py tests/test_solver_gate.py
git commit -m "fix: watchdog every engine reply in play_run (BUG-040 mitigation)

play_run now drives the engine through EngineProcess: a plan_combat_turn
reply gets budget + 60 s, anything else 120 s, and a hang kills the whole
engine process group and records a HANG result (paired_eval status
\"stuck\", excluded from pairing) with act/floor/hp from the last decision.
A BUG-040 hang now costs one game instead of stalling a lane for a day.

Also cross-checks the engine's reported solver budget against the
harness's own STS2_SOLVER_BUDGET on every plan, and stops piping an unread
stderr in non-verbose mode (the Phase 1 stderr-deadlock hazard).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: 完整回归门槛（默认档位）

CLAUDE.md 要求任何代码改动都跑完整回归。这次改了引擎回复结构和 harness 的进程管理，必须跑。

- [ ] **Step 1: 跑 5 角色 × 5 局（默认 120 s、默认线程）**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
OUT=~/.sts2-train/phase2b1_gate_$(date +%Y%m%d_%H%M%S); mkdir -p "$OUT"
scripts/run_caffeinated.sh bash -c 'for char in Ironclad Silent Defect Regent Necrobinder; do
    echo "===== $char ====="; .venv/bin/python -u python/play_full_run.py 5 "$char"; done' \
    > "$OUT/regression.log" 2>&1
grep -E "^===== |Wins: |Solver engagement|SOLVER NEVER ENGAGED|Run [0-9]+: (TIMEOUT|ERROR|HANG)" "$OUT/regression.log"
```

Expected: 5 个角色全部 `Completed: 5/5`，`Solver engagement` 全部 100%，没有 `TIMEOUT`/`ERROR`/`HANG` 行。**如果出现 `HANG` 行**：看门狗在按设计工作（BUG-040 又出现了），但门槛是 `Completed: 5/5`，HANG 不算完成——在报告里如实记录，用同一个种子重跑那一局确认是偶发还是必现，**不要**当成通过。

- [ ] **Step 2: 确认遥测在所有角色上都有值**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python - <<'EOF'
import glob, json, os, time
recent = [p for p in glob.glob("logs/*.jsonl") if time.time() - os.path.getmtime(p) < 6 * 3600]
by_char = {}
for p in recent:
    ch = os.path.basename(p).split("_")[2]
    for line in open(p):
        e = json.loads(line) if line.strip() else {}
        d = e.get("data") or {}
        if e.get("type") == "state" and d.get("type") == "combat_plan":
            s = d.get("search") or {}
            by_char.setdefault(ch, []).append(s.get("budget_ms"))
for ch, v in sorted(by_char.items()):
    print(ch, len(v), "plans, budget_ms values:", sorted(set(v)))
EOF
```

Expected: 5 个角色都有 plan，`budget_ms` 全部是 `[120000]`。

---

#### Task 5 实测结果（2026-09-23）

回归目录 `~/.sts2-train/phase2b1_gate_20260923_185946/`。5 个角色全部 `Completed: 5/5`，solver 参与率 100%（Ironclad 260/260、Silent 268/268、Defect 238/238、Regent 164/164、Necrobinder 289/289），没有 TIMEOUT / ERROR / HANG。

**本计划的改动在默认 120 s 下完全不改变 solver 的行为。** 25 局逐局对比 Phase 2 的回归（`~/.sts2-train/phase2_gate_20260921_165327/`），seed、步数、act、floor 全部一致，solver 调用次数也逐角色相同。这是 Task 6 复用 Phase 2 的 120 s 数据作基线的直接依据。（两边都是 8 线程、串行跑；Phase 2 A/B 的 ON 臂是 3 线程并发，这里没有直接覆盖到那个配置。）

**遥测第一次给出了"搜索为什么停"：绝大多数没收敛的搜索是撞了节点上限，而不是时间上限。**

| 角色 | 撞节点上限 `NodeLimit` | 撞时间上限 `TimeLimit` | 最长一次搜索 |
|---|---|---|---|
| Necrobinder | 61 / 289 | 2 | 120.2 s |
| Regent | 14 / 164 | 0 | 85.2 s |
| Ironclad | 7 / 344（含 Task 1 的两局验证对局，各 42 次） | 0 | 75.4 s |
| Defect | 2 / 238 | 0 | 73.1 s |
| Silent | 0 / 316（含 Task 4 的一局冒烟对局） | 0 | 29.4 s |

对档位研究的含义：在 120 s 下，没跑完的搜索主要是被 `MaxExpandedNodes = 120 000` 卡住的，所以**把时间加到 180/300 s 大概率帮不上什么**。唯一可能起作用的路径是 `EscalateSearchWhenNoVictory`：找不到胜利路线时，它会把节点上限翻倍重搜，但前提是剩余时间放得下预计耗时。预算更长就能多做几轮这种升级。阶段 B 的 `total_expanded_nodes` 遥测能直接看出升级有没有发生。这也提示：比起时间，**节点上限可能是更值得研究的杠杆**。这一点留给 Task 6 出结果后再定，不在本计划里临时加测。

### Task 6: 档位配对评测

**基线复用 Phase 2 的 ON 臂**（`~/.sts2-train/phase2_ab_20260922_102622/<角色>_on_global.jsonl`：120 s、`STS2_SOLVER_THREADS=3`、种子 `run_1..run_40`、全局层数口径）。复用成立的前提是：本计划没有改变 solver 在 120 s 下的行为——Task 1 的遥测只在 `Solve` 返回后读字段，`STS2_SOLVER_BUDGET` 未设置时 `BudgetOverrideMilliseconds` 仍是 `null`，看门狗不参与出牌。**如果执行过程中还改了任何影响 solver 出牌的东西，就必须同样条件重跑一遍 120 s 基线，不能复用。**

**两个阶段并发跑（6 条 lane × 3 线程 = 18 线程）：**
- **阶段 A — 30 s，全部 5 个角色**：回答"训练能不能用 30 s"。这是非劣效问题。
- **阶段 B — 300 s，只测 Necrobinder**：回答"多给时间有没有用"。只有 Necrobinder 在 120 s 下还有被截断的搜索（6.9%），其余四个是 0%。

6 条 lane 比基线的 5 条多一条，CPU 争用略高。这对两个阶段都是**保守**的偏差：会让阶段 A 的 30 s 更难被判为"不劣"，让阶段 B 的 300 s 更难被判为"更好"。所以如果结论是正向的，它是稳的。

**预计耗时**：Necrobinder 300 s 那条最长。基线里 Necrobinder 的 40 局 ON 臂就跑了约 15.7 小时，300 s 只会更长，保守估 16–20 小时。

- [ ] **Step 1: 写启动脚本**

创建 `/tmp/sts2-cli/phase2b1_tiers.sh`（一次性脚本，按 CLAUDE.md 约定放 `/tmp/sts2-cli/`）：

```bash
#!/bin/bash
# Phase 2b-1 Task 6: solver budget tiers, paired against the Phase 2 120 s ON arms.
# Stage A: 30 s for all five characters. Stage B: 300 s for Necrobinder only.
# STS2_SOLVER_THREADS=3 matches the baseline arms; 6 lanes x 3 = 18 threads.
set -u
REPO=/Users/bytedance/mygit/sts2-cli
OUT="$1"
SEEDS=40
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
export STS2_SOLVER_THREADS=3
cd "$REPO"

lane() {   # $1 character, $2 budget seconds
    STS2_SOLVER_CHARS="$1" STS2_SOLVER_BUDGET="$2" \
        .venv/bin/python -u python/play_full_run.py "$SEEDS" "$1" \
        --results-log "$OUT/${1}_b${2}.jsonl" > "$OUT/${1}_b${2}.log" 2>&1
    echo "[$(date +%H:%M:%S)] $1 budget=${2}s done" >> "$OUT/progress.txt"
}

for c in Ironclad Silent Defect Regent Necrobinder; do lane "$c" 30 & done
lane Necrobinder 300 &
wait
echo "[$(date +%H:%M:%S)] ALL LANES DONE" >> "$OUT/progress.txt"
```

- [ ] **Step 2: 启动**

```bash
cd /Users/bytedance/mygit/sts2-cli
T=~/.sts2-train/phase2b1_tiers_$(date +%Y%m%d_%H%M%S); mkdir -p "$T"
echo "$T" > /tmp/sts2-cli/last_tier_dir.txt
cp /tmp/sts2-cli/phase2b1_tiers.sh "$T/"
nohup caffeinate -i bash "$T/phase2b1_tiers.sh" "$T" > "$T/driver.log" 2>&1 &
sleep 120; ps aux | grep -c "[S]ts2Headless"; ls "$T"/*.jsonl | wc -l
```

Expected: 6 个 `.jsonl` 文件已创建，有 6 组引擎进程在跑。

看门狗现在在 harness 里，挂死只会让那一局变成 HANG，lane 会继续跑下去。但仍要定期检查 `progress.txt`，并检查每条 lane 的 `.log` 是否还在增长——看门狗本身也可能有没测到的毛病。

- [ ] **Step 3: 出配对报告**（全部 lane 跑完后）

```bash
cd /Users/bytedance/mygit/sts2-cli
BASE=~/.sts2-train/phase2_ab_20260922_102622
T=$(cat /tmp/sts2-cli/last_tier_dir.txt)
for c in Ironclad Silent Defect Regent Necrobinder; do
    echo "################ $c: 120 s (baseline) vs 30 s"
    .venv/bin/python -m agent.paired_eval "$BASE/${c}_on_global.jsonl" "$T/${c}_b30.jsonl" \
        --label-a 120s --label-b 30s | grep -E "pairs used|^floor |^win "
done
echo "################ Necrobinder: 120 s (baseline) vs 300 s"
.venv/bin/python -m agent.paired_eval "$BASE/Necrobinder_on_global.jsonl" "$T/Necrobinder_b300.jsonl" \
    --label-a 120s --label-b 300s | grep -E "pairs used|^floor |^win "
```

grep 里**必须保留 `^win `**：只按 `^floor` 过滤会漏掉赢局——这个坑记在项目记忆里，出过事。

- [ ] **Step 4: 出遥测报告**

创建 `/tmp/sts2-cli/tier_telemetry.py`：

```python
"""Per-call solver telemetry from game logs, grouped by (character, budget).

Reads the `search` dict every combat_plan reply carries since Phase 2b-1
Task 1. Two views of "was this search cut short", because they disagree by
design:
  - boundary == "TimeLimit": the engine's own label, but it is only set when
    the chosen line had no natural stopping point of its own (a Shuffle or
    PendingChoice boundary wins over TimeLimit, CombatBeamSolver.Phases.cs:
    474-484), so it UNDER-counts time-capped searches;
  - elapsed >= budget - 1 s: wall clock, catches every search that ran to the
    budget however its best line ended.
Usage: tier_telemetry.py <since YYYYmmdd_HHMMSS>
"""
import glob
import json
import os
import statistics
import sys
from collections import defaultdict

since = sys.argv[1]
rows = defaultdict(list)   # (character, budget_s) -> [(elapsed_ms, boundary)]
for path in glob.glob("logs/*.jsonl"):
    name = os.path.basename(path)
    if name[:15] < since:
        continue
    character = name.split("_")[2]
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = entry.get("data") or {}
        if entry.get("type") != "state" or data.get("type") != "combat_plan":
            continue
        search = data.get("search")
        if search:
            rows[(character, search["budget_ms"] // 1000)].append(
                (search["elapsed_ms"], search["boundary"]))

print(f"{'character':<12} {'budget':>6} {'solves':>7} {'median':>8} {'mean':>8} "
      f"{'TimeLimit':>10} {'ran to budget':>14} {'NodeLimit':>10}")
for (character, budget), v in sorted(rows.items()):
    elapsed = [x for x, _ in v]
    n = len(v)
    time_limit = sum(1 for _, b in v if b == "TimeLimit") / n * 100
    node_limit = sum(1 for _, b in v if b == "NodeLimit") / n * 100
    ran_to_budget = sum(1 for x in elapsed if x >= budget * 1000 - 1000) / n * 100
    print(f"{character:<12} {budget:>5}s {n:>7} {statistics.median(elapsed) / 1000:>7.2f}s "
          f"{statistics.fmean(elapsed) / 1000:>7.2f}s {time_limit:>9.1f}% "
          f"{ran_to_budget:>13.1f}% {node_limit:>9.1f}%")
```

运行（参数是 `T` 目录名里的时间戳，只统计本次评测开始之后的日志）：

```bash
cd /Users/bytedance/mygit/sts2-cli
T=$(cat /tmp/sts2-cli/last_tier_dir.txt)
.venv/bin/python /tmp/sts2-cli/tier_telemetry.py "$(basename "$T" | sed 's/phase2b1_tiers_//')"
```

- [ ] **Step 5: 按判据下结论**

paired_eval 输出每个角色 `floor` 的配对差值 `diff` 和标准误 `se`。95% 置信区间下界 = `diff − 1.96 × se`。

**阶段 A（30 s vs 120 s），逐角色：**
- **下界 ≥ −1.0 层** → 30 s 对该角色**不劣**（非劣效界值定为 1 层：相对 solver 本身 +4.7 到 +10.5 层的收益，1 层算小）。
- **diff < 0 且 p < 0.05** → 30 s **伤害**该角色。该角色的训练档位不能用 30 s，在报告里写成"该角色在 Phase 2b-2 需要测 60 s"。
- **其余情况**（下界 < −1.0 但不显著）→ **样本不够，判不了**。该角色的 30 s 臂扩到 120 个种子重判（基线同样要扩到 120 个种子：同样条件、`STS2_SOLVER_BUDGET` 不设），**不要**当成"没差异"放过。

**阶段 B（Necrobinder，300 s vs 120 s）：**
- **diff > 0 且 p < 0.05** → 多给时间对 Necrobinder 有用。2b-2 要考虑按角色设档位，并补测 180 s 找拐点。
- **否则** → 超过 120 s 的档位没有测得出来的收益。结合遥测看原因：如果 300 s 臂"跑满预算"的比例很低，或者 `NodeLimit` 占比高，说明是节点上限先卡住，多给的时间根本没被用上。

**每个角色都要写结论，包括"不劣"的那些。**

- [ ] **Step 6: 把结果写进本文件**

在本 Task 下方新增"档位评测结果（实测）"小节。逐角色、逐档位列出：有效配对数、floor 配对差值、se、95% 下界、p、遥测（中位/均值耗时、TimeLimit、跑满预算、NodeLimit 的占比）、结论。最后写一句**给 2b-2 的建议**：训练用哪一档（按角色列），评测用哪一档。

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/plans/2026-09-23-combatsolver-phase2b1-budget-tiers.md
git commit -m "docs: Phase 2b-1 budget-tier A/B results and the tier to train with

<阶段 A 每角色的 diff / 95% 下界 / 结论；阶段 B Necrobinder 的 diff / p / 结论；
给 2b-2 的档位建议>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

#### 档位评测结果（实测，2026-09-24）

目录 `~/.sts2-train/phase2b1_tiers_20260923_220516/`；基线是 Phase 2 的 ON 臂 `~/.sts2-train/phase2_ab_20260922_102622/<角色>_on_global.jsonl`。6 条 lane、每条 3 线程，共 240 局，2026-09-23 22:05 开始，2026-09-24 11:46 结束。

**阶段 A：30 s 对比 120 s（非劣效界值 1 层，95% 下界 = diff − 1.96·se）**

| 角色 | 有效配对 | 120 s → 30 s（全局层数） | diff | se | 95% 下界 | p | 结论 |
|---|---|---|---|---|---|---|---|
| Ironclad | 38 | 17.24 → 16.97 | −0.26 | 0.159 | −0.58 | 0.10 | **不劣** |
| Silent | 40 | 14.65 → 14.50 | −0.15 | 0.231 | −0.60 | 0.52 | **不劣** |
| Defect | 37 | 22.08 → 22.78 | +0.70 | 0.424 | −0.13 | 0.10 | **不劣** |
| Necrobinder | 40 | 13.90 → 13.98 | +0.08 | 0.337 | −0.59 | 0.82 | **不劣** |
| Regent | 39 | 16.95 → 17.10 | +0.15 | 0.835 | **−1.48** | 0.85 | **判不了**：点估计为正，但方差是其它角色的 2–4 倍，置信区间越过了 −1 层。按预先定的判据，需要两臂都扩到 120 个种子重判 |

两臂都没有赢局（`win` 行全为 0）。无效配对的来源：
- **基线**：Ironclad 2 局 BUG-040 挂死；Defect 2 局、Regent 1 局 `plan_combat_turn_execution_failed`（BUG-042）。
- **30 s 臂**：Ironclad `run_32` 1 局被看门狗击杀（BUG-040，见下）；Defect `run_12`/`run_14`/`run_27` 3 局、Regent `run_6` 1 局 `plan_combat_turn_execution_failed`（BUG-042）。

其中 Defect `run_12`（125 步）、`run_27`（103 步）和 Regent `run_6`（123 步）跟基线里失败的是**同一批种子，失败时的步数也完全相同**。也就是说 **BUG-042 同样可以稳定复现**，只有 Defect `run_14` 是新出现的。这几行 ERROR 仍然显示 `act=None floor=None`，也就是 BUG-042 的后半部分，还没修。

**阶段 B：Necrobinder 300 s 对比 120 s**：13.90 → 14.33，diff +0.43，se 0.258，p = 0.099。**没有测得出来的收益。**

**遥测**（`search` 字段，逐次搜索）：

| 角色 | 档位 | 搜索次数 | 中位数 | 均值 | TimeLimit | 跑满预算 | NodeLimit | 平均展开节点 |
|---|---|---|---|---|---|---|---|---|
| Defect | 30 s | 2328 | 6.79 s | 9.11 s | 1.8% | 4.1% | 0% | 15 645 |
| Ironclad | 30 s | 1970 | 5.26 s | 8.00 s | 1.7% | 3.5% | 0% | 15 137 |
| Necrobinder | 30 s | 2117 | 7.90 s | 12.06 s | 19.2% | **21.6%** | 0% | 21 371 |
| Necrobinder | 300 s | 2124 | 5.43 s | 23.03 s | 0.8% | 0.9% | **18.0%** | 41 942 |
| Regent | 30 s | 1933 | 10.67 s | 12.89 s | 8.4% | 12.2% | 0% | 22 005 |
| Silent | 30 s | 1829 | 10.00 s | 11.68 s | 1.2% | 4.4% | 0% | 22 295 |

- **30 s 截断了 Necrobinder 五分之一的搜索，棋力却没有可测的下降**（+0.08 层）。
- **300 s 下，节点上限取代时间成了瓶颈**：只有 0.9% 的搜索用满时间，18% 撞节点上限；平均展开节点翻倍（21k → 42k），棋力却没有显著提升。和 Task 5 的预测一致：**多给的时间被节点上限浪费掉了**。
- "跑满预算"的比例比 `TimeLimit` 高（比如 Necrobinder 21.6% 对 19.2%），印证了 Task 1 注释里说的：`boundary` 会少算被时间截断的搜索。

**实际省下的时间**：用游戏日志时间戳计算，两边都是 3 线程、5–6 条 lane 并行，条件可比。

| 角色 | 120 s 下平均每次搜索 | 30 s 下 | 省下 |
|---|---|---|---|
| Necrobinder | 26.67 s | 12.06 s | 55% |
| Regent | 23.64 s | 12.90 s | 45% |
| Silent | 17.59 s | 11.69 s | 34% |
| Defect | 13.46 s | 9.12 s | 32% |
| Ironclad | 9.16 s | 8.00 s | 13% |

这比写计划时用单进程 8 线程数据推算的 27% 多得多：批量跑时 3 线程加上争用，搜索本来就更慢，被 30 s 截断的就更多。

**看门狗第一次实战**：Ironclad `run_32` 在 A1F17（boss）挂死，90 s 后被杀，只损失了这一局。**这和 Phase 2 A/B 里 BUG-040 的第二次挂死是同一个种子、同一场 boss 战**。那次是 120 s 预算，这次是 30 s，所以**BUG-040 在这个种子上可以稳定复现**，不是偶发（与 Task 5 证明的"solver 行为是确定的"一致）。

**给 Phase 2b-2 的建议**

- **训练用 30 s**（Ironclad、Silent、Defect、Necrobinder）：不劣，而且批量条件下省 13–55% 的搜索时间。**Regent 暂用 120 s**，直到扩样本给出结论。
- **评测维持 120 s**：这是 Phase 2 验证过的档位。
- **不用 180/300 s**：在现在的节点上限下，多给的时间用不上。
- **下一个值得研究的杠杆是节点上限 `MaxExpandedNodes`**，不是时间预算。
- **BUG-040 现在有稳定复现**：Ironclad `run_32`，`STS2_SOLVER_THREADS=3`，打到 A1F17 就会挂死。可以用来查根因。

### Task 7: 文档同步

**Files:**
- Modify: `CLAUDE.md`（Protocol notes 里 solver 相关的几条）
- Modify: `agent/bug.md`（BUG-040）
- Modify: `docs/superpowers/specs/2026-09-17-combatsolver-port-design.md`（Phase 2b 段落）

- [ ] **Step 1: `CLAUDE.md`**

在 `python/play_full_run.py` 那条（提到 `STS2_SOLVER_CHARS` / `STS2_SOLVER_THREADS` 的那条）里，加上 `STS2_SOLVER_BUDGET`：只接受 `30/60/120/180/300`（秒），不设就是 120；档位表不在列表里会直接报错；每个 `combat_plan` 回复都带 `search.{budget_ms, elapsed_ms, boundary, expanded_nodes, total_expanded_nodes}`；`boundary == "TimeLimit"` 会少算被时间截断的搜索（原因见 Task 1 Step 3 的注释）。

把讲 BUG-040 的那条改成：`play_full_run.py` 现在通过 `python/engine_process.py` 给每次回复设超时（`plan_combat_turn` 是预算 + 60 s，其它 120 s），超时就杀掉整个引擎进程组，这一局记为 `HANG`（paired_eval 状态 `stuck`，不参与配对）；根因仍未解决；**其它直接起引擎子进程的地方（`tests/conftest.py`、`agent/sts2_bridge.py`）还没有这个保护**。

- [ ] **Step 2: `agent/bug.md` BUG-040**

保持 `[OPEN]`（根因没修），在条目里加一条 `- **Mitigation (2026-09-23)**:`，写明看门狗的机制、超时值、HANG 状态，以及 Task 5 回归和 Task 6 评测里它实际触发了几次（从各 lane 日志里 `grep -c "ENGINE HANG"` 统计）。

- [ ] **Step 3: 设计文档**

把 "Phase 2b — 接进 RL 路径，再退休旧规划器（未开始）" 拆成：
- **Phase 2b-1 — 时间档位 + 看门狗（已完成）**：一句话结论 + 指向本计划。
- **Phase 2b-2 — 接进 RL 路径，再退休旧规划器（未开始）**：保留原来的三条要点。把其中"前置条件：先有单次调用看门狗"那条改成"harness 侧已有（Phase 2b-1），接进 `combat_env` 时要用同样的 `EngineProcess` 机制——`combat_env` 自己管理引擎子进程（`agent/combat_env.py:3206`，已经是 `start_new_session=True`），得在那里加上等价的超时"，再补一条"训练档位用 Phase 2b-1 的结论"。

- [ ] **Step 4: 构建 + 测试**

```bash
cd /Users/bytedance/mygit/sts2-cli
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | grep -E "Error\(s\)"
.venv/bin/python -m pytest tests/test_solver_gate.py tests/test_engine_process.py tests/test_plan_combat_turn_resolution.py tests/test_map_route_determinism.py -q 2>&1 | tail -1
```

Expected: `0 Error(s)`；全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md agent/bug.md docs/superpowers/specs/2026-09-17-combatsolver-port-design.md
git commit -m "docs: Phase 2b-1 -- budget tiers, watchdog, and what 2b-2 inherits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase 2b-1 完成的判定标准

- [ ] `STS2_SOLVER_BUDGET` 在 C# 和 Python 两侧都只接受 30/60/120/180/300；不设就是 120；两侧不一致时对局会大声失败。
- [ ] 每个 `combat_plan` 回复都带 `search` 遥测，而且没有碰 `progressCallback`。
- [ ] 看门狗有单测覆盖（挂死 → `EngineHang`，按进程组杀，引擎退出 → EOF 而不是挂死，正常关闭不留孤儿），也有 `play_run` 集成测试覆盖。
- [ ] 完整回归门槛（默认档位）5 角色全部 `Completed: 5/5`、solver 参与率 100%。
- [ ] 阶段 A 5 个角色、阶段 B Necrobinder **每一个**都有配对数字、遥测和结论写进本文件，并给出 2b-2 该用的档位。
- [ ] `CLAUDE.md`、`agent/bug.md`、设计文档与代码实际行为一致。
