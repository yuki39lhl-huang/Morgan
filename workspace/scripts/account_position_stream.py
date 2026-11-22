"""Use a futures account stream to confirm an empty account without fast REST polling."""

import asyncio
import json
import logging
import math
import time
from urllib.parse import quote, urlsplit

import aiohttp
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from config import get_binance_env
from rest_backoff import CACHE_TTL_SEC, GATE

log = logging.getLogger(__name__)
LISTEN_KEY = "/fapi/v1/listenKey"
FLAT_MAX_AGE = 300
REFRESH_INTERVAL = 240
KEEPALIVE_INTERVAL = 30 * 60
KEEPALIVE_RETRY_DELAY = 15
RETRY_INTERVAL = 30


def snapshot_is_flat(rows):
    """Validate the raw risk response, including positions the strategy considers dust."""
    if not isinstance(rows, list):
        return None
    try:
        amounts = [float(row["positionAmt"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(amount) for amount in amounts):
        return None
    return not any(amounts)


class AccountPositionStream:

    def __init__(self, socket_url, proxy, fetch_risks, *, clock=time.monotonic):
        self.socket_url = socket_url.rstrip("/")
        self.proxy = proxy
        self.fetch_risks = fetch_risks
        self.clock = clock
        self.connected = False
        self._generation = 0
        self._flat_at = None
        self._refresh_not_before = 0
        self._dirty = asyncio.Event()

    def confirms_flat(self):
        return (
            self.connected
            and self._flat_at is not None
            and self.clock() - self._flat_at < FLAT_MAX_AGE
        )

    def _invalidate(self):
        self._generation += 1
        self._flat_at = None
        self._refresh_not_before = self.clock() + CACHE_TTL_SEC + 1
        self._dirty.set()

    def handle_event(self, message):
        event = message.get("e")
        if event == "listenKeyExpired":
            self.connected = False
            self._invalidate()
            return False
        if event == "ACCOUNT_UPDATE":
            account = message.get("a")
            if not isinstance(account, dict) or account.get("P"):
                self._invalidate()
        elif event == "ORDER_TRADE_UPDATE":
            order = message.get("o")
            if not isinstance(order, dict) or order.get("x") == "TRADE":
                self._invalidate()
        return True

    async def _refresh(self):
        sequence = self._generation
        risks = await asyncio.to_thread(self.fetch_risks)
        flat = snapshot_is_flat(risks)
        if self.connected and sequence == self._generation:
            self._flat_at = self.clock() if flat is True else None
        return flat

    async def _refresh_loop(self):
        while self.connected:
            # Let any pre-event cached REST response expire before trusting an empty snapshot.
            while self.clock() < self._refresh_not_before:
                await asyncio.sleep(max(0, self._refresh_not_before - self.clock()))
            self._dirty.clear()
            flat = await self._refresh()
            delay = RETRY_INTERVAL if flat is None else REFRESH_INTERVAL
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _listen_key(self, session, method):
        env = get_binance_env()
        endpoint = urlsplit(env.auto_base)
        if (
            not env.testnet or not env.api_key or endpoint.scheme != "https"
            or endpoint.hostname not in {"testnet.binancefuture.com", "demo-fapi.binance.com"}
        ):
            raise RuntimeError("account stream requires the testnet API key")
        action, _ = GATE.before(method, LISTEN_KEY)
        if action == "stop":
            raise RuntimeError("listenKey gated")
        async with session.request(
            method, env.auto_base.rstrip("/") + LISTEN_KEY,
            headers={"X-MBX-APIKEY": env.api_key}, proxy=self.proxy,
        ) as response:
            try:
                body = await response.json(content_type=None)
            except ValueError:
                body = {}
            GATE.after(method, LISTEN_KEY, None, body, response.headers, response.status)
            if response.status != 200 or not isinstance(body, dict) or body.get("code", 0) not in (0, 200):
                raise RuntimeError("listenKey rejected")
            key = body.get("listenKey")
            if method == "POST" and (not isinstance(key, str) or not key):
                raise RuntimeError("missing listenKey")
            return key

    async def _keepalive(self, session):
        while self.connected:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                await self._listen_key(session, "PUT")
            except (aiohttp.ClientConnectionError, TimeoutError):
                # The previous lease is still valid halfway through its lifetime.
                log.warning("币安账户流续期网络抖动，15 秒后重试一次")
                await asyncio.sleep(KEEPALIVE_RETRY_DELAY)
                await self._listen_key(session, "PUT")

    async def _receive(self, socket):
        async for frame in socket:
            message = json.loads(frame)
            if not isinstance(message, dict) or not self.handle_event(message):
                return

    async def _connect(self, session):
        key = await self._listen_key(session, "POST")
        async with connect(
            self.socket_url + "/" + quote(key, safe=""),
            proxy=self.proxy, open_timeout=10, ping_interval=20, ping_timeout=20, close_timeout=3,
        ) as socket:
            self.connected = True
            self._invalidate()
            log.info("币安测试网账户 WebSocket 已连接")
            tasks = {
                asyncio.create_task(self._receive(socket)),
                asyncio.create_task(self._refresh_loop()),
                asyncio.create_task(self._keepalive(session)),
            }
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self.connected = False
                self._invalidate()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
            while True:
                try:
                    await self._connect(session)
                except (aiohttp.ClientError, WebSocketException, OSError, RuntimeError, ValueError) as exc:
                    # Exceptions may carry the listenKey in their URL.
                    log.warning("币安账户 WebSocket 暂不可用 (%s)，沿用 REST", type(exc).__name__)
                await asyncio.sleep(max(RETRY_INTERVAL, GATE.banned_until - time.time()))
