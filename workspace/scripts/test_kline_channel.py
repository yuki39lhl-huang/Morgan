#!/usr/bin/env python3
"""行情通道：只收已收盘 K 线，断线重连，封禁期不再打剩余 REST。"""
import asyncio
import unittest

import rest_backoff
from rest_backoff import RateGate


HOUR_MS = 3_600_000


class Clock:
    def __init__(self):
        self.t = 1_794_000_000.0

    def __call__(self):
        return self.t


def rest_bar(open_ms, close_px, close_ms=None):
    if close_ms is None:
        close_ms = open_ms + HOUR_MS - 1
    px = f"{close_px:.4f}"
    return [open_ms, px, px, px, px, "1", close_ms, "1", 1, "0", "0", "0"]


def declining_closed(n=40, start_open=1_700_000_000_000):
    rows = []
    for i in range(n):
        rows.append(rest_bar(start_open + i * HOUR_MS, 100.0 - i))
    return rows


class TestClosedKlines(unittest.TestCase):
    def test_open_candle_is_not_cached(self):
        import data_layer

        stream = data_layer.PriceStream(["SOL"])
        open_k = {
            "t": 1_700_000_000_000,
            "T": 1_700_003_599_999,
            "s": "SOLUSDT",
            "o": "80",
            "c": "81",
            "h": "82",
            "l": "79",
            "x": False,
        }
        self.assertIsNone(data_layer.rest_kline_from_ws(open_k))
        self.assertFalse(stream.ingest_closed_kline(open_k))
        self.assertEqual(stream.closed_klines("SOL"), [])

        closed = dict(open_k)
        closed["x"] = True
        self.assertTrue(stream.ingest_closed_kline(closed))
        cached = stream.closed_klines("SOL")
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0][0], open_k["t"])
        self.assertEqual(float(cached[0][4]), 81.0)
        self.assertEqual(cached[0][6], open_k["T"])

    def test_snapshot_plus_increment_matches_rest_closed(self):
        import data_layer
        from rsi_fade import closed_pairs, plan_actions

        closed = declining_closed()
        last_open = closed[-1][0]
        forming_open = last_open + HOUR_MS
        forming = rest_bar(forming_open, 1000.0, close_ms=forming_open + HOUR_MS - 1)
        now_ms = closed[-1][6] + 1
        payload = closed + [forming]
        self.assertGreater(forming[6], now_ms)

        stream = data_layer.PriceStream(["SOL"])
        seen = []

        class Ok:
            status_code = 200
            headers = {}
            text = ""

            def json(self):
                return payload

        def send(url, **kwargs):
            seen.append(kwargs.get("params"))
            return Ok()

        stream.http.get = send
        status = stream.snapshot_symbol("SOL", now_ms=now_ms)
        self.assertEqual(status, "ok")
        self.assertEqual(seen[0]["interval"], "1h")
        self.assertEqual(seen[0]["limit"], 120)
        self.assertNotIn(forming_open, [row[0] for row in stream.closed_klines("SOL")])

        def actions(rows):
            pairs = closed_pairs(rows, now_ms)
            return plan_actions({"SOL": pairs}, [], {}, ["SOL"], 4, {"SOL": 1})

        self.assertEqual(closed_pairs(stream.closed_klines("SOL"), now_ms), closed_pairs(closed, now_ms))
        got = actions(stream.closed_klines("SOL"))
        expect = actions(closed)
        self.assertTrue(expect)
        self.assertEqual(
            [(a["op"], a["side"], a["open_ts"]) for a in got],
            [(a["op"], a["side"], a["open_ts"]) for a in expect],
        )
        self.assertAlmostEqual(got[0]["rsi"], expect[0]["rsi"])

        final_close = forming[4]
        stream.ingest_closed_kline({
            "t": forming_open,
            "T": forming[6],
            "s": "SOLUSDT",
            "o": final_close,
            "c": final_close,
            "h": final_close,
            "l": final_close,
            "x": True,
        })
        later = forming[6] + 1
        full = closed + [forming]
        self.assertEqual(
            closed_pairs(stream.closed_klines("SOL"), later),
            closed_pairs(full, later),
        )
        got2 = plan_actions(
            {"SOL": closed_pairs(stream.closed_klines("SOL"), later)},
            [], {}, ["SOL"], 4, {"SOL": 1},
        )
        expect2 = plan_actions(
            {"SOL": closed_pairs(full, later)},
            [], {}, ["SOL"], 4, {"SOL": 1},
        )
        self.assertEqual(
            [(a["op"], a.get("side"), a["open_ts"], round(a["rsi"], 6)) for a in got2],
            [(a["op"], a.get("side"), a["open_ts"], round(a["rsi"], 6)) for a in expect2],
        )


