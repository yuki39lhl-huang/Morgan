#!/usr/bin/env python3
"""
hist_backtest.py — 主网历史 K 线回测（离线，不改实盘）

信号与现网入场规则一致：15m 指标 + 1h EMA，只做多、震荡不开、评分门槛走 config，
新开仓还要 ATR% ≥ min_entry_atr_pct（现网 0.4%）。
情绪固定中性、不接 AI（无 ai_conflict / ai_range），称「纯数学入场」。
实盘 max_positions_per_symbol 全 0 是 2026-11-01 止血，不读进回测，否则基线会被清空。
出场走 1m：开仓 ATR 止损 + 持仓中每根 15m 重估 ATR 止损（未进早保本/移动止盈时）
+ 早保本 1.5%→锁 0.5% + 移动止盈 + 硬 TP1 + 48h 超时。同一根 1m 先看止损。
费用与滑点、样本内/外窗口都是文件顶部常量，禁止为凑正期望改窗口或滑点。

用法：
  python3 hist_backtest.py --no-download                         # 旧基线（默认）
  python3 hist_backtest.py --no-download --entry baseline
  python3 hist_backtest.py --no-download --entry trend_breakout_atr
  python3 hist_backtest.py --no-download --entry trend_pullback_atr
  python3 hist_backtest.py --no-download --entry fixed_r_1h
  python3 hist_backtest.py --no-download --entry baseline_no_early_be
  python3 hist_backtest.py --no-download --entry funding_carry_long
  python3 hist_backtest.py --with-stopped   # 额外把 BTC/ETH 当可开仓对照
"""
import io
import os
import sys
import time
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_config, get_exit_config, get_score_config  # noqa: E402
from strategy_layer import calc_atr_sl_pct, calc_tp_sl, set_sentiment_engine  # noqa: E402

CST = timezone(timedelta(hours=8))
CACHE = Path.home() / ".cache" / "morgan_hist"
VISION = "https://data.binance.vision/data/futures/um"
FEE_RT = 0.001          # 往返费率，与 exit_replay 一致；名义 200U → 0.2U/笔
SLIPPAGE_RT = 0.0       # 写死：止损/止盈按精确价成交，无滑点、无跳空。结果偏乐观。本任务不另造滑点模型。
NOTIONAL = 200.0
START = "2024-10-01"
# 开仓日（CST）< SAMPLE_IN_END 为样本内，≥ 为样本外。对齐实盘考试段起点。禁止为凑正期望改。
SAMPLE_IN_END = "2026-10-04"
POOL = ["SOL", "BNB", "DOT", "LINK", "XRP"]
# 候选 A。常量写死，禁止网格搜索。
TB_N = 20
TB_VOL_MULT = 1.2
PASS_OOS_MIN = 30
ENTRY_BASELINE = "baseline"
ENTRY_TREND = "trend_breakout_atr"
# 候选 B。与 A 不同：不在突破当根追。常量写死，禁止网格。
PB_M = 10
PB_VOL_N = 20
PB_VOL_MULT = 1.0
ENTRY_PULLBACK = "trend_pullback_atr"
# 候选 C。1h 固定盈亏比，只在 --entry fixed_r_1h 走这条出场。常量写死，禁止网格。
FR_M = 5
FR_SL_ATR = 1.5
FR_TP_ATR = 3.0
ENTRY_FIXED = "fixed_r_1h"
# 候选 D。入场仍是 baseline。只改研究路径出场，不改 walk_exit 的默认参数。
ENTRY_NO_BE = "baseline_no_early_be"
# 候选 E。资金费率拥挤后做多。阈值写死，不用 90 日分位。禁止网格。
ENTRY_FUND = "funding_carry_long"
FUND_MIN = 0.0005
FUND_SENS = 0.0003
FUND_SL_ATR = 1.5
FUND_TP_ATR = 2.5
FUND_COOL_MS = 24 * 60 * 60_000
CONTROL = ["BTC", "ETH"]
MAX_POS = 4
WARMUP_15M = 100


class _FlatSentiment:
    def sentiment_score(self, symbol, direction, fg, fr):
        return 0


def _months(a: str, b: str):
    y, m = int(a[:4]), int(a[5:7])
    end_y, end_m = int(b[:4]), int(b[5:7])
    while (y, m) <= (end_y, end_m):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _get(url: str) -> bytes | None:
    s = requests.Session()
    for i in range(4):
        try:
            r = s.get(url, timeout=60)
            if r.status_code == 200 and r.content[:2] == b"PK":
                return r.content
            if r.status_code == 404:
                return None
        except Exception:
            time.sleep(1.2 * (i + 1))
    return None


