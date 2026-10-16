#!/usr/bin/env python3
"""
exit_replay.py — 真实开仓 × 1 分钟 K 线出场重放（离线，只读）

对归因库里已平仓的真实开仓，按开仓价和开仓时 ATR 重算 TP/SL，用测试网 1m K 线
逐根模拟不同出场规则，比较扣费后盈亏。入场不变，只换出场；可叠加入场过滤（ATR 下限）。

同一根 K 线 TP/SL 都触及时按先止损（保守）。ATR 动态止损的盘中调整不模拟。
K 线缓存在 ~/.cache/morgan_klines/，重跑不重复拉取。

用法：
  python3 exit_replay.py                 # 只看多单，全部变体
  python3 exit_replay.py --all-dirs      # 多空都算
"""
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_exit_config  # noqa: E402
from strategy_layer import calc_tp_sl  # noqa: E402

urllib3.disable_warnings()

DB = Path(__file__).with_name("trade_data.db")
CACHE = Path.home() / ".cache" / "morgan_klines"
KLINES_URL = "https://testnet.binancefuture.com/fapi/v1/klines"
FEE_ROUND_TRIP = 0.001
CST = timezone(timedelta(hours=8))
SEGMENTS = [("09-19前", "0000", "2026-09-19"), ("09-19~10-04", "2026-09-19", "2026-10-04"),
            ("10-04后", "2026-10-04", "9999")]

# 变体：早保本 (trigger, lock) 或 None；trailing 是否启用
VARIANTS = {
    "现行(早保本1.5/0.5+移动)": {"be": (0.015, 0.005), "trail": True},
    "无早保本(只移动止盈)": {"be": None, "trail": True},
    "早保本2.0/0.5+移动": {"be": (0.020, 0.005), "trail": True},
    "只硬TP/SL": {"be": None, "trail": False},
}


