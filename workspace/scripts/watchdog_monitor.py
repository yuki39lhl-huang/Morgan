#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看门狗监控 - 自动重启交易监控进程
- 每 30 秒检查 crypto_signal_monitor.py 是否运行
- 进程不存在时自动重启
- 记录重启历史
"""

import subprocess
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/root/.openclaw/workspace/scripts/watchdog_monitor.log")
MONITOR_SCRIPT = "/root/.openclaw/workspace/scripts/crypto_signal_monitor.py"
MONITOR_NAME = "crypto_signal_monitor.py"
CHECK_INTERVAL = 30  # 秒

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_line + "\n")
    except:
        pass

def check_process():
    """检查监控进程是否运行"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', MONITOR_NAME],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except:
        return False

def restart_monitor():
    """重启交易监控进程"""
    log("🔄 检测到监控进程已退出，开始重启...")
    
    try:
        # 切换到脚本目录
        script_dir = Path(MONITOR_SCRIPT).parent
        
        # 启动新进程
        cmd = f"cd {script_dir} && nohup python3 -u {MONITOR_SCRIPT} > crypto_monitor_stdout.log 2>&1 &"
        subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
        
        # 等待启动
        time.sleep(5)
        
        # 验证是否启动成功
        if check_process():
            pid = subprocess.run(['pgrep', '-f', MONITOR_NAME], 
                               capture_output=True, text=True).stdout.strip()
            log(f"✅ 监控进程重启成功（PID: {pid}）")
            return True
        else:
            log("❌ 监控进程重启失败：进程未启动")
            return False
            
    except Exception as e:
        log(f"❌ 重启异常：{e}")
        return False

def main():
    log("=" * 60)
    log("🐕 看门狗监控启动")
    log(f"监控目标：{MONITOR_NAME}")
    log(f"检查间隔：{CHECK_INTERVAL}秒")
    log("=" * 60)
    
    restart_count = 0
    
    while True:
        try:
            is_running = check_process()
            
            if is_running:
                log("💓 监控进程正常运行")
            else:
                restart_count += 1
                log(f"⚠️ 监控进程已退出（第{restart_count}次重启）")
                
                success = restart_monitor()
                
                if not success:
                    log("🚨 重启失败，5 秒后重试...")
                    time.sleep(5)
                    restart_monitor()
            
        except Exception as e:
            log(f"❌ 检查异常：{e}")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
