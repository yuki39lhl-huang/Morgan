"""Discover Binance perpetual anomalies and queue independent testnet entries."""

import asyncio
import json
import logging
import math
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from statistics import median

import aiohttp
import requests

import news_testnet
from news_entry_shadow import entry_terms
from news_event_quotes import STREAM as TESTNET_STREAM, now_ms, read_until

log = logging.getLogger(__name__)
MARKET = "wss://fstream.binance.com/market/ws/!miniTicker@arr"
SCHEDULE = "https://fapi.binance.com/fapi/v1/tradingSchedule"
MINUTE = 60_000
BAR_MS = 5 * MINUTE
ENTRY_WINDOW = 45_000
HORIZONS = (5, 15, 60)
MIN_24H_USDT = 5_000_000
MAX_PER_GROUP = 8
MAX_ANALYSES = 5
RANK_REFRESH_MS = 10_000
RANK_LIMIT = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS market_scan_signals (
    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, detected_ms INTEGER NOT NULL,
    signal_ms INTEGER NOT NULL, setup TEXT NOT NULL, action TEXT, status TEXT NOT NULL,
    reason TEXT, metrics_json TEXT NOT NULL, observed_price REAL NOT NULL,
    markouts_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_market_scan_detected ON market_scan_signals(detected_ms);
CREATE TABLE IF NOT EXISTS market_scan_health (
    id INTEGER PRIMARY KEY CHECK (id=1), checked_ms INTEGER NOT NULL, status TEXT NOT NULL,
    universe INTEGER NOT NULL, subscriptions INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS market_scan_rankings (
    id INTEGER PRIMARY KEY CHECK (id=1), checked_ms INTEGER NOT NULL,
    window_minutes INTEGER NOT NULL, rows_json TEXT NOT NULL
);
"""
PROMPT = (
    "你只评估币安测试网短线异动。输入是已收盘行情摘要。"
    "如果量能和价格支持该方向、不是追高杀低且尚有短线延续空间，输出输入给出的方向。"
    "若放量冲高回落、反弹证据不足或盘口风险难以判断，输出 wait 或 skip。"
    "只输出 JSON 对象，字段 action 为 long、short、wait、skip，reason 为一句简短中文。"
)


def init(db):
    db.executescript(SCHEMA)
    news_testnet.ensure_schema(db)


def contracts_for(catalog):
    return {row["symbol"]: row for row in (catalog or {}).get("symbols", [])
            if row.get("status") == "TRADING" and row.get("quoteAsset") == "USDT"
            and row.get("contractType") in {"PERPETUAL", "TRADIFI_PERPETUAL"}}


def scan_setup(bars):
    if len(bars) != 15 or any(b["time"] - a["time"] != BAR_MS for a, b in zip(bars, bars[1:])):
        return None
    current = bars[-1]
    earlier = bars[:-3]
    base = median(sum(b["quote"] for b in earlier[i:i + 3]) for i in range(0, 12, 3))
    if base <= 0:
        return None
    volume_ratio = sum(b["quote"] for b in bars[-3:]) / base
    move = current["close"] / earlier[-1]["close"] - 1
    if volume_ratio < 1.5 or abs(move) < .015:
        return None
    peak = max(b["high"] for b in earlier)
    trough = min(b["low"] for b in bars[-6:])
    if move > 0 and trough / peak - 1 <= -.02 and current["close"] >= bars[-2]["high"]:
        setup, action = "rebound", "long"
    elif current["close"] > peak:
        setup, action = "breakout", "long"
    elif current["close"] < min(b["low"] for b in earlier):
        setup, action = "breakout", "short"
    else:
        return None
    return {"setup": setup, "direction": action, "move_pct": round(move * 100, 4),
            "volume_ratio": round(volume_ratio, 4), "close": current["close"],
            "last_bar_range_pct": round((current["high"] - current["low"]) / current["close"] * 100, 4)}


def model_result(request, symbol, signal):
    text = request([{"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps({"symbol": symbol, **signal})}],
                   timeout=(3, 7), temperature=0, json_mode=True)
    obj = json.loads(text)
    action, reason = obj["action"], obj["reason"]
    if action not in (signal["direction"], "wait", "skip") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid market scan model response")
    return action, reason.strip()[:240]


def signal_claim(db, key, symbol, stamp, observed, signal):
    with closing(db.connect()) as conn, conn:
        return bool(conn.execute(
            """INSERT INTO market_scan_signals
               (event_key,symbol,detected_ms,signal_ms,setup,status,metrics_json,observed_price)
               VALUES (?,?,?,?,?,'detected',?,?) ON CONFLICT(event_key) DO NOTHING""",
            (key, symbol, observed, stamp, signal["setup"], json.dumps(signal), signal["close"]),
        ).rowcount)


def signal_status(db, key, status, *, action=None, reason=None):
    with closing(db.connect()) as conn, conn:
        conn.execute("""UPDATE market_scan_signals SET status=?,action=COALESCE(?,action),
                     reason=COALESCE(?,reason) WHERE event_key=?""", (status, action, reason, key))


def read_schedule(proxy):
    from rest_backoff import fapi_public_get

    with requests.Session() as session:
        session.trust_env = False
        response = fapi_public_get(
            session, SCHEDULE, "/fapi/v1/tradingSchedule", background=True,
            proxies={"https": proxy} if proxy else None, timeout=(4, 6),
        )
        if response.status_code != 200:
            return None
        body = response.json()
        return body.get("marketSchedules") if isinstance(body, dict) else None


def eligible_tradfi(contract, schedule, now, deadline, config):
    blocks = config.get("market_scan_symbol_blackouts") or {}
    if int(blocks.get(contract["symbol"], 0)) > now:
        return "corporate_action_blackout"
    if contract["contractType"] != "TRADIFI_PERPETUAL":
        return None
    market = contract.get("underlyingType")
    if contract["symbol"] in {"SKHYNIXUSDT", "SAMSUNGUSDT", "HYUNDAIUSDT"}:
        market = "KR_EQUITY"
    sessions = (schedule or {}).get(market, {}).get("sessions", [])
    if not sessions:
        return "schedule_unavailable"
    if not any(row.get("type") == "REGULAR" and row["startTime"] <= now
               and row["endTime"] >= deadline + news_testnet.SCAN_HOLD_SECONDS * 1000 for row in sessions):
        return "market_not_regular"
    return None


def health(db, status, seen, watched):
    with closing(db.connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO market_scan_health VALUES(1,?,?,?,?)",
                     (now_ms(), status, seen, watched))


def store_ranking(db, stamp, window_minutes, rows):
    with closing(db.connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO market_scan_rankings VALUES(1,?,?,?)",
                     (stamp, window_minutes, json.dumps(rows, allow_nan=False)))


def update_markouts(db, ticks, now):
    with closing(db.connect()) as conn, conn:
        rows = conn.execute("""SELECT event_key,symbol,signal_ms,observed_price,markouts_json
                             FROM market_scan_signals WHERE signal_ms>=?""", (now - 61 * MINUTE,))
        for key, symbol, stamp, entry, prior in rows:
            if symbol not in ticks:
                continue
            tick, price = ticks[symbol]
            results = json.loads(prior)
            for horizon in HORIZONS:
                target = stamp + horizon * MINUTE
                if str(horizon) not in results and tick >= target:
                    results[str(horizon)] = (round((price / entry - 1) * 100, 4) if tick < target + 75_000 else None)
            new = json.dumps(results)
            if new != prior:
                conn.execute("UPDATE market_scan_signals SET markouts_json=? WHERE event_key=?", (new, key))


async def evaluate(session, db, key, symbol, signal, stamp, request, proxy):
    try:
        action, reason = await asyncio.wait_for(asyncio.to_thread(model_result, request, symbol, signal), 12)
    except (TimeoutError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
        await asyncio.to_thread(signal_status, db, key, "model_error", reason=type(exc).__name__)
        return
    if action in {"wait", "skip"}:
        await asyncio.to_thread(signal_status, db, key, action, action=action, reason=reason)
        return
    await asyncio.to_thread(signal_status, db, key, "checking_book", action=action, reason=reason)
    deadline = stamp + ENTRY_WINDOW
    if now_ms() >= deadline:
        await asyncio.to_thread(signal_status, db, key, "stale_decision")
        return
    try:
        async with asyncio.timeout(min(7, (deadline - now_ms()) / 1000)):
            async with session.ws_connect(f"{TESTNET_STREAM}/{symbol.lower()}@bookTicker", proxy=proxy) as ws:
                quote = await read_until(ws, symbol, deadline, stamp)
    except (TimeoutError, aiohttp.ClientError, OSError):
        quote = None
    if quote is None:
        await asyncio.to_thread(signal_status, db, key, "no_quote")
        return
    entry, blocked = entry_terms(action, quote)
    if blocked:
        await asyncio.to_thread(signal_status, db, key, blocked)
        return
    if abs(entry["reference"] / signal["close"] - 1) > .005:
        await asyncio.to_thread(signal_status, db, key, "testnet_price_deviation")
        return
    event = {"event_key": key, "stage": "confirmed", "symbol": symbol}
    end = min(deadline, quote["received_ms"] + 25_000)
    queued = await asyncio.to_thread(news_testnet.enqueue, db, event, entry, end, now_ms(),
                                     strategy=news_testnet.SCAN_STRATEGY)
    if not queued:
        await asyncio.to_thread(signal_status, db, key, "expired_or_duplicate")


class Feed:
    def __init__(self):
        self.ticks = {}
        self.liquidity = {}
        self.day_open = {}
        self.history = defaultdict(lambda: deque(maxlen=34))
        self.bars = defaultdict(lambda: deque(maxlen=15))
        self.watched = set()

    def update(self, entries, now):
        for row in entries:
            try:
                symbol, stamp = row["s"], int(row["E"])
                price, value = float(row["c"]), float(row["q"])
            except (KeyError, ValueError, TypeError):
                continue
            if (not symbol.endswith("USDT") or abs(stamp - now) > 8_000
                    or not math.isfinite(price) or price <= 0
                    or not math.isfinite(value) or value < MIN_24H_USDT
                    or stamp <= self.ticks.get(symbol, (0, 0))[0]):
                continue
            self.ticks[symbol] = (stamp, price)
            self.liquidity[symbol] = value
            try:
                opened = float(row["o"])
                if math.isfinite(opened) and opened > 0:
                    self.day_open[symbol] = opened
            except (KeyError, TypeError, ValueError):
                pass
            hist = self.history[symbol]
            if not hist or stamp - hist[-1][0] >= 30_000:
                hist.append((stamp, price))

    def leaderboard(self, contracts, now):
        rows = []
        for symbol, (stamp, price) in self.ticks.items():
            if now - stamp > 15_000:
                continue
            hist = self.history[symbol]

            def change(minutes):
                target = stamp - minutes * MINUTE
                reference = next(((ts, old) for ts, old in reversed(hist) if ts <= target), None)
                if reference and target - reference[0] <= 45_000:
                    return round((price / reference[1] - 1) * 100, 4)
                return None

            opened = self.day_open.get(symbol)
            rows.append({
                "symbol": symbol, "contract_type": contracts.get(symbol, {}).get("contractType"),
                "last_price": price, "change_5m": change(5), "change_15m": change(15),
                "change_24h": round((price / opened - 1) * 100, 4) if opened else None,
                "quote_volume_24h": self.liquidity[symbol],
            })
        field, window = ("change_5m", 5) if any(row["change_5m"] is not None for row in rows) else ("change_24h", 1440)
        rows = [row for row in rows if row[field] is not None]
        rows.sort(key=lambda row: (-abs(row[field]), -row["quote_volume_24h"], row["symbol"]))
        return window, rows[:RANK_LIMIT]


    def pick(self, contracts, now):
        groups = defaultdict(list)
        for symbol, (stamp, price) in self.ticks.items():
            if now - stamp > 15_000:
                continue
            hist = self.history[symbol]
            past = next((old for t, old in hist if 4 * MINUTE <= stamp - t <= 6 * MINUTE), None)
            move = abs(price / past - 1) if past else 0
            # Prewarm the busiest pairs and retain a fixed budget of anomaly candidates.
            group = (contracts.get(symbol) or {}).get("contractType", "PERPETUAL")
            groups[group].append((move, self.liquidity[symbol], symbol))
        selected = set()
        for group in groups.values():
            liquid = sorted(group, key=lambda item: item[1], reverse=True)[:3]
            selected.update(row[-1] for row in liquid)
            remaining = [row for row in group if row not in liquid]
            ranked = sorted(remaining, key=lambda row: (row[0] >= .006,
                            row[-1] in self.watched, row[0], row[1]), reverse=True)
            selected.update(row[-1] for row in ranked[:MAX_PER_GROUP - len(liquid)])
        return selected

    def close_bar(self, payload, now):
        symbol, bar = payload["s"], payload["k"]
        if symbol not in self.watched or bar.get("x") is not True:
            return None
        stamp = int(bar["T"]) + 1
        if not 0 <= now - stamp < ENTRY_WINDOW:
            return None
        try:
            row = {"time": int(bar["t"]), "high": float(bar["h"]), "low": float(bar["l"]),
                   "close": float(bar["c"]), "quote": float(bar["q"])}
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(n) for n in row.values()) or not 0 < row["low"] <= row["close"] <= row["high"] or row["quote"] < 0:
            return None
        hist = self.bars[symbol]
        if hist and row["time"] <= hist[-1]["time"]:
            return None
        if hist and row["time"] - hist[-1]["time"] != BAR_MS:
            hist.clear()
        hist.append(row)
        signal = scan_setup(hist)
        return (symbol, stamp, signal) if signal else None

    async def subscribe(self, ws, watched, *, reconnecting=False):
        current = set() if reconnecting else self.watched
        for request_id, method, symbols in ((1, "UNSUBSCRIBE", current - watched),
                                           (2, "SUBSCRIBE", watched - current)):
            if symbols:
                await ws.send_json({"method": method, "id": request_id,
                                    "params": [f"{symbol.lower()}@kline_5m" for symbol in sorted(symbols)]})
        for symbol in self.watched - watched:
            self.bars.pop(symbol, None)
        self.watched = watched


async def watch(db, get_config, request, session):
    from binance_auto_trade import BASE_URL
    from jin10_collector import CATALOG, read_catalog

    with closing(db.connect()) as conn:
        init(conn)
    backoff = 5
    tasks = set()
    schedule_task = None
    next_schedule = budget_window = analyses = 0
    schedule = None
    feed = Feed()
    try:
        while True:
            cfg = get_config()
            if cfg.get("market_scan_enabled") is not True:
                await asyncio.to_thread(health, db, "disabled", 0, 0)
                await asyncio.sleep(15)
                continue
            proxy = cfg.get("proxy")
            scan_at = rank_at = 0
            reconnecting = True
            contracts = {}
            try:
                async with asyncio.timeout(12):
                    ws = await session.ws_connect(MARKET, proxy=proxy)
                async with ws:
                    while True:
                        msg = await ws.receive(timeout=20)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            raise OSError("market scan websocket closed")
                        value = msg.json()
                        now = now_ms()
                        if isinstance(value, list):
                            feed.update(value, now)
                            backoff = 5
                        elif "code" in value and "id" in value:
                            raise ValueError("market scan subscription rejected")
                        if now >= scan_at:
                            cfg = get_config()
                            if cfg.get("market_scan_enabled") is not True:
                                break
                            contracts = contracts_for(read_catalog(CATALOG, now / 1000))
                            await feed.subscribe(ws, feed.pick(contracts, now), reconnecting=reconnecting)
                            reconnecting = False
                            await asyncio.to_thread(health, db, "live", len(feed.ticks), len(feed.watched))
                            await asyncio.to_thread(update_markouts, db, dict(feed.ticks), now)
                            scan_at = now + MINUTE
                            if schedule_task and schedule_task.done():
                                try:
                                    schedule = schedule_task.result()
                                except (requests.RequestException, TypeError, ValueError):
                                    schedule = None
                                schedule_task = None
                            if now >= next_schedule and schedule_task is None:
                                schedule_task = asyncio.create_task(asyncio.to_thread(read_schedule, proxy))
                                next_schedule = now + 60 * MINUTE
                        if isinstance(value, list) and now >= rank_at:
                            window, ranking = feed.leaderboard(contracts, now)
                            await asyncio.to_thread(store_ranking, db, now, window, ranking)
                            rank_at = now + RANK_REFRESH_MS
                        candidate = feed.close_bar(value, now) if isinstance(value, dict) and value.get("e") == "kline" else None
                        if not candidate:
                            continue
                        symbol, stamp, signal = candidate
                        key = f"market_scan:{symbol}:{stamp}"
                        if not await asyncio.to_thread(signal_claim, db, key, symbol, stamp, now, signal):
                            continue
                        contract = contracts.get(symbol)
                        reason = "no_testnet_contract" if not contract else eligible_tradfi(
                            contract, schedule, now, stamp + ENTRY_WINDOW, cfg)
                        if reason is None and not news_testnet.testnet_enabled(cfg, BASE_URL, news_testnet.SCAN_STRATEGY):
                            reason = "testnet_disabled"
                        minute = now // MINUTE
                        if minute != budget_window:
                            budget_window, analyses = minute, 0
                        if reason is None and (analyses >= MAX_ANALYSES or len(tasks) >= MAX_ANALYSES):
                            reason = "analysis_budget"
                        if reason:
                            await asyncio.to_thread(signal_status, db, key, reason)
                            continue
                        analyses += 1
                        task = asyncio.create_task(evaluate(session, db, key, symbol, signal, stamp, request, proxy))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                        task.add_done_callback(report_task_result)
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                log.warning("Market scan reconnect (%s)", type(exc).__name__)
                await asyncio.to_thread(health, db, "reconnecting", 0, 0)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
    finally:
        pending = list(tasks)
        if schedule_task:
            pending.append(schedule_task)
        for running in pending:
            running.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def report_task_result(task):
    if not task.cancelled() and (exc := task.exception()):
        log.error("Market scan analysis failed (%s)", type(exc).__name__)
