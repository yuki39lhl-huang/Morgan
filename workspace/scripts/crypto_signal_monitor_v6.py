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
import websockets
import numpy as np
from datetime import datetime, timedelta
from collections import deque
from typing import Optional

# 添加脚本目录到路径，以便导入 binance_auto_trade
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 自动交易模块导入
try:
    from binance_auto_trade import place_order, get_all_positions, close_position as api_close_position
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
    "symbols": ["ETH", "SOL", "BNB", "DOGE", "XRP"],

    # 资金管理
    "total_capital": 100,          # USDT（模拟盘）
    "position_size_pct": 0.25,     # 单仓25%
    "max_positions": 4,

    # 开仓阈值（按市场状态分层）
    "score_threshold": {
        "trending":  45,
        "ranging":   55,
        "volatile":  62,
    },

    # 基础止盈止损（ATR动态覆盖）
    "base_tp_pct":  0.06,
    "base_sl_pct":  0.03,

    # 双TP配置
    "tp1_atr_mult": 1.5,   # 第一目标：ATR×1.5，平50%仓
    "tp2_atr_mult": 3.0,   # 第二目标：ATR×3.0，平剩余

    # 移动止盈
    "trailing_trigger_pct": 0.045,  # 盈利4.5%启动
    "trailing_gap_pct":     0.015,  # 追踪距离1.5%

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
    "coingecko_base":  "https://pro-api.coingecko.com/api/v3",
    "coingecko_key":   "CG-DZoCE8UMF3FWpeYhBMvqGq4g",
    "proxy":           "http://127.0.0.1:8081",

    # Qwen AI
    "qwen_url":  "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "qwen_key":  "sk-168ecc496bc743aebd14209f618a8b4a",
    "qwen_model": "qwen-plus",

    # 文件路径
    "positions_file":     "crypto_positions.json",
    "state_file":         "crypto_state.json",
    "llm_usage_file":     "llm_usage.json",
    "position_highs_file":"position_highs.json",
    "log_file":           "crypto_monitor.log",
    "alert_file":         "crypto_alert.txt",

    # 扫描
    "scan_interval": 15,
}

