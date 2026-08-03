#!/usr/bin/env python3
"""
crypto_signal_monitor.py
加密货币自动交易策略 v6.0
优化：WebSocket实时价格 + 分层扫描 + 市场状态识别 + 移动止盈 + 熔断保护
"""

import asyncio
import json
import time
import logging
import os
import sys
import requests
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp 未安装，运行 pip install aiohttp")

# 添加脚本目录到路径（须在 openclaw_logging / binance 导入之前）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────
# 日志（归档目录 /root/.openclaw/logs/trading/）
# ─────────────────────────────────────────────
from openclaw_logging import configure_root_logging, daily_alert_path, trading_log_path

LOG_FILE = str(trading_log_path())
configure_root_logging("trading")
log = logging.getLogger(__name__)

# 自动交易模块导入
try:
    from binance_auto_trade import (
        place_order,
        get_all_positions,
        close_all_positions,
        cancel_all_orders,
        cancel_algo_orders,
        place_algo_conditional_order,
        request,
        format_quantity,
    )
    AUTO_TRADE_ENABLED = True
    log.info("✅ 自动交易模块已加载")
except Exception as e:
    AUTO_TRADE_ENABLED = False
    log.warning(f"⚠️ 自动交易模块未加载：{e}")

# 2026-06-09 情绪引擎全局引用（main 中初始化一次，calc_score 直接读取）
SENTIMENT_ENGINE = None

# ─────────────────────────────────────────────
# 配置区
# ─────────────────────────────────────────────
# 配置统一外置：config.json + secrets.json（改参数无需改代码）
from config import get_config

CONFIG = get_config()

# AI 浮亏触发冷却（放在主循环外面）
ai_loss_cooldown = {}  # symbol → 上次触发时间

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
                        proxies={'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'},
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
                            proxies={'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'},
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
        self.session.proxies = {"https": CONFIG["proxy"], "http": CONFIG["proxy"]}
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
    trending  趋势市 → 动量策略，门槛45
    ranging   震荡市 → 反转策略，门槛55
    volatile  高波动 → 保守策略，门槛62 + 减仓
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
        self.session.proxies = {"https": CONFIG["proxy"], "http": CONFIG["proxy"]}
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


# ═══════════════════════════════════════════════════════════════
# 五、信号评分系统 v6.0
# ═══════════════════════════════════════════════════════════════
def calc_score(
    symbol: str,
    price_data: dict,
    ind: dict,
    regime: str,
    fg: int,
    funding_rate: float,
    btc_ind: dict,  # 2026-03-28 老公指示：用 BTC 作为大盘参考
) -> tuple[int, str]:
    """
    返回 (score, direction)
    direction: 'LONG' | 'SHORT' | 'NONE'
    """
    if not ind or not price_data:
        return 0, "NONE"

    price      = float(price_data.get("price") or ind.get("last_close") or 0)
    # 成交量：优先 15m K 线 vol_current（与 vol_avg20 同量纲），避免 WS 24h 量纲不一致
    vol        = float(ind.get("vol_current") or price_data.get("volume") or 0)
    vol_avg    = ind["vol_avg20"]
    atr_pct    = ind["atr_pct"]
    resistance = ind["resistance"]
    support    = ind["support"]

    long_score  = 0
    short_score = 0

    # ① 价格突破（40 分）
    if price >= resistance * 0.999:
        long_score += 40
    if price <= support * 1.001:
        short_score += 40

    # ② 成交量放大确认（30 分）
    if vol > vol_avg * 1.5:
        long_score  += 30
        short_score += 30
    elif vol > vol_avg * 1.2:
        long_score  += 15
        short_score += 15

    # ③ ATR 波动率适中（20 分）
    if 0.005 < atr_pct < 0.03:
        long_score  += 20
        short_score += 20

    # ④ BTC 大盘方向（10 分）- 2026-03-28 老公指示：用 BTC 代替 ETH
    _btc = btc_ind if btc_ind else {}
    if _btc:
        btc_bull = _btc.get("ema20_15m", 0) > _btc.get("ema60_15m", 0)
        if btc_bull:
            long_score  += 10
            short_score -= 5
        else:
            short_score += 10
            long_score  -= 5

    long_score  = max(0, long_score)
    short_score = max(0, short_score)

    # ⑤ 情绪加分（最多+15分）- 2026-06-09 老公指示：接入情绪引擎
    # 恐惧→做空顺势，贪婪→做多顺势；资金费率极端→反向
    if SENTIMENT_ENGINE:
        long_score  += SENTIMENT_ENGINE.sentiment_score(symbol, "LONG", fg, funding_rate)
        short_score += SENTIMENT_ENGINE.sentiment_score(symbol, "SHORT", fg, funding_rate)

    threshold = CONFIG["score_threshold"][regime]
    # 单币阈值加成：低胜率币种提高门槛
    threshold += CONFIG["score_threshold"].get("per_symbol_bonus", {}).get(symbol, 0)
    if long_score > short_score and long_score >= threshold:
        return long_score, "LONG"
    elif short_score > long_score and short_score >= threshold:
        return short_score, "SHORT"

    return max(long_score, short_score), "NONE"


def calc_tp_sl(
    entry: float,
    direction: str,
    ind: dict,
    price_data: dict,
) -> dict:
    """
    返回 {tp1, sl}

    2026-06-03 老公指示：止损改为 ATR 动态 + 2% 保底
    - 止盈：+4%（TP1 全平 100%）
    - 止损：max(2%, ATR% × 分层倍数)，防止止损价被行情轻易打穿
      atr_sl_tiers: <2%ATR→1.0x, <4%→1.5x, ≥4%→2.0x
    - 无 TP2（已废弃）
    """
    TAKE_PROFIT_PCT = 0.04  # 保底 4%（2026-03-30 老公指示）
    TP_MAX_PCT      = 0.15  # 止盈上限 15%
    MIN_SL_PCT      = 0.02  # 保底 2%
    MAX_SL_PCT      = 0.15  # 2026-06-06: 止损上限 15%，防止极端 ATR 下止损失控

    # ── ATR 动态止盈止损 ──
    atr_pct = ind.get("atr_pct", 0) if ind else 0

    # 2026-06-06 老公指示：止盈 ATR 动态，崩盘时自动放宽
    tp_pct = TAKE_PROFIT_PCT
    if atr_pct > 0:
        tp_pct = max(TAKE_PROFIT_PCT, min(atr_pct * 2.0, TP_MAX_PCT))

    # ATR 动态止损距离
    atr_mult = 1.0  # 默认 1 倍
    if atr_pct > 0:
        atr_tiers = CONFIG.get("atr_sl_tiers", [(0.02, 1.0), (0.04, 1.5), (9999, 2.0)])
        for threshold, mult in atr_tiers:
            if atr_pct < threshold:
                atr_mult = mult
                break
    sl_pct = max(MIN_SL_PCT, min(atr_pct * atr_mult, MAX_SL_PCT))

    if direction == "LONG":
        tp1 = entry * (1 + tp_pct)
        sl  = entry * (1 - sl_pct)
    else:
        tp1 = entry * (1 - tp_pct)
        sl  = entry * (1 + sl_pct)

    return {
        "tp1_price": round(tp1, 4),
        "sl_price":  round(sl, 4),
        "peak_pnl":  0.0,        # 移动止盈用
        "tp_pct":    round(tp_pct, 4),  # 2026-06-06: 记录动态止盈比例
    }


# ═══════════════════════════════════════════════════════════════
# 七、止盈止损检查（15秒必跑，极轻量）
# ═══════════════════════════════════════════════════════════════
def check_exits_fast(positions: list, prices: dict) -> list[tuple]:
    """
    返回 [(position, reason, close_size_ratio)]
    纯价格比较，无任何计算，<5ms
    
    2026-03-28 老公指示：TP1 全平，无 TP2
    """
    exits = []
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        tp1 = pos["tp1_price"]
        sl  = pos["sl_price"]
        
        # 跳过止盈止损为 0 的无效持仓
        if tp1 == 0.0 or sl == 0.0:
            log.warning(f"⚠️ {sym} 止盈止损无效 (TP1={tp1}, SL={sl})，跳过检查")
            continue
        
        typ = pos["type"]

        if typ == "LONG":
            if p >= tp1:
                exits.append((pos, "TP1", 1.0))  # 全平
            elif p <= sl:
                exits.append((pos, "SL", 1.0))   # 全平
        else:  # SHORT
            if p <= tp1:
                exits.append((pos, "TP1", 1.0))  # 全平
            elif p >= sl:
                exits.append((pos, "SL", 1.0))   # 全平

    return exits


def update_trailing_stop(positions: list, prices: dict):
    """
    移动止盈 - 2026-06-06 老公指示：改为相对止盈比例（ATR 动态止盈联动）
    三档基于实际止盈比例动态计算：
    - 50%止盈进度 → 回撤=实际止盈×20%
    - 75%止盈进度 → 回撤=实际止盈×35%
    - 100%止盈 → 全平
    
    2026-03-31 老公指示：每次更新 sl_price 后同步到 Binance（取消旧单 + 提交新单）
    """
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        entry = pos["entry_price"]
        typ   = pos["type"]
        tp1   = pos.get("tp1_price", 0)

        # 反推止盈比例（兼容旧数据无 tp_pct）
        tp_pct = pos.get("tp_pct", 0)
        if tp_pct == 0 and entry > 0 and tp1 > 0:
            tp_pct = abs(tp1 - entry) / entry
        
        if tp_pct == 0:
            continue

        pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry

        # 更新峰值盈亏 和 峰值价格
        if pnl_pct > pos.get("peak_pnl", 0):
            pos["peak_pnl"] = pnl_pct
            pos["peak_price"] = p  # ✅ 记录峰值价格

        # 找到当前利润对应的档位（取最高档）
        active_tier = None
        peak_pnl = pos.get("peak_pnl", 0)
        for tier in CONFIG["trailing_tiers"]:
            if peak_pnl >= tp_pct * tier["pnl_ratio"]:
                active_tier = tier
            else:
                break

        if not active_tier:
            continue

        # 第3档 - 全平
        if active_tier.get("gap", 0) >= 0.9:
            old_sl = pos["sl_price"]
            pos["sl_price"] = p * 0.999 if typ == "LONG" else p * 1.001
            sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))
            continue
        
        # 前两档 - 按比例回撤
        gap = tp_pct * active_tier["gap_ratio"]
        peak_p = pos.get("peak_price", entry)
        
        if typ == "LONG":
            new_sl = peak_p * (1 - gap)
            if new_sl > pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))
        else:
            new_sl = peak_p * (1 + gap)
            if new_sl < pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl, pos.get("tp1_price", 0), qty=abs(pos.get("amount", 0)))

# ATR 动态止损冷却跟踪
_atr_sl_last_update: dict[str, float] = {}  # symbol → 上次更新时间戳
_ATR_SL_COOLDOWN = 300  # 5 分钟冷却，防止频繁取消/重建 Algo 单


