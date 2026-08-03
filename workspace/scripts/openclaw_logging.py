#!/usr/bin/env python3
"""
OpenClaw 统一日志目录与自动归档。

目录结构（根：/root/.openclaw/logs/）：
    trading/          策略、开平仓、AI、Binance 下单
    watchdog/         看门狗
    news/             新闻守护
    clash/            Mihomo 代理
    alerts/           整点汇报文本备份（按日分文件）
    gateway/          说明：Gateway 仍由 OpenClaw 写在 /tmp/openclaw/

归档规则：
    - 单文件超过 max_mb → 轮转，旧文件移入各分类下的 archive/
    - 归档文件名：{分类}_{YYYY-MM-DD_HH-MM-SS}.log
    - 首次启用时会把旧路径的大日志迁移到 archive/legacy_*.log

按天清理（与按大小轮转并存，每日最多执行一次）：
    - trading/archive/     保留 30 天
    - alerts/hourly_*.log  保留 60 天
    - watchdog/news/clash/archive/  保留 14 天
"""

from __future__ import annotations

import logging
import logging.handlers
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

LOG_ROOT = config.LOG_ROOT

# 分类 → 当前日志文件名、单文件上限(MB)、保留归档份数
LOG_CATEGORIES: dict[str, dict] = {
    "trading": {"filename": "monitor.log", "max_mb": 10, "backups": 30},
    "watchdog": {"filename": "watchdog.log", "max_mb": 5, "backups": 14},
    "news": {"filename": "news.log", "max_mb": 5, "backups": 14},
    "clash": {"filename": "clash.log", "max_mb": 5, "backups": 7},
}

# 按天保留（仅清理 archive/ 与 alerts/hourly_*.log，不删当前活跃日志）
RETENTION_DAYS: dict[str, int] = {
    "trading_archive": 30,
    "alerts": 60,
    "other_archive": 14,  # watchdog / news / clash
}
_OTHER_ARCHIVE_CATEGORIES = ("watchdog", "news", "clash")

_PURGE_MARKER = LOG_ROOT / ".last_retention_purge"
_purged_today = False

# 旧路径 → 新分类（一次性迁移，路径基于 config 推导）
LEGACY_LOG_MIGRATIONS: list[tuple[Path, str]] = [
    (config.WORKSPACE_ROOT / "crypto_monitor.log", "trading"),
    (config.SCRIPT_DIR / "crypto_monitor.log", "trading"),
    (config.SCRIPT_DIR / "watchdog.log", "watchdog"),
    (config.SCRIPT_DIR / "news_fetcher_daemon.log", "news"),
    (config.SCRIPT_DIR / "clash.log", "clash"),
]

_migrated = False


def ensure_log_dirs() -> None:
    for cat in LOG_CATEGORIES:
        (LOG_ROOT / cat).mkdir(parents=True, exist_ok=True)
        (LOG_ROOT / cat / "archive").mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / "alerts").mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / "gateway").mkdir(parents=True, exist_ok=True)
    purge_old_logs_by_age()


def _file_age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400.0


def _should_run_daily_purge(force: bool) -> bool:
    global _purged_today
    if force:
        return True
    if _purged_today:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        if _PURGE_MARKER.read_text(encoding="utf-8").strip() == today:
            _purged_today = True
            return False
    except OSError:
        pass
    return True


def _unlink_if_older(path: Path, max_days: int, removed: list[str]) -> None:
    if not path.is_file():
        return
    try:
        if _file_age_days(path) <= max_days:
            return
        path.unlink()
        removed.append(str(path))
    except OSError:
        pass


def purge_old_logs_by_age(force: bool = False) -> dict[str, int]:
    """
    删除超过保留天数的归档与告警日志。
    返回 {"trading": n, "alerts": n, "other": n} 各类删除数量。
    """
    global _purged_today
    if not _should_run_daily_purge(force):
        return {"trading": 0, "alerts": 0, "other": 0}

    counts = {"trading": 0, "alerts": 0, "other": 0}
    removed: list[str] = []

    trading_dir = LOG_ROOT / "trading" / "archive"
    if trading_dir.is_dir():
        for p in trading_dir.iterdir():
            before = len(removed)
            _unlink_if_older(p, RETENTION_DAYS["trading_archive"], removed)
            if len(removed) > before:
                counts["trading"] += 1

    alerts_dir = LOG_ROOT / "alerts"
    if alerts_dir.is_dir():
        for p in alerts_dir.glob("hourly_*.log"):
            before = len(removed)
            _unlink_if_older(p, RETENTION_DAYS["alerts"], removed)
            if len(removed) > before:
                counts["alerts"] += 1

    for cat in _OTHER_ARCHIVE_CATEGORIES:
        archive_dir = LOG_ROOT / cat / "archive"
        if not archive_dir.is_dir():
            continue
        for p in archive_dir.iterdir():
            before = len(removed)
            _unlink_if_older(p, RETENTION_DAYS["other_archive"], removed)
            if len(removed) > before:
                counts["other"] += 1

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        _PURGE_MARKER.write_text(today + "\n", encoding="utf-8")
    except OSError:
        pass
    _purged_today = True

    if any(counts.values()):
        log = logging.getLogger(__name__)
        if log.handlers:
            log.info(
                "日志按天清理: trading=%d alerts=%d other=%d",
                counts["trading"],
                counts["alerts"],
                counts["other"],
            )
    return counts


