# OpenClaw 加密货币监控 - 自启动配置
# 位置：/root/.openclaw/workspace/AUTO_START.md
# 说明：OpenClaw 启动时自动执行的任务配置

---

## 🚀 自启动脚本

**路径：** `/root/.openclaw/workspace/scripts/startup.sh`

**执行方式：**
```bash
# OpenClaw 启动时自动执行
/root/.openclaw/workspace/scripts/startup.sh
```

---

## ✅ 自动启动的脚本

| 脚本 | 功能 | 状态 |
| :--- | :--- | :--- |
| `crypto_signal_monitor.py` | 主监控 - 信号评分系统 | ✅ 启用 |
| `news_fetcher_daemon.py` | 新闻抓取守护进程 | ✅ 启用 |
| `crypto_news_analyzer.py` | 新闻情绪分析 | ✅ 启用 |
| `crypto_report_pusher.py` | 报告推送 | ✅ 启用 |
| `proxy_health_monitor.py` | 代理健康监控 | ✅ 启用 |

---

## ⛔ 禁止自动启动

| 脚本 | 原因 |
| :--- | :--- |
| `/root/.openclaw/crypto/monitor.py` | 用户脚本，需用户手动启动 |

---

## 📝 日志目录

**路径：** `/root/.openclaw/workspace/scripts/logs/`

**日志文件：**
- `hysteria.log` - 代理日志
- `signal_monitor.log` - 主监控日志
- `news_fetcher.log` - 新闻抓取日志
- `news_analyzer.log` - 新闻分析日志
- `report_pusher.log` - 报告推送日志
- `proxy_monitor.log` - 代理监控日志

---

## 🔄 手动管理

**启动所有：**
```bash
/root/.openclaw/workspace/scripts/startup.sh
```

**查看运行状态：**
```bash
ps aux | grep -E "python3|hysteria" | grep -v grep
```

**停止所有：**
```bash
pkill -9 -f "crypto_.*\.py"
pkill -9 -f "news_.*\.py"
pkill -9 -f "proxy_.*\.py"
```

---

**最后更新：** 2026-03-12 00:20
**配置版本：** v1.0
