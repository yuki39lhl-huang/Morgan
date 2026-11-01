#!/usr/bin/env python3
"""候选 H：压缩后的区间突破。不下载行情。不改 F/G 的常量，实盘上限仍全 0。"""
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import hist_backtest as hb

START = int(datetime(2024, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)


def pack(bars, start=START, step_h=1):
    rows = []
    for i, (high, low, close) in enumerate(bars):
        t = start + i * step_h * 3_600_000
        rows.append((t, float(high), float(low), float(close), 1.0))
    return {"SOL": np.asarray(rows, dtype=np.float64)}


def _setup(signal, after, squeeze=None):
    base = [(110.0, 90.0, 100.0)] * hb.COMP_BASE
    box = [(101.0, 99.0, 100.0)] * hb.COMP_SQUEEZE if squeeze is None else squeeze
    return base + box + [signal] + list(after)


class TestVolCompressBreak(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_COMP, "vol_compress_break")
        self.assertEqual(hb.COMP_SQUEEZE, 12)
        self.assertEqual(hb.COMP_BASE, 48)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.COMP_HOLD_H, 12)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertEqual(hb.XS_BAR_MIN, 240)
        self.assertEqual(hb.REV_Z_WIN, 48)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SLIPPAGE_RT, 0.0)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        caps = cfg["max_positions_per_symbol"]
        self.assertEqual(caps, {"BTC": 0, "ETH": 0, "SOL": 1, "BNB": 1, "DOT": 1, "LINK": 1, "XRP": 1})
        self.assertEqual(cfg["max_positions"], 2)
        self.assertEqual(cfg["entry_strategy"], "rsi_fade")
        self.assertTrue(cfg["binance_testnet"])
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("vol_compress_break", text)
            self.assertNotIn("xs_mean_revert", text)
            self.assertNotIn("xs_rel_strength", text)

    def test_break_requires_compression_and_close(self):
        need = hb.COMP_BASE + hb.COMP_SQUEEZE + 1
        high = np.array([110.0] * hb.COMP_BASE + [101.0] * hb.COMP_SQUEEZE + [103.0])
        low = np.array([90.0] * hb.COMP_BASE + [99.0] * hb.COMP_SQUEEZE + [102.0])
        close = np.array([100.0] * (need - 1) + [102.5])
        side, box_high, box_low, ratio = hb.compression_break(high, low, close)
        self.assertEqual(side, "LONG")
        self.assertAlmostEqual(box_high, 101.0)
        self.assertAlmostEqual(box_low, 99.0)
        self.assertAlmostEqual(ratio, 2.0 / 20.0)
        close[-1] = 98.0
        low[-1] = 97.0
        side, _, _, _ = hb.compression_break(high, low, close)
        self.assertEqual(side, "SHORT")
        close[-1] = 100.0
        side, _, _, _ = hb.compression_break(high, low, close)
        self.assertIsNone(side)
        # 对照区间一样宽，不叫压缩，收盘冲出去也不做
        high[:need] = 110.0
        low[:need] = 90.0
        close[-1] = 120.0
        side, _, _, ratio = hb.compression_break(high, low, close)
        self.assertIsNone(side)
        self.assertGreater(ratio, hb.COMP_RATIO)

    def test_wick_above_the_box_does_not_enter(self):
        bars = _setup((110.0, 99.5, 100.5), [(101.0, 100.0, 100.2)] * 3)
        trades = hb.run_compress(pack(bars), ["SOL"])["trades"]
        self.assertEqual(trades, [])

    def test_long_break_exits_on_the_other_side_of_the_box(self):
        after = [(104.0, 103.0, 103.5)] * 2 + [(100.0, 97.0, 98.0)]
        bars = _setup((103.0, 102.0, 102.5), after)
        trades = hb.run_compress(pack(bars), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "structure")
        self.assertAlmostEqual(t["entry"], 102.5)
        self.assertAlmostEqual(t["exit"], 98.0)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], t["gross"] - 0.2)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / 3_600_000, 3.0)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "TP", "SL", "revert", "rebalance"})

    def test_time_exit_when_the_break_holds(self):
        after = [(104.0, 103.0, 103.5)] * hb.COMP_HOLD_H
        bars = _setup((103.0, 102.0, 102.5), after)
        trades = hb.run_compress(pack(bars), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "time")
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / 3_600_000, 12.0)
        self.assertGreater(t["gross"], 0)

    def test_gap_in_the_window_does_not_trade(self):
        bars = _setup((103.0, 102.0, 102.5), [(104.0, 103.0, 103.5)] * 3)
        data = pack(bars)
        # 抽掉对照区间里的一小时，窗口不再连续
        data["SOL"] = np.delete(data["SOL"], 10, axis=0)
        self.assertEqual(hb.run_compress(data, ["SOL"])["trades"], [])

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_h(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([row("2026-10-03", 1.0) for _ in range(80)]))


if __name__ == "__main__":
    unittest.main()
