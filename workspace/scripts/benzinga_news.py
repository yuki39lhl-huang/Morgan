"""Ingest Benzinga headlines through the news-only API."""

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp

from jin10_collector import TOKEN_FILE as JIN10_TOKEN_FILE, read_token
from news_event_quotes import now_ms
from news_event_shadow import TZ

SOURCE = "Benzinga"
TOKEN_FILE = JIN10_TOKEN_FILE.with_name("benzinga_news.token")
REST = "https://api.benzinga.com/api/v2/news"
STREAM = "wss://api.benzinga.com/api/v1/news/stream"
MAX_AGE_MS = 6 * 60 * 60 * 1000
KEY_RECHECK_SECONDS = 300
REST_INTERVAL_SECONDS = 120
PUSH_SILENCE_MS = 5 * 60_000
REST_OVERLAP_MS = PUSH_SILENCE_MS + REST_INTERVAL_SECONDS * 1000
PAGE_SIZE = 50
MAX_PAGES = 3
SCHEMA = """
CREATE TABLE IF NOT EXISTS benzinga_transport (
    channel TEXT PRIMARY KEY, checked_ms INTEGER NOT NULL, status TEXT NOT NULL,
    last_article_ms INTEGER, last_heartbeat_ms INTEGER, last_ok_ms INTEGER
)
"""
log = logging.getLogger(__name__)


def as_news(article, observed):
    try:
        title, url = article["title"], article["url"]
        published = parsedate_to_datetime(article["created"])
        if published.tzinfo is None or not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(url, str) or urlsplit(url).scheme not in {"https", "http"} or not urlsplit(url).hostname:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    stamp = int(published.timestamp() * 1000)
    if not observed - MAX_AGE_MS <= stamp <= observed:
        return None
    return {"source": SOURCE, "title": " ".join(title.split()), "url": url,
            "time": published.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
            "published_ms": stamp, "observed_ms": observed}


def push_article(payload):
    if not isinstance(payload, dict) or payload.get("kind") != "news":
        return None
    data = payload.get("data")
    if isinstance(data, dict) and str(data.get("action")).lower() == "created":
        content = data.get("content")
        return content if isinstance(content, dict) else None
    return None


def record_transport(db, channel, status, *, article=False):
    stamp = now_ms()
    with closing(db.connect()) as conn, conn:
        conn.execute(
            """INSERT INTO benzinga_transport VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel) DO UPDATE SET checked_ms=excluded.checked_ms, status=excluded.status,
               last_article_ms=COALESCE(excluded.last_article_ms, benzinga_transport.last_article_ms),
               last_heartbeat_ms=COALESCE(excluded.last_heartbeat_ms, benzinga_transport.last_heartbeat_ms),
               last_ok_ms=COALESCE(excluded.last_ok_ms, benzinga_transport.last_ok_ms)""",
            (channel, stamp, status, stamp if article else None,
             stamp if status == "heartbeat" else None,
             stamp if status in {"ok", "heartbeat", "connected"} else None),
        )


async def save(db, articles):
    observed = now_ms()
    rows = [row for article in articles if isinstance(article, dict) and (row := as_news(article, observed))]
    if rows:
        await asyncio.to_thread(db.insert_news, rows)
        await health(db, "ok", item_count=len(rows), latest_published_ms=max(row["published_ms"] for row in rows))
    else:
        await health(db, "unchanged")
    return bool(rows)


async def health(db, status, **details):
    try:
        await asyncio.to_thread(db.record_news_source, SOURCE, {
            "checked_ms": now_ms(), "status": status, **details,
        })
    except sqlite3.Error:
        log.warning("Benzinga source health could not be saved")


async def catch_up(session, db, token, since):
    articles = []
    for page in range(MAX_PAGES):
        async with session.get(
            REST, params={"token": token, "page": page, "pageSize": PAGE_SIZE,
                          "updatedSince": since // 1000, "displayOutput": "headline"},
            headers={"Accept": "application/json"}, allow_redirects=False,
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            if not isinstance(payload, list):
                raise ValueError("invalid Benzinga news feed")
            articles.extend(payload)
        if len(payload) < PAGE_SIZE:
            break
    else:
        log.warning("Benzinga REST catch-up reached its page limit")
    return await save(db, articles)


async def receive(session, db, token, state):
    async with session.ws_connect(STREAM, params={"token": token}) as ws:
        await asyncio.to_thread(record_transport, db, "push", "connected")
        await ws.send_str("ping")
        while True:
            try:
                msg = await ws.receive(timeout=30)
            except TimeoutError:
                await ws.send_str("ping")
                msg = await ws.receive(timeout=10)
            if msg.type != aiohttp.WSMsgType.TEXT:
                return
            if msg.data.strip().lower() in {"pong", '"pong"'}:
                await asyncio.to_thread(record_transport, db, "push", "heartbeat")
                continue
            try:
                article = push_article(json.loads(msg.data))
            except ValueError:
                continue
            if article and await save(db, [article]):
                state["last_push"] = now_ms()
                await asyncio.to_thread(record_transport, db, "push", "ok", article=True)


def failure(exc):
    status = getattr(exc, "status", None)
    headers = getattr(exc, "headers", None)
    retry_after = headers.get("Retry-After", "") if headers else ""
    delay = int(retry_after) if retry_after.isdecimal() and len(retry_after) <= 8 else 0
    if status in {401, 403}:
        delay = max(delay, KEY_RECHECK_SECONDS)
    return f"HTTP {status}" if status else type(exc).__name__, delay


async def watch_channel(session, db, state, channel):
    backoff = 5
    last_rest = 0
    while True:
        started = now_ms()
        if channel == "rest" and last_rest and started - state["last_push"] < PUSH_SILENCE_MS:
            await asyncio.sleep(REST_INTERVAL_SECONDS)
            continue
        try:
            token = read_token(TOKEN_FILE)
        except (OSError, ValueError):
            token = None
        if not token:
            await asyncio.to_thread(record_transport, db, channel, "credential_unavailable")
            await asyncio.sleep(KEY_RECHECK_SECONDS)
            continue
        minimum = 0
        try:
            if channel == "rest":
                since = max(started - MAX_AGE_MS, last_rest - REST_OVERLAP_MS)
                received = await catch_up(session, db, token, since)
                await asyncio.to_thread(record_transport, db, channel, "ok", article=received)
                last_rest = started
                backoff = 5
                await asyncio.sleep(REST_INTERVAL_SECONDS)
                continue
            await receive(session, db, token, state)
            error = "stream_closed"
        except (aiohttp.ClientError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
            error, minimum = failure(exc)
        try:
            await asyncio.to_thread(record_transport, db, channel, error)
        except sqlite3.Error:
            log.warning("Benzinga transport status could not be saved")
        if channel == "rest":
            await health(db, "error", error=error)
        if now_ms() - started >= 60_000:
            backoff = 5
        interval = REST_INTERVAL_SECONDS if channel == "rest" else backoff
        await asyncio.sleep(max(minimum, interval, backoff))
        backoff = min(backoff * 2, KEY_RECHECK_SECONDS)


async def watch_benzinga(db):
    with closing(db.connect()) as conn, conn:
        conn.execute(SCHEMA)
    state = {"last_push": 0}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with asyncio.TaskGroup() as group:
            for channel in ("push", "rest"):
                group.create_task(watch_channel(session, db, state, channel))
