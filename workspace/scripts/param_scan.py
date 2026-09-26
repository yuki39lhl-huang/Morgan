#!/usr/bin/env python3
"""
param_scan.py — Step B：参数扫描

目的：抽离关键参数做网格扫描，对比不同参数下的胜率/盈亏，辅助选参。
只 import backtester.simulate()（零耦合），不改动任何生产模块。

扫参维度（可各自启用）：
  - score_threshold：各 regime 开仓阈值（当前 trending=70/ranging=80/volatile=90）
  - atr_sl_tiers 乘数：整体放宽/收紧 ATR 动态止损距离

用法：python3 param_scan.py [symbol] [days]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.basicConfig(level=logging.WARNING)

from config import CONFIG, get_config
import backtester



def _base_threshold() -> dict:
    return {k: (CONFIG["score_threshold"][k] if k in ("trending", "ranging", "volatile") else v)
            for k, v in CONFIG["score_threshold"].items()}


def scan_threshold(symbol, days, deltas):
    """只扫 score_threshold：对 3 个 regime 统一加偏移量。"""
    base = _base_threshold()
    rows = []
    for delta in deltas:
        th = {k: (v + delta if k in ("trending", "ranging", "volatile") else v)
              for k, v in base.items()}
        res = backtester.simulate(symbol, days, {"score_threshold": th})
        rows.append((f"thr+{delta:+.0f}", res))
        _print_row(f"thr+{delta:+.0f}", res)
    return rows


def scan_atr_mult(symbol, days, mults):
    """只扫 atr_sl_tiers：整体乘以某系数。"""
    base = [list(t) for t in CONFIG["atr_sl_tiers"]]
    rows = []
    for m in mults:
        tiers = [[t[0], round(t[1] * m, 2)] for t in base]
        res = backtester.simulate(symbol, days, {"atr_sl_tiers": tiers})
        rows.append((f"atr×{m}", res))
        _print_row(f"atr×{m}", res)
    return rows


def _print_row(label, res):
    if "error" in res:
        print(f"  {label:<10} error: {res['error']}")
        return
    print(f"  {label:<10} n={res['n']:<3} win={res['win_rate']:5.1f}%  "
          f"avg={res['avg_pnl_pct']:+.3f}%  total={res['total_pnl_pct']:+.2f}%  "
          f"PF={res['profit_factor']:.2f}")


def scan_sl_floor(symbol, days, floors):
    """只扫止损地板 bt_min_sl_pct：验证 SL 距离放开后能否改变出场分布/盈亏。"""
    rows = []
    for f in floors:
        res = backtester.simulate(symbol, days, {"bt_min_sl_pct": f})
        rows.append((f"minSL={f:.1%}", res))
        _print_row(f"minSL={f:.1%}", res)
    return rows


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 14

    print(f"═══ 基准（当前 config）═══")
    base = backtester.simulate(symbol, days)
    _print_row("baseline", base)

    print("\n═══ 阈值扫描 (score_threshold，3 regime 统一偏移) ═══")
    scan_threshold(symbol, days, [-10, 0, 10, 20])

    print("\n═══ ATR 止损层扫描 (atr_sl_tiers 整体乘数) ═══")
    scan_atr_mult(symbol, days, [1.0, 1.5, 2.0])

    print("\n═══ 止损地板扫描 (bt_min_sl_pct) ═══")
    scan_sl_floor(symbol, days, [0.02, 0.01, 0.005, 0.003])


if __name__ == "__main__":
    main()