#!/usr/bin/env python3
"""
trade_features.py — 特征记录层（量化升级 Phase 1 / Phase 2.6 迁移至 SQLite）

职责：每笔开仓记录 5 维特征 + AI 观点，平仓时回填盈亏。
存储：trade_data.db（见 trade_db.py），取代此前的 trade_features.jsonl。

对外 API 保持不变（record_open / record_close / new_trade_id），
调用方 execution_layer 与 crypto_signal_monitor 无需改动。

单点回滚：本文件是唯一写入出口，恢复旧版（JSONL 实现）即可回退。
落盘失败一律记 ERROR 而非 WARNING —— 本项目历史上多次因静默失败丢数据
（15 天 0 样本、48h 超时失效），必须让故障可见。
"""
import logging
from datetime import datetime
from uuid import uuid4

import trade_db

log = logging.getLogger(__name__)


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
    """开仓成功后才调用，写入特征样本。

    score_raw / ai_score_adjust：Phase 2 归因字段（2026-08-25 新增）——
    记录数学原始分与 AI 一致性调整量（+5/-3/0），供 AB 对比精确归因
    「AI 加分单 vs 减分单 vs 无 AI 单」的盈亏差异。
    """
    try:
        trade_db.insert_trade(
            trade_id=trade_id, ts=datetime.now().isoformat(), symbol=symbol,
            direction=direction, entry_price=round(float(entry_price), 6) if entry_price else None,
            score=score, score_raw=score_raw, ai_score_adjust=ai_score_adj,
            regime=regime, features=features or {}, ai_result=ai_result or {},
        )
    except Exception as e:
        log.error(f"❌ trade 归因写入失败（trade_id={trade_id} {symbol}）：{e}")


def record_close(
    trade_id: str,
    pnl_usdt: float,
    pnl_pct: float,
    reason: str,
    symbol: str = None,
) -> None:
    """平仓成功后回填盈亏。

    trade_id 为空时用 symbol 回退匹配（2026-09-02 修复）：monitor 重启会从 API
    重建在场持仓导致 trade_id 丢失，但策略约束同 symbol 同时最多 1 仓，
    故「该 symbol 最近一笔未被 close 消费的 open」即为本笔平仓对应的开仓。
    2026-09-22 起重建路径已回填 trade_id（Phase 2.5），此处作为兜底保留。
    """
    tid = trade_id
    if not tid:
        tid = trade_db.resolve_open_by_symbol(symbol)
        if tid:
            log.info(f"♻️ {symbol} trade_id 丢失，按 symbol 回填为 {tid}")
        elif symbol:
            log.info(f"⚠️ {symbol} 平仓无匹配 open 记录（历史旧仓），跳过归因回填")
    if not tid:
        return
    try:
        # 同一 trade_id 的第二条 close（残余仓清理）降级为非主记录，
        # 避免污染归因统计（历史 18 例重复 close 曾导致口径二选一）
        is_primary = not trade_db.has_primary_close(tid)
        if not is_primary:
            log.info(f"ℹ️ {tid} 已有主平仓记录，本条按残余仓清理写入（{reason}）")
        trade_db.insert_close(
            trade_id=tid, ts=datetime.now().isoformat(),
            pnl_usdt=round(float(pnl_usdt), 4), pnl_pct=round(float(pnl_pct), 6),
            reason=str(reason), is_primary=is_primary,
        )
    except Exception as e:
        log.error(f"❌ 归因回填失败（trade_id={tid} {symbol}）：{e}")