"""Check arrival-time classification and recorded evidence linkage."""
import json
import sqlite3
import unittest
from contextlib import closing

from news_entry_shadow import init_schema
from news_event_shadow import store_events
from news_replay import replay


class NewsReplayTest(unittest.TestCase):
    def test_arrival_window_and_exact_evidence(self):
        start = 1_795_500_000_000
        end = start + 500_000
        official = "https://www.binance.com/en/support/announcement/demo"
        fixtures = [
            ("Binance Futures will list TOWNSUSDT perpetual", official, 20_000, 50_000),
            ("Binance Futures will delist STORJUSDT perpetual", official, 10_000, 130_000),
            ("Binance Futures will list TOWNSUSDT perpetual (update)", official, 140_000, 160_000),
            ("Wallet maintenance announced", official, 180_000, 183_000),
            ("Price alert for TOWNSUSDT", official, 190_000, 193_000),
            ("Future arrival outside window", official, 480_000, 510_000),
        ]
        articles = [dict(source="币安", title=title, url=url, published_ms=start + published,
                         observed_ms=start + observed, source_id=str(index))
                    for index, (title, url, published, observed) in enumerate(fixtures)]
        with closing(sqlite3.connect(":memory:")) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE news(id INTEGER PRIMARY KEY,source,title,url,published_ms,observed_ms)")
            db.executemany("INSERT INTO news VALUES(NULL,:source,:title,:url,:published_ms,:observed_ms)", articles)
            init_schema(db)
            store_events(db, articles, {"TOWNSUSDT", "STORJUSDT"})
            event = db.execute("SELECT event_key FROM news_event_observations WHERE title=?", (articles[0]["title"],)).fetchone()
            db.execute(
                """INSERT INTO news_entry_decisions
                   (event_key,stage,symbol,started_ms,decision_ms,action,status,evidence_json)
                   VALUES (?,'confirmed','TOWNSUSDT',?,?,'long','testnet_queued',?)""",
                (event[0], start + 51_000, start + 52_000, json.dumps(articles[:1])),
            )
            db.execute(
                """INSERT INTO news_testnet_orders
                   (event_key,stage,symbol,direction,ready_ms,deadline_ms,client_id,notional,
                    reference_price,status,submitted_ms,opened_ms)
                   VALUES (?,'confirmed','TOWNSUSDT','LONG',?,?,'fixture-id',200,1,'open',?,?)""",
                (event[0], start + 54_000, start + 100_000, start + 55_000, end + 1),
            )
            result = replay(db, start, end)

        self.assertEqual(result["counts"], {
            "headlines": 5, "recognized_headlines": 4, "timely_confirmed": 2,
            "late_news": 1, "unmatched_headlines": 1, "market_only": 1,
        })
        source = result["by_source"]["币安"]
        self.assertEqual((source["timed"], source["within_window"], source["p50_ms"], source["p90_ms"]),
                         (5, 4, 20_000, 120_000))
        self.assertEqual(result["recorded_model"]["started"], 1)
        self.assertEqual(result["recorded_testnet"], {"queued": 1, "claimed_for_submission": 1})
        first, _, update, _ = result["candidates"]
        self.assertTrue(first["model_started"])
        self.assertEqual(first["action"], "long")
        self.assertNotIn("recorded_status", first)
        self.assertTrue(update["indexed"])
        self.assertFalse(update["model_started"])
