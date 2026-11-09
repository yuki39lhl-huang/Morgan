#!/usr/bin/env python3
"""单币 RSI(14) 极值反向。回测和测试网试跑共用这一份阈值。

≥70 做空，≤30 做多。回到 50，或满 24 小时，就平。
数字写死。改 config 对不上这里就拒绝下单，不拿来调参。
"""
import json
from pathlib import Path

import numpy as np

RSI_N = 14
RSI_HIGH = 70.0
RSI_LOW = 30.0
RSI_MID = 50.0
RSI_HOLD_H = 24
HOUR_MS = 3_600_000
TRIAL_NOTIONAL = 200.0
TRIAL_MAX_CONCURRENT = 4

STATE_PATH = Path(__file__).resolve().parent / "rsi_fade_state.json"


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def wilder_rsi(close: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Wilder RSI(14)。小时不连续就重开一段，不跨缺口平滑。"""
    rsi = np.full(len(close), np.nan)
    period = RSI_N
    n = len(close)
    i = 0
    while i < n:
        end = i + 1
        while (
            end < n
            and int(times[end]) - int(times[end - 1]) == HOUR_MS
            and close[end] > 0
            and close[end - 1] > 0
        ):
            end += 1
        if end - i >= period + 1:
            gain = loss = 0.0
            for k in range(i + 1, i + 1 + period):
                d = float(close[k] - close[k - 1])
                gain += max(d, 0.0)
                loss += max(-d, 0.0)
            avg_gain = gain / period
            avg_loss = loss / period
            rsi[i + period] = _rsi_from_avgs(avg_gain, avg_loss)
            for k in range(i + period + 1, end):
                d = float(close[k] - close[k - 1])
                avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
                avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
                rsi[k] = _rsi_from_avgs(avg_gain, avg_loss)
        if end < n and not (close[end] > 0):
            i = end + 1
        else:
            i = end
    return rsi


def rsi_side(rsi: float):
    if not np.isfinite(rsi):
        return None
    if rsi >= RSI_HIGH:
        return "SHORT"
    if rsi <= RSI_LOW:
        return "LONG"
    return None


def rsi_back_to_mid(side: str, rsi: float) -> bool:
    if not np.isfinite(rsi):
        return False
    if side == "SHORT":
        return rsi <= RSI_MID
    if side == "LONG":
        return rsi >= RSI_MID
    return False


def closed_pairs(klines, now_ms: int) -> list[tuple[int, float]]:
    """只留已经收完的 1h。正在走的那根丢掉。"""
    rows = []
    for k in klines or []:
        open_ts = int(k[0])
        close_ts = int(k[6]) if len(k) > 6 else open_ts + HOUR_MS - 1
        if close_ts > int(now_ms):
            continue
        px = float(k[4])
        if px <= 0:
            continue
        rows.append((open_ts, px))
    rows.sort()
    return rows


def _arrays(series: list[tuple[int, float]]):
    opens = np.asarray([t for t, _ in series], dtype=np.int64)
    closes = np.asarray([p for _, p in series], dtype=np.float64)
    return opens, closes, wilder_rsi(closes, opens)


def evaluate_symbol(series, position, exited_open):
    """只看最后一根已收盘的 1h。position 是 {side, entry_open} 或 None。"""
    if len(series) < RSI_N + 2:
        return None
    opens, closes, rsi = _arrays(series)
    i = len(closes) - 1
    cur = float(rsi[i])
    open_ts = int(opens[i])
    price = float(closes[i])
    if position is not None:
        prev_open = int(opens[i - 1])
        continuous = int(opens[i]) - prev_open == HOUR_MS and np.isfinite(cur)
        if not continuous:
            return {
                "op": "exit",
                "reason": "gap",
                "side": position["side"],
                "price": float(closes[i - 1]),
                "open_ts": open_ts,
                "rsi": cur,
            }
        if rsi_back_to_mid(position["side"], cur):
            return {
                "op": "exit",
                "reason": "mid",
                "side": position["side"],
                "price": price,
                "open_ts": open_ts,
                "rsi": cur,
            }
        held_h = (open_ts - int(position["entry_open"])) / HOUR_MS
        if held_h >= RSI_HOLD_H:
            return {
                "op": "exit",
                "reason": "time",
                "side": position["side"],
                "price": price,
                "open_ts": open_ts,
                "rsi": cur,
            }
        return None
    if exited_open is not None and int(exited_open) == open_ts:
        return None
    side = rsi_side(cur)
    if side is None:
        return None
    return {
        "op": "enter",
        "reason": "rsi",
        "side": side,
        "price": price,
        "open_ts": open_ts,
        "rsi": cur,
    }


def _is_rsi_pos(pos: dict) -> bool:
    return pos.get("strategy") == "rsi_fade" or pos.get("regime") == "rsi"


def plan_actions(bars, positions, acted, symbols, max_concurrent, per_caps) -> list[dict]:
    """先平再开。平仓那一根不再开。同时持仓不超过 max_concurrent。"""
    held = {}
    for pos in positions or []:
        sym = pos.get("symbol")
        if sym:
            held[sym] = pos
    actions = []
    exiting = set()
    for sym in symbols:
        pos = held.get(sym)
        if not pos or not _is_rsi_pos(pos):
            continue
        series = bars.get(sym) or []
        entry_open = pos.get("entry_bar_open")
        if entry_open is None:
            entry_open = 0
        act = evaluate_symbol(
            series,
            {"side": pos.get("type"), "entry_open": int(entry_open)},
            None,
        )
        if act and act["op"] == "exit":
            act = dict(act)
            act["symbol"] = sym
            actions.append(act)
            exiting.add(sym)
    open_n = len(held) - len(exiting)
    for sym in symbols:
        if open_n >= max_concurrent:
            break
        if sym in held and sym not in exiting:
            continue
        if sym in exiting:
            continue
        if int(per_caps.get(sym, 0) or 0) <= 0:
            continue
        series = bars.get(sym) or []
        if not series:
            continue
        open_ts = int(series[-1][0])
        if int(acted.get(sym) or -1) == open_ts:
            continue
        act = evaluate_symbol(series, None, None)
        if not act or act["op"] != "enter":
            continue
        act = dict(act)
        act["symbol"] = sym
        actions.append(act)
        open_n += 1
        held[sym] = {"symbol": sym, "strategy": "rsi_fade"}
    return actions


def rules_match(spec: dict) -> bool:
    """配置必须和写死的规则一致，否则拒绝下单。"""
    if not isinstance(spec, dict):
        return False
    try:
        return (
            int(spec.get("period")) == RSI_N
            and float(spec.get("high")) == RSI_HIGH
            and float(spec.get("low")) == RSI_LOW
            and float(spec.get("mid")) == RSI_MID
            and int(spec.get("max_hold_hours")) == RSI_HOLD_H
            and float(spec.get("notional_usdt")) == TRIAL_NOTIONAL
            and int(spec.get("max_concurrent")) == TRIAL_MAX_CONCURRENT
            and spec.get("enabled") is True
        )
    except (TypeError, ValueError):
        return False


def testnet_ok(cfg: dict) -> bool:
    """试跑只允许币安合约测试网。主网地址直接拒绝。"""
    if not isinstance(cfg, dict) or not cfg.get("binance_testnet"):
        return False
    auto = str(cfg.get("binance_auto_base") or "")
    fut = str(cfg.get("binance_futures") or "")
    if "testnet.binancefuture.com" not in auto or "testnet.binancefuture.com" not in fut:
        return False
    if "fapi.binance.com" in auto or "fapi.binance.com" in fut:
        return False
    return True


def load_state(path: Path | None = None) -> dict:
    path = path or STATE_PATH
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"acted": {}}
    if not isinstance(data, dict):
        return {"acted": {}}
    acted = data.get("acted") or {}
    if not isinstance(acted, dict):
        acted = {}
    return {"acted": acted}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.write_text(json.dumps({"acted": state.get("acted") or {}}, ensure_ascii=False))
