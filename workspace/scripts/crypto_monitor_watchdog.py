#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
加密货币监控脚本看门狗（智能版）
- 检测到进程未运行时自动启动
- 通过 PID 文件检查是否是 kj 正在启动
- 避免重复启动导致的双进程问题
"""

import subprocess
import time
from datetime import datetime
from pathlib import Path
import os

SCRIPT_NAME = "crypto_signal_monitor.py"
SCRIPT_PATH = Path("/root/.openclaw/workspace/scripts") / SCRIPT_NAME
REMINDER_NAME = "reminder_scheduler.py"
REMINDER_PATH = Path("/root/.openclaw/workspace/scripts") / REMINDER_NAME
from openclaw_logging import append_log, current_log_path, trading_log_path

LOG_FILE = current_log_path("watchdog")
PID_FILE = "/tmp/crypto_monitor.pid"
LOCK_FILE = "/tmp/crypto_monitor.lock"
CHECK_INTERVAL = 10  # 10 秒检查一次
STARTUP_WAIT = 15  # kj 启动后等待 15 秒
LOG_MAINTENANCE_INTERVAL = 3600  # 日志维护提示间隔（与旧版「每小时清理」一致）
_last_log_maintenance = 0.0

def log(message):
    """写日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    try:
        append_log("watchdog", log_line)
    except Exception:
        pass

