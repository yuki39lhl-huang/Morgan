#!/usr/bin/env python3
"""
backtester.py — 离线回测引擎（原型的 Step A：位置模拟 + 分批过滤）

职位说明：
  在 backtester_proto.py（仅复现信号）基础上，加入真实位置模拟：
    - 分批过滤：持仓期间不再重复开仓（避免同向密集信号被重复计为多笔）
    - 止盈止损：复用现有 calc_tp_sl()，逐 K 线判断 TP/SL 是否触发
    - 持仓超时：按 config.timeout_exit 超时市价离场
    - intrabar 保守假设：同一根 K 线 TP/SL 都触及 → 视为先触发 SL

零耦合：只 import data_layer / strategy_layer / config。
simulate() 被抽成纯函数，供 Step B 参数扫描（param_scan.py）直接调用。

⚠️ 本引擎用固定情绪(0)+无BTD大盘，用于参数对比的相对结论；绝对盈亏仅供参照。
"""
import os
import sys
import bisect
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
import time
import warnings
warnings.filterwarnings("ignore")

from config import CONFIG, get_config
from data_layer import IndicatorEngine, detect_regime
from strategy_layer import calc_score, calc_tp_sl


# 单边手续费(占比)，Binance U 本位期货 taker 典型值；两进两出各扣一次
FEE_PCT = 0.0005


class _FakeSentimentEngine:
    """离线性情绪替身：固定中性，使 calc_score 情绪分支稳定可复现。"""
    def sentiment_score(self, symbol, direction, fg, fr):
        return 0


_INTERVAL_MS = {"15m": 15 * 60000, "1h": 60 * 60000}


def _fetch_klines(session, endpoint: str, symbol: str, interval: str, total: int) -> list:
    """
    分页拉取 K 线（Binance klines 单次 limit≤1500，超长需 startTime 分页）。
    用调用方传入的 session（带代理、verify=False），从最早往前逐页取，去重排序。
    """
    step_ms = _INTERVAL_MS[interval]
    step = min(1000, total)
    url = f"{endpoint}/klines"
    params = {"symbol": f"{symbol}USDT", "interval": interval, "limit": step}
    # 从距今向前推 total 根开始，避免首页 400（limit 超限）
    now_ms = int(time.time() * 1000)
    start = now_ms - total * step_ms
    bars, seen = [], set()
    for _ in range(60):  # 保险上限
        chunk = None
        for attempt in range(4):  # 代理连接间歇 SSL EOF，重试
            try:
                r = session.get(url, params={**params, "startTime": start}, timeout=20)
                if r.status_code == 200:
                    chunk = r.json()
                    break
                else:
                    time.sleep(0.5 * (attempt + 1))
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        if chunk is None:
            break  # 重试耗尽/非 200
        for b in chunk:
            if b[0] not in seen:
                seen.add(b[0])
                bars.append(b)
        if len(chunk) < step:
            break  # 已到历史尽头
        start = chunk[-1][0] + step_ms  # 上次末根之后一根
        if len(bars) >= total:
            break
        time.sleep(0.15)  # 限速，避免被限频
    bars.sort(key=lambda b: b[0])
    return bars[-total:]


def _kline_cut(bars: list, ts: int, n: int) -> list:
    """返回 openTime <= ts 的最近 n 根 K 线（bars 按时间升序）。O(log n)。"""
    times = _kline_cut._times
    j = bisect.bisect_right(times, ts)
    return bars[max(0, j - n):j]


def _build_ind(ind_engine, window, hour_window):
    """与实时 IndicatorEngine.calc() 产出的 ind 字段一致，供 calc_score/detect_regime 使用。"""
    closes_15m = [float(k[4]) for k in window]
    highs = [float(k[2]) for k in window]
    lows = [float(k[3]) for k in window]
    vols = [float(k[5]) for k in window]
    price = closes_15m[-1]
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


def simulate(symbol: str, days: int = 14, override: dict = None, use_mainnet: bool = True) -> dict:
    """
    离线回测一个标的。返回统计 dict 与平仓明细 trades。
    override: 覆盖 config 键（Step B 参数扫描用），如
        {"score_threshold": {...}, "atr_sl_tiers": [...]}
    use_mainnet: 默认主网只读端点拉历史 K 线（样本量大）；False 回退测试网。
    """
    # override 必须写入全局 CONFIG，calc_score/calc_tp_sl 读的是模块全局而非本地参数。
    ov = override or {}
    backup = {k: CONFIG[k] for k in ov if k in CONFIG}
    CONFIG.update(ov)
    try:
        return _simulate_impl(symbol, days, use_mainnet)
    finally:
        for k, v in backup.items():
            CONFIG[k] = v