def load_trades(all_dirs: bool) -> list[dict]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT t.trade_id, t.ts, t.symbol, t.direction, t.entry_price, t.f_atr_pct,
               p.pnl_usdt, p.pnl_pct, p.reason,
               COALESCE((SELECT SUM(pnl_usdt) FROM trade_closes r
                         WHERE r.trade_id = t.trade_id AND r.is_primary = 0), 0) AS residual
        FROM trades t JOIN trade_closes p ON p.trade_id = t.trade_id AND p.is_primary = 1
        ORDER BY t.ts
    """).fetchall()
    out = []
    for r in rows:
        if not all_dirs and r["direction"] != "LONG":
            continue
        pct = r["pnl_pct"] or 0
        notional = abs(r["pnl_usdt"] / pct) if abs(pct) > 1e-4 else 200.0
        out.append({**dict(r), "notional": min(max(notional, 50.0), 400.0),
                    "actual": (r["pnl_usdt"] or 0) + r["residual"]})
    return out


def klines(symbol: str, start_ms: int, hours: int) -> list:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{symbol}_{start_ms}_{hours}.json"
    if f.exists():
        return json.loads(f.read_text())
    bars, cur, end = [], start_ms, start_ms + hours * 3600_000
    s = requests.Session()
    s.trust_env = False
    while cur < end:
        for attempt in range(4):
            try:
                r = s.get(KLINES_URL, params={"symbol": f"{symbol}USDT", "interval": "1m",
                                              "startTime": cur, "limit": 1500}, timeout=15, verify=False)
                page = r.json()
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"{symbol} K 线拉取失败 @ {cur}")
        if not isinstance(page, list) or not page:
            break
        bars.extend(page)
        cur = page[-1][0] + 60_000
        time.sleep(0.15)
    bars = [b for b in bars if b[0] < end]
    f.write_text(json.dumps(bars))
    return bars


def simulate(t: dict, bars: list, be, trail: bool, timeout_h: float, tiers) -> float:
    """返回扣费前收益率（相对开仓价，已按方向取号）。"""
    entry, long_ = t["entry_price"], t["direction"] == "LONG"
    tp_sl = calc_tp_sl(entry, t["direction"], {"atr_pct": t["f_atr_pct"] or 0}, {})
    tp, sl, tp_pct = tp_sl["tp1_price"], tp_sl["sl_price"], tp_sl["tp_pct"]
    peak_ret, peak_px = 0.0, entry
    ret = lambda px: (px - entry) / entry if long_ else (entry - px) / entry
    deadline = bars[0][0] + timeout_h * 3600_000 if bars else 0
    for b in bars:
        hi, lo, close = float(b[2]), float(b[3]), float(b[4])
        adverse, favorable = (lo, hi) if long_ else (hi, lo)
        if (long_ and adverse <= sl) or (not long_ and adverse >= sl):
            return ret(sl)
        if (long_ and favorable >= tp) or (not long_ and favorable <= tp):
            return ret(tp)
        if b[0] >= deadline:
            return ret(close)
        if ret(favorable) > peak_ret:
            peak_ret, peak_px = ret(favorable), favorable
        if be and peak_ret >= be[0]:
            be_sl = entry * (1 + be[1]) if long_ else entry * (1 - be[1])
            sl = max(sl, be_sl) if long_ else min(sl, be_sl)
        if trail:
            active = None
            for tier in tiers:
                if peak_ret >= tp_pct * tier["pnl_ratio"]:
                    active = tier
                else:
                    break
            if active and active.get("gap", 0) < 0.9:
                gap = tp_pct * active["gap_ratio"]
                new_sl = peak_px * (1 - gap) if long_ else peak_px * (1 + gap)
                sl = max(sl, new_sl) if long_ else min(sl, new_sl)
    return ret(float(bars[-1][4])) if bars else 0.0


def summarize(label: str, trades: list, pnl_key: str):
    print(f"  {label:28}", end="")
    for _, a, b in SEGMENTS:
        seg = [t for t in trades if a <= t["ts"][:10] < b]
        print(f" {sum(t[pnl_key] for t in seg):+8.1f}({len(seg):3})", end="")
    print(f" | 合计 {sum(t[pnl_key] for t in trades):+8.1f}({len(trades)})")


def main():
    all_dirs = "--all-dirs" in sys.argv
    ex = get_exit_config()
    timeout_h = ex.timeout_hours if ex.timeout_enabled else 48
    tiers = list(ex.trailing_tiers)
    trades = load_trades(all_dirs)
    print(f"样本 {len(trades)} 笔（{'多空' if all_dirs else '只多单'}），拉取/读取 1m K 线 ...")
    for i, t in enumerate(trades, 1):
        start = int(datetime.fromisoformat(t["ts"]).replace(tzinfo=CST).timestamp() * 1000)
        t["bars"] = klines(t["symbol"], start, int(timeout_h) + 1)
        if i % 25 == 0:
            print(f"  {i}/{len(trades)}")
    trades = [t for t in trades if t["bars"]]

    for t in trades:
        fee = t["notional"] * FEE_ROUND_TRIP
        t["实际"] = t["actual"] - fee
        for name, v in VARIANTS.items():
            t[name] = simulate(t, t["bars"], v["be"], v["trail"], timeout_h, tiers) * t["notional"] - fee

    cur = "现行(早保本1.5/0.5+移动)"
    live = [t for t in trades if t["ts"][:10] >= "2026-10-04"]
    agree = sum((t[cur] > 0) == (t["实际"] > 0) for t in live)
    print(f"\n校验：10-04 后现行规则重放与实际盈亏同号 {agree}/{len(live)}")

    header = "".join(f" {name:>13}" for name, _, _ in SEGMENTS)
    for flt_label, flt in [("全部入场", lambda t: True),
                           ("入场过滤 ATR≥0.4%", lambda t: (t["f_atr_pct"] or 0) >= 0.004)]:
        sub = [t for t in trades if flt(t)]
        print(f"\n【{flt_label}】扣费后 U（笔数）{header}")
        summarize("实际成交", sub, "实际")
        for name in VARIANTS:
            summarize(name, sub, name)


if __name__ == "__main__":
    main()
