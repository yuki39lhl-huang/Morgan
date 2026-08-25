#!/usr/bin/env python3
"""
trade_features.py — 特征记录层（量化升级 Phase 1）

职责：每笔开仓记录 5 维特征 + AI 观点（open 事件），平仓时回填盈亏（close 事件）。
数据落盘 scripts/trade_features.jsonl（与 ai_scan.jsonl 同目录，路径用 __file__ 派生），
供 analyze_features.py 生成特征归因周报。

事件格式（每行一个 JSON，追加写）：
  open:  {"event":"open",  "ts","trade_id","symbol","direction","score","regime",
          "features":{"breakout","volume_ratio","atr_pct","btc_bull","sentiment"},
          "ai":{"direction","confidence"}, "entry_price"}
  close: {"event":"close", "ts","trade_id","pnl_usdt","pnl_pct","reason"}

依赖方向：无业务依赖（基础设施/记录层），execution_layer 与 main 均可调用。
"""
import json
import logging
import os
from datetime import datetime
from uuid import uuid4

log = logging.getLogger(__name__)

FEATURES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_features.jsonl")


def _append(line: dict) -> None:
    try:
        with open(FEATURES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"⚠️ trade_features 落盘失败: {e}")


def new_trade_id(symbol: str) -> str:
    """生成唯一交易标识：时间戳-币种-随机后缀（平仓回填用）。"""
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{symbol}-{uuid4().hex[:6]}"


def record_open(
    trade_id: str,
    symbol: str,
    direction: str,
    score: int,
    regime: str,
    features: dict,
    ai_result: dict,
    entry_price: float,
    score_raw: int = None,
    ai_score_adj: int = 0,
) -> None:
    """开仓成功后才调用，写入特征样本（open 事件）。

    score_raw / ai_score_adj：Phase 2 归因字段（2026-08-25 新增）——
    记录数学原始分与 AI 一致性调整量（+5/-3/0），供 AB 对比精确归因
    「AI 加分单 vs 减分单 vs 无 AI 单」的盈亏差异。
    """
    _append({
        "event": "open",
        "ts": datetime.now().isoformat(),
        "trade_id": trade_id,
        "symbol": symbol,
        "direction": direction,
        "score": score,
        "score_raw": score_raw,
        "ai_score_adjust": ai_score_adj,
        "regime": regime,
        "features": features or {},
        "ai": ai_result,
        "entry_price": round(float(entry_price), 6) if entry_price else None,
    })


def record_close(trade_id: str, pnl_usdt: float, pnl_pct: float, reason: str) -> None:
    """平仓成功后回填盈亏（close 事件）；无 trade_id（旧持仓/修复仓）则跳过。"""
    if not trade_id:
        return
    _append({
        "event": "close",
        "ts": datetime.now().isoformat(),
        "trade_id": trade_id,
        "pnl_usdt": round(float(pnl_usdt), 4),
        "pnl_pct": round(float(pnl_pct), 6),
        "reason": str(reason),
    })
