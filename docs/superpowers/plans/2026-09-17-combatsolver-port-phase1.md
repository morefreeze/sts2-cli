# Combat Solver 引擎移植 Phase 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Steam 创意工坊 mod Combat Solver（[Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver)，MIT）的 Engine/Search/Prediction/Strategy 战斗搜索引擎移植进 `src/Sts2Headless`，在 Ironclad 一个角色上跑通"给定真实战斗状态 → 返回最优出牌序列"这条链路，验证正确性，为 Phase 2/3 全角色接入、PPO 收缩到只学地图路线打基础。

**Architecture:** `RunSimulator.cs` 在 `combat_play` 决策点已经持有真实的 `Player`/`Creature`/`CombatState`（`MegaCrit.Sts2.Core.Combat` 命名空间下的游戏原生类型）。移植进来的 `CombatRootSnapshot.Capture(CombatState state)` 直接消费这些真实对象生成一份不可变快照，交给 `CombatSearchCoordinator.Solve(...)` 做 Best-First Width Search，返回 `SolverResult`；新增一个 JSON action（`plan_combat_turn`）把这条链路暴露给 Python 侧。移植范围不包含 Combat Solver 的 `Runtime`/`UI`/`Api`（mod 加载器与覆盖层胶水，深度绑定 RitsuLib/Godot 运行时 UI），这些职责由 `RunSimulator.cs` 自己的决策分发逻辑代替。

**Tech Stack:** C# / .NET 9，HarmonyLib，Krafs.Publicizer（新增），本仓库现有的 `src/GodotStubs` + `lib/sts2.dll`。

---

## 背景依据（已验证，写作本计划前实测过，不是猜测）

