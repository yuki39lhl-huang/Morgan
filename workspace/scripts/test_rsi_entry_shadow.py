import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import trade_db
from ai_layer import AIPredictor
from rsi_entry_shadow import CST, HOUR_MS, record_batch, summarize

SIGNAL = int(datetime(2026, 11, 22, 12, tzinfo=CST).timestamp() * 1000)


def bars(rising=False):
    result = []
    for i in range(21):
        start = SIGNAL - (20 - i) * HOUR_MS
        price = 100 + i if rising else 100 - i
        result.append([start, price, price + 1, price - 1, price, 1, start + HOUR_MS - 1])
    return result


class RSIEntryShadowTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = str(Path(temp) / "trade_data.db")
        self.enterContext(patch.object(trade_db, "DB_PATH", self.path))
        trade_db.init_db()
        self.predictor = Mock()
        self.predictor.assess_rsi_entry.return_value = {"action": "wait_one_bar", "reason": "延续风险"}

    def news(self, hours_ago, observed_ms, title, source="CoinDesk"):
        published = datetime.fromtimestamp(SIGNAL / 1000, CST) - timedelta(hours=hours_ago)
        return {
            "time": published.strftime("%Y-%m-%d %H:%M"), "observed_ms": observed_ms,
            "title": title, "source": source, "url": "https://example.com/news",
        }

    def row(self, symbol="BTC"):
        with closing(trade_db.connect(readonly=True)) as conn:
            row = conn.execute("SELECT * FROM rsi_entry_shadow WHERE symbol = ?", (symbol,)).fetchone()
            return dict(row) if row else None

    def test_point_in_time_inputs_dedup_and_outcomes(self):
        early = self.news(1, SIGNAL - 60_001, "BTC 会计公告")
        late = self.news(1, SIGNAL + 1, "BTC 延迟收录")
        future = self.news(-1, SIGNAL - 1, "BTC 未来发布")
        legacy = self.news(2, None, "BTC 旧记录")
        other_symbol = self.news(1, SIGNAL - 1, "Solana 公告")
        trade_db.insert_news([early, late, future, legacy, other_symbol])
        trade_db.insert_news([{**early, "observed_ms": SIGNAL + 2}])
        cutoff = datetime.fromtimestamp(SIGNAL / 1000)
        trade_db.insert_market_ext((cutoff - timedelta(minutes=1)).isoformat(), {"BTC": {"funding": 0.001}})
        trade_db.insert_market_ext((cutoff + timedelta(minutes=1)).isoformat(), {"BTC": {"funding": 0.002}})

        with patch("rsi_entry_shadow.time.time_ns", return_value=(SIGNAL + 1_000) * 1_000_000):
            record_batch(self.predictor, {"BTC": bars()}, SIGNAL + 10, 25)
        record_batch(self.predictor, {"BTC": bars()}, SIGNAL + 100, 25)
        row = self.row()
        inputs = json.loads(row["inputs_json"])
        results = json.loads(row["result_json"])
        self.assertEqual(inputs["market"]["side"], "LONG")
        self.assertEqual(inputs["market"]["close"], 81)
        self.assertEqual(inputs["market"]["funding"]["rate"], 0.001)
        self.assertEqual(inputs["market"]["volume_vs_previous_6h"], 1)
        self.assertEqual(inputs["baseline"], "enter_now")
        self.assertEqual(inputs["market_rule"], "wait_one_bar")
        self.assertEqual([n["title"] for n in inputs["news"]], [early["title"]])
        self.assertEqual(inputs["news"][0]["observed_ms"], early["observed_ms"])
        self.assertEqual(results["news_ai"]["action"], "wait_one_bar")
        self.assertEqual(self.predictor.assess_rsi_entry.call_count, 2)
        self.assertIsNone(row["next_bar_json"])

        record_batch(self.predictor, {"BTC": bars()}, SIGNAL + HOUR_MS + 10, 25)
        with closing(trade_db.connect(readonly=True)) as conn:
            outcome = conn.execute(
                "SELECT next_bar_json FROM rsi_entry_shadow WHERE symbol = ? AND signal_ms = ?",
                ("BTC", SIGNAL),
            ).fetchone()[0]
            total, paired, counts = summarize(conn, 1.0)
        self.assertEqual((total, paired), (2, 1))
        self.assertEqual(counts["触线即进"]["entered_adverse"], 1)
        self.assertEqual(counts["行情加新闻 AI"]["wait"], 1)
        self.assertEqual(json.loads(outcome), {"open_ms": SIGNAL, "high": 81, "low": 79, "close": 80})

    def test_short_and_model_failure_and_stale_cache(self):
        self.predictor.assess_rsi_entry.side_effect = TimeoutError
        record_batch(self.predictor, {"ETH": bars(rising=True)}, SIGNAL + 10, None)
        record_batch(self.predictor, {"SOL": bars()}, SIGNAL + 4 * 60_000, None)
        self.assertIsNone(self.row("SOL"))
        row = self.row("ETH")
        self.assertEqual(json.loads(row["inputs_json"])["market"]["side"], "SHORT")
        result = json.loads(row["result_json"])
        self.assertEqual(result["market_ai"], {"status": "unavailable"})
        self.assertEqual(result["news_ai"], {"status": "no_news"})

    def test_legacy_rows_get_arrival_only_after_refetch(self):
        legacy = self.news(2, None, "BTC 旧记录")
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "legacy.db")
            with patch.object(trade_db, "DB_PATH", path), closing(sqlite3.connect(path)) as conn:
                conn.executescript(trade_db.SCHEMA.replace(
                    "    observed_ms INTEGER,           -- 首次有可验证时间的采集\n", ""
                ))
                conn.execute(
                    "INSERT INTO news (published, fetched, source, title) VALUES (?,?,?,?)",
                    (legacy["time"], legacy["time"], legacy["source"], legacy["title"]),
                )
                conn.commit()
                trade_db.init_db()
                self.assertIsNone(conn.execute("SELECT observed_ms FROM news").fetchone()[0])
                self.assertEqual(trade_db.insert_news([{**legacy, "observed_ms": SIGNAL}]), 0)
                self.assertEqual(conn.execute("SELECT observed_ms FROM news").fetchone()[0], SIGNAL)

    def test_deepseek_request_ignores_inherited_proxy(self):
        model_cfg = {
            "deepseek_url": "https://api.deepseek.example/v1/chat/completions",
            "deepseek_api_key": "test-key",
            "deepseek_model": "test-model",
        }
        reply = requests.Response()
        reply.status_code = 200
        reply._content = json.dumps({
            "choices": [{"message": {"content": '{"action":"skip","reason":"risk"}'}}],
        }).encode()
        with (
            patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890", "NO_PROXY": ""}, clear=True),
            patch("ai_layer.CONFIG", model_cfg),
            patch("requests.adapters.HTTPAdapter.send", return_value=reply) as send,
        ):
            result = AIPredictor().assess_rsi_entry({"symbol": "BTC"})
        self.assertEqual(result["action"], "skip")
        self.assertEqual(send.call_args.kwargs["proxies"], {})
        self.assertEqual(send.call_args.kwargs["timeout"], (3, 8))

    def test_model_result_validation(self):
        predictor = AIPredictor()
        with patch.object(predictor, "_request", return_value='{"action":"sell_all","reason":"bad"}'):
            with self.assertRaises(ValueError):
                predictor.assess_rsi_entry({"symbol": "BTC"})
        with patch.object(predictor, "_request", return_value='{"action":"skip","reason":"风险"}'):
            self.assertEqual(predictor.assess_rsi_entry({"symbol": "BTC"})["action"], "skip")


if __name__ == "__main__":
    unittest.main()
