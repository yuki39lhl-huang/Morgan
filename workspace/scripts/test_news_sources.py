import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import crypto_news_fetcher as news
import trade_db

RSS = b"""<rss><channel><item><title>Monetary &amp; policy</title>
<link>https://example.com/policy</link><pubDate>Fri, 20 Nov 2026 11:01:19 GMT</pubDate>
</item></channel></rss>"""
TELEGRAM = b"""<div data-post="binance_announcements/123">
<div class="tgme_widget_message_text"><b>Binance updates BTC terms</b><br>Details &amp; links</div>
<a><time datetime="2026-11-20T11:01:19+00:00"></time></a>
</div><div data-post="other/456"><time datetime="2026-11-20T11:01:19+00:00"></time></div>"""
OBSERVED = 1795351806851


class NewsSourcesTest(unittest.TestCase):
    def setUp(self):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(trade_db, "DB_PATH", str(temp / "test.db")))
        self.enterContext(patch.object(news, "NEWS_FILE", temp / "news.json"))
        self.enterContext(patch.object(news, "ANALYSIS_FILE", temp / "report.md"))
        trade_db.init_db()

    def test_parsers_preserve_publication_time_and_permalink(self):
        rss = news._parse_rss("Fed", RSS, OBSERVED)[0]
        posts = news._parse_binance("币安", TELEGRAM, OBSERVED)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["url"], "https://t.me/binance_announcements/123")
        self.assertEqual(posts[0]["title"], "Binance updates BTC terms")
        self.assertIn("Details & links", posts[0]["content"])
        self.assertEqual(posts[0]["published_ms"], rss["published_ms"])
        self.assertEqual(posts[0]["time"], "2026-11-20 19:01")

    def test_http_304_and_failure_keep_source_state(self):
        response = Mock(status_code=304)
        with patch.object(news.requests, "get", return_value=response) as get:
            items, health = news._fetch("CoinDesk", "https://example.com", {"etag": "x"})
        self.assertIsNone(items)
        self.assertEqual(health["status"], "unchanged")
        self.assertEqual(get.call_args.kwargs["headers"]["If-None-Match"], "x")
        trade_db.record_news_source("CoinDesk", {"status": "ok", "checked_ms": 1,
                                                  "item_count": 15, "etag": "x"})
        trade_db.record_news_source("CoinDesk", {**health, "checked_ms": 2})
        with patch.object(news.requests, "get", side_effect=requests.Timeout):
            _, failed = news._fetch("CoinDesk", "https://example.com", {})
        trade_db.record_news_source("CoinDesk", {**failed, "checked_ms": 3})
        with sqlite3.connect(trade_db.DB_PATH) as db:
            row = db.execute("SELECT last_ok_ms, attempts, failures, item_count, etag FROM news_source_health").fetchone()
        db.close()
        self.assertEqual(row, (2, 3, 1, 15, "x"))

    def test_snapshot_retains_first_arrival_across_304(self):
        feed = {"CoinDesk": "https://example.com"}
        item = news._parse_rss("CoinDesk", RSS, OBSERVED)[0]
        with patch.object(news, "SOURCES", feed), patch.object(news, "_fetch", side_effect=[
            ([item], {"status": "ok", "checked_ms": OBSERVED, "etag": "valid"}),
            (None, {"status": "unchanged", "checked_ms": OBSERVED + 1}),
            (None, {"status": "error", "checked_ms": OBSERVED + 2}),
        ]) as fetch:
            self.assertEqual(news.main(), 0)
            self.assertEqual(news.main(), 0)
            before = news.NEWS_FILE.read_bytes()
            self.assertEqual(news.main(), 1)
        self.assertEqual(fetch.call_args_list[1].args[2]["etag"], "valid")
        self.assertEqual(news.NEWS_FILE.read_bytes(), before)
        item = json.loads(before)["news"][0]
        self.assertEqual(item["observed_ms"], OBSERVED)
        with sqlite3.connect(trade_db.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT count(*), max(observed_ms) FROM news").fetchone(), (1, OBSERVED))
        db.close()


if __name__ == "__main__":
    unittest.main()
