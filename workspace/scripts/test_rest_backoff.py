#!/usr/bin/env python3
"""限频闸：-1003 封禁期内不得再打 HTTP。"""
import unittest

import rest_backoff
from rest_backoff import RateGate


class Clock:
    def __init__(self):
        self.t = 1_793_860_000.0

    def __call__(self):
        return self.t


class TestRateGate(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = RateGate(clock=self.clock, uniform=lambda: 1.0)

    def test_1003_stops_further_calls_until_ban_ends(self):
        ban_ms = int((self.clock.t + 120) * 1000)
        body = {
            "code": -1003,
            "msg": f"Way too many requests; IP(1.2.3.4) banned until {ban_ms}.",
        }
        self.gate.after("GET", "/fapi/v2/positionRisk", {}, body, {}, 200)
        action, cached = self.gate.before("GET", "/fapi/v1/openOrders", {})
        self.assertEqual(action, "stop")
        self.assertTrue(cached.get("rate_limited"))
        self.clock.t += 119
        action, _ = self.gate.before("GET", "/fapi/v2/positionRisk", {})
        self.assertEqual(action, "stop")
        self.clock.t += 2
        action, _ = self.gate.before("GET", "/fapi/v2/positionRisk", {})
        self.assertEqual(action, "send")

    def test_retry_after_header_is_honored(self):
        body = {"code": -1003, "msg": "Too many requests"}
        self.gate.after("GET", "/fapi/v1/openOrders", {}, body, {"Retry-After": "90"}, 429)
        self.clock.t += 89
        self.assertEqual(self.gate.before("GET", "/fapi/v1/openOrders", {})[0], "stop")
        self.clock.t += 2
        self.assertEqual(self.gate.before("GET", "/fapi/v1/openOrders", {})[0], "send")

    def test_success_is_cached_then_refreshed(self):
        payload = [{"symbol": "XRPUSDT", "positionAmt": "0"}]
        self.gate.after("GET", "/fapi/v2/positionRisk", {}, payload, {}, 200)
        self.assertEqual(self.gate.before("GET", "/fapi/v2/positionRisk", {"timestamp": 1})[0], "stop")
        self.clock.t += rest_backoff.CACHE_TTL_SEC + 1
        self.assertEqual(self.gate.before("GET", "/fapi/v2/positionRisk", {"timestamp": 2})[0], "send")

    def test_read_failure_backs_off_with_jitter_step(self):
        self.gate.after("GET", "/fapi/v1/openOrders", {}, {"error": "timeout"}, {}, 0)
        self.assertEqual(self.gate.before("GET", "/fapi/v1/openOrders", {})[0], "stop")
        self.clock.t += 0.9
        self.assertEqual(self.gate.before("GET", "/fapi/v1/openOrders", {})[0], "stop")
        self.clock.t += 0.2
        self.assertEqual(self.gate.before("GET", "/fapi/v1/openOrders", {})[0], "send")


class TestRequestDoesNotHammer(unittest.TestCase):
    def test_simulated_1003_hits_transport_once(self):
        import binance_auto_trade as bat

        clock = Clock()
        gate = RateGate(clock=clock, uniform=lambda: 1.0)
        calls = {"n": 0}
        ban_ms = int((clock.t + 300) * 1000)

        class Resp:
            status_code = 200
            headers = {"Retry-After": "300"}

            def json(self):
                return {
                    "code": -1003,
                    "msg": f"Way too many requests; banned until {ban_ms}.",
                }

        def send(method, url, headers):
            calls["n"] += 1
            return Resp()

        old_send = bat._http_send
        old_gate = rest_backoff.GATE
        bat._http_send = send
        rest_backoff.GATE = gate
        try:
            first = bat.request("GET", "/fapi/v2/positionRisk")
            self.assertEqual(first.get("code"), -1003)
            for _ in range(20):
                again = bat.request("GET", "/fapi/v1/openOrders")
                self.assertTrue(again.get("rate_limited"))
            self.assertEqual(calls["n"], 1)
            clock.t += 301
            calls["n"] = 0

            class Ok:
                status_code = 200
                headers = {}

                def json(self):
                    return []

            bat._http_send = lambda method, url, headers: Ok()
            # 上面的 lambda 没计入 calls，换一个会计数的。
            def send_ok(method, url, headers):
                calls["n"] += 1
                return Ok()

            bat._http_send = send_ok
            got = bat.request("GET", "/fapi/v2/positionRisk")
            self.assertEqual(got, [])
            self.assertEqual(calls["n"], 1)
            cached = bat.request("GET", "/fapi/v2/positionRisk")
            self.assertEqual(cached, [])
            self.assertEqual(calls["n"], 1)
        finally:
            bat._http_send = old_send
            rest_backoff.GATE = old_gate


class TestPublicAndClose(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = RateGate(clock=self.clock, uniform=lambda: 1.0)
        self.old_gate = rest_backoff.GATE
        rest_backoff.GATE = self.gate

    def tearDown(self):
        rest_backoff.GATE = self.old_gate
        saved = getattr(self, "_saved", None)
        if not saved:
            return
        import execution_layer as el
        for name, old in saved.items():
            setattr(el, name, old)
        if getattr(self, "_old_sleep", None) is not None:
            el.time.sleep = self._old_sleep

    def test_418_stops_kline_and_ticker_until_ban_ends(self):
        import data_layer

        calls = {"kline": 0, "ticker": 0}
        ban_ms = int((self.clock.t + 180) * 1000)

        class Banned:
            status_code = 418
            headers = {}
            text = f'{{"code":-1003,"msg":"IP banned until {ban_ms}"}}'

            def json(self):
                return {"code": -1003, "msg": f"Way too many requests; IP banned until {ban_ms}."}

        eng = data_layer.IndicatorEngine()
        eng.session.get = lambda *a, **k: calls.__setitem__("kline", calls["kline"] + 1) or Banned()
        got = eng._get_klines("DOT", "1h", 60)
        self.assertEqual(got, [])
        self.assertEqual(calls["kline"], 1)
        again = eng._get_klines("SOL", "1h", 60)
        self.assertEqual(again, [])
        self.assertEqual(calls["kline"], 1)

        def ticker_send():
            calls["ticker"] += 1
            return Banned()

        skipped = rest_backoff.public_get(ticker_send, "/fapi/v1/ticker/price")
        self.assertTrue(getattr(skipped, "skipped", False))
        self.assertEqual(calls["ticker"], 0)
        self.clock.t = self.gate.banned_until + 1
        resumed = rest_backoff.public_get(ticker_send, "/fapi/v1/ticker/price")
        self.assertEqual(calls["ticker"], 1)
        self.assertEqual(resumed.status_code, 418)

    def test_unparsed_418_uses_conservative_wait(self):
        class Bare:
            status_code = 418
            headers = {}
            text = "teapot"

            def json(self):
                raise ValueError("not json")

        rest_backoff.public_get(lambda: Bare(), "/fapi/v1/exchangeInfo")
        self.assertGreaterEqual(self.gate.banned_until, self.clock.t + rest_backoff.UNPARSED_BAN_SEC)
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            return Bare()

        rest_backoff.public_get(send, "/fapi/v1/klines")
        self.assertEqual(calls["n"], 0)

    def test_close_does_not_send_during_ban_and_stays_reduce_only(self):
        import execution_layer as el

        self._saved = {
            name: getattr(el, name)
            for name in (
                "format_quantity", "cancel_algo_orders", "write_alert",
                "push_signal_alert", "record_close", "_exchange_position", "place_order",
            )
        }
        self._old_sleep = el.time.sleep
        calls = []
        el.time.sleep = lambda *_a, **_k: None
        el.format_quantity = lambda _symbol, quantity, *_a, **_k: float(quantity)
        el.cancel_algo_orders = lambda *_a, **_k: None
        el.write_alert = lambda *_a, **_k: None
        el.push_signal_alert = lambda *_a, **_k: None
        el.record_close = lambda *_a, **_k: None
        el._exchange_position = lambda _symbol: (True, "LONG", 2.34)

        import rsi_flat_guard
        rsi_flat_guard.flat_claim_blocks = lambda _pos: (False, "")

        def place_order(**kwargs):
            calls.append(kwargs["reduce_only"])
            return {"success": False, "order": {"code": -1003, "msg": "banned"}, "rate_limited": True}

        el.place_order = place_order
        pos = {
            "symbol": "SOL",
            "type": "LONG",
            "entry_price": 85.76,
            "qty": 2.34,
            "amount": 2.34,
            "strategy": "rsi_fade",
            "regime": "rsi",
        }
        self.gate.banned_until = self.clock.t + 600
        pnl, closed = el.close_position(pos, "rsi_time", 1.0, 85.0)
        self.assertFalse(closed)
        self.assertEqual(pnl, 0.0)
        self.assertEqual(calls, [])
        self.clock.t = self.gate.banned_until + 1
        pnl, closed = el.close_position(dict(pos), "rsi_time", 1.0, 85.0)
        self.assertFalse(closed)
        self.assertEqual(calls, [True])

    def test_close_1003_response_does_not_retry(self):
        import execution_layer as el

        self._saved = {
            name: getattr(el, name)
            for name in (
                "format_quantity", "cancel_algo_orders", "write_alert",
                "push_signal_alert", "record_close", "_exchange_position", "place_order",
            )
        }
        self._old_sleep = el.time.sleep
        calls = []
        el.time.sleep = lambda *_a, **_k: None
        el.format_quantity = lambda _symbol, quantity, *_a, **_k: float(quantity)
        el.cancel_algo_orders = lambda *_a, **_k: None
        el.write_alert = lambda *_a, **_k: None
        el.push_signal_alert = lambda *_a, **_k: None
        el.record_close = lambda *_a, **_k: None
        el._exchange_position = lambda _symbol: (True, "SHORT", 1.0)

        def place_order(**kwargs):
            calls.append(kwargs["reduce_only"])
            return {"success": False, "order": {"code": -1003, "msg": "banned until 1"}}

        el.place_order = place_order
        pos = {
            "symbol": "SOL",
            "type": "SHORT",
            "entry_price": 90.0,
            "qty": 1.0,
            "amount": 1.0,
            "strategy": "rsi_fade",
            "regime": "rsi",
        }
        pnl, closed = el.close_position(pos, "rsi_mid", 1.0, 88.0)
        self.assertFalse(closed)
        self.assertEqual(pnl, 0.0)
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