def update_atr_dynamic_stops(positions: list, indicators: dict) -> bool:
    """
    ATR 动态止损 — 持仓期间实时跟新。
    用当前 ATR 重算止损距离，只在未进入移动止盈档位且冷却期外时生效。
    
    2026-06-16 老公指示：ATR 倍率（4%-15%）应该在持仓期也动态跑，
    而不只是开仓时算一次。行情波动大了自动放宽，小了自动收紧。
    同次修改：加 5 分钟冷却 + 仅层级变化时更新，防止过度调用 Binance。
    """
    global _atr_sl_last_update
    changed = False
    now_ts = time.time()
    
    for pos in positions:
        sym = pos["symbol"]
        ind = indicators.get(sym)
        if not ind:
            continue
        
        atr_pct = ind.get("atr_pct", 0)
        if atr_pct <= 0:
            continue
        
        entry = pos["entry_price"]
        typ = pos["type"]
        tp_pct = pos.get("tp_pct", 0.04)
        
        # 已进入移动止盈档位 → 三档跟止损接管，ATR 不干预
        peak_pnl = pos.get("peak_pnl", 0)
        first_tier_threshold = tp_pct * CONFIG["trailing_tiers"][0]["pnl_ratio"]
        if peak_pnl >= first_tier_threshold:
            continue
        
        # 冷却期内跳过
        last_upd = _atr_sl_last_update.get(sym, 0)
        if now_ts - last_upd < _ATR_SL_COOLDOWN:
            continue
        
        # ATR 分层倍率（与 calc_tp_sl 一致）
        atr_mult = 1.0
        atr_tier = 0  # 0=<2%, 1=<4%, 2=≥4%
        tiers = CONFIG["atr_sl_tiers"]
        for i, (threshold, mult) in enumerate(tiers):
            if atr_pct < threshold:
                atr_mult = mult
                atr_tier = i
                break
        
        sl_pct = max(0.02, min(atr_pct * atr_mult, 0.15))
        
        if typ == "LONG":
            atr_sl = round(entry * (1 - sl_pct), 4)
        else:  # SHORT
            atr_sl = round(entry * (1 + sl_pct), 4)
        
        # 只有止损价格变化超过 0.1% 才动手（过滤噪音，也说明层级真变了）
        sl_change = abs(atr_sl - pos["sl_price"]) / pos["sl_price"] if pos["sl_price"] else 0
        if sl_change < 0.001:
            continue
        
        old_sl = pos["sl_price"]
        pos["sl_price"] = atr_sl
        sync_stop_loss_to_binance(sym, typ, atr_sl, old_sl, pos.get("tp1_price", 0),
                                  qty=abs(pos.get("amount", 0)))
        _atr_sl_last_update[sym] = now_ts
        log.info(f"📐 {sym} ATR动态止损{tiers[atr_tier][0]*100:.0f}%档(atr={atr_pct*100:.2f}%): "
                 f"{old_sl:.4f}→{atr_sl:.4f} (变化{sl_change*100:.1f}%)")
        changed = True
    
    return changed


def _cancel_algo_by_type(symbol_usdt: str, order_type: str):
    """取消某币种指定类型的 Algo 单（不影响另一边）"""
    try:
        result = request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol_usdt})
        if isinstance(result, list):
            for o in result:
                if o.get('orderType') == order_type and o.get('algoStatus') == 'NEW':
                    algo_id = o.get('algoId')
                    if algo_id:
                        request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
    except Exception as e:
        log.warning(f"⚠️ 按类型取消 Algo 单失败 ({order_type})：{e}")

def sync_stop_loss_to_binance(symbol: str, typ: str, new_sl: float, old_sl: float, tp_price: float = 0, qty: float = 0):
    """
    2026-06-03 修复：精准替换止损/止盈，不碰另一边
    原逻辑 cancel_all → 止损失败 → 止盈也被删 → 永远缺一边
    2026-06-04 修复：传入精确 qty 代替 closePosition=true（测试网算错数量）
    """
    try:
        sym_usdt = f"{symbol}USDT"
        sl_side = "SELL" if typ == "LONG" else "BUY"
        
        # 只取消止损单，不动止盈
        _cancel_algo_by_type(sym_usdt, "STOP_MARKET")
        sl_result = place_algo_conditional_order(
            sym_usdt, sl_side, "STOP_MARKET", new_sl, quantity=qty
        )

        if isinstance(sl_result, dict) and sl_result.get("algoId"):
            log.info(f"📌 {symbol} 止损 Algo 单已更新：{old_sl:.4f} → {new_sl:.4f}")
        else:
            log.warning(f"⚠️ {symbol} 更新 Binance 止损单失败：{sl_result}")

        # 同时更新止盈（如果有变更，只取消止盈不动止损）
        if tp_price > 0:
            tp_side = "SELL" if typ == "LONG" else "BUY"
            _cancel_algo_by_type(sym_usdt, "TAKE_PROFIT_MARKET")
            tp_result = place_algo_conditional_order(
                sym_usdt, tp_side, "TAKE_PROFIT_MARKET", tp_price, quantity=qty
            )
            if isinstance(tp_result, dict) and tp_result.get("algoId"):
                log.info(f"📌 {symbol} 止盈 Algo 单已恢复：{tp_price}")
            else:
                log.warning(f"⚠️ {symbol} 恢复 Binance 止盈单失败：{tp_result}")
    except Exception as e:
        log.warning(f"⚠️ {symbol} 同步止损/止盈单异常：{e}")

def submit_initial_algo_orders(pos: dict):
    """
    为从 API 同步/方向反转后的持仓提交初始止盈止损 Algo 单。

    2026-06-05 修复：方向反转 / 初始同步场景先全量清理该币种所有 Algo 单，
    再重新提交正确的 TP+SL。防止旧方向的条件单残留（如 LONG 的 SELL TP
    在反转成 SHORT 后没删干净）。
    """
    if not AUTO_TRADE_ENABLED:
        return
    sym = pos["symbol"]
    try:
        typ = pos["type"]
        sl_price = pos.get("sl_price")
        tp_price = pos.get("tp1_price")
        if not sl_price or not tp_price:
            return
        
        qty = abs(pos.get("amount", 0))
        sym_usdt = f"{sym}USDT"

        # 🔧 2026-06-05 修复：全量清理，不留任何旧单
        cancel_algo_orders(sym_usdt)

        # 提交止损
        sl_side = "SELL" if typ == "LONG" else "BUY"
        sl_result = place_algo_conditional_order(sym_usdt, sl_side, "STOP_MARKET", sl_price, quantity=qty)
        if isinstance(sl_result, dict) and sl_result.get("algoId"):
            log.info(f"📌 {sym} 止损 Algo 单已提交：{sl_price} algoId={sl_result.get('algoId')}")
        else:
            log.warning(f"⚠️ {sym} 止损 Algo 单提交失败：{sl_result}")

        # 提交止盈
        tp_side = "SELL" if typ == "LONG" else "BUY"
        tp_result = place_algo_conditional_order(sym_usdt, tp_side, "TAKE_PROFIT_MARKET", tp_price, quantity=qty)
        if isinstance(tp_result, dict) and tp_result.get("algoId"):
            log.info(f"📌 {sym} 止盈 Algo 单已提交：{tp_price} algoId={tp_result.get('algoId')}")
        else:
            log.warning(f"⚠️ {sym} 止盈 Algo 单提交失败：{tp_result}")
    except Exception as e:
        log.warning(f"⚠️ {sym} 提交初始 Algo 单异常：{e}")

# ═══════════════════════════════════════════════════════════════
# 八、熔断器
# ═══════════════════════════════════════════════════════════════
class CircuitBreaker:

    def __init__(self):
        self.daily_pnl   = 0.0
        self.reset_date  = datetime.now().date()
        self.paused_until: Optional[datetime] = None

    def record_trade(self, pnl: float):
        today = datetime.now().date()
        if today != self.reset_date:
            self.daily_pnl = 0.0
            self.reset_date = today
            self.consecutive_losses = 0  # 每日重置连亏计数
        self.daily_pnl += pnl

        # 连亏计数
        if pnl < 0:
            self.consecutive_losses = getattr(self, 'consecutive_losses', 0) + 1
            if self.consecutive_losses >= 3:
                self.paused_until = datetime.now() + timedelta(hours=2)
                log.warning(f"⚠️ 连亏{self.consecutive_losses}次，暂停 2 小时")
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0  # 盈利重置

    def is_trading_allowed(self) -> tuple[bool, str]:
        if not CONFIG.get("circuit_breaker_enabled", True):
            return True, "ok (熔断已关闭)"
        # 检查暂停
        if self.paused_until and datetime.now() < self.paused_until:
            remain = int((self.paused_until - datetime.now()).total_seconds() / 60)
            return False, f"熔断中，还需 {remain} 分钟"

        # 检查日亏损
        loss_pct = self.daily_pnl / CONFIG["total_capital"]
        if loss_pct < CONFIG["circuit_breaker_pct"]:
            self.paused_until = datetime.now() + timedelta(hours=CONFIG["circuit_breaker_hours"])
            msg = f"⚠️ 熔断！日亏损 {loss_pct:.1%}，暂停 {CONFIG['circuit_breaker_hours']} 小时"
            log.warning(msg)
            write_alert(msg)
            return False, msg

        return True, "ok"


# ═══════════════════════════════════════════════════════════════
# 九、信号冷却
# ═══════════════════════════════════════════════════════════════
class CooldownManager:

    def __init__(self):
        self._last: dict = {}   # key → timestamp
        self._push_buffer: dict = {}  # symbol → {signals: [], last_time: 0}

    def can_open(self, symbol: str, direction: str) -> bool:
        now = time.time()
        same_key = f"{direction}_{symbol}"
        opp_dir  = "SHORT" if direction == "LONG" else "LONG"
        opp_key  = f"{opp_dir}_{symbol}"

        # 同向冷却（优先用单币覆盖，否则默认）
        cooldown_sec = CONFIG["signal_cooldown"].get("per_symbol", {}).get(
            symbol, CONFIG["signal_cooldown"]["same_direction"]
        )
        elapsed = now - self._last.get(same_key, 0)
        if elapsed < cooldown_sec:
            log.info(f"⏳ {symbol} {direction} 冷却中，还需 {int(cooldown_sec - elapsed)}秒 (冷却={cooldown_sec}s)")
            return False

        self._last.pop(opp_key, None)
        return True

    def record(self, symbol: str, direction: str):
        self._last[f"{direction}_{symbol}"] = time.time()

    def reset_after_close(self, symbol: str):
        """平仓后清除该币种所有方向的冷却（让下次开仓不被卡）"""
        self._last.pop(f"LONG_{symbol}", None)
        self._last.pop(f"SHORT_{symbol}", None)

    def add_push_signal(self, symbol: str, signal: dict):
        """添加推送信号到缓冲区"""
        now = time.time()
        if symbol not in self._push_buffer:
            self._push_buffer[symbol] = {'signals': [], 'last_time': now}
        
        self._push_buffer[symbol]['signals'].append(signal)
        self._push_buffer[symbol]['last_time'] = now

    def flush_push_buffer(self) -> list:
        """刷新推送缓冲，合并同一币种的信号"""
        now = time.time()
        merged_signals = []
        
        for symbol, buffer in self._push_buffer.items():
            if not buffer['signals']:
                continue
            
            # 如果距离上次推送超过 5 分钟，立即发送
            if now - buffer['last_time'] > 300:
                # 合并所有信号
                merged = self._merge_signals(symbol, buffer['signals'])
                merged_signals.append(merged)
                buffer['signals'] = []
                buffer['last_time'] = now  # ✅ 修复：重置 last_time，防止循环推送
        
        return merged_signals

    def _merge_signals(self, symbol: str, signals: list) -> dict:
        """合并多个信号为一个"""
        if len(signals) == 1:
            return signals[0]
        
        # 分离开仓和平仓信号
        opens = [s for s in signals if s.get('action') in ['开多', '开空']]
        closes = [s for s in signals if s.get('action') in ['止盈', '止损', '平仓']]
        
        # 优先显示开仓，其次平仓
        if opens:
            latest_open = opens[-1]
            total_pnl = sum(s.get('pnl', 0) for s in closes)
            if total_pnl != 0:
                latest_open['merged_pnl'] = total_pnl
                latest_open['merged_close_count'] = len(closes)
            return latest_open
        elif closes:
            # 合并所有平仓
            total_pnl = sum(s.get('pnl', 0) for s in closes)
            latest_close = closes[-1].copy()
            latest_close['pnl'] = total_pnl
            latest_close['merged_count'] = len(closes)
            return latest_close
        
        return signals[-1]


