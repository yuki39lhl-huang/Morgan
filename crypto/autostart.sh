#!/bin/bash
# ============================================================
# 开机自启动脚本
# 由 .bashrc 调用，确保所有服务在登录后自动启动
# ============================================================

SCRIPT_DIR="/root/.openclaw/workspace/scripts"
LOCK_FILE="/tmp/crypto_monitor.lock"
AUTOSTART_LOG="$SCRIPT_DIR/autostart.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$AUTOSTART_LOG"
}

# 🔒 防止重复启动
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "⏳ kj 已经在运行中，跳过"
    exit 0
fi

# 检查是否已经启动（Gateway 是否在跑）
if pgrep -f "openclaw-gateway" > /dev/null 2>&1; then
    log "✅ OpenClaw Gateway 已在运行，跳过自启"
    exit 0
fi

log "🚀 开机自启开始..."

# 等待网络就绪
log "📡 等待网络就绪..."
for i in $(seq 1 10); do
    if curl -s -m 2 https://www.baidu.com > /dev/null 2>&1; then
        log "✅ 网络已就绪"
        break
    fi
    sleep 2
done

# 启动 kj（所有服务）
log "🔧 启动 kj 服务..."
bash "$SCRIPT_DIR/start_all.sh" >> "$AUTOSTART_LOG" 2>&1 &

log "✅ 自启完成"
