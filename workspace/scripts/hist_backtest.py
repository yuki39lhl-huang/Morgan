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
  python3 hist_backtest.py --entry xs_rel_strength   # 候选 F，截面排序；有 1m 缓存则重采样，否则下 1h
  python3 hist_backtest.py --entry xs_mean_revert    # 候选 G，残差均值回归；同样读 1h
  python3 hist_backtest.py --entry vol_compress_break  # 候选 H，压缩后突破；同样读 1h
  python3 hist_backtest.py --entry funding_extreme_fade  # 候选 I，费率极端反向；另下资金费率
  python3 hist_backtest.py --entry donchian_channel  # 候选 J，唐奇安通道；读 1h 再合成 4h
  python3 hist_backtest.py --entry pair_spread_revert  # 候选 K，SOL/BNB 对数价差回归
  python3 hist_backtest.py --entry utc_orb  # 候选 L，UTC 日开盘区间突破
  python3 hist_backtest.py --entry dual_ma_cross  # 候选 M，快慢均线交叉
  python3 hist_backtest.py --entry rsi_fade  # 候选 N，单币 RSI 极值反向
  python3 hist_backtest.py --with-stopped   # 额外把 BTC/ETH 当可开仓对照
"""
import calendar
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
# 候选 F。只在固定池里比谁强谁弱。常量写死，禁止网格。不走 A–E 的 ATR / 均线 / 突破 / 费率。
ENTRY_XS = "xs_rel_strength"
XS_BAR_MIN = 240          # 4h 收盘才调仓
XS_LOOKBACK = 6           # 6 根 4h = 24h 收益
# 候选 G。和 F 不是同一族：不做动量排名，做短期特异残差的均值回归。常量写死，禁止网格。
ENTRY_REV = "xs_mean_revert"
REV_Z_WIN = 48            # 自身过去 48 根 1h 残差，不含当根
REV_Z_ENTER = 1.0         # |z| 达到 1 才逆向开仓
REV_MAX_HOLD_H = 6        # 残差没回到 0 就满 6 小时平掉
# 候选 H。波动压缩后的区间突破。不是 F 的排名，也不是 G 的残差 z。常量写死，禁止网格。
ENTRY_COMP = "vol_compress_break"
COMP_SQUEEZE = 12         # 信号棒之前的窄区间，1h 根数
COMP_BASE = 48            # 窄区间再往前的对照区间
COMP_RATIO = 0.5          # 窄区间宽度 <= 对照宽度的一半
COMP_HOLD_H = 12          # 没打到区间另一边就满 12 小时平
# 候选 I。费率相对自身近期历史的极端尾部，反向平拥挤。不是 E 的绝对阈值+均线+ATR。常量写死。
ENTRY_FADE = "funding_extreme_fade"
FADE_Z_WIN = 90           # 不含当次的已结算期数（8h 一期，约 30 天）
FADE_Z_ENTER = 2.0        # |z| 达到 2 才反向
FADE_MAX_HOLD_H = 24      # 费率 z 没回到 0 就满 24 小时平
# 候选 J。唐奇安通道，海龟系统 1：20 根进、10 根出。用 4h，不用日线。
# 日线 20 根的持仓比样本外这一个多月还长，笔数会先被日历卡住。这一步在看盈亏之前定死。
# 不要求波动压缩，不排名，不算残差，不看费率，没有 ATR% 和早保本。常量写死，禁止网格。
ENTRY_DON = "donchian_channel"
DON_BAR_H = 4
DON_ENTRY_N = 20
DON_EXIT_N = 10
# 候选 K。只做一对：SOL 对 BNB 的对数价格价差。β 固定 1，不做滚动回归。
# z 用过去 72 根 1h（不含当根）。|z|≥2 才反向。回到 0 或满 24 小时平。
# 这一档在看盈亏之前写死。不是 G 的五币收益残差，也不是把 |z| 从 2 降下来。
ENTRY_PAIR = "pair_spread_revert"
PAIR_Y = "SOL"
PAIR_X = "BNB"
PAIR_Z_WIN = 72
PAIR_Z_ENTER = 2.0
PAIR_MAX_HOLD_H = 24
PAIR_LEG = NOTIONAL / 2.0   # 两条腿各 100U，合计名义仍是 200U，一对算一笔
# 候选 L。每个 UTC 日单独做开盘区间。前 4 根 1h（00:00–03:00 UTC）锁高低。
# 之后收盘突破这个区间才顺势。打到区间另一边、满 8 小时，或 UTC 日切，就平。
# 这一档在看盈亏之前写死。不是滚动 N 根唐奇安，也不要求先压缩。
ENTRY_ORB = "utc_orb"
ORB_OPEN_H = 4
ORB_HOLD_H = 8
# 候选 M。快慢简单均线交叉。10 根和 30 根 1h，在看盈亏之前写死。
# 不用 50/200：那一对在样本外这一个多月里交叉太少，笔数会先被日历卡住。
# 死区只用于已经持仓、且还没反向交叉时把缠在一起的均线平掉。交叉当根照开。
# 不是开盘区间，不是唐奇安，不要求压缩。
ENTRY_MA = "dual_ma_cross"
MA_FAST = 10
MA_SLOW = 30
MA_DEAD = 0.0001          # |快-慢| / 收盘 < 1bp 视为缠在一起
# 候选 N。阈值在 rsi_fade.py，和测试网试跑共用。14、70/30、50、24 小时写死。
from rsi_fade import (  # noqa: E402
    RSI_HOLD_H,
    RSI_HIGH,
    RSI_LOW,
    RSI_MID,
    RSI_N,
    rsi_back_to_mid,
    rsi_side,
    wilder_rsi,
)
ENTRY_RSI = "rsi_fade"
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


def _daily_rows(symbol: str, interval: str, mo: str, end_day: str) -> list[tuple]:
    """某月日包。月包还没出时用。晚于 end_day 的日期不请求。"""
    y, m = int(mo[:4]), int(mo[5:7])
    last = calendar.monthrange(y, m)[1]
    end = datetime.fromisoformat(end_day).date()
    rows = []
    got = 0
    for d in range(1, last + 1):
        ds_date = datetime(y, m, d).date()
        if ds_date > end:
            break
        ds = ds_date.isoformat()
        url = f"{VISION}/daily/klines/{symbol}USDT/{interval}/{symbol}USDT-{interval}-{ds}.zip"
        blob = _get(url)
        if blob:
            rows.extend(_parse_zip(blob))
            got += 1
    if got:
        print(f"  {symbol} {mo} 日包 {got} 天", flush=True)
    return rows


def download_symbol(symbol: str, end_day: str, interval: str = "1m") -> np.ndarray:
    """OHLCV，列 t,h,l,c,v。月包优先，没有再下日包。interval 默认 1m，缓存名与旧版一致。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    dest = CACHE / f"{symbol}USDT_{interval}_{START}_{end_day}.npz"
    if dest.exists():
        return np.load(dest)["a"]
    rows = []
    months = list(_months(START, end_day))
    for mo in months[:-1]:
        url = f"{VISION}/monthly/klines/{symbol}USDT/{interval}/{symbol}USDT-{interval}-{mo}.zip"
        blob = _get(url)
        if blob is None:
            alt = _daily_rows(symbol, interval, mo, end_day)
            if not alt:
                raise RuntimeError(f"缺少月线 {symbol} {mo}")
            rows.extend(alt)
        else:
            rows.extend(_parse_zip(blob))
        print(f"  {symbol} {mo} {len(rows)}", flush=True)
    # 最后一个月：日包（月包往往未出）
    y, m = int(months[-1][:4]), int(months[-1][5:7])
    day = datetime(y, m, 1, tzinfo=CST)
    end = datetime.fromisoformat(end_day).replace(tzinfo=CST)
    while day.date() <= end.date():
        ds = day.strftime("%Y-%m-%d")
        url = f"{VISION}/daily/klines/{symbol}USDT/{interval}/{symbol}USDT-{interval}-{ds}.zip"
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


def load_1h(symbol: str, end_day: str, allow_download: bool) -> tuple[np.ndarray, str]:
    """已有 1m 缓存就重采样成 1h，和基线用同一套收盘价。否则读或下 1h。"""
    hits_1m = sorted(CACHE.glob(f"{symbol}USDT_1m_{START}_*.npz"))
    if hits_1m:
        return resample(np.load(hits_1m[-1])["a"], 60), f"1m重采样:{hits_1m[-1].name}"
    exact = CACHE / f"{symbol}USDT_1h_{START}_{end_day}.npz"
    if exact.exists():
        return np.load(exact)["a"], f"1h缓存:{exact.name}"
    if not allow_download:
        hits = sorted(CACHE.glob(f"{symbol}USDT_1h_{START}_*.npz"))
        if not hits:
            raise SystemExit(f"无缓存 {symbol} 1h/1m")
        return np.load(hits[-1])["a"], f"1h缓存:{hits[-1].name}"
    return download_symbol(symbol, end_day, interval="1h"), "1h下载"


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


