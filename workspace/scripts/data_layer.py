#!/usr/bin/env python3
"""
data_layer.py — 数据层

职责：实时价格流（WebSocket）、K 线技术指标、市场状态识别、情绪数据（恐惧贪婪/资金费率）。
依赖方向：仅依赖 config / aiohttp / requests / numpy，禁止反向依赖。
"""
import asyncio
import contextlib
import json
import logging
import math
from pathlib import Path
import tempfile
import time

import numpy as np
import requests
from typing import Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp 未安装，运行 pip install aiohttp")

from config import CONFIG, LOG_ROOT

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 一、WebSocket 实时价格流
# ═══════════════════════════════════════════════════════════════
KLINE_KEEP = 120
KLINE_HOUR_MS = 3_600_000
KLINE_SNAPSHOT_PAUSE_SEC = 2
KLINE_CACHE_PATH = LOG_ROOT / "binance_closed_1h_klines.json"
PRICE_FRESH_SEC = 60.0


def rest_kline_from_ws(k: dict) -> list | None:
    """把已收盘的 WS k 线收成 REST /klines 的一行。未收盘返回 None。"""
    if not isinstance(k, dict):
        return None
    closed = k.get("x")
    if closed is not True and closed != "true":
        return None
    try:
        open_ts = int(k["t"])
        close_ts = int(k["T"])
        close_px = float(k["c"])
    except (KeyError, TypeError, ValueError):
        return None
    if close_px <= 0:
        return None
    return [
        open_ts,
        k.get("o", "0"),
        k.get("h", "0"),
        k.get("l", "0"),
        close_px,
        k.get("v", "0"),
        close_ts,
        k.get("q", "0"),
        int(k.get("n") or 0),
        k.get("V", "0"),
        k.get("Q", "0"),
        k.get("B", "0"),
    ]


