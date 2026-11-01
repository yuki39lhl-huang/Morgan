#!/usr/bin/env python3
"""候选 G：特异残差均值回归。不下载行情。不改 F 的常量，实盘上限仍全 0。"""
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import hist_backtest as hb

POOL = list(hb.POOL)
START = int(datetime(2024, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)


def hourly_from_returns(rets: np.ndarray):
    """rets shape (n_steps, 5)，列顺序与 POOL 一致。第 0 根收盘价是 100。"""
    closes = np.empty((len(rets) + 1, rets.shape[1]), dtype=np.float64)
    closes[0] = 100.0
    for i, row in enumerate(rets):
        closes[i + 1] = closes[i] * (1.0 + row)
    data = {}
    for k, sym in enumerate(POOL):
        rows = []
        for i in range(len(closes)):
            px = float(closes[i, k])
            t = START + i * 3_600_000
            rows.append((t, px, px, px, 1.0))
        data[sym] = np.asarray(rows, dtype=np.float64)
    return data, closes


def _bg(n_steps: int) -> np.ndarray:
    """小幅、各币不同相位的收益。幅度压在自身 z 进场线下面。"""
    rets = np.zeros((n_steps, 5), dtype=np.float64)
    for i in range(n_steps):
        for j in range(5):
            rets[i, j] = 0.0004 * np.sin((i + 3 * j) / 5.0)
    return rets


class TestXsMeanRevert(unittest.TestCase):
    def test_constants_and_f_untouched(self):
        self.assertEqual(hb.ENTRY_REV, "xs_mean_revert")
        self.assertEqual(hb.REV_Z_WIN, 48)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertEqual(hb.ENTRY_XS, "xs_rel_strength")
        self.assertEqual(hb.XS_BAR_MIN, 240)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SLIPPAGE_RT, 0.0)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        caps = cfg["max_positions_per_symbol"]
        self.assertEqual(set(caps), {"BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"})
        self.assertTrue(all(v == 0 for v in caps.values()))
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("xs_mean_revert", text)
            self.assertNotIn("xs_rel_strength", text)

    def test_residual_is_versus_equal_weight_basket(self):
        raw = np.array([0.05, 0.00, 0.00, 0.00, 0.00])
        resid = hb.idiosyncratic(raw)
        self.assertAlmostEqual(float(resid.sum()), 0.0)
        self.assertAlmostEqual(float(resid[0]), 0.04)
        self.assertTrue(np.all(resid[1:] < 0))
        self.assertTrue(np.isnan(hb.idiosyncratic(np.array([0.01, np.nan, 0.0, 0.0, 0.0]))).all())

    def test_own_z_uses_prior_window_not_current(self):
        hist = np.zeros((48, 1))
        hist[:, 0] = 0.001
        hist[0, 0] = -0.001
        now = np.array([0.05])
        mu = float(hist.mean())
        sd = float(hist.std())
        z = hb.own_residual_z(hist, now)
        self.assertAlmostEqual(float(z[0]), (0.05 - mu) / sd)
        flat = hb.own_residual_z(np.zeros((48, 1)), np.array([0.01]))
        self.assertTrue(np.isnan(flat[0]))

    def test_fade_direction_and_ties(self):
        z = {"SOL": -2.0, "BNB": -0.2, "DOT": 1.5, "LINK": 0.4, "XRP": 0.0}
        long_s, short_s = hb.revert_sides(z, {})
        self.assertEqual(long_s, "SOL")
        self.assertEqual(short_s, "DOT")
        long_s, short_s = hb.revert_sides(z, {"SOL": {"side": "LONG"}})
        self.assertIsNone(long_s)
        self.assertEqual(short_s, "DOT")
        tied = {"SOL": 2.0, "BNB": 2.0, "DOT": 0.0, "LINK": -0.2, "XRP": -3.0}
        long_s, short_s = hb.revert_sides(tied, {})
        self.assertEqual(long_s, "XRP")
        self.assertIsNone(short_s)

    def test_fade_dumps_and_exits_when_residual_reverts(self):
        rets = _bg(80)
        rets[60, 0] = -0.04
        rets[61, 0] = 0.045
        data, closes = hourly_from_returns(rets)
        trades = hb.run_revert(data, POOL)["trades"]
        # rets[60] 落在 close[61]。进场价应是跌完的这根，不是前一根。
        sol = [t for t in trades if t["symbol"] == "SOL" and abs(t["entry"] - closes[61, 0]) < 1e-6]
        self.assertEqual(len(sol), 1)
        t = sol[0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "revert")
        self.assertGreater(t["exit"], t["entry"])
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], t["gross"] - 0.2)
        self.assertNotEqual(t["entry"], closes[60, 0])
        self.assertTrue(all(x["reason"] not in {"BE", "trail", "TP1", "TP", "SL"} for x in trades))

    def test_unreverted_residual_exits_at_six_hours(self):
        rets = _bg(80)
        rets[60:70, 0] = -0.01
        data, _closes = hourly_from_returns(rets)
        trades = hb.run_revert(data, POOL)["trades"]
        timed = [t for t in trades if t["symbol"] == "SOL" and t["reason"] == "time"]
        self.assertGreaterEqual(len(timed), 1)
        hold = (timed[0]["exit_ts"] - timed[0]["entry_ts"]) / 3_600_000
        self.assertAlmostEqual(hold, 6.0)
        self.assertEqual(timed[0]["direction"], "LONG")

    def test_common_move_does_not_open(self):
        rets = np.zeros((80, 5))
        rets[60] = 0.03
        data, _closes = hourly_from_returns(rets)
        trades = hb.run_revert(data, POOL)["trades"]
        self.assertEqual(trades, [])

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_g(trades)

        self.assertTrue(judge([row("2026-10-04", 0.1) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.1) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([row("2026-10-03", 1.0) for _ in range(50)]))


if __name__ == "__main__":
    unittest.main()
