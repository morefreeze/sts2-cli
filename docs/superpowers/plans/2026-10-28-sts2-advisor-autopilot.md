# Sts2Advisor 全托管（AutoPilot）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Sts2Advisor mod 里加一个 F9 全托管模式：按 greedy 的建议亲自点完本局所有局外决策，战斗交给 CombatSolver，打到结算页停。

**Architecture:** 复用游戏自带 AutoSlay（`MegaCrit.Sts2.Core.AutoSlay`）的 handler 做界面点击，但**不调 `AutoSlayer.Start()`**。我们自己的状态驱动循环 `AutoPilotLoop` 每一轮看当前画面（overlay → 战斗 → 地图 → 房间）决定调哪个 handler。AutoSlay handler 里的 `random.NextItem(list)` 和 `HandleEventCombat(ct)`（作弊：+100 力量并秒杀敌人）用 Harmony **transpiler** 改写成调用我们的 `AutoPilotChoice.Pick<T>` 和 `CombatWait.ForEventCombat`。没有缓存的建议时，`Pick` 抛 `NeedAdviceException`；循环接住后异步问 sidecar，再重跑这个 handler（handler 都从当前 UI 状态读起，可重入），主线程不阻塞。卡牌奖励（要能跳过）、奖励页（跳过后不能再点开）、地图、商店用我们自己的 handler。可测试的纯逻辑（建议 → 动作、会话状态机、面板文字、CombatSolver 设置读取、配置、trace）放在 `Sts2Advisor.Core`，用 xunit 测。

**Tech Stack:** C# / .NET 9、Godot 4 C#（游戏自带）、Harmony 2.4.2（游戏自带 `0Harmony.dll`）、Krafs.Publicizer（访问游戏私有成员）、xunit。

**Spec:** `docs/superpowers/specs/2026-10-28-sts2-advisor-autopilot-design.md`。本计划在三处偏离 spec 的写法（Task 13 会回写 spec）：

1. 注入用 transpiler 改写 handler 的 `MoveNext`，不用 `Rng.NextItem<T>` 前缀补丁（前缀可能被 JIT 内联绕过，而且会影响全局）。
2. 地图和奖励页用我们自己的 handler，不 patch `SelectNextRoom`。
3. 循环是"每轮看画面再分派"，不照搬 `PlayRunAsync` 的线性流程，这样从任意位置接管都成立。

**背景事实（2026-10-28 反编译 `lib/sts2.dll.original` 核实；反编译方法见 Task 0）：**

- AutoSlay handler 都是 `IHandler`：`Task HandleAsync(Rng random, CancellationToken ct)` + `TimeSpan Timeout`；界面 handler 还有 `IScreenHandler.ScreenType`。
- `random.NextItem(list)` 出现在：
  - `EventRoomHandler`（1 处，外加 2 处 `HandleEventCombat(ct)`）
  - `RestSiteRoomHandler`
  - `DeckUpgradeScreenHandler` / `DeckTransformScreenHandler` / `DeckEnchantScreenHandler` / `DeckCardSelectScreenHandler` / `SimpleCardSelectScreenHandler`
  - `ChooseACardScreenHandler`
  - `ChooseABundleScreenHandler`

  以上各 1 处，都在 `HandleAsync` 的 async 状态机 `MoveNext` 里。
- 列表元素和模型的对应：`NCardHolder.CardModel`（`NGridCardHolder` 是子类）、`NEventOptionButton.Option`、`NRestSiteButton.Option`、`NCardBundle.Bundle`、`NMapPoint.Point`、`NMerchantSlot.Entry`。
- 卡牌奖励的跳过：`NCardRewardSelectionScreen._extraOptions[j].OptionId == "Skip"`，对应按钮是 `_rewardAlternativesContainer` 里第 j 个 `NCardRewardAlternativeButton`。
- greedy 动作（`agent/combat_env.py`）：

  | 决策 | 动作 |
  |---|---|
  | `map_select` | `select_map_node{col,row}` |
  | `card_reward` | `select_card_reward{card_index}` / `skip_card_reward` |
  | `event_choice`、`rest_site` | `choose_option{option_index}` |
  | `card_select` | `select_cards{indices:"1,3"}` / `skip_select` |
  | `bundle_select` | `select_bundle{bundle_index}` |
  | `shop` | `buy_card{card_index}` / `buy_relic{relic_index}` / `buy_potion{potion_index}` / `remove_card` / `leave_room` |

  下标对应 `DecisionSerializer` 的列表顺序。商店卡牌是 `CharacterCardEntries` 拼 `ColorlessCardEntries`；事件选项包含已锁定的选项。
- 构建：`~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release`（默认 `Sts2DataDir` 指向 Steam 安装目录）。
- 测试：`~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests`，基线 228 个全过。

## File Structure

**Core（不依赖游戏，有单元测试）**

| 文件 | 职责 |
|---|---|
| `src/Sts2Advisor.Core/AdvisorConfig.cs`（改） | 新增 `autopilot_hotkey`，默认 `F9` |
| `src/Sts2Advisor.Core/AutoPilotPlan.cs`（新） | `PlannedAction` / `PlanKind` / `AutoPilotException`；把 `AdviceReply` 变成可执行动作并校验 |
| `src/Sts2Advisor.Core/AutoPilotSession.cs`（新） | 模式、`StopReason`、步骤记录、离线计时、重问计数、交还玩家 |
| `src/Sts2Advisor.Core/CombatSolverSettings.cs`（新） | 读 `combat_solver_settings.json` 的 `autoEnableFullAuto` |
| `src/Sts2Advisor.Core/AutoPilotText.cs`（新） | 全托管面板文字、停止横幅、步骤文字 |
| `src/Sts2Advisor.Core/PanelText.cs`（改） | `PickText` 改 public，新增 `DecisionName` |
| `src/Sts2Advisor.Core/AdvisorTraceLog.cs`（改） | `Append` 新增可选的 `autopilot` 字段 |
| `src/Sts2Advisor.Core.Tests/AutoPilot*Tests.cs`、`CombatSolverSettingsTests.cs`（新） | 对应测试 |

**Mod（依赖游戏，靠编译 + 实机验收）**，新目录 `src/Sts2Advisor/AutoPilot/`：

| 文件 | 职责 |
|---|---|
| `AutoPilotChoice.cs` | `CachedAdvice`、`NeedAdviceException`、`IAutoPilotHost`、`UiModel`、`AutoPilotChoice.Pick<T>` |
| `CombatWait.cs` | 等战斗结束；替代事件战斗作弊 |
| `HandlerPatches.cs` | transpiler + 自检（每个 handler 应改写几处） |
| `RewardsHandler.cs`、`CardRewardHandler.cs`、`MapHandler.cs`、`ShopHandler.cs` | 我们自己的 handler |
| `AutoPilotLoop.cs` | 状态驱动循环、问建议、overlay 处理、停止原因 |
| `AutoPilotControl.cs` | 开/停控制器、面板文字、停止横幅 |

**改动的已有文件**

| 文件 | 改动 |
|---|---|
| `src/Sts2Advisor/DecisionCapture.cs` | `Capture` 加 `Choices` / `Inventory` |
| `src/Sts2Advisor/AdvisorRuntime.cs` | F9、全托管期间暂停建议、横幅 |
| `src/Sts2Advisor/ModEntry.cs` | 打补丁 |
| `src/Sts2Advisor/Sts2Advisor.csproj` | 引用 `0Harmony` |
| `tools/install_advisor_mod.sh` | 新配置键 |
| `CLAUDE.md`、spec | 文档 |

---

### Task 0: 准备反编译参考（只读，不提交）

后面的 handler 代码照抄自 AutoSlay；实现时需要对照原文。

- [ ] **Step 1: 反编译 sts2.dll 到 /tmp**

```bash
mkdir -p /tmp/sts2-cli/autoslay
DOTNET_ROOT=~/.dotnet-arm64 DOTNET_ROLL_FORWARD=Major ~/.dotnet/tools/ilspycmd -p -o /tmp/sts2-cli/autoslay/ns \
  -r "$HOME/Library/Application Support/Steam/steamapps/common/Slay the Spire 2/SlayTheSpire2.app/Contents/Resources/data_sts2_macos_arm64" \
  lib/sts2.dll.original --nested-directories >/dev/null 2>&1
ls /tmp/sts2-cli/autoslay/ns/MegaCrit/sts2/Core/AutoSlay/Handlers/Rooms
```

Expected: 列出 `CombatRoomHandler.cs EventRoomHandler.cs RestSiteRoomHandler.cs ShopRoomHandler.cs TreasureRoomHandler.cs VictoryRoomHandler.cs`。（/tmp 会被清，没了就重跑。）

---

### Task 1: 配置 `autopilot_hotkey`

**Files:**
- Modify: `src/Sts2Advisor.Core/AdvisorConfig.cs`
- Modify: `tools/install_advisor_mod.sh`
- Test: `src/Sts2Advisor.Core.Tests/AdvisorConfigTests.cs`

- [ ] **Step 1: 写失败的测试**（追加到 `AdvisorConfigTests` 类末尾）

```csharp
    [Fact]
    public void AutopilotHotkeyDefaultsToF9()
    {
        Assert.Equal("F9", AdvisorConfig.Defaults.AutopilotHotkey);
        var c = AdvisorConfig.Load(Write("""{"repo_path": "/r"}"""));
        Assert.Equal("F9", c.AutopilotHotkey);
    }

    [Fact]
    public void ReadsAutopilotHotkey()
    {
        var c = AdvisorConfig.Load(Write("""{"autopilot_hotkey": "F10"}"""));
        Assert.Equal("F10", c.AutopilotHotkey);
        Assert.Equal("F8", c.Hotkey);
    }

    [Fact]
    public void NonStringAutopilotHotkeyTakesTheDefault()
    {
        var c = AdvisorConfig.Load(Write("""{"autopilot_hotkey": 9}"""));
        Assert.Equal("F9", c.AutopilotHotkey);
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AdvisorConfigTests`
Expected: 编译失败，`'AdvisorConfig' does not contain a definition for 'AutopilotHotkey'`

- [ ] **Step 3: 实现**

在 `src/Sts2Advisor.Core/AdvisorConfig.cs` 里：

把 record 头改为（新参数放最后、带默认值，已有的 5 参构造调用不受影响）：

```csharp
public sealed record AdvisorConfig(string RepoPath, string Python, int Port, bool Autostart, string Hotkey,
                                   string AutopilotHotkey = AdvisorConfig.DefaultAutopilotHotkey)
{
    public const int DefaultPort = 8767;
    public const string DefaultHotkey = "F8";
    public const string DefaultAutopilotHotkey = "F9";
```

`Load` 里的 `return new AdvisorConfig(...)` 改为：

```csharp
            return new AdvisorConfig(
                Str(root, "repo_path") ?? Defaults.RepoPath,
                Str(root, "python") ?? Defaults.Python,
                ReadPort(root),
                ReadAutostart(root),
                Str(root, "hotkey") ?? DefaultHotkey,
                Str(root, "autopilot_hotkey") ?? DefaultAutopilotHotkey);
```

`Load` 的 summary 末尾补一句：`"autopilot_hotkey" is a Godot Key name like "hotkey" (else "F9").`

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AdvisorConfigTests`
Expected: PASS（含原有用例）

- [ ] **Step 5: 安装脚本写出新键**

`tools/install_advisor_mod.sh` 的 heredoc 改为：

```bash
  cat > "$USER_DIR/config.json" <<EOCFG
{
  "repo_path": "$ROOT",
  "python": "$ROOT/.venv/bin/python",
  "port": 8767,
  "autostart": true,
  "hotkey": "F8",
  "autopilot_hotkey": "F9"
}
EOCFG
```

（已有的 config.json 不重写；缺这个键时取默认 F9。）

- [ ] **Step 6: Commit**

```bash
git add src/Sts2Advisor.Core/AdvisorConfig.cs src/Sts2Advisor.Core.Tests/AdvisorConfigTests.cs tools/install_advisor_mod.sh
git commit -m "feat(advisor): autopilot_hotkey config (default F9)"
```

---

### Task 2: `AutoPilotPlan` —— 建议 → 可执行动作

**Files:**
- Create: `src/Sts2Advisor.Core/AutoPilotPlan.cs`
- Test: `src/Sts2Advisor.Core.Tests/AutoPilotPlanTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AutoPilotPlanTests.cs
using Sts2Advisor.Core;

public class AutoPilotPlanTests
{
    private static AdviceReply Reply(string actionJson) =>
        AdviceReply.Parse($$"""{"action": {{actionJson}}, "options": [], "rules": [], "warnings": [], "unverified": false, "policy": "greedy"}""");

    private static PlannedAction Plan(string decision, string actionJson) => AutoPilotPlan.From(decision, Reply(actionJson));

    [Fact]
    public void MapNodeCarriesTheCoordinate()
    {
        var p = Plan("map_select", """{"action": "select_map_node", "args": {"col": 3, "row": 1}}""");
        Assert.Equal(PlanKind.MapNode, p.Kind);
        Assert.Equal((3, 1), (p.Col, p.Row));
        Assert.Empty(p.Indices);
    }

    [Theory]
    [InlineData("card_reward", "select_card_reward", "card_index", 2)]
    [InlineData("event_choice", "choose_option", "option_index", 0)]
    [InlineData("rest_site", "choose_option", "option_index", 1)]
    [InlineData("bundle_select", "select_bundle", "bundle_index", 2)]
    public void SinglePicks(string decision, string action, string arg, int index)
    {
        var p = Plan(decision, $$"""{"action": "{{action}}", "args": {"{{arg}}": {{index}}}}""");
        Assert.Equal(PlanKind.Pick, p.Kind);
        Assert.Equal(new[] { index }, p.Indices);
    }

    [Fact]
    public void CardRewardSkip()
    {
        Assert.Equal(PlanKind.Skip, Plan("card_reward", """{"action": "skip_card_reward"}""").Kind);
    }

    [Fact]
    public void CardSelectParsesCommaIndicesInOrderWithoutDuplicates()
    {
        var p = Plan("card_select", """{"action": "select_cards", "args": {"indices": "4, 1,4"}}""");
        Assert.Equal(PlanKind.Pick, p.Kind);
        Assert.Equal(new[] { 4, 1 }, p.Indices);
    }

    [Fact]
    public void CardSelectAcceptsANumber()
    {
        Assert.Equal(new[] { 17 }, Plan("card_select", """{"action": "select_cards", "args": {"indices": 17}}""").Indices);
    }

    [Theory]
    [InlineData("""{"action": "buy_card", "args": {"card_index": 6}}""", PlanKind.BuyCard, 6)]
    [InlineData("""{"action": "buy_relic", "args": {"relic_index": 1}}""", PlanKind.BuyRelic, 1)]
    [InlineData("""{"action": "buy_potion", "args": {"potion_index": 0}}""", PlanKind.BuyPotion, 0)]
    public void ShopPurchases(string action, PlanKind kind, int index)
    {
        var p = Plan("shop", action);
        Assert.Equal(kind, p.Kind);
        Assert.Equal(new[] { index }, p.Indices);
    }

