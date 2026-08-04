#!/usr/bin/env python3
"""
统一配置模块 —— 所有脚本共用

配置来源（位于本脚本同目录）：
    config.json    非敏感运行参数（币种 / 阈值 / 路径 / URL / 调度）
    secrets.json   敏感凭证（飞书 / DeepSeek / CoinGecko API Key）

约定：
    - 一律通过 get_config() 读取，禁止在业务代码里硬编码绝对路径 / 密钥 / 端口
    - 相对路径字段自动基于 SCRIPT_DIR 解析为绝对路径
    - 配置文件变更后下次 get_config() 自动重载（长驻进程可定期调用）
    - 以下划线 "_" 开头的 key 仅作注释说明，不进入运行配置
"""
import json
import os
import time
from pathlib import Path

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

_CACHE: dict = None
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
    # 相对路径 -> 绝对路径
    for key in _PATH_KEYS:
        val = cfg.get(key)
        if isinstance(val, str) and val and not os.path.isabs(val):
            cfg[key] = str(SCRIPT_DIR / val)
    # 代理规范化：proxy(字符串, aiohttp/ws 用) + proxies(dict, requests 用)
    proxy = cfg.get("proxy")
    if isinstance(proxy, str) and proxy:
        cfg["proxies"] = {"http": proxy, "https": proxy}
    _CACHE = cfg
    _MTIME = _file_mtime()
    return cfg


def get_config() -> dict:
    """读取配置（文件变更后自动重载）。"""
    global _CACHE, _MTIME
    mtime = _file_mtime()
    if _CACHE is None or mtime != _MTIME:
        _load()
    return _CACHE


def feishu() -> dict:
    """飞书相关凭证/目标（已合并 secrets.json）。"""
    cfg = get_config()
    return {
        "app_id": cfg.get("feishu_app_id", ""),
        "app_secret": cfg.get("feishu_app_secret", ""),
        "chat_id": cfg.get("feishu_chat_id", ""),
        "open_id": cfg.get("feishu_open_id", ""),
    }
