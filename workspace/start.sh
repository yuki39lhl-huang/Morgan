#!/bin/bash
# OpenClaw 一键启动脚本
# 位置：/root/.openclaw/workspace/start.sh
# 用法：./start.sh 或 /root/.openclaw/workspace/start.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"

echo "🚀 OpenClaw 加密货币监控网关启动"
echo "⏰ 启动时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 创建日志目录
mkdir -p "$LOG_DIR"

# 检查并启动代理
if ! pgrep -x "hysteria" > /dev/null; then
    echo "📡 启动 Hysteria2 代理..."
    nohup /tmp/hysteria --config "$SCRIPT_DIR/scripts/hysteria.yaml" client > "$LOG_DIR/hysteria.log" 2>&1 &
    sleep 3
fi

# 等待代理就绪
sleep 2

# 启动监控任务
echo "📊 启动监控任务..."
cd "$SCRIPT_DIR/scripts"

# 检查并启动主监控
if ! pgrep -f "crypto_signal_monitor.py" > /dev/null; then
    nohup python3 crypto_signal_monitor.py > "$LOG_DIR/signal_monitor.log" 2>&1 &
    echo "  ✅ crypto_signal_monitor.py"
fi

# 检查并启动新闻抓取
if ! pgrep -f "news_fetcher_daemon.py" > /dev/null; then
    nohup python3 news_fetcher_daemon.py > "$LOG_DIR/news_fetcher.log" 2>&1 &
    echo "  ✅ news_fetcher_daemon.py"
fi

# 检查并启动新闻分析
if ! pgrep -f "crypto_news_analyzer.py" > /dev/null; then
    nohup python3 crypto_news_analyzer.py > "$LOG_DIR/news_analyzer.log" 2>&1 &
    echo "  ✅ crypto_news_analyzer.py"
fi

# 检查并启动报告推送
if ! pgrep -f "crypto_report_pusher.py" > /dev/null; then
    nohup python3 crypto_report_pusher.py > "$LOG_DIR/report_pusher.log" 2>&1 &
    echo "  ✅ crypto_report_pusher.py"
fi

# 检查并启动代理监控
if ! pgrep -f "proxy_health_monitor.py" > /dev/null; then
    nohup python3 proxy_health_monitor.py > "$LOG_DIR/proxy_monitor.log" 2>&1 &
    echo "  ✅ proxy_health_monitor.py"
fi

sleep 2

echo ""
echo "✅ 监控任务启动完成！"
echo ""
echo "📋 运行中的进程："
ps aux | grep -E "python3.*crypto|python3.*news|python3.*proxy|hysteria" | grep -v grep | awk '{printf "  - PID %s: %s\n", $2, $11}'
echo ""

# 启动 OpenClaw Gateway
echo "🦞 启动 OpenClaw Gateway (端口 18789)..."
echo "⚠️  按 Ctrl+C 停止"
echo ""

openclaw gateway --port 18789
