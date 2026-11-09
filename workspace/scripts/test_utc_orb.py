#!/usr/bin/env python3
"""候选 L：UTC 日开盘区间突破。不下载。不改 F–K 的常量，实盘上限仍全 0。"""
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import hist_backtest as hb

START = int(datetime(2024, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
HOUR = 3_600_000


def pack(days, symbol="SOL"):
    """days: list of 24 (high, low, close)，或 None 表示这小时缺失。"""
    rows = []
    for d, hours in enumerate(days):
        for h, bar in enumerate(hours):
            if bar is None:
                continue
            high, low, close = bar
            t = START + (d * 24 + h) * HOUR
            rows.append((t, float(high), float(low), float(close), 1.0))
    return {symbol: np.asarray(rows, dtype=np.float64)}


def quiet_day(high=110.0, low=90.0, close=100.0):
    return [(high, low, close)] * 24


def with_hours(base, hours):
    out = list(base)
    for h, bar in hours.items():
        out[h] = bar
    return out


class TestUtcOrb(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_ORB, "utc_orb")
        self.assertEqual(hb.ORB_OPEN_H, 4)
        self.assertEqual(hb.ORB_HOLD_H, 8)
        self.assertEqual(hb.PAIR_Z_WIN, 72)
        self.assertEqual(hb.PAIR_Z_ENTER, 2.0)
        self.assertEqual(hb.PAIR_MAX_HOLD_H, 24)
        self.assertEqual(hb.PAIR_Y, "SOL")
        self.assertEqual(hb.PAIR_X, "BNB")
        self.assertEqual(hb.DON_ENTRY_N, 20)
        self.assertEqual(hb.DON_EXIT_N, 10)
        self.assertEqual(hb.DON_BAR_H, 4)
        self.assertEqual(hb.FADE_Z_ENTER, 2.0)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.PASS_OOS_MIN, 30)

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        self.assertEqual(cfg["max_positions_per_symbol"], {"BTC": 0, "ETH": 0, "SOL": 1, "BNB": 1, "DOT": 1, "LINK": 1, "XRP": 1})
        self.assertEqual(cfg["max_positions"], 4)
        self.assertEqual(cfg["entry_strategy"], "rsi_fade")
        self.assertTrue(cfg["binance_testnet"])
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("utc_orb", text)
            self.assertNotIn("pair_spread_revert", text)
            self.assertNotIn("donchian_channel", text)

    def test_range_does_not_trade_until_it_locks(self):
        day = quiet_day(high=100.0, low=90.0, close=95.0)
        day[1] = (160.0, 90.0, 150.0)
        day[2] = (160.0, 90.0, 140.0)
        day[3] = (160.0, 90.0, 140.0)
        day[4] = (155.0, 130.0, 150.0)
        self.assertEqual(hb.run_orb(pack([day]), ["SOL"])["trades"], [])

    def test_wick_does_not_break_the_range(self):
        day = with_hours(quiet_day(), {4: (130.0, 95.0, 105.0)})
        self.assertEqual(hb.run_orb(pack([day]), ["SOL"])["trades"], [])

    def test_close_above_prior_day_high_is_not_required(self):
        # 昨天高点 300。今天开盘区间只有 110。收盘 111 仍做多。这不是滚动唐奇安。
        yesterday = quiet_day(high=300.0, low=250.0, close=280.0)
        today = with_hours(quiet_day(), {4: (112.0, 100.0, 111.0), 12: (112.0, 100.0, 105.0)})
        trades = hb.run_orb(pack([yesterday, today]), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "LONG")
        self.assertAlmostEqual(trades[0]["entry"], 111.0)

    def test_long_exits_on_the_other_side_of_the_range(self):
        day = with_hours(quiet_day(), {
            4: (112.0, 100.0, 111.0),
            6: (112.0, 80.0, 89.0),
            8: (120.0, 100.0, 115.0),
        })
        trades = hb.run_orb(pack([day]), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "range")
        self.assertAlmostEqual(t["entry"], 111.0)
        self.assertAlmostEqual(t["exit"], 89.0)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], (89.0 - 111.0) / 111.0 * 200.0 - 0.2)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 2.0)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "channel", "normalize"})

    def test_time_exit_at_8h(self):
        hours = {4: (112.0, 100.0, 111.0)}
        for h in range(5, 16):
            hours[h] = (112.0, 100.0, 108.0)
        day = with_hours(quiet_day(), hours)
        t = hb.run_orb(pack([day]), ["SOL"])["trades"][0]
        self.assertEqual(t["reason"], "time")
        self.assertAlmostEqual(t["exit"], 108.0)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 8.0)

    def test_flat_at_the_utc_day_boundary(self):
        day = with_hours(quiet_day(), {18: (112.0, 100.0, 111.0)})
        for h in range(19, 24):
            day[h] = (108.0, 100.0, 105.0)
        nxt = quiet_day(high=140.0, low=120.0, close=130.0)
        trades = hb.run_orb(pack([day, nxt]), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["reason"], "day")
        self.assertAlmostEqual(t["exit"], 105.0)
        self.assertLess((t["exit_ts"] - t["entry_ts"]) / HOUR, hb.ORB_HOLD_H)
        exit_utc = datetime.fromtimestamp(t["exit_ts"] / 1000, timezone.utc)
        self.assertEqual(exit_utc.hour, 0)
        self.assertEqual(exit_utc.day, 2)

    def test_missing_opening_hour_skips_the_day(self):
        day = with_hours(quiet_day(), {2: None, 4: (130.0, 100.0, 120.0)})
        out = hb.run_orb(pack([day]), ["SOL"])
        self.assertEqual(out["trades"], [])
        self.assertEqual(out["n_skip"], 1)

    def test_cap_keeps_four(self):
        day = with_hours(quiet_day(), {4: (112.0, 100.0, 111.0), 5: (105.0, 100.0, 102.0)})
        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        data = {s: pack([day])["SOL"] for s in names}
        out = hb.run_orb(data, names)
        self.assertEqual(len(out["trades"]), 4)
        self.assertEqual(out["n_capped"], 1)
        self.assertEqual(sorted(t["symbol"] for t in out["trades"]), ["BNB", "DOT", "LINK", "SOL"])

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_l(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([]))


if __name__ == "__main__":
    unittest.main()
