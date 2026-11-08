"""平仓记账：部分成交与 reduceOnly 拒绝。回放 2026-11-05～07 的测试网成交。"""
import unittest

from fill_accounting import (
    book_realized,
    fill_from_trades,
    income_realized,
    order_executed_qty,
    remaining_after_fill,
)

# 测试网 userTrades / income，2026-11-05 00:00 至 11-08 00:00（北京时间）。
# 只留记账需要的字段。XRP 空仓在窗口开始前已有 152.1，LINK 空仓 17.33。
TRADES = [
    {"symbol": "LINKUSDT", "orderId": 1067951074, "side": "BUY", "qty": "17.33", "price": "11.437", "realizedPnl": "2.07960000", "time": 1},
    {"symbol": "XRPUSDT", "orderId": 3536796068, "side": "BUY", "qty": "12.3", "price": "1.3052", "realizedPnl": "0.12177000", "time": 2},
    {"symbol": "XRPUSDT", "orderId": 3536872153, "side": "BUY", "qty": "139.8", "price": "1.2612", "realizedPnl": "7.53522000", "time": 3},
    {"symbol": "SOLUSDT", "orderId": 4241310348, "side": "BUY", "qty": "2.22", "price": "90.2400", "realizedPnl": "0", "time": 4},
    {"symbol": "BNBUSDT", "orderId": 2675684697, "side": "BUY", "qty": "0.29", "price": "689.560", "realizedPnl": "0", "time": 5},
    {"symbol": "SOLUSDT", "orderId": 4241855954, "side": "SELL", "qty": "2.22", "price": "90.8700", "realizedPnl": "1.39860000", "time": 6},
    {"symbol": "XRPUSDT", "orderId": 3537670841, "side": "BUY", "qty": "168.9", "price": "1.1844", "realizedPnl": "0", "time": 7},
    {"symbol": "BNBUSDT", "orderId": 2676186095, "side": "SELL", "qty": "0.29", "price": "696.390", "realizedPnl": "1.98070000", "time": 8},
    {"symbol": "XRPUSDT", "orderId": 3538241267, "side": "SELL", "qty": "168.9", "price": "1.1674", "realizedPnl": "-2.87130000", "time": 9},
]
INCOME = [
    {"income": "2.07960000"},
    {"income": "0.12177000"},
    {"income": "7.53522000"},
    {"income": "1.39860000"},
    {"income": "1.98070000"},
    {"income": "-2.87130000"},
]

XRP_ACK = {
    "orderId": 3536796068,
    "status": "NEW",
    "origQty": "12.3",
    "executedQty": "0.0",
    "cumQty": "0.0",
    "side": "BUY",
    "reduceOnly": True,
    "type": "MARKET",
}
XRP_TRADE = {
    "symbol": "XRPUSDT",
    "orderId": 3536796068,
    "side": "BUY",
    "qty": "12.3",
    "price": "1.3052",
    "realizedPnl": "0.12177000",
}


def replay(trades):
    """按成交回报滚动本地数量。已实现只加 realizedPnl。"""
    book = {
        "XRP": 152.1,
        "LINK": 17.33,
    }
    steps = []
    for t in sorted(trades, key=lambda row: int(row["time"])):
        sym = t["symbol"].replace("USDT", "")
        qty = float(t["qty"])
        realized = float(t["realizedPnl"] or 0)
        if abs(realized) < 1e-12:
            book[sym] = qty
            continue
        remain, done = remaining_after_fill(book[sym], qty)
        book[sym] = remain
        steps.append({"symbol": sym, "filled": qty, "realized": realized, "remain": remain, "done": done})
    return book, steps