# ═══════════════════════════════════════════════════════════════
# 十、AI 预测（DeepSeek，无调用次数/间隔限制）
# ═══════════════════════════════════════════════════════════════
class AIPredictor:

    def predict(self, symbol: str, direction: str, ind: dict, price_data: dict, fg: int = 50) -> Optional[dict]:
        prompt = f"""你是加密货币分析师，基于以下数据预测{symbol}未来 1 小时趋势：

【实时数据】
当前价格：{price_data.get('price')}
24h 涨跌：{price_data.get('change_24h')}%
恐惧贪婪指数：{fg}

【技术指标】
RSI：{ind.get('rsi', 0):.1f}
ATR%：{ind.get('atr_pct', 0):.3f}
价格 vs 支撑位：{((price_data.get('price',0)-ind.get('support',1))/ind.get('support',1)*100):.2f}%
价格 vs 阻力位：{((price_data.get('price',0)-ind.get('resistance',1))/ind.get('resistance',1)*100):.2f}%

只返回 JSON，不要任何解释：
{{"direction":"做多/做空/震荡","confidence":0,"reason":"一句话"}}"""

        try:
            headers = {
                "Authorization": f"Bearer {CONFIG['deepseek_api_key']}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": CONFIG["deepseek_model"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "thinking": {"type": "disabled"},
            }
            r = requests.post(
                CONFIG["deepseek_url"],
                headers=headers,
                json=payload,
                timeout=20,
                proxies={}  # DeepSeek 国内直连，不需要代理
            )
            content = r.json()["choices"][0]["message"]["content"].strip()
            # 提取 JSON
            import re
            match = re.search(r'\{.*?\}', content, re.DOTALL)
            if match:
                result = json.loads(match.group())
                log.info(f"🤖 AI 预测 {symbol}: {result}")
                return result
        except Exception as e:
            log.warning(f"⚠️ AI 预测失败 {symbol}: {e}")
        return None

def position_amount(pos: dict) -> float:
    """持仓数量（兼容 qty / amount，API 同步只写 qty 时整点汇报曾显示 0 盈亏）"""
    for key in ("amount", "qty"):
        try:
            v = abs(float(pos.get(key) or 0))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def normalize_position(pos: dict) -> dict:
    """统一 amount/qty，避免整点汇报盈亏为 0"""
    amt = position_amount(pos)
    if amt > 0:
        if pos.get("amount") != amt or pos.get("qty") != amt:
            sym = pos.get("symbol", "?")
            log.info(f"🔧 持仓 {sym} 数量归一：qty={pos.get('qty')} amount={pos.get('amount')} → {amt}")
        pos["amount"] = amt
        pos["qty"] = amt
    return pos


def load_positions() -> list:
    """加载持仓并补全缺失字段（兼容旧版本）"""
    try:
        with open(CONFIG["positions_file"]) as f:
            positions = json.load(f)
        
        # 补全缺失字段（兼容旧版本持仓文件）
        for pos in positions:
            if "peak_pnl" not in pos:
                pos["peak_pnl"] = 0.0
            if "high_24h" not in pos:
                pos["high_24h"] = 0.0
            if "low_24h" not in pos:
                pos["low_24h"] = 0.0
            normalize_position(pos)
            # 2026-03-28 老公指示：已删除 tp1_hit, size_remaining
        
        return positions
    except Exception as e:
        log.warning(f"⚠️ 加载持仓失败：{e}")
        return []


def save_positions(positions: list):
    for pos in positions:
        normalize_position(pos)
    with open(CONFIG["positions_file"], "w") as f:
        json.dump(positions, f, indent=2, ensure_ascii=False)



def open_position(
    symbol: str,
    direction: str,
    entry_price: float,
    score: int,
    tp_sl: dict,
    regime: str,
    ai_result: Optional[dict] = None,
) -> dict:
    # 2026-04-27 老公指示：下单前最后一道闸门 —— Binance API 实时确认无同币种持仓
    # 防止多进程/race condition 重复开仓（之前 12 秒内开 2 次 XRP / SSL EOF 后又开一次 BNB 都是这个 bug）
    try:
        _api_pos = get_all_positions() or []
        if any(p.get("symbol") == symbol 
               and float(p.get("amount", 0)) != 0
               and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0 
               for p in _api_pos):
            log.warning(f"🚫 {symbol} 下单闸门：API 已有持仓，拒绝重复下单")
            return {
                "symbol": symbol, "type": direction, "entry_price": entry_price,
                "qty": 0, "amount": 0, "score": score, "regime": regime,
                "ai_result": ai_result,
                "order_result": {"success": False, "message": "API 已有持仓，拒绝重复下单"},
                "entry_time": datetime.now().isoformat(),
                "high_24h": 0.0, "low_24h": 0.0, "peak_pnl": 0.0,
                **tp_sl,
            }
    except Exception as _e:
        log.warning(f"⚠️ 下单闸门查持仓异常：{_e}（继续走流程）")

    # 🔒 防御：价格无效（0 或异常大）时拒绝下单
    if not entry_price or entry_price <= 0 or entry_price > 1_000_000:
        log.error(f"🚫 {symbol} 价格异常 entry_price={entry_price}，拒绝开仓")
        return {
            "symbol": symbol, "type": direction, "entry_price": entry_price,
            "qty": 0, "amount": 0, "score": score, "regime": regime,
            "ai_result": ai_result,
            "order_result": {"success": False, "message": f"价格异常 entry_price={entry_price}"},
            "entry_time": datetime.now().isoformat(),
            "high_24h": 0.0, "low_24h": 0.0, "peak_pnl": 0.0,
            **tp_sl,
        }

    size_pct = CONFIG["position_size_pct"]
    if regime == "volatile":
        size_pct *= 0.6

    # 计算数量并格式化到正确精度
    raw_qty = (CONFIG["total_capital"] * size_pct * 10) / entry_price  # 2026-03-28 老公指示：加 10x 杠杆
    qty = format_quantity(symbol, raw_qty, entry_price)

    # 实盘交易调用
    order_result = None
    if AUTO_TRADE_ENABLED:
        try:
            # 调用 binance_auto_trade 模块下单
            side = "BUY" if direction == "LONG" else "SELL"
            order_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                quantity=round(qty, 6),
                leverage=10,  # 2026-03-27 老公指示：3x→10x 测试两天
                tp_price=tp_sl.get("tp1_price"),
                sl_price=tp_sl.get("sl_price"),
                price=entry_price,
                reduce_only=False,
            )
            log.info(f"📝 下单结果：{order_result}")
        except Exception as e:
            log.error(f"❌ 下单失败：{e}")
            order_result = {"error": str(e)}

    pos = {
        "symbol":       symbol,
        "type":         direction,
        "entry_price":  entry_price,
        "entry_time":   datetime.now().isoformat(),
        "qty":          round(qty, 6),
        "amount":       round(qty, 6),  # 与 get_all_positions 返回格式一致，用于盈亏计算
        "score":        score,
        "regime":       regime,
        "ai_result":    ai_result,
        "order_result": order_result,
        "high_24h":     0.0,
        "low_24h":      0.0,
        "peak_pnl":     0.0,
        **tp_sl,
    }

    # 检查订单是否真正成功（默认 False，强制要求 place_order 显式返回 success=True）
    order_success = False
    if isinstance(order_result, dict):
        order_success = bool(order_result.get('success', False))
    
    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    if not order_success:
        err_msg = order_result.get('message', '未知') if isinstance(order_result, dict) else str(order_result)
        msg += f" | ❌ 下单失败：{err_msg}"
        # 立即推送下单失败告警到飞书（不走缓冲，老公需要立刻知道）
        try:
            push_feishu_card(
                f"🚨 {symbol} 下单失败 - 信号未执行",
                [
                    {"tag": "div", "text": {"tag": "lark_md", "content":
                        f"**币种：** {symbol}\n"
                        f"**方向：** {'开多' if direction == 'LONG' else '开空'}\n"
                        f"**信号价：** ${entry_price:,.4f}\n"
                        f"**评分：** {score}/100\n"
                        f"**失败原因：** `{err_msg}`\n\n"
                        f"⚠️ **币安实际未下单**，请检查 API key / 余额 / 网络"
                    }},
                    {"tag": "note", "elements": [{"tag": "plain_text",
                        "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"}]},
                ],
                "red"
            )
        except Exception:
            pass
    else:
        msg += f" | ✅ 下单成功"
    log.info(msg)
    write_alert(msg)
    
    # 只有订单成功才推送飞书（走缓冲区合并，避免刷屏）
    if order_success:
        # 提取订单 ID（让用户能在币安直接查证）
        order_id = ''
        if order_result and isinstance(order_result, dict):
            order_obj = order_result.get('order', {})
            if isinstance(order_obj, dict):
                order_id = str(order_obj.get('orderId', ''))
        
        push_signal_alert({
            'symbol': symbol,
            'type': direction,
            'action': '开多' if direction == 'LONG' else '开空',
            'price': entry_price,
            'score': score,
            'regime': regime,
            'tp_price': pos.get('tp1_price', 0),   # 修复：字段名应为 tp1_price
            'sl_price': pos.get('sl_price', 0),
            'order_id': order_id,
        }, immediate=False)
    
    return pos


