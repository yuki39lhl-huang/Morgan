"""Observe testnet top-of-book for fresh news events without sending orders."""

import argparse
import asyncio
import json
import math
import re
import sys
import time
from contextlib import AsyncExitStack, closing

import aiohttp

from news_event_shadow import HORIZONS, SCHEMA

STREAM = "wss://stream.binancefuture.com/ws"
ENTRY_WINDOW_MS = 15_000
LAG_MS = 5_000
MAX_JOBS = 4


def now_ms():
    return time.time_ns() // 1_000_000


def parse_book(payload, symbol, received_ms):
    if payload.get("s") != symbol:
        return None
    try:
        bid, ask, bid_qty, ask_qty = (float(payload[key]) for key in ("b", "a", "B", "A"))
        exchange_ms = int(payload.get("T") or payload["E"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(n) and n > 0 for n in (bid, ask, bid_qty, ask_qty)) or ask < bid:
        return None
    if not -LAG_MS <= received_ms - exchange_ms <= LAG_MS:
        return None
    return {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
            "exchange_ms": exchange_ms, "received_ms": received_ms}


def markouts(entry, samples):
    return {
        "entry": entry,
        "spread_bps": round(10000 * (entry["ask"] - entry["bid"]) / entry["ask"], 4),
        "horizons": {
            str(seconds): {
                "long_pct": round((quote["bid"] / entry["ask"] - 1) * 100, 6),
                "short_pct": round((1 - quote["ask"] / entry["bid"]) * 100, 6),
            } if (quote := samples.get(seconds)) else None
            for seconds in HORIZONS
        },
    }


async def read_until(stream, symbol, deadline_ms, earliest_ms=0):
    while (remaining := (deadline_ms - now_ms()) / 1000) > 0:
        try:
            message = await asyncio.wait_for(stream.receive(), remaining)
        except TimeoutError:
            return None
        if message.type != aiohttp.WSMsgType.TEXT:
            if message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                return None
            continue
        received = now_ms()
        if received < earliest_ms:
            continue
        try:
            payload = message.json()
        except ValueError:
            continue
        if isinstance(payload, dict):
            quote = parse_book(payload, symbol, received)
            if quote and quote["exchange_ms"] >= earliest_ms:
                return quote
    return None


def claim_jobs(db, now):
    abandoned = now - 1000 * HORIZONS[-1] - ENTRY_WINDOW_MS - LAG_MS
    if db.execute("""SELECT 1 FROM news_event_targets
                     WHERE status='pending' OR (status='monitoring' AND observed_ms < ?) LIMIT 1""",
                  (abandoned,)).fetchone() is None:
        return []
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE news_event_targets SET status='expired' WHERE status='pending' AND observed_ms < ?",
                   (now - ENTRY_WINDOW_MS,))
        db.execute("UPDATE news_event_targets SET status='incomplete' WHERE status='monitoring' AND observed_ms < ?",
                   (abandoned,))
        running = db.execute("SELECT count(*) FROM news_event_targets WHERE status='monitoring'").fetchone()[0]
        jobs = db.execute(
            """SELECT event_key,symbol,observed_ms FROM news_event_targets
               WHERE status='pending' ORDER BY observed_ms LIMIT ?""", (max(0, MAX_JOBS - running),)
        ).fetchall()
        db.executemany("UPDATE news_event_targets SET status='monitoring' WHERE event_key=?", ((job[0],) for job in jobs))
    return jobs


def save_progress(db_module, key, status, data=None):
    with closing(db_module.connect()) as db, db:
        db.execute(
            "UPDATE news_event_targets SET status=?,result_json=? WHERE event_key=? AND status='monitoring'",
            (status, json.dumps(data) if data else None, key),
        )


async def observe(session, proxy, db_module, job):
    key, symbol, observed = job
    entry = None
    samples = {}
    deadline = observed + ENTRY_WINDOW_MS
    remaining = (deadline - now_ms()) / 1000
    try:
        if remaining <= 0:
            save_progress(db_module, key, "expired")
            return
        async with AsyncExitStack() as stack:
            async with asyncio.timeout(remaining):
                stream = await stack.enter_async_context(
                    session.ws_connect(f"{STREAM}/{symbol.lower()}@bookTicker", proxy=proxy, heartbeat=20)
                )
                entry = await read_until(stream, symbol, deadline, observed)
            if entry:
                await track_horizons(stream, db_module, key, symbol, entry, samples)
        if not entry:
            save_progress(db_module, key, "no_quote")
    except (aiohttp.ClientError, TimeoutError, OSError):
        save_progress(db_module, key, "incomplete" if entry else "no_quote", markouts(entry, samples) if entry else None)


async def track_horizons(stream, db_module, key, symbol, entry, samples):
    save_progress(db_module, key, "monitoring", markouts(entry, samples))
    for seconds in HORIZONS:
        target = entry["received_ms"] + seconds * 1000
        samples[seconds] = await read_until(stream, symbol, target + LAG_MS, target)
        save_progress(db_module, key, "monitoring", markouts(entry, samples))
    save_progress(db_module, key, "complete" if all(samples.values()) else "incomplete", markouts(entry, samples))


async def run(args):
    from config import CONFIG

    proxy = CONFIG.get("proxy")
    async with aiohttp.ClientSession() as session:
        if args.probe:
            symbol = args.probe.upper()
            if not re.fullmatch(r"[A-Z0-9]{2,19}USDT", symbol):
                raise ValueError("需要有效的 USDT 合约代码")
            async with asyncio.timeout(12):
                async with session.ws_connect(f"{STREAM}/{symbol.lower()}@bookTicker", proxy=proxy) as stream:
                    quote = await read_until(stream, symbol, now_ms() + 8000)
                    if not quote:
                        raise ValueError("未收到有效盘口")
            return {"symbol": symbol, **quote}
        import trade_db

        with closing(trade_db.connect()) as db:
            db.executescript(SCHEMA)
        if args.observe_pending:
            with closing(trade_db.connect()) as db:
                jobs = claim_jobs(db, now_ms())
            await asyncio.gather(*(observe(session, proxy, trade_db, job) for job in jobs))
            return {"processed": len(jobs)}
        tasks = set()
        while True:
            done = {task for task in tasks if task.done()}
            tasks -= done
            for task in done:
                if error := task.exception():
                    print(f"新闻盘口任务失败：{type(error).__name__}", file=sys.stderr)
            with closing(trade_db.connect()) as db:
                jobs = claim_jobs(db, now_ms())
            tasks.update(asyncio.create_task(observe(session, proxy, trade_db, job)) for job in jobs)
            await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", metavar="SYMBOL", help="只读验证一个公开测试网盘口")
    mode.add_argument("--observe-pending", action="store_true", help="处理当前 SQLite 中排队的新闻观察")
    mode.add_argument("--watch", action="store_true", help="持续处理 SQLite 中新出现的待观察项")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        parser.exit(1, f"盘口观察未完成：{exc}\n")


if __name__ == "__main__":
    main()