def current_log_path(category: str) -> Path:
    if category not in LOG_CATEGORIES:
        raise ValueError(f"未知日志分类: {category}")
    ensure_log_dirs()
    return LOG_ROOT / category / LOG_CATEGORIES[category]["filename"]


def _archive_name(category: str, default_name: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    archive_dir = LOG_ROOT / category / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    return str(archive_dir / f"{category}_{ts}.log")


def make_rotating_handler(category: str) -> logging.Handler:
    cfg = LOG_CATEGORIES[category]
    path = current_log_path(category)
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=int(cfg["max_mb"] * 1024 * 1024),
        backupCount=int(cfg["backups"]),
        encoding="utf-8",
    )
    handler.namer = lambda _default: _archive_name(category, _default)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    return handler


def setup_logger(
    name: str,
    category: str = "trading",
    level: int = logging.INFO,
) -> logging.Logger:
    """为模块配置带归档的 FileHandler（不重复添加 handler）。"""
    ensure_log_dirs()
    migrate_legacy_logs_once()
    logger = logging.getLogger(name)
    logger.setLevel(level)
    marker = f"openclaw_{category}"
    if any(getattr(h, "_openclaw_category", None) == marker for h in logger.handlers):
        return logger
    handler = make_rotating_handler(category)
    handler._openclaw_category = marker  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def configure_root_logging(category: str = "trading", level: int = logging.INFO) -> Path:
    """替换 root/basicConfig 场景：清空旧 handler 后只写归档日志。"""
    ensure_log_dirs()
    migrate_legacy_logs_once()
    path = current_log_path(category)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(make_rotating_handler(category))
    return path


def append_log(category: str, line: str) -> Path:
    """非 logging 模块用的追加写（看门狗、新闻守护等）。"""
    path = current_log_path(category)  # 内含 ensure_log_dirs + 当日清理
    migrate_legacy_logs_once()
    maybe_rotate_by_size(category, path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    return path


def maybe_rotate_by_size(category: str, path: Path) -> None:
    """手动 append 前检查大小，超限则归档（与 RotatingFileHandler 规则一致）。"""
    cfg = LOG_CATEGORIES.get(category)
    if not cfg or not path.exists():
        return
    limit = int(cfg["max_mb"] * 1024 * 1024)
    if path.stat().st_size < limit:
        return
    dest = Path(_archive_name(category, str(path)))
    shutil.move(str(path), str(dest))
    path.touch()


def daily_alert_path(day: Optional[datetime] = None) -> Path:
    """整点汇报 / 告警按日存储，便于分析。"""
    ensure_log_dirs()
    d = (day or datetime.now()).strftime("%Y-%m-%d")
    return LOG_ROOT / "alerts" / f"hourly_{d}.log"


def migrate_legacy_logs_once() -> None:
    global _migrated
    if _migrated:
        return
    _migrated = True
    ensure_log_dirs()
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    for old_path, category in LEGACY_LOG_MIGRATIONS:
        if not old_path.exists() or old_path.stat().st_size == 0:
            continue
        dest = LOG_ROOT / category / "archive" / f"legacy_{category}_{ts}_{old_path.name}"
        try:
            if dest.exists():
                continue
            shutil.move(str(old_path), str(dest))
        except OSError:
            try:
                shutil.copy2(str(old_path), str(dest))
            except OSError:
                pass


def trading_log_path() -> Path:
    migrate_legacy_logs_once()
    return current_log_path("trading")


if __name__ == "__main__":
    for cat in LOG_CATEGORIES:
        (LOG_ROOT / cat).mkdir(parents=True, exist_ok=True)
        (LOG_ROOT / cat / "archive").mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / "alerts").mkdir(parents=True, exist_ok=True)
    (LOG_ROOT / "gateway").mkdir(parents=True, exist_ok=True)
    result = purge_old_logs_by_age(force=True)
    print(f"按天清理完成: {result}")
