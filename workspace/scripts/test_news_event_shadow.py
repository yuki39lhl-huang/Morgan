import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import Mock

from news_event_shadow import (
    Jin10Client, active_contracts, event_candidates, flash_article,
    minute_followup, paper_followup, replay,
)


def article(subject, source, seq, published, observed, url="https://flash.jin10.com"):
    return {
        "title": subject, "source": source, "source_id": seq, "published_ms": published,
        "observed_ms": observed, "url": url,
    }


def contract(symbol, state="TRADING"):
    return {"symbol": symbol, "status": state, "quoteAsset": "USDT", "contractType": "PERPETUAL"}


class NewsEventTest(unittest.TestCase):
    def test_event_lifecycle_and_duplicate(self):
        watch = article("贝森特不再排除黄仁勋复任英伟达 CEO", "快讯", "1", 100, 110)
        report = article("市场消息：英伟达董事会恢复黄仁勋 CEO 职务", "快讯", "2", 200, 210)
        confirmation = article("NVIDIA Board Reinstates Jensen Huang as CEO", "SEC", "3", 220, 230,
                               "https://www.sec.gov/Archives/edgar/data/1045810/filing")
        repost = article(report["title"], "Other", "4", 201, 225)
        payload = {
            "articles": [confirmation, report, report, watch, repost],
            "exchange_info": {"symbols": [{**contract("NVDAUSDT"), "contractType": "TRADIFI_PERPETUAL"}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixtures.json"
            fixture.write_text(json.dumps(payload))
            db_path = Path(tmp) / "events.db"
            events = replay(fixture, db_path)
            self.assertEqual([e["stage"] for e in events], ["watch", "reported", "reported", "confirmed"])
            self.assertEqual(len({e["event_key"] for e in events}), 1)
            self.assertEqual(replay(fixture, db_path), [])
            with closing(sqlite3.connect(db_path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM news_event_paper").fetchone()[0], 2)

    def test_dynamic_symbol_must_be_tradeable(self):
        row = article("$TEST CEO reinstated", "快讯", "1", 100, 110)
        catalog = active_contracts({"symbols": [contract("TESTUSDT"), contract("NVDAUSDT", "HALT")]})
        self.assertEqual(event_candidates(row, catalog), [("TESTUSDT", "reported")])
        self.assertEqual(event_candidates(row, {"NVDAUSDT"}), [])

    def test_price_surge_is_not_a_call_to_rehire(self):
        story = article("Nvidia shares surge 27% as Jensen Huang returns as CEO", "CoinDesk", "1", 100, 110)
        self.assertEqual(event_candidates(story, {"NVDAUSDT"}), [("NVDAUSDT", "reported")])

    def test_simulations_use_only_quotes_and_openings_after_signal(self):
        quotes = [
            {"timestamp_ms": 59_999, "bid": 88, "ask": 89},
            {"timestamp_ms": 60_001, "bid": 99, "ask": 100},
            {"timestamp_ms": 120_001, "bid": 102, "ask": 103},
        ]
        result = paper_followup(quotes, 60_000)
        self.assertEqual(result["entry_ask"], 100)
        self.assertEqual(result["returns_pct_before_fees"], {"60": 2.0, "300": None, "900": None})
        minute = minute_followup([[60_000, 90], [120_000, 100], [180_000, 101]], 60_025)
        self.assertEqual(minute["entry_price"], 100)
        self.assertEqual(minute["returns_pct_before_fees"]["60"], 1.0)

    def test_jin10_protocol_and_timestamp(self):
        flash = {"id": "1", "time": "2026-11-23T20:56:27+08:00", "title": "", "content": "英伟达快讯"}
        session = Mock()

        def post(url, headers, json, timeout, stream):
            self.assertEqual(headers["Authorization"], "Bearer fixture")
            if json["method"] == "initialize":
                result = {"protocolVersion": "2025-03-26"}
            else:
                self.assertEqual(headers["Mcp-Session-Id"], "fixture-session")
                result = {"structuredContent": {"status": 200, "data": {"items": [flash]}}}
            response = Mock(status_code=200, headers={"Mcp-Session-Id": "fixture-session"})
            response.json.return_value = {"id": json.get("id"), "result": result}
            return nullcontext(response)

        session.post.side_effect = post
        result = flash_article(Jin10Client(session, "fixture").list_flash()[0], 1795438590000)
        self.assertEqual(result["title"], "英伟达快讯")
        self.assertEqual(result["time"], "2026-11-23 20:56")
        self.assertEqual(session.post.call_count, 3)

    def test_jin10_row_without_an_id_has_stable_identity(self):
        row = {"time": "2026-11-23T20:56:27+08:00", "content": "英伟达快讯",
               "url": "https://flash.jin10.com/detail/20261123205627001200"}
        self.assertEqual(flash_article(row, 1)["source_id"], "20261123205627001200")
        row.pop("url")
        self.assertEqual(flash_article(row, 1)["source_id"], flash_article(row, 2)["source_id"])
