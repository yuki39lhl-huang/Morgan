#!/bin/bash
# OpenClaw Hourly Report v6.0
cd /root/.openclaw/workspace
exec 2>&1

echo "【⏰ 每小时行情简报】$(date '+%Y-%m-%d %H:%M')"
echo ""

# Price snapshot
openclaw crypto price --summary | head -n 15

echo ""
echo "【📊 持仓快照】"
openclaw crypto position --summary

echo ""
echo "【💡 信号摘要】"
openclaw crypto signal --summary | head -n 10

echo ""
echo "—— 由王姐守护 · $(( $(date +\%s) % 100 ))" | tee /dev/stderr