# ─────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 一、WebSocket 实时价格流
# ═══════════════════════════════════════════════════════════════
class PriceStream:
    """
    Binance WebSocket 合并流，所有币种一个连接。
    每次收到推送立刻更新缓存，主循环直接读缓存，0 REST请求。
    """

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        streams = "/".join([f"{s.lower()}usdt@ticker" for s in symbols])
        self.url = f"wss://stream.binance.com:9443/stream?streams={streams}"
        self.prices: dict = {}
        self._running = False

    async def start(self):
        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.url, ping_interval=20) as ws:
                    log.info("WebSocket 价格流已连接")
                    async for raw in ws:
                        data = json.loads(raw)
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
            except Exception as e:
                log.warning(f"WebSocket 断线，5秒后重连: {e}")
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

    def _get_klines(self, symbol: str, interval: str = "15m", limit: int = 100) -> list:
        try:
            r = self.session.get(
                f"{CONFIG['binance_base']}/klines",
                params={"symbol": f"{symbol}USDT", "interval": interval, "limit": limit},
                timeout=8,
            )
            return r.json()
        except Exception as e:
            log.error(f"K线获取失败 {symbol}: {e}")
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
        klines_1h  = self._get_klines(symbol, "1h",  60)

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

    if atr_pct > 0.04:
        return "volatile"
    elif ema_slope > 0.002:
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
                    timeout=8,
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
    eth_ind: dict,
) -> tuple[int, str]:
    """
    返回 (score, direction)
    direction: 'LONG' | 'SHORT' | 'NONE'
    """
    if not ind or not price_data:
        return 0, "NONE"

    price      = price_data["price"]
    change_24h = price_data["change_24h"]
    vol        = price_data["volume"]

    rsi        = ind["rsi"]
    ema20_15m  = ind["ema20_15m"]
    ema60_15m  = ind["ema60_15m"]
    ema20_1h   = ind["ema20_1h"]
    ema60_1h   = ind["ema60_1h"]
    vol_avg    = ind["vol_avg20"]

    long_score  = 0
    short_score = 0

    # ① RSI（30分）
    if rsi < 40:
        long_score += 30
    elif rsi < 50:
        long_score += 15
    if rsi > 65:
        short_score += 30
    elif rsi > 55:
        short_score += 15

    # ② 24h 涨跌（20分）
    if change_24h < -2:
        long_score += 20
    elif change_24h < -1:
        long_score += 10
    if change_24h > 3:
        short_score += 20
    elif change_24h > 1.5:
        short_score += 10

    # ③ EMA 15分钟趋势（10分）
    if ema20_15m > ema60_15m:
        long_score += 10
    else:
        short_score += 10

    # ④ EMA 1小时确认（10分，多时间框架）
    if ema20_1h > ema60_1h:
        long_score += 10
    else:
        short_score += 10

    # ⑤ 成交量确认（10分）
    if vol > vol_avg:
        long_score  += 10
        short_score += 10

    # ⑥ ETH大盘趋势（10分）
    if eth_ind:
        eth_bull = eth_ind.get("ema20_15m", 0) > eth_ind.get("ema60_15m", 0)
        if eth_bull:
            long_score  += 10
            short_score -= 5
        else:
            short_score += 10
            long_score  -= 5

    # ⑦ 情绪加分（最多15分）
    sentiment_engine = SentimentEngine.__new__(SentimentEngine)
    long_score  += sentiment_engine.sentiment_score(symbol, "LONG",  fg, funding_rate)
    short_score += sentiment_engine.sentiment_score(symbol, "SHORT", fg, funding_rate)

    long_score  = max(0, long_score)
    short_score = max(0, short_score)

    if long_score > short_score and long_score >= CONFIG["score_threshold"][regime]:
        return long_score, "LONG"
    elif short_score > long_score and short_score >= CONFIG["score_threshold"][regime]:
        return short_score, "SHORT"

    return max(long_score, short_score), "NONE"


# ═══════════════════════════════════════════════════════════════
# 六、智能止盈止损计算（双TP + ATR分层）
# ═══════════════════════════════════════════════════════════════
def calc_tp_sl(
    entry: float,
    direction: str,
    ind: dict,
    price_data: dict,
) -> dict:
    """
    返回 {tp1, tp2, sl, tp1_hit, size_remaining}
    使用 ATR分层 + 支撑阻力 + 24h高低点 综合确定
    """
    atr       = ind["atr"]
    atr_pct   = ind["atr_pct"]
    support   = ind["support"]
    resistance= ind["resistance"]
    high_24h  = price_data["high_24h"]
    low_24h   = price_data["low_24h"]
    rsi       = ind["rsi"]

    # ATR止损倍数分层
    sl_mult = 2.0
    for threshold, mult in CONFIG["atr_sl_tiers"]:
        if atr_pct < threshold:
            sl_mult = mult
            break

    # RSI调整
    tp1_mult = CONFIG["tp1_atr_mult"]
    tp2_mult = CONFIG["tp2_atr_mult"]
    if direction == "LONG":
        if rsi > 70:   tp1_mult *= 0.8   # 超买保守止盈
        if rsi < 30:   tp2_mult *= 1.2   # 超卖激进止盈
    else:
        if rsi < 30:   tp1_mult *= 0.8
        if rsi > 70:   tp2_mult *= 1.2

    if direction == "LONG":
        tp1_raw = entry + atr * tp1_mult
        tp2_raw = entry + atr * tp2_mult
        sl_raw  = entry - atr * sl_mult

        # 24h高点 & 阻力位约束
        tp1 = min(tp1_raw, resistance, high_24h)
        tp2 = min(tp2_raw, resistance, high_24h)
        sl  = max(sl_raw,  support * 0.995, low_24h * 0.98)
    else:
        tp1_raw = entry - atr * tp1_mult
        tp2_raw = entry - atr * tp2_mult
        sl_raw  = entry + atr * sl_mult

        tp1 = max(tp1_raw, support, low_24h)
        tp2 = max(tp2_raw, support, low_24h)
        sl  = min(sl_raw,  resistance * 1.005, high_24h * 1.02)

    # 盈亏比检查（至少1.5:1）
    tp_space = abs(tp1 - entry)
    sl_space = abs(sl - entry)
    if sl_space > 0 and tp_space / sl_space < 1.5:
        # 扩大止盈
        if direction == "LONG":
            tp1 = entry + sl_space * 1.5
        else:
            tp1 = entry - sl_space * 1.5

    return {
        "tp1_price":      round(tp1, 4),
        "tp2_price":      round(tp2, 4),
        "sl_price":       round(sl, 4),
        "tp1_hit":        False,
        "size_remaining": 1.0,
        "peak_pnl":       0.0,   # 移动止盈用
    }