def check_process():
    """检查主脚本是否运行"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', SCRIPT_NAME],
            capture_output=True,
            text=True
        )
        pids = result.stdout.strip()
        return bool(pids), pids
    except:
        return False, ""

def check_reminder():
    """检查提醒调度器是否运行"""
    try:
        result = subprocess.run(
            ['pgrep', '-f', REMINDER_NAME],
            capture_output=True,
            text=True
        )
        pids = result.stdout.strip()
        return bool(pids), pids
    except:
        return False, ""

def check_kj_is_starting():
    """检查 kj 是否正在启动（通过锁文件判断）"""
    if os.path.exists(LOCK_FILE):
        try:
            # 尝试获取锁，如果失败说明 kj 正在运行
            import fcntl
            with open(LOCK_FILE) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False  # 能获取锁，说明 kj 没在运行
        except:
            return True  # 不能获取锁，说明 kj 正在运行
    return False

def start_script():
    """启动主脚本"""
    try:
        log(f"🚀 启动主脚本 {SCRIPT_NAME}...")
        subprocess.Popen(
            ['python3', str(SCRIPT_PATH)],
            cwd=str(SCRIPT_PATH.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(3)  # 等待启动
        
        is_running, pids = check_process()
        if is_running:
            pid = pids.split()[0]
            log(f"✅ 主脚本启动成功 (PID: {pid})")
            
            # 写入 PID 文件
            try:
                with open(PID_FILE, 'w') as f:
                    f.write(pid)
                log(f"📝 PID 文件已更新：{PID_FILE}")
            except Exception as e:
                log(f"⚠️  写入 PID 文件失败：{e}")
            
            return True
        else:
            log("❌ 主脚本启动失败")
            return False
    except Exception as e:
        log(f"❌ 启动异常：{e}")
        return False

def start_reminder():
    """启动提醒调度器"""
    try:
        log(f"🚀 启动提醒调度器 {REMINDER_NAME}...")
        subprocess.Popen(
            ['python3', str(REMINDER_PATH)],
            cwd=str(REMINDER_PATH.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(2)
        
        is_running, pids = check_reminder()
        if is_running:
            pid = pids.split()[0]
            log(f"✅ 提醒调度器启动成功 (PID: {pid})")
            return True
        else:
            log("❌ 提醒调度器启动失败")
            return False
    except Exception as e:
        log(f"❌ 提醒调度器启动异常：{e}")
        return False

def main():
    log("=" * 60)
    log("🐕 加密货币监控看门狗启动（智能版）")
    log(f"监控目标：{SCRIPT_NAME}")
    log(f"提醒目标：{REMINDER_NAME}")
    log(f"检查间隔：{CHECK_INTERVAL}秒")
    log("⚠️  检测到进程未运行时自动启动（带 PID 文件检查）")
    log("=" * 60)
    
    consecutive_failures = 0
    max_failures = 3
    last_start_time = 0
    startup_cooldown = 60  # 启动后 60 秒内不再检查
    
    reminder_consecutive_failures = 0
    reminder_last_start_time = 0
    reminder_max_failures = 3
    
    while True:
        # 旧版在此「截断 monitor 日志为 1000 行」已停用，改由 openclaw_logging 按大小归档
        maybe_log_maintenance()
        try:
            is_running, pids = check_process()
            kj_running = check_kj_is_starting()
            
            if is_running:
                consecutive_failures = 0
                pid = pids.split()[0] if pids else "?"
                log(f"💓 心跳正常 (PID: {pid})")
                
                # 检查 PID 文件是否同步
                if os.path.exists(PID_FILE):
                    try:
                        with open(PID_FILE) as f:
                            file_pid = f.read().strip()
                        if file_pid != pid:
                            log(f"📝 同步 PID 文件：{file_pid} → {pid}")
                            with open(PID_FILE, 'w') as f:
                                f.write(pid)
                    except Exception as e:
                        log(f"⚠️  检查 PID 文件失败：{e}")
                else:
                    log(f"⚠️  PID 文件不存在，创建中...")
                    try:
                        with open(PID_FILE, 'w') as f:
                            f.write(pid)
                    except Exception as e:
                        log(f"⚠️  创建 PID 文件失败：{e}")
            else:
                # 进程未运行
                current_time = time.time()
                
                # 检查是否是 kj 正在启动
                if kj_running:
                    log(f"⏳ kj 正在启动，等待...")
                    time.sleep(STARTUP_WAIT)
                    continue
                
                # 检查是否在冷却期
                if current_time - last_start_time < startup_cooldown:
                    log(f"⏳ 刚启动过，等待冷却...")
                    time.sleep(CHECK_INTERVAL)
                    continue
                
                # 尝试自动启动
                log(f"🚨 主脚本未运行！尝试自动启动...")
                success = start_script()
                
                if success:
                    last_start_time = current_time
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    log(f"⚠️  连续启动失败 {consecutive_failures}/{max_failures} 次")
                    
                    if consecutive_failures >= max_failures:
                        log(f"🚨 连续失败 {max_failures} 次，请使用 kj 命令手动重启")
                        consecutive_failures = 0  # 重置，继续尝试
            
            # ── 提醒调度器监控 ──
            reminder_running, reminder_pids = check_reminder()
            if not reminder_running:
                rtime = time.time()
                if not kj_running and rtime - reminder_last_start_time >= startup_cooldown:
                    log(f"🚨 提醒调度器未运行！尝试自动启动...")
                    if start_reminder():
                        reminder_last_start_time = rtime
                        reminder_consecutive_failures = 0
                    else:
                        reminder_consecutive_failures += 1
                        log(f"⚠️  提醒调度器连续启动失败 {reminder_consecutive_failures}/{reminder_max_failures} 次")
                        if reminder_consecutive_failures >= reminder_max_failures:
                            log(f"🚨 提醒调度器连续失败 {reminder_max_failures} 次，请使用 kj 命令手动重启")
                            reminder_consecutive_failures = 0
            else:
                reminder_consecutive_failures = 0
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            log("👋 看门狗停止")
            break
        except Exception as e:
            log(f"❌ 看门狗异常：{e}")
            time.sleep(CHECK_INTERVAL)

def maybe_log_maintenance():
    """每小时提示一次日志路径（不再截断/清空交易日志，避免与 RotatingFileHandler 冲突）。"""
    global _last_log_maintenance
    now = time.time()
    if now - _last_log_maintenance < LOG_MAINTENANCE_INTERVAL:
        return
    _last_log_maintenance = now
    tlog = trading_log_path()
    if tlog.exists():
        size_mb = tlog.stat().st_size / (1024 * 1024)
        log(f"📁 交易日志 {tlog} ({size_mb:.1f}MB)，>10MB 时自动归档至 logs/trading/archive/")


if __name__ == "__main__":
    main()
