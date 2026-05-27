# OpenClaw 日志目录

## 结构

| 目录 | 内容 | 当前文件 | 归档条件 |
|------|------|----------|----------|
| `trading/` | 信号监控、开平仓、Binance、AI | `monitor.log` | 单文件 > **10MB** → `archive/trading_日期_时间.log` |
| `watchdog/` | 看门狗心跳、进程重启 | `watchdog.log` | > **5MB** |
| `news/` | 新闻抓取守护 | `news.log` | > **5MB** |
| `clash/` | Mihomo 代理 | `clash.log` | > **5MB** |
| `alerts/` | 整点汇报文本 | `hourly_YYYY-MM-DD.log` | **按自然日** 一个新文件 |
| `gateway/` | 说明文件 | — | 实际日志在 `/tmp/openclaw/openclaw-YYYY-MM-DD.log` |

## 常用命令

```bash
# 实时交易日志
tail -f /root/.openclaw/logs/trading/monitor.log

# 查某一天交易记录
grep "2026-05-25" /root/.openclaw/logs/trading/monitor.log
ls /root/.openclaw/logs/trading/archive/

# 整点汇报备份
cat /root/.openclaw/logs/alerts/hourly_2026-05-25.log
```

## 配置

修改 `workspace/scripts/openclaw_logging.py`：

- `LOG_CATEGORIES`：按大小轮转（`max_mb`、`backups`）
- `RETENTION_DAYS`：按天清理（与轮转并存，每日最多执行一次）
  - `trading/archive/` → 30 天
  - `alerts/hourly_*.log` → 60 天
  - `watchdog` / `news` / `clash` 的 `archive/` → 14 天

手动执行清理：`python3 workspace/scripts/openclaw_logging.py`
