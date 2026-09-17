# 移植 Combat Solver 引擎替换战斗出牌

## 目标

用 Steam 创意工坊 mod **Combat Solver**（[Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver)，MIT）的战斗搜索引擎，取代当前 `agent/sim` + `agent/turn_planner.py` 这套自写的 Python 战斗规则/1回合 DFS，最终让 PPO 只负责地图路线（`map_select`），不再学习战斗出牌——每局的出牌完全由移植进来的 solver 决定。

动机：

- `agent/sim` 是手写的战斗规则重实现，目前只在 Ironclad 上验证过正确；用在其他角色上实测是负收益（Defect −3.96 层，见记忆 `sts2-sim-card-db-is-ironclad-only`）。
- Combat Solver 的 Engine 不是重新实现规则，而是直接 `MemberwiseClone` 真实游戏的 Card/Enemy/Player 对象，在克隆上跑游戏本身的逻辑——天然对全部角色正确。
- 记忆 `sts2-ppo-training-is-inert` 反复证实 PPO 学不动战斗出牌；把这部分交给确定性搜索，PPO 只需要学习地图路线，问题规模显著变小。

## 可行性依据（已验证，非推测）

克隆 [Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver) 到本地读过源码后确认：

- `.csproj` 目标 `net9.0`，引用 `sts2.dll` / `0Harmony.dll` / `GodotSharp.dll`——与本仓库 `src/Sts2Headless` 打的是同一套补丁对象，同一个游戏版本（v0.111.0）。
- 核心逻辑目录 `Engine`（155 文件）/ `Search`（113 文件）/ `Prediction`（54 文件）/ `Strategy`（1 文件）中，只有 **4 处**引用 RitsuLib（游戏 mod 框架，深度绑定 Godot 运行时 UI），且都很窄：
  - 2 处是"自定义卡牌动态数值"克隆/预测兼容（`NativeModelCloneConcurrency.cs`、`CombatPredictionDynamicVarExtensions.cs`）——只服务 RitsuLib 自定义卡，本仓库只跑官方角色卡池，可直接 no-op。
  - 1 处是 Harmony IL 反射小工具（`CardOnPlayInferrer.cs` 用 `STS2RitsuLib.Utils.HarmonyIl`）。
  - 1 处是单个函数调用 `RitsuLibFramework.GetMaxHandSize(player)`（`PredictionModHookSubscriberCapture.cs`）。
- 核心逻辑里 **没有** `[HarmonyPatch]` 挂钩（只有 Prediction/Testing 各 1 处，且不在我们要用的路径上）——引擎不依赖拦截游戏自身的运行循环，可以当纯函数库调用。
- `Search` 目录是一套完整的 Best-First Width Search + beam search + novelty pruning + transposition table 实现（`CombatBeamSolver`、`CombatSearchCoordinator`、`SimulatedCombatState` 等），覆盖大量 relic/power/potion/monster AI 机制——体量远大于当前的 500-序列 1 回合 DFS，是一次真正的引擎级升级，不是小改动。

## 架构

```
Python (turn_planner.py 被移除，改为直接转发)
    │  JSON: {"cmd":"action","action":"combat_play_plan", ...}
    ▼
src/Sts2Headless (C#)
    │  在 combat_play 决策点，把当前真实 Card/Enemy/Player
    │  对象交给移植进来的 CombatBeamSolver
    ▼
CombatSolver.Engine / Search / Prediction / Strategy
  （MemberwiseClone 真实对象 → 搜索 → 返回 CombatPlan）
    │
    ▼
返回最优出牌序列 → RunSimulator.cs 逐步执行 DoPlayCard/DoEndTurn
```

- 移植的四个目录（Engine/Search/Prediction/Strategy）作为新增源码目录纳入 `src/Sts2Headless`（或同解决方案下的新 csproj，视命名冲突情况定），编译对象仍是 `lib/sts2.dll` + `0Harmony.dll` + 本仓库现有的 `src/GodotStubs`。
- `Runtime`（56 文件，34% 触碰 RitsuLib）、`UI`（23 文件）、`Api`（6 文件）、`Diagnostics`、`Replay` 不移植——那是 mod 加载器/覆盖层胶水代码，无头环境不需要，其职责（触发时机、结果消费）由 `RunSimulator.cs` 自己的决策点分发逻辑代替。
- 4 处 RitsuLib 触点：2 处自定义卡兼容直接删除调用（或替换成返回默认值的 no-op），1 处 IL 工具视其复杂度决定重写或裁剪对应的 inferrer 分支，`GetMaxHandSize` 直接读 `player.maxHandSize`（或等价字段）替代。

## 分阶段计划

**Phase 1 — 单角色跑通并验证正确性**
把 Engine+Search+Prediction+Strategy 四个目录拉进仓库、解决 4 处 RitsuLib 触点、接一个新的 JSON 决策动作，让 `CombatBeamSolver` 在**一个角色**（建议 Ironclad，因为 `agent/sim` 现有基线也是 Ironclad，方便对比）的真实 `combat_play` 决策点上跑起来、返回可执行的出牌序列。用现有 replay 日志或新跑的对局验证：solver 给出的每一步都能被 `DoPlayCard`/`DoEndTurn` 正常执行，且不崩溃、不卡死。

**Phase 2 — 全角色接入，退休旧规划器**
确认 Phase 1 在其余 4 个角色上同样可用（这四个角色目前正是 `agent/sim` 已知不安全的对象），全量替换 `combat_play` 决策的来源；删除 `agent/sim`、`agent/turn_planner.py`、以及 `eval_rl.py`/`train.py` 里调用它们的分支。

**Phase 3 — PPO 缩到只学地图路线**
战斗决策完全由 solver 接管后，精简 PPO 的观测空间和动作空间到只覆盖 `map_select`；`combat_play`/`card_reward`/`rest_site` 等决策点视情况仍可能需要人工启发式或 solver 建议（Combat Solver 的 `Strategy`/`Prediction` 是否覆盖map外决策还需在 Phase 1 期间进一步确认），本阶段范围以地图路线学习为主。

## 风险与未决问题

- **IL 工具触点**（`CardOnPlayInferrer.cs`）的裁剪成本目前只是估计，Phase 1 期间需要实际读代码确认。
- **搜索预算**：Combat Solver 默认给 16GB 内存做 Medium 档搜索（面向单机大内存场景）；训练/评测要跑大量并发对局，需要在 Phase 1 期间测出适合批量无头运行的预算档位，避免搜索本身成为新的资源瓶颈（类比当前 5 角色并行训练已经把机器打到 idle 0.56% 的教训）。
- **Phase 3 范围**：Combat Solver 本身不做地图路线规划，`map_planner.py` 保留；具体哪些非战斗决策点（`card_reward`/`rest_site`/`event_choice`）继续用现有启发式、哪些改用 solver 建议，留到 Phase 1 验证完主链路后再定。

## 测试

- Phase 1 完成的判定标准：至少跑通若干局真实对局（`play_full_run.py` 或 `eval_rl.py` 等价路径），solver 推荐的动作序列 100% 被无头引擎接受执行，且 avg_floor/胜率不低于同角色现有 `agent/sim` DFS 基线的抽样表现。
- 沿用仓库既有回归要求（[CLAUDE.md](../../../CLAUDE.md) 中 5 角色×5 局 0 crash/stuck）作为集成后的最终把关，但 Phase 1 阶段只需单角色验证。
