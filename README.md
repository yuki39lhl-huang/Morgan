# Morgan

## 📊 加密货币自动交易与监控系统

基于 Binance Testnet 的加密货币自动化交易系统，集信号生成、AI 决策、风险管理、飞书实时推送于一体。

### 核心功能

| 模块 | 说明 |
| :--- | :--- |
| **信号监控** | 7 币种 (BTC/ETH/SOL/BNB/DOT/LINK/XRP) 15 秒轮询，RSI + MA60 + 24h涨跌 + 成交量四维评分 0-100 |
| **AI 混合预测** | 集成 DeepSeek v4-flash，关键信号/重大新闻时触发，与数学信号交叉验证决策 |
| **自动开仓** | 动态阈值（Trending/Ranging/Volatile），逐仓 10x 杠杆，单笔 20 USDT，最大 4 仓 |
| **移动止盈止损** | 三档递进止盈 + 智能止损（ATR + RSI 因子），盈亏比 ≥ 1.5:1 |
| **飞书推送** | 价格预警、开平仓通知、小时汇报、AI 分析简报，实时卡片推送 |
| **风险熔断** | 日亏损 -10% 暂停 4h，同币种 3 分钟信号冷却 |
| **作息提醒** | 独立调度器，7 个时段推送（起床/早餐/午餐/午睡/跑步/晚餐/睡觉），附带实时天气 |
| **健康审计** | 进程健康检查、策略审计、交易验证、异常自动恢复 |

### 技术栈

- **数据源**: Binance WebSocket + REST API（主力），CoinGecko（备用）
- **代理**: Clash (Mihomo) 科学上网
- **AI**: DeepSeek v4-flash API（交易决策）/ v4-flash（日常对话）
- **通信**: OpenClaw Gateway + 飞书机器人
- **系统**: Linux / ARM64 (PRoot)

### 目录结构
scripts/
├── crypto_signal_monitor.py   # 信号监控主进程
├── binance_auto_trade.py      # 自动交易引擎
├── manual_trade.py            # 手动交易 CLI
├── reminder_scheduler.py      # 作息提醒调度器
├── health_check.py            # 系统健康检查
├── audit_strategy.py          # 策略审计
├── verify_trades.py           # 交易验证
├── query_price.py             # 价格查询
├── query_positions.py         # 持仓查询
├── query_news.py              # 新闻查询
├── news_fetcher_daemon.py     # 新闻抓取守护进程
├── crypto_monitor_watchdog.py # 进程看门狗
├── feishu_helper.py           # 飞书 API 封装
└── clash-config.yaml          # Clash 代理配置

### 一键启动
```bash
kj
