#!/bin/bash
# 日志自动清理脚本
# 保留最近 50000 行，每天凌晨 3 点执行

LOG_FILE="/root/.openclaw/workspace/scripts/crypto_monitor.log"
MAX_LINES=50000
BACKUP_DIR="/root/.openclaw/workspace/scripts/logs_backup"

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 如果日志超过 MAX_LINES 行，清理旧日志
if [ -f "$LOG_FILE" ]; then
    CURRENT_LINES=$(wc -l < "$LOG_FILE")
    if [ "$CURRENT_LINES" -gt "$MAX_LINES" ]; then
        # 备份旧日志（带日期）
        BACKUP_FILE="$BACKUP_DIR/crypto_monitor_$(date +%Y%m%d_%H%M%S).log"
        cp "$LOG_FILE" "$BACKUP_FILE"
        gzip "$BACKUP_FILE"
        
        # 保留最近 50000 行
        tail -n $MAX_LINES "$LOG_FILE" > "$LOG_FILE.tmp"
        mv "$LOG_FILE.tmp" "$LOG_FILE"
        
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 日志清理完成：$CURRENT_LINES → $MAX_LINES 行"
    fi
fi

# 清理 7 天前的备份
find "$BACKUP_DIR" -name "*.log.gz" -mtime +7 -delete