def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]
    symbol = pos["symbol"]
    sym_usdt = f"{symbol}USDT"

    # 🐛 修复：从 API 取真实持仓数量，不用本地缓存
    # 反转重建会把本地缓存改成风控标准量，但交易所实际量可能不同
    # 用本地缓存的量会导致 reduceOnly 被拒（-2022），产生降级连锁反应
    actual_qty = pos.get("qty", 0)
    try:
        from binance_auto_trade import get_all_positions
        for ap in (get_all_positions() or []):
            if ap.get("symbol") == symbol:
                api_amt = abs(float(ap.get("amount", 0) or 0))
                if api_amt > 0:
                    actual_qty = api_amt
                    break
    except Exception:
        pass  # 降级使用本地缓存
    
    qty_raw = actual_qty * size_ratio
    qty = format_quantity(symbol, qty_raw, entry)

    # 实盘平仓调用（使用 place_order with reduce_only=True）
    # 2026-06-03 修复：币安测试网 reduceOnly 全线返回 -2022，降级为不带 reduceOnly 的平仓
    close_result = None
    margin_replenished = False  # 2026-06-03：-4164 保证金不足时只补一次
    position_padded = False     # 2026-06-03：名义价值 < $20 时只补一次
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        for attempt in range(5):
            try:
                side = "SELL" if typ == "LONG" else "BUY"
                
                # 🔧 2026-06-03：第一次用 reduceOnly；如果被拒（-2022）则降级不带 reduceOnly
                close_result = place_order(
                    symbol=sym_usdt,
                    side=side,
                    quantity=qty,
                    leverage=10,
                    reduce_only=(attempt == 0),
                    price=current_price,
                )
                log.info(f"📝 平仓结果：{close_result}")
                
                if close_result.get('success', True):
                    break
                
                order_code = close_result.get('order', {}).get('code', 0)
                
                # 降级1：reduceOnly 被拒 → 不带 reduceOnly
                if attempt == 0 and order_code == -2022:
                    log.warning(f"⚠️ {symbol} reduceOnly 被币安拒（-2022），降级为普通平仓")
                    time.sleep(1)
                    close_result = place_order(
                        symbol=sym_usdt, side=side, quantity=qty,
                        leverage=10, reduce_only=False, price=current_price,
                    )
                    log.info(f"📝 平仓结果（降级）：{close_result}")
                    if close_result.get('success', True):
                        break
                    order_code = close_result.get('order', {}).get('code', 0)
                
                # 降级2：-4164 补充保证金
                if order_code == -4164 and not margin_replenished:
                    log.warning(f"⚠️ {symbol} 保证金/名义价值不足（-4164），补充 5 USDT")
                    try:
                        request("POST", "/fapi/v1/positionMargin", {
                            "symbol": sym_usdt, "amount": 5.0, "type": 1
                        })
                        margin_replenished = True
                        time.sleep(1)
                        close_result = place_order(
                            symbol=sym_usdt, side=side, quantity=qty,
                            leverage=10, reduce_only=False, price=current_price,
                        )
                        log.info(f"📝 平仓结果（补保证金后）：{close_result}")
                        if close_result.get('success', True):
                            break
                        order_code = close_result.get('order', {}).get('code', 0)
                    except Exception as me:
                        log.error(f"❌ 补充保证金失败：{me}")
                
                # 降级3：名义价值 < $20 → 买入≥$20 名义价值的量撑大后全平
                if order_code == -4164 and not position_padded and current_price > 0:
                    notional = qty * current_price
                    if notional < 20:
                        pad_qty = 20.0 / current_price  # 最少买入 $20 名义价值
                        pad_qty = format_quantity(symbol, pad_qty, current_price)
                        if pad_qty > 0:
                            log.warning(f"⚠️ {symbol} 名义 {notional:.1f} < $20，买入 {pad_qty} 撑大后全平")
                            try:
                                pad_side = "BUY" if typ == "LONG" else "SELL"
                                r = place_order(symbol=sym_usdt, side=pad_side, quantity=pad_qty,
                                                leverage=10, reduce_only=False, price=current_price)
                                if r.get('success', True):
                                    position_padded = True
                                    time.sleep(1)
                                    # 平全部（原持仓 + 补仓量）
                                    r_pos = request("GET", "/fapi/v2/positionRisk", {"symbol": sym_usdt})
                                    amt = abs(float(r_pos[0]['positionAmt'])) if isinstance(r_pos, list) and r_pos else qty
                                    total_qty = format_quantity(symbol, amt, current_price) if amt > qty else qty
                                    close_result = place_order(
                                        symbol=sym_usdt, side=side, quantity=total_qty,
                                        leverage=10, reduce_only=False, price=current_price,
                                    )
                                    log.info(f"📝 平仓结果（撑大后）：{close_result}")
                                    if close_result.get('success', True):
                                        break
                            except Exception as pe:
                                log.error(f"❌ 加仓撑大失败：{pe}")
                
                log.warning(f"⚠️ 平仓第{attempt+1}次失败，{'重试' if attempt < 4 else '放弃'}")
                if attempt < 4:
                    time.sleep(2)
            except Exception as e:
                log.error(f"❌ 平仓异常第{attempt+1}次：{e}")
                close_result = {"error": str(e), "success": False}
                if attempt < 4:
                    time.sleep(2)

    # 计算盈亏 - 使用实际持仓数量，不再用标准仓位
    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    qty_raw = pos.get("qty", 0) * size_ratio
    pnl_usdt = qty_raw * entry * pnl_pct  # 实际数量 × 入场价 × 涨跌幅
    # 2026-03-25 老公指示：用实际成交价计算真实盈亏（API 返回的 realizedPnl 优先）
    if close_result and close_result.get('success') and close_result.get('order'):
        realized = close_result.get('order', {}).get('realizedPnl')
        if realized is not None:
            pnl_usdt = float(realized)


    # 检查平仓是否真正成功
    close_success = True
    if close_result:
        close_success = close_result.get('success', True)
    
    emoji = "✅" if pnl_usdt >= 0 else "❌"
    msg = (
        f"{emoji} 平仓 {symbol} {reason} | "
        f"入场:{entry:.4f} 出场:{current_price:.4f} | "
        f"PnL:{pnl_pct*100:.2f}% ({pnl_usdt:+.2f}USDT) | "
        f"平{size_ratio*100:.0f}%仓"
    )
    if not close_success:
        msg += f" | ❌ 平仓失败"
    else:
        msg += f" | ✅ 平仓成功"
    log.info(msg)
    write_alert(msg)
    
    # 只有平仓成功才推送飞书（走缓冲区合并，避免刷屏）
    if close_success:
        # ✅ 平仓后清除该币种冷却，让下次开仓不被卡 5 分钟
        try:
            global cooldown_manager
            if cooldown_manager and size_ratio >= 1.0:
                cooldown_manager.reset_after_close(symbol)
                log.info(f"🔄 {symbol} 平仓后已清除冷却记录")
        except Exception as e:
            log.warning(f"清除冷却异常: {e}")
        
        # 确保 score 和 regime 有值（从持仓中读取，如果没有则用 0/unknown）
        score = pos.get('score')
        if score is None or score == '':
            score = 0
        regime = pos.get('regime')
        if regime is None or regime == '':
            regime = 'unknown'
        
        # 提取触发价（止盈用 tp1_price，止损用 sl_price）
        if 'TP' in reason:
            trigger_px = pos.get('tp1_price', 0)
        else:
            trigger_px = pos.get('sl_price', 0)
        
        # 提取真实成交价（如果 close_result 里有）
        actual_px = current_price
        if close_result and close_result.get('order'):
            try:
                avg = close_result['order'].get('avgPrice')
                if avg and float(avg) > 0:
                    actual_px = float(avg)
            except Exception:
                pass
        
        # 提取订单 ID
        order_id = ''
        if close_result and close_result.get('order'):
            order_id = str(close_result['order'].get('orderId', ''))
        
        push_signal_alert({
            'symbol': symbol,
            'type': typ,
            'action': '止盈' if 'TP' in reason else '止损',
            'price': actual_px,
            'trigger_price': trigger_px,
            'score': score,
            'regime': regime,
            'pnl': pnl_usdt,
            'order_id': order_id,
        }, immediate=False)  # 改为缓冲模式
    
    return pnl_usdt


# ═══════════════════════════════════════════════════════════════
# 十二、新闻过滤
# ═══════════════════════════════════════════════════════════════
class NewsFilter:

    def __init__(self):
        self.suspended: dict = {}   # symbol → 解封时间
        self.major_news_cache: dict = {}  # symbol → (has_major, timestamp)

    def check_suspend(self, symbol: str) -> bool:
        """True=允许交易"""
        if symbol in self.suspended:
            if datetime.now() < self.suspended[symbol]:
                return False
            del self.suspended[symbol]
        return True

    def process_news(self, title: str, symbols: list[str]):
        title_lower = title.lower()
        is_negative = any(kw in title_lower for kw in CONFIG["news_blacklist"])
        is_positive = any(kw in title_lower for kw in CONFIG["news_whitelist"])

        if is_negative:
            until = datetime.now() + timedelta(hours=CONFIG["news_suspend_hours"])
            for sym in symbols:
                if any(sym.lower() in title_lower for sym in symbols):
                    self.suspended[sym] = until
                    log.warning(f"📰 负面新闻，暂停 {sym} {CONFIG['news_suspend_hours']}h: {title}")
        elif is_positive:
            log.info(f"📰 利好新闻：{title}")

    def has_major_news(self, symbol: str) -> bool:
        """检查最近 1 小时内是否有重大新闻"""
        try:
            import json
            from pathlib import Path
            from datetime import datetime, timedelta

            news_file = Path("/root/.openclaw/workspace/scripts/crypto_news.json")
            if not news_file.exists():
                return False

            with open(news_file) as f:
                data = json.load(f)

            # 重大新闻关键词
            major_keywords = [
                "美联储", "fed", "sec", "利率", "rate",
                "监管", "regulation", "etf", "财政部",
                "treasury", "crackdown", "ban", "lawsuit"
            ]

            # 检查最近 1 小时的新闻
            one_hour_ago = datetime.now() - timedelta(hours=1)

            for news in data.get("news", []):
                # 检查时间
                try:
                    news_time = datetime.strptime(
                        news.get("time", ""), "%Y-%m-%d %H:%M"
                    )
                    if news_time < one_hour_ago:
                        continue
                except:
                    continue

                # 检查关键词
                title = news.get("title", "").lower()
                for kw in major_keywords:
                    if kw.lower() in title:
                        return True

            return False

        except Exception as e:
            return False


# ═══════════════════════════════════════════════════════════════
# 十三、飞书推送（统一走 feishu_helper，本模块仅保留别名）
# ═══════════════════════════════════════════════════════════════
from feishu_helper import get_token as get_feishu_token, push_card as push_feishu_card


def push_signal_alert(signal: dict, immediate: bool = False):
    """推送交易信号告警（开仓/平仓）- 支持缓冲合并"""
    global cooldown_manager
    symbol = signal['symbol']
    
    # 添加到缓冲区（除非立即推送）
    try:
        if not immediate and cooldown_manager:
            cooldown_manager.add_push_signal(symbol, signal)
            return
    except NameError:
        pass  # cooldown_manager 未初始化，直接推送
    
    # 立即推送
    _send_push_signal(signal)

def _send_push_signal(signal: dict):
    """实际发送推送信号"""
    symbol = signal['symbol']
    sig_type = signal.get('type', 'UNKNOWN')
    action = signal.get('action', '信号')
    price = signal.get('price', 0)
    score = signal.get('score', 0)
    regime = signal.get('regime', 'unknown')
    tp_price = signal.get('tp_price', 0)
    sl_price = signal.get('sl_price', 0)
    pnl = signal.get('pnl', 0)
    trigger_price = signal.get('trigger_price', 0)
    order_id = signal.get('order_id', '')
    merged_pnl = signal.get('merged_pnl', 0)
    merged_count = signal.get('merged_count', 0)
    merged_close_count = signal.get('merged_close_count', 0)
    
    # 颜色和 emoji
    if sig_type == 'LONG':
        emoji = '🟢'
        template = 'green'
        type_text = '开多'
    elif sig_type == 'SHORT':
        emoji = '🔴'
        template = 'red'
        type_text = '开空'
    else:
        emoji = '⚠️'
        template = 'blue'
        type_text = action
    
    is_merged = bool(merged_close_count) and action in ['开多', '开空']
    
    content = f"**{emoji} 币种：** {symbol}\n"
    content += f"**📊 方向：** {type_text}\n"
    
    if action in ['开多', '开空']:
        content += f"**💰 开仓价：** ${price:,.4f}\n"
    elif action in ['止盈', '止损', '平仓']:
        if trigger_price and trigger_price != price:
            content += f"**🎯 触发价：** ${trigger_price:,.4f}\n"
            content += f"**💰 成交价：** ${price:,.4f}（市价滑点）\n"
        else:
            content += f"**💰 成交价：** ${price:,.4f}\n"
    else:
        content += f"**💰 价格：** ${price:,.4f}\n"
    
    content += f"**📈 评分：** {score}/100\n"
    content += f"**🎯 状态：** {regime}\n"
    
    if action in ['开多', '开空'] and tp_price and sl_price:
        content += f"**🎯 止盈：** ${tp_price:,.4f}\n"
        content += f"**🛑 止损：** ${sl_price:,.4f}\n"
    
    if action in ['止盈', '止损', '平仓']:
        content += f"**💰 盈亏：** {pnl:+.4f} USDT\n"
        if merged_count and merged_count > 1:
            content += f"**📦 合并：** {merged_count} 次平仓\n"
    
    if merged_pnl != 0 and is_merged:
        content += f"\n---\n**📦 缓冲区合并播报（5分钟内同币种汇总）**\n"
        content += f"**💰 期间已平仓盈亏：** {merged_pnl:+.4f} USDT\n"
        content += f"**🔢 期间平仓次数：** {merged_close_count} 次\n"
    elif merged_pnl != 0:
        content += f"**💰 累计盈亏：** {merged_pnl:+.4f} USDT\n"
        if merged_close_count:
            content += f"**📦 包含：** {merged_close_count} 次平仓\n"
    
    if order_id:
        content += f"**🆔 订单：** `{order_id}`\n"
    
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": content
            }
        },
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8) · ⚠️ 仅供参考，注意风险"
            }]
        }
    ]
    
    if is_merged:
        title = f"📊 {symbol} 交易汇总（最新：{action}）"
    else:
        title = f"🚨 {action} - {symbol}"
    push_feishu_card(title, elements, template)


def _normalize_price_data(prices: dict) -> dict:
    """整点汇报用：保证每个币种价格是完整 dict（含 change_24h / volume）"""
    out = {}
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym)
        if isinstance(pd, (int, float)):
            pd = {"price": float(pd)}
        elif not isinstance(pd, dict):
            pd = {}
        if pd.get("price", 0) <= 0:
            continue
        if not pd.get("change_24h") and pd.get("open_24h", 0) > 0:
            o = float(pd["open_24h"])
            p = float(pd["price"])
            pd["change_24h"] = (p - o) / o * 100
        pd.setdefault("change_24h", 0.0)
        pd.setdefault("volume", 0.0)
        out[sym] = pd
    return out


def _fetch_24h_tickers() -> dict[str, dict]:
    """REST 批量拉 24h ticker（价格、涨跌幅、成交量兜底）"""
    out = {}
    try:
        eng = IndicatorEngine()
        r = eng.session.get(
            f"{CONFIG['binance_futures']}/ticker/24hr",
            timeout=15,
        )
        if r.status_code != 200:
            return out
        for t in r.json():
            sym = t.get("symbol", "").replace("USDT", "")
            if sym in CONFIG["symbols"]:
                out[sym] = {
                    "price": float(t.get("lastPrice", 0) or 0),
                    "change_24h": float(t.get("priceChangePercent", 0) or 0),
                    "volume": float(t.get("volume", 0) or 0),
                }
    except Exception as e:
        log.warning(f"⚠️ 整点汇报拉 24h ticker 失败：{e}")
    return out


