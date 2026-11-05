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


if __name__ == "__main__":
    unittest.main()
