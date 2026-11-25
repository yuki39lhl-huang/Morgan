"""Exercise news execution without contacting Binance or the trading ledger."""

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import news_entry_shadow
import news_testnet
import trade_db
from trading_dashboard import read_closed_positions


class NewsExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(trade_db, "DB_PATH", str(Path(self.temp) / "mock-ledger.db")))
        self.clock = self.enterContext(patch.object(news_testnet, "now_ms", return_value=1_790_000_000_000))
        self.opened = self.clock.return_value
        trade_db.init_db()
        with closing(trade_db.connect()) as db:
            news_entry_shadow.init_schema(db)
        self.event = {"event_key": "fixture", "stage": "confirmed", "symbol": "SKHYNIXUSDT"}
        self.catalog = {"symbols": [{
            "symbol": self.event["symbol"], "status": "TRADING", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        }]}
        self.positions = []
        self.circuit = Mock()
        self.circuit.is_trading_allowed.return_value = (True, "")
        self.config = {"leverage": 10, "max_positions": 4}
        self.request = Mock()
        self.entry = {"action": "long", "notional_usdt": 200, "reference": 100}
        self.save = Mock()

    def row(self):
        with closing(trade_db.connect(readonly=True)) as db:
            return dict(db.execute("SELECT * FROM news_testnet_orders").fetchone())

    def enqueue(self):
        return news_testnet.enqueue(trade_db, self.event, self.entry, self.opened + 20_000, self.opened)

    def tick(self):
        return news_testnet.service(trade_db, self.positions, self.config,
                                    self.circuit, self.request, self.catalog, self.save)

    def position(self, amount):
        return [{"symbol": self.event["symbol"], "positionSide": "BOTH", "positionAmt": str(amount)}]

    def order(self, event, order_id, *, side="BUY", qty="2", status="FILLED"):
        return {"symbol": self.event["symbol"], "positionSide": "BOTH", "side": side,
                "clientOrderId": event, "orderId": order_id, "updateTime": self.clock.return_value,
                "status": status, "executedQty": qty, "avgPrice": "100" if side == "BUY" else "103"}

    def reduce_fills(self, order_id, trade_id, quantity, pnl):
        return [{"symbol": self.event["symbol"], "side": "SELL", "positionSide": "BOTH", "orderId": order_id,
                 "id": trade_id, "qty": quantity, "price": "103", "realizedPnl": pnl}]

    def opening_orders(self):
        return [x for x in self.request.call_args_list
                if x.args[:2] == ("POST", "/fapi/v1/order") and x.args[2]["side"] == "BUY"]

    def test_timeout_queries_by_client_id_without_resubmitting(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        client_id = self.row()["client_id"]
        self.request.side_effect = [[], {"code": -4046}, {"leverage": 10},
                                    {"error": "connection reset"}, self.order(client_id, 1)]
        self.assertTrue(self.tick())
        self.assertEqual(self.positions, [])
        self.assertFalse(self.tick())
        self.assertEqual(self.positions[0]["regime"], "news_trend")
        self.assertEqual(self.row()["order_id"], 1)
        self.assertEqual(len(self.opening_orders()), 1)
        self.assertFalse(self.opening_orders()[0].kwargs["retry_order"])
        self.assertEqual(self.request.call_args.args[2]["origClientOrderId"], client_id)

    def test_rejected_order_releases_slot_after_flat_check(self):
        self.enqueue()
        self.request.side_effect = [[], {"code": 200}, {"leverage": 10},
                                    {"code": -2019}, {"code": -2013}, []]
        self.assertTrue(self.tick())
        self.clock.return_value += 60_001
        self.assertFalse(self.tick())
        self.assertEqual(self.row()["status"], "rejected")
        self.assertEqual(len(self.opening_orders()), 1)
        self.assertEqual(self.positions, [])

    def test_expired_entry_never_submits(self):
        self.enqueue()
        self.clock.return_value += 20_000
        self.assertFalse(self.tick())
        self.assertEqual(self.row()["status"], "expired")
        self.request.assert_not_called()

    def test_shared_rsi_capacity_blocks_news_order(self):
        self.enqueue()
        self.positions.extend({"symbol": name, "regime": "rsi"} for name in ("BTC", "ETH", "BNB", "SOL"))
        self.request.return_value = []
        self.assertFalse(self.tick())
        self.assertEqual(self.row()["status"], "rejected")
        self.assertEqual(self.opening_orders(), [])
        self.assertEqual(len(self.positions), 4)

    def test_existing_exchange_position_blocks_duplicate(self):
        self.enqueue()
        self.request.return_value = self.position(2)
        self.assertFalse(self.tick())
        self.assertEqual(self.row()["status"], "rejected")
        self.assertEqual(self.opening_orders(), [])

    def test_partial_fill_and_timed_reduce_only_close_reconcile_once(self):
        self.enqueue()
        client_id = self.row()["client_id"]
        self.request.side_effect = [[], {"code": 200}, {"leverage": 10},
                                    self.order(client_id, 1, qty="1.5", status="EXPIRED")]
        self.assertFalse(self.tick())
        self.assertEqual(self.row()["quantity"], 1.5)
        self.assertEqual(self.positions[0]["qty"], 1.5)
        self.request.reset_mock()
        self.clock.return_value += news_testnet.HOLD_SECONDS * 1000 - 1
        self.tick()
        self.request.assert_not_called()
        self.clock.return_value += 1
        first = client_id + "_c1"
        second = client_id + "_c2"
        self.request.side_effect = [
            self.position(1.5), self.order(first, 2, side="SELL", qty="1", status="EXPIRED"),
            self.reduce_fills(2, 101, "1", "3"), self.position(.5),
            self.position(.5), self.order(second, 3, side="SELL", qty=".5"),
            self.reduce_fills(3, 102, ".5", "1.5"), [],
        ]
        self.tick()
        self.assertEqual(self.row()["status"], "open")
        with closing(trade_db.connect(readonly=True)) as db:
            self.assertEqual(read_closed_positions(db), [])
        self.tick()
        self.assertEqual(self.row()["status"], "closed")
        self.assertEqual(self.positions, [])
        placed = [x.args[2] for x in self.request.call_args_list if x.args[:2] == ("POST", "/fapi/v1/order")]
        self.assertEqual([x["reduceOnly"] for x in placed], ["true", "true"])
        self.assertEqual([float(x["quantity"]) for x in placed], [1.5, .5])
        self.assertEqual([x.args[0] for x in self.circuit.record_trade.call_args_list], [3.0, 1.5])
        with closing(trade_db.connect(readonly=True)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*),SUM(pnl_usdt) FROM trade_closes").fetchone()[:], (2, 4.5))
            closed, = read_closed_positions(db)
            self.assertEqual(closed["pnl_usdt"], 4.5)


if __name__ == "__main__":
    unittest.main()