class TestReplay(unittest.TestCase):
    def test_window_matches_income(self):
        booked = book_realized(TRADES)
        self.assertAlmostEqual(booked, income_realized(INCOME), places=4)
        self.assertAlmostEqual(booked, 10.2446, places=4)

    def test_xrp_partial_does_not_book_the_requested_qty(self):
        self.assertIsNone(order_executed_qty(XRP_ACK))
        got = fill_from_trades([XRP_TRADE])
        self.assertAlmostEqual(got["filled_qty"], 12.3, places=4)
        self.assertAlmostEqual(got["realized_pnl"], 0.12177, places=5)
        remain, done = remaining_after_fill(152.1, got["filled_qty"])
        self.assertAlmostEqual(remain, 139.8, places=4)
        self.assertFalse(done)
        self.assertNotAlmostEqual(got["realized_pnl"], 1.55, places=2)

    def test_replay_leaves_flat_book(self):
        book, steps = replay(TRADES)
        xrp = steps[1]
        self.assertEqual(xrp["symbol"], "XRP")
        self.assertAlmostEqual(xrp["filled"], 12.3, places=4)
        self.assertAlmostEqual(xrp["remain"], 139.8, places=4)
        self.assertFalse(xrp["done"])
        bnb = next(s for s in steps if s["symbol"] == "BNB")
        self.assertAlmostEqual(bnb["realized"], 1.9807, places=4)
        self.assertNotAlmostEqual(bnb["realized"], 0.74, places=2)
        for sym in ("XRP", "LINK", "SOL", "BNB"):
            self.assertAlmostEqual(book[sym], 0.0, places=6)
        self.assertAlmostEqual(sum(s["realized"] for s in steps), income_realized(INCOME), places=4)


class TestClosePaths(unittest.TestCase):
    def setUp(self):
        import execution_layer as el

        self.el = el
        self.calls = []
        self.recorded = []
        self.amt = 152.1
        el.time.sleep = lambda *_a, **_k: None
        el.format_quantity = lambda _symbol, quantity, *_a, **_k: round(float(quantity), 4)
        el.cancel_algo_orders = lambda *_a, **_k: None
        el.write_alert = lambda *_a, **_k: None
        el.push_signal_alert = lambda *_a, **_k: None
        el.record_close = lambda *a, **_k: self.recorded.append(a)
        el.cooldown_manager = None
        el._exchange_position = lambda _symbol: (True, "SHORT", self.amt)

    def _pos(self):
        return {
            "symbol": "XRP",
            "type": "SHORT",
            "entry_price": 1.3152,
            "qty": 152.1,
            "amount": 152.1,
            "trade_id": "20261104-230033-XRP-b5b279",
            "strategy": "rsi_fade",
            "regime": "rsi",
        }

    def test_partial_fill_keeps_residual_and_books_the_trade(self):
        def place_order(**kwargs):
            self.calls.append(kwargs["reduce_only"])
            if len(self.calls) == 1:
                self.amt = 139.8
                return {"success": True, "order": dict(XRP_ACK), "quantity_used": 152.1}
            return {"success": False, "order": {"code": -2022, "msg": "ReduceOnly Order is rejected."}}

        self.el.place_order = place_order
        self.el.request = lambda *_a, **_k: [dict(XRP_TRADE)]
        pos = self._pos()
        pnl, closed = self.el.close_position(pos, "rsi_mid", 1.0, 1.305)
        self.assertFalse(closed)
        self.assertAlmostEqual(pnl, 0.12177, places=5)
        self.assertAlmostEqual(pos["qty"], 139.8, places=4)
        self.assertEqual(self.calls, [True, True])
        self.assertAlmostEqual(self.recorded[0][1], 0.12177, places=5)
        self.assertTrue(pos.get("_book_pnl"))

    def test_reduce_only_reject_never_sends_a_normal_order(self):
        def place_order(**kwargs):
            self.calls.append(kwargs["reduce_only"])
            return {"success": False, "order": {"code": -2022, "msg": "ReduceOnly Order is rejected."}}

        self.el.place_order = place_order
        self.el.request = lambda *_a, **_k: []
        pos = self._pos()
        pnl, closed = self.el.close_position(pos, "rsi_mid", 1.0, 1.2564)
        self.assertFalse(closed)
        self.assertEqual(pnl, 0.0)
        self.assertAlmostEqual(pos["qty"], 152.1, places=4)
        self.assertEqual(self.calls, [True, True])
        self.assertEqual(self.recorded, [])

    def test_unconfirmed_ack_is_not_a_full_close(self):
        def place_order(**kwargs):
            self.calls.append(kwargs["reduce_only"])
            return {"success": True, "order": dict(XRP_ACK), "quantity_used": 152.1}

        self.el.place_order = place_order
        self.el.request = lambda *_a, **_k: []
        pos = self._pos()
        pnl, closed = self.el.close_position(pos, "rsi_mid", 1.0, 1.305)
        self.assertFalse(closed)
        self.assertEqual(pnl, 0.0)
        self.assertAlmostEqual(pos["qty"], 152.1, places=4)
        self.assertTrue(all(flag is True for flag in self.calls))
        self.assertEqual(self.recorded, [])


if __name__ == "__main__":
    unittest.main()