克隆 [Torch1230/CombatSolver](https://github.com/Torch1230/CombatSolver) 到 `/tmp/sts2-cli/CombatSolver` 读过源码后确认：

1. `CombatSolver.csproj` 用 `Krafs.Publicizer` 把 `sts2.dll` 的私有/内部成员编译期公开访问（`<Publicize Include="sts2" IncludeVirtualMembers="false" IncludeCompilerGeneratedMembers="false" />`）；本仓库 `Sts2Headless.csproj` 目前**没有**这个包，只是直接引用 `sts2.dll`——RunSimulator.cs 至今能编译是因为它只用到了 `Player`/`PlayerCombatState`/`Card`/`TargetType` 这些本来就公开的表层 API。Combat Solver 的 Engine/Search 大概率会摸到更深的私有字段，所以第一步必须先加 Publicizer。
2. Engine（155 文件）/ Search（113 文件）/ Prediction（54 文件）/ Strategy（1 文件）里只有 4 个文件引用 `RitsuLib`：
   - `Engine/Common/NativeModelCloneConcurrency.cs`、`Engine/InCombat/Simulation/CombatPredictionDynamicVarExtensions.cs`：判断"自定义卡牌动态数值克隆是否安全"的检测逻辑。当 RitsuLib 未加载时（我们的无头环境从不加载任何 mod），`AccessTools.TypeByName("STS2RitsuLib...")` 本来就返回 `null`，判定结果本来就是"不安全，走保守路径"——这是一个纯粹的性能优化开关，不影响正确性。
   - `Engine/InCombat/Mirrors/Cards/OnPlay/CardOnPlayInferrer.cs`：用 `STS2RitsuLib.Utils.HarmonyIl` 做 IL 反射推断卡牌效果。
   - `Prediction/PredictionModHookSubscriberCapture.cs`：调用 `RitsuLibFramework.GetMaxHandSize(player)` 一行。
3. 核心逻辑目录里没有 `[HarmonyPatch]` 挂钩（只有 Prediction/Testing 各 1 个，不在我们要用的路径上）——引擎是纯函数库，不依赖拦截游戏自身运行循环。
4. 真正的搜索入口是 `internal static partial class CombatSearchCoordinator` 的
   `public static SolverResult Solve(CombatRootSnapshot root, SolverDisplayNames displayNames, BattleDamageSnapshot battleDamage, SearchPolicySnapshot policy, CancellationToken cancellationToken, Action<SolverProgress>? progressCallback)`（`src/Search/CombatSearchCoordinator.cs:8`）。
5. `CombatRootSnapshot` 定义在 `src/Runtime/CombatRootSnapshot.cs`（0 处 RitsuLib 引用），构造入口是
   `public static CombatRootSnapshot Capture(CombatState state)`（同文件 125 行），内部直接用 `LocalContext.GetMe(state)` 拿 `Player`、`player.PlayerCombatState` 拿 `PlayerCombatState`——和 `RunSimulator.cs:DoPlayCard` 里已经在用的 `player.PlayerCombatState` 是同一个真实游戏类型。**结论：`CombatRootSnapshot.cs` 也要一起移植，尽管它物理上在 `Runtime/` 目录下。**
6. `Capture` 开头有 `if (!NGame.IsMainThread()) throw ...`——这是一个真实风险点，本无头环境是否被 `NGame` 判定为"主线程"未经验证，Task 4 里必须实测。

## 移植范围

| 来源目录 | 文件数 | 是否移植 | 理由 |
|---|---|---|---|
| `src/Engine/` | 155 | 是 | 核心：真实对象克隆 + 战斗规则镜像 |
| `src/Search/` | 113 | 是 | 核心：BFWS/beam search 算法 |
| `src/Prediction/` | 54 | 是 | 核心：多回合效果预测 |
| `src/Strategy/` | 1 | 是 | 核心：纯值函数（`CardMechanismFacts.cs`），无依赖 |
| `src/Runtime/CombatRootSnapshot.cs` | 1 | 是 | Search.Solve 的入参类型，0 RitsuLib 依赖 |
| `src/Runtime/`（其余 55 个） | 55 | 否 | mod 生命周期钩子、RitsuLib 注册，无头环境不需要 |

> **范围修正（Task 3 实测发现，2026-09-17）**：上面这行"其余 55 个否"判断错了。Engine/Search/Prediction 还依赖 `Runtime/` 下另外 8 个文件定义的数据/策略类型（`SolverProgress.cs`、`SolverSettings.cs`、`BattleDamageTracker.cs`、`ContinuationStamp.cs`、`LiveCombatStamp.cs`、`SearchGcLifecycleMetrics.cs`、`SearchMemoryPressureSignal.cs`、`SolverDisplayNames.cs`），编译期缺失表现为 12 个 `CS0246`。其中 7 个文件只依赖已引入的 `MegaCrit.Sts2.*`/`CombatSolver.Engine.*`，可以直接照搬 vendor；`SolverSettings.cs`（716 行）整份引了真实 `Godot` 命名空间做 mod 设置的 JSON 持久化，不能整体 vendor——但我们只需要它里面的 2 个纯枚举 `SolverPotionPolicy`/`BossHpStrategy`，改为单独摘出到一个新文件 `Runtime/SolverSettingsEnums.cs`，只放这两个枚举定义，注明"节选自 SolverSettings.cs，只取枚举，不引入 Godot 依赖"。
| `src/UI/` `src/Api/` `src/Diagnostics/` `src/Replay/` `src/Testing/` | 259 | 否 | UI 覆盖层、mod 对外 API、诊断工具、mod 自己的测试框架——本阶段不需要 |

合计移植 324 个文件，保留原始 `namespace CombatSolver` 前缀不改名（减少 diff，Phase 2 如有真实命名冲突再处理）。

---

### Task 1: 让 `Sts2Headless.csproj` 能访问 sts2.dll 的私有成员

**Files:**
- Modify: `src/Sts2Headless/Sts2Headless.csproj`

- [x] **Step 1: 在 `<ItemGroup>` 里加入 Krafs.Publicizer，设置与 CombatSolver 完全一致的选项**

在 `Sts2Headless.csproj` 现有的 `<Reference Include="sts2">...</Reference>` 所在 `<ItemGroup>` 之前，新增一个 `<ItemGroup>`：

```xml
  <ItemGroup>
    <PackageReference Include="Krafs.Publicizer" Version="2.3.0" PrivateAssets="all" />
    <Publicize Include="sts2" IncludeVirtualMembers="false" IncludeCompilerGeneratedMembers="false" />
  </ItemGroup>
```

- [x] **Step 2: 确认包能正常还原并且现有代码仍然编译**

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -40
```

Expected: `Build succeeded.`（这一步只加了 Publicizer，没有加任何新源码，理论上不应该产生新的编译错误——如果报错，先在这一步止损，不要带着坏的基线进入 Task 2）。

- [x] **Step 3: Commit** (943cd9d)

```bash
git add src/Sts2Headless/Sts2Headless.csproj
git commit -m "build: add Krafs.Publicizer so Sts2Headless can access sts2.dll internals

Needed to compile the ported Combat Solver engine (Phase 1 of
docs/superpowers/specs/2026-09-17-combatsolver-port-design.md), which
touches private/internal members of the real game assembly the same
way the original mod's own .csproj does."
```

---

### Task 2: 拉取 Combat Solver 源码到本仓库

**Files:**
- Create: `src/Sts2Headless/CombatSolverEngine/` （新目录，容纳全部移植文件）

- [x] **Step 1: 克隆上游仓库到临时目录（如果 `/tmp/sts2-cli/CombatSolver` 已存在就跳过这一步）**

```bash
mkdir -p /tmp/sts2-cli
rm -rf /tmp/sts2-cli/CombatSolver
git clone --depth 1 https://github.com/Torch1230/CombatSolver.git /tmp/sts2-cli/CombatSolver
```

- [x] **Step 2: 按移植范围表复制文件**

```bash
cd /Users/bytedance/mygit/sts2-cli
mkdir -p src/Sts2Headless/CombatSolverEngine
cp -r /tmp/sts2-cli/CombatSolver/src/Engine src/Sts2Headless/CombatSolverEngine/Engine
cp -r /tmp/sts2-cli/CombatSolver/src/Search src/Sts2Headless/CombatSolverEngine/Search
cp -r /tmp/sts2-cli/CombatSolver/src/Prediction src/Sts2Headless/CombatSolverEngine/Prediction
cp -r /tmp/sts2-cli/CombatSolver/src/Strategy src/Sts2Headless/CombatSolverEngine/Strategy
mkdir -p src/Sts2Headless/CombatSolverEngine/Runtime
cp /tmp/sts2-cli/CombatSolver/src/Runtime/CombatRootSnapshot.cs src/Sts2Headless/CombatSolverEngine/Runtime/CombatRootSnapshot.cs
find src/Sts2Headless/CombatSolverEngine -name "*.cs" | wc -l
```

Expected: 最后一行输出 `324`（如果不是，对照上面的移植范围表检查漏了哪个目录）。

- [x] **Step 3: 在 `Sts2Headless.csproj` 里确认新文件被编译器捡到**

`Microsoft.NET.Sdk` 项目默认按 glob `**/*.cs` 包含所有子目录源码，不需要手动加 `<Compile Include>`。用下面命令确认没有被意外排除：

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj -v:normal 2>&1 | grep -c "CombatSolverEngine"
```

Expected: 输出一个 > 0 的数字（说明编译器确实在处理这些文件；具体报错留到 Task 3 处理）。

- [x] **Step 4: (b664dcb, README.md cleanup in 429befb) 记录来源，方便以后同步上游更新**

```bash
cat > src/Sts2Headless/CombatSolverEngine/VENDORED.md << 'EOF'
# Vendored from Combat Solver

Source: https://github.com/Torch1230/CombatSolver (MIT license)
Vendored: 2026-09-17, from `main` branch, commit at clone time.
Scope: Engine/, Search/, Prediction/, Strategy/, and Runtime/CombatRootSnapshot.cs only.
NOT vendored: Runtime/ (remainder), UI/, Api/, Diagnostics/, Replay/, Testing/ —
these are the live-mod / RitsuLib / overlay glue this headless integration
does not need. See docs/superpowers/specs/2026-09-17-combatsolver-port-design.md.

Do not hand-edit ported files' algorithm logic. If upstream fixes a bug we
need, re-vendor the affected file(s) from a fresh clone instead of patching
by hand, so we don't silently diverge from upstream.
EOF
git add src/Sts2Headless/CombatSolverEngine
git commit -m "vendor: import Combat Solver Engine/Search/Prediction/Strategy (unmodified)

Straight copy from https://github.com/Torch1230/CombatSolver (MIT),
Engine+Search+Prediction+Strategy+Runtime/CombatRootSnapshot.cs only.
Does not compile yet -- RitsuLib touchpoints and any remaining
private-member access are resolved in the next tasks."
```

---

### Task 3: 解决编译错误，得到一个干净的 build

这一步的产出是可验证的（`dotnet build` 干净通过），但具体报什么错在实际跑之前无法穷举，因此用下面这套**判定规则**逐类处理，而不是逐行猜测：

- [x] **Step 1: 先跑一次完整 build，把所有报错存下来**

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tee /tmp/sts2-cli/build_errors_1.log | grep -E "error CS" | sort | uniq -c | sort -rn > /tmp/sts2-cli/build_errors_1_summary.txt
cat /tmp/sts2-cli/build_errors_1_summary.txt
```

- [x] **Step 2: 按错误类型分类处理，按下表的优先级顺序修**

> 实测发现第 5 个 RitsuLib 触点（`PowerDynamicVarMaterializationGuardPatch.cs`，处理方式同表格第一行），以及 Runtime 下另外 13 个文件的连锁依赖（见上面"范围修正"）。

| 错误特征 | 处理方式 |
|---|---|
| `error CS0246: The type or namespace name 'STS2RitsuLib...' could not be found` | 定位到 4 个已知触点文件之一（`NativeModelCloneConcurrency.cs`、`CombatPredictionDynamicVarExtensions.cs`、`CardOnPlayInferrer.cs`、`PredictionModHookSubscriberCapture.cs`）。对前两个文件：删掉 `using STS2RitsuLib...;` 这一行，把涉及 `STS2RitsuLib` 类型的检测函数体直接改成 `return false;`（保持"未加载 RitsuLib 时的真实行为"不变，见本文件"背景依据"第 2 条）。对 `PredictionModHookSubscriberCapture.cs`：把 `RitsuLibFramework.GetMaxHandSize(player)` 替换为 `player.PlayerCombatState?.MaxHandSize ?? player.MaxHandSize`（先读 `src/Sts2Headless/CombatSolverEngine/Prediction/PredictionModHookSubscriberCapture.cs` 附近代码确认真实存在的属性名，字段名不对就用 `dotnet build` 报错里给出的"did you mean"提示纠正）。对 `CardOnPlayInferrer.cs`：读完整文件后判断 `STS2RitsuLib.Utils.HarmonyIl` 具体做什么 IL 操作，如果只是"没有更好来源时的兜底推断"，改成直接跳过该分支（返回 unknown/null，交由上层已有的 fallback 处理）；如果是必需路径，记录下来作为本任务的已知缺口，不要为了消掉编译错误而臆造替代实现。 |
| `error CS0122/CS0117: '...' is inaccessible due to its protection level` 或 `does not contain a definition for '...'` | 说明 Publicizer 的 `IncludeVirtualMembers`/`IncludeCompilerGeneratedMembers` 设置或作用范围不够。先确认报错的类型确实属于 `sts2` 程序集（不是拼写错误），再考虑把 Task 1 里的 `<Publicize>` 设置对齐 CombatSolver 上游最新配置（检查 `/tmp/sts2-cli/CombatSolver/local.props` 是否存在覆盖项）。 |
| `error CS0234: The type or namespace name 'X' does not exist in the namespace 'MegaCrit.Sts2...'` | 说明本仓库 `lib/sts2.dll` 的游戏版本和 CombatSolver 目标版本（v0.111.0）之间有差异。先跑 `grep -n "game_version\|v0.111" CLAUDE.md` 确认当前仓库锁定的版本，如果版本一致但类型缺失，用 `ildasm`/反编译工具确认该类型是否改了名字或改了命名空间，按需在受影响文件里做最小改名，并在 commit message 里记录改了什么、为什么。 |
| 其它 CS 错误（命名冲突、`partial` 类拆分导致的可见性问题等） | 逐个读报错定位到具体文件和行号，修复原则：只改**编译期**问题（using、可见性、类型名），不改算法逻辑本身。 |

- [ ] **Step 3: 每解决完一类错误，重新 build 确认数量在下降**

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | grep -c "error CS"
```

重复 Step 1-3，直到这个数字是 0。**已达成**：`dotnet build` 从 85 个唯一错误收敛到 0（过程：85 → 81 → 80 → 1 → 0，中间两次收敛分别是修完 4 个已知触点、以及把 Runtime 缺口补全后又炸出的两波连锁依赖）。

- [x] **Step 4: 确认现有测试套件（未涉及新代码的部分）没有被破坏**

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -5
```

Expected: `Build succeeded. 0 Warning(s) 0 Error(s)`（Warning 数量不必是 0，但不能新增编译 Error）。**已确认**：从清空 `obj`/`bin` 的干净状态重新 build，`0 Error(s)`（有若干 pre-existing 风格的 warning，如 `CS0649` 未赋值字段，均为 vendored 代码本身特性，非本任务引入的新问题）。

- [x] **Step 5: Commit** (09311aa，文档措辞修正在 254afd8)

```bash
git add src/Sts2Headless/CombatSolverEngine
git commit -m "fix: resolve RitsuLib touchpoints and compile errors in vendored engine

- NativeModelCloneConcurrency / CombatPredictionDynamicVarExtensions:
  drop RitsuLib-authored-card fast-path check, always take the safe
  (non-independent) clone path -- identical behavior to RitsuLib not
  being loaded, which is always true in this headless build.
- PredictionModHookSubscriberCapture: replace
  RitsuLibFramework.GetMaxHandSize with the real game's own max-hand
  accessor.
- [describe CardOnPlayInferrer.cs resolution once known]

Build is clean: dotnet build src/Sts2Headless/Sts2Headless.csproj
succeeds with 0 errors."
```

> **Task 4/5 需要知道的一个隐患**（Task 3 代码质量审核发现）：`PowerDynamicVarMaterializationGuardPatch.cs` 在真实 mod 里是一个"检测到后台模拟违规提前物化 Power 动态数值就立刻 throw"的 fail-fast 保护，靠 RitsuLib 把它装成真实 Harmony 补丁才生效。本仓库从不安装任何 Harmony 补丁（`Entry.Initialize()` 从不运行），所以这个保护现在是**彻底不生效的**——不是"改成了安全的保守路径"，而是"该 throw 的地方现在完全沉默"。如果 Task 4/5 验证时看到 solver 对某些跟 Power 相关的局面给出奇怪的方案，这是一个排查线索：某处后台模拟可能违反了这个不变量，但因为保护没装，不会有异常告诉你。

---

### Task 4: 暴露 `plan_combat_turn` JSON 动作

**Files:**
- Modify: `src/Sts2Headless/RunSimulator.cs`（在 `ExecuteAction` 的 `switch` 里加一个 case，紧挨 `case "play_card":` 之后；新增一个 `DoPlanCombatTurn` 方法，放在 `DoPlayCard` 方法之后）

- [x] **Step 1: 读一遍 `CombatRootSnapshot.Capture` 的完整实现和它三个搭档类型的构造方式**

```bash
cat /Users/bytedance/mygit/sts2-cli/src/Sts2Headless/CombatSolverEngine/Runtime/CombatRootSnapshot.cs
grep -rn "class SolverDisplayNames\|class BattleDamageSnapshot\|class SearchPolicySnapshot" /Users/bytedance/mygit/sts2-cli/src/Sts2Headless/CombatSolverEngine/
```

记录这三个类型各自最小可用的构造方式（可能是无参默认值，也可能需要从 `player`/`state` 派生几个字段）——这决定了下面 `DoPlanCombatTurn` 里怎么构造它们，写代码前必须先看到真实定义，不能猜。

- [x] **Step 2: 验证 `NGame.IsMainThread()` 在无头进程里的返回值**

```bash
grep -rn "class NGame\b" /Users/bytedance/mygit/sts2-cli/src/Sts2Headless/CombatSolverEngine/ /Users/bytedance/mygit/sts2-cli/src/GodotStubs/ 2>/dev/null
```

如果 `NGame` 来自 `sts2.dll`（不在 GodotStubs 里），大概率是判断 `Thread.CurrentThread == 某个记录下来的主线程 ID`；由于 `RunSimulator.cs` 本身就是单线程同步跑（`InlineSynchronizationContext`），这个判断多半天然成立，但必须实测确认，不能假设。**已实测确认**：`NGame.IsMainThread()` 返回 `true`，`CombatRootSnapshot.Capture` 在无头进程里不会因为这个检查抛异常（用一次性脚本驱动真实对局验证的，不是读代码猜的）。

- [x] **Step 3: 在 `ExecuteAction` 里加新 case**

在 `src/Sts2Headless/RunSimulator.cs` 里找到：

```csharp
                case "play_card":
                    return DoPlayCard(player, args);
```

改成：

```csharp
                case "play_card":
                    return DoPlayCard(player, args);
                case "plan_combat_turn":
                    return DoPlanCombatTurn(player, args);
```

- [x] **Step 4: 实现 `DoPlanCombatTurn`**

> 实际实现里 `SearchPolicySnapshot` 在整个 vendor 的代码树里都找不到任何构造点（mod 自己的设置 UI 没移植），是手工按字段拼的。过程中踩了两个真实的行为坑，被 spec 审核抓到并修复：`AcceptableBattleHpLoss` 一开始设成 `int.MaxValue`，本意是"关掉找到就收手的提前退出"，实际效果正好相反（让 solver 找到第一个能赢的方案就不再继续搜），修成 `-1`（`ProjectedBattleHpLost` 系列字段全是非负累加量，`-1` 让比较永远不成立）；`UseBeamWidthPortfolio` 漏设，默认 `false`，跟 vendored 源码自己"默认开启"的文档注释矛盾，改成 `true`。细节见 commit c12cd3d/4a4ca8a。

在 `DoPlayCard` 方法定义结束之后加入（具体字段名以 Step 1 读到的真实定义为准，下面是骨架，`???` 处必须替换成真实类型/字段，不能保留占位符）：

```csharp
    private Dictionary<string, object?> DoPlanCombatTurn(Player player, Dictionary<string, object?>? args)
    {
        var pcs = player.PlayerCombatState;
        if (pcs == null)
            return Error("Not in combat");

        CombatState combatState = ???; // Step 1 里确认从 player/pcs/_runState 怎么拿到真实的 CombatState 实例
        CombatRootSnapshot snapshot;
        try
        {
            snapshot = CombatRootSnapshot.Capture(combatState);
        }
        catch (Exception ex)
        {
            return ErrorWithTrace("CombatRootSnapshot.Capture failed", ex);
        }

        var displayNames = ???; // Step 1 里的最小构造
        var battleDamage = ???;
        var policy = ???;

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
            // Step 5 里根据 SolverResult/CombatPlan 的真实字段把动作序列序列化成
            // JSON 能表达的形式（card_index/target_index/end_turn 的有序列表），
            // 具体字段名同样以真实源码为准。
        };
    }
```

- [x] **Step 5: 把 `SolverResult` 里的最优出牌序列转换成 `play_card`/`end_turn` 动作列表**

读 `src/Sts2Headless/CombatSolverEngine/Search/CombatPlan.cs` 和 `SolverResult` 的定义，找到"最佳节点的动作序列"字段，写一个私有辅助方法把它转换成形如
`[{"action": "play_card", "card_index": 2, "target_index": 0}, {"action": "end_turn"}]`
的列表，塞进上面返回值的 `["actions"]` 键。这一步的具体字段名必须来自实际读到的源码，不能照抄本计划的骨架。

> 实际返回的是 `card_id`/`card_occurrence`（稳定身份），不是 `card_index`（手牌位置）——因为计划跨越未来若干回合，那些回合的手牌内容在 JSON 协议这一层根本没模拟，手牌位置索引过了第 0 回合就没有意义。同理 `target_index`/`target_combat_id` 对第 2 回合及之后的动作也没有办法对上普通 `combat_play` 决策返回的敌人列表（那边只暴露位置 `index`）。**约定**：只执行到计划里第一个 `end_turn` 为止，然后重新调用一次 `plan_combat_turn` 拿下一回合的新计划，不要试图盲目执行整份多回合计划——已经记录进 [CLAUDE.md](../../../CLAUDE.md) 的 Protocol notes。`PlanAction.Choice`/`NestedChoices`（弃牌/消耗/换形态这类卡牌自带的子选择）已在 4bd49d5 补上序列化，走引擎自带的 `GetActionChoicesInExecutionOrder()`。

- [x] **Step 6: build 确认新代码编译通过**

```bash
cd /Users/bytedance/mygit/sts2-cli
dotnet build src/Sts2Headless/Sts2Headless.csproj 2>&1 | tail -10
```

Expected: `Build succeeded.` **已确认**，0 error。

- [x] **Step 7: Commit** (c12cd3d 初版，4a4ca8a 修复上面两个策略字段的 bug，后续 commit 补 Choice 序列化)

```bash
git add src/Sts2Headless/RunSimulator.cs
git commit -m "feat: expose plan_combat_turn action backed by the ported Combat Solver

At a combat_play decision, captures a CombatRootSnapshot from the real
live Player/CombatState and runs CombatSearchCoordinator.Solve to get
a full multi-turn action sequence, instead of only ever recommending
one card at a time."
```

---

### Task 5: 在 Ironclad 上验证

**Files:**
- Test: 用现有的 `python/play_full_run.py` 或 `agent/sts2_bridge.py` 手动跑

> **执行时发现的协议契约（写完本计划文字之后才确认，执行时以 [CLAUDE.md](../../../CLAUDE.md) 的 Protocol notes 为准）**：`plan_combat_turn` 的出牌动作用 `card_id`/`card_occurrence`（稳定身份）寻址，不是 `card_index`（手牌位置）；第 2 回合及之后动作的 `target_index`/`target_combat_id` 在普通 `combat_play` 决策的敌人列表里没有对应物。约定：**只执行到计划里第一个 `end_turn` 为止，然后重新调用一次 `plan_combat_turn` 拿下一回合的新计划**，不要试图盲目执行整份多回合计划。下面 Step 1-5 均按这个约定执行（而不是按写计划时设想的"直接执行 actions 列表"）。

- [x] **Step 1: 用 HTTP bridge 起一局 Ironclad，在第一个 `combat_play` 决策点调用新 action**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
python3 agent/sts2_bridge.py 9877 --compact --log /tmp/sts2-cli/combatsolver_smoke.jsonl &
BRIDGE_PID=$!
sleep 2
curl -s -X POST localhost:9877 -d '{"cmd":"start_run","character":"Ironclad","seed":"combatsolver_smoke_1","ascension":0}'
# 手动推进到第一个 combat_play 决策点（map_select -> 进入战斗房间），
# 到了以后调用：
curl -s -X POST localhost:9877 -d '{"cmd":"action","action":"plan_combat_turn"}'
kill $BRIDGE_PID
```

Expected: 返回一个 `"type": "combat_plan"` 的 JSON，`actions` 列表里全是本回合合法的 `play_card`/`end_turn`（不是 error，`only_death_routes_found` 为 `false`）。**已确认**：在一场自然到达（非 `enter_room` 强制）的 Nibbit 战斗上验证，返回合法 `combat_plan`。

- [x] **Step 2: 把返回的 `actions` 依次真的执行，确认无头引擎能正常吃下每一步**

写一个一次性脚本（放 `/tmp/sts2-cli/`，不进仓库）驱动 bridge：调用 `plan_combat_turn` 拿到 actions 后，依次 POST 每个 action，确认没有任何一步返回 `"type": "error"`，直到 `decision` 变成非 `combat_play`（战斗结束）。**已确认**（`/tmp/sts2-cli/verify_task5.py`，未提交）：按上面的"只执行到第一个 end_turn，然后重新 plan"约定跑完整场 4 回合战斗，`card_id`/`card_occurrence` → 手牌 `card_index` 的解析在全部 4 个回合边界上都正确（手牌内容/位置每回合都在变化），全程 0 damage（77→77 HP，与 solver 自己的 `projected_player_hp` 完全一致），`all_enemies_dead=true`。

- [x] **Step 3: 对比同一个种子下 `agent/sim` + `turn_planner.py` 现有基线的表现**

```bash
cd /Users/bytedance/mygit/sts2-cli
.venv/bin/python -c "
from agent.turn_planner import plan_turn
# 用相同的战斗初始状态跑一次旧的 1 回合 DFS，记录它选出的第一步动作，
# 和 plan_combat_turn 返回的第一步动作做比较——不要求完全一致（旧的只看
# 1 回合，新的看多回合，选择可能不同），但两者都不应该导致角色在这场战斗
# 里死亡或卡死；如果新 solver 选择的路线导致比旧 DFS 更差的结果，记录下来
# 作为 Phase 2 前必须先查的问题，不要悄悄放过。
"
```

**已确认**：在同一场 Nibbit 战斗的第 1 回合真实状态上分别跑了 `turn_planner.plan_action()`（旧 1 回合 DFS）和 `plan_combat_turn`（新 solver）。旧的选择打 Strike（手牌槽 0，打敌人 0）；新 solver 选择先打 Defend。两者不要求一致——新 solver 的完整计划全程 0 掉血清场，不比旧规划器差，不是一次质量倒退。

- [x] **Step 4: 跑满一整局 Ironclad，确认 solver 全程接管 combat_play 不会导致 crash/stuck/reset_failure**

```bash
cd /Users/bytedance/mygit/sts2-cli
export STS2_GAME_DIR="$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64"
scripts/run_caffeinated.sh .venv/bin/python python/play_full_run.py 5 Ironclad 2>&1 | tail -30
```

Expected: `Completed: 5/5`，且日志里没有 `plan_combat_turn`/`CombatRootSnapshot`/`CombatSearchCoordinator` 相关的未捕获异常。注意：`play_full_run.py` 目前的简单 AI 不会主动调用 `plan_combat_turn`——这一步需要先给它加一个"combat_play 时优先尝试 plan_combat_turn，失败则退回原逻辑"的最小分支，改动限定在 `python/play_full_run.py` 的 `combat_play` 分支内，不动其它决策类型的处理。

**已确认（跑了 4 次 5 局回归，如实记录，不是只挑一次干净的）**：
- 第 1 次（问题 1/2 修复前）：`Completed: 5/5`，但这是假阳性——问题 1（见下面 postmortem）会把真实的执行失败悄悄算成 `Completed`。
- 第 2 次（问题 1/2 修复后）：`Wins: 0/5, Completed: 5/5, avg_floor=11.4`，无任何 `!!`/异常。
- 第 3 次（同样的代码，换一批地图路线）：**`Wins: 0/5, Completed: 4/5, avg_floor=10.8`**——run 3 第一次被正确标成 `ERROR`（问题 1 的修复生效，抓到了问题 3：`expected card_select for choice MoveToDrawTop, got decision='combat_play'`）。
- 第 4 次（问题 3 修复后）：`Wins: 0/5, Completed: 5/5, avg_floor=11.8`，无任何 `!!`/异常，包括 run 3 用的种子。

全程 5 局都在 Act 1 落败（未见 Win），但这是 `play_full_run.py` 自己"random agent"式的地图路线/卡牌奖励/商店策略造成的混杂因素，不是本任务要评估的对象——solver 本身的出牌质量评估见 Step 3。

- [x] **Step 5: Commit（如果 Step 4 改了 play_full_run.py）**

初版落地于 d6585bb。Spec review 发现两个真实问题（问题 1/2），修复后重跑回归又自己暴露出第三个（问题 3——正是问题 1 修复之后才不再被静默吞掉）。三个问题的修复和对应 commit 见下面的 postmortem。

### Task 5 postmortem：三个真实问题（前两个来自 spec review，第三个是修复问题 1 之后自己暴露的）

**问题 1（已修复）：`summarize()` 把 `plan_combat_turn` 执行失败静默算作 "completed"。**

`play_run()` 里 `_execute_combat_plan_actions` 失败时返回的字典只有 `error` 键，没有 `timeout` 键；而 `summarize()` 原来的 `completed = sum(1 for r in results if r and not r.get("timeout"))` 只排除 `timeout`，不排除 `error`——于是这一局在 SUMMARY 里显示成一行普通的 `LOSS`（`act=None floor=None` 是唯一的线索），却被计入 `Completed: N/5`。这正好会让 CLAUDE.md 的回归门槛（"`Completed: 5/5` = 0 crash/stuck"）在真出问题的时候看起来像通过了。修复：`completed` 同时排除 `timeout` 和 `error`；每行状态新增 `ERROR`（区别于 `WIN`/`TIMEOUT`/`LOSS`），并在行尾附上 `error=...` 消息。

**问题 2（已修复，根因在我们自己的协议胶水层，不在 vendor 引擎里）：solver 提议使用一个已经用过的药水槽位。**

现象：turn 10 一次全新调用 `plan_combat_turn` 提议 `use_potion` slot=1，但真实引擎回绝 `"Invalid potion index 1"`——那个槽位在 turn 8（两次重新规划之前）已经被真实消耗过。

排查过程：
1. 确认 `CombatRootSnapshot.Capture` 本身没有跨调用缓存——`DoPlanCombatTurn` 每次都用 `CombatManager.Instance.DebugOnlyGetState()` 现取 `CombatState`，`Capture` 内部又用 `LocalContext.GetMe(state)` 现取 `Player`，`SimulatedCombatState` 的构造函数（`Search/SimulatedCombatState.cs:336-350`）每次都用 `player.GetPotionAtSlotIndex(slot)` 现读真实药水槽——vendor 引擎这条链路是干净的，每次调用都是新鲜状态，不是 `BattleDamageTracker` 那种静态缓存模式。
2. 反编译 `lib/sts2.dll`（`ilspycmd -t MegaCrit.Sts2.Core.Entities.Players.Player`）找到真正根因：`Player.PotionSlots` 是带空位的固定长度物理槽位数组，而 `Player.Potions => PotionSlots.Where(p => p != null)`——是一个**跳过空位、会因为消耗而收缩重排**的枚举。Combat Solver 的 `PlanAction.PotionSlot`（来自 `Search/CombatBeamSolver.Expansion.cs` 等处的 `potion.Id.Entry`/物理槽位）寻址的是 `PotionSlots`（物理槽位，我们没有引入任何 bug）；但 `RunSimulator.cs` 的 `DoUsePotion` 和 `PlayerSummary` 里的 `potions` 字段一直用的是 `player.Potions`（压缩列表）。这两套编号只要还没有槽位被消耗时碰巧一致，一旦任意更早的槽位被消耗，`PotionSlot` 和 `player.Potions` 里的位置就会永久错位——这正是我们自己（Task 4）在 `ConvertPlanActionsToJson` 里直接把 `action.PotionSlot` 透传成 `potion_index` 时踩中的协议层 bug，不是 vendor 引擎缺陷，因此不需要在 `VENDORED.md` 里记已知缺口，直接在这一层修。
3. 修复：给 `PlayerSummary` 里 `potions` 每一项加一个稳定身份字段 `["id"] = p.Id.ToString()`（和手牌卡片的 `["id"] = c.Id.ToString()` 同一套约定），`python/play_full_run.py` 新增 `_resolve_potion_index`，按 `potion_id`（`_norm_entity_id` 归一化后）在当前活的 `player.potions` 列表里找同身份的条目，取它的 `index`，而不是直接把 solver 的 `potion_slot` 当 `potion_index` 用。药水没有升级/附魔那类状态，同名药水互相等价，所以不需要像卡牌那样再引入一个 occurrence 字段。

**问题 3（已修复，同样是我们自己胶水层的假设错误，不是引擎 bug）：`_apply_action_choices` 把"引擎已经自动完成的选择"误判成失败。**

现象：第二次重跑回归（`step4_regression_v3.log`）里，run 3 在 `Completed: 5/5` 表格里第一次显示成了 `ERROR`（问题 1 的修复生效了——这正是它被设计要抓的那类情况）。具体报错：`!! plan_combat_turn: expected card_select for choice MoveToDrawTop, got decision='combat_play'`。

排查：从 `logs/20260917_154119_Ironclad_run_3.jsonl` 里还原出真实序列——turn 4 先打 Thrash（进 discard pile），再打 Headbutt（`choices: [{"effect": "MoveToDrawTop", "source_pile": "Discard", "cards": [{"card_id": "THRASH", ...}]}]`）。打 Headbutt 那一步的**前一个状态**显示 `discard_pile_count: 1`，`discard_pile: ["CARD.THRASH"]`——弃牌堆里只有唯一一张牌，没有真正的"选择"可做。打完 Headbutt 之后，`decision` 全程停留在 `combat_play`（从未变成 `card_select`），但下一次的 `draw_pile[0]` 确认是 `CARD.THRASH`——solver 想要的效果**已经真实生效**，只是引擎在候选唯一时直接自动结算，不走一次显式的 `card_select` 往返。这和本仓库 CLAUDE.md 已经记录的引擎不变式吻合："任何可能打开 card_select 的路径必须在 `_cardSelector.HasPending` 出现时才 yield"——反过来说，decision 没有变成 `card_select`，就说明真的没有任何选择在等待答复，不是 bug，是我们自己的校验太严格。

修复：`_apply_action_choices` 里，`decision == "combat_play"`（选择已经在同一次 play_card/use_potion 调用里自动结算，跳过继续）和`decision` 变成除 `card_select` 之外的别的合法状态（比如这张牌的伤害刚好斩杀最后一个敌人，直接跳到 `card_reward`/`game_over`）都不再算失败——只有真正意外的情形才失败。同时给 `_execute_combat_plan_actions` 加了一个配套检查：只要某个动作执行完之后 `decision` 已经不是 `combat_play`（无论是因为战斗提前结束还是别的原因），就立刻停止这一批的执行并返回成功，不再尝试拿一个已经不存在的手牌去解析下一个计划动作的 `card_id`（那样会把"仗提前打完了"误报成"卡牌解析失败"）。

**已知未测风险（未确认是 bug，只是没验证过）：`CardStateKey`/`card_occurrence` 在手牌被重排后的正确性。** `_resolve_card_index`/`_resolve_choice_indices` 按"沿手牌顺序数第 N 个同 `card_id` 的匹配"解析——如果同一 `card_id` 但状态不同的两张卡同时在手（例如未升级的 Strike 和 Strike+），且一次 Discard/Exhaust/Rearrange 选择在同一回合内改变了手牌顺序，`card_occurrence` 的计数基准可能和执行时的真实顺序对不上，从而解析到错误的物理卡。本次验证的战斗（Nibbit 4 回合、以及 5 局回归里遇到的所有战斗）都没有触发"同 id 不同状态 + 重排"的组合，所以这是一个理论上的未测风险，不是已确认的 bug；`PlanAction` 其实还有一个 `CardStateKey`/`CardStateOccurrence` 字段（vendor 引擎自己在 `FindCardForReplay`/`Search/CombatBeamSolver.Expansion.cs` 里优先用它而不是 `CardId`/`CardOccurrence`），但 `ConvertPlanActionsToJson` 目前没有把它序列化出来给 JSON 协议用——如果以后遇到疑似此类错误解析，先补上这个字段的序列化，而不是继续猜 occurrence。

---

## Phase 1 完成的判定标准（对照 spec）

- [ ] `dotnet build src/Sts2Headless/Sts2Headless.csproj` 干净通过，包含全部 324 个移植文件。
- [ ] `plan_combat_turn` 在真实 Ironclad 对局的至少一个 `combat_play` 决策点上返回可执行的多步计划。
- [ ] 至少 5 局 Ironclad 全程由 solver 接管出牌，跑到 `game_over`，0 crash / stuck / reset_failure。
- [ ] 已知缺口（`CardOnPlayInferrer.cs` 的 RitsuLib 触点、`NGame.IsMainThread()` 实测结果、`SolverDisplayNames`/`BattleDamageSnapshot`/`SearchPolicySnapshot` 的真实构造方式）都已经在对应 commit message 或本文件里写清楚，不留没记录的隐藏假设。

达标后回到 [docs/superpowers/specs/2026-09-17-combatsolver-port-design.md](../specs/2026-09-17-combatsolver-port-design.md) 开 Phase 2 的计划（全角色接入 + 退休 `agent/sim`/`turn_planner.py`）。
