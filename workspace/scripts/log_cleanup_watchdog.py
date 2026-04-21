#!/usr/bin/env python3
"""
日志自动清理守护进程
每小时检查一次，保留最近 50000 行
"""

import os
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/root/.openclaw/workspace/scripts/crypto_monitor.log")
BACKUP_DIR = Path("/root/.openclaw/workspace/scripts/logs_backup")
MAX_LINES = 50000
CHECK_INTERVAL = 3600  # 1 小时检查一次

def cleanup():
    """清理日志"""
    if not LOG_FILE.exists():
        return
    
    # 计算行数
    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    
    current_lines = len(lines)
    if current_lines <= MAX_LINES:
        print(f"[{datetime.now()}] 日志行数正常：{current_lines}/{MAX_LINES}")
        return
    
    # 创建备份
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_file = BACKUP_DIR / f"crypto_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    with open(backup_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    # 压缩备份（可选）
    import gzip
    with gzip.open(f"{backup_file}.gz", 'wt', encoding='utf-8') as f:
        f.writelines(lines)
    os.remove(backup_file)
    
    # 保留最近 MAX_LINES 行
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.writelines(lines[-MAX_LINES:])
    
    print(f"[{datetime.now()}] 日志清理完成：{current_lines} → {MAX_LINES} 行，备份：{backup_file}.gz")
    
    # 清理 7 天前的备份
    now = time.time()
    for f in BACKUP_DIR.glob("*.gz"):
        if now - f.stat().st_mtime > 7 * 24 * 3600:
            f.unlink()
            print(f"[{datetime.now()}] 删除旧备份：{f.name}")

if __name__ == "__main__":
    print(f"[{datetime.now()}] 🕐 日志清理守护进程启动")
    print(f"   检查间隔：{CHECK_INTERVAL}秒")
    print(f"   最大行数：{MAX_LINES}")
    
    while True:
        try:
            cleanup()
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 清理失败：{e}")
        
        time.sleep(CHECK_INTERVAL)
