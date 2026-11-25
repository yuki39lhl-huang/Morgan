"""Record RSI entry alternatives and their next-hour outcome without trading."""

import argparse
import asyncio
import json
import logging
import math
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

import numpy as np

import trade_db
from ai_layer import news_for_symbol
from config import CONFIG
from rsi_fade import HOUR_MS, RSI_N, closed_pairs, rsi_side, wilder_rsi

log = logging.getLogger(__name__)
MAX_SIGNAL_AGE_MS = 3 * 60_000
NEWS_WINDOW_MS = 6 * HOUR_MS
CST = timezone(timedelta(hours=8))


def _finite(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _market_inputs(symbol, rows, closes, bar_open, fear_greed):
    opens = np.array(sorted(closes), dtype=np.int64)
    prices = np.array([closes[ts] for ts in opens])
    if len(prices) < RSI_N + 2 or not np.isfinite(prices).all():
        return None
    rsi_series = wilder_rsi(prices, opens)
    rsi = float(rsi_series[-1])
    side = rsi_side(rsi)
    if side is None:
        return None
    recent = {int(row[0]): row for row in rows}
    volumes = [_finite(recent[ts][5]) if len(recent[ts]) > 5 else None for ts in opens[-7:]]
    volume_ratio = None
    if all(v is not None and v >= 0 for v in volumes):
        average = sum(volumes[:-1]) / len(volumes[:-1])
        if average > 0:
            volume_ratio = volumes[-1] / average
    return {
        "symbol": symbol,
        "side": side,
        "signal_ms": bar_open + HOUR_MS,
        "close": float(prices[-1]),
        "rsi": rsi,
        "previous_rsi": _finite(rsi_series[-2]),
        "return_1h_pct": float((prices[-1] / prices[-2] - 1) * 100),
        "return_3h_pct": float((prices[-1] / prices[-4] - 1) * 100),
        "volume_vs_previous_6h": volume_ratio,
        "fear_greed": _finite(fear_greed),
    }


def _available_news(conn, cutoff_ms, symbol):
    cutoff = datetime.fromtimestamp(cutoff_ms / 1000, CST)
    earliest = cutoff - timedelta(hours=6)
    rows = conn.execute(
        """SELECT id, published, observed_ms, source, title, url, sentiment FROM news
           WHERE observed_ms <= ? AND observed_ms >= ? AND published >= ? AND published <= ?
           ORDER BY published DESC, observed_ms DESC, id DESC LIMIT 80""",
        (cutoff_ms, cutoff_ms - NEWS_WINDOW_MS, earliest.strftime("%Y-%m-%d %H:%M"),
         cutoff.strftime("%Y-%m-%d %H:%M")),
    )
    return news_for_symbol(symbol, [dict(row) for row in rows])


def _funding(conn, symbol, cutoff_ms):
    # market_ext timestamps were recorded in the host's local timezone.
    cutoff = datetime.fromtimestamp(cutoff_ms / 1000)
    row = conn.execute(
        """SELECT ts, funding FROM market_ext WHERE symbol = ? AND ts >= ? AND ts <= ?
           AND funding IS NOT NULL ORDER BY ts DESC LIMIT 1""",
        (symbol, (cutoff - timedelta(hours=2)).isoformat(), cutoff.isoformat()),
    ).fetchone()
    return {"source": "OKX", "timestamp": row["ts"], "rate": _finite(row["funding"])} if row else None


def _simple_rule(market):
    direction = 1 if market["side"] == "LONG" else -1
    still_moving = all(direction * market[key] < 0 for key in ("return_1h_pct", "return_3h_pct"))
    return "wait_one_bar" if still_moving else "enter_now"


def _model_result(predictor, market, news=None):
    try:
        return {"status": "ok", **predictor.assess_rsi_entry(market, news)}
    except Exception as exc:
        log.warning("RSI 影子模型失败 %s (%s)", market["symbol"], type(exc).__name__)
        return {"status": "unavailable"}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def record_batch(predictor, snapshots, observed_ms, fear_greed):
    for symbol, rows in snapshots.items():
        closes = dict(closed_pairs(rows, observed_ms))
        if not closes:
            continue
        bar_open = max(closes)
        rows_by_open = {int(row[0]): row for row in rows}
        with closing(trade_db.connect()) as conn, conn:
            for pending in conn.execute(
                """SELECT signal_ms FROM rsi_entry_shadow WHERE symbol = ?
                   AND next_bar_json IS NULL AND signal_ms <= ? AND signal_ms >= ?""",
                (symbol, bar_open, min(closes)),
            ).fetchall():
                start = pending["signal_ms"]
                if start in closes:
                    row = rows_by_open[start]
                    high, low = _finite(row[2]), _finite(row[3])
                    if high is None or low is None or not 0 < low <= closes[start] <= high:
                        continue
                    outcome = {"open_ms": start, "high": high, "low": low, "close": closes[start]}
                    conn.execute(
                        "UPDATE rsi_entry_shadow SET next_bar_json = ? WHERE symbol = ? AND signal_ms = ?",
                        (_dump(outcome), symbol, start),
                    )
            signal_ms = bar_open + HOUR_MS
            if not 0 <= observed_ms - signal_ms <= MAX_SIGNAL_AGE_MS:
                continue
            market = _market_inputs(symbol, rows, closes, bar_open, fear_greed)
            if market is None:
                continue
            market["funding"] = _funding(conn, symbol, signal_ms)
            news = _available_news(conn, signal_ms, symbol)
            inputs = {
                "market": market, "news": news, "observed_ms": observed_ms,
                "model": CONFIG.get("deepseek_model"),
                "baseline": "enter_now", "market_rule": _simple_rule(market),
            }
            claimed = conn.execute(
                """INSERT OR IGNORE INTO rsi_entry_shadow (symbol, bar_open_ms, signal_ms, inputs_json)
                   VALUES (?, ?, ?, ?)""",
                (symbol, bar_open, signal_ms, _dump(inputs)),
            ).rowcount
        if not claimed:
            continue
        result = {
            "market_ai": _model_result(predictor, market),
            "news_ai": _model_result(predictor, market, news) if news else {"status": "no_news"},
            "completed_ms": time.time_ns() // 1_000_000,
        }
        with closing(trade_db.connect()) as conn, conn:
            conn.execute(
                "UPDATE rsi_entry_shadow SET result_json = ? WHERE symbol = ? AND bar_open_ms = ?",
                (_dump(result), symbol, bar_open),
            )


class RSIEntryShadow:
    """Schedule at most one background batch, using only existing market caches."""

    def __init__(self, predictor):
        self.predictor = predictor
        self._last_bar = {}
        self._task = None

    def schedule(self, price_stream, symbols, fear_greed):
        if self._task is not None and not self._task.done():
            return
        observed_ms = time.time_ns() // 1_000_000
        snapshots = {}
        bar_opens = {}
        for symbol in symbols:
            rows = price_stream.closed_klines(symbol)
            if not rows:
                continue
            latest = int(rows[-1][0])
            # A final candle may arrive before the local clock reaches its signal time.
            # Leave it unmarked so the next poll can record it.
            if observed_ms < latest + HOUR_MS:
                continue
            if latest > self._last_bar.get(symbol, -1):
                snapshots[symbol] = rows
                bar_opens[symbol] = latest
        if snapshots:
            self._task = asyncio.create_task(self._record(snapshots, bar_opens, observed_ms, fear_greed))

    async def _record(self, snapshots, bar_opens, observed_ms, fear_greed):
        try:
            await asyncio.to_thread(record_batch, self.predictor, snapshots, observed_ms, fear_greed)
        except Exception:
            log.exception("RSI 影子记录失败")
        else:
            self._last_bar.update(bar_opens)


def summarize(conn, adverse_pct):
    counts = {
        label: {"enter": 0, "wait": 0, "skip": 0, "entered_adverse": 0, "missed_favorable": 0}
        for label in ("触线即进", "纯行情规则", "纯行情 AI", "行情加新闻 AI")
    }
    total = paired = 0
    rows = conn.execute("SELECT signal_ms, inputs_json, result_json, next_bar_json FROM rsi_entry_shadow")
    for row in rows:
        total += 1
        if not row["result_json"] or not row["next_bar_json"]:
            continue
        results = json.loads(row["result_json"])
        if results["completed_ms"] >= row["signal_ms"] + HOUR_MS:
            continue
        if any(results[key]["status"] != "ok" for key in ("market_ai", "news_ai")):
            continue
        inputs = json.loads(row["inputs_json"])
        market = inputs["market"]
        outcome = json.loads(row["next_bar_json"])
        long = market["side"] == "LONG"
        adverse_move = 1 - outcome["low"] / market["close"] if long else outcome["high"] / market["close"] - 1
        favorable_close = outcome["close"] > market["close"] if long else outcome["close"] < market["close"]
        actions = (
            inputs["baseline"], inputs["market_rule"],
            results["market_ai"]["action"], results["news_ai"]["action"],
        )
        for stats, action in zip(counts.values(), actions):
            if action == "enter_now":
                stats["enter"] += 1
                stats["entered_adverse"] += adverse_move >= adverse_pct / 100
            else:
                stats["wait" if action == "wait_one_bar" else "skip"] += 1
                stats["missed_favorable"] += favorable_close
        paired += 1
    return total, paired, counts


def main():
    parser = argparse.ArgumentParser(description="只读对照 RSI 影子信号和下一小时走势")
    parser.add_argument("--adverse-pct", type=float, default=1.0, help="显著逆行的价格幅度百分比")
    args = parser.parse_args()
    if not math.isfinite(args.adverse_pct) or args.adverse_pct <= 0:
        parser.error("--adverse-pct 要求为正数")
    with closing(trade_db.connect(readonly=True)) as conn:
        total, paired, counts = summarize(conn, args.adverse_pct)
    print(f"触线信号 {total} 笔，模型与下一小时数据齐全的同样本 {paired} 笔")
    print(f"方案 | 立即进 | 等一根 | 跳过 | 立即进遇逆行 ≥{args.adverse_pct:g}% | 等或跳后同向收盘")
    for label, stats in counts.items():
        print(label, *stats.values(), sep=" | ")


if __name__ == "__main__":
    main()
