# Morgan

基于 **Binance Futures Testnet** 的加密货币自动交易与监控系统（OpenClaw 工作区）。  
规则评分开仓 + DeepSeek 辅助过滤，飞书实时推送；正在从「规则驱动」向「数据驱动」演进。

> 当前阶段：测试网验证稳定性与期望值，**不上深度学习**。仓位基准 100U × 20% × 10x。

---

## 核心能力

| 模块 | 说明 |
| :--- | :--- |
| **信号引擎** | 7 币（BTC/ETH/SOL/BNB/DOT/LINK/XRP），15s 主循环；突破/量能/ATR/BTC 方向/情绪评分 |
| **市场状态门槛** | `trending≥70` / `ranging≥80` / `volatile≥90`（震荡市已抬高门槛） |
| **AI 角色** | DeepSeek `deepseek-flash`：开仓否决闸门 + 同向/反向加减分（`ai_score`）；**非整仓决策器** |
| **执行** | 逐仓 10x、最多 4 仓；交易所 Algo TP/SL + 本地软件止盈止损/移动止盈；48h 超时退出 |
| **归因存储** | **SQLite** `trade_data.db`（WAL）：开仓特征 / 平仓 / AI 扫描 / 否决反事实 |
| **运维** | `kj` 一键启动、看门狗、健康检查、Clash 代理、飞书卡片、周报自动推送 |

---

## 架构（分层）

```
crypto_signal_monitor.py   # 调度 + 装配（main）
├── data_layer.py          # 价格流 / 指标 / regime / 情绪
├── strategy_layer.py      # calc_score / calc_tp_sl / ai_score_adjust
├── risk_layer.py          # 熔断 / 冷却 / 新闻过滤
├── ai_layer.py            # DeepSeek 预测与方向归一化
├── execution_layer.py     # 开平仓 / 移动止损 / Algo / 外部平仓回填
├── notify_layer.py        # 飞书推送 / 整点汇报
├── position_store.py      # 持仓 JSON 持久化
└── trade_features.py      # 归因写入出口 → trade_db.py (SQLite)
```

依赖方向：`main → layers → binance_auto_trade / config / feishu_helper`。

---

## 归因库（Phase 2.6，已上线）

事实源：`workspace/scripts/trade_data.db`（不进 git；运行时生成）

| 表 / 视图 | 用途 |
| :--- | :--- |
| `trades` | 开仓特征 + AI 观点快照 |
| `trade_closes` | 平仓结果（主平仓唯一索引） |
| `ai_scans` | 整点 7 币 AI 扫描 |
| `vetoes` / `veto_outcomes` | 被 AI 拦下的信号 + 反事实价格 |
| `v_trade_attribution` | 开平仓配对视图 |

常用工具：

```bash
cd workspace/scripts
python3 query_db.py              # 概览
python3 query_db.py summary      # 按 regime / AI / 平仓原因
python3 analyze_features.py      # 特征归因
python3 analyze_closes.py        # 平仓盈亏比诊断
python3 backfill_orphan_closes.py --dry-run   # 历史漏记平仓补记
```

**近期修复（2026-09-24）**：交易所 Algo 先成交时，API 同步会调用 `reconcile_vanished_positions` 补写平仓；重建持仓缺 `trade_id` 时从归因库回填，避免超时退出失效与配对率塌陷。

---

## 量化演进（摘要）

| 阶段 | 状态 | 要点 |
| :--- | :--- | :--- |
| Phase 0 / 0.5 | ✅ | confidence 归一化、分层解耦、止损放宽、震荡门槛 |
| Phase 1 | ✅ 工具链 | 特征记录 + 分析脚本（样本向 200+ 积累中） |
| Phase 2 | ⏳ | AI 加减分已接入，用数据验预测力（不盲目回滚） |
| Phase 2.5 / 2.6 | ✅ | 记录链路修复 + **JSONL → SQLite** |
| Phase 3 | ⛔ | 样本充足后再用统计模型学权重 |
| Phase 4 | ⛔ | 深度学习对 100U 测试网属过度工程 |

原则：每阶段独立验收、单点可回滚、先修换手与数据质量，再谈离场优化与模型。

---

## 配置与密钥

| 文件 | 说明 |
| :--- | :--- |
| `workspace/scripts/config.json` | 交易参数（币种、门槛、timeout、ai_score…） |
| `workspace/scripts/secrets.json` | **唯一密钥源**（不进 git） |
| `openclaw.template.json` | 网关配置模板（占位符） |
| `openclaw.json` | 由 `render_gateway_config.py` 渲染生成（不进 git） |

```bash
kj   # Clash → 渲染网关配置 → Gateway → 新闻/监控/提醒/看门狗
```

---

## 目录速览

```
.openclaw/
├── kj.sh / openclaw.template.json
├── workspace/
│   ├── 策略v6.1.md / SOUL.md / ...
│   └── scripts/          # 交易与运维（见上）
├── logs/                 # trading / watchdog / news / alerts
└── agents/ cron/ ...     # OpenClaw 运行时
```

---

## 技术栈

- Python 3 + Binance USD-M Futures Testnet（WebSocket + REST + Algo Order）
- DeepSeek `deepseek-flash`（经 OpenClaw Gateway / 直连）
- SQLite（WAL）归因；Clash/Mihomo 代理；飞书长连接推送
- 运行环境：Linux / ARM64（PRoot）

---

## 上实盘门槛（当前约定）

- 连续多日稳定盈利、盈亏比 ≥ 1.5
- 代码稳定、配对率与超时退出等记录链路验收通过
- **100U 实盘容错极低**：上线前应单独验收杠杆/仓位，不与策略调参绑死

---

## License / 说明

个人实验项目，仅供学习。Testnet ≠ 主网流动性与滑点；过往收益不代表未来表现。
