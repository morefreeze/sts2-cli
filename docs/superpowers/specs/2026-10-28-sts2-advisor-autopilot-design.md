# Sts2Advisor 全托管（AutoPilot）— design

Date: 2026-10-28. Approved by the user in chat. Branch `feat/deterministic-planner`.
Builds on `docs/superpowers/specs/2026-10-21-sts2-advisor-mod-design.md` (the advice-only mod).
Revised 2026-10-28（写计划与两轮代码评审后）：实现方式见文末"实现修订"一节，它取代第 1–3 段里与之冲突的写法。

## 目标

在现有 Sts2Advisor mod 上加一个"全托管"模式：玩家按一个键，mod 就按 greedy 的建议**亲自点完**本局所有局外决策，战斗交给已安装的 CombatSolver 全自动，一直打到本局结束。可以和"皮皮极速 (SpeedX)"加速 mod 一起用。

## 用户决策

1. **战斗交给 CombatSolver。** 本 mod 只托管局外；进战斗后等它打完。不自己实现实机战斗。
2. **加速 = 兼容 SpeedX。** 本 mod 不改 `Engine.TimeScale`、不改游戏 Fast Mode，只保证 SpeedX 开着时托管照样稳定。SpeedX 由玩家自己订阅。
3. **范围 = 当前这局打完为止。** 打到胜利/死亡结算页就停；不自动开新局。
4. **两档模式：建议 / 全托管。** 热键切换，随时接管。不做半托管、单步。
5. **没有主动暂停点。** 进 Boss 前、商店、事件都自动；只在搞不定的情况下停（见第 2 段）。
6. **实现走方案 B：复用游戏自带的 AutoSlay。** 不自己从零写每个界面的点击逻辑，而是复用 `MegaCrit.Sts2.Core.AutoSlay` 的 handler，用 Harmony 把它们的随机选择换成 greedy 的建议。

## 背景事实（2026-10-28 已核实，反编译 `lib/sts2.dll.original`）

- **`AutoSlayer.Start(seed, log)` 不能直接用。** 它的 `PlayRunAsync` 会：在主菜单点"放弃当前局"、随机选角色、`ObtainEpochOverride` 往存档里写解锁、关新手引导、把 `PrefsSave.FastMode` 设为 Fast、装 `AutoSlayCardSelector`（战斗内选牌也随机）、`runTimeout` 到点抛异常，`finally` 里 `QuitGame` 退出游戏。`IsActive` 为真还会打开 `NonInteractiveMode.AutoSlayerCheck`。
- **它的零件是 public 的、可单独用。** `IRoomHandler` / `IScreenHandler` 都是 `HandleAsync(Rng random, CancellationToken ct)` + `Timeout`；handler 只用 `AutoSlayer.CurrentWatchdog?.`（为 null 时安全）、静态的 `AutoSlayer.GetCurrentScreen<T>()`、`UiHelper` / `WaitHelper` / `AutoSlayLog`。主循环 `PlayRunAsync` 和 `DrainOverlayScreensAsync` 是 private，约 150 行。
- **每个界面的选择都是 handler 体内一句 `random.NextItem(list)`**，`list` 是 UI 节点（`NCardHolder`、`NEventOptionButton`、`NRestSiteButton`、`NGridCardHolder`、`NCardBundle`、`NMerchantSlot`…）。`Rng.NextItem<T>` 非虚，内部调 `NextInt(0, count)`。
- **点击方式**：`UiHelper.Click` = `NClickableControl.ForceClick()` + `Task.Delay`；卡牌用 `EmitSignal(NCardHolder.SignalName.Pressed, holder)`；商店用 `slot.Entry.OnTryPurchaseWrapper(inventory)`。所有等待都是真实时间（`Task.Delay`、`WaitHelper.Until` 轮询），不依赖动画时长。
- **handler 和 greedy 的差距**：
  - `MapScreenHandler.SelectNextRoom`（private static）永远走上一个节点的第一个子节点，不用 Rng。
  - `CardRewardScreenHandler` 一定拿一张，不能跳过。
  - `ShopRoomHandler` 过滤掉删牌（`NMerchantCardRemoval`），随机买到买不起为止，不会主动离开。
  - `CombatRoomHandler` 随机打牌。
  - 没有 greedy 建议的界面：`NChooseARelicSelection`、`NCrystalSphereScreen`。
