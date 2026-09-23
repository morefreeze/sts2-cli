# Combat Solver 引擎移植 Phase 2 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `plan_combat_turn`（Phase 1 移植进来的 Combat Solver 搜索引擎）在全部 5 个角色上跑通并解除 `character == "Ironclad"` 的硬 gate，同时用**配对评测**证明它对每个角色都不比现有启发式差——而不是只证明"不崩"。

**Architecture:** Phase 1 已经把整条链路打通（`RunSimulator.DoPlanCombatTurn` → `CombatRootSnapshot.Capture` → `CombatSearchCoordinator.Solve` → JSON 计划 → `python/combat_plan_driver.py` 解析执行）。本阶段**不新增引擎能力**：把 `python/play_full_run.py` 里写死的 `if character == "Ironclad"` 换成一个可配置开关，逐角色冒烟、修胶水层暴露的问题、解除 gate、再用 `agent/paired_eval.py` 做每角色 solver-on vs solver-off 的配对对比。

**Tech Stack:** Python 3.11（`.venv/bin/python`）、pytest、C# / .NET 9（仅在冒烟暴露引擎侧问题时才改 `src/Sts2Headless/RunSimulator.cs`）。

---

## 范围决定（写计划时核对代码后做的修正，必须先读）

原设计文档 [2026-09-17-combatsolver-port-design.md](../specs/2026-09-17-combatsolver-port-design.md) 把 Phase 2 写成"全角色接入 + 删除 `agent/sim`、`agent/turn_planner.py` 以及调用分支"。**实际代码状态与这句话不符，本计划据此收窄范围**：

1. **`plan_combat_turn` 目前只接在 `python/play_full_run.py`（回归 harness）里**，真正的 agent 路径 `agent/combat_env.py` / `agent/eval_rl.py` 从来没调用过它（`grep -rn "plan_combat_turn" agent/` 为空）。所以"全量替换 `combat_play` 决策的来源"不是"删掉一个 gate"，而是另一件需要独立验证的工作。
2. **`agent/turn_planner.py` 早就不只是 1 回合 DFS**。它同时是这些东西的宿主，且全部有现役调用方：
   - `defense_override_enabled` / `intent_defense_override` / `hallway_danger_threshold` / `elite_danger_threshold` —— 走廊格挡阈值是**已上线、实测有收益**的改动（5 个角色 +0.45..+1.47 层）。
   - `apply_vantom_slippery_mask`（`agent/combat_env.py:1409`）。
   - 调用方：`agent/decision_advisor.py:12`、`agent/rl_agent.py:5`、`agent/eval_rl.py:22,761,783`、`agent/combat_env.py:1409,1419`、`agent/boss_retry.py:108`；`agent/sim` 还被 `agent/card_scoring.py:1988`（`rollout_recursive`）使用。

   直接 `rm agent/turn_planner.py` 会连带删掉一个已验证的收益来源。

**因此本计划的范围是**：全角色验证 + 解除 gate + 质量度量。**不包含**：把 solver 接进 `combat_env`/`eval_rl`、以及退休 `agent/sim`/`turn_planner.py`——这两件事留给 Phase 2b（前置条件是本计划 Task 6 的配对评测通过，否则"替换"本身就没有依据）。执行本计划期间不要顺手删任何 `agent/sim`/`turn_planner` 的代码。

## 已知风险清单（冒烟时对照着看，不是预言）

| 风险 | 为什么担心 | 冒烟时怎么看出来 |
|---|---|---|
| 角色机制未覆盖 | `_resolve_card_index`/`_resolve_potion_index`/`_apply_action_choices` 只在 Ironclad 上跑过；Defect 充能球、Silent 小刀/中毒、姿态、Necrobinder 随从都没验过 | 日志里 `!! plan_combat_turn:` 开头的行、或 `plan_combat_turn_execution_failed` |
| vendor 引擎问题 8（斩杀后仍施加 debuff → 误触发联动） | Phase 1 已诊断未修，靠"计划里的牌找不到 → 重新规划"规避。其它角色的同类卡（如 Silent 的伤害+中毒）可能更频繁触发 | 日志里 `plan drifted` / 重新规划频次异常高；单局步数显著高于 Ironclad |
| `card_occurrence` 在手牌重排后的计数基准 | Phase 1 遗留的未测风险；Silent 的弃牌/消耗流派最容易触发 | 打出的牌与计划不符，或引擎回 `Card could not be played` |
| 搜索预算 | 原 mod 面向 16GB 单机大内存；批量跑会不会成为瓶颈从未实测 | 单局墙钟时间、内存占用 |

> 只有 `PlayCard`/`UsePotion`/`EndTurn` 三种 `PlanActionKind`（`CombatSolverEngine/Search/CombatPlan.cs:9-14`），三种都已序列化，所以**不预期**出现"未知 action kind"——真出现了就是引擎版本变了，按 Task 3 的判定表处理。

---

### Task 1: 把 Ironclad gate 换成可配置开关（行为不变）

