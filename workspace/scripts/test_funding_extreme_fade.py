#!/usr/bin/env python3
"""候选 I：费率相对自身历史的极端反向。不下载。不改 F/G/H/E 的常量，实盘上限仍全 0。"""
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


def prices(n_hours, at):
    rows = []
    for i in range(n_hours):
        px = float(at(i))
        rows.append((START + i * HOUR, px, px, px, 1.0))
    return np.asarray(rows, dtype=np.float64)


def funding(n, at, bump_ms=None):
    rows = []
    for i in range(n):
        extra = 1 if bump_ms is not None and i in bump_ms else 0
        rows.append((START + i * 8 * HOUR + extra, float(at(i))))
    return np.asarray(rows, dtype=np.float64)


def calm(i):
    return 0.0001 if i % 2 == 0 else -0.0001


class TestFundingExtremeFade(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_FADE, "funding_extreme_fade")
        self.assertEqual(hb.FADE_Z_WIN, 90)
        self.assertEqual(hb.FADE_Z_ENTER, 2.0)
        self.assertEqual(hb.FADE_MAX_HOLD_H, 24)
        self.assertEqual(hb.FUND_MIN, 0.0005)
        self.assertEqual(hb.ENTRY_FUND, "funding_carry_long")
        self.assertEqual(hb.COMP_SQUEEZE, 12)
        self.assertEqual(hb.COMP_BASE, 48)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.COMP_HOLD_H, 12)
        self.assertEqual(hb.REV_Z_WIN, 48)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertEqual(hb.XS_BAR_MIN, 240)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SLIPPAGE_RT, 0.0)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.PASS_OOS_MIN, 30)

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        caps = cfg["max_positions_per_symbol"]
        self.assertTrue(all(v == 0 for v in caps.values()))
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("funding_extreme_fade", text)
            self.assertNotIn("vol_compress_break", text)
            self.assertNotIn("xs_mean_revert", text)
            self.assertNotIn("xs_rel_strength", text)

    def test_funding_z_and_side(self):
        hist = np.arange(hb.FADE_Z_WIN, dtype=np.float64)
        now = 100.0
        z = hb.funding_z(hist, now)
        self.assertAlmostEqual(z, (now - hist.mean()) / hist.std())
        self.assertTrue(np.isnan(hb.funding_z(np.ones(hb.FADE_Z_WIN), 1.0)))
        self.assertTrue(np.isnan(hb.funding_z(hist[:-1], now)))
        self.assertEqual(hb.fade_side(2.0), "SHORT")
        self.assertEqual(hb.fade_side(-2.0), "LONG")
        self.assertIsNone(hb.fade_side(1.9))
        self.assertIsNone(hb.fade_side(float("nan")))
        self.assertTrue(hb.funding_normalized("SHORT", 0.0))
        self.assertFalse(hb.funding_normalized("SHORT", 0.1))
        self.assertTrue(hb.funding_normalized("LONG", 0.0))
        self.assertFalse(hb.funding_normalized("LONG", -0.1))

    def test_short_fade_uses_settlement_hour_close_and_normalizes(self):
        def rate(i):
            if i < 90:
                return calm(i)
            if i == 90:
                return 0.05
            return -0.05

        px = {90 * 8 - 1: 150.0, 90 * 8: 100.0, 91 * 8: 90.0}
        bars = prices(92 * 8 + 2, lambda i: px.get(i, 100.0))
        out = hb.run_funding_fade(
            {"SOL": bars},
            {"SOL": funding(92, rate)},
            ["SOL"],
        )
        self.assertEqual(len(out["trades"]), 1)
        t = out["trades"][0]
        self.assertEqual(t["symbol"], "SOL")
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "normalize")
        self.assertAlmostEqual(t["entry"], 100.0)
        self.assertAlmostEqual(t["exit"], 90.0)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], (100.0 - 90.0) / 100.0 * 200.0 - 0.2)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 8.0)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "TP", "SL", "rebalance", "structure"})

    def test_plus_one_ms_still_maps_to_the_same_hour(self):
        def rate(i):
            if i < 90:
                return calm(i)
            if i == 90:
                return 0.05
            return -0.05

        px = {90 * 8: 80.0, 91 * 8: 70.0}
        bars = prices(92 * 8 + 2, lambda i: px.get(i, 100.0))
        out = hb.run_funding_fade(
            {"SOL": bars},
            {"SOL": funding(92, rate, bump_ms={90, 91})},
            ["SOL"],
        )
        t = out["trades"][0]
        self.assertAlmostEqual(t["entry"], 80.0)
        self.assertAlmostEqual(t["exit"], 70.0)
        self.assertEqual(t["entry_ts"], START + 90 * 8 * HOUR + HOUR)

    def test_long_fade_when_funding_is_extremely_negative(self):
        def rate(i):
            if i < 90:
                return calm(i)
            if i == 90:
                return -0.05
            return 0.05

        px = {90 * 8: 100.0, 91 * 8: 110.0}
        bars = prices(92 * 8 + 2, lambda i: px.get(i, 100.0))
        t = hb.run_funding_fade({"SOL": bars}, {"SOL": funding(92, rate)}, ["SOL"])["trades"][0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "normalize")
        self.assertAlmostEqual(t["pnl"], (110.0 - 100.0) / 100.0 * 200.0 - 0.2)

    def test_sustained_extreme_exits_at_24h(self):
        def rate(i):
            if i < 90:
                return calm(i)
            return 0.01 * (i - 89)

        bars = prices(100 * 8, lambda i: 100.0 + i * 0.01)
        trades = hb.run_funding_fade({"SOL": bars}, {"SOL": funding(100, rate)}, ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "time")
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 24.0)
        self.assertTrue(all(x["reason"] != "normalize" for x in trades))
        for a, b in zip(trades, trades[1:]):
            self.assertLessEqual(a["exit_ts"], b["entry_ts"])

    def test_cap_keeps_four_and_drops_the_fifth(self):
        def rate(i):
            if i < 90:
                return calm(i)
            if i == 90:
                return 0.05
            return -0.05

        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        bars = prices(92 * 8 + 2, lambda i: 100.0)
        fr = funding(92, rate)
        out = hb.run_funding_fade({s: bars for s in names}, {s: fr for s in names}, names)
        self.assertEqual(len(out["trades"]), 4)
        self.assertEqual(out["n_dropped"], 1)
        self.assertEqual([t["symbol"] for t in out["trades"]], names[:4])

    def test_missing_funding_is_not_replaced(self):
        bars = prices(10, lambda i: 100.0)
        out = hb.run_funding_fade({"SOL": bars}, {"SOL": np.zeros((0, 2))}, ["SOL"])
        self.assertEqual(out["trades"], [])
        self.assertEqual(out["n_signal"], 0)

    def test_pass_bar_does_not_loosen_when_sparse(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_i(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([row("2026-10-03", 1.0) for _ in range(80)]))
        self.assertFalse(judge([]))


if __name__ == "__main__":
    unittest.main()