def latest_closed_1h_open(now_ms: int) -> int:
    return ((now_ms + 1) // KLINE_HOUR_MS - 1) * KLINE_HOUR_MS


def closed_rest_klines(rows, now_ms: int) -> list:
    """丢掉收盘时间还在未来的那根。和 rsi_fade.closed_pairs 同一条线。"""
    out = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        try:
            open_ts, close_ts = int(row[0]), int(row[6])
            close_px = float(row[4])
        except (TypeError, ValueError, OverflowError):
            continue
        if close_ts != open_ts + KLINE_HOUR_MS - 1 or close_ts > now_ms:
            continue
        if not math.isfinite(close_px) or close_px <= 0:
            continue
        out.append(list(row))
    return out


class PriceStream:
    """
    Binance 测试网合并流：ticker 加 1h K 线，所有币种一个连接。
    连接后补齐缓存缺口，之后只收已收盘的 WS 增量。
    断线补一轮价就返回，由 start() 重连，不再死循环打 REST。
    """

    def __init__(
        self, symbols: list[str], proxy: str = None, *, kline_cache_path: Path | None = None,
        observation_symbols: list[str] | None = None,
    ):
        self.symbols = list(symbols)
        self.kline_symbols = list(dict.fromkeys(self.symbols + (observation_symbols or [])))
        streams = "/".join(
            [f"{s.lower()}usdt@ticker" for s in self.symbols]
            + [f"{s.lower()}usdt@kline_1h" for s in self.kline_symbols]
        )
        # 测试网 WebSocket，和交易环境一致
        self.url = f"wss://stream.binancefuture.com/stream?streams={streams}"
        self.proxy = proxy
        self.prices: dict = {}
        self.klines: dict = {}
        self.kline_cache_path = kline_cache_path
        self.kline_market = CONFIG["binance_futures"]
        self._load_kline_cache()
        self._running = False
        self.http = requests.Session()
        self.http.proxies = CONFIG.get("proxies") or {}
        self.http.verify = False

    def _load_kline_cache(self) -> None:
        if self.kline_cache_path is None:
            return
        try:
            payload = json.loads(self.kline_cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("market") != self.kline_market:
                return
            candles = payload.get("candles")
            if not isinstance(candles, dict):
                return
            now_ms = int(time.time() * 1000)
            oldest = latest_closed_1h_open(now_ms) - (KLINE_KEEP - 1) * KLINE_HOUR_MS
            for symbol in self.kline_symbols:
                rows = candles.get(symbol)
                if not isinstance(rows, list):
                    continue
                closed = [
                    row for row in closed_rest_klines(rows, now_ms)
                    if int(row[0]) >= oldest and int(row[0]) % KLINE_HOUR_MS == 0
                ]
                self._merge_closed(symbol, closed, persist=False)
            log.info("已恢复 1h K 线缓存：%s", {sym: len(rows) for sym, rows in self.klines.items()})
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("无法读取 1h K 线缓存：%s", exc)

    def _save_kline_cache(self) -> None:
        path = self.kline_cache_path
        if path is None:
            return
        temp_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, prefix=f".{path.name}.", encoding="utf-8", delete=False,
            ) as temp:
                temp_path = Path(temp.name)
                json.dump({"market": self.kline_market, "candles": self.klines}, temp)
            temp_path.replace(path)
        except (OSError, ValueError) as exc:
            log.warning("无法保存 1h K 线缓存：%s", exc)
        finally:
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    async def start(self):
        self._running = True
        while self._running:
            try:
                connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(
                        self.url,
                        proxy=self.proxy,
                        heartbeat=20,
                    ) as ws:
                        log.info("WebSocket 价格流已连接 (aiohttp + 代理)")
                        snap = asyncio.create_task(self.snapshot_closed_klines())
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    self.handle_ws_message(json.loads(msg.data))
                                elif msg.type == aiohttp.WSMsgType.ERROR:
                                    log.warning(f"WebSocket 错误：{ws.exception()}")
                                    await self.fallback_to_spot_api()
                                    break
                        finally:
                            snap.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await snap
            except Exception as e:
                log.warning(f"WebSocket 断线，5 秒后重连：{e}")
                await asyncio.sleep(5)
            else:
                if self._running:
                    log.warning("WebSocket 结束，5 秒后重连")
                    await asyncio.sleep(5)

    def get(self, symbol: str) -> dict:
        return self.prices.get(symbol, {})

    def is_ready(self) -> bool:
        return len(self.prices) == len(self.symbols)

    def fresh_price(self, symbol: str, now: float | None = None, max_age: float = PRICE_FRESH_SEC):
        """60 秒内更新过的成交价。过期不当作可下单价。"""
        quote = self.prices.get(symbol) or {}
        try:
            px = float(quote.get("price") or 0)
            ts = float(quote.get("ts") or 0)
        except (TypeError, ValueError):
            return None
        if px <= 0 or ts <= 0:
            return None
        now = time.time() if now is None else float(now)
        if now - ts > float(max_age):
            return None
        return px

    def closed_klines(self, symbol: str) -> list:
        return list(self.klines.get(symbol) or [])

    def handle_ws_message(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return
        if payload.get("e") == "kline" or isinstance(payload.get("k"), dict):
            self.ingest_closed_kline(payload.get("k") or {})
            return
        sym = str(payload.get("s") or "").replace("USDT", "")
        if sym not in self.symbols or "c" not in payload:
            return
        try:
            price = float(payload["c"])
        except (TypeError, ValueError):
            return
        open_24h = float(payload.get("o") or 0)
        chg_pct = float(payload.get("P") or 0)
        if chg_pct == 0 and open_24h > 0:
            chg_pct = (price - open_24h) / open_24h * 100
        self.prices[sym] = {
            "price":      price,
            "open_24h":   open_24h,
            "high_24h":   float(payload.get("h") or 0),
            "low_24h":    float(payload.get("l") or 0),
            "volume":     float(payload.get("v") or 0),
            "change_24h": chg_pct,
            "ts":         time.time(),
        }

    def ingest_closed_kline(self, k: dict) -> bool:
        row = rest_kline_from_ws(k)
        if row is None:
            return False
        sym = str(k.get("s") or "").replace("USDT", "")
        if sym not in self.kline_symbols:
            return False
        self._merge_closed(sym, [row])
        return True

    def _merge_closed(self, symbol: str, rows: list, *, persist=True) -> None:
        by_open = {int(r[0]): list(r) for r in self.klines.get(symbol) or [] if len(r) > 6}
        for row in rows:
            by_open[int(row[0])] = list(row)
        merged = [by_open[k] for k in sorted(by_open)]
        if len(merged) > KLINE_KEEP:
            merged = merged[-KLINE_KEEP:]
        if merged != (self.klines.get(symbol) or []):
            self.klines[symbol] = merged
            if persist:
                self._save_kline_cache()

    def snapshot_symbol(self, symbol: str, now_ms: int | None = None) -> str:
        """补当前窗口的第一个连续缺口。partial 让后台任务稍后补下一段。"""
        from rest_backoff import GATE, fapi_public_get, response_limited

        check_ms = int(time.time() * 1000) if now_ms is None else now_ms
        latest = latest_closed_1h_open(check_ms)
        oldest = latest - (KLINE_KEEP - 1) * KLINE_HOUR_MS
        cached = {int(row[0]) for row in self.closed_klines(symbol)}
        missing = [ts for ts in range(oldest, latest + 1, KLINE_HOUR_MS) if ts not in cached]
        if not missing:
            return "cached"
        if float(GATE.clock()) < float(GATE.banned_until or 0):
            return "banned"
        start = end = missing[0]
        for ts in missing[1:]:
            if ts != end + KLINE_HOUR_MS:
                break
            end = ts
        try:
            resp = fapi_public_get(
                self.http,
                f"{self.kline_market}/klines",
                "/fapi/v1/klines",
                params={
                    "symbol": f"{symbol}USDT", "interval": "1h",
                    "startTime": start, "endTime": end + KLINE_HOUR_MS - 1,
                    "limit": (end - start) // KLINE_HOUR_MS + 1,
                },
                timeout=15,
            )
        except Exception as exc:
            log.warning(f"1h K 线快照失败 {symbol}：{exc}")
            return "fail"
        if response_limited(resp):
            log.warning(f"1h K 线快照封禁 {symbol}")
            return "banned"
        if getattr(resp, "status_code", 0) != 200:
            log.warning(f"1h K 线快照 HTTP {getattr(resp, 'status_code', 0)} {symbol}")
            return "fail"
        try:
            rows = resp.json()
        except Exception as exc:
            log.warning(f"1h K 线快照解析失败 {symbol}：{exc}")
            return "fail"
        if not isinstance(rows, list):
            return "fail"
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        closed = closed_rest_klines(rows, now_ms)
        self._merge_closed(symbol, closed)
        log.info("1h K 线快照 %s 已收盘 %d 根", symbol, len(closed))
        if end < missing[-1]:
            received = {int(row[0]) for row in closed}
            if all(ts in received for ts in range(start, end + 1, KLINE_HOUR_MS)):
                return "partial"
        return "ok"

    async def snapshot_closed_klines(self) -> None:
        """分批补缺失 K 线。封禁时暂停，解封后重新检查缺口。"""
        from rest_backoff import GATE

        pending = list(self.kline_symbols)
        while self._running and pending:
            nxt = []
            for i, sym in enumerate(pending):
                status = self.snapshot_symbol(sym)
                while self._running and status == "partial":
                    await asyncio.sleep(KLINE_SNAPSHOT_PAUSE_SEC)
                    status = self.snapshot_symbol(sym)
                if status == "banned":
                    nxt.extend(pending[i:])
                    break
                if status == "cached":
                    continue
                if status != "ok":
                    log.warning("1h K 线快照 %s 未写入，本连接不再重试", sym)
                if i + 1 < len(pending):
                    await asyncio.sleep(KLINE_SNAPSHOT_PAUSE_SEC)
            if not nxt:
                return
            pending = nxt
            wait = float(GATE.banned_until or 0) - float(GATE.clock())
            if wait > 0:
                log.warning("1h K 线快照等解封 %.0f 秒", wait)
            await asyncio.sleep(min(max(wait, KLINE_SNAPSHOT_PAUSE_SEC), 3600))

    async def fallback_to_spot_api(self):
        """断线后最多补一轮价，然后返回，让 start() 去重连。封禁中不打 REST。"""
        from rest_backoff import GATE, fapi_public_get, response_limited

        wait = float(GATE.banned_until or 0) - float(GATE.clock())
        if wait > 0:
            log.warning("REST 封禁中，跳过断线补价 %.0f 秒", wait)
            return
        log.warning("⚠️ WebSocket 断连，补一轮测试网期货价后重连...")
        for sym in self.symbols:
            if float(GATE.clock()) < float(GATE.banned_until or 0):
                log.warning("REST 封禁中，停止补价")
                return
            try:
                resp = fapi_public_get(
                    self.http,
                    f"{CONFIG['binance_futures']}/ticker/price",
                    "/fapi/v1/ticker/price",
                    params={"symbol": f"{sym}USDT"},
                    timeout=5,
                )
                if response_limited(resp):
                    log.warning("行情 HTTP %s，停止补价", getattr(resp, "status_code", 418))
                    return
                data = resp.json()
                price = float(data["price"])
                entry = {
                    "price": price,
                    "ts": time.time(),
                    "source": "futures_testnet_fallback",
                    "change_24h": 0.0,
                    "volume": 0.0,
                }
                try:
                    tr = fapi_public_get(
                        self.http,
                        f"{CONFIG['binance_futures']}/ticker/24hr",
                        "/fapi/v1/ticker/24hr",
                        params={"symbol": f"{sym}USDT"},
                        timeout=5,
                    )
                    if response_limited(tr):
                        self.prices[sym] = entry
                        log.warning("行情 HTTP %s，停止补价", getattr(tr, "status_code", 418))
                        return
                    if tr.status_code == 200:
                        t = tr.json()
                        entry["change_24h"] = float(t.get("priceChangePercent", 0))
                        entry["volume"] = float(t.get("volume", 0))
                except Exception:
                    pass
                self.prices[sym] = entry
                log.info(f"✅ {sym} 价格已从测试网期货 API 更新：${price:.6f}")
            except Exception as e:
                log.error(f"❌ {sym} 测试网期货 API 获取失败：{e}")


# ═══════════════════════════════════════════════════════════════
# 二、技术指标计算
# ═══════════════════════════════════════════════════════════════
class IndicatorEngine:
    """
    从 Binance REST 拉K线，计算 RSI / EMA / ATR / 支撑阻力。
    结果缓存60秒，主循环复用。
    """

    def __init__(self):
        self.cache: dict = {}
        self.cache_ts: dict = {}
        self.session = requests.Session()
        self.session.proxies = CONFIG["proxies"]
        self.session.verify = False  # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误

    def _get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list:
        """获取 K 线。连接错误仍重试；418/-1003 立刻停止，不再连打。"""
        from rest_backoff import GATE, fapi_public_get, response_limited

        if float(GATE.clock()) < float(GATE.banned_until or 0):
            return []
        for attempt in range(5):
            try:
                r = fapi_public_get(
                    self.session,
                    f"{CONFIG['binance_futures']}/klines",
                    "/fapi/v1/klines",
                    params={"symbol": f"{symbol}USDT", "interval": interval, "limit": limit},
                    timeout=15,
                )
                if response_limited(r):
                    log.warning(f"K 线 HTTP {getattr(r, 'status_code', 418)} {symbol}: 封禁，停止重试")
                    return []
                if r.status_code == 200:
                    return r.json()
                log.warning(f"K 线 HTTP {r.status_code} {symbol}: {r.text[:100]}")
            except Exception as e:
                if attempt < 4:
                    log.warning(f"K 线重试 {attempt+1}/5 {symbol}: {e}")
                else:
                    log.error(f"K 线失败 {symbol} (5 次重试耗尽): {e}")
        return []


    def get_24h_klines(self, symbol: str) -> list:
        """Get 24h klines with 3-layer fallback"""
        # 1. Binance 1h klines (60 bars = 24h)
        klines = self._get_klines(symbol, "1h", 60)
        if klines and len(klines) >= 10:
            return klines

        # 2. Binance 测试网期货 24hr Ticker
        try:
            from rest_backoff import fapi_public_get, response_limited

            r = fapi_public_get(
                self.session,
                f"{CONFIG['binance_futures']}/ticker/24hr",
                "/fapi/v1/ticker/24hr",
                params={"symbol": f"{symbol}USDT"},
                timeout=15,
            )
            if response_limited(r):
                log.warning(f"24hr Ticker 封禁 {symbol}")
                return []
            if r.status_code == 200:
                t = r.json()
                now = int(time.time() * 1000)
                # [time, open, high, low, close, volume]
                return [[
                    now - 86400000,
                    float(t['openPrice']),
                    float(t['highPrice']),
                    float(t['lowPrice']),
                    float(t['lastPrice']),
                    float(t['volume'])
                ]]
        except Exception as e:
            log.warning(f"24hr Ticker failed {symbol}: {e}")

        # 3. Return empty (indicator will use 15m as fallback)
        log.error(f"24h klines failed all fallbacks {symbol}")
        return []

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if len(values) < period:
            return values[-1] if values else 0
        k = 2 / (period + 1)
        ema = values[0]
        for v in values[1:]:
            ema = v * k + ema * (1 - k)
        return ema

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(klines: list, period: int = 14) -> float:
        if len(klines) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(klines)):
            h = float(klines[i][2])
            l = float(klines[i][3])
            pc = float(klines[i - 1][4])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return float(np.mean(trs[-period:]))

    def calc(self, symbol: str) -> dict:
        now = time.time()
        if symbol in self.cache and now - self.cache_ts.get(symbol, 0) < 60:
            return self.cache[symbol]

        klines_15m = self._get_klines(symbol, "15m", 100)
        klines_1h  = self.get_24h_klines(symbol)

        if not klines_15m:
            return {}

        closes_15m = [float(k[4]) for k in klines_15m]
        closes_1h  = [float(k[4]) for k in klines_1h] if klines_1h else closes_15m
        highs  = [float(k[2]) for k in klines_15m]
        lows   = [float(k[3]) for k in klines_15m]
        vols   = [float(k[5]) for k in klines_15m]

        atr = self._atr(klines_15m)
        price = closes_15m[-1]

        result = {
            "last_close":  price,
            # RSI
            "rsi":         self._rsi(closes_15m),
            # EMA 15分钟
            "ema20_15m":   self._ema(closes_15m, 20),
            "ema60_15m":   self._ema(closes_15m, 60),
            # EMA 1小时（多时间框架确认）
            "ema20_1h":    self._ema(closes_1h, 20),
            "ema60_1h":    self._ema(closes_1h, 60),
            # 成交量
            "vol_current": vols[-1],
            "vol_avg20":   float(np.mean(vols[-20:])),
            # ATR
            "atr":         atr,
            "atr_pct":     atr / price if price else 0,
            # 支撑阻力（20根K线）
            "resistance":  max(highs[-20:]),
            "support":     min(lows[-20:]),
            # 市场状态原始数据
            "ema_slope":   (closes_15m[-1] - closes_15m[-5]) / closes_15m[-5] if closes_15m[-5] else 0,
        }

        self.cache[symbol] = result
        self.cache_ts[symbol] = now
        return result


# ═══════════════════════════════════════════════════════════════
# 三、市场状态识别（Regime Detection）
# ═══════════════════════════════════════════════════════════════
def detect_regime(ind: dict) -> str:
    """
    trending / ranging / volatile。
    开仓分数门槛见 config.score_threshold（现网约 70/80/90），与本函数无关。
    """
    atr_pct   = ind.get("atr_pct", 0)
    ema_slope = abs(ind.get("ema_slope", 0))

    if atr_pct > 0.05:          # ATR>5% 才是 volatile
        return "volatile"
    elif ema_slope > 0.005:     # EMA 斜率>0.5% 才是 trending（提高门槛）
        return "trending"
    else:
        return "ranging"


# ═══════════════════════════════════════════════════════════════
# 四、情绪数据（恐惧贪婪 + 资金费率）
# ═══════════════════════════════════════════════════════════════
class SentimentEngine:

    def __init__(self):
        self._fg_cache: Optional[int] = None
        self._fr_cache: dict = {}
        self._fg_ts = 0
        self._fr_ts = 0
        self.session = requests.Session()
        self.session.proxies = CONFIG["proxies"]
        self.session.verify = False  # 🐛 Bug 修复：禁用 SSL 验证，防止代理 SSL 错误

    def fear_greed(self) -> int:
        """Alternative.me 恐惧贪婪指数，0-100"""
        if time.time() - self._fg_ts < 300 and self._fg_cache is not None:
            return self._fg_cache
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
            val = int(r.json()["data"][0]["value"])
            self._fg_cache = val
            self._fg_ts = time.time()
            return val
        except Exception as e:
            log.warning(f"恐惧贪婪指数获取失败: {e}")
            return 50  # 中性

    def funding_rates(self, symbols: list[str]) -> dict:
        """Binance资金费率（正=多头付费，偏多情绪）"""
        from rest_backoff import GATE, fapi_public_get, response_limited

        if time.time() - self._fr_ts < 300:
            return self._fr_cache
        if float(GATE.clock()) < float(GATE.banned_until or 0):
            return self._fr_cache
        rates = {}
        for sym in symbols:
            try:
                r = fapi_public_get(
                    self.session,
                    f"{CONFIG['binance_futures']}/fundingRate",
                    "/fapi/v1/fundingRate",
                    params={"symbol": f"{sym}USDT", "limit": 1},
                    timeout=15,  # 2026-03-31 优化：8->15 秒
                )
                if response_limited(r):
                    log.warning(f"资金费率封禁，停止后续请求 {sym}")
                    break
                data = r.json()
                if isinstance(data, list) and data:
                    rates[sym] = float(data[0]["fundingRate"])
            except Exception as e:
                log.warning(f"资金费率获取失败 {sym}: {e}")
                rates[sym] = 0.0
        self._fr_cache = rates
        self._fr_ts = time.time()
        return rates

    def sentiment_score(self, symbol: str, direction: str, fg: int, fr: float) -> int:
        """情绪加分，最多+15分

        2026-06-09 老公指示：趋势跟随代替反向押注
        - 恐惧 → 做空（顺势），贪婪 → 做多（顺势）
        - 资金费率极端 → 反向（拥挤交易反转）
        """
        bonus = 0
        if direction == "LONG":
            if fg > 75:       bonus += 10   # 极度贪婪做多（顺势）
            elif fg > 60:     bonus += 5
            if fr > 0.002:    bonus += 5    # 多头付费（多头主导）→ 做多顺势
        elif direction == "SHORT":
            if fg < 25:       bonus += 10   # 极度恐惧做空（顺势，不接飞刀）
            elif fg < 40:     bonus += 5
            if fr < -0.001:   bonus += 5    # 空头付费（空头主导）→ 做空顺势
        return bonus
