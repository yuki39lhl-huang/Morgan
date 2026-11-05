#!/usr/bin/env python3
"""心跳超时兜底：模拟 monitor 被杀，只能平仓一次。不碰交易所。"""
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import rsi_fade as rf
from rsi_flat_guard import (
    HEARTBEAT_TIMEOUT_SEC,
    FlatBook,
    close_params,
    flat_claim_blocks,
    flatten_once,
    heartbeat_age_sec,
    should_emergency_flat,
)


class FakeExchange:
    def __init__(self, rows, fail=0):
        self.rows = rows
        self.fail = fail
        self.calls = []

    def positions(self):
        return self.rows

    def reduce_only(self, params):
        self.calls.append(params)
        if params.get("reduceOnly") is not True:
            return {"ok": False, "reduce_only": False, "error": "opened"}
        if self.fail > 0:
            self.fail -= 1
            return {"ok": False, "reduce_only": True, "error": "timeout"}
        return {"ok": True, "reduce_only": True, "order_id": 1000 + len(self.calls)}


def _book(positions):
    return [
        {
            "symbol": "LINK",
            "type": "SHORT",
            "strategy": "rsi_fade",
            "regime": "rsi",
            "trade_id": "link-1",
            "entry_price": 11.543,
            "amount": 17.33,
        },
        {
            "symbol": "XRP",
            "type": "SHORT",
            "strategy": "rsi_fade",
            "regime": "rsi",
            "trade_id": "xrp-1",
            "entry_price": 1.3152,
            "amount": 152.1,
        },
        {
            "symbol": "BTC",
            "type": "LONG",
            "regime": "trending",
            "trade_id": "btc-1",
            "entry_price": 80000,
            "amount": 0.01,
        },
    ]


class TestFlatOnce(unittest.TestCase):
    def test_killed_monitor_flattens_rsi_once(self):
        now = datetime(2026, 11, 5, 13, 0, 0)
        beat = {"last_update": (now - timedelta(seconds=HEARTBEAT_TIMEOUT_SEC + 1)).isoformat()}
        age = heartbeat_age_sec(beat, now)
        self.assertTrue(should_emergency_flat(False, age))
        rows = [
            {"symbol": "LINK", "type": "SHORT", "amount": -17.33},
            {"symbol": "XRP", "type": "SHORT", "amount": -152.1},
            {"symbol": "BTC", "type": "LONG", "amount": 0.01},
        ]
        ex = FakeExchange(rows)
        book = FlatBook(Path("/tmp/rsi_flat_guard_test.json"))
        if book.path.exists():
            book.path.unlink()
        logs = []
        sent = flatten_once(_book(None), ex, book, now.timestamp(), logs.append, None)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(ex.calls), 2)
        for call in ex.calls:
            self.assertTrue(call["reduceOnly"])
            self.assertEqual(call["side"], "BUY")
            self.assertTrue(call["newClientOrderId"].startswith("flat"))
        again = flatten_once(_book(None), ex, book, now.timestamp() + 10, logs.append, None)
        self.assertEqual(again, [])
        self.assertEqual(len(ex.calls), 2)
        self.assertTrue(flat_claim_blocks(_book(None)[0], book.path)[0])
        self.assertIn("BTC", {row["symbol"] for row in rows})
        self.assertNotIn("BTCUSDT", {c["symbol"] for c in ex.calls})

    def test_fresh_heartbeat_does_not_flatten_after_kill(self):
        self.assertFalse(should_emergency_flat(False, 5))
        self.assertFalse(should_emergency_flat(True, 10))
        self.assertTrue(should_emergency_flat(True, HEARTBEAT_TIMEOUT_SEC))

    def test_exchange_failure_retries_once_then_stops(self):
        now = 1_793_860_000.0
        ex = FakeExchange([{"symbol": "LINK", "type": "SHORT", "amount": -17.33}], fail=1)
        path = Path("/tmp/rsi_flat_guard_retry.json")
        if path.exists():
            path.unlink()
        book = FlatBook(path)
        logs = []
        pos = [_book(None)[0]]
        first = flatten_once(pos, ex, book, now, logs.append, None)
        self.assertEqual(first, [])
        self.assertEqual(len(ex.calls), 1)
        self.assertTrue(any("后再试" in line for line in logs))
        flatten_once(pos, ex, book, now + 0.5, logs.append, None)
        self.assertEqual(len(ex.calls), 1)
        flatten_once(pos, ex, book, now + 2, logs.append, None)
        self.assertEqual(len(ex.calls), 2)
        self.assertTrue(ex.calls[-1]["reduceOnly"])
        flatten_once(pos, ex, book, now + 100, logs.append, None)
        self.assertEqual(len(ex.calls), 2)

    def test_close_params_cannot_open(self):
        pos = _book(None)[0]
        params = close_params(pos, 17.33)
        again = close_params(pos, 17.33)
        self.assertTrue(params["reduceOnly"])
        self.assertEqual(params["side"], "BUY")
        self.assertEqual(params["newClientOrderId"], again["newClientOrderId"])
        long_pos = dict(pos, type="LONG", trade_id="link-long")
        self.assertEqual(close_params(long_pos, 1)["side"], "SELL")

    def test_rsi_exit_rules_and_config_untouched(self):
        self.assertEqual(rf.RSI_N, 14)
        self.assertEqual(rf.RSI_HIGH, 70.0)
        self.assertEqual(rf.RSI_LOW, 30.0)
        self.assertEqual(rf.RSI_MID, 50.0)
        self.assertEqual(rf.RSI_HOLD_H, 24)
        self.assertEqual(rf.TRIAL_NOTIONAL, 200.0)
        self.assertEqual(rf.TRIAL_MAX_CONCURRENT, 2)
        root = Path(__file__).resolve().parents[2]
        diff = subprocess.check_output(
            ["git", "diff", "eabfe51", "--", "workspace/scripts/config.json"],
            cwd=root,
        )
        self.assertEqual(diff, b"")
        watchdog = (Path(__file__).resolve().parent / "crypto_monitor_watchdog.py").read_text()
        self.assertIn("production_tick", watchdog)


if __name__ == "__main__":
    unittest.main()
