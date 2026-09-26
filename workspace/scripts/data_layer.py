#!/usr/bin/env python3
"""
data_layer.py — 数据层

职责：实时价格流（WebSocket）、K 线技术指标、市场状态识别、情绪数据（恐惧贪婪/资金费率）。
依赖方向：仅依赖 config / aiohttp / requests / numpy，禁止反向依赖。
"""
import asyncio
import json
import logging
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

from config import CONFIG, get_config

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 一、WebSocket 实时价格流
# ═══════════════════════════════════════════════════════════════
class PriceStream:
    """
    Binance WebSocket 合并流，所有币种一个连接。
    每次收到推送立刻更新缓存，主循环直接读缓存，0 REST请求。
    """

    def __init__(self, symbols: list[str], proxy: str = None):
        self.symbols = symbols
        streams = "/".join([f"{s.lower()}usdt@ticker" for s in symbols])
        # ✅ 2026-04-01 老公指示：改用测试网 WebSocket，和交易环境一致
        self.url = f"wss://stream.binancefuture.com/stream?streams={streams}"
        self.proxy = proxy
        self.prices: dict = {}
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                # 使用 aiohttp 支持代理
                connector = aiohttp.TCPConnector(ssl=False) if self.proxy else None
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.ws_connect(
                        self.url,
                        proxy=self.proxy,
                        heartbeat=20,  # aiohttp 使用 heartbeat 代替 ping_interval
                    ) as ws:
                        log.info("WebSocket 价格流已连接 (aiohttp + 代理)")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                t = data.get("data", {})
                                sym = t.get("s", "").replace("USDT", "")
                                if sym in self.symbols:
                                    price = float(t["c"])
                                    open_24h = float(t.get("o") or 0)
                                    chg_pct = float(t.get("P") or 0)
                                    if chg_pct == 0 and open_24h > 0:
                                        chg_pct = (price - open_24h) / open_24h * 100
                                    self.prices[sym] = {
                                        "price":      price,
                                        "open_24h":   open_24h,
                                        "high_24h":   float(t["h"]),
                                        "low_24h":    float(t["l"]),
                                        "volume":     float(t["v"]),
                                        "change_24h": chg_pct,
                                        "ts":         time.time(),
                                    }
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                log.warning(f"WebSocket 错误：{ws.exception()}")
                                # 🔁 WebSocket 断连时，启动现货 API 兜底
                                await self.fallback_to_spot_api()
                                break
            except Exception as e:
                log.warning(f"WebSocket 断线，5 秒后重连：{e}")
                await asyncio.sleep(5)
    def get(self, symbol: str) -> dict:
        return self.prices.get(symbol, {})

    def is_ready(self) -> bool:
        return len(self.prices) == len(self.symbols)

    async def fallback_to_spot_api(self):
        """WebSocket 断连时，用测试网期货 API 兜底获取价格（每30秒一次）"""
        log.warning("⚠️ WebSocket 断连，启动测试网期货 API 兜底...")
        while True:
            for sym in self.symbols:
                try:
                    resp = requests.get(
                        f'https://testnet.binancefuture.com/fapi/v1/ticker/price?symbol={sym}USDT',
                        proxies=CONFIG['proxies'],
                        timeout=5,
                        verify=False
                    )
                    data = resp.json()
                    price = float(data['price'])
                    entry = {
                        'price': price,
                        'ts': time.time(),
                        'source': 'futures_testnet_fallback',
                        'change_24h': 0.0,
                        'volume': 0.0,
                    }
                    try:
                        tr = requests.get(
                            f'https://testnet.binancefuture.com/fapi/v1/ticker/24hr?symbol={sym}USDT',
                            proxies=CONFIG['proxies'],
                            timeout=5,
                            verify=False,
                        )
                        if tr.status_code == 200:
                            t = tr.json()
                            entry['change_24h'] = float(t.get('priceChangePercent', 0))
                            entry['volume'] = float(t.get('volume', 0))
                    except Exception:
                        pass
                    self.prices[sym] = entry
                    log.info(f"✅ {sym} 价格已从测试网期货 API 更新：${price:.6f}")
                except Exception as e:
                    log.error(f"❌ {sym} 测试网期货 API 获取失败：{e}")
            await asyncio.sleep(30)


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
        """获取 K 线，带 5 次重试（测试网期货接口）"""
        for attempt in range(5):
            try:
                r = self.session.get(
                    f"{CONFIG['binance_futures']}/klines",
                    params={"symbol": f"{symbol}USDT", "interval": interval, "limit": limit},
                    timeout=15,
                )
                if r.status_code == 200:
                    return r.json()
                else:
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
            r = self.session.get(
                f"{CONFIG['binance_futures']}/ticker/24hr",
                params={"symbol": f"{symbol}USDT"},
                timeout=15,
            )
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
        if time.time() - self._fr_ts < 300:
            return self._fr_cache
        rates = {}
        for sym in symbols:
            try:
                r = self.session.get(
                    f"{CONFIG['binance_futures']}/fundingRate",
                    params={"symbol": f"{sym}USDT", "limit": 1},
                    timeout=15,  # 2026-03-31 优化：8->15 秒
                )
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
