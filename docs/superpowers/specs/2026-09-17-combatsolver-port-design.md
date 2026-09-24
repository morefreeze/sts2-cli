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

> **状态（2026-09-23）**：Phase 1 已完成（[实施计划](../plans/2026-09-17-combatsolver-port-phase1.md)）。Phase 2 按下面拆开后的范围完成，见 [Phase 2 实施计划](../plans/2026-09-21-combatsolver-port-phase2.md)。

**Phase 2 — 全角色接入 + 质量门槛（已完成）**
确认 Phase 1 在其余 4 个角色上同样可用（这四个角色目前正是 `agent/sim` 已知不安全的对象），并且证明它不只是"不崩"，而是确实比现有启发式打得好。结果：解除 Ironclad gate 后 5 角色 × 5 局回归全部 `Completed: 5/5`、solver 参与率 100%；每角色 40 个共享种子的配对评测（solver 开 vs 关），全局层数提升 **+4.7 ~ +10.5**（Defect +10.5、Ironclad +7.9、Regent +6.8、Silent +4.9、Necrobinder +4.7），五个角色 p 均 < 0.0001。

**Phase 2b-1 — 搜索时间档位 + 挂死看门狗（已完成）**
`STS2_SOLVER_BUDGET` 可选 30/60/120/180/300 秒；每次搜索都有遥测；`python/play_full_run.py` 通过 `python/engine_process.py` 给每次引擎回复设超时，超时就杀掉整个进程组。档位配对评测（[实施计划与结果](../plans/2026-09-23-combatsolver-phase2b1-budget-tiers.md)）的结论：
- 30 s 对 Ironclad/Silent/Defect/Necrobinder 不劣于 120 s，批量条件下能省 13–55% 的搜索时间。Regent 方差太大，还判不了。
- 300 s 没有测得出来的收益：更长的搜索会先撞 12 万节点上限（`MaxExpandedNodes`），多给的时间用不上。
- 所以接下来值得研究的是节点上限，而不是时间。

**Phase 2b-2 — 接进 RL 路径，再退休旧规划器（未开始）**
原计划写的"全量替换 `combat_play` 决策的来源；删除 `agent/sim`、`agent/turn_planner.py`、以及 `eval_rl.py`/`train.py` 里调用它们的分支"，在写 Phase 2 计划时核对代码后发现不能一步做完：

- 上面的质量结论只来自 `python/play_full_run.py`（回归 harness）。`agent/combat_env.py` / `agent/eval_rl.py` 从来没有调用过 `plan_combat_turn`，所以 PPO/eval 的任何数字都还没有体现 solver。要先把它接进 `combat_env` 的 `combat_play` 决策，并在那条管线上重新做配对评测。
- `agent/turn_planner.py` 不只是旧的 1 回合 DFS，它还承载着已上线、实测有收益的走廊/精英格挡阈值（`defense_override_enabled`/`intent_defense_override`/`hallway_danger_threshold`/`elite_danger_threshold`）和 `apply_vantom_slippery_mask`，调用方有 `decision_advisor`/`rl_agent`/`eval_rl`/`combat_env`/`boss_retry`。退休它之前要先把这些搬到独立模块，不能随文件一起删。
- 挂死防护：`plan_combat_turn` 会永久挂死（`agent/bug.md` BUG-040，Ironclad 种子 `run_32` 可以稳定复现）。harness 侧已经有看门狗（Phase 2b-1）。`combat_env` 自己管理引擎子进程（`agent/combat_env.py:3206`，已经是 `start_new_session=True`），接进去时要加同等的单次回复超时，否则一次挂死就会让一个训练 worker 永远停住。
- 训练档位用 Phase 2b-1 的结论：Ironclad/Silent/Defect/Necrobinder 用 30 s，Regent 在扩样本判定之前用 120 s；评测维持 120 s。

**Phase 3 — PPO 缩到只学地图路线**
战斗决策完全由 solver 接管后，精简 PPO 的观测空间和动作空间到只覆盖 `map_select`；`combat_play`/`card_reward`/`rest_site` 等决策点视情况仍可能需要人工启发式或 solver 建议（Combat Solver 的 `Strategy`/`Prediction` 是否覆盖map外决策还需在 Phase 1 期间进一步确认），本阶段范围以地图路线学习为主。

## 风险与未决问题

- **IL 工具触点**（`CardOnPlayInferrer.cs`）的裁剪成本目前只是估计，Phase 1 期间需要实际读代码确认。
- **搜索预算**：Combat Solver 默认给 16GB 内存做 Medium 档搜索（面向单机大内存场景）；训练/评测要跑大量并发对局，需要在 Phase 1 期间测出适合批量无头运行的预算档位，避免搜索本身成为新的资源瓶颈（类比当前 5 角色并行训练已经把机器打到 idle 0.56% 的教训）。
  - **实测结论（Phase 2）**：移植版本里**没有档位**——原 mod 的档位选择住在没移植的 `SolverSettings.cs` 里，我们只有唯一的 `SolverSearchProfile.Default`（`BeamWidth` 60、`MaxExpandedNodes` 120 000、软时间预算 **120 s**）。单次 `plan_combat_turn` 中位数 3.4 s、均值 9.2 s（8 线程、无争用），约为启发式的 300 倍，一局要好几分钟。预算按墙钟计时，所以多个 solver 进程并发时 CPU 饥饿会让搜索变弱——批量跑必须用 `STS2_SOLVER_THREADS` 把每进程线程数压到核数以内（Phase 2 A/B 用 5 进程 × 3 线程）。
- **Phase 3 范围**：Combat Solver 本身不做地图路线规划，`map_planner.py` 保留；具体哪些非战斗决策点（`card_reward`/`rest_site`/`event_choice`）继续用现有启发式、哪些改用 solver 建议，留到 Phase 1 验证完主链路后再定。

## 测试

- Phase 1 完成的判定标准：至少跑通若干局真实对局（`play_full_run.py` 或 `eval_rl.py` 等价路径），solver 推荐的动作序列 100% 被无头引擎接受执行，且 avg_floor/胜率不低于同角色现有 `agent/sim` DFS 基线的抽样表现。
- 沿用仓库既有回归要求（[CLAUDE.md](../../../CLAUDE.md) 中 5 角色×5 局 0 crash/stuck）作为集成后的最终把关，但 Phase 1 阶段只需单角色验证。
