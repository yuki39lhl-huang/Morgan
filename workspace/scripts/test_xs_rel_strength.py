#!/usr/bin/env python3
"""候选 F 的截面排序。不下载行情。实盘上限必须仍是全 0。"""
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


def bars_from_closes(start_ms, closes_by_sym):
    data = {}
    for sym, closes in closes_by_sym.items():
        rows = []
        for i, c in enumerate(closes):
            for k in range(4):
                t = start_ms + (i * 4 + k) * 3_600_000
                px = float(c)
                rows.append((t, px, px, px, 1.0))
        data[sym] = np.array(rows, dtype=np.float64)
    return data


def _flat(n, **over):
    closes = {s: [100.0] * n for s in POOL}
    for sym, seq in over.items():
        closes[sym] = list(seq)
    return bars_from_closes(START, closes)


class TestXsRelStrength(unittest.TestCase):
    def test_fee_and_window_constants(self):
        self.assertAlmostEqual(hb.NOTIONAL * hb.FEE_RT, 0.2)
        self.assertEqual(hb.SLIPPAGE_RT, 0.0)
        self.assertEqual(hb.SAMPLE_IN_END, "2026-10-04")
        self.assertEqual(hb.XS_BAR_MIN, 240)
        self.assertEqual(hb.XS_LOOKBACK, 6)
        self.assertEqual(hb.ENTRY_XS, "xs_rel_strength")

    def test_live_kill_switch_still_off(self):
        cfg = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
        caps = cfg["max_positions_per_symbol"]
        self.assertEqual(set(caps), {"BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"})
        self.assertTrue(all(v == 0 for v in caps.values()))
        self.assertFalse(cfg["score_threshold"]["allow_short"])

    def test_not_wired_into_live_loop(self):
        root = Path(__file__).resolve().parent
        for name in ("crypto_signal_monitor.py", "binance_auto_trade.py", "strategy_layer.py"):
            self.assertNotIn("xs_rel_strength", (root / name).read_text())

    def test_targets_ignore_symbol_name_on_ties(self):
        self.assertEqual(hb.xs_targets({"A": 1.0, "B": 1.0, "C": 1.0}, {}), (None, None))
        self.assertEqual(hb.xs_targets({"A": 2.0, "B": 2.0, "C": 0.0}, {}), (None, "C"))
        held = {"A": {"side": "LONG"}}
        self.assertEqual(hb.xs_targets({"A": 2.0, "B": 2.0, "C": 0.0}, held), ("A", "C"))
        self.assertEqual(hb.xs_targets({"A": 2.0, "B": 0.0, "C": 0.0}, {}), ("A", None))
        self.assertEqual(hb.xs_targets({"SOL": 0.3, "XRP": -0.2, "BNB": 0.0}, {}), ("SOL", "XRP"))

    def test_hold_while_rank_unchanged_charges_fee_once(self):
        n = 20
        data = _flat(
            n,
            SOL=[100 + i for i in range(n)],
            XRP=[200 - i for i in range(n)],
        )
        out = hb.run_xs(data, POOL)
        trades = out["trades"]
        self.assertEqual(len(trades), 2)
        by = {t["symbol"]: t for t in trades}
        self.assertEqual(set(by), {"SOL", "XRP"})
        self.assertEqual(by["SOL"]["direction"], "LONG")
        self.assertEqual(by["XRP"]["direction"], "SHORT")
        self.assertTrue(all(t["reason"] == "eod" for t in trades))
        self.assertTrue(all(abs(t["fee"] - 0.2) < 1e-9 for t in trades))
        self.assertAlmostEqual(by["SOL"]["entry"], 106.0)
        self.assertAlmostEqual(by["SOL"]["exit"], 119.0)
        self.assertAlmostEqual(by["XRP"]["entry"], 194.0)
        self.assertAlmostEqual(by["XRP"]["exit"], 181.0)
        self.assertGreater(by["SOL"]["gross"], 0)
        self.assertGreater(by["XRP"]["gross"], 0)
        self.assertAlmostEqual(by["SOL"]["pnl"], by["SOL"]["gross"] - 0.2)
        # 6→19 共 13 根 4h，不是每根都平掉再开
        self.assertAlmostEqual((by["SOL"]["exit_ts"] - by["SOL"]["entry_ts"]) / 3_600_000, 52.0)

    def test_rank_change_rebalances_once(self):
        n = 20
        sol, xrp = [], []
        for i in range(n):
            if i <= 12:
                sol.append(100 * (1.01 ** i))
                xrp.append(100 * (0.99 ** i))
            else:
                # 换手之后趋势还在，最后一根不会因为 24h 收益归零而提前平掉
                sol.append(50 * (0.99 ** (i - 12)))
                xrp.append(200 * (1.01 ** (i - 12)))
        data = _flat(n, SOL=sol, XRP=xrp)
        trades = hb.run_xs(data, POOL)["trades"]
        self.assertEqual(len(trades), 4)
        reasons = sorted(t["reason"] for t in trades)
        self.assertEqual(reasons, ["eod", "eod", "rebalance", "rebalance"])
        first = [t for t in trades if t["reason"] == "rebalance"]
        self.assertEqual(sorted((t["symbol"], t["direction"]) for t in first), [("SOL", "LONG"), ("XRP", "SHORT")])
        second = [t for t in trades if t["reason"] == "eod"]
        self.assertEqual(sorted((t["symbol"], t["direction"]) for t in second), [("SOL", "SHORT"), ("XRP", "LONG")])
        self.assertTrue(all(abs(t["fee"] - 0.2) < 1e-9 for t in trades))
        self.assertTrue(all(t["reason"] not in {"BE", "trail", "TP1", "TP", "SL", "timeout"} for t in trades))

    def test_tie_does_not_open_a_new_long_by_name(self):
        # 最后一根的回看仍落在两边同价的底座上，并列不会在收尾时把多头换成 BNB
        n = 12
        sol, bnb, xrp = [], [], []
        for i in range(n):
            sol.append(110.0 if i == 6 else 111.0 if i == 7 else 112.0 if i == 8 else 120.0 if i >= 9 else 100.0)
            bnb.append(120.0 if i >= 9 else 100.0)
            xrp.append(80.0 if i >= 6 else 100.0)
        data = _flat(n, SOL=sol, BNB=bnb, XRP=xrp)
        trades = hb.run_xs(data, POOL)["trades"]
        self.assertEqual(sorted(t["symbol"] for t in trades), ["SOL", "XRP"])
        self.assertTrue(all(t["reason"] == "eod" for t in trades))

    def test_no_lookahead_before_the_jump_bar(self):
        n = 12
        sol = [100.0] * n
        for i in range(10, n):
            sol[i] = 150.0
        data = _flat(n, SOL=sol)
        trades = hb.run_xs(data, POOL)["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["symbol"], "SOL")
        self.assertEqual(t["direction"], "LONG")
        self.assertAlmostEqual(t["entry"], 150.0)
        self.assertAlmostEqual(t["gross"], 0.0)
        self.assertAlmostEqual(t["pnl"], -0.2)

    def test_incomplete_pool_does_not_trade(self):
        data = _flat(20, SOL=[100 + i for i in range(20)], XRP=[200 - i for i in range(20)])
        data["DOT"] = np.zeros((0, 5))
        out = hb.run_xs(data, POOL)
        self.assertEqual(out["trades"], [])

    def test_pass_bar_is_oos_ev_and_count_only(self):
        def row(day, pnl):
            return {"day": day, "pnl": pnl, "gross": pnl + 0.2, "direction": "LONG"}

        def judge(trades):
            with redirect_stdout(io.StringIO()):
                return hb._passes_f(trades)

        self.assertTrue(judge([row("2026-10-04", 1.0) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", -1.0) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", 0.0) for _ in range(30)]))
        self.assertFalse(judge([row("2026-10-04", 1.0) for _ in range(29)]))
        self.assertFalse(judge([row("2026-10-03", 5.0) for _ in range(100)]))


if __name__ == "__main__":
    unittest.main()