目的：后面每个角色的冒烟都不需要改代码/改 diff 就能跑，同时 Task 4 解除 gate 时只改一个默认值。

**Files:**
- Modify: `python/play_full_run.py`（新增模块级函数 `solver_characters()`，改 `play_run()` 里 240-260 行那段 gate）
- Test: `tests/test_solver_gate.py`（新建）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_solver_gate.py`：

```python
"""Which characters route combat through plan_combat_turn.

The gate used to be a hard-coded `character == "Ironclad"` (Phase 1 scoped
itself to one character). Phase 2 makes it configurable so each character can
be smoke-tested without editing code, and so lifting the gate is a one-line
default change.
"""
import sys
import os

# Same sys.path pattern tests/test_plan_combat_turn_resolution.py uses.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
import play_full_run


def test_default_is_ironclad_only():
    assert play_full_run.solver_characters({}) == {"Ironclad"}


def test_env_var_overrides_default():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "Defect"}) == {"Defect"}


def test_env_var_accepts_comma_separated_list_and_strips_space():
    assert play_full_run.solver_characters(
        {"STS2_SOLVER_CHARS": "Ironclad, Silent ,Defect"}
    ) == {"Ironclad", "Silent", "Defect"}


def test_all_keyword_enables_every_valid_character():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "all"}) == set(
        play_full_run.VALID_CHARACTERS
    )


def test_none_keyword_disables_the_solver_entirely():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "none"}) == set()


def test_empty_value_falls_back_to_the_default():
    assert play_full_run.solver_characters({"STS2_SOLVER_CHARS": "  "}) == {"Ironclad"}


def test_unknown_character_raises_rather_than_silently_disabling():
    # A typo must not look like "solver quietly off for this character" --
    # that would make a whole regression run measure the wrong thing.
    try:
        play_full_run.solver_characters({"STS2_SOLVER_CHARS": "Ironcladd"})
    except ValueError as exc:
        assert "Ironcladd" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown character name")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q
```

Expected: FAIL，`AttributeError: module 'play_full_run' has no attribute 'solver_characters'`。

- [ ] **Step 3: 实现 `solver_characters()`**

在 `python/play_full_run.py` 的 `VALID_CHARACTERS = [...]` 定义之后加入：

```python
# Which characters route their combat_play decisions through the ported
# Combat Solver (plan_combat_turn) instead of the simple one-card-at-a-time
# heuristic. Phase 1 hard-coded this to Ironclad because the resolution glue
# (_resolve_card_index / _resolve_potion_index / _apply_action_choices) had
# only ever been exercised against Ironclad's mechanics; Phase 2 validates the
# other four one at a time via STS2_SOLVER_CHARS before changing this default.
_SOLVER_CHARS_DEFAULT = frozenset({"Ironclad"})


def solver_characters(env=None) -> set:
    """Resolve the set of characters allowed to call plan_combat_turn.

    STS2_SOLVER_CHARS accepts a comma-separated character list, "all", or
    "none". Unset/blank means the default. An unrecognized name raises rather
    than being dropped: a typo that silently disabled the solver would make a
    whole regression or A/B run measure the opposite of what it claims to.
    """
    env = os.environ if env is None else env
    raw = (env.get("STS2_SOLVER_CHARS") or "").strip()
    if not raw:
        return set(_SOLVER_CHARS_DEFAULT)
    if raw.lower() == "all":
        return set(VALID_CHARACTERS)
    if raw.lower() == "none":
        return set()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    unknown = [n for n in names if n not in VALID_CHARACTERS]
    if unknown:
        raise ValueError(
            f"STS2_SOLVER_CHARS names unknown character(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(VALID_CHARACTERS)}"
        )
    return set(names)
```

确认 `python/play_full_run.py` 顶部已经 `import os`（有的话不要重复 import）。

- [ ] **Step 4: 改 `play_run()` 里的 gate**

把 `python/play_full_run.py` 中这两行：

```python
                if character == "Ironclad":
                    plan = send({"cmd": "action", "action": "plan_combat_turn"})
                else:
                    plan = {"type": "error"}
```

替换成：

```python
                if character in solver_chars:
                    plan = send({"cmd": "action", "action": "plan_combat_turn"})
                else:
                    plan = {"type": "error"}
```

并在 `play_run()` 函数体开头（`rng = random.Random(seed)` 那一行附近）加上：

```python
    solver_chars = solver_characters()
```

同时把那段 gate 上方的注释里"Phase 2 lifts this gate once the other 4 characters are actually regression-tested against this path."改成：

```python
                # Which characters take this path is resolved by
                # solver_characters() (STS2_SOLVER_CHARS); see
                # docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md.
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py tests/test_plan_combat_turn_resolution.py tests/test_map_route_determinism.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 确认默认行为没变（Ironclad 仍走 solver，其它角色仍不走）**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
.venv/bin/python -u python/play_full_run.py 1 Ironclad 2>&1 | grep -c "plan_combat_turn"
.venv/bin/python -u python/play_full_run.py 1 Silent 2>&1 | grep -c "plan_combat_turn"
```

Expected: 第一条 > 0（Ironclad 仍在调 solver），第二条 = 0（Silent 仍走旧启发式）。

- [ ] **Step 7: Commit**

```bash
git add python/play_full_run.py tests/test_solver_gate.py
git commit -m "refactor: make the plan_combat_turn character gate configurable