# ═══════════════════════════════════════════════════════════════
# 七、止盈止损检查（15秒必跑，极轻量）
# ═══════════════════════════════════════════════════════════════
def check_exits_fast(positions: list, prices: dict) -> list[tuple]:
    """
    返回 [(position, reason, close_size_ratio)]
    纯价格比较，无任何计算，<5ms
    """
    exits = []
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        tp1 = pos["tp1_price"]
        tp2 = pos["tp2_price"]
        sl  = pos["sl_price"]
        hit = pos.get("tp1_hit", False)
        typ = pos["type"]

        if typ == "LONG":
            if not hit and p >= tp1:
                exits.append((pos, "TP1", 0.5))
            elif hit and p >= tp2:
                exits.append((pos, "TP2", pos.get("size_remaining", 0.5)))
            elif p <= sl:
                exits.append((pos, "SL", pos.get("size_remaining", 1.0)))
        else:  # SHORT
            if not hit and p <= tp1:
                exits.append((pos, "TP1", 0.5))
            elif hit and p <= tp2:
                exits.append((pos, "TP2", pos.get("size_remaining", 0.5)))
            elif p >= sl:
                exits.append((pos, "SL", pos.get("size_remaining", 1.0)))

    return exits


def update_trailing_stop(positions: list, prices: dict):
    """
    移动止盈：盈利>4.5%后，止损跟踪最高点-1.5%
    只向有利方向移动
    """
    for pos in positions:
        sym = pos["symbol"]
        p   = prices.get(sym, {}).get("price")
        if not p:
            continue

        entry = pos["entry_price"]
        typ   = pos["type"]

        pnl_pct = (p - entry) / entry if typ == "LONG" else (entry - p) / entry

        # 更新峰值盈亏
        if pnl_pct > pos.get("peak_pnl", 0):
            pos["peak_pnl"] = pnl_pct

        if pos["peak_pnl"] < CONFIG["trailing_trigger_pct"]:
            continue

        # 移动止损
        gap = CONFIG["trailing_gap_pct"]
        if typ == "LONG":
            new_sl = p * (1 - gap)
            if new_sl > pos["sl_price"]:   # 只上移
                pos["sl_price"] = round(new_sl, 4)
        else:
            new_sl = p * (1 + gap)
            if new_sl < pos["sl_price"]:   # 只下移
                pos["sl_price"] = round(new_sl, 4)


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
            self.daily_pnl  = 0.0
            self.reset_date = today
        self.daily_pnl += pnl

    def is_trading_allowed(self) -> tuple[bool, str]:
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

    def can_open(self, symbol: str, direction: str) -> bool:
        now = time.time()
        same_key = f"{direction}_{symbol}"
        opp_dir  = "SHORT" if direction == "LONG" else "LONG"
        opp_key  = f"{opp_dir}_{symbol}"

        # 同向冷却
        if now - self._last.get(same_key, 0) < CONFIG["signal_cooldown"]["same_direction"]:
            return False

        # 反向无冷却（允许立即反向）
        self._last.pop(opp_key, None)
        return True

    def record(self, symbol: str, direction: str):
        self._last[f"{direction}_{symbol}"] = time.time()


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

    def predict(self, symbol: str, direction: str, ind: dict, price_data: dict) -> Optional[dict]:
        ok, reason = self._can_call()
        if not ok:
            log.info(f"AI跳过({reason})，使用纯数学决策")
            return None

        prompt = f"""你是加密货币量化交易专家，请基于以下数据给出交易建议。

币种: {symbol}USDT
当前价格: {price_data.get('price', 'N/A')}
RSI(14): {ind.get('rsi', 'N/A'):.1f}
EMA20(15m): {ind.get('ema20_15m', 'N/A'):.2f}
EMA60(15m): {ind.get('ema60_15m', 'N/A'):.2f}
EMA20(1h): {ind.get('ema20_1h', 'N/A'):.2f}
EMA60(1h): {ind.get('ema60_1h', 'N/A'):.2f}
ATR: {ind.get('atr', 'N/A'):.4f}
24h涨跌: {price_data.get('change_24h', 'N/A'):.2f}%
成交量/均量比: {price_data.get('volume', 0) / max(ind.get('vol_avg20', 1), 1):.2f}
数学信号: {direction}

注意：N/A表示数据暂缺，请降低该维度权重。

只返回JSON，不要任何解释：
{{"direction":"做多/做空/震荡","target_price":0.0,"confidence":0,"reasoning":"一句话"}}"""

        try:
            r = requests.post(
                CONFIG["qwen_url"],
                headers={
                    "Authorization": f"Bearer {CONFIG['qwen_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": CONFIG["qwen_model"],
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
                timeout=15,
            )
            content = r.json()["choices"][0]["message"]["content"]
            # 去除可能的markdown fence
            content = content.replace("```json", "").replace("```", "").strip()
            result = json.loads(content)
            self.call_times.append(time.time())
            self.last_call = time.time()
            self._save_usage()
            return result
        except Exception as e:
            log.error(f"AI预测失败: {e}")
            return None

    def _save_usage(self):
        try:
            data = {"calls_this_hour": len(self.call_times), "last_call": self.last_call}
            with open(CONFIG["llm_usage_file"], "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# 十一、持仓管理
# ═══════════════════════════════════════════════════════════════
def load_positions() -> list:
    try:
        with open(CONFIG["positions_file"]) as f:
            return json.load(f)
    except Exception:
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

    qty = (CONFIG["total_capital"] * size_pct) / entry_price

    # 实盘交易调用
    order_result = None
    if AUTO_TRADE_ENABLED:
        try:
            # 调用 binance_auto_trade 模块下单
            side = "BUY" if direction == "LONG" else "SELL"
            order_result = place_order(
                symbol=f"{symbol}USDT",
                side=side,
                position_side="BOTH",
                qty=round(qty, 6),
                tp_price=tp_sl.get("tp1_price"),
                sl_price=tp_sl.get("sl_price"),
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

    msg = (
        f"🟢 开多 {symbol}" if direction == "LONG" else f"🔴 开空 {symbol}"
    ) + f" @{entry_price} | 评分:{score} | 状态:{regime}"
    if ai_result:
        msg += f" | AI:{ai_result.get('direction')}({ai_result.get('confidence')}%)"
    if order_result and "error" in order_result:
        msg += f" | ❌ 下单失败"
    else:
        msg += f" | ✅ 下单成功"
    log.info(msg)
    write_alert(msg)
    return pos


def close_position(pos: dict, reason: str, size_ratio: float, current_price: float):
    entry = pos["entry_price"]
    typ   = pos["type"]
    symbol = pos["symbol"]
    qty = pos.get("qty", 0) * size_ratio

    # 实盘平仓调用
    close_result = None
    if AUTO_TRADE_ENABLED and size_ratio > 0:
        try:
            side = "SELL" if typ == "LONG" else "BUY"  # 平仓方向与持仓相反
            close_result = api_close_position(
                symbol=f"{symbol}USDT",
                side=side,
                qty=round(qty, 6),
            )
            log.info(f"📝 平仓结果：{close_result}")
        except Exception as e:
            log.error(f"❌ 平仓失败：{e}")
            close_result = {"error": str(e)}

    # 计算盈亏
    pnl_pct = (current_price - entry) / entry if typ == "LONG" else (entry - current_price) / entry
    pnl_usdt = CONFIG["total_capital"] * CONFIG["position_size_pct"] * pnl_pct * size_ratio

    emoji = "✅" if pnl_usdt >= 0 else "❌"
    msg = (
        f"{emoji} 平仓 {symbol} {reason} | "
        f"入场:{entry:.4f} 出场:{current_price:.4f} | "
        f"PnL:{pnl_pct*100:.2f}% ({pnl_usdt:+.2f}USDT) | "
        f"平{size_ratio*100:.0f}%仓"
    )
    if close_result and "error" in close_result:
        msg += f" | ❌ 平仓失败"
    else:
        msg += f" | ✅ 平仓成功"
    log.info(msg)
    write_alert(msg)
    return pnl_usdt


# ═══════════════════════════════════════════════════════════════
# 十二、新闻过滤
# ═══════════════════════════════════════════════════════════════
class NewsFilter:

    def __init__(self):
        self.suspended: dict = {}   # symbol → 解封时间

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
            log.info(f"📰 利好新闻: {title}")


# ═══════════════════════════════════════════════════════════════
# 十三、辅助工具
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
def hourly_report(positions: list, prices: dict, circuit: CircuitBreaker, fg: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
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
        lines.append(
            f"  {sym}: {pd.get('price',0):.4f} ({pd.get('change_24h',0):+.2f}%)"
        )
    lines.append("=" * 50)
    report = "\n".join(lines)
    log.info(report)
    write_alert(report)


# ═══════════════════════════════════════════════════════════════
# 十六、主循环
# ═══════════════════════════════════════════════════════════════
async def main():
    log.info("🚀 crypto_signal_monitor v6.0 启动")

    # 初始化各模块
    price_stream = PriceStream(CONFIG["symbols"])
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

    # 加载持仓
    positions = load_positions()

    last_report_hour = -1

    while True:
        loop_start = time.time()
        scheduler.tick_up()

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

                if reason == "TP1":
                    # 平50%，止损移至成本
                    pos["tp1_hit"]        = True
                    pos["size_remaining"] = 0.5
                    pos["sl_price"]       = pos["entry_price"]
                    log.info(f"TP1触发 {pos['symbol']}，止损移至成本，保留50%仓位")
                else:
                    positions.remove(pos)

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
                    eth_ind = indicators.get("ETH", {})

                    for sym in CONFIG["symbols"]:
                        if len(positions) >= CONFIG["max_positions"]:
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
                            sym, pd, ind, regime, fg_cache, fr, eth_ind
                        )

                        if direction == "NONE":
                            continue
                        if not cooldown_manager.can_open(sym, direction):
                            continue

                        # 已有同向持仓则跳过
                        existing = [p for p in positions if p["symbol"] == sym and p["type"] == direction]
                        if existing:
                            continue

                        # AI辅助预测
                        ai_result = ai_predictor.predict(sym, direction, ind, pd)

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

                        # 开仓
                        entry = pd["price"]
                        tp_sl = calc_tp_sl(entry, direction, ind, pd)
                        pos = open_position(sym, direction, entry, score, tp_sl, regime, ai_result)
                        positions.append(pos)
                        cooldown_manager.record(sym, direction)
                        save_positions(positions)

            # ──────────────────────────────────
            # Step 6: 整点汇报
            # ──────────────────────────────────
            current_hour = datetime.now().hour
            if scheduler.should("hourly_report") and current_hour != last_report_hour:
                hourly_report(positions, prices, circuit_breaker, fg_cache)
                last_report_hour = current_hour

            # 保存状态
            save_state({
                "last_update": datetime.now().isoformat(),
                "prices":      {k: v.get("price") for k, v in prices.items()},
                "positions":   len(positions),
                "daily_pnl":   circuit_breaker.daily_pnl,
                "fear_greed":  fg_cache,
                "tick":        scheduler.tick,
            })

        except Exception as e:
            log.error(f"主循环异常: {e}", exc_info=True)

        # 精确15秒间隔
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0, CONFIG["scan_interval"] - elapsed))


# ─────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