def _price_data_for_score(symbol: str, pd: dict, ind: dict) -> dict:
    """整点汇报评分：价格/成交量与指标引擎同源"""
    price = float(pd.get("price") or ind.get("last_close") or 0)
    return {
        "price": price,
        "volume": float(ind.get("vol_current") or pd.get("volume") or 0),
        "change_24h": float(pd.get("change_24h", 0) or 0),
    }


def _regime_label(regime: str) -> str:
    return {"trending": "趋势", "ranging": "震荡", "volatile": "高波动"}.get(regime, regime)


def _format_hourly_score_line(symbol: str, pd: dict, ind: dict, regime: str, fg: int, fr: float, btc_ind: dict) -> str:
    """整点汇报单行：评分 + RSI + 市场状态（与飞书问答表格一致）"""
    score_pd = _price_data_for_score(symbol, pd, ind)
    score, direction = calc_score(symbol, score_pd, ind, regime, fg, fr, btc_ind)
    rsi = ind.get("rsi", 0)
    return f"{score}分 RSI{rsi:.0f} {_regime_label(regime)} → {direction}"


def prepare_hourly_report_data(
    prices: dict,
    indicators: dict,
    indicator_engine: Optional["IndicatorEngine"] = None,
) -> tuple[dict, dict]:
    """
    整点汇报前强制刷新：价格涨跌幅 + 全币种指标。
    避免整点时刻 indicators 未更新、change_24h 为 0、评分为 0。
    """
    prices = _normalize_price_data(prices)
    ticker_map = _fetch_24h_tickers()
    for sym, tk in ticker_map.items():
        if sym not in prices:
            prices[sym] = {}
        if tk.get("price", 0) > 0:
            prices[sym]["price"] = tk["price"]
        prices[sym]["change_24h"] = tk.get("change_24h", prices[sym].get("change_24h", 0))
        if not prices[sym].get("volume"):
            prices[sym]["volume"] = tk.get("volume", 0)

    engine = indicator_engine or IndicatorEngine()
    refreshed = {}
    for sym in CONFIG["symbols"]:
        ind = engine.calc(sym)
        if ind:
            refreshed[sym] = ind
            if sym in prices and ind.get("last_close"):
                prices[sym]["price"] = float(ind["last_close"])
                prices[sym]["volume"] = float(ind.get("vol_current") or prices[sym].get("volume") or 0)
    if refreshed:
        indicators = refreshed
        log.info(f"📋 整点汇报已刷新指标：{len(refreshed)}/{len(CONFIG['symbols'])} 个币种")
    else:
        log.warning("⚠️ 整点汇报指标刷新失败，评分可能为 0")

    return prices, indicators


def push_hourly_report(
    positions: list,
    prices: dict,
    indicators: dict,
    daily_pnl: float,
    fg: int,
    indicator_engine: Optional["IndicatorEngine"] = None,
):
    """推送整点汇报（显示所有币种的价格和评分）"""
    prices, indicators = prepare_hourly_report_data(prices, indicators, indicator_engine)
    btc_ind = indicators.get("BTC", {})

    # 恐惧贪婪描述
    if fg < 25:
        fg_text = "极度恐惧"
        fg_color = "🔴"
    elif fg < 40:
        fg_text = "恐惧"
        fg_color = "🟠"
    elif fg < 60:
        fg_text = "中性"
        fg_color = "🟡"
    elif fg < 75:
        fg_text = "贪婪"
        fg_color = "🟢"
    else:
        fg_text = "极度贪婪"
        fg_color = "🔵"
    
    # 优先用 Binance API 浮动盈亏（与 query_positions / 飞书问答一致）
    api_by_symbol = {}
    try:
        from binance_auto_trade import get_all_positions
        for ap in get_all_positions() or []:
            sym = ap.get("symbol")
            if sym:
                api_by_symbol[sym] = ap
    except Exception as e:
        log.warning(f"⚠️ 整点汇报拉 API 持仓失败，用本地计算：{e}")

    # 构建持仓列表（包含价格、评分、盈亏）
    pos_lines = []
    total_floating = 0.0
    log.info(f"📋 整点汇报：持仓数={len(positions)}, prices 缓存={len(prices)}")
    for pos in positions:
        normalize_position(pos)
        symbol = pos['symbol']
        pos_type = pos['type']
        entry_price = float(pos.get('entry_price', 0) or 0)
        amount = position_amount(pos)  # 本地缓存数量（可能已被反转重建修正）
        api_pos = api_by_symbol.get(symbol, {})

        if api_pos:
            api_amount = abs(float(api_pos.get("amount", 0) or 0))
            # 检测交易所持仓数量与本地缓存的偏差（可能是历史 bug 残留仓位）
            if api_amount > 0 and amount != api_amount:
                deviation = abs(amount - api_amount) / max(amount, api_amount)
                if deviation > 0.05:  # 偏差 >5% 才告警
                    log.warning(
                        f"⚠️ {symbol} 交易所数量({api_amount:.1f})与本地缓存({amount:.1f})不一致，"
                        f"偏差 {deviation:.1%}，可能是历史 bug 残留仓位"
                    )
            if api_pos.get("unrealized_pnl") is not None and api_amount > 0:
                unrealized_pnl = float(api_pos["unrealized_pnl"])
                current_price = float(
                    api_pos.get("current_price") or api_pos.get("mark_price") or 0
                ) or prices.get(symbol, {}).get('price', entry_price)
            else:
                current_price = prices.get(symbol, {}).get('price', entry_price)
                # 用 API 真实数量算盈亏（不回退到本地缓存）
                calc_qty = api_amount if api_amount > 0 else amount
                unrealized_pnl = (
                    (current_price - entry_price) * calc_qty
                    if pos_type == 'LONG'
                    else (entry_price - current_price) * calc_qty
                )
        else:
            current_price = prices.get(symbol, {}).get('price', entry_price)
            unrealized_pnl = (
                (current_price - entry_price) * amount
                if pos_type == 'LONG'
                else (entry_price - current_price) * amount
            )

        log.info(
            f"  📍 {symbol} {pos_type}: entry={entry_price}, amount={amount}, "
            f"current={current_price}, pnl={unrealized_pnl:+.2f}"
        )

        notional = entry_price * amount
        pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0
        
        display_price = prices.get(symbol, {}).get("price") or current_price
        open_score = pos.get("score", "—")
        total_floating += unrealized_pnl
        pos_lines.append(
            f"**{symbol}** {pos_type} | 开仓：${entry_price:,.2f} | "
            f"现价：${display_price:,.2f} | 开仓评分：{open_score} | "
            f"盈亏：${unrealized_pnl:+.2f} ({pnl_pct:+.2f}%)"
        )

    # 构建所有币种的价格和评分列表
    all_coins_lines = []

    for symbol in CONFIG["symbols"]:
        if prices.get(symbol):
            price = prices[symbol].get('price', 0)
            change_24h = prices[symbol].get('change_24h', 0)
            
            ind = indicators.get(symbol, {})
            regime = detect_regime(ind) if ind else "ranging"
            fr = 0
            score_txt = _format_hourly_score_line(
                symbol, prices[symbol], ind, regime, fg, fr, btc_ind
            ) if ind else "—分（指标未就绪）"

            holding = "📌" if any(p['symbol'] == symbol for p in positions) else "  "
            all_coins_lines.append(
                f"{holding} **{symbol}** ${price:,.2f} ({change_24h:+.2f}%) | {score_txt}"
            )
    
    # 构建卡片元素（使用 div 标签）
    content_lines = [
        f"**📊 今日已实现：** {daily_pnl:+.2f} USDT",
        f"**💧 浮动盈亏：** {total_floating:+.2f} USDT",
        f"**😨 恐惧贪婪：** {fg_color} {fg_text} ({fg})",
        f"**📦 持仓数：** {len(positions)}/{CONFIG['max_positions']}"
    ]
    
    content_lines.append("")
    content_lines.append("**📊 全部币种:**")
    content_lines.extend(all_coins_lines)
    
    if pos_lines:
        content_lines.append("")
        content_lines.append("**📌 持仓详情:**")
        content_lines.extend(pos_lines)
    
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(content_lines)
            }
        },
        {
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)"
            }]
        }
    ]
    
    title = "📊 整点汇报"
    return push_feishu_card(title, elements, "blue")


