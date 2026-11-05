#!/usr/bin/env python3
"""monitor 心跳超时后的进程级兜底平仓。不开新仓。

优先级
1. 心跳还新鲜时，出场只属于 rsi_fade：1h RSI 回到 50，或持仓满 24 小时。
   看门狗不发平仓单。进程刚被杀掉、心跳还没过期时也只重启 monitor，不市价平。
2. 心跳超过 HEARTBEAT_TIMEOUT_SEC（180 秒）没有更新，说明策略出场的主人
   已经死了或卡死。这时才对本地 rsi_fade 仓发 reduceOnly 市价单。
3. 同一 claim（优先 trade_id）成功过一次就写成 done。monitor 重启后看到
   done 或 inflight，不再发第二笔。交易所失败只记日志，并按指数退避再试。

为什么是看门狗市价平，而不是交易所常驻灾难止损
rsi_fade 没有固定价格止损。一张一直挂着的 STOP 会在 monitor 还活着的时候
先于「回 50 / 24h」成交，等于改了出场。条件单还会占 reduceOnly 额度，
测试网上已经因此出现过 -2022。市价平只在心跳超时之后发生，和正常出场错开。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

HEARTBEAT_TIMEOUT_SEC = 180.0
MAX_BACKOFF_SEC = 60.0

from config import SCRIPT_DIR

STATE_PATH = SCRIPT_DIR / "rsi_fade_flat_guard.json"
LOCK_PATH = SCRIPT_DIR / "rsi_fade_flat_guard.lock"
HEARTBEAT_PATH = SCRIPT_DIR / "crypto_state.json"
POSITIONS_PATH = SCRIPT_DIR / "crypto_positions.json"


def is_rsi_fade(pos: dict) -> bool:
    return pos.get("strategy") == "rsi_fade" or pos.get("regime") == "rsi"


def claim_key(pos: dict) -> str:
    trade_id = str(pos.get("trade_id") or "").strip()
    if trade_id:
        return "id:" + trade_id
    return "{symbol}:{side}:{entry}:{qty}".format(
        symbol=pos.get("symbol"),
        side=pos.get("type"),
        entry=pos.get("entry_price"),
        qty=pos.get("amount") or pos.get("qty"),
    )


def flat_client_id(key: str) -> str:
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return "flat" + digest


def should_emergency_flat(process_alive: bool, heartbeat_age: float | None, timeout: float = HEARTBEAT_TIMEOUT_SEC) -> bool:
    """心跳过期才平。刚死、心跳还新：交给重启后的 RSI 出场。"""
    if heartbeat_age is None:
        return not process_alive
    return heartbeat_age >= timeout


def heartbeat_age_sec(payload: dict | None, now: datetime) -> float | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("last_update")
    if not raw:
        return None
    try:
        then = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return (now - then).total_seconds()


def close_params(pos: dict, qty: float) -> dict:
    """只生成 reduceOnly 市价平仓参数。多单卖、空单买。"""
    side = "SELL" if str(pos.get("type")).upper() == "LONG" else "BUY"
    symbol = str(pos.get("symbol") or "")
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    key = claim_key(pos)
    return {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "reduceOnly": True,
        "quantity": qty,
        "newClientOrderId": flat_client_id(key),
    }


class FlatBook:
    """同一仓只允许一笔成功的 reduceOnly。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"claims": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"claims": {}}
        if not isinstance(data, dict) or not isinstance(data.get("claims"), dict):
            return {"claims": {}}
        return data

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def blocks(self, pos: dict) -> tuple[bool, str]:
        rec = self.load()["claims"].get(claim_key(pos))
        if not isinstance(rec, dict):
            return False, ""
        status = rec.get("status")
        if status in ("done", "inflight"):
            return True, str(status)
        return False, ""


def _backoff(attempts: int) -> float:
    step = min(MAX_BACKOFF_SEC, float(2 ** max(0, attempts - 1)))
    return step


def flatten_once(positions: list, exchange, book: FlatBook, now: float, log, lock_path: Path | None = None) -> list:
    """对 rsi_fade 仓各发一笔 reduceOnly。成功过的 claim 再进来不会再发。

    exchange.positions()：list，或带 error/rate_limited 的 dict。
    exchange.reduce_only(params) → {ok, order_id, reduce_only}.
    """
    lock_file = None
    if lock_path is not None:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "a")
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("兜底平仓锁被占用，本轮跳过，避免和另一侧同时下单")
            lock_file.close()
            return []
    try:
        return _flatten_locked(positions, exchange, book, now, log)
    finally:
        if lock_file is not None:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()