- **CombatSolver（Workshop 3790899961，v0.50.1）** 自带"全自动"：`<user dir>/combat_solver_settings.json` 的 `autoEnableFullAuto` 决定每场战斗开始时是否自动开启；`stopFullAutoOnDeathTurn` / `stopFullAutoOnWorseRecalculation` 会让它中途暂停交还玩家。它检测到 SpeedX 会提示"悬浮显示可能持续产生大量临时内存"，并建议战斗加速改用它自带的"自动出牌速度 = 瞬间"。

## 1. 架构

```
AdvisorRuntime.Tick (主线程, 每帧)
   ├─ 建议模式：现有逻辑不变（DecisionCapture → sidecar → 面板）；全托管期间暂停，避免重复请求
   └─ F9 → AutoPilot.Start() / Stop()

AutoPilot (新, src/Sts2Advisor/AutoPilot/)
   ├─ AutoPilotLoop      取代 AutoSlayer.PlayRunAsync：接管进行中的局，逐房间调 handler，打到结算页停
   ├─ OverlayDrain       抄 AutoSlayer.DrainOverlayScreensAsync / DrainOverlayScreensUntilAsync
   ├─ Handlers           房间/界面 → handler 表（游戏的 + 我们的）
   ├─ ChoiceInjector     Harmony：专用 Rng 实例上的 NextItem / MapScreenHandler.SelectNextRoom
   └─ AdviceFetcher      调 handler 前先 DecisionCapture + 异步问 sidecar，把答案放进 ChoiceInjector
```

**不调用 `AutoSlayer.Start()`**，所以不放弃当前局、不写存档、不退游戏、不装 `AutoSlayCardSelector`、`AutoSlayer.IsActive` 保持 false。

**`AutoPilotLoop`** 从玩家按 F9 时所在的位置接着走，结构同 `PlayRunAsync` 的 while 循环：

1. 先清掉当前 overlay（`OverlayDrain`），再看当前房间：还在房间里没处理完就调该房间的 handler；在地图上就走地图 handler。
2. 房间 handler → 战斗类房间等奖励界面 / 其他房间等 500 ms → `OverlayDrain` → 篝火/事件补点 Proceed（同 `ClickRestSiteProceedIfNeeded` / `ClickEventProceedIfNeeded`）→ Boss 后等换幕 → 地图 handler，循环。
3. 出现 `NGameOverScreen`（胜利或死亡）就停，不点任何按钮。
4. 每次调 handler 都包 `WaitHelper.WithTimeout(handler.Timeout)`。战斗是例外，见下表。

**handler 表**

| 房间/界面 | handler | 选择来自 |
|---|---|---|
| `NRewardsScreen` | 游戏的 `RewardsScreenHandler` | 无选择（全领，药水栏满跳过药水） |
| Treasure 房 | 游戏的 `TreasureRoomHandler` | 无选择 |
| Event 房（含先古对话、假商人） | 游戏的 `EventRoomHandler` | 注入：`event_choice` |
| RestSite 房 | 游戏的 `RestSiteRoomHandler` | 注入：`rest_site` |
| `NDeckUpgradeSelectScreen` / `NDeckTransformSelectScreen` / `NDeckEnchantSelectScreen` / `NDeckCardSelectScreen` / `NSimpleCardSelectScreen` | 游戏的对应 handler | 注入：`card_select` |
| `NChooseACardSelectionScreen` | 游戏的 `ChooseACardScreenHandler` | 注入：`card_select` |
| `NChooseABundleSelectionScreen` | 游戏的 `ChooseABundleScreenHandler` | 注入：`bundle_select` |
| 地图 | 游戏的 `MapScreenHandler` | Harmony 前缀替换 `SelectNextRoom`：`map_select` |
| `NCardRewardSelectionScreen` | **我们的** `CardRewardHandler` | `card_reward`（可跳过：点跳过按钮） |
| Shop 房 | **我们的** `ShopHandler` | `shop`，逐步执行，见下 |
| Monster / Elite / Boss 房 | **我们的** `CombatWaitHandler` | 不选；等 `CombatManager.IsInProgress` 变 false 且奖励界面或地图出现 |
| `NChooseARelicSelection`、`NCrystalSphereScreen`、其他未知界面 | **我们的** `HandBackHandler` | 面板提示"请手动完成此界面"，等该界面离开 overlay 栈后自动继续 |