# ═══════════════════════════════════════════════════════════════
# 十四、辅助工具
# ═══════════════════════════════════════════════════════════════
def write_alert(msg: str):
    try:
        with open(daily_alert_path(), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def save_state(state: dict):
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


def sync_daily_pnl_from_exchange(circuit: CircuitBreaker) -> float:
    """以交易所当日成交汇总为准，刷新今日已实现盈亏（用于启动/整点汇报）。"""
    today = datetime.now().date()
    circuit.reset_date = today
    try:
        from binance_auto_trade import fetch_today_realized_pnl

        api_pnl = fetch_today_realized_pnl(CONFIG["symbols"])
        if api_pnl is None:
            return circuit.daily_pnl
        circuit.daily_pnl = api_pnl
        return api_pnl
    except Exception as e:
        log.warning(f"⚠️ 同步今日已实现盈亏失败：{e}")
        return circuit.daily_pnl


def restore_daily_pnl(circuit: CircuitBreaker) -> float:
    """启动时恢复今日已实现：优先交易所汇总，失败则用同日状态文件。"""
    today = datetime.now().date()
    circuit.reset_date = today
    state_pnl = 0.0
    try:
        if os.path.exists(CONFIG["state_file"]):
            with open(CONFIG["state_file"]) as f:
                state = json.load(f)
            if state.get("daily_pnl_date") == today.isoformat():
                state_pnl = float(state.get("daily_pnl", 0.0))
    except Exception as e:
        log.warning(f"⚠️ 读取状态 daily_pnl 失败：{e}")

    try:
        from binance_auto_trade import fetch_today_realized_pnl

        api_pnl = fetch_today_realized_pnl(CONFIG["symbols"])
        if api_pnl is not None:
            circuit.daily_pnl = api_pnl
            log.info(f"📋 今日已实现盈亏：{api_pnl:+.2f}U（来源：交易所 userTrades）")
            return api_pnl
    except Exception as e:
        log.warning(f"⚠️ 交易所今日盈亏同步失败：{e}")

    circuit.daily_pnl = state_pnl
    log.info(f"📋 今日已实现盈亏：{state_pnl:+.2f}U（来源：状态文件）")
    return state_pnl


# ═══════════════════════════════════════════════════════════════
# 十四、分层调度器
# ═══════════════════════════════════════════════════════════════
class ScanScheduler:
    """控制各任务执行频率，避免每15秒都跑重计算"""

    def __init__(self):
        self.tick = 0   # 每15秒+1

    def tick_up(self):
        self.tick += 1

    def should(self, task: str) -> bool:
        # task → 每隔多少个tick执行一次
        intervals = {
            "indicators":   4,    # 60秒
            "sentiment":    20,   # 300秒
            "funding":      20,   # 300秒
            "hourly_report":240,  # 3600秒
        }
        n = intervals.get(task, 1)
        return self.tick % n == 0


# ═══════════════════════════════════════════════════════════════
# 十五、整点汇报
# ═══════════════════════════════════════════════════════════════
def hourly_report(
    positions: list,
    prices: dict,
    indicators: dict,
    circuit: CircuitBreaker,
    fg: int,
    indicator_engine: Optional["IndicatorEngine"] = None,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sync_daily_pnl_from_exchange(circuit)

    # 如果数据为空，从 API 重新获取
    if not positions:
        log.warning("⚠️ 小时汇报时 positions 为空，从 API 重新获取...")
        try:
            from binance_auto_trade import get_all_positions
            api_positions = get_all_positions()
            if api_positions:
                positions = []
                for p in api_positions:
                    amt = float(p.get('amount', 0))
                    entry = float(p.get('entry_price', 0))
                    # 🔧 2026-06-06 修复：过滤粉尘仓位
                    if amt != 0 and abs(amt) * entry >= 5.0:
                        symbol = p['symbol']
                        # 🐛 Bug 修复：从 API 同步时计算合理的止盈止损（基于 entry_price ±3%）
                        is_long = amt > 0
                        tp1 = round(entry * 1.03, 4) if is_long else round(entry * 0.97, 4)
                        tp2 = round(entry * 1.06, 4) if is_long else round(entry * 0.94, 4)
                        sl = round(entry * 0.97, 4) if is_long else round(entry * 1.03, 4)
                        
                        pos = {
                            'symbol': symbol,
                            'type': 'SHORT' if amt < 0 else 'LONG',
                            'entry_price': entry,
                            'entry_time': datetime.now().isoformat(),
                            'qty': abs(amt),
                            'amount': abs(amt),
                            'score': 55,
                            'regime': 'trending',
                            'ai_result': None,
                            'order_result': {'success': True, 'message': '从 API 同步'},
                            'high_24h': 0.0,
                            'low_24h': 0.0,
                            'tp1_price': tp1,
                            'tp2_price': tp2,  # 保留兼容（汇报用）
                            'sl_price': sl,
                            'tp1_hit': False,  # 保留兼容
                            'size_remaining': 1.0,  # 保留兼容
                            'peak_pnl': 0.0,
                            'tp_pct': 0.04,  # 汇报用默认值
                        }
                        positions.append(pos)
                log.info(f"✅ 小时汇报从 API 恢复 {len(positions)} 个持仓")
        except Exception as e:
            log.error(f"❌ 小时汇报获取持仓失败：{e}")
    
    lines = [
        f"\n{'='*50}",
        f"📊 整点汇报 {now}",
        f"恐惧贪婪指数: {fg} ({'极度恐惧' if fg<25 else '恐惧' if fg<40 else '中性' if fg<60 else '贪婪' if fg<75 else '极度贪婪'})",
        f"今日PnL: {circuit.daily_pnl:+.2f} USDT ({circuit.daily_pnl/CONFIG['total_capital']*100:+.1f}%)",
        f"持仓数: {len(positions)}/{CONFIG['max_positions']}",
    ]
    for pos in positions:
        normalize_position(pos)
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price", 0)
        entry = pos["entry_price"]
        typ = pos["type"]
        amt = position_amount(pos)
        if amt > 0 and p > 0:
            pnl_usdt = (p - entry) * amt if typ == "LONG" else (entry - p) * amt
            pnl_pct = pnl_usdt / (entry * amt) * 100 if entry > 0 else 0
        else:
            pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry
            pnl_pct *= 100
            pnl_usdt = 0
        lines.append(
            f"  {'🟢' if typ=='LONG' else '🔴'} {sym} {typ} @{entry:.4f} "
            f"现价:{p:.4f} PnL:{pnl_usdt:+.2f}U ({pnl_pct:+.2f}%)"
        )
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym, {})
        if pd.get('price', 0):  # 只显示有价格的币种
            lines.append(
                f"  {sym}: {pd.get('price',0):.4f} ({pd.get('change_24h',0):+.2f}%)"
            )
    lines.append("=" * 50)
    report = "\n".join(lines)
    log.info(report)
    write_alert(report)
    
    # 📊 添加各币种评分详情日志（2026-03-27 老公指示）
    score_lines = [f"\n📊 评分详情 ({now}):"]
    btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 代替 ETH 作为大盘参考
    for sym in CONFIG["symbols"]:
        pd = prices.get(sym, {})
        ind = indicators.get(sym, {})
        if pd.get('price', 0) and ind:
            regime = detect_regime(ind)
            score, direction = calc_score(sym, pd, ind, regime, fg, 0, btc_ind)
            rsi = ind.get('rsi', 0)
            ma60_ok = "✓" if pd.get('price', 0) >= ind.get('ma60', 999999) else "✗"
            score_lines.append(
                f"  {sym}: {score}分 (RSI:{rsi:.0f}, 24h:{pd.get('change_24h',0):+.1f}%, MA60:{ma60_ok}) → 信号：{direction}"
            )
    score_log = "\n".join(score_lines)
    log.info(score_log)
    
    # 推送飞书（带错误检查和重试）
    try:
        success = push_hourly_report(
            positions, prices, indicators, circuit.daily_pnl, fg, indicator_engine
        )
        if not success:
            log.warning("⚠️ 小时汇报推送失败，已跳过")
    except Exception as e:
        log.error(f"❌ 小时汇报推送异常：{e}")


# ═══════════════════════════════════════════════════════════════
# 十六、主循环
# ═══════════════════════════════════════════════════════════════
async def main():
    global cooldown_manager
    log.info("🚀 crypto_signal_monitor v6.0 启动")
    
    # 启动横幅：可视化确认 API key（防止使用旧 key 而不自知）
    if AUTO_TRADE_ENABLED:
        try:
            import binance_auto_trade as bat
            ak = bat._api_key()
            log.info(f"🔑 当前 Binance API key: {ak[:8]}...{ak[-4:]} (动态读取，文件改动会自动重载)")
        except Exception as e:
            log.warning(f"⚠️ 无法读取 API key 信息：{e}")

    # 初始化各模块
    price_stream = PriceStream(CONFIG["symbols"], proxy=CONFIG["proxy"])
    indicator_engine = IndicatorEngine()
    sentiment_engine = SentimentEngine()
    global SENTIMENT_ENGINE
    SENTIMENT_ENGINE = sentiment_engine
    ai_predictor     = AIPredictor()
    circuit_breaker  = CircuitBreaker()
    cooldown_manager = CooldownManager()
    news_filter      = NewsFilter()
    scheduler        = ScanScheduler()

    # 缓存
    indicators:  dict = {}
    fg_cache:    int  = 50
    fr_cache:    dict = {}

    # 启动WebSocket（后台）
    asyncio.create_task(price_stream.start())

    # 等待WebSocket就绪
    log.info("等待WebSocket价格流就绪...")
    for _ in range(30):
        if price_stream.is_ready():
            break
        await asyncio.sleep(1)
    log.info(f"价格流就绪: {list(price_stream.prices.keys())}")

    # 加载持仓（从 Binance API 同步真实持仓）
    log.info("🔄 从 Binance API 同步持仓...")
    try:
        api_positions = get_all_positions()
        # 如果 API 返回空，尝试从本地文件加载
        if not api_positions:
            log.warning("⚠️ Binance API 返回空持仓，尝试从本地文件加载...")
            positions = load_positions()
            if positions:
                log.info(f"✅ 从本地文件恢复 {len(positions)} 个持仓")
            else:
                positions = []
                log.warning("⚠️ 本地文件也无持仓，初始化为空")
        else:
            # 2026-03-28 老公指示：先加载本地缓存，保留止盈止损
            old_positions = load_positions()
            old_map = {p.get("symbol", ""): p for p in old_positions}
            log.info(f'📊 加载本地缓存 {len(old_positions)} 个持仓用于保留止盈止损')
            
            # 过滤有实际持仓的币种
            positions = []
            for p in api_positions:
                amt = float(p.get('amount', 0))
                entry = float(p.get('entry_price', 0))
                # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5 不纳入）
                if amt != 0 and abs(amt) * entry >= 5.0:
                    # 转换为本地格式
                    symbol = p['symbol']
                    entry = float(p.get('entry_price', 0))
                    direction = 'SHORT' if amt < 0 else 'LONG'
                    
                    # 查找旧缓存
                    old = old_map.get(symbol, {})
                    old_direction = old.get("type", "")
                    
                    # 用默认参数计算新的 SL/TP（作为兜底）
                    new_tp_sl = calc_tp_sl(entry, direction, {}, {})
                    
                    # 🔧 2026-06-16 修复：启动时保留三档移动止盈收窄过的 SL
                    # 如果旧缓存里同方向且 SL 更紧（对持仓更有利），优先保留旧的
                    old_sl = old.get("sl_price", 0)
                    new_sl = new_tp_sl["sl_price"]
                    old_tp1 = old.get("tp1_price", 0)
                    new_tp1 = new_tp_sl["tp1_price"]
                    
                    sl_price = new_sl  # 默认用新值
                    tp1_price = new_tp1
                    peak_pnl = 0.0
                    peak_price = 0.0
                    tp_pct = new_tp_sl.get("tp_pct", 0.04)
                    
                    if old_direction == direction:
                        has_peak = old.get("peak_pnl", 0) > 0
                        
                        if old_sl > 0:
                            # 判断谁的 SL 更紧
                            if direction == "SHORT":
                                tighter = old_sl < new_sl
                            else:
                                tighter = old_sl > new_sl
                            
                            if tighter:
                                sl_price = old_sl
                                log.info(f"🔒 {symbol} 保留旧 SL {old_sl:.4f}（比新 SL {new_sl:.4f} 更紧）")
                        elif has_peak:
                            # SL 丢了但有峰值数据 → 用 max(calc默认, entry±动态峰值)
                            # 从旧 peak_price 推算 SL（三档移动止盈反推）
                            old_peak_price = old.get("peak_price", 0)
                            if old_peak_price > 0:
                                # 反推：peak_price 附近的 SL（给 0.5% 缓冲）
                                if direction == "SHORT":
                                    estimated_sl = old_peak_price * 1.005
                                    sl_price = min(new_sl, estimated_sl)
                                else:
                                    estimated_sl = old_peak_price * 0.995
                                    sl_price = max(new_sl, estimated_sl)
                                sl_price = round(sl_price, 4)
                                log.info(f"🔧 {symbol} SL丢失恢复：peak_price={old_peak_price} → 估算SL={sl_price:.4f}")
                        
                        # 恢复峰值数据（三档移动止盈依赖）
                        peak_pnl = old.get("peak_pnl", 0)
                        peak_price = old.get("peak_price", 0)
                        tp_pct = old.get("tp_pct", tp_pct)
                        tp1_price = old_tp1 if old_tp1 > 0 else new_tp1
                        
                        if peak_pnl > 0:
                            log.info(f"📈 {symbol} 恢复 peak_pnl={peak_pnl:.2%} peak_price={peak_price}")
                    
                    pos = {
                        'symbol': symbol,
                        'type': direction,
                        'entry_price': entry,
                        'entry_time': old.get("entry_time", datetime.now().isoformat()),
                        'qty': abs(amt),
                        'amount': abs(amt),
                        'score': old.get("score", 55),
                        'regime': old.get("regime", "trending"),
                        'ai_result': None,
                        'order_result': {'success': True, 'message': '从 API 同步'},
                        'high_24h': old.get("high_24h", 0.0),
                        'low_24h': old.get("low_24h", 0.0),
                        'tp1_price': tp1_price,
                        'sl_price': sl_price,
                        'peak_pnl': peak_pnl,
                        'peak_price': peak_price,
                        'tp_pct': tp_pct,
                    }
                    positions.append(pos)
                    status = "恢复数据" if peak_pnl > 0 else "新同步"
                    log.info(f"✅ 同步持仓({status})：{symbol} {pos['type']} @ ${pos['entry_price']:.2f} "
                             f"SL={sl_price:.4f} TP={tp1_price:.4f}")
        save_positions(positions)
        log.info(f"✅ 持仓同步完成：{len(positions)}个")
        
        # 🔧 2026-06-06 修复：启动时清理不属于任何持仓的孤儿 Algo 条件单
        if AUTO_TRADE_ENABLED:
            position_symbols = {p.get('symbol', '') for p in positions}
            try:
                all_algos = request("GET", "/fapi/v1/openAlgoOrders", {})
                if isinstance(all_algos, list):
                    orphan_count = 0
                    for a in all_algos:
                        sym_raw = a.get('symbol', '')
                        sym = sym_raw.replace('USDT', '') if sym_raw else ''
                        if sym and sym not in position_symbols:
                            algo_id = a.get('algoId')
                            if algo_id:
                                try:
                                    request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                                    orphan_count += 1
                                    log.info(f"🗑️ 启动清理孤儿 Algo 单：{sym} algoId={algo_id}")
                                except Exception:
                                    pass
                    if orphan_count > 0:
                        log.info(f"🗑️ 启动清理完成：{orphan_count} 个孤儿 Algo 单")
            except Exception as e:
                log.warning(f"⚠️ 启动清理 Algo 单异常：{e}")
        
        # 🐛 仓位反转修复：启动时为从 API 同步的每个持仓提交止盈止损 Algo 单
        for pos in positions:
            submit_initial_algo_orders(pos)
    except Exception as e:
        log.error(f"❌ 持仓同步失败：{e}")
        positions = load_positions()  # 回退到本地文件

    # 从状态文件恢复上次推送时间、峰值盈亏；今日已实现由 restore_daily_pnl 校准
    last_report_hour = -1
    last_report_time = 0
    peak_pnl_map = {}
    try:
        if os.path.exists(CONFIG["state_file"]):
            with open(CONFIG["state_file"]) as f:
                state = json.load(f)
                if state.get("last_report_hour") is not None:
                    last_report_hour = state["last_report_hour"]
                if state.get("last_report_time"):
                    last_report_time = state["last_report_time"]
                if state.get("peak_pnl_map"):
                    peak_pnl_map = state.get("peak_pnl_map", {})
    except Exception as e:
        log.warning(f"⚠️ 加载状态文件失败：{e}")

    realized_pnl = restore_daily_pnl(circuit_breaker)
    # 熔断暂停仅由 is_trading_allowed 管理，不再在启动时清零 daily_pnl
    circuit_breaker.paused_until = None
    circuit_breaker.consecutive_losses = 0
    log.info(
        f"📋 恢复状态：last_report_hour={last_report_hour}, "
        f"daily_pnl={realized_pnl:+.2f}U, peak_pnl={peak_pnl_map}"
    )

    # 应用恢复的 peak_pnl 到持仓（移动止盈关键数据）
    if peak_pnl_map and positions:
        for pos in positions:
            sym = pos["symbol"]
            if sym in peak_pnl_map:
                pos["peak_pnl"] = peak_pnl_map[sym]
        save_positions(positions)
        log.info(f"✅ 恢复 peak_pnl 到 {len(positions)} 个持仓")

    while True:
        loop_start = time.time()
        scheduler.tick_up()
        
        # 2026-03-28 老公指示：爆仓保护检查（保证金率<5% 强制平仓）
        try:
            from binance_auto_trade import check_margin_ratio_protection
            check_margin_ratio_protection()
        except Exception as e:
            log.warning(f"⚠️ 爆仓保护检查失败：{e}")

        # 每 20 个 tick（5 分钟）从 API 同步持仓
        # 🐛 Bug 修复：API 是真相，本地是缓存。API 返回 0 说明真的没有持仓
        if scheduler.tick % 20 == 0:
            try:
                api_positions = get_all_positions()
                # 🐛 Bug 修复：API 返回 dict 表示错误，保留缓存不处理
                if isinstance(api_positions, dict) and 'error' in api_positions:
                    log.warning(f"⚠️ API 查询失败，保留本地缓存：{api_positions['error'][:50]}")
                    api_count = -1  # 标记失败
                else:
                    # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5），防止误重建
                    api_count = sum(1 for p in api_positions 
                        if float(p.get('amount', 0)) != 0
                        and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0)
                
                # 判断是否需要重建持仓：计数变化 OR 方向变化
                need_rebuild = False
                
                if api_count == 0:
                    if len(positions) > 0:
                        log.info(f"🔄 持仓同步：API 持仓=0，清空本地缓存（原{len(positions)}个）")
                        # 🔧 2026-06-06 修复：清空持仓时同步取消 Binance 上所有残留 Algo 条件单
                        if AUTO_TRADE_ENABLED:
                            for old_pos in positions:
                                try:
                                    cancel_algo_orders(f"{old_pos['symbol']}USDT")
                                except Exception:
                                    pass
                        positions = []
                        save_positions(positions)
                        log.info("✅ 持仓同步完成：0 个（API 为空）")
                elif api_count > 0 and api_count != len(positions):
                    need_rebuild = True
                    log.info(f"🔄 持仓同步：本地{len(positions)}个 → API{api_count}个")
                elif api_count > 0 and api_count == len(positions) and api_count > 0:
                    # 🐛 2026-06-15 修复：计数相同但币种集合不同 → 僵尸缓存（外部平仓后新单替补，缓存未清理）
                    api_symbols = set()
                    for p in api_positions:
                        amt = float(p.get('amount', 0))
                        if amt != 0 and abs(amt) * float(p.get('entry_price', 0)) >= 5.0:
                            api_symbols.add(p['symbol'])
                    local_symbols = {lp['symbol'] for lp in positions}
                    if api_symbols != local_symbols:
                        need_rebuild = True
                        stale = local_symbols - api_symbols
                        new_syms = api_symbols - local_symbols
                        log.info(f"🔄 持仓同步：缓存币种变化 旧={sorted(local_symbols)} → 新={sorted(api_symbols)}"
                                 + (f" (僵尸:{sorted(stale)})" if stale else "")
                                 + (f" (新增:{sorted(new_syms)})" if new_syms else ""))
                    else:
                        # 🐛 仓位反转检测：计数相同、币种相同但方向不同时也要重建
                        for p in api_positions:
                            amt = float(p.get('amount', 0))
                            entry = float(p.get('entry_price', 0))
                            if amt != 0 and abs(amt) * entry >= 5.0:  # 🔧 过滤粉尘
                                sym = p['symbol']
                                api_dir = 'SHORT' if amt < 0 else 'LONG'
                                for lp in positions:
                                    if lp['symbol'] == sym and lp['type'] != api_dir:
                                        need_rebuild = True
                                        log.info(f"🔄 持仓同步：{sym} 方向变化 {lp['type']}→{api_dir}，强制重建")
                                        break
                                if need_rebuild:
                                    break
                
                if need_rebuild:
                    new_positions = []
                    for p in api_positions:
                        amt = float(p.get('amount', 0))
                        entry = float(p.get('entry_price', 0))
                        # 🔧 2026-06-06 修复：过滤粉尘仓位（名义价值<$5 不重建）
                        if amt != 0 and abs(amt) * entry >= 5.0:
                            symbol = p['symbol']
                            entry = float(p.get('entry_price', 0))
                            # 🐛 修复：反转重建持仓时按风控规则重算数量，不用 API 的 abs(amt)
                            size_pct = CONFIG["position_size_pct"]
                            expected_qty = (CONFIG["total_capital"] * size_pct * 10) / entry
                            correct_amount = format_quantity(symbol, expected_qty, entry)
                            api_amount = abs(amt)
                            size_ratio = api_amount / correct_amount if correct_amount > 0 else 1.0
                            log.info(f"🔧 反转重建 {symbol}：API原始={api_amount:.4f} → 风控标准={correct_amount:.4f} (比例={size_ratio:.1%})")
                            
                            # 🔧 2026-06-10 修复：API 持仓量严重偏小（<80%风控标准）→ 迷你仓位，关闭后用标准量重开
                            if size_ratio < 0.8 and AUTO_TRADE_ENABLED:
                                log.warning(f"⚠️ {symbol} 迷你仓位检测：API={api_amount:.4f} < 标准80%，尝试修复")
                                try:
                                    # 构造临时持仓对象，复用 close_position 的完整降级逻辑
                                    tmp_pos = {
                                        'symbol': symbol, 'type': 'SHORT' if amt < 0 else 'LONG',
                                        'entry_price': entry, 'amount': api_amount, 'qty': api_amount,
                                        'tp1_price': 0, 'sl_price': 0, 'tp1_hit': False,
                                        'size_remaining': 1.0, 'tp_pct': 0.04, 'peak_pnl': 0.0,
                                    }
                                    close_position(tmp_pos, "迷你仓修复", 1.0, entry)
                                    time.sleep(2)
                                    # 检查是否真的平掉了
                                    api_check = get_all_positions()
                                    still_there = any(
                                        float(ap.get('amount', 0)) != 0 
                                        and abs(float(ap.get('amount', 0))) * float(ap.get('entry_price', 0)) >= 5.0
                                        and ap.get('symbol', '') == symbol
                                        for ap in (api_check or [])
                                    )
                                    if still_there:
                                        log.error(f"❌ {symbol} 迷你仓关闭未生效（仓位仍在API），跳过重建")
                                        continue
                                    
                                    # 2. 用标准量重新开仓
                                    side_open = "BUY" if amt > 0 else "SELL"
                                    sym_usdt = f"{symbol}USDT"
                                    open_result = place_order(
                                        symbol=sym_usdt, side=side_open,
                                        quantity=correct_amount, leverage=10,
                                        reduce_only=False, price=entry,
                                    )
                                    if open_result.get('success', True):
                                        log.info(f"✅ {symbol} 修复成功：{api_amount:.4f}→{correct_amount:.4f}")
                                        tp_sl = calc_tp_sl(entry, 'LONG' if amt > 0 else 'SHORT', {}, {})
                                        new_positions.append({
                                            'symbol': symbol,
                                            'type': 'SHORT' if amt < 0 else 'LONG',
                                            'entry_price': entry,
                                            'amount': correct_amount,
                                            'tp1_price': tp_sl['tp1_price'],
                                            'tp2_price': 0.0,
                                            'sl_price': tp_sl['sl_price'],
                                            'tp1_hit': False,
                                            'size_remaining': 1.0,
                                            'tp_pct': tp_sl.get('tp_pct', 0.04),
                                            'peak_pnl': 0.0,
                                        })
                                    else:
                                        log.error(f"❌ {symbol} 修复失败（重开被拒）：{open_result}")
                                except Exception as repair_e:
                                    log.error(f"❌ {symbol} 修复异常：{repair_e}")
                            else:
                                # 正常重建（数量匹配）
                                tp_sl = calc_tp_sl(entry, 'LONG' if amt > 0 else 'SHORT', {}, {})
                                tp1 = tp_sl['tp1_price']
                                sl = tp_sl['sl_price']
                                new_positions.append({
                                    'symbol': symbol,
                                    'type': 'SHORT' if amt < 0 else 'LONG',
                                    'entry_price': entry,
                                    'amount': correct_amount,
                                    'tp1_price': tp1,
                                    'tp2_price': 0.0,
                                    'sl_price': sl,
                                    'tp1_hit': False,
                                    'size_remaining': 1.0,
                                    'tp_pct': tp_sl.get('tp_pct', 0.04),
                                    'peak_pnl': 0.0,
                                })
                    # 🔧 2026-06-06 修复：清理已消失币种的孤儿 Algo 条件单
                    old_symbols = {p['symbol'] for p in positions}  # 修复前缓存
                    new_symbols = {p['symbol'] for p in new_positions}
                    orphan_symbols = old_symbols - new_symbols
                    if orphan_symbols and AUTO_TRADE_ENABLED:
                        for sym in orphan_symbols:
                            try:
                                cancel_algo_orders(f"{sym}USDT")
                                log.info(f"🗑️ 清理孤儿 Algo 单：{sym}")
                            except Exception:
                                pass
                    positions = new_positions
                    save_positions(positions)
                    log.info(f"✅ 持仓同步完成：{len(positions)}个")
                    # 🐛 仓位反转修复：重建后提交止盈止损 Algo 单
                    for pos in positions:
                        submit_initial_algo_orders(pos)
            except Exception as e:
                log.error(f"⚠️ 同步失败：{e}")

        try:
            # ──────────────────────────────────
            # Step 1: 获取实时价格（0 REST请求）
            # ──────────────────────────────────
            prices = {sym: price_stream.get(sym) for sym in CONFIG["symbols"]}

            # ──────────────────────────────────
            # Step 2: 止盈止损检查（每15秒，最轻量）
            # ──────────────────────────────────
            exits = check_exits_fast(positions, prices)
            for pos, reason, size in exits:
                p = prices[pos["symbol"]]["price"]
                pnl = close_position(pos, reason, size, p)
                circuit_breaker.record_trade(pnl)

                # 🐛 2026-06-03 修复：平仓后验证 API 才删除本地持仓，防止平仓失败时被 API 同步救回循环
                if reason == "TP1":
                    sym_closed = pos['symbol']
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"TP1 触发 {sym_closed}，全平移除")
                    # 🔧 2026-06-05 修复：平仓后清理该币种所有 Algo 条件单，防止孤儿单
                    if AUTO_TRADE_ENABLED:
                        cancel_algo_orders(f"{sym_closed}USDT")
                elif reason == "SL":
                    # 先同步 API 确认仓位真的没了再删
                    sym_closed = pos['symbol']
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"SL 触发 {sym_closed}，止损移除")
                    try:
                        api_positions = get_all_positions()
                        api_still_there = any(
                            float(p.get('amount', 0)) != 0 
                            and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0
                            and p.get('symbol', '') == sym_closed
                            for p in (api_positions or [])
                        )
                        if api_still_there:
                            log.error(f"⛔ SL 平仓 {sym_closed} 未成交！Binance 仓位仍在，恢复本地缓存")
                            # 从 API 重建该币种持仓
                            for ap in api_positions:
                                amt = float(ap.get('amount', 0))
                                entry = float(ap.get('entry_price', 0))
                                if amt != 0 and abs(amt) * entry >= 5.0 and ap.get('symbol', '') == sym_closed:
                                    entry = float(ap.get('entry_price', 0))
                                    typ = 'SHORT' if amt < 0 else 'LONG'
                                    old = pos
                                    positions.append({
                                        'symbol': sym_closed,
                                        'type': typ,
                                        'entry_price': entry,
                                        'amount': abs(amt),
                                        'tp1_price': old.get('tp1_price', 0),
                                        'sl_price': old.get('sl_price', 0),
                                        'tp1_hit': old.get('tp1_hit', False),
                                        'size_remaining': old.get('size_remaining', 1.0),
                                        'tp_pct': old.get('tp_pct', 0.04),
                                        'peak_pnl': old.get('peak_pnl', 0.0),
                                    })
                                    save_positions(positions)
                                    log.warning(f"🔄 已恢复本地持仓 {sym_closed}（Binance 上仓位未被平掉）")
                                    break
                        else:
                            api_count = sum(1 for p in api_positions 
                                if float(p.get('amount', 0)) != 0
                                and abs(float(p.get('amount', 0))) * float(p.get('entry_price', 0)) >= 5.0)
                            if api_count == 0:
                                positions = []
                                save_positions(positions)
                                log.info("📊 SL 平仓后同步：API 持仓为 0，清空本地缓存")
                            else:
                                log.info(f"📊 SL 平仓后同步：API 还有{api_count}个持仓，保留本地缓存")
                    except Exception as e:
                        log.error(f"⚠️ SL 平仓后同步失败：{e}")
                    # 🔧 2026-06-05 修复：止损平仓后清理该币种所有 Algo 条件单
                    if AUTO_TRADE_ENABLED:
                        try:
                            cancel_algo_orders(f"{sym_closed}USDT")
                        except Exception:
                            pass


            # 移动止盈更新
            update_trailing_stop(positions, prices)
            save_positions(positions)


            # ──────────────────────────────────
            # Step 3: 指标计算（60秒一次）
            # ──────────────────────────────────
            if scheduler.should("indicators"):
                for sym in CONFIG["symbols"]:
                    ind = indicator_engine.calc(sym)
                    if ind:
                        indicators[sym] = ind
                # ATR 动态止损同步（指标更新后跑一次）
                if update_atr_dynamic_stops(positions, indicators):
                    save_positions(positions)

            # ──────────────────────────────────
            # Step 4: 情绪数据（300秒一次）
            # ──────────────────────────────────
            if scheduler.should("sentiment"):
                fg_cache = sentiment_engine.fear_greed()
            if scheduler.should("funding"):
                fr_cache = sentiment_engine.funding_rates(CONFIG["symbols"])

            # ──────────────────────────────────
            # Step 5: 开仓评分（60秒一次）
            # ──────────────────────────────────
            if scheduler.should("indicators") and indicators:
                # 熔断检查
                allowed, reason = circuit_breaker.is_trading_allowed()
                if not allowed:
                    log.info(f"交易暂停: {reason}")
                else:
                    btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 作为大盘参考
                    
                    # 🐛 Bug 修复：API 是真相，本地是缓存
                    # 优先相信 API，只有当 API 失败时才用本地缓存
                    current_pos = get_all_positions()
                    api_count = sum(1 for p in current_pos 
                        if float(p.get("amount", 0)) != 0
                        and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0)
                    local_count = len(positions)
                    
                    # API 返回 0 时，说明真的没有持仓（刚平仓）
                    # API 有数据时，相信 API
                    # API 失败/异常时，才用本地缓存
                    if api_count > 0:
                        actual_count = api_count  # 相信 API
                    else:
                        actual_count = local_count  # API 为空，用本地（可能是刚启动）
                    
                    log.info(f"📊 开仓前检查：API 持仓={api_count}, 本地缓存={local_count}, 采用={actual_count}/{CONFIG['max_positions']}")
                    
                    # 开仓计数器（本循环内累加）
                    opened_this_loop = 0

                    for sym in CONFIG["symbols"]:
                        # 检查是否已满仓（包含本循环已开的仓位）
                        if actual_count + opened_this_loop >= CONFIG["max_positions"]:
                            log.info(f"⚠️ {sym} 已达最大持仓数，跳过开仓")
                            break
                        if not indicators.get(sym) or not prices.get(sym):
                            continue
                        if not news_filter.check_suspend(sym):
                            continue

                        ind    = indicators[sym]
                        pd     = prices[sym]
                        regime = detect_regime(ind)
                        fr     = fr_cache.get(sym, 0)

                        score, direction = calc_score(
                            sym, pd, ind, regime, fg_cache, fr, btc_ind
                        )

                        if direction == "NONE":
                            continue
                        if not cooldown_manager.can_open(sym, direction):
                            continue

                        # 同一币种不能重复开仓 - 2026-04-27 老公指示：API 真相优先
                        # 本地 positions 缓存可能滞后（多进程/race condition），必须从 Binance API 实时查
                        try:
                            api_positions_check = get_all_positions() or []
                        except Exception as _e:
                            log.warning(f"⚠️ 检查持仓时 API 异常：{_e}，回退本地缓存")
                            api_positions_check = positions
                        api_has_symbol = any(
                            p.get("symbol") == sym 
                            and float(p.get("amount", 0)) != 0
                            and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0
                            for p in api_positions_check
                        )
                        local_has_symbol = any(p["symbol"] == sym for p in positions)
                        if api_has_symbol or local_has_symbol:
                            log.info(f"⚠️ {sym} 已有持仓（api={api_has_symbol}, local={local_has_symbol}），跳过重复开仓")
                            continue

                        # 2026-03-28 老公指示：移除同方向限制，只保留总持仓数限制
                        # 原因：同方向 0-1U 浮动的仓位占位置，导致其他信号进不来

                        # ═══════════════════════════════════════
                        # AI 触发条件判断（2026-03-28 老公指示）
                        # ═══════════════════════════════════════
                        # 满仓时只保留新闻和浮亏判断，不做开仓确认
                        is_full = len(positions) >= CONFIG["max_positions"]
                        
                        should_trigger_ai = False
                        
                        # 条件 1：有开仓信号且未满仓
                        if not is_full and score >= CONFIG["score_threshold"][regime]:
                            should_trigger_ai = True
                            log.info(f"🤖 {sym} 触发 AI：开仓信号确认 ({score}分)")
                        
                        # 条件 2：重大新闻（满仓也触发）
                        if news_filter.has_major_news(sym):
                            should_trigger_ai = True
                            log.info(f"🤖 {sym} 触发 AI：重大新闻")
                        
                        # 条件 3：持仓浮亏超 1%，2 小时冷却
                        for pos in positions:
                            if pos["symbol"] == sym:
                                p = prices.get(sym, {}).get("price", 0)
                                entry = pos["entry_price"]
                                pnl_pct = (p-entry)/entry if pos["type"]=="LONG" else (entry-p)/entry
                                last_trigger = ai_loss_cooldown.get(sym, 0)
                                if pnl_pct < -0.02 and time.time() - last_trigger > 7200:
                                    should_trigger_ai = True
                                    ai_loss_cooldown[sym] = time.time()
                                    log.info(f"🤖 {sym} 触发 AI：浮亏{pnl_pct*100:.1f}%")
                        
                        # 只有满足条件才调用 AI
                        if should_trigger_ai:
                            ai_result = ai_predictor.predict(sym, direction, ind, pd, fg_cache)
                        else:
                            ai_result = None  # 纯数学决策
                            log.info(f"🤖 {sym} 跳过 AI（评分{score}分，纯数学决策）")

                        # AI与数学信号冲突时降级观望
                        if ai_result:
                            ai_dir = ai_result.get("direction", "")
                            conflict = (
                                (direction == "LONG"  and ai_dir in ["做空", "震荡"]) or
                                (direction == "SHORT" and ai_dir in ["做多", "震荡"])
                            )
                            # 🐛 修复：AI 说震荡 → 无条件观望；AI 反向 + 高信心 → 观望
                            if ai_dir == "震荡":
                                log.info(f"⏸️ {sym} AI判断震荡市，观望（数学={direction}，AI=震荡）")
                                continue
                            if conflict and ai_result.get("confidence", 0) >= 70:
                                log.info(f"⏸️ {sym} AI与数学信号冲突({ai_result.get('confidence')}%)，观望")
                                continue

                        # 开仓前再次检查持仓数（双重保险）
                        # 🐛 Bug 修复：API 是真相，本地是缓存
                        current_pos = get_all_positions()
                        api_count = sum(1 for p in current_pos 
                            if float(p.get("amount", 0)) != 0
                            and abs(float(p.get("amount", 0))) * float(p.get("entry_price", 0)) >= 5.0)
                        local_count = len(positions)
                        
                        # API 有数据时相信 API，否则用本地
                        if api_count > 0:
                            actual_count = api_count + opened_this_loop
                        else:
                            actual_count = local_count + opened_this_loop
                        
                        if actual_count >= CONFIG["max_positions"]:
                            log.info(f"⚠️ {sym} 已达最大持仓数 {actual_count}/{CONFIG['max_positions']}，跳过开仓")
                            continue
                        
                        entry = pd["price"]
                        tp_sl = calc_tp_sl(entry, direction, ind, pd)
                        pos = open_position(sym, direction, entry, score, tp_sl, regime, ai_result)
                        # 只有订单成功才记录持仓和冷却
                        if pos.get("order_result", {}).get("success", True):
                            positions.append(pos)
                            cooldown_manager.record(sym, direction)
                            save_positions(positions)
                            opened_this_loop += 1  # 累加本循环开仓数
                            log.info(f"✅ {sym} 开仓成功，当前持仓：{len(positions)}/{CONFIG['max_positions']}")
                        else:
                            # ✅ 2026-03-24 修复：失败也要记录冷却，防止循环重试刷屏
                            cooldown_manager.record(sym, direction)
                            log.error(f"❌ {sym} 开仓失败，已记录冷却 300 秒")

            # 刷新推送缓冲区（合并同一币种的信号）
            merged_signals = cooldown_manager.flush_push_buffer()
            for signal in merged_signals:
                _send_push_signal(signal)

            # ──────────────────────────────────
            # Step 6: 整点汇报（每小时一次，重启后自动补发）
            # ──────────────────────────────────
            current_hour = datetime.now().hour
            current_time = time.time()
            
            # 整点汇报：每小时第一次检查时触发 + 距离上次推送至少 30 分钟
            if current_hour != last_report_hour and (current_time - last_report_time) > 1800:
                # 整点汇报：每小时第一次检查时触发（不依赖 tick）
                hourly_report(
                    positions, prices, indicators, circuit_breaker, fg_cache, indicator_engine
                )
                last_report_hour = current_hour
                last_report_time = current_time  # 记录推送时间

            # 保存状态（包含 peak_pnl 用于移动止盈）
            save_state({
                "last_update": datetime.now().isoformat(),
                "prices":      {k: v.get("price") for k, v in prices.items()},
                "positions":   len(positions),
                "daily_pnl":   circuit_breaker.daily_pnl,
                "daily_pnl_date": datetime.now().date().isoformat(),
                "fear_greed":  fg_cache,
                "tick":        scheduler.tick,
                "last_report_hour": last_report_hour,
                "last_report_time": last_report_time,
                "peak_pnl_map":  {p["symbol"]: p.get("peak_pnl", 0.0) for p in positions},  # 保存峰值盈亏
            })

        except Exception as e:
            log.error(f"主循环异常: {e}", exc_info=True)

        # 精确15秒间隔
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0, CONFIG["scan_interval"] - elapsed))


if __name__ == "__main__":

    asyncio.run(main())
