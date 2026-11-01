#!/usr/bin/env python3
"""候选 N：单币 RSI(14) 极值反向。不下载。不改 F–M 的常量，实盘上限仍全 0。"""
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


def times(n):
    return START + np.arange(n) * HOUR


class TestRsiFade(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_RSI, "rsi_fade")
        self.assertEqual(hb.RSI_N, 14)
        self.assertEqual(hb.RSI_HIGH, 70.0)
        self.assertEqual(hb.RSI_LOW, 30.0)
        self.assertEqual(hb.RSI_MID, 50.0)
        self.assertEqual(hb.RSI_HOLD_H, 24)
        self.assertEqual(hb.MA_FAST, 10)
        self.assertEqual(hb.MA_SLOW, 30)
        self.assertEqual(hb.MA_DEAD, 0.0001)
        self.assertEqual(hb.ORB_OPEN_H, 4)
        self.assertEqual(hb.ORB_HOLD_H, 8)
        self.assertEqual(hb.PAIR_Z_ENTER, 2.0)
        self.assertEqual(hb.PAIR_Z_WIN, 72)
        self.assertEqual(hb.DON_ENTRY_N, 20)
        self.assertEqual(hb.DON_EXIT_N, 10)
        self.assertEqual(hb.FADE_Z_ENTER, 2.0)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.PASS_OOS_MIN, 30)

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        self.assertTrue(all(v == 0 for v in cfg["max_positions_per_symbol"].values()))
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("rsi_fade", text)
            self.assertNotIn("dual_ma_cross", text)
            self.assertNotIn("utc_orb", text)

    def test_wilder_seed_and_smooth(self):
        up = np.arange(100.0, 130.0)
        rsi = hb.wilder_rsi(up, times(len(up)))
        self.assertTrue(np.isnan(rsi[13]))
        self.assertEqual(rsi[14], 100.0)
        down = np.arange(130.0, 100.0, -1.0)
        self.assertEqual(hb.wilder_rsi(down, times(len(down)))[14], 0.0)
        # 14 根上涨之后跌 1。种子平均涨幅 1，再按 Wilder 滚一次。
        mixed = np.concatenate([np.arange(100.0, 115.0), [113.0]])  # 14 次 +1 之后跌 1
        got = hb.wilder_rsi(mixed, times(len(mixed)))
        avg_gain = (1.0 * 13 + 0.0) / 14
        avg_loss = (0.0 * 13 + 1.0) / 14
        rs = avg_gain / avg_loss
        expect = 100.0 - 100.0 / (1.0 + rs)
        self.assertAlmostEqual(got[15], expect)

    def test_overbought_short_exits_when_rsi_returns_to_50(self):
        prices = list(np.arange(100.0, 115.0)) + [101.0, 101.0, 101.0]
        trades = hb.run_rsi_fade(pack(prices), ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "mid")
        self.assertAlmostEqual(t["entry"], 114.0)
        self.assertAlmostEqual(t["exit"], 101.0)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], (114.0 - 101.0) / 114.0 * 200.0 - 0.2)
        self.assertGreater(t["pnl"], 0)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "cross", "channel", "normalize"})

    def test_oversold_goes_long(self):
        prices = list(np.arange(130.0, 115.0, -1.0)) + [140.0, 140.0]
        t = hb.run_rsi_fade(pack(prices), ["SOL"])["trades"][0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "mid")
        self.assertAlmostEqual(t["entry"], 116.0)
        self.assertAlmostEqual(t["pnl"], (140.0 - 116.0) / 116.0 * 200.0 - 0.2)

    def test_sustained_extreme_exits_at_24h_without_same_bar_reentry(self):
        prices = list(np.arange(100.0, 160.0))
        trades = hb.run_rsi_fade(pack(prices), ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 2)
        t = trades[0]
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "time")
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 24.0)
        self.assertEqual(trades[1]["entry_ts"] - t["exit_ts"], HOUR)

    def test_gap_does_not_bridge_rsi(self):
        prices = list(np.arange(100.0, 120.0))
        data = pack(prices)
        data["SOL"] = np.delete(data["SOL"], 8, axis=0)
        self.assertEqual(hb.run_rsi_fade(data, ["SOL"])["trades"], [])

    def test_cap_keeps_four(self):
        prices = list(np.arange(100.0, 160.0))
        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        data = {s: pack(prices)["SOL"] for s in names}
        out = hb.run_rsi_fade(data, names)
        first = min(t["entry_ts"] for t in out["trades"])
        opened = [t["symbol"] for t in out["trades"] if t["entry_ts"] == first]
        self.assertEqual(sorted(opened), ["BNB", "DOT", "LINK", "SOL"])
        self.assertGreaterEqual(out["n_capped"], 1)

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_n(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([]))


if __name__ == "__main__":
    unittest.main()
