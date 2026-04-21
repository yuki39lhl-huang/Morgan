#!/usr/bin/env python3
"""
crypto_signal_monitor.py
加密货币自动交易策略 v6.0
优化：WebSocket实时价格 + 分层扫描 + 市场状态识别 + 双TP出场 + 熔断保护
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
from collections import deque
from typing import Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp 未安装，运行 pip install aiohttp")

# ─────────────────────────────────────────────
# 日志（定义在导入前，避免 NameError）
# ─────────────────────────────────────────────
LOG_FILE = "/root/.openclaw/workspace/crypto_monitor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        # logging.StreamHandler(),  # 禁用控制台输出，防止重复
    ],
)
log = logging.getLogger(__name__)

# 添加脚本目录到路径，以便导入 binance_auto_trade
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自动交易模块导入
try:
    from binance_auto_trade import place_order, get_all_positions, close_all_positions, cancel_all_orders, request, format_quantity
    AUTO_TRADE_ENABLED = True
    log.info("✅ 自动交易模块已加载")
except Exception as e:
    AUTO_TRADE_ENABLED = False
    log.warning(f"⚠️ 自动交易模块未加载：{e}")

# ─────────────────────────────────────────────
# 配置区
# ─────────────────────────────────────────────
CONFIG = {
    # 交易标的
    "symbols": ["BTC", "ETH", "SOL", "BNB", "DOT", "LINK", "XRP"],  # 2026-03-30 老公指示：加回 XRP

    # 资金管理
    "total_capital": 100,          # USDT（模拟盘）
    "position_size_pct": 0.20,     # 单仓 20% - 2026-03-28 老公指示
    "max_positions": 4,

    # 开仓阈值（按市场状态分层）- 2026-03-26 老公指示：提高门槛
    "score_threshold": {
        "trending":  70,
        "ranging":   30,  # 临时测试
        "volatile":  90,
    },

    # 基础止盈止损（ATR动态覆盖）
    "base_tp_pct":  0.08,
    "base_sl_pct":  0.05,

    # 双TP配置
    "tp1_atr_mult": 3.0,  # 2026-03-25 老公指示：ATR×3.0，平 50% 仓
    "tp2_atr_mult": 6.0,  # 2026-03-25 老公指示：ATR×6.0，平剩余

    # 移动止盈 - 2026-03-30 老公指示：关闭移动止盈，严格固定 -2% 止损
    # 2026-03-30 老公指示：三档移动止盈（回撤比例）
    "trailing_tiers": [
        {"pnl": 0.02, "gap": 0.008},  # 2% 时，回撤 0.8% → 锁定 1.2%（2.4U @20U）
        {"pnl": 0.03, "gap": 0.01},   # 3% 时，回撤 1% → 锁定 2%（4U @20U）⭐️ 老公指示
        {"pnl": 0.04, "gap": 0.999},  # 4% 时，全平 → 锁定 4%（8U @20U）
    ],

    # ATR止损倍数分层
    "atr_sl_tiers": [
        (0.02, 1.0),   # ATR% < 2%  →  止损1.0倍ATR
        (0.04, 1.5),   # ATR% < 4%  →  止损1.5倍ATR
        (9999, 2.0),   # ATR% >= 4% →  止损2.0倍ATR
    ],

    # 熔断
    "circuit_breaker_pct": -0.10,   # 日亏损超10%触发
    "circuit_breaker_hours": 4,     # 暂停4小时

    # 信号冷却
    "signal_cooldown": {
        "same_direction": 300,    # 同向5分钟
        "opposite":       0,      # 反向立即
    },

    # AI调用限流
    "ai_max_per_hour": 10,
    "ai_min_interval": 180,       # 两次调用最少间隔3分钟

    # 新闻过滤
    "news_blacklist": [
        "hack", "exploit", "ban", "lawsuit", "regulation",
        "crackdown", "freeze", "arrest", "breach", "exit scam"
    ],
    "news_whitelist": [
        "ETF", "approval", "partnership", "institutional",
        "adoption", "upgrade", "launch"
    ],
    "news_suspend_hours": 2,

    # API配置
    "binance_base":    "https://api.binance.com/api/v3",
    "binance_futures": "https://fapi.binance.com/fapi/v1",
    "proxy":           "http://127.0.0.1:7890",  # 2026-03-30 修复：Clash 端口 7890

    # Qwen AI - 2026-03-24 老公指示：改成和聊天同一个 API
    "qwen_url":  "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "qwen_key":  "sk-sp-ae2a006db7b040919e022851a261e0f1",
    "qwen_model": "qwen-plus",

    # 文件路径
    "positions_file":     "crypto_positions.json",
    "state_file":         "crypto_state.json",
    "llm_usage_file":     "llm_usage.json",
    "position_highs_file":"position_highs.json",
    "log_file":           "/root/.openclaw/workspace/crypto_monitor.log",
    "alert_file":         "crypto_alert.txt",

    # 扫描
    "scan_interval": 15,
    
    
    # 飞书推送配置
    "feishu_app_id": "cli_a92eff25e5789cbd",
    "feishu_app_secret": "cRcwedFpvtCzOxcQmNktJc2bivnAS4zs",
    "feishu_chat_id": "oc_37fa1adc4c8987639b46fdffb9ce4ed8",
    "feishu_token_cache": "feishu_token.json",
}

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
                                    self.prices[sym] = {
                                        "price":      float(t["c"]),
                                        "high_24h":   float(t["h"]),
                                        "low_24h":    float(t["l"]),
                                        "volume":     float(t["v"]),
                                        "change_24h": float(t["P"]),
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
        """获取 K 线，带 3 次重试（无阻塞）"""
        for attempt in range(5):  # 2026-03-31 优化：3->5 次
            try:
                r = self.session.get(
                    f"{CONFIG['binance_base']}/klines",
                    params={"symbol": f"{symbol}USDT", "interval": interval, "limit": limit},
                    timeout=15,  # 2026-03-31 优化：8->15 秒
                )
                if r.status_code == 200:
                    return r.json()
            except Exception as e:
                if attempt < 4:
                    log.warning(f"K 线重试 {attempt+1}/3 {symbol}: {e}")
                else:
                    log.error(f"K 线失败 {symbol} (5 次重试耗尽): {e}")
        return []


    def get_24h_klines(self, symbol: str) -> list:
        """Get 24h klines with 3-layer fallback"""
        # 1. Binance 1h klines (60 bars = 24h)
        klines = self._get_klines(symbol, "1h", 60)
        if klines and len(klines) >= 10:
            return klines
        
        # 2. Binance 24hr Ticker synthetic kline
        try:
            r = self.session.get(
                f"{CONFIG['binance_base']}/ticker/24hr",
                params={"symbol": f"{symbol}USDT"},
                timeout=15,  # 2026-03-31 优化：8->15 秒
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
        """情绪加分，最多+15分"""
        bonus = 0
        if direction == "LONG":
            if fg < 25:       bonus += 10   # 极度恐惧做多
            elif fg < 40:     bonus += 5
            if fr < -0.001:   bonus += 5    # 空头付费，多头有利
        elif direction == "SHORT":
            if fg > 75:       bonus += 10   # 极度贪婪做空
            elif fg > 60:     bonus += 5
            if fr > 0.002:    bonus += 5    # 多头付费过高，反转信号
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

    price      = price_data["price"]
    vol        = price_data["volume"]
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
    btc_ind = ind.get("btc_ind", {})
    if btc_ind:
        btc_bull = btc_ind.get("ema20_15m", 0) > btc_ind.get("ema60_15m", 0)
        if btc_bull:
            long_score  += 10
            short_score -= 5
        else:
            short_score += 10
            long_score  -= 5

    long_score  = max(0, long_score)
    short_score = max(0, short_score)

    threshold = CONFIG["score_threshold"][regime]
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
    
    2026-03-28 老公指示：固定百分比止盈止损 + 全平
    - 止盈：+4%（TP1 全平 100%）
    - 止损：-2%
    - 无 TP2（已废弃）
    """
    TAKE_PROFIT_PCT = 0.04  # 2026-03-30 老公指示：改回 4%（8U）
    STOP_LOSS_PCT   = 0.01  # 保持 1%
    
    if direction == "LONG":
        tp1 = entry * (1 + TAKE_PROFIT_PCT)  # 做多 +4%
        sl  = entry * (1 - STOP_LOSS_PCT)    # 做多 -1%
    else:
        tp1 = entry * (1 - TAKE_PROFIT_PCT)  # 做空 -4%
        sl  = entry * (1 + STOP_LOSS_PCT)    # 做空 +1%

    return {
        "tp1_price": round(tp1, 4),
        "sl_price":  round(sl, 4),
        "peak_pnl":  0.0,   # 移动止盈用
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
    移动止盈 - 2026-03-30 老公指示：三档移动止盈（从峰值价回撤）
    2% → 回撤 0.8% → 锁定 1.2%
    3% → 回撤 1% → 锁定 2% ⭐️ 老公指示
    4% → 全平
    
    2026-03-31 老公指示：每次更新 sl_price 后同步到 Binance（取消旧单 + 提交新单）
    """
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        entry = pos["entry_price"]
        typ   = pos["type"]

        pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry

        # 更新峰值盈亏 和 峰值价格
        if pnl_pct > pos.get("peak_pnl", 0):
            pos["peak_pnl"] = pnl_pct
            pos["peak_price"] = p  # ✅ 记录峰值价格

        # 找到当前利润对应的档位（取最高档）
        active_tier = None
        peak_pnl = pos.get("peak_pnl", 0)  # ✅ 兼容旧数据
        for tier in CONFIG["trailing_tiers"]:
            if peak_pnl >= tier["pnl"]:
                active_tier = tier
            else:
                break

        if not active_tier:
            continue

        # 4% 全平特殊处理
        if active_tier.get("gap", 0) >= 0.9:
            old_sl = pos["sl_price"]
            pos["sl_price"] = p * 0.999 if typ == "LONG" else p * 1.001
            # 同步到 Binance
            sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl)
            continue
        
        # 回撤比例（老公指示）- 从峰值价回撤！
        gap = active_tier["gap"]
        peak_p = pos.get("peak_price", entry)  # ✅ 用峰值价，不是当前价
        
        if typ == "LONG":
            new_sl = peak_p * (1 - gap)  # 峰值价回撤 gap%
            if new_sl > pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                # 同步到 Binance（取消旧单 + 提交新单）
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl)
        else:
            new_sl = peak_p * (1 + gap)  # 峰值价回撤 gap%
            if new_sl < pos["sl_price"]:
                old_sl = pos["sl_price"]
                pos["sl_price"] = round(new_sl, 4)
                # 同步到 Binance（取消旧单 + 提交新单）
                sync_stop_loss_to_binance(sym, typ, pos["sl_price"], old_sl)

def sync_stop_loss_to_binance(symbol: str, typ: str, new_sl: float, old_sl: float):
    """
    2026-03-31 老公指示：同步止损价到 Binance
    取消旧止损单 + 提交新止损单
    """
    try:
        sym_usdt = f"{symbol}USDT"
        sl_side = "SELL" if typ == "LONG" else "BUY"
        
        # 1. 取消所有挂单（包括旧止损单）
        cancel_result = request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": sym_usdt})
        
        # 2. 提交新止损单
        sl_result = request("POST", "/fapi/v1/order", {
            "symbol": sym_usdt,
            "side": sl_side,
            "type": "STOP_MARKET",
            "stopPrice": str(new_sl),
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "timeInForce": "GTE_GTC"
        })
        
        # 检查是否成功
        if "error" in str(sl_result):
            log.warning(f"⚠️ {symbol} 更新 Binance 止损单失败：{sl_result}")
        else:
            log.info(f"📌 {symbol} 止损单已更新 Binance：{old_sl:.4f} → {new_sl:.4f}")
    except Exception as e:
        log.warning(f"⚠️ {symbol} 同步止损单异常：{e}")

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
        # 2026-03-31 22:07 老公指示：清除熔断
        self.paused_until = None
        self.consecutive_losses = 0
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

        # 同向冷却
        elapsed = now - self._last.get(same_key, 0)
        if elapsed < CONFIG["signal_cooldown"]["same_direction"]:
            log.info(f"⏳ {symbol} {direction} 冷却中，还需 {int(CONFIG['signal_cooldown']['same_direction']-elapsed)}秒")
            return False

        self._last.pop(opp_key, None)
        return True

    def record(self, symbol: str, direction: str):
        self._last[f"{direction}_{symbol}"] = time.time()

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
# 十、AI 预测（限流保护）
# ═══════════════════════════════════════════════════════════════
class AIPredictor:

    def __init__(self):
        self.call_times: deque = deque()
        self.last_call = 0

    def _can_call(self) -> tuple[bool, str]:
        now = time.time()
        self.call_times = deque(t for t in self.call_times if now - t < 3600)
        if len(self.call_times) >= CONFIG["ai_max_per_hour"]:
            return False, "每小时AI调用已满"
        if now - self.last_call < CONFIG["ai_min_interval"]:
            remain = int(CONFIG["ai_min_interval"] - (now - self.last_call))
            return False, f"AI冷却中({remain}s)"
        return True, "ok"

    def predict(self, symbol: str, direction: str, ind: dict, price_data: dict, fg: int = 50) -> Optional[dict]:
        ok, reason = self._can_call()
        if not ok:
            log.info(f"AI跳过({reason})，使用纯数学决策")
            return None

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
            # ✅ 2026-04-01 修复：兼容旧的 qty 字段
            if "amount" not in pos and "qty" in pos:
                pos["amount"] = pos["qty"]
                log.info(f"🔧 持仓 {pos['symbol']} 兼容转换：qty={pos['qty']} → amount")
            # 2026-03-28 老公指示：已删除 tp1_hit, size_remaining
        
        return positions
    except Exception as e:
        log.warning(f"⚠️ 加载持仓失败：{e}")
        return []


def save_positions(positions: list):
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
    # 高波动时减仓
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
        "score":        score,
        "regime":       regime,
        "ai_result":    ai_result,
        "order_result": order_result,
        "high_24h":     0.0,
        "low_24h":      0.0,
        **tp_sl,
    }

    # 检查订单是否真正成功
    order_success = True
    if order_result:
        order_success = order_result.get('success', True)
    
    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    if not order_success:
        msg += f" | ❌ 下单失败"
    else:
        msg += f" | ✅ 下单成功"
    log.info(msg)
    write_alert(msg)
    
    # 只有订单成功才推送飞书（走缓冲区合并，避免刷屏）
    if order_success:
        push_signal_alert({
            'symbol': symbol,
            'type': direction,
            'action': '开多' if direction == 'LONG' else '开空',
            'price': entry_price,
            'score': score,
            'regime': regime,
            'tp_price': pos.get('tp_price', 0),
            'sl_price': pos.get('sl_price', 0),
        }, immediate=False)  # 改为缓冲模式
    
    return pos


def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]
    symbol = pos["symbol"]
    qty_raw = pos.get("qty", 0) * size_ratio
    
    qty = format_quantity(symbol, qty_raw, entry)

    # 实盘平仓调用（使用 place_order with reduce_only=True）
    # 平仓重试机制（最多 3 次）
    close_result = None
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        for attempt in range(5):  # 2026-03-31 优化：3->5 次
            try:
                side = "SELL" if typ == "LONG" else "BUY"
                # 🐛 Bug 修复：传递 current_price 用于日志显示正确价格
                close_result = place_order(
                    symbol=f"{symbol}USDT",
                    side=side,
                    quantity=qty,
                    leverage=10,  # 2026-03-27 老公指示：3x→10x 测试两天
                    reduce_only=True,
                    price=current_price,  # 传递当前价格用于日志
                )
                log.info(f"📝 平仓结果：{close_result}")
                # 成功就跳出重试
                if close_result.get('success', True):
                    break
                else:
                    log.warning(f"⚠️ 平仓第{attempt+1}次失败，{'重试' if attempt < 2 else '放弃'}")
                    if attempt < 4:
                        time.sleep(2)
            except Exception as e:
                log.error(f"❌ 平仓异常第{attempt+1}次：{e}")
                close_result = {"error": str(e), "success": False}
                if attempt < 4:
                    time.sleep(2)

    # 计算盈亏 - 2026-03-30 老公指示：添加杠杆倍数
    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    LEVERAGE = 10
    pnl_usdt = CONFIG["total_capital"] * CONFIG["position_size_pct"] * LEVERAGE * pnl_pct * size_ratio
    # 2026-03-25 老公指示：用实际成交价计算真实盈亏
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
        # 确保 score 和 regime 有值（从持仓中读取，如果没有则用 0/unknown）
        score = pos.get('score')
        if score is None or score == '':
            score = 0
        regime = pos.get('regime')
        if regime is None or regime == '':
            regime = 'unknown'
        
        push_signal_alert({
            'symbol': symbol,
            'type': typ,
            'action': '止盈' if 'TP' in reason else '止损',
            'price': current_price,
            'score': score,
            'regime': regime,
            'pnl': pnl_usdt,
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
# 十三、飞书推送
# ═══════════════════════════════════════════════════════════════
def get_feishu_token(force_refresh: bool = False) -> Optional[str]:
    """获取飞书 tenant_access_token（带缓存）"""
    cache_file = CONFIG["feishu_token_cache"]
    now = time.time()
    
    # 强制刷新时跳过缓存
    if force_refresh and os.path.exists(cache_file):
        try:
            os.remove(cache_file)
            log.info("🔄 强制刷新飞书 token")
        except Exception as e:
            log.warning(f"删除旧 token 失败：{e}")
    
    # 尝试读取缓存
    try:
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                cache = json.load(f)
            if now - cache.get('time', 0) < 7000:  # token 有效期 2 小时，7000 秒安全边际
                return cache.get('token')
    except Exception as e:
        log.warning(f"读取飞书 token 缓存失败：{e}")
    
    # 获取新 token
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": CONFIG["feishu_app_id"],
        "app_secret": CONFIG["feishu_app_secret"]
    }
    try:
        # 飞书 API 不能走代理，必须直连
        r = requests.post(url, json=payload, timeout=10, proxies=None)
        data = r.json()
        if data.get('code') == 0:
            token = data.get('tenant_access_token')
            # 缓存 token
            with open(cache_file, 'w') as f:
                json.dump({'token': token, 'time': now}, f)
            log.info("✅ 飞书 token 已更新")
            return token
        else:
            log.error(f"❌ 获取飞书 token 失败：{data}")
    except Exception as e:
        log.error(f"❌ 获取飞书 token 异常：{e}")
    return None


def push_feishu_card(title: str, elements: list, template: str = "blue"):
    """推送飞书卡片消息"""
    token = get_feishu_token()
    if not token:
        log.warning("⚠️ 飞书 token 缺失，跳过推送")
        return False
    
    # 构建卡片内容（飞书格式）
    card_content = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title}
        },
        "elements": elements
    }
    
    # 发送消息
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    # 注意：content 字段必须是 JSON 字符串，不能用 json=payload（会双重序列化）
    payload = {
        "receive_id": CONFIG["feishu_chat_id"],
        "msg_type": "interactive",
        "content": json.dumps(card_content, ensure_ascii=False)
    }
    
    try:
        # 使用 data 而不是 json，避免 content 字段被二次序列化
        # 飞书 API 不能走代理，必须直连
        r = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False), timeout=10, proxies=None)
        data = r.json()
        if data.get('code') == 0:
            log.info("✅ 飞书推送成功")
            return True
        else:
            # token 过期时强制刷新并重试
            if data.get('code') == 99991663:
                log.warning("⚠️ 飞书 token 过期，强制刷新后重试...")
                token = get_feishu_token(force_refresh=True)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    r = requests.post(url, headers=headers, data=json.dumps(payload, ensure_ascii=False), timeout=10)
                    data = r.json()
                    if data.get('code') == 0:
                        log.info("✅ 飞书推送成功（刷新 token 后）")
                        return True
            log.error(f"❌ 飞书推送失败：{data}")
            return False
    except Exception as e:
        log.error(f"❌ 飞书推送异常：{e}")
        return False


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
    
    # 构建卡片元素（使用 div 标签）
    content = f"**{emoji} 币种：** {symbol}\n"
    content += f"**📊 方向：** {type_text}\n"
    content += f"**💰 价格：** ${price:,.2f}\n"
    content += f"**📈 评分：** {score}/100\n"
    content += f"**🎯 状态：** {regime}\n"
    
    # 开仓信号添加止盈止损
    if action in ['开多', '开空'] and tp_price and sl_price:
        content += f"**🎯 止盈：** ${tp_price:,.2f}\n"
        content += f"**🛑 止损：** ${sl_price:,.2f}\n"
    
    # 平仓信号添加盈亏
    if action in ['止盈', '止损', '平仓']:
        content += f"**💰 盈亏：** {pnl:+.2f} USDT\n"
        if merged_count and merged_count > 1:
            content += f"**📦 合并：** {merged_count} 次平仓\n"
    
    # 如果有合并的平仓盈亏
    if merged_pnl != 0:
        content += f"**💰 累计盈亏：** {merged_pnl:+.2f} USDT\n"
        if merged_close_count:
            content += f"**📦 包含：** {merged_close_count} 次平仓\n"
    
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
    
    title = f"🚨 {action} - {symbol}"
    push_feishu_card(title, elements, template)


def push_hourly_report(positions: list, prices: dict, indicators: dict, daily_pnl: float, fg: int):
    """推送整点汇报（显示所有币种的价格和评分）"""
    from binance_auto_trade import request as api_request
    
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
    
    # 构建持仓列表（包含价格、评分、盈亏）
    pos_lines = []
    log.info(f"📋 整点汇报：持仓数={len(positions)}, prices 缓存={len(prices)}")
    for pos in positions:
        symbol = pos['symbol']
        pos_type = pos['type']
        entry_price = pos['entry_price']
        amount = abs(pos.get('amount', 0))
        
        # 🐛 调试日志
        log.info(f"  📍 {symbol} {pos_type}: entry={entry_price}, amount={amount}, current_price={prices.get(symbol, {}).get('price', 0)}")
        
        # ✅ 2026-04-01 老公指示：从 WebSocket 价格缓存取实时价格
        current_price = prices.get(symbol, {}).get('price', entry_price)
        
        # ✅ 用 WebSocket 价格重新计算盈亏（确保价格和盈亏一致）
        if pos_type == 'LONG':
            unrealized_pnl = (current_price - entry_price) * amount
        else:
            unrealized_pnl = (entry_price - current_price) * amount
        
        # 计算盈亏百分比（用名义价值）
        notional = entry_price * amount
        pnl_pct = (unrealized_pnl / notional * 100) if notional > 0 else 0
        
        # ✅ 实时计算评分（和全部币种列表一致）
        ind = indicators.get(symbol, {})
        regime = detect_regime(ind)
        fr = 0
        btc_ind = indicators.get("BTC", {})  # 2026-03-28 老公指示：用 BTC 作为大盘参考
        score, direction = calc_score(symbol, prices.get(symbol, {}), ind, regime, fg, fr, btc_ind)
        
        pos_lines.append(f"**{symbol}** {pos_type} | 开仓：${entry_price:,.2f} | 当前：${current_price:,.2f} | 评分：{score} | 盈亏：${unrealized_pnl:+.2f} ({pnl_pct:+.2f}%)")
    
    # 构建所有币种的价格和评分列表
    all_coins_lines = []
    
    # 如果 indicators 为空，实时计算指标
    if not indicators:
        log.warning("⚠️ 整点汇报时 indicators 为空，实时计算指标...")
        indicator_engine = IndicatorEngine()
        for sym in CONFIG["symbols"]:
            ind = indicator_engine.calc(sym)
            if ind:
                indicators[sym] = ind
    
    for symbol in CONFIG["symbols"]:
        if prices.get(symbol):
            price = prices[symbol].get('price', 0)
            change_24h = prices[symbol].get('change_24h', 0)
            
            # 计算评分
            ind = indicators.get(symbol, {})
            regime = detect_regime(ind)
            fr = 0
            btc_ind = indicators.get("BTC", {})
            score, direction = calc_score(symbol, prices[symbol], ind, regime, fg, fr, btc_ind)
            
            # 标记持仓状态
            holding = "📌" if any(p['symbol'] == symbol for p in positions) else "  "
            
            all_coins_lines.append(f"{holding} **{symbol}** ${price:,.2f} ({change_24h:+.2f}%) | 评分：{score} | 信号：{direction}")
    
    # 构建卡片元素（使用 div 标签）
    content_lines = [
        f"**📊 今日盈亏：** {daily_pnl:+.2f} USDT",
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
        with open(CONFIG["alert_file"], "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def save_state(state: dict):
    with open(CONFIG["state_file"], "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, default=str)


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
def hourly_report(positions: list, prices: dict, indicators: dict, circuit: CircuitBreaker, fg: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
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
                    if amt != 0:
                        symbol = p['symbol']
                        # 🐛 Bug 修复：从 API 同步时计算合理的止盈止损（基于 entry_price ±3%）
                        entry = float(p.get('entry_price', 0))
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
                            'peak_pnl': 0.0
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
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price", 0)
        entry = pos["entry_price"]
        typ = pos["type"]
        pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry
        lines.append(
            f"  {'🟢' if typ=='LONG' else '🔴'} {sym} {typ} @{entry:.4f} "
            f"现价:{p:.4f} PnL:{pnl_pct*100:+.2f}%"
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
        success = push_hourly_report(positions, prices, indicators, circuit.daily_pnl, fg)
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

    # 初始化各模块
    price_stream = PriceStream(CONFIG["symbols"], proxy=CONFIG["proxy"])
    indicator_engine = IndicatorEngine()
    sentiment_engine = SentimentEngine()
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
            log.info(f'📊 加载本地缓存 {len(old_positions)} 个持仓用于保留止盈止损')
            
            # 过滤有实际持仓的币种
            positions = []
            for p in api_positions:
                amt = float(p.get('amount', 0))
                if amt != 0:
                    # 转换为本地格式
                    symbol = p['symbol']
                    # 2026-03-30 老公指示：用新百分比重新计算止盈止损（2%/1%）
                    entry = float(p.get('entry_price', 0))
                    direction = 'SHORT' if amt < 0 else 'LONG'
                    tp_sl = calc_tp_sl(entry, direction, {}, {})  # 重新计算，不保留旧缓存
                    
                    pos = {
                        'symbol': symbol,
                        'type': direction,
                        'entry_price': entry,
                        'entry_time': datetime.now().isoformat(),
                        'qty': abs(amt),
                        'score': 55,
                        'regime': 'trending',
                        'ai_result': None,
                        'order_result': {'success': True, 'message': '从 API 同步'},
                        'high_24h': 0.0,
                        'low_24h': 0.0,
                        'tp1_price': tp_sl['tp1_price'],  # 新 2% 止盈
                        'sl_price': tp_sl['sl_price'],    # 新 1% 止损
                        'peak_pnl': 0.0
                    }
                    positions.append(pos)
                    log.info(f"✅ 同步持仓：{symbol} {pos['type']} @ ${pos['entry_price']:.2f}")
        save_positions(positions)
        log.info(f"✅ 持仓同步完成：{len(positions)}个")
    except Exception as e:
        log.error(f"❌ 持仓同步失败：{e}")
        positions = load_positions()  # 回退到本地文件

    # 从状态文件恢复上次推送时间、今日盈亏和峰值盈亏（防止重启后丢失）
    last_report_hour = -1
    last_report_time = 0
    realized_pnl = 0.0  # 已实现盈亏（今日）
    peak_pnl_map = {}   # 各币种峰值盈亏（用于移动止盈）
    try:
        if os.path.exists(CONFIG["state_file"]):
            with open(CONFIG["state_file"]) as f:
                state = json.load(f)
                if state.get("last_report_hour"):
                    last_report_hour = state["last_report_hour"]
                if state.get("last_report_time"):
                    last_report_time = state["last_report_time"]
                if state.get("daily_pnl"):
                    realized_pnl = state.get("daily_pnl", 0.0)
                    circuit_breaker.daily_pnl = realized_pnl
                if state.get("peak_pnl_map"):
                    peak_pnl_map = state.get("peak_pnl_map", {})
                # 2026-03-31 22:07 老公指示：清除熔断
                realized_pnl = 0.0  # 重置日盈亏
                circuit_breaker.daily_pnl = 0.0  # ✅ 同时重置 circuit_breaker
                log.info(f"📋 恢复状态：last_report_hour={last_report_hour}, daily_pnl={realized_pnl:+.2f}U, peak_pnl={peak_pnl_map} ✅ 熔断已清除")
    except Exception as e:
        log.warning(f"⚠️ 加载状态文件失败：{e}")

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
                    api_count = sum(1 for p in api_positions if float(p.get('amount', 0)) != 0)
                
                # 只有 API 成功且确实 0 持仓时，才清空缓存
                if api_count == 0:
                    if len(positions) > 0:
                        log.info(f"🔄 持仓同步：API 持仓=0，清空本地缓存（原{len(positions)}个）")
                        positions = []
                        save_positions(positions)
                        log.info("✅ 持仓同步完成：0 个（API 为空）")
                elif api_count > 0 and api_count != len(positions):
                    log.info(f"🔄 持仓同步：本地{len(positions)}个 → API{api_count}个")
                    new_positions = []
                    for p in api_positions:
                        amt = float(p.get('amount', 0))
                        if amt != 0:
                            symbol = p['symbol']
                            entry = float(p.get('entry_price', 0))
                            # 重建止盈止损字段（防止 KeyError）
                            atr = abs(entry * 0.015)  # 估算 ATR
                            tp1 = round(entry * (1 - 0.015) if amt < 0 else entry * (1 + 0.015), 4)
                            tp2 = round(entry * (1 - 0.03) if amt < 0 else entry * (1 + 0.03), 4)
                            sl = round(entry * (1 + 0.008) if amt < 0 else entry * (1 - 0.008), 4)
                            new_positions.append({
                                'symbol': symbol,
                                'type': 'SHORT' if amt < 0 else 'LONG',
                                'entry_price': entry,
                                'amount': abs(amt),  # ✅ 2026-04-01 修复：qty → amount
                                'tp1_price': tp1,
                                'tp2_price': tp2,
                                'sl_price': sl,
                                'tp1_hit': False,
                                'size_remaining': 1.0,
                            })
                    positions = new_positions
                    save_positions(positions)
                    log.info(f"✅ 持仓同步完成：{len(positions)}个")
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

                # 🚨 2026-03-29 老公指示：平仓成功后立即从本地删除，不等下次同步
                if reason == "TP1":
                    # 2026-03-28 老公指示：TP1 全平，直接移除
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"TP1 触发 {pos['symbol']}，全平移除")
                elif reason == "TP2":
                    positions.remove(pos)
                    save_positions(positions)
                    # 🐛 Bug 修复：TP2 平仓后立即同步 API，防止本地缓存残留
                    try:
                        api_positions = get_all_positions()
                        api_count = sum(1 for p in api_positions if float(p.get('amount', 0)) != 0)
                        if api_count == 0:
                            positions = []
                            save_positions(positions)
                            log.info("📊 TP2 平仓后同步：API 持仓为 0，清空本地缓存")
                        else:
                            log.info(f"📊 TP2 平仓后同步：API 还有{api_count}个持仓，保留本地缓存")
                    except Exception as e:
                        log.error(f"⚠️ TP2 平仓后同步失败：{e}")
                elif reason == "SL":
                    # 🚨 紧急修复：止损后也必须立即删除本地持仓！
                    positions.remove(pos)
                    save_positions(positions)
                    log.info(f"SL 触发 {pos['symbol']}，止损移除")
                    # 止损后同步 API，确保本地和 Binance 一致
                    try:
                        api_positions = get_all_positions()
                        api_count = sum(1 for p in api_positions if float(p.get('amount', 0)) != 0)
                        if api_count == 0:
                            positions = []
                            save_positions(positions)
                            log.info("📊 SL 平仓后同步：API 持仓为 0，清空本地缓存")
                        else:
                            log.info(f"📊 SL 平仓后同步：API 还有{api_count}个持仓，保留本地缓存")
                    except Exception as e:
                        log.error(f"⚠️ SL 平仓后同步失败：{e}")


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
                    api_count = sum(1 for p in current_pos if float(p.get("amount", 0)) != 0)
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

                        # 同一币种不能重复开仓
                        existing_same_symbol = [p for p in positions if p["symbol"] == sym]
                        if existing_same_symbol:
                            log.info(f"⚠️ {sym} 已有持仓，跳过重复开仓")
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
                                if pnl_pct < -0.01 and time.time() - last_trigger > 7200:
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
                            if conflict and ai_result.get("confidence", 0) > 70:
                                log.info(f"⏸️ {sym} AI与数学信号冲突，观望")
                                continue

                        # 开仓前再次检查持仓数（双重保险）
                        # 🐛 Bug 修复：API 是真相，本地是缓存
                        current_pos = get_all_positions()
                        api_count = sum(1 for p in current_pos if float(p.get("amount", 0)) != 0)
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
                hourly_report(positions, prices, indicators, circuit_breaker, fg_cache)
                last_report_hour = current_hour
                last_report_time = current_time  # 记录推送时间

            # 保存状态（包含 peak_pnl 用于移动止盈）
            save_state({
                "last_update": datetime.now().isoformat(),
                "prices":      {k: v.get("price") for k, v in prices.items()},
                "positions":   len(positions),
                "daily_pnl":   circuit_breaker.daily_pnl,
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


# ─────────────────────────────────────────────
    async def fallback_to_spot_api(self):
        """WebSocket 断连时，用现货 API 兜底获取价格（每30秒一次）"""
        import requests
        import time
        from datetime import datetime

        log.warning("⚠️ WebSocket 断连，启动现货 API 兜底...")
        while True:
            for sym in self.symbols:
                try:
                    # 使用 Clash 代理（7890）
                    resp = requests.get(
                        f'https://api.binance.com/api/v3/ticker/price?symbol={sym}USDT',
                        proxies={'https': 'http://127.0.0.1:7890', 'http': 'http://127.0.0.1:7890'},
                        timeout=5,
                        verify=False
                    )
                    data = resp.json()
                    price = float(data['price'])
                    self.prices[sym] = {
                        'price': price,
                        'ts': time.time(),
                        'source': 'spot_api_fallback'
                    }
                    log.info(f"✅ {sym} 价格已从现货 API 更新：${price:.6f}")
                except Exception as e:
                    log.error(f"❌ {sym} 现货 API 获取失败：{e}")
                    # Fallback to CoinGecko if Binance fails
                    try:
                        cg_resp = requests.get(
                            f'https://api.coingecko.com/api/v3/simple/price?ids={sym.lower()}&vs_currencies=usd',
                            timeout=5
                        )
                        cg_data = cg_resp.json()
                        price = cg_data.get(sym.lower(), {}).get('usd', 0)
                        if price > 0:
                            self.prices[sym] = {
                                'price': price,
                                'ts': time.time(),
                                'source': 'coingecko_fallback'
                            }
                            log.info(f"✅ {sym} 价格已从 CoinGecko 更新：${price:.6f}")
                    except Exception as ce:
                        log.error(f"❌ {sym} CoinGecko 备用也失败：{ce}")
            await asyncio.sleep(30)

if __name__ == "__main__":

    asyncio.run(main())