**`ChoiceInjector`**：`AutoPilot` 持有一个专用 `Rng` 实例，只把它传给 handler。Harmony 前缀挂在 `Rng.NextItem<T>`（引用类型实例共享同一份 JIT 代码，一个补丁覆盖所有 UI 节点类型）上，只在 `ReferenceEquals(__instance, autopilotRng)` 时生效：从 `items` 里找出建议对应的那个节点返回。别的 Rng 一律直通，游戏自己的随机数不受影响。

- **备选**（实测泛型补丁不可靠时）：改挂 `Rng.NextInt(int, int)`，同样按实例过滤；索引由我们按 handler 的同一过滤条件重建列表求得，`count` 对不上就当作失败（见第 2 段）。
- 找不到对应节点、或没有待用的建议时，**不回退到随机**，直接抛异常，由循环停掉托管。

**建议 → UI 节点**：`Capture` 新增按序的选项模型列表（序列化决策 JSON 时用的那份：卡牌奖励的 `CardModel`、事件的 `EventOption`、篝火选项、地图 `MapPoint`、商店条目…）。建议里的下标 → 模型对象 → 持有该对象的 UI 节点（`NCardHolder.CardModel`、`NEventOptionButton.Option`、`NMapPoint.Point`…）。按对象引用匹配，不按名字。

**`AdviceFetcher`**：循环在调某个需要选择的 handler 之前，先等界面稳定（沿用现有"同一指纹连续两次"规则）、`DecisionCapture.Detect` + `Build()`（主线程）、`await advisor.AdviseAsync(json)`（后台），把答案和本次 `Capture` 一起交给 `ChoiceInjector`，然后才调 handler。handler 内部的 `NextItem` 只读缓存，主线程从不阻塞在 HTTP 上。

**`ShopHandler`**：打开商店（`room.OpenInventory()`）后循环：capture → 问 `shop` → 执行这一步 → 等状态变化 → 再问，直到建议是离开；然后关库存、点 Proceed。

- 买卡/遗物/药水：对应 `NMerchantSlot.Entry.OnTryPurchaseWrapper(...)`，期间用 `OverlayDrain` 处理弹出的界面（同 `ShopRoomHandler`）。
- 删牌：点 `NMerchantCardRemoval`，弹出的 `NDeckCardSelectScreen` 走注入的 `card_select`。
- 安全上限 30 步。

**`CombatWaitHandler`**：不设总超时（CombatSolver 暂停时玩家手打，多久都行）。战斗中面板显示"战斗中（CombatSolver）"。开托管时读 `combat_solver_settings.json`：CombatSolver 目录不在或 `autoEnableFullAuto` 不是 true，面板常驻警告"战斗不会自动进行"，托管照常继续。

## 2. 交互与停止

- **热键 F9**：在建议和全托管之间切换。配置 `autopilot_hotkey`，写进 `config.json`，`AdvisorConfig` 加这个键，默认 `F9`。F8 只控制面板显示，隐藏面板不影响托管。
- **托管中的面板**：标题"全托管中 · F9 接管"，下面是最近几步"做了什么 + 一句理由"（理由取自同一次回复的 trace，沿用 `PanelText` 的渲染）。
- **停止**：以下情况全托管停下、回到建议模式，面板写明原因，`Sts2Advisor.log` 记一行。
  1. 玩家按 F9；
  2. 出现结算页（胜利或死亡）；
  3. 离开本局（回主菜单、读取中）或检测到联机；
  4. handler 超时、抛异常，或找不到建议对应的节点；
  5. 同一界面处理 3 次 overlay 栈不变（同 AutoSlay 的死循环检测）；
  6. 建议服务连续不可达超过 10 秒；
  7. `TypeLoadException` / `MissingMemberException`（游戏版本未适配，整个 mod 停用，同现有行为）。
