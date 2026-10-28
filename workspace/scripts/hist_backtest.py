#!/usr/bin/env python3
"""
hist_backtest.py — 主网历史 K 线回测（离线，不改实盘）

信号与现网一致：15m 指标 + 1h EMA，只做多、震荡不开、评分门槛走 config。
情绪固定中性、不接 AI（无 ai_conflict / ai_range），称「纯数学入场」。
出场走 1m：开仓 ATR 止损 + 持仓中每根 15m 重估 ATR 止损（未进早保本/移动止盈时）
+ 早保本 1.5%→锁 0.5% + 移动止盈 + 硬 TP1 + 48h 超时。同一根 1m 先看止损。

用法：
  python3 hist_backtest.py                  # 下载缓存并跑现行池
  python3 hist_backtest.py --with-stopped   # 额外把 BTC/ETH 当可开仓对照
  python3 hist_backtest.py --no-download    # 只用已有缓存
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
from config import get_exit_config, get_score_config, max_positions_for_symbol  # noqa: E402
from strategy_layer import calc_atr_sl_pct, calc_tp_sl, set_sentiment_engine  # noqa: E402

CST = timezone(timedelta(hours=8))
CACHE = Path.home() / ".cache" / "morgan_hist"
VISION = "https://data.binance.vision/data/futures/um"
FEE_RT = 0.001          # 往返，与 exit_replay 一致；名义 200U → 约 0.2U
NOTIONAL = 200.0
START = "2024-10-01"
POOL = ["SOL", "BNB", "DOT", "LINK", "XRP"]
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


def _prepare_signals(series: dict):
    """每个 15m 下标是否纯数学做多。情绪=0，与 calc_score 同式。"""
    sc = get_score_config()
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
            th = sc.gate(reg, sym)
            if th is not None and ls > ss and ls >= th:
                sig[i] = True
                score[i] = ls
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
    print("  预计算 15m 信号…", flush=True)
    _prepare_signals(series)

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

    tradable = [s for s in symbols if s in series and (s in force or (s not in stopped and max_positions_for_symbol(s) > 0))]

    for ts in clock:
        done = [s for s, p in positions.items() if p["exit_ts"] <= ts]
        for sname in done:
            p = positions.pop(sname)
            pnl = p["ret"] * NOTIONAL - NOTIONAL * FEE_RT
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            trades.append({**p, "pnl": pnl, "equity": equity})
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
            updater = (lambda sym=sym, entry=entry: (lambda t, sym=sym, entry=entry: atr_sl_fn(sym, t, entry)))()
            _i, ret, reason = walk_exit(
                s["m1"], i0, entry, tp_sl["tp1_price"], tp_sl["sl_price"], tp_sl["tp_pct"],
                timeout_h, be, tiers, updater,
            )
            positions[sym] = {
                "symbol": sym, "entry_ts": entry_ms, "exit_ts": int(s["m1"][_i, 0]),
                "ret": ret, "reason": reason, "score": score, "regime": regime,
                "month": datetime.fromtimestamp(entry_ms / 1000, CST).strftime("%Y-%m"),
            }
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


def summarize(label, result):
    trades = result["trades"]
    n = len(trades)
    if not n:
        print(f"\n## {label}\n无成交")
        return
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    tp1 = sum(1 for t in trades if t["reason"] == "TP1")
    print(f"\n## {label}")
    print(f"笔数 {n}  扣费后合计 {sum(pnls):+.1f}U  期望 {np.mean(pnls):+.3f}U/笔  "
          f"胜率 {100*len(wins)/n:.1f}%  最大回撤 {result['max_dd']:.1f}U  TP1触发 {tp1} ({100*tp1/n:.1f}%)")
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
    # 预计算 BTC 15m 序列进 data 结构：run() 里 series["BTC"]
    run.force = set()
    run.p = 0.0
    print("\n跑纯数学入场（现行池，BTC/ETH 停开）…", flush=True)
    base = run(POOL + ["BTC"], data, bull, btc_filter=False, stopped=set(CONTROL), seed=None)
    summarize("纯数学 · 现行池（停 BTC/ETH）", base)

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
          "止损按精确价成交，无滑点、无跳空，结果偏乐观。"
          "ATR 止损只用上一根已收完的 15m。入场价=15m 收盘，出场从下一根 1m。")


if __name__ == "__main__":
    main()
