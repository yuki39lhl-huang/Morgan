#!/bin/bash
# v3.23 - 一键 kj：修复 Gateway 启动误杀与误报（启动后不再主动 stop、就绪等待放宽到 300s）
# v3.22 - 一键 kj：强化 Gateway/锁/端口清理，Termux 重启后也可直接 kj
# 关键：openclaw 启动时屏蔽全局代理（飞书必须直连）

SCRIPT_DIR="/root/.openclaw/workspace/scripts"
OPENCLAW_LOG_ROOT="/root/.openclaw/logs"
mkdir -p "$OPENCLAW_LOG_ROOT"/{trading,watchdog,news,clash,alerts,gateway}/archive
PID_FILE="/tmp/crypto_monitor.pid"
GATEWAY_PID_FILE="/tmp/openclaw_gateway.pid"
GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"
SYSTEM_NODE="/usr/bin/node"
OPENCLAW_ENTRY="/usr/bin/openclaw"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

export NODE_COMPILE_CACHE=/var/tmp/openclaw-compile-cache
export OPENCLAW_NO_RESPAWN=1
mkdir -p /var/tmp/openclaw-compile-cache /tmp/openclaw

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${BLUE}🚀 加密货币监控系统启动 (v3.23)${NC}"
echo -e "${BLUE}时间: $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${BLUE}==============================================${NC}\n"

chmod +x "$SCRIPT_DIR/mihomo" 2>/dev/null || true
chmod +x "/root/.openclaw/kj.sh" 2>/dev/null || true

# ── 进程清理（避免管道子 shell 导致 kill 无效）──
kill_pids_matching() {
    local sig="$1"
    shift
    local pat pid
    for pat in "$@"; do
        while IFS= read -r pid; do
            [ -z "$pid" ] && continue
            [ "$pid" = "$$" ] && continue
            [ "$pid" = "$PPID" ] && continue
            kill "$sig" "$pid" 2>/dev/null || true
        done < <(pgrep -f "$pat" 2>/dev/null || true)
    done
}