- **停止的实现**：`CancellationTokenSource.Cancel()`；所有 handler 都检查 `ct`。已经发出的点击不撤回。
- **SpeedX 兼容**：所有等待和超时用真实时间（`Task.Delay`、`WaitHelper`、`Stopwatch`），游戏加速只会让条件更快满足。本 mod 不读不写 `Engine.TimeScale` 和 `PrefsSave.FastMode`。
- **只支持单人**，同现有 mod。

## 3. 错误处理、测试、验收

- **trace**：每一步执行都追加到 `advisor_trace.jsonl`，加字段 `executed: true` 和 `autopilot_step`；停止也记一行 `autopilot_stop` + 原因。
- **单元测试**（`src/Sts2Advisor.Core.Tests`，不需要游戏）：
  - 模式状态机（开/停/各停止原因/hand-back 后继续）；
  - 从回复中取下标（每种决策类型，含跳过、离开、越界）；
  - `autopilot_hotkey` 配置解析。
  这些逻辑放在 `Sts2Advisor.Core`，不引用 Godot。
- **补丁自检**：`ChoiceInjector` 在 mod 加载时确认 Harmony 补丁都挂上了（`Harmony.GetPatchInfo`）；任一没挂上，就禁用全托管（面板说明），建议模式照常。
- **实机验收**（mod 没有自动化测试，同现有约定）：
  1. 不开 SpeedX，从 A1 第一个地图节点开始全托管，打完一局（CombatSolver 全自动开）。
  2. 开 SpeedX 再打一局。
  3. 中途按 F9 接管、再按 F9 恢复，各一次，分别发生在地图、事件、商店。
  4. 遇到一次 hand-back 界面（选遗物或水晶球）。
  通过标准：两局都打到结算页，没有意外停止；trace 里每个局外决策都有 `executed: true`；存档里的解锁状态和开托管前一致。
- **回归**：本改动不碰 `src/Sts2Shared/` 的序列化输出。若为了"选项模型列表"改了 `DecisionSerializer`，必须按 CLAUDE.md 跑 `tools/replay_states.py --compare`（逐字节一致）并跑全量 regression。

## 实现修订（2026-10-28，计划 `docs/superpowers/plans/2026-10-28-sts2-advisor-autopilot.md` 与两轮代码评审）

本节取代第 1–3 段里与之冲突的写法。

- **注入**：`HandlerPatches` 在 mod 加载时用 Harmony **transpiler** 改写 9 个 AutoSlay handler 的 `HandleAsync` 状态机 `MoveNext`：
  - 9 个 handler 是 Event、RestSite、DeckUpgrade、DeckTransform、DeckEnchant、DeckCardSelect、SimpleCardSelect、ChooseACard、ChooseABundle；
  - `random.NextItem(list)` 改成 `AutoPilotChoice.Pick<T>(random, list)`；
  - `HandleEventCombat(ct)` 改成 `CombatWait.ForEventCombat(handler, ct)`；
  - 每个 handler 应改写几处写在代码里，对不上就禁用全托管，日志和面板说明原因。

  没有用 `Rng.NextItem<T>` 前缀补丁，原因有两个：泛型方法的补丁影响全局；NextItem 可能被 JIT 内联进调用方，从而绕过补丁。
- **事件战斗作弊被替换**：原 `HandleEventCombat` 会给玩家 +100 力量/覆甲/再生，并直接杀死敌人；改写后只等 CombatSolver 打完。只有游戏自己的 AutoSlay 在跑时才调回原方法。
- **问建议**：
  - `Pick` 先读当前画面。缓存的建议是同一指纹，就按建议、以对象引用找节点。网格里的卡退而按卡牌 id + 升级等级匹配。
  - 没有对应建议时抛 `NeedAdviceException`。循环接住它，等画面稳定、异步问 sidecar，再重跑 handler。handler 在 NextItem 之前不点任何东西，所以可重入。
  - 重问上限：同一决策 5 次，两步之间合计 10 次。
  - 我们自己的 handler 直接 `await` 问建议。
