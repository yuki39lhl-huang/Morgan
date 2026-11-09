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

    def test_live_trial_is_small_and_testnet_only(self):
        import rsi_fade as rf

        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        self.assertEqual(
            cfg["max_positions_per_symbol"],
            {"BTC": 0, "ETH": 0, "SOL": 1, "BNB": 1, "DOT": 1, "LINK": 1, "XRP": 1},
        )
        self.assertEqual(cfg["max_positions"], 4)
        self.assertEqual(cfg["entry_strategy"], "rsi_fade")
        self.assertEqual(cfg["total_capital"], 100)
        self.assertAlmostEqual(cfg["position_size_pct"], 0.20)
        self.assertEqual(cfg["leverage"], 10)
        self.assertEqual(cfg["total_capital"] * cfg["position_size_pct"] * cfg["leverage"], 200)
        self.assertTrue(cfg["binance_testnet"])
        self.assertIn("testnet.binancefuture.com", cfg["binance_auto_base"])
        self.assertIn("testnet.binancefuture.com", cfg["binance_futures"])
        self.assertNotIn("fapi.binance.com", cfg["binance_auto_base"])
        self.assertFalse(cfg["score_threshold"]["allow_short"])
        self.assertTrue(rf.rules_match(cfg["rsi_fade"]))
        self.assertTrue(rf.testnet_ok(cfg))
        root = Path(__file__).resolve().parent
        monitor = (root / "crypto_signal_monitor.py").read_text()
        self.assertIn("rsi_fade", monitor)
        self.assertIn("_rsi_fade_trial_pass", monitor)
        for name in ("binance_auto_trade.py", "strategy_layer.py"):
            text = (root / name).read_text()
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

    def _series(self, prices):
        return [(START + i * HOUR, float(px)) for i, px in enumerate(prices)]

    def test_plan_enters_short_and_caps_at_two(self):
        import rsi_fade as rf

        series = self._series(list(np.arange(100.0, 160.0)))
        act = rf.evaluate_symbol(series, None, None)
        self.assertEqual(act["op"], "enter")
        self.assertEqual(act["side"], "SHORT")
        self.assertGreaterEqual(act["rsi"], 70)
        names = ["SOL", "BNB", "DOT", "LINK", "XRP"]
        bars = {s: series for s in names}
        caps = {s: 1 for s in names}
        entered = [a["symbol"] for a in rf.plan_actions(bars, [], {}, names, 2, caps) if a["op"] == "enter"]
        self.assertEqual(entered, ["SOL", "BNB"])
        acted = {s: series[-1][0] for s in entered}
        nxt = [a["symbol"] for a in rf.plan_actions(bars, [], acted, names, 2, caps) if a["op"] == "enter"]
        self.assertEqual(nxt, ["DOT", "LINK"])

    def test_time_exit_does_not_reenter_same_bar(self):
        import rsi_fade as rf

        series = self._series(list(np.arange(100.0, 160.0)))
        pos = {
            "symbol": "SOL",
            "type": "SHORT",
            "strategy": "rsi_fade",
            "regime": "rsi",
            "entry_bar_open": series[-1][0] - 24 * HOUR,
        }
        actions = rf.plan_actions({"SOL": series}, [pos], {}, ["SOL"], 2, {"SOL": 1})
        self.assertEqual([(a["op"], a["reason"]) for a in actions], [("exit", "time")])

    def test_rules_reject_retune_and_mainnet(self):
        import rsi_fade as rf

        spec = {
            "enabled": True, "period": 14, "high": 70, "low": 30, "mid": 50,
            "max_hold_hours": 24, "notional_usdt": 200, "max_concurrent": 4,
        }
        self.assertTrue(rf.rules_match(spec))
        spec["high"] = 65
        self.assertFalse(rf.rules_match(spec))
        self.assertFalse(rf.testnet_ok({
            "binance_testnet": True,
            "binance_auto_base": "https://fapi.binance.com",
            "binance_futures": "https://fapi.binance.com/fapi/v1",
        }))
        forming = START + 5 * HOUR
        klines = [
            [START + i * HOUR, 1, 1, 1, 100 + i, 1, START + (i + 1) * HOUR - 1]
            for i in range(6)
        ]
        self.assertEqual(len(rf.closed_pairs(klines, forming)), 5)

    def _row(self, day, pnl, symbol="SOL", side="LONG", hour=0):
        dt = datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=hb.CST)
        dt = dt.replace(hour=hour)
        return {
            "day": day,
            "pnl": pnl,
            "symbol": symbol,
            "direction": side,
            "entry_ts": int(dt.timestamp() * 1000),
        }

    def test_robustness_rule_without_changing_thresholds(self):
        before = (hb.RSI_N, hb.RSI_HIGH, hb.RSI_LOW, hb.RSI_MID, hb.RSI_HOLD_H, hb.SAMPLE_IN_END)
        oos = (
            [self._row("2026-10-04", 1.0, symbol=s, hour=h % 20) for h, s in enumerate(["SOL", "BNB", "DOT", "LINK", "XRP"] * 2)]
            + [self._row("2026-10-16", 1.0, symbol="BNB", hour=h) for h in range(10)]
            + [self._row("2026-10-21", 1.0, symbol="DOT", side="SHORT", hour=h) for h in range(20)]
        )
        early = [self._row("2026-09-10", 1.0, symbol="LINK", hour=h) for h in range(5)]
        with redirect_stdout(io.StringIO()):
            good = hb.rsi_robustness(early + oos)
        self.assertEqual(good["verdict"], "ROBUST")
        self.assertTrue(good["a_ok"] and good["b_ok"] and good["c_ok"])
        self.assertEqual(good["main"]["n"], 40)
        self.assertEqual(good["drop3"]["n"], 37)
        self.assertGreater(good["drop3"]["ev"], 0)
        self.assertEqual(good["n_cut_ok"], 3)
        self.assertEqual((hb.RSI_N, hb.RSI_HIGH, hb.RSI_LOW, hb.RSI_MID, hb.RSI_HOLD_H, hb.SAMPLE_IN_END), before)

        fragile = (
            [self._row("2026-10-05", 50.0, symbol="SOL", hour=1),
             self._row("2026-10-06", 50.0, symbol="BNB", hour=2),
             self._row("2026-10-07", 50.0, symbol="DOT", hour=3)]
            + [self._row("2026-10-08", -1.0, symbol="XRP", hour=h % 20) for h in range(30)]
        )
        with redirect_stdout(io.StringIO()):
            bad_tail = hb.rsi_robustness(fragile)
        self.assertEqual(bad_tail["verdict"], "FRAGILE")
        self.assertTrue(bad_tail["a_ok"])
        self.assertFalse(bad_tail["b_ok"])
        self.assertLess(bad_tail["drop3"]["ev"], 0)

        clustered = [self._row("2026-10-06", 1.0, hour=h % 23) for h in range(40)]
        with redirect_stdout(io.StringIO()):
            one_cut = hb.rsi_robustness(clustered)
        self.assertEqual(one_cut["verdict"], "FRAGILE")
        self.assertTrue(one_cut["a_ok"] and one_cut["b_ok"])
        self.assertEqual(one_cut["n_cut_ok"], 1)
        self.assertEqual(one_cut["by_side"]["LONG"]["n"], 40)
        self.assertEqual(one_cut["half1"]["n"] + one_cut["half2"]["n"], 40)


if __name__ == "__main__":
    unittest.main()
