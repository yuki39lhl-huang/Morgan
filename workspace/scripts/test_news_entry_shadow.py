import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from requests import Response
from requests.exceptions import HTTPError

import news_entry_shadow as shadow
from trading_dashboard import read_news_entries
from news_event_shadow import SCHEMA, event_targets, store_events


class NewsEntryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(tmp) / "trades.db"
        with closing(self.connect()) as db:
            db.executescript(
                "CREATE TABLE news (source TEXT,title TEXT,url TEXT); " + SCHEMA + shadow.SCHEMA + shadow.news_testnet.SCHEMA
            )

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def source(self, current, *, official, observed=None, published=None):
        now = shadow.now_ms()
        title = "Binance Futures Will Launch SUIUSDT Perpetual Contract"
        source = "币安" if official else "金十数据"
        url = "https://t.me/binance_announcements/123" if official else "https://flash.jin10.com/detail/123"
        row = {"title": title, "source": source, "url": url, "source_id": str(current),
               "published_ms": published or now - 100, "observed_ms": observed or now}
        with closing(self.connect()) as db, db:
            store_events(db, [row], {"SUIUSDT"})
            db.execute("INSERT INTO news VALUES (?,?,?)", (source, title, url))
        return row

    def test_official_evidence_does_not_inherit_trust_from_a_headline(self):
        raw = self.source(1, official=False)
        trusted = {**raw, "source": "币安", "url": "https://www.binance.com/en/support/announcement/123"}
        forged = {**trusted, "url": "https://other.example/binance/support/announcement/123"}
        self.assertEqual(event_targets(raw), [("SUIUSDT", "listing", "reported")])
        self.assertEqual(event_targets(trusted), [("SUIUSDT", "listing", "confirmed")])
        self.assertEqual(event_targets(forged), [("SUIUSDT", "listing", "reported")])

    async def test_report_is_blocked_confirmation_upgrades_and_paper_deduplicates(self):
        self.source(1, official=False)
        model = Mock(return_value='{"action":"long","reason":"模拟理由"}')
        session = Mock()

        @asynccontextmanager
        async def ws(*args, **kwargs):
            yield object()

        session.ws_connect.side_effect = ws
        slots = asyncio.Semaphore(2)
        with closing(self.connect()) as db:
            report, = shadow.candidates(db, shadow.now_ms(), 3)
        await shadow.evaluate(session, None, self, model, {"SUIUSDT"}, report, slots)
        session.ws_connect.assert_not_called()
        self.source(2, official=True)
        with closing(self.connect()) as db:
            event, = shadow.candidates(db, shadow.now_ms(), 3)
        quotes = [
            {"bid": bid, "ask": bid + .01, "ask_qty": 1000, "bid_qty": 1000,
             "received_ms": shadow.now_ms() + 20 + i, "exchange_ms": shadow.now_ms() + 20 + i}
            for i, bid in enumerate((10, 10.2, 9.95, 10.3))
        ]
        with patch.object(shadow, "read_until", new=AsyncMock(side_effect=quotes)) as reader:
            await shadow.evaluate(session, None, self, model, {"SUIUSDT"}, event, slots)
            await shadow.evaluate(session, None, self, model, {"SUIUSDT"}, event, slots)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(reader.await_count, 1)
        with closing(self.connect()) as db:
            statuses = dict(db.execute("SELECT stage,status FROM news_entry_decisions"))
            self.assertEqual(statuses, {"reported": "needs_confirmation", "confirmed": "testnet_queued"})
            row = db.execute("SELECT direction,status FROM news_testnet_orders").fetchone()
            self.assertEqual(tuple(row), ("LONG", "queued"))
            self.assertEqual(db.execute("SELECT count(*) FROM news_entry_papers").fetchone()[0], 0)

    async def test_old_confirmed_news_keeps_the_decision_but_never_requests_a_book(self):
        self.source(1, official=True, published=shadow.now_ms() - shadow.FRESH_MS - 1000)
        model = Mock(return_value='{"action":"long","reason":"参考"}')
        session = Mock()
        with closing(self.connect()) as db:
            self.assertEqual(shadow.candidates(db, shadow.now_ms(), 3), [])
        model.assert_not_called()
        session.ws_connect.assert_not_called()

    async def test_retry_exposes_status_and_keeps_last_failure_after_recovery(self):
        self.source(1, official=False)
        response = Response()
        response.status_code = 429
        response.headers["Retry-After"] = "4"
        model = Mock(side_effect=[
            HTTPError("private provider body", response=response),
            '{"action":"wait","reason":"证据不足"}',
        ])
        with closing(self.connect()) as db:
            event, = shadow.candidates(db, shadow.now_ms(), 3)

        async def check_wait(delay):
            self.assertEqual(delay, 4)
            with closing(self.connect()) as db:
                item, = read_news_entries(db)
                self.assertEqual(item["status"], "model_retrying")
                self.assertEqual(item["model_error"]["http_status"], 429)
                self.assertNotIn("private provider body", str(item))

        with patch.object(shadow.asyncio, "sleep", side_effect=check_wait):
            await shadow.evaluate(Mock(), None, self, model, set(), event, asyncio.Semaphore(1))
        with closing(self.connect()) as db:
            item, = read_news_entries(db)
        self.assertEqual(item["status"], "wait")
        self.assertEqual(item["model_error"]["http_status"], 429)
        self.assertEqual(model.call_count, 2)

    async def test_bad_credentials_and_retry_beyond_window_stop(self):
        for index, code, after in ((1, 401, "1"), (2, 503, "120")):
            self.source(index, official=index == 2)
            response = Response()
            response.status_code = code
            response.headers["Retry-After"] = after
            model = Mock(side_effect=HTTPError(response=response))
            with closing(self.connect()) as db:
                event, = shadow.candidates(db, shadow.now_ms(), 3)
            await shadow.evaluate(Mock(), None, self, model, set(), event, asyncio.Semaphore(1))
            self.assertEqual(model.call_count, 1)
            with closing(self.connect()) as db:
                item = next(row for row in read_news_entries(db) if row["stage"] == event["stage"])
            self.assertEqual(item["status"], "model_error")
            self.assertEqual(item["model_error"]["http_status"], code)
            self.assertIsNone(item["model_error"]["next_retry_ms"])

    def test_migration_keeps_existing_rows(self):
        with closing(self.connect()) as db, db:
            db.execute("ALTER TABLE news_entry_decisions DROP COLUMN model_error_json")
            db.execute("INSERT INTO news_entry_decisions VALUES ('e','watch','TESTUSDT',1,2,NULL,'model_error','HTTPError','[]',NULL)")
            self.assertIsNone(read_news_entries(db)[0]["model_error"])
        with closing(self.connect()) as db:
            shadow.init_schema(db)
            shadow.init_schema(db)
            item, = read_news_entries(db)
        self.assertEqual(item["reason"], "HTTPError")
        self.assertIsNone(item["model_error"])

    def test_unified_feed_observes_delayed_headlines_but_only_fresh_candidates_are_analyzed(self):
        now = shadow.now_ms()
        title = "NVIDIA Board Reinstates Jensen Huang as President, CEO and Board Director"
        with closing(self.connect()) as db, db:
            db.execute("ALTER TABLE news ADD COLUMN id INTEGER")
            db.execute("ALTER TABLE news ADD COLUMN published_ms INTEGER")
            db.execute("ALTER TABLE news ADD COLUMN observed_ms INTEGER")
            db.executemany("INSERT INTO news VALUES (?,?,?,?,?,?)", (
                ("NVIDIA", title, "https://nvidianews.nvidia.com/news/old", 1, now - 360_000, now - 1),
                ("NVIDIA", title, "https://nvidianews.nvidia.com/news/new", 2, now - 45_000, now),
            ))
        marker = shadow.index_recent_news(self, {"NVDAUSDT"}, now, 0)
        self.assertEqual(marker, 2)
        with closing(self.connect()) as db:
            event, = shadow.candidates(db, now, 3)
            self.assertEqual((event["symbol"], event["stage"]), ("NVDAUSDT", "confirmed"))
            urls = db.execute("SELECT source_id FROM news_event_observations").fetchall()
            self.assertCountEqual([row[0] for row in urls], ["1", "2"])

    def test_market_alert_is_observation_only_and_stablecoin_news_is_recognized(self):
        now = shadow.now_ms()
        base = {"published_ms": now - 1_000, "observed_ms": now, "url": "https://example.com/article"}
        rows = [
            {**base, "source": "Equation News", "source_id": "1",
             "title": "币安合约监测：AKEUSDT 在过去 10 秒内下跌 6%。"},
            {**base, "source": "Benzinga", "source_id": "2",
             "title": "Justice Department Moves to Forfeit $1.1B in USDC"},
            {**base, "source": "Cointelegraph", "source_id": "3",
             "title": "USDT buyers face 22% VAT when Moscow Exchange opens crypto trading"},
        ]
        with closing(self.connect()) as db, db:
            store_events(db, rows, {"AKEUSDT", "USDCUSDT"})
            results = shadow.candidates(db, now, 10)
            self.assertEqual({r["symbol"] for r in results}, {"USDCUSDT", "USDT"})
            alert, = db.execute("SELECT topic,stage FROM news_event_observations WHERE symbol='AKEUSDT'")
            self.assertEqual(tuple(alert), ("market_move", "watch"))
            health, = db.execute("SELECT status FROM news_event_targets WHERE symbol='AKEUSDT'")
            self.assertEqual(health[0], "watch")

    def test_spread_depth_and_costs(self):
        narrow = {"bid": 10, "ask": 10.01, "bid_qty": 40, "ask_qty": 40}
        self.assertEqual(shadow.entry_terms("long", {**narrow, "ask": 10.1})[1], "wide_spread")
        self.assertEqual(shadow.entry_terms("short", {**narrow, "bid_qty": 1})[1], "thin_book")
        entry, blocked = shadow.entry_terms("short", narrow)
        self.assertIsNone(blocked)
        self.assertEqual(entry["notional_usdt"], 200)
        self.assertLess(entry["price"], narrow["bid"])
