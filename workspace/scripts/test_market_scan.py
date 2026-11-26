"""Exercise scan decisions, equity sessions and shared testnet execution."""
from contextlib import closing
from decimal import Decimal
import unittest
from unittest.mock import Mock

import market_scan
import news_testnet
import rsi_fade
import test_news_testnet
import trade_db


class ScanSignals(unittest.TestCase):
    def test_only_closed_continuous_and_abnormally_active_bars(self):
        bars = [{"time": i * market_scan.BAR_MS, "high": 100.1, "close": 100,
                 "low": 99.9, "quote": 1000} for i in range(15)]
        bars[-1].update(high=102, close=102, quote=3000)
        result = market_scan.scan_setup(bars)
        self.assertEqual((result["setup"], result["direction"]), ("breakout", "long"))
        bars[-1]["quote"] = 1000
        self.assertIsNone(market_scan.scan_setup(bars))
        bars[-1].update(quote=3000, time=14 * market_scan.BAR_MS + 1)
        self.assertIsNone(market_scan.scan_setup(bars))

    def test_tradfi_calendar_and_dividend_gate(self):
        info = {"symbol": "SKHYNIXUSDT", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "HK_EQUITY"}
        stamp = 13_000
        close = stamp + 7_200_000
        schedule = {"KR_EQUITY": {"sessions": [{"type": "REGULAR", "startTime": 0, "endTime": close}]}}
        self.assertIsNone(market_scan.eligible_tradfi(info, schedule, stamp, stamp + 5000, {}))
        cfg = {"market_scan_symbol_blackouts": {"SKHYNIXUSDT": close}}
        self.assertEqual(market_scan.eligible_tradfi(info, schedule, stamp, stamp + 5000, cfg),
                         "corporate_action_blackout")
        self.assertEqual(market_scan.eligible_tradfi(info, None, stamp, stamp + 5000, {}),
                         "schedule_unavailable")

    def test_rsi_accepts_five_shared_slots_and_rejects_six(self):
        config = dict(enabled=True, period=14, high=70, low=30, mid=50, max_hold_hours=24,
                      notional_usdt=200, max_concurrent=5)
        self.assertTrue(rsi_fade.rules_match(config))
        self.assertFalse(rsi_fade.rules_match(dict(config, max_concurrent=6)))


class SharedExecution(unittest.TestCase):
    def setUp(self):
        fx = test_news_testnet.NewsExecutionTest()
        self.addCleanup(fx.doCleanups)
        fx.setUp()
        self.fx = fx
        fx.catalog["symbols"][0]["filters"].extend([
            {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "100"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "20000"},
        ])
        fx.config["max_positions"] = 5
        with closing(trade_db.connect()) as db:
            market_scan.init(db)
        self.enabled = {news_testnet.SCAN_STRATEGY, news_testnet.STRATEGY}

    def queue(self):
        fx = self.fx
        market_scan.signal_claim(trade_db, fx.event["event_key"], fx.event["symbol"], fx.opened, fx.opened,
                                 {"setup": "rebound", "close": 100})
        return news_testnet.enqueue(trade_db, fx.event, fx.entry, fx.opened + 20_000, fx.opened,
                                    strategy=news_testnet.SCAN_STRATEGY)

    def service(self):
        fx = self.fx
        return news_testnet.service(trade_db, fx.positions, fx.config, fx.circuit, fx.request, fx.catalog,
                                    fx.save, enabled=self.enabled)

    def test_fifth_slot_ioc_stop_and_booked_exit(self):
        fx = self.fx
        fx.positions.extend({"symbol": s, "regime": "rsi"} for s in ("BTC", "SOL", "DOT", "ADA"))
        self.assertTrue(self.queue())
        self.assertFalse(self.queue())
        client = fx.row()["client_id"]
        fx.request.side_effect = [[], {"code": -4046}, {"leverage": 10},
                                  fx.order(client, 101, qty="1.99", status="EXPIRED")]
        self.assertTrue(self.service())
        self.assertEqual(len(fx.positions), 5)
        self.assertEqual(fx.positions[-1]["regime"], "market_scan")
        self.assertEqual(fx.positions[-1]["qty"], 1.99)
        order = fx.opening_orders()[0].args[2]
        self.assertEqual((order["type"], order["timeInForce"], order["price"]), ("LIMIT", "IOC", "100.15"))
        self.assertLessEqual(Decimal(order["price"]) * Decimal(order["quantity"]), Decimal("200"))
        fx.request.side_effect = [{"clientAlgoId": client + "_s", "symbol": fx.event["symbol"], "algoStatus": "NEW"}]
        self.assertFalse(self.service())
        self.assertEqual(fx.row()["protected"], 1)
        self.assertEqual(fx.request.call_args.args[2]["reduceOnly"], "true")
        fx.clock.return_value += 3_600_001
        fx.request.side_effect = [
            {"clientAlgoId": client + "_s", "symbol": fx.event["symbol"], "algoStatus": "NEW"},
            {"code": 200}, fx.position("1.99"),
            fx.order(client + "_c1", 102, side="SELL", qty="1.99"),
            fx.reduce_fills(102, 1, "1.99", "5.97"), [],
        ]
        self.service()
        self.assertEqual(fx.row()["status"], "closed")
        self.assertEqual(len(fx.positions), 4)

    def test_sixth_slot_rejected_without_sending_an_order(self):
        fx = self.fx
        fx.positions.extend({"symbol": s, "regime": "rsi"} for s in ("BTC", "SOL", "DOT", "ADA", "ETH"))
        self.assertTrue(self.queue())
        fx.request.return_value = []
        self.service()
        self.assertEqual(fx.row()["status"], "rejected")
        self.assertEqual(fx.opening_orders(), [])