class TestGateStillStopsRest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = RateGate(clock=self.clock, uniform=lambda: 1.0)
        self.old = rest_backoff.GATE
        rest_backoff.GATE = self.gate

    def tearDown(self):
        rest_backoff.GATE = self.old

    def test_snapshot_ban_does_not_call_the_rest(self):
        import data_layer

        calls = {"n": 0}
        ban_ms = int((self.clock.t + 120) * 1000)

        class Banned:
            status_code = 418
            headers = {}
            text = f'{{"code":-1003,"msg":"IP banned until {ban_ms}"}}'

            def json(self):
                return {"code": -1003, "msg": f"Way too many requests; IP banned until {ban_ms}."}

        stream = data_layer.PriceStream(["SOL", "BNB"])

        def send(url, **kwargs):
            calls["n"] += 1
            return Banned()

        stream.http.get = send
        self.assertEqual(stream.snapshot_symbol("SOL"), "banned")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(stream.snapshot_symbol("BNB"), "banned")
        self.assertEqual(calls["n"], 1)

    def test_stale_price_is_not_used_while_banned(self):
        import crypto_signal_monitor as mon

        class Stream:
            def fresh_price(self, symbol):
                return None

        calls = {"n": 0}

        class Session:
            def get(self, *args, **kwargs):
                calls["n"] += 1
                raise AssertionError("banned path must not hit REST")

        self.gate.banned_until = self.clock.t + 600
        px = mon._trial_live_price(Stream(), Session(), "SOL")
        self.assertIsNone(px)
        self.assertEqual(calls["n"], 0)

        class Fresh:
            def fresh_price(self, symbol):
                return 71.5

        self.gate.banned_until = self.clock.t + 600
        self.assertEqual(mon._trial_live_price(Fresh(), Session(), "SOL"), 71.5)
        self.assertEqual(calls["n"], 0)


class TestReconnect(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = RateGate(clock=self.clock, uniform=lambda: 1.0)
        self.old = rest_backoff.GATE
        rest_backoff.GATE = self.gate

    def tearDown(self):
        rest_backoff.GATE = self.old

    async def _run_until_second_connect(self, rest_calls, banned=False):
        import data_layer

        ps = data_layer.PriceStream(["SOL"], proxy=None)
        connects = {"n": 0}
        sleeps = []

        class Err:
            type = data_layer.aiohttp.WSMsgType.ERROR

        class WS:
            def __init__(self, n):
                self.n = n

            def exception(self):
                return RuntimeError("ws down")

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.n == 1:
                    self.n = 0
                    return Err()
                ps._running = False
                raise StopAsyncIteration

        class CM:
            def __init__(self, n):
                self.ws = WS(n)

            async def __aenter__(self):
                return self.ws

            async def __aexit__(self, *args):
                return False

        class Session:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def ws_connect(self, *args, **kwargs):
                connects["n"] += 1
                return CM(connects["n"])

        async def noop_snapshot():
            return None

        async def fast_sleep(sec):
            sleeps.append(sec)
            if sec >= 30 or len(sleeps) > 3:
                ps._running = False
                raise AssertionError(f"REST 兜底仍在循环：{sleeps}")

        class Resp:
            status_code = 200
            headers = {}

            def json(self):
                return {"price": "70", "priceChangePercent": "0", "volume": "1"}

        def public_get(session, url, path, **kwargs):
            rest_calls.append(path)
            return Resp()

        old_session = data_layer.aiohttp.ClientSession
        old_sleep = data_layer.asyncio.sleep
        old_public = rest_backoff.fapi_public_get
        data_layer.aiohttp.ClientSession = Session
        data_layer.asyncio.sleep = fast_sleep
        rest_backoff.fapi_public_get = public_get
        ps.snapshot_closed_klines = noop_snapshot
        if banned:
            self.gate.banned_until = self.clock.t + 3600
        try:
            await ps.start()
        finally:
            data_layer.aiohttp.ClientSession = old_session
            data_layer.asyncio.sleep = old_sleep
            rest_backoff.fapi_public_get = old_public
            ps._running = False
        return connects["n"], sleeps

    async def test_ws_error_reconnects_instead_of_rest_loop(self):
        calls = []
        connects, sleeps = await self._run_until_second_connect(calls)
        self.assertGreaterEqual(connects, 2)
        self.assertEqual(calls, ["/fapi/v1/ticker/price", "/fapi/v1/ticker/24hr"])
        self.assertNotIn(30, sleeps)

    async def test_banned_fallback_returns_without_rest(self):
        calls = []
        connects, sleeps = await self._run_until_second_connect(calls, banned=True)
        self.assertGreaterEqual(connects, 2)
        self.assertEqual(calls, [])
        self.assertTrue(all(sec < 30 for sec in sleeps))


if __name__ == "__main__":
    unittest.main()
