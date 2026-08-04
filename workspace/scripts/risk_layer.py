#!/usr/bin/env python3
"""
risk_layer.py — 风控层

职责：熔断器（CircuitBreaker）、信号冷却（CooldownManager）、新闻过滤（NewsFilter）。
依赖方向：仅依赖 config / notify_layer.write_alert（通用告警写入），禁止反向依赖其他业务层。
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from config import get_config
from notify_layer import write_alert

CONFIG = get_config()
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 八、熔断器
# ═══════════════════════════════════════════════════════════════
class CircuitBreaker:

    def __init__(self):
        self.daily_pnl   = 0.0
        self.reset_date  = datetime.now().date()
        self.paused_until: Optional[datetime] = None
        self.consecutive_losses = 0

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

            news_file = Path(CONFIG["news_file"])
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