Phase 2 validates the other four characters one at a time; hard-coding
the gate to Ironclad meant every smoke run needed a code edit. Default is
unchanged (Ironclad only). An unknown name in STS2_SOLVER_CHARS raises
instead of silently disabling the solver, so a typo can't make a whole
A/B run measure the opposite of what it reports."
```

---

### Task 2: 四个角色逐个冒烟

**Files:**
- 不改代码。产出是 `/tmp/sts2-cli/phase2_smoke_<character>.log` 四份日志 + 一段归类结论。

- [ ] **Step 1: 逐角色各跑 3 局**

一次只开一个角色，避免一份日志里混着两种机制的失败：

```bash
cd /Users/bytedance/mygit/sts2-cli
mkdir -p /tmp/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
for char in Silent Defect Regent Necrobinder; do
    STS2_SOLVER_CHARS="$char" scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py 3 "$char" \
        > "/tmp/sts2-cli/phase2_smoke_${char}.log" 2>&1
    echo "=== $char ==="
    grep -E "Wins:|Run [0-9]+: (TIMEOUT|ERROR)" "/tmp/sts2-cli/phase2_smoke_${char}.log"
done
```

Expected（**不是要求**，是观察项）：每个角色 `Completed: 3/3`。达不到是正常的——这正是本任务要找的东西。

- [ ] **Step 2: 把每个角色的失败归类**

对每一份日志跑：

```bash
for char in Silent Defect Regent Necrobinder; do
    echo "=== $char ==="
    grep -E "!! plan_combat_turn|plan_combat_turn_execution_failed|Card could not be played|engine_error|STUCK" \
        "/tmp/sts2-cli/phase2_smoke_${char}.log" | sort | uniq -c | sort -rn | head
done
```

把结果按下面的类别记下来（写进本文件的 Task 3 下方，作为执行记录）：

- **A 类：胶水层解析失败** —— `!! plan_combat_turn: ...`（card_id/potion_id/choice 解析不出来）。
- **B 类：引擎拒绝动作** —— `Card could not be played` / `Invalid potion index` / `target_index is required`。
- **C 类：solver 自身返回 error** —— `plan_combat_turn` 返回 `{"type": "error"}`，日志里能看到紧接着回落旧启发式。
- **D 类：卡死/超时** —— `STUCK` / `TIMEOUT`。
- **E 类：只是打得差** —— 全部 `Completed`，但 `avg_floor` 明显低于同角色 solver-off 基线（这不是 bug，交给 Task 6 的配对评测判定）。

- [ ] **Step 3: 记录每角色的单局墙钟时间（搜索预算风险项）**

```bash
for char in Silent Defect Regent Necrobinder; do
    echo -n "$char steps-per-run: "
    grep -oE "steps=[0-9]+" "/tmp/sts2-cli/phase2_smoke_${char}.log" | tr '\n' ' '; echo
