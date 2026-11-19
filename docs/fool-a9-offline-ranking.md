# 愚者 A9：CLI 分叉采数与离线排序

本工具只在隔离的 Headless 进程运行。已运行的远征、建议服务和线上选牌权重不使用训练产物。当前兼容目标是 Steam **v0.113.1**；旧 v0.111 的 `lib/sts2.dll` 不包含愚者，需要在独立 checkout 内用 `setup.sh` 刷新依赖。不要在运行远征的 checkout 里更新 HEAD、替换 DLL 或安装建议插件。

## 引擎与验证

- 使用真实游戏 DLL 和 Godot stubs，CLI 支持 `Fool` / A9；初始牌组含进阶诅咒。
- Solver 更新到上游 0.51.5，固定提交 `3677d4cac2407e332f1419741878e635c1ce2ee3`，覆盖箭矢、瞄准、旋律、塔罗；来源和本地兼容说明见 `src/Sts2Headless/CombatSolverEngine/VENDORED.md`。
- CLI 显式返回箭矢可用数、槽位/状态及敌方扎穿箭和剩余拔箭命中次数。测试确认初始愚蠢射击为 15+6=21 伤害，射后箭离手。
- CLI `user://` 使用独立进程临时目录，原始 Steam DLL、真实远征存档和配置只读。
- `STS2_COLLECTION_BUDGET_MS=3500` 为离线采数预算，支持 1000–30000 毫秒；常规 Solver 档位默认不变。结果和批次记录实际预算，不能把它解释成线上长预算的胜率。
- BUG-057 修复了事件选择返回过早：等待所属异步事件完成，或等到真正的新选择提示，才返回观察。旧试点 12 个分叉有 1 个恢复不一致；旧数据仅保留诊断，新数据必须使用修复后的引擎。

## 同状态证明

`tools/collect_fool_a9.py` 保存完整命令前缀和每一步原始 JSON，而不是靠重建牌组/血量来伪造恢复。

1. 在发现选牌点的原进程冻结完整观察，记录 SHA256。
2. 原进程按当时启发式选择控制选项，沿固定命令前进到下一场战斗，记录原始完整战斗观察、初始手牌、敌人身份和哈希。
3. 三个新进程重放完整前缀，逐步校验 JSON，再按相同控制选项/固定延续，与原进程未来观察精确比对。
4. 每个分叉重新启动 CLI，验证恢复后才选择一个选项；确认拿牌确实入牌组，跳过确实保持牌组。所有选项走相同延续、同一下一战。
5. 战斗只执行可用的 Solver 计划。未解决的战斗选牌、技术终局、超时、状态漂移或拒绝动作都记为技术错误，不能充当失败标签。
6. 每条前缀和分叉命令记录 `origin=solver/ooc` 及前后阶段；导出时从头重算审计。Toolbox 等开场选择、未解决的战斗奖励/卡包提示不会被当成场外选择。Monster/Elite/Boss 房间只有经 Solver 明确结束战斗后，才允许领奖及 Kifuda 等拿牌后选择；旅行和事件选择会清除这一证明。

指纹包含游戏版本、原始/补丁后游戏 DLL、CLI DLL、stubs 和本地化。完成结果不被续跑覆盖；重试的旧错误及 trace 会归档。

## 命令

在隔离 checkout 中执行，且不要与该 checkout 的回归批次并行：

```bash
.venv/bin/python tools/collect_fool_a9.py discover \
  --output logs/experiments/fool-a9-offline/final-v2 --cases 24 --per-seed 4
.venv/bin/python tools/collect_fool_a9.py verify \
  --output logs/experiments/fool-a9-offline/final-v2
.venv/bin/python tools/collect_fool_a9.py collect \
  --output logs/experiments/fool-a9-offline/final-v2
.venv/bin/python -m agent.offline_fool_ranker \
  logs/experiments/fool-a9-offline/final-v2/dataset.jsonl \
  --out logs/experiments/fool-a9-offline/ranker-v3
```

默认至多尝试 12 个种子来取得 24 个决策，覆盖至少 6 个种子；每个选项（包括跳过）3 次，1 个 Solver worker。`--case` 可选案例，`--option` 可选选项索引，`--retry-errors` 只重试未完成错误。调整预算时应对该案例所有选项同等重跑，例如 `--budget-ms 12000`；读取器只用最高完整公共预算，不混合不同选项的预算。

## 模型与指标边界

- 小型 L2 正则线性配对排序模型；特征版本 3，共 194 个数值特征。
- 输入是决策前事实：费用、真实/条件抽牌、悲伤、拉伸、瞄准、箭容量、持续效果、触发条件、牌组支持、血量/遗物/阶段。手写基础分和折扣系数不参与特征；卡 ID、种子、选项位置、未来结果不作为模型特征。
- 对旋律额外段数、每旋律格挡/降费和哇啾叽牌数量增伤，显式使用机制量与已有支持数量的乘积；这是潜在支持量，不是假定当前战斗已经获得所有旋律。
- 标签只在同一状态内比较，存活优先，其次比较多次结果中分离的剩余血量区间。技术错误、未验证恢复、缺少命令来源审计、不同下一战、重复编号、预算不公平以及不稳定生死结果均排除。
- 按种子整组划分训练/验证/测试，标准化只用训练集，正则强度只用验证集。至少 24 个合格案例、6 个种子，且每个划分都有可区分选项，否则只生成数据不足报告，不生成模型。
- 报告包含稳定性、覆盖/特征碰撞、留出集最优选项命中、配对准确率以及相对启发式/随机/始终跳过的存活与 HP 遗憾。
- 初始标签 horizon 是 **下一场战斗**。以上指标不等于整局胜率、Boss 通过率或长期构筑价值；即使留出指标改善，也不自动接入线上。

模型和完整数据只保存在 `logs/experiments/fool-a9-offline/`，没有线上加载入口。
