#!/usr/bin/env python3
"""
push_hourly_once.py — 手动触发一次「整点播报」推送

复用 notify_layer.hourly_report()，用于按需推送（不等待整点）。
持仓实时从 Binance API 拉取（positions=[] 触发 hourly_report 内部 API 恢复路径）。
"""
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openclaw_logging import configure_root_logging
configure_root_logging("trading")
log = logging.getLogger("push_hourly_once")

from config import CONFIG
from data_layer import IndicatorEngine, PriceStream, SentimentEngine
from risk_layer import CircuitBreaker
from notify_layer import hourly_report
from crypto_signal_monitor import _build_hourly_score_map


def main() -> int:
    indicator_engine = IndicatorEngine()

    indicators = {}
    prices = {}
    for sym in CONFIG["symbols"]:
        ind = indicator_engine.calc(sym)
        if not ind:
            log.warning(f"⚠️ {sym} 指标计算失败，跳过")
            continue
        indicators[sym] = ind
        lp = ind.get("last_close")
        if lp:
            prices[sym] = {
                "price": float(lp),
                "change_24h": 0,
                "volume": float(ind.get("vol_current") or 0),
            }
    log.info(f"📋 指标就绪：{len(indicators)}/{len(CONFIG['symbols'])} 个币种")

    try:
        fg = SentimentEngine().fear_greed()
    except Exception as e:
        log.warning(f"⚠️ 恐惧贪婪获取失败，使用默认 50：{e}")
        fg = 50

    circuit = CircuitBreaker()
    score_map = _build_hourly_score_map(CONFIG["symbols"], prices, indicators, fg)
    stream = PriceStream(CONFIG["symbols"], proxy=CONFIG.get("proxy"))
    for sym in CONFIG["symbols"]:
        try:
            stream.snapshot_symbol(sym)
        except Exception as exc:
            log.warning("1h K 线快照失败 %s：%s", sym, type(exc).__name__)

    # 传空 positions → hourly_report 内部从 Binance API 实时恢复
    hourly_report(
        [], prices, indicators, circuit, fg, indicator_engine, score_map,
        price_stream=stream,
    )
    log.info("✅ 整点播报已触发")
    return 0


if __name__ == "__main__":
    sys.exit(main())