def _flatten_locked(positions, exchange, book: FlatBook, now: float, log) -> list:
    sent = []
    state = book.load()
    claims = state["claims"]
    api = exchange.positions()
    if isinstance(api, dict):
        log(f"兜底平仓查仓失败，本轮不发单：{api.get('error') or api.get('msg') or api}")
        return []
    by_symbol = {}
    for row in api or []:
        sym = row.get("symbol")
        if sym:
            by_symbol[sym] = row
    for pos in positions or []:
        if not isinstance(pos, dict) or not is_rsi_fade(pos):
            continue
        key = claim_key(pos)
        rec = claims.get(key) if isinstance(claims.get(key), dict) else {}
        if rec.get("status") == "done":
            continue
        if rec.get("status") == "retry" and float(rec.get("next_try") or 0) > now:
            continue
        ex = by_symbol.get(pos.get("symbol"))
        if ex is None:
            claims[key] = {"status": "done", "reason": "exchange_flat", "at": now}
            log(f"兜底平仓 {pos.get('symbol')} 交易所已无仓，记 done，不再发单")
            continue
        if str(ex.get("type")).upper() != str(pos.get("type")).upper():
            log(f"兜底平仓 {pos.get('symbol')} 方向和本地不一致，不下单")
            continue
        qty = abs(float(ex.get("amount") or 0))
        if qty <= 0:
            claims[key] = {"status": "done", "reason": "qty_zero", "at": now}
            continue
        params = close_params(pos, qty)
        if params.get("reduceOnly") is not True:
            log(f"兜底平仓拒绝非 reduceOnly 参数：{params}")
            continue
        claims[key] = {"status": "inflight", "at": now, "client_id": params["newClientOrderId"]}
        book.save(state)
        try:
            result = exchange.reduce_only(params) or {}
        except Exception as exc:
            result = {"ok": False, "reduce_only": True, "error": str(exc)}
        if result.get("ok") and result.get("reduce_only") is True:
            claims[key] = {
                "status": "done",
                "order_id": result.get("order_id"),
                "client_id": params["newClientOrderId"],
                "at": now,
            }
            sent.append({"key": key, "params": params, "order_id": result.get("order_id")})
            log(
                f"兜底平仓 {params['symbol']} {params['side']} qty={qty} "
                f"reduceOnly orderId={result.get('order_id')}"
            )
        else:
            attempts = int(rec.get("attempts") or 0) + 1
            delay = _backoff(attempts)
            claims[key] = {
                "status": "retry",
                "attempts": attempts,
                "next_try": now + delay,
                "at": now,
                "error": str(result.get("error") or "")[:200],
            }
            log(f"兜底平仓失败 {params['symbol']} 第{attempts}次，{delay:.0f}s 后再试：{result.get('error')}")
        book.save(state)
    book.save(state)
    return sent


def flat_claim_blocks(pos: dict, path: Path | None = None) -> tuple[bool, str]:
    return FlatBook(path or STATE_PATH).blocks(pos)


def production_tick(process_alive: bool, log, now: datetime | None = None, exchange=None) -> list:
    """给看门狗主循环用。心跳新鲜时立刻返回，不查仓、不下单。"""
    now = now or datetime.now()
    payload = None
    if HEARTBEAT_PATH.exists():
        try:
            payload = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    age = heartbeat_age_sec(payload, now)
    if not should_emergency_flat(process_alive, age):
        return []
    log(f"monitor 心跳超时 age={age}，只平 rsi_fade，禁止开仓")
    positions = []
    if POSITIONS_PATH.exists():
        try:
            loaded = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                positions = loaded
        except (OSError, json.JSONDecodeError) as exc:
            log(f"读本地持仓失败，本轮不平：{exc}")
            return []
    book = FlatBook(STATE_PATH)
    exch = exchange if exchange is not None else _LiveExchange()
    return flatten_once(positions, exch, book, now.timestamp(), log, LOCK_PATH)


class _LiveExchange:
    """只在心跳超时后被构造。reduce_only 恒为真。"""

    def positions(self):
        from binance_auto_trade import get_all_positions
        return get_all_positions()

    def reduce_only(self, params: dict) -> dict:
        from binance_auto_trade import _query_order_by_client_id, place_order
        if params.get("reduceOnly") is not True:
            return {"ok": False, "reduce_only": False, "error": "refused_open"}
        base = str(params["symbol"]).replace("USDT", "")
        result = place_order(
            symbol=params["symbol"],
            side=params["side"],
            quantity=params["quantity"],
            reduce_only=True,
            price=0,
            client_order_id=params.get("newClientOrderId"),
        )
        order = result.get("order") if isinstance(result, dict) else None
        order_id = order.get("orderId") if isinstance(order, dict) else None
        ok = bool(result.get("success")) if isinstance(result, dict) else False
        err = ""
        if isinstance(result, dict) and not ok:
            err = str(result.get("message") or result.get("error") or order or "")
            found = _query_order_by_client_id(params["symbol"], params.get("newClientOrderId"))
            if isinstance(found, dict) and found.get("orderId"):
                return {
                    "ok": True,
                    "order_id": found.get("orderId"),
                    "reduce_only": True,
                    "error": "",
                    "symbol": base,
                }
        return {"ok": ok, "order_id": order_id, "reduce_only": True, "error": err, "symbol": base}
