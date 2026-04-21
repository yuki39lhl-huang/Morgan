#!/bin/bash
# v3.18 - 最终修复版 kj 脚本

SCRIPT_DIR="/root/.openclaw/workspace/scripts"
LOG_DIR="$SCRIPT_DIR"
LOCK_FILE="/tmp/crypto_monitor.lock"
PID_FILE="/tmp/crypto_monitor.pid"
GATEWAY_PID_FILE="/tmp/openclaw_gateway.pid"
GATEWAY_LOG="$TODAYS_LOG"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo -e "${RED}❌ 另一个 kj 进程正在运行，无法重复启动！${NC}"
    exit 1
fi

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${BLUE}🚀 加密货币监控系统启动 (v3.18)${NC}"
echo -e "${BLUE}时间: $(date '+%Y-%m-%d %H:%M:%S')${NC}"
echo -e "${BLUE}==============================================${NC}\n"

echo -e "${YELLOW}📋 停止旧进程...${NC}"
pkill -f "crypto_signal_monitor.py" 2>/dev/null
pkill -f "binance_auto_trade.py" 2>/dev/null
pkill -f "crypto_news_fetcher.py" 2>/dev/null
pkill -f "openclaw gateway" 2>/dev/null
pkill -f "openclaw-gateway" 2>/dev/null
pkill -f "mihomo" 2>/dev/null
pkill -f "ngrok" 2>/dev/null
sleep 2

rm -f "$PID_FILE" "$GATEWAY_PID_FILE"
rm -f /tmp/openclaw_gateway.lock
mkdir -p /tmp/openclaw

echo -e "\n${BLUE}🔧 启动 Clash (Mihomo) 代理...${NC}"
CLASH_CONFIG="$SCRIPT_DIR/clash-config.yaml"
CLASH_LOG="$SCRIPT_DIR/clash.log"

nohup "$SCRIPT_DIR/mihomo" -d "$SCRIPT_DIR" -f "$CLASH_CONFIG" > "$CLASH_LOG" 2>&1 &
CLASH_PID=$!
sleep 5

if ps -p $CLASH_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Clash 代理已启动 (PID: $CLASH_PID)${NC}"
    CONNECTED=false
    for i in {1..3}; do
        if curl -s -x http://127.0.0.1:7890 -m 5 https://api.binance.com/api/v3/ping > /dev/null 2>&1; then
            echo -e "${GREEN}✅ 代理连接测试成功 (Binance)${NC}"
            CONNECTED=true
            break
        fi
        sleep 2
    done
    if [ "$CONNECTED" = false ]; then
        echo -e "${YELLOW}⚠️ 代理连接测试失败（非致命，继续）${NC}"
    fi
else
    echo -e "${RED}❌ Clash 代理启动失败${NC}"
fi

echo -e "\n${BLUE}🌐 启动 OpenClaw Gateway (v3.13) ...${NC}"
TODAYS_LOG="/tmp/openclaw/openclaw-$(date '+%Y-%m-%d').log"
GATEWAY_LOG="${GATEWAY_LOG:-/tmp/openclaw/openclaw-$(date '+%Y-%m-%d').log}"
mkdir -p "$(dirname "$GATEWAY_LOG")"

nohup openclaw gateway --no-auth --group-policy open > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!
echo $GATEWAY_PID > "$GATEWAY_PID_FILE"

GATEWAY_OK=false
for i in $(seq 1 60); do
    if ! ps -p $GATEWAY_PID > /dev/null 2>&1; then
        echo -e "${RED}❌ Gateway 进程已退出${NC}"
        break
    fi
    for logfile in "$GATEWAY_LOG" "$TODAYS_LOG"; do
        if [ -f "$logfile" ] && grep -q "listening on ws://" "$logfile" 2>/dev/null; then
            echo -e "${GREEN}✅ OpenClaw Gateway 已就绪 (PID: $GATEWAY_PID)${NC}"
            GATEWAY_OK=true
            break 2
        fi
    done
    sleep 1
done

