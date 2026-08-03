#!/usr/bin/env python3
"""
新闻抓取守护进程
⏰ 每小时自动抓取新闻分析
"""

import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from openclaw_logging import append_log, current_log_path

LOG_FILE = current_log_path("news")
INTERVAL = config.get_config().get("news_interval", 3600)  # 抓取间隔（config.json）

# 抓取脚本路径基于本文件目录推导，避免硬编码绝对路径
FETCHER_SCRIPT = Path(__file__).resolve().parent / "crypto_news_fetcher.py"

def get_beijing_time():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))

def log(msg):
    timestamp = get_beijing_time().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    append_log("news", log_msg)

def run_fetcher():
    """运行新闻抓取脚本"""
    try:
        log("🚀 开始抓取新闻...")
        result = subprocess.run(
            ['python3', str(FETCHER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            log("✅ 新闻抓取成功")
        else:
            log(f"❌ 抓取失败：{result.stderr}")
    except Exception as e:
        log(f"❌ 错误：{e}")

def main():
    log("="*60)
    log("📰 新闻抓取守护进程启动")
    log(f"抓取间隔：{INTERVAL}秒 (1 小时)")
    log("="*60)
    
    # 启动时立即运行一次
    run_fetcher()
    
    while True:
        time.sleep(INTERVAL)
        run_fetcher()

if __name__ == "__main__":
    main()
