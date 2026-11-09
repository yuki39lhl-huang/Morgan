#!/usr/bin/env python3
"""候选 M：10/30 均线交叉。不下载。不改 F–L 的常量，实盘上限仍全 0。"""
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


def pack(prices, symbol="SOL"):
    rows = []
    for i, px in enumerate(prices):
        t = START + i * HOUR
        rows.append((t, float(px), float(px), float(px), 1.0))
    return {symbol: np.asarray(rows, dtype=np.float64)}


class TestDualMaCross(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_MA, "dual_ma_cross")
        self.assertEqual(hb.MA_FAST, 10)
        self.assertEqual(hb.MA_SLOW, 30)
        self.assertEqual(hb.MA_DEAD, 0.0001)
        self.assertEqual(hb.ORB_OPEN_H, 4)
        self.assertEqual(hb.ORB_HOLD_H, 8)
        self.assertEqual(hb.PAIR_Z_ENTER, 2.0)
        self.assertEqual(hb.PAIR_Z_WIN, 72)
        self.assertEqual(hb.PAIR_Y, "SOL")
        self.assertEqual(hb.DON_ENTRY_N, 20)
        self.assertEqual(hb.DON_EXIT_N, 10)
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
            self.assertNotIn("dual_ma_cross", text)
            self.assertNotIn("utc_orb", text)
            self.assertNotIn("pair_spread_revert", text)

    def test_cross_and_deadband_helpers(self):
        self.assertEqual(hb.ma_cross_side(1, 2, 3, 2), "LONG")
        self.assertEqual(hb.ma_cross_side(3, 2, 1, 2), "SHORT")
        self.assertIsNone(hb.ma_cross_side(3, 2, 4, 2))
        self.assertIsNone(hb.ma_cross_side(2, 2, 2, 2))
        self.assertTrue(hb.ma_in_deadband(100.0, 100.005, 100.0))
        self.assertFalse(hb.ma_in_deadband(100.0, 101.0, 100.0))

    def test_up_cross_then_down_cross_reverses(self):
        prices = [100.0] * 30 + [120.0] * 15 + [50.0] * 25
        trades = hb.run_dual_ma(pack(prices), ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 2)
        self.assertEqual(trades[0]["direction"], "LONG")
        self.assertEqual(trades[0]["reason"], "cross")
        self.assertAlmostEqual(trades[0]["entry"], 120.0)
        self.assertAlmostEqual(trades[0]["exit"], 50.0)
        self.assertAlmostEqual(trades[0]["fee"], 0.2)
        self.assertAlmostEqual(trades[0]["pnl"], (50.0 - 120.0) / 120.0 * 200.0 - 0.2)
        self.assertEqual(trades[1]["direction"], "SHORT")
        self.assertAlmostEqual(trades[1]["entry"], 50.0)
        self.assertEqual(trades[0]["exit_ts"], trades[1]["entry_ts"])
        self.assertNotIn(trades[0]["reason"], {"BE", "trail", "TP1", "range", "channel", "normalize"})

    def test_deadband_flattens_without_a_reverse(self):
        prices = [100.0] * 30 + [120.0] * 80
        trades = hb.run_dual_ma(pack(prices), ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "LONG")
        self.assertEqual(trades[0]["reason"], "deadband")
        self.assertEqual(len(trades), 1)

    def test_gap_does_not_bridge_the_averages(self):
        prices = [100.0] * 30 + [120.0] * 10
        data = pack(prices)
        # 抽掉一根，后面的均线不能把缺口两侧拼起来再开新仓
        data["SOL"] = np.delete(data["SOL"], 5, axis=0)
        trades = hb.run_dual_ma(data, ["SOL"])["trades"]
        self.assertEqual(trades, [])

    def test_cap_keeps_four(self):
        prices = [100.0] * 30 + [120.0] * 5
        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        data = {s: pack(prices)["SOL"] for s in names}
        out = hb.run_dual_ma(data, names)
        first = min(t["entry_ts"] for t in out["trades"])
        opened = [t["symbol"] for t in out["trades"] if t["entry_ts"] == first]
        self.assertEqual(sorted(opened), ["BNB", "DOT", "LINK", "SOL"])
        self.assertGreaterEqual(out["n_capped"], 1)

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_m(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([]))


if __name__ == "__main__":
    unittest.main()
