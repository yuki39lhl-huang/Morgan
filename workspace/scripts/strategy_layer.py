#!/usr/bin/env python3
"""
strategy_layer.py — 策略层

职责：信号评分（calc_score）、止盈止损计算（calc_tp_sl）与评分格式化。
依赖方向：仅依赖 config；情绪引擎由 main 通过 set_sentiment_engine() 注入。
"""
import logging

from config import get_config

CONFIG = get_config()
log = logging.getLogger(__name__)

# 情绪引擎引用（由 main 装配时注入，避免跨层 import）
_SENTIMENT_ENGINE = None


def set_sentiment_engine(engine) -> None:
    """注入情绪引擎实例（data_layer.SentimentEngine）供评分使用。"""
    global _SENTIMENT_ENGINE
    _SENTIMENT_ENGINE = engine


# ═══════════════════════════════════════════════════════════════
# 五、信号评分系统 v6.0
# ═══════════════════════════════════════════════════════════════
def calc_score(
    symbol: str,
    price_data: dict,
    ind: dict,
    regime: str,
    fg: int,
    funding_rate: float,
    btc_ind: dict,  # 2026-03-28 老公指示：用 BTC 作为大盘参考
) -> tuple[int, str]:
    """
    返回 (score, direction)
    direction: 'LONG' | 'SHORT' | 'NONE'
    """
    if not ind or not price_data:
        return 0, "NONE"

    price      = float(price_data.get("price") or ind.get("last_close") or 0)
    # 成交量：优先 15m K 线 vol_current（与 vol_avg20 同量纲），避免 WS 24h 量纲不一致
    vol        = float(ind.get("vol_current") or price_data.get("volume") or 0)
    vol_avg    = ind["vol_avg20"]
    atr_pct    = ind["atr_pct"]
    resistance = ind["resistance"]
    support    = ind["support"]

    long_score  = 0
    short_score = 0

    # ① 价格突破（40 分）
    if price >= resistance * 0.999:
        long_score += 40
    if price <= support * 1.001:
        short_score += 40

    # ② 成交量放大确认（30 分）
    if vol > vol_avg * 1.5:
        long_score  += 30
        short_score += 30
    elif vol > vol_avg * 1.2:
        long_score  += 15
        short_score += 15

    # ③ ATR 波动率适中（20 分）
    if 0.005 < atr_pct < 0.03:
        long_score  += 20
        short_score += 20

    # ④ BTC 大盘方向（10 分）- 2026-03-28 老公指示：用 BTC 代替 ETH
    _btc = btc_ind if btc_ind else {}
    if _btc:
        btc_bull = _btc.get("ema20_15m", 0) > _btc.get("ema60_15m", 0)
        if btc_bull:
            long_score  += 10
            short_score -= 5
        else:
            short_score += 10
            long_score  -= 5

    long_score  = max(0, long_score)
    short_score = max(0, short_score)

    # ⑤ 情绪加分（最多+15分）- 2026-06-09 老公指示：接入情绪引擎
    # 恐惧→做空顺势，贪婪→做多顺势；资金费率极端→反向
    if _SENTIMENT_ENGINE:
        long_score  += _SENTIMENT_ENGINE.sentiment_score(symbol, "LONG", fg, funding_rate)
        short_score += _SENTIMENT_ENGINE.sentiment_score(symbol, "SHORT", fg, funding_rate)

    threshold = CONFIG["score_threshold"][regime]
    # 单币阈值加成：低胜率币种提高门槛
    threshold += CONFIG["score_threshold"].get("per_symbol_bonus", {}).get(symbol, 0)
    if long_score > short_score and long_score >= threshold:
        return long_score, "LONG"
    elif short_score > long_score and short_score >= threshold:
        return short_score, "SHORT"

    return max(long_score, short_score), "NONE"


def calc_tp_sl(
    entry: float,
    direction: str,
    ind: dict,
    price_data: dict,
) -> dict:
    """
    返回 {tp1, sl}

    2026-06-03 老公指示：止损改为 ATR 动态 + 2% 保底
    - 止盈：+4%（TP1 全平 100%）
    - 止损：max(2%, ATR% × 分层倍数)，防止止损价被行情轻易打穿
      atr_sl_tiers: <2%ATR→1.0x, <4%→1.5x, ≥4%→2.0x
    - 无 TP2（已废弃）
    """
    TAKE_PROFIT_PCT = 0.04  # 保底 4%（2026-03-30 老公指示）
    TP_MAX_PCT      = 0.15  # 止盈上限 15%
    MIN_SL_PCT      = 0.02  # 保底 2%
    MAX_SL_PCT      = 0.15  # 2026-06-06: 止损上限 15%，防止极端 ATR 下止损失控

    # ── ATR 动态止盈止损 ──
    atr_pct = ind.get("atr_pct", 0) if ind else 0

    # 2026-06-06 老公指示：止盈 ATR 动态，崩盘时自动放宽
    tp_pct = TAKE_PROFIT_PCT
    if atr_pct > 0:
        tp_pct = max(TAKE_PROFIT_PCT, min(atr_pct * 2.0, TP_MAX_PCT))

    # ATR 动态止损距离
    atr_mult = 1.0  # 默认 1 倍
    if atr_pct > 0:
        atr_tiers = CONFIG.get("atr_sl_tiers", [(0.02, 1.0), (0.04, 1.5), (9999, 2.0)])
        for threshold, mult in atr_tiers:
            if atr_pct < threshold:
                atr_mult = mult
                break
    sl_pct = max(MIN_SL_PCT, min(atr_pct * atr_mult, MAX_SL_PCT))

    if direction == "LONG":
        tp1 = entry * (1 + tp_pct)
        sl  = entry * (1 - sl_pct)
    else:
        tp1 = entry * (1 - tp_pct)
        sl  = entry * (1 + sl_pct)

    return {
        "tp1_price": round(tp1, 4),
        "sl_price":  round(sl, 4),
        "peak_pnl":  0.0,        # 移动止盈用
        "tp_pct":    round(tp_pct, 4),  # 2026-06-06: 记录动态止盈比例
    }
