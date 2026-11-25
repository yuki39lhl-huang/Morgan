import sqlite3
import unittest
from contextlib import closing

from news_event_quotes import claim_jobs, markouts, parse_book
from news_event_shadow import event_targets, store_events


class NewsBookTest(unittest.TestCase):
    def test_currency_amount_is_not_an_asset_symbol(self):
        self.assertEqual(event_targets({"title": "Wrapped Monero hit by a $13.5M exploit"}), [])
        self.assertEqual(event_targets({"title": "Binance lists $1000PEPE"}),
                         [("1000PEPEUSDT", "listing", "reported")])

    def test_outside_rsi_pool_queues_once_and_missing_contract_stays_visible(self):
        now = 100_000
        items = [
            {"title": "Binance lists $SUI", "source": "news", "source_id": "1",
             "published_ms": now - 20, "observed_ms": now},
            {"title": "Binance lists XYZTEST/USDT", "source": "news", "source_id": "2",
             "published_ms": now - 10, "observed_ms": now},
            {"title": "$SUI gets new listing", "source": "other", "source_id": "3",
             "published_ms": now - 10, "observed_ms": now},
        ]
        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                self.assertEqual(len(store_events(db, items, {"SUIUSDT"})), 3)
            rows = db.execute("SELECT symbol,status FROM news_event_targets ORDER BY symbol").fetchall()
            self.assertEqual(rows, [("SUIUSDT", "pending"), ("XYZTESTUSDT", "no_testnet_contract")])
            self.assertEqual([job[1] for job in claim_jobs(db, now + 1000)], ["SUIUSDT"])
            self.assertEqual(claim_jobs(db, now + 1050), [])

    def test_expired_job_cannot_use_current_price(self):
        item = {"title": "$SUI was hacked", "source": "news", "source_id": "1",
                "published_ms": 100, "observed_ms": 101}
        with closing(sqlite3.connect(":memory:")) as db:
            with db:
                store_events(db, [item], {"SUIUSDT"})
            self.assertEqual(claim_jobs(db, 20_000), [])
            self.assertEqual(db.execute("SELECT status FROM news_event_targets").fetchone()[0], "expired")

    def test_book_validates_time_and_marks_both_directions(self):
        book = {"s": "SUIUSDT", "b": "10", "a": "11", "B": "1", "A": "2", "T": 99_000}
        entry = parse_book(book, "SUIUSDT", 100_000)
        self.assertIsNotNone(entry)
        self.assertIsNone(parse_book(book, "SUIUSDT", 120_000))
        self.assertIsNone(parse_book({**book, "a": "9"}, "SUIUSDT", 100_000))
        result = markouts(entry, {60: {"bid": 12, "ask": 13}})
        self.assertEqual(result["horizons"]["60"], {"long_pct": 9.090909, "short_pct": -30})
        self.assertIsNone(result["horizons"]["300"])
