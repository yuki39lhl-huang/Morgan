#!/usr/bin/env python3
"""
backtester_proto.py — 离线回测最小可行性原型（Step 1）

目标：证明「用历史 15m K 线离线重放当前策略的信号」可行。
做法：单标的，逐根滑动窗口。对每根 K 线，用「过去至该根」的窗口：
  - IndicatorEngine 算指标（完全复用现有实现）
  - calc_score 算出 LONG/SHORT/NONE 方向
  - 输出触发的信号时间点 + regime + score，验证历史信号能离线重现。

零耦合：只 import data_layer / strategy_layer / config，不碰 monitor 主循环。
情绪引擎暂用离线固定值近似（fg=50），验证指标+评分链路，情绪后续用历史快照补。

本文件是原型，非生产模块；验证通过后可升级为完整 backtester.py。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import warnings
warnings.filterwarnings("ignore")  # 拉数据时的泛型 HTTP 告警，与本原型无关

from config import get_config
from data_layer import IndicatorEngine, detect_regime
from strategy_layer import calc_score

CONFIG = get_config()


class FakeSentimentEngine:
    """离线情绪引擎替身：固定中性情绪，使 calc_score 的情绪分支稳定可复现。"""
    def sentiment_score(self, symbol, direction, fg, fr):
        return 0


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 14

    ind_engine = IndicatorEngine()
    from strategy_layer import set_sentiment_engine
    set_sentiment_engine(FakeSentimentEngine())

    # ── 1. 拉取历史 15m + 1h K线（数据源与实时引擎完全一致）
    klines_15m = ind_engine._get_klines(symbol, "15m", limit=100 + days * 96)
    klines_1h = ind_engine._get_klines(symbol, "1h", limit=days * 24 + 60)
    if len(klines_15m) < 100:
        print(f"K线不足（{len(klines_15m)} 根 < 100），无法回放")
        return

    print(f"拉取 {symbol} 15m K线 {len(klines_15m)} 根 / 1h K线 {len(klines_1h)} 根")
    print(f"回放区间：{datetime.fromtimestamp(klines_15m[0][0]/1000)} ~ "
          f"{datetime.fromtimestamp(klines_15m[-1][0]/1000)}")

    # ── 2. 逐根滑动窗口重放
    signals = []
    # 每根时间戳 → 该根 15m 窗口（含该根，最多 100 根，用于指标）
    for i in range(99, len(klines_15m)):
        window = klines_15m[: i + 1][-100:]
        # 该根过去 1h 窗口（EMA20/60 需要，最多 60 根）
        ts = window[-1][0]
        hour_window = [k for k in klines_1h if k[0] <= ts][-60:]

        # 构造该时间点的 ind（复用 IndicatorEngine.calc 内部逻辑）
        ind = _build_ind(ind_engine, window, hour_window, symbol)
        if ind is None:
            continue
        regime = detect_regime(ind)
        price = float(window[-1][4])
        price_data = {"price": price, "volume": float(window[-1][5])}

        score, direction = calc_score(
            symbol, price_data, ind, regime,
            fg=50, funding_rate=0.0, btc_ind=None,
        )
        if direction in ("LONG", "SHORT"):
            signals.append({
                "ts": ts, "time": datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M"),
                "dir": direction, "score": score, "regime": regime,
                "price": price,
            })

    # ── 3. 输出
    print(f"\n回放出 {len(signals)} 个信号：")
    for s in signals[:40]:
        print(f"  {s['time']}  {s['dir']:<5} score={s['score']:<3} "
              f"regime={s['regime']:<9} price={s['price']:.2f}")

    if len(signals) > 40:
        print(f"  ... 共 {len(signals)} 个信号")
    # 按方向/regime 汇总
    from collections import Counter
    c = Counter((s["dir"], s["regime"]) for s in signals)
    print("\n信号分布(dir, regime)：")
    for k, v in c.most_common():
        print(f"  {k[0]:<5} {k[1]:<9}: {v}")

    print("\n✅ 原型跑通：历史信号已能离线重现（若分布合理，证明离线回测可行）")


def _build_ind(ind_engine, window, hour_window, symbol):
    """
    复用 IndicatorEngine 的静态指标方法，从滑动窗口构造 ind 字典。
    与实时 calc() 产出的 ind 字段一致（见 data_layer._get_klines 使用处的字段）。
    """
    try:
        closes_15m = [float(k[4]) for k in window]
        highs = [float(k[2]) for k in window]
        lows = [float(k[3]) for k in window]
        vols = [float(k[5]) for k in window]
        price = closes_15m[-1]

        # 与实时 calc() 严格一致：支撑阻力/成交量均用最近 20 根 15m
        closes_1h = [float(k[4]) for k in hour_window] if hour_window else closes_15m

        atr = ind_engine._atr(window)
        return {
            "last_close": price,
            "price": price,
            "resistance": max(highs[-20:]),
            "support": min(lows[-20:]),
            "vol_avg20": sum(vols[-20:]) / len(vols[-20:]) if vols else 0,
            "vol_current": vols[-1],
            "atr_pct": atr / price if price else 0,
            "ema20_15m": ind_engine._ema(closes_15m, 20),
            "ema60_15m": ind_engine._ema(closes_15m, 60),
            "ema20_1h": ind_engine._ema(closes_1h, 20),
            "ema60_1h": ind_engine._ema(closes_1h, 60),
            "ema_slope": (closes_15m[-1] - closes_15m[-5]) / closes_15m[-5]
                         if len(closes_15m) >= 5 and closes_15m[-5] else 0,
        }
    except Exception as e:
        print(f"  [跳过] {symbol} 指标计算失败: {e}")
        return None


if __name__ == "__main__":
    main()