def _parse_zip(blob: bytes) -> list[tuple]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        for line in zf.read(name).decode().splitlines():
            p = line.split(",")
            if not p or not p[0].isdigit():
                continue
            # open, high, low, close, volume
            rows.append((int(p[0]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return rows


def download_symbol(symbol: str, end_day: str) -> np.ndarray:
    """1m OHLCV，列 t,h,l,c,v。月包优先，当月用日包补。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"{symbol}USDT_1m_{START}_{end_day}.npz"
    if dest.exists():
        return np.load(dest)["a"]
    rows = []
    months = list(_months(START, end_day))
    for mo in months[:-1]:
        url = f"{VISION}/monthly/klines/{symbol}USDT/1m/{symbol}USDT-1m-{mo}.zip"
        blob = _get(url)
        if blob is None:
            raise RuntimeError(f"缺少月线 {symbol} {mo}")
        rows.extend(_parse_zip(blob))
        print(f"  {symbol} {mo} {len(rows)}", flush=True)
    # 最后一个月：日包（月包往往未出）
    y, m = int(months[-1][:4]), int(months[-1][5:7])
    day = datetime(y, m, 1, tzinfo=CST)
    end = datetime.fromisoformat(end_day).replace(tzinfo=CST)
    while day.date() <= end.date():
        ds = day.strftime("%Y-%m-%d")
        url = f"{VISION}/daily/klines/{symbol}USDT/1m/{symbol}USDT-1m-{ds}.zip"
        blob = _get(url)
        if blob:
            rows.extend(_parse_zip(blob))
        day += timedelta(days=1)
    if not rows:
        raise RuntimeError(symbol)
    arr = np.array(rows, dtype=np.float64)
    arr = arr[np.argsort(arr[:, 0])]
    # 去重
    _, idx = np.unique(arr[:, 0], return_index=True)
    arr = arr[np.sort(idx)]
    np.savez_compressed(dest, a=arr)
    print(f"  {symbol} 缓存 {len(arr)} 根 → {dest.name}", flush=True)
    return arr


def resample(bars: np.ndarray, minutes: int) -> np.ndarray:
    """1m → N 分钟。列 t,h,l,c,v。未收完的最后一根丢掉。"""
    step = minutes * 60_000
    t = bars[:, 0].astype(np.int64)
    bucket = (t // step) * step
    # 向量化分组
    change = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.r_[0, change]
    ends = np.r_[change, len(bars)]
    out = np.empty((len(starts), 5), dtype=np.float64)
    for i, (a, b) in enumerate(zip(starts, ends)):
        sl = bars[a:b]
        out[i, 0] = bucket[a]
        out[i, 1] = sl[:, 1].max()
        out[i, 2] = sl[:, 2].min()
        out[i, 3] = sl[-1, 3]
        out[i, 4] = sl[:, 4].sum()
    # 丢掉根数不足的未完成桶（最后一根若未满）
    if len(out) and (t[-1] - out[-1, 0]) < step - 60_000:
        out = out[:-1]
    return out


def _exit_reason(ret, be_on, trailed, hit_tp) -> str:
    if hit_tp:
        return "TP1"
    if ret > 0 and be_on:
        return "BE"
    if ret > 0 and trailed:
        return "trail"
    if ret <= 0:
        return "SL"
    return "SL_pos"


def walk_exit(bars_1m, i0, entry, tp, sl, tp_pct, timeout_h, be, tiers, atr_updater):
    """从 1m 下标 i0 起模拟多单出场。返回 (exit_i, ret, reason)。ret 未扣费。"""
    deadline = int(bars_1m[i0, 0]) + int(timeout_h * 3600_000)
    peak_ret, peak_px = 0.0, entry
    be_on = trailed = False
    init_sl = sl
    n = len(bars_1m)
    for i in range(i0, n):
        hi, lo, close = float(bars_1m[i, 1]), float(bars_1m[i, 2]), float(bars_1m[i, 3])
        ts = int(bars_1m[i, 0])
        if lo <= sl:
            r = (sl - entry) / entry
            return i, r, _exit_reason(r, be_on, trailed or sl > init_sl + 1e-12, False)
        if hi >= tp:
            return i, (tp - entry) / entry, "TP1"
        if ts >= deadline:
            return i, (close - entry) / entry, "timeout"
        fav = (hi - entry) / entry
        if fav > peak_ret:
            peak_ret, peak_px = fav, hi
        if be and peak_ret >= be[0]:
            sl = max(sl, entry * (1 + be[1]))
            be_on = True
        if tiers:
            active = None
            for tier in tiers:
                if peak_ret >= tp_pct * tier["pnl_ratio"]:
                    active = tier
                else:
                    break
            if active and active.get("gap", 0) < 0.9:
                gap = tp_pct * active["gap_ratio"]
                new_sl = peak_px * (1 - gap)
                if new_sl > sl:
                    sl = new_sl
                    trailed = True
        if atr_updater and ts % (15 * 60_000) == 0 and not be_on and not trailed:
            atr_sl = atr_updater(ts)
            if atr_sl and abs(atr_sl - sl) / sl >= 0.001:
                sl = atr_sl
                init_sl = sl  # 动态 ATR 替换基准，避免误记成移动止盈
    last = float(bars_1m[-1, 3])
    return n - 1, (last - entry) / entry, "eod"


def walk_fixed_r(bars_1m, i0, entry, tp, sl, reason_tp="TP"):
    """固定 SL/TP。无早保本、无移动止盈、无超时、无 ATR 重估。同一根先碰止损算止损。"""
    n = len(bars_1m)
    for i in range(i0, n):
        hi, lo, close = float(bars_1m[i, 1]), float(bars_1m[i, 2]), float(bars_1m[i, 3])
        if lo <= sl:
            return i, (sl - entry) / entry, "SL"
        if hi >= tp:
            return i, (tp - entry) / entry, reason_tp
    last = float(bars_1m[-1, 3])
    return n - 1, (last - entry) / entry, "eod"


def _parse_funding(blob: bytes) -> list[tuple]:
    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        for line in zf.read(name).decode().splitlines():
            p = line.split(",")
            if len(p) < 3 or not p[0].isdigit():
                continue
            rows.append((int(p[0]), float(p[2])))
    return rows


def load_funding(symbol: str, end_month: str = "2026-10") -> np.ndarray:
    """主网已结算资金费率。缓存 ~/.cache/morgan_hist/funding/。无包的月份跳过。"""
    dest_dir = CACHE / "funding"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{symbol}USDT_funding_{START}_{end_month}.npz"
    if dest.exists():
        return np.load(dest)["a"]
    rows = []
    for mo in _months(START, end_month):
        url = f"{VISION}/monthly/fundingRate/{symbol}USDT/{symbol}USDT-fundingRate-{mo}.zip"
        blob = _get(url)
        if blob is None:
            print(f"  {symbol} 费率缺 {mo}", flush=True)
            continue
        rows.extend(_parse_funding(blob))
    if not rows:
        return np.zeros((0, 2), dtype=np.float64)
    arr = np.array(rows, dtype=np.float64)
    arr = arr[np.argsort(arr[:, 0])]
    _, idx = np.unique(arr[:, 0], return_index=True)
    arr = arr[np.sort(idx)]
    np.savez_compressed(dest, a=arr)
    print(f"  {symbol} 费率 {len(arr)} 期 → {dest.name}", flush=True)
    return arr


def _ema_series(x: np.ndarray, period: int) -> np.ndarray:
    k = 2 / (period + 1)
    out = np.empty(len(x), dtype=np.float64)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def _roll_mean(x: np.ndarray, n: int) -> np.ndarray:
    c = np.cumsum(np.insert(x.astype(np.float64), 0, 0.0))
    out = np.zeros(len(x), dtype=np.float64)
    if len(x) >= n:
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _prepare_signals(series: dict, entry_mode: str = ENTRY_BASELINE):
    """每个 15m 下标是否纯数学做多。情绪=0。baseline 与 calc_score 同式；候选 A 不走分数门槛。"""
    sc = get_score_config()
    min_atr = float(get_config().get("min_entry_atr_pct") or 0)
    btc = series["BTC"]
    btc_t = btc["t15"]
    btc_e20 = _ema_series(btc["m15"][:, 3], 20)
    btc_e60 = _ema_series(btc["m15"][:, 3], 60)
    for sym, s in series.items():
        m = s["m15"]
        h, l, c, v = m[:, 1], m[:, 2], m[:, 3], m[:, 4]
        t = s["t15"]
        prev = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
        atr = _roll_mean(tr, 14)
        atr_pct = np.divide(atr, c, out=np.zeros_like(c), where=c > 0)
        vol_ma = _roll_mean(v, 20)
        res = np.empty(len(c))
        sup = np.empty(len(c))
        for i in range(len(c)):
            a = max(0, i - 19)
            res[i] = h[a:i + 1].max()
            sup[i] = l[a:i + 1].min()
        slope = np.zeros(len(c))
        slope[5:] = (c[5:] - c[:-5]) / np.where(c[:-5] == 0, 1, c[:-5])
        sig = np.zeros(len(c), dtype=bool)
        score = np.zeros(len(c))
        regime = np.empty(len(c), dtype=object)
        bj = np.searchsorted(btc_t, t, side="right") - 1
        for i in range(WARMUP_15M, len(c)):
            atrp = float(atr_pct[i])
            if atrp > 0.05:
                reg = "volatile"
            elif abs(float(slope[i])) > 0.005:
                reg = "trending"
            else:
                reg = "ranging"
            regime[i] = reg
            br = sc.breakout_points if c[i] >= res[i] * 0.999 else 0
            brs = sc.breakout_points if c[i] <= sup[i] * 1.001 else 0
            vr = v[i] / vol_ma[i] if vol_ma[i] else 0
            if vr > sc.vol_high_ratio:
                vs = sc.vol_high_points
            elif vr > sc.vol_mid_ratio:
                vs = sc.vol_mid_points
            else:
                vs = 0
            ats = sc.atr_points if sc.atr_low < atrp < sc.atr_high else 0
            ls, ss = br + vs + ats, brs + vs + ats
            j = int(bj[i])
            if j >= 60:
                if btc_e20[j] > btc_e60[j]:
                    ls += sc.btc_align
                    ss -= sc.btc_oppose
                else:
                    ss += sc.btc_align
                    ls -= sc.btc_oppose
            ls, ss = max(0, ls), max(0, ss)
            if entry_mode == ENTRY_BASELINE:
                th = sc.gate(reg, sym)
                # allow_ranging=false 时 gate 对 ranging 返回 None。allow_short=false：只发多头。
                if th is not None and ls > ss and ls >= th and (min_atr <= 0 or atrp >= min_atr):
                    sig[i] = True
                    score[i] = ls
        if entry_mode == ENTRY_TREND:
            # 近 N 根 = 信号棒之前的 N 根，不含当根。当根高点/成交量不参与门槛。
            wh = np.lib.stride_tricks.sliding_window_view(h, TB_N)
            wv = np.lib.stride_tricks.sliding_window_view(v, TB_N)
            prior_high = np.full(len(c), np.inf)
            prior_vol = np.zeros(len(c))
            prior_high[TB_N:] = wh[:-1].max(axis=1)
            prior_vol[TB_N:] = wv[:-1].mean(axis=1)
            for i in range(max(WARMUP_15M, TB_N), len(c)):
                atrp = float(atr_pct[i])
                if regime[i] != "trending":
                    continue
                if min_atr > 0 and atrp < min_atr:
                    continue
                if prior_vol[i] <= 0 or v[i] < prior_vol[i] * TB_VOL_MULT:
                    continue
                if c[i] > prior_high[i]:
                    sig[i] = True
                    score[i] = 1.0
        if entry_mode == ENTRY_PULLBACK:
            # 写死：趋势 = 收盘>EMA20 且 EMA20>EMA60。不用旧 trending regime，也不走 calc_score。
            # 回踩看信号根之前的 M 根（不含当根）：某根最低触及/跌破该根 EMA20，且该根收盘仍在 EMA20 上。
            # 触发在回踩之后的确认根，不在回踩当根开仓：收盘仍在 EMA20 上，且收盘 > 前一根高点。
            ema20 = _ema_series(c, 20)
            ema60 = _ema_series(c, 60)
            touch = (l <= ema20) & (c > ema20)
            tw = np.lib.stride_tricks.sliding_window_view(touch.astype(np.uint8), PB_M)
            had = np.zeros(len(c), dtype=bool)
            had[PB_M:] = tw[:-1].max(axis=1) > 0
            prev_high = np.empty(len(c))
            prev_high[0] = np.inf
            prev_high[1:] = h[:-1]
            vw = np.lib.stride_tricks.sliding_window_view(v, PB_VOL_N)
            prior_vol_pb = np.zeros(len(c))
            prior_vol_pb[PB_VOL_N:] = vw[:-1].mean(axis=1)
            start = max(WARMUP_15M, 60, PB_M, PB_VOL_N)
            for i in range(start, len(c)):
                atrp = float(atr_pct[i])
                if not (c[i] > ema20[i] and ema20[i] > ema60[i]):
                    continue
                if not had[i]:
                    continue
                if c[i] <= prev_high[i]:
                    continue
                if min_atr > 0 and atrp < min_atr:
                    continue
                if prior_vol_pb[i] <= 0 or v[i] < prior_vol_pb[i] * PB_VOL_MULT:
                    continue
                sig[i] = True
                score[i] = 1.0
        s["sig"] = sig
        s["score"] = score
        s["regime"] = regime
        s["atr_pct"] = atr_pct
        s["close"] = c


def run(symbols, data_1m, btc_1h_bull_at, btc_filter: bool, stopped: set, seed: int | None):
    """seed 非空 = 随机入场（概率 run.p）。"""
    ex = get_exit_config()
    be = (ex.breakeven_trigger_pct, ex.breakeven_lock_pct) if ex.breakeven_trigger_pct > 0 else None
    tiers = list(ex.trailing_tiers)
    timeout_h = ex.timeout_hours if ex.timeout_enabled else 48
    set_sentiment_engine(_FlatSentiment())

    series = {}
    for sym in symbols:
        m1 = data_1m[sym]
        m15 = resample(m1, 15)
        series[sym] = {"m1": m1, "m15": m15, "t15": m15[:, 0].astype(np.int64)}
    if "BTC" not in series and "BTC" in data_1m:
        m15 = resample(data_1m["BTC"], 15)
        series["BTC"] = {"m1": data_1m["BTC"], "m15": m15, "t15": m15[:, 0].astype(np.int64)}
    entry_mode = getattr(run, "entry", ENTRY_BASELINE)
    print(f"  预计算 15m 信号（{entry_mode}）…", flush=True)
    _prepare_signals(series, entry_mode)

    clock = series[symbols[0]]["t15"]
    clock = clock[WARMUP_15M:]
    rng = np.random.default_rng(seed) if seed is not None else None
    p_random = getattr(run, "p", 0.02)
    force = getattr(run, "force", set())

    positions = {}
    trades = []
    equity = peak = max_dd = 0.0

    def atr_sl_fn(sym, ts, entry):
        """用上一根已收完的 15m。ts 落在 15m 开盘时，side=right-1 会指到刚开始、高低收还没发生的那根。"""
        s = series[sym]
        j = int(np.searchsorted(s["t15"], ts, side="right")) - 1
        while j >= 0 and int(s["t15"][j]) + 15 * 60_000 > ts:
            j -= 1
        if j < 20:
            return None
        return entry * (1 - calc_atr_sl_pct(float(s["atr_pct"][j])))

    # 不读实盘 max_positions_per_symbol。2026-11-01 起七币全 0 是止血开关，不是入场规则。
    tradable = [s for s in symbols if s in series and (s in force or s not in stopped)]

    for ts in clock:
        done = [s for s, p in positions.items() if p["exit_ts"] <= ts]
        for sname in done:
            p = positions.pop(sname)
            gross = p["ret"] * NOTIONAL
            cost = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
            pnl = gross - cost
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            day = datetime.fromtimestamp(p["entry_ts"] / 1000, CST).strftime("%Y-%m-%d")
            trades.append({
                **p, "pnl": pnl, "gross": gross, "fee": cost,
                "direction": "LONG", "day": day, "equity": equity,
            })
        if len(positions) >= MAX_POS:
            continue
        if btc_filter and not btc_1h_bull_at(int(ts)):
            continue
        order = list(tradable)
        if rng is not None:
            rng.shuffle(order)
        for sym in order:
            if len(positions) >= MAX_POS or sym in positions:
                continue
            s = series[sym]
            j = int(np.searchsorted(s["t15"], ts, side="right")) - 1
            if j < WARMUP_15M or int(s["t15"][j]) != int(ts):
                continue
            if rng is None:
                if not s["sig"][j]:
                    continue
                score = float(s["score"][j])
                regime = s["regime"][j]
            else:
                if float(rng.random()) > p_random:
                    continue
                score, regime = 0, s["regime"][j] or "random"
            entry = float(s["close"][j])
            entry_ms = int(ts) + 15 * 60_000
            i0 = int(np.searchsorted(s["m1"][:, 0], entry_ms, side="left"))
            if i0 >= len(s["m1"]) - 2:
                continue
            ind = {"atr_pct": float(s["atr_pct"][j])}
            tp_sl = calc_tp_sl(entry, "LONG", ind, {})
            exit_mode = getattr(run, "exit_mode", "baseline")
            if exit_mode == "hard_tp":
                # 候选 D：只用开仓时的 ATR 止损和硬 TP。不碰早保本、移动止盈、超时、持仓中 ATR 重估。
                _i, ret, reason = walk_fixed_r(
                    s["m1"], i0, entry, tp_sl["tp1_price"], tp_sl["sl_price"], reason_tp="TP1",
                )
            else:
                updater = (lambda sym=sym, entry=entry: (lambda t, sym=sym, entry=entry: atr_sl_fn(sym, t, entry)))()
                use_be = None if exit_mode == "no_be" else be
                _i, ret, reason = walk_exit(
                    s["m1"], i0, entry, tp_sl["tp1_price"], tp_sl["sl_price"], tp_sl["tp_pct"],
                    timeout_h, use_be, tiers, updater,
                )
            positions[sym] = {
                "symbol": sym, "entry_ts": entry_ms, "exit_ts": int(s["m1"][_i, 0]),
                "ret": ret, "reason": reason, "score": score, "regime": regime,
                "month": datetime.fromtimestamp(entry_ms / 1000, CST).strftime("%Y-%m"),
            }
    return {"trades": trades, "max_dd": max_dd, "equity": equity}


def run_fixed_r(symbols, data_1m, btc_1h_bull_at, btc_filter: bool, stopped: set):
    """1h 回踩 + 固定 1.5/3.0 ATR。不调用 walk_exit，基线出场函数保持原样。"""
    min_atr = float(get_config().get("min_entry_atr_pct") or 0)
    series = {}
    for sym in symbols:
        m1 = data_1m[sym]
        h1 = resample(m1, 60)
        series[sym] = {"m1": m1, "h1": h1, "t1": h1[:, 0].astype(np.int64)}
    if "BTC" not in series and "BTC" in data_1m:
        h1 = resample(data_1m["BTC"], 60)
        series["BTC"] = {"m1": data_1m["BTC"], "h1": h1, "t1": h1[:, 0].astype(np.int64)}
    print("  预计算 1h 信号（fixed_r_1h）…", flush=True)
    for sym, s in series.items():
        m = s["h1"]
        h, l, c = m[:, 1], m[:, 2], m[:, 3]
        prev = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
        atr = _roll_mean(tr, 14)
        atr_pct = np.divide(atr, c, out=np.zeros_like(c), where=c > 0)
        ema20 = _ema_series(c, 20)
        ema60 = _ema_series(c, 60)
        touch = (l <= ema20).astype(np.uint8)
        tw = np.lib.stride_tricks.sliding_window_view(touch, FR_M)
        had = np.zeros(len(c), dtype=bool)
        had[FR_M:] = tw[:-1].max(axis=1) > 0
        sig = np.zeros(len(c), dtype=bool)
        start = max(WARMUP_15M, 60, FR_M)
        for i in range(start, len(c)):
            if atr[i] <= 0 or not had[i]:
                continue
            if not (c[i] > ema20[i] and ema20[i] > ema60[i]):
                continue
            if min_atr > 0 and float(atr_pct[i]) < min_atr:
                continue
            sig[i] = True
        s["sig"] = sig
        s["atr"] = atr
        s["close"] = c

    clock = series[symbols[0]]["t1"]
    clock = clock[max(WARMUP_15M, 60):]
    tradable = [s for s in symbols if s in series and s not in stopped]
    positions = {}
    trades = []
    equity = peak = max_dd = 0.0
    for ts in clock:
        done = [s for s, p in positions.items() if p["exit_ts"] <= ts]
        for sname in done:
            p = positions.pop(sname)
            gross = p["ret"] * NOTIONAL
            cost = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
            pnl = gross - cost
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            day = datetime.fromtimestamp(p["entry_ts"] / 1000, CST).strftime("%Y-%m-%d")
            trades.append({
                **p, "pnl": pnl, "gross": gross, "fee": cost,
                "direction": "LONG", "day": day, "equity": equity,
            })
        if len(positions) >= MAX_POS:
            continue
        if btc_filter and not btc_1h_bull_at(int(ts)):
            continue
        for sym in tradable:
            if len(positions) >= MAX_POS or sym in positions:
                continue
            s = series[sym]
            j = int(np.searchsorted(s["t1"], ts, side="right")) - 1
            if j < 60 or int(s["t1"][j]) != int(ts) or not s["sig"][j]:
                continue
            entry = float(s["close"][j])
            atr = float(s["atr"][j])
            sl = entry - FR_SL_ATR * atr
            tp = entry + FR_TP_ATR * atr
            if sl <= 0 or sl >= entry or tp <= entry:
                continue
            entry_ms = int(ts) + 60 * 60_000
            i0 = int(np.searchsorted(s["m1"][:, 0], entry_ms, side="left"))
            if i0 >= len(s["m1"]) - 2:
                continue
            _i, ret, reason = walk_fixed_r(s["m1"], i0, entry, tp, sl)
            positions[sym] = {
                "symbol": sym, "entry_ts": entry_ms, "exit_ts": int(s["m1"][_i, 0]),
                "ret": ret, "reason": reason, "score": 1.0, "regime": "1h",
                "month": datetime.fromtimestamp(entry_ms / 1000, CST).strftime("%Y-%m"),
            }
    return {"trades": trades, "max_dd": max_dd, "equity": equity}


def run_funding(symbols, data_1m, funding: dict, threshold: float, stopped: set):
    """已结算费率 ≥ threshold 后，下一根 1h 收盘仍 > EMA20 才开多。固定 1.5/2.5 ATR。"""
    min_atr = float(get_config().get("min_entry_atr_pct") or 0)
    series = {}
    tradable = []
    for sym in symbols:
        if sym in stopped or sym not in funding or sym not in data_1m:
            continue
        m1 = data_1m[sym]
        h1 = resample(m1, 60)
        h, l, c = h1[:, 1], h1[:, 2], h1[:, 3]
        prev = np.r_[c[0], c[:-1]]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
        atr = _roll_mean(tr, 14)
        atr_pct = np.divide(atr, c, out=np.zeros_like(c), where=c > 0)
        series[sym] = {
            "m1": m1,
            "t1": h1[:, 0].astype(np.int64),
            "close": c,
            "ema20": _ema_series(c, 20),
            "atr": atr,
            "atr_pct": atr_pct,
        }
        tradable.append(sym)
    cands = []
    raw_hits = 0
    for sym in tradable:
        s = series[sym]
        t1 = s["t1"]
        fr = funding[sym]
        for ft, rate in zip(fr[:, 0], fr[:, 1]):
            if rate < threshold:
                continue
            raw_hits += 1
            j = int(np.searchsorted(t1, int(ft) - 60 * 60_000, side="right"))
            if j < 60 or j >= len(t1):
                continue
            if int(t1[j]) + 60 * 60_000 <= int(ft):
                continue
            if not (float(s["close"][j]) > float(s["ema20"][j])):
                continue
            if min_atr > 0 and float(s["atr_pct"][j]) < min_atr:
                continue
            entry = float(s["close"][j])
            atrv = float(s["atr"][j])
            if atrv <= 0:
                continue
            sl = entry - FUND_SL_ATR * atrv
            tp = entry + FUND_TP_ATR * atrv
            if sl <= 0 or sl >= entry or tp <= entry:
                continue
            entry_ms = int(t1[j]) + 60 * 60_000
            cands.append((entry_ms, sym, entry, sl, tp))
    cands.sort()
    print(f"  费率≥{threshold:g} 的已结算期 {raw_hits}，1h 确认后 {len(cands)}", flush=True)

    def book(p, trades, equity, peak, max_dd):
        gross = p["ret"] * NOTIONAL
        cost = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
        pnl = gross - cost
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        day = datetime.fromtimestamp(p["entry_ts"] / 1000, CST).strftime("%Y-%m-%d")
        trades.append({
            **p, "pnl": pnl, "gross": gross, "fee": cost,
            "direction": "LONG", "day": day, "equity": equity,
        })
        return equity, peak, max_dd

    positions = {}
    last_fill = {}
    trades = []
    equity = peak = max_dd = 0.0
    for entry_ms, sym, entry, sl, tp in cands:
        done = [name for name, p in positions.items() if p["exit_ts"] <= entry_ms]
        for sname in done:
            equity, peak, max_dd = book(positions.pop(sname), trades, equity, peak, max_dd)
        if sym in positions or len(positions) >= MAX_POS:
            continue
        if entry_ms < last_fill.get(sym, 0) + FUND_COOL_MS:
            continue
        s = series[sym]
        i0 = int(np.searchsorted(s["m1"][:, 0], entry_ms, side="left"))
        if i0 >= len(s["m1"]) - 2:
            continue
        _i, ret, reason = walk_fixed_r(s["m1"], i0, entry, tp, sl)
        last_fill[sym] = entry_ms
        positions[sym] = {
            "symbol": sym, "entry_ts": entry_ms, "exit_ts": int(s["m1"][_i, 0]),
            "ret": ret, "reason": reason, "score": 1.0, "regime": "funding",
            "month": datetime.fromtimestamp(entry_ms / 1000, CST).strftime("%Y-%m"),
        }
    for sname in list(positions):
        equity, peak, max_dd = book(positions.pop(sname), trades, equity, peak, max_dd)
    return {"trades": trades, "max_dd": max_dd, "equity": equity}


def btc_1h_bull_fn(btc_h1):
    t = btc_h1[:, 0].astype(np.int64)
    e20 = _ema_series(btc_h1[:, 3], 20)
    e60 = _ema_series(btc_h1[:, 3], 60)
    flag = {int(t[i]): bool(e20[i] > e60[i]) for i in range(60, len(t))}

    def bull(ts) -> bool:
        k = (int(ts) // 3_600_000) * 3_600_000 - 3_600_000
        return flag.get(k, True)
    return bull


def _stats(rows):
    n = len(rows)
    if not n:
        return None
    gross = float(sum(t["gross"] for t in rows))
    net = float(sum(t["pnl"] for t in rows))
    wins = sum(1 for t in rows if t["pnl"] > 0)
    return n, 100.0 * wins / n, gross, net, net / n


def _print_fee_table(trades):
    """笔数、胜率（扣费后 pnl>0）、毛利、扣费净利、单笔期望；多/空/合计 × 全样本/样本内/样本外。"""
    windows = (
        ("全样本", trades),
        ("样本内", [t for t in trades if t["day"] < SAMPLE_IN_END]),
        ("样本外", [t for t in trades if t["day"] >= SAMPLE_IN_END]),
    )
    print(f"费用 {NOTIONAL * FEE_RT:.2f}U/笔（名义 {NOTIONAL:.0f} × 往返 {FEE_RT:.3%}）；"
          f"滑点往返 {SLIPPAGE_RT:.3%}（写死为 0）。"
          f"样本内开仓日 < {SAMPLE_IN_END}，样本外 ≥ {SAMPLE_IN_END}（CST，写死）。")
    print(f"{'窗口':<8}{'方向':<8}{'笔数':>6}{'胜率':>8}{'毛利U':>10}{'扣费净利U':>12}{'期望U/笔':>10}")
    for wlabel, rows in windows:
        for side in ("LONG", "SHORT", "合计"):
            sub = rows if side == "合计" else [t for t in rows if t.get("direction") == side]
            st = _stats(sub)
            if st is None:
                print(f"{wlabel:<8}{side:<8}{0:6}{'—':>8}{0:10.1f}{0:12.1f}{'—':>10}")
            else:
                n, win, gross, net, exp = st
                print(f"{wlabel:<8}{side:<8}{n:6}{win:7.1f}%{gross:10.1f}{net:12.1f}{exp:10.3f}")


def summarize(label, result):
    trades = result["trades"]
    n = len(trades)
    print(f"\n## {label}")
    if not n:
        print("无成交")
        _print_fee_table(trades)
        return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    tp1 = sum(1 for t in trades if t["reason"] == "TP1")
    _print_fee_table(trades)
    print(f"最大回撤 {result['max_dd']:.1f}U  TP1触发 {tp1} ({100*tp1/n:.1f}%)  "
          f"扣费后合计 {sum(pnls):+.1f}U  胜率 {100*len(wins)/n:.1f}%")
    by_m = defaultdict(lambda: [0, 0.0, 0])
    by_r = defaultdict(lambda: [0, 0.0, 0])
    for t in trades:
        by_m[t["month"]][0] += 1
        by_m[t["month"]][1] += t["pnl"]
        by_m[t["month"]][2] += t["reason"] == "TP1"
        by_r[t["regime"]][0] += 1
        by_r[t["regime"]][1] += t["pnl"]
        by_r[t["regime"]][2] += t["reason"] == "TP1"
    print("按月（笔数 / 扣费后U / TP1率）:")
    for m in sorted(by_m):
        c, s, tp = by_m[m]
        print(f"  {m}  {c:4}  {s:+8.1f}  {100*tp/c:5.1f}%")
    print("按开仓市况:")
    for r, (c, s, tp) in sorted(by_r.items()):
        print(f"  {r:10} {c:4}  {s:+8.1f}  TP1 {100*tp/c:.1f}%  期望 {s/c:+.3f}")
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1
    print("离场:", dict(reasons))


def diagnose(trades):
    """基线成交结构。只打印，不改出场。盈亏用毛利，费用另列。"""
    if not trades:
        print("诊断：无成交")
        return
    wins = [t["gross"] for t in trades if t["gross"] > 0]
    losses = [t["gross"] for t in trades if t["gross"] <= 0]
    avg_w = float(np.mean(wins)) if wins else 0.0
    avg_l = float(np.mean(losses)) if losses else 0.0
    payoff = abs(avg_w / avg_l) if avg_l else None
    fee = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
    fee_share = fee / avg_w if avg_w else None
    labels = {
        "SL": "硬止损",
        "SL_pos": "硬止损",
        "BE": "早保本",
        "trail": "移动止盈",
        "TP1": "TP1",
        "TP": "固定止盈",
        "timeout": "超时",
        "eod": "数据结束",
    }
    buckets = defaultdict(int)
    raw = defaultdict(int)
    for t in trades:
        raw[t["reason"]] += 1
        buckets[labels.get(t["reason"], t["reason"])] += 1
    n = len(trades)
    payoff_s = f"{payoff:.3f}" if payoff is not None else "—"
    fee_s = f"{100 * fee_share:.1f}%" if fee_share is not None else "—"
    print("诊断（毛利，未扣费）：")
    print(f"  平均盈利单 {avg_w:.3f}U  平均亏损单 {avg_l:.3f}U  盈亏比 {payoff_s}")
    print(f"  费用 {fee:.2f}U 占平均盈利单 {fee_s}")
    print("  离场占比:", " ".join(f"{k} {v} ({100 * v / n:.1f}%)" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])))
    print("  原始原因:", dict(raw))
    print("  移动止盈只计止损抬过成本后仍盈利的 trail；抬过但仍亏的记在硬止损。")


def buy_hold(symbols, data_1m):
    """等权现货买入持有（无杠杆），用日收盘。"""
    # 对齐到日
    daily = {}
    for sym in symbols:
        m1 = data_1m[sym]
        d = resample(m1, 1440)
        daily[sym] = d
    # 共同日期
    sets = [set(daily[s][:, 0].astype(np.int64)) for s in symbols]
    common = sorted(set.intersection(*sets))
    if len(common) < 2:
        return
    rets = []
    eq = 1.0
    peak = 1.0
    dd = 0.0
    month = defaultdict(float)
    for a, b in zip(common, common[1:]):
        rs = []
        for sym in symbols:
            px = {int(t): c for t, c in zip(daily[sym][:, 0], daily[sym][:, 3])}
            rs.append(px[b] / px[a] - 1)
        r = float(np.mean(rs))
        eq *= (1 + r)
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
        rets.append(r)
        key = datetime.fromtimestamp(b / 1000, CST).strftime("%Y-%m")
        month[key] += r
    print("\n## 买入持有（等权、无杠杆）")
    print(f"区间收益 {(eq-1)*100:+.1f}%  最大回撤 {dd*100:.1f}%  日数 {len(rets)}")
    print("按月收益%:", " ".join(f"{k}:{v*100:+.1f}" for k, v in sorted(month.items())))


def _passes(trades) -> bool:
    """写死：样本外笔数≥30 且样本外扣费期望>0 且全样本扣费期望>0。不调参。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_all, n_oos = len(trades), len(oos)
    exp_all = float(np.mean([t["pnl"] for t in trades])) if n_all else None
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    c3 = exp_all is not None and exp_all > 0
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    print(
        f"通过线：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外期望 {exp_oos_s} {'>' if c2 else '≤'} 0；"
        f"全样本期望 {exp_all_s} {'>' if c3 else '≤'} 0"
    )
    ok = c1 and c2 and c3
    print("结论：通过" if ok else "结论：淘汰（整套扔掉，不调常量硬凑）")
    return ok


def _entry_mode() -> str:
    if "--entry" not in sys.argv:
        return ENTRY_BASELINE
    i = sys.argv.index("--entry")
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("--"):
        raise SystemExit("缺少 --entry 的值")
    mode = sys.argv[i + 1]
    allowed = (ENTRY_BASELINE, ENTRY_TREND, ENTRY_PULLBACK, ENTRY_FIXED, ENTRY_NO_BE, ENTRY_FUND)
    if mode not in allowed:
        raise SystemExit(f"未知入场 {mode}，只接受 {' / '.join(allowed)}")
    return mode


def main():
    end_day = datetime.now(CST).strftime("%Y-%m-%d")
    symbols = POOL + CONTROL  # BTC 必须在，供大盘评分
    print(f"下载/读取 1m {START} → {end_day} ：{symbols}")
    if "--no-download" not in sys.argv:
        data = {s: download_symbol(s, end_day) for s in symbols}
    else:
        data = {}
        for s in symbols:
            hits = sorted(CACHE.glob(f"{s}USDT_1m_{START}_*.npz"))
            if not hits:
                raise SystemExit(f"无缓存 {s}")
            data[s] = np.load(hits[-1])["a"]
    for s, a in data.items():
        print(f"  {s} {len(a)} 根  {datetime.fromtimestamp(a[0,0]/1000, CST):%Y-%m-%d} → {datetime.fromtimestamp(a[-1,0]/1000, CST):%Y-%m-%d}")

    btc_h1 = resample(data["BTC"], 60)
    bull = btc_1h_bull_fn(btc_h1)
    run.force = set()
    run.p = 0.0
    mode = _entry_mode()
    run.entry = mode
    run.exit_mode = "baseline"
    sc = get_score_config()
    min_atr = float(get_config().get("min_entry_atr_pct") or 0)
    print("不读实盘 max_positions_per_symbol（2026-11-01 全 0 是止血，不是入场规则）。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    if sc.allow_short:
        print("警告：allow_short=true，本脚本仍只模拟多头，空头行会是 0。本任务不补空头公式。")

    if run.entry == ENTRY_TREND:
        print(
            f"\n候选 A trend_breakout_atr：只做多；只要 trending；"
            f"收盘 > 前 {TB_N} 根高点；当根量 ≥ 前 {TB_N} 根均量 × {TB_VOL_MULT}；"
            f"ATR≥{min_atr * 100:.2f}%。不走 calc_score。N 与倍数写死。"
        )
        print("主结论：BTC 1h 过滤关", flush=True)
        cand = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
        summarize("候选 A · trend_breakout_atr（BTC 1h 过滤关）", cand)
        _passes(cand["trades"])
        print("\n对照（不当主结论，不参与通过线）：BTC 1h EMA20>EMA60 才开", flush=True)
        filt = run(POOL + ["BTC"], data, bull, btc_filter=True, stopped=set(CONTROL), seed=None)
        summarize("候选 A + BTC 1h 过滤（仅对照）", filt)
        print("\n注明：滑点往返写死为 0，结果偏乐观。未改出场、未改样本窗、未改费用。实盘仍全 0。")
        return

    if run.entry == ENTRY_PULLBACK:
        print(
            f"\n候选 B trend_pullback_atr：只做多；收盘>EMA20 且 EMA20>EMA60；"
            f"前 {PB_M} 根内有回踩（低点触及该根 EMA20 且收盘仍在其上）；"
            f"确认根收盘>前一根高点；量 ≥ 前 {PB_VOL_N} 根均量 × {PB_VOL_MULT}；"
            f"ATR≥{min_atr * 100:.2f}%。不在回踩当根开。不走 calc_score。常量写死。"
        )
        print("主结论：BTC 1h 过滤关", flush=True)
        cand = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
        summarize("候选 B · trend_pullback_atr（BTC 1h 过滤关）", cand)
        _passes(cand["trades"])
        print("\n对照（不当主结论，不参与通过线）：BTC 1h EMA20>EMA60 才开", flush=True)
        filt = run(POOL + ["BTC"], data, bull, btc_filter=True, stopped=set(CONTROL), seed=None)
        summarize("候选 B + BTC 1h 过滤（仅对照）", filt)
        print("\n注明：滑点往返写死为 0，结果偏乐观。未改出场、未改样本窗、未改费用。实盘仍全 0。候选 A 常量未改。")
        return

    if run.entry == ENTRY_FIXED:
        print(
            f"\n候选 C fixed_r_1h：1h；收盘>EMA20 且 EMA20>EMA60；"
            f"前 {FR_M} 根有回踩（低点触及该根 EMA20）；确认根收盘重新站上 EMA20；"
            f"不追突破。SL={FR_SL_ATR}×ATR，TP={FR_TP_ATR}×ATR。"
            f"本模式无早保本/移动止盈/超时。ATR≥{min_atr * 100:.2f}%。常量写死。"
        )
        print("主结论：无 BTC 过滤", flush=True)
        cand = run_fixed_r(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL))
        summarize("候选 C · fixed_r_1h（无 BTC 过滤）", cand)
        _passes(cand["trades"])
        print("\n对照（不当主结论，不参与通过线）：BTC 1h EMA20>EMA60 才开", flush=True)
        filt = run_fixed_r(POOL + ["BTC"], data, bull, btc_filter=True, stopped=set(CONTROL))
        summarize("候选 C + BTC 1h 过滤（仅对照）", filt)
        print("\n注明：滑点往返写死为 0，结果偏乐观。默认 baseline 仍走原出场。实盘仍全 0。A/B 常量未改。")
        return

    if mode == ENTRY_NO_BE:
        run.entry = ENTRY_BASELINE
        run.exit_mode = "hard_tp"
        print(
            "\n候选 D baseline_no_early_be：入场与 baseline 相同（ATR≥下限、震荡不开、只做多）。"
            "出场只留开仓 ATR 止损和硬 TP（calc_tp_sl）。"
            "关闭早保本、移动止盈、48h 超时，也不做持仓中 ATR 重估。不改入场阈值。"
        )
        print("主结论：硬 TP + 开仓止损", flush=True)
        cand = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
        summarize("候选 D · 关掉早保本和移动止盈", cand)
        diagnose(cand["trades"])
        _passes(cand["trades"])
        print("\n对照（不当主结论，不参与通过线）：只关早保本，保留移动止盈、超时和持仓中 ATR 重估", flush=True)
        run.exit_mode = "no_be"
        alt = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
        summarize("对照 · 只关早保本", alt)
        diagnose(alt["trades"])
        print("\n注明：滑点往返写死为 0。baseline 出场调用未改。实盘仍全 0。A/B/C 结论不改。")
        return

    if mode == ENTRY_FUND:
        print(
            f"\n候选 E funding_carry_long：只用已结算资金费率 ≥ {FUND_MIN:g}（不用 90 日分位）。"
            "下一根 1h 收盘仍 > EMA20 才开多；1h ATR%≥"
            f"{min_atr * 100:.2f}%。SL={FUND_SL_ATR}×ATR，TP={FUND_TP_ATR}×ATR。"
            "无早保本/移动止盈/超时。同币成交后冷却 24h。不走 calc_score。"
        )
        funding = {}
        skipped = []
        for sym in POOL:
            arr = load_funding(sym)
            if len(arr) == 0:
                skipped.append(sym)
                continue
            t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
            t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
            print(f"  {sym} {len(arr)} 期  {t0:%Y-%m-%d} → {t1:%Y-%m-%d}")
            funding[sym] = arr
        if skipped:
            print("无费率、已跳过:", " ".join(skipped))
        else:
            print("五币费率都有，池未删减。")
        pool = [s for s in POOL if s in funding]
        cand = run_funding(pool, data, funding, FUND_MIN, stopped=set(CONTROL))
        summarize(f"候选 E · funding≥{FUND_MIN:g}", cand)
        oos_n = sum(1 for t in cand["trades"] if t["day"] >= SAMPLE_IN_END)
        if oos_n < PASS_OOS_MIN:
            print(f"结论：样本不足，不作通过（样本外 {oos_n} 笔 < {PASS_OOS_MIN}，不放宽阈值）")
        else:
            _passes(cand["trades"])
        print(f"\n敏感性（不当主结论，不翻案）：阈值改 {FUND_SENS:g}", flush=True)
        sens = run_funding(pool, data, funding, FUND_SENS, stopped=set(CONTROL))
        summarize(f"敏感性 · funding≥{FUND_SENS:g}", sens)
        print("\n注明：滑点往返写死为 0。费率用结算时刻之后的下一根 1h。baseline 未改。实盘仍全 0。")
        return

    print(
        f"\n基线入场：只做多 allow_short={sc.allow_short} allow_ranging={sc.allow_ranging} "
        f"ATR≥{min_atr * 100:.2f}%  池={POOL}"
    )
    print("\n跑纯数学入场（现行规则基线）…", flush=True)
    base = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
    summarize("纯数学 · 现行入场（ATR≥下限、只做多、震荡不开）", base)
    diagnose(base["trades"])

    print("\n跑大盘过滤（BTC 1h EMA20<EMA60 不开）…", flush=True)
    filt = run(POOL + ["BTC"], data, bull, btc_filter=True, stopped=set(CONTROL), seed=None)
    summarize("纯数学 + BTC 1h 均线过滤（仅回测）", filt)

    n = max(len(base["trades"]), 1)
    # 粗算概率：可开仓 15m 根数约 2年*96*5
    eligible = 2 * 365 * 96 * len(POOL)
    run.p = min(0.5, n / eligible * 8)  # 并发与持仓占用，放大以便笔数接近
    print(f"\n跑随机入场 p={run.p:.4f} …", flush=True)
    rnd = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=19)
    summarize("随机入场 + 同样出场（种子 19）", rnd)

    if "--with-stopped" in sys.argv:
        run.force = set(CONTROL)
        run.p = 0.0
        ctrl = run(POOL + CONTROL, data, bull, btc_filter=False, stopped=set(), seed=None)
        summarize("对照：BTC/ETH 也允许开仓", ctrl)

    buy_hold(POOL, data)
    print("\n注明：纯数学入场（情绪=0、无 AI），结论不是整套系统的死刑。"
          f"滑点往返写死为 {SLIPPAGE_RT:.3%}，止损按精确价成交，无跳空，结果偏乐观。"
          "ATR 止损只用上一根已收完的 15m。入场价=15m 收盘，出场从下一根 1m。"
          f"样本窗口写死：内 < {SAMPLE_IN_END}，外 ≥ {SAMPLE_IN_END}，禁止为凑正期望改。")


if __name__ == "__main__":
    main()