def _simulate_impl(symbol: str, days: int, use_mainnet: bool) -> dict:
    cfg = dict(CONFIG)

    ind_engine = IndicatorEngine()
    from strategy_layer import set_sentiment_engine
    set_sentiment_engine(_FakeSentimentEngine())

    # 回测专用：临时把 CONFIG 指向主网只读端点（仅本进程，try/finally 还原）
    src = CONFIG["binance_futures"]
    mainnet = cfg.get("binance_futures_mainnet")
    if use_mainnet and mainnet:
        CONFIG["binance_futures"] = mainnet
    try:
        klines_15m = _fetch_klines(ind_engine.session, CONFIG["binance_futures"], symbol, "15m", 100 + days * 96)
        klines_1h = _fetch_klines(ind_engine.session, CONFIG["binance_futures"], symbol, "1h", days * 24 + 60)
        # BTC 大盘参考：同区间拉 BTC 15m/1h，用于 calc_score 的 btc_ind（与实盘同源）
        btc_15m = _fetch_klines(ind_engine.session, CONFIG["binance_futures"], "BTC", "15m", 100 + days * 96)
        btc_1h = _fetch_klines(ind_engine.session, CONFIG["binance_futures"], "BTC", "1h", days * 24 + 60)
    finally:
        if use_mainnet and mainnet:
            CONFIG["binance_futures"] = src
    if len(klines_15m) < 100:
        return {"symbol": symbol, "error": f"K线不足 {len(klines_15m)}", "trades": []}

    threshold_by_regime = cfg["score_threshold"]
    timeout_ms = cfg.get("timeout_exit", {}).get("hours", 0) * 3600 * 1000
    timeout_on = cfg.get("timeout_exit", {}).get("enabled", False)

    # 预计算 BTC 时间戳列，供 _kline_cut 二分定位
    _kline_cut._times = [int(k[0]) for k in btc_15m]

    trades, pos = [], None
    for i in range(99, len(klines_15m)):
        window = klines_15m[: i + 1][-100:]
        ts = int(window[-1][0])
        c = float(window[-1][4])
        c_high = float(window[-1][2])
        c_low = float(window[-1][3])

        # ── 1. 若有持仓：先判平仓 ──
        if pos:
            if timeout_on and ts - pos["entry_ts"] >= timeout_ms:
                # 超时 → 按当前收盘价市价离场
                trades.append(pos | {"reason": "timeout", "exit_ts": ts,
                                     "exit_price": c, "pnl_pct": _pnl(pos, c)})
                pos = None
            else:
                if pos["dir"] == "LONG":
                    tp, sl = pos["tp"], pos["sl"]
                    if c_low <= sl or c_high >= tp:
                        # 先触发 SL（保守）；仅触及 TP 则按 TP
                        hit_sl = c_low <= sl
                        exit_p = sl if hit_sl else tp
                        trades.append(pos | {"reason": "SL" if hit_sl else "TP",
                                             "exit_ts": ts, "exit_price": exit_p,
                                             "pnl_pct": _pnl(pos, exit_p)})
                        pos = None
                else:  # SHORT
                    tp, sl = pos["tp"], pos["sl"]  # sl 在 entry 上方
                    if c_high >= sl or c_low <= tp:
                        # 保守：先触发 SL（价格先走回到 sl 视为赔）
                        hit_sl = c_high >= sl
                        exit_p = sl if hit_sl else tp
                        trades.append(pos | {"reason": "SL" if hit_sl else "TP",
                                             "exit_ts": ts, "exit_price": exit_p,
                                             "pnl_pct": _pnl(pos, exit_p)})
                        pos = None

        # ── 2. 若无持仓：生成信号，分批过滤题意下仅此时开仓 ──
        if not pos:
            hour_window = _kline_cut(klines_1h, ts, 60)
            ind = _build_ind(ind_engine, window, hour_window)
            # BTC 大盘参考：取截至 ts 的 BTC 窗口并构建 btc_ind（与实盘 IndicatorEngine 同源）
            btc_window = _kline_cut(btc_15m, ts, 100)
            btc_hour = _kline_cut(btc_1h, ts, 60)
            btc_ind = _build_ind(ind_engine, btc_window, btc_hour) if len(btc_window) >= 100 else None
            regime = detect_regime(ind)
            price_data = {"price": c, "volume": float(window[-1][5])}
            score, direction = calc_score(
                symbol, price_data, ind, regime,
                fg=50, funding_rate=0.0, btc_ind=btc_ind,
            )
            threshold = threshold_by_regime.get(regime, 90)
            threshold += threshold_by_regime.get("per_symbol_bonus", {}).get(symbol, 0)
            if direction in ("LONG", "SHORT") and score >= threshold:
                tpsl = calc_tp_sl(c, direction, ind, price_data,
                                  min_sl_pct=cfg.get("bt_min_sl_pct", 0.02),
                                  max_sl_pct=cfg.get("bt_max_sl_pct", 0.15))
                pos = {
                    "symbol": symbol, "dir": direction, "score": score,
                    "regime": regime, "entry": c, "entry_ts": ts,
                    "tp": tpsl["tp1_price"], "sl": tpsl["sl_price"],
                }

    # 未平仓的剔除（统计只算有结论的交易）
    return _stats(symbol, trades)