free_gateway_port() {
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${GATEWAY_PORT}/tcp" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        local pids
        pids=$(lsof -ti ":${GATEWAY_PORT}" 2>/dev/null || true)
        [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
    fi
}

clear_gateway_locks() {
    rm -f /tmp/openclaw/*.lock 2>/dev/null || true
    rm -f /tmp/openclaw-*/gateway*.lock 2>/dev/null || true
    find /tmp -maxdepth 2 -name 'gateway.*.lock' -delete 2>/dev/null || true
}

stop_openclaw_gateway() {
    echo -e "${BLUE}   停止 OpenClaw Gateway...${NC}"

    # 官方 stop（用系统 Node，避免 Termux 默认 node 版本不对）
    if [ -x "$SYSTEM_NODE" ] && [ -f "$OPENCLAW_ENTRY" ]; then
        timeout 20 env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
            NO_PROXY="*" no_proxy="*" \
            "$SYSTEM_NODE" "$OPENCLAW_ENTRY" gateway stop >/dev/null 2>&1 || true
    fi

    # pid 文件里的可能是 nohup 壳进程，真正常驻多为 openclaw-gateway
    if [ -f "$GATEWAY_PID_FILE" ]; then
        local old
        old=$(cat "$GATEWAY_PID_FILE" 2>/dev/null)
        if [ -n "$old" ]; then
            kill "$old" 2>/dev/null || true
            sleep 1
            kill -9 "$old" 2>/dev/null || true
        fi
    fi

    kill_pids_matching TERM \
        "openclaw-gateway" \
        "openclaw gateway" \
        "node.*openclaw.*gateway" \
        "node /usr/bin/openclaw gateway" \
        "node.*openclaw/dist"
    sleep 2
    kill_pids_matching -9 \
        "openclaw-gateway" \
        "openclaw gateway" \
        "node.*openclaw.*gateway" \
        "node /usr/bin/openclaw gateway" \
        "node.*openclaw/dist"

    free_gateway_port
    clear_gateway_locks
    # 等待端口真正释放（该环境 fuser/lsof 常不可用），避免新实例启动时被旧端口阻塞交接
    for _ in $(seq 1 15); do
        if timeout 1 bash -c "</dev/tcp/127.0.0.1/$GATEWAY_PORT" 2>/dev/null; then
            sleep 1
        else
            break
        fi
    done
    rm -f "$GATEWAY_PID_FILE" 2>/dev/null || true

    if is_gateway_running; then
        echo -e "${YELLOW}   ⚠️ 仍有 openclaw-gateway 进程，再次强杀...${NC}"
        kill_pids_matching -9 "openclaw-gateway"
        sleep 1
    fi
}

is_gateway_running() {
    # Termux/PRoot 下 comm 常被截断为 openclaw-gatewa，不能用 pgrep -x
    pgrep -f '[o]penclaw-gateway' >/dev/null 2>&1
}

resolve_gateway_pid() {
    pgrep -f '[o]penclaw-gateway' 2>/dev/null | head -1
}

echo -e "${BLUE}🧹 清理旧进程（避免重复运行）...${NC}"

stop_openclaw_gateway

kill_pids_matching TERM \
    "crypto_signal_monitor.py" \
    "crypto_monitor_watchdog.py" \
    "news_fetcher_daemon.py" \
    "crypto_news_fetcher.py" \
    "reminder_scheduler.py" \
    "weekly_scheduler.py" \
    "/root/.openclaw/workspace/scripts/mihomo"
sleep 2
kill_pids_matching -9 \
    "crypto_signal_monitor.py" \
    "crypto_monitor_watchdog.py" \
    "news_fetcher_daemon.py" \
    "crypto_news_fetcher.py" \
    "reminder_scheduler.py" \
    "weekly_scheduler.py" \
    "/root/.openclaw/workspace/scripts/mihomo"

rm -f "$PID_FILE" /tmp/crypto_monitor.lock 2>/dev/null || true

echo -e "${GREEN}✅ 旧进程清理完成${NC}"

if [ -f "$SCRIPT_DIR/mihomo" ] && [ ! -x "$SCRIPT_DIR/mihomo" ]; then
    chmod +x "$SCRIPT_DIR/mihomo"
    echo -e "${YELLOW}🔧 已自动修复 mihomo 执行权限${NC}"
fi

# 订阅节点同步（节点文件缺失或超 24h 时刷新；失败时沿用旧文件，保证 Clash 可启动）
if command -v python3 >/dev/null 2>&1; then
    if [ ! -f "$SCRIPT_DIR/sub-nodes.yaml" ] || [ -n "$(find "$SCRIPT_DIR/sub-nodes.yaml" -mmin +1440 2>/dev/null)" ]; then
        python3 "$SCRIPT_DIR/update_clash_nodes.py" >/dev/null 2>&1 \
            || echo -e "${YELLOW}⚠️ 订阅节点更新失败（沿用已有节点文件）${NC}"
    fi
fi

echo -e "\n${BLUE}🔧 启动 Clash (Mihomo) 代理...${NC}"
CLASH_CONFIG="$SCRIPT_DIR/clash-config.yaml"
CLASH_LOG="$OPENCLAW_LOG_ROOT/clash/clash.log"
if [ -f "$CLASH_LOG" ] && [ "$(stat -c%s "$CLASH_LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    mv "$CLASH_LOG" "$OPENCLAW_LOG_ROOT/clash/archive/clash_$(date '+%Y-%m-%d_%H-%M-%S').log"
    touch "$CLASH_LOG"
fi

nohup "$SCRIPT_DIR/mihomo" -d "$SCRIPT_DIR" -f "$CLASH_CONFIG" >> "$CLASH_LOG" 2>&1 &
CLASH_PID=$!
sleep 5

if ps -p $CLASH_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Clash 代理已启动 (PID: $CLASH_PID)${NC}"
    CONNECTED=false
    for i in {1..8}; do
        if curl -s -x http://127.0.0.1:7890 -m 5 https://api.binance.com/api/v3/ping > /dev/null 2>&1 \
           || curl -s -x http://127.0.0.1:7890 -m 5 https://testnet.binancefuture.com/fapi/v1/ping > /dev/null 2>&1; then
            echo -e "${GREEN}✅ 代理连接测试成功 (Binance)${NC}"
            CONNECTED=true
            break
        fi
        sleep 3
    done
    if [ "$CONNECTED" = false ]; then
        echo -e "${YELLOW}⚠️ 代理连接测试未通（非致命，节点可能稍后才就绪）${NC}"
    fi
else
    echo -e "${RED}❌ Clash 代理启动失败${NC}"
fi

echo -e "\n${BLUE}🌐 启动 OpenClaw Gateway ...${NC}"
TODAYS_LOG="/tmp/openclaw/openclaw-$(date '+%Y-%m-%d').log"
GATEWAY_LOG="$TODAYS_LOG"
GATEWAY_LOG_BYTES_BEFORE=0
[ -f "$GATEWAY_LOG" ] && GATEWAY_LOG_BYTES_BEFORE=$(wc -c < "$GATEWAY_LOG" 2>/dev/null || echo 0)

if [ ! -x "$SYSTEM_NODE" ]; then
    echo -e "${RED}❌ 系统 Node 不存在: $SYSTEM_NODE${NC}"
    exit 1
fi
if [ ! -f "$OPENCLAW_ENTRY" ]; then
    echo -e "${RED}❌ openclaw 入口不存在: $OPENCLAW_ENTRY${NC}"
    exit 1
fi

gateway_log_since_start() {
    if [ ! -f "$GATEWAY_LOG" ]; then
        return 1
    fi
    tail -c +$((GATEWAY_LOG_BYTES_BEFORE + 1)) "$GATEWAY_LOG" 2>/dev/null
}

# 冷启动时 openclaw-gateway 进程可能要 10~30 秒才出现（插件注册、锁/端口交接），
# 这里轮询等待真实进程出现，而不是固定 sleep 后立刻判定失败。
start_gateway_once() {
    GATEWAY_LOG_BYTES_BEFORE=0
    [ -f "$GATEWAY_LOG" ] && GATEWAY_LOG_BYTES_BEFORE=$(wc -c < "$GATEWAY_LOG" 2>/dev/null || echo 0)
    nohup env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
        NO_PROXY="*" no_proxy="*" \
        "$SYSTEM_NODE" "$OPENCLAW_ENTRY" gateway >>/dev/null 2>&1 &
    local launcher=$!
    local real
    for _ in $(seq 1 30); do
        real=$(resolve_gateway_pid)
        if [ -n "$real" ]; then
            echo "$real" > "$GATEWAY_PID_FILE"
            echo "$real"
            return 0
        fi
        sleep 1
    done
    # 兜底：真实进程尚未出现但 launcher 还活着，按已拉起处理（交给下方就绪等待）
    if ps -p "$launcher" >/dev/null 2>&1; then
        echo "$launcher" > "$GATEWAY_PID_FILE"
        echo "$launcher"
        return 0
    fi
    return 1
}

# 关键：启动后就绪等待阶段绝不再主动 stop —— v3.22 曾在此把刚拉起还没就绪的
# gateway 误杀（SIGTERM），导致后续交接要等数分钟并误报"进程已退出"。
GATEWAY_PID=$(start_gateway_once)
[ -z "$GATEWAY_PID" ] && GATEWAY_PID=0

GATEWAY_OK=false
FEISHU_OK=false
GATEWAY_DEAD=false
GATEWAY_ALREADY_RUNNING_RETRIED=false
WAIT_BUDGET=300

# launcher 立刻死掉时只给 60 秒容错，随后直接报错
if [ "$GATEWAY_PID" -eq 0 ]; then
    for _ in $(seq 1 60); do
        GATEWAY_PID=$(resolve_gateway_pid)
        [ -n "$GATEWAY_PID" ] && break
        sleep 1
    done
    [ -z "$GATEWAY_PID" ] && GATEWAY_PID=0
fi

if [ "$GATEWAY_PID" -eq 0 ]; then
    GATEWAY_DEAD=true
else
    for i in $(seq 1 "$WAIT_BUDGET"); do
        real=$(resolve_gateway_pid)
        if [ -n "$real" ]; then
            GATEWAY_PID=$real
            echo "$GATEWAY_PID" > "$GATEWAY_PID_FILE"
        fi

        # 旧实例仍占锁/端口（本次启动被拒）→ 清理后重试一次
        if [ "$GATEWAY_ALREADY_RUNNING_RETRIED" = false ] \
            && gateway_log_since_start | grep -q "gateway already running"; then
            echo -e "${YELLOW}   检测到 gateway already running，清理旧实例后重启...${NC}"
            GATEWAY_ALREADY_RUNNING_RETRIED=true
            stop_openclaw_gateway
            sleep 2
            GATEWAY_PID=$(start_gateway_once)
            [ -z "$GATEWAY_PID" ] && GATEWAY_PID=0
            [ "$GATEWAY_PID" -eq 0 ] && break
            continue
        fi

        gateway_log_since_start | grep -qE "listening on ws://|bind=lan|Gateway listening|gateway started" && GATEWAY_OK=true
        gateway_log_since_start | grep -qE "feishu.*connect|lark.*websocket|feishu.*ready|feishu.*long.*connect|ws client ready|WebSocket client started|client ready" && FEISHU_OK=true

        # 已就绪后提前退出（避免后续每轮重复读大日志）
        if [ "$GATEWAY_OK" = true ] && [ "$FEISHU_OK" = true ]; then
            break
        fi

        [ $((i % 30)) -eq 0 ] && echo -e "${YELLOW}   ⏳ 等待 Gateway 就绪中（${i}s / ${WAIT_BUDGET}s）...${NC}"
        sleep 1
    done
fi

if [ "$GATEWAY_DEAD" = true ] || ! is_gateway_running; then
    echo -e "${RED}❌ Gateway 未能启动${NC}"
    echo -e "${RED}   常见原因：lock 未释放、端口被占、飞书凭据错误${NC}"
    echo -e "${YELLOW}   本次启动日志最后 30 行：${NC}"
    LOG_NEW=$(gateway_log_since_start)
    if [ -n "$LOG_NEW" ]; then
        echo "$LOG_NEW" | tail -30 | sed 's/^/   /'
    else
        echo -e "${YELLOW}   （本次启动尚无日志输出，以下为完整日志尾部）${NC}"
        tail -30 "$GATEWAY_LOG" 2>/dev/null | sed 's/^/   /'
    fi
    rm -f "$GATEWAY_PID_FILE"
else
    GW_PID=$(resolve_gateway_pid || echo "$GATEWAY_PID")
    echo "$GW_PID" > "$GATEWAY_PID_FILE" 2>/dev/null || true
    if [ "$GATEWAY_OK" = true ] && [ "$FEISHU_OK" = true ]; then
        echo -e "${GREEN}✅ OpenClaw Gateway 已就绪 (PID: $GW_PID)${NC}"
        echo -e "${GREEN}✅ 飞书长连接已建立${NC}"
    elif [ "$GATEWAY_OK" = true ]; then
        echo -e "${GREEN}✅ OpenClaw Gateway 后台运行中 (PID: $GW_PID)${NC}"
        echo -e "${YELLOW}⚠️ 飞书长连接状态未确认，查看日志：tail -f $GATEWAY_LOG${NC}"
    else
        echo -e "${YELLOW}⚠️ Gateway 进程存活但就绪日志未出现（可能仍在初始化），查看：tail -f $GATEWAY_LOG${NC}"
    fi
fi

echo -e "\n${BLUE}📰 启动新闻抓取守护进程（每小时周期性抓取）...${NC}"
cd "$SCRIPT_DIR"
nohup python3 news_fetcher_daemon.py >/dev/null 2>&1 &
NEWS_PID=$!
sleep 2
if ps -p $NEWS_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 新闻守护进程已启动 (PID: $NEWS_PID)${NC}"
else
    echo -e "${RED}❌ 新闻守护进程启动失败${NC}"
fi

echo -e "\n${BLUE}💰 自动交易模块${NC}"
echo -e "${GREEN}✅ binance_auto_trade.py 由信号监控按需调用（无需独立启动）${NC}"

echo -e "\n${BLUE}📊 启动信号监控主进程...${NC}"
cd "$SCRIPT_DIR"
nohup python3 crypto_signal_monitor.py >/dev/null 2>&1 &
SIGNAL_PID=$!
echo $SIGNAL_PID > "$PID_FILE"
sleep 3
if ps -p $SIGNAL_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 信号监控已启动 (PID: $SIGNAL_PID)${NC}"
else
    echo -e "${RED}❌ 信号监控启动失败${NC}"
    rm -f "$PID_FILE"
fi

echo -e "\n${BLUE}⏰ 启动作息提醒调度器...${NC}"
REMINDER_SCRIPT="$SCRIPT_DIR/reminder_scheduler.py"
if [ -f "$REMINDER_SCRIPT" ]; then
    nohup python3 "$REMINDER_SCRIPT" >/dev/null 2>&1 &
    REMINDER_PID=$!
    sleep 2
    if ps -p $REMINDER_PID > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 作息提醒调度器已启动 (PID: $REMINDER_PID)${NC}"
    else
        echo -e "${RED}❌ 作息提醒调度器启动失败${NC}"
    fi
else
    echo -e "${YELLOW}⚠️ 未找到提醒调度脚本: $REMINDER_SCRIPT${NC}"
fi

echo -e "\n${BLUE}📊 启动量化周报调度器（每周一 09:00 推送盈亏比验证周报）...${NC}"
WEEKLY_SCRIPT="$SCRIPT_DIR/weekly_scheduler.py"
if [ -f "$WEEKLY_SCRIPT" ]; then
    nohup python3 "$WEEKLY_SCRIPT" >/dev/null 2>&1 &
    WEEKLY_PID=$!
    sleep 2
    if ps -p $WEEKLY_PID > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 周报调度器已启动 (PID: $WEEKLY_PID)${NC}"
    else
        echo -e "${RED}❌ 周报调度器启动失败${NC}"
    fi
else
    echo -e "${YELLOW}⚠️ 未找到周报调度脚本: $WEEKLY_SCRIPT${NC}"
fi

echo -e "\n${BLUE}🐕 启动看门狗（监控进程崩溃自动重启）...${NC}"
cd "$SCRIPT_DIR"
nohup python3 crypto_monitor_watchdog.py >/dev/null 2>&1 &
WATCHDOG_PID=$!
sleep 2
if ps -p $WATCHDOG_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 看门狗已启动 (PID: $WATCHDOG_PID)${NC}"
else
    echo -e "${RED}❌ 看门狗启动失败${NC}"
fi

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${BLUE}📋 当前运行进程${NC}"
echo -e "${BLUE}==============================================${NC}"
ps aux | grep -E "crypto_signal_monitor|crypto_monitor_watchdog|news_fetcher_daemon|crypto_news_fetcher|reminder_scheduler|weekly_scheduler|mihomo|openclaw-gateway" | grep -v grep

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${GREEN}✅ v3.22 kj 启动完成${NC}"
echo -e "${BLUE}==============================================${NC}\n"

echo -e "${BLUE}📁 日志目录: $OPENCLAW_LOG_ROOT${NC}"
echo -e "  - 交易: $OPENCLAW_LOG_ROOT/trading/monitor.log （>10MB 自动归档）"
echo -e "  - 看门狗: $OPENCLAW_LOG_ROOT/watchdog/watchdog.log"
echo -e "  - 新闻: $OPENCLAW_LOG_ROOT/news/news.log"
echo -e "  - Clash: $OPENCLAW_LOG_ROOT/clash/clash.log"
echo -e "  - 整点备份: $OPENCLAW_LOG_ROOT/alerts/hourly_YYYY-MM-DD.log"
echo -e "  - Gateway: $GATEWAY_LOG\n"

echo -e "${YELLOW}💡 查看实时日志: tail -f $GATEWAY_LOG${NC}"
echo -e "${YELLOW}💡 清理 PID 文件: rm -f $PID_FILE $GATEWAY_PID_FILE${NC}\n"
