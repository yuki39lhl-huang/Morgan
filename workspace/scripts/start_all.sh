#!/bin/bash
# ============================================================
# 加密货币监控系统 - 统一启动脚本（Clash 版）
# 🚀 一键启动所有任务进程 + OpenClaw Gateway
# 
# 🔒 防重复机制：
#   1. flock 文件锁 - 防止并发执行
#   2. PID 文件检查 - 确保进程唯一
# ============================================================

SCRIPT_DIR="/root/.openclaw/workspace/scripts"
LOG_DIR="$SCRIPT_DIR"
LOCK_FILE="/tmp/crypto_monitor.lock"
PID_FILE="/tmp/crypto_monitor.pid"

# 🔒 使用 flock 防止并发执行
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "❌ 另一个 kj 进程正在运行，无法重复启动！"
    echo "   如需强制重启，先执行：rm -f $LOCK_FILE"
    exit 1
fi

echo "============================================================"
echo "🚀 加密货币监控系统启动"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

# 停止旧进程（彻底清理）
echo "📋 停止旧进程..."

kill_process() {
    local pattern=$1
    local pids=$(pgrep -f "$pattern" 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "   杀死 $pattern: PIDs $pids"
        kill -9 $pids 2>/dev/null
        sleep 1
    fi
}

kill_process "crypto_signal_monitor.py"
kill_process "news_fetcher_daemon.py"
kill_process "openclaw gateway"
kill_process "crypto_monitor_watchdog.py"
kill_process "log_cleanup_watchdog.py"
kill_process "mihomo"
kill_process "clash"

sleep 3

# 启动 Clash (Mihomo) 代理
echo "🔧 启动 Clash (Mihomo) 代理..."
CLASH_CONFIG="$SCRIPT_DIR/clash-config.yaml"
CLASH_LOG="$SCRIPT_DIR/clash.log"

nohup /root/.openclaw/workspace/scripts/mihomo -d "$SCRIPT_DIR" -f "$CLASH_CONFIG" > "$CLASH_LOG" 2>&1 &
CLASH_PID=$!
sleep 5

if ps -p $CLASH_PID > /dev/null 2>&1; then
    echo "✅ Clash 代理已启动 (PID: $CLASH_PID)"
    if curl -s -x http://127.0.0.1:7890 -m 5 https://testnet.binancefuture.com/fapi/v1/time > /dev/null 2>&1; then
        echo "✅ 代理连接测试成功 (Binance Testnet)"
    else
        echo "⚠️  代理连接测试失败，但进程正常运行"
    fi
else
    echo "❌ Clash 代理启动失败"
fi

# ✅ Clash 代理健康检查已集成到 crypto_monitor_watchdog.py
# 不再需要单独的 proxy_health_monitor.py (Hysteria 已废弃)

# 启动 OpenClaw Gateway
echo "🌐 启动 OpenClaw Gateway (端口 18789)..."
nohup openclaw gateway --port 18789 >> gateway.log 2>&1 &
GATEWAY_PID=$!
sleep 3

if ps -p $GATEWAY_PID > /dev/null 2>&1; then
    echo "✅ OpenClaw Gateway 已启动 (PID: $GATEWAY_PID, 端口：18789)"
else
    echo "❌ OpenClaw Gateway 启动失败"
fi

# 启动新闻守护进程
echo "📰 启动新闻守护进程..."
cd "$SCRIPT_DIR"
nohup python3 news_fetcher_daemon.py >> news_fetcher_daemon.log 2>&1 &
NEWS_PID=$!
sleep 2

if ps -p $NEWS_PID > /dev/null 2>&1; then
    echo "✅ 新闻守护进程已启动 (PID: $NEWS_PID)"
else
    echo "❌ 新闻守护进程启动失败"
fi

# 启动信号监控主进程
echo "📊 启动信号监控主进程..."
cd "$SCRIPT_DIR"
nohup python3 crypto_signal_monitor.py >> crypto_monitor.log 2>&1 &
SIGNAL_PID=$!
echo $SIGNAL_PID > "$PID_FILE"
sleep 3

if ps -p $SIGNAL_PID > /dev/null 2>&1; then
    echo "✅ 信号监控进程已启动 (PID: $SIGNAL_PID)"
else
    echo "❌ 信号监控进程启动失败"
    rm -f "$PID_FILE"
fi

# 启动看门狗守护进程
echo "🐕 启动看门狗守护进程..."
cd "$SCRIPT_DIR"
nohup python3 crypto_monitor_watchdog.py >> watchdog.log 2>&1 &
WATCHDOG_PID=$!
sleep 2

if ps -p $WATCHDOG_PID > /dev/null 2>&1; then
    echo "✅ 看门狗已启动 (PID: $WATCHDOG_PID)"
else
    echo "❌ 看门狗启动失败"
fi

# 启动日志清理守护进程
echo "🗑️ 启动日志清理守护进程..."
cd "$SCRIPT_DIR"
nohup python3 log_cleanup_watchdog.py >> log_cleanup.log 2>&1 &
CLEANUP_PID=$!
sleep 1

if ps -p $CLEANUP_PID > /dev/null 2>&1; then
    echo "✅ 日志清理已启动 (PID: $CLEANUP_PID)"
else
    echo "❌ 日志清理启动失败"
fi

# 验证进程状态
echo ""
echo "============================================================"
echo "📋 进程状态检查"
echo "============================================================"
ps aux | grep -E "crypto_signal_monitor|news_fetcher_daemon|openclaw gateway|mihomo|watchdog" | grep -v grep

echo ""
echo "============================================================"
echo "✅ 所有任务进程启动完成"
echo "============================================================"
echo ""
echo "📁 日志文件位置："
echo "   - Clash 代理：$LOG_DIR/clash.log"
echo "   - OpenClaw Gateway: $LOG_DIR/gateway.log"
echo "   - 信号监控：$LOG_DIR/crypto_monitor.log"
echo "   - 看门狗：$LOG_DIR/watchdog.log"
echo ""
echo "💡 查看实时日志：tail -f $LOG_DIR/clash.log"
echo "💡 强制停止：pkill -f crypto_signal_monitor.py && pkill -f mihomo"
echo "💡 强制解锁：rm -f $LOCK_FILE $PID_FILE"
echo ""