def _to_4h(bars_1h: np.ndarray) -> np.ndarray:
    """1h → 4h。桶里要有 4 根、并且跨满 3 小时才算收完。
    不复用 resample()：那条「差 1 分钟算没收完」是给 1m 源写的，拿 1h 源会把最后一根完整 4h 丢掉。
    """
    if len(bars_1h) == 0:
        return np.zeros((0, 5), dtype=np.float64)
    step = XS_BAR_MIN * 60_000
    t = bars_1h[:, 0].astype(np.int64)
    bucket = (t // step) * step
    change = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.r_[0, change]
    ends = np.r_[change, len(bars_1h)]
    rows = []
    for a, b in zip(starts, ends):
        if b - a < 4:
            continue
        sl = bars_1h[a:b]
        if int(sl[-1, 0]) - int(sl[0, 0]) < 3 * 3_600_000:
            continue
        rows.append((
            float(bucket[a]),
            float(sl[:, 1].max()),
            float(sl[:, 2].min()),
            float(sl[-1, 3]),
            float(sl[:, 4].sum()),
        ))
    if not rows:
        return np.zeros((0, 5), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def xs_targets(rets: dict, positions: dict) -> tuple:
    """最强做多、最弱做空。并列时不按币名挑；手里已经有并列里的一只就留着，否则这侧空仓。"""
    if len(rets) < 2:
        return None, None
    vals = list(rets.values())
    lo, hi = min(vals), max(vals)
    if not hi > lo:
        return None, None
    strong = [s for s, v in rets.items() if v == hi]
    weak = [s for s, v in rets.items() if v == lo]

    def pick(members, side):
        if len(members) == 1:
            return members[0]
        held = [s for s in members if positions.get(s, {}).get("side") == side]
        if len(held) == 1:
            return held[0]
        return None

    long_s, short_s = pick(strong, "LONG"), pick(weak, "SHORT")
    if long_s is not None and long_s == short_s:
        return None, None
    return long_s, short_s


def _book_xs(positions, sym, exit_px, exit_ts, reason, trades, state, regime="xs"):
    p = positions.pop(sym)
    entry = p["entry"]
    if p["side"] == "LONG":
        ret = (exit_px - entry) / entry
    else:
        ret = (entry - exit_px) / entry
    gross = ret * NOTIONAL
    cost = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
    pnl = gross - cost
    state["equity"] += pnl
    state["peak"] = max(state["peak"], state["equity"])
    state["max_dd"] = max(state["max_dd"], state["peak"] - state["equity"])
    day = datetime.fromtimestamp(p["entry_ts"] / 1000, CST).strftime("%Y-%m-%d")
    trades.append({
        "symbol": sym,
        "entry_ts": p["entry_ts"],
        "exit_ts": exit_ts,
        "entry": entry,
        "exit": exit_px,
        "ret": ret,
        "reason": reason,
        "score": p["ret_sig"],
        "regime": regime,
        "month": datetime.fromtimestamp(p["entry_ts"] / 1000, CST).strftime("%Y-%m"),
        "pnl": pnl,
        "gross": gross,
        "fee": cost,
        "direction": p["side"],
        "day": day,
        "equity": state["equity"],
    })


def _xs_result(trades, state, n_dec, n_skip):
    return {
        "trades": trades,
        "max_dd": state["max_dd"],
        "equity": state["equity"],
        "n_dec": n_dec,
        "n_skip": n_skip,
    }


def run_xs(data_1h: dict, pool: list[str]):
    """4h 收盘按 24h 收益排序。多最强、空最弱，名次不变就续持。不调用 walk_exit / calc_score。"""
    if len(pool) < 2:
        raise ValueError("截面至少要 2 个币")
    bars = {}
    ts_maps = {}
    for sym in pool:
        b = _to_4h(data_1h[sym])
        bars[sym] = b
        ts_maps[sym] = {int(t): i for i, t in enumerate(b[:, 0])} if len(b) else {}
    common_sets = [set(ts_maps[s]) for s in pool]
    common = np.array(sorted(set.intersection(*common_sets)), dtype=np.int64) if common_sets else np.zeros(0, dtype=np.int64)
    step = XS_BAR_MIN * 60_000
    need = XS_LOOKBACK * step
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_skip = 0
    n_dec = 0
    if len(common) <= XS_LOOKBACK:
        return _xs_result(trades, state, n_dec, n_skip)
    for i in range(XS_LOOKBACK, len(common)):
        ts = int(common[i])
        ts_lb = int(common[i - XS_LOOKBACK])
        if ts - ts_lb != need:
            n_skip += 1
            continue
        rets = {}
        px = {}
        bad = False
        for sym in pool:
            c0 = float(bars[sym][ts_maps[sym][ts_lb], 3])
            c1 = float(bars[sym][ts_maps[sym][ts], 3])
            if c0 <= 0 or c1 <= 0:
                bad = True
                break
            rets[sym] = c1 / c0 - 1.0
            px[sym] = c1
        if bad or len(rets) != len(pool):
            n_skip += 1
            continue
        n_dec += 1
        fill_ts = ts + step
        tgt_l, tgt_s = xs_targets(rets, positions)
        for sym in list(positions):
            side = positions[sym]["side"]
            keep = (side == "LONG" and sym == tgt_l) or (side == "SHORT" and sym == tgt_s)
            if not keep:
                _book_xs(positions, sym, px[sym], fill_ts, "rebalance", trades, state)
        if i == len(common) - 1:
            continue
        if tgt_l and tgt_l not in positions:
            positions[tgt_l] = {
                "side": "LONG", "entry": px[tgt_l], "entry_ts": fill_ts, "ret_sig": rets[tgt_l],
            }
        if tgt_s and tgt_s not in positions:
            positions[tgt_s] = {
                "side": "SHORT", "entry": px[tgt_s], "entry_ts": fill_ts, "ret_sig": rets[tgt_s],
            }
    if len(common):
        last_ts = int(common[-1])
        last_fill = last_ts + step
        for sym in list(positions):
            exit_px = float(bars[sym][ts_maps[sym][last_ts], 3])
            _book_xs(positions, sym, exit_px, last_fill, "eod", trades, state)
    return _xs_result(trades, state, n_dec, n_skip)


def idiosyncratic(rets: np.ndarray) -> np.ndarray:
    """收益减去等权篮子。有非有限值时整段作废，不拿缺币的篮子凑。"""
    out = np.full(rets.shape, np.nan, dtype=np.float64)
    if rets.ndim == 1:
        if np.isfinite(rets).all():
            out[:] = rets - float(rets.mean())
        return out
    ok = np.isfinite(rets).all(axis=1)
    if ok.any():
        mu = rets[ok].mean(axis=1)
        out[ok] = rets[ok] - mu[:, None]
    return out


def own_residual_z(resid_hist: np.ndarray, resid_now: np.ndarray) -> np.ndarray:
    """自身残差 z。hist 不含当根，标准差用总体（除以窗口长度）。标准差为 0 则是 nan。"""
    mu = resid_hist.mean(axis=0)
    sd = resid_hist.std(axis=0)
    z = np.full(resid_now.shape, np.nan, dtype=np.float64)
    ok = np.isfinite(sd) & (sd > 0) & np.isfinite(resid_now) & np.isfinite(mu)
    z[ok] = (resid_now[ok] - mu[ok]) / sd[ok]
    return z


def revert_sides(z: dict, positions: dict) -> tuple:
    """z>=阈值做空领涨的残差，z<=-阈值做多落后的残差。各只取最极端的一只。
    这一侧已经有仓就不再开。并列不按币名挑。已持有的币不另开一边。
    """
    busy = set(positions)

    def extreme(want_short: bool):
        side = "SHORT" if want_short else "LONG"
        if any(p.get("side") == side for p in positions.values()):
            return None
        cands = {}
        for sym, val in z.items():
            if sym in busy or not np.isfinite(val):
                continue
            if want_short and val >= REV_Z_ENTER:
                cands[sym] = float(val)
            if not want_short and val <= -REV_Z_ENTER:
                cands[sym] = float(val)
        if not cands:
            return None
        target = max(cands.values()) if want_short else min(cands.values())
        members = [s for s, v in cands.items() if v == target]
        if len(members) != 1:
            return None
        return members[0]

    return extreme(False), extreme(True)


def run_revert(data_1h: dict, pool: list[str]):
    """1h 特异残差相对自身历史做逆向。残差回到 0 或满 6 小时平仓。不走排名续持，也不走 walk_exit。"""
    if len(pool) < 2:
        raise ValueError("截面至少要 2 个币")
    ts_maps = {}
    for sym in pool:
        b = data_1h[sym]
        ts_maps[sym] = {int(t): i for i, t in enumerate(b[:, 0])} if len(b) else {}
    common_sets = [set(ts_maps[s]) for s in pool]
    common = np.array(sorted(set.intersection(*common_sets)), dtype=np.int64) if common_sets else np.zeros(0, dtype=np.int64)
    step = 3_600_000
    n_sym = len(pool)
    closes = np.zeros((len(common), n_sym), dtype=np.float64)
    for k, sym in enumerate(pool):
        idx = [ts_maps[sym][int(t)] for t in common]
        closes[:, k] = data_1h[sym][idx, 3]
    resid = np.full((len(common), n_sym), np.nan, dtype=np.float64)
    if len(common) >= 2:
        raw = np.full_like(resid, np.nan)
        prev = closes[:-1]
        nxt = closes[1:]
        ok = (prev > 0) & (nxt > 0) & (np.diff(common)[:, None] == step)
        with np.errstate(divide="ignore", invalid="ignore"):
            raw[1:] = np.where(ok, nxt / prev - 1.0, np.nan)
        resid[1:] = idiosyncratic(raw[1:])
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_skip = 0
    n_dec = 0
    span = REV_Z_WIN + 1
    if len(common) <= span:
        return _xs_result(trades, state, n_dec, n_skip)

    def z_at(i: int):
        if int(common[i]) - int(common[i - span]) != span * step:
            return None
        hist = resid[i - REV_Z_WIN:i]
        cur = resid[i]
        if not np.isfinite(hist).all() or not np.isfinite(cur).all():
            return None
        z = own_residual_z(hist, cur)
        if not np.isfinite(z).all():
            return None
        return {pool[k]: float(z[k]) for k in range(n_sym)}

    for i in range(span, len(common)):
        fill_ts = int(common[i]) + step
        px = {pool[k]: float(closes[i, k]) for k in range(n_sym)}
        z = z_at(i)
        if z is None:
            n_skip += 1
            for sym in list(positions):
                if fill_ts - positions[sym]["entry_ts"] >= REV_MAX_HOLD_H * step:
                    _book_xs(positions, sym, px[sym], fill_ts, "time", trades, state, regime="revert")
            continue
        n_dec += 1
        for sym in list(positions):
            p = positions[sym]
            zz = z[sym]
            revert = (p["side"] == "LONG" and zz >= 0) or (p["side"] == "SHORT" and zz <= 0)
            held_h = fill_ts - p["entry_ts"]
            if revert:
                _book_xs(positions, sym, px[sym], fill_ts, "revert", trades, state, regime="revert")
            elif held_h >= REV_MAX_HOLD_H * step:
                _book_xs(positions, sym, px[sym], fill_ts, "time", trades, state, regime="revert")
        if i == len(common) - 1:
            continue
        tgt_l, tgt_s = revert_sides(z, positions)
        if tgt_l and tgt_l not in positions:
            positions[tgt_l] = {
                "side": "LONG", "entry": px[tgt_l], "entry_ts": fill_ts, "ret_sig": z[tgt_l],
            }
        if tgt_s and tgt_s not in positions:
            positions[tgt_s] = {
                "side": "SHORT", "entry": px[tgt_s], "entry_ts": fill_ts, "ret_sig": z[tgt_s],
            }
    if len(common):
        last_fill = int(common[-1]) + step
        for sym in list(positions):
            k = pool.index(sym)
            _book_xs(
                positions, sym, float(closes[-1, k]), last_fill, "eod", trades, state, regime="revert",
            )
    return _xs_result(trades, state, n_dec, n_skip)


def compression_break(high: np.ndarray, low: np.ndarray, close: np.ndarray):
    """最后一根是信号棒。前面 COMP_SQUEEZE 根是盒子，再往前 COMP_BASE 根是对照。
    盒子宽度不超过对照的 COMP_RATIO，且收盘突破盒子，才给方向。
    返回 (方向或 None, 盒子高, 盒子低, 宽度比)。长度不对则方向是 None。
    """
    need = COMP_BASE + COMP_SQUEEZE + 1
    if len(close) != need or len(high) != need or len(low) != need:
        return None, np.nan, np.nan, np.nan
    base_w = float(np.max(high[:COMP_BASE]) - np.min(low[:COMP_BASE]))
    box_high = float(np.max(high[COMP_BASE:COMP_BASE + COMP_SQUEEZE]))
    box_low = float(np.min(low[COMP_BASE:COMP_BASE + COMP_SQUEEZE]))
    squeeze_w = box_high - box_low
    if not (base_w > 0 and squeeze_w > 0):
        return None, box_high, box_low, np.nan
    ratio = squeeze_w / base_w
    if ratio > COMP_RATIO:
        return None, box_high, box_low, ratio
    px = float(close[-1])
    if px > box_high:
        return "LONG", box_high, box_low, ratio
    if px < box_low:
        return "SHORT", box_high, box_low, ratio
    return None, box_high, box_low, ratio


def run_compress(data_1h: dict, pool: list[str]):
    """单币：窄区间相对前一段被压扁之后，收盘突破该区间就顺着走。
    打到区间另一边，或满 12 小时，就平。不排名，不算残差 z，不走 ATR。
    """
    if len(pool) < 1:
        raise ValueError("至少要 1 个币")
    step = 3_600_000
    need = COMP_BASE + COMP_SQUEEZE + 1
    series = {}
    events = []
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        b = data_1h[sym]
        if len(b) == 0:
            series[sym] = None
            continue
        series[sym] = {
            "t": b[:, 0].astype(np.int64),
            "h": b[:, 1].astype(np.float64),
            "l": b[:, 2].astype(np.float64),
            "c": b[:, 3].astype(np.float64),
        }
        t = series[sym]["t"]
        for i in range(len(t)):
            events.append((int(t[i]) + step, order[sym], sym, i))
    events.sort()
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_skip = 0
    n_dec = 0
    idx = 0
    while idx < len(events):
        fill_ts = events[idx][0]
        group = []
        while idx < len(events) and events[idx][0] == fill_ts:
            group.append(events[idx])
            idx += 1
        for _fill, _ord, sym, i in group:
            if sym not in positions:
                continue
            p = positions[sym]
            if p["entry_ts"] >= fill_ts:
                continue
            px = float(series[sym]["c"][i])
            if p["side"] == "LONG" and px <= p["box_low"]:
                _book_xs(positions, sym, px, fill_ts, "structure", trades, state, regime="compress")
            elif p["side"] == "SHORT" and px >= p["box_high"]:
                _book_xs(positions, sym, px, fill_ts, "structure", trades, state, regime="compress")
            elif fill_ts - p["entry_ts"] >= COMP_HOLD_H * step:
                _book_xs(positions, sym, px, fill_ts, "time", trades, state, regime="compress")
        for _fill, _ord, sym, i in group:
            s = series[sym]
            if sym in positions or len(positions) >= MAX_POS:
                continue
            if i < need - 1 or i == len(s["t"]) - 1:
                continue
            t = s["t"]
            i0 = i - (need - 1)
            if int(t[i]) - int(t[i0]) != (need - 1) * step:
                n_skip += 1
                continue
            side, box_high, box_low, ratio = compression_break(
                s["h"][i0:i + 1], s["l"][i0:i + 1], s["c"][i0:i + 1],
            )
            n_dec += 1
            if side is None or not np.isfinite(ratio):
                continue
            positions[sym] = {
                "side": side,
                "entry": float(s["c"][i]),
                "entry_ts": fill_ts,
                "ret_sig": float(ratio),
                "box_high": box_high,
                "box_low": box_low,
            }
    for sym in list(positions):
        s = series[sym]
        _book_xs(
            positions, sym, float(s["c"][-1]), int(s["t"][-1]) + step, "eod",
            trades, state, regime="compress",
        )
    return _xs_result(trades, state, n_dec, n_skip)


def funding_z(hist: np.ndarray, now: float) -> float:
    """自身过去 FADE_Z_WIN 期已结算费率的 z。不含当次。总体标准差。"""
    hist = np.asarray(hist, dtype=np.float64)
    if len(hist) != FADE_Z_WIN or not np.isfinite(hist).all() or not np.isfinite(now):
        return float("nan")
    sd = float(hist.std())
    if sd <= 0:
        return float("nan")
    return (float(now) - float(hist.mean())) / sd


def fade_side(z: float):
    """z 很高说明多头在付费，反向做空；z 很低说明空头在付费，反向做多。"""
    if not np.isfinite(z):
        return None
    if z >= FADE_Z_ENTER:
        return "SHORT"
    if z <= -FADE_Z_ENTER:
        return "LONG"
    return None


def funding_normalized(side: str, z: float) -> bool:
    """z 回到自身均值的另一侧，视为拥挤消退。"""
    if not np.isfinite(z):
        return False
    if side == "SHORT":
        return z <= 0.0
    if side == "LONG":
        return z >= 0.0
    return False


def _settle_hour(ft: int) -> int:
    """结算时间落到整点。文件里有的戳会多 1ms。"""
    return (int(ft) // 3_600_000) * 3_600_000


def _close_at_open(t_arr: np.ndarray, c_arr: np.ndarray, open_ts: int):
    j = int(np.searchsorted(t_arr, open_ts, side="left"))
    if j >= len(t_arr) or int(t_arr[j]) != int(open_ts) or not np.isfinite(c_arr[j]) or float(c_arr[j]) <= 0:
        return None
    return float(c_arr[j]), int(open_ts) + 3_600_000


def _simulate_funding_fade(symbol: str, t_arr, c_arr, times: np.ndarray, rates: np.ndarray):
    """单币成交列表。同一币不重叠。返回 (trades, n_signal, n_unpriced)。"""
    raw = []
    n_signal = 0
    n_unpriced = 0
    n = len(rates)
    w = FADE_Z_WIN
    step = 3_600_000
    i = w
    while i < n:
        z = funding_z(rates[i - w:i], float(rates[i]))
        side = fade_side(z)
        if side is None:
            i += 1
            continue
        n_signal += 1
        opened = _close_at_open(t_arr, c_arr, _settle_hour(int(times[i])))
        if opened is None:
            n_unpriced += 1
            i += 1
            continue
        entry_px, entry_fill = opened
        deadline = entry_fill + FADE_MAX_HOLD_H * step
        exit_px = exit_fill = None
        reason = None
        k = i + 1
        while k < n:
            z_k = funding_z(rates[k - w:k], float(rates[k]))
            marked = _close_at_open(t_arr, c_arr, _settle_hour(int(times[k])))
            if marked is None:
                k += 1
                continue
            px_k, fill_k = marked
            if fill_k > deadline:
                break
            if funding_normalized(side, z_k):
                exit_px, exit_fill, reason = px_k, fill_k, "normalize"
                k = k + 1
                break
            k += 1
        if exit_fill is None:
            timed = _close_at_open(t_arr, c_arr, deadline - step)
            if timed is not None and timed[1] > entry_fill:
                exit_px, exit_fill = timed
                reason = "time"
            else:
                last_fill = int(t_arr[-1]) + step
                if last_fill > entry_fill:
                    exit_px, exit_fill, reason = float(c_arr[-1]), last_fill, "eod"
                else:
                    n_unpriced += 1
                    i += 1
                    continue
        raw.append({
            "symbol": symbol,
            "side": side,
            "entry": entry_px,
            "exit": exit_px,
            "entry_ts": entry_fill,
            "exit_ts": exit_fill,
            "reason": reason,
            "ret_sig": z,
        })
        i = k
    return raw, n_signal, n_unpriced


def run_funding_fade(data_1h: dict, funding: dict, pool: list[str]):
    """费率 z 极端就反向。z 回到 0 或满 24 小时平。不看均线，不看 ATR。"""
    raw = []
    n_signal = 0
    n_unpriced = 0
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        fr = funding.get(sym)
        bars = data_1h.get(sym)
        if fr is None or len(fr) == 0 or bars is None or len(bars) == 0:
            continue
        times = fr[:, 0].astype(np.int64)
        rates = fr[:, 1].astype(np.float64)
        got, n_sig, n_miss = _simulate_funding_fade(sym, bars[:, 0].astype(np.int64), bars[:, 3], times, rates)
        raw.extend(got)
        n_signal += n_sig
        n_unpriced += n_miss
    raw.sort(key=lambda t: (t["entry_ts"], order.get(t["symbol"], 99)))
    accepted = []
    for t in raw:
        alive = [a for a in accepted if a["entry_ts"] <= t["entry_ts"] < a["exit_ts"]]
        if len(alive) >= MAX_POS:
            continue
        accepted.append(t)
    accepted.sort(key=lambda t: (t["exit_ts"], order.get(t["symbol"], 99)))
    positions_book = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    # _book_xs 按币名当键，这里逐笔平掉，键用递增序号避免互相覆盖
    for n, t in enumerate(accepted):
        key = f"{t['symbol']}#{n}"
        positions_book[key] = {
            "side": t["side"],
            "entry": t["entry"],
            "entry_ts": t["entry_ts"],
            "ret_sig": t["ret_sig"],
        }
        _book_xs(
            positions_book, key, t["exit"], t["exit_ts"], t["reason"], trades, state, regime="funding_fade",
        )
        trades[-1]["symbol"] = t["symbol"]
    out = _xs_result(trades, state, n_signal, n_unpriced)
    out["n_signal"] = n_signal
    out["n_dropped"] = len(raw) - len(accepted)
    return out


def _to_don_bars(bars_1h: np.ndarray) -> np.ndarray:
    """1h → DON_BAR_H 小时。桶里根数要够，并且跨满，才算收完。缺的小时丢掉，不插值。"""
    if len(bars_1h) == 0:
        return np.zeros((0, 5), dtype=np.float64)
    step = DON_BAR_H * 3_600_000
    t = bars_1h[:, 0].astype(np.int64)
    bucket = (t // step) * step
    change = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.r_[0, change]
    ends = np.r_[change, len(bars_1h)]
    rows = []
    for a, b in zip(starts, ends):
        if b - a < DON_BAR_H:
            continue
        sl = bars_1h[a:b]
        if int(sl[-1, 0]) - int(sl[0, 0]) < (DON_BAR_H - 1) * 3_600_000:
            continue
        rows.append((
            float(bucket[a]),
            float(sl[:, 1].max()),
            float(sl[:, 2].min()),
            float(sl[-1, 3]),
            float(sl[:, 4].sum()),
        ))
    if not rows:
        return np.zeros((0, 5), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def donchian_entry(high: np.ndarray, low: np.ndarray, close: np.ndarray):
    """收盘高于前 DON_ENTRY_N 根最高价做多，低于前 DON_ENTRY_N 根最低价做空。
    不含当根。影线不算。长度不对就不开。
    """
    n = DON_ENTRY_N + 1
    if len(close) != n or len(high) != n or len(low) != n:
        return None
    if not np.isfinite(high).all() or not np.isfinite(low).all() or not np.isfinite(close[-1]):
        return None
    px = float(close[-1])
    if px <= 0:
        return None
    if px > float(np.max(high[:-1])):
        return "LONG"
    if px < float(np.min(low[:-1])):
        return "SHORT"
    return None


def donchian_exit(side: str, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> bool:
    """多单收盘跌破前 DON_EXIT_N 根最低价，空单收盘升破前 DON_EXIT_N 根最高价。不含当根。"""
    n = DON_EXIT_N + 1
    if len(close) != n or len(high) != n or len(low) != n:
        return False
    if not np.isfinite(high).all() or not np.isfinite(low).all() or not np.isfinite(close[-1]):
        return False
    px = float(close[-1])
    if side == "LONG":
        return px < float(np.min(low[:-1]))
    if side == "SHORT":
        return px > float(np.max(high[:-1]))
    return False


def run_donchian(data_1h: dict, pool: list[str]):
    """4h 收盘突破前 20 根通道就顺着做。打到前 10 根的另一边就平。不看压缩、排名、残差、费率、ATR。"""
    if len(pool) < 1:
        raise ValueError("至少要 1 个币")
    step = DON_BAR_H * 3_600_000
    series = {}
    events = []
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        b = _to_don_bars(data_1h[sym])
        if len(b) == 0:
            continue
        series[sym] = {
            "t": b[:, 0].astype(np.int64),
            "h": b[:, 1].astype(np.float64),
            "l": b[:, 2].astype(np.float64),
            "c": b[:, 3].astype(np.float64),
        }
        t = series[sym]["t"]
        for i in range(len(t)):
            events.append((int(t[i]) + step, order[sym], sym, i))
    events.sort()
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_skip = 0
    n_dec = 0
    n_capped = 0
    idx = 0
    while idx < len(events):
        fill_ts = events[idx][0]
        group = []
        while idx < len(events) and events[idx][0] == fill_ts:
            group.append(events[idx])
            idx += 1
        for _fill, _ord, sym, i in group:
            if sym not in positions:
                continue
            p = positions[sym]
            if p["entry_ts"] >= fill_ts:
                continue
            s = series[sym]
            if i < DON_EXIT_N:
                continue
            if int(s["t"][i]) - int(s["t"][i - DON_EXIT_N]) != DON_EXIT_N * step:
                n_skip += 1
                continue
            sl = slice(i - DON_EXIT_N, i + 1)
            if donchian_exit(p["side"], s["h"][sl], s["l"][sl], s["c"][sl]):
                _book_xs(
                    positions, sym, float(s["c"][i]), fill_ts, "channel",
                    trades, state, regime="donchian",
                )
        for _fill, _ord, sym, i in group:
            s = series[sym]
            if sym in positions:
                continue
            if i < DON_ENTRY_N or i == len(s["t"]) - 1:
                continue
            if int(s["t"][i]) - int(s["t"][i - DON_ENTRY_N]) != DON_ENTRY_N * step:
                n_skip += 1
                continue
            sl = slice(i - DON_ENTRY_N, i + 1)
            side = donchian_entry(s["h"][sl], s["l"][sl], s["c"][sl])
            n_dec += 1
            if side is None:
                continue
            if len(positions) >= MAX_POS:
                n_capped += 1
                continue
            px = float(s["c"][i])
            hi = float(np.max(s["h"][i - DON_ENTRY_N:i]))
            lo = float(np.min(s["l"][i - DON_ENTRY_N:i]))
            dist = (px / hi - 1.0) if side == "LONG" else (lo / px - 1.0)
            positions[sym] = {
                "side": side,
                "entry": px,
                "entry_ts": fill_ts,
                "ret_sig": dist,
            }
    for sym in list(positions):
        s = series[sym]
        _book_xs(
            positions, sym, float(s["c"][-1]), int(s["t"][-1]) + step, "eod",
            trades, state, regime="donchian",
        )
    out = _xs_result(trades, state, n_dec, n_skip)
    out["n_capped"] = n_capped
    return out


def spread_z(hist: np.ndarray, now: float) -> float:
    """对数价差相对自己过去 PAIR_Z_WIN 根的 z。不含当根。总体标准差。"""
    hist = np.asarray(hist, dtype=np.float64)
    if len(hist) != PAIR_Z_WIN or not np.isfinite(hist).all() or not np.isfinite(now):
        return float("nan")
    sd = float(hist.std())
    if sd <= 0:
        return float("nan")
    return (float(now) - float(hist.mean())) / sd


def pair_side(z: float):
    """价差 z 很高：SOL 相对 BNB 偏贵，做空价差。很低则做多价差。"""
    if not np.isfinite(z):
        return None
    if z >= PAIR_Z_ENTER:
        return "SHORT"
    if z <= -PAIR_Z_ENTER:
        return "LONG"
    return None


def spread_normalized(side: str, z: float) -> bool:
    if not np.isfinite(z):
        return False
    if side == "SHORT":
        return z <= 0.0
    if side == "LONG":
        return z >= 0.0
    return False


def pair_gross(side: str, sol0: float, bnb0: float, sol1: float, bnb1: float) -> float:
    """多价差 = 多 SOL、空 BNB。空价差反过来。两条腿各 PAIR_LEG。"""
    if min(sol0, bnb0, sol1, bnb1) <= 0:
        return float("nan")
    if side == "LONG":
        sol_pnl = PAIR_LEG * (sol1 / sol0 - 1.0)
        bnb_pnl = PAIR_LEG * (1.0 - bnb1 / bnb0)
    elif side == "SHORT":
        sol_pnl = PAIR_LEG * (1.0 - sol1 / sol0)
        bnb_pnl = PAIR_LEG * (bnb1 / bnb0 - 1.0)
    else:
        return float("nan")
    return float(sol_pnl + bnb_pnl)


def _book_pair(side, sol0, bnb0, sol1, bnb1, entry_ts, exit_ts, reason, z, trades, state):
    gross = pair_gross(side, sol0, bnb0, sol1, bnb1)
    cost = NOTIONAL * (FEE_RT + SLIPPAGE_RT)
    pnl = gross - cost
    state["equity"] += pnl
    state["peak"] = max(state["peak"], state["equity"])
    state["max_dd"] = max(state["max_dd"], state["peak"] - state["equity"])
    day = datetime.fromtimestamp(entry_ts / 1000, CST).strftime("%Y-%m-%d")
    trades.append({
        "symbol": f"{PAIR_Y}/{PAIR_X}",
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry": float(np.log(sol0) - np.log(bnb0)),
        "exit": float(np.log(sol1) - np.log(bnb1)),
        "ret": gross / NOTIONAL,
        "reason": reason,
        "score": z,
        "regime": "pair",
        "month": datetime.fromtimestamp(entry_ts / 1000, CST).strftime("%Y-%m"),
        "pnl": pnl,
        "gross": gross,
        "fee": cost,
        "direction": side,
        "day": day,
        "equity": state["equity"],
        "sol0": sol0,
        "bnb0": bnb0,
        "sol1": sol1,
        "bnb1": bnb1,
    })


def run_pair(data_1h: dict):
    """SOL/BNB 对数价差偏离自身 72 小时均值就反向。回到 0 或满 24 小时平。只留一对。"""
    yb = data_1h.get(PAIR_Y)
    xb = data_1h.get(PAIR_X)
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    if yb is None or xb is None or len(yb) == 0 or len(xb) == 0:
        out = _xs_result([], state, 0, 0)
        out["n_signal"] = 0
        return out
    map_y = {int(t): i for i, t in enumerate(yb[:, 0].astype(np.int64))}
    map_x = {int(t): i for i, t in enumerate(xb[:, 0].astype(np.int64))}
    common = np.array(sorted(set(map_y) & set(map_x)), dtype=np.int64)
    step = 3_600_000
    sy = np.array([float(yb[map_y[int(t)], 3]) for t in common], dtype=np.float64)
    sx = np.array([float(xb[map_x[int(t)], 3]) for t in common], dtype=np.float64)
    ok = (sy > 0) & (sx > 0)
    spread = np.full(len(common), np.nan, dtype=np.float64)
    spread[ok] = np.log(sy[ok]) - np.log(sx[ok])
    trades = []
    position = None
    n_dec = 0
    n_skip = 0
    n_signal = 0
    w = PAIR_Z_WIN
    for i in range(w, len(common)):
        if int(common[i]) - int(common[i - w]) != w * step or not np.isfinite(spread[i - w:i + 1]).all():
            n_skip += 1
            continue
        z = spread_z(spread[i - w:i], float(spread[i]))
        fill_ts = int(common[i]) + step
        if position is not None and fill_ts > position["entry_ts"]:
            held_h = (fill_ts - position["entry_ts"]) / step
            reason = None
            if spread_normalized(position["side"], z):
                reason = "normalize"
            elif held_h >= PAIR_MAX_HOLD_H:
                reason = "time"
            if reason is not None:
                _book_pair(
                    position["side"], position["sol0"], position["bnb0"],
                    float(sy[i]), float(sx[i]), position["entry_ts"], fill_ts,
                    reason, position["z"], trades, state,
                )
                position = None
                continue
        if position is not None or i == len(common) - 1:
            continue
        n_dec += 1
        side = pair_side(z)
        if side is None:
            continue
        n_signal += 1
        position = {
            "side": side,
            "sol0": float(sy[i]),
            "bnb0": float(sx[i]),
            "entry_ts": fill_ts,
            "z": float(z),
        }
    if position is not None and len(common):
        i = len(common) - 1
        _book_pair(
            position["side"], position["sol0"], position["bnb0"],
            float(sy[i]), float(sx[i]), position["entry_ts"], int(common[i]) + step,
            "eod", position["z"], trades, state,
        )
    out = _xs_result(trades, state, n_dec, n_skip)
    out["n_signal"] = n_signal
    return out


def _utc_day(open_ts: int) -> int:
    return int(open_ts) // 86_400_000


def run_orb(data_1h: dict, pool: list[str]):
    """UTC 日开盘 4 小时锁区间。收盘突破就顺势。另一边、8 小时或日切平。一天一笔。"""
    if len(pool) < 1:
        raise ValueError("至少要 1 个币")
    step = 3_600_000
    day_ms = 86_400_000
    series = {}
    ranges = {}
    last_idx = {}
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        b = data_1h.get(sym)
        if b is None or len(b) == 0:
            continue
        t = b[:, 0].astype(np.int64)
        series[sym] = {
            "t": t,
            "h": b[:, 1].astype(np.float64),
            "l": b[:, 2].astype(np.float64),
            "c": b[:, 3].astype(np.float64),
        }
        by_day = defaultdict(list)
        for i, ts in enumerate(t):
            by_day[_utc_day(int(ts))].append(i)
        ranges[sym] = {}
        last_idx[sym] = {}
        for day, idxs in by_day.items():
            last_idx[sym][day] = idxs[-1]
            found = []
            for h in range(ORB_OPEN_H):
                expect = day * day_ms + h * step
                hit = next((i for i in idxs if int(t[i]) == expect), None)
                if hit is None:
                    found = None
                    break
                found.append(hit)
            if not found:
                continue
            hi = float(np.max(series[sym]["h"][found]))
            lo = float(np.min(series[sym]["l"][found]))
            if not np.isfinite(hi) or not np.isfinite(lo) or hi < lo:
                continue
            ranges[sym][day] = (hi, lo)
    events = []
    for sym, s in series.items():
        t = s["t"]
        for i, ts in enumerate(t):
            day = _utc_day(int(ts))
            if int(ts) - day * day_ms < ORB_OPEN_H * step:
                continue
            events.append((int(ts) + step, order[sym], sym, i, day))
    events.sort()
    positions = {}
    traded = set()
    skipped = set()
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_dec = 0
    n_skip = 0
    n_capped = 0
    idx = 0
    while idx < len(events):
        fill_ts = events[idx][0]
        group = []
        while idx < len(events) and events[idx][0] == fill_ts:
            group.append(events[idx])
            idx += 1
        for _fill, _ord, sym, i, day in group:
            if sym not in positions:
                continue
            p = positions[sym]
            if p["entry_ts"] >= fill_ts:
                continue
            px = float(series[sym]["c"][i])
            held_h = (fill_ts - p["entry_ts"]) / step
            if p["side"] == "LONG" and px <= p["lo"]:
                reason = "range"
            elif p["side"] == "SHORT" and px >= p["hi"]:
                reason = "range"
            elif held_h >= ORB_HOLD_H:
                reason = "time"
            elif i == last_idx[sym].get(day) or p["day"] != day:
                reason = "day"
            else:
                reason = None
            if reason is not None:
                _book_xs(positions, sym, px, fill_ts, reason, trades, state, regime="orb")
        for _fill, _ord, sym, i, day in group:
            s = series[sym]
            if sym in positions or (sym, day) in traded:
                continue
            if i == last_idx[sym].get(day):
                continue
            box = ranges[sym].get(day)
            if box is None:
                if (sym, day) not in skipped:
                    skipped.add((sym, day))
                    n_skip += 1
                continue
            hi, lo = box
            px = float(s["c"][i])
            if not np.isfinite(px) or px <= 0:
                continue
            n_dec += 1
            if px > hi:
                side = "LONG"
            elif px < lo:
                side = "SHORT"
            else:
                continue
            if len(positions) >= MAX_POS:
                n_capped += 1
                continue
            traded.add((sym, day))
            positions[sym] = {
                "side": side,
                "entry": px,
                "entry_ts": fill_ts,
                "ret_sig": (px / hi - 1.0) if side == "LONG" else (lo / px - 1.0),
                "hi": hi,
                "lo": lo,
                "day": day,
            }
    n_open = 0
    for sym in list(positions):
        p = positions[sym]
        s = series[sym]
        i = last_idx[sym].get(p["day"], len(s["t"]) - 1)
        fill = int(s["t"][i]) + step
        if fill <= p["entry_ts"]:
            positions.pop(sym)
            n_open += 1
            continue
        _book_xs(positions, sym, float(s["c"][i]), fill, "day", trades, state, regime="orb")
    out = _xs_result(trades, state, n_dec, n_skip)
    out["n_capped"] = n_capped
    out["n_open"] = n_open
    return out


def ma_cross_side(fast_prev, slow_prev, fast_now, slow_now):
    """上一根快线还在慢线一侧，这一根收到了另一侧，才算交叉。相等不算。"""
    vals = (fast_prev, slow_prev, fast_now, slow_now)
    if not all(np.isfinite(v) for v in vals):
        return None
    if fast_prev <= slow_prev and fast_now > slow_now:
        return "LONG"
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "SHORT"
    return None


def ma_in_deadband(fast, slow, close) -> bool:
    if not (np.isfinite(fast) and np.isfinite(slow) and np.isfinite(close)) or close <= 0:
        return False
    return abs(float(fast) - float(slow)) / float(close) < MA_DEAD


def _sma_pair(close: np.ndarray, times: np.ndarray):
    """连续 1h 才算均线。断了的窗口是 nan，不拿缺口两侧拼。"""
    n = len(close)
    fast = np.full(n, np.nan)
    slow = np.full(n, np.nan)
    step = 3_600_000
    for i in range(MA_SLOW - 1, n):
        if int(times[i]) - int(times[i - MA_SLOW + 1]) != (MA_SLOW - 1) * step:
            continue
        window = close[i - MA_SLOW + 1:i + 1]
        if not np.isfinite(window).all() or np.any(window <= 0):
            continue
        slow[i] = float(window.mean())
        fast[i] = float(close[i - MA_FAST + 1:i + 1].mean())
    return fast, slow


def run_dual_ma(data_1h: dict, pool: list[str]):
    """快均线上穿慢均线做多，下穿做空。反向交叉就翻面。缠在死区里又还没翻面，就平。"""
    if len(pool) < 1:
        raise ValueError("至少要 1 个币")
    step = 3_600_000
    series = {}
    events = []
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        b = data_1h.get(sym)
        if b is None or len(b) == 0:
            continue
        t = b[:, 0].astype(np.int64)
        c = b[:, 3].astype(np.float64)
        fast, slow = _sma_pair(c, t)
        series[sym] = {"t": t, "c": c, "fast": fast, "slow": slow}
        for i in range(len(t)):
            events.append((int(t[i]) + step, order[sym], sym, i))
    events.sort()
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_dec = 0
    n_skip = 0
    n_capped = 0
    idx = 0
    while idx < len(events):
        fill_ts = events[idx][0]
        group = []
        while idx < len(events) and events[idx][0] == fill_ts:
            group.append(events[idx])
            idx += 1
        pending = {}
        for _fill, _ord, sym, i in group:
            if sym not in positions:
                continue
            p = positions[sym]
            s = series[sym]
            if int(s["t"][i]) - p["last_open"] != step or not np.isfinite(s["fast"][i]):
                _book_xs(
                    positions, sym, p["last_px"], p["last_fill"], "gap",
                    trades, state, regime="ma",
                )
                n_skip += 1
                continue
            fast, slow = float(s["fast"][i]), float(s["slow"][i])
            px = float(s["c"][i])
            cross = ma_cross_side(s["fast"][i - 1], s["slow"][i - 1], fast, slow)
            opposite = (p["side"] == "LONG" and cross == "SHORT") or (p["side"] == "SHORT" and cross == "LONG")
            if opposite:
                _book_xs(positions, sym, px, fill_ts, "cross", trades, state, regime="ma")
                pending[sym] = cross
            elif ma_in_deadband(fast, slow, px):
                _book_xs(positions, sym, px, fill_ts, "deadband", trades, state, regime="ma")
            else:
                p["last_open"] = int(s["t"][i])
                p["last_px"] = px
                p["last_fill"] = fill_ts
        for _fill, _ord, sym, i in group:
            if sym in positions:
                continue
            s = series[sym]
            if i == len(s["t"]) - 1:
                continue
            if i < 1 or not np.isfinite(s["fast"][i]) or not np.isfinite(s["fast"][i - 1]):
                continue
            if int(s["t"][i]) - int(s["t"][i - 1]) != step:
                n_skip += 1
                continue
            n_dec += 1
            side = pending.get(sym)
            if side is None:
                side = ma_cross_side(s["fast"][i - 1], s["slow"][i - 1], s["fast"][i], s["slow"][i])
            if side is None:
                continue
            if len(positions) >= MAX_POS:
                n_capped += 1
                continue
            px = float(s["c"][i])
            positions[sym] = {
                "side": side,
                "entry": px,
                "entry_ts": fill_ts,
                "ret_sig": float(s["fast"][i] - s["slow"][i]),
                "last_open": int(s["t"][i]),
                "last_px": px,
                "last_fill": fill_ts,
            }
    for sym in list(positions):
        s = series[sym]
        _book_xs(
            positions, sym, float(s["c"][-1]), int(s["t"][-1]) + step, "eod",
            trades, state, regime="ma",
        )
    out = _xs_result(trades, state, n_dec, n_skip)
    out["n_capped"] = n_capped
    return out


def run_rsi_fade(data_1h: dict, pool: list[str]):
    """单币 1h RSI(14)。≥70 做空，≤30 做多。回到 50 或满 24 小时平。"""
    if len(pool) < 1:
        raise ValueError("至少要 1 个币")
    step = 3_600_000
    series = {}
    events = []
    order = {sym: i for i, sym in enumerate(pool)}
    for sym in pool:
        b = data_1h.get(sym)
        if b is None or len(b) == 0:
            continue
        t = b[:, 0].astype(np.int64)
        c = b[:, 3].astype(np.float64)
        series[sym] = {"t": t, "c": c, "rsi": wilder_rsi(c, t)}
        for i in range(len(t)):
            events.append((int(t[i]) + step, order[sym], sym, i))
    events.sort()
    positions = {}
    trades = []
    state = {"equity": 0.0, "peak": 0.0, "max_dd": 0.0}
    n_dec = 0
    n_skip = 0
    n_capped = 0
    idx = 0
    while idx < len(events):
        fill_ts = events[idx][0]
        group = []
        while idx < len(events) and events[idx][0] == fill_ts:
            group.append(events[idx])
            idx += 1
        exited = set()
        for _fill, _ord, sym, i in group:
            if sym not in positions:
                continue
            p = positions[sym]
            s = series[sym]
            if int(s["t"][i]) - p["last_open"] != step or not np.isfinite(s["rsi"][i]):
                _book_xs(
                    positions, sym, p["last_px"], p["last_fill"], "gap",
                    trades, state, regime="rsi",
                )
                exited.add(sym)
                n_skip += 1
                continue
            rsi = float(s["rsi"][i])
            px = float(s["c"][i])
            if rsi_back_to_mid(p["side"], rsi):
                reason = "mid"
            elif (fill_ts - p["entry_ts"]) / step >= RSI_HOLD_H:
                reason = "time"
            else:
                reason = None
            if reason is not None:
                _book_xs(positions, sym, px, fill_ts, reason, trades, state, regime="rsi")
                exited.add(sym)
            else:
                p["last_open"] = int(s["t"][i])
                p["last_px"] = px
                p["last_fill"] = fill_ts
        for _fill, _ord, sym, i in group:
            if sym in positions or sym in exited:
                continue
            s = series[sym]
            if i == len(s["t"]) - 1 or not np.isfinite(s["rsi"][i]):
                continue
            n_dec += 1
            side = rsi_side(float(s["rsi"][i]))
            if side is None:
                continue
            if len(positions) >= MAX_POS:
                n_capped += 1
                continue
            px = float(s["c"][i])
            positions[sym] = {
                "side": side,
                "entry": px,
                "entry_ts": fill_ts,
                "ret_sig": float(s["rsi"][i]),
                "last_open": int(s["t"][i]),
                "last_px": px,
                "last_fill": fill_ts,
            }
    for sym in list(positions):
        s = series[sym]
        _book_xs(
            positions, sym, float(s["c"][-1]), int(s["t"][-1]) + step, "eod",
            trades, state, regime="rsi",
        )
    out = _xs_result(trades, state, n_dec, n_skip)
    out["n_capped"] = n_capped
    return out


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


def _print_xs_plr(trades):
    """扣费后盈亏比。胜率口径与 _print_fee_table 相同：pnl>0。"""
    print("扣费后盈亏比 = |pnl>0 的均值| / |pnl≤0 的均值|。缺一边则写 —。")
    windows = (
        ("全样本", trades),
        ("样本内", [t for t in trades if t["day"] < SAMPLE_IN_END]),
        ("样本外", [t for t in trades if t["day"] >= SAMPLE_IN_END]),
    )
    print(f"{'窗口':<8}{'方向':<8}{'笔数':>6}{'胜率':>8}{'期望U/笔':>10}{'盈亏比':>8}{'均盈U':>9}{'均亏U':>9}")
    for wlabel, rows in windows:
        for side in ("LONG", "SHORT", "合计"):
            sub = rows if side == "合计" else [t for t in rows if t.get("direction") == side]
            st = _stats(sub)
            if st is None:
                print(f"{wlabel:<8}{side:<8}{0:6}{'—':>8}{'—':>10}{'—':>8}{'—':>9}{'—':>9}")
                continue
            n, win, _gross, _net, exp = st
            wins = [t["pnl"] for t in sub if t["pnl"] > 0]
            losses = [t["pnl"] for t in sub if t["pnl"] <= 0]
            avg_w = float(np.mean(wins)) if wins else None
            avg_l = float(np.mean(losses)) if losses else None
            if avg_w is not None and avg_l not in (None, 0.0):
                plr_s = f"{abs(avg_w / avg_l):.3f}"
            else:
                plr_s = "—"
            aw = f"{avg_w:.3f}" if avg_w is not None else "—"
            al = f"{avg_l:.3f}" if avg_l is not None else "—"
            print(f"{wlabel:<8}{side:<8}{n:6}{win:7.1f}%{exp:10.3f}{plr_s:>8}{aw:>9}{al:>9}")
    if not trades:
        return
    holds = np.array([(t["exit_ts"] - t["entry_ts"]) / 3_600_000 for t in trades], dtype=np.float64)
    print(f"持仓小时：均值 {holds.mean():.1f}  中位 {float(np.median(holds)):.1f}  最长 {holds.max():.1f}")
    by = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by[t["symbol"]][0] += 1
        by[t["symbol"]][1] += t["pnl"]
    print("按币（笔数 / 扣费后U）:", " ".join(
        f"{s} {by[s][0]}/{by[s][1]:+.1f}" for s in POOL if s in by
    ))


def _passes_f(trades) -> bool:
    """候选 F：样本外笔数≥30 且样本外扣费期望>0。全样本只打印，不调常量。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（F）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不复开。")
        return True
    if not c1 and not c2:
        print("结论：淘汰。样本外笔数和扣费期望都没过。不调回看、不调周期。实盘仍全 0。")
    elif not c1:
        print("结论：样本不足，不作通过。不放宽笔数门槛。实盘仍全 0。")
    else:
        print("结论：淘汰。样本外扣费期望 ≤ 0。不调回看、不调周期。实盘仍全 0。")
    return False


def _run_entry_xs(end_day: str):
    allow = "--no-download" not in sys.argv
    span_h = XS_LOOKBACK * XS_BAR_MIN // 60
    print(
        f"\n候选 F xs_rel_strength：固定池 {'/'.join(POOL)} 里做截面排序，不看单币 ATR/均线。"
        f"每 {XS_BAR_MIN // 60}h 收盘，用过去 {XS_LOOKBACK} 根（{span_h}h）收盘收益。"
        "收益最高的做多、最低的做空，各 1 个，名义 200U。"
        "并列最强或最弱时不按币名挑；已经持着并列里的一只就留着，否则这一侧不开。"
        "名次没变就续持，不重复付费。掉出槽位就在这根 4h 收盘平掉。"
        "最后一根只平仓、不开新仓。无止损、无止盈、无早保本、无移动止盈、无超时、无 ATR、无分数、无资金费率。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_xs(data, list(POOL))
    summarize("候选 F · xs_rel_strength", result)
    _print_xs_plr(result["trades"])
    print(f"决策 {result['n_dec']} 次，跳过 {result['n_skip']} 次（整池对不齐，或回看不是正好 {span_h}h）。")
    _passes_f(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 4h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场不是早保本、移动止盈或固定 ATR 盈亏比。"
        "A–E 的 N、EMA、费率阈值都没动。实盘仍全 0。"
    )


def _passes_g(trades) -> bool:
    """候选 G：样本外笔数≥30 且样本外扣费期望>0。全样本只打印。不调 z 窗口和持仓小时。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（G）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1 and not c2:
        print("结论：淘汰。样本外笔数和扣费期望都没过。不调 z 窗口、不调持仓小时。实盘仍全 0。")
    elif not c1:
        print("结论：样本不足，不作通过。不放宽笔数门槛。实盘仍全 0。")
    else:
        print("结论：淘汰。样本外扣费期望 ≤ 0。不调 z 窗口、不调持仓小时。实盘仍全 0。")
    return False


def _run_entry_revert(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 G xs_mean_revert：和 F 不是同一族。不按 24h 收益排名去追强的，"
        f"做 1h 特异残差的均值回归。残差 = 这一小时收益 − 五币等权篮子。"
        f"z 用该币自己过去 {REV_Z_WIN} 根残差（不含当根，总体标准差）。"
        f"z ≥ {REV_Z_ENTER:g} 做空，z ≤ -{REV_Z_ENTER:g} 做多，各 1 个，名义 200U。"
        "这一侧已有仓就不换人。并列最极端时不按币名挑。"
        f"持仓每小时重算 z：多单 z 回到 ≥0、空单 z 回到 ≤0 就平；否则满 {REV_MAX_HOLD_H} 小时平。"
        "最后一根只平仓、不开新仓。无 ATR、无均线、无突破、无分数、无资金费率、无早保本、无移动止盈。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F 的 4h 调仓和 24h 回看没有改。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_revert(data, list(POOL))
    summarize("候选 G · xs_mean_revert", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，跳过 {result['n_skip']} 次"
        f"（1h 对不齐，或过去 {REV_Z_WIN} 根残差里有断档 / 标准差为 0）。"
    )
    _passes_g(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是残差回到 0 或满 6 小时，不是早保本、移动止盈、ATR 或 F 的名次续持。"
        "A–F 的常量都没动。实盘仍全 0。"
    )


def _passes_h(trades) -> bool:
    """候选 H：样本外笔数≥30 且样本外扣费期望>0。全样本只打印。不调压缩倍数和窗口。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（H）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1 and not c2:
        print("结论：淘汰。样本外笔数和扣费期望都没过。不调压缩倍数、不调窗口。实盘仍全 0。")
    elif not c1:
        print("结论：样本不足，不作通过。不放宽笔数门槛。实盘仍全 0。")
    else:
        print("结论：淘汰。样本外扣费期望 ≤ 0。不调压缩倍数、不调窗口。实盘仍全 0。")
    return False


def _run_entry_compress(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 H vol_compress_break：单币波动压缩之后做区间突破，不是截面排名，也不是残差回归。"
        f"信号棒之前 {COMP_SQUEEZE} 根 1h 的高低点当盒子，再往前 {COMP_BASE} 根当对照。"
        f"盒子宽度 ≤ 对照宽度的 {COMP_RATIO:g} 才算压扁。"
        "收盘高于盒子高点做多，低于盒子低点做空。影线不算突破。各币同时最多 1 仓，"
        f"全池同时最多 {MAX_POS} 仓，同一小时按池子顺序占坑，名义 200U。"
        f"之后每小时看收盘：多单收到盒子低点或更低、空单收到盒子高点或更高，就按结构平；"
        f"否则满 {COMP_HOLD_H} 小时平。最后一根只平仓、不开新仓。"
        "无 ATR%、无均线、无成交量门槛、无分数、无资金费率、无早保本、无移动止盈，也不用 F/G 的排名或 z。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F 的 24h 回看、4h 调仓，G 的 z 窗口和 6 小时，都没有改。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_compress(data, list(POOL))
    summarize("候选 H · vol_compress_break", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，跳过 {result['n_skip']} 次"
        f"（{COMP_BASE + COMP_SQUEEZE} 小时窗口不是连续 1h）。"
    )
    _passes_h(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是区间另一边或满 12 小时，不是早保本、ATR、资金费率，也不是 F/G 的排名或残差 z。"
        "A–G 的常量都没动。实盘仍全 0。"
    )


def _passes_i(trades) -> bool:
    """候选 I：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就是失败，不降 z、不用价格代理。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（I）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。已结算费率缺失，或相对自身历史的 |z|≥2 太稀，"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把 z 从 2 降下来，不把 90 期窗口缩短，不用价格、持仓量或基差当费率代理。"
            "实盘仍全 0。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不调 z、不调 24 小时。实盘仍全 0。")
    return False


def _run_entry_fade(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 I funding_extreme_fade：单币已结算资金费率相对自己过去 {FADE_Z_WIN} 期的 z。"
        f"z ≥ {FADE_Z_ENTER:g} 做空（多头拥挤在付费），z ≤ -{FADE_Z_ENTER:g} 做多（空头拥挤在付费）。"
        "不用绝对阈值 0.0005，不看 EMA，不看 ATR。"
        "成交价是结算时间所在那根 1h 的收盘（结算戳多 1ms 先落到整点），不是前一根。"
        f"之后的结算若 z 回到 0 的正常一侧，且该收盘不晚于开仓后 {FADE_MAX_HOLD_H} 小时，就平；"
        f"否则满 {FADE_MAX_HOLD_H} 小时按 1h 收盘平。"
        f"同一币不重叠。五币同时最多 {MAX_POS} 仓，同一时刻按池子顺序占坑。名义 200U。"
        "这不是 E 的顺费率做多，也不是 F/G/H。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F/G/H 的回看、z、压缩倍数和持仓小时都没有改。E 的 0.0005 也没改。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}；资金费率月包到 2026-10（11 月月包没有，日包也没有）")
    data = {}
    funding = {}
    n_prints = 0
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
        fr = load_funding(sym, "2026-10")
        funding[sym] = fr
        n_prints += len(fr)
        if len(fr):
            f0 = datetime.fromtimestamp(fr[0, 0] / 1000, CST)
            f1 = datetime.fromtimestamp(fr[-1, 0] / 1000, CST)
            print(f"  {sym} 费率 {len(fr)} 期  {f0:%Y-%m-%d %H:%M} → {f1:%Y-%m-%d %H:%M} CST", flush=True)
        else:
            print(f"  {sym} 费率 0 期", flush=True)
    if n_prints < FADE_Z_WIN + 1:
        print(
            f"资金费率合计 {n_prints} 期，不够 {FADE_Z_WIN + 1}。"
            "失败：序列缺失。不用价格代理去补。"
        )
    result = run_funding_fade(data, funding, list(POOL))
    summarize("候选 I · funding_extreme_fade", result)
    _print_xs_plr(result["trades"])
    print(
        f"信号 {result['n_signal']} 次，缺价跳过 {result['n_skip']} 次，"
        f"同时持仓满 {MAX_POS} 丢掉 {result['n_dropped']} 笔。"
    )
    _passes_i(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是费率 z 回到 0 或满 24 小时，不是早保本、ATR、均线，也不是 F/G/H 的排名、残差或压缩突破。"
        "A–H 的常量都没动。实盘仍全 0。"
    )


def _passes_j(trades) -> bool:
    """候选 J：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就不改 20/10，也不改 4h。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（J）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把 20/10 改短，不把 4h 改成 1h。实盘仍全 0。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不改 20/10，不改 4h。实盘仍全 0。")
    return False


def _run_entry_donchian(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 J donchian_channel：单币唐奇安通道，不是 H 的压缩前提，也不是 A 的 ATR% / 量能 / 早保本。"
        f"1h 合成 {DON_BAR_H}h。收盘高于前 {DON_ENTRY_N} 根最高价做多，低于前 {DON_ENTRY_N} 根最低价做空。"
        "通道不含当根，影线不算。"
        f"多单收盘跌破前 {DON_EXIT_N} 根最低价、空单收盘升破前 {DON_EXIT_N} 根最高价就平。"
        "同一根先平再看能不能按 20 根通道反向开。同一币同时 1 仓，"
        f"五币同时最多 {MAX_POS} 仓，同一根按池子顺序占坑。名义 200U。"
        "窗口必须是连续 4h，断了就跳过，不插值。最后一根只平、不开。"
        "无 ATR、无均线、无成交量门槛、无分数、无资金费率、无早保本、无固定持仓小时。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F/G/H/I 的常量都没有改。20 进 10 出和 4h 在看盈亏之前写死。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_donchian(data, list(POOL))
    summarize("候选 J · donchian_channel", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，跳过 {result['n_skip']} 次（4h 窗口不连续），"
        f"同时持仓满 {MAX_POS} 丢掉 {result['n_capped']} 笔。"
    )
    _passes_j(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 4h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是 10 根通道的另一边，不是早保本、ATR、资金费率，也不是 F/G/H/I 的排名、残差、压缩或费率 z。"
        "A–I 的常量都没动。实盘仍全 0。"
    )


def _passes_k(trades) -> bool:
    """候选 K：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就不降 z、不换一对币。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（K）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把 z 从 2 降下来，不缩短 72 小时，不换一对币。实盘仍全 0。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不降 z、不换一对币、不改 24 小时。实盘仍全 0。")
    return False


def _run_entry_pair(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 K pair_spread_revert：只做 {PAIR_Y} 对 {PAIR_X} 这一对，不是五币篮子。"
        f"价差 = log({PAIR_Y}) − log({PAIR_X})，β 固定为 1。"
        f"z 用这对自己过去 {PAIR_Z_WIN} 根 1h 价差（不含当根，总体标准差）。"
        f"z ≥ {PAIR_Z_ENTER:g} 做空价差（空 {PAIR_Y}、多 {PAIR_X}），"
        f"z ≤ -{PAIR_Z_ENTER:g} 做多价差（多 {PAIR_Y}、空 {PAIR_X}）。"
        f"两条腿各 {PAIR_LEG:.0f}U，合计名义 {NOTIONAL:.0f}U，一对算一笔。"
        f"之后每小时重算 z：空价差 z 回到 ≤0、多价差 z 回到 ≥0 就平；否则满 {PAIR_MAX_HOLD_H} 小时平。"
        "同一根平完不再开新的一对。窗口必须连续 1h。最后一根只平、不开。"
        "无 ATR、无均线、无通道、无排名、无五币残差、无资金费率、无早保本。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F/G/H/I/J 的常量都没有改。72 小时、|z|≥2、24 小时在看盈亏之前写死。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"多价差记成 LONG，空价差记成 SHORT。读取 1h {START} → {end_day}")
    data = {}
    for sym in (PAIR_Y, PAIR_X):
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_pair(data)
    summarize("候选 K · pair_spread_revert", result)
    _print_xs_plr(result["trades"])
    if result["trades"]:
        net = float(sum(t["pnl"] for t in result["trades"]))
        print(f"配对 {PAIR_Y}/{PAIR_X}：{len(result['trades'])} 笔，扣费后 {net:+.1f}U")
    print(
        f"决策 {result['n_dec']} 次，信号 {result['n_signal']} 次，"
        f"跳过 {result['n_skip']} 次（两币小时对不齐，或 {PAIR_Z_WIN} 根不连续）。"
    )
    _passes_k(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是价差 z 回到 0 或满 24 小时，不是早保本、ATR、通道，也不是 F/G/H/I/J 的排名、五币残差、压缩、费率或唐奇安。"
        "A–J 的常量都没动。实盘仍全 0。"
    )


def _passes_l(trades) -> bool:
    """候选 L：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就不缩短开盘小时，也不改 8 小时。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（L）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把开盘 4 小时缩短，不把 8 小时改掉。实盘仍全 0。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不改开盘小时，不改 8 小时。实盘仍全 0。")
    return False


def _run_entry_orb(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 L utc_orb：每个 UTC 日单独看开盘区间，不是滚动 {DON_ENTRY_N} 根唐奇安，也不先看压缩。"
        f"当天 00:00 UTC 起的前 {ORB_OPEN_H} 根 1h 锁最高价和最低价。缺任何一根，这一天这个币不做。"
        f"区间锁上之后，收盘高于区间高点做多，低于区间低点做空。影线不算。"
        f"多单收盘回到区间低点或更低、空单回到区间高点或更高，就平；"
        f"否则满 {ORB_HOLD_H} 小时平；到了这个 UTC 日的最后一根也平，不拿到下一天。"
        "同一币同一天只开一笔，平了不再开。同一小时五币同时最多 "
        f"{MAX_POS} 仓，按池子顺序占坑。名义 200U。"
        "无 ATR、无均线、无成交量、无资金费率、无五币价差、无早保本。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F–K 的常量都没有改。开盘 4 小时和 8 小时在看盈亏之前写死。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}（开仓时刻用 CST 日，区间本身按 UTC 日）"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_orb(data, list(POOL))
    summarize("候选 L · utc_orb", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，开盘不齐跳过 {result['n_skip']} 个币日，"
        f"同时持仓满 {MAX_POS} 丢掉 {result['n_capped']} 笔，"
        f"开完没有后续价丢掉 {result['n_open']} 笔。"
    )
    _passes_l(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是开盘区间的另一边、满 8 小时，或 UTC 日切，不是早保本、ATR，也不是 F–K 的排名、残差、压缩、费率、唐奇安或价差 z。"
        "A–K 的常量都没动。实盘仍全 0。"
    )


def _passes_m(trades) -> bool:
    """候选 M：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就不改 10/30，也不放宽死区。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（M）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。实盘 max_positions_per_symbol 仍全 0，不在这条回测里自动复开。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把 10/30 改短，不放宽死区。实盘仍全 0。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不改 10/30，不改死区。实盘仍全 0。")
    return False


def _run_entry_ma(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 M dual_ma_cross：每个币自己的快慢简单均线，不是 MACD 信号线，也不是开盘区间或唐奇安。"
        f"快线 {MA_FAST} 根 1h，慢线 {MA_SLOW} 根 1h，都含当根收盘。"
        "上一根快线还不超过慢线、这一根收到上方，做多；相反做空。"
        "已经持仓时，反向交叉就平并在同一根翻面。"
        f"还没反向、但 |快-慢|/收盘 < {MA_DEAD:.4%}，就只平不翻。"
        f"同一币同时 1 仓。五币同时最多 {MAX_POS} 仓，同一小时按池子顺序占坑。名义 200U。"
        "均线窗口必须是连续 1h，断了就用断口前的收盘平掉，不把两边拼起来。"
        "最后一根只平、不开。无 ATR、无通道、无开盘区间、无价差、无费率、无早保本。"
    )
    print("不读实盘 max_positions_per_symbol（七币全 0 保持）。allow_short 配置不动，空头只在这条回测里出现。")
    print("F–L 的常量都没有改。10/30 和 1bp 死区在看盈亏之前写死。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_dual_ma(data, list(POOL))
    summarize("候选 M · dual_ma_cross", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，断档跳过 {result['n_skip']} 次，"
        f"同时持仓满 {MAX_POS} 丢掉 {result['n_capped']} 笔。"
    )
    _passes_m(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是反向交叉或死区，不是早保本、ATR，也不是 F–L 的排名、残差、压缩、费率、唐奇安、价差或开盘区间。"
        "A–L 的常量都没动。实盘仍全 0。"
    )


# 候选 N 的稳健性切日。日期在看这组数字之前由复查指定，不拿来改 70/30 或 24 小时。
RSI_ALT_CUTS = ("2026-09-01", "2026-10-15", "2026-10-20")
RSI_ALT_MIN = 20


def _rsi_slice(rows):
    n = len(rows)
    net = float(sum(t["pnl"] for t in rows)) if n else 0.0
    wins = sum(1 for t in rows if t["pnl"] > 0)
    return {
        "n": n,
        "wr": (100.0 * wins / n) if n else None,
        "net": net,
        "ev": (net / n) if n else None,
    }


def rsi_robustness(trades):
    """同一批 rsi_fade 成交上切日、去最大盈利、分币、对半。不改阈值，不重跑规则。"""
    full = _rsi_slice(trades)
    oos = [t for t in trades if t.get("day", "") >= SAMPLE_IN_END]
    main = _rsi_slice(oos)
    a_ok = main["n"] >= PASS_OOS_MIN and main["ev"] is not None and main["ev"] > 0

    winners = sorted(
        (t for t in oos if t["pnl"] > 0),
        key=lambda t: (-float(t["pnl"]), t.get("entry_ts", 0), str(t.get("symbol", ""))),
    )

    def _drop(k):
        ban = {id(t) for t in winners[:k]}
        kept = [t for t in oos if id(t) not in ban]
        picked = [
            {
                "symbol": t.get("symbol"),
                "day": t.get("day"),
                "direction": t.get("direction"),
                "pnl": float(t["pnl"]),
                "entry_ts": t.get("entry_ts"),
            }
            for t in winners[:k]
        ]
        return _rsi_slice(kept), picked

    drop1, top1 = _drop(1)
    drop3, top3 = _drop(3)
    remain_can_hit_30 = len(oos) - 3 >= PASS_OOS_MIN
    b_n_ok = drop3["n"] >= PASS_OOS_MIN if remain_can_hit_30 else True
    b_ok = len(winners) >= 3 and drop3["ev"] is not None and drop3["ev"] > 0 and b_n_ok

    cuts = []
    n_cut_ok = 0
    for cut in RSI_ALT_CUTS:
        st = _rsi_slice([t for t in trades if t.get("day", "") >= cut])
        ok = st["n"] >= RSI_ALT_MIN and st["ev"] is not None and st["ev"] > 0
        n_cut_ok += int(ok)
        cuts.append({"cut": cut, "ok": ok, **st})
    c_ok = n_cut_ok >= 2

    by_symbol = []
    for sym in POOL:
        rows = [t for t in oos if t.get("symbol") == sym]
        by_symbol.append({
            "symbol": sym,
            **_rsi_slice(rows),
            "long": _rsi_slice([t for t in rows if t.get("direction") == "LONG"]),
            "short": _rsi_slice([t for t in rows if t.get("direction") == "SHORT"]),
        })
    by_side = {
        "LONG": _rsi_slice([t for t in oos if t.get("direction") == "LONG"]),
        "SHORT": _rsi_slice([t for t in oos if t.get("direction") == "SHORT"]),
    }

    ordered = sorted(oos, key=lambda t: (t.get("entry_ts", 0), str(t.get("symbol", ""))))
    split = "none"
    mid_ts = None
    first, second = [], []
    if len(ordered) >= 2:
        t0 = int(ordered[0]["entry_ts"])
        t1 = int(ordered[-1]["entry_ts"])
        mid_ts = (t0 + t1) / 2.0
        first = [t for t in ordered if int(t["entry_ts"]) <= mid_ts]
        second = [t for t in ordered if int(t["entry_ts"]) > mid_ts]
        split = "time"
        if not first or not second:
            mid_i = len(ordered) // 2
            first, second = ordered[:mid_i], ordered[mid_i:]
            mid_ts = int(second[0]["entry_ts"])
            split = "count"
    verdict = "ROBUST" if (a_ok and b_ok and c_ok) else "FRAGILE"
    return {
        "verdict": verdict,
        "a_ok": a_ok,
        "b_ok": b_ok,
        "c_ok": c_ok,
        "n_cut_ok": n_cut_ok,
        "full": full,
        "main": main,
        "drop1": drop1,
        "drop3": drop3,
        "top1": top1,
        "top3": top3,
        "cuts": cuts,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "half1": _rsi_slice(first),
        "half2": _rsi_slice(second),
        "half_split": split,
        "half_mid_ts": mid_ts,
        "half1_end": first[-1].get("entry_ts") if first else None,
        "half2_start": second[0].get("entry_ts") if second else None,
    }


def _fmt_rsi_ev(st):
    return "—" if st["ev"] is None else f"{st['ev']:+.3f}"


def _fmt_rsi_wr(st):
    return "—" if st["wr"] is None else f"{st['wr']:.1f}%"


def _fmt_rsi_ts(ts):
    if ts is None:
        return "—"
    return datetime.fromtimestamp(float(ts) / 1000, CST).strftime("%Y-%m-%d %H:%M")


def _print_rsi_robustness(trades):
    """打印稳健性。阈值保持 RSI(14)、70/30、回到 50、24 小时。回测不改试跑额度。"""
    r = rsi_robustness(trades)
    print(
        "\n稳健性（N）：规则不变。RSI(14)、≥70 空、≤30 多、回到 50 或满 24 小时、费用 0.2U。"
        "只改统计窗口，不重跑、不改阈值。"
    )
    print(
        f"全样本只报告：{r['full']['n']} 笔，胜率 {_fmt_rsi_wr(r['full'])}，"
        f"扣费净利 {r['full']['net']:+.1f}U，期望 {_fmt_rsi_ev(r['full'])}U。不参与稳健判定。"
    )
    print(
        f"(a) 主样本外 ≥ {SAMPLE_IN_END}：{r['main']['n']} 笔，胜率 {_fmt_rsi_wr(r['main'])}，"
        f"扣费净利 {r['main']['net']:+.1f}U，期望 {_fmt_rsi_ev(r['main'])}U。"
        f"{'过' if r['a_ok'] else '不过'}（要笔数 ≥ {PASS_OOS_MIN} 且期望 > 0）。"
    )
    for label, st, picked in (
        ("去掉最大 1 笔盈利", r["drop1"], r["top1"]),
        ("去掉最大 3 笔盈利", r["drop3"], r["top3"]),
    ):
        names = "，".join(
            f"{p['symbol']} {p['day']} {p['direction']} {p['pnl']:+.3f}U" for p in picked
        ) or "无"
        print(
            f"    {label}：剩 {st['n']} 笔，胜率 {_fmt_rsi_wr(st)}，"
            f"扣费净利 {st['net']:+.1f}U，期望 {_fmt_rsi_ev(st)}U。拿掉：{names}"
        )
    print(
        f"(b) 去掉最大 3 笔后期望仍 > 0"
        f"{'，且剩下笔数 ≥ ' + str(PASS_OOS_MIN) if r['main']['n'] - 3 >= PASS_OOS_MIN else ''}："
        f"{'过' if r['b_ok'] else '不过'}。"
    )
    print(f"(c) 另外三个开仓日切点，各自要期望 > 0 且笔数 ≥ {RSI_ALT_MIN}。至少 2 个才算过。")
    for c in r["cuts"]:
        print(
            f"    ≥ {c['cut']}：{c['n']} 笔，胜率 {_fmt_rsi_wr(c)}，"
            f"扣费净利 {c['net']:+.1f}U，期望 {_fmt_rsi_ev(c)}U。{'过' if c['ok'] else '不过'}"
        )
    print(f"    三个里过了 {r['n_cut_ok']} 个。{'过' if r['c_ok'] else '不过'}。")
    print(f"主样本外多空：")
    for side, name in (("LONG", "多"), ("SHORT", "空")):
        st = r["by_side"][side]
        print(
            f"    {name}：{st['n']} 笔，胜率 {_fmt_rsi_wr(st)}，"
            f"扣费净利 {st['net']:+.1f}U，期望 {_fmt_rsi_ev(st)}U"
        )
    print("主样本外按币（合计 / 多 / 空）：")
    for row in r["by_symbol"]:
        print(
            f"    {row['symbol']}：{row['n']} 笔 胜率 {_fmt_rsi_wr(row)} "
            f"净利 {row['net']:+.1f}U 期望 {_fmt_rsi_ev(row)}U"
            f" | 多 {row['long']['n']} {_fmt_rsi_ev(row['long'])}U"
            f" | 空 {row['short']['n']} {_fmt_rsi_ev(row['short'])}U"
        )
    print(
        f"样本外按开仓时刻对半（{r['half_split']}，分界 {_fmt_rsi_ts(r['half_mid_ts'])} CST）。"
        f"前半最后一笔 {_fmt_rsi_ts(r['half1_end'])}，后半第一笔 {_fmt_rsi_ts(r['half2_start'])}。"
    )
    print(
        f"    前半：{r['half1']['n']} 笔，胜率 {_fmt_rsi_wr(r['half1'])}，"
        f"扣费净利 {r['half1']['net']:+.1f}U，期望 {_fmt_rsi_ev(r['half1'])}U"
    )
    print(
        f"    后半：{r['half2']['n']} 笔，胜率 {_fmt_rsi_wr(r['half2'])}，"
        f"扣费净利 {r['half2']['net']:+.1f}U，期望 {_fmt_rsi_ev(r['half2'])}U"
    )
    print(
        f"稳健性结论：{r['verdict']}。"
        "三条都要过才是 ROBUST：主样本外仍过线，去掉最大 3 笔盈利后期望仍 > 0，"
        "三个切日里至少两个期望 > 0 且笔数 ≥ 20。"
        "不过就是 FRAGILE。回测不改 config 里的试跑额度。不做候选 O。"
    )
    return r


def _passes_n(trades) -> bool:
    """候选 N：样本外笔数≥30 且样本外扣费期望>0。不够 30 笔就不改 70/30，也不缩短 24 小时。"""
    oos = [t for t in trades if t["day"] >= SAMPLE_IN_END]
    n_oos = len(oos)
    exp_oos = float(np.mean([t["pnl"] for t in oos])) if n_oos else None
    exp_all = float(np.mean([t["pnl"] for t in trades])) if trades else None
    c1 = n_oos >= PASS_OOS_MIN
    c2 = exp_oos is not None and exp_oos > 0
    exp_oos_s = f"{exp_oos:+.3f}" if exp_oos is not None else "—"
    exp_all_s = f"{exp_all:+.3f}" if exp_all is not None else "—"
    print(
        f"通过线（N）：样本外笔数 {n_oos} {'≥' if c1 else '<'} {PASS_OOS_MIN}；"
        f"样本外扣费期望 {exp_oos_s} {'>' if c2 else '≤'} 0。"
        f"全样本扣费期望 {exp_all_s} 只报告，不另设第三条否决。"
    )
    if c1 and c2:
        print("结论：通过。回测不改 config。测试网试跑额度在 config.json，不由这条命令放大。")
        return True
    if not c1:
        print(
            "结论：失败。样本不足。"
            f"样本外只有 {n_oos} 笔，凑不满 {PASS_OOS_MIN}。"
            "不把 70/30 放宽，不缩短 24 小时。回测不改试跑额度。"
        )
        return False
    print("结论：淘汰。样本外扣费期望 ≤ 0。不改 70/30，不改 24 小时。回测不改试跑额度。")
    return False


def _run_entry_rsi(end_day: str):
    allow = "--no-download" not in sys.argv
    print(
        f"\n候选 N rsi_fade：每个币自己的 1h Wilder RSI({RSI_N})，不是五币残差，也不是两币价差。"
        f"RSI ≥ {RSI_HIGH:g} 做空，RSI ≤ {RSI_LOW:g} 做多。空单 RSI 回到 ≤ {RSI_MID:g}、"
        f"多单回到 ≥ {RSI_MID:g} 就平；否则满 {RSI_HOLD_H} 小时平。"
        "同一根平完不再开。同一币同时 1 仓。"
        f"五币同时最多 {MAX_POS} 仓，同一小时按池子顺序占坑。名义 200U。"
        "小时不连续就重开 RSI，不跨缺口平滑；仓还在就用断口前的收盘平掉。"
        "最后一根只平、不开。无均线、无通道、无开盘区间、无费率、无早保本。"
    )
    print("回测不读、也不改 config 里的试跑额度。旧评分 allow_short 仍是 false。")
    print("F–M 的常量都没有改。14、70/30、50 和 24 小时在看盈亏之前写死。")
    print(
        f"费用 {NOTIONAL * FEE_RT:.2f}U/笔  滑点往返 {SLIPPAGE_RT:.3%}（写死）  "
        f"样本内开仓日 < {SAMPLE_IN_END}  样本外 ≥ {SAMPLE_IN_END}"
    )
    print(f"读取 1h {START} → {end_day}")
    data = {}
    for sym in POOL:
        arr, src = load_1h(sym, end_day, allow)
        if len(arr) == 0:
            raise SystemExit(f"{sym} 1h 为空")
        data[sym] = arr
        t0 = datetime.fromtimestamp(arr[0, 0] / 1000, CST)
        t1 = datetime.fromtimestamp(arr[-1, 0] / 1000, CST)
        print(
            f"  {sym} {src} {len(arr)} 根  {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} CST",
            flush=True,
        )
    result = run_rsi_fade(data, list(POOL))
    summarize("候选 N · rsi_fade", result)
    _print_xs_plr(result["trades"])
    print(
        f"决策 {result['n_dec']} 次，断档跳过 {result['n_skip']} 次，"
        f"同时持仓满 {MAX_POS} 丢掉 {result['n_capped']} 笔。"
    )
    _passes_n(result["trades"])
    _print_rsi_robustness(result["trades"])
    print(
        "\n注明：滑点往返写死为 0，按 1h 收盘成交，没有跳空，结果偏乐观。不模拟强平。"
        "出场是 RSI 回到 50 或满 24 小时，不是早保本、ATR，也不是 F–M 的排名、残差、压缩、费率、唐奇安、价差、开盘区间或均线交叉。"
        "稳健性只切统计窗口，不改 14、70/30、50 和 24 小时。A–M 的常量都没动。回测不改试跑额度。"
    )


def _entry_mode() -> str:
    if "--entry" not in sys.argv:
        return ENTRY_BASELINE
    i = sys.argv.index("--entry")
    if i + 1 >= len(sys.argv) or sys.argv[i + 1].startswith("--"):
        raise SystemExit("缺少 --entry 的值")
    mode = sys.argv[i + 1]
    allowed = (
        ENTRY_BASELINE, ENTRY_TREND, ENTRY_PULLBACK, ENTRY_FIXED, ENTRY_NO_BE, ENTRY_FUND,
        ENTRY_XS, ENTRY_REV, ENTRY_COMP, ENTRY_FADE, ENTRY_DON, ENTRY_PAIR, ENTRY_ORB, ENTRY_MA,
        ENTRY_RSI,
    )
    if mode not in allowed:
        raise SystemExit(f"未知入场 {mode}，只接受 {' / '.join(allowed)}")
    return mode


def main():
    end_day = datetime.now(CST).strftime("%Y-%m-%d")
    mode = _entry_mode()
    if mode == ENTRY_XS:
        _run_entry_xs(end_day)
        return
    if mode == ENTRY_REV:
        _run_entry_revert(end_day)
        return
    if mode == ENTRY_COMP:
        _run_entry_compress(end_day)
        return
    if mode == ENTRY_FADE:
        _run_entry_fade(end_day)
        return
    if mode == ENTRY_DON:
        _run_entry_donchian(end_day)
        return
    if mode == ENTRY_PAIR:
        _run_entry_pair(end_day)
        return
    if mode == ENTRY_ORB:
        _run_entry_orb(end_day)
        return
    if mode == ENTRY_MA:
        _run_entry_ma(end_day)
        return
    if mode == ENTRY_RSI:
        _run_entry_rsi(end_day)
        return
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
