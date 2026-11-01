#!/usr/bin/env python3
"""候选 K：SOL/BNB 对数价差回归。不下载。不改 F–J 的常量，实盘上限仍全 0。"""
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


def pack(sol, bnb, start=START):
    rows_s, rows_b = [], []
    for i, (s, b) in enumerate(zip(sol, bnb)):
        t = start + i * HOUR
        rows_s.append((t, float(s), float(s), float(s), 1.0))
        rows_b.append((t, float(b), float(b), float(b), 1.0))
    return {
        "SOL": np.asarray(rows_s, dtype=np.float64),
        "BNB": np.asarray(rows_b, dtype=np.float64),
    }


def calm_sol(i):
    return 100.0 if i % 2 == 0 else 101.0


class TestPairSpreadRevert(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_PAIR, "pair_spread_revert")
        self.assertEqual(hb.PAIR_Y, "SOL")
        self.assertEqual(hb.PAIR_X, "BNB")
        self.assertEqual(hb.PAIR_Z_WIN, 72)
        self.assertEqual(hb.PAIR_Z_ENTER, 2.0)
        self.assertEqual(hb.PAIR_MAX_HOLD_H, 24)
        self.assertAlmostEqual(hb.PAIR_LEG * 2, hb.NOTIONAL)
        self.assertEqual(hb.DON_BAR_H, 4)
        self.assertEqual(hb.DON_ENTRY_N, 20)
        self.assertEqual(hb.DON_EXIT_N, 10)
        self.assertEqual(hb.FADE_Z_ENTER, 2.0)
        self.assertEqual(hb.FADE_Z_WIN, 90)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_Z_WIN, 48)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertEqual(hb.FUND_MIN, 0.0005)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.PASS_OOS_MIN, 30)

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        self.assertEqual(cfg["max_positions_per_symbol"], {"BTC": 0, "ETH": 0, "SOL": 1, "BNB": 1, "DOT": 1, "LINK": 1, "XRP": 1})
        self.assertEqual(cfg["max_positions"], 2)
        self.assertEqual(cfg["entry_strategy"], "rsi_fade")
        self.assertTrue(cfg["binance_testnet"])
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("pair_spread_revert", text)
            self.assertNotIn("donchian_channel", text)
            self.assertNotIn("funding_extreme_fade", text)

    def test_spread_z_and_side(self):
        hist = np.linspace(-0.01, 0.01, hb.PAIR_Z_WIN)
        now = 0.05
        self.assertAlmostEqual(hb.spread_z(hist, now), (now - hist.mean()) / hist.std())
        self.assertTrue(np.isnan(hb.spread_z(np.ones(hb.PAIR_Z_WIN), 1.0)))
        self.assertTrue(np.isnan(hb.spread_z(hist[:-1], now)))
        self.assertEqual(hb.pair_side(2.0), "SHORT")
        self.assertEqual(hb.pair_side(-2.0), "LONG")
        self.assertIsNone(hb.pair_side(1.9))
        self.assertTrue(hb.spread_normalized("SHORT", 0.0))
        self.assertFalse(hb.spread_normalized("SHORT", 0.1))
        self.assertTrue(hb.spread_normalized("LONG", 0.0))

    def test_rich_sol_is_short_the_spread_and_normalizes(self):
        n = hb.PAIR_Z_WIN + 2
        sol = [calm_sol(i) for i in range(n)]
        bnb = [100.0] * n
        sol[hb.PAIR_Z_WIN] = 300.0
        sol[hb.PAIR_Z_WIN + 1] = 100.0
        trades = hb.run_pair(pack(sol, bnb))["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["symbol"], "SOL/BNB")
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "normalize")
        self.assertAlmostEqual(t["sol0"], 300.0)
        self.assertAlmostEqual(t["sol1"], 100.0)
        self.assertAlmostEqual(t["bnb0"], 100.0)
        self.assertAlmostEqual(t["bnb1"], 100.0)
        gross = 100.0 * (1.0 - 100.0 / 300.0) + 100.0 * (100.0 / 100.0 - 1.0)
        self.assertAlmostEqual(t["gross"], gross)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], gross - 0.2)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 1.0)
        self.assertGreater(t["pnl"], 0)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "channel", "structure", "rebalance"})

    def test_other_coins_do_not_change_the_pair(self):
        n = hb.PAIR_Z_WIN + 2
        sol = [calm_sol(i) for i in range(n)]
        bnb = [100.0] * n
        sol[hb.PAIR_Z_WIN] = 300.0
        sol[hb.PAIR_Z_WIN + 1] = 100.0
        data = pack(sol, bnb)
        alone = hb.run_pair(data)["trades"][0]["pnl"]
        data["DOT"] = data["SOL"] * 3
        data["LINK"] = data["BNB"] * 0.5
        self.assertAlmostEqual(hb.run_pair(data)["trades"][0]["pnl"], alone)

    def test_cheap_sol_is_long_the_spread(self):
        n = hb.PAIR_Z_WIN + 2
        sol = [calm_sol(i) for i in range(n)]
        bnb = [100.0] * n
        sol[hb.PAIR_Z_WIN] = 50.0
        sol[hb.PAIR_Z_WIN + 1] = 100.0
        t = hb.run_pair(pack(sol, bnb))["trades"][0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "normalize")
        gross = 100.0 * (100.0 / 50.0 - 1.0) + 100.0 * (1.0 - 100.0 / 100.0)
        self.assertAlmostEqual(t["pnl"], gross - 0.2)

    def test_sustained_extreme_exits_at_24h_and_does_not_reenter_same_bar(self):
        n = hb.PAIR_Z_WIN + 40
        sol = [calm_sol(i) for i in range(n)]
        bnb = [100.0] * n
        for i in range(hb.PAIR_Z_WIN, n):
            sol[i] = 180.0 + i
        trades = hb.run_pair(pack(sol, bnb))["trades"]
        self.assertGreaterEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "time")
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 24.0)
        for a, b in zip(trades, trades[1:]):
            self.assertLess(a["exit_ts"], b["entry_ts"])

    def test_gap_in_the_window_does_not_trade(self):
        n = hb.PAIR_Z_WIN + 5
        sol = [calm_sol(i) for i in range(n)]
        bnb = [100.0] * n
        sol[hb.PAIR_Z_WIN] = 300.0
        sol[hb.PAIR_Z_WIN + 1] = 100.0
        data = pack(sol, bnb)
        data["SOL"] = np.delete(data["SOL"], 10, axis=0)
        data["BNB"] = np.delete(data["BNB"], 10, axis=0)
        self.assertEqual(hb.run_pair(data)["trades"], [])

    def test_pass_bar_does_not_loosen_when_sparse(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_k(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([]))


if __name__ == "__main__":
    unittest.main()
