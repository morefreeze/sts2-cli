# Sts2Advisor：正式游戏内的局外决策建议 mod — design

Date: 2026-10-21. Approved by the user in chat, section by section. Branch `feat/deterministic-planner`.

## 目标

做一个《杀戮尖塔 2》正式版 mod，在整局爬塔过程中读取**真实游戏**里的状态，在屏幕右下角的面板上显示每个局外决策点的选择建议。战斗不在范围内，交给已上架创意工坊的 [CombatSolver](https://github.com/Torch1230/CombatSolver) mod（它就是本仓库 `src/Sts2Headless/CombatSolverEngine/` 的上游，自带游戏内路线面板和自动出牌）。

建议逻辑复用 A/B 验证过的局外策略 `agent.combat_env.greedy_action`（a1 配合 solver：Ironclad 胜率 13%→58%，Defect 25%→57.5%，Necrobinder 9%→46%，见 CLAUDE.md Protocol notes）。

## 用户决策

- **先自用，再公开。** 第一版在本机跑，可以依赖本仓库和一个本地 Python 进程；架构上为公开版（C# 移植、上传创意工坊）留好接口，但移植本身不在这个 spec 里。
- **路线用现有启发式推荐。** `map_select` 用 `HpAwareMapStrategy` 给出下一个节点，面板上标"未验证"。它在 solver 时代从没和随机路线做过 A/B，唯一做过 A/B 的 value planner 输了（`logs/batches/p3b-route-ab-20261012-160225`，胜场 62→39，p=0.003）。
- **展示方式：只用角落面板。** 不改原生界面，不高亮卡牌或按钮。
- **架构：方案 1。** mod 负责读状态和画面板，建议走可替换的 `IAdvisor`，第一版是 HTTP sidecar。
- **从 `RunSimulator.cs` 拆出共享序列化。** 动 headless 引擎，用逐字节一致的金标测试和完整回归兜底。

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

## 1. 架构与组件

```
正式游戏进程（Godot + sts2.dll）
 └─ Sts2Advisor.dll（mod，affects_gameplay=false）
     ├─ DecisionCapture     参照 AutoSlay：判断"现在是哪个决策点"
     ├─ DecisionSerializer  ← 和 Sts2Headless 共用的源文件：游戏模型 → 决策 JSON
     ├─ IAdvisor
     │    ├─ SidecarAdvisor   本版：HTTP → 127.0.0.1
     │    └─ NativeAdvisor    公开版：C# 移植（本 spec 只留接口）
     └─ AdvisorPanel        Godot CanvasLayer，右下角面板
              ↕ localhost HTTP / JSON
agent/advisor_server.py（.venv）
 └─ 和 play_full_run 同一个 greedy 入口 + 每个选项的分数
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
      "ranking": [{"index": 0, "label": {"en": "...", "zh": "..."}, "score": 7.4}],
      "note": {"en": "...", "zh": "..."},
      "unverified": false,
      "policy": "greedy"}
     ```
   - `action` 永远是 `greedy_action(state)` 的原样输出，通过 `play_full_run._load_greedy_action()` 的同一条导入路径取得。环境变量和 `play_full_run` 的默认值一致：`GREEDY_KNOBS` 全部不设；启动时发现其中任何一个被设置，就打印警告并写进每条回复的 `note`。
   - `ranking` 用 greedy 内部同一套打分函数算出（`score_card_in_deck`、`pick_best_card` 的阈值、`_score_event_option`、`_score_shop_relic`、`_score_shop_potion` 等），**只作展示，不参与选择**。
   - `map_select` 的回复固定 `unverified: true`。
   - `GET /health` 返回策略名和 git commit，给面板显示"已连接"用。
5. **生命周期**：mod 的 `Sts2Advisor.config.json` 放在 dll 旁边，包含 `repo_path`、`python`（默认 `<repo>/.venv/bin/python`）、`port`、`autostart`（默认 `true`）、`hotkey`（默认 `F8`）。
   - `autostart` 时，mod 在初始化阶段启动一次 `python agent/advisor_server.py --port N`，游戏退出时结束它。端口已经被一个健康的服务占用时，直接复用，不再启动。
   - 连不上时面板显示"建议服务离线"，不影响游戏。
6. **战斗**：`combat_play` 时 mod 不发任何请求，面板缩成一行"战斗中，交给 CombatSolver"。不声明对 CombatSolver 的依赖。

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
- 标题行：决策类型、进度（`A1F7a1`，记法见 CLAUDE.md Conventions）、HP、金币。
- 推荐项：`action` 对应的选项名；`action` 是 `leave_room`、`skip_card_reward` 时显示"离开"或"跳过"。
- 排序列表：`ranking` 的前 5 项及其分数。
- 一行 `note`，例如"跳过阈值 5.5"或"路线：启发式，未经 A/B 验证"。
- 文字语言跟随游戏，简体中文显示 `zh`，其他语言显示 `en`。`F8`（可配置）显示或隐藏面板。

## 3. 错误处理、测试、安装

**错误处理：mod 永远不能把游戏搞崩**
- 每次 `Timer` 回调和每个 postfix 都包在 try/catch 里。异常写进 mod 目录下的 `Sts2Advisor.log`，面板显示"读取失败：<界面名>"。同一种界面连续失败 3 次就停止对它取数，直到换到别的界面。
- sidecar 连不上或超时，面板显示"建议服务离线"，按指数退避重连（1 秒起，最长 30 秒）。自动拉起每次游戏启动只尝试一次。
- greedy 抛异常或返回 `None` 时，服务端回 `{"action": null, "note": "无建议"}`。**不用 naive 兜底**：占位策略不能被当作建议显示出来。
- 游戏更新导致类型或成员缺失（`TypeLoadException`、`MissingMethodException`、`MissingFieldException`）时，捕获后面板显示"游戏版本未适配"，停止取数。

**测试**
1. **序列化重构金标**：重构前，用固定种子、`STS2_SOLVER_CHARS=none`（启发式战斗，可复现）、每个角色几局，用 `--keep-game-logs` 跑出基线日志。重构后用同样设置再跑，逐字节比较每个日志里所有 `type == "state"` 行的 `data` 对象（不比 `ts`、`run_id` 这些每次都会变的外层字段），必须完全一致。然后按 CLAUDE.md 跑完整回归：`run_batch.py --name regression --kind regression --games 5 --chars all`，每条 lane 都要 `Completed: 5/5`。
2. **Godot 桩审计**：`DecisionSerializer.cs` 不引用 Godot。`tools/audit_godot_stub_refs.py` 仍须输出 `TOTAL missing: 0`，`tools/jit_probe.py` 通过。
3. **`advisor_server` 测试**：从保留下来的 `logs/batches/*/games/*.jsonl` 里抽出所有局外决策状态作为语料，测试只读取，不依赖 `logs/` 顶层那些 7 天后会被清理的文件。
   - `/advise` 的 `action` 必须等于直接调 `greedy_action` 的结果。
   - 对能打分的决策类型，统计 `ranking[0]` 和 `action` 不一致的比例。允许不一致的情形在测试里逐条写明，例如 quality gate 过滤、跳过阈值。
   - `map_select` 必须是 `unverified: true`；`GREEDY_KNOBS` 被设置时必须有警告。
4. **mod 纯逻辑单元测试**：指纹计算、面板文案、回复解析、退避计时放在不依赖 Godot 的类里，用一个单独的 C# 测试工程测。
5. **字段契约测试**：mod 把每次发出的决策 JSON 和收到的回复追加写进 `advisor_trace.jsonl`。第一次实机游玩后，把这个文件存为测试夹具。一个 Python 测试按决策类型，逐个比对它和 headless 日志里同类型决策的字段集合与类型。这一步专门抓"真实游戏里某个字段为空或格式不同"的问题，而 greedy 会对这些问题静默地给出错误建议。
6. **实机验收**：在正式游戏里用 mod 打一整局（任选角色，a1）。
   - 覆盖表里每种界面都出现过建议或"此处暂无建议"。
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
- `NativeAdvisor`（greedy 的 C# 移植）和创意工坊上传：另起 spec。移植的验收方式定为：用第 3 段第 3 条的同一份语料逐条比对 C# 和 Python 的 `action`。
- 路线启发式和随机路线的 A/B。
