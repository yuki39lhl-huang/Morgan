#!/usr/bin/env python3
"""
position_store.py — 持仓状态与持久化工具层

职责：持仓数量归一、读写持仓文件、读写状态文件。
依赖方向：各 layer 可依赖本模块；本模块仅依赖 config / openclaw_logging。
禁止反向依赖（本模块不得 import 其他 layer）。
"""
import json
import logging

from config import CONFIG, get_config

log = logging.getLogger(__name__)


def position_amount(pos: dict) -> float:
    """持仓数量（兼容 qty / amount，API 同步只写 qty 时整点汇报曾显示 0 盈亏）"""
    for key in ("amount", "qty"):
        try:
            v = abs(float(pos.get(key) or 0))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def normalize_position(pos: dict) -> dict:
    """统一 amount/qty，避免整点汇报盈亏为 0"""
    amt = position_amount(pos)
    if amt > 0:
        if pos.get("amount") != amt or pos.get("qty") != amt:
            sym = pos.get("symbol", "?")
            log.info(f"🔧 持仓 {sym} 数量归一：qty={pos.get('qty')} amount={pos.get('amount')} → {amt}")
        pos["amount"] = amt
        pos["qty"] = amt
    return pos


def load_positions() -> list:
    """加载持仓并补全缺失字段（兼容旧版本）"""
    try:
        with open(CONFIG["positions_file"]) as f:
            positions = json.load(f)

        # 补全缺失字段（兼容旧版本持仓文件）
        for pos in positions:
            if "peak_pnl" not in pos:
                pos["peak_pnl"] = 0.0
            if "high_24h" not in pos:
                pos["high_24h"] = 0.0
            if "low_24h" not in pos:
                pos["low_24h"] = 0.0
            normalize_position(pos)
            # 2026-03-28 老公指示：已删除 tp1_hit, size_remaining

        return positions
    except Exception as e:
        log.warning(f"⚠️ 加载持仓失败：{e}")
        return []


def save_positions(positions: list):
    for pos in positions:
        normalize_position(pos)
    with open(CONFIG["positions_file"], "w") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)


def save_state(state: dict):
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)
