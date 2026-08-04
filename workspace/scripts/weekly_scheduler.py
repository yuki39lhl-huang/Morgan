#!/usr/bin/env python3
"""
weekly_scheduler.py — 量化周报定时调度（Phase 0.5 S3 验证）

常驻进程，每 60 秒检查一次北京时间，到「每周 weekday 的 hour:minute」触发 weekly_report 推送。
替代 crontab（当前环境无 cron），模式与 reminder_scheduler.py 一致（由 kj.sh 统一拉起）。

解耦设计：
  - 调度只负责「到点触发」：调用 weekly_report.main()（装配层）
  - 触发时间等参数外置 config.json → weekly_report 段（weekday/hour/minute）
  - 防重复：发送成功后记录「本周已发」到状态文件（LOG_ROOT/weekly_report_state.json）
  - 日志写 LOG_ROOT/scheduler.log（与 reminder_scheduler 共用）

用法：
  python3 weekly_scheduler.py          # 常驻调度
  python3 weekly_scheduler.py --now    # 立即触发一次（验证用），不进入常驻循环
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import weekly_report

STATE_FILE = config.LOG_ROOT / "weekly_report_state.json"
LOG_FILE = config.LOG_ROOT / "scheduler.log"


def bj_now() -> datetime:
    """北京时间（时区在 config 环境固定 UTC+8，避免依赖系统时区）。"""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))


def log(msg: str) -> None:
    ts = bj_now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass
    print(f"[{ts}] {msg}")


def load_sent_week() -> str:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("sent_week", "")
    except (OSError, ValueError):
        return ""


def save_sent_week(week_key: str) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"sent_week": week_key, "ts": bj_now().isoformat()}, f)
    except OSError as e:
        log(f"⚠️ 状态写入失败: {e}")


def week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def main() -> int:
    if "--now" in sys.argv:
        log("📊 --now 手动触发周报")
        try:
            weekly_report.main()
        except Exception as e:
            log(f"❌ 周报触发失败: {e}")
            return 1
        return 0

    cfg = config.get_config().get("weekly_report", {})
    if not cfg.get("enabled", True):
        log("⏸ 周报未启用（config.json weekly_report.enabled=false），退出")
        return 0
    weekday = int(cfg.get("weekday", 0))    # 0=周一
    hour = int(cfg.get("hour", 9))
    minute = int(cfg.get("minute", 0))

    log(f"🚀 周报调度器启动：每周{'一二三四五六日'[weekday]} {hour:02d}:{minute:02d}（{week_key(bj_now())}）")
    while True:
        now = bj_now()
        if now.weekday() == weekday and now.hour == hour and now.minute == minute:
            wk = week_key(now)
            if load_sent_week() != wk:
                log("📊 到点触发量化周报推送")
                try:
                    weekly_report.main()
                    save_sent_week(wk)
                    log(f"✅ 周报完成并记录（{wk}）")
                except Exception as e:
                    log(f"❌ 周报触发失败: {e}")
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
