# Hybrid LLM + RL Agent for STS2

RL 处理战斗出牌，Claude API 处理战略决策（路线、卡牌奖励、休息、事件、商店）。

## 架构

```
GameCoordinator
├── combat_play  →  RLAgent (MaskablePPO)
└── 其他决策     →  LLMAgent (Claude API) / greedy fallback
```

| 模块 | 作用 |
|------|------|
| `state_encoder.py` | 游戏 JSON → 130 维向量 + 41 维 action mask |
| `combat_env.py` | gymnasium.Env 包装器，直接启动游戏子进程 |
| `rl_agent.py` | 加载训练好的 PPO checkpoint 做推理 |
| `train.py` | 4 并行环境训练 MaskablePPO |
| `llm_agent.py` | Claude API 做卡牌/路线/事件等战略决策 |
| `coordinator.py` | 主循环：路由决策到 RL 或 LLM |

---

## 快速开始

### 前置条件

两个平台都需要：

1. **Slay the Spire 2** 已安装（Steam）
2. **[.NET 9+ SDK](https://dotnet.microsoft.com/download)**
3. **Python 3.10–3.12**（3.13+ 暂不支持 PyTorch）
4. **sts2-cli 已 setup**（游戏引擎已编译）：
   ```bash
   git clone https://github.com/wuhao21/sts2-cli.git
   cd sts2-cli
   ./setup.sh          # macOS/Linux
   # Windows: python python/play.py 首次运行会自动 setup
   ```

### macOS (Apple Silicon)

```bash
# 1. 创建 Python 3.12 虚拟环境
python3.12 -m venv .venv312
source .venv312/bin/activate

# 2. 安装依赖
pip install -r requirements-agent.txt

# 3. 验证安装
python -c "import torch; print('MPS:', torch.backends.mps.is_available())"
python -m pytest tests/agent/ -v
```

> PyTorch 在 Apple Silicon 上自动使用 MPS 加速。训练时会自动检测。

### macOS (Intel) / Linux

```bash
python3.12 -m venv .venv312
source .venv312/bin/activate
pip install -r requirements-agent.txt

# 验证（Intel Mac / Linux 用 CPU）
python -c "import torch; print('CUDA:', torch.cuda.is_available())"
python -m pytest tests/agent/ -v
```

### Windows

```powershell
# 1. 安装 Python 3.12（从 python.org 下载）
# 2. 创建虚拟环境
python -m venv .venv312
.venv312\Scripts\activate

# 3. 安装依赖
pip install -r requirements-agent.txt

# 4. 如果有 NVIDIA GPU，安装 CUDA 版 PyTorch（可选，大幅加速训练）
# 先卸载 CPU 版，再装 CUDA 版：
pip uninstall torch -y
pip install torch --index-url https://download.pytorch.org/whl/cu121

# 5. 验证
python -c "import torch; print('CUDA:', torch.cuda.is_available())"
python -m pytest tests/agent/ -v
```

> Windows 上 .NET SDK 路径不同。如果 `dotnet` 不在 PATH 中，需要修改 `agent/coordinator.py` 和 `agent/combat_env.py` 中的 `DOTNET` 变量。

### .NET 路径配置

代码中默认 `DOTNET = ~/.dotnet-arm64/dotnet`（macOS ARM64）。其他平台需要调整：

| 平台 | 典型路径 |
|------|----------|
| macOS ARM64 | `~/.dotnet-arm64/dotnet` |
| macOS Intel | `/usr/local/share/dotnet/dotnet` |
| Linux | `/usr/share/dotnet/dotnet` 或 `~/.dotnet/dotnet` |
| Windows | `dotnet`（已在 PATH 中） |

修改 `combat_env.py` 和 `coordinator.py` 开头的 `DOTNET` 常量即可。

---

## 训练

### Step 1: 运行随机 baseline（可选）

先看看随机 agent 能打到什么程度：

```bash
python python/play_full_run.py 5 Ironclad
```

### Step 2: RL 训练

```bash
# 100k steps，4 并行环境，约 1-2 小时（Apple Silicon）
python agent/train.py --character Ironclad --steps 100000 --n-envs 4

# 指定 ascension 等级
python agent/train.py --character Ironclad --steps 100000 --ascension 1

# 从 checkpoint 继续训练
python agent/train.py --character Ironclad --steps 200000 \
    --checkpoint checkpoints/ppo_ironclad_100k.zip
```

训练过程每 25k steps 自动保存到 `checkpoints/` 目录。

TensorBoard 日志在 `checkpoints/tb_logs/`：
```bash
pip install tensorboard
tensorboard --logdir checkpoints/tb_logs/
```

### Step 3: 评估 RL（无 LLM）

```bash
python agent/coordinator.py --character Ironclad --mode eval-rl --n-games 20
```

### Step 4: 评估 Hybrid（RL + LLM）

```bash
export ANTHROPIC_API_KEY=sk-ant-xxx   # Windows: set ANTHROPIC_API_KEY=sk-ant-xxx
python agent/coordinator.py --character Ironclad --mode eval-full --n-games 20
```

对比 eval-rl 和 eval-full 的胜率，衡量 LLM 战略层的收益。

---

## 支持的角色

`--character` 参数支持：`Ironclad`, `Silent`, `Defect`, `Watcher`, `Regent`

---

## 常见问题

### PyTorch 安装失败
- 确认 Python 版本是 3.10–3.12，3.13+ 没有 PyTorch wheels
- Windows 如需 GPU 加速，参考上面 CUDA 版安装命令

### 训练时 EOF / 进程崩溃
- 正常现象，`CombatEnv` 会自动重启子进程，崩溃的 episode 计为失败（reward = -1.0）

### `dotnet` 找不到
- 确认 .NET 9 SDK 已安装：`dotnet --version`
- 修改代码中的 `DOTNET` 路径常量

### MPS 相关错误（Apple Silicon）
- 更新 PyTorch 到最新版：`pip install -U torch`
- 如果持续报错，设置环境变量 `PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0` 重试

### Windows 上路径问题
- 使用正斜杠或原始字符串，确保路径中无中文字符
- 如果 setup.sh 无法运行，先用 `python python/play.py` 自动 setup