- **事件结束时的"继续"**：游戏会补一个合成的 PROCEED 选项。`Pick` 直接选它，不问建议，也不记步骤。
- **overlay 盖住房间时**：事件 / 篝火的 handler 若在有 overlay 时要选项，`Pick` 抛 `YieldToLoopException`。循环先处理 overlay，再重跑房间 handler。
- **自己的 handler**：
  - 地图（`MapHandler`）：按建议坐标点节点。
  - 奖励页（`RewardsHandler`）：同一奖励页上点过的按钮跨调用记住，跳过的卡牌奖励不会被反复点开。
  - 卡牌奖励（可跳过）。
  - 商店：逐步买 / 删 / 离开。删牌走 `MerchantCardRemovalEntry` 自己的购买入口。整体超时 4 小时，因为每步都有自己的时限。
  - **网格选牌**（`GridSelectHandler`，包住 5 个 Deck*/Simple handler）：
    - 先按建议按下所有卡，再交给原 handler 做预览和确认。原因是原 handler 一旦"确认"可点就停止选牌，范围选牌（Min<Max）或可选 0 张时会少选或空选。
    - 按完后界面自己关掉（定数选择）就直接结束。
    - 原 handler 返回后界面 2 秒内没关，就停。
- **循环是状态驱动的**（不照搬 `PlayRunAsync`），每轮依次看：
  1. 没局了，停（`LeftRun`）。
  2. 结算页，停（`GameOver`）。
  3. 模态弹窗（如新手提示），交还玩家。
  4. 战斗中，等。**战斗判断在 overlay 之前**，战斗里绝不处理 overlay。
  5. 有 overlay，交给对应 handler；没有建议的界面交还玩家。
  6. 地图可走，交给地图 handler。
  7. 按房间：事件有可选项，交给事件 handler；篝火、商店有进展才算完成；宝箱跑一次。
  8. 点房间里可用的 Proceed / 事件"继续"。

  同一房间 handler 连续 3 次没进展就停。什么都做不了时，30 秒停（`Stuck`）。handler 内部出现模态弹窗也交还玩家，等待时间不计入问建议的时限。
- **开关安全**：
  - 停止后旧循环的收尾不会清掉新会话。
  - 停下的全托管里，`Pick` 抛取消异常，**绝不**回退随机。
  - 旧循环还在收尾时按 F9，会记成"待启动"，收尾后自动开始；再按一次取消。
  - 游戏 API 不匹配时整个 mod 停用，不会再自动重启全托管。
- **热键冲突**：`hotkey` 与 `autopilot_hotkey` 相同时禁用全托管，面板开关照常。
- **停止原因**比第 2 段多两个：`BadReply`（建议服务回复格式不对）和 `Disabled`（补丁或配置缺失，开不了）。玩家主动离开本局（`LeftRun`）、按 F9、本局结束这三种用灰字显示，其余用警告色。
- **trace**：
  - 执行的步骤写成 `"autopilot": {"executed": true, "step": n}`（嵌套对象，由 `AdvisorTraceLog.Append(..., autopilotStep: n)` 写出）。
  - 停止写成 `decision: autopilot_stop` 的一行：`reply` 为 null，`error` 为原因。这种行会被 `tests/test_advisor_trace_contract.py` 的读取函数丢掉。
- **已知未在实机验证的路径**（验收重点）：
  - 商店删牌；
  - 网格选牌的按下方式（Pressed 信号还是 grid 的 HolderPressed，按各原 handler 的写法）；
  - 多页事件的重入；
  - 模态弹窗交还；
  - `NSimpleCardSelectScreen` 的范围选择。

## 不做

- 自动开新局、连续多局挂机。
- 自己实现实机战斗；在 CombatSolver 暂停时代打。
- 自己调游戏速度（`TimeScale`、Fast Mode）。
- 半托管、单步执行、主动暂停点。
- 联机。
- 对 greedy 没覆盖的界面（选遗物、水晶球）做随机选择：一律交还玩家。