if [ "$GATEWAY_OK" = false ]; then
    if ps -p $GATEWAY_PID > /dev/null 2>&1; then
        echo -e "${GREEN}✅ OpenClaw Gateway 后台运行中 (PID: $GATEWAY_PID)${NC}"
        echo -e "${YELLOW}💡 查看日志: tail -f $TODAYS_LOG${NC}"
    else
        echo -e "${RED}❌ Gateway 启动失败，请检查日志${NC}"
    fi
fi

echo -e "\n${BLUE}🌐 启动 Ngrok 隧道 (端口 18789)...${NC}"
if ! command -v ngrok &> /dev/null; then
    echo -e "${YELLOW}⚠️ ngrok 未安装，跳过隧道启动${NC}"
else
    NGROK_TOKEN=$(cat ~/.ngrok2/ngrok.yml 2>/dev/null | grep auth_token | awk '{print $2}' | tr -d '"' 2>/dev/null)
    if [ -z "$NGROK_TOKEN" ]; then
        echo -e "${YELLOW}⚠️ ngrok token 未配置，跳过隧道启动${NC}"
    else
        nohup ngrok http 18789 > /dev/null 2>&1 &
        sleep 3
        echo -e "${GREEN}✅ Ngrok 已启动${NC}"
    fi
fi

echo -e "\n${BLUE}📰 启动新闻抓取...${NC}"
cd "$SCRIPT_DIR"
nohup python3 crypto_news_fetcher.py >> "$LOG_DIR/crypto_news.log" 2>&1 &
NEWS_PID=$!
sleep 2
if ps -p $NEWS_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 新闻抓取已启动 (crypto_news_fetcher.py, PID: $NEWS_PID)${NC}"
else
    echo -e "${RED}❌ 新闻抓取启动失败${NC}"
fi

echo -e "\n${BLUE}💰 启动自动交易...${NC}"
cd "$SCRIPT_DIR"
nohup python3 binance_auto_trade.py >> "$LOG_DIR/binance_trade.log" 2>&1 &
TRADE_PID=$!
sleep 2
if ps -p $TRADE_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 自动交易已启动 (PID: $TRADE_PID)${NC}"
else
    echo -e "${RED}❌ 自动交易启动失败${NC}"
fi

echo -e "\n${BLUE}📊 启动信号监控主进程...${NC}"
cd "$SCRIPT_DIR"
nohup python3 crypto_signal_monitor.py >> "$LOG_DIR/crypto_monitor.log" 2>&1 &
SIGNAL_PID=$!
echo $SIGNAL_PID > "$PID_FILE"
sleep 3
if ps -p $SIGNAL_PID > /dev/null 2>&1; then
    echo -e "${GREEN}✅ 信号监控已启动 (PID: $SIGNAL_PID)${NC}"
else
    echo -e "${RED}❌ 信号监控启动失败${NC}"
    rm -f "$PID_FILE"
fi

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${BLUE}📋 当前运行进程${NC}"
echo -e "${BLUE}==============================================${NC}"
ps aux | grep -E "crypto_signal_monitor|binance_auto_trade|crypto_news_fetcher|mihomo|openclaw" | grep -v grep

echo -e "\n${BLUE}==============================================${NC}"
echo -e "${GREEN}✅ v3.18 kj 启动完成${NC}"
echo -e "${BLUE}==============================================${NC}\n"

echo -e "${BLUE}📁 日志文件位置:${NC}"
echo -e "  - Clash: $LOG_DIR/clash.log"
echo -e "  - Gateway: $TODAYS_LOG"
echo -e "  - 信号监控: $LOG_DIR/crypto_monitor.log"
echo -e "  - 自动交易: $LOG_DIR/binance_trade.log\n"

echo -e "${YELLOW}💡 查看实时日志: tail -f $TODAYS_LOG${NC}"
echo -e "${YELLOW}💡 强制解锁: rm -f $LOCK_FILE $PID_FILE $GATEWAY_PID_FILE${NC}\n"

exec 200>&-
