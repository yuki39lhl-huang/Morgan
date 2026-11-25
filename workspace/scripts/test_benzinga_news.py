"""Exercise Benzinga replay and live delivery without using a provider token."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

import benzinga_news


class FakeWebSocket:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send_str(self, command):
        self.sent.append(command)

    async def receive(self, *, timeout):
        return next(self.frames)


class TestBenzingaStream(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def article(title, created, timestamp):
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps({
            "kind": "news", "data": {
                "action": "Created", "timestamp": timestamp,
                "content": {"title": title, "url": "https://example.com/article", "created": created},
            },
        }))

    async def test_replay_is_stored_separately_and_cannot_pause_rest(self):
        connected = 1_795_584_600_000
        observed = connected + 3_000
        replay = self.article("replayed headline", "2026-11-25T05:29:00Z", "2026-11-25T05:29:01Z")
        live = self.article("live headline", "Wed, 25 Nov 2026 05:30:01 GMT", "2026-11-25T05:30:01Z")
        pong = SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data="pong")
        closed = SimpleNamespace(type=aiohttp.WSMsgType.CLOSED)
        rows, transport = [], []

        def record(_db, channel, status, *, article=False):
            transport.append((channel, status, article))

        for frames in ((pong, replay, closed), (pong, live, closed)):
            ws = FakeWebSocket(frames)
            session = SimpleNamespace(ws_connect=lambda *a, **k: ws)
            db = SimpleNamespace(insert_news=rows.extend, record_news_source=lambda *a: None)
            state = {"last_push": 0, "last_replay_request": 0}
            with patch.object(benzinga_news, "now_ms", side_effect=[connected, observed, observed]), patch.object(
                benzinga_news, "record_transport", record,
            ):
                await benzinga_news.receive(session, db, "unused", state)
            self.assertEqual(ws.sent, ["ping", "replay"])
            self.assertEqual(state["last_push"], observed if frames[1] is live else 0)

        self.assertIn(("replay", "ok", True), transport)
        self.assertIn(("push", "ok", True), transport)
        self.assertLess(rows[0]["published_ms"], connected)
        self.assertEqual(rows[0]["observed_ms"], observed)
