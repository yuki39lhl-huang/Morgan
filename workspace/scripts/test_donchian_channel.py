#!/usr/bin/env python3
"""候选 J：唐奇安 20/10。不下载。不改 F/G/H/I 的常量，实盘上限仍全 0。"""
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


def pack(buckets):
    """每个 4h 桶展开成 4 根 1h。高低点放在桶的最后一小时，收盘就是该小时收盘。"""
    rows = []
    for b, (high, low, close) in enumerate(buckets):
        for h in range(hb.DON_BAR_H):
            t = START + (b * hb.DON_BAR_H + h) * HOUR
            if h < hb.DON_BAR_H - 1:
                rows.append((t, float(close), float(close), float(close), 1.0))
            else:
                rows.append((t, float(high), float(low), float(close), 1.0))
    return {"SOL": np.asarray(rows, dtype=np.float64)}


def quiet(n, high=110.0, low=90.0, close=100.0):
    return [(high, low, close)] * n


class TestDonchianChannel(unittest.TestCase):
    def test_constants_frozen(self):
        self.assertEqual(hb.ENTRY_DON, "donchian_channel")
        self.assertEqual(hb.DON_BAR_H, 4)
        self.assertEqual(hb.DON_ENTRY_N, 20)
        self.assertEqual(hb.DON_EXIT_N, 10)
        self.assertEqual(hb.FADE_Z_WIN, 90)
        self.assertEqual(hb.FADE_Z_ENTER, 2.0)
        self.assertEqual(hb.FADE_MAX_HOLD_H, 24)
        self.assertEqual(hb.COMP_SQUEEZE, 12)
        self.assertEqual(hb.COMP_BASE, 48)
        self.assertEqual(hb.COMP_RATIO, 0.5)
        self.assertEqual(hb.COMP_HOLD_H, 12)
        self.assertEqual(hb.REV_Z_WIN, 48)
        self.assertEqual(hb.REV_Z_ENTER, 1.0)
        self.assertEqual(hb.REV_MAX_HOLD_H, 6)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertEqual(hb.XS_BAR_MIN, 240)
        self.assertEqual(hb.FUND_MIN, 0.0005)
        self.assertEqual(hb.TB_N, 20)
        self.assertEqual(hb.TB_VOL_MULT, 1.2)
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SLIPPAGE_RT, 0.0)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.PASS_OOS_MIN, 30)

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        caps = cfg["max_positions_per_symbol"]
        self.assertEqual(caps, {"BTC": 0, "ETH": 0, "SOL": 1, "BNB": 1, "DOT": 1, "LINK": 1, "XRP": 1})
        self.assertEqual(cfg["max_positions"], 4)
        self.assertEqual(cfg["entry_strategy"], "rsi_fade")
        self.assertTrue(cfg["binance_testnet"])
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
            self.assertNotIn("donchian_channel", text)
            self.assertNotIn("funding_extreme_fade", text)
            self.assertNotIn("vol_compress_break", text)

    def test_entry_uses_prior_channel_not_the_wick(self):
        high = np.array([110.0] * 20 + [130.0])
        low = np.array([90.0] * 20 + [100.0])
        close = np.array([100.0] * 20 + [105.0])
        self.assertIsNone(hb.donchian_entry(high, low, close))
        close[-1] = 110.0
        self.assertIsNone(hb.donchian_entry(high, low, close))
        close[-1] = 111.0
        self.assertEqual(hb.donchian_entry(high, low, close), "LONG")
        close[-1] = 89.0
        self.assertEqual(hb.donchian_entry(high, low, close), "SHORT")

    def test_wide_range_still_breaks_out(self):
        # 区间很宽也做。不要求先压缩。
        buckets = quiet(20, high=200.0, low=50.0, close=100.0) + [(210.0, 90.0, 201.0), (205.0, 180.0, 190.0)]
        trades = hb.run_donchian(pack(buckets), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "LONG")
        self.assertAlmostEqual(trades[0]["entry"], 201.0)

    def test_long_exits_on_the_shorter_channel(self):
        buckets = quiet(20) + [(112.0, 108.0, 111.0)]
        buckets += [(115.0, 108.0, 112.0)] * 9
        buckets += [(116.0, 106.0, 107.0), (116.0, 106.0, 112.0)]
        trades = hb.run_donchian(pack(buckets), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "LONG")
        self.assertEqual(t["reason"], "channel")
        self.assertAlmostEqual(t["entry"], 111.0)
        self.assertAlmostEqual(t["exit"], 107.0)
        self.assertAlmostEqual(t["fee"], 0.2)
        self.assertAlmostEqual(t["pnl"], t["gross"] - 0.2)
        self.assertAlmostEqual((t["exit_ts"] - t["entry_ts"]) / HOUR, 10 * hb.DON_BAR_H)
        self.assertNotIn(t["reason"], {"BE", "trail", "TP1", "TP", "SL", "normalize", "structure"})

    def test_short_break_and_channel_exit(self):
        buckets = quiet(20) + [(95.0, 88.0, 89.0)]
        buckets += [(92.0, 85.0, 88.0)] * 9
        buckets += [(96.0, 84.0, 96.0), (90.0, 84.0, 88.0)]
        t = hb.run_donchian(pack(buckets), ["SOL"])["trades"][0]
        self.assertEqual(t["direction"], "SHORT")
        self.assertEqual(t["reason"], "channel")
        self.assertAlmostEqual(t["entry"], 89.0)
        self.assertAlmostEqual(t["exit"], 96.0)
        self.assertAlmostEqual(t["pnl"], (89.0 - 96.0) / 89.0 * 200.0 - 0.2)

    def test_trend_holds_until_the_channel_not_a_fixed_hour(self):
        buckets = quiet(20) + [(112.0, 105.0, 111.0)]
        buckets += [(130.0, 120.0, 125.0)] * 30
        trades = hb.run_donchian(pack(buckets), ["SOL"])["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["reason"], "eod")
        self.assertGreater((t["exit_ts"] - t["entry_ts"]) / HOUR, 24)

    def test_last_bar_does_not_open(self):
        buckets = quiet(20) + [(112.0, 105.0, 111.0)]
        self.assertEqual(hb.run_donchian(pack(buckets), ["SOL"])["trades"], [])

    def test_gap_in_the_window_does_not_trade(self):
        buckets = quiet(40)
        buckets[30] = (112.0, 105.0, 111.0)
        intact = hb.run_donchian(pack(buckets), ["SOL"])["trades"]
        self.assertEqual(len(intact), 1)
        data = pack(buckets)
        # 抽掉回看中间的一小时，4h 桶不完整，通道不再连续
        data["SOL"] = np.delete(data["SOL"], 20 * hb.DON_BAR_H + 1, axis=0)
        self.assertEqual(hb.run_donchian(data, ["SOL"])["trades"], [])

    def test_reverse_when_the_close_also_breaks_the_entry_channel(self):
        buckets = quiet(20) + [(112.0, 105.0, 111.0)]
        buckets += [(112.0, 100.0, 111.0)] * 9
        buckets += [(95.0, 85.0, 89.0), (90.0, 80.0, 88.0)]
        trades = hb.run_donchian(pack(buckets), ["SOL"])["trades"]
        self.assertGreaterEqual(len(trades), 2)
        self.assertEqual(trades[0]["direction"], "LONG")
        self.assertEqual(trades[0]["reason"], "channel")
        self.assertAlmostEqual(trades[0]["exit"], 89.0)
        self.assertEqual(trades[1]["direction"], "SHORT")
        self.assertAlmostEqual(trades[1]["entry"], 89.0)
        self.assertEqual(trades[0]["exit_ts"], trades[1]["entry_ts"])

    def test_cap_keeps_four(self):
        buckets = quiet(20) + [(112.0, 105.0, 111.0), (112.0, 105.0, 111.0)]
        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        data = {s: pack(buckets)["SOL"] for s in names}
        out = hb.run_donchian(data, names)
        self.assertEqual(len(out["trades"]), 4)
        self.assertEqual(out["n_capped"], 1)
        self.assertEqual(sorted(t["symbol"] for t in out["trades"]), ["BNB", "DOT", "LINK", "SOL"])

    def test_pass_bar(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_j(trades)

        self.assertTrue(judge([row("2026-10-04", 0.2) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -0.2) for _ in range(40)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([row("2026-10-03", 1.0) for _ in range(80)]))


if __name__ == "__main__":
    unittest.main()
