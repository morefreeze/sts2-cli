# Sts2Advisor：正式游戏内的局外决策建议 mod — design

Date: 2026-10-21. Approved by the user in chat, section by section. Branch `feat/deterministic-planner`.
Revised 2026-10-21（用户要求）：建议的依据改为从 greedy 同一条代码路径产出的 trace；地图启发式修楼层语义；商店的 Ironclad 删牌规则只展示不改。

## 目标

做一个《杀戮尖塔 2》正式版 mod，在整局爬塔过程中读取**真实游戏**里的状态，在屏幕右下角的面板上显示每个局外决策点的选择建议。战斗不在范围内，交给已上架创意工坊的 [CombatSolver](https://github.com/Torch1230/CombatSolver) mod（它就是本仓库 `src/Sts2Headless/CombatSolverEngine/` 的上游，自带游戏内路线面板和自动出牌）。

建议逻辑复用 A/B 验证过的局外策略 `agent.combat_env.greedy_action`（a1 配合 solver：Ironclad 胜率 13%→58%，Defect 25%→57.5%，Necrobinder 9%→46%，见 CLAUDE.md Protocol notes）。

## 用户决策

- **先自用，再公开。** 第一版在本机跑，可以依赖本仓库和一个本地 Python 进程；架构上为公开版（C# 移植、上传创意工坊）留好接口，但移植本身不在这个 spec 里。
- **路线用现有启发式推荐。** `map_select` 用 `HpAwareMapStrategy` 给出下一个节点，面板上标"未验证"。它在 solver 时代从没和随机路线做过 A/B，唯一做过 A/B 的 value planner 输了（`logs/batches/p3b-route-ab-20261012-160225`，胜场 62→39，p=0.003）。
- **展示方式：只用角落面板。** 不改原生界面，不高亮卡牌或按钮。
- **架构：方案 1。** mod 负责读状态和画面板，建议走可替换的 `IAdvisor`，第一版是 HTTP sidecar。
- **从 `RunSimulator.cs` 拆出共享序列化。** 动 headless 引擎，用逐字节一致的金标测试和完整回归兜底。
- **依据就是 greedy 做决定时实际比较的数。** 给 `greedy_action` 加一个可选的 trace 记录器，在做决定的同一段代码里记录分项和规则判断，不另外重算，也不事后生成理由。打开 trace 不能改变任何一个选择。
- **地图启发式修楼层语义**（第 1 段第 6 条）。面板上继续标"未验证"。
- **商店的"删基础牌"规则只对 Ironclad 生效，保持不改**（这是 A/B 实测过的配置），面板依据里如实显示。
- **面板不显示胜率或任何概率。** 分数都是启发式打分；A/B 只验证过整套策略，没有验证过其中单独某一项。

## 背景事实（2026-10-21 已核实）

- **正式版自带 mod 加载器**（`MegaCrit.Sts2.Core.Modding.ModManager`，游戏 v0.111.0）。
  - 从 `<可执行文件目录>/mods/` 递归找 `*.json` 清单。macOS 上这个目录是 `SlayTheSpire2.app/Contents/MacOS/mods/`。Steam Workshop 订阅的 mod 也会加载。
  - 清单字段（`ModManifest`）：`id`、`name`、`author`、`description`、`version`、`has_pck`、`has_dll`、`dependencies`（`[{id, min_version}]`）、`affects_gameplay`（默认 `true`）、`min_game_version`。
  - 加载的是 `<mod 目录>/<id>.dll`，进 `sts2.dll` 所在的同一个 `AssemblyLoadContext`，所以 mod 可以直接使用真实的游戏类型、`GodotSharp` 和 `0Harmony`（游戏自带）。
  - 带 `[ModInitializer("方法名")]` 的类会被调用它的初始化方法；没有的话加载器会对整个程序集执行 `Harmony.PatchAll`。
  - 玩家第一次必须在弹窗里同意加载 mod（`PlayerAgreedToModLoading`）；启动参数 `--nomods` 会跳过所有 mod。
  - `affects_gameplay: false` 的 mod 不参与联机的 mod 一致性校验。
- **游戏自带官方自动打牌器 `MegaCrit.Sts2.Core.AutoSlay`。** 每个房间、每个界面都有 handler（`Handlers/Rooms/*`、`Handlers/Screens/*`），它判断当前决策点靠的是**轮询**：
  - `NOverlayStack.Instance.Peek()` 返回的屏幕类型，每个 `IScreenHandler` 声明自己的 `ScreenType`，比如 `CardRewardScreenHandler.ScreenType == typeof(NCardRewardSelectionScreen)`；
  - `runState.CurrentRoom.RoomType`；
  - `NMapScreen.Instance.IsOpen`。

  它是"真实游戏里每个决策点的数据在哪"的权威参照。
- **`greedy_action(state)` 是纯函数。** 只读一个 state dict 和几个环境变量开关（`play_full_run.GREEDY_KNOBS`），不连引擎（`STS2_MC_ROLLOUT` 默认关）。卡牌打分按 card **id** 查表（`card_scoring.score_card`），和语言无关。
- **headless 的状态构造函数吃的是游戏模型对象**：`EventChoiceState(EventRoom)`、`RestSiteState(RestSiteRoom)`、`ShopState(MerchantRoom, Player)`、`MapSelectState()`、`PlayerSummary(Player)`、`RunContext()`。正式游戏的 `RunState.CurrentRoom` 是同样的类型。但这些函数和 headless 的控制流混在一起（`ForceToMap()`、`_pendingCardReward`、`_cardSelector`），必须先拆开才能共用。
- 游戏运行时是 `net9.0`（`sts2.runtimeconfig.json`），和本仓库的构建一致。
- **greedy 各决策的实际判断方式**（决定了能展示哪些依据）：
  - 卡牌奖励：`pick_best_card` 的总分由 9 项相加：`score_card_in_deck`（基础评级 + 牌组协同）、组合预测 `predictor_v2_set_bonuses`、标签协同、战斗类型、Boss 格挡缺口、Boss 就绪、牌组质量提升、流派（三种取最大）、历史 Q（`q_adjustment`）。最高分低于跳过阈值时跳过；阈值由幕数和牌组是否 ≥18 张决定。第 1 幕第 12 层起，质量门 `is_act1_card_reward_eligible` 先过滤候选（其他幕一律放行）。
  - 篝火：`rest_site_action` 按固定顺序判断：必须休息线、Vantom 规则（`STS2_VANTOM_REST_HEAL`，默认关）、Boss 前区段、必升卡（`best_smith_target`）、本层休息阈值、默认锻造。
  - 事件：`_score_event_option` 对选项英文标题和描述做关键词匹配并加减分，取最高分；锁定的选项排除。
  - 商店：一串有先后的规则，第一条成立的就是这一步的动作：低 HP 买回血药水 → 删基础牌 → 删牌价值高于最佳卡 → 卡 ≥8.0 → 卡 ≥ 买卡阈值（4.5/5.0/5.5）→ 遗物分 ≥4.0 且至少留 25 金 → 药水分 ≥4.5 → 离开。
  - 卷轴盒：每包各卡 `score_card` 之和取最大。选牌：先按房间类型、有没有低于 3 分的卡、选项是否超过 10 个，判断这次是"挑高分"还是"挑低分"，再排序。
  - 地图：`HpAwareMapStrategy` 只看**下一个**节点。每个可选节点按房间类型得一个优先级（越小越好），再按 HP、金币、楼层加减，取最小。
- **`HpAwareMapStrategy` 的楼层语义错了。** 它用 `floor >= 16`（`ACT2_START`）判断"第 2 幕"，用 `floor >= 19`（`ACT2_BOSS_ZONE`）判断"第 2 幕 Boss 前"，但 `context.floor` 是幕内楼层（`_runState.ActFloor`，每幕从 1 开始）。结果"第 2 幕"规则在每一幕的最后几层生效，`ACT2_BOSS_ZONE` 永远不触发。卡牌奖励和商店的同类判断早已改成读 `context.act`，地图这里漏了。经 `greedy_action` 走地图的只有 `CombatEnv`（RL 环境）、`eval_rl` 和 `coordinator`；`play_full_run` 的回归和 A/B 批次用随机路线，不经过它。
- **商店的删基础牌规则默认只统计 `STRIKE_IRONCLAD` 和 `DEFEND_IRONCLAD`**，设了 `STS2_BASIC_PURGE_ALL` 才统计五个角色的基础牌。A/B 测的是默认配置，所以其他四个角色在实测里从不触发这条规则。

## 1. 架构与组件

```
正式游戏进程（Godot + sts2.dll）
 └─ Sts2Advisor.dll（mod，affects_gameplay=false）
     ├─ DecisionCapture     参照 AutoSlay：判断"现在是哪个决策点"
     ├─ DecisionSerializer  ← 和 Sts2Headless 共用的源文件：游戏模型 → 决策 JSON
     ├─ IAdvisor
     │    ├─ SidecarAdvisor   本版：HTTP → 127.0.0.1
     │    └─ NativeAdvisor    公开版：C# 移植（本 spec 只留接口）
     ├─ TraceText           依据 trace → 中/英文案（不依赖 Godot）
     └─ AdvisorPanel        Godot CanvasLayer，右下角面板
              ↕ localhost HTTP / JSON
agent/advisor_server.py（.venv）
 └─ greedy_action(state, trace=recorder)：和 play_full_run 同一个入口，
    action 原样返回，依据来自同一次调用的 trace
```

1. **`src/Sts2Advisor/`**：新建 C# 工程，`net9.0`。
   - 引用 Steam 安装目录（`$STS2_GAME_DIR`，默认值同 `python/run_batch.py` 的 `DEFAULT_GAME_DIR`）里**真实的** `sts2.dll`、`GodotSharp.dll`、`0Harmony.dll`，设 `Private=false` 不复制进输出。不用本仓库的 `src/GodotStubs`，也不用 `lib/` 里 IL 打过补丁的 `sts2.dll`。
   - 产出 `Sts2Advisor.dll` 和清单 `Sts2Advisor.json`：`id: "Sts2Advisor"`、`has_dll: true`、`has_pck: false`、`affects_gameplay: false`、`min_game_version: "0.111.0"`、`dependencies: []`。
   - 入口是 `[ModInitializer]` 类：读配置，按需拉起 sidecar，把 `AdvisorPanel` 挂到场景树根节点。
   - 本版不需要 Harmony 补丁来识别决策点（见第 2 段）；只在轮询拿不到的数据上加只读 postfix。
2. **`DecisionSerializer`**：新共享源文件 `src/Sts2Shared/DecisionSerializer.cs`，两个工程都用 `<Compile Include=... Link=...>` 编进去。
   - 内容是从 `RunSimulator.cs` 抽出来的"模型 → `Dictionary<string, object?>`"纯函数：卡牌、玩家摘要、`RunContext`、事件、篝火、商店、地图、卡牌奖励、选牌、卷轴盒。
   - 输入是游戏模型对象和一个 `ILocSource`。不读 `RunSimulator` 的私有状态，不推进游戏，**不引用任何 Godot 类型**。
   - `RunSimulator` 保留自己的控制流（何时 `ForceToMap`、待决的 selector/bundle），改为调用这些函数。
3. **`ILocSource`**：只有 `Card(entry)`、`Bilingual(table, key)` 这些现在 `LocLookup` 上被序列化用到的方法。
   - headless：现有的 `LocLookup`（读仓库里的 `localization_eng/`、`localization_zhs/`）。
   - mod：同一个实现，读配置里 `repo_path` 下的同一份目录，所以字符串和 headless 完全一致。
4. **`agent/advisor_server.py`**：只绑 `127.0.0.1`，端口来自配置（默认 `8767`，避开 workbench 的 8765）。
   - `POST /advise`，请求体是一个决策 JSON，返回：
     ```json
     {"action": {...} | null,
      "options": [{"index": 0, "label": {"en": "...", "zh": "..."}, "score": 7.4,
                   "status": "ok",
                   "components": [{"key": "card.base", "value": 6.8, "detail": "A"},
                                  {"key": "card.boss_defense_gap", "value": 0.9}]}],
      "rules": [{"key": "rest.critical_heal", "outcome": "skipped",
                 "values": {"hp_ratio": 0.51, "threshold": 0.40}}],
      "warnings": [],
      "unverified": false,
      "policy": "greedy"}
     ```
   - `action` 永远是 `greedy_action(state, trace=recorder)` 的原样输出，通过 `play_full_run._load_greedy_action()` 的同一条导入路径取得。`options`、`rules` 都来自**同一次调用**里的 recorder，服务端不另外打分。
   - `options[].status`：`ok`、`filtered`（被质量门过滤）、`locked`（事件选项锁定）、`unaffordable`（商店买不起）、`broken`（在 `BROKEN_CARDS` 里）。
   - `rules[].outcome`：`fired`（这条规则决定了动作）、`skipped`（条件不成立，继续看下一条）。一个决策里最多一条 `fired`。
   - `warnings`：会改变 greedy 行为的环境变量和 `play_full_run` 的默认值不一致时，在这里列出。检查范围是 `GREEDY_KNOBS`，再加上 `STS2_CARD_PICK_TEMP`、`STS2_MC_ROLLOUT`、`STS2_VANTOM_REST_HEAL`；默认全部不设。任何一个被设置，启动时打印警告，并在每条回复里带上它。
   - `map_select` 的回复固定 `unverified: true`。
   - `GET /health` 返回策略名和 git commit，给面板显示"已连接"用。
5. **greedy trace**（改 `agent/combat_env.py`、`agent/card_scoring.py`、`agent/strategy.py`）：
   - `greedy_action(state, trace=None)`。`trace` 是一个 recorder，提供 `option(index, score, components, status)` 和 `rule(key, outcome, values)`。它被一路传进 `pick_best_card`、`rest_site_action`、`_score_event_option`、商店分支、`HpAwareMapStrategy.choose` 等真正做判断的函数。
   - recorder 只在判断发生的地方被调用。`trace is None` 时代码路径和现在完全一样。
   - `pick_best_card` 改成先构建一张"每张卡 × 9 个分项"的表，再按行求和取最大。它现在本来就是先算 9 个列表再相加，所以这只是换一种写法，不改变算式。
   - 每个 `key` 是稳定的字符串，比如 `card.base`、`card.set_bonus`、`rest.must_upgrade`、`shop.remove_basic`、`event.kw.rare_card`、`map.elite_hp_low`。`_score_event_option` 的每个 `if` 分支各有一个 key。key 列表写在一个模块常量里，测试会检查它。
   - `shop.remove_basic` 的 `values` 里带 `basic_scope: "ironclad_only"`（默认）或 `"all"`（设了 `STS2_BASIC_PURGE_ALL`）和统计到的张数。
6. **地图楼层语义修复**（改 `agent/strategy.py`）：
   - "第 2 幕及以后"的判断从 `floor >= ACT2_START` 改成 `context.act >= 2`。
   - `ACT2_BOSS_ZONE` 改成 `act >= 2 且幕内楼层 >= 13`，和第 1 幕已有的 Boss 前阈值 `floor >= 13` 对齐。
   - 其他阈值（3、6、12、13）保持不变，继续按幕内楼层在每一幕生效。`ELITE_AVOID_FLOOR = 3` 的注释写的是"第 1 幕"，但它实际在每一幕的前两层都生效；这次不改它的范围。因为 `in_act2` 分支排在前面，修复后"满血偏好精英"（第 7–12 层）和"Boss 前回避精英 +3"（第 13 层起）只在第 1 幕生效，这和原作者按全局楼层写的分支顺序一致。
   - 只影响经 `greedy_action` 走地图的调用方：`CombatEnv`、`eval_rl`、`coordinator`。`play_full_run` 的回归和 A/B 不经过它，所以已有的实测结论都不受影响。
   - 修了之后仍然没有和随机路线做过 A/B，所以 `unverified` 保持 `true`。
7. **生命周期**：mod 的 `Sts2Advisor.config.json` 放在 dll 旁边，包含 `repo_path`、`python`（默认 `<repo>/.venv/bin/python`）、`port`、`autostart`（默认 `true`）、`hotkey`（默认 `F8`）。
   - `autostart` 时，mod 在初始化阶段启动一次 `python agent/advisor_server.py --port N`，游戏退出时结束它。端口已经被一个健康的服务占用时，直接复用，不再启动。
   - 连不上时面板显示"建议服务离线"，不影响游戏。
8. **战斗**：`combat_play` 时 mod 不发任何请求，面板缩成一行"战斗中，交给 CombatSolver"。不声明对 CombatSolver 的依赖。

## 2. 数据流与决策点覆盖

**一次建议的流程**

1. `AdvisorPanel` 上挂一个 Godot `Timer`，每 0.3 秒在**主线程**按 AutoSlay 的方式判断当前决策点：先看 `NOverlayStack.Instance.Peek()` 的屏幕类型，再看 `NMapScreen.Instance.IsOpen`，最后看 `RunManager.Instance.DebugOnlyGetState()`（AutoSlay 取 `RunState` 用的就是它）的 `CurrentRoom` 类型。
2. 计算**决策指纹**：决策类型、各选项 id（或卡牌 id 加升级状态）、金币、牌组大小、HP、事件页 id。指纹没变就什么都不做。
3. 指纹一变，就在主线程调 `DecisionSerializer` 生成决策 JSON。游戏状态不是线程安全的，只能在主线程读。
4. 在后台线程发 `POST /advise`，超时 2 秒。
5. 结果用 `CallDeferred` 回到主线程。如果当前指纹已经和发请求时不同，结果作废，直接丢弃；否则刷新面板。
6. 只有轮询拿不到的数据才加 Harmony postfix，而且只读。例如选牌的 `min_select`/`max_select` 只作为 `CardSelectCmd.FromDeckFor*` 和 `CardSelectorPrefs` 的参数传进来：postfix 把它们存到一个静态字段，界面关闭时清掉。

**覆盖范围**（界面列以 AutoSlay handler 为准）

| 游戏界面 | 决策 JSON | 建议来源 |
|---|---|---|
| Neow / 事件（`EventRoomHandler`） | `event_choice` | greedy，按选项打分 |
| 地图（`MapScreenHandler`） | `map_select` | `HpAwareMapStrategy`，`unverified: true` |
| 卡牌奖励（`CardRewardScreenHandler`） | `card_reward` | greedy，给出各卡分数和跳过阈值 |
| 篝火（`RestSiteRoomHandler`） | `rest_site` | `rest_site_action` |
| 商店（`ShopRoomHandler`） | `shop` | greedy：下一件买什么，或者离开 |
| 卷轴盒（`ChooseABundleScreenHandler`） | `bundle_select` | greedy |
| 升级 / 删牌 / 变化 / 附魔 / 选牌（`DeckUpgradeScreenHandler`、`DeckCardSelectScreenHandler`、`DeckTransformScreenHandler`、`DeckEnchantScreenHandler`、`SimpleCardSelectScreenHandler`、`ChooseACardScreenHandler`） | `card_select` | greedy |
| 选遗物（`ChooseARelicScreenHandler`）、水晶球（`CrystalSphereScreenHandler`）、宝箱、奖励总览（`RewardsScreenHandler`）、联机 | — | 面板显示"此处暂无建议" |
| 战斗（`CombatRoomHandler`） | — | 不出声 |

商店、选牌和多页事件里，greedy 本来就是"一步一个动作"。面板只显示**当前这一步**的建议；玩家操作后指纹变化，自动刷新成下一步。这和 `play_full_run` 调 greedy 的方式一致（商店每次购买后都重新调一次）。

**面板内容**
- 标题行：决策类型、进度（`A1F13a1`，记法见 CLAUDE.md Conventions）、HP、金币。
- 推荐项：`action` 对应的选项名；`action` 是 `leave_room`、`skip_card_reward` 时显示"离开"或"跳过"。
- 依据：按决策类型取 trace 里的内容，见下表。
- 底部一行来源：`greedy 启发式打分，不是概率`；地图改成`启发式，只看下一步，未经 A/B 验证`。`warnings` 不为空时，在这一行前面再加一行警告。
- 文字语言跟随游戏，简体中文显示 `zh`，其他语言显示 `en`。`F8`（可配置）显示或隐藏面板。

**各决策点的依据**

| 决策 | 推荐项 | 依据（全部来自同一次调用的 trace） |
|---|---|---|
| 卡牌奖励 | 推荐卡，或"跳过" | 每张卡的总分；每张卡贡献最大的 2–3 个非零分项，`card.base` 总是显示并带评级字母；跳过阈值及其来源（第几幕、牌组是否 ≥18 张）；被质量门过滤的卡标"已过滤"，`BROKEN_CARDS` 里的卡标"不可用"。 |
| 篝火 | 休息或锻造 | 按顺序列出每条规则，`skipped` 的灰色、`fired` 的高亮，每条都带比较的两个数，例如"HP 51% < 本层休息阈值 85%"。 |
| 事件 | 推荐选项 | 每个可选选项的分数和命中的关键词（带加减分，例如"稀有卡 +8""失去 HP −2"）；锁定的选项标"已锁定"；最后一行注明"按选项英文描述的关键词打分"。 |
| 商店 | 这一步：买什么、删牌或离开 | 从第一条规则列到 `fired` 那条，每条带比较的数，例如"删牌价值 3.1，不高于最佳卡 6.2"、"最佳卡 6.2 ≥ 买卡阈值 4.5（第 2 幕）"。非 Ironclad 角色时，`shop.remove_basic` 这一行写明"只统计 Ironclad 的打击和防御（实测配置），已跳过"。最后一行"买完会重新评估下一步"。 |
| 卷轴盒 | 推荐哪一包 | 每包的总分，以及包里各卡的分数。 |
| 选牌 | 推荐哪几张 | 先写这次按"挑高分"还是"挑低分"处理，以及判断依据（房间类型、有没有低于 3 分的卡、选项是否超过 10 个）；再列各卡分数。 |
| 地图 | 推荐下一个节点 | 每个可选节点的类型、优先级（越小越好）和各项加减，例如"精英：HP 45% < 60%，+7"。 |

`TraceText` 按 `key` 把 trace 渲染成中、英文案，所有模板集中在这一个类里。遇到没有模板的 key，原样显示 key 和 values，不丢弃。选项名、卡名、遗物名用决策 JSON 里已有的双语 `label`，也就是游戏官方译名；其余文字是 mod 自己的界面文案，按 CLAUDE.md 的 `t(en, zh)` 约定提供双语。

## 3. 错误处理、测试、安装

**错误处理：mod 永远不能把游戏搞崩**
- 每次 `Timer` 回调和每个 postfix 都包在 try/catch 里。异常写进 mod 目录下的 `Sts2Advisor.log`，面板显示"读取失败：<界面名>"。同一种界面连续失败 3 次就停止对它取数，直到换到别的界面。
- sidecar 连不上或超时，面板显示"建议服务离线"，按指数退避重连（1 秒起，最长 30 秒）。自动拉起每次游戏启动只尝试一次。
- greedy 抛异常或返回 `None` 时，服务端回 `{"action": null, "options": [], "rules": [], "warnings": ["greedy_raised: <异常类型>"]}`（或 `greedy_none`），面板显示"无建议"。**不用 naive 兜底**：占位策略不能被当作建议显示出来。trace 记录本身出错时同样处理：宁可不显示依据，也不显示和选择对不上的依据。
- 游戏更新导致类型或成员缺失（`TypeLoadException`、`MissingMethodException`、`MissingFieldException`）时，捕获后面板显示"游戏版本未适配"，停止取数。

**测试**
1. **序列化重构金标**：重构前，用固定种子、`STS2_SOLVER_CHARS=none`（启发式战斗，可复现）、每个角色几局，用 `--keep-game-logs` 跑出基线日志。重构后用同样设置再跑，逐字节比较每个日志里所有 `type == "state"` 行的 `data` 对象（不比 `ts`、`run_id` 这些每次都会变的外层字段），必须完全一致。然后按 CLAUDE.md 跑完整回归：`run_batch.py --name regression --kind regression --games 5 --chars all`，每条 lane 都要 `Completed: 5/5`。
2. **Godot 桩审计**：`DecisionSerializer.cs` 不引用 Godot。`tools/audit_godot_stub_refs.py` 仍须输出 `TOTAL missing: 0`，`tools/jit_probe.py` 通过。
3. **`advisor_server` 测试**：从保留下来的 `logs/batches/*/games/*.jsonl` 里抽出所有局外决策状态作为语料，测试只读取，不依赖 `logs/` 顶层那些 7 天后会被清理的文件。
   - **trace 不改变选择**：对语料里每一条状态，`greedy_action(state, trace=recorder)` 和 `greedy_action(state)` 的返回必须完全相等。
   - `/advise` 的 `action` 必须等于直接调 `greedy_action(state)` 的结果。
   - **依据和选择一致**：卡牌奖励、卷轴盒、事件、地图里，被选中的选项必须是 `status == "ok"` 中分数最高的（地图是优先级最小的）；卡牌奖励选择跳过时，最高分必须低于记录下来的阈值。选牌里，选中的就是按记录的模式（挑高分或挑低分）排序后的前 `max_select` 张。篝火和商店里，恰好一条规则 `fired`，而且它对应的动作就是 `action`。
   - **分项加起来等于总分**：每张卡的 `components` 之和等于它的 `score`（浮点误差 1e-9 以内）。
   - 所有出现过的 `key` 都在模块常量的 key 列表里。
   - `map_select` 必须是 `unverified: true`；`GREEDY_KNOBS` 被设置时 `warnings` 不为空。
   - 现有的 greedy 相关测试（`tests/test_ooc_policy.py` 等）全部照常通过。
4. **地图修复的单元测试**：构造幕内楼层的 `map_select` 状态，断言第 1 幕第 16、17 层**不再**套用第 2 幕规则；第 2 幕第 2 层**会**套用；`ACT2_BOSS_ZONE` 在第 2、3 幕第 13 层起生效，在第 1 幕不生效。
5. **mod 纯逻辑单元测试**：指纹计算、`TraceText` 渲染、回复解析、退避计时放在不依赖 Godot 的类里，用一个单独的 C# 测试工程测。`TraceText` 必须覆盖 key 列表里的每一个 key（测试从 Python 侧导出的 key 列表读取）；没有模板的 key 原样显示。
6. **字段契约测试**：mod 把每次发出的决策 JSON 和收到的回复追加写进 `advisor_trace.jsonl`。第一次实机游玩后，把这个文件存为测试夹具。一个 Python 测试按决策类型，逐个比对它和 headless 日志里同类型决策的字段集合与类型。这一步专门抓"真实游戏里某个字段为空或格式不同"的问题，而 greedy 会对这些问题静默地给出错误建议。
7. **实机验收**：在正式游戏里用 mod 打一整局（任选角色，a1）。
   - 覆盖表里每种界面都出现过建议或"此处暂无建议"；有建议的界面都显示了依据表里对应的内容。
   - `Sts2Advisor.log` 里没有未捕获异常，游戏不崩。
   - `F8` 能切换面板；关掉 sidecar 后面板显示离线，重启 sidecar 后能自动恢复。

**安装（自用阶段）**
- `tools/install_advisor_mod.sh`：
  - 编译 `src/Sts2Advisor`；
  - 把 `Sts2Advisor.dll` 和 `Sts2Advisor.json` 拷进 `SlayTheSpire2.app/Contents/MacOS/mods/Sts2Advisor/`；
  - 配置文件不存在时，生成 `Sts2Advisor.config.json`（`repo_path` 填当前仓库）。
- 第一次启动游戏会弹窗问是否加载 mod，选同意。Steam 更新或"验证文件完整性"可能清掉 app 包里的 `mods/`，那时重跑脚本即可。

## 不做

- 自动执行选择；在原生界面上高亮。
- 选遗物、水晶球、奖励总览的建议；联机。
- `NativeAdvisor`（greedy 的 C# 移植）和创意工坊上传：另起 spec。移植的验收方式定为：用第 3 段第 3 条的同一份语料，逐条比对 C# 和 Python 的 `action` 和 trace（`options`、`rules`）。
- 路线启发式和随机路线的 A/B。
- 在面板上显示胜率或任何概率。
