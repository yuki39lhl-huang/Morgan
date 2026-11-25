"""Collect public Equation News for observation and news briefs."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp

from news_event_quotes import now_ms
from news_event_shadow import TZ

SOURCE = "Equation News"
REST = "https://api.eqn.news/public/v1/news"
STREAM = "wss://api.eqn.news/public/ws"
POLL_SECONDS = 15
OBSERVATION_MS = 15 * 60 * 1000
log = logging.getLogger(__name__)


def as_news(item, seen, push_times):
    title = item.get("zh") or item.get("en")
    url = item.get("source_url") or item.get("url")
    try:
        published = datetime.fromisoformat(item["time"])
        if published.tzinfo is None or not isinstance(title, str) or not title.strip():
            return None
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    stamp = int(published.timestamp() * 1000)
    if not seen - OBSERVATION_MS <= stamp <= seen:
        return None
    observed = push_times.get(url, seen)
    if observed < stamp:
        observed = seen
    return {"source": SOURCE, "title": " ".join(title.split()), "url": url,
            "time": published.astimezone(TZ).strftime("%Y-%m-%d %H:%M"),
            "published_ms": stamp, "observed_ms": observed}


async def stream(session, wake, push_times):
    delay = 2
    while True:
        try:
            async with session.ws_connect(STREAM, timeout=aiohttp.ClientWSTimeout(ws_receive=40)) as ws:
                delay = 2
                await ws.send_str("ping")
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), 30)
                    except TimeoutError:
                        await ws.send_str("ping")
                        async with asyncio.timeout(10):
                            msg = await ws.receive()
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    if msg.data == "pong":
                        continue
                    try:
                        row = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and isinstance(row.get("url"), str) and row.get("news_title"):
                        if len(push_times) >= 512:
                            push_times.pop(next(iter(push_times)))
                        push_times.setdefault(row["url"], now_ms())
                        wake.set()
        except (aiohttp.ClientError, TimeoutError):
            log.info("Equation News push temporarily unavailable, polling continues")
        await asyncio.sleep(delay)
        delay = min(60, delay * 2)


async def poll(session, db, wake, push_times):
    etag = None
    delay = POLL_SECONDS
    while True:
        wake.clear()
        try:
            async with session.get(REST, params={"limit": 30}, headers={"If-None-Match": etag} if etag else {}) as res:
                stamp = now_ms()
                if res.status == 304:
                    health = {"checked_ms": stamp, "status": "unchanged"}
                else:
                    res.raise_for_status()
                    payload = await res.json()
                    stamp = now_ms()
                    items = payload["items"]
                    if not isinstance(items, list):
                        raise ValueError("invalid Equation News list")
                    rows = [r for i in items if isinstance(i, dict) and (r := as_news(i, stamp, push_times))]
                    await asyncio.to_thread(db.insert_news, rows)
                    health = {"checked_ms": stamp, "status": "ok", "item_count": len(items),
                              "latest_published_ms": max((r["published_ms"] for r in rows), default=None),
                              "etag": res.headers.get("ETag")}
                    etag = health["etag"]
            await asyncio.to_thread(db.record_news_source, SOURCE, health)
        except (aiohttp.ClientError, ValueError, KeyError, TypeError, TimeoutError, sqlite3.Error) as exc:
            log.warning("Equation News poll failed (%s)", type(exc).__name__)
            retry = getattr(exc, "headers", None)
            after = retry.get("Retry-After", "") if retry else ""
            try:
                await asyncio.to_thread(db.record_news_source, SOURCE, {
                    "checked_ms": now_ms(), "status": "error", "error": type(exc).__name__,
                })
            except sqlite3.Error:
                pass
            delay = max(min(300, delay * 2), int(after) if after.isdecimal() and len(after) <= 8 else 0)
            await asyncio.sleep(delay)
            continue
        delay = POLL_SECONDS
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(wake.wait(), POLL_SECONDS - 2)
        except TimeoutError:
            pass


async def watch_equation(db):
    push_times = {}
    wake = asyncio.Event()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=True) as session:
        async with asyncio.TaskGroup() as group:
            group.create_task(stream(session, wake, push_times))
            group.create_task(poll(session, db, wake, push_times))
