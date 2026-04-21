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
LOG_FILE = SCRIPT_PATH.parent / "watchdog.log"
PID_FILE = "/tmp/crypto_monitor.pid"
LOCK_FILE = "/tmp/crypto_monitor.lock"
CHECK_INTERVAL = 10  # 10 秒检查一次
STARTUP_WAIT = 15  # kj 启动后等待 15 秒

def log(message):
    """写日志"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_line + "\n")
    except:
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

def main():
    log("=" * 60)
    log("🐕 加密货币监控看门狗启动（智能版）")
    log(f"监控目标：{SCRIPT_NAME}")
    log(f"检查间隔：{CHECK_INTERVAL}秒")
    log("⚠️  检测到进程未运行时自动启动（带 PID 文件检查）")
    log("=" * 60)
    
    consecutive_failures = 0
    max_failures = 3
    last_start_time = 0
    startup_cooldown = 60  # 启动后 60 秒内不再检查
    
    while True:
        # 每小时清理一次日志
        cleanup_old_logs()
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
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            log("👋 看门狗停止")
            break
        except Exception as e:
            log(f"❌ 看门狗异常：{e}")
            time.sleep(CHECK_INTERVAL)

def cleanup_old_logs(max_size_mb=20, max_age_days=7):
    """清理旧日志（保留最近 10MB，最多 7 天）"""
    import time
    log_file = Path("/root/.openclaw/workspace/scripts/crypto_monitor.log")
    
    if not log_file.exists():
        return
    
    try:
        # 检查文件大小
        size_mb = log_file.stat().st_size / (1024 * 1024)
        
        if size_mb > max_size_mb:
            # 文件太大，清空（保留最后 1000 行）
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            if len(lines) > 1000:
                with open(log_file, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-1000:])
                log.info(f"🧹 日志清理：保留最后 1000 行（原 {len(lines)} 行）")
            else:
                log_file.write_text("")
                log.info(f"🧹 日志清理：文件过大，已清空")
        
        # 检查文件年龄
        mtime = log_file.stat().st_mtime
        age_days = (time.time() - mtime) / (24 * 3600)
        
        # 如果超过 7 天没更新，备份并清空
        if age_days > max_age_days:
            backup = log_file.with_suffix(f".log.{int(mtime)}")
            log_file.rename(backup)
            log.info(f"🧹 日志清理：备份旧日志到 {backup.name}")
    
    except Exception as e:
        log.error(f"⚠️ 日志清理失败：{e}")


if __name__ == "__main__":
    main()
