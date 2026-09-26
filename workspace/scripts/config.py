#!/usr/bin/env python3
"""
统一配置模块 —— 所有脚本共用

配置来源（位于本脚本同目录）：
    config.json    非敏感运行参数（币种 / 阈值 / 路径 / URL / 调度）
    secrets.json   敏感凭证（飞书 / DeepSeek / CoinGecko API Key）

约定：
    - 一律通过 get_config() / CONFIG 代理读取，禁止业务代码硬编码密钥/端口
    - 相对路径字段自动基于 SCRIPT_DIR 解析为绝对路径
    - 配置文件变更后下次 get_config() 自动重载；CONFIG 代理每次访问都走 get_config()
    - 以下划线 "_" 开头的 key 仅作注释说明，不进入运行配置
    - ExitConfig / RiskConfig / AiTriggerConfig：结构化出口（Phase 2.9 S1）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent          # workspace/
OPENCLAW_ROOT = WORKSPACE_ROOT.parent       # .openclaw/
LOG_ROOT = OPENCLAW_ROOT / "logs"

CONFIG_FILE = SCRIPT_DIR / "config.json"
SECRETS_FILE = SCRIPT_DIR / "secrets.json"

# 相对路径字段（自动基于 SCRIPT_DIR 解析为绝对路径）
_PATH_KEYS = (
    "positions_file",
    "state_file",
    "llm_usage_file",
    "position_highs_file",
    "news_file",
    "analysis_file",
    "alert_file",
)

_CACHE: Optional[dict] = None
_MTIME: float = -1.0


def _file_mtime() -> float:
    try:
        return max(CONFIG_FILE.stat().st_mtime, SECRETS_FILE.stat().st_mtime)
    except OSError:
        return -1.0


def _load() -> dict:
    global _CACHE, _MTIME
    cfg: dict = {}
    for path in (CONFIG_FILE, SECRETS_FILE):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in data.items() if not k.startswith("_")})
        except Exception as e:
            print(f"[config] 加载 {path.name} 失败: {e}")
    for key in _PATH_KEYS:
        val = cfg.get(key)
        if isinstance(val, str) and val and not os.path.isabs(val):
            cfg[key] = str(SCRIPT_DIR / val)
    proxy = cfg.get("proxy")
    if isinstance(proxy, str) and proxy:
        cfg["proxies"] = {"http": proxy, "https": proxy}
    _CACHE = cfg
    _MTIME = _file_mtime()
    return cfg


def get_config() -> dict:
    """读取配置（文件变更后自动重载）。返回可变 dict（回测 override 可写）。"""
    global _CACHE, _MTIME
    mtime = _file_mtime()
    if _CACHE is None or mtime != _MTIME:
        _load()
    return _CACHE  # type: ignore[return-value]


class ConfigProxy:
    """
    模块级 CONFIG 热重载代理：CONFIG[k] 每次都走 get_config()，
    避免 `CONFIG = get_config()` 在 import 时固化旧引用。
    支持 update/__setitem__ 供 backtester 临时 override。
    """

    def __getitem__(self, key: str) -> Any:
        return get_config()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        get_config()[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return get_config().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in get_config()

    def keys(self):
        return get_config().keys()

    def items(self):
        return get_config().items()

    def values(self):
        return get_config().values()

    def __iter__(self) -> Iterator:
        return iter(get_config())

    def update(self, other=None, **kwargs) -> None:
        cfg = get_config()
        if other:
            cfg.update(other)
        if kwargs:
            cfg.update(kwargs)

    def copy(self) -> dict:
        return dict(get_config())


CONFIG = ConfigProxy()


# ─── 结构化配置（Phase 2.9 S1）────────────────────────────────

@dataclass(frozen=True)
class ExitConfig:
    base_tp_pct: float
    min_sl_pct: float
    max_sl_pct: float
    tp_atr_mult: float
    tp_max_pct: float
    atr_sl_tiers: tuple
    trailing_tiers: tuple
    timeout_enabled: bool
    timeout_hours: float
    atr_sl_cooldown_sec: int = 300
    atr_sl_min_change: float = 0.001


@dataclass(frozen=True)
class RiskConfig:
    total_capital: float
    position_size_pct: float
    max_positions: int
    leverage: int
    volatile_size_mult: float = 0.6
    circuit_breaker_enabled: bool = False
    circuit_breaker_pct: float = -0.10
    circuit_breaker_hours: float = 4.0
    consecutive_loss_limit: int = 3
    consecutive_loss_pause_hours: float = 2.0
    dust_notional_usdt: float = 5.0


@dataclass(frozen=True)
class AiTriggerConfig:
    """开仓前 AI 调用/观望阈值（原 monitor 硬编码）。"""
    loss_pnl_pct: float = -0.02
    loss_cooldown_sec: int = 7200
    conflict_confidence: float = 0.7


def get_exit_config() -> ExitConfig:
    c = get_config()
    te = c.get("timeout_exit") or {}
    tiers = c.get("atr_sl_tiers") or [[0.02, 2.0], [0.04, 2.0], [9999, 2.5]]
    trailing = c.get("trailing_tiers") or []
    return ExitConfig(
        base_tp_pct=float(c.get("base_tp_pct", 0.04)),
        min_sl_pct=float(c.get("min_sl_pct", 0.02)),
        max_sl_pct=float(c.get("max_sl_pct", 0.15)),
        tp_atr_mult=float(c.get("tp_atr_mult", 2.0)),
        tp_max_pct=float(c.get("tp_max_pct", 0.15)),
        atr_sl_tiers=tuple(tuple(x) for x in tiers),
        trailing_tiers=tuple(trailing),
        timeout_enabled=bool(te.get("enabled", False)),
        timeout_hours=float(te.get("hours", 48)),
        atr_sl_cooldown_sec=int(c.get("atr_sl_cooldown_sec", 300)),
        atr_sl_min_change=float(c.get("atr_sl_min_change", 0.001)),
    )


def get_risk_config() -> RiskConfig:
    c = get_config()
    return RiskConfig(
        total_capital=float(c.get("total_capital", 100)),
        position_size_pct=float(c.get("position_size_pct", 0.20)),
        max_positions=int(c.get("max_positions", 4)),
        leverage=int(c.get("leverage", 10)),
        volatile_size_mult=float(c.get("volatile_size_mult", 0.6)),
        circuit_breaker_enabled=bool(c.get("circuit_breaker_enabled", False)),
        circuit_breaker_pct=float(c.get("circuit_breaker_pct", -0.10)),
        circuit_breaker_hours=float(c.get("circuit_breaker_hours", 4)),
        consecutive_loss_limit=int(c.get("consecutive_loss_limit", 3)),
        consecutive_loss_pause_hours=float(c.get("consecutive_loss_pause_hours", 2)),
        dust_notional_usdt=float(c.get("dust_notional_usdt", 5.0)),
    )


def get_ai_trigger_config() -> AiTriggerConfig:
    c = get_config()
    raw = c.get("ai_trigger") or {}
    return AiTriggerConfig(
        loss_pnl_pct=float(raw.get("loss_pnl_pct", -0.02)),
        loss_cooldown_sec=int(raw.get("loss_cooldown_sec", 7200)),
        conflict_confidence=float(raw.get("conflict_confidence", 0.7)),
    )


def feishu() -> dict:
    """飞书相关凭证/目标（已合并 secrets.json）。"""
    cfg = get_config()
    return {
        "app_id": cfg.get("feishu_app_id", ""),
        "app_secret": cfg.get("feishu_app_secret", ""),
        "chat_id": cfg.get("feishu_chat_id", ""),
        "open_id": cfg.get("feishu_open_id", ""),
    }