def _pnl(pos: dict, exit_price: float) -> float:
    """单笔收益率（%），含双边手续费。LONG=(exit/entry-1)；SHORT=(entry/exit-1)。"""
    entry = pos["entry"]
    if pos["dir"] == "LONG":
        raw = exit_price / entry - 1
    else:
        raw = entry / exit_price - 1
    return (raw - 2 * FEE_PCT) * 100


def _stats(symbol: str, trades: list) -> dict:
    if not trades:
        return {"symbol": symbol, "n": 0, "trades": []}
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "symbol": symbol,
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_pnl_pct": sum(pnls) / len(trades),
        "total_pnl_pct": sum(pnls),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "reasons": dict(Counter(t["reason"] for t in trades)),
        "trades": trades,
    }


def run_multi(symbols: list, days: int = 14, override: dict = None) -> dict:
    """多币统参回测：逐币 simulate 后聚合。返回每币统计 + 聚合合计。"""
    per = {s: simulate(s, days, override) for s in symbols}
    all_trades = []
    for res in per.values():
        all_trades.extend(res.get("trades", []))
    agg = _stats("ALL", all_trades)
    agg.pop("trades", None)
    return {"per": per, "aggregate": agg}


def _fmt_trade(t):
    return (f"  {datetime.fromtimestamp(t['entry_ts']/1000).strftime('%m-%d %H:%M')} "
            f"{t['dir']:<5} {t['reason']:<7} score={t['score']:<3} "
            f"pnl={t['pnl_pct']:+.2f}%  "
            f"entry={t['entry']:.1f}→exit={t['exit_price']:.1f}")


def main():
    argv = sys.argv[1:]
    days = int(argv[1]) if len(argv) > 1 else 14
    # 多币：第一个参数为逗号分隔的符号列表或 "ALL"
    if argv and argv[0].upper() == "ALL":
        symbols = ["BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"]
    else:
        symbols = [s.strip().upper() for s in (argv[0] if argv else "BTC").split(",") if s.strip()]

    if len(symbols) == 1:
        res = simulate(symbols[0], days)
        if "error" in res:
            print(res["error"]); return
        print(f"[{symbols[0]}] 回测 {days} 天：平仓 {res['n']} 笔")
        for t in res["trades"]:
            print(_fmt_trade(t))
        print(f"\n胜率={res['win_rate']:.1f}%  平均每笔={res['avg_pnl_pct']:+.3f}%  "
              f"累计={res['total_pnl_pct']:+.2f}%  PF={res['profit_factor']:.2f}")
        print(f"出场分布：{res['reasons']}")
        return

    # 多币聚合
    r = run_multi(symbols, days)
    print(f"═ 多币回测 {days} 天（BTC 大盘参考）═")
    print(f"{'币':<6}{'笔数':>5}{'胜率':>8}{'平均%':>9}{'累计%':>10}{'PF':>7}")
    for s, res in r["per"].items():
        if "error" in res:
            print(f"{s:<6} error: {res['error']}")
            continue
        print(f"{s:<6}{res['n']:>5}{res['win_rate']:>7.1f}%{res['avg_pnl_pct']:>+9.3f}"
              f"{res['total_pnl_pct']:>+10.2f}{res['profit_factor']:>7.2f}")
    a = r["aggregate"]
    print(f"{'合计':<6}{a['n']:>5}{a['win_rate']:>7.1f}%{a['avg_pnl_pct']:>+9.3f}"
          f"{a['total_pnl_pct']:>+10.2f}{a['profit_factor']:>7.2f}")


if __name__ == "__main__":
    main()