    [Fact]
    public void ShopRemoveAndLeave()
    {
        Assert.Equal(PlanKind.RemoveCard, Plan("shop", """{"action": "remove_card"}""").Kind);
        Assert.Equal(PlanKind.Leave, Plan("shop", """{"action": "leave_room"}""").Kind);
    }

    [Fact]
    public void NoAdviceThrows()
    {
        var reply = AdviceReply.Parse("""{"action": null, "options": [], "rules": [], "warnings": ["greedy_none"], "unverified": false, "policy": "greedy"}""");
        var e = Assert.Throws<AutoPilotException>(() => AutoPilotPlan.From("shop", reply));
        Assert.Contains("greedy_none", e.Message);
    }

    [Theory]
    [InlineData("shop", """{"action": "select_card_reward", "args": {"card_index": 0}}""")]   // wrong decision
    [InlineData("card_select", """{"action": "skip_select"}""")]                              // never emitted with cards; not executable
    [InlineData("rest_site", """{"action": "proceed"}""")]                                    // greedy's fallback
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {}}""")]          // missing index
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {"card_index": -1}}""")]
    [InlineData("card_reward", """{"action": "select_card_reward", "args": {"card_index": "1"}}""")]
    [InlineData("card_select", """{"action": "select_cards", "args": {"indices": ""}}""")]
    [InlineData("card_select", """{"action": "select_cards", "args": {"indices": "1,x"}}""")]
    [InlineData("map_select", """{"action": "select_map_node", "args": {"col": 1}}""")]
    public void UnexecutableAdviceThrows(string decision, string action)
    {
        Assert.Throws<AutoPilotException>(() => Plan(decision, action));
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AutoPilotPlanTests`
Expected: 编译失败，`The type or namespace name 'PlannedAction' could not be found`

- [ ] **Step 3: 实现**

```csharp
// src/Sts2Advisor.Core/AutoPilotPlan.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

/// <summary>Pick: choose the options at Indices (one, or several for card_select). Skip: the card reward's skip.
/// MapNode: travel to (Col, Row). Buy*: the shop entry at Indices[0]. RemoveCard / Leave: the shop's removal / exit.</summary>
public enum PlanKind { Pick, Skip, MapNode, BuyCard, BuyRelic, BuyPotion, RemoveCard, Leave }

public sealed record PlannedAction(PlanKind Kind, IReadOnlyList<int> Indices, int Col = -1, int Row = -1);

/// <summary>Advice the autopilot cannot carry out, or a screen that does not match it. Stops the autopilot.</summary>
public sealed class AutoPilotException(string message) : Exception(message);

/// <summary>Turns greedy's engine action (agent/combat_env.py greedy_action) into what the autopilot clicks.
/// Indices point into the decision JSON's lists (DecisionSerializer order).</summary>
public static class AutoPilotPlan
{
    public static PlannedAction From(string decision, AdviceReply reply)
    {
        string action = reply.ActionName
            ?? throw new AutoPilotException($"{decision}: no advice ({string.Join("; ", reply.Warnings)})");
        return (decision, action) switch
        {
            ("map_select", "select_map_node") =>
                new(PlanKind.MapNode, Array.Empty<int>(), Int(reply, decision, "col"), Int(reply, decision, "row")),
            ("card_reward", "select_card_reward") => One(PlanKind.Pick, reply, decision, "card_index"),
            ("card_reward", "skip_card_reward") => new(PlanKind.Skip, Array.Empty<int>()),
            ("event_choice" or "rest_site", "choose_option") => One(PlanKind.Pick, reply, decision, "option_index"),
            ("card_select", "select_cards") => new(PlanKind.Pick, Indices(reply, decision)),
            ("bundle_select", "select_bundle") => One(PlanKind.Pick, reply, decision, "bundle_index"),
            ("shop", "buy_card") => One(PlanKind.BuyCard, reply, decision, "card_index"),
            ("shop", "buy_relic") => One(PlanKind.BuyRelic, reply, decision, "relic_index"),
            ("shop", "buy_potion") => One(PlanKind.BuyPotion, reply, decision, "potion_index"),
            ("shop", "remove_card") => new(PlanKind.RemoveCard, Array.Empty<int>()),
            ("shop", "leave_room") => new(PlanKind.Leave, Array.Empty<int>()),
            _ => throw new AutoPilotException($"{decision}: autopilot cannot execute '{action}'"),
        };
    }

    private static PlannedAction One(PlanKind kind, AdviceReply reply, string decision, string arg) =>
        new(kind, new[] { Int(reply, decision, arg) });

    private static int Int(AdviceReply reply, string decision, string arg) =>
        reply.Arg(arg) is { ValueKind: JsonValueKind.Number } v && v.TryGetInt32(out int i) && i >= 0
            ? i
            : throw new AutoPilotException($"{decision}: '{reply.ActionName}' needs a non-negative integer '{arg}'");

    /// <summary>"4, 1,4" -> [4, 1]: order kept, duplicates dropped. A bare number is one index.</summary>
    private static IReadOnlyList<int> Indices(AdviceReply reply, string decision)
    {
        var arg = reply.Arg("indices");
        if (arg is { ValueKind: JsonValueKind.Number } n && n.TryGetInt32(out int single) && single >= 0)
            return new[] { single };
        if (arg is not { ValueKind: JsonValueKind.String } s)
            throw new AutoPilotException($"{decision}: select_cards needs 'indices'");
        var result = new List<int>();
        foreach (var part in s.GetString()!.Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries))
        {
            if (!int.TryParse(part, out int i) || i < 0)
                throw new AutoPilotException($"{decision}: bad index '{part}' in '{s.GetString()}'");
            if (!result.Contains(i))
                result.Add(i);
        }
        return result.Count > 0 ? result : throw new AutoPilotException($"{decision}: select_cards with no indices");
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AutoPilotPlanTests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor.Core/AutoPilotPlan.cs src/Sts2Advisor.Core.Tests/AutoPilotPlanTests.cs
git commit -m "feat(advisor): AutoPilotPlan turns greedy advice into an executable action"
```

---

### Task 3: `AutoPilotSession` —— 会话状态机

**Files:**
- Create: `src/Sts2Advisor.Core/AutoPilotSession.cs`
- Test: `src/Sts2Advisor.Core.Tests/AutoPilotSessionTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AutoPilotSessionTests.cs
using Sts2Advisor.Core;

public class AutoPilotSessionTests
{
    [Fact]
    public void StartsInAdviceModeAndStartsOnce()
    {
        var s = new AutoPilotSession();
        Assert.Equal(AutoPilotMode.Advice, s.Mode);
        Assert.True(s.Start());
        Assert.Equal(AutoPilotMode.Running, s.Mode);
        Assert.False(s.Start());
    }

    [Fact]
    public void StopRecordsTheReasonAndClearsTransientState()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.HandBack("NChooseARelicSelection");
        s.InCombat = true;
        s.Stop(StopReason.Stuck, "NRewardsScreen did not close");
        Assert.Equal(AutoPilotMode.Advice, s.Mode);
        Assert.Equal(StopReason.Stuck, s.LastStop);
        Assert.Equal("NRewardsScreen did not close", s.LastStopDetail);
        Assert.Null(s.HandBackScreen);
        Assert.False(s.InCombat);
    }

    [Fact]
    public void StartClearsTheLastRun()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("a", null);
        s.Stop(StopReason.Hotkey, null);
        s.Start();
        Assert.Empty(s.Steps);
        Assert.Equal(0, s.StepCount);
        Assert.Null(s.LastStop);
    }

    [Fact]
    public void KeepsTheNewestStepsFirst()
    {
        var s = new AutoPilotSession();
        s.Start();
        for (int i = 1; i <= AutoPilotSession.MaxSteps + 2; i++)
            Assert.Equal(i, s.Record($"step {i}", i == 1 ? "why" : null));
        Assert.Equal(AutoPilotSession.MaxSteps, s.Steps.Count);
        Assert.Equal("step 8", s.Steps[0].Text);
        Assert.Equal(8, s.Steps[0].Number);
        Assert.Equal("step 3", s.Steps[^1].Text);
        Assert.Equal(8, s.StepCount);
    }

    [Fact]
    public void OfflineOnlyCountsWhenTenSecondsPass()
    {
        var s = new AutoPilotSession();
        Assert.False(s.Offline(100));
        Assert.False(s.Offline(109.9));
        Assert.True(s.Offline(110.1));
        s.Online();
        Assert.False(s.Offline(200));
    }

    [Fact]
    public void AdviceRetriesAreCappedPerFingerprintAndResetByAStep()
    {
        var s = new AutoPilotSession();
        for (int i = 0; i < AutoPilotSession.MaxAdviceRetries; i++)
            Assert.True(s.CountAdviceRetry("f1"));
        Assert.False(s.CountAdviceRetry("f1"));
        Assert.True(s.CountAdviceRetry("f2"));
        for (int i = 1; i < AutoPilotSession.MaxAdviceRetries; i++)
            s.CountAdviceRetry("f2");
        s.Record("picked", null);
        Assert.True(s.CountAdviceRetry("f2"));
    }

    [Fact]
    public void HandBackIsSetAndCleared()
    {
        var s = new AutoPilotSession();
        s.HandBack("NCrystalSphereScreen");
        Assert.Equal("NCrystalSphereScreen", s.HandBackScreen);
        s.ClearHandBack();
        Assert.Null(s.HandBackScreen);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AutoPilotSessionTests`
Expected: 编译失败，`'AutoPilotSession' could not be found`

- [ ] **Step 3: 实现**

```csharp
// src/Sts2Advisor.Core/AutoPilotSession.cs
namespace Sts2Advisor.Core;

public enum AutoPilotMode { Advice, Running }

/// <summary>Why the autopilot handed the run back. Hotkey: the player pressed it. GameOver: the run's
/// result screen. LeftRun: no run any more (main menu, loading). Multiplayer: more than one player.
/// HandlerFailed: a handler threw or the advice did not fit the screen. Stuck: no progress (a screen that
/// will not close, nothing to do for 30 s, no settled decision, the same advice asked for too often).
/// Offline: the advice service unreachable for more than 10 s. BadReply: it answered unusably.
/// Unsupported: game API mismatch. Disabled: autopilot could not start (patches or config missing).</summary>
public enum StopReason { Hotkey, GameOver, LeftRun, Multiplayer, HandlerFailed, Stuck, Offline, BadReply, Unsupported, Disabled }

public sealed record AutoPilotStep(int Number, string Text, string? Reason);

/// <summary>The autopilot's state, kept apart from the game so it can be tested. Main thread only.</summary>
public sealed class AutoPilotSession
{
    public const int MaxSteps = 6;
    public const double OfflineLimitSeconds = 10;
    public const int MaxAdviceRetries = 5;

    private readonly List<AutoPilotStep> _steps = new();  // newest first
    private double? _offlineSince;
    private string? _retryFingerprint;
    private int _retries;

    public AutoPilotMode Mode { get; private set; }
    public StopReason? LastStop { get; private set; }
    public string? LastStopDetail { get; private set; }
    /// <summary>The screen the player has been asked to finish by hand; null when none.</summary>
    public string? HandBackScreen { get; private set; }
    public bool InCombat { get; set; }
    public IReadOnlyList<AutoPilotStep> Steps => _steps;
    public int StepCount { get; private set; }

    /// <summary>False when already running.</summary>
    public bool Start()
    {
        if (Mode == AutoPilotMode.Running)
            return false;
        Mode = AutoPilotMode.Running;
        _steps.Clear();
        StepCount = 0;
        LastStop = null;
        LastStopDetail = null;
        HandBackScreen = null;
        InCombat = false;
        _offlineSince = null;
        _retryFingerprint = null;
        _retries = 0;
        return true;
    }

    public void Stop(StopReason reason, string? detail)
    {
        Mode = AutoPilotMode.Advice;
        LastStop = reason;
        LastStopDetail = detail;
        HandBackScreen = null;
        InCombat = false;
    }

    /// <summary>Records an executed step; returns its number (1-based, over the whole session).</summary>
    public int Record(string text, string? reason)
    {
        StepCount++;
        _steps.Insert(0, new AutoPilotStep(StepCount, text, reason));
        if (_steps.Count > MaxSteps)
            _steps.RemoveAt(_steps.Count - 1);
        _retryFingerprint = null;
        _retries = 0;
        return StepCount;
    }

    public void HandBack(string screen) => HandBackScreen = screen;

    public void ClearHandBack() => HandBackScreen = null;

    /// <summary>One failed request at <paramref name="now"/> (seconds); true once the outage is longer than 10 s.</summary>
    public bool Offline(double now)
    {
        _offlineSince ??= now;
        return now - _offlineSince.Value > OfflineLimitSeconds;
    }

    public void Online() => _offlineSince = null;

    /// <summary>One more "ask for advice, then rerun the handler" on this decision; false past the cap
    /// (the screen keeps changing under the handler, or the advice never fits).</summary>
    public bool CountAdviceRetry(string fingerprint)
    {
        if (fingerprint != _retryFingerprint)
        {
            _retryFingerprint = fingerprint;
            _retries = 0;
        }
        return ++_retries <= MaxAdviceRetries;
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AutoPilotSessionTests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor.Core/AutoPilotSession.cs src/Sts2Advisor.Core.Tests/AutoPilotSessionTests.cs
git commit -m "feat(advisor): AutoPilotSession state, stop reasons, offline and retry limits"
```

---

### Task 4: `CombatSolverSettings` —— 读 CombatSolver 的全自动开关

**Files:**
- Create: `src/Sts2Advisor.Core/CombatSolverSettings.cs`
- Test: `src/Sts2Advisor.Core.Tests/CombatSolverSettingsTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/CombatSolverSettingsTests.cs
using Sts2Advisor.Core;

public class CombatSolverSettingsTests
{
    private static string Write(string text)
    {
        var path = Path.Combine(Path.GetTempPath(), $"cs-{Guid.NewGuid()}.json");
        File.WriteAllText(path, text);
        return path;
    }

    [Fact]
    public void MissingFile() =>
        Assert.Equal(CombatSolverStatus.Missing,
                     CombatSolverSettings.Read(Path.Combine(Path.GetTempPath(), $"absent-{Guid.NewGuid()}.json")));

    [Fact]
    public void FullAutoOn() =>
        Assert.Equal(CombatSolverStatus.Ok, CombatSolverSettings.Read(Write("""{"solverDisabled": false, "autoEnableFullAuto": true}""")));

    [Theory]
    [InlineData("""{"autoEnableFullAuto": false}""")]
    [InlineData("""{"solverDisabled": false}""")]
    [InlineData("""{"autoEnableFullAuto": "true"}""")]
    [InlineData("""{"autoEnableFullAuto": true, "solverDisabled": true}""")]
    public void FullAutoOff(string json) =>
        Assert.Equal(CombatSolverStatus.FullAutoOff, CombatSolverSettings.Read(Write(json)));

    [Theory]
    [InlineData("not json")]
    [InlineData("[1, 2]")]
    public void Unreadable(string text) =>
        Assert.Equal(CombatSolverStatus.Unreadable, CombatSolverSettings.Read(Write(text)));
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter CombatSolverSettingsTests`
Expected: 编译失败，`'CombatSolverSettings' could not be found`

- [ ] **Step 3: 实现**

```csharp
// src/Sts2Advisor.Core/CombatSolverSettings.cs
using System.Text.Json;

namespace Sts2Advisor.Core;

public enum CombatSolverStatus { Ok, Missing, FullAutoOff, Unreadable }

/// <summary>Whether the CombatSolver Workshop mod will play combats by itself: its settings file
/// (&lt;game user dir&gt;/combat_solver_settings.json) has "autoEnableFullAuto": true and not
/// "solverDisabled": true. The autopilot only warns when it is not; it never writes the file.</summary>
public static class CombatSolverSettings
{
    public const string FileName = "combat_solver_settings.json";

    public static CombatSolverStatus Read(string path)
    {
        try
        {
            if (!File.Exists(path))
                return CombatSolverStatus.Missing;
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return CombatSolverStatus.Unreadable;
            bool disabled = root.TryGetProperty("solverDisabled", out var d) && d.ValueKind == JsonValueKind.True;
            bool fullAuto = root.TryGetProperty("autoEnableFullAuto", out var a) && a.ValueKind == JsonValueKind.True;
            return fullAuto && !disabled ? CombatSolverStatus.Ok : CombatSolverStatus.FullAutoOff;
        }
        catch (Exception)
        {
            return CombatSolverStatus.Unreadable;
        }
    }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter CombatSolverSettingsTests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor.Core/CombatSolverSettings.cs src/Sts2Advisor.Core.Tests/CombatSolverSettingsTests.cs
git commit -m "feat(advisor): read whether CombatSolver full-auto is on"
```

---

### Task 5: `AutoPilotText` —— 面板文字

**Files:**
- Modify: `src/Sts2Advisor.Core/PanelText.cs`（`PickText` 改 public，加 `DecisionName`）
- Create: `src/Sts2Advisor.Core/AutoPilotText.cs`
- Test: `src/Sts2Advisor.Core.Tests/AutoPilotTextTests.cs`

- [ ] **Step 1: 写失败的测试**

```csharp
// src/Sts2Advisor.Core.Tests/AutoPilotTextTests.cs
using Sts2Advisor.Core;

public class AutoPilotTextTests
{
    private static readonly Dictionary<string, string> Labels = new() { ["card:1"] = "痛击" };

    [Fact]
    public void StepTextNamesTheDecisionAndThePick()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        Assert.Equal("卡牌奖励：痛击", AutoPilotText.StepText("card_reward", reply, Labels, zh: true));
        Assert.Equal("Card reward: 痛击", AutoPilotText.StepText("card_reward", reply, Labels, zh: false));
    }

    [Fact]
    public void ReasonIsTheFirstFiredRule()
    {
        var reply = AdviceReply.Parse(AdviceReplyTests.CardRewardJson);
        Assert.Equal(TraceText.Rule(reply.Rules[0], true), AutoPilotText.Reason(reply, zh: true));
        var none = AdviceReply.Parse("""{"action": {"action": "leave_room"}, "rules": [{"key": "shop.leave", "outcome": "skipped", "values": {}}]}""");
        Assert.Null(AutoPilotText.Reason(none, zh: true));
    }

    [Fact]
    public void RunningShowsTitleStepsNewestFirstAndWarnings()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.Record("地图：问号", null);
        s.Record("事件：离开", "因为");
        s.HandBack("NChooseARelicSelection");
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.FullAutoOff, zh: true);
        Assert.Equal(new PanelLine("全托管中 · F9 接管", LineStyle.Title), lines[0]);
        Assert.Equal(LineStyle.Warning, lines[1].Style);
        Assert.Contains("CombatSolver", lines[1].Text);
        Assert.Equal(LineStyle.Warning, lines[2].Style);
        Assert.Contains("手动", lines[2].Text);
        Assert.Equal(new PanelLine("事件：离开", LineStyle.Pick), lines[3]);
        Assert.Equal(new PanelLine("  因为", LineStyle.Muted), lines[4]);
        Assert.Equal(new PanelLine("地图：问号", LineStyle.Normal), lines[5]);
        Assert.Equal(6, lines.Count);
    }

    [Fact]
    public void RunningWithNothingYetSaysItIsWaiting()
    {
        var s = new AutoPilotSession();
        s.Start();
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.Ok, zh: false);
        Assert.Equal("Autopilot on · F9 to take over", lines[0].Text);
        Assert.Equal(LineStyle.Muted, lines[1].Style);
        Assert.Equal(2, lines.Count);
    }

    [Fact]
    public void RunningInCombatSaysSo()
    {
        var s = new AutoPilotSession();
        s.Start();
        s.InCombat = true;
        var lines = AutoPilotText.Running(s, "F9", CombatSolverStatus.Ok, zh: true);
        Assert.Contains(lines, l => l.Text.Contains("CombatSolver") && l.Style == LineStyle.Muted);
    }

    [Fact]
    public void EveryStopReasonHasItsOwnTextInBothLanguages()
    {
        foreach (var r in Enum.GetValues<StopReason>())
        {
            string zh = AutoPilotText.StopReasonText(r, true), en = AutoPilotText.StopReasonText(r, false);
            Assert.False(string.IsNullOrWhiteSpace(zh));
            Assert.False(string.IsNullOrWhiteSpace(en));
            Assert.NotEqual(r.ToString(), en);
            Assert.NotEqual(zh, en);
        }
        Assert.Equal(Enum.GetValues<StopReason>().Length,
                     Enum.GetValues<StopReason>().Select(r => AutoPilotText.StopReasonText(r, true)).Distinct().Count());
    }

    [Fact]
    public void StopBannerCarriesReasonDetailAndHotkey()
    {
        var b = AutoPilotText.StopBanner(StopReason.Stuck, "NRewardsScreen", "F9", zh: true);
        Assert.Equal(LineStyle.Warning, b.Style);
        Assert.Contains(AutoPilotText.StopReasonText(StopReason.Stuck, true), b.Text);
        Assert.Contains("NRewardsScreen", b.Text);
        Assert.Contains("F9", b.Text);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.Hotkey, null, "F9", zh: true).Style);
        Assert.Equal(LineStyle.Muted, AutoPilotText.StopBanner(StopReason.GameOver, null, "F9", zh: true).Style);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AutoPilotTextTests`
Expected: 编译失败，`'AutoPilotText' could not be found`

- [ ] **Step 3: 改 PanelText**

`src/Sts2Advisor.Core/PanelText.cs`：

把 `private static string PickText(` 改成 `public static string PickText(`。

在 `Header` 方法前加：

```csharp
    /// <summary>"卡牌奖励" / "Card reward"; the raw decision name when it has none.</summary>
    public static string DecisionName(string decision, bool zh) =>
        DecisionNames.TryGetValue(decision, out var n) ? (zh ? n.Zh : n.En) : decision;
```

- [ ] **Step 4: 写 AutoPilotText**

```csharp
// src/Sts2Advisor.Core/AutoPilotText.cs
namespace Sts2Advisor.Core;

/// <summary>The panel while the autopilot runs, and the one-line banner after it stops.</summary>
public static class AutoPilotText
{
    private static string T(bool zh, string zhText, string enText) => zh ? zhText : enText;

    /// <summary>"卡牌奖励：痛击".</summary>
    public static string StepText(string decision, AdviceReply reply, IReadOnlyDictionary<string, string> labels, bool zh) =>
        PanelText.DecisionName(decision, zh) + T(zh, "：", ": ") + PanelText.PickText(decision, reply, labels, zh);

    /// <summary>The first fired, shown rule of the same reply; null when none fired.</summary>
    public static string? Reason(AdviceReply reply, bool zh) =>
        reply.Rules.FirstOrDefault(r => r.Outcome == "fired" && !TraceText.IsHidden(r)) is { } rule
            ? TraceText.Rule(rule, zh) : null;

    public static IReadOnlyList<PanelLine> Running(AutoPilotSession s, string hotkey, CombatSolverStatus combat, bool zh)
    {
        var lines = new List<PanelLine>
        {
            new(T(zh, $"全托管中 · {hotkey} 接管", $"Autopilot on · {hotkey} to take over"), LineStyle.Title),
        };
        if (combat != CombatSolverStatus.Ok)
            lines.Add(new PanelLine(CombatWarning(combat, zh), LineStyle.Warning));
        if (s.HandBackScreen != null)
            lines.Add(new PanelLine(T(zh, "请手动完成此界面，完成后自动继续",
                                          "Please finish this screen yourself; autopilot resumes after it"), LineStyle.Warning));
        if (s.InCombat)
            lines.Add(new PanelLine(T(zh, "战斗中（CombatSolver）", "In combat (CombatSolver)"), LineStyle.Muted));
        for (int i = 0; i < s.Steps.Count; i++)
        {
            var step = s.Steps[i];
            lines.Add(new PanelLine(step.Text, i == 0 ? LineStyle.Pick : LineStyle.Normal));
            if (step.Reason != null)
                lines.Add(new PanelLine("  " + step.Reason, LineStyle.Muted));
        }
        if (s.Steps.Count == 0 && s.HandBackScreen == null && !s.InCombat)
            lines.Add(new PanelLine(T(zh, "等待下一个决策…", "Waiting for the next decision…"), LineStyle.Muted));
        return lines;
    }

    public static PanelLine StopBanner(StopReason reason, string? detail, string hotkey, bool zh)
    {
        string text = T(zh, "全托管已停止：", "Autopilot stopped: ") + StopReasonText(reason, zh)
            + (string.IsNullOrEmpty(detail) ? "" : T(zh, "（", " (") + detail + T(zh, "）", ")"))
            + T(zh, $" · {hotkey} 重新开启", $" · {hotkey} to restart");
        return new PanelLine(text, reason is StopReason.Hotkey or StopReason.GameOver ? LineStyle.Muted : LineStyle.Warning);
    }

    public static string StopReasonText(StopReason reason, bool zh) => reason switch
    {
        StopReason.Hotkey => T(zh, "玩家接管", "taken over by the player"),
        StopReason.GameOver => T(zh, "本局结束", "the run is over"),
        StopReason.LeftRun => T(zh, "已离开本局", "left the run"),
        StopReason.Multiplayer => T(zh, "联机不支持", "multiplayer is not supported"),
        StopReason.HandlerFailed => T(zh, "执行失败", "an action failed"),
        StopReason.Stuck => T(zh, "界面卡住", "the screen is stuck"),
        StopReason.Offline => T(zh, "建议服务离线超过 10 秒", "advice service offline for over 10 s"),
        StopReason.BadReply => T(zh, "建议服务回复格式不对", "unexpected reply from the advice service"),
        StopReason.Unsupported => T(zh, "游戏版本未适配", "unsupported game version"),
        StopReason.Disabled => T(zh, "全托管不可用", "autopilot unavailable"),
        _ => reason.ToString(),
    };

    private static string CombatWarning(CombatSolverStatus status, bool zh) => status switch
    {
        CombatSolverStatus.Missing => T(zh, "没找到 CombatSolver 设置，战斗不会自动进行",
                                            "CombatSolver settings not found: combats will not play themselves"),
        CombatSolverStatus.FullAutoOff => T(zh, "CombatSolver 未开启“每场战斗开始时自动开启全自动”，战斗不会自动进行",
                                                "CombatSolver auto full-auto is off: combats will not play themselves"),
        _ => T(zh, "CombatSolver 设置读不了，战斗可能不会自动进行",
                   "CombatSolver settings unreadable: combats may not play themselves"),
    };
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests`
Expected: 全部 PASS（原有的 PanelText 测试也要过）

- [ ] **Step 6: Commit**

```bash
git add src/Sts2Advisor.Core/PanelText.cs src/Sts2Advisor.Core/AutoPilotText.cs src/Sts2Advisor.Core.Tests/AutoPilotTextTests.cs
git commit -m "feat(advisor): autopilot panel text, step text and stop banner"
```

---

### Task 6: trace 记录执行的步骤

**Files:**
- Modify: `src/Sts2Advisor.Core/AdvisorTraceLog.cs`
- Test: `src/Sts2Advisor.Core.Tests/AdvisorTraceLogTests.cs`

- [ ] **Step 1: 写失败的测试**（追加到 `AdvisorTraceLogTests`）

```csharp
    [Fact]
    public void AutopilotStepsCarryTheirMarker()
    {
        var path = NewPath();
        var log = new AdvisorTraceLog(path);
        log.Append("shop", """{"decision": "shop"}""", """{"action": null}""", null, """{"executed": true, "step": 3}""");
        log.Append("shop", """{"decision": "shop"}""", """{"action": null}""", null);
        var lines = File.ReadAllLines(path);
        using var first = JsonDocument.Parse(lines[0]);
        var ap = first.RootElement.GetProperty("autopilot");
        Assert.True(ap.GetProperty("executed").GetBoolean());
        Assert.Equal(3, ap.GetProperty("step").GetInt32());
        using var second = JsonDocument.Parse(lines[1]);
        Assert.False(second.RootElement.TryGetProperty("autopilot", out _));
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests --filter AdvisorTraceLogTests`
Expected: 编译失败，`No overload for method 'Append' takes 5 arguments`

- [ ] **Step 3: 实现**

`src/Sts2Advisor.Core/AdvisorTraceLog.cs`：

`Append` 签名和第一行改为：

```csharp
    /// <summary><paramref name="autopilotJson"/>: set when the autopilot executed this advice
    /// ({"executed": true, "step": n}); written as an "autopilot" object.</summary>
    public void Append(string decision, string stateJson, string? replyJson, string? error, string? autopilotJson = null)
    {
        try
        {
            var line = BuildLine(decision, stateJson, replyJson, error, autopilotJson);
```

`BuildLine` 改为：

```csharp
    private static string BuildLine(string decision, string stateJson, string? replyJson, string? error, string? autopilotJson)
    {
        using var buffer = new MemoryStream();
        using (var w = new Utf8JsonWriter(buffer, WriterOptions))
        {
            w.WriteStartObject();
            w.WriteString("ts", DateTime.UtcNow.ToString("O"));
            w.WriteString("decision", decision);
            w.WritePropertyName("state");
            WriteJson(w, stateJson);
            w.WritePropertyName("reply");
            if (replyJson == null) w.WriteNullValue(); else WriteJson(w, replyJson);
            if (error != null) w.WriteString("error", error);
            if (autopilotJson != null)
            {
                w.WritePropertyName("autopilot");
                WriteJson(w, autopilotJson);
            }
            w.WriteEndObject();
        }
        return Encoding.UTF8.GetString(buffer.ToArray()) + "\n";
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor.Core/AdvisorTraceLog.cs src/Sts2Advisor.Core.Tests/AdvisorTraceLogTests.cs
git commit -m "feat(advisor): trace lines can mark an autopilot-executed step"
```

---

### Task 7: mod 引用 Harmony；`Capture` 带上选项模型

**Files:**
- Modify: `src/Sts2Advisor/Sts2Advisor.csproj`
- Modify: `src/Sts2Advisor/DecisionCapture.cs`

- [ ] **Step 1: csproj 加 0Harmony 引用**

在 `<Reference Include="GodotSharp">…</Reference>` 后面加：

```xml
    <Reference Include="0Harmony">
      <HintPath>$(Sts2DataDir)/0Harmony.dll</HintPath>
      <Private>false</Private>
    </Reference>
```

- [ ] **Step 2: Capture 加两个属性**

`src/Sts2Advisor/DecisionCapture.cs`：文件头加 `using MegaCrit.Sts2.Core.Entities.Merchant;`。在 `Capture` 类的 `Build` 属性后面加：

```csharp
    /// <summary>The options in the order the decision JSON lists them, so an advised index finds its
    /// game object: CardModel (card_reward, card_select), EventOption, RestSiteOption, MapPoint,
    /// IReadOnlyList&lt;CardModel&gt; (bundle_select), MerchantEntry (shop cards: character then colorless).</summary>
    public IReadOnlyList<object> Choices { get; init; } = Array.Empty<object>();

    /// <summary>Shop only: relics, potions and the removal are found here by index.</summary>
    public MerchantInventory? Inventory { get; init; }
```

- [ ] **Step 3: 每个 builder 填 Choices**

在各 `new Capture { … }` 初始化里各加一行（只加这一行，别的不动）：

| 方法 | 加的行 |
|---|---|
| `Map` | `Choices = points.Cast<object>().ToList(),` |
| `CardReward` | `Choices = cards.Cast<object>().ToList(),` |
| `CardSelect` | `Choices = cards.Cast<object>().ToList(),` |
| `Bundle` | `Choices = bundles.Cast<object>().ToList(),` |
| `Event` | `Choices = options.Cast<object>().ToList(),` |
| `Rest` | `Choices = options.Cast<object>().ToList(),` |
| `Shop` | `Choices = cardEntries.Cast<object>().ToList(), Inventory = inv,` |

`Capture` 类的 summary 改为：`... Build() produces the decision JSON (the headless engine's shape, via the shared DecisionSerializer); it must run on the main thread, in the same tick as Detect(). Choices lists the game objects behind the JSON's option indices.`

- [ ] **Step 4: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -3`
Expected: `0 Error(s)`

（没改 `src/Sts2Shared/`，决策 JSON 不变，不需要 `replay_states --compare`。）

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor/Sts2Advisor.csproj src/Sts2Advisor/DecisionCapture.cs
git commit -m "feat(advisor-mod): captures keep the game objects behind each option index"
```

---

### Task 8: `AutoPilotChoice` —— 把建议变成 handler 要的 UI 节点

**Files:**
- Create: `src/Sts2Advisor/AutoPilot/AutoPilotChoice.cs`

- [ ] **Step 1: 写代码**

```csharp
// src/Sts2Advisor/AutoPilot/AutoPilotChoice.cs
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Nodes.Cards;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.RestSite;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Shops;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>One answer from the advice service for one settled screen.</summary>
internal sealed record CachedAdvice(Capture Capture, AdviceReply Reply, PlannedAction Plan, string Json);

/// <summary>Thrown out of a handler when it picks on a screen there is no advice for yet. The loop catches
/// it, asks the service (off the main thread), and reruns the handler, which reads the screen afresh.</summary>
internal sealed class NeedAdviceException(string screen, string fingerprint)
    : Exception($"autopilot needs advice for {screen}")
{
    public string Screen { get; } = screen;
    public string Fingerprint { get; } = fingerprint;
}

/// <summary>Stops the autopilot with a reason (the loop's way out).</summary>
internal sealed class AutoPilotStopException(StopReason reason, string? detail)
    : Exception($"autopilot stop: {reason} {detail}")
{
    public StopReason Reason { get; } = reason;
    public string? Detail { get; } = detail;
}

/// <summary>What the handlers call back into (implemented by AutoPilotLoop).</summary>
internal interface IAutoPilotHost
{
    Task<CachedAdvice> FetchAdviceAsync(CancellationToken ct);
    void StepTaken(CachedAdvice advice);
    Task DrainUntilAsync(Task pending, CancellationToken ct);
}

/// <summary>The game object a UI node stands for, as Capture.Choices holds it.</summary>
internal static class UiModel
{
    public static object? Of(object node) => node switch
    {
        NCardHolder h => h.CardModel,  // NGridCardHolder too
        NEventOptionButton b => b.Option,
        NRestSiteButton b => b.Option,
        NCardBundle b => b.Bundle,
        NMapPoint p => p.Point,
        NMerchantSlot s => s.Entry,
        _ => null,
    };

    /// <summary>A grid may show a copy of a deck card: same card and upgrade level is the same choice.</summary>
    public static bool SameCard(object? a, object? b) =>
        a is CardModel x && b is CardModel y && x.Id == y.Id && x.CurrentUpgradeLevel == y.CurrentUpgradeLevel;
}

/// <summary>Replaces `random.NextItem(items)` inside the AutoSlay handlers (HandlerPatches rewrites the calls).
/// Main thread only, like the handlers. Outside the autopilot (the game's own AutoSlay) it is NextItem.</summary>
internal static class AutoPilotChoice
{
    private static LocLookup? _loc;
    private static IAutoPilotHost? _host;
    private static CachedAdvice? _advice;
    private static readonly List<int> Remaining = new();
    private static bool _stepRecorded;

    public static bool Active => _host != null;

    public static void Begin(LocLookup loc, IAutoPilotHost host)
    {
        _loc = loc;
        _host = host;
        Clear();
    }

    public static void End()
    {
        _host = null;
        Clear();
    }

    public static void Offer(CachedAdvice advice)
    {
        _advice = advice;
        Remaining.Clear();
        Remaining.AddRange(advice.Plan.Indices);
        _stepRecorded = false;
    }

    private static void Clear()
    {
        _advice = null;
        Remaining.Clear();
        _stepRecorded = false;
    }

    public static T? Pick<T>(Rng random, IEnumerable<T> items)
    {
        if (!Active)
            return random.NextItem(items);
        var list = items as IReadOnlyList<T> ?? items.ToList();
        var cap = DecisionCapture.Detect(_loc!);
        if (cap is not { Kind: CaptureKind.Advise })
            throw new AutoPilotException($"a handler picks among {list.Count} {typeof(T).Name} but the screen reads as {cap?.Screen ?? "nothing"}");
        if (_advice == null || _advice.Capture.Fingerprint != cap.Fingerprint)
            throw new NeedAdviceException(cap.Screen, cap.Fingerprint);
        if (_advice.Plan.Kind != PlanKind.Pick)
            throw new AutoPilotException($"{cap.Screen}: the handler picks an option but the advice is {_advice.Plan.Kind}");

        foreach (int i in Remaining)
        {
            if (i >= cap.Choices.Count)
                throw new AutoPilotException($"{cap.Screen}: advised index {i} is outside the {cap.Choices.Count} options");
            object model = cap.Choices[i];
            var node = list.FirstOrDefault(n => n != null && ReferenceEquals(UiModel.Of(n), model));
            if (node == null)
            {
                node = list.FirstOrDefault(n => n != null && UiModel.SameCard(UiModel.Of(n), model));
                if (node != null)
                    ModLog.Info($"autopilot: {cap.Screen} option {i} matched by card id, not by object");
            }
            if (node == null)
                continue;
            Remaining.Remove(i);
            if (!_stepRecorded)
            {
                _stepRecorded = true;
                _host!.StepTaken(_advice);
            }
            return node;
        }
        throw new AutoPilotException(Remaining.Count == 0
            ? $"{cap.Screen}: the handler wants more picks than the advice has ({_advice.Plan.Indices.Count})"
            : $"{cap.Screen}: no advised option ({string.Join(",", Remaining)}) is among the {list.Count} the screen offers");
    }

    /// <summary>After a handler returns: advised picks it never asked for (a screen that confirmed early).</summary>
    public static void WarnUnusedPicks(string handler)
    {
        if (_advice != null && _stepRecorded && Remaining.Count > 0)
            ModLog.Info($"autopilot: {handler} finished with advised picks {string.Join(",", Remaining)} unused");
    }
}
```

- [ ] **Step 2: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -3`
Expected: `0 Error(s)`。（`Capture`、`DecisionCapture`、`ModLog` 在外层命名空间 `Sts2Advisor`，嵌套命名空间里直接可见。）

- [ ] **Step 3: Commit**

```bash
git add src/Sts2Advisor/AutoPilot/AutoPilotChoice.cs
git commit -m "feat(advisor-mod): AutoPilotChoice maps cached advice onto the handler's UI nodes"
```

---

### Task 9: `CombatWait` + `HandlerPatches` + 在 ModEntry 打补丁

**Files:**
- Create: `src/Sts2Advisor/AutoPilot/CombatWait.cs`
- Create: `src/Sts2Advisor/AutoPilot/HandlerPatches.cs`
- Modify: `src/Sts2Advisor/ModEntry.cs`
- Modify: `src/Sts2Advisor/AdvisorRuntime.cs`（`Start` 多一个参数）

- [ ] **Step 1: CombatWait**

```csharp
// src/Sts2Advisor/AutoPilot/CombatWait.cs
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;

namespace Sts2Advisor.AutoPilot;

/// <summary>Combat belongs to CombatSolver (or the player, when it pauses full-auto): only wait. No timeout --
/// a time-budgeted solver turn can take minutes.</summary>
internal static class CombatWait
{
    public static async Task UntilOverAsync(CancellationToken ct)
    {
        while (CombatManager.Instance?.IsInProgress == true)
            await Task.Delay(250, ct);
    }

    /// <summary>Replaces EventRoomHandler.HandleEventCombat (HandlerPatches), which gives the player
    /// 100 Strength/Plating/Regen and kills the enemies outright.</summary>
    public static async Task ForEventCombat(EventRoomHandler handler, CancellationToken ct)
    {
        if (!AutoPilotChoice.Active)
        {
            await handler.HandleEventCombat(ct);
            return;
        }
        await WaitHelper.Until(() => CombatManager.Instance.IsInProgress, ct, AutoSlayConfig.nodeWaitTimeout, "Event combat not started");
        await UntilOverAsync(ct);
    }
}
```

（`HandleEventCombat` 是 private，Publicizer 让它可调用。）

- [ ] **Step 2: HandlerPatches**

```csharp
// src/Sts2Advisor/AutoPilot/HandlerPatches.cs
using System.Reflection;
using System.Reflection.Emit;
using HarmonyLib;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.Random;

namespace Sts2Advisor.AutoPilot;

/// <summary>Rewrites the AutoSlay handlers the autopilot reuses: each `random.NextItem(list)` becomes
/// AutoPilotChoice.Pick, and EventRoomHandler's `HandleEventCombat(ct)` becomes CombatWait.ForEventCombat.
/// A transpiler on the handlers' own async MoveNext, not a prefix on Rng.NextItem: nothing else in the
/// game is touched, and an inlined NextItem cannot slip past it. Both replacements are no-ops outside
/// the autopilot.</summary>
internal static class HandlerPatches
{
    private static readonly MethodInfo NextItem = AccessTools.Method(typeof(Rng), nameof(Rng.NextItem));
    private static readonly MethodInfo Pick = AccessTools.Method(typeof(AutoPilotChoice), nameof(AutoPilotChoice.Pick));
    private static readonly MethodInfo EventCombat = AccessTools.Method(typeof(EventRoomHandler), "HandleEventCombat");
    private static readonly MethodInfo EventCombatWait = AccessTools.Method(typeof(CombatWait), nameof(CombatWait.ForEventCombat));

    /// <summary>Handler, and how many NextItem / HandleEventCombat calls its HandleAsync must contain
    /// (2026-10-28, game v0.111.0). A different count means the game changed: autopilot stays off.</summary>
    private static readonly (Type Handler, int NextItems, int EventCombats)[] Targets =
    {
        (typeof(EventRoomHandler), 1, 2),
        (typeof(RestSiteRoomHandler), 1, 0),
        (typeof(DeckUpgradeScreenHandler), 1, 0),
        (typeof(DeckTransformScreenHandler), 1, 0),
        (typeof(DeckEnchantScreenHandler), 1, 0),
        (typeof(DeckCardSelectScreenHandler), 1, 0),
        (typeof(SimpleCardSelectScreenHandler), 1, 0),
        (typeof(ChooseACardScreenHandler), 1, 0),
        (typeof(ChooseABundleScreenHandler), 1, 0),
    };

    /// <summary>What the transpiler rewrote during the Patch call in progress (Patch runs it synchronously).</summary>
    private static (int NextItems, int EventCombats) _rewritten;

    /// <summary>Patches every target; null when all were rewritten as expected, else what went wrong.</summary>
    public static string? Apply(Harmony harmony)
    {
        var problems = new List<string>();
        foreach (var (handler, nextItems, eventCombats) in Targets)
        {
            var moveNext = AccessTools.AsyncMoveNext(AccessTools.Method(handler, "HandleAsync"));
            if (moveNext == null)
            {
                problems.Add($"{handler.Name}: no async HandleAsync");
                continue;
            }
            _rewritten = (0, 0);
            harmony.Patch(moveNext, transpiler: new HarmonyMethod(typeof(HandlerPatches), nameof(Redirect)));
            var got = _rewritten;
            if (got != (nextItems, eventCombats))
                problems.Add($"{handler.Name}: rewrote {got.NextItems} NextItem / {got.EventCombats} HandleEventCombat, expected {nextItems} / {eventCombats}");
        }
        return problems.Count == 0 ? null : string.Join("; ", problems);
    }

    private static IEnumerable<CodeInstruction> Redirect(IEnumerable<CodeInstruction> instructions)
    {
        int nextItems = 0, eventCombats = 0;
        var result = new List<CodeInstruction>();
        foreach (var ins in instructions)
        {
            if ((ins.opcode == OpCodes.Callvirt || ins.opcode == OpCodes.Call) && ins.operand is MethodInfo m)
            {
                // Stack [rng, items] -> static Pick<T>(Rng, IEnumerable<T>); [handler, ct] -> static ForEventCombat(handler, ct).
                if (m.IsGenericMethod && m.GetGenericMethodDefinition() == NextItem)
                {
                    ins.opcode = OpCodes.Call;
                    ins.operand = Pick.MakeGenericMethod(m.GetGenericArguments());
                    nextItems++;
                }
                else if (m == EventCombat)
                {
                    ins.opcode = OpCodes.Call;
                    ins.operand = EventCombatWait;
                    eventCombats++;
                }
            }
            result.Add(ins);
        }
        _rewritten = (nextItems, eventCombats);
        return result;
    }
}
```

- [ ] **Step 3: ModEntry 打补丁并把结果交给 runtime**

`src/Sts2Advisor/ModEntry.cs`：文件头加 `using HarmonyLib;` 和 `using Sts2Advisor.AutoPilot;`。把 `AdvisorRuntime.Start(config, configPath, userDir);` 换成：

```csharp
        string? patchProblem;
        try
        {
            patchProblem = HandlerPatches.Apply(new Harmony("morefreeze.sts2advisor.autopilot"));
        }
        catch (Exception ex)
        {
            patchProblem = $"{ex.GetType().Name}: {ex.Message}";
        }
        if (patchProblem != null)
            ModLog.Error($"autopilot disabled, AutoSlay handlers not patched: {patchProblem}");
        else
            ModLog.Info("autopilot: AutoSlay handlers patched");
        AdvisorRuntime.Start(config, configPath, userDir, patchProblem);
```

`ModLog.Info($"loaded; config …")` 那行末尾加上 `autopilot_hotkey={config.AutopilotHotkey}`。

`src/Sts2Advisor/AdvisorRuntime.cs`：`Start` 签名改为 `public static void Start(AdvisorConfig config, string configPath, string userDir, string? autopilotProblem)`（参数这一步先不用，Task 12 接上）。

- [ ] **Step 4: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -3`
Expected: `0 Error(s)`。若 `handler.HandleEventCombat` 报不可访问：确认 csproj 的 `<Publicize Include="sts2" …/>` 还在（它公开 private 方法）。

- [ ] **Step 5: Commit**

```bash
git add src/Sts2Advisor/AutoPilot/CombatWait.cs src/Sts2Advisor/AutoPilot/HandlerPatches.cs src/Sts2Advisor/ModEntry.cs src/Sts2Advisor/AdvisorRuntime.cs
git commit -m "feat(advisor-mod): transpile AutoSlay handlers to take advised picks; event combat waits for CombatSolver"
```

---

### Task 10: 我们自己的四个 handler

**Files:**
- Create: `src/Sts2Advisor/AutoPilot/RewardsHandler.cs`
- Create: `src/Sts2Advisor/AutoPilot/CardRewardHandler.cs`
- Create: `src/Sts2Advisor/AutoPilot/MapHandler.cs`
- Create: `src/Sts2Advisor/AutoPilot/ShopHandler.cs`

- [ ] **Step 1: RewardsHandler**（照 AutoSlay `RewardsScreenHandler`，区别：已点过的按钮在同一个奖励页上跨调用记住，这样跳过的卡牌奖励不会被反复点开）

```csharp
// src/Sts2Advisor/AutoPilot/RewardsHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Context;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rewards;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rewards;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Advisor.AutoPilot;

/// <summary>AutoSlay's RewardsScreenHandler, except that the buttons already clicked on this rewards screen
/// are remembered across calls: a skipped card reward leaves its button enabled, and must not be reopened.</summary>
internal sealed class RewardsHandler : IScreenHandler
{
    private NRewardsScreen? _screen;
    private readonly HashSet<NRewardButton> _attempted = new();

    public Type ScreenType => typeof(NRewardsScreen);
    public TimeSpan Timeout => TimeSpan.FromSeconds(30);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var screen = AutoSlayer.GetCurrentScreen<NRewardsScreen>();
        if (!ReferenceEquals(screen, _screen))
        {
            _screen = screen;
            _attempted.Clear();
        }
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            bool hasPotionSlots = LocalContext.GetMe(RunManager.Instance.DebugOnlyGetState())?.HasOpenPotionSlots ?? false;
            var button = UiHelper.FindAll<NRewardButton>(screen).FirstOrDefault(b =>
                b.IsEnabled && !_attempted.Contains(b) && (b.Reward is not PotionReward || hasPotionSlots));
            if (button == null)
                break;
            _attempted.Add(button);
            await UiHelper.Click(button);
            await Task.Delay(500, ct);
            var top = NOverlayStack.Instance?.Peek();
            if (top != null && top != screen)
                return;  // a card reward (or another child screen) opened: the loop handles it, then comes back
        }
        var proceed = UiHelper.FindFirst<NProceedButton>(screen);
        if (proceed != null)
        {
            await UiHelper.Click(proceed);
            await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || NOverlayStack.Instance?.Peek() != screen
                                         || (NMapScreen.Instance?.IsOpen ?? false),
                                   ct, TimeSpan.FromSeconds(10), "Rewards screen did not close after proceed");
        }
    }
}
```

- [ ] **Step 2: CardRewardHandler**

```csharp
// src/Sts2Advisor/AutoPilot/CardRewardHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Nodes.Cards.Holders;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Takes the advised card or presses Skip (AutoSlay's CardRewardScreenHandler always takes one).</summary>
internal sealed class CardRewardHandler(IAutoPilotHost host) : IScreenHandler
{
    public Type ScreenType => typeof(NCardRewardSelectionScreen);
    public TimeSpan Timeout => TimeSpan.FromSeconds(60);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var screen = AutoSlayer.GetCurrentScreen<NCardRewardSelectionScreen>();
        await Task.Delay(400, ct);
        var advice = await host.FetchAdviceAsync(ct);
        if (advice.Capture.Header.Decision != "card_reward")
            throw new AutoPilotException($"card reward screen, but the settled decision is {advice.Capture.Header.Decision}");
        if (advice.Plan.Kind == PlanKind.Skip)
        {
            int j = screen._extraOptions.ToList().FindIndex(a => string.Equals(a.OptionId, "Skip", StringComparison.OrdinalIgnoreCase));
            var buttons = screen._rewardAlternativesContainer.GetChildren().OfType<NCardRewardAlternativeButton>().ToList();
            if (j < 0 || j >= buttons.Count)
                throw new AutoPilotException("card reward: the advice skips but there is no skip button");
            host.StepTaken(advice);
            await UiHelper.Click(buttons[j]);
        }
        else
        {
            int i = advice.Plan.Indices[0];
            if (i >= advice.Capture.Choices.Count)
                throw new AutoPilotException($"card reward: index {i} is outside the {advice.Capture.Choices.Count} cards");
            var card = advice.Capture.Choices[i];
            var holder = UiHelper.FindAll<NCardHolder>(screen).FirstOrDefault(h => ReferenceEquals(h.CardModel, card))
                ?? throw new AutoPilotException($"card reward: no card holder shows card {i}");
            host.StepTaken(advice);
            holder.EmitSignal(NCardHolder.SignalName.Pressed, holder);
        }
        await WaitHelper.Until(() => !GodotObject.IsInstanceValid(screen) || !screen.IsVisibleInTree(),
                               ct, TimeSpan.FromSeconds(10), "Card reward screen did not close");
    }
}
```

- [ ] **Step 3: MapHandler**（照 AutoSlay `MapScreenHandler`，节点改用建议的坐标）

```csharp
// src/Sts2Advisor/AutoPilot/MapHandler.cs
using MegaCrit.Sts2.Core.AutoSlay;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Travels to the advised map node.</summary>
internal sealed class MapHandler(IAutoPilotHost host) : IHandler
{
    public TimeSpan Timeout => TimeSpan.FromSeconds(60);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var map = NMapScreen.Instance ?? throw new AutoPilotException("map screen gone");
        var advice = await host.FetchAdviceAsync(ct);
        if (advice.Plan.Kind != PlanKind.MapNode)
            throw new AutoPilotException($"map screen, but the advice is {advice.Plan.Kind} for {advice.Capture.Header.Decision}");
        int col = advice.Plan.Col, row = advice.Plan.Row;
        if (!advice.Capture.Choices.OfType<MapPoint>().Any(p => p.coord.col == col && p.coord.row == row))
            throw new AutoPilotException($"map: ({col},{row}) is not a reachable node");
        var point = UiHelper.FindAll<NMapPoint>(map).FirstOrDefault(p => p.Point.coord.col == col && p.Point.coord.row == row)
            ?? throw new AutoPilotException($"map: no node drawn at ({col},{row})");
        await WaitHelper.Until(() => point.IsEnabled, ct, AutoSlayConfig.mapPointEnabledTimeout, "Map point not enabled");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnEntered() => entered.TrySetResult();
        RunManager.Instance.RoomEntered += OnEntered;
        try
        {
            host.StepTaken(advice);
            await UiHelper.Click(point);
            await WaitHelper.ForTask(entered.Task, ct, AutoSlayConfig.mapScreenTimeout, "Room not entered after map click");
        }
        finally
        {
            RunManager.Instance.RoomEntered -= OnEntered;
        }
    }
}
```

- [ ] **Step 4: ShopHandler**（照 AutoSlay `ShopRoomHandler` 的购买方式，按建议一步步买/删/离开）

```csharp
// src/Sts2Advisor/AutoPilot/ShopHandler.cs
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Entities.Merchant;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Rooms;
using MegaCrit.Sts2.Core.Random;
using Sts2Advisor.Core;

namespace Sts2Advisor.AutoPilot;

/// <summary>Opens the merchant, then asks greedy one step at a time (buy a card/relic/potion, remove a card,
/// leave) and does it, re-asking after every purchase. A removal opens the deck select screen, which the
/// loop's overlay handling answers from the advice (card_select).</summary>
internal sealed class ShopHandler(IAutoPilotHost host) : IHandler
{
    private const int MaxSteps = 30;

    public TimeSpan Timeout => TimeSpan.FromMinutes(5);

    public async Task HandleAsync(Rng random, CancellationToken ct)
    {
        var root = ((SceneTree)Engine.GetMainLoop()).Root;
        var room = await WaitHelper.ForNode<NMerchantRoom>(root, "/root/Game/RootSceneContainer/Run/RoomContainer/MerchantRoom", ct);
        if (!room.Inventory.IsOpen)
        {
            room.OpenInventory();
            await Task.Delay(500, ct);
        }
        for (int step = 0; ; step++)
        {
            if (step >= MaxSteps)
                throw new AutoPilotException($"shop: more than {MaxSteps} steps");
            var advice = await host.FetchAdviceAsync(ct);
            if (advice.Capture.Header.Decision != "shop")
                throw new AutoPilotException($"in the shop, but the settled decision is {advice.Capture.Header.Decision}");
            if (advice.Plan.Kind == PlanKind.Leave)
            {
                host.StepTaken(advice);
                break;
            }
            var entry = EntryFor(advice);
            var inventory = room.Inventory.Inventory ?? throw new AutoPilotException("shop: no inventory");
            host.StepTaken(advice);
            var purchase = entry.OnTryPurchaseWrapper(inventory);
            await host.DrainUntilAsync(purchase, ct);
            if (!await purchase)
                throw new AutoPilotException($"shop: {advice.Plan.Kind} was refused");
            await Task.Delay(300, ct);
        }
        var back = UiHelper.FindFirst<NBackButton>(room);
        if (back != null && room.Inventory.IsOpen)
        {
            await UiHelper.Click(back);
            await Task.Delay(300, ct);
        }
        await UiHelper.Click(room.ProceedButton);
    }

    private static MerchantEntry EntryFor(CachedAdvice advice)
    {
        var cap = advice.Capture;
        var inv = cap.Inventory ?? throw new AutoPilotException("shop capture without an inventory");
        int i = advice.Plan.Indices.Count > 0 ? advice.Plan.Indices[0] : -1;
        MerchantEntry? entry = advice.Plan.Kind switch
        {
            PlanKind.BuyCard => i < cap.Choices.Count ? cap.Choices[i] as MerchantEntry : null,
            PlanKind.BuyRelic => i < inv.RelicEntries.Count ? inv.RelicEntries[i] : null,
            PlanKind.BuyPotion => i < inv.PotionEntries.Count ? inv.PotionEntries[i] : null,
            PlanKind.RemoveCard => inv.CardRemovalEntry,
            _ => throw new AutoPilotException($"shop: unexpected plan {advice.Plan.Kind}"),
        };
        return entry is { IsStocked: true }
            ? entry
            : throw new AutoPilotException($"shop: {advice.Plan.Kind} {i} is not for sale");
    }
}
```

- [ ] **Step 5: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | grep -E "error|Error\(s\)" | head`
Expected: `0 Error(s)`。可能出现的编译问题和对照（以 Task 0 的反编译为准）：
- `NMerchantInventory.IsOpen`：public get。
- `NCardRewardAlternativeButton`：命名空间 `MegaCrit.Sts2.Core.Nodes.Screens.CardSelection`。
- `room.Inventory.Inventory`：`NMerchantInventory.Inventory` 是 `MerchantInventory?`。

- [ ] **Step 6: Commit**

```bash
git add src/Sts2Advisor/AutoPilot/RewardsHandler.cs src/Sts2Advisor/AutoPilot/CardRewardHandler.cs src/Sts2Advisor/AutoPilot/MapHandler.cs src/Sts2Advisor/AutoPilot/ShopHandler.cs
git commit -m "feat(advisor-mod): autopilot handlers for rewards, card reward (with skip), map and shop"
```

---

### Task 11: `AutoPilotLoop` —— 状态驱动循环

**Files:**
- Create: `src/Sts2Advisor/AutoPilot/AutoPilotLoop.cs`
- Modify: `src/Sts2Advisor/AdvisorRuntime.cs`（`JsonOpts` 改 `internal`）

- [ ] **Step 1: JsonOpts 可见**

`src/Sts2Advisor/AdvisorRuntime.cs`：`private static readonly JsonSerializerOptions JsonOpts` 改为 `internal static readonly JsonSerializerOptions JsonOpts`。

- [ ] **Step 2: 写 AutoPilotLoop**

```csharp
// src/Sts2Advisor/AutoPilot/AutoPilotLoop.cs
using System.Text.Json;
using Godot;
using MegaCrit.Sts2.Core.AutoSlay.Handlers;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Rooms;
using MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens;
using MegaCrit.Sts2.Core.AutoSlay.Helpers;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Nodes.CommonUi;
using MegaCrit.Sts2.Core.Nodes.Events;
using MegaCrit.Sts2.Core.Nodes.GodotExtensions;
using MegaCrit.Sts2.Core.Nodes.Screens;
using MegaCrit.Sts2.Core.Nodes.Screens.CardSelection;
using MegaCrit.Sts2.Core.Nodes.Screens.GameOverScreen;
using MegaCrit.Sts2.Core.Nodes.Screens.Map;
using MegaCrit.Sts2.Core.Nodes.Screens.Overlays;
using MegaCrit.Sts2.Core.Random;
using MegaCrit.Sts2.Core.Rooms;
using MegaCrit.Sts2.Core.Runs;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>Plays the rest of the run from wherever the player switched the autopilot on. Every turn it looks
/// at the screen and does the one thing that screen needs: an overlay (rewards, card reward, deck select, ...),
/// combat (wait for CombatSolver), the map, or the room itself (event, rest site, shop, treasure, proceed).
/// Runs on the main thread (started from the frame tick; awaits resume there through Godot's
/// SynchronizationContext, as the game's own AutoSlay relies on). AutoSlayer.Start() is never called: it would
/// abandon the run, write unlocks to the save and quit the game.</summary>
internal sealed class AutoPilotLoop : IAutoPilotHost
{
    private const string RoomContainerPath = "/root/Game/RootSceneContainer/Run/RoomContainer";
    private const double StuckSeconds = 30;
    private const double FetchSeconds = 20;
    private const int MaxSameOverlay = 3;
    /// <summary>An event can start a combat; CombatSolver may need minutes per turn.</summary>
    private static readonly TimeSpan EventTimeout = TimeSpan.FromHours(4);

    private readonly IAdvisor _advisor;
    private readonly LocLookup _loc;
    private readonly AdvisorTraceLog? _trace;
    private readonly AutoPilotSession _session;
    private readonly Func<double> _now;
    private readonly Rng _rng = new(0uL);  // never drawn from: every NextItem in the handlers is redirected
    private readonly Dictionary<Type, IScreenHandler> _screens;
    private readonly EventRoomHandler _event = new();
    private readonly RestSiteRoomHandler _rest = new();
    private readonly TreasureRoomHandler _treasure = new();
    private readonly MapHandler _map;
    private readonly ShopHandler _shop;
    private readonly HashSet<string> _doneRooms = new();
    private string _progressKey = "";
    private double _progressAt;
    private int _sameOverlay;

    public AutoPilotLoop(IAdvisor advisor, LocLookup loc, AdvisorTraceLog? trace, AutoPilotSession session, Func<double> now)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _session = session;
        _now = now;
        _map = new MapHandler(this);
        _shop = new ShopHandler(this);
        _screens = new Dictionary<Type, IScreenHandler>
        {
            [typeof(NRewardsScreen)] = new RewardsHandler(),
            [typeof(NCardRewardSelectionScreen)] = new CardRewardHandler(this),
            [typeof(NDeckUpgradeSelectScreen)] = new DeckUpgradeScreenHandler(),
            [typeof(NDeckTransformSelectScreen)] = new DeckTransformScreenHandler(),
            [typeof(NDeckEnchantSelectScreen)] = new DeckEnchantScreenHandler(),
            [typeof(NDeckCardSelectScreen)] = new DeckCardSelectScreenHandler(),
            [typeof(NSimpleCardSelectScreen)] = new SimpleCardSelectScreenHandler(),
            [typeof(NChooseACardSelectionScreen)] = new ChooseACardScreenHandler(),
            [typeof(NChooseABundleSelectionScreen)] = new ChooseABundleScreenHandler(),
        };
    }

    /// <summary>Runs until something stops it; never throws.</summary>
    public async Task<(StopReason Reason, string? Detail)> RunAsync(CancellationToken ct)
    {
        AutoPilotChoice.Begin(_loc, this);
        try
        {
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                await StepAsync(ct);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            return (StopReason.Hotkey, null);
        }
        catch (AutoPilotStopException e)
        {
            return (e.Reason, e.Detail);
        }
        catch (Exception e) when (e is TypeLoadException or MissingMemberException)
        {
            ModLog.Error($"autopilot: game API mismatch: {e}");
            return (StopReason.Unsupported, e.GetType().Name);
        }
        catch (Exception e)
        {
            ModLog.Error($"autopilot: {e}");
            return (StopReason.HandlerFailed, $"{e.GetType().Name}: {e.Message}");
        }
        finally
        {
            AutoPilotChoice.End();
            _session.InCombat = false;
        }
    }

    private async Task StepAsync(CancellationToken ct)
    {
        var rs = RunManager.Instance?.DebugOnlyGetState() ?? throw new AutoPilotStopException(StopReason.LeftRun, null);
        if (rs.Players.Count != 1)
            throw new AutoPilotStopException(StopReason.Multiplayer, null);

        var overlay = NOverlayStack.Instance?.Peek();
        if (overlay is NGameOverScreen)
            throw new AutoPilotStopException(StopReason.GameOver, null);
        if (overlay != null)
        {
            await HandleOverlayAsync(overlay, ct);
            return;
        }
        _sameOverlay = 0;

        if (CombatManager.Instance?.IsInProgress == true)
        {
            _session.InCombat = true;
            try { await CombatWait.UntilOverAsync(ct); }
            finally { _session.InCombat = false; }
            MarkProgress();
            return;
        }
        if (NMapScreen.Instance is { IsOpen: true, IsTravelEnabled: true })
        {
            await RunHandlerAsync(_map, _map.Timeout, ct);
            MarkProgress();
            return;
        }

        var room = rs.CurrentRoom;
        string roomKey = $"{rs.CurrentActIndex}/{rs.TotalFloor}/{room?.RoomType}";
        switch (room)
        {
            case EventRoom er when !er.LocalMutableEvent.IsFinished && er.LocalMutableEvent.CurrentOptions.Any(o => !o.IsLocked):
                await RunHandlerAsync(_event, EventTimeout, ct);
                MarkProgress();
                return;
            case RestSiteRoom rr when rr.Options.Any(o => o.IsEnabled) && _doneRooms.Add(roomKey):
                await RunHandlerAsync(_rest, _rest.Timeout, ct);
                MarkProgress();
                return;
            case MerchantRoom when _doneRooms.Add(roomKey):
                await RunHandlerAsync(_shop, _shop.Timeout, ct);
                MarkProgress();
                return;
            case TreasureRoom when _doneRooms.Add(roomKey):
                await RunHandlerAsync(_treasure, _treasure.Timeout, ct);
                MarkProgress();
                return;
        }
        if (await ClickProceedAsync(ct))
            return;
        await IdleAsync(rs, room, ct);
    }

    private async Task HandleOverlayAsync(IOverlayScreen overlay, CancellationToken ct)
    {
        if (!_screens.TryGetValue(overlay.GetType(), out var handler))
        {
            // No advice exists for it (choose a relic, crystal sphere, ...): the player does this one screen.
            string name = overlay.GetType().Name;
            _session.HandBack(name);
            ModLog.Info($"autopilot: handing {name} back to the player");
            while (NOverlayStack.Instance?.Peek() == overlay)
                await Task.Delay(200, ct);  // no timeout: the player takes as long as they like
            _session.ClearHandBack();
            MarkProgress();
            return;
        }
        int countBefore = NOverlayStack.Instance!.ScreenCount;
        await RunHandlerAsync(handler, handler.Timeout, ct);
        var stack = NOverlayStack.Instance;
        if (stack != null && stack.ScreenCount == countBefore && stack.Peek() == overlay)
        {
            if (++_sameOverlay >= MaxSameOverlay)
                throw new AutoPilotStopException(StopReason.Stuck, $"{overlay.GetType().Name} did not close after {MaxSameOverlay} attempts");
        }
        else
        {
            _sameOverlay = 0;
        }
        MarkProgress();
        await Task.Delay(100, ct);
    }

    /// <summary>Runs a handler; when it stops for want of advice, asks for it and runs the handler again.</summary>
    private async Task RunHandlerAsync(IHandler handler, TimeSpan timeout, CancellationToken ct)
    {
        while (true)
        {
            try
            {
                await WaitHelper.WithTimeout(t => handler.HandleAsync(_rng, t), timeout, ct);
                AutoPilotChoice.WarnUnusedPicks(handler.GetType().Name);
                return;
            }
            catch (NeedAdviceException e)
            {
                if (!_session.CountAdviceRetry(e.Fingerprint))
                    throw new AutoPilotStopException(StopReason.Stuck,
                        $"{e.Screen}: asked for advice {AutoPilotSession.MaxAdviceRetries} times without settling");
                await FetchAdviceAsync(ct);
            }
            catch (AutoPilotException e)
            {
                throw new AutoPilotStopException(StopReason.HandlerFailed, e.Message);
            }
        }
    }

    /// <summary>Event proceed option, or an enabled Proceed button in the room. True when one was clicked.</summary>
    private async Task<bool> ClickProceedAsync(CancellationToken ct)
    {
        var container = ((SceneTree)Engine.GetMainLoop()).Root.GetNodeOrNull(RoomContainerPath);
        if (container == null)
            return false;
        NClickableControl? button = UiHelper.FindAll<NEventOptionButton>(container)
            .FirstOrDefault(o => o.Option.IsProceed && !o.Option.IsLocked && o.IsEnabled && o.IsVisibleInTree());
        button ??= UiHelper.FindAll<NProceedButton>(container).FirstOrDefault(b => b.IsEnabled && b.IsVisibleInTree());
        if (button == null)
            return false;
        await UiHelper.Click(button);
        await Task.Delay(300, ct);
        MarkProgress();
        return true;
    }

    private async Task IdleAsync(RunState rs, AbstractRoom? room, CancellationToken ct)
    {
        string key = $"{rs.CurrentActIndex}/{rs.TotalFloor}/{room?.RoomType}/{NMapScreen.Instance?.IsOpen}/{NOverlayStack.Instance?.ScreenCount}";
        double now = _now();
        if (key != _progressKey)
        {
            _progressKey = key;
            _progressAt = now;
        }
        else if (now - _progressAt > StuckSeconds)
        {
            throw new AutoPilotStopException(StopReason.Stuck, $"nothing to do for {StuckSeconds:0} s at {key}");
        }
        await Task.Delay(200, ct);
    }

    private void MarkProgress() => _progressKey = "";

    public async Task<CachedAdvice> FetchAdviceAsync(CancellationToken ct)
    {
        string? last = null;
        double deadline = _now() + FetchSeconds;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var cap = DecisionCapture.Detect(_loc);
            bool advisable = cap is { Kind: CaptureKind.Advise };
            // Screens animate in: ask only about a fingerprint seen on two polls in a row (as the advice panel does).
            if (advisable && cap!.Fingerprint == last)
            {
                var advice = await AskAsync(cap, ct);
                if (advice != null)
                    return advice;
                last = null;
                deadline = _now() + FetchSeconds;  // an outage is timed by the session, not by this deadline
                continue;
            }
            last = advisable ? cap!.Fingerprint : null;
            if (_now() > deadline)
                throw new AutoPilotStopException(StopReason.Stuck, $"no settled decision on {cap?.Screen ?? "no screen"}");
            await Task.Delay(300, ct);
        }
    }

    /// <summary>Null when the service could not be reached (retry); throws when the reply is unusable.</summary>
    private async Task<CachedAdvice?> AskAsync(Capture cap, CancellationToken ct)
    {
        string decision = cap.Header.Decision;
        string json = JsonSerializer.Serialize(cap.Build!(), AdvisorRuntime.JsonOpts);
        AdviceReply reply;
        try
        {
            reply = await _advisor.AdviseAsync(json, ct);
        }
        catch (Exception e) when (e is AdviceFormatException or AdviceServiceException)
        {
            _trace?.Append(decision, json, null, e.GetType().Name);
            throw new AutoPilotStopException(StopReason.BadReply, e.Message);
        }
        catch (Exception e) when (!ct.IsCancellationRequested)
        {
            _trace?.Append(decision, json, null, e.GetType().Name);
            if (_session.Offline(_now()))
                throw new AutoPilotStopException(StopReason.Offline, e.Message);
            await Task.Delay(1000, ct);
            return null;
        }
        _session.Online();
        PlannedAction plan;
        try
        {
            plan = AutoPilotPlan.From(decision, reply);
        }
        catch (AutoPilotException e)
        {
            _trace?.Append(decision, json, reply.Raw, "autopilot_plan");
            throw new AutoPilotStopException(StopReason.HandlerFailed, e.Message);
        }
        var advice = new CachedAdvice(cap, reply, plan, json);
        AutoPilotChoice.Offer(advice);
        return advice;
    }

    public void StepTaken(CachedAdvice a)
    {
        bool zh = GameText.IsChinese;
        int n = _session.Record(AutoPilotText.StepText(a.Capture.Header.Decision, a.Reply, a.Capture.Labels, zh),
                                AutoPilotText.Reason(a.Reply, zh));
        _trace?.Append(a.Capture.Header.Decision, a.Json, a.Reply.Raw, null, autopilotStep: n);
        ModLog.Info($"autopilot step {n}: {a.Capture.Header.Decision} {a.Reply.ActionName}");
    }

    /// <summary>Answers the screens a pending action opens (AutoSlayer.DrainOverlayScreensUntilAsync).</summary>
    public async Task DrainUntilAsync(Task pending, CancellationToken ct)
    {
        while (!pending.IsCompleted)
        {
            ct.ThrowIfCancellationRequested();
            var overlay = NOverlayStack.Instance?.Peek();
            if (overlay != null && overlay is not NGameOverScreen)
                await HandleOverlayAsync(overlay, ct);
            await Task.Delay(50, ct);
        }
        await pending;
    }
}
```

- [ ] **Step 3: 编译**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | grep -E "error|Error\(s\)" | head`
Expected: `0 Error(s)`。若报 `RestSiteRoomHandler.Timeout` 之类不可访问：AutoSlay handler 的 `Timeout` 是 public 接口成员，可以用 `((IHandler)_rest).Timeout`。若 `er.LocalMutableEvent` 不存在：`DecisionCapture.Event` 用的就是它，照抄那里的写法。

- [ ] **Step 4: Commit**

```bash
git add src/Sts2Advisor/AutoPilot/AutoPilotLoop.cs src/Sts2Advisor/AdvisorRuntime.cs
git commit -m "feat(advisor-mod): AutoPilotLoop plays the rest of the run screen by screen"
```

---

### Task 12: 控制器 + 接进 AdvisorRuntime（F9、面板、横幅）

**Files:**
- Create: `src/Sts2Advisor/AutoPilot/AutoPilotControl.cs`
- Modify: `src/Sts2Advisor/AdvisorRuntime.cs`

- [ ] **Step 1: 控制器**

```csharp
// src/Sts2Advisor/AutoPilot/AutoPilotControl.cs
using System.Text.Json;
using Sts2Advisor.Core;
using Sts2Shared;

namespace Sts2Advisor.AutoPilot;

/// <summary>Switches the autopilot on and off (the hotkey) and says what it is doing. Main thread only.</summary>
internal static class AutoPilotControl
{
    private const double BannerSeconds = 15;

    private static readonly AutoPilotSession Session = new();
    private static IAdvisor? _advisor;
    private static LocLookup? _loc;
    private static AdvisorTraceLog? _trace;
    private static string _gameUserDir = "";
    private static Func<double> _now = () => 0;
    private static string? _unavailable;
    private static CancellationTokenSource? _cts;
    private static CombatSolverStatus _combatSolver = CombatSolverStatus.Ok;
    private static double _stoppedAt = double.NegativeInfinity;

    public static bool IsRunning => Session.Mode == AutoPilotMode.Running;

    /// <summary><paramref name="unavailable"/>: why the autopilot cannot run (handlers not patched, no
    /// localization); null when it can.</summary>
    public static void Init(IAdvisor advisor, LocLookup? loc, AdvisorTraceLog? trace, string gameUserDir,
                            Func<double> now, string? unavailable)
    {
        _advisor = advisor;
        _loc = loc;
        _trace = trace;
        _gameUserDir = gameUserDir;
        _now = now;
        _unavailable = unavailable;
    }

    public static void Toggle()
    {
        if (IsRunning) Stop(StopReason.Hotkey, null);
        else Start();
    }

    private static void Start()
    {
        if (_unavailable != null || _loc == null || _advisor == null)
        {
            Session.Stop(StopReason.Disabled, _unavailable ?? "no localization");
            _stoppedAt = _now();
            return;
        }
        if (!Session.Start())
            return;
        _combatSolver = CombatSolverSettings.Read(Path.Combine(_gameUserDir, CombatSolverSettings.FileName));
        var cts = new CancellationTokenSource();
        _cts = cts;
        ModLog.Info($"autopilot: started (CombatSolver {_combatSolver})");
        var loop = new AutoPilotLoop(_advisor, _loc, _trace, Session, _now);
        _ = RunAsync(loop, cts);
    }

    private static async Task RunAsync(AutoPilotLoop loop, CancellationTokenSource cts)
    {
        var (reason, detail) = await loop.RunAsync(cts.Token);
        if (ReferenceEquals(cts, _cts))  // a hotkey Stop has already finished this session
            Finish(reason, detail);
    }

    public static void Stop(StopReason reason, string? detail)
    {
        if (!IsRunning)
            return;
        var cts = _cts;
        Finish(reason, detail);
        cts?.Cancel();
    }

    private static void Finish(StopReason reason, string? detail)
    {
        _cts = null;
        Session.Stop(reason, detail);
        _stoppedAt = _now();
        ModLog.Info($"autopilot: stopped ({reason}{(detail == null ? "" : ": " + detail)}) after {Session.StepCount} step(s)");
        _trace?.Append("autopilot_stop", JsonSerializer.Serialize(new { reason = reason.ToString(), detail, steps = Session.StepCount }),
                       null, reason.ToString());
    }

    public static IReadOnlyList<PanelLine> RunningLines(string hotkey, bool zh) =>
        AutoPilotText.Running(Session, hotkey, _combatSolver, zh);

    /// <summary>The last stop, for 15 s after it (prepended to the advice panel).</summary>
    public static PanelLine? Banner(string hotkey, bool zh) =>
        !IsRunning && Session.LastStop is { } reason && _now() - _stoppedAt < BannerSeconds
            ? AutoPilotText.StopBanner(reason, Session.LastStopDetail, hotkey, zh)
            : null;
}
```

（`autopilot_stop` 行的 `reply` 为 null、带 `error`，`tests/test_advisor_trace_contract.py` 的 `load_game_states` 本来就丢掉这种行，不用改测试。）

- [ ] **Step 2: AdvisorRuntime 接线**

`src/Sts2Advisor/AdvisorRuntime.cs`：

1. 文件头加 `using Sts2Advisor.AutoPilot;`（控制器类叫 `AutoPilotControl`，不和命名空间 `Sts2Advisor.AutoPilot` 同名）。
2. 字段区加：

```csharp
    private static Key _autoKey = Key.F9;
    private static string _autoKeyName = "F9";
    private static bool _autoKeyDown;
```

3. `Start` 改成：先建 `_loc`，再不论成败都 `AutoPilotControl.Init`：

```csharp
    public static void Start(AdvisorConfig config, string configPath, string userDir, string? autopilotProblem)
    {
        _advisor = new SidecarAdvisor(config.Port);
        _trace = new AdvisorTraceLog(System.IO.Path.Combine(userDir, "advisor_trace.jsonl"));
        if (!Enum.TryParse(config.Hotkey, ignoreCase: true, out _hotkey))
            _hotkey = Key.F8;
        if (!Enum.TryParse(config.AutopilotHotkey, ignoreCase: true, out _autoKey))
            _autoKey = Key.F9;
        _autoKeyName = _autoKey.ToString();
        if (_autoKey == _hotkey)  // one press would both hide the panel and start the autopilot
            autopilotProblem ??= $"hotkey and autopilot_hotkey are both {_autoKeyName} ({configPath})";
        _loc = string.IsNullOrWhiteSpace(config.RepoPath) ? null : new LocLookup(config.RepoPath);
        bool locOk = _loc is { IsLoaded: true };
        string gameUserDir = System.IO.Path.GetDirectoryName(userDir) ?? userDir;
        AutoPilotControl.Init(_advisor, locOk ? _loc : null, _trace, gameUserDir, () => Clock.Elapsed.TotalSeconds,
                   autopilotProblem ?? (locOk ? null : $"repo_path ({configPath})"));
        if (!locOk)
        {
            _configProblem = $"repo_path ({configPath})";
            ModLog.Error($"no localization_eng/ under repo_path '{config.RepoPath}'; set repo_path in {configPath}");
            return;
        }
        var advisor = _advisor;
        _ = Task.Run(() => SidecarProcess.EnsureStartedAsync(config, advisor));
    }
```

4. `Tick` 里 `catch (Exception ex) when (ex is TypeLoadException or MissingMemberException)` 块，在 `_disabled = true;` 之后加：`try { AutoPilotControl.Stop(StopReason.Unsupported, ex.GetType().Name); } catch (Exception) { }`

5. `TickInner` 里，在 `if (_configProblem != null) { … return; }` 之后、`Capture? cap;` 之前加：

```csharp
        if (AutoPilotControl.IsRunning)
        {
            // The autopilot asks for its own advice; the advice panel would only ask twice.
            _requested = null;
            _lastSeenFingerprint = null;
            Show(AutoPilotControl.RunningLines(_autoKeyName, Zh));
            return;
        }
```

6. `PollHotkey` 末尾加：

```csharp
        bool autoDown = Input.IsKeyPressed(_autoKey);
        if (autoDown && !_autoKeyDown)
        {
            AutoPilotControl.Toggle();
            _requested = null;
        }
        _autoKeyDown = autoDown;
```

7. `Show` 改为在建议模式下带上停止横幅：

```csharp
    private static void Show(IReadOnlyList<PanelLine> lines)
    {
        if (!_visible) return;
        var banner = AutoPilotControl.Banner(_autoKeyName, Zh);
        _panel?.Show(banner == null ? lines : lines.Prepend(banner).ToList(), _language);
    }
```

8. 类 summary 末尾补一句：`The autopilot hotkey (F9) hands the run to AutoPilot; while it runs, the panel shows its steps and the advice requests here pause.`

- [ ] **Step 3: 编译 + Core 测试**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -3 && ~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests 2>&1 | tail -2`
Expected: `0 Error(s)`；测试全过（基线 228 个 + 本计划新增）。

- [ ] **Step 4: Commit**

```bash
git add src/Sts2Advisor/AutoPilot/AutoPilotControl.cs src/Sts2Advisor/AdvisorRuntime.cs
git commit -m "feat(advisor-mod): F9 toggles the autopilot; panel shows its steps and why it stopped"
```

---

### Task 13: 文档

**Files:**
- Modify: `docs/superpowers/specs/2026-10-28-sts2-advisor-autopilot-design.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: spec 回写实现方式**

1. spec 第 1 段的架构图里，`ChoiceInjector` 那行改为 `HandlerPatches    Harmony transpiler：改写 AutoSlay handler MoveNext 里的 NextItem / HandleEventCombat`，`AdviceFetcher` 那行删掉。
2. handler 表的四行改为：
   - 地图行："**我们的** `MapHandler`（照 `MapScreenHandler`，按建议坐标点节点）"；
   - 奖励页行："**我们的** `RewardsHandler`（照 `RewardsScreenHandler`，同一奖励页上点过的按钮跨调用记住，跳过的卡牌奖励不会被反复点开）"；
   - 事件行补一句："事件里触发的战斗：`HandleEventCombat`（会给玩家 +100 力量/覆甲/再生并直接杀死敌人）被改写成等 CombatSolver 打完"；
   - 战斗行补一句："同样不设超时，事件 handler 整体超时放宽到 4 小时"。
3. "`ChoiceInjector`" 和 "`AdviceFetcher`" 两段替换为：

   > **注入**：`HandlerPatches` 在 mod 加载时用 transpiler 改写 9 个 AutoSlay handler 的 `HandleAsync` 状态机 `MoveNext`：`random.NextItem(list)` → `AutoPilotChoice.Pick<T>(random, list)`，`HandleEventCombat(ct)` → `CombatWait.ForEventCombat(handler, ct)`。每个 handler 应改写的处数写在代码里，对不上就不启用全托管（面板说明原因）。不用 `Rng.NextItem<T>` 前缀补丁：泛型方法补丁会影响全局，而且 NextItem 可能被 JIT 内联到调用方从而绕过补丁。全托管没开时，两处替换都调回原方法。
   >
   > **问建议**：`Pick` 先读当前画面（`DecisionCapture.Detect`）。缓存的建议是同一指纹就按建议选节点；否则抛 `NeedAdviceException`。`AutoPilotLoop` 接住后等画面稳定、异步问 sidecar，再重跑这个 handler（handler 从当前 UI 读起，可重入）。同一决策最多重问 5 次。我们自己的 handler（卡牌奖励、地图、商店）直接 `await` 问建议。

4. "`AutoPilotLoop` 从玩家按 F9 时所在的位置接着走，结构同 `PlayRunAsync`…" 那段换成：

   > **`AutoPilotLoop` 是状态驱动的**：每轮按顺序看：没局了 → 停；结算页 → 停；有 overlay → 对应 handler（没有建议的界面交还玩家）；战斗中 → 等；地图可走 → 地图 handler；否则按当前房间：事件有可选项 → 事件 handler；篝火、商店、宝箱各一次；最后点房间里可用的 Proceed / 事件"继续"选项。什么都不能做时等 200 ms，30 秒没进展就停（`Stuck`）。

5. 第 3 段 trace 条目改为：执行的步骤写成 `"autopilot": {"executed": true, "step": n}`（嵌套对象，由 `AdvisorTraceLog.Append(..., autopilotStep: n)` 写出）；停止写成 `decision: autopilot_stop` 的一行（`reply` 为 null，带 `error` = 原因）。
6. 停止原因列表加上 `BadReply`（建议服务回复格式不对）和 `Disabled`（补丁或配置缺失，开不了）。
7. 文件头 `Date:` 行后加：`Revised 2026-10-28（写计划时）：注入改为 transpiler；地图、奖励页用自己的 handler；循环改为状态驱动；事件战斗作弊被替换。`

- [ ] **Step 2: CLAUDE.md**

在 `## Sts2Advisor (in-game advice mod)` 一节的 `- **Failure behaviour**` 条目之后插入：

```markdown
- **Autopilot (全托管)**: F9 (config `autopilot_hotkey`) hands the rest of the run to `src/Sts2Advisor/AutoPilot/`. It clicks every out-of-combat decision greedy advises, leaves combat to the CombatSolver mod's full-auto (it only warns when `combat_solver_settings.json` has `autoEnableFullAuto` off), and stops at the result screen, on F9, or on anything it cannot handle. The panel says why it stopped. Screens with no advice (choose a relic, crystal sphere) are handed to the player, and the autopilot resumes once they close. It reuses the game's own AutoSlay handlers but never calls `AutoSlayer.Start()`, which abandons the run, writes unlocks to the save and quits the game. `HandlerPatches` transpiles 9 handlers' `MoveNext` so that `NextItem` becomes `AutoPilotChoice.Pick` and the cheating `EventRoomHandler.HandleEventCombat` (+100 Strength, kills the enemies) becomes a wait. A game update that changes those call counts disables the autopilot (logged at load). It never touches game speed: use CombatSolver's 瞬间 setting or SpeedX. Executed steps are in `advisor_trace.jsonl` with `"autopilot": {"executed": true, "step": n}`; stops are `decision: autopilot_stop` lines. Spec `docs/superpowers/specs/2026-10-28-sts2-advisor-autopilot-design.md`, plan `docs/superpowers/plans/2026-10-28-sts2-advisor-autopilot.md`.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-10-28-sts2-advisor-autopilot-design.md CLAUDE.md
git commit -m "docs(advisor): autopilot spec follows the plan; CLAUDE.md section"
```

---

### Task 14: 验证

- [ ] **Step 1: Core 测试 + 构建 + 契约测试**

```bash
~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests 2>&1 | tail -2
~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | tail -2
.venv/bin/python -m pytest tests/test_advisor_trace_contract.py tests/test_advisor_trace_text_keys.py -q
```

Expected: 测试全过、`0 Error(s)`、pytest 全过。

- [ ] **Step 2: 回归（CLAUDE.md 要求）**

本改动不碰 headless 引擎、`src/Sts2Shared/` 和 Python。但 CLAUDE.md 规定任何代码改动都要跑全量回归。按用户约定，批次只在晚上（22:00 起）跑，先确认没有别的批次在跑：

```bash
pgrep -f play_full_run || .venv/bin/python python/run_batch.py --name regression --kind regression --games 5 --chars all
```

Expected: 收尾汇总里每条 lane 都是 `Completed: 5/5`。白天的话就记下"回归待今晚"，不要跳过。

- [ ] **Step 3: 安装到游戏**

```bash
pkill -f advisor_server.py; tools/install_advisor_mod.sh
```

Expected: 最后打印 `installed to …/mods/Sts2Advisor`。启动游戏后，`~/Library/Application Support/SlayTheSpire2/Sts2Advisor/Sts2Advisor.log` 里有 `autopilot: AutoSlay handlers patched`。如果是 `autopilot disabled, AutoSlay handlers not patched: …`，按其中的数字对照 Task 9 的 `Targets` 查原因。

- [ ] **Step 4: 实机验收（用户操作，spec 第 3 段）**

每一项做完，在 trace 和日志里核对：

1. **不开 SpeedX 打一局**：从 A1 第一个地图节点按 F9，打完一局（CombatSolver 全自动开着）。
   - 通过：打到结算页，面板显示"全托管已停止：本局结束"。
   - 每个局外决策在 `advisor_trace.jsonl` 里都有一行 `"autopilot": {"executed": true, …}`。
   - `grep "autopilot: stopped" Sts2Advisor.log` 只有一行，原因是 `GameOver`。
2. **开 SpeedX 再打一局**：通过标准同上。
3. **接管再恢复**：在地图、事件、商店各按一次 F9 接管、再按一次 F9 恢复。恢复后从当前画面接着走，不重复点已完成的步骤。
4. **交还玩家**：遇到一次选遗物或水晶球界面。面板显示"请手动完成此界面"，手动完成后自动继续。
5. **存档解锁不变**：开托管前后，主菜单的角色解锁和进阶状态一致（我们从不调 `AutoSlayer.Start`）。

已知风险，验收时重点看：
- 商店删牌：AutoSlay 从没走过这条路径。确认 `OnTryPurchaseWrapper` 弹出删牌选择、选中建议的牌后完成。
- 网格选牌：节点上的 `CardModel` 可能不是牌组里的同一个对象。日志里出现 "matched by card id, not by object" 属于正常降级。
- `NSimpleCardSelectScreen`：最少可选 0 张时，AutoSlay handler 会先点确认（日志 "advised picks … unused"）。出现了就记下来，以后再改。

失败时：日志里 `autopilot: stopped (原因: 细节)` 指明停在哪；按 CLAUDE.md 的 Bug Fix Workflow 记进 `agent/bug.md`。

- [ ] **Step 5: 验收通过后更新 fixture（可选）**

新跑出的 trace 可以替换 `tests/fixtures/advisor/real_game_trace.jsonl.gz`（CLAUDE.md 有命令），然后重跑 `tests/test_advisor_trace_contract.py`。

---

## 追加（2026-10-28，用户要求）：面板按钮 + 单步

Spec："修订 2：面板按钮与单步"。

单步 = 执行一个建议的选择，继续做不需要决策的事，**下一次要问建议时停**（`StopReason.StepDone`）。面板在建议下方加按钮「执行这一步 (F10)」「全自动 (F9)」；运行中换成一个「停止」。

### Task 15: Core —— `step_hotkey`、单步会话、按钮与文字

**Files:**
- Modify: `src/Sts2Advisor.Core/AdvisorConfig.cs`、`AutoPilotSession.cs`、`AutoPilotText.cs`
- Test: `src/Sts2Advisor.Core.Tests/AdvisorConfigTests.cs`、`AutoPilotSessionTests.cs`、`AutoPilotTextTests.cs`

- [ ] **Step 1: 写失败的测试**

`AdvisorConfigTests` 追加：

```csharp
    [Fact]
    public void StepHotkeyDefaultsToF10AndIsRead()
    {
        Assert.Equal("F10", AdvisorConfig.Defaults.StepHotkey);
        Assert.Equal("F10", AdvisorConfig.Load(Write("""{"repo_path": "/r"}""")).StepHotkey);
        Assert.Equal("F11", AdvisorConfig.Load(Write("""{"step_hotkey": "F11"}""")).StepHotkey);
        Assert.Equal("F10", AdvisorConfig.Load(Write("""{"step_hotkey": 11}""")).StepHotkey);
    }
```

`AutoPilotSessionTests` 追加：

```csharp
    [Fact]
    public void SingleStepIsDoneAfterOneStep()
    {
        var s = new AutoPilotSession();
        Assert.True(s.Start(singleStep: true));
        Assert.True(s.SingleStep);
        Assert.False(s.StepDone);
        s.Record("地图：问号", null);
        Assert.True(s.StepDone);
        s.Stop(StopReason.StepDone, null);
        Assert.True(s.Start());
        Assert.False(s.SingleStep);
        s.Record("x", null);
        Assert.False(s.StepDone);
    }
```

`AutoPilotTextTests`：

- 把现有用例里所有 `AutoPilotText.Running(s, "F9", ...)` 改成 `AutoPilotText.Running(s, "F9", "F10", ...)`。
- 把所有 `AutoPilotText.StopBanner(r, d, "F9", zh)` 改成 `AutoPilotText.StopBanner(r, d, "F9", "F10", zh)`。
- 再追加：

```csharp
    [Fact]
    public void SingleStepTitleNamesTheStepKey()
    {
        var s = new AutoPilotSession();
        s.Start(singleStep: true);
        Assert.Equal("单步执行中 · F10 停止", AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: true)[0].Text);
        Assert.Equal("Single step · F10 to stop", AutoPilotText.Running(s, "F9", "F10", CombatSolverStatus.Ok, zh: false)[0].Text);
    }

    [Fact]
    public void StepDoneBannerIsMutedAndOffersTheNextStep()
    {
        var b = AutoPilotText.StopBanner(StopReason.StepDone, null, "F9", "F10", zh: true);
        Assert.Equal(LineStyle.Muted, b.Style);
        Assert.Contains("F10", b.Text);
        Assert.DoesNotContain("F9", b.Text);
    }

    [Fact]
    public void ButtonsOfferStepAndFullAutoOrStop()
    {
        var idle = AutoPilotText.Buttons(running: false, "F10", "F9", zh: true);
        Assert.Equal(new[] { PanelAction.Step, PanelAction.Auto }, idle.Select(b => b.Action));
        Assert.Equal("执行这一步 (F10)", idle[0].Text);
        Assert.Equal("全自动 (F9)", idle[1].Text);
        var running = AutoPilotText.Buttons(running: true, "F10", "F9", zh: false);
        Assert.Equal(PanelAction.Stop, Assert.Single(running).Action);
        Assert.Equal("Stop", running[0].Text);
    }
```

- [ ] **Step 2: 跑测试确认失败**

Run: `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests`
Expected: 编译失败（`StepHotkey`、`singleStep`、`PanelAction` 不存在）

- [ ] **Step 3: 实现**

`AdvisorConfig.cs`：

- record 头末尾加参数 `string StepHotkey = AdvisorConfig.DefaultStepHotkey`；
- 加常量 `public const string DefaultStepHotkey = "F10";`；
- `Load` 末尾参数加 `Str(root, "step_hotkey") ?? DefaultStepHotkey`；
- summary 里补一句 `"step_hotkey" likewise (else "F10")`。

`AutoPilotSession.cs`：

- `StopReason` 枚举**末尾**加 `StepDone`。enum summary 补一句 `StepDone: a single step executed its one advised choice and reached the next decision.`
- 加属性：

```csharp
    /// <summary>Started as a single step: stop at the next decision once one advised choice is executed.</summary>
    public bool SingleStep { get; private set; }

    /// <summary>A single step has executed its choice; the next request for advice ends it.</summary>
    public bool StepDone => SingleStep && StepCount > 0;
```

- `Start()` 改为 `public bool Start(bool singleStep = false)`，在 `Mode = AutoPilotMode.Running;` 后加 `SingleStep = singleStep;`。

`AutoPilotText.cs`：

- 文件顶部（类外）加：

```csharp
public enum PanelAction { Step, Auto, Stop }

public sealed record PanelButton(PanelAction Action, string Text);
```

- `Running` 签名改为 `Running(AutoPilotSession s, string autoKey, string stepKey, CombatSolverStatus combat, bool zh)`，标题行改为：

```csharp
            s.SingleStep
                ? new(T(zh, $"单步执行中 · {stepKey} 停止", $"Single step · {stepKey} to stop"), LineStyle.Title)
                : new(T(zh, $"全托管中 · {autoKey} 接管", $"Autopilot on · {autoKey} to take over"), LineStyle.Title),
```

- `StopBanner` 签名改为 `StopBanner(StopReason reason, string? detail, string autoKey, string stepKey, bool zh)`。末尾提示改为：

```csharp
            + (reason == StopReason.StepDone
                ? T(zh, $" · {stepKey} 下一步", $" · {stepKey} for the next step")
                : T(zh, $" · {autoKey} 重新开启", $" · {autoKey} to restart"));
```

  Muted 条件加上 `StopReason.StepDone`。

- `StopReasonText` 加 `StopReason.StepDone => T(zh, "单步完成", "step done"),`。
- 加：

```csharp
    /// <summary>The panel's buttons: "do this step" and "full auto" while advising; one "stop" while running.</summary>
    public static IReadOnlyList<PanelButton> Buttons(bool running, string stepKey, string autoKey, bool zh) =>
        running
            ? new[] { new PanelButton(PanelAction.Stop, T(zh, "停止", "Stop")) }
            : new[]
            {
                new PanelButton(PanelAction.Step, T(zh, $"执行这一步 ({stepKey})", $"Do this step ({stepKey})")),
                new PanelButton(PanelAction.Auto, T(zh, $"全自动 ({autoKey})", $"Full auto ({autoKey})")),
            };
```

- [ ] **Step 4: 跑测试确认通过**（全部 Core 测试）。mod 会因签名变化编译失败，Task 16 修。

- [ ] **Step 5: Commit** `feat(advisor): single-step session, step_hotkey and panel buttons (core)`

### Task 16: mod —— 单步执行与面板按钮

**Files:**
- Modify: `src/Sts2Advisor/AutoPilot/AutoPilotControl.cs`、`AutoPilotLoop.cs`、`src/Sts2Advisor/AdvisorPanel.cs`、`src/Sts2Advisor/AdvisorRuntime.cs`

- [ ] **Step 1: 循环在单步完成后停**

`AutoPilotLoop.cs`：

- `FetchAdviceAsync` 开头（`string? last = null;` 之前）加：

```csharp
        if (_session.StepDone)  // a single step stops at the next decision
            throw new AutoPilotStopException(StopReason.StepDone, null);
```

- `HandleOverlayAsync` 里非模态的交还分支（没有 handler 的 overlay）在交还之前加同样两行：选遗物、水晶球也是决策。模态弹窗的交还不加。

- [ ] **Step 2: 控制器支持单步**

`AutoPilotControl.cs`：

- 加字段 `private static bool _pendingSingle;`。
- `Toggle()` 改为 `Toggle(bool singleStep = false)`：
  - 运行中：照旧 `Stop(StopReason.Hotkey, null)`。
  - 旧 run 还在收尾：`_startPending = !_startPending; _pendingSingle = singleStep;`。
  - 否则：`Start(singleStep)`。
- `Start(bool previousRunDone = false)` 改为 `Start(bool singleStep, bool previousRunDone = false)`：
  - `Session.Start()` 改成 `Session.Start(singleStep)`；
  - 日志写 `started ({(singleStep ? "single step" : "full auto")}, CombatSolver …)`。
- `RunAsync` 的 finally 里改为 `Start(_pendingSingle, previousRunDone: true)`。
- 加：

```csharp
    /// <summary>A panel button: Step / Auto start that mode; Stop stops whichever runs.</summary>
    public static void Press(PanelAction action)
    {
        if (action == PanelAction.Stop) Stop(StopReason.Hotkey, null);
        else Toggle(singleStep: action == PanelAction.Step);
    }
```

- `RunningLines(string hotkey, bool zh)` 改为 `RunningLines(string autoKey, string stepKey, bool zh) => AutoPilotText.Running(Session, autoKey, stepKey, _combatSolver, zh);`
- `Banner(string hotkey, bool zh)` 改为 `Banner(string autoKey, string stepKey, bool zh)`，转发给新的 `StopBanner`。

- [ ] **Step 3: 面板加按钮**

`AdvisorPanel.cs`：

- 类 summary 改为：`... Ignores the mouse except on its buttons: it never blocks a click elsewhere.`
- `TryCreate` 里，把 `panel.AddChild(text);` 改为放一个 `VBoxContainer`：

```csharp
        var column = new VBoxContainer { Name = "Column", MouseFilter = Control.MouseFilterEnum.Ignore };
        column.AddThemeConstantOverride("separation", 8);
        var buttons = new HBoxContainer { Name = "Buttons", MouseFilter = Control.MouseFilterEnum.Ignore };
        buttons.AddThemeConstantOverride("separation", 8);
        column.AddChild(text);
        column.AddChild(buttons);
        panel.AddChild(column);
```

  构造函数多存一个 `HBoxContainer _buttons`。

- 加字段 `private string? _shownButtons;`。
- `Show` 签名改为 `Show(IReadOnlyList<PanelLine> lines, string? language, IReadOnlyList<PanelButton> buttons, Action<PanelAction> onPress)`。在设置文字之后加：

```csharp
        // Rebuilt only when the buttons change (every 0.3 s poll re-shows the same ones).
        string key = string.Join("|", buttons.Select(b => $"{b.Action}:{b.Text}")) + "|" + _fontLanguage;
        if (key != _shownButtons)
        {
            foreach (var child in _buttons.GetChildren())
                child.QueueFree();
            foreach (var b in buttons)
            {
                var action = b.Action;
                // FocusMode None: a click must not move keyboard/controller focus away from the game's own UI.
                var button = new Button { Text = b.Text, FocusMode = Control.FocusModeEnum.None,
                                          MouseFilter = Control.MouseFilterEnum.Stop };
                button.AddThemeFontSizeOverride("font_size", 14);
                button.ApplyLocaleFontSubstitution(FontType.Regular, "font");
                button.Pressed += () => onPress(action);
                _buttons.AddChild(button);
            }
            _shownButtons = key;
        }
        _buttons.Visible = buttons.Count > 0;
```

  `ApplyLocaleFontSubstitution` 和 `RichTextLabel` 上用的是同一个扩展方法。确认它接受 `Control` 和主题字体名 `"font"`（在反编译里查它的定义）；不接受的话就照它对 `Button` 的正确写法改。

- [ ] **Step 4: AdvisorRuntime 接线**

1. 字段：`private static Key _stepKey = Key.F10; private static string _stepKeyName = "F10"; private static bool _stepKeyDown;`。
2. `Start`：在解析 `_autoKey` 后加：

```csharp
        if (!Enum.TryParse(config.StepHotkey, ignoreCase: true, out _stepKey))
            _stepKey = Key.F10;
        _stepKeyName = _stepKey.ToString();
        if (_stepKey == _hotkey || _stepKey == _autoKey)
            autopilotProblem ??= $"step_hotkey {_stepKeyName} clashes with hotkey/autopilot_hotkey ({configPath})";
```

   `ModEntry` 的 loaded 日志行加上 `step_hotkey={config.StepHotkey}`。
3. `PollHotkey` 末尾：步进键同样做边沿检测。和 `_hotkey` 相同时不轮询（面板开关优先）；和 `_autoKey` 相同时也不轮询，避免一下按两次。按下时 `AutoPilotControl.Toggle(singleStep: true); _requested = null;`。
4. `RunningLines(_autoKeyName, Zh)` 改为 `RunningLines(_autoKeyName, _stepKeyName, Zh)`。
5. `Show`：

```csharp
    private static void Show(IReadOnlyList<PanelLine> lines)
    {
        if (!_visible) return;
        var banner = AutoPilotControl.Banner(_autoKeyName, _stepKeyName, Zh);
        // No buttons while the mod cannot act (config missing, game version unsupported).
        var buttons = _configProblem != null || _disabled
            ? Array.Empty<PanelButton>()
            : AutoPilotText.Buttons(AutoPilotControl.IsRunning, _stepKeyName, _autoKeyName, Zh);
        _panel?.Show(banner == null ? lines : lines.Prepend(banner).ToList(), _language, buttons, OnPanelButton);
    }

    private static void OnPanelButton(PanelAction action)
    {
        try
        {
            AutoPilotControl.Press(action);
            _requested = null;
        }
        catch (Exception ex)
        {
            LogTickFailure(ex);  // a Godot signal callback must not throw
        }
    }
```

6. 类 summary 补一句：`F10 (step_hotkey) or the panel's "do this step" button runs a single step: one advised choice, then stop at the next decision.`

- [ ] **Step 5: 编译 + Core 测试**

Run: `~/.dotnet-arm64/dotnet build src/Sts2Advisor/Sts2Advisor.csproj -c Release 2>&1 | grep -E "warn|Error\(s\)"` 和 `~/.dotnet-arm64/dotnet test src/Sts2Advisor.Core.Tests 2>&1 | tail -1`
Expected: `0 Error(s)`，Core 测试全过。

- [ ] **Step 6: Commit** `feat(advisor-mod): panel buttons and single step (F10)`

### Task 17: 文档

- `tools/install_advisor_mod.sh`：config heredoc 加 `"step_hotkey": "F10"`。
- `CLAUDE.md` 的 Autopilot 条目：
  - 开头改为 `F9 (config autopilot_hotkey) or the panel's 全自动 button hands the rest of the run to ...; F10 (step_hotkey) or 执行这一步 runs a single step -- one advised choice plus whatever needs no decision, stopping at the next decision (StopReason.StepDone). Any two equal hotkeys disable the autopilot.`
  - 补一句 `The panel ignores the mouse except on its buttons, which never take keyboard focus.`
- Commit `docs(advisor): single step and panel buttons`。

### Task 18: 验证

同 Task 14 Step 1；实机验收追加三项：

1. 在地图、事件、商店、卡牌奖励各按一次「执行这一步」，确认只做了一个选择、停在下一个决策，横幅显示"单步完成 · F10 下一步"。
2. 按钮点击不会穿透到游戏，也不抢键盘/手柄焦点。
3. F8 隐藏面板时，按钮一起消失。
