# sts2-cli

<details open>
<summary><b>English</b></summary>

A CLI for Slay the Spire 2.

Runs the real game engine headless in your terminal — all damage, card effects, enemy AI, relics, and RNG are identical to the actual game. Everything is unlocked from the start: all characters, cards, relics, potions, and ascension levels — no timeline progression required.

![demo](docs/demo_en.gif)

## Setup

Requirements:
- [Slay the Spire 2](https://store.steampowered.com/app/2868840/Slay_the_Spire_2/) on Steam
- [.NET 9+ SDK](https://dotnet.microsoft.com/download)
- Python 3.9+

```bash
git clone https://github.com/wuhao21/sts2-cli.git
cd sts2-cli
./setup.sh      # copies DLLs from Steam → IL patches → builds
```

Or just run `python3 python/play.py` — it auto-detects and sets up on first run.

## Play

```bash
python3 python/play.py                        # interactive (Chinese)
python3 python/play.py --lang en              # interactive (English)
python3 python/play.py --ascension 10         # Ascension 10
python3 python/play.py --character Silent      # play as Silent
```

Type `help` in-game:

```
  help     — show help
  map      — show map
  deck     — show deck
  potions  — show potions
  relics   — show relics
  quit     — quit

  Map:     enter path number (0, 1, 2)
  Combat:  card index / e (end turn) / p0 (use potion)
  Reward:  card index / s (skip)
  Rest:    option index
  Event:   option index / leave
  Shop:    c0 (card) / r0 (relic) / p0 (potion) / rm (remove) / leave
```

## JSON Protocol

For programmatic control (AI agents, RL, etc.), communicate via stdin/stdout JSON:

```bash
dotnet run --project src/Sts2Headless/Sts2Headless.csproj
```

```json
{"cmd": "start_run", "character": "Ironclad", "seed": "test", "ascension": 0}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "action", "action": "end_turn"}
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}
{"cmd": "action", "action": "skip_card_reward"}
{"cmd": "quit"}
```

Each command returns a JSON decision point (`map_select` / `combat_play` / `card_reward` / `rest_site` / `event_choice` / `shop` / `game_over`). All names are in English.

## Game Logs

Every run is automatically logged to `logs/` as a JSONL file (one JSON per line), recording each game state and action with timestamps. Logs older than 7 days are cleaned up automatically.

```bash
python3 python/play.py --no-log    # disable logging
```

**When filing a bug report, please attach the relevant log file from `logs/`** — it contains the full step-by-step game state needed to reproduce the issue.

## Supported Characters

| Character | Status |
|---|---|
| Ironclad | Fully playable |
| Silent | Fully playable |
| Defect | Fully playable |
| Necrobinder | Fully playable |
| Regent | Fully playable |

## Architecture

```
Your code (Python / JS / LLM)
    │  JSON stdin/stdout
    ▼
src/Sts2Headless (C#)
    │  RunSimulator.cs
    ▼
sts2.dll (game engine, IL patched)
  + src/GodotStubs (replaces GodotSharp.dll)
  + Harmony patches (localization)
```

## RL Training

Train a combat agent with MaskablePPO (requires `requirements-agent.txt`):

```bash
# Single training run
python3 agent/train.py --character Ironclad --steps 100000

# Autonomous train-eval loop (runs unattended for hours)
python3 agent/train_loop.py --character Ironclad --n-eval-games 15

# Parallel evaluation (4x faster eval phase)
python3 agent/train_loop.py --character Ironclad --n-eval-games 15 --n-eval-workers 4

# Custom milestones
python3 agent/train_loop.py --milestones 10000,25000,50000,100000

# Auto-resumes from latest checkpoint after interruption
python3 agent/train_loop.py --character Ironclad
```

The loop trains to each milestone, saves a checkpoint, evaluates with full game runs (RL combat + heuristic strategy), and appends results to `training_log.jsonl`.

### Evaluate a Trained Model

```bash
# Run 30 games with verbose output
python3 agent/coordinator.py --character Ironclad \
    --checkpoint checkpoints/ppo_ironclad_100k.zip \
    --n-games 30 --verbose

# Quick results summary
python3 -c "
import json, sys
for line in open('training_log.jsonl'):
    r = json.loads(line)
    e = r['eval']
    t = r.get('train_metrics', {})
    print(f\"{r['milestone']//1000}k steps | win={e['win_rate']:.0%} | avg_floor={e['avg_floor']:.1f}\")
"
```

### Map Strategy

Non-combat decisions (map routing, card rewards, shops) use a swappable strategy:

```python
from agent.combat_env import set_map_strategy
from agent.strategy import Act1SafeStrategy

# Default: avoid fights, prefer rest sites, shop when rich
set_map_strategy(Act1SafeStrategy())

# Swap to a custom or LLM-based strategy
set_map_strategy(MyCustomStrategy())
```

</details>

<details>
<summary><b>中文</b></summary>

杀戮尖塔2的命令行版本。

在终端里运行真实游戏引擎 — 所有伤害计算、卡牌效果、敌人AI、遗物触发、随机数都和真实游戏一致。所有内容从一开始就全部解锁：全角色、全卡牌、全遗物、全药水、全渐进难度等级，无需时间线进度。

![demo](docs/demo_zh.gif)

## 安装

需要：
- [Slay the Spire 2](https://store.steampowered.com/app/2868840/Slay_the_Spire_2/) (Steam)
- [.NET 9+ SDK](https://dotnet.microsoft.com/download)
- Python 3.9+

```bash
git clone https://github.com/wuhao21/sts2-cli.git
cd sts2-cli
./setup.sh      # 从 Steam 复制 DLL → IL patch → 编译
```

或者直接运行 `python3 python/play.py`，首次会自动完成 setup。

## 玩

```bash
python3 python/play.py                        # 中文交互模式
python3 python/play.py --lang en              # English
python3 python/play.py --ascension 10         # 渐进难度 10
python3 python/play.py --character Silent      # 选择静默猎手
```

游戏内输入 `help` 查看所有命令：

```
  help     — 帮助
  map      — 显示地图
  deck     — 查看牌组
  potions  — 查看药水
  relics   — 查看遗物
  quit     — 退出

  地图:    输入编号 (0, 1, 2)
  战斗:    输入卡牌编号 / e 结束回合 / p0 使用药水
  奖励:    输入卡牌编号 / s 跳过
  休息:    输入选项编号
  事件:    输入选项编号 / leave 离开
  商店:    c0 买卡 / r0 买遗物 / p0 买药水 / rm 移除 / leave 离开
```

## 角色支持

| 角色 | 状态 |
|---|---|
| 铁甲战士 (Ironclad) | 完全可玩 |
| 静默猎手 (Silent) | 完全可玩 |
| 故障机器人 (Defect) | 完全可玩 |
| 亡灵契约师 (Necrobinder) | 完全可玩 |
| 储君 (Regent) | 完全可玩 |

## JSON 协议

除了交互模式，也可以通过 stdin/stdout JSON 协议编程控制（写 AI agent、RL 训练等）：

```bash
dotnet run --project src/Sts2Headless/Sts2Headless.csproj
```

```json
{"cmd": "start_run", "character": "Ironclad", "seed": "test", "ascension": 0}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "action", "action": "end_turn"}
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}
{"cmd": "action", "action": "skip_card_reward"}
{"cmd": "quit"}
```

每个命令返回一个 JSON decision point（`map_select` / `combat_play` / `card_reward` / `rest_site` / `event_choice` / `shop` / `game_over`），所有名称为英文。

## 游戏日志

每局游戏会自动记录到 `logs/` 目录下的 JSONL 文件中，包含每一步的游戏状态和操作，附带时间戳。超过 7 天的旧日志会自动清理。

```bash
python3 python/play.py --no-log    # 关闭日志
```

**提交 bug 报告时，请附上 `logs/` 中对应的日志文件** — 它包含了复现问题所需的完整游戏步骤。

## 架构

```
你的代码 (Python / JS / LLM)
    │  JSON stdin/stdout
    ▼
src/Sts2Headless (C#)
    │  RunSimulator.cs
    ▼
sts2.dll (游戏引擎, IL patched)
  + src/GodotStubs (替代 GodotSharp.dll)
  + Harmony patches (本地化)
```

## RL 训练

使用 MaskablePPO 训练战斗 AI（需要 `requirements-agent.txt`）：

```bash
# 单次训练
python3 agent/train.py --character Ironclad --steps 100000

# 自动训练-评估循环（无人值守，运行数小时）
python3 agent/train_loop.py --character Ironclad --n-eval-games 15

# 并行评估（4 线程加速评估阶段）
python3 agent/train_loop.py --character Ironclad --n-eval-games 15 --n-eval-workers 4

# 自定义里程碑
python3 agent/train_loop.py --milestones 10000,25000,50000,100000

# 中断后自动从最新 checkpoint 恢复
python3 agent/train_loop.py --character Ironclad
```

训练循环会在每个里程碑保存 checkpoint，运行完整游戏评估（RL 战斗 + 策略决策），并将结果追加到 `training_log.jsonl`。每 10k 步自动存盘，崩溃不会丢失进度。

### 评估训练好的模型

```bash
# 运行 30 局并显示详细输出
python3 agent/coordinator.py --character Ironclad \
    --checkpoint checkpoints/ppo_ironclad_100k.zip \
    --n-games 30 --verbose

# 快速查看训练日志摘要
python3 -c "
import json
for line in open('training_log.jsonl'):
    r = json.loads(line)
    e = r['eval']
    print(f\"{r['milestone']//1000}k 步 | 胜率={e['win_rate']:.0%} | 平均层数={e['avg_floor']:.1f}\")
"
```

### 地图策略

非战斗决策（地图路线、卡牌奖励、商店）使用可替换的策略：

```python
from agent.combat_env import set_map_strategy
from agent.strategy import Act1SafeStrategy

# 默认：避开战斗，优先火堆，有钱去商店
set_map_strategy(Act1SafeStrategy())

# 替换为自定义或 LLM 策略
set_map_strategy(MyCustomStrategy())
```

</details>
