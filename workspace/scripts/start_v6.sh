#!/bin/bash
# 启动 v6.0 监控脚本（含 Clash 自启 + 进程守护）

cd /root/.openclaw/workspace/scripts

# 1. 启动 Clash（后台静默，写入 PID）
./mihomo -f clash-config.yaml -p 7890 --silent &
CLASH_PID=$!
echo $CLASH_PID > clash.pid
echo "Clash 启动中（PID: $CLASH_PID）..."

# 2. 等待 Clash API 就绪（最多 10s）
for i in {1..10}; do
  if curl -s http://127.0.0.1:9090/configs | grep -q '"mode"'; then
    echo "✅ Clash 已就绪"
    break
  fi
  sleep 1
done

# 3. 启动监控（后台运行，写入 PID）
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="http://127.0.0.1:7890"
python3 crypto_signal_monitor.py > crypto_monitor.out 2>&1 &
MONITOR_PID=$!
echo $MONITOR_PID > monitor.pid
echo "✅ 监控脚本已启动（PID: $MONITOR_PID）"

echo "\n🎉 全链路启动完成！"
echo "- Clash PID: $(cat clash.pid)"
echo "- Monitor PID: $(cat monitor.pid)"
echo "- 日志查看：tail -f crypto_monitor.out"