done
```

再对照 Ironclad 的同口径数字（`STS2_SOLVER_CHARS=Ironclad .venv/bin/python -u python/play_full_run.py 3 Ironclad`）。如果某个角色的单局耗时是 Ironclad 的 3 倍以上，在 Task 3 的记录里单独标出来——那是搜索预算问题，不是解析问题。

- [ ] **Step 4: 不提交代码，只把结论写进本文件**

在本文件 Task 3 标题下新增一个"冒烟结果（实测）"小节，逐角色写清楚：跑了几局、完成几局、命中哪些类别、对应日志行号。**不要只记录干净的那一跑**（Phase 1 的教训：5 局干净是假阳性，扩到 20 局就炸了 4 局）。

---

### Task 3: 按判定规则修复冒烟暴露的问题

冒烟具体会炸什么在跑之前无法穷举，所以这里给的是**判定规则**，和 Phase 1 Task 3 同一套做法。每修完一类，重跑该角色的 3 局冒烟确认这一类消失。

- [ ] **Step 1: 按类别对照处理**

| 现象 | 处理方式 |
|---|---|
| **A 类**：`!! plan_combat_turn: could not resolve card_id ... in hand` | 先按 Phase 1 问题 8 的结论判断：这是"solver 预测与真实状态分歧"（该牌根本没被抽到）还是"解析算法错"。前者已有规避（`_execute_combat_plan_actions` 返回 `ok=True` 重新规划），日志里应该**没有** `plan_combat_turn_execution_failed`；后者才是 bug。区分方法：从该局 `logs/*.jsonl` 里取出这一步的 `hand` 和计划的 `card_id`/`card_occurrence`，手动按 `python/combat_plan_driver.py` 的 `_resolve_card_index` 算一遍。 |
| **A 类**：`!! plan_combat_turn: expected card_select for choice <Effect>, got decision='...'` | 对照 `CombatSolverEngine/Search/CombatPlan.cs:16-42` 的 `PlanChoiceEffect` 全表确认这个 effect 的真实语义。Phase 1 已经确认"引擎在候选唯一时自动结算、不走 card_select 往返"是**合法**的（问题 3），`_apply_action_choices` 已对 `combat_play` 放行；如果这次 `decision` 是别的值（例如 `bundle_select`），先读 `RunSimulator.cs` 里该决策的产生条件，再决定是放行还是补一条解析分支——**不要**为了消掉报错直接放行所有 decision。 |
| **B 类**：`Card could not be played (still in hand after action)` | 这是 `DoPlayCard` 的事后校验（`can_play: true` 与运行时拒绝并不矛盾，见 `agent/bug.md` BUG-004/006/039）。先确认这张牌是不是角色特有机制（Defect 需要空充能球位、Silent 需要小刀在手等）。如果是"solver 认为能打、真实引擎不让打"，那是 vendor 引擎的模拟保真度问题：**按 Phase 1 问题 8 的先例处理**——诊断写进 `agent/bug.md`，在胶水层把它当成"计划过时"信号触发重新规划，不改 vendor 代码。 |
| **B 类**：`target_index is required` / `Invalid potion index` | 这两类 Phase 1 已经分别用 `target_combat_id`（问题 7）和 `potion_id`（问题 2）根治过。再次出现说明某条路径没走新解析函数——定位到 `python/combat_plan_driver.py` 里对应的 `_resolve_enemy_target_index`/`_resolve_potion_index` 调用点，而不是新写一套。 |
| **C 类**：`plan_combat_turn` 返回 error | 读返回里的 `message`/`stack_trace`。如果栈落在 `CombatRootSnapshot.Capture`，多半是该角色的某个字段在无头环境下为 null；如果落在 `CombatSearchCoordinator.Solve`，先确认不是 `only_death_routes_found` 被误当 error。 |
| **D 类**：STUCK/TIMEOUT | 先用 `logs/*.jsonl` 确认卡在哪个 decision。卡在 `combat_play` 且每步状态完全不变 → 计划为空或全部动作被跳过，检查 `plan.get("actions")`；卡在 `event_choice` → 是 Phase 1 已知的既有缺陷（Crystal Sphere 类），不在本计划范围，记录即可。 |
| **搜索预算**（Task 2 Step 3 标出来的角色） | 不要动 vendor 的搜索算法。`RunSimulator.cs` 里 `SearchPolicySnapshot` 的构造处（约 1148 行）有 `BudgetOverrideMilliseconds: null`，先实测把它设成一个具体毫秒数对该角色 avg_floor 的影响，再决定是否需要按角色分档——**这一步如果动了，必须在 Task 6 的配对评测里带上**。 |

- [ ] **Step 2: 每修一类，重跑该角色 3 局冒烟**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
STS2_SOLVER_CHARS="<角色>" .venv/bin/python -u python/play_full_run.py 3 "<角色>" 2>&1 | tail -12
```

Expected: 该类别的报错消失，`Completed: 3/3`。

- [ ] **Step 3: 每个修复单独 commit，并在 `agent/bug.md` 立条目**

`agent/bug.md` 的条目要写清：现象、根因定位到哪个文件哪一行、修的是胶水层还是引擎层、验证方式。格式对照最近的 BUG-039。

```bash
git add -A
git commit -m "fix: <一句话现象> (Phase 2 <角色> smoke)

<根因，落到具体文件:行>
<修在哪一层，为什么不是另一层>
<验证：跑了什么，结果是什么>"
```

---

### Task 4: 解除 gate 并跑完整回归

**Files:**
- Modify: `python/play_full_run.py`（`_SOLVER_CHARS_DEFAULT`）
- Modify: `tests/test_solver_gate.py`（默认值的那条测试）

- [ ] **Step 1: 先改测试（TDD）**

把 `tests/test_solver_gate.py` 里的：

```python
def test_default_is_ironclad_only():
    assert play_full_run.solver_characters({}) == {"Ironclad"}
```

改成：

```python
def test_default_is_every_character():
    # Phase 2 lifted the Ironclad-only gate after smoke-testing the other four
    # (docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md Task 2/3)
    # and confirming the paired evaluation in Task 6.
    assert play_full_run.solver_characters({}) == set(play_full_run.VALID_CHARACTERS)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q
```

Expected: FAIL，`assert {'Ironclad'} == {'Ironclad', 'Silent', ...}`。

- [ ] **Step 3: 改默认值**

`python/play_full_run.py`：

```python
_SOLVER_CHARS_DEFAULT = frozenset(VALID_CHARACTERS)
```

（保留上方注释，并把"Phase 2 validates the other four one at a time"改成"Phase 2 lifted this after per-character smoke + paired evaluation; STS2_SOLVER_CHARS=none turns the solver off for an A/B arm."）

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/ -q
```

Expected: 全部 PASS。（`tests/agent/test_eval_rl.py::test_eval_cli_requires_checkpoint_and_defaults_to_fixed_seeds` 是**既有**失败——committed 的 `--invalid-retries` 默认是 3、测试写的是 1，与本计划无关；如果它仍然是唯一的失败项，照原样放过并在 commit message 里点名。）

- [ ] **Step 5: 跑 CLAUDE.md 的完整回归门槛**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
OUT=~/.sts2-train/phase2_gate_$(date +%Y%m%d_%H%M%S); mkdir -p "$OUT"
scripts/run_caffeinated.sh bash -c 'for char in Ironclad Silent Defect Regent Necrobinder; do
    echo "===== $char ====="; .venv/bin/python -u python/play_full_run.py 5 "$char"; done' \
    > "$OUT/regression.log" 2>&1
grep -E "^===== |Wins: |Run [0-9]+: (TIMEOUT|ERROR)" "$OUT/regression.log"
```

Expected: 5 个角色全部 `Completed: 5/5`，没有 `TIMEOUT`/`ERROR` 行。**不达标就回到 Task 3**，不要带着 4/5 往下走。

> 注意：地图路线现在由 `random.Random(seed)` 锁定（commit `233ba34`），所以同一个种子可以真正复现失败——失败时直接用同种子重跑定位，不要再假设"换一跑就好了"。

- [ ] **Step 6: Commit**

```bash
git add python/play_full_run.py tests/test_solver_gate.py
git commit -m "feat: route all five characters' combat through the ported solver

Lifts the Phase 1 Ironclad-only gate after per-character smoke tests and
the fixes they produced. Full CLAUDE.md gate (5 games x 5 characters):
Completed 5/5 for every character. STS2_SOLVER_CHARS=none still turns the
solver off, which is what the paired A/B arm uses."
```

---

### Task 5: 让 `play_full_run.py` 能产出配对评测用的 JSONL

`agent/paired_eval.py` 吃的是每行一条 `{"seed", "status", "floor"}` 的 JSONL（`load_arm`，`agent/paired_eval.py:62`；有效状态集 `_VALID_STATUSES = {"win", "dead"}`，`agent/paired_eval.py:54`）。`play_full_run.py` 目前只打印 SUMMARY 文本，没法直接喂给它。

**Files:**
- Modify: `python/play_full_run.py`（新增 `result_to_eval_row()` + `--results-log` 参数）
- Test: `tests/test_solver_gate.py`（同一个文件追加，不新建）

- [ ] **Step 1: 写失败的测试**

在 `tests/test_solver_gate.py` 末尾追加：

```python
def test_result_row_maps_a_win_to_status_win():
    row = play_full_run.result_to_eval_row(
        {"victory": True, "seed": "run_1", "floor": 51, "act": 3, "steps": 200}, "Ironclad")
    assert row["seed"] == "run_1"
    assert row["status"] == "win"
    assert row["floor"] == 51
    assert row["character"] == "Ironclad"


def test_result_row_maps_an_ordinary_loss_to_status_dead():
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "run_2", "floor": 12, "act": 1, "steps": 90}, "Defect")
    assert row["status"] == "dead"


def test_result_row_maps_timeout_and_error_to_technical_statuses():
    # paired_eval only pairs {"win", "dead"}; everything else must land on a
    # name its _VALID_STATUSES check will drop, never on "dead" -- a failed run
    # counted as an ordinary death would silently bias the floor average.
    assert play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "timeout": True}, "Silent")["status"] == "timeout"
    assert play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "error": "engine_error: x"}, "Silent")["status"] == "crash"


def test_result_row_keeps_floor_none_rather_than_defaulting_to_zero():
    # A run that never reached a floor must not report floor 0 -- that would
    # read as "died on floor 0" in an average instead of "no data".
    row = play_full_run.result_to_eval_row(
        {"victory": False, "seed": "s", "timeout": True}, "Regent")
    assert row["floor"] is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q
```

Expected: FAIL，`AttributeError: module 'play_full_run' has no attribute 'result_to_eval_row'`。

- [ ] **Step 3: 实现**

在 `python/play_full_run.py` 的 `summarize()` 之前加入：

```python
def result_to_eval_row(result: dict, character: str) -> dict:
    """Convert one play_run() result into an agent/paired_eval.py input row.

    paired_eval pairs only rows whose status is "win" or "dead"
    (agent/paired_eval.py:54) and reads `floor` for the run-level metric
    (agent/paired_eval.py:287). Harness-internal failures therefore have to map
    onto names outside that set -- "timeout" for the STUCK/max-steps path and
    "crash" for an engine error -- so a run that never finished cannot be
    averaged in as if it were an ordinary death.
    """
    if result.get("victory"):
        status = "win"
    elif result.get("timeout"):
        status = "timeout"
    elif result.get("error"):
        status = "crash"
    else:
        status = "dead"
    floor = result.get("floor")
    return {
        "seed": result.get("seed"),
        "status": status,
        "floor": floor if isinstance(floor, (int, float)) else None,
        "act": result.get("act"),
        "steps": result.get("steps"),
        "character": character,
        "solver": sorted(solver_characters()),
    }
```

在 `main()` 的 argparse 里加：

```python
    parser.add_argument("--results-log", default=None,
                        help="Append one JSONL row per run, in agent/paired_eval.py's "
                             "input format (seed/status/floor). Use for A/B arms.")
```

并在 `main()` 的循环里，`results.append(result)` 之后加入：

```python
        if args.results_log:
            with open(args.results_log, "a") as fh:
                fh.write(json.dumps(result_to_eval_row(result, character)) + "\n")
```

确认文件顶部已 `import json`（没有就加）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -m pytest tests/test_solver_gate.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 端到端确认文件格式能被 paired_eval 读进去**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
rm -f /tmp/sts2-cli/fmt_check.jsonl
.venv/bin/python -u python/play_full_run.py 2 Ironclad --results-log /tmp/sts2-cli/fmt_check.jsonl > /dev/null 2>&1
cat /tmp/sts2-cli/fmt_check.jsonl
.venv/bin/python -c "
from agent.paired_eval import load_arm
print(load_arm('/tmp/sts2-cli/fmt_check.jsonl')['diagnostics'])
"
```

Expected: 两行 JSON；`load_arm` 打印的 diagnostics 里 `unique_seeds` = 2，且 `valid_seeds` 等于其中真正打到 `game_over` 的局数。

- [ ] **Step 6: Commit**

```bash
git add python/play_full_run.py tests/test_solver_gate.py
git commit -m "feat: emit paired_eval-compatible JSONL from play_full_run

Needed for the Phase 2 quality gate: 'it completes 5/5' says nothing about
whether the solver plays BETTER than the heuristic it replaces. Maps
harness-internal failures onto statuses outside paired_eval's
{win, dead} pairing set so an unfinished run can't be averaged in as a
death."
```

---

### Task 6: 每角色配对评测 solver-on vs solver-off

这是本计划真正的验收门槛。"跑完不崩"是 Task 4，"确实不更差"是这里。

**Files:**
- 不改代码。产出是 `~/.sts2-train/phase2_ab_<character>_{on,off}.jsonl` + 每角色一份 `paired_eval` 报告。

- [ ] **Step 1: 每角色跑两条臂，各 40 个种子**

两条臂用**同一批种子**（`run_1`..`run_40`），地图路线由 `random.Random(seed)` 锁定，所以是真正的配对。

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
AB=~/.sts2-train/phase2_ab_$(date +%Y%m%d_%H%M%S); mkdir -p "$AB"
for char in Ironclad Silent Defect Regent Necrobinder; do
    STS2_SOLVER_CHARS=none scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py 40 "$char" \
        --results-log "$AB/${char}_off.jsonl" > "$AB/${char}_off.log" 2>&1
    STS2_SOLVER_CHARS="$char" scripts/run_caffeinated.sh .venv/bin/python -u python/play_full_run.py 40 "$char" \
        --results-log "$AB/${char}_on.jsonl" > "$AB/${char}_on.log" 2>&1
done
echo "$AB" > /tmp/sts2-cli/last_ab_dir.txt
```

这一步很长（5 角色 × 80 局）。用 `scripts/run_caffeinated.sh` 包住（macOS 空闲 1 分钟就会睡眠），输出放 `~/.sts2-train/`（`/tmp` 会被清）。

- [ ] **Step 2: 出配对报告**

```bash
cd /Users/bytedance/mygit/sts2-cli
AB=$(cat /tmp/sts2-cli/last_ab_dir.txt)
for char in Ironclad Silent Defect Regent Necrobinder; do
    echo "########## $char ##########"
    .venv/bin/python -m agent.paired_eval "$AB/${char}_off.jsonl" "$AB/${char}_on.jsonl" \
        --label-a "heuristic" --label-b "solver"
done
```

- [ ] **Step 3: 按判据决定每个角色的去留**

对每个角色看 `floor` 这一行的配对差值和 p 值：

- **差值 ≥ 0**（solver 不更差）→ 该角色保持开启。
- **差值 < 0 且 p < 0.05** → **该角色必须回到 gate 后面**：把 `_SOLVER_CHARS_DEFAULT` 改成不含它的集合，并在本文件记录实测数字。不要因为"理论上引擎更准"就留着——本仓库已有先例（记忆 `sts2-sim-card-db-is-ironclad-only`：一个"理论上更好"的规划器让 Defect 实测倒扣 3.96 层）。
- **差值 < 0 但 p ≥ 0.05** → 样本不够，把该角色的两条臂各扩到 120 个种子再判一次（改上面命令里的 `40` 和 `--results-log` 文件名），**不要**直接当作"没有差异"放过。

同时检查报告里的 `invalid_seeds`：任何一条臂的无效局数显著高于另一条（例如 >5%），说明这不是"打得好不好"的差异而是稳定性差异，回到 Task 3。

- [ ] **Step 4: 把实测数字写进本文件**

在本 Task 下方新增"配对评测结果（实测）"小节，逐角色写：两条臂各多少有效种子、floor 配对差值、p 值、结论（保持开启 / 回到 gate 后面 / 扩样本）。**五个角色一个都不能省**，包括结论是"保持开启"的。

- [ ] **Step 5: Commit（如果 Step 3 改了默认集合）**

```bash
git add python/play_full_run.py tests/test_solver_gate.py docs/superpowers/plans/2026-09-21-combatsolver-port-phase2.md
git commit -m "fix: keep <角色> on the heuristic -- paired eval says the solver is worse

<两条臂的有效种子数、floor 配对差值、p 值>"
```

---

#### 配对评测结果（实测，2026-09-23）

跑法：每角色 40 个共享种子 × 2 臂，5 条 lane 并行；ON 臂 `STS2_SOLVER_THREADS=3`（5×3=15 ≤ 18 核，避免时间预算下的 CPU 饥饿把 solver 测弱，见 e7c4ed8）。原始数据 `~/.sts2-train/phase2_ab_20260922_102622/`。

**第一次出数是错的，已更正。** `result_to_eval_row`（Task 5）把引擎 game_over 里的**幕内**层数（每幕从 1 重新数）当成了 paired_eval 需要的**全局**层数。偏差只朝一个方向：启发式臂 200 局有 199 局死在 Act 1，幕内 = 全局，不受影响；solver 臂有 54 局死在 Act 2/3，每局被少记约 17 层。第一版结果把 Defect 算成 −0.24 层（p=0.79），而它 40 局里有 24 局打进了 Act 2 以上——这个矛盾暴露了问题。修复见 a6472da（`global_floor()`，与 `agent/eval_rl.py:109` 的 `global_floor_from_state` 同一公式，有测试钉住两者一致）。下表是用修好的换算对**同一批原始数据**重算的，没有重跑对局（`*_global.jsonl`）。

| 角色 | 有效配对 | 启发式 → solver（全局层数） | 配对差值 | t | p | 结论 |
|---|---|---|---|---|---|---|
| Ironclad | 38（solver 臂 2 局 crash，均为 BUG-040 挂死被杀） | 9.34 → 17.24 | **+7.90** | 6.88 | <0.0001 | 保持开启 |
| Silent | 40 | 9.75 → 14.65 | **+4.90** | 4.91 | <0.0001 | 保持开启 |
| Defect | 38（solver 臂 2 局 `plan_combat_turn_execution_failed`，BUG-042） | 12.08 → 22.58 | **+10.50** | 7.63 | <0.0001 | 保持开启 |
| Regent | 39（solver 臂 1 局 execution_failed，BUG-042） | 10.13 → 16.95 | **+6.82** | 5.67 | <0.0001 | 保持开启 |
| Necrobinder | 40 | 9.20 → 13.90 | **+4.70** | 4.88 | <0.0001 | 保持开启 |

五个角色全部正向显著。paired_eval 的 p 值未做多重比较校正；按 5 个角色做 Bonferroni（阈值 0.01）结论不变，最小 t = 4.88。判据里"差值为负就退回 gate"的分支一个都没触发，`_SOLVER_CHARS_DEFAULT` 维持全部 5 个角色。

对照臂的完整性：启发式臂 200/200 局有效、solver 调用 0 次；solver 臂 solver 调用合计 9 000+ 次、solver 报错 4 次（全部是 Regent 的 BUG-041，那几回合回落到启发式，是对 solver 不利的稀释）。

分房间的战损（Act 1、只算战斗内掉血）在跑的过程中另算过一版：普通战斗每场掉血在五个角色上几乎减半（例如 Defect 12.0 → 5.5），精英战也全部下降；过 Act 1 的比例从启发式 200 局只有 1 局，变成 solver 臂的 19%–63%。Boss 战的"每场掉血"反而是 solver 更高，那是幸存者偏差（启发式很少打到 boss，到了也只剩 22–47 HP，没血可掉），不是 solver 打得差——应当看到达率、入场血量和存活率，三项都是 solver 明显更好。

### Task 7: 文档同步

**Files:**
- Modify: `CLAUDE.md`（Protocol notes 里 `plan_combat_turn` 那段）
- Modify: `docs/superpowers/specs/2026-09-17-combatsolver-port-design.md`（Phase 2 段落）
- Modify: `src/Sts2Headless/RunSimulator.cs`（`ConvertPlanCardChoiceToJson` 上方的过时注释）

- [ ] **Step 1: 改 `CLAUDE.md` 的 Protocol notes**

把 `plan_combat_turn` 那一条里的 "gate `plan_combat_turn` to Ironclad only" 相关措辞，改成 Task 6 得出的真实结论（哪些角色走 solver、由 `STS2_SOLVER_CHARS` 控制、默认值是什么）。"只执行到第一个 `end_turn` 就重新规划"这条约定**不变**，不要动。

- [ ] **Step 2: 改设计文档的 Phase 2 段落**

把 "Phase 2 — 全角色接入，退休旧规划器" 拆成两段，如实反映本计划开头"范围决定"里的发现：本计划只做全角色接入 + 质量门槛；"退休 `agent/sim`/`turn_planner.py`"要先把 solver 接进 `agent/combat_env.py`，且 `turn_planner.py` 里的 `defense_override_enabled`/`intent_defense_override`/`hallway_danger_threshold`/`elite_danger_threshold`/`apply_vantom_slippery_mask` 是现役且有实测收益的代码，不能随文件一起删——要先搬到独立模块。

- [ ] **Step 3: 修 `RunSimulator.cs` 里的过时注释**

`ConvertPlanCardChoiceToJson` 上方写着 "OptionOccurrence disambiguate duplicate cards the same way CardOccurrence does"——Phase 1 的单测已经证伪（`OptionOccurrence` 要求 Entry **和** `CurrentUpgradeLevel` 同时相同，`CardOccurrence` 只看 Entry；见 Phase 1 计划"后续验证"小节）。把这条注释改成真实语义，并指向 `Search/CardChoiceSupport.cs` 的 `CountTokenOccurrence`。C# 代码本身不需要改。

- [ ] **Step 4: 重新 build 确认注释改动没碰坏代码**

```bash
cd /Users/bytedance/mygit/sts2-cli
~/.dotnet-arm64/dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -3
```

Expected: `0 Error(s)`。

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-17-combatsolver-port-design.md src/Sts2Headless/RunSimulator.cs
git commit -m "docs: record Phase 2 outcome; correct a falsified OptionOccurrence comment

Also narrows the design doc's Phase 2 claim: retiring turn_planner.py is
not a delete -- it currently hosts the shipped hallway/elite defense
override and apply_vantom_slippery_mask, with live callers in
decision_advisor / rl_agent / eval_rl / combat_env / boss_retry."
```

---

## Phase 2 完成的判定标准

- [x] `STS2_SOLVER_CHARS` 开关存在、默认值反映 Task 6 的真实结论，且单测覆盖（未知角色名报错、`all`/`none`、逗号列表）。
- [x] 完整回归门槛（5 角色 × 5 局）全部 `Completed: 5/5`，0 `TIMEOUT`/`ERROR`。
- [x] 5 个角色**每一个**都有配对评测数字（有效种子数、floor 配对差值、p 值）写进本文件，没有一个角色是"没测但开着"。
- [x] 冒烟/修复过程中发现的每个问题都在 `agent/bug.md` 有条目，注明修在胶水层还是引擎层。
- [x] 文档（`CLAUDE.md`、设计文档）与代码实际行为一致（逐条对照代码和原始数据核实过）。

达标后开 Phase 2b：把 solver 接进 `agent/combat_env.py` 的 `combat_play` 决策（现在完全没接），再谈退休 `agent/sim`/`turn_planner.py`。

---

## 待办（执行中发现，Task 6 跑完之后再动——现在改任何 solver 路径都会作废正在跑的 A/B）

- [ ] **接上 solver 的进度遥测。** `DoPlanCombatTurn` 目前传的是 `progressCallback: null`（`src/Sts2Headless/RunSimulator.cs:1249`），诊断 sink 的 `info`/`debug` 也是两个空函数（Phase 1 为避开"stderr 写满管道 → 引擎阻塞"的死锁特意关掉的）。所以 `SolverProgress` 里的 `ExpandedNodes`/`MaxNodes`/`FrontierNodes`/`EndedNodes`/`ReviewedWorldlines`/`ElapsedMilliseconds`/`Phase` 一条都没采。做法：只在 callback 里记最后一份 progress，随 `combat_plan` 结果一起放进 JSON 返回，**不走 stderr**，从而绕开原来那个死锁。收益：能直接回答每次搜索是自己收敛（`passSettled`）、撞节点上限（`MaxExpandedNodes = 120_000`）、还是撞时间上限（120 s）——目前只能从耗时分布间接推测（8 线程无争用时 23.5% 秒内收手、1.4% 顶到 120 s）。注意：它只能估到"预算耗尽"的时间，估不到"遍历穷尽"的时间——这是 beam search（宽度 60），从不穷举。
- [ ] **BUG-040：`plan_combat_turn` 可以永久挂死。** 见 `agent/bug.md`。先做 harness 层的单次调用看门狗（远高于 120 s，比如 300 s，超时就重启引擎并记一个独立状态），让一次挂死只损失一局而不是一整条 lane；再用保存下来的 core dump 查根因。
- [ ] **BUG-041：`Cannot fork with pending Power amount changes`**（Regent，力量）。见 `agent/bug.md`。
- [ ] **BUG-042：3 局 `plan_combat_turn_execution_failed`，且 ERROR 行丢了 act/floor。** 见 `agent/bug.md`。
