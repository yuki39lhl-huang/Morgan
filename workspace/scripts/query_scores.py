#!/usr/bin/env python3
"""一次性查询：当前所有币种的评分与方向（复用 monitor 同款逻辑，只读不改仓）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import get_config
from data_layer import IndicatorEngine, SentimentEngine, detect_regime
from strategy_layer import calc_score, set_sentiment_engine, _SENTIMENT_ENGINE

CONFIG = get_config()
symbols = CONFIG["symbols"]


def _score_detail(symbol, ind, regime, fg, funding_rate, btc_ind):
    """复刻 calc_score 内部多空分计算（用于展示）"""
    price = ind["last_close"]
    vol = ind["vol_current"]
    vol_avg = ind["vol_avg20"]
    atr_pct = ind["atr_pct"]
    resistance = ind["resistance"]
    support = ind["support"]
    long_score = 0
    short_score = 0
    if price >= resistance * 0.999:
        long_score += 40
    if price <= support * 1.001:
        short_score += 40
    if vol > vol_avg * 1.5:
        long_score += 30; short_score += 30
    elif vol > vol_avg * 1.2:
        long_score += 15; short_score += 15
    if 0.005 < atr_pct < 0.03:
        long_score += 20; short_score += 20
    if btc_ind:
        btc_bull = btc_ind.get("ema20_15m", 0) > btc_ind.get("ema60_15m", 0)
        if btc_bull:
            long_score += 10; short_score -= 5
        else:
            short_score += 10; long_score -= 5
    long_score = max(0, long_score)
    short_score = max(0, short_score)
    if _SENTIMENT_ENGINE:
        long_score += _SENTIMENT_ENGINE.sentiment_score(symbol, "LONG", fg, funding_rate)
        short_score += _SENTIMENT_ENGINE.sentiment_score(symbol, "SHORT", fg, funding_rate)
    return long_score, short_score


eng = IndicatorEngine()
sent = SentimentEngine()
set_sentiment_engine(sent)

fg = sent.fear_greed()
fr = sent.funding_rates(symbols)

btc_ind = eng.calc("BTC") or {}

print(f"恐惧贪婪指数: {fg}")
print(f"{'币种':<6}{'现价':>10}{'状态':<10}{'做多':>5}{'做空':>5}{'方向':<7}{'阈值':>5}")
print("-" * 60)
for sym in symbols:
    ind = eng.calc(sym)
    if not ind:
        print(f"{sym:<6} 指标获取失败")
        continue
    price = ind["last_close"]
    regime = detect_regime(ind)
    threshold = CONFIG["score_threshold"][regime] + CONFIG["score_threshold"].get("per_symbol_bonus", {}).get(sym, 0)
    long_s, short_s = _score_detail(sym, ind, regime, fg, fr.get(sym, 0), btc_ind)
    score, direction = calc_score(sym, {"price": price}, ind, regime, fg, fr.get(sym, 0), btc_ind)
    print(f"{sym:<6}{price:>10.4f}{regime:<10}{long_s:>5}{short_s:>5}{direction:<7}{threshold:>5}  (总分{score})